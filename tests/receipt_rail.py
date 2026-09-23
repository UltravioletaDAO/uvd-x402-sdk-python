"""A facilitator over a real socket, in the three shapes a seller meets today.

``mode="receipts"`` is the receipt rail of x402-rs 2.39.0 (PR #97, merge
``91d12f94``): ``docs/facilitator-receipts.md``, "Replays of an admitted
authorization", and ``src/receipts/mod.rs`` (``verify``, ``settle``,
``replay``, ``bound``). An authorization it already admitted gets its original
answer back only with the binding that admitted it, the same
``Idempotency-Key`` or the same ``X-UVD-Purchase``:

* ``/settle`` bound: the original ``200`` with ``Idempotent-Replayed: true``,
  or ``202 settlement_in_progress`` while the payment is in flight;
* ``/verify`` bound: the stored verdict and receipt;
* unbound: ``409 authorization_already_settled`` (confirmed) or
  ``409 authorization_in_flight``, with the receipt when the payment has no
  purchase context, never ``success: true`` nor ``Idempotent-Replayed``;
  ``/verify`` answers ``isValid: false`` with the same reason;
* another purchase context or other terms: ``409 receipt_request_conflict``.

``mode="receipts-2.38"`` is the same rail as 2.36.0 to 2.38.0 shipped it
(``src/receipts/mod.rs`` at ``cc2cf345``; production until 2026-09-23
07:06:29Z): those versions did not tie the replay to the binding, so they
answer a resend of the same request with the original answer, binding or not,
and the stored verdict on ``/verify``.

``mode="legacy"`` is a network without receipts, the ``post_settle``
idempotency contract of x402-rs 2.28.0 that
``tests/test_idempotency_local_facilitator.py`` pins: ``/verify`` ignores the
key; same key + same raw body is the cached ``200`` with
``Idempotent-Replayed: true``; same key + another body is ``409
idempotency_key_conflict``; only a success is cached; otherwise the settle
executes, and the "chain" answers a second execution of one authorization with
``400 contract_call_failed (ref)``.

In every mode the "chain" moves an authorization once. ``hold_before_confirm``
keeps the first settle in flight for that long (the pending window);
``hold_after_confirm`` confirms it and then sits on the answer (a response lost
after the payment moved). ``store_down`` is a store the facilitator cannot
read: ``503 receipt_store_unavailable`` on ``/settle`` with receipts, ``503
idempotency_store_unavailable`` on a keyed ``/settle`` without; nothing moves.

It is NOT the facilitator binary, for the reasons given in
``tests/test_idempotency_local_facilitator.py``.
"""
from __future__ import annotations

import base64
import copy
import hashlib
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Optional

from uvd_x402_sdk.receipts import commitment

MODES = ("receipts", "receipts-2.38", "legacy")

PAYER = "0xSender"
RECIPIENT = "0x2222222222222222222222222222222222222222"

_VECTORS = json.loads((Path(__file__).parent / "fixtures/facilitator-receipts-v1.json").read_text())


def x_payment(nonce: str = "0x01", network: str = "arc") -> str:
    """The X-PAYMENT a buyer presents, base64 JSON, for one authorization."""
    envelope = {
        "x402Version": 1,
        "scheme": "exact",
        "network": network,
        "payload": {
            "signature": "0xsig",
            "authorization": {
                "from": PAYER,
                "to": RECIPIENT,
                "value": "10000",
                "validAfter": "0",
                "validBefore": "9999999999",
                "nonce": nonce,
            },
        },
    }
    return base64.b64encode(json.dumps(envelope).encode("utf-8")).decode("ascii")


def _receipt(status: str, tx: Optional[str], purchase_id: Optional[str]) -> Dict[str, Any]:
    """A receipt that passes ``parse_receipt``: the Arc USDC fixture, restated."""
    receipt = copy.deepcopy(_VECTORS["cases"][0]["receipt"])
    receipt.update(
        operation="settle",
        status=status,
        purchaseId=purchase_id,
        settlement={"id": tx, "idType": "evm-transaction-hash"} if tx else None,
        proof=None,
    )
    if status == "pending":
        receipt["retry"] = {"action": "poll", "afterSeconds": 2}
    receipt["request"]["purchaseId"] = purchase_id
    receipt["requestHash"] = commitment(receipt["requestHashVersion"], receipt["request"])
    return receipt


def _purchase_id(capability: Optional[str]) -> Optional[str]:
    if capability is None:
        return None
    return json.loads(base64.b64decode(capability))["purchaseId"]


class _Record:
    def __init__(self, fingerprint: str, key: Optional[str], capability: Optional[str]) -> None:
        self.fingerprint = fingerprint
        self.key = key
        self.capability = capability
        self.status = "pending"
        self.body: Dict[str, Any] = {}
        self.receipt = _receipt("pending", None, _purchase_id(capability))


class Facilitator:
    def __init__(
        self,
        mode: str = "receipts",
        *,
        hold_before_confirm: float = 0.0,
        hold_after_confirm: float = 0.0,
        store_down: bool = False,
    ) -> None:
        assert mode in MODES, mode
        self.mode = mode
        self.store_down = store_down
        self.hold_before_confirm = hold_before_confirm
        self.hold_after_confirm = hold_after_confirm
        #: (path, Idempotency-Key or None, X-UVD-Purchase or None), in arrival order.
        self.calls: list = []
        self.executed = 0
        self._records: Dict[str, _Record] = {}
        self._cache: Dict[str, tuple] = {}
        self._settled: set = set()
        self._lock = threading.Lock()
        rail = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 - http.server's name
                raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                key = (self.headers.get("Idempotency-Key") or "").strip() or None
                capability = self.headers.get("X-UVD-Purchase")
                status, body, extra = rail.handle(self.path, key, capability, raw)
                data = json.dumps(body).encode("utf-8")
                try:
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(data)))
                    for name, value in extra.items():
                        self.send_header(name, value)
                    self.end_headers()
                    self.wfile.write(data)
                except OSError:
                    pass  # the client gave up on this request; the settle stands

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
        return [key for called, key, _ in self.calls if called == path]

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    # -- the contract -------------------------------------------------------

    def handle(self, path: str, key: Optional[str], capability: Optional[str], raw: bytes):
        with self._lock:
            self.calls.append((path, key, capability))
            if path not in ("/verify", "/settle"):
                return 404, {"error": "not_found"}, {}
            request = json.loads(raw)
            authorization = json.dumps(
                request["paymentPayload"]["payload"]["authorization"], sort_keys=True
            )
            fingerprint = hashlib.sha256(
                json.dumps(request, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
            if self.store_down and path == "/settle":
                if self.mode != "legacy":
                    return 503, _failure("receipt_store_unavailable", retryable=True), {}
                if key is not None:
                    body = {"error": "idempotency_store_unavailable", "correlation_id": "local"}
                    return 503, body, {}
            if self.mode == "legacy":
                answered = self._legacy_lookup(path, key, raw)
                if answered is not None:
                    return answered
            else:
                record = self._records.get(authorization)
                if path == "/verify":
                    return self._verify(record, fingerprint, key, capability)
                if record is not None:
                    return self._replay(record, fingerprint, key, capability)
                record = _Record(fingerprint, key, capability)
                self._records[authorization] = record
            self.executed += 1
            first = self.executed == 1
            if self.mode == "legacy" and authorization in self._settled:
                return 400, {"error": "contract_call_failed (ref: local)"}, {}

        if first and self.hold_before_confirm:
            time.sleep(self.hold_before_confirm)
        with self._lock:
            self._settled.add(authorization)
            tx = f"0xf00d{self.executed}"
            body: Dict[str, Any] = {
                "success": True,
                "transaction": tx,
                "network": "arc",
                "payer": PAYER,
            }
            if self.mode != "legacy":
                record.status = "confirmed"
                record.receipt = _receipt("confirmed", tx, _purchase_id(capability))
                body["receipt"] = record.receipt
                record.body = body
        if first and self.hold_after_confirm:
            time.sleep(self.hold_after_confirm)
        if self.mode == "legacy" and key is not None:
            # Written after the settle, with no lock on the key meanwhile, as
            # x402-rs writes it (fire-and-forget).
            with self._lock:
                self._cache[key] = (hashlib.sha256(raw).hexdigest(), body)
        return 200, body, {}

    def _bound(self, record: _Record, fingerprint: str, key, capability) -> bool:
        if record.fingerprint != fingerprint or record.capability != capability:
            return False
        if self.mode == "receipts-2.38":
            return True
        return record.capability is not None or (key is not None and key == record.key)

    def _admitted_reason(self, record: _Record) -> str:
        if record.status == "confirmed":
            return "authorization_already_settled"
        return "authorization_in_flight"

    def _verify(self, record: Optional[_Record], fingerprint: str, key, capability):
        if record is None:
            return 200, {"isValid": True, "payer": PAYER}, {}
        if self._bound(record, fingerprint, key, capability):
            return 200, {"isValid": True, "payer": PAYER, "receipt": record.receipt}, {}
        body: Dict[str, Any] = {
            "isValid": False,
            "invalidReason": self._admitted_reason(record),
            "payer": PAYER,
        }
        if record.fingerprint == fingerprint and record.capability is None:
            body["receipt"] = record.receipt
        return 200, body, {}

    def _replay(self, record: _Record, fingerprint: str, key, capability):
        if record.fingerprint != fingerprint or record.capability != capability:
            return 409, _failure("receipt_request_conflict", retryable=False), {}
        if self._bound(record, fingerprint, key, capability):
            if record.status == "pending":
                body = {
                    "success": False,
                    "error": "settlement_in_progress",
                    "retryable": True,
                    "safeToReplay": False,
                    "receipt": record.receipt,
                }
                return 202, body, {"Idempotent-Replayed": "true"}
            return 200, record.body, {"Idempotent-Replayed": "true"}
        body = _failure(self._admitted_reason(record), retryable=False)
        if record.capability is None:
            body["receipt"] = record.receipt
        return 409, body, {"Cache-Control": "no-store"}

    def _legacy_lookup(self, path: str, key, raw: bytes):
        """The answers a network without receipts gives before executing, if any."""
        if path == "/verify":
            return 200, {"isValid": True, "payer": PAYER}, {}
        if key is not None and key in self._cache:
            cached_hash, cached = self._cache[key]
            if cached_hash == hashlib.sha256(raw).hexdigest():
                return 200, cached, {"Idempotent-Replayed": "true"}
            return 409, {"error": "idempotency_key_conflict", "correlation_id": "local"}, {}
        return None


def _failure(code: str, *, retryable: bool) -> Dict[str, Any]:
    return {"success": False, "error": code, "retryable": retryable, "safeToReplay": False}
