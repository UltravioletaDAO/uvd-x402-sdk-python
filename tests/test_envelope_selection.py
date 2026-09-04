"""The client CHOOSES the x402 envelope; it used to impose v1.

The defect these tests pin: ``X402Client.verify_payment`` and ``_settle_once``
wrote ``"x402Version": 1`` as a literal, so the SDK could advertise v2 in a 402
and then was structurally unable to speak it — the exact failure that broke a
real ChatGPT payment through the TypeScript SDK on 2026-09-02. The v2 builders
had lived in ``envelope_v2.py`` since 0.62.0 with nobody calling them.

Every shape asserted here was measured against
``https://facilitator.ultravioletadao.xyz`` (2.10.0) on 2026-09-04 with a
fabricated signature, so a 200 means "the facilitator read the body" and a 400
means "it could not". The four rows that decide the design:

  · v1 envelope + plain names                 -> 200   (must not change)
  · v1 envelope + plain names + marker ``2``  -> 200   (must not change)
  · v2 envelope + ``eip155:8453``             -> 200   (the new path)
  · v2 envelope + ``base``                    -> **400** (v2 cannot carry it)

Two of the tests below are NO-REGRESSION GUARDS and are green in both states:
they exist to prove v1 did not move, not to prove v2 arrived.
"""
from __future__ import annotations

import json
from decimal import Decimal

import pytest

from uvd_x402_sdk import X402Client, X402Config
from uvd_x402_sdk.envelope import (
    build_verify_request_for_version,
    resolve_envelope_version,
    to_accepted_requirements_v2,
    to_resource_info_v2,
)
from uvd_x402_sdk.models import PaymentPayload, PaymentRequirements

RECIPIENT = "0x1234567890123456789012345678901234567890"
USDC_BASE = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
SENDER = "0x7052cA44a3e8B3Ff9E6cEd05Fe2C1b0e5B9d402B"


# ── harness ──────────────────────────────────────────────────────────────────


class _FakeResponse:
    def __init__(self, status_code: int = 200, body: dict | None = None):
        self.status_code = status_code
        self._body = body if body is not None else {}
        self.text = json.dumps(self._body)
        self.headers: dict = {}

    def json(self) -> dict:
        return self._body


class _FakeHttpClient:
    """Records the JSON body of every POST and answers with a canned success."""

    def __init__(self, response: _FakeResponse):
        self.response = response
        self.bodies: list[dict] = []
        self.urls: list[str] = []

    def post(self, url, json=None, **kwargs):  # noqa: A002 - httpx's own kwarg name
        self.urls.append(url)
        self.bodies.append(json)
        return self.response


def _payload(network: str, marker: int = 1) -> PaymentPayload:
    return PaymentPayload(
        x402Version=marker,
        scheme="exact",
        network=network,
        payload={
            "signature": "0x" + "ab" * 65,
            "authorization": {
                "from": SENDER,
                "to": RECIPIENT,
                "value": "10000",
                "validAfter": "0",
                "validBefore": "9999999999",
                "nonce": "0x" + "00" * 31 + "01",
            },
        },
    )


def _client(monkeypatch, response: _FakeResponse, **config_kwargs):
    client = X402Client(
        config=X402Config(
            recipient_evm=RECIPIENT,
            resource_url="https://api.example.com/protected",
            description="One API call",
            **config_kwargs,
        )
    )
    fake = _FakeHttpClient(response)
    monkeypatch.setattr(client, "_get_http_client", lambda: fake)
    return client, fake


_VERIFY_OK = _FakeResponse(200, {"isValid": True, "payer": SENDER})
_SETTLE_OK = _FakeResponse(200, {"success": True, "transaction": "0xf00d", "payer": SENDER})


# ── the new path: CAIP-2 on the wire gets the v2 envelope ────────────────────


class TestUpgradesToV2:
    def test_verify_sends_the_v2_envelope_when_the_network_is_caip2(self, monkeypatch):
        client, fake = _client(monkeypatch, _VERIFY_OK)
        client.verify_payment(_payload("eip155:8453"), Decimal("0.01"))

        body = fake.bodies[0]
        assert body["x402Version"] == 2
        assert "paymentRequirements" not in body, "that key IS the v1 envelope"
        assert set(body) == {"x402Version", "paymentPayload", "resource", "accepted"}

    def test_settle_sends_the_v2_envelope_on_the_same_trigger(self, monkeypatch):
        client, fake = _client(monkeypatch, _SETTLE_OK)
        client.settle_payment(_payload("eip155:8453"), Decimal("0.01"))

        body = fake.bodies[0]
        assert body["x402Version"] == 2
        assert "paymentRequirements" not in body

    def test_the_v2_body_has_the_exact_shape_the_facilitator_accepts(self, monkeypatch):
        """Pinned against the live 200, key for key.

        Every one of these was a measured 400 when wrong: a `resource` missing
        one of its three keys, an `amount` still spelled `maxAmountRequired`, a
        plain network name, an absent `maxTimeoutSeconds`. None of them is named
        by the facilitator's error, which only says "matched no variant".
        """
        client, fake = _client(monkeypatch, _VERIFY_OK)
        client.verify_payment(_payload("eip155:8453"), Decimal("0.01"))
        body = fake.bodies[0]

        assert body["resource"] == {
            "url": "https://api.example.com/protected",
            "description": "One API call",
            "mimeType": "application/json",
        }
        assert body["accepted"] == {
            "scheme": "exact",
            "network": "eip155:8453",
            "asset": USDC_BASE,
            "amount": "10000",
            "payTo": RECIPIENT,
            "maxTimeoutSeconds": 60,
            "extra": {"name": "USD Coin", "version": "2"},
        }
        # The payer's signed material travels verbatim; reshaping it invalidates it.
        assert body["paymentPayload"]["payload"] == _payload("eip155:8453").payload

    def test_the_inner_resource_accepted_pair_is_repeated(self, monkeypatch):
        """Optional on facilitators from 2026-09-04, required before that.

        Sending it is what makes one body work against both generations, so its
        absence is a compatibility regression even while the live 200 tolerates it.
        """
        client, fake = _client(monkeypatch, _VERIFY_OK)
        client.verify_payment(_payload("eip155:8453"), Decimal("0.01"))
        inner = fake.bodies[0]["paymentPayload"]

        assert inner["x402Version"] == 2
        assert inner["resource"] == fake.bodies[0]["resource"]
        assert inner["accepted"] == fake.bodies[0]["accepted"]

    def test_the_eip712_domain_survives_the_conversion(self, monkeypatch):
        """`extra` carries the EIP-712 domain for tokens the facilitator does not
        know by address (EURC, the bridged USDCs). Dropping it makes them unpayable."""
        client, fake = _client(monkeypatch, _VERIFY_OK)
        client.verify_payment(
            _payload("eip155:8453"),
            Decimal("0.01"),
            asset="0x60a3E35Cc302bFA44Cb288Bc5a4F316Fdb1adb42",
            eip712_domain={"name": "EURC", "version": "2"},
        )
        assert fake.bodies[0]["accepted"]["extra"] == {"name": "EURC", "version": "2"}
        assert fake.bodies[0]["accepted"]["asset"] == (
            "0x60a3E35Cc302bFA44Cb288Bc5a4F316Fdb1adb42"
        )


# ── no-regression guards: green BEFORE and AFTER the change ──────────────────


class TestV1IsUntouched:
    def test_a_plain_network_still_gets_the_v1_envelope_byte_for_byte(self, monkeypatch):
        client, fake = _client(monkeypatch, _VERIFY_OK)
        payload = _payload("base")
        client.verify_payment(payload, Decimal("0.01"))

        assert fake.bodies[0] == {
            "x402Version": 1,
            "paymentPayload": payload.model_dump(by_alias=True),
            "paymentRequirements": {
                "scheme": "exact",
                "network": "base",
                "maxAmountRequired": "10000",
                "resource": "https://api.example.com/protected",
                "description": "One API call",
                "mimeType": "application/json",
                "payTo": RECIPIENT,
                "maxTimeoutSeconds": 60,
                "asset": USDC_BASE,
                "extra": {"name": "USD Coin", "version": "2"},
            },
        }

    def test_a_header_that_only_DECLARES_v2_is_not_upgraded(self, monkeypatch):
        """Measured: that body is a 200 in the v1 envelope and a **400** in v2.

        The facilitator's envelope enum is untagged — it matches on shape and
        ignores the version marker — so a header carrying `x402Version: 2` with
        plain network names is being served correctly today. Upgrading it on the
        strength of the marker would break a call that works.
        """
        client, fake = _client(monkeypatch, _VERIFY_OK)
        client.verify_payment(_payload("base", marker=2), Decimal("0.01"))

        assert fake.bodies[0]["x402Version"] == 1
        assert "paymentRequirements" in fake.bodies[0]

    def test_the_v1_envelope_version_is_the_envelope_not_the_header(self, monkeypatch):
        """`x402Version` at the top level names the BODY's shape. Echoing the
        payer's `2` there would declare a v2 body while sending a v1 one."""
        client, fake = _client(monkeypatch, _VERIFY_OK)
        client.verify_payment(_payload("base", marker=2), Decimal("0.01"))

        assert fake.bodies[0]["x402Version"] == 1
        assert fake.bodies[0]["paymentPayload"]["x402Version"] == 2


# ── the pin wins over the wire ───────────────────────────────────────────────


class TestExplicitPin:
    def test_pinning_1_keeps_v1_on_a_caip2_wire(self, monkeypatch):
        client, fake = _client(monkeypatch, _VERIFY_OK, x402_version=1)
        client.verify_payment(_payload("eip155:8453"), Decimal("0.01"))

        assert fake.bodies[0]["x402Version"] == 1
        assert fake.bodies[0]["paymentRequirements"]["network"] == "eip155:8453"

    def test_pinning_2_forces_v2_on_a_plain_wire(self, monkeypatch):
        """The plain name is converted, because a v2 body carrying `base` is a 400."""
        client, fake = _client(monkeypatch, _VERIFY_OK, x402_version=2)
        client.verify_payment(_payload("base"), Decimal("0.01"))

        assert fake.bodies[0]["x402Version"] == 2
        assert fake.bodies[0]["accepted"]["network"] == "eip155:8453"

    def test_pinning_2_on_a_network_with_no_caip2_form_fails_loudly(self, monkeypatch):
        """XRPL has no CAIP-2 form — its v1 string IS its identifier.

        Falling back to v1 silently would be defensible; falling back to a v1
        network name INSIDE a v2 body is the 400 this module exists to prevent,
        and a silent downgrade would hide a pin the caller asked for.
        """
        client, fake = _client(monkeypatch, _VERIFY_OK, x402_version=2)
        with pytest.raises(ValueError, match="no CAIP-2 form"):
            client.verify_payment(_payload("xrpl-mainnet"), Decimal("0.01"))
        assert fake.bodies == [], "must fail before the POST"

    def test_auto_leaves_xrpl_on_v1(self, monkeypatch):
        client, fake = _client(monkeypatch, _VERIFY_OK)
        client.verify_payment(_payload("xrpl-mainnet"), Decimal("0.01"))
        assert fake.bodies[0]["x402Version"] == 1

    def test_a_bogus_pin_is_rejected(self):
        with pytest.raises(ValueError, match="must be 1, 2 or 'auto'"):
            resolve_envelope_version(_payload("base"), _requirements("base"), 3)


# ── the helpers, on their own ────────────────────────────────────────────────


def _requirements(network: str, **over) -> PaymentRequirements:
    fields = {
        "scheme": "exact",
        "network": network,
        "maxAmountRequired": "10000",
        "resource": "https://api.example.com/protected",
        "description": "One API call",
        "mimeType": "application/json",
        "payTo": RECIPIENT,
        "maxTimeoutSeconds": 60,
        "asset": USDC_BASE,
    }
    fields.update(over)
    return PaymentRequirements(**fields)


class TestConversion:
    def test_caip2_in_EITHER_half_of_the_wire_selects_v2(self):
        """The client builds both halves from the same string, but a caller
        assembling requirements by hand can mix them — and the facilitator
        tolerates the mix, so the rule reads both."""
        assert resolve_envelope_version(_payload("eip155:8453"), _requirements("base")) == 2
        assert resolve_envelope_version(_payload("base"), _requirements("eip155:8453")) == 2
        assert resolve_envelope_version(_payload("base"), _requirements("base")) == 1

    def test_maxAmountRequired_becomes_amount(self):
        accepted = to_accepted_requirements_v2(_requirements("base"))
        assert accepted.amount == "10000"
        assert not hasattr(accepted, "maxAmountRequired")

    def test_the_resource_string_becomes_an_object_with_all_three_keys(self):
        resource = to_resource_info_v2(_requirements("base"))
        assert resource.model_dump(by_alias=True) == {
            "url": "https://api.example.com/protected",
            "description": "One API call",
            "mimeType": "application/json",
        }

    def test_an_already_caip2_network_is_passed_through_untouched(self):
        assert to_accepted_requirements_v2(_requirements("eip155:8453")).network == (
            "eip155:8453"
        )

    def test_extra_is_omitted_from_the_wire_when_absent(self):
        """`exclude_none` — an explicit `"extra": null` is not what was measured."""
        body = build_verify_request_for_version(
            _payload("eip155:8453"), _requirements("eip155:8453"), 2
        )
        assert "extra" not in body["accepted"]

    def test_the_v2_envelope_never_carries_paymentRequirements(self):
        body = build_verify_request_for_version(
            _payload("eip155:8453"), _requirements("eip155:8453"), 2
        )
        assert "paymentRequirements" not in body
        assert "paymentRequirements" not in body["paymentPayload"]


# ── the settle timeout fallback re-sends whatever was sent ───────────────────


class TestSettleFallbackReplaysTheSameEnvelope:
    def test_the_fallback_resends_the_v2_body_not_a_rebuilt_v1(self, monkeypatch):
        """The fallback asks "did my settle land?" — with a DIFFERENT body it is
        asking about a different payment, and a v1 rebuild of a v2 settle would
        get a confident "never saw it" about money that may already have moved."""
        import httpx

        client, fake = _client(monkeypatch, _SETTLE_OK)
        calls: list[dict] = []

        def _timeout_then_record(url, json=None, **kwargs):  # noqa: A002
            calls.append(json)
            if len(calls) == 1:
                raise httpx.TimeoutException("too slow")
            return _SETTLE_OK

        monkeypatch.setattr(fake, "post", _timeout_then_record)
        client.settle_payment(_payload("eip155:8453"), Decimal("0.01"))

        assert len(calls) == 2
        assert calls[0] == calls[1]
        assert calls[0]["x402Version"] == 2
