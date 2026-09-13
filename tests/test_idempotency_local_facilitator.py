"""A repeated settle of the same authorization does not execute twice.

``_LocalFacilitator`` is the ``/settle`` idempotency contract of x402-rs 2.28.0
(origin/main 331d31d4, ``src/handlers.rs`` ``post_settle`` and its
``settle_idempotency_tests``) over a real socket:

* the key comes from the ``Idempotency-Key`` header and the RAW body is hashed
  with sha256 (``idempotency_store::hash_request_body``, no JSON re-encoding),
  so the bytes compared are the bytes the SDK put on the wire;
* same key + same hash: the cached response, ``200`` with
  ``Idempotent-Replayed: true``, nothing executes;
* same key + different hash: ``409 {"error": "idempotency_key_conflict"}``;
* only a successful settle is cached;
* the "chain" accepts an authorization once (EIP-3009), and a second execution
  answers the way the ContractCall arm does: ``400 contract_call_failed (ref)``,
  with nothing in it that says "nonce".

It is NOT the facilitator binary. Running x402-rs locally needs chain RPC and a
funded signer, and its own handoff (``docs/handoffs/2026-09-02-mcp-listo.md``)
records ``POST /settle`` hanging locally without AWS credentials.

Only APIs that existed before 0.83.0 are imported here, so this file runs
against the previous release and shows what changed: there, the second settle
reaches the chain.
"""
from __future__ import annotations

import hashlib
import json
import threading
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from uvd_x402_sdk import X402Client
from uvd_x402_sdk.exceptions import FacilitatorError
from uvd_x402_sdk.models import PaymentPayload

RECIPIENT = "0x1234567890123456789012345678901234567890"

# The X-PAYMENT a buyer presents, as JSON text. Every seller below parses its
# own copy: a key that matched only because two calls shared one object would
# prove nothing about a seller that restarted.
HEADER_JSON = json.dumps(
    {
        "x402Version": 1,
        "scheme": "exact",
        "network": "base",
        "payload": {
            "signature": "0xsig",
            "authorization": {
                "from": "0xSender",
                "to": RECIPIENT,
                "value": "10000",
                "validAfter": "0",
                "validBefore": "9999999999",
                "nonce": "0x01",
            },
        },
    }
)


def _payload() -> PaymentPayload:
    return PaymentPayload(**json.loads(HEADER_JSON))


class _LocalFacilitator:
    def __init__(self) -> None:
        self.executed = 0
        self.keys: list = []
        self.replayed = 0
        self._cache: dict = {}
        self._settled: set = set()
        self._lock = threading.Lock()
        local = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 - http.server's name
                raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                key = (self.headers.get("Idempotency-Key") or "").strip() or None
                status, body, extra = local.handle(self.path, key, raw)
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                for name, value in extra.items():
                    self.send_header(name, value)
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args: object) -> None:
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self._server.server_address[1]}"
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    def handle(self, path: str, key, raw: bytes):
        with self._lock:
            self.keys.append((path, key))
            if path != "/settle":
                return 404, b"{}", {}
            request_hash = hashlib.sha256(raw).hexdigest()
            if key is not None and key in self._cache:
                cached_hash, cached = self._cache[key]
                if cached_hash == request_hash:
                    self.replayed += 1
                    return 200, cached, {"Idempotent-Replayed": "true"}
                conflict = {"error": "idempotency_key_conflict", "correlation_id": "local"}
                return 409, json.dumps(conflict).encode(), {}

            self.executed += 1
            authorization = json.dumps(json.loads(raw)["paymentPayload"], sort_keys=True)
            if authorization in self._settled:
                return 400, b'{"error": "contract_call_failed (ref: local)"}', {}
            self._settled.add(authorization)
            body = json.dumps(
                {
                    "success": True,
                    "transaction": f"0xf00d{self.executed}",
                    "network": "base",
                    "payer": "0xSender",
                }
            ).encode()
            if key is not None:
                self._cache[key] = (request_hash, body)
            return 200, body, {}


@pytest.fixture
def facilitator():
    local = _LocalFacilitator()
    yield local
    local.close()


def _seller(facilitator: _LocalFacilitator, **config) -> X402Client:
    """A fresh client, as a seller process that restarted would build it."""
    return X402Client(recipient_address=RECIPIENT, facilitator_url=facilitator.url, **config)


def test_a_repeated_settle_of_the_same_authorization_executes_once(facilitator):
    """The buyer re-presents the same X-PAYMENT, and the seller that took it
    has restarted in between.

    Without the key the second settle reaches the chain, which refuses the
    spent authorization with an opaque 400: the seller's only reading of that
    is "rejected", and a paywall answers 402, "sign again", for a payment that
    already moved. With it the facilitator answers the first settle's success."""
    first = _seller(facilitator).try_settle_payment(_payload(), Decimal("0.01"))
    again = _seller(facilitator).try_settle_payment(_payload(), Decimal("0.01"))

    assert facilitator.executed == 1, f"the retry reached the chain: {again}"
    assert first["success"] and again["success"]
    assert again["tx_hash"] == first["tx_hash"]
    assert facilitator.replayed == 1


def test_the_same_authorization_under_other_terms_is_refused_not_executed(facilitator):
    _seller(facilitator).settle_payment(_payload(), Decimal("0.01"))

    with pytest.raises(FacilitatorError) as caught:
        _seller(facilitator).settle_payment(_payload(), Decimal("0.02"))

    assert facilitator.executed == 1
    assert caught.value.status_code == 409
    assert caught.value.error_code == "idempotency_key_conflict"


def test_verify_and_settle_carry_different_keys_for_the_same_authorization(facilitator):
    """The store is one namespace; a verify cache must never answer a settle."""
    client = _seller(facilitator)
    try:
        client.verify_payment(_payload(), Decimal("0.01"))
    except FacilitatorError:
        pass  # the local facilitator has no /verify; only the key it saw matters
    client.settle_payment(_payload(), Decimal("0.01"))

    keys = dict(facilitator.keys)
    assert keys["/verify"] and keys["/settle"]
    assert keys["/verify"] != keys["/settle"]


def test_with_the_key_switched_off_the_retry_reaches_the_chain(facilitator):
    """Control: the switch exists for a facilitator whose store is down, and
    what it gives back is exactly the previous behaviour."""
    _seller(facilitator, send_idempotency_key=False).try_settle_payment(
        _payload(), Decimal("0.01")
    )
    again = _seller(facilitator, send_idempotency_key=False).try_settle_payment(
        _payload(), Decimal("0.01")
    )

    assert facilitator.executed == 2
    assert not again["success"]
    assert all(key is None for _, key in facilitator.keys)
