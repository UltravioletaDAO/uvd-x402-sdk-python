"""ERC-8128 / RFC 9421 pure core — stdlib only, no crypto, no clock, no I/O.

This module is the ONE place the wire format exists. The signer builds a
signature base from a parameter LIST; the verifier builds the same base from
the parameter SUBSTRING it read off the wire. Same function, same bytes.

**The load-bearing rule:** the verifier NEVER re-serialises ``@signature-params``.
:func:`parse_signature_input` keeps ``params_raw`` verbatim and
:func:`build_signature_params` returns it untouched. That is what makes
``alg`` present, ``alg`` absent, a checksummed keyid, a reordered parameter
list and any future RFC 9421 parameter verify through one byte path with zero
flags. Do NOT reintroduce an anchored regex that enumerates the allowed
parameters — that is the exact bug this module exists to delete (MeshRelay
shipped it and rejected every canonical signer in the fleet with
``signature_input_invalid``).
"""

from __future__ import annotations

import base64
import hashlib
import re
from dataclasses import dataclass, field
from typing import Dict, List, Mapping, Optional, Sequence, Tuple, Union
from urllib.parse import urlsplit

from uvd_x402_sdk.erc8128.errors import Erc8128Error

#: Generation of the wire contract this module implements.
WIRE_CONTRACT_VERSION = "F3-3"

#: Signature algorithm parameter — pinned wire format (always emitted by the
#: canonical profile, verified in both profiles).
ALG = "eip191"

#: Default signature label.
DEFAULT_LABEL = "eth"

#: Default validity window (seconds). Server policy caps at 300.
DEFAULT_VALIDITY_SEC = 300

#: keyid chain binding (Base mainnet, the production auth chain).
DEFAULT_CHAIN_ID = 8453

#: ``erc8128:<chain-id decimal>:<0x + 40 hex>``. Mixed case is accepted on the
#: wire (a checksummed keyid still authenticates); emitters MUST lowercase.
KEYID_RE = re.compile(r"^erc8128:(\d+):(0x[0-9A-Fa-f]{40})$")

#: RFC 9530 Content-Digest, sha-256 only.
CONTENT_DIGEST_RE = re.compile(r"^sha-256=:([A-Za-z0-9+/]+={0,2}):$")

#: RFC 8941 byte sequence.
_BYTE_SEQUENCE_RE = re.compile(r"^:([A-Za-z0-9+/]*={0,2}):$")

#: An inner list is a whitespace-separated run of quoted tokens and NOTHING
#: else. Spacing is tolerated (the base is rebuilt from the verbatim
#: substring anyway); unquoted junk is not.
_INNER_LIST_RE = re.compile(r'^\s*(?:"[^"]*"(?:\s+"[^"]*")*)?\s*$')
_TOKEN_RE = re.compile(r'"([^"]*)"')

#: Components the base builder resolves itself. Anything else in the covered
#: list is resolved from ``extra_components`` (EM resolves unknown covered
#: components from request headers, absent header ⇒ empty string).
_DERIVED_COMPONENTS = ("@method", "@authority", "@path", "@query", "content-digest")

#: The port each scheme implies. RFC 9421 §2.2.3 keeps it OUT of ``@authority``.
DEFAULT_PORTS: Dict[str, str] = {"http": "80", "https": "443", "ws": "80", "wss": "443"}

#: ``("name", value)``. An int value is emitted bare, a str value quoted.
SigParam = Tuple[str, Union[str, int]]

ObservedProfile = str  # "canonical" | "legacy_no_alg" | "legacy_alg_checksum_keyid"


@dataclass(frozen=True)
class CanonicalMessage:
    """Everything the signature base is built from.

    ``params`` is either a list of ``(name, value)`` pairs (the signer, which
    knows what it wants to emit) or the verbatim ``@signature-params`` value
    read off the wire (the verifier, which must not re-serialise anything).
    """

    method: str
    authority: str
    path: str
    query: Optional[str] = None  # INCLUDES the leading '?'
    content_digest: Optional[str] = None  # full 'sha-256=:…:' value
    covered: Sequence[str] = ()
    params: Union[Sequence[SigParam], str] = ()
    #: Values for covered components this builder does not derive (arbitrary
    #: header fields). Keys are lowercase component ids.
    extra_components: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ParsedSignatureInput:
    """The parse of a ``Signature-Input`` header. Pure — no crypto, no policy."""

    label: str
    covered: Tuple[str, ...]
    params_raw: str  # VERBATIM from the wire, including the covered list
    created: int
    expires: int
    nonce: Optional[str]
    keyid: str  # ORIGINAL case — it is re-emitted into the signed base
    chain_id: int
    wallet: str  # lowercase
    alg: Optional[str]
    observed_profile: ObservedProfile


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def normalize_authority(authority: str, scheme: str) -> str:
    """The URL-DERIVED ``@authority`` in its NORMALISED form (RFC 9421 §2.2.3):
    lowercased, with THAT scheme's default port omitted (443 for https/wss, 80
    for http/ws). Any other port is kept (``host:8443``, and also ``host:80``
    under https), because it is part of the authority.

    The rule is IDEMPOTENT: an authority already written this way — which is
    every request the fleet actually sends — comes back byte for byte, so
    adopting it moves no live signature. What it fixes is the same request
    written two ways: ``https://host:443/x`` and ``https://host/x`` are the
    same request, and ``urlsplit().netloc`` would have signed two different
    authorities for them, only one of which any verifier reproduces (a server
    sees the normalised form in ``Host``).

    ``scheme`` is REQUIRED and has no default, because every caller of this
    function has one: the signer reads it off the URL it is signing, and a
    verifier deriving the authority from an incoming request knows the scheme
    it is served over. The CONFIGURED policy authority is NOT such a caller —
    it carries no scheme, so "the default port" has no answer there and
    guessing one silently breaks two real deployment shapes (``https`` on
    ``:80`` and ``http`` on ``:443``). That value goes through
    :func:`~uvd_x402_sdk.erc8128.verifier.policy_authority`, which never
    touches ports.
    """
    value = (authority or "").strip().lower()
    port_at = value.rfind(":")
    # `rfind("]")` is what keeps an IPv6 literal's own colons from reading as a
    # port: in `[::1]:443` the last ':' is after the bracket, in `[::1]` it is
    # not.
    if port_at > value.rfind("]") and value[port_at + 1 :] == DEFAULT_PORTS.get(
        (scheme or "").strip().lower()
    ):
        return value[:port_at]
    return value


def select_covered(url: str, has_body: bool) -> List[str]:
    """The canonical covered list for a request: method, authority, path,
    ``@query`` only when the URL HAS a query string, ``content-digest`` only
    when the request has a body.

    Omitting ``@query`` when there is no query string is the canonical rule —
    the ``query or "?"`` fallback inside the base builders is dead code on the
    emit path and lives there only for verifier bug-compat with EM.
    """
    parts = urlsplit(url)
    covered = ["@method", "@authority", "@path"]
    if parts.query:
        covered.append("@query")
    if has_body:
        covered.append("content-digest")
    return covered


def canonical_params(
    created: int,
    expires: int,
    keyid: str,
    nonce: Optional[str] = None,
    alg: Optional[str] = ALG,
) -> List[SigParam]:
    """The pinned parameter order: created, expires, nonce, keyid, alg.

    ``nonce=None`` omits the nonce (a "replayable" signature, rejected by
    default by both live verifiers). ``alg=None`` emits the legacy no-alg
    wire.
    """
    params: List[SigParam] = [("created", int(created)), ("expires", int(expires))]
    if nonce:
        params.append(("nonce", nonce))
    params.append(("keyid", keyid))
    if alg:
        params.append(("alg", alg))
    return params


def _format_param(name: str, value: Union[str, int]) -> str:
    if isinstance(value, bool):  # bool is an int subclass — never emit True/False
        raise ValueError(f"boolean signature parameter is not supported: {name}")
    if isinstance(value, int):
        return f"{name}={value}"
    return f'{name}="{value}"'


def build_signature_params(
    covered: Sequence[str], params: Union[Sequence[SigParam], str]
) -> str:
    """Build (or pass through) the ``@signature-params`` value.

    When ``params`` is a string it is the COMPLETE value read off the wire
    (starting with the parenthesised covered list) and is returned byte for
    byte — no re-quoting, no re-spacing, no dropped unknown parameter.
    """
    if isinstance(params, str):
        if not params.startswith("("):
            raise ValueError(
                "a verbatim @signature-params value must start with the covered list"
            )
        return params
    comp_str = " ".join(f'"{c}"' for c in covered)
    parts = [f"({comp_str})"]
    parts.extend(_format_param(name, value) for name, value in params)
    return ";".join(parts)


def _resolve_component(msg: CanonicalMessage, component: str) -> str:
    if component == "@method":
        return msg.method.upper()
    if component == "@authority":
        return msg.authority
    if component == "@path":
        return msg.path
    if component == "@query":
        # `?` is the present-but-empty fallback. The signer never reaches it
        # (it omits @query entirely when there is no query string); the
        # verifier does, when a client covers @query on a query-less request.
        return msg.query or "?"
    if component == "content-digest":
        return msg.content_digest or ""
    return msg.extra_components.get(component.lower(), "")


def build_signature_base(msg: CanonicalMessage) -> str:
    """The RFC 9421 signature base: one ``"<id>": <value>`` line per covered
    component IN THE COVERED ORDER, then ``"@signature-params": <params>``,
    joined with a single LF and no trailing newline.
    """
    lines = [f'"{c}": {_resolve_component(msg, c)}' for c in msg.covered]
    lines.append(f'"@signature-params": {build_signature_params(msg.covered, msg.params)}')
    return "\n".join(lines)


def compute_content_digest(body: Union[str, bytes]) -> str:
    """RFC 9530 ``sha-256=:<base64>:`` over the exact body bytes."""
    raw = body.encode("utf-8") if isinstance(body, str) else body
    return "sha-256=:" + base64.b64encode(hashlib.sha256(raw).digest()).decode("ascii") + ":"


def canonical_keyid(chain_id: int, address: str) -> str:
    """``erc8128:<chain_id>:<address>`` with the address ALWAYS lowercased —
    a checksummed keyid caused the v9.x silent-auth incident.
    """
    return f"erc8128:{int(chain_id)}:{address.lower()}"


def eip191_byte_length(base: str) -> int:
    """Length of the EIP-191 message in UTF-8 BYTES, never code points.

    Named on purpose: ``len(message)`` on a Python ``str`` is code points, and
    a verifier that prefixes with that hashes a different message than
    ``encode_defunct`` as soon as the base carries one non-ASCII byte.
    """
    return len(base.encode("utf-8"))


def eip191_message_hash(base: str) -> bytes:
    """keccak256 of the EIP-191 (0x45) framing of ``base``.

    Only needed for the ERC-1271 path — ``eth_account`` does this internally
    for the EOA path. Requires the ``signer`` extra.
    """
    from eth_utils import keccak  # lazy: base install has no eth-account

    raw = base.encode("utf-8")
    return keccak(b"\x19Ethereum Signed Message:\n" + str(len(raw)).encode("ascii") + raw)


# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------


def _split_dict_members(raw: str) -> "Dict[str, str]":
    """Split an RFC 8941 dictionary into ``label -> value``, respecting
    parentheses and quoted strings. Insertion-ordered.
    """
    members: Dict[str, str] = {}
    s = raw.strip()
    i = 0
    while i < len(s):
        eq = s.find("=", i)
        if eq < 0:
            break
        label = s[i:eq].strip()
        j = eq + 1
        start = j
        depth = 0
        in_quotes = False
        escaped = False
        while j < len(s):
            ch = s[j]
            if in_quotes:
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == '"':
                    in_quotes = False
            elif ch == '"':
                in_quotes = True
            elif ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
            elif ch == "," and depth <= 0:
                break
            j += 1
        if label:
            members[label] = s[start:j].strip()
        i = j + 1
    return members


def _split_params(raw: str) -> List[str]:
    """Split ``;a=1;b="x;y"`` into ``['a=1', 'b="x;y"']``."""
    if not raw:
        return []
    if not raw.startswith(";"):
        raise Erc8128Error(
            "signature_input_invalid", "Invalid Signature-Input parameter section"
        )
    parts: List[str] = []
    current: List[str] = []
    in_quotes = False
    escaped = False
    for ch in raw[1:]:
        if in_quotes:
            current.append(ch)
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_quotes = False
            continue
        if ch == '"':
            in_quotes = True
            current.append(ch)
        elif ch == ";":
            parts.append("".join(current))
            current = []
        else:
            current.append(ch)
    parts.append("".join(current))
    return parts


def _parse_params(raw: str) -> "Dict[str, Union[str, int]]":
    params: Dict[str, Union[str, int]] = {}
    for item in _split_params(raw):
        if not item.strip():
            raise Erc8128Error(
                "signature_input_invalid", "Empty Signature-Input parameter"
            )
        name, sep, value = item.partition("=")
        name = name.strip()
        if not sep or not name:
            raise Erc8128Error(
                "signature_input_invalid", f"Malformed Signature-Input parameter: {item}"
            )
        value = value.strip()
        if len(value) >= 2 and value.startswith('"') and value.endswith('"'):
            params[name] = value[1:-1]
        elif re.fullmatch(r"-?\d+", value):
            params[name] = int(value)
        else:
            params[name] = value
    return params


def parse_signature_input(value: str) -> ParsedSignatureInput:
    """Parse a ``Signature-Input`` header. Pure: no clock, no crypto, no policy.

    Keeps ``params_raw`` VERBATIM so the verifier can rebuild the exact bytes
    the client signed. Raises :class:`Erc8128Error` with a stable code.
    """
    members = _split_dict_members(value or "")
    if not members:
        raise Erc8128Error("signature_input_invalid", "Invalid Signature-Input format")

    label = DEFAULT_LABEL if DEFAULT_LABEL in members else next(iter(members))
    member = members[label]
    if not member.startswith("("):
        raise Erc8128Error(
            "signature_input_invalid", "Signature-Input must start with a covered list"
        )
    close = member.find(")")
    if close < 0:
        raise Erc8128Error("signature_input_invalid", "Unterminated covered component list")

    inner = member[1:close]
    if not _INNER_LIST_RE.match(inner):
        raise Erc8128Error("signature_input_invalid", "Invalid covered component list")
    covered = tuple(_TOKEN_RE.findall(inner))
    if not covered:
        raise Erc8128Error("signature_input_invalid", "Empty covered component list")

    params = _parse_params(member[close + 1 :])

    created = params.get("created")
    expires = params.get("expires")
    if not isinstance(created, int) or not isinstance(expires, int):
        raise Erc8128Error(
            "signature_input_invalid", "created and expires must be integer timestamps"
        )

    keyid = params.get("keyid")
    if not isinstance(keyid, str):
        raise Erc8128Error("signature_input_invalid", "Missing keyid parameter")
    m = KEYID_RE.match(keyid)
    if not m:
        raise Erc8128Error("signature_input_invalid", "Invalid keyid format")
    chain_id = int(m.group(1))
    address = m.group(2)

    nonce = params.get("nonce")
    if nonce is not None and not isinstance(nonce, str):
        nonce = str(nonce)
    alg = params.get("alg")
    if alg is not None and not isinstance(alg, str):
        alg = str(alg)

    if alg is None:
        observed = "legacy_no_alg"
    elif address != address.lower():
        observed = "legacy_alg_checksum_keyid"
    else:
        observed = "canonical"

    return ParsedSignatureInput(
        label=label,
        covered=covered,
        params_raw=member,
        created=created,
        expires=expires,
        nonce=nonce,
        keyid=keyid,
        chain_id=chain_id,
        wallet=address.lower(),
        alg=alg,
        observed_profile=observed,
    )


def parse_signature_header(value: str, label: Optional[str] = None) -> bytes:
    """Extract the 65 signature bytes (r||s||v) for ``label`` from a
    ``Signature`` header. ``v`` is the Ethereum 27/28 convention.
    """
    members = _split_dict_members(value or "")
    if not members:
        raise Erc8128Error("signature_invalid", "Invalid Signature header")
    if label is None:
        label = DEFAULT_LABEL if DEFAULT_LABEL in members else next(iter(members))
    raw = members.get(label)
    if raw is None:
        raise Erc8128Error("signature_invalid", f"No signature found for label '{label}'")

    m = _BYTE_SEQUENCE_RE.match(raw)
    if not m:
        raise Erc8128Error("signature_invalid", "Invalid Signature byte sequence")
    try:
        signature = base64.b64decode(m.group(1), validate=True)
    except Exception as exc:  # noqa: BLE001 - any decode failure is the same answer
        raise Erc8128Error("signature_invalid", "Invalid Signature base64") from exc
    if len(signature) != 65:
        raise Erc8128Error("signature_invalid", "Ethereum signature must be 65 bytes")
    return signature


def extract_keyid_wallet(signature_input: str) -> Optional[str]:
    """The lowercase wallet from the keyid, or ``None`` if the header does not
    parse. Deliberately dependency-free: this is what a rate-limit middleware
    needs, and it must not drag in ``eth_account``.
    """
    try:
        return parse_signature_input(signature_input).wallet
    except Erc8128Error:
        return None


__all__ = [
    "ALG",
    "CONTENT_DIGEST_RE",
    "DEFAULT_CHAIN_ID",
    "DEFAULT_LABEL",
    "DEFAULT_PORTS",
    "DEFAULT_VALIDITY_SEC",
    "KEYID_RE",
    "WIRE_CONTRACT_VERSION",
    "CanonicalMessage",
    "ObservedProfile",
    "ParsedSignatureInput",
    "SigParam",
    "build_signature_base",
    "build_signature_params",
    "canonical_keyid",
    "canonical_params",
    "compute_content_digest",
    "eip191_byte_length",
    "eip191_message_hash",
    "extract_keyid_wallet",
    "normalize_authority",
    "parse_signature_header",
    "parse_signature_input",
    "select_covered",
]
