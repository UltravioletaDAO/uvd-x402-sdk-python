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
tests.

A key read badly must never break a payment. A value read from a file with a
trailing carriage return or line feed is an invalid header value: httpx raises
``LocalProtocolError`` before sending, with the value in its message, so every
``/verify`` and ``/settle`` of the client would fail and the key would land in
the error. The value is therefore stripped and checked against the format, and
one that does not match is not sent: the request goes as a third party's, the
payment goes through, and a warning says so once per process, without the
value. The key never appears in an error, a log, a repr or a message.

Without a key to send, a request is made exactly as before this existed: no
``headers`` keyword is added where there was none.
"""

import logging
import re
from collections.abc import Iterable
from typing import Any, Optional, Union
from urllib.parse import urlsplit

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

# Set by the first warning of each kind: one per process, whatever the number
# of clients or requests.
_warned = False
_warned_host = False


def usable_stack_key(value: object) -> Optional[str]:
    """The key to send for ``value``, or ``None`` when nothing is sent.

    ``None`` and a blank string mean no key was configured. Any other value is
    stripped and must match the format; one that does not (or is not a string)
    gives ``None`` and a warning, the first time only, that never carries the
    value. Never raises.
    """
    if value is None:
        return None
    if isinstance(value, str):
        key = value.strip()
        if not key:
            return None
        if _STACK_KEY_FORMAT.fullmatch(key):
            return key
    _warn_unusable()
    return None


def stack_key_allowed(url: str, hosts: StackKeyHosts = None) -> bool:
    """Whether the key may travel to ``url``.

    ``https`` to a host of :data:`DEFAULT_STACK_KEY_HOSTS` or of ``hosts``;
    plain ``http`` only to ``127.0.0.1`` or ``localhost`` named in ``hosts``.
    Hosts compare whole and case-insensitively: no prefix, no subdomain, no
    port. Never raises.
    """
    try:
        parts = urlsplit(url)
        host = parts.hostname
    except (TypeError, ValueError):
        return False
    if not host:
        return False
    listed = _listed(hosts)
    scheme = parts.scheme.lower()
    if scheme == "https":
        return host in DEFAULT_STACK_KEY_HOSTS or host in listed
    if scheme == "http":
        return host in _LOOPBACK_HOSTS and host in listed
    return False


def stack_key_headers(value: object, url: str, hosts: StackKeyHosts = None) -> dict[str, str]:
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
    return {STACK_KEY_HEADER: key}


def stack_key_request_kwargs(
    value: object,
    url: str,
    hosts: StackKeyHosts = None,
    headers: Optional[dict[str, str]] = None,
) -> dict[str, Any]:
    """The ``headers=`` keyword of a request to ``url``: ``headers`` and the key.

    Empty when there is nothing to send, so that a request without a key is
    made exactly as before (a caller's double of the HTTP client that knows no
    ``headers`` keyword keeps working).
    """
    merged = {**(headers or {}), **stack_key_headers(value, url, hosts)}
    return {"headers": merged} if merged else {}


def _listed(hosts: StackKeyHosts) -> frozenset:
    if hosts is None:
        return frozenset()
    if isinstance(hosts, str):
        hosts = [hosts]
    return frozenset(h.strip().lower() for h in hosts if isinstance(h, str))


def _origin(url: str) -> str:
    """``scheme://host[:port]`` of ``url``: never its user info or its path."""
    try:
        parts = urlsplit(url)
        host = parts.hostname
        port = parts.port
    except (TypeError, ValueError):
        return "an unparseable URL"
    if not host:
        return "a URL without a host"
    return f"{parts.scheme.lower()}://{host}" + (f":{port}" if port else "")


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


def _warn_host(url: str) -> None:
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
