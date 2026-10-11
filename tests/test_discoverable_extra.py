"""``X402Config(discoverable=True)``: the seller opts in to the facilitator's
settlement auto-registration.

The facilitator registers a resource after a successful ``/settle`` when the
settled requirements carry ``extra.discoverable == true`` as a JSON boolean
(x402-rs ``src/handlers.rs``, "Phase 2: Settlement Tracking":
``payment_requirements.extra.get("discoverable").and_then(as_bool)``). That is
NOT ``extensions.bazaar.discoverable`` (``bazaar_extension``), which only goes
into the 402 and ``/discovery/register``.

Pinned here:

1. Not set by default: no ``discoverable`` key anywhere (402, ``/verify``, ``/settle``).
2. On: ``true`` as a bool in every v2 402 option's ``extra`` and in the
   requirements of BOTH calls of ``process_payment`` (verify and settle), in the
   v1 and the v2 envelope, with the EIP-712 ``name`` / ``version`` and the
   caller's other ``extra`` entries kept. ``False`` writes a JSON ``false`` in
   the same places, as the TypeScript SDK's ``discoverable: false``.
3. Native Hedera is left as it comes at config level (its ``extra`` must be
   exactly ``{"feePayer"}``, any spelling of the network); the per-accept
   ``with_discoverable`` raises on it, as ``withDiscoverable`` in TypeScript.
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

import uvd_x402_sdk
from uvd_x402_sdk import X402Client
from uvd_x402_sdk.config import X402Config
from uvd_x402_sdk.models import PaymentPayload
from uvd_x402_sdk.networks import get_network
from uvd_x402_sdk.response import (
    Payment402BuilderV2,
    create_402_response_v2,
    with_discoverable,
)

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

    def test_none_is_the_same_body_as_omitted(self):
        omitted = create_402_response_v2(Decimal("0.01"), config(), resource="/r")
        unset = create_402_response_v2(Decimal("0.01"), config(discoverable=None), resource="/r")
        assert json.dumps(unset) == json.dumps(omitted)

    def test_false_writes_a_json_false_where_true_goes(self):
        body = create_402_response_v2(
            Decimal("0.01"), config(discoverable=False), resource="/r",
            networks=["base", "solana"],
        )
        assert len(body["accepts"]) == 2
        for option in body["accepts"]:
            assert option["extra"]["discoverable"] is False
        assert body["accepts"][0]["extra"] == {**DOMAIN, "discoverable": False}
        assert '"discoverable":false' in json.dumps(body, separators=(",", ":"))

    def test_on_every_option_carries_a_json_true_and_keeps_the_domain(self):
        body = create_402_response_v2(
            Decimal("0.01"), config(discoverable=True), resource="/r",
            networks=["base", "polygon", "solana"],
        )
        networks = {o["network"]: o for o in body["accepts"]}
        solana = "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp"
        assert set(networks) == {"eip155:8453", "eip155:137", solana}
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

    @pytest.mark.parametrize("flag", [True, False])
    # Only the canonical id: create_402_response_v2 drops any other spelling of a
    # network from the offers (config.is_network_enabled), flag or not, as on base.
    @pytest.mark.parametrize("hedera", ["hedera:testnet"])
    def test_native_hedera_is_left_as_it_comes_and_the_rest_flagged(self, hedera, flag):
        pytest.importorskip("hiero_sdk_python")
        cfg = X402Config(
            recipient_evm=RECIPIENT,
            network_configs={},
            recipient_hedera="0.0.1234",
            discoverable=flag,
        )
        body = create_402_response_v2(
            Decimal("0.01"), cfg, resource="/r", networks=["base", hedera]
        )
        by_network = {o["network"]: o for o in body["accepts"]}
        assert set(by_network) == {"eip155:8453", "hedera:testnet"}
        assert set(by_network["hedera:testnet"]["extra"]) == {"feePayer"}
        assert by_network["eip155:8453"]["extra"] == {**DOMAIN, "discoverable": flag}


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

    def test_omitted_the_requirements_do_not_carry_it(self):
        fac = Facilitator()
        fac.client().process_payment(header("base"), Decimal("0.01"))
        bodies = fac.bodies("/verify") + fac.bodies("/settle")
        assert len(bodies) == 2
        for body in bodies:
            for extra in requirements_extras(body):
                assert extra == DOMAIN
        assert all(b"discoverable" not in r.content for r in fac.requests)

    def test_false_both_calls_carry_a_json_false(self):
        fac = Facilitator()
        fac.client(discoverable=False).process_payment(header("base"), Decimal("0.01"))
        bodies = fac.bodies("/verify") + fac.bodies("/settle")
        assert len(bodies) == 2
        for body in bodies:
            for extra in requirements_extras(body):
                assert extra == {**DOMAIN, "discoverable": False}
                assert extra["discoverable"] is False

    @pytest.mark.parametrize("flag", [True, False])
    def test_native_hedera_requirements_are_left_as_they_come(self, flag):
        hiero = pytest.importorskip("hiero_sdk_python")
        from uvd_x402_sdk.hedera import HederaSigner, build_hedera_requirements

        offer = build_hedera_requirements("hedera:testnet", "0.0.222", "1000")
        signer = HederaSigner(
            "0.0.111", hiero.PrivateKey.generate_ed25519().to_string_der(),
            network="hedera:testnet",
        )
        client = X402Client(config=X402Config(
            recipient_hedera="0.0.222", supported_networks=["hedera:testnet"],
            discoverable=flag,
        ))
        p = client.extract_payload(signer.create_payment_header(offer))
        requirements = client._build_payment_requirements(p, Decimal("0.001"))
        assert requirements.extra == offer["extra"]
        assert set(requirements.extra) == {"feePayer"}

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
        fac.client(discoverable=True).settle_payment(
            p, Decimal("0.01"), extra={"discoverable": True}
        )
        (extra,) = requirements_extras(fac.bodies("/settle")[0])
        assert extra["discoverable"] is True

    @pytest.mark.parametrize("on", [True, False])
    def test_a_caller_value_against_the_config_is_refused_before_io(self, on):
        fac = Facilitator()
        p = PaymentPayload.model_validate(payload("base"))
        with pytest.raises(ValueError, match="discoverable"):
            fac.client(discoverable=on).settle_payment(
                p, Decimal("0.01"), extra={"discoverable": not on}
            )
        assert fac.requests == []

    def test_with_the_config_unset_a_caller_value_goes_as_given(self):
        fac = Facilitator()
        p = PaymentPayload.model_validate(payload("base"))
        fac.client().settle_payment(p, Decimal("0.01"), extra={"discoverable": True})
        (extra,) = requirements_extras(fac.bodies("/settle")[0])
        assert extra == {**DOMAIN, "discoverable": True}


# --- non-bool inputs ---------------------------------------------------------


NOT_BOOLS = ["true", "True", "", " true ", 1, 0, 1.0, None, [], {}, b"true"]
# None is "not set" for the config (the default) and for X402Client's kwargs.
CONFIG_NOT_BOOLS = [v for v in NOT_BOOLS if v is not None]


class TestNonBoolInputsAreRefusedBeforeIO:
    @pytest.mark.parametrize("value", CONFIG_NOT_BOOLS, ids=repr)
    def test_the_config_refuses_it(self, value):
        with pytest.raises(ValueError, match="discoverable must be a bool"):
            config(discoverable=value)

    def test_none_is_not_set(self):
        assert config().discoverable is None
        assert config(discoverable=None).with_discoverable({"a": 1}) == {"a": 1}
        assert config(discoverable=None).with_discoverable(None) is None

    @pytest.mark.parametrize("value", CONFIG_NOT_BOOLS, ids=repr)
    def test_the_client_kwargs_refuse_it_and_nothing_is_sent(self, value):
        fac = Facilitator()
        with pytest.raises(ValueError, match="discoverable must be a bool"):
            fac.client(discoverable=value)
        assert fac.requests == []

    @pytest.mark.parametrize("on", [None, False, True])
    @pytest.mark.parametrize("value", NOT_BOOLS, ids=repr)
    def test_a_caller_extra_that_is_not_a_bool_is_refused_and_nothing_is_sent(self, value, on):
        fac = Facilitator()
        p = PaymentPayload.model_validate(payload("base"))
        with pytest.raises(ValueError, match="discoverable must be a bool"):
            fac.client(discoverable=on).settle_payment(
                p, Decimal("0.01"), extra={"discoverable": value}
            )
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


# --- with_discoverable (one accept) ------------------------------------------


def base_accept(**extra: Any) -> dict[str, Any]:
    accept: dict[str, Any] = {
        "scheme": "exact",
        "network": "eip155:8453",
        "asset": BASE.usdc_address,
        "amount": "10000",
        "payTo": RECIPIENT,
        "maxTimeoutSeconds": 60,
    }
    if extra:
        accept["extra"] = extra
    return accept


class TestWithDiscoverable:
    def test_defaults_to_true_and_keeps_the_other_extras(self):
        accept = base_accept(**DOMAIN, nested={"a": [1]})
        out = with_discoverable(accept)
        assert out["extra"] == {**DOMAIN, "nested": {"a": [1]}, "discoverable": True}
        assert {k: v for k, v in out.items() if k != "extra"} == {
            k: v for k, v in accept.items() if k != "extra"
        }

    def test_false_writes_a_json_false(self):
        out = with_discoverable(base_accept(**DOMAIN), False)
        assert out["extra"] == {**DOMAIN, "discoverable": False}
        assert '"discoverable":false' in json.dumps(out, separators=(",", ":"))

    def test_an_accept_without_extra_gets_one(self):
        assert with_discoverable(base_accept())["extra"] == {"discoverable": True}

    @pytest.mark.parametrize("flag", [True, False])
    def test_it_overrides_a_prior_value(self, flag):
        out = with_discoverable(base_accept(discoverable=not flag), flag)
        assert out["extra"]["discoverable"] is flag

    def test_the_accept_is_not_modified(self):
        accept = base_accept(**DOMAIN)
        before = json.dumps(accept, sort_keys=True)
        out = with_discoverable(accept)
        assert json.dumps(accept, sort_keys=True) == before
        assert out is not accept and out["extra"] is not accept["extra"]

    @pytest.mark.parametrize("value", NOT_BOOLS, ids=repr)
    def test_a_flag_that_is_not_a_bool_raises(self, value):
        with pytest.raises(ValueError, match="discoverable must be a bool"):
            with_discoverable(base_accept(), value)

    @pytest.mark.parametrize("accept", [None, [], "eip155:8453", 1], ids=repr)
    def test_something_that_is_not_an_accept_raises(self, accept):
        with pytest.raises(TypeError, match="an accept must be a dict"):
            with_discoverable(accept)

    @pytest.mark.parametrize("network", [None, 8453, b"base"], ids=repr)
    def test_an_accept_without_a_string_network_raises(self, network):
        with pytest.raises(TypeError, match="no network"):
            with_discoverable({**base_accept(), "network": network})

    @pytest.mark.parametrize("extra", [[], "x", 1, None], ids=repr)
    def test_an_extra_that_is_not_a_dict_raises(self, extra):
        accept = {**base_accept(), "extra": extra}
        if extra is None:  # an explicit null extra is "no extra"
            assert with_discoverable(accept)["extra"] == {"discoverable": True}
            return
        with pytest.raises(TypeError, match="extra must be a dict"):
            with_discoverable(accept)

    @pytest.mark.parametrize("flag", [True, False])
    @pytest.mark.parametrize("network", [
        "hedera:mainnet", "HEDERA:MAINNET", "Hedera:Testnet", "hedera:testnet",
        "HEDERA", "hedera", "hedera-testnet", "Hedera-Testnet",
    ])
    def test_a_native_hedera_accept_raises_in_any_case(self, network, flag):
        accept = {**base_accept(), "network": network, "extra": {"feePayer": "0.0.1"}}
        with pytest.raises(ValueError, match="native Hedera"):
            with_discoverable(accept, flag)

    @pytest.mark.parametrize("network", ["EIP155:8453", "Base", "BASE", "base"])
    def test_other_networks_in_any_case_are_flagged(self, network):
        out = with_discoverable({**base_accept(), "network": network})
        assert out["extra"]["discoverable"] is True

    def test_it_flags_an_option_of_create_402_response_v2(self):
        body = create_402_response_v2(Decimal("0.01"), config(), resource="/r", networks=["base"])
        (option,) = body["accepts"]
        assert with_discoverable(option)["extra"] == {**DOMAIN, "discoverable": True}
        assert "discoverable" not in option["extra"]

    def test_it_is_exported_from_the_package_root(self):
        assert uvd_x402_sdk.with_discoverable is with_discoverable
        assert "with_discoverable" in uvd_x402_sdk.__all__
