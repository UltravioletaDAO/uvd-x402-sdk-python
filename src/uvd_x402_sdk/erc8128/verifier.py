"""ERC-8128 verifier — ONE pipeline, one byte path, policy as data.

Strict (MeshRelay) and lenient (EM) are not two code paths: they are two
:class:`VerifyPolicy` values over the same pipeline

    parse → accept-profile → freshness → chain → nonce presence
          → content-digest → components → [consume nonce] → recover → [consume nonce]

The signature base is rebuilt from the VERBATIM ``@signature-params``
substring (see :mod:`uvd_x402_sdk.erc8128.core`), so ``alg`` present, ``alg``
absent, a checksummed keyid, a reordered parameter list and any future RFC
9421 parameter all verify with no flags.

Content-Digest semantics: Execution Market's rule is canonical (owner
decision) — a digest is required IF AND ONLY IF the request actually carries a
body, and a bodyless request that voluntarily covers ``content-digest`` is
still verified. Body injection stays closed because body presence is
re-derived from the WIRE on every request: attaching a body to a
bodyless-signed request flips ``has_body`` and the signature, which did not
cover ``content-digest``, is rejected before any signature math.

One hardening over EM's exact code: EM decides body presence from
``content-length``/``transfer-encoding`` alone, which is only safe under
HTTP/1.1 framing (its production entrypoint is uvicorn — an ENVIRONMENTAL
invariant, not a code-enforced one). A shared SDK cannot inherit that
assumption, so a non-empty ``raw_body`` also counts as a body. That can only
ever require MORE, never less.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import inspect
import logging
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, Mapping, Optional, Sequence, Tuple
from urllib.parse import urlsplit

from uvd_x402_sdk.erc8128.core import (
    ALG,
    CONTENT_DIGEST_RE,
    DEFAULT_PORTS,
    CanonicalMessage,
    ParsedSignatureInput,
    build_signature_base,
    eip191_message_hash,
    parse_signature_header,
    parse_signature_input,
)
from uvd_x402_sdk.erc8128.errors import (
    ERC8128_ERROR_RETRYABLE,
    ERC8128_ERROR_STATUS,
    Erc8128Error,
)
from uvd_x402_sdk.erc8128.nonce import NoncePolicy, _nonce_ttl

logger = logging.getLogger(__name__)

_IDEMPOTENT_METHODS = ("GET", "HEAD")
_REQUEST_BOUND_COMPONENTS = ("@method", "@authority", "@path")

#: Every port that is the default for SOME scheme this SDK speaks: 80 and 443.
#: A CONFIGURED authority may not carry one — see :func:`policy_authority`.
_DEFAULT_PORT_VALUES = frozenset(DEFAULT_PORTS.values())

#: Characters that can never appear in a configured authority. The whitespace
#: run is explicit rather than ``str.isspace()`` so it is the SAME set the
#: TypeScript SDK rejects with ``/[\s/@?#]/``: any of these reaching the
#: rebuilt signature base corrupts the bytes the client actually signed.
_FORBIDDEN_AUTHORITY_CHARS = frozenset(" \t\n\r\v\f\u00a0/@?#")


@dataclass(frozen=True)
class VerifiableRequest:
    """The bytes that came off the wire. Nothing is derived for you."""

    method: str
    url: str  # absolute or origin-relative; only path+query are read
    headers: Mapping[str, Any]
    raw_body: Optional[bytes] = None


@dataclass(frozen=True)
class VerifyPolicy:
    """The posture, as a value.

    ``authority`` is a VALUE, never derived by the SDK. MeshRelay passes its
    pinned config; EM's REST/MCP pass their forwarded-host resolution; EM's
    WebSocket path MUST keep passing ``url.netloc`` — deriving it from a
    client-controlled header is how a caller once got the verifier to rebuild
    the base over an authority the ATTACKER chose.

    The value is lowercased and validated before the base is rebuilt, and its
    PORT IS LEFT ALONE (:func:`policy_authority`): it is the expected result of
    the URL-derived rule, so it must already be written the way the signer
    emits it. A default port in the config is a misconfiguration with its own
    message, not something to strip — the signer of an ``https`` deployment
    listening on ``:80`` really does sign ``host:80``.
    """

    authority: str
    accept: str = "accept-both"  # "canonical" | "legacy" | "accept-both"
    components: str = "exact-ordered"  # | "request-bound-subset"
    content_digest: str = "body-present"  # | "non-idempotent-methods"
    nonce: Optional[NoncePolicy] = None
    allowed_chain_ids: Optional[Tuple[int, ...]] = None  # None ⇒ any (EM posture)
    max_validity_sec: int = 300
    clock_skew_future_sec: int = 30
    clock_skew_past_expiry_sec: int = 0  # EM uses 30
    label: str = "eth"  # "any" accepts whatever label the client used
    contract_verifier: Optional[Callable[..., Any]] = None  # ERC-1271, no RPC in SDK
    contract_verifier_timeout: float = 10.0
    now: Optional[Callable[[], int]] = None
    on_observed_profile: Optional[Callable[..., None]] = None


@dataclass(frozen=True)
class VerifyResult:
    """Success carries everything a caller needs to authorise; failure carries
    the stable code, its HTTP status and whether a retry could ever work.
    """

    ok: bool
    wallet: Optional[str] = None
    chain_id: Optional[int] = None
    keyid: Optional[str] = None
    label: Optional[str] = None
    nonce: Optional[str] = None
    created: Optional[int] = None
    expires: Optional[int] = None
    signature_base: Optional[str] = None
    observed_profile: Optional[str] = None
    via: Optional[str] = None  # "eoa" | "erc1271"
    code: Optional[str] = None
    message: Optional[str] = None
    status: Optional[int] = None
    retryable: bool = False


# ---------------------------------------------------------------------------
# Request helpers
# ---------------------------------------------------------------------------


def _header(request: VerifiableRequest, name: str) -> Optional[str]:
    target = name.lower()
    for key, value in request.headers.items():
        if str(key).lower() == target:
            if isinstance(value, (list, tuple)):
                return str(value[0]) if value else None
            return None if value is None else str(value)
    return None


def _path_and_query(url: str) -> Tuple[str, Optional[str]]:
    parts = urlsplit(url)
    path = parts.path or "/"
    query = f"?{parts.query}" if parts.query else None
    return path, query


def _has_body(request: VerifiableRequest) -> bool:
    if request.raw_body:
        return True
    if _header(request, "content-length") not in (None, "0"):
        return True
    return _header(request, "transfer-encoding") is not None


def _fail(code: str, message: str) -> Erc8128Error:
    return Erc8128Error(code, message)


# ---------------------------------------------------------------------------
# Pipeline steps
# ---------------------------------------------------------------------------


def _check_accept_profile(parsed: ParsedSignatureInput, policy: VerifyPolicy) -> None:
    if parsed.alg is not None and parsed.alg != ALG:
        raise _fail(
            "alg_unsupported",
            f"Unsupported ERC-8128 alg: only {ALG} is verified",
        )
    if policy.accept == "canonical":
        if parsed.alg is None:
            raise _fail("alg_missing", 'Canonical profile requires alg="eip191"')
        address = parsed.keyid.rsplit(":", 1)[-1]
        if address != address.lower():
            raise _fail("keyid_not_lowercase", "Canonical profile requires a lowercase keyid")
    elif policy.accept == "legacy" and parsed.alg is not None:
        raise _fail("alg_unsupported", "Policy accepts only the legacy no-alg profile")


def _check_freshness(parsed: ParsedSignatureInput, policy: VerifyPolicy, now: int) -> None:
    if (
        parsed.expires <= parsed.created
        or parsed.expires - parsed.created > policy.max_validity_sec
        or parsed.created > now + policy.clock_skew_future_sec
        or parsed.expires <= now - policy.clock_skew_past_expiry_sec
    ):
        raise _fail(
            "signature_stale",
            "ERC-8128 signature is expired or outside the freshness window",
        )


def _check_content_digest(
    request: VerifiableRequest, parsed: ParsedSignatureInput, policy: VerifyPolicy
) -> Optional[str]:
    """Returns the Content-Digest value to fold into the base (the client's
    literal header text), or ``None`` when the digest is not covered.
    """
    covered = "content-digest" in parsed.covered
    has_body = _has_body(request)
    if policy.content_digest == "non-idempotent-methods":
        required = request.method.upper() not in _IDEMPOTENT_METHODS
    else:
        required = has_body

    header_value = _header(request, "content-digest")

    if required:
        if not covered:
            # CRY-001. The test is on the SIGNED list, never on header
            # presence: an unsigned Content-Digest header can never satisfy it.
            raise _fail(
                "content_digest_required",
                "Request with a body MUST cover content-digest in the signature",
            )
        if header_value is None and policy.content_digest == "non-idempotent-methods":
            raise _fail(
                "content_digest_required",
                "Signed write requires a raw body and a Content-Digest header",
            )

    if not covered:
        return None

    if header_value is None:
        # ABSENT-when-required is `content_digest_required`, in every branch
        # that reaches it. The partition both languages implement: absent while
        # required ⇒ required, present but unparseable/wrong algorithm ⇒
        # invalid, present and well-formed but not the body's bytes ⇒ mismatch.
        raise _fail(
            "content_digest_required",
            "content-digest covered but Content-Digest header missing",
        )
    m = CONTENT_DIGEST_RE.match(header_value)
    if not m:
        raise _fail(
            "content_digest_invalid",
            "Invalid Content-Digest header (only sha-256 is supported)",
        )
    try:
        provided = base64.b64decode(m.group(1), validate=True)
    except Exception as exc:  # noqa: BLE001 - any decode failure is the same answer
        raise _fail("content_digest_invalid", "Invalid Content-Digest base64") from exc
    expected = hashlib.sha256(request.raw_body or b"").digest()
    if len(provided) != len(expected) or not hmac.compare_digest(provided, expected):
        raise _fail("content_digest_mismatch", "Content-Digest does not match request body")
    # The client's LITERAL header text goes into the base — the verifier never
    # canonicalises a value the client signed.
    return header_value


def _check_components(
    parsed: ParsedSignatureInput,
    policy: VerifyPolicy,
    query: Optional[str],
    digest_covered: bool,
) -> None:
    covered = list(parsed.covered)
    if policy.components == "request-bound-subset":
        missing = [c for c in _REQUEST_BOUND_COMPONENTS if c not in covered]
        if missing or (query and "@query" not in covered):
            raise _fail(
                "class_bound_rejected",
                "Class-bound signatures not accepted (missing required components)",
            )
        return

    expected = list(_REQUEST_BOUND_COMPONENTS)
    if query:
        expected.append("@query")
    if digest_covered:
        expected.append("content-digest")
    if covered != expected:
        raise _fail(
            "components_invalid",
            "ERC-8128 must cover method, authority, path, query when present, "
            "and content-digest when the request has a body — in that order",
        )


async def _consume_nonce(
    parsed: ParsedSignatureInput, policy: VerifyPolicy
) -> None:
    nonce_policy = policy.nonce
    if nonce_policy is None or parsed.nonce is None:
        return
    ttl = _nonce_ttl(parsed.created, parsed.expires, policy.clock_skew_past_expiry_sec)
    try:
        outcome = nonce_policy.store.consume(
            parsed.nonce,
            wallet=parsed.wallet,
            chain_id=parsed.chain_id,
            ttl_seconds=ttl,
            created=parsed.created,
            expires=parsed.expires,
        )
        if inspect.isawaitable(outcome):
            outcome = await outcome
    except Erc8128Error:
        raise
    except Exception as exc:  # noqa: BLE001 - infra failure, NOT a bad signature
        logger.warning("ERC-8128 nonce store unavailable: %s", exc)
        raise _fail("nonce_store_unavailable", "Nonce store is unavailable") from exc

    if outcome == "ok":
        return
    if outcome == "replayed":
        raise _fail("nonce_replayed", "Nonce has already been used")
    if outcome == "unknown":
        raise _fail("nonce_unknown", "Nonce was not issued by this server")
    if outcome == "expired":
        raise _fail("nonce_expired", "Nonce has expired")
    if outcome == "unavailable":
        raise _fail("nonce_store_unavailable", "Nonce store is unavailable")
    raise _fail("nonce_invalid", f"Nonce store returned an unknown outcome: {outcome!r}")


async def _recover(
    base: str, signature: bytes, parsed: ParsedSignatureInput, policy: VerifyPolicy
) -> str:
    from eth_account import Account  # lazy: base install has no eth-account
    from eth_account.messages import encode_defunct

    try:
        recovered = Account.recover_message(
            encode_defunct(text=base), signature=signature
        ).lower()
    except Exception as exc:  # noqa: BLE001 - malformed signature bytes
        raise _fail("signature_invalid", "Ethereum signature recovery failed") from exc

    if recovered == parsed.wallet:
        return "eoa"

    if policy.contract_verifier is not None:
        try:
            verdict = policy.contract_verifier(
                address=parsed.wallet,
                chain_id=parsed.chain_id,
                message_hash=eip191_message_hash(base),
                signature=signature,
            )
            if inspect.isawaitable(verdict):
                verdict = await asyncio.wait_for(
                    verdict, timeout=policy.contract_verifier_timeout
                )
            if verdict:
                return "erc1271"
        except asyncio.TimeoutError as exc:
            raise _fail(
                "wallet_mismatch",
                f"ERC-1271 verification timed out for {parsed.wallet} "
                f"on chain {parsed.chain_id}",
            ) from exc
        except Exception as exc:  # noqa: BLE001 - contract call failure
            raise _fail(
                "wallet_mismatch",
                f"ERC-1271 verification failed for {parsed.wallet}: {exc}",
            ) from exc

    raise _fail("wallet_mismatch", "Recovered wallet does not match the ERC-8128 keyid")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def verify_request(
    request: VerifiableRequest, policy: VerifyPolicy
) -> VerifyResult:
    """Verify an ERC-8128 signed request. Never raises for a bad signature —
    a failure is a :class:`VerifyResult` with a stable ``code``/``status``.
    """
    state: Dict[str, Any] = {}
    try:
        result = await _verify(request, policy, state)
    except Erc8128Error as err:
        parsed: Optional[ParsedSignatureInput] = state.get("parsed")
        result = VerifyResult(
            ok=False,
            code=err.code,
            message=err.message,
            status=err.status,
            retryable=err.retryable,
            observed_profile=parsed.observed_profile if parsed else None,
            # `wallet` stays None on every rejection, deliberately.
            #
            # parsed.wallet is the address the CLIENT wrote into its keyid. On
            # the accept path it has been checked against the address recovered
            # from the signature, which is what makes it an identity. On this
            # path that check either failed or never ran, so the value is
            # nothing but attacker-controlled input — and handing it back in a
            # field named `wallet` invites a caller who forgets to test `ok`
            # into treating it as an authenticated principal.
            #
            # The TypeScript VerifyResult has no `wallet` member at all in its
            # failure variant, so this is also what makes the two agree.
            chain_id=parsed.chain_id if parsed else None,
            keyid=parsed.keyid if parsed else None,
            label=parsed.label if parsed else None,
        )
    _fire_census(policy, state.get("parsed"), result)
    return result


async def _verify(
    request: VerifiableRequest, policy: VerifyPolicy, state: Dict[str, Any]
) -> VerifyResult:
    now = int(policy.now()) if policy.now is not None else int(time.time())

    authority = policy_authority(policy.authority)

    sig_input_raw = _header(request, "signature-input")
    sig_raw = _header(request, "signature")
    if not sig_input_raw:
        raise _fail("signature_input_invalid", "Missing Signature-Input header")
    if not sig_raw:
        raise _fail("signature_invalid", "Missing Signature header")

    parsed = parse_signature_input(sig_input_raw)
    state["parsed"] = parsed

    if policy.label != "any" and parsed.label != policy.label:
        raise _fail(
            "signature_input_invalid",
            f"Signature label '{parsed.label}' is not accepted",
        )
    signature = parse_signature_header(sig_raw, parsed.label)

    _check_accept_profile(parsed, policy)
    _check_freshness(parsed, policy, now)

    if policy.allowed_chain_ids is not None and parsed.chain_id not in tuple(
        policy.allowed_chain_ids
    ):
        raise _fail("chain_not_allowed", "ERC-8128 chain id is not allowed")

    if (
        parsed.nonce is None
        and policy.nonce is not None
        and policy.nonce.mode == "required"
    ):
        raise _fail("nonce_required", "Replayable signatures are not accepted (no nonce)")

    # Digest BEFORE components: a bodied request that did not sign the digest
    # must answer content_digest_required, not components_invalid — that is
    # the order both live verifiers produce and what the vector matrix pins.
    content_digest = _check_content_digest(request, parsed, policy)
    path, query = _path_and_query(request.url)
    _check_components(parsed, policy, query, content_digest is not None)

    if policy.nonce is not None and policy.nonce.consume == "before-verify":
        await _consume_nonce(parsed, policy)

    base = build_signature_base(
        CanonicalMessage(
            method=request.method,
            authority=authority,
            path=path,
            query=query,
            content_digest=content_digest,
            covered=parsed.covered,
            params=parsed.params_raw,
            extra_components=_resolve_extra_components(request, parsed.covered),
        )
    )
    via = await _recover(base, signature, parsed, policy)

    if policy.nonce is not None and policy.nonce.consume == "after-verify":
        await _consume_nonce(parsed, policy)

    return VerifyResult(
        ok=True,
        wallet=parsed.wallet,
        chain_id=parsed.chain_id,
        keyid=parsed.keyid,
        label=parsed.label,
        nonce=parsed.nonce,
        created=parsed.created,
        expires=parsed.expires,
        signature_base=base,
        observed_profile=parsed.observed_profile,
        via=via,
    )


def policy_authority(authority: str) -> str:
    """The CONFIGURED authority: lowercased, validated, and NEVER re-ported.

    This is deliberately NOT :func:`~uvd_x402_sdk.erc8128.core.normalize_authority`.
    That function needs a scheme to know which port is the default one; the
    configured value carries none, so applying it here means guessing — and
    every guess is wrong for some real deployment. Measured, over the four
    shapes and the ``@authority`` each one actually signs::

        deploy          signed @authority   assume https   drop either port
        https on :443   host                OK             OK
        http  on :80    host                BROKEN         OK
        https on :80    host:80             OK             BROKEN
        http  on :443   host:443            BROKEN         BROKEN

    So neither guess is taken. The configured value is the EXPECTED RESULT of
    the URL-derived rule, so it must already be written in that form, and a
    port it does carry is preserved verbatim (``host:8443`` stays
    ``host:8443``).

    Fatal (``authority_invalid`` ⇒ 503 — the OPERATOR misconfigured the server;
    answering 401 would blame the client for a typo it cannot see):

    * empty, or empty after trimming
    * longer than 253 characters
    * ANY whitespace — space, tab, CR, LF, VT, FF and U+00A0 NBSP, none of
      which may reach the rebuilt signature base — or any of ``/ @ ? #``,
      which is also how a pasted URL (``https://host``, the ``//``) and a path
      component (``host/x``) are caught
    * a port that is the default for EITHER scheme (``:443`` / ``:80``). This
      one gets its own message, because the fix is specific: write the
      authority the way it will be signed, with the default port omitted.
      Silently stripping it is what broke https-on-``:80``, whose signer really
      does sign ``host:80``.
    """
    # Normalise FIRST, then test — including the emptiness test. A value that
    # is only whitespace normalises to "" and must not reach the base builder;
    # rebuilding `"@authority": ` with nothing after it is a signature over an
    # authority nobody configured.
    value = (authority or "").strip().lower()
    if (
        not value
        or len(value) > 253
        or any(ch in _FORBIDDEN_AUTHORITY_CHARS for ch in value)
    ):
        raise Erc8128Error(
            "authority_invalid", "ERC-8128 authority is not configured safely"
        )

    port_at = value.rfind(":")
    # `rfind("]")` is what keeps an IPv6 literal's own colons from reading as a
    # port: in `[::1]:443` the last ':' is after the bracket, in `[::1]` it is
    # not.
    if port_at > value.rfind("]"):
        port = value[port_at + 1 :]
        if port in _DEFAULT_PORT_VALUES:
            raise Erc8128Error(
                "authority_invalid",
                "ERC-8128 authority must be configured exactly as it is signed: "
                f"the default port ':{port}' is never part of @authority — "
                f"configure '{value[:port_at]}', not '{value}'",
            )
    return value


def _resolve_extra_components(
    request: VerifiableRequest, covered: Sequence[str]
) -> Dict[str, str]:
    """Values for covered components the base builder does not derive.

    A superset covered list is accepted under ``request-bound-subset`` (EM's
    posture); an absent header resolves to the empty string, matching EM. It
    binds nothing extra — the client signed it themselves.
    """
    extra: Dict[str, str] = {}
    for component in covered:
        if component.startswith("@") or component == "content-digest":
            continue
        extra[component.lower()] = _header(request, component) or ""
    return extra


def _fire_census(
    policy: VerifyPolicy,
    parsed: Optional[ParsedSignatureInput],
    result: VerifyResult,
) -> None:
    """Deprecation census. Fires on SUCCESSFUL **and FAILED** verifications,
    tagged with the outcome — otherwise "zero legacy for 14 days" is satisfied
    by legacy emitters that are already failing for another reason, and the
    hardening flip turns a recoverable outage into a permanent one.

    Aggregate by ``observed_profile`` (3 values). The keyid is passed for
    context only: aggregating by it is per-wallet cardinality.
    """
    if policy.on_observed_profile is None or parsed is None:
        return
    try:
        policy.on_observed_profile(
            parsed.observed_profile,
            {
                "wallet": parsed.wallet,
                "chain_id": parsed.chain_id,
                "keyid": parsed.keyid,
                "outcome": "ok" if result.ok else result.code,
            },
        )
    except Exception as exc:  # noqa: BLE001 - a metrics hook must not 500 auth
        logger.warning("ERC-8128 observed-profile hook raised: %s", exc)


__all__ = [
    "ERC8128_ERROR_RETRYABLE",
    "ERC8128_ERROR_STATUS",
    "VerifiableRequest",
    "VerifyPolicy",
    "VerifyResult",
    "verify_request",
]
