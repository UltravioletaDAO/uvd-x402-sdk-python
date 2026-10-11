"""``X402Config(discoverable=True)``: the seller opts in to the facilitator's
settlement auto-registration.

The facilitator registers a resource after a successful ``/settle`` when the
settled requirements carry ``extra.discoverable == true`` as a JSON boolean
(x402-rs ``src/handlers.rs``, "Phase 2: Settlement Tracking":
``payment_requirements.extra.get("discoverable").and_then(as_bool)``). That is
NOT ``extensions.bazaar.discoverable`` (``bazaar_extension``), which only goes
into the 402 and ``/discovery/register``.

Pinned here:

1. Off by default: no ``discoverable`` key anywhere (402, ``/verify``, ``/settle``).
2. On: ``true`` as a bool in every v2 402 option's ``extra`` and in the
   requirements of BOTH calls of ``process_payment`` (verify and settle), in the
   v1 and the v2 envelope, with the EIP-712 ``name`` / ``version`` and the
   caller's other ``extra`` entries kept.
3. Native Hedera is left alone: its ``extra`` must be exactly ``{"feePayer"}``.
4. A value that is not a bool is refused before any request is sent.

The facilitator is an ``httpx.MockTransport`` on ``http://facilitator.invalid``.
"""
from __future__ import annotations

import base64
import json
from decimal import Decimal
from typing import Any

import httpx
import pytest

from uvd_x402_sdk import X402Client
from uvd_x402_sdk.config import X402Config
from uvd_x402_sdk.models import PaymentPayload
from uvd_x402_sdk.networks import get_network
from uvd_x402_sdk.response import Payment402BuilderV2, create_402_response_v2

FACILITATOR = "http://facilitator.invalid"
RECIPIENT = "0x2222222222222222222222222222222222222222"
PAYER = "0x774A351d1AA8cd221a1B87da639EFcc5A56cd9ce"
BASE = get_network("base")
DOMAIN = {"name": BASE.usdc_domain_name, "version": BASE.usdc_domain_version}
SETTLED = {
    "success": True,
    "payer": PAYER,
    "transaction": "0x" + "5e" * 32,
    "network": "base",
}


class Facilitator:
    """Answers ``/verify`` valid and ``/settle`` success; keeps every request."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path.endswith("/verify"):
            return httpx.Response(200, json={"isValid": True, "payer": PAYER})
        if request.url.path.endswith("/settle"):
            return httpx.Response(200, json=SETTLED)
        return httpx.Response(404, json={"error": "not here"})

    def client(self, **config: Any) -> X402Client:
        return X402Client(
            recipient_address=RECIPIENT,
            facilitator_url=FACILITATOR,
            http_client=httpx.Client(transport=httpx.MockTransport(self.handler)),
            **config,
        )

    def bodies(self, path: str) -> list[dict[str, Any]]:
        return [json.loads(r.content) for r in self.requests if r.url.path.endswith(path)]


def payload(network: str = "base") -> dict[str, Any]:
    return {
        "x402Version": 2 if ":" in network else 1,
        "scheme": "exact",
        "network": network,
        "payload": {
            "signature": "0x" + "11" * 65,
            "authorization": {
                "from": PAYER,
                "to": RECIPIENT,
                "value": "10000",
                "validAfter": "0",
                "validBefore": "9999999999",
                "nonce": "0x" + "42" * 32,
            },
        },
    }


def header(network: str = "base") -> str:
    return base64.b64encode(json.dumps(payload(network)).encode()).decode()


def requirements_extras(body: dict[str, Any]) -> list[Any]:
    """Every copy of the requirements' ``extra`` the facilitator may read."""
    if "paymentRequirements" in body:  # v1 envelope
        return [body["paymentRequirements"].get("extra")]
    return [body["accepted"].get("extra"), body["paymentPayload"]["accepted"].get("extra")]


def config(**kwargs: Any) -> X402Config:
    return X402Config(recipient_evm=RECIPIENT, recipient_solana="So1" + "1" * 40, **kwargs)


# --- 402 ---------------------------------------------------------------------


class TestThe402:
    def test_off_by_default_no_option_carries_the_key(self):
        body = create_402_response_v2(Decimal("0.01"), config(), resource="/r")
        assert body["accepts"]
        for option in body["accepts"]:
            assert "discoverable" not in (option.get("extra") or {})
        evm = [o for o in body["accepts"] if o["network"] == "eip155:8453"]
        assert evm[0]["extra"] == DOMAIN

    def test_false_is_the_same_body_as_omitted(self):
        omitted = create_402_response_v2(Decimal("0.01"), config(), resource="/r")
        off = create_402_response_v2(Decimal("0.01"), config(discoverable=False), resource="/r")
        assert json.dumps(off) == json.dumps(omitted)

    def test_on_every_option_carries_a_json_true_and_keeps_the_domain(self):
        body = create_402_response_v2(
            Decimal("0.01"), config(discoverable=True), resource="/r",
            networks=["base", "polygon", "solana"],
        )
        networks = {o["network"]: o for o in body["accepts"]}
        assert set(networks) == {"eip155:8453", "eip155:137", "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp"}
        for option in body["accepts"]:
            assert option["extra"]["discoverable"] is True
        assert networks["eip155:8453"]["extra"] == {**DOMAIN, "discoverable": True}
        polygon = get_network("polygon")
        assert networks["eip155:137"]["extra"] == {
            "name": polygon.usdc_domain_name,
            "version": polygon.usdc_domain_version,
            "discoverable": True,
        }
        assert '"discoverable":true' in json.dumps(body, separators=(",", ":"))

    def test_the_v2_builder_reads_it_from_the_config(self):
        body = Payment402BuilderV2(config(discoverable=True)).networks(["base"]).build()
        assert body["accepts"][0]["extra"] == {**DOMAIN, "discoverable": True}

    def test_extensions_are_untouched(self):
        ext = {"bazaar": {"discoverable": False}}
        body = create_402_response_v2(
            Decimal("0.01"), config(discoverable=True), resource="/r",
            networks=["base"], extensions=ext,
        )
        assert body["extensions"] == {"bazaar": {"discoverable": False}}
        assert body["accepts"][0]["extra"]["discoverable"] is True

    def test_native_hedera_keeps_an_extra_of_only_fee_payer(self):
        pytest.importorskip("hiero_sdk_python")
        cfg = X402Config(
            recipient_evm=RECIPIENT,
            network_configs={},
            recipient_hedera="0.0.1234",
            discoverable=True,
        )
        body = create_402_response_v2(
            Decimal("0.01"), cfg, resource="/r", networks=["hedera:testnet"]
        )
        for option in body["accepts"]:
            assert set(option["extra"]) == {"feePayer"}


# --- verify + settle ---------------------------------------------------------


class TestTheRequirementsSentToTheFacilitator:
    @pytest.mark.parametrize("network", ["base", "eip155:8453"])
    def test_on_both_calls_carry_a_json_true_beside_the_domain(self, network):
        fac = Facilitator()
        client = fac.client(discoverable=True)
        if ":" in network:
            p = PaymentPayload.model_validate(payload(network))
            assert client.verify_payment(p, Decimal("0.01")).isValid
            assert client.settle_payment(p, Decimal("0.01")).success
        else:
            result = client.process_payment(header(network), Decimal("0.01"))
            assert result.success
        verify, settle = fac.bodies("/verify"), fac.bodies("/settle")
        assert len(verify) == 1 and len(settle) == 1
        for body in verify + settle:
            for extra in requirements_extras(body):
                assert extra == {**DOMAIN, "discoverable": True}
                assert extra["discoverable"] is True
        raw_settle = [r.content for r in fac.requests if r.url.path.endswith("/settle")][0]
        assert b'"discoverable":true' in raw_settle.replace(b" ", b"")

    def test_process_payment_uses_the_v1_envelope_and_carries_it_on_both(self):
        fac = Facilitator()
        fac.client(discoverable=True).process_payment(header("base"), Decimal("0.01"))
        for body in fac.bodies("/verify") + fac.bodies("/settle"):
            assert body["paymentRequirements"]["extra"]["discoverable"] is True

    @pytest.mark.parametrize("flag", [{}, {"discoverable": False}])
    def test_off_or_omitted_the_requirements_do_not_announce_it(self, flag):
        fac = Facilitator()
        fac.client(**flag).process_payment(header("base"), Decimal("0.01"))
        bodies = fac.bodies("/verify") + fac.bodies("/settle")
        assert len(bodies) == 2
        for body in bodies:
            for extra in requirements_extras(body):
                assert extra == DOMAIN
        assert all(b"discoverable" not in r.content for r in fac.requests)

    def test_the_callers_other_extra_entries_stay(self):
        fac = Facilitator()
        client = fac.client(discoverable=True)
        p = PaymentPayload.model_validate(payload("base"))
        client.settle_payment(p, Decimal("0.01"), extra={"8004-reputation": {"includeProof": True}})
        (extra,) = requirements_extras(fac.bodies("/settle")[0])
        assert extra == {**DOMAIN, "discoverable": True, "8004-reputation": {"includeProof": True}}

    def test_the_eip712_override_is_kept_too(self):
        fac = Facilitator()
        client = fac.client(discoverable=True)
        p = PaymentPayload.model_validate(payload("base"))
        client.settle_payment(p, Decimal("0.01"), eip712_domain={"name": "Token", "version": "1"},
                              asset="0x" + "ab" * 20, token_decimals=6)
        (extra,) = requirements_extras(fac.bodies("/settle")[0])
        assert extra == {"name": "Token", "version": "1", "discoverable": True}

    def test_non_evm_requirements_get_only_the_flag(self):
        cfg = config(discoverable=True)
        client = X402Client(config=cfg)
        p = PaymentPayload.model_validate({**payload("solana"), "payload": {"transaction": "AA=="}})
        requirements = client._build_payment_requirements(p, Decimal("0.01"))
        assert requirements.extra == {"discoverable": True}

    def test_a_caller_true_with_the_config_on_is_accepted(self):
        fac = Facilitator()
        p = PaymentPayload.model_validate(payload("base"))
        fac.client(discoverable=True).settle_payment(p, Decimal("0.01"), extra={"discoverable": True})
        (extra,) = requirements_extras(fac.bodies("/settle")[0])
        assert extra["discoverable"] is True

    def test_a_caller_false_with_the_config_on_is_refused_before_io(self):
        fac = Facilitator()
        p = PaymentPayload.model_validate(payload("base"))
        with pytest.raises(ValueError, match="discoverable"):
            fac.client(discoverable=True).settle_payment(
                p, Decimal("0.01"), extra={"discoverable": False}
            )
        assert fac.requests == []


# --- non-bool inputs ---------------------------------------------------------


NOT_BOOLS = ["true", "True", "", " true ", 1, 0, 1.0, None, [], {}, b"true"]


class TestNonBoolInputsAreRefusedBeforeIO:
    @pytest.mark.parametrize("value", NOT_BOOLS, ids=repr)
    def test_the_config_refuses_it(self, value):
        with pytest.raises(ValueError, match="discoverable must be a bool"):
            config(discoverable=value)

    # X402Client drops kwargs that are None (= not passed), so None is the default there.
    @pytest.mark.parametrize("value", [v for v in NOT_BOOLS if v is not None], ids=repr)
    def test_the_client_kwargs_refuse_it_and_nothing_is_sent(self, value):
        fac = Facilitator()
        with pytest.raises(ValueError, match="discoverable must be a bool"):
            fac.client(discoverable=value)
        assert fac.requests == []

    @pytest.mark.parametrize("on", [False, True])
    @pytest.mark.parametrize("value", NOT_BOOLS, ids=repr)
    def test_a_caller_extra_that_is_not_a_bool_is_refused_and_nothing_is_sent(self, value, on):
        fac = Facilitator()
        p = PaymentPayload.model_validate(payload("base"))
        with pytest.raises(ValueError, match="discoverable must be a bool"):
            fac.client(discoverable=on).settle_payment(p, Decimal("0.01"), extra={"discoverable": value})
        assert fac.requests == []

    def test_a_value_flipped_after_construction_is_refused_before_io(self):
        fac = Facilitator()
        client = fac.client(discoverable=True)
        client.config.discoverable = "true"  # type: ignore[assignment]
        with pytest.raises(ValueError, match="discoverable must be a bool"):
            client.process_payment(header("base"), Decimal("0.01"))
        assert fac.requests == []
        with pytest.raises(ValueError, match="discoverable must be a bool"):
            create_402_response_v2(Decimal("0.01"), client.config, resource="/r")
