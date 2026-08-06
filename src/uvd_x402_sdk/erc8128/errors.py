"""ERC-8128 error taxonomy — the SAME code strings as the TypeScript SDK.

The codes are a superset of MeshRelay's public HTTP contract (19 codes with a
status per code, ``meshrelay/api/src/lib/erc8128-auth.ts``) union Execution
Market's one stable string (``nonce_store_unavailable``, string-compared in
``mcp_server/api/auth.py`` to answer 503-retry instead of a terminal 401).

Consumers must be able to keep answering with the same status and the same
``code`` field after adopting the SDK, so neither the spelling nor the status
of an existing code may change without a MAJOR bump.
"""

from __future__ import annotations

from typing import Dict, Optional

#: Every error code the verifier can return. Same strings as ``Erc8128Code``
#: in the TypeScript SDK — a consumer switching on them must not have to care
#: which language produced the result.
ERC8128_CODES = (
    "signature_input_invalid",
    "signature_invalid",
    "alg_unsupported",
    "alg_missing",
    "keyid_not_lowercase",
    "nonce_invalid",
    "nonce_unknown",
    "nonce_replayed",
    "nonce_expired",
    "nonce_required",
    "nonce_rate_limited",
    "nonce_capacity",
    "nonce_limits_invalid",
    "nonce_ttl_invalid",
    "nonce_store_unavailable",
    "content_digest_required",
    "content_digest_invalid",
    "content_digest_mismatch",
    "components_invalid",
    "class_bound_rejected",
    "signature_stale",
    "chain_not_allowed",
    "wallet_invalid",
    "wallet_mismatch",
    "authority_invalid",
)

#: HTTP status per code. 401 = the signature is not acceptable, 409 = the
#: nonce was already spent (a distinct outcome a client can react to), 429 =
#: back off, 503 = the SERVER is not able to answer right now.
ERC8128_ERROR_STATUS: Dict[str, int] = {
    "signature_input_invalid": 401,
    "signature_invalid": 401,
    "alg_unsupported": 401,
    "alg_missing": 401,
    "keyid_not_lowercase": 401,
    "nonce_invalid": 401,
    "nonce_unknown": 401,
    "nonce_replayed": 409,
    "nonce_expired": 401,
    "nonce_required": 401,
    "nonce_rate_limited": 429,
    "nonce_capacity": 429,
    "nonce_limits_invalid": 503,
    "nonce_ttl_invalid": 503,
    "nonce_store_unavailable": 503,
    "content_digest_required": 401,
    "content_digest_invalid": 401,
    "content_digest_mismatch": 401,
    "components_invalid": 401,
    "class_bound_rejected": 401,
    "signature_stale": 401,
    "chain_not_allowed": 401,
    "wallet_invalid": 401,
    "wallet_mismatch": 401,
    "authority_invalid": 503,
}

#: Whether re-sending the SAME request could succeed later. This is what turns
#: EM's string-compare on ``nonce_store_unavailable`` into a lookup: a caller
#: answers 503 + Retry-After for retryable codes and a terminal error for the
#: rest. ``nonce_replayed`` is NOT retryable — the client needs a new nonce and
#: a new signature, not a retry.
ERC8128_ERROR_RETRYABLE: Dict[str, bool] = {code: False for code in ERC8128_CODES}
ERC8128_ERROR_RETRYABLE.update(
    {
        "nonce_store_unavailable": True,
        "nonce_rate_limited": True,
        "nonce_capacity": True,
    }
)


class Erc8128Error(Exception):
    """A verification failure carrying its stable code, status and retryability.

    ``verify_request`` never lets this escape — it converts it into a
    :class:`~uvd_x402_sdk.erc8128.verifier.VerifyResult`. The pure parsers
    (:func:`parse_signature_input`, :func:`parse_signature_header`) raise it,
    so a caller using them standalone gets the same taxonomy.
    """

    def __init__(self, code: str, message: str, status: Optional[int] = None) -> None:
        super().__init__(message)
        if code not in ERC8128_ERROR_STATUS:
            raise ValueError(f"unknown ERC-8128 error code: {code}")
        self.code = code
        self.message = message
        self.status = status if status is not None else ERC8128_ERROR_STATUS[code]
        self.retryable = ERC8128_ERROR_RETRYABLE[code]


__all__ = [
    "ERC8128_CODES",
    "ERC8128_ERROR_STATUS",
    "ERC8128_ERROR_RETRYABLE",
    "Erc8128Error",
]
