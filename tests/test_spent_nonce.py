"""Spent-nonce classification: did the facilitator say this authorization was ALREADY USED?

Ported from tarotof's paywall (``api/main.py``, ``_codigo_de_nonce_gastado`` and
``_huele_a_nonce_gastado``; the cases below include those of ``api/test_api.py``),
because the question is not tarotof's. Every x402 server has to decide what to
tell a buyer whose settle failed, and a 402 says "sign a new authorization":
said over one that already settled, that is how a buyer pays twice. Hence the
bias of the text heuristic: a false positive costs the buyer a lookup, a false
negative costs money.

Upstream-first: tarotof must be able to adopt this without reading LESS. The
fixed table below pins that, row by row, against tarotof's own verdicts.

A structured code wins over the wording, because a code is a contract and a
message is prose somebody rewrites.
"""
# ruff: noqa: E501 - the fixed table below keeps one row per line on purpose: it
# is generated output, and a wrapped row stops matching what produced it.
import json

import pytest

from uvd_x402_sdk import is_spent_nonce_error, is_transient_error, spent_nonce_evidence
from uvd_x402_sdk.exceptions import (
    FacilitatorError,
    PaymentSettlementError,
    PaymentVerificationError,
)
from uvd_x402_sdk.exceptions import TimeoutError as X402TimeoutError

#: x402-rs chain/failure.rs, Reason::NonceOrMempool, client_message().
REFUSED = (
    "The node refused this transaction on nonce or mempool grounds and never queued it. "
    "Retry later."
)


def _facilitator(status: int, body: dict) -> FacilitatorError:
    return FacilitatorError(
        f"Facilitator settle failed with status {status}",
        status_code=status,
        response_body=json.dumps(body),
    )


# -- the two cases the port was asked for (tarotof api/test_api.py:563 and :650)


def test_a_spent_nonce_is_recognised():
    exc = PaymentSettlementError(
        "Payment settlement failed: nonce already used",
        network="base",
        reason="nonce already used",
    )
    assert is_spent_nonce_error(exc)


def test_a_foreign_code_is_not_mistaken_for_a_spent_nonce():
    exc = _facilitator(400, {"code": "INSUFFICIENT_BALANCE"})
    assert not is_spent_nonce_error(exc)
    assert spent_nonce_evidence(exc) is None


# -- the rest of tarotof's cases


def test_the_structured_code_beats_the_wording():
    # The free message says nothing about nonces; the CODE does.
    exc = _facilitator(
        409, {"code": "NONCE_ALREADY_USED", "message": "request could not proceed"}
    )
    assert spent_nonce_evidence(exc) == "structured"


def test_the_wording_is_the_fallback():
    exc = _facilitator(409, {"message": "settlement rejected, the nonce was already spent"})
    assert spent_nonce_evidence(exc) == "wording"


def test_a_settle_error_reason_is_read_as_a_code_before_as_text():
    as_code = PaymentSettlementError("settle failed", network="base", reason="nonce_used")
    as_text = PaymentSettlementError(
        "settle failed",
        network="base",
        reason="the authorization nonce was consumed earlier",
    )
    assert spent_nonce_evidence(as_code) == "structured"
    assert spent_nonce_evidence(as_text) == "wording"


@pytest.mark.parametrize("code", ["NONCE_ALREADY_USED", "nonce-already-used", "alreadySettled"])
def test_code_spellings_are_normalised(code):
    assert spent_nonce_evidence(_facilitator(400, {"errorCode": code})) == "structured"


def test_an_unrelated_code_and_an_empty_body_say_nothing():
    assert spent_nonce_evidence(_facilitator(429, {"errorCode": "RATE_LIMITED"})) is None
    assert spent_nonce_evidence(FacilitatorError("Facilitator request failed")) is None


def test_a_nested_code_is_found():
    exc = _facilitator(400, {"errors": [{"detail": {"code": "duplicate_nonce"}}]})
    assert spent_nonce_evidence(exc) == "structured"


def test_a_verification_failure_is_judged_too():
    exc = PaymentVerificationError("Payment verification failed", reason="nonce_already_used")
    assert spent_nonce_evidence(exc) == "structured"


def test_an_exception_outside_the_payment_path_is_not_judged():
    assert spent_nonce_evidence(RuntimeError("nonce already used")) is None
    assert not is_spent_nonce_error(ValueError("already settled"))


# -- the four inputs the review measured: tarotof reads them as spent, and a
#    whole-word match did not (a 402, and the buyer signs and pays again)


@pytest.mark.parametrize(
    "exc",
    [
        _facilitator(400, {"error": "invalid payment: NonceAlreadyUsed"}),
        _facilitator(400, {"error": 'NonceReused { from: "G", nonce: 5 }'}),
        _facilitator(400, {"error": "nonce reused"}),
        PaymentSettlementError(
            "Payment settlement failed: error: NonceAlreadyUsed",
            network="base",
            reason="error: NonceAlreadyUsed",
        ),
    ],
    ids=["camel-in-prose", "rust-debug-reused", "prose-reused", "settle-reason-camel"],
)
def test_camel_case_and_reused_inside_prose_and_structs_read_as_spent(exc):
    assert is_spent_nonce_error(exc)


# -- what this SDK adds, measured against x402-rs 2.28.0 and USDC


def test_an_idempotency_key_conflict_reads_as_already_settled():
    """x402-rs caches only a SUCCESSFUL settle under an ``Idempotency-Key``
    (``post_settle``: the cache write is guarded by ``valid_response.success``),
    so its ``409 idempotency_key_conflict`` means a settle under that key already
    succeeded with a different body. With the key this SDK derives from the
    authorization, that is this authorization, settled. Read as a plain 4xx, a
    paywall turns it into a 402 and the buyer signs again."""
    exc = _facilitator(409, {"error": "idempotency_key_conflict", "correlation_id": "local"})
    assert spent_nonce_evidence(exc) == "structured"


def test_a_corrupt_cache_record_reads_as_already_settled():
    """``503 idempotency_cache_corrupt`` exists only on the replay branch: a
    record under this key with the SAME body hash, i.e. this exact settle
    succeeded, that cannot be parsed back. As a mere transient it is 24 hours of
    503s over a payment that moved."""
    exc = _facilitator(503, {"error": "idempotency_cache_corrupt"})
    assert spent_nonce_evidence(exc) == "structured"


@pytest.mark.parametrize(
    "exc",
    [
        _facilitator(400, {"error": "FiatTokenV2: authorization is used or canceled"}),
        PaymentVerificationError(
            "Payment verification failed",
            reason="FiatTokenV2: authorization is used or canceled",
        ),
    ],
    ids=["facilitator-body", "verification-reason"],
)
def test_the_usdc_revert_for_a_used_authorization_reads_as_spent(exc):
    # USDC's own words; no "nonce" in them, so no nonce heuristic could catch it.
    assert spent_nonce_evidence(exc) == "wording"


def test_an_unreadable_idempotency_store_is_not_a_spent_nonce():
    # 503, fail-closed: nothing settled. `is_transient_error` owns this one.
    exc = _facilitator(503, {"error": "idempotency_store_unavailable", "correlation_id": "local"})
    assert not is_spent_nonce_error(exc)


@pytest.mark.parametrize(
    "error",
    [REFUSED, "upstream_nonce_or_mempool (ref: local)"],
    ids=["client-message", "category-token"],
)
def test_the_facilitators_own_transaction_nonce_is_not_the_payers_authorization(error):
    """The facilitator's signer nonce, refused by the node and never queued:
    transient, and retrying the SAME credential is exactly right.

    tarotof matched substrings, and ``"refused"`` contains ``"used"``, so this
    message, next to the word ``nonce``, read as a spent authorization. This SDK
    keeps the substring match and removes ``refused`` before it."""
    exc = _facilitator(502, {"error": error, "retryable": True})
    assert spent_nonce_evidence(exc) is None


def test_unused_is_not_used():
    exc = _facilitator(400, {"message": "the nonce is unused"})
    assert spent_nonce_evidence(exc) is None


@pytest.mark.parametrize(
    "exc",
    [
        FacilitatorError("Facilitator request failed: [Errno 111] Connection refused"),
        X402TimeoutError(operation="settle", timeout_seconds=90),
        # x402-rs chain/failure.rs client_message() for Transport and RateLimited.
        _facilitator(
            502,
            {
                "error": "Upstream RPC unavailable for this network; the request was not "
                "rejected, the node could not answer. Retry later.",
                "retryable": True,
            },
        ),
        _facilitator(
            503,
            {
                "error": "The facilitator is being rate limited by this network's RPC "
                "provider. The request was not rejected. Retry later.",
                "retryable": True,
            },
        ),
        _facilitator(502, {"error": REFUSED, "retryable": True}),
        _facilitator(429, {"error": "rate_limited"}),
        _facilitator(503, {"error": "idempotency_store_unavailable", "correlation_id": "local"}),
    ],
    ids=[
        "transport",
        "timeout",
        "rpc-unavailable",
        "rpc-rate-limited",
        "nonce-or-mempool",
        "429",
        "store-unavailable",
    ],
)
def test_a_transient_network_or_rpc_failure_is_not_a_spent_nonce(exc):
    """A spent-nonce verdict tells the buyer "you may have paid, do not sign
    again"; over a failure where nothing reached the chain, that turns a retry
    into a lost sale. These stay with ``is_transient_error``: same credential,
    later."""
    assert is_transient_error(exc)
    assert spent_nonce_evidence(exc) is None


# -- the fixed table: tarotof's own classifier, run on one corpus

#: tarotof ``api/main.py`` at commit 534d133d9d5d21c4e2acc59ac29b5ecdc6e3773c
#: (``_codigo_de_nonce_gastado`` then ``_huele_a_nonce_gastado``, applied the way
#: its ``_traducir_error`` applies them: the details or the JSON body for the
#: code, ``message + reason`` or the raw body for the wording), executed on each
#: row. The last column is what it answered. tarotof has no public remote, so the
#: verdicts are copied here rather than computed in CI.
#: Rows: (id, kind, status | message, body | reason, tarotof verdict).
TAROTOF_534D133D = [
    ("tarotof-code", "facilitator", 409, {"code": "NONCE_ALREADY_USED", "message": "request could not proceed"}, "structured"),
    ("tarotof-wording", "facilitator", 409, {"message": "settlement rejected, the nonce was already spent"}, "wording"),
    ("tarotof-errorcode-upper", "facilitator", 400, {"errorCode": "NONCE_ALREADY_USED"}, "structured"),
    ("tarotof-errorcode-kebab", "facilitator", 400, {"errorCode": "nonce-already-used"}, "structured"),
    ("tarotof-errorcode-camel", "facilitator", 400, {"errorCode": "alreadySettled"}, "structured"),
    ("tarotof-settle-text", "settle", "Payment settlement failed: nonce already used", "nonce already used", "structured"),
    ("tarotof-settle-code", "settle", "settle failed", "nonce_used", "structured"),
    ("tarotof-settle-consumed", "settle", "settle failed", "the authorization nonce was consumed earlier", "wording"),
    ("camel-whole", "facilitator", 400, {"error": "NonceAlreadyUsed"}, "structured"),
    ("camel-in-prose", "facilitator", 400, {"error": "invalid payment: NonceAlreadyUsed"}, "wording"),
    ("rust-debug-reused", "facilitator", 400, {"error": 'NonceReused { from: "G", nonce: 5 }'}, "wording"),
    ("prose-reused", "facilitator", 400, {"error": "nonce reused"}, "wording"),
    ("settle-reason-camel", "settle", "Payment settlement failed: error: NonceAlreadyUsed", "error: NonceAlreadyUsed", "wording"),
    ("snake-invalid-reason", "facilitator", 400, {"invalidReason": "invalid_exact_evm_payload_authorization_nonce_used"}, "wording"),
    ("stellar-nonce-store", "facilitator", 400, {"error": "Nonce 5 already used for address GABC"}, "wording"),
    ("solana-replay-store", "facilitator", 400, {"error": "settlement account replay protection: Nonce already used: solana:abc"}, "wording"),
    ("algorand-replay", "facilitator", 400, {"error": "Transaction group already processed (replay attempt)"}, "wording"),
    ("stem-consumed", "facilitator", 400, {"error": "nonce consumed by an earlier settlement"}, "wording"),
    ("stem-duplicate", "facilitator", 400, {"error": "duplicate nonce"}, "structured"),
    ("stem-duplicate-prose", "facilitator", 400, {"error": "duplicate nonce detected in request"}, "wording"),
    ("stem-replay", "facilitator", 400, {"error": "nonce replay detected"}, "wording"),
    ("stem-spent", "facilitator", 400, {"error": "nonce spent"}, "structured"),
    ("cheap-eoa-nonce-used", "facilitator", 502, {"error": "nonce has already been used"}, "wording"),
    ("cheap-negation-consumed", "facilitator", 400, {"error": "Gas estimation reverted; no nonce consumed"}, "wording"),
    ("cheap-negation-not-used", "facilitator", 400, {"error": "the nonce was not used"}, "wording"),
    ("cheap-gas-used-echo", "facilitator", 500, {"error": "rpc_timeout", "nonce": "0x01", "gas_used": "21000"}, "wording"),
    ("excluded-refused", "facilitator", 502, {"error": REFUSED, "retryable": True}, "wording"),
    ("excluded-unused", "facilitator", 400, {"message": "the nonce is unused"}, "wording"),
    ("neg-insufficient-balance", "facilitator", 400, {"code": "INSUFFICIENT_BALANCE"}, None),
    ("neg-rate-limited", "facilitator", 429, {"errorCode": "RATE_LIMITED"}, None),
    ("neg-nonce-or-mempool-token", "facilitator", 502, {"error": "upstream_nonce_or_mempool (ref: local)", "retryable": True}, None),
    ("neg-store-unavailable", "facilitator", 503, {"error": "idempotency_store_unavailable", "correlation_id": "local"}, None),
    ("neg-contract-call-failed", "facilitator", 400, {"error": "contract_call_failed (ref: local)"}, None),
    ("neg-nonce-too-low", "facilitator", 502, {"error": "nonce too low: next nonce 7, tx nonce 6"}, None),
    ("neg-settle-balance", "settle", "Payment settlement failed: insufficient balance", "insufficient balance", None),
    ("sdk-key-conflict", "facilitator", 409, {"error": "idempotency_key_conflict", "correlation_id": "local"}, None),
    ("sdk-cache-corrupt", "facilitator", 503, {"error": "idempotency_cache_corrupt"}, None),
    ("sdk-usdc-revert", "facilitator", 400, {"error": "FiatTokenV2: authorization is used or canceled"}, None),
]

#: The only rows where this SDK reads LESS than tarotof, each measured.
EXCLUDED_ON_PURPOSE = {"excluded-refused", "excluded-unused"}
#: Rows this SDK reads as spent and tarotof does not.
ADDED_OVER_TAROTOF = {"sdk-key-conflict", "sdk-cache-corrupt", "sdk-usdc-revert"}


def _build(kind, first, second):
    if kind == "settle":
        return PaymentSettlementError(first, network="base", reason=second)
    return _facilitator(first, second)


@pytest.mark.parametrize(
    "row_id, kind, first, second, tarotof",
    TAROTOF_534D133D,
    ids=[row[0] for row in TAROTOF_534D133D],
)
def test_everything_tarotof_reads_as_spent_this_sdk_reads_too(row_id, kind, first, second, tarotof):
    """Where tarotof says ``structured``, so does the SDK; where it says
    ``wording``, the SDK reads it as spent too. The rows starting with ``cheap-``
    are tarotof's false positives, kept on purpose: a lookup, not a payment."""
    sdk = spent_nonce_evidence(_build(kind, first, second))
    if row_id in EXCLUDED_ON_PURPOSE:
        assert tarotof is not None and sdk is None
    elif row_id in ADDED_OVER_TAROTOF:
        assert tarotof is None and sdk is not None
    elif tarotof is None:
        assert sdk is None
    elif tarotof == "structured":
        assert sdk == "structured"
    else:
        assert sdk is not None
