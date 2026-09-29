"""payment_failure_verdict(): may the buyer be asked to pay again?

Three answers: ``rejected`` (nothing moved, a 402 is safe), ``transient`` (no
verdict yet, 503 and the SAME credential) and ``unconfirmed`` (the payment may
have moved, never ask for another one). No network: the exceptions are the
SDK's own, built the way the client raises them.

The rule pinned by ``TestSameDecisionAsTheIntegrations``: with
``settle_attempted=True`` the verdict is exactly what the integrations answer
(``_undelivered_response``): rejected iff ``None``, transient iff ``503``,
unconfirmed iff ``409`` or ``500``, and ``body`` / ``headers`` are that answer.
"""
import dataclasses
import json
from typing import Any, Callable, NamedTuple, Optional

import pytest

import uvd_x402_sdk
import uvd_x402_sdk.client as client_module
from uvd_x402_sdk import (
    PAYMENT_REJECTED,
    PAYMENT_TRANSIENT,
    PAYMENT_UNCONFIRMED,
    PaymentFailureVerdict,
    payment_failure_verdict,
)
from uvd_x402_sdk.exceptions import (
    PAYMENT_ALREADY_USED,
    PAYMENT_STORE_UNAVAILABLE,
    FacilitatorError,
    InvalidPayloadError,
    PaymentBindingError,
    PaymentSettlementError,
    PaymentVerificationError,
    X402Error,
)
from uvd_x402_sdk.exceptions import (
    TimeoutError as X402TimeoutError,
)

TX = "0x" + "ab" * 32
PAYMENT_ID = "0x" + "cd" * 32

R, T, U = PAYMENT_REJECTED, PAYMENT_TRANSIENT, PAYMENT_UNCONFIRMED


def facilitator(
    status: Optional[int], body: Optional[dict[str, Any]] = None, **kwargs: Any
) -> FacilitatorError:
    return FacilitatorError(
        "facilitator error",
        status_code=status,
        response_body=None if body is None else json.dumps(body),
        **kwargs,
    )


class Case(NamedTuple):
    make: Callable[[], BaseException]
    #: Verdict with settle_attempted=True, then with settle_attempted=False.
    settled: str
    verified_only: str


CASES: dict[str, Case] = {
    # A transaction on the failure: broadcast, may be mined. Always.
    "502 settlement_unconfirmed with a hash": Case(
        lambda: facilitator(502, {
            "error": "settlement_unconfirmed", "transaction": TX,
            "paymentId": PAYMENT_ID, "retryable": False,
        }, operation="settle"),
        U, U,
    ),
    "500 with a hash": Case(lambda: facilitator(500, {"error": "boom", "transaction": TX}), U, U),
    "settle error with tx_hash": Case(
        lambda: PaymentSettlementError("reverted", network="arc", tx_hash=TX), U, U
    ),
    # The receipt rail's admitted authorization.
    "409 authorization_already_settled": Case(
        lambda: facilitator(409, {"error": "authorization_already_settled"}, operation="settle"),
        U, U,
    ),
    "409 receipt_request_conflict": Case(
        lambda: facilitator(409, {"error": "receipt_request_conflict"}, operation="settle"), U, U
    ),
    "409 authorization_in_flight": Case(
        lambda: facilitator(409, {"error": "authorization_in_flight"}, operation="settle"), T, T
    ),
    "verify authorization_in_flight": Case(
        lambda: PaymentVerificationError("in flight", reason="authorization_in_flight"), T, T
    ),
    # Spent before transient: a 5xx that also names a used authorization.
    "503 idempotency_cache_corrupt": Case(
        lambda: facilitator(503, {"error": "idempotency_cache_corrupt"}, operation="settle"), U, U
    ),
    "500 nonce_already_used": Case(lambda: facilitator(500, {"error": "nonce_already_used"}), U, U),
    # No verdict: the same credential later.
    "503 with Retry-After": Case(
        lambda: facilitator(503, {"error": "upstream_rpc_unavailable"}, retry_after=7.0), T, T
    ),
    "202 settlement_in_progress": Case(
        lambda: facilitator(202, {"error": "settlement_in_progress"}, operation="settle"), T, T
    ),
    "429": Case(lambda: facilitator(429, {"error": "rate_limited"}), T, T),
    "transport error": Case(lambda: facilitator(None), T, T),
    "timeout": Case(lambda: X402TimeoutError(operation="settle", timeout_seconds=1), T, T),
    # A settle 5xx the facilitator said not to resend: may come after the
    # broadcast. /verify alone never moves money.
    "502 settlement_unconfirmed without a hash": Case(
        lambda: facilitator(502, {"error": "settlement_unconfirmed", "retryable": False}), U, R
    ),
    "502 broadcast_uncertain without a hash": Case(
        lambda: facilitator(502, {"error": "broadcast_uncertain", "retryable": False}), U, R
    ),
    "500 retryable false": Case(
        lambda: facilitator(500, {"error": "internal_error", "retryable": False}), U, R
    ),
    # Up to x402-rs 2.39.5 these carried no `retryable: false`: transient, as
    # the integrations answer them today (503, the same credential).
    "502 broadcast_uncertain, no retryable": Case(
        lambda: facilitator(502, {"error": "broadcast_uncertain"}), T, T
    ),
    # A settle 4xx after /verify accepted the same payload.
    "400 contract_call_failed (ref) on settle": Case(
        lambda: facilitator(400, {"error": "contract_call_failed (ref: x)"}, operation="settle"),
        U, U,
    ),
    "400 contract_call_failed (ref) on verify": Case(
        lambda: facilitator(400, {"error": "contract_call_failed (ref: x)"}, operation="verify"),
        R, R,
    ),
    # Refusals: nothing ran.
    "403 on settle": Case(
        lambda: facilitator(403, {"error": "Address blocked"}, operation="settle"), R, R
    ),
    "verification error": Case(
        lambda: PaymentVerificationError("invalid signature", reason="invalid_signature"), R, R
    ),
    "settlement error, no hash": Case(
        lambda: PaymentSettlementError("insufficient funds", reason="insufficient_funds"), R, R
    ),
    "invalid payload": Case(lambda: InvalidPayloadError("not base64"), R, R),
    # The seller's own binding.
    "binding: store unavailable": Case(
        lambda: PaymentBindingError(PAYMENT_STORE_UNAVAILABLE, "store down"), T, T
    ),
    "binding: already used": Case(
        lambda: PaymentBindingError(PAYMENT_ALREADY_USED, "another resource"), U, U
    ),
    # Outside the SDK's hierarchy: after a settle nobody can say it did not move.
    "ValueError": Case(lambda: ValueError("boom"), U, R),
}

IDS = list(CASES)


@pytest.mark.parametrize("name", IDS)
def test_verdict_after_a_settle(name: str) -> None:
    case = CASES[name]
    assert payment_failure_verdict(case.make(), settle_attempted=True).kind == case.settled


@pytest.mark.parametrize("name", IDS)
def test_verdict_when_only_verify_ran(name: str) -> None:
    case = CASES[name]
    assert payment_failure_verdict(case.make(), settle_attempted=False).kind == case.verified_only
    assert payment_failure_verdict(case.make()).kind == case.verified_only


class TestSameDecisionAsTheIntegrations:
    """The property: with a settle attempted, one decision, not two."""

    @pytest.mark.parametrize("name", IDS)
    def test_kind_matches_the_answer(self, name: str) -> None:
        exc = CASES[name].make()
        verdict = payment_failure_verdict(exc, settle_attempted=True)
        answer = client_module._undelivered_response(exc)  # type: ignore[arg-type]
        if isinstance(exc, X402Error):
            assert (answer is None) == (verdict.kind == PAYMENT_REJECTED)
            assert (answer is not None and answer[0] == 503) == (verdict.kind == PAYMENT_TRANSIENT)
            assert (answer is not None and answer[0] in (409, 500)) == (
                verdict.kind == PAYMENT_UNCONFIRMED
            )
        else:
            # Never reached by an integration (they catch X402Error); the rule
            # still holds.
            assert answer is not None and answer[0] == 500
            assert verdict.kind == PAYMENT_UNCONFIRMED

    @pytest.mark.parametrize("name", IDS)
    def test_body_and_headers_are_the_answer(self, name: str) -> None:
        exc = CASES[name].make()
        verdict = payment_failure_verdict(exc, settle_attempted=True)
        answer = client_module._undelivered_response(exc)  # type: ignore[arg-type]
        if answer is None:
            assert verdict.body is None and verdict.headers is None
        else:
            assert (verdict.body, verdict.headers) == (answer[1], answer[2])

    def test_every_status_the_integrations_answer_is_covered(self) -> None:
        statuses = set()
        for case in CASES.values():
            answer = client_module._undelivered_response(case.make())  # type: ignore[arg-type]
            statuses.add(None if answer is None else answer[0])
        assert statuses == {None, 409, 500, 503}


def test_the_hash_and_the_payment_id_travel() -> None:
    verdict = payment_failure_verdict(
        CASES["502 settlement_unconfirmed with a hash"].make(), settle_attempted=True
    )
    assert verdict.transaction == TX
    assert verdict.payment_id == PAYMENT_ID
    assert verdict.error_code == "settlement_unconfirmed"
    assert verdict.retry_after is None
    assert verdict.may_have_moved and not verdict.may_ask_new_payment
    assert verdict.body is not None and verdict.body["transaction"] == TX


def test_a_transient_verdict_carries_retry_after() -> None:
    verdict = payment_failure_verdict(CASES["503 with Retry-After"].make(), settle_attempted=True)
    assert verdict.kind == PAYMENT_TRANSIENT
    assert verdict.retry_after == 7
    assert verdict.headers is not None and verdict.headers["Retry-After"] == "7"
    assert not verdict.may_have_moved and not verdict.may_ask_new_payment


def test_a_fractional_retry_after_is_rounded_up() -> None:
    verdict = payment_failure_verdict(facilitator(503, {"error": "x"}, retry_after=2.5))
    assert verdict.retry_after == 3


def test_the_admitted_code_is_the_error_code() -> None:
    verdict = payment_failure_verdict(CASES["verify authorization_in_flight"].make())
    assert verdict.error_code == "authorization_in_flight"


def test_a_rejection_carries_no_answer() -> None:
    verdict = payment_failure_verdict(CASES["verification error"].make(), settle_attempted=True)
    assert verdict.may_ask_new_payment and not verdict.may_have_moved
    assert (verdict.body, verdict.headers, verdict.retry_after) == (None, None, None)


def test_a_settle_failure_counts_as_attempted_whatever_the_caller_says() -> None:
    exc = facilitator(500, {"error": "internal_error", "retryable": False}, operation="settle")
    assert payment_failure_verdict(exc, settle_attempted=False).kind == PAYMENT_UNCONFIRMED


def test_an_unknown_exception_body_carries_nothing_of_its_text() -> None:
    verdict = payment_failure_verdict(ValueError("secret detail"), settle_attempted=True)
    assert verdict.body is not None
    assert "secret detail" not in json.dumps(verdict.body)
    assert verdict.body["details"] == {"exception": "ValueError"}


class _UnreadableError(X402Error):
    def to_dict(self) -> dict[str, Any]:
        raise RuntimeError("cannot serialise")


@pytest.mark.parametrize("settle_attempted, kind", [(True, U), (False, R)])
def test_never_raises(settle_attempted: bool, kind: str) -> None:
    exc = _UnreadableError("x", details={"retryable": True})
    verdict = payment_failure_verdict(exc, settle_attempted=settle_attempted)
    assert verdict == PaymentFailureVerdict(kind=kind)


def test_the_verdict_is_frozen() -> None:
    verdict = PaymentFailureVerdict(kind=PAYMENT_REJECTED)
    with pytest.raises(dataclasses.FrozenInstanceError):
        verdict.kind = PAYMENT_UNCONFIRMED  # type: ignore[misc]


def test_exported() -> None:
    assert (PAYMENT_REJECTED, PAYMENT_TRANSIENT, PAYMENT_UNCONFIRMED) == (
        "rejected", "transient", "unconfirmed"
    )
    for name in (
        "payment_failure_verdict", "PaymentFailureVerdict",
        "PAYMENT_REJECTED", "PAYMENT_TRANSIENT", "PAYMENT_UNCONFIRMED",
    ):
        assert name in uvd_x402_sdk.__all__
        assert getattr(uvd_x402_sdk, name) is getattr(client_module, name)
