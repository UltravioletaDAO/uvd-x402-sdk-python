"""Every middleware and decorator, and a payment that may have moved: never a 402.

A 402 tells the buyer "your payment was rejected, sign a new one". Said over a
payment that may have settled, a buyer that obeys pays twice. Each entry point
runs in its own framework against a facilitator over a real socket that answers
``/verify`` and ``/settle`` as each case says. What is pinned, per entry point:

* ``502 settlement_unconfirmed`` (x402-rs ``SettlementUnconfirmedResponse``:
  broadcast, no receipt, may be mined) and any other ``5xx`` whose body names a
  transaction: 500 with ``transaction`` and ``paymentId``, never 402 and never
  503 + ``Retry-After``. The TypeScript SDK answers ``settlement_unconfirmed``
  the same way (2.98.0, ``src/backend/index.ts``, ``settlementFailureBody``);
* a ``5xx`` whose body says ``retryable: false`` without a transaction: 500;
* an authorization the facilitator says was already used
  (``spent_nonce_evidence``): 409 with the evidence, never 402;
* a rejection (an invalid signature, insufficient funds) keeps the answer it
  had: 402, and 400 in ``require_payment``. So does the opaque ``400
  contract_call_failed (ref)``, which cannot be told apart from an invalid
  signature;
* the mutations: without each new branch, its case is answered as before.

Nothing is delivered in any of them. The SDK re-sends none of them either:
the anti-double-settle guard of ``is_transient_error`` is unchanged.

No ``from __future__ import annotations`` here, for the reason given in
``tests/test_integrations_replay.py``, whose entry points this reuses.
"""
import base64
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional

import pytest

import uvd_x402_sdk.client as client_module
from tests.receipt_rail import PAYER, _receipt, x_payment
from tests.test_integrations_replay import SITES

TX = "0x" + "ab" * 32
PAYMENT_ID = "0x" + "cd" * 32

Answer = tuple[int, dict[str, Any], dict[str, str]]

VERIFIED: Answer = (200, {"isValid": True, "payer": PAYER}, {})
SETTLED: Answer = (
    200, {"success": True, "transaction": TX, "network": "arc", "payer": PAYER}, {}
)


class Scripted:
    """A facilitator over a real socket that answers as it is told."""

    def __init__(self, verify: Answer = VERIFIED, settle: Answer = SETTLED) -> None:
        self.answers = {"/verify": verify, "/settle": settle}
        #: Paths in arrival order.
        self.calls: list = []
        rail = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 - http.server's name
                self.rfile.read(int(self.headers.get("Content-Length", "0")))
                rail.calls.append(self.path)
                missing = (404, {"error": "not_found"}, {})
                status, body, extra = rail.answers.get(self.path, missing)
                data = json.dumps(body).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                for name, value in extra.items():
                    self.send_header(name, value)
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args: object) -> None:
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self._server.server_address[1]}"
        # A short poll: shutdown() waits up to one interval, once per case.
        threading.Thread(
            target=self._server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
        ).start()

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


@pytest.fixture
def scripted():
    opened = []

    def open_rail(**answers: Answer) -> Scripted:
        rail = Scripted(**answers)
        opened.append(rail)
        return rail

    yield open_rail
    for rail in opened:
        rail.close()


def _inner(body: dict[str, Any]) -> dict[str, Any]:
    """FastAPI's dependencies answer ``{"detail": body}``; everything else, ``body``."""
    inner = body.get("detail", body)
    return inner if isinstance(inner, dict) else {}


all_sites = pytest.mark.parametrize("mount", list(SITES), ids=lambda mount: mount.__name__)


# -- the payment may have moved: 500 ------------------------------------------

#: case -> (settle answer, transaction and paymentId the buyer must get, reason)
MAY_HAVE_SETTLED = {
    "502-settlement-unconfirmed": (
        (502, {"error": "settlement_unconfirmed", "transaction": TX, "paymentId": PAYMENT_ID,
               "retryable": False}, {}),
        TX, PAYMENT_ID, "settlement_unconfirmed",
    ),
    "500-with-transaction": (
        (500, {"error": "internal_error", "transaction": TX}, {}),
        TX, None, "internal_error",
    ),
    # The hash wins over the body's own `retryable: true`
    # (FacilitatorError._retryable_verdict), and so over its Retry-After.
    "503-with-transaction-and-retryable-true": (
        (503, {"error": "service_unavailable", "transaction": TX, "retryable": True},
         {"Retry-After": "5"}),
        TX, None, "service_unavailable",
    ),
    "502-retryable-false-without-transaction": (
        (502, {"error": "upstream_failure", "retryable": False}, {}),
        None, None, "upstream_failure",
    ),
}


@all_sites
@pytest.mark.parametrize("case", list(MAY_HAVE_SETTLED))
def test_a_payment_that_may_have_moved_is_500_with_what_to_check(scripted, mount, case):
    settle, transaction, payment_id, reason = MAY_HAVE_SETTLED[case]
    rail = scripted(settle=settle)
    site = mount(rail)

    status, body, headers = site.get(x_payment())

    assert status == 500, body
    inner = _inner(body)
    assert inner["retryable"] is False and inner["safeToReplay"] is False
    assert inner["reason"] == reason
    assert inner.get("transaction") == transaction
    assert inner.get("paymentId") == payment_id
    assert inner["message"] == (
        client_module._MAY_HAVE_SETTLED_MESSAGE if transaction
        else client_module._MAY_HAVE_SETTLED_NO_TRANSACTION_MESSAGE
    )
    assert "retry-after" not in headers  # not an invitation to resend
    assert site.delivered == 0
    assert rail.calls == ["/verify", "/settle"]  # and the SDK did not resend it


@all_sites
def test_the_receipt_of_a_payment_that_may_have_moved_travels_in_payment_response(
    scripted, mount
):
    body = {"error": "settlement_unconfirmed", "transaction": TX, "paymentId": PAYMENT_ID,
            "retryable": False, "receipt": _receipt("pending", TX, None)}
    site = mount(scripted(settle=(502, body, {})))

    status, answer, headers = site.get(x_payment())

    assert status == 500, answer
    sent = json.loads(base64.b64decode(headers["payment-response"]))
    assert sent["receipt"]["settlement"]["id"] == TX


# -- the authorization was already used: 409 ----------------------------------

#: case -> (verify answer, settle answer, evidence, reason)
ALREADY_USED = {
    # A non-EVM nonce store (x402-rs src/chain/stellar.rs, NonceReused).
    "400-nonce-store-wording": (
        VERIFIED, (400, {"error": "Nonce 5 already used for address GABC"}, {}),
        "wording", None,
    ),
    "200-settle-error-reason": (
        VERIFIED,
        (200, {"success": False, "errorReason": "nonce_already_used", "network": "arc",
               "payer": PAYER}, {}),
        "structured", "nonce_already_used",
    ),
    "200-verify-invalid-reason": (
        (200, {"isValid": False, "payer": PAYER,
               "invalidReason": "invalid_exact_evm_payload_authorization_nonce_used"}, {}),
        SETTLED, "wording", None,
    ),
    "409-idempotency-key-conflict": (
        VERIFIED, (409, {"error": "idempotency_key_conflict", "correlation_id": "local"}, {}),
        "structured", "idempotency_key_conflict",
    ),
    # Transient AND spent: the record of a settle of this exact request that
    # succeeded. It was 503; a resend in a new handling carries a new key, and
    # on EVM gets the opaque 400 below, which is a 402.
    "503-idempotency-cache-corrupt": (
        VERIFIED, (503, {"error": "idempotency_cache_corrupt"}, {}),
        "structured", "idempotency_cache_corrupt",
    ),
}


@all_sites
@pytest.mark.parametrize("case", list(ALREADY_USED))
def test_an_authorization_already_used_is_409_with_the_evidence(scripted, mount, case):
    verify, settle, evidence, reason = ALREADY_USED[case]
    site = mount(scripted(verify=verify, settle=settle))

    status, body, headers = site.get(x_payment())

    assert status == 409, body
    inner = _inner(body)
    assert inner["retryable"] is False and inner["safeToReplay"] is False
    assert inner["spentNonceEvidence"] == evidence
    assert inner.get("reason") == reason
    assert inner["message"] == client_module._AUTHORIZATION_ALREADY_USED_MESSAGE
    assert "retry-after" not in headers
    assert site.delivered == 0


# -- a rejection keeps its answer ----------------------------------------------

#: case -> (verify answer, settle answer)
REJECTED = {
    "invalid-signature": (
        (200, {"isValid": False, "invalidReason": "invalid_exact_evm_payload_signature",
               "payer": PAYER}, {}),
        SETTLED,
    ),
    "insufficient-funds-on-verify": (
        (200, {"isValid": False, "invalidReason": "insufficient_funds", "payer": PAYER}, {}),
        SETTLED,
    ),
    "insufficient-funds-on-settle": (
        VERIFIED,
        (200, {"success": False, "errorReason": "insufficient_funds", "network": "arc",
               "payer": PAYER}, {}),
    ),
    # x402-rs's ContractCall arm withholds the revert on purpose: a used
    # authorization and an invalid signature read the same. Closing it takes a
    # stable code from the facilitator (docs/planning/BACKLOG.md, 2026-09-13).
    "opaque-contract-call-failed": (
        VERIFIED, (400, {"error": "contract_call_failed (ref: local)"}, {}),
    ),
}


@all_sites
@pytest.mark.parametrize("case", list(REJECTED))
def test_a_rejection_keeps_the_answer_it_had(scripted, mount, case):
    verify, settle = REJECTED[case]
    site = mount(scripted(verify=verify, settle=settle))

    status, body, _ = site.get(x_payment())

    assert status == SITES[mount], body
    assert site.delivered == 0


@all_sites
def test_a_payment_is_still_delivered(scripted, mount):
    site = mount(scripted())

    status, body, _ = site.get(x_payment())

    assert (status, site.delivered) == (200, 1), body


# -- the mutations: without each branch, its case goes back ---------------------


def _answer(
    scripted, mount, settle: Answer, verify: Answer = VERIFIED
) -> tuple[int, dict[str, Any]]:
    status, body, _ = mount(scripted(verify=verify, settle=settle)).get(x_payment())
    return status, _inner(body)


@all_sites
def test_without_the_broadcast_branch_the_transaction_is_lost(scripted, mount, monkeypatch):
    settle = MAY_HAVE_SETTLED["502-settlement-unconfirmed"][0]
    monkeypatch.setattr(client_module, "_broadcast_transaction", lambda exc: None)

    status, inner = _answer(scripted, mount, settle)

    # Caught by the 5xx branch still, but without the hash to check.
    assert status == 500 and "transaction" not in inner


@all_sites
def test_without_the_500_answer_a_payment_that_may_have_moved_is_a_402(
    scripted, mount, monkeypatch
):
    monkeypatch.setattr(client_module, "_may_have_settled_response", lambda exc, tx: None)

    for case in MAY_HAVE_SETTLED:
        status, _ = _answer(scripted, mount, MAY_HAVE_SETTLED[case][0])
        assert status == SITES[mount], case


@all_sites
def test_without_the_spent_branch_an_authorization_already_used_is_a_402(
    scripted, mount, monkeypatch
):
    monkeypatch.setattr(client_module, "spent_nonce_evidence", lambda exc: None)

    for case in ALREADY_USED:
        verify, settle, _, _ = ALREADY_USED[case]
        status, _ = _answer(scripted, mount, settle, verify)
        # The cache-corrupt row is transient as well, and goes back to its 503.
        assert status == (503 if case == "503-idempotency-cache-corrupt" else SITES[mount]), case


def test_the_mapping_itself_first_match_wins():
    """The order, without a framework: conflict, hash, spent, transient, 5xx."""
    from uvd_x402_sdk.exceptions import FacilitatorError, PaymentSettlementError

    def status(code: Optional[int], body: dict[str, Any]) -> Optional[int]:
        answer = client_module._undelivered_response(
            FacilitatorError("x", status_code=code, response_body=json.dumps(body))
        )
        return None if answer is None else answer[0]

    assert status(409, {"error": "authorization_already_settled"}) == 409
    assert status(409, {"error": "authorization_in_flight"}) == 503
    # A hash on a transient answer is the in-flight settle's: resend it (0.89.0).
    assert status(202, {"error": "settlement_in_progress", "retryable": True,
                        "transaction": TX}) == 503
    assert status(502, {"error": "nonce already used", "transaction": TX}) == 500
    assert status(502, {"error": "nonce already used"}) == 409
    assert status(503, {"error": "receipt_store_unavailable", "retryable": True}) == 503
    assert status(500, {"error": "x", "retryable": False}) == 500
    assert status(400, {"error": "contract_call_failed (ref: a)"}) is None
    assert status(None, {}) == 503
    broadcast = PaymentSettlementError("settle failed", network="arc", tx_hash=TX)
    answer = client_module._undelivered_response(broadcast)
    assert answer is not None and answer[0] == 500 and answer[1]["transaction"] == TX
