"""The ``Idempotency-Key``: how it is made, and the header on the wire.

What the facilitator does with the key is exercised in
``tests/test_idempotency_local_facilitator.py`` and
``tests/test_receipt_rail_binding.py``; why a key is made the way it is, in the
docstrings of :func:`uvd_x402_sdk.client.new_idempotency_key` and
:func:`uvd_x402_sdk.client.derive_idempotency_key`.
"""
from __future__ import annotations

import base64
import json
import re
from decimal import Decimal

import httpx
import pytest

from uvd_x402_sdk import (
    IDEMPOTENCY_KEY_HEADER,
    X402Client,
    derive_idempotency_key,
    new_idempotency_key,
    payment_response_headers,
)
from uvd_x402_sdk.models import PaymentPayload

#: The shape of a key the SDK made itself, the one ``createIdempotencyKey()``
#: makes in the TypeScript SDK.
RANDOM_KEY = re.compile(r"x402-[0-9a-f]{64}")

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


def _header() -> str:
    """The X-PAYMENT of ``_payload()``, as a buyer presents it."""
    envelope = {
        "x402Version": 1,
        "scheme": "exact",
        "network": "base",
        "payload": _payload().payload,
    }
    return base64.b64encode(json.dumps(envelope).encode("utf-8")).decode("ascii")


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

    def test_verify_and_settle_derive_one_key(self):
        """One key per payment since 0.89.0: the receipt rail binds only the
        key that admitted the payment, on /verify as on /settle. The value is
        the settle key 0.83.0 to 0.88.0 derived, so a purchase settled before
        an upgrade keeps its key."""
        verify = derive_idempotency_key(_payload(), "verify")
        settle = derive_idempotency_key(_payload(), "settle")
        assert verify == settle
        assert settle == (
            "x402-settle-6b62041bcbd7d49d05741f8bcd646f73ab239a76582b119c49e9679dc1cf6c1d"
        )

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

    def test_verify_and_settle_derive_one_key_under_a_scope(self):
        assert derive_idempotency_key(_payload(), "verify", scope="order-1") == (
            derive_idempotency_key(_payload(), "settle", scope="order-1")
        )

    @pytest.mark.parametrize("scope", ["", "   "], ids=["empty", "blank"])
    def test_an_empty_scope_is_refused(self, scope):
        with pytest.raises(ValueError):
            derive_idempotency_key(_payload(), "settle", scope=scope)

    def test_a_scope_that_is_not_a_string_is_refused(self):
        with pytest.raises(TypeError):
            derive_idempotency_key(_payload(), "settle", scope=42)


class TestNewKey:
    def test_the_shape(self):
        assert RANDOM_KEY.fullmatch(new_idempotency_key())

    def test_every_key_is_new(self):
        assert len({new_idempotency_key() for _ in range(50)}) == 50

    def test_it_is_not_derived_from_the_payment(self):
        key = new_idempotency_key()
        assert key != derive_idempotency_key(_payload(), "settle")
        assert not key.startswith("x402-settle-")


class TestOnTheWire:
    def test_settle_sends_the_scoped_key(self, monkeypatch):
        client, fake = _wire(monkeypatch, [_Response(_SETTLE_OK)], send_idempotency_key=True)
        client.settle_payment(_payload(), Decimal("0.01"), idempotency_scope="order-1")

        headers = fake.calls[0]["headers"]
        assert headers[IDEMPOTENCY_KEY_HEADER] == derive_idempotency_key(
            _payload(), "settle", scope="order-1"
        )
        assert headers["Content-Type"] == "application/json"

    def test_verify_sends_the_same_scoped_key_as_settle(self, monkeypatch):
        client, fake = _wire(monkeypatch, [_Response(_VERIFY_OK)], send_idempotency_key=True)
        client.verify_payment(_payload(), Decimal("0.01"), idempotency_scope="order-1")

        assert fake.calls[0]["headers"][IDEMPOTENCY_KEY_HEADER] == derive_idempotency_key(
            _payload(), "settle", scope="order-1"
        )

    def test_by_default_a_settle_sends_a_fresh_random_key(self, monkeypatch):
        client, fake = _wire(monkeypatch, [_Response(_SETTLE_OK), _Response(_SETTLE_OK)])
        assert client.config.send_idempotency_key is True
        first = client.settle_payment(_payload(), Decimal("0.01"))
        again = client.settle_payment(_payload(), Decimal("0.01"))

        sent = [call["headers"][IDEMPOTENCY_KEY_HEADER] for call in fake.calls]
        assert all(RANDOM_KEY.fullmatch(key) for key in sent)
        assert sent[0] != sent[1]
        assert [first.idempotency_key, again.idempotency_key] == sent

    def test_two_handlings_of_the_same_x_payment_send_different_keys(self, monkeypatch):
        client, fake = _wire(
            monkeypatch,
            [_Response(_VERIFY_OK), _Response(_SETTLE_OK)] * 2,
        )
        header = _header()
        first = client.process_payment(header, Decimal("0.01"))
        again = client.process_payment(header, Decimal("0.01"))

        keys = [call["headers"][IDEMPOTENCY_KEY_HEADER] for call in fake.calls]
        assert keys[0] == keys[1] and keys[2] == keys[3]
        assert keys[0] != keys[2]
        assert first.idempotency_key == keys[0] and again.idempotency_key == keys[2]

    def test_a_key_the_caller_brings_goes_out_verbatim_on_verify_and_settle(self, monkeypatch):
        client, fake = _wire(monkeypatch, [_Response(_VERIFY_OK), _Response(_SETTLE_OK)])
        key = new_idempotency_key()
        result = client.process_payment(_header(), Decimal("0.01"), idempotency_key=key)

        assert [call["headers"][IDEMPOTENCY_KEY_HEADER] for call in fake.calls] == [key, key]
        assert result.idempotency_key == key

    def test_the_key_never_reaches_the_buyer(self, monkeypatch):
        """Merchant-private: out of ``model_dump()``, so out of PAYMENT-RESPONSE."""
        client, fake = _wire(monkeypatch, [_Response(_VERIFY_OK), _Response(_SETTLE_OK)])
        result = client.process_payment(_header(), Decimal("0.01"))

        assert result.idempotency_key
        assert "idempotency_key" not in result.model_dump()
        encoded = payment_response_headers(result)["PAYMENT-RESPONSE"]
        assert result.idempotency_key not in base64.b64decode(encoded).decode()

    @pytest.mark.parametrize(
        "key, error",
        [
            ("receipt:mine", ValueError),
            ("", ValueError),
            ("with space", ValueError),
            ("x" * 256, ValueError),
            ("clé", ValueError),
            (42, TypeError),
        ],
        ids=["reserved", "empty", "space", "too-long", "non-ascii", "not-a-string"],
    )
    def test_a_key_the_facilitator_cannot_carry_is_refused_before_anything_is_sent(
        self, monkeypatch, key, error
    ):
        client, fake = _wire(monkeypatch, [_Response(_SETTLE_OK)])
        with pytest.raises(error):
            client.settle_payment(_payload(), Decimal("0.01"), idempotency_key=key)
        assert fake.calls == []

    def test_a_key_and_a_scope_at_once_are_refused(self, monkeypatch):
        client, fake = _wire(monkeypatch, [_Response(_SETTLE_OK)])
        with pytest.raises(ValueError):
            client.settle_payment(
                _payload(), Decimal("0.01"),
                idempotency_key=new_idempotency_key(), idempotency_scope="order-1",
            )
        assert fake.calls == []

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
        """No purchase named: the handling gets a fresh key, never one derived
        from the payment alone."""
        client, fake = _wire(monkeypatch, [_Response(_SETTLE_OK)], send_idempotency_key=True)
        client.settle_payment(_payload(), Decimal("0.01"), idempotency_scope=scope)

        assert RANDOM_KEY.fullmatch(fake.calls[0]["headers"][IDEMPOTENCY_KEY_HEADER])

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

    def test_the_timeout_fallback_resends_the_fresh_key_of_its_own_handling(self, monkeypatch):
        client, fake = _wire(
            monkeypatch, [httpx.TimeoutException("too slow"), _Response(_SETTLE_OK)]
        )
        settled = client.settle_payment(_payload(), Decimal("0.01"))

        first, fallback = (call["headers"][IDEMPOTENCY_KEY_HEADER] for call in fake.calls)
        assert RANDOM_KEY.fullmatch(first)
        assert fallback == first == settled.idempotency_key

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

    def test_a_settle_with_retry_sends_one_fresh_key_on_every_attempt(self, monkeypatch):
        monkeypatch.setattr("uvd_x402_sdk.client.time.sleep", lambda seconds: None)
        client, fake = _wire(
            monkeypatch,
            [
                _Response({"error": "upstream unavailable"}, status_code=502),
                _Response(_SETTLE_OK),
            ],
        )
        client.settle_payment(_payload(), Decimal("0.01"), retry=True)

        first, second = (call["headers"][IDEMPOTENCY_KEY_HEADER] for call in fake.calls)
        assert RANDOM_KEY.fullmatch(first) and second == first

    def test_process_payment_sends_the_scoped_key_on_verify_and_on_settle(self, monkeypatch):
        client, fake = _wire(
            monkeypatch,
            [_Response(_VERIFY_OK), _Response(_SETTLE_OK)],
            send_idempotency_key=True,
        )
        client.process_payment(_header(), Decimal("0.01"), idempotency_scope="order-1")

        verify, settle = (call["headers"].get(IDEMPOTENCY_KEY_HEADER) for call in fake.calls)
        assert verify == settle == derive_idempotency_key(_payload(), "settle", scope="order-1")
