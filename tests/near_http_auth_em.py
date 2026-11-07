"""Execution Market's NEAR HTTP verifier, transcribed for the tests (no SDK code).

Source: execution-market ``origin/main`` c282814379bbf9c294efacf21ee404035f07dfc1,
``mcp_server/integrations/near/http_auth.py::verify_near_request`` and the parts
of ``integrations/erc8128/verifier.py`` (``verify_erc8128_request`` up to the
``near:`` branch, ``_parse_signature_input``, ``_extract_signature_bytes``,
``_validate_timestamps``, ``_determine_binding``, ``_authority_candidates``,
``_resolve_authority``, ``_verify_content_digest``, ``_build_signature_base``,
``_resolve_component``, ``_build_signature_params``, ``_format_param``),
``integrations/erc8128/profile.py`` (the structured-field parser),
``integrations/near/nep413.py`` and ``integrations/solana/signature.py::b58decode``.
Re-transcribe from there when EM changes them; never adapt it to the SDK.

What is NOT transcribed, and why: the access-key RPC (``view_access_key`` at
``finality: final``; the test says what it answers, as EM's own tests do), the
TTL cache in front of it, and EM's settings (the authority allowlist is a
parameter, production's two hosts by default, ``is_production`` true).

The request is modelled as EM's Starlette sees it after an HTTP client sent
it: :func:`wire_request` lets httpx build the request (it lowercases the host,
drops the default port and removes dot segments) and decodes the path as an
ASGI server does (``scope["path"]``); the query string stays raw.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import re
import struct
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import unquote

import httpx

EM_COMMIT = "c282814379bbf9c294efacf21ee404035f07dfc1"
EM_RECIPIENT = "execution.market"
PRODUCTION_AUTHORITIES = frozenset({"api.execution.market", "mcp.execution.market"})
REQUEST_BOUND_COMPONENTS = {"@method", "@authority", "@path"}
MAX_SIGNATURE_FIELD_LENGTH = 8 * 1024

_B58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_B58_INDEX = {c: i for i, c in enumerate(_B58_ALPHABET)}
_ACCOUNT_ID_RE = re.compile(r"(?:[a-z0-9]+[-_])*[a-z0-9]+(?:\.(?:[a-z0-9]+[-_])*[a-z0-9]+)*")


@dataclass
class ServerRequest:
    """What EM reads off a request: Starlette's ``request.url`` + headers + body."""

    method: str
    netloc: str  # Starlette rebuilds request.url from the Host header
    path: str  # DECODED, as scope["path"]
    query: str  # raw query string
    headers: dict[str, str]  # lowercase names
    body: bytes = b""


@dataclass
class Policy:
    max_validity_sec: int = 300
    clock_skew_sec: int = 30
    required_components: set = field(default_factory=set)


@dataclass
class Result:
    ok: bool
    address: str | None = None
    reason: str | None = None


def wire_request(method: str, url: str, headers: dict[str, str], body: Any = None) -> ServerRequest:
    content = body.encode("utf-8") if isinstance(body, str) else body
    sent = httpx.Request(method, url, headers=headers, content=content)
    raw_path, _, query = sent.url.raw_path.decode("ascii").partition("?")
    return ServerRequest(
        method=sent.method,
        netloc=sent.headers["host"],
        path=unquote(raw_path),
        query=query,
        headers={k.lower(): v for k, v in sent.headers.items()},
        body=content or b"",
    )


# -- profile.py ----------------------------------------------------------------
def _dict_value_end(s: str, start: int) -> int:
    depth = 0
    in_quotes = False
    escaped = False
    for i in range(start, len(s)):
        ch = s[i]
        if in_quotes:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_quotes = False
        elif ch == '"' and (i == start or s[i - 1] != "\\"):
            in_quotes = True
        else:
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
            elif ch == "," and depth == 0:
                return i
    return len(s)


def split_dict_members(raw: str) -> dict[str, str]:
    result = []
    s = raw.strip()
    pos = 0
    while pos < len(s):
        eq_idx = s.find("=", pos)
        if eq_idx < 0:
            break
        end = _dict_value_end(s, eq_idx + 1)
        result.append((s[pos:eq_idx].strip(), s[eq_idx + 1 : end].strip()))
        pos = end + 1
    return dict(result)


def _split_params(s: str) -> list[str]:
    parts = []
    current = ""
    in_quotes = False
    escaped = False
    for ch in s:
        if in_quotes:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_quotes = False
            current += ch
        elif ch == '"':
            in_quotes = True
            current += ch
        elif ch == ";":
            if current.strip():
                parts.append(current)
            current = ""
        else:
            current += ch
    if current.strip():
        parts.append(current)
    return parts


def _parse_params(params_str: str) -> dict[str, Any]:
    params: dict[str, Any] = {}
    for part in _split_params(params_str):
        part = part.strip()
        if not part or "=" not in part:
            continue
        key, _, val = part.partition("=")
        key = key.strip()
        val = val.strip()
        if val.startswith('"') and val.endswith('"'):
            val = val[1:-1]
        else:
            try:
                val = int(val)
            except ValueError:
                pass
        params[key] = val
    return params


def parse_inner_list_with_params(value: str) -> tuple[list[str], dict[str, Any]]:
    paren_start = value.find("(")
    paren_end = value.find(")")
    if paren_start < 0 or paren_end < 0:
        return [], {}
    components = re.findall(r'"([^"]*)"', value[paren_start + 1 : paren_end])
    return components, _parse_params(value[paren_end + 1 :])


def _is_candidate(params: dict[str, Any]) -> bool:
    return "tag" not in params or params["tag"] == "erc8128"


# -- verifier.py -----------------------------------------------------------------
def parse_signature_input(raw: str) -> tuple[str | None, list[str], dict[str, Any]]:
    entries = {
        label: value
        for label, value in split_dict_members(raw).items()
        if _is_candidate(parse_inner_list_with_params(value)[1])
    }
    if "eth" in entries:
        label = "eth"
    elif entries:
        label = next(iter(entries))
    else:
        return None, [], {}
    covered, params = parse_inner_list_with_params(entries[label])
    return label, covered, params


def _extract_signature_bytes(sig_header: str, label: str) -> bytes | None:
    value = split_dict_members(sig_header).get(label)
    if not value:
        return None
    value = value.strip()
    b64_data = value[1:-1] if value.startswith(":") and value.endswith(":") else value
    try:
        return base64.b64decode(b64_data)
    except Exception:
        return None


def _validate_timestamps(created: Any, expires: Any, policy: Policy, now: int) -> str | None:
    try:
        created_ts = int(created)
        expires_ts = int(expires)
    except (ValueError, TypeError):
        return "created/expires must be integer Unix timestamps"
    if expires_ts <= created_ts:
        return "expires must be greater than created"
    if expires_ts - created_ts > policy.max_validity_sec:
        return "Validity window too large"
    if created_ts > now + policy.clock_skew_sec:
        return "Signature not yet valid (created is in the future)"
    if expires_ts < now - policy.clock_skew_sec:
        return "Signature has expired"
    return None


def _determine_binding(request: ServerRequest, covered: list[str]) -> str:
    covered_set = set(covered)
    if not REQUEST_BOUND_COMPONENTS.issubset(covered_set):
        return "class-bound"
    if request.query and "@query" not in covered_set:
        return "class-bound"
    return "request-bound"


def _resolve_authority(request: ServerRequest, allowed: frozenset) -> str:
    candidates = []
    forwarded = request.headers.get("x-forwarded-host")
    if forwarded:
        candidates.append(forwarded.split(",")[0].strip())
    if request.netloc:
        candidates.append(request.netloc)
    host = request.headers.get("host")
    if host:
        candidates.append(host.strip())
    for candidate in candidates:
        if candidate and candidate.lower() in allowed:
            return candidate
    return ""


def _verify_content_digest(request: ServerRequest) -> str | None:
    digest_header = request.headers.get("content-digest")
    if not digest_header:
        return "content-digest covered but Content-Digest header missing"
    m = re.match(r"sha-256=:([A-Za-z0-9+/=]+):", digest_header)
    if not m:
        return "Unsupported Content-Digest format (only sha-256 supported)"
    try:
        expected_hash = base64.b64decode(m.group(1))
    except Exception:
        return "Invalid Content-Digest base64 encoding"
    if hashlib.sha256(request.body).digest() != expected_hash:
        return "Content-Digest mismatch: body has been tampered with"
    return None


def _format_param(key: str, value: Any) -> str:
    if isinstance(value, int):
        return f"{key}={value}"
    return f'{key}="{value}"'


def build_signature_params(covered: list[str], params: dict[str, Any]) -> str:
    parts = ["(" + " ".join(f'"{c}"' for c in covered) + ")"]
    parts.extend(_format_param(k, v) for k, v in params.items())
    return ";".join(parts)


def _resolve_component(request: ServerRequest, component: str, allowed: frozenset) -> str:
    if component == "@scheme":
        return "https"
    if component == "@method":
        return request.method.upper()
    if component == "@authority":
        return _resolve_authority(request, allowed)
    if component == "@path":
        return request.path
    if component == "@query":
        return f"?{request.query}" if request.query else "?"
    return request.headers.get(component.lower()) or ""


def build_signature_base(
    request: ServerRequest, covered: list[str], params: dict[str, Any], allowed: frozenset
) -> str:
    lines = [f'"{c}": {_resolve_component(request, c, allowed)}' for c in covered]
    lines.append(f'"@signature-params": {build_signature_params(covered, params)}')
    return "\n".join(lines)


# -- nep413.py / solana signature.py ---------------------------------------------
def b58decode(value: str) -> bytes | None:
    if not value or not isinstance(value, str):
        return None
    num = 0
    for char in value:
        digit = _B58_INDEX.get(char)
        if digit is None:
            return None
        num = num * 58 + digit
    body = num.to_bytes((num.bit_length() + 7) // 8, "big") if num else b""
    return b"\x00" * (len(value) - len(value.lstrip("1"))) + body


def parse_ed25519_public_key(value: Any) -> bytes | None:
    if not isinstance(value, str) or not value.startswith("ed25519:"):
        return None
    raw = b58decode(value[len("ed25519:") :])
    return raw if raw is not None and len(raw) == 32 else None


def parse_near_keyid(value: Any) -> tuple[str, str] | None:
    if not isinstance(value, str) or not value.startswith("near:"):
        return None
    parts = value.split(":", 2)
    if (
        len(parts) != 3
        or not 2 <= len(parts[1]) <= 64
        or _ACCOUNT_ID_RE.fullmatch(parts[1]) is None
    ):
        return None
    key = parts[2]
    if not key.startswith("ed25519:"):
        key = "ed25519:" + key
    if not 40 <= len(key) <= 52 or parse_ed25519_public_key(key) is None:
        return None
    return parts[1], key


def _borsh_string(value: str) -> bytes:
    data = value.encode("utf-8")
    return struct.pack("<I", len(data)) + data


def nep413_hash(
    message: str, nonce: bytes, recipient: str, callback_url: str | None = None
) -> bytes:
    if len(nonce) != 32:
        raise ValueError("NEP-413 nonce must be 32 bytes")
    body = _borsh_string(message) + nonce + _borsh_string(recipient)
    body += b"\x00" if callback_url is None else b"\x01" + _borsh_string(callback_url)
    return hashlib.sha256(struct.pack("<I", 2**31 + 413) + body).digest()


def _decode_signature(value: str) -> bytes | None:
    if len(value) % 4:
        value += "=" * (-len(value) % 4)
    try:
        raw = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        return None
    return raw if len(raw) == 64 else None


def verify_nep413_signature(
    *, public_key: str, signature: str, message: str, nonce: bytes, recipient: str
) -> bool:
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    raw_key = parse_ed25519_public_key(public_key)
    raw_sig = _decode_signature(signature)
    if raw_key is None or raw_sig is None or len(nonce) != 32:
        return False
    try:
        Ed25519PublicKey.from_public_bytes(raw_key).verify(
            raw_sig, nep413_hash(message, nonce, recipient)
        )
        return True
    except (InvalidSignature, ValueError):
        return False


# -- the NEAR branch -------------------------------------------------------------
class NonceStore:
    def __init__(self) -> None:
        self.keys: list[str] = []

    def consume(self, key: str) -> bool:
        if key in self.keys:
            return False
        self.keys.append(key)
        return True


def verify(
    request: ServerRequest,
    *,
    now: int,
    store: NonceStore | None = None,
    policy: Policy | None = None,
    allowed: frozenset = PRODUCTION_AUTHORITIES,
    recipient: str = EM_RECIPIENT,
    full_access: bool = True,
) -> Result:
    """``verify_erc8128_request`` for a ``near:`` keyid, offline. Order is EM's."""
    policy = policy or Policy()
    store = store if store is not None else NonceStore()
    sig_input_raw = request.headers.get("signature-input")
    sig_raw = request.headers.get("signature")
    if not sig_input_raw or not sig_raw:
        return Result(False, reason="Missing Signature-Input or Signature header")
    if len(sig_input_raw) > MAX_SIGNATURE_FIELD_LENGTH or len(sig_raw) > MAX_SIGNATURE_FIELD_LENGTH:
        return Result(False, reason="field too long")
    label, covered, params = parse_signature_input(sig_input_raw)
    if label is None:
        return Result(False, reason="Could not parse Signature-Input header")
    signature = _extract_signature_bytes(sig_raw, label)
    if signature is None:
        return Result(False, reason=f"No signature found for label '{label}'")
    keyid = params.get("keyid")
    if not keyid:
        return Result(False, reason="Missing keyid parameter")
    if not (isinstance(keyid, str) and keyid.startswith("near:")):
        return Result(False, reason="not the NEAR profile")
    # verify_near_request
    parsed = parse_near_keyid(keyid)
    if parsed is None:
        return Result(False, reason="invalid_near_keyid")
    account, public_key = parsed
    if params.get("alg") != "ed25519-nep413" or len(signature) != 64:
        return Result(False, reason="invalid_near_algorithm_or_signature")
    if any(type(params.get(k)) is not int for k in ("created", "expires")):
        return Result(False, reason="created/expires must be integer Unix timestamps")
    error = _validate_timestamps(params["created"], params["expires"], policy, now)
    if error:
        return Result(False, reason=error)
    nonce = params.get("nonce")
    if not isinstance(nonce, str) or not nonce or len(nonce) > 256:
        return Result(
            False, reason="NEAR signatures require a nonempty nonce (at most 256 characters)"
        )
    if _determine_binding(request, covered) != "request-bound":
        return Result(False, reason="NEAR signatures must be request-bound")
    if not _resolve_authority(request, allowed):
        return Result(False, reason="authority_not_allowed")
    if not policy.required_components.issubset(covered):
        return Result(False, reason="Required component not covered")
    if not store.consume(f"near:{account}:{nonce}"):
        return Result(False, reason="Nonce already consumed (replay attempt)")
    if request.body and "content-digest" not in covered:
        return Result(False, reason="Bodied request MUST cover content-digest")
    if request.body or "content-digest" in covered:
        error = _verify_content_digest(request)
        if error:
            return Result(False, reason=error)
    base = build_signature_base(request, covered, params, allowed)
    if not verify_nep413_signature(
        public_key=public_key,
        signature=base64.b64encode(signature).decode("ascii"),
        message=base,
        nonce=hashlib.sha256(nonce.encode("utf-8")).digest(),
        recipient=recipient,
    ):
        return Result(False, reason="NEP-413 signature verification failed")
    if not full_access:
        return Result(False, reason="near_function_call_only")
    return Result(True, address=f"near:{account}")
