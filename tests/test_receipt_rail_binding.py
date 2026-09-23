"""One purchase binding per payment handling, against the facilitator's receipt rail.

The rail (x402-rs 2.39.0, see ``tests/receipt_rail.py``) hands an admitted
payment's answer back only to the binding that admitted it. Possession of the
signed payment alone is not a binding. What that asks of this SDK, and what
these tests pin:

* a key that is never derived from the X-PAYMENT alone: random per handling
  (``new_idempotency_key``) unless the caller brings its own, so two handlings
  of the same X-PAYMENT send different keys;
* that key the same on ``/verify``, ``/settle``, the settle's retries and the
  timeout fallback's resend of one handling, so a lost answer is recovered;
* a replayed settle that reaches a handling with no binding of its own before
  any of its attempts could have admitted the payment refused, whatever made
  the key (a facilitator before 2.39.0 hands it to a bare resend);
* the rail's refusals classified: ``authorization_already_settled`` and
  ``receipt_request_conflict`` spent (409), ``authorization_in_flight``
  transient (503 + Retry-After), none of them a success, a rejection or a 402;
* ``Idempotent-Replayed`` exposed as ``idempotent_replayed``;
* and, on a network without receipts, the previous answers.
"""
from __future__ import annotations

import re
import threading
import time
from decimal import Decimal

import pytest

import uvd_x402_sdk.client as client_module
from tests.receipt_rail import PAYER, RECIPIENT, Facilitator, x_payment
from uvd_x402_sdk import (
    AUTHORIZATION_ALREADY_SETTLED,
    AUTHORIZATION_IN_FLIGHT,
    RECEIPT_REQUEST_CONFLICT,
    X402Client,
    admitted_authorization_code,
    is_spent_nonce_error,
    is_transient_error,
    new_idempotency_key,
    payment_conflict_response,
    transient_503_response,
)
from uvd_x402_sdk.exceptions import (
    FacilitatorError,
    PaymentSettlementError,
    PaymentVerificationError,
)
from uvd_x402_sdk.exceptions import TimeoutError as X402TimeoutError
from uvd_x402_sdk.models import SettleResponse

PRICE = Decimal("0.01")
ORDER = "order-1"
RANDOM_KEY = re.compile(r"x402-[0-9a-f]{64}")


@pytest.fixture
def rails():
    opened = []

    def open_rail(mode: str = "receipts", **holds) -> Facilitator:
        rail = Facilitator(mode, **holds)
        opened.append(rail)
        return rail

    yield open_rail
    for rail in opened:
        rail.close()


def _seller(rail: Facilitator, *, settle_timeout: float = 0.0, **config) -> X402Client:
    """A fresh client, as a seller process that restarted would build it."""
    client = X402Client(recipient_address=RECIPIENT, facilitator_url=rail.url, **config)
    if settle_timeout:
        client._get_settle_timeout = lambda network: settle_timeout  # type: ignore[method-assign]
    return client


def _payload(nonce: str = "0x01"):
    return X402Client(recipient_address=RECIPIENT).extract_payload(x_payment(nonce))


def _in_background(target) -> threading.Thread:
    worker = threading.Thread(target=target)
    worker.start()
    time.sleep(0.2)  # the worker's settle is admitted and in flight
    return worker


# ---------------------------------------------------------------------------
# One key per handling
# ---------------------------------------------------------------------------


def test_verify_and_settle_of_one_handling_carry_one_random_key(rails):
    rail = rails()
    result = _seller(rail).process_payment(x_payment(), PRICE)

    (verify,), (settle,) = rail.keys("/verify"), rail.keys("/settle")
    assert RANDOM_KEY.fullmatch(settle)
    assert verify == settle == result.idempotency_key


def test_two_handlings_of_the_same_x_payment_send_different_keys(rails):
    """The buyer resends their own X-PAYMENT in a NEW request. A key derived
    from the payment would be the same one, and the rail would hand the new
    request the first one's settle: the replay it exists to refuse."""
    rail = rails()
    _seller(rail).process_payment(x_payment(), PRICE)
    with pytest.raises(PaymentVerificationError):
        _seller(rail).process_payment(x_payment(), PRICE)

    first, second = rail.keys("/verify")
    assert first != second
    assert rail.executed == 1


def test_a_purchase_resumed_with_its_own_key_reads_its_verdict_and_gets_its_settle_back(rails):
    """The seller created the key, stored it with the order, lost the answer
    and restarted. The same key is the same binding. (With a key that differed
    per operation, as 0.83.0 to 0.88.0 sent, the retry's /verify came back
    ``authorization_already_settled`` for a payment that had settled.)"""
    rail = rails()
    key = new_idempotency_key()
    first = _seller(rail).process_payment(x_payment(), PRICE, idempotency_key=key)
    again = _seller(rail).process_payment(x_payment(), PRICE, idempotency_key=key)

    assert rail.executed == 1 and rail.moved == 1
    assert first.idempotent_replayed is False and again.idempotent_replayed is True
    assert again.transaction_hash == first.transaction_hash
    assert again.receipt is not None and again.receipt.status == "confirmed"
    assert set(rail.keys("/verify") + rail.keys("/settle")) == {key}


def test_a_purchase_resumed_under_the_same_secret_scope_gets_its_settle_back(rails):
    rail = rails()
    first = _seller(rail).process_payment(x_payment(), PRICE, idempotency_scope=ORDER)
    again = _seller(rail).process_payment(x_payment(), PRICE, idempotency_scope=ORDER)

    assert again.idempotent_replayed is True
    assert again.transaction_hash == first.transaction_hash
    assert rail.executed == 1


def test_another_purchase_of_the_same_payment_is_refused_as_already_used(rails):
    rail = rails()
    _seller(rail).process_payment(x_payment(), PRICE, idempotency_scope=ORDER)

    with pytest.raises(PaymentVerificationError) as caught:
        _seller(rail).process_payment(x_payment(), PRICE, idempotency_scope="order-2")

    assert admitted_authorization_code(caught.value) == AUTHORIZATION_ALREADY_SETTLED
    assert rail.executed == 1


# ---------------------------------------------------------------------------
# The timeout fallback
# ---------------------------------------------------------------------------


def test_a_lost_settle_answer_is_recovered_by_the_fallback_under_the_handlings_key(rails):
    """The payment moved and its answer did not arrive in time. The fallback
    resends under the same fresh key and gets its own admitted settle back."""
    rail = rails(hold_after_confirm=0.8)
    settled = _seller(rail, settle_timeout=0.3).settle_payment(_payload(), PRICE)

    assert settled.success and settled.idempotent_replayed is True
    assert settled.get_transaction_hash() == "0xf00d1"
    assert rail.executed == 1 and rail.moved == 1
    first, fallback = rail.keys("/settle")
    assert RANDOM_KEY.fullmatch(first) and fallback == first == settled.idempotency_key


def test_with_the_key_off_the_fallback_reads_the_409_as_already_used_not_as_a_timeout(rails):
    """No key and no X-UVD-Purchase: the timed-out settle was admitted, and its
    resend is unbound. The rail answers 409 with the receipt. PROBABLY this
    seller's own payment, but nothing proves it: raised as the facilitator's
    answer (not "not confirmed", not a timeout, never a 402), with the receipt
    to reconcile before delivering anything."""
    rail = rails(hold_after_confirm=0.8)
    client = _seller(rail, settle_timeout=0.3, send_idempotency_key=False)

    with pytest.raises(FacilitatorError) as caught:
        client.settle_payment(_payload(), PRICE, retry=True)

    error = caught.value
    assert error.status_code == 409
    assert admitted_authorization_code(error) == AUTHORIZATION_ALREADY_SETTLED
    assert is_spent_nonce_error(error) and not is_transient_error(error)
    assert error.receipt is not None and error.receipt.status == "confirmed"
    assert error.receipt.settlement["id"] == "0xf00d1"
    # The retry policy does not re-POST a 409: the settle and its fallback only.
    assert rail.keys("/settle") == [None, None]
    assert rail.executed == 1


@pytest.fixture
def quick_polls(monkeypatch):
    """The fallback's pause between asks, shortened for a test's clock."""
    monkeypatch.setattr(client_module, "_IN_FLIGHT_POLL_MAX_INTERVAL_SECONDS", 0.1)


def test_a_settle_that_outlives_its_timeout_is_awaited_in_the_same_handling(rails, quick_polls):
    """The settle timed out while the payment was in flight. The fallback's
    resend under the same key gets ``202 settlement_in_progress``: this
    handling's own payment, moving. It asks again within the budget, and the
    same call ends in the settle, once."""
    rail = rails(hold_before_confirm=1.0)
    settled = _seller(rail, settle_timeout=0.3).settle_payment(_payload(), PRICE)

    assert settled.success and settled.idempotent_replayed is True
    assert rail.executed == 1 and rail.moved == 1
    assert len(set(rail.keys("/settle"))) == 1 and len(rail.keys("/settle")) >= 3


def test_a_settle_still_in_flight_after_the_budget_raises_the_202_not_a_timeout(
    rails, quick_polls, monkeypatch
):
    """Past the budget the 202 itself is raised: transient, with the reason and
    the receipt, so a paywall answers 503 + Retry-After. A TimeoutError here
    was answered 402 by every middleware, over a payment that was moving."""
    monkeypatch.setattr(client_module, "SETTLE_IN_FLIGHT_POLL_SECONDS", 0.3)
    rail = rails(hold_before_confirm=1.5)
    key = new_idempotency_key()

    with pytest.raises(FacilitatorError) as caught:
        _seller(rail, settle_timeout=0.3).settle_payment(_payload(), PRICE, idempotency_key=key)

    error = caught.value
    assert error.status_code == 202 and error.error_code == "settlement_in_progress"
    assert error.retryable and is_transient_error(error)
    assert error.receipt is not None and error.receipt.status == "pending"
    assert payment_conflict_response(error) is None  # not another request's payment
    body, headers = transient_503_response(error)
    assert body["reason"] == "settlement_in_progress" and headers["Retry-After"]

    time.sleep(1.5)  # the first settle confirms
    later = _seller(rail).settle_payment(_payload(), PRICE, idempotency_key=key)
    assert later.success and later.idempotent_replayed is True
    assert rail.executed == 1 and rail.moved == 1


def test_a_bound_202_is_retried_by_the_retry_policy_until_the_settle_comes_back(rails):
    """Two workers of one seller settle the same stored purchase at once. The
    second gets ``202 settlement_in_progress``: "in flight, same binding", so
    the retry policy presents the same request again."""
    rail = rails(hold_before_confirm=0.8)
    key = new_idempotency_key()
    worker = _in_background(
        lambda: _seller(rail).settle_payment(_payload(), PRICE, idempotency_key=key)
    )
    try:
        settled = _seller(rail).settle_payment(
            _payload(), PRICE, retry=True, idempotency_key=key
        )
    finally:
        worker.join()

    assert settled.success and settled.idempotent_replayed is True
    assert rail.executed == 1 and rail.moved == 1


def test_a_202_in_progress_is_transient_and_nothing_else_2xx_is():
    in_flight = FacilitatorError(
        message="in flight",
        status_code=202,
        response_body='{"success":false,"error":"settlement_in_progress","retryable":true}',
    )
    assert in_flight.retryable and is_transient_error(in_flight)
    assert in_flight.to_dict()["details"]["retryable"] is True

    for body in (
        '{"success":false,"error":"accepted"}',
        '{"error":"settlement_in_progress","retryable":false}',
    ):
        other = FacilitatorError(message="other", status_code=202, response_body=body)
        assert not other.retryable and not is_transient_error(other)


def test_in_flight_for_another_request_is_transient_and_not_spent(rails):
    rail = rails(hold_before_confirm=1.0)
    worker = _in_background(lambda: _seller(rail).settle_payment(_payload(), PRICE))
    try:
        with pytest.raises(FacilitatorError) as settled:
            _seller(rail).settle_payment(_payload(), PRICE)
        with pytest.raises(PaymentVerificationError) as verified:
            _seller(rail).verify_payment(_payload(), PRICE)
    finally:
        worker.join()

    for error in (settled.value, verified.value):
        assert admitted_authorization_code(error) == AUTHORIZATION_IN_FLIGHT
        assert is_transient_error(error)
        assert not is_spent_nonce_error(error)
        status, body, headers = payment_conflict_response(error)
        assert status == 503 and headers["Retry-After"]
        assert body["reason"] == AUTHORIZATION_IN_FLIGHT and body["retryable"] is True
    assert settled.value.status_code == 409 and settled.value.retryable
    assert rail.executed == 1


# ---------------------------------------------------------------------------
# A resend of the same X-PAYMENT in a new request
# ---------------------------------------------------------------------------


def test_a_bare_resend_after_settlement_is_refused_by_verify_and_never_settled_again(rails):
    rail = rails()
    _seller(rail).process_payment(x_payment(), PRICE)

    with pytest.raises(PaymentVerificationError) as caught:
        _seller(rail).process_payment(x_payment(), PRICE)

    assert admitted_authorization_code(caught.value) == AUTHORIZATION_ALREADY_SETTLED
    assert is_spent_nonce_error(caught.value) and not is_transient_error(caught.value)
    assert caught.value.receipt is not None and caught.value.receipt.status == "confirmed"
    assert len(rail.keys("/settle")) == 1 and rail.executed == 1


def test_other_terms_for_an_admitted_authorization_are_a_conflict(rails):
    rail = rails()
    key = new_idempotency_key()
    client = _seller(rail)
    client.settle_payment(_payload(), PRICE, idempotency_key=key)

    with pytest.raises(FacilitatorError) as caught:
        client.settle_payment(_payload(), Decimal("0.02"), idempotency_key=key)

    assert caught.value.status_code == 409
    assert admitted_authorization_code(caught.value) == RECEIPT_REQUEST_CONFLICT
    assert is_spent_nonce_error(caught.value) and not is_transient_error(caught.value)
    assert rail.executed == 1


# ---------------------------------------------------------------------------
# A facilitator before 2.39.0: the replay was not tied to the binding
# ---------------------------------------------------------------------------


def test_before_2_39_a_replay_on_the_first_attempt_of_a_new_request_is_refused(rails):
    """2.36.0 to 2.38.0 did not tie the replay to the binding: /verify gives the
    stored verdict and /settle the original 200 with Idempotent-Replayed. This
    handling carried a fresh key and no X-UVD-Purchase, and none of its
    attempts had run: not its payment."""
    rail = rails("receipts-2.38")
    first = _seller(rail).process_payment(x_payment(), PRICE)

    with pytest.raises(PaymentSettlementError) as caught:
        _seller(rail).process_payment(x_payment(), PRICE)

    assert admitted_authorization_code(caught.value) == AUTHORIZATION_ALREADY_SETTLED
    assert caught.value.receipt == first.receipt
    assert is_spent_nonce_error(caught.value)
    status, body, _ = payment_conflict_response(caught.value)
    assert status == 409 and body["reason"] == AUTHORIZATION_ALREADY_SETTLED
    assert rail.executed == 1


def test_before_2_39_a_replay_in_flight_on_a_first_attempt_is_refused_as_in_flight(rails):
    rail = rails("receipts-2.38", hold_before_confirm=1.0)
    worker = _in_background(lambda: _seller(rail).settle_payment(_payload(), PRICE))
    try:
        with pytest.raises(PaymentSettlementError) as caught:
            _seller(rail).settle_payment(_payload(), PRICE, retry=True)
    finally:
        worker.join()

    assert admitted_authorization_code(caught.value) == AUTHORIZATION_IN_FLIGHT
    assert is_transient_error(caught.value)
    # Refused on the first answer: the retry policy never got to turn a later
    # replay of somebody else's settle into "our own retry".
    assert len(rail.keys("/settle")) == 2


def test_before_2_39_the_handlings_own_fallback_still_gets_its_settle(rails):
    rail = rails("receipts-2.38", hold_after_confirm=0.8)
    settled = _seller(rail, settle_timeout=0.3).settle_payment(_payload(), PRICE)

    assert settled.success and settled.idempotent_replayed is True
    assert rail.executed == 1


def test_before_2_39_the_handlings_own_retry_after_a_5xx_still_gets_its_settle(
    rails, monkeypatch
):
    """The first attempt reached the rail, was admitted, and came back as a 5xx
    without a verdict. The retry replays this handling's own admission."""
    monkeypatch.setattr(client_module.time, "sleep", lambda seconds: None)
    rail = rails("receipts-2.38")
    original = rail.handle
    answered = []

    def first_answer_lost(path, key, capability, raw):
        status, body, headers = original(path, key, capability, raw)
        if path == "/settle" and not answered:
            answered.append(status)
            return 502, {"error": "upstream unavailable"}, {}
        return status, body, headers

    rail.handle = first_answer_lost  # type: ignore[method-assign]
    settled = _seller(rail).settle_payment(_payload(), PRICE, retry=True)

    assert settled.success and settled.idempotent_replayed is True
    assert answered == [200] and rail.executed == 1


def test_before_2_39_a_key_the_caller_brought_is_its_own_binding(rails):
    rail = rails("receipts-2.38")
    key = new_idempotency_key()
    _seller(rail).process_payment(x_payment(), PRICE, idempotency_key=key)
    again = _seller(rail).process_payment(x_payment(), PRICE, idempotency_key=key)

    assert again.idempotent_replayed is True
    assert rail.executed == 1


def test_the_refusal_is_the_guard_not_the_double(rails, monkeypatch):
    """Mutation check: without the guard the same replay comes back as a
    success, which is what a middleware would deliver on."""
    rail = rails("receipts-2.38")
    _seller(rail).process_payment(x_payment(), PRICE)
    monkeypatch.setattr(client_module._Binding, "refuse_foreign_replay", lambda *args: None)

    again = _seller(rail).process_payment(x_payment(), PRICE)
    assert again.success and again.idempotent_replayed is True


# ---------------------------------------------------------------------------
# A network without receipts
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("keyed", [True, False], ids=["key-on", "key-off"])
def test_legacy_a_bare_resend_still_reaches_the_chain_and_reads_as_before(rails, keyed):
    rail = rails("legacy")
    _seller(rail, send_idempotency_key=keyed).process_payment(x_payment(), PRICE)

    with pytest.raises(FacilitatorError) as caught:
        _seller(rail, send_idempotency_key=keyed).process_payment(x_payment(), PRICE)

    assert caught.value.status_code == 400
    assert admitted_authorization_code(caught.value) is None
    assert payment_conflict_response(caught.value) is None
    assert rail.executed == 2 and rail.moved == 1


@pytest.mark.parametrize("keyed", [True, False], ids=["key-on", "key-off"])
def test_legacy_a_fallback_that_arrives_before_the_record_still_ends_in_a_timeout(rails, keyed):
    rail = rails("legacy", hold_after_confirm=0.8)
    client = _seller(rail, settle_timeout=0.3, send_idempotency_key=keyed)

    with pytest.raises(X402TimeoutError):
        client.settle_payment(_payload(), PRICE)
    assert rail.moved == 1


def test_legacy_a_resumed_purchase_comes_from_the_cache_and_says_so(rails):
    rail = rails("legacy")
    key = new_idempotency_key()
    first = _seller(rail).process_payment(x_payment(), PRICE, idempotency_key=key)
    again = _seller(rail).process_payment(x_payment(), PRICE, idempotency_key=key)

    assert rail.executed == 1
    assert first.idempotent_replayed is False and again.idempotent_replayed is True
    assert again.transaction_hash == first.transaction_hash


@pytest.mark.parametrize("mode", ["receipts", "receipts-2.38", "legacy"])
def test_a_first_payment_is_the_same_on_every_facilitator(rails, mode):
    rail = rails(mode)
    result = _seller(rail).process_payment(x_payment(), PRICE)

    assert result.success and result.transaction_hash == "0xf00d1"
    assert result.payer_address == PAYER
    assert result.idempotent_replayed is False
    assert rail.keys("/verify") == rail.keys("/settle") == [result.idempotency_key]


# ---------------------------------------------------------------------------
# The pieces
# ---------------------------------------------------------------------------


def _facilitator_error(status: int, error: str) -> FacilitatorError:
    return FacilitatorError("x", status_code=status, response_body=f'{{"error":"{error}"}}')


@pytest.mark.parametrize(
    "error, code, spent, transient",
    [
        (
            _facilitator_error(409, "authorization_already_settled"),
            AUTHORIZATION_ALREADY_SETTLED, True, False,
        ),
        (
            _facilitator_error(409, "authorization_in_flight"),
            AUTHORIZATION_IN_FLIGHT, False, True,
        ),
        (
            _facilitator_error(409, "receipt_request_conflict"),
            RECEIPT_REQUEST_CONFLICT, True, False,
        ),
        (
            PaymentVerificationError("x", reason=" Authorization_Already_Settled "),
            AUTHORIZATION_ALREADY_SETTLED, True, False,
        ),
        (
            PaymentVerificationError("x", reason="authorization_in_flight"),
            AUTHORIZATION_IN_FLIGHT, False, True,
        ),
        (
            PaymentSettlementError("x", reason="authorization_in_flight"),
            AUTHORIZATION_IN_FLIGHT, False, True,
        ),
        (_facilitator_error(409, "idempotency_key_conflict"), None, True, False),
        (_facilitator_error(400, "contract_call_failed (ref: a)"), None, False, False),
        (_facilitator_error(503, "receipt_store_unavailable"), None, False, True),
        (PaymentVerificationError("x", reason="invalid_signature"), None, False, False),
        (PaymentVerificationError("x"), None, False, False),
        (ValueError("authorization_already_settled"), None, False, False),
    ],
    ids=lambda value: type(value).__name__ if isinstance(value, Exception) else str(value),
)
def test_the_classification(error, code, spent, transient):
    assert admitted_authorization_code(error) == code
    assert is_spent_nonce_error(error) is spent
    assert is_transient_error(error) is transient
    conflict = payment_conflict_response(error) if isinstance(error, Exception) and code else None
    if code is None:
        if not isinstance(error, ValueError):
            assert payment_conflict_response(error) is None
    else:
        assert conflict[0] == (503 if code == AUTHORIZATION_IN_FLIGHT else 409)
        assert conflict[1]["reason"] == code and conflict[1]["safeToReplay"] is False


def test_the_body_cannot_claim_a_replay_only_the_header_can(rails):
    """``idempotent_replayed`` is the facilitator's HEADER, never its body."""
    rail = rails("legacy")
    original = rail.handle

    def body_says_replayed(*args):
        status, body, headers = original(*args)
        return status, {**body, "idempotent_replayed": True}, headers

    rail.handle = body_says_replayed  # type: ignore[method-assign]
    settled = _seller(rail).settle_payment(_payload(), PRICE)
    assert settled.idempotent_replayed is False
    assert SettleResponse(success=True).idempotent_replayed is False


def test_the_409_a_paywall_answers_carries_the_code_and_the_receipt(rails):
    rail = rails()
    _seller(rail).process_payment(x_payment(), PRICE)
    with pytest.raises(PaymentVerificationError) as caught:
        _seller(rail).process_payment(x_payment(), PRICE)

    status, body, headers = payment_conflict_response(caught.value)
    assert status == 409
    assert body["reason"] == AUTHORIZATION_ALREADY_SETTLED and body["retryable"] is False
    assert body["error"] == "PAYMENT_VERIFICATION_FAILED"
    assert headers["PAYMENT-RESPONSE"] == headers["X-PAYMENT-RESPONSE"]
    assert headers["Cache-Control"] == "no-store"
