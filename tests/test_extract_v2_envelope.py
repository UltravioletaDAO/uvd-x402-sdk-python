"""The server must accept the v2 X-PAYMENT envelope its own challenge asks for.

Why this exists: ``create_402_response_v2`` announces ``x402Version: 2`` and
instructs the payer to echo the chosen accept back as ``accepted`` with NO
top-level ``network`` — but ``extract_payload`` parsed only the flat v1
``PaymentPayload`` model, whose ``network`` field is required. Every payment
built exactly as the challenge asked died with "network Field required": the
server rejected the format it had itself requested.

Measured with real money on 2026-08-12 (describe-net): Saul paid from Rabby
on Base, the wallet signed perfectly, and the payment only settled once the
paywall translated the envelope before handing it to this SDK (on-chain
receipt in describe-net's history — their first successful x402 payment
ever). Every server using this SDK and announcing v2 had the same bug.

The production vector below is byte-faithful to describe-net's ``pay.js``,
the only v2 producer observed in the wild: it echoes the accept object
verbatim (the spec requires the chosen object back unchanged) and sends the
EIP-3009 block as top-level ``payload``. The signature must pass through
untouched — the whole point of the translation is that only the wrapper
changes.

Where this improves on the downstream shim it replaces: unknown networks
raise a clear InvalidPayloadError instead of reproducing the old cryptic
failure, and CAIP-2 resolution covers the non-EVM namespaces (solana, near,
stellar) the shim's ``^eip155:(\\d+)$`` regex silently let through broken.
"""
from __future__ import annotations

import base64
import json

import pytest

from uvd_x402_sdk import X402Client
from uvd_x402_sdk.exceptions import InvalidPayloadError


def _client() -> X402Client:
    return X402Client(recipient_address="0x" + "11" * 20)


def _header(data: dict) -> str:
    return base64.b64encode(json.dumps(data).encode()).decode()


# Byte-faithful to describe-net's site/pay.js:181-188 — the accept object is
# echoed verbatim and the EIP-3009 block rides as top-level `payload`.
def _pay_js_envelope(network: str = "eip155:8453") -> dict:
    return {
        "x402Version": 2,
        "accepted": {
            "scheme": "exact",
            "network": network,
            "asset": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
            "amount": "10000",
            "payTo": "0x" + "22" * 20,
            "maxTimeoutSeconds": 300,
        },
        "payload": {
            "signature": "0x" + "ab" * 65,
            "authorization": {
                "from": "0x" + "33" * 20,
                "to": "0x" + "22" * 20,
                "value": "10000",
                "validAfter": "0",
                "validBefore": "1893456000",
                "nonce": "0x" + "44" * 32,
            },
        },
    }


class TestV2EnvelopeIsAccepted:
    def test_the_production_vector_parses(self):
        envelope = _pay_js_envelope()
        payload = _client().extract_payload(_header(envelope))
        assert payload.network == "base"
        assert payload.scheme == "exact"
        assert payload.x402Version == 2

    def test_signature_and_authorization_pass_through_untouched(self):
        envelope = _pay_js_envelope()
        payload = _client().extract_payload(_header(envelope))
        assert payload.payload == envelope["payload"]

    def test_caip2_avalanche_resolves(self):
        payload = _client().extract_payload(_header(_pay_js_envelope("eip155:43114")))
        assert payload.network == "avalanche"

    def test_non_evm_caip2_resolves_too(self):
        # The shim's ^eip155:\d+$ regex silently dropped these.
        envelope = _pay_js_envelope("solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp")
        payload = _client().extract_payload(_header(envelope))
        assert payload.network == "solana"

    def test_plain_network_name_inside_accepted_is_kept(self):
        payload = _client().extract_payload(_header(_pay_js_envelope("base")))
        assert payload.network == "base"

    def test_sdk_outbound_nesting_variant_is_unwrapped(self):
        envelope = _pay_js_envelope()
        inner = envelope.pop("payload")
        envelope["paymentPayload"] = {"payload": inner}
        payload = _client().extract_payload(_header(envelope))
        assert payload.payload == inner

    def test_payment_payload_without_inner_payload_is_used_directly(self):
        envelope = _pay_js_envelope()
        inner = envelope.pop("payload")
        envelope["paymentPayload"] = inner
        payload = _client().extract_payload(_header(envelope))
        assert payload.payload == inner


class TestV1StaysByteIdentical:
    def test_flat_v1_header_parses_exactly_as_before(self):
        v1 = {
            "x402Version": 1,
            "scheme": "exact",
            "network": "base",
            "payload": {"signature": "0x" + "ab" * 65, "authorization": {"from": "0x0"}},
        }
        payload = _client().extract_payload(_header(v1))
        assert payload.network == "base"
        assert payload.scheme == "exact"
        assert payload.x402Version == 1
        assert payload.payload == v1["payload"]

    def test_hybrid_with_top_level_network_is_treated_as_v1(self):
        # Same rule the battle-tested shim used: a top-level `network` wins
        # and the envelope passes through untranslated.
        hybrid = {
            "x402Version": 2,
            "scheme": "exact",
            "network": "avalanche",
            "accepted": {"scheme": "exact", "network": "eip155:8453"},
            "payload": {"signature": "0xff"},
        }
        payload = _client().extract_payload(_header(hybrid))
        assert payload.network == "avalanche"


class TestV2EnvelopeFailsLoudly:
    def test_unknown_caip2_names_the_network(self):
        with pytest.raises(InvalidPayloadError, match="eip155:999999999"):
            _client().extract_payload(_header(_pay_js_envelope("eip155:999999999")))

    def test_missing_accepted_network_says_what_to_echo(self):
        envelope = _pay_js_envelope()
        del envelope["accepted"]["network"]
        with pytest.raises(InvalidPayloadError, match="accepted.network"):
            _client().extract_payload(_header(envelope))

    def test_null_accepted_is_a_clear_error(self):
        envelope = _pay_js_envelope()
        envelope["accepted"] = None
        with pytest.raises(InvalidPayloadError, match="accepted"):
            _client().extract_payload(_header(envelope))

    def test_missing_payload_is_a_clear_error(self):
        envelope = _pay_js_envelope()
        del envelope["payload"]
        with pytest.raises(InvalidPayloadError, match="payload"):
            _client().extract_payload(_header(envelope))
