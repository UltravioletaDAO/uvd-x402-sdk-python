"""NEP-413 HTTP request authentication for a NEAR account (offline signer).

Execution Market authenticates an agent that holds only a NEAR account with
the ERC-8128 envelope (RFC 9421 ``Signature-Input`` / ``Signature``) and a
NEP-413 signature instead of EIP-191. Only a ``keyid`` that starts with
``near:`` selects that profile on the server; the identity it proves is
``near:<account_id>``. This module builds the headers; it sends nothing and
reads no RPC.

Wire contract, measured on EM ``origin/main`` (``mcp_server/integrations/near/
http_auth.py`` and ``integrations/erc8128/verifier.py``) and pinned by EM's
public vector ``shared/test-vectors/near-http-auth.json`` (byte copy in
``tests/fixtures/near-http-auth.json``):

* label ``near``; covered components ``@method @authority @path``, then
  ``@query`` when the URL has a query string and ``content-digest`` when there
  is a body (``body is not None``, the empty body included);
* parameters in the order ``created;expires;nonce;keyid;alg`` with
  ``keyid="near:<account_id>:ed25519:<base58 public key>"`` and
  ``alg="ed25519-nep413"``;
* the signed bytes are NEP-413's: ``sha256(u32_le(2**31 + 413) ||
  borsh({message, nonce, recipient, callbackUrl}))`` with ``message`` = the
  exact RFC 9421 signature base, ``nonce`` = ``sha256(utf8(<RFC 9421 nonce>))``,
  ``recipient`` = the verifier's (``"execution.market"`` for EM) and no
  ``callbackUrl``; ``Signature: near=:<base64 of the 64 bytes>:``.

The base is built by :mod:`uvd_x402_sdk.erc8128.core`, the one place the RFC
9421 wire exists in this SDK; the NEP-413 bytes and the local check of the
returned signature are :func:`uvd_x402_sdk.near_nep413.sign_nep413`. Any
:class:`~uvd_x402_sdk.near_escrow.NearLocalSigner` signs: the in-memory
:class:`~uvd_x402_sdk.near_escrow.NearEd25519Signer` or
:class:`~uvd_x402_sdk.near_ows.OwsNearSigner`.

What the signature does NOT prove, and the verifier must: that the key is a
**FullAccess** key of ``account_id`` (EM reads ``view_access_key`` at
``finality: final`` on NEAR mainnet, for implicit accounts too), that the
nonce is unspent (EM consumes ``near:<account>:<nonce>`` before checking the
signature, so a retry, even after EM's ``503 near_rpc_unavailable``, needs a
NEW nonce and a new signature), that ``@authority`` is a host it serves, and
the time window (EM: ``expires - created <= 300``, 30 s of clock skew).

Every input is checked before the signer is touched. A URL is signed only
when EM rebuilds the signed bytes from what each measured HTTP client sends
for it: httpx 0.28, requests 2.34 (urllib3 2.8), urllib (CPython 3.11) and
aiohttp 3.14 (yarl 1.25), against EM's verifier. EM consumes the nonce before
it checks the signature, so a URL one of them rewrites costs a nonce and a 401.
The authority signed is the URL's ``host[:port]`` as written, which is what
every client that sends the URL puts in ``Host`` once it passes. Refused, with
the client that rewrites it:

* percent-encoded octets in the path (EM reads Starlette's DECODED path,
  whatever the client sent);
* dot segments (httpx, requests and aiohttp remove them);
* a port written empty or with leading zeros (httpx, requests and aiohttp
  rewrite it in ``Host``; urllib sends it as written, which no allowlist names);
* an upper-case host (httpx, requests and aiohttp lowercase it in ``Host``;
  urllib sends it as written) and an explicit default port (the same three drop
  it; urllib sends ``host:443``);
* a host that ends in ``.`` (requests and aiohttp send ``Host`` without it,
  httpx and urllib with it);
* an IPv6 literal other than ``ipaddress``'s compressed form of it (aiohttp
  sends that form: ``[0::1]`` as ``[::1]``, and on CPython 3.11.14
  ``[::ffff:7f00:1]`` as ``[::ffff:127.0.0.1]``; httpx and urllib send it as
  written, requests lowercased);
* in the query, a percent-encoded octet in lower-case hex (requests and aiohttp
  send it upper-case), one that encodes an unreserved character (both send it
  decoded) or one of ``/ ? : @ ! $ ' ( ) * ,`` (aiohttp sends it decoded).
  Written as the character itself, each means the same to a form parser and
  every client sends it unchanged; ``%26 %2B %3B %3D %25 %23 %20`` and
  non-ASCII octets in upper-case hex pass;
* brackets outside the host and a ``%`` that starts no octet (requests and
  aiohttp re-encode both).

A client may instead refuse a URL outright, which sends nothing and spends no
nonce: requests refuses a host that starts with ``.`` or ``*``.

Also refused, as policy rather than a measurement: an upper-case scheme,
anything after an IPv6 host but an optional ``:port`` (CPython 3.9.6's
``urlsplit`` accepts ``[::1]443``), a ``%`` in the host, user info, a fragment
(no client sends it) and any character outside RFC 3986.
"""

from __future__ import annotations

import hashlib
import ipaddress
import re
import time
from urllib.parse import urlsplit

from .erc8128.core import (
    CanonicalMessage,
    build_signature_base,
    build_signature_params,
    canonical_params,
    compute_content_digest,
    select_covered,
)
from .near_escrow import NearLocalSigner, _account
from .near_nep413 import sign_nep413
from .solana_signing import _b58encode

#: The ``alg`` parameter of the NEAR profile.
NEAR_HTTP_AUTH_ALG = "ed25519-nep413"

#: The signature label EM's signer and vector use.
NEAR_HTTP_AUTH_LABEL = "near"

#: EM's ``VerifyPolicy.max_validity_sec``: a wider window is refused there.
NEAR_HTTP_AUTH_MAX_VALIDITY_SEC = 300

#: EM refuses a nonce longer than this (characters).
NEAR_HTTP_AUTH_MAX_NONCE_LENGTH = 256

#: The NEP-413 ``recipient`` EM fixes for every NEAR proof (never from a request).
EXECUTION_MARKET_NEP413_RECIPIENT = "execution.market"

# RFC 9110 token.
_METHOD_RE = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+")
# RFC 3986 characters only.
_URL_RE = re.compile(r"[A-Za-z0-9._~:/?#\[\]@!$&'()*+,;=%-]+")
_BAD_PERCENT_RE = re.compile(r"%(?![0-9A-Fa-f]{2})")
_PERCENT_OCTET_RE = re.compile(r"%([0-9A-Fa-f]{2})")
# Sent decoded when percent-encoded in the query: the unreserved characters by
# requests and aiohttp, these reserved ones by aiohttp (yarl) as well.
_QUERY_DECODED_BY_CLIENTS = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~/?:@!$'()*,"
)
# httpx, requests and aiohttp rewrite `:0443`, `:` and `:08443` in Host, urllib does not.
_PORT_RE = re.compile(r"[1-9][0-9]{0,4}")
_DEFAULT_PORTS = {"http": 80, "https": 443}
# A bracketed host, then nothing or `:port` (CPython 3.9.6's urlsplit accepts `[::1]443`).
_IPV6_NETLOC_RE = re.compile(r"\[([0-9A-Fa-f:.]+)\](?::[^:]*)?")
# EM's own nonces are secrets.token_urlsafe; base64, hex and UUIDs also fit.
# No quote, backslash or space: EM writes the value back into the base verbatim.
_NONCE_RE = re.compile(rf"[A-Za-z0-9._~+/=-]{{1,{NEAR_HTTP_AUTH_MAX_NONCE_LENGTH}}}")
# Keep every emitted integer a safe JS integer (TS twin).
_MAX_TIMESTAMP = 2**53 - 1

_HEADER_NAMES = {
    "title": ("Signature", "Signature-Input", "Content-Digest"),
    "lower": ("signature", "signature-input", "content-digest"),
}


def _compressed_ipv6_netloc(netloc: str) -> bool:
    # aiohttp (yarl) sends the literal as this Python's ipaddress compresses it;
    # the other clients send it as written (requests lowercased).
    match = _IPV6_NETLOC_RE.fullmatch(netloc)
    if match is None:
        return False
    try:
        return ipaddress.IPv6Address(match.group(1)).compressed == match.group(1)
    except ValueError:
        return False


def _url_parts(url: object) -> tuple:
    if not isinstance(url, str) or _URL_RE.fullmatch(url) is None:
        raise ValueError("url: expected an absolute http(s) URL of RFC 3986 ASCII characters")
    if _BAD_PERCENT_RE.search(url) is not None:
        raise ValueError("url: '%' must start a percent-encoded octet")
    if "#" in url:
        raise ValueError("url: a fragment is never sent; sign the URL without it")
    if not url.startswith(("http://", "https://")):
        # Policy: the normal form. (httpx keeps `HTTPS://h:443`'s port: the default-port guard.)
        raise ValueError("url: the scheme must be written http:// or https://")
    parts = urlsplit(url)
    netloc = parts.netloc
    if "@" in netloc:
        raise ValueError("url: user info is never part of the signed authority")
    if "%" in netloc:
        raise ValueError("url: a percent-encoded host is not signed")
    if netloc.rfind(":") > netloc.rfind("]"):
        if _PORT_RE.fullmatch(netloc[netloc.rfind(":") + 1 :]) is None:
            raise ValueError("url: write the port in decimal without leading zeros")
    if ("[" in netloc or "]" in netloc) and not _compressed_ipv6_netloc(netloc):
        raise ValueError("url: an IPv6 host is [its compressed form] with an optional :port")
    try:
        port = parts.port
    except ValueError:
        raise ValueError("url: invalid port") from None
    if not parts.hostname:
        raise ValueError("url: a host is required")
    if parts.hostname.endswith("."):
        # requests and aiohttp send Host without it, httpx and urllib with it.
        raise ValueError("url: write the host without a trailing dot")
    if netloc != netloc.lower():
        # urllib sends Host as written, the other clients lowercase it.
        raise ValueError("url: write the host in lowercase")
    if port == _DEFAULT_PORTS[parts.scheme]:
        # urllib sends `host:443` in Host, the other clients drop the default port.
        raise ValueError("url: leave out the scheme's default port")
    path = parts.path or "/"
    if "[" in path or "]" in path or "[" in parts.query or "]" in parts.query:
        raise ValueError("url: brackets belong to an IPv6 host only")
    if "%" in path:
        # EM rebuilds @path from the decoded path; these bytes would never match.
        raise ValueError("url: percent-encoded octets in the path are not verifiable")
    if any(segment in (".", "..") for segment in path.split("/")):
        raise ValueError("url: dot segments are removed by HTTP clients before sending")
    for octet in _PERCENT_OCTET_RE.findall(parts.query):
        if octet != octet.upper():
            raise ValueError("url: write percent-encoded octets in the query in upper-case hex")
        if chr(int(octet, 16)) in _QUERY_DECODED_BY_CLIENTS:
            raise ValueError("url: write this query character itself, not percent-encoded")
    return netloc, path, parts.query


def _check_recipient(recipient: object) -> str:
    # The same rules as near_nep413.nep413_preimage, checked before the key.
    if not isinstance(recipient, str) or not recipient or recipient.strip() != recipient:
        raise ValueError("recipient must be nonempty without surrounding whitespace")
    if recipient.lower().removesuffix(".") == "intents.near":
        raise ValueError("intents.near is not an allowed recipient")
    # A lone surrogate raises UnicodeEncodeError (a ValueError) here, not after the key.
    recipient.encode("utf-8")
    return recipient


def _body_bytes(body: object) -> bytes | None:
    if body is None:
        return None
    if isinstance(body, bytes):
        return body
    if isinstance(body, str):
        return body.encode("utf-8")  # UnicodeEncodeError is a ValueError
    raise ValueError("body must be str, bytes or None")


def sign_near_http_request(
    signer: NearLocalSigner,
    account_id: str,
    method: str,
    url: str,
    *,
    nonce: str,
    recipient: str,
    body: str | bytes | None = None,
    created: int | None = None,
    validity_sec: int = NEAR_HTTP_AUTH_MAX_VALIDITY_SEC,
    header_case: str = "title",
) -> dict[str, str]:
    """Sign one HTTP request as ``account_id``; return the headers to send.

    Args:
        signer: a NEAR ed25519 signer (``public_key`` + ``sign(digest)``) whose
            key is a FullAccess key of ``account_id`` on the verifier's network.
        account_id: the NEAR account (named or 64-hex implicit), lowercase.
        method: the HTTP method; signed upper-cased.
        url: the absolute URL exactly as it will be requested.
        nonce: single use; a fresh one per request and per retry.
        recipient: the verifier's NEP-413 recipient; for EM
            :data:`EXECUTION_MARKET_NEP413_RECIPIENT`. Required: a signature
            names the one service it is for.
        body: the exact body bytes sent (``str`` is UTF-8); ``None`` = no body.
        created: Unix seconds; default now.
        validity_sec: ``expires - created``, 1..300.
        header_case: ``"title"`` or ``"lower"`` header names.

    Returns:
        ``Content-Digest`` (when there is a body), ``Signature-Input`` and
        ``Signature``. Raises ``ValueError`` before reading the key for any
        invalid input, and after signing if the signer's output does not verify.
    """
    if not isinstance(header_case, str) or header_case not in _HEADER_NAMES:
        raise ValueError("header_case must be 'title' or 'lower'")
    _account(account_id, "account_id")
    if not isinstance(method, str) or _METHOD_RE.fullmatch(method) is None:
        raise ValueError("method must be an HTTP token")
    authority, path, query = _url_parts(url)
    if not isinstance(nonce, str) or _NONCE_RE.fullmatch(nonce) is None:
        raise ValueError(
            f"nonce: 1 to {NEAR_HTTP_AUTH_MAX_NONCE_LENGTH} characters of [A-Za-z0-9._~+/=-]"
        )
    recipient = _check_recipient(recipient)
    raw_body = _body_bytes(body)
    if type(validity_sec) is not int or not 1 <= validity_sec <= NEAR_HTTP_AUTH_MAX_VALIDITY_SEC:
        raise ValueError("validity_sec must be an int from 1 to 300")
    if created is None:
        created = int(time.time())
    elif type(created) is not int:
        raise ValueError("created must be an int of Unix seconds")
    if not 0 <= created <= _MAX_TIMESTAMP - validity_sec:
        raise ValueError("created is out of range")

    public_key = signer.public_key
    if not isinstance(public_key, bytes) or len(public_key) != 32:
        raise ValueError("expected 32-byte Ed25519 public key")
    public_text = "ed25519:" + _b58encode(public_key)
    keyid = f"near:{account_id}:{public_text}"

    covered = select_covered(url, raw_body is not None)
    digest = None if raw_body is None else compute_content_digest(raw_body)
    params = canonical_params(
        created=created,
        expires=created + validity_sec,
        keyid=keyid,
        nonce=nonce,
        alg=NEAR_HTTP_AUTH_ALG,
    )
    base = build_signature_base(
        CanonicalMessage(
            method=method,
            authority=authority,
            path=path,
            query=f"?{query}" if query else None,
            content_digest=digest,
            covered=covered,
            params=params,
        )
    )
    signed = sign_nep413(
        signer,
        account_id=account_id,
        message=base,
        nonce=hashlib.sha256(nonce.encode("utf-8")).digest(),
        recipient=recipient,
    )
    # sign_nep413 verified the signature against the key it read; the keyid
    # names the key read above. A signer whose key changed in between would
    # put one key in the header and another's signature next to it.
    if signed["publicKey"] != public_text:
        raise ValueError("the signer's public key changed while signing")

    sig_name, input_name, digest_name = _HEADER_NAMES[header_case]
    headers: dict[str, str] = {}
    if digest is not None:
        headers[digest_name] = digest
    headers[input_name] = f"{NEAR_HTTP_AUTH_LABEL}={build_signature_params(covered, params)}"
    headers[sig_name] = f"{NEAR_HTTP_AUTH_LABEL}=:{signed['signature']}:"
    return headers


__all__ = [
    "EXECUTION_MARKET_NEP413_RECIPIENT",
    "NEAR_HTTP_AUTH_ALG",
    "NEAR_HTTP_AUTH_LABEL",
    "NEAR_HTTP_AUTH_MAX_NONCE_LENGTH",
    "NEAR_HTTP_AUTH_MAX_VALIDITY_SEC",
    "sign_near_http_request",
]
