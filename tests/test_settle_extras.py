"""The settle asks for the proof, returns it, and explains its failures.

Every answer the facilitator gives here is one x402-rs 2.40.0 (commit 8ee44114)
produced with its own response code: ``tests/fixtures/facilitator-settle-2.40.0``,
recorded as status, ``Retry-After`` and exact body bytes (see the README there).
A few cases derive a variant from a recorded body (a field removed, a reason
swapped for another the same function emits); each says which and why.

Everything enters through the public API: ``X402Client(http_client=...)`` with an
``httpx.MockTransport`` that serves the recorded bytes. The facilitator URL is
``http://facilitator.invalid``, a name that never resolves, so a client that
ignored the injected transport fails here instead of reaching a real facilitator.

1. ``settle_payment(extra=...)`` merges into ``paymentRequirements.extra``, and
   ``PaymentRequirements.extra`` takes any JSON value (``Dict[str, Any]``).
2. ``SettleResponse.proof_of_payment`` is the facilitator's ``proofOfPayment``.
3. ``PaymentSettlementError`` / ``PaymentVerificationError`` carry
   ``status_code``, ``error_reason`` and the body (at most 4096 bytes).
4. ``FacilitatorError.safe_to_retry``: ``True`` only when the facilitator said
   nothing was sent, ``False`` when the payment may be on chain, else ``None``.
5. ``forward_unconfirmed`` is ambiguous; ``forward_failed`` stays ambiguous.
6. ``http_client=``: the caller's client, whose response hooks see raw answers.
7. ``try_settle_payment()`` returns ``proof_of_payment`` and ``safe_to_retry``.
8. The default USD amount is converted in ``Decimal``, in the settle and in the
   402 that advertises it.
9. ``X402Config.max_timeout_seconds`` is the requirements' ``maxTimeoutSeconds``.
"""
from __future__ import annotations

import json
import warnings
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest

from tests.receipt_rail import _receipt
from uvd_x402_sdk import X402Client
from uvd_x402_sdk.client import _undelivered_response, is_spent_nonce_error
from uvd_x402_sdk.config import X402Config
from uvd_x402_sdk.erc8004 import ERC8004_EXTENSION_ID, build_erc8004_payment_requirements
from uvd_x402_sdk.exceptions import (
    MAX_ERROR_BODY_BYTES,
    WRITE_AMBIGUOUS_REASONS,
    FacilitatorError,
    PaymentSettlementError,
    PaymentVerificationError,
    WriterUnavailableError,
    write_retry_is_safe,
)
from uvd_x402_sdk.models import PaymentPayload, PaymentRequirements, SettleResponse
from uvd_x402_sdk.networks import get_network
from uvd_x402_sdk.response import create_402_response_v2

FIXTURES = Path(__file__).parent / "fixtures" / "facilitator-settle-2.40.0"
FACILITATOR = "http://facilitator.invalid"
RECIPIENT = "0x2222222222222222222222222222222222222222"
PROOF_EXTRA = {ERC8004_EXTENSION_ID: {"includeProof": True}}


def recorded(name: str) -> dict[str, Any]:
    """One recorded answer: ``status``, ``retryAfter`` and the raw ``body``."""
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def with_body(name: str, **changes: Any) -> dict[str, Any]:
    """A recorded answer whose JSON body gets ``changes`` (``None`` removes a key)."""
    answer = recorded(name)
    body = json.loads(answer["body"])
    for key, value in changes.items():
        if value is None:
            body.pop(key, None)
        else:
            body[key] = value
    answer["body"] = json.dumps(body, separators=(",", ":"))
    return answer


class Facilitator:
    """Serves recorded answers in order and keeps every request it got."""

    def __init__(self, *answers: dict[str, Any]) -> None:
        self.answers = list(answers)
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        answer = self.answers.pop(0)
        headers = {"content-type": "application/json"}
        if answer.get("retryAfter"):
            headers["retry-after"] = answer["retryAfter"]
        return httpx.Response(
            answer["status"], headers=headers, content=answer["body"].encode("utf-8")
        )

    def http_client(self, **kwargs: Any) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handler), **kwargs)

    def sent(self, index: int = -1) -> dict[str, Any]:
        return json.loads(self.requests[index].content)


def client_for(
    facilitator: Facilitator, *, http: dict[str, Any] | None = None, **config: Any
) -> X402Client:
    """A client on ``facilitator``; ``http`` goes to httpx, ``config`` to X402Config."""
    return X402Client(
        recipient_address=RECIPIENT,
        facilitator_url=FACILITATOR,
        http_client=facilitator.http_client(**(http or {})),
        **config,
    )


def evm_payload(network: str = "base") -> PaymentPayload:
    return PaymentPayload(
        x402Version=2 if ":" in network else 1,
        scheme="exact",
        network=network,
        payload={
            "signature": "0x" + "11" * 65,
            "authorization": {
                "from": "0x774A351d1AA8cd221a1B87da639EFcc5A56cd9ce",
                "to": RECIPIENT,
                "value": "10000",
                "validAfter": "0",
                "validBefore": "9999999999",
                "nonce": "0x" + "42" * 32,
            },
        },
    )


def settle(facilitator: Facilitator, **kwargs: Any) -> SettleResponse:
    network = kwargs.pop("network", "base")
    return client_for(facilitator).settle_payment(
        evm_payload(network), Decimal("0.01"), **kwargs
    )


def settle_failure(answer: dict[str, Any]) -> FacilitatorError:
    with pytest.raises(FacilitatorError) as caught:
        settle(Facilitator(answer))
    return caught.value


# ── 1. extra ────────────────────────────────────────────────────────────────


class TestExtraReachesTheRequirements:
    def test_v1_envelope_carries_the_extension_next_to_the_domain(self) -> None:
        facilitator = Facilitator(recorded("settle_success_with_proof"))
        settle(facilitator, extra=PROOF_EXTRA)
        assert facilitator.sent()["paymentRequirements"]["extra"] == {
            "name": "USD Coin",
            "version": "2",
            ERC8004_EXTENSION_ID: {"includeProof": True},
        }

    def test_v2_envelope_carries_it_in_accepted(self) -> None:
        facilitator = Facilitator(recorded("settle_success_with_proof"))
        settle(facilitator, network="eip155:8453", extra=PROOF_EXTRA)
        body = facilitator.sent()
        assert body["x402Version"] == 2
        for accepted in (body["accepted"], body["paymentPayload"]["accepted"]):
            assert accepted["extra"][ERC8004_EXTENSION_ID] == {"includeProof": True}
            assert accepted["extra"]["name"] == "USD Coin"

    def test_without_extra_the_request_is_what_it_was(self) -> None:
        facilitator = Facilitator(recorded("settle_success_without_proof"))
        settle(facilitator)
        assert facilitator.sent()["paymentRequirements"]["extra"] == {
            "name": "USD Coin",
            "version": "2",
        }

    def test_a_nested_extra_raises_no_pydantic_warning(self) -> None:
        """``Dict[str, str]`` warned on every settle with a nested value."""
        facilitator = Facilitator(recorded("settle_success_with_proof"))
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            settle(facilitator, extra=PROOF_EXTRA)

    def test_requirements_take_any_json_value_and_still_take_strings(self) -> None:
        base = dict(
            network="base", maxAmountRequired="1", resource="r", description="d",
            payTo=RECIPIENT, asset="0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
        )
        nested = PaymentRequirements(extra=PROOF_EXTRA, **base)
        assert nested.extra == PROOF_EXTRA
        flat = PaymentRequirements(extra={"name": "USD Coin", "version": "2"}, **base)
        assert flat.extra == {"name": "USD Coin", "version": "2"}

    def test_the_same_domain_value_is_accepted(self) -> None:
        facilitator = Facilitator(recorded("settle_success_without_proof"))
        settle(facilitator, extra={"name": "USD Coin", "version": "2"})
        assert facilitator.sent()["paymentRequirements"]["extra"]["name"] == "USD Coin"

    def test_another_domain_value_raises_before_sending(self) -> None:
        facilitator = Facilitator()
        with pytest.raises(ValueError, match="eip712_domain"):
            settle(facilitator, extra={"name": "Other Coin"})
        assert facilitator.requests == []

    @pytest.mark.parametrize(
        "extra, error",
        [({1: "x"}, TypeError), (["x"], TypeError), ({"x": object()}, ValueError)],
    )
    def test_an_unusable_extra_raises_before_sending(self, extra: Any, error: type) -> None:
        facilitator = Facilitator()
        with pytest.raises(error):
            settle(facilitator, extra=extra)
        assert facilitator.requests == []

    @pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
    def test_nan_and_infinity_are_refused_by_the_sdk(self, value: float) -> None:
        """Not JSON. The SDK refuses them itself: the message is its own, not the
        one some httpx versions raise, and others send them as bare tokens."""
        facilitator = Facilitator()
        with pytest.raises(ValueError, match="extra must be JSON-serialisable"):
            settle(facilitator, extra={"x": value})
        assert facilitator.requests == []

    def test_the_callers_dict_is_not_changed(self) -> None:
        extra = {ERC8004_EXTENSION_ID: {"includeProof": True}}
        settle(Facilitator(recorded("settle_success_with_proof")), extra=extra)
        assert extra == {ERC8004_EXTENSION_ID: {"includeProof": True}}

    def test_try_settle_payment_forwards_it(self) -> None:
        facilitator = Facilitator(recorded("settle_success_with_proof"))
        result = client_for(facilitator).try_settle_payment(
            evm_payload(), Decimal("0.01"), extra=PROOF_EXTRA
        )
        assert result["success"] is True
        assert ERC8004_EXTENSION_ID in facilitator.sent()["paymentRequirements"]["extra"]

    def test_every_retry_attempt_carries_it(self) -> None:
        facilitator = Facilitator(
            recorded("upstream_rpc_unavailable"), recorded("settle_success_with_proof")
        )
        client = client_for(facilitator)
        import uvd_x402_sdk.client as client_mod

        original_sleep = client_mod.time.sleep
        client_mod.time.sleep = lambda _seconds: None
        try:
            client.settle_payment(evm_payload(), Decimal("0.01"), retry=True, extra=PROOF_EXTRA)
        finally:
            client_mod.time.sleep = original_sleep
        assert len(facilitator.requests) == 2
        for index in (0, 1):
            extra = facilitator.sent(index)["paymentRequirements"]["extra"]
            assert extra[ERC8004_EXTENSION_ID] == {"includeProof": True}


# ── 2. proof_of_payment ─────────────────────────────────────────────────────


class TestProofOfPayment:
    def test_the_recorded_proof_is_returned(self) -> None:
        answer = recorded("settle_success_with_proof")
        wire = json.loads(answer["body"])["proofOfPayment"]
        response = settle(Facilitator(answer), extra=PROOF_EXTRA)
        proof = response.proof_of_payment
        assert proof is not None
        assert proof.transaction_hash == wire["transactionHash"]
        assert proof.block_number == wire["blockNumber"]
        assert proof.payment_hash == wire["paymentHash"]
        assert proof.amount == wire["amount"]

    def test_it_dumps_back_to_the_facilitators_shape(self) -> None:
        """What /feedback and DX402 take is the facilitator's camelCase object."""
        answer = recorded("settle_success_with_proof")
        wire = json.loads(answer["body"])["proofOfPayment"]
        proof = settle(Facilitator(answer), extra=PROOF_EXTRA).proof_of_payment
        assert proof is not None
        assert proof.model_dump(by_alias=True) == wire

    def test_a_settle_without_proof_has_none(self) -> None:
        response = settle(Facilitator(recorded("settle_success_without_proof")))
        assert response.success is True
        assert response.proof_of_payment is None

    def test_a_proof_that_does_not_parse_is_none_and_the_settle_stands(self) -> None:
        """Derived: the recorded proof without ``paymentHash``."""
        answer = recorded("settle_success_with_proof")
        body = json.loads(answer["body"])
        del body["proofOfPayment"]["paymentHash"]
        answer["body"] = json.dumps(body)
        response = settle(Facilitator(answer), extra=PROOF_EXTRA)
        assert response.success is True
        assert response.get_transaction_hash() == body["transaction"]
        assert response.proof_of_payment is None

    def test_the_snake_case_spelling_is_read_too(self) -> None:
        """Derived: older facilitator docs spell it ``proof_of_payment``; TS reads both."""
        answer = recorded("settle_success_with_proof")
        body = json.loads(answer["body"])
        body["proof_of_payment"] = body.pop("proofOfPayment")
        answer["body"] = json.dumps(body)
        proof = settle(Facilitator(answer), extra=PROOF_EXTRA).proof_of_payment
        assert proof is not None
        assert proof.payment_hash == body["proof_of_payment"]["paymentHash"]


# ── 3. What a failed settle or verify carries ───────────────────────────────


class TestSettlementErrorCarriesTheAnswer:
    def test_a_mined_and_reverted_settle(self) -> None:
        answer = recorded("settle_mined_reverted")
        with pytest.raises(PaymentSettlementError) as caught:
            settle(Facilitator(answer))
        exc = caught.value
        assert exc.status_code == 200
        assert exc.error_reason == "invalid_scheme"
        assert exc.response_body == answer["body"]
        # Unchanged from 0.90.1: message, reason, hash and to_dict().
        assert exc.message == "Payment settlement failed: None"
        assert exc.reason == "invalid_scheme"
        assert exc.tx_hash == json.loads(answer["body"])["transaction"]
        assert exc.to_dict() == {
            "error": "PAYMENT_SETTLEMENT_FAILED",
            "message": "Payment settlement failed: None",
            "details": {
                "network": "base",
                "transactionHash": exc.tx_hash,
                "reason": "invalid_scheme",
            },
        }

    def test_a_settle_rejected_by_re_validation(self) -> None:
        """x402-rs answers it in the verify's shape: the recorded /verify rejection."""
        answer = recorded("verify_invalid_signature")
        with pytest.raises(PaymentSettlementError) as caught:
            settle(Facilitator(answer))
        exc = caught.value
        assert exc.status_code == 200
        assert exc.error_reason == "invalid_signature"
        assert exc.response_body == answer["body"]
        assert exc.message == "Payment settlement failed: invalid_signature"

    def test_a_verify_rejection(self) -> None:
        answer = recorded("verify_invalid_signature")
        facilitator = Facilitator(answer)
        with pytest.raises(PaymentVerificationError) as caught:
            client_for(facilitator).verify_payment(evm_payload(), Decimal("0.01"))
        exc = caught.value
        assert exc.status_code == 200
        assert exc.error_reason == "invalid_signature"
        assert exc.response_body == answer["body"]
        assert exc.reason == "invalid_signature"
        assert exc.to_dict() == {
            "error": "PAYMENT_VERIFICATION_FAILED",
            "message": "Payment verification failed: None",
            "details": {"reason": "invalid_signature"},
        }

    def test_the_body_is_cut_at_4096_bytes_never_mid_character(self) -> None:
        """Derived: the recorded reverted settle with a long ``message`` of two-byte
        characters, written as UTF-8 (not ``\\u`` escapes), padded so that byte
        4096 falls inside one of them."""
        answer = recorded("settle_mined_reverted")
        body = json.loads(answer["body"])
        body["message"] = ""
        start = len(json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode("utf-8")) - 2
        pad = "a" * ((MAX_ERROR_BODY_BYTES - 1 - start) % 2)
        body["message"] = pad + "ñ" * 3000
        answer["body"] = json.dumps(body, separators=(",", ":"), ensure_ascii=False)
        raw = answer["body"].encode("utf-8")
        assert 0xC0 <= raw[MAX_ERROR_BODY_BYTES - 1] and 0x80 <= raw[MAX_ERROR_BODY_BYTES] < 0xC0
        with pytest.raises(PaymentSettlementError) as caught:
            settle(Facilitator(answer))
        assert caught.value.response_body == raw[: MAX_ERROR_BODY_BYTES - 1].decode("utf-8")

    def test_constructed_by_hand_they_carry_none(self) -> None:
        for exc in (PaymentSettlementError("x"), PaymentVerificationError("x")):
            assert (exc.status_code, exc.error_reason, exc.response_body) == (None, None, None)

    def test_the_body_does_not_move_the_spent_nonce_verdict(self) -> None:
        """The settle body is not re-read as free text: a rejection stays one."""
        body = json.dumps({"success": False, "errorReason": "invalid_scheme",
                           "receipt": {"nonce": "0x01", "safeToReplay": False}})
        exc = PaymentSettlementError(
            "Payment settlement failed: None", reason="invalid_scheme",
            status_code=200, error_reason="invalid_scheme", response_body=body,
        )
        assert is_spent_nonce_error(exc) is False


# ── 4. safe_to_retry ────────────────────────────────────────────────────────


class TestSafeToRetry:
    @pytest.mark.parametrize(
        "name, expected",
        [
            ("receipt_store_unavailable", True),     # safeToRetry: true
            ("forward_unconfirmed", False),          # the holder got it
            ("settlement_unconfirmed", False),       # a hash
            ("broadcast_uncertain", False),          # retryable: false
            ("forward_failed", False),               # ambiguous up to 2.39.5
            ("upstream_rpc_unavailable", None),      # same answer up to 2.39.5 for a lost send
            ("facilitator_signer_unfunded", None),
            ("contract_call_failed", None),          # a refusal
        ],
    )
    def test_each_recorded_answer(self, name: str, expected: bool | None) -> None:
        assert settle_failure(recorded(name)).safe_to_retry is expected

    def test_a_named_pre_hop_reason_is_true(self) -> None:
        """Derived: ``writer_lease_unavailable`` sends the same body with its reason."""
        answer = with_body("forward_failed", reason="holder_unknown")
        assert settle_failure(answer).safe_to_retry is True

    def test_forward_unconfirmed_is_false_by_its_name_alone(self) -> None:
        """Derived: the recorded body without ``retryable: false``."""
        answer = with_body("forward_unconfirmed", retryable=None)
        assert settle_failure(answer).safe_to_retry is False

    @pytest.mark.parametrize(
        "name, error",
        [
            ("broadcast_uncertain", None),
            ("settlement_unconfirmed", None),
            ("broadcast_uncertain", "receipt_pending (ref: x)"),
            ("broadcast_uncertain", "receipt_response_unreadable"),
        ],
    )
    def test_the_2_39_5_shape_without_retryable_false_is_still_false(
        self, name: str, error: str | None
    ) -> None:
        """Derived: up to 2.39.5 ``broadcast_uncertain`` and ``receipt_pending`` had
        no ``retryable: false``. Here nothing but the ``error`` token is left: no
        ``retryable``, no ``transaction``, no ``paymentId``."""
        token = {"error": error} if error is not None else {}
        answer = with_body(name, retryable=None, transaction=None, paymentId=None, **token)
        assert set(json.loads(answer["body"])) == {"error"}
        assert settle_failure(answer).safe_to_retry is False

    def test_a_hash_alone_is_false(self) -> None:
        """Derived: a failure naming a transaction under a token this SDK does not
        know, without ``retryable: false`` ("a ``transaction`` in a failure")."""
        answer = with_body("settlement_unconfirmed", error="a_future_token", retryable=None)
        assert settle_failure(answer).safe_to_retry is False

    @pytest.mark.parametrize("status", ["unknown", "pending"])
    def test_a_receipt_not_final_is_false(self, status: str) -> None:
        """Derived: the recorded ``safeToRetry`` answer carrying a receipt that is
        not final ("any failure carrying a ``receipt`` with ``status: unknown``")."""
        answer = with_body("receipt_store_unavailable", receipt=_receipt(status, None, None))
        assert settle_failure(answer).safe_to_retry is False

    def test_may_be_on_chain_wins_over_safe_to_retry(self) -> None:
        """Derived: a body saying both is read as the one that can cost a payment."""
        answer = with_body("receipt_store_unavailable", retryable=False)
        assert settle_failure(answer).safe_to_retry is False

    def test_a_4xx_is_never_true(self) -> None:
        """Derived: the status is the ceiling, as for ``retryable``."""
        answer = recorded("receipt_store_unavailable")
        answer["status"] = 400
        assert settle_failure(answer).safe_to_retry is None

    @pytest.mark.parametrize(
        "status, code",
        [(409, "authorization_already_settled"), (409, "authorization_in_flight"),
         (409, "receipt_request_conflict"), (503, "idempotency_cache_corrupt")],
    )
    def test_an_admitted_or_settled_authorization_is_false(self, status: int, code: str) -> None:
        body = json.dumps({"success": False, "error": code, "safeToReplay": False})
        assert FacilitatorError("x", status_code=status, response_body=body).safe_to_retry is False

    def test_no_answer_is_none(self) -> None:
        assert FacilitatorError("transport").safe_to_retry is None

    def test_to_dict_is_unchanged(self) -> None:
        exc = settle_failure(recorded("receipt_store_unavailable"))
        assert "safeToRetry" not in exc.to_dict()["details"]

    def test_the_docstring_points_at_the_facilitator_table(self) -> None:
        assert "docs/settle-errors.md" in (FacilitatorError.__doc__ or "")


# ── 5. forward_unconfirmed / forward_failed ─────────────────────────────────


class TestWriteAmbiguousReasons:
    def test_both_forward_reasons_are_ambiguous(self) -> None:
        assert WRITE_AMBIGUOUS_REASONS == {"forward_failed", "forward_unconfirmed"}
        for reason in WRITE_AMBIGUOUS_REASONS:
            assert write_retry_is_safe(reason) is False
            assert WriterUnavailableError("x", 503, reason=reason).safe_to_retry is False

    def test_the_integrations_answer_them_as_before(self) -> None:
        """forward_unconfirmed may have moved (500); forward_failed is transient (503)."""
        unconfirmed = _undelivered_response(settle_failure(recorded("forward_unconfirmed")))
        failed = _undelivered_response(settle_failure(recorded("forward_failed")))
        assert unconfirmed is not None and unconfirmed[0] == 500
        assert failed is not None and failed[0] == 503
        assert failed[1]["safeToRetry"] is False


# ── 6. http_client ──────────────────────────────────────────────────────────


class TestInjectedHttpClient:
    def test_a_response_hook_sees_the_raw_settle_answer(self) -> None:
        answer = recorded("settle_success_with_proof")
        seen: list[bytes] = []

        def hook(response: httpx.Response) -> None:
            response.read()
            seen.append(response.content)

        facilitator = Facilitator(answer)
        client = client_for(facilitator, http={"event_hooks": {"response": [hook]}})
        client.settle_payment(evm_payload(), Decimal("0.01"), extra=PROOF_EXTRA)
        assert seen == [answer["body"].encode("utf-8")]

    def test_verify_goes_through_it_too(self) -> None:
        facilitator = Facilitator(recorded("verify_invalid_signature"))
        with pytest.raises(PaymentVerificationError):
            client_for(facilitator).verify_payment(evm_payload(), Decimal("0.01"))
        assert [r.url.path for r in facilitator.requests] == ["/verify"]

    def test_close_leaves_the_callers_client_open(self) -> None:
        """A settle goes through it first, so the SDK has used it before closing."""
        facilitator = Facilitator(recorded("settle_success_without_proof"))
        http = facilitator.http_client()
        client = X402Client(recipient_address=RECIPIENT, facilitator_url=FACILITATOR,
                            http_client=http)
        with client:
            client.settle_payment(evm_payload(), Decimal("0.01"))
        assert len(facilitator.requests) == 1
        assert http.is_closed is False
        http.close()

    def test_without_one_the_sdk_owns_its_client(self) -> None:
        client = X402Client(recipient_address=RECIPIENT, facilitator_url=FACILITATOR)
        own = client._get_http_client()
        client.close()
        assert own.is_closed is True


# ── try_settle_payment() carries the proof and safe_to_retry ────────────────

FIVE_KEYS = ("success", "tx_hash", "payment_id", "error_code", "error")


def try_settle(answer: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
    return client_for(Facilitator(answer)).try_settle_payment(
        evm_payload(), Decimal("0.01"), **kwargs
    )


class TestTrySettlePaymentResult:
    def test_success_returns_the_proof_as_the_facilitators_object(self) -> None:
        answer = recorded("settle_success_with_proof")
        body = json.loads(answer["body"])
        result = try_settle(answer, extra=PROOF_EXTRA)
        assert result["proof_of_payment"] == body["proofOfPayment"]
        assert result["safe_to_retry"] is None
        assert {key: result[key] for key in FIVE_KEYS} == {
            "success": True,
            "tx_hash": body["transaction"],
            "payment_id": None,
            "error_code": None,
            "error": None,
        }

    def test_success_without_a_proof_has_none(self) -> None:
        result = try_settle(recorded("settle_success_without_proof"))
        assert result["success"] is True
        assert result["proof_of_payment"] is None

    @pytest.mark.parametrize(
        "name, expected",
        [("forward_unconfirmed", False), ("receipt_store_unavailable", True),
         ("upstream_rpc_unavailable", None)],
    )
    def test_a_facilitator_error_carries_safe_to_retry(
        self, name: str, expected: bool | None
    ) -> None:
        result = try_settle(recorded(name))
        assert result["success"] is False
        assert result["safe_to_retry"] is expected
        assert result["proof_of_payment"] is None

    def test_a_settlement_error_has_none(self) -> None:
        answer = recorded("settle_mined_reverted")
        result = try_settle(answer)
        assert result["success"] is False
        assert result["tx_hash"] == json.loads(answer["body"])["transaction"]
        assert result["safe_to_retry"] is None


# ── The default USD amount is converted in Decimal ──────────────────────────

CENT_PRICES = range(1, 10_000)  # $0.01 .. $99.99
LONG_PRICE = Decimal("12345678901.234567")  # 17 significant digits: no float holds it


class TestDefaultAmountIsExact:
    def test_every_cent_price_converts_exactly(self) -> None:
        """Scaled as a binary float, 151 of these came out one base unit short
        (``int(2.01 * 10**6)`` is ``2009999``). Decimal, float and str inputs."""
        base = get_network("base")
        assert base is not None
        inputs = (
            lambda c: Decimal(c) / 100,
            lambda c: c / 100,
            lambda c: str(Decimal(c) / 100),
        )
        for to_input in inputs:
            wrong = [c for c in CENT_PRICES if base.get_token_amount(to_input(c)) != c * 10**4]
            assert wrong == []

    def test_the_erc8004_requirements_helper_converts_exactly(self) -> None:
        """``build_erc8004_payment_requirements`` had its own float conversion,
        with the same 151 prices one base unit short."""
        wrong = [
            c for c in CENT_PRICES
            if build_erc8004_payment_requirements(str(Decimal(c) / 100), RECIPIENT, FACILITATOR)[
                "maxAmountRequired"
            ] != str(c * 10**4)
        ]
        assert wrong == []

    @pytest.mark.parametrize(
        "price, atomic",
        [(Decimal("2.01"), "2010000"), (2.01, "2010000"), (Decimal("4.1"), "4100000"),
         (LONG_PRICE, "12345678901234567")],
    )
    def test_the_settle_sends_the_exact_amount(self, price: Any, atomic: str) -> None:
        facilitator = Facilitator(recorded("settle_success_without_proof"))
        client_for(facilitator).settle_payment(evm_payload(), price)
        assert facilitator.sent()["paymentRequirements"]["maxAmountRequired"] == atomic

    @pytest.mark.parametrize("price", [Decimal("2.01"), LONG_PRICE])
    def test_the_402_and_the_settle_say_the_same_number(self, price: Decimal) -> None:
        """What a seller advertises is what its settle then requires."""
        config = X402Config(recipient_evm=RECIPIENT, supported_networks=["base"])
        offer = create_402_response_v2(price, config)
        advertised = next(o["amount"] for o in offer["accepts"] if o["network"] == "eip155:8453")
        facilitator = Facilitator(recorded("settle_success_without_proof"))
        client_for(facilitator).settle_payment(evm_payload(), price)
        required = facilitator.sent()["paymentRequirements"]["maxAmountRequired"]
        assert advertised == required == str(int(price * 10**6))


# ── X402Config.max_timeout_seconds ──────────────────────────────────────────


class TestMaxTimeoutSeconds:
    def test_the_default_is_60(self) -> None:
        facilitator = Facilitator(recorded("settle_success_without_proof"))
        settle(facilitator)
        assert facilitator.sent()["paymentRequirements"]["maxTimeoutSeconds"] == 60

    def test_the_configured_value_reaches_verify_and_settle(self) -> None:
        facilitator = Facilitator(
            recorded("verify_invalid_signature"), recorded("settle_success_without_proof")
        )
        client = client_for(facilitator, max_timeout_seconds=300)
        with pytest.raises(PaymentVerificationError):
            client.verify_payment(evm_payload(), Decimal("0.01"))
        client.settle_payment(evm_payload(), Decimal("0.01"))
        windows = [facilitator.sent(i)["paymentRequirements"]["maxTimeoutSeconds"] for i in (0, 1)]
        assert windows == [300, 300]

    def test_it_reaches_the_v2_envelope(self) -> None:
        facilitator = Facilitator(recorded("settle_success_without_proof"))
        client_for(facilitator, max_timeout_seconds=300).settle_payment(
            evm_payload("eip155:8453"), Decimal("0.01")
        )
        assert facilitator.sent()["accepted"]["maxTimeoutSeconds"] == 300

    @pytest.mark.parametrize("bad", [0, -1, "300", 1.5, True, None])
    def test_a_value_that_is_not_a_positive_integer_is_refused(self, bad: Any) -> None:
        with pytest.raises(ValueError, match="max_timeout_seconds"):
            X402Config(recipient_evm=RECIPIENT, max_timeout_seconds=bad)

    def test_a_caller_pinning_its_decimals_and_window_needs_no_override(self) -> None:
        """Its own token decimals and settlement window, both on the wire, through
        the public API only: no private method replaced."""
        price = Decimal("0.29")
        facilitator = Facilitator(recorded("settle_success_without_proof"))
        client = client_for(facilitator, max_timeout_seconds=300)
        result = client.try_settle_payment(
            evm_payload(), price, asset="0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
            eip712_domain={"name": "USD Coin", "version": "2"}, token_decimals=6, retry=True,
        )
        assert result["success"] is True
        requirements = facilitator.sent()["paymentRequirements"]
        assert requirements["maxAmountRequired"] == str(int(price * Decimal(10**6)))
        assert requirements["maxTimeoutSeconds"] == 300
