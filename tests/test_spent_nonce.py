"""Spent-nonce classification: did the facilitator say this authorization was ALREADY USED?

Ported from tarotof's paywall (``api/main.py``, ``_codigo_de_nonce_gastado`` and
``_huele_a_nonce_gastado``; the cases below are those of ``api/test_api.py``),
because the question is not tarotof's. Every x402 server has to decide what to
tell a buyer whose settle failed, and a 402 says "sign a new authorization":
said over one that already settled, that is how a buyer pays twice. Hence the
bias of the text heuristic: a false positive costs the buyer a lookup, a false
negative costs money.

A structured code wins over the wording, because a code is a contract and a
message is prose somebody rewrites.
"""
import json

import pytest

from uvd_x402_sdk import is_spent_nonce_error, spent_nonce_evidence
from uvd_x402_sdk.exceptions import (
    FacilitatorError,
    PaymentSettlementError,
    PaymentVerificationError,
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


# -- what this SDK adds, measured against x402-rs 2.28.0


def test_an_idempotency_key_conflict_reads_as_already_settled():
    """x402-rs caches only a SUCCESSFUL settle under an ``Idempotency-Key``
    (``post_settle``: the cache write is guarded by ``valid_response.success``),
    so its ``409 idempotency_key_conflict`` means a settle under that key already
    succeeded with a different body. With the key this SDK derives from the
    authorization, that is this authorization, settled. Read as a plain 4xx, a
    paywall turns it into a 402 and the buyer signs again."""
    exc = _facilitator(409, {"error": "idempotency_key_conflict", "correlation_id": "local"})
    assert spent_nonce_evidence(exc) == "structured"


def test_an_unreadable_idempotency_store_is_not_a_spent_nonce():
    # 503, fail-closed: nothing settled. `is_transient_error` owns this one.
    exc = _facilitator(503, {"error": "idempotency_store_unavailable", "correlation_id": "local"})
    assert not is_spent_nonce_error(exc)


@pytest.mark.parametrize(
    "error",
    [
        # x402-rs chain/failure.rs, Reason::NonceOrMempool, client_message().
        "The node refused this transaction on nonce or mempool grounds and never "
        "queued it. Retry later.",
        # The same failure on the ContractCall arm: the category token.
        "upstream_nonce_or_mempool (ref: local)",
    ],
)
def test_the_facilitators_own_transaction_nonce_is_not_the_payers_authorization(error):
    """The facilitator's signer nonce, refused by the node and never queued:
    transient, and retrying the SAME credential is exactly right.

    tarotof matched SUBSTRINGS, and ``"refused"`` contains ``"used"``, so this
    message, next to the word ``nonce``, read as a spent authorization. Matching
    by whole word is what keeps it out."""
    exc = _facilitator(502, {"error": error, "retryable": True})
    assert spent_nonce_evidence(exc) is None


def test_unused_is_not_used():
    exc = _facilitator(400, {"message": "the nonce is unused"})
    assert spent_nonce_evidence(exc) is None
