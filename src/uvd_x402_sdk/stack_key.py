"""``X-UVD-Stack-Key``: the credential an Ultravioleta DAO service presents to
the facilitator.

The facilitator exempts the services of Ultravioleta DAO's own stack from its
POLICY ``429``s (the per-address budgets) when a request carries a key it
knows. The contract is all this module knows about it:

* every request to the facilitator carries the header ``X-UVD-Stack-Key``;
* the key is ``uvdsk_`` followed by 43 to 128 base64url characters
  (``uvdsk_`` and 32 random bytes in base64url give 43);
* the facilitator compares its SHA-256 in constant time. One that does not
  know the header ignores it, and on a route it does not govern the header
  does nothing.

It is not for third parties: Ultravioleta DAO issues a key to each of its own
services, and a client without one pays exactly as before.

The key only travels to a facilitator of Ultravioleta DAO: ``https`` to
``facilitator.ultravioletadao.xyz`` (:data:`DEFAULT_STACK_KEY_HOSTS`) or to a
host the caller adds with ``stack_key_hosts``. Anything else (another host,
plain ``http``, a facilitator that ``facilitator_by_network`` routes to a third
party) gets no header, and a warning says so once per process, naming the host
and nothing about the key. Plain ``http`` is accepted only to ``127.0.0.1`` or
``localhost``, and only when ``stack_key_hosts`` names it: a local stand-in for
tests. The host is the one the HTTP library will contact, read by that
library's own parser (httpx, and urllib3 for a ``requests`` session): a URL
they read as different hosts gets the key only if every reading is allowed.

The key does not follow redirects. A request that carries it is sent with
redirects off, whatever the HTTP client would do, and a redirect answered to
it raises :class:`~uvd_x402_sdk.exceptions.StackKeyRedirectError`; the key is
never sent again.

A key read badly must never break a payment. A value read from a file with a
byte order mark, or a trailing carriage return or line feed, is an invalid
header value: httpx raises ``LocalProtocolError`` before sending, with the
value in its message, so every ``/verify`` and ``/settle`` of the client would
fail and the key would land in the error. So a leading U+FEFF and the
whitespace at both ends are removed, the rest is checked against the format,
and a value that does not match is not sent: the request goes as a third
party's, the payment goes through, and a warning says so once per process,
without the value. The key never appears in an error, a log, a repr, a pickle
or a ``dataclasses.asdict``.

Without a key to send, a request is made exactly as before this existed: no
``headers`` or redirect keyword is added where there was none.
"""

import logging
import re
from collections.abc import Iterable, Mapping
from typing import Any, Optional, Union

import httpx

from uvd_x402_sdk.exceptions import StackKeyRedirectError

try:  # the parser of a ``requests`` session; urllib3 comes with requests
    from urllib3.util import parse_url as _urllib3_parse_url
except ImportError:  # pragma: no cover - only without urllib3
    _urllib3_parse_url = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

#: The header the facilitator reads the key from.
STACK_KEY_HEADER = "X-UVD-Stack-Key"

#: The environment variable :meth:`X402Config.from_env` reads the key from.
STACK_KEY_ENV = "UVD_STACK_KEY"

#: The facilitators of Ultravioleta DAO the key travels to, over ``https`` only.
#: ``stack_key_hosts`` adds to this list; it never removes from it.
DEFAULT_STACK_KEY_HOSTS = ("facilitator.ultravioletadao.xyz",)

#: The only hosts plain ``http`` may carry the key to, and only when listed.
_LOOPBACK_HOSTS = ("127.0.0.1", "localhost")

StackKeyHosts = Optional[Union[str, Iterable[str]]]

_STACK_KEY_FORMAT = re.compile(r"uvdsk_[A-Za-z0-9_-]{43,128}")

_BYTE_ORDER_MARK = "﻿"

# Set by the first warning of each kind: one per process, whatever the number
# of clients or requests.
_warned = False
_warned_host = False


class _StackKey(str):
    """A usable key: the ``str`` a header needs, which no dump carries.

    ``repr`` hides it, so a ``repr``, ``vars()`` or ``dataclasses.asdict()`` of
    whatever holds it does not show it; pickling or deep-copying it gives
    ``None``, so ``pickle.dumps(config)``, ``copy.deepcopy(config)`` and
    ``dataclasses.asdict(config)`` carry no key (configure it again where the
    copy is used). ``str(key)`` is the plain value.
    """

    __slots__ = ()

    def __repr__(self) -> str:
        return "'<X-UVD-Stack-Key>'"

    def __reduce_ex__(self, protocol: Any) -> Any:
        return (_no_stack_key, ())


def _no_stack_key() -> None:
    return None


def usable_stack_key(value: object) -> Optional[str]:
    """The key to send for ``value``, or ``None`` when nothing is sent.

    ``None`` and a blank string mean no key was configured. Any other value
    loses a leading U+FEFF and the whitespace at both ends and must then match
    the format; one that does not (or is not a string) gives ``None`` and a
    warning, the first time only, that never carries the value. Never raises.
    """
    if value is None:
        return None
    if isinstance(value, str):
        key = value.strip()
        if key.startswith(_BYTE_ORDER_MARK):
            key = key[1:].strip()
        if not key:
            return None
        if _STACK_KEY_FORMAT.fullmatch(key):
            return _StackKey(key)
    _warn_unusable()
    return None


def stack_key_allowed(url: Union[str, httpx.URL], hosts: StackKeyHosts = None) -> bool:
    """Whether the key may travel to ``url``.

    ``https`` to a host of :data:`DEFAULT_STACK_KEY_HOSTS` or of ``hosts``;
    plain ``http`` only to ``127.0.0.1`` or ``localhost`` named in ``hosts``.
    The host is the one httpx will contact (``httpx.URL``), lowercased and
    without a final dot, compared whole: no prefix, no subdomain, no port.
    When urllib3 (a ``requests`` session) reads another host in the same URL,
    that reading must be allowed too. Never raises.
    """
    readings = _readings(url)
    if not readings:
        return False
    listed = _listed(hosts)
    return all(_reading_allowed(scheme, host, listed) for scheme, host in readings)


def stack_key_headers(
    value: object, url: Union[str, httpx.URL], hosts: StackKeyHosts = None
) -> dict[str, str]:
    """``{"X-UVD-Stack-Key": key}`` for a request to ``url``, or ``{}``.

    ``{}`` when ``value`` is no usable key, or when the key may not travel to
    ``url`` (:func:`stack_key_allowed`).
    """
    key = usable_stack_key(value)
    if key is None:
        return {}
    if not stack_key_allowed(url, hosts):
        _warn_host(url)
        return {}
    return {STACK_KEY_HEADER: str(key)}


def no_redirect_kwargs(
    headers: Optional[Mapping[str, str]], client: object = None
) -> dict[str, Any]:
    """The keyword that keeps ``client`` from following a redirect, when
    ``headers`` carry the key; ``{}`` otherwise.

    ``allow_redirects=False`` for a ``requests`` session, ``follow_redirects=False``
    for httpx (a client, or the module's functions when ``client`` is None).
    Per request, so it holds whatever the caller's client was built with.
    """
    if not headers or STACK_KEY_HEADER not in headers:
        return {}
    if type(client).__module__.split(".")[0] == "requests":
        return {"allow_redirects": False}
    return {"follow_redirects": False}


def stack_key_request_kwargs(
    value: object,
    url: Union[str, httpx.URL],
    hosts: StackKeyHosts = None,
    headers: Optional[dict[str, str]] = None,
    client: object = None,
) -> dict[str, Any]:
    """The keywords of a request to ``url``: ``headers`` and the key, and
    redirects off when the key is among them.

    Empty when there is nothing to send, so that a request without a key is
    made exactly as before (a caller's double of the HTTP client that knows no
    ``headers`` keyword keeps working).
    """
    merged = {**(headers or {}), **stack_key_headers(value, url, hosts)}
    if not merged:
        return {}
    return {"headers": merged, **no_redirect_kwargs(merged, client)}


def refuse_redirect(response: Any, operation: Optional[str] = None) -> None:
    """Raise :class:`~uvd_x402_sdk.exceptions.StackKeyRedirectError` when
    ``response`` answers a request that carried the key with a redirect.

    That request went with redirects off, so nothing followed it; this makes
    the redirect an error instead of an answer, and nothing is sent again. A
    request without the key, or an answer that is not a 3xx, passes.
    """
    status = getattr(response, "status_code", None)
    if not isinstance(status, int) or not 300 <= status < 400:
        return
    try:
        sent = response.request.headers
    except Exception:  # noqa: BLE001 - a double without the request it answered
        return
    if sent is not None and STACK_KEY_HEADER in sent:
        raise StackKeyRedirectError(status, operation)


async def refuse_redirect_hook(response: httpx.Response) -> None:
    """:func:`refuse_redirect` as a response hook of an ``httpx.AsyncClient``."""
    refuse_redirect(response)


def _readings(url: Union[str, httpx.URL]) -> list[tuple[str, str]]:
    """``(scheme, host)`` as each library the SDK sends through reads ``url``:
    httpx, and urllib3 when installed. Empty when one of them cannot read it."""
    try:
        parsed = httpx.URL(url)
    except Exception:  # noqa: BLE001 - unreadable = no key
        return []
    readings = [(parsed.scheme, parsed.host)]
    if _urllib3_parse_url is not None:
        try:
            other = _urllib3_parse_url(str(url))
        except Exception:  # noqa: BLE001
            return []
        readings.append((other.scheme or "", other.host or ""))
    return readings


def _normal_host(host: str) -> str:
    host = host.strip("[]").lower()
    return host[:-1] if host.endswith(".") else host


def _reading_allowed(scheme: str, host: str, listed: frozenset) -> bool:
    host = _normal_host(host)
    if not host:
        return False
    scheme = scheme.lower()
    if scheme == "https":
        return host in DEFAULT_STACK_KEY_HOSTS or host in listed
    if scheme == "http":
        return host in _LOOPBACK_HOSTS and host in listed
    return False


def _listed(hosts: StackKeyHosts) -> frozenset:
    if hosts is None:
        return frozenset()
    if isinstance(hosts, str):
        hosts = [hosts]
    return frozenset(_normal_host(h.strip()) for h in hosts if isinstance(h, str))


def _origin(url: Union[str, httpx.URL]) -> str:
    """``scheme://host[:port]`` of ``url`` as httpx reads it: never its user
    info or its path."""
    try:
        parsed = httpx.URL(url)
    except Exception:  # noqa: BLE001
        return "an unparseable URL"
    if not parsed.host:
        return "a URL without a host"
    port = f":{parsed.port}" if parsed.port else ""
    return f"{parsed.scheme}://{parsed.host}{port}"


def _warn_unusable() -> None:
    global _warned
    if _warned:
        return
    _warned = True
    logger.warning(
        "The configured stack key (stack_key / %s) is not a well-formed key: "
        "uvdsk_ followed by 43 to 128 base64url characters. %s is NOT sent, so "
        "the facilitator treats these requests as a third party's; payments work "
        "as without a key. Look for quotes or whitespace inside the value.",
        STACK_KEY_ENV,
        STACK_KEY_HEADER,
    )


def _warn_host(url: Union[str, httpx.URL]) -> None:
    global _warned_host
    if _warned_host:
        return
    _warned_host = True
    logger.warning(
        "stack key not sent: %s is not a house facilitator. %s travels only over "
        "https to %s or to a host added with stack_key_hosts; this request goes "
        "as a third party's.",
        _origin(url),
        STACK_KEY_HEADER,
        ", ".join(DEFAULT_STACK_KEY_HOSTS),
    )
