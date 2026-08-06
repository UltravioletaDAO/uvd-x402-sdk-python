"""ERC-8128 Signed HTTP Requests (RFC 9421) — signer AND verifier.

Two functions carry the protocol: :func:`sign_request` signs,
:func:`verify_request` verifies. Everything that used to justify a private
copy of the wire format — strict vs lenient posture, pinned vs forwarded
authority, nonce consumption order, ``alg`` present or absent — is a value the
caller passes, not a branch of code.

    from uvd_x402_sdk.erc8128 import (
        POLICY_PRESETS, VerifiableRequest, policy_from_preset,
        sign_request, verify_request,
    )

    headers = sign_request(wallet, method="POST", url=url, body=body, nonce=nonce)

    policy = policy_from_preset("em-lenient", authority="api.execution.market",
                                nonce_store=my_store)
    result = await verify_request(
        VerifiableRequest(method="POST", url=url, headers=headers, raw_body=raw),
        policy,
    )

The verifier NEVER re-serialises ``@signature-params``: it feeds the verbatim
substring from ``Signature-Input`` to the same base builder the signer uses.
That single byte path is what makes both wire generations, a checksummed
keyid and any future RFC 9421 parameter verify with no flags.

The SDK ships no nonce store and never issues nonces — replay protection that
does not survive a restart or a second container is not replay protection.
"""

from __future__ import annotations

# Kept importable so a suite can freeze the clock (or fake httpx) by patching
# `uvd_x402_sdk.erc8128.time` / `.httpx`, the contract from when this package
# was a flat module. New code should pass `now=` instead.
import time  # noqa: F401
from typing import Any

import httpx  # noqa: F401

from uvd_x402_sdk.erc8128.core import (
    ALG,
    CONTENT_DIGEST_RE,
    DEFAULT_CHAIN_ID,
    DEFAULT_LABEL,
    DEFAULT_VALIDITY_SEC,
    KEYID_RE,
    WIRE_CONTRACT_VERSION,
    CanonicalMessage,
    ParsedSignatureInput,
    build_signature_base,
    build_signature_params,
    canonical_keyid,
    canonical_params,
    compute_content_digest,
    eip191_byte_length,
    eip191_message_hash,
    extract_keyid_wallet,
    normalize_authority,
    parse_signature_header,
    parse_signature_input,
    select_covered,
)
from uvd_x402_sdk.erc8128.errors import (
    ERC8128_CODES,
    ERC8128_ERROR_RETRYABLE,
    ERC8128_ERROR_STATUS,
    Erc8128Error,
)
from uvd_x402_sdk.erc8128.nonce import (
    IssuingNonceStore,
    NoncePolicy,
    NonceStore,
)
from uvd_x402_sdk.erc8128.presets import (
    POLICY_PRESETS,
    PRESET_NONCE_CONSUME,
    policy_from_preset,
    preset_as_data,
)
from uvd_x402_sdk.erc8128.signer import fetch_nonce, fetch_nonce_sync, sign_request
from uvd_x402_sdk.erc8128.vectors import (
    CONFORMANCE_SHA256,
    ConformanceReport,
    load_vectors,
    run_conformance,
    vector_bytes,
)
from uvd_x402_sdk.erc8128.verifier import (
    VerifiableRequest,
    VerifyPolicy,
    VerifyResult,
    verify_request,
)

_LAZY = {
    "CONFORMANCE_VECTORS_F3_1": "f3-1",
    "CONFORMANCE_VECTORS_F3_3": "f3-3",
}


def __getattr__(name: str) -> Any:
    # Lazy: `import uvd_x402_sdk` must not parse two JSON files.
    if name in _LAZY:
        return load_vectors(_LAZY[name])
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    # wire constants
    "ALG",
    "CONTENT_DIGEST_RE",
    "DEFAULT_CHAIN_ID",
    "DEFAULT_LABEL",
    "DEFAULT_VALIDITY_SEC",
    "KEYID_RE",
    "WIRE_CONTRACT_VERSION",
    # pure core
    "CanonicalMessage",
    "ParsedSignatureInput",
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
    # signer
    "fetch_nonce",
    "fetch_nonce_sync",
    "sign_request",
    # verifier
    "ERC8128_CODES",
    "ERC8128_ERROR_RETRYABLE",
    "ERC8128_ERROR_STATUS",
    "Erc8128Error",
    "IssuingNonceStore",
    "NoncePolicy",
    "NonceStore",
    "POLICY_PRESETS",
    "PRESET_NONCE_CONSUME",
    "VerifiableRequest",
    "VerifyPolicy",
    "VerifyResult",
    "policy_from_preset",
    "preset_as_data",
    "verify_request",
    # conformance
    "CONFORMANCE_SHA256",
    "CONFORMANCE_VECTORS_F3_1",
    "CONFORMANCE_VECTORS_F3_3",
    "ConformanceReport",
    "load_vectors",
    "run_conformance",
    "vector_bytes",
]
