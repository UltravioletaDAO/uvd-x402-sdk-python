"""``derive_idempotency_key`` and the header on the wire.

What the facilitator does with the key is exercised in
``tests/test_idempotency_local_facilitator.py``; why the key is built the way it
is, in the docstring of :func:`uvd_x402_sdk.client.derive_idempotency_key`.
"""
from __future__ import annotations

import base64
import json
import re
from decimal import Decimal

import httpx
import pytest

from uvd_x402_sdk import IDEMPOTENCY_KEY_HEADER, X402Client, derive_idempotency_key
from uvd_x402_sdk.models import PaymentPayload

RECIPIENT = "0x1234567890123456789012345678901234567890"


def _authorization(nonce: str = "0x01") -> dict:
    return {
        "from": "0xSender",
        "to": RECIPIENT,
        "value": "10000",
        "validAfter": "0",
        "validBefore": "9999999999",
        "nonce": nonce,
    }


def _payload(
    signature: str = "0xsig", nonce: str = "0x01", network: str = "base"
) -> PaymentPayload:
    return PaymentPayload(
        x402Version=1,
        scheme="exact",
        network=network,
        payload={"signature": signature, "authorization": _authorization(nonce)},
    )


class _Response:
    def __init__(self, body: dict, status_code: int = 200):
        self.status_code = status_code
        self._body = body
        self.text = json.dumps(body)
        self.headers: dict = {}

    def json(self) -> dict:
        return self._body


class _RecordingClient:
    """Stands in for httpx.Client: records every POST with its headers."""

    def __init__(self, script):
        self.script = list(script)
        self.calls: list = []

    def post(self, url, json=None, headers=None, timeout=None):  # noqa: A002
        self.calls.append({"url": url, "headers": dict(headers or {})})
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


_SETTLE_OK = {"success": True, "transaction": "0xf00d", "payer": "0xSender", "network": "base"}
_VERIFY_OK = {"isValid": True, "payer": "0xSender"}


def _wire(monkeypatch, script, **config):
    client = X402Client(recipient_address=RECIPIENT, **config)
    fake = _RecordingClient(script)
    monkeypatch.setattr(client, "_get_http_client", lambda: fake)
    return client, fake


class TestDerive:
    def test_the_shape(self):
        key = derive_idempotency_key(_payload(), "settle")
        assert re.fullmatch(r"x402-settle-[0-9a-f]{64}", key)

    def test_the_pinned_vector(self):
        """For the TypeScript twin: this block, keys sorted at every level and no
        whitespace, is exactly the text below, and the key is its sha256. The
        digest was computed from the literal text, not from this SDK."""
        canonical = (
            '{"authorization":{"from":"0xSender","nonce":"0x01",'
            '"to":"0x1234567890123456789012345678901234567890",'
            '"validAfter":"0","validBefore":"9999999999","value":"10000"},'
            '"signature":"0xsig"}'
        )
        assert json.dumps(
            _payload().payload, sort_keys=True, separators=(",", ":")
        ) == canonical
        assert derive_idempotency_key(_payload(), "settle") == (
            "x402-settle-6b62041bcbd7d49d05741f8bcd646f73ab239a76582b119c49e9679dc1cf6c1d"
        )

    def test_the_same_authorization_gives_the_same_key_whatever_the_key_order(self):
        reordered = PaymentPayload(
            x402Version=1,
            scheme="exact",
            network="base",
            payload={
                "authorization": dict(reversed(list(_authorization().items()))),
                "signature": "0xsig",
            },
        )
        assert derive_idempotency_key(reordered, "settle") == derive_idempotency_key(
            _payload(), "settle"
        )

    def test_another_authorization_gives_another_key(self):
        key = derive_idempotency_key(_payload(), "settle")
        assert derive_idempotency_key(_payload(nonce="0x02"), "settle") != key
        assert derive_idempotency_key(_payload(signature="0xother"), "settle") != key

    def test_only_the_signed_block_enters_the_key(self):
        # The same authorization named in either network dialect is the same
        # authorization; the envelope around it is not part of the key.
        assert derive_idempotency_key(_payload(network="eip155:8453"), "settle") == (
            derive_idempotency_key(_payload(network="base"), "settle")
        )

    def test_verify_and_settle_are_namespaced(self):
        verify = derive_idempotency_key(_payload(), "verify")
        settle = derive_idempotency_key(_payload(), "settle")
        assert verify.startswith("x402-verify-")
        assert settle.startswith("x402-settle-")
        assert verify != settle

    def test_an_empty_block_gets_no_key(self):
        empty = PaymentPayload(x402Version=1, scheme="exact", network="base", payload={})
        assert derive_idempotency_key(empty, "settle") is None

    def test_an_unknown_operation_is_refused(self):
        with pytest.raises(ValueError):
            derive_idempotency_key(_payload(), "refund")

    def test_the_pinned_scoped_vector(self):
        """The same for a scope: the JSON array ``["x402-idempotency-scope/1",
        <block>, <scope>]``, keys sorted at every level and no whitespace, is
        exactly the text below, and the key is its sha256. The digest was
        computed from the literal text, not from this SDK."""
        canonical = (
            '["x402-idempotency-scope/1",'
            '{"authorization":{"from":"0xSender","nonce":"0x01",'
            '"to":"0x1234567890123456789012345678901234567890",'
            '"validAfter":"0","validBefore":"9999999999","value":"10000"},'
            '"signature":"0xsig"},"order-1"]'
        )
        assert json.dumps(
            ["x402-idempotency-scope/1", _payload().payload, "order-1"],
            sort_keys=True,
            separators=(",", ":"),
        ) == canonical
        assert derive_idempotency_key(_payload(), "settle", scope="order-1") == (
            "x402-settle-ef86baf730caf7d60f5ec8c88e3c8904e211beb42139b9e37b28b0223a3d6083"
        )

    def test_the_pinned_non_ascii_scoped_vector(self):
        """Non-ASCII text is hashed as its UTF-8 bytes, never as JSON escapes: the
        scope enters the canonical text exactly as written below. Computed from
        the literal text, like the vectors above."""
        assert derive_idempotency_key(_payload(), "settle", scope="pedido-ñandú-€") == (
            "x402-settle-28ffb4b65f92ae391d745f14df3bac41ffcce883b53b06261094fce7e1dbd629"
        )

    def test_a_block_shaped_like_a_scoped_material_does_not_derive_the_scoped_key(self):
        """Domain separation. 0.83.1 hashed the object ``{"payload": <block>,
        "scope": <scope>}`` for a scoped key, so the UNSCOPED key of a block built
        with that shape was the scoped key. A block is always a JSON object and
        the scoped material is now an array, so no shape of block reaches it."""
        shaped = PaymentPayload(
            x402Version=1,
            scheme="exact",
            network="base",
            payload={"payload": _payload().payload, "scope": "order-1"},
        )
        assert derive_idempotency_key(shaped, "settle") != (
            derive_idempotency_key(_payload(), "settle", scope="order-1")
        )

    def test_no_scope_is_the_unscoped_key(self):
        assert derive_idempotency_key(_payload(), "settle", scope=None) == (
            derive_idempotency_key(_payload(), "settle")
        )

    def test_the_same_authorization_and_scope_give_the_same_key(self):
        assert derive_idempotency_key(_payload(), "settle", scope="order-1") == (
            derive_idempotency_key(_payload(), "settle", scope="order-1")
        )

    def test_another_scope_gives_another_key(self):
        key = derive_idempotency_key(_payload(), "settle", scope="order-1")
        assert derive_idempotency_key(_payload(), "settle", scope="order-2") != key
        assert derive_idempotency_key(_payload(), "settle") != key

    def test_the_scope_does_not_take_the_block_out_of_the_key(self):
        # Two sellers that name their purchases alike still get two keys.
        assert derive_idempotency_key(_payload(nonce="0x02"), "settle", scope="order-1") != (
            derive_idempotency_key(_payload(), "settle", scope="order-1")
        )

    def test_verify_and_settle_stay_namespaced_under_a_scope(self):
        assert derive_idempotency_key(_payload(), "verify", scope="order-1") != (
            derive_idempotency_key(_payload(), "settle", scope="order-1")
        )

    @pytest.mark.parametrize("scope", ["", "   "], ids=["empty", "blank"])
    def test_an_empty_scope_is_refused(self, scope):
        with pytest.raises(ValueError):
            derive_idempotency_key(_payload(), "settle", scope=scope)

    def test_a_scope_that_is_not_a_string_is_refused(self):
        with pytest.raises(TypeError):
            derive_idempotency_key(_payload(), "settle", scope=42)


class TestOnTheWire:
    def test_settle_sends_the_scoped_key(self, monkeypatch):
        client, fake = _wire(monkeypatch, [_Response(_SETTLE_OK)], send_idempotency_key=True)
        client.settle_payment(_payload(), Decimal("0.01"), idempotency_scope="order-1")

        headers = fake.calls[0]["headers"]
        assert headers[IDEMPOTENCY_KEY_HEADER] == derive_idempotency_key(
            _payload(), "settle", scope="order-1"
        )
        assert headers["Content-Type"] == "application/json"

    def test_verify_sends_its_own_scoped_key(self, monkeypatch):
        client, fake = _wire(monkeypatch, [_Response(_VERIFY_OK)], send_idempotency_key=True)
        client.verify_payment(_payload(), Decimal("0.01"), idempotency_scope="order-1")

        assert fake.calls[0]["headers"][IDEMPOTENCY_KEY_HEADER] == derive_idempotency_key(
            _payload(), "verify", scope="order-1"
        )

    def test_the_switch_sends_no_key(self, monkeypatch):
        client, fake = _wire(
            monkeypatch,
            [_Response(_VERIFY_OK), _Response(_SETTLE_OK)],
            send_idempotency_key=False,
        )
        client.verify_payment(_payload(), Decimal("0.01"), idempotency_scope="order-1")
        client.settle_payment(_payload(), Decimal("0.01"), idempotency_scope="order-1")

        assert len(fake.calls) == 2
        assert all(IDEMPOTENCY_KEY_HEADER not in call["headers"] for call in fake.calls)

    @pytest.mark.parametrize("scope", ["", "   "], ids=["empty", "blank"])
    def test_a_blank_scope_is_no_scope(self, monkeypatch, scope):
        client, fake = _wire(monkeypatch, [_Response(_SETTLE_OK)], send_idempotency_key=True)
        client.settle_payment(_payload(), Decimal("0.01"), idempotency_scope=scope)

        assert IDEMPOTENCY_KEY_HEADER not in fake.calls[0]["headers"]

    def test_a_scope_that_is_not_a_string_is_refused_before_anything_is_sent(self, monkeypatch):
        client, fake = _wire(monkeypatch, [_Response(_SETTLE_OK)], send_idempotency_key=True)

        with pytest.raises(TypeError):
            client.settle_payment(_payload(), Decimal("0.01"), idempotency_scope=42)
        assert fake.calls == []

    def test_the_timeout_fallback_asks_with_the_same_key(self, monkeypatch):
        """The fallback asks "did my settle land?". Under the same key, a settle
        that completed is answered from the facilitator's cache instead of being
        executed a second time."""
        client, fake = _wire(
            monkeypatch,
            [httpx.TimeoutException("too slow"), _Response(_SETTLE_OK)],
            send_idempotency_key=True,
        )
        client.settle_payment(_payload(), Decimal("0.01"), idempotency_scope="order-1")

        assert len(fake.calls) == 2
        first, fallback = (call["headers"].get(IDEMPOTENCY_KEY_HEADER) for call in fake.calls)
        assert first is not None
        assert fallback == first

    def test_a_settle_with_retry_sends_the_scoped_key_on_every_attempt(self, monkeypatch):
        monkeypatch.setattr("uvd_x402_sdk.client.time.sleep", lambda seconds: None)
        client, fake = _wire(
            monkeypatch,
            [
                _Response({"error": "upstream unavailable"}, status_code=502),
                _Response(_SETTLE_OK),
            ],
            send_idempotency_key=True,
        )
        client.settle_payment(
            _payload(), Decimal("0.01"), retry=True, idempotency_scope="order-1"
        )

        expected = derive_idempotency_key(_payload(), "settle", scope="order-1")
        sent = [call["headers"].get(IDEMPOTENCY_KEY_HEADER) for call in fake.calls]
        assert sent == [expected, expected]

    def test_process_payment_sends_the_scoped_key_on_verify_and_on_settle(self, monkeypatch):
        client, fake = _wire(
            monkeypatch,
            [_Response(_VERIFY_OK), _Response(_SETTLE_OK)],
            send_idempotency_key=True,
        )
        envelope = {
            "x402Version": 1,
            "scheme": "exact",
            "network": "base",
            "payload": _payload().payload,
        }
        header = base64.b64encode(json.dumps(envelope).encode("utf-8")).decode("ascii")
        client.process_payment(header, Decimal("0.01"), idempotency_scope="order-1")

        verify, settle = (call["headers"].get(IDEMPOTENCY_KEY_HEADER) for call in fake.calls)
        assert verify == derive_idempotency_key(_payload(), "verify", scope="order-1")
        assert settle == derive_idempotency_key(_payload(), "settle", scope="order-1")
