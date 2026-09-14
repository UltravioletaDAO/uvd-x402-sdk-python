"""The ``Idempotency-Key`` is opt-in, and bound to the purchase the caller names.

``_Facilitator`` is the ``/verify`` + ``/settle`` idempotency contract of
x402-rs ``post_settle`` over a real socket, the same contract
``tests/test_idempotency_local_facilitator.py`` pins, plus the branches these
tests need:

* ``/verify`` ignores the header and answers valid (it does not model chain
  state; what these tests pin is the settle);
* a keyed ``/settle`` is looked up first: a store it cannot read is
  ``503 idempotency_store_unavailable`` and nothing executes; same key + same
  sha256 of the RAW body is the cached ``200`` with
  ``Idempotent-Replayed: true``; same key + another hash is
  ``409 idempotency_key_conflict``;
* otherwise the settle executes, and the "chain" accepts an authorization once:
  a second execution answers ``400 contract_call_failed (ref)``;
* only a successful settle is cached.

Why the key needs a scope: the docstring of
:func:`uvd_x402_sdk.client.derive_idempotency_key`.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import threading
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import uvd_x402_sdk.client as client_module
from uvd_x402_sdk import X402Client, is_transient_error, spent_nonce_evidence
from uvd_x402_sdk.exceptions import FacilitatorError

RECIPIENT = "0x1234567890123456789012345678901234567890"
PAYER = "0xSender"
PRICE = Decimal("0.01")

# One X-PAYMENT, presented as the buyer sends it (base64 JSON).
X_PAYMENT = base64.b64encode(
    json.dumps(
        {
            "x402Version": 1,
            "scheme": "exact",
            "network": "base",
            "payload": {
                "signature": "0xsig",
                "authorization": {
                    "from": PAYER,
                    "to": RECIPIENT,
                    "value": "10000",
                    "validAfter": "0",
                    "validBefore": "9999999999",
                    "nonce": "0x01",
                },
            },
        }
    ).encode("utf-8")
).decode("ascii")


class _Facilitator:
    def __init__(self) -> None:
        self.calls: list = []  # (path, Idempotency-Key or None), in arrival order
        self.executed = 0
        self.replayed = 0
        self.store_down = False
        self._records: dict = {}
        self._settled: set = set()
        self._lock = threading.Lock()
        local = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 - http.server's name
                raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                key = (self.headers.get("Idempotency-Key") or "").strip() or None
                status, body, extra = local.handle(self.path, key, raw)
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
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    @property
    def moved(self) -> int:
        """Authorizations the chain executed: money that actually moved."""
        return len(self._settled)

    def keys(self, path: str) -> list:
        return [key for called, key in self.calls if called == path]

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    def handle(self, path: str, key, raw: bytes):
        with self._lock:
            self.calls.append((path, key))
            if path == "/verify":
                return 200, {"isValid": True, "payer": PAYER}, {}
            if path != "/settle":
                return 404, {"error": "not_found"}, {}

            body_hash = hashlib.sha256(raw).hexdigest()
            if key is not None:
                if self.store_down:
                    body = {"error": "idempotency_store_unavailable", "correlation_id": "local"}
                    return 503, body, {}
                record = self._records.get(key)
                if record is not None:
                    recorded_hash, response = record
                    if recorded_hash == body_hash:
                        self.replayed += 1
                        return 200, response, {"Idempotent-Replayed": "true"}
                    return 409, {"error": "idempotency_key_conflict", "correlation_id": "local"}, {}

            self.executed += 1
            authorization = json.dumps(json.loads(raw)["paymentPayload"]["payload"], sort_keys=True)
            if authorization in self._settled:
                return 400, {"error": "contract_call_failed (ref: local)"}, {}
            self._settled.add(authorization)
            response = {
                "success": True,
                "transaction": f"0xf00d{len(self._settled)}",
                "network": "base",
                "payer": PAYER,
            }
            if key is not None:
                self._records[key] = (body_hash, response)
            return 200, response, {}


@pytest.fixture
def facilitator():
    local = _Facilitator()
    yield local
    local.close()


def _seller(facilitator: _Facilitator, **config) -> X402Client:
    """A fresh client, as a seller process that restarted would build it."""
    return X402Client(recipient_address=RECIPIENT, facilitator_url=facilitator.url, **config)


# -- (a) off by default


def test_by_default_neither_verify_nor_settle_carries_the_key(facilitator):
    seller = _seller(facilitator)
    assert seller.config.send_idempotency_key is False

    seller.process_payment(X_PAYMENT, PRICE)

    assert facilitator.keys("/verify") == [None]
    assert facilitator.keys("/settle") == [None]


def test_by_default_a_second_purchase_is_not_answered_from_the_first_ones_cache(facilitator):
    """A key derived from the payment alone does not tell two purchases of the
    same price apart: their settle requests are byte-identical. Without a key,
    the second purchase's settle executes and stands or falls on its own."""
    _seller(facilitator).process_payment(X_PAYMENT, PRICE)

    with pytest.raises(FacilitatorError) as caught:
        _seller(facilitator).process_payment(X_PAYMENT, PRICE)

    assert caught.value.status_code == 400
    assert facilitator.replayed == 0
    assert facilitator.moved == 1


# -- (b) key on, two purchases: two keys


def test_with_the_key_on_two_scopes_do_not_share_a_cached_settle(facilitator):
    """The same payment named for another purchase gets another key, so the
    facilitator executes that settle instead of replaying the first one."""
    _seller(facilitator, send_idempotency_key=True).process_payment(
        X_PAYMENT, PRICE, idempotency_scope="order-a"
    )

    with pytest.raises(FacilitatorError) as caught:
        _seller(facilitator, send_idempotency_key=True).process_payment(
            X_PAYMENT, PRICE, idempotency_scope="order-b"
        )

    first, second = facilitator.keys("/settle")
    assert first is not None and second is not None
    assert first != second
    assert caught.value.status_code == 400
    assert facilitator.replayed == 0
    assert facilitator.moved == 1


# -- (c) key on, the same purchase retried: one key


def test_with_the_key_on_the_same_scope_replays_the_settle_that_completed(facilitator):
    """The retry of the same purchase, from a seller that restarted, comes back
    from the cache with the first transaction; nothing executes twice."""
    first = _seller(facilitator, send_idempotency_key=True).process_payment(
        X_PAYMENT, PRICE, idempotency_scope="order-a"
    )
    again = _seller(facilitator, send_idempotency_key=True).process_payment(
        X_PAYMENT, PRICE, idempotency_scope="order-a"
    )

    assert again.transaction_hash == first.transaction_hash
    assert facilitator.replayed == 1
    assert facilitator.executed == 1
    assert len(set(facilitator.keys("/settle"))) == 1


# -- (d) key on, no scope: fail-safe


def test_with_the_key_on_and_no_scope_no_key_goes_out_and_it_warns_once(
    facilitator, monkeypatch, caplog
):
    """The config asks for the key but the call names no purchase. The requests
    go out exactly as with the key off, and the SDK says so once per process."""
    monkeypatch.setattr(
        client_module, "_missing_idempotency_scope_warned", False, raising=False
    )
    seller = _seller(facilitator, send_idempotency_key=True)

    with caplog.at_level(logging.WARNING, logger="uvd_x402_sdk.client"):
        seller.process_payment(X_PAYMENT, PRICE)
        _seller(facilitator, send_idempotency_key=True).try_settle_payment(
            seller.extract_payload(X_PAYMENT), PRICE
        )

    assert facilitator.keys("/verify") == [None]
    assert facilitator.keys("/settle") == [None, None]
    warned = [r for r in caplog.records if "idempotency_scope" in r.getMessage()]
    assert len(warned) == 1


# -- (e) the two idempotency answers, classified


def test_a_conflict_under_the_scoped_key_is_evidence_of_a_settle_not_a_transient(facilitator):
    """The same authorization for the same purchase under other terms. The 409
    exists only because a settle under that key already succeeded: do not
    release the purchase, do not ask for a new signature, do not retry."""
    seller = _seller(facilitator, send_idempotency_key=True)
    payload = seller.extract_payload(X_PAYMENT)
    seller.settle_payment(payload, PRICE, idempotency_scope="order-a")

    with pytest.raises(FacilitatorError) as caught:
        seller.settle_payment(payload, Decimal("0.02"), idempotency_scope="order-a")

    assert caught.value.status_code == 409
    assert spent_nonce_evidence(caught.value) == "structured"
    assert not is_transient_error(caught.value)
    assert facilitator.executed == 1


def test_an_unreadable_store_is_transient_and_the_same_credential_settles_later(facilitator):
    """Fail-closed: nothing executed and nothing moved, so presenting the SAME
    credential again is the right answer, and it settles once the store is back."""
    seller = _seller(facilitator, send_idempotency_key=True)
    payload = seller.extract_payload(X_PAYMENT)
    facilitator.store_down = True

    with pytest.raises(FacilitatorError) as caught:
        seller.settle_payment(payload, PRICE, idempotency_scope="order-a")

    assert caught.value.status_code == 503
    assert is_transient_error(caught.value)
    assert spent_nonce_evidence(caught.value) is None
    assert facilitator.moved == 0

    facilitator.store_down = False
    assert seller.settle_payment(payload, PRICE, idempotency_scope="order-a").success
    assert facilitator.moved == 1


def test_a_409_that_is_not_an_idempotency_conflict_is_no_evidence_of_a_settle():
    """Only the idempotency conflict proves a settle. The classification dates
    from 0.83.0 (``tests/test_spent_nonce.py``); this negative sits next to the
    scoped positive above so the pair cannot drift apart."""
    exc = FacilitatorError(
        message="Facilitator settle failed with status 409",
        status_code=409,
        response_body=json.dumps({"error": "request_conflict", "correlation_id": "local"}),
    )

    assert spent_nonce_evidence(exc) is None
    assert not is_transient_error(exc)
