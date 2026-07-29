"""
Contract tests for the x402 v2 request envelope.

The shape below is pinned to a body VERIFIED against the live facilitator on
2026-07-29: POSTing it to /verify returns ``contract_call_failed``, meaning it
deserialised and reached on-chain verification (the signature is fake, which is
why the chain call is what fails).

This module exists because the SDK previously could not express v2 AT ALL:
``verify_payment`` / ``settle_payment`` hardcode the v1 envelope. Two teams lost
a day to that — the SDK advertised v2 (CAIP-2 ids in accepts[]) while being
structurally unable to CALL the facilitator in v2, and the rejection was
"data did not match any variant of untagged enum", which names no field.
"""

from uvd_x402_sdk.envelope_v2 import (
    AcceptedRequirementsV2,
    ResourceInfoV2,
    build_settle_request_v2,
    build_verify_request_v2,
)

RESOURCE = ResourceInfoV2(
    url="https://irc.meshrelay.xyz/channel/alpha-test",
    description="Alpha test channel",
    mime_type="application/json",
)

ACCEPTED = AcceptedRequirementsV2(
    scheme="exact",
    network="eip155:8453",
    asset="0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
    amount="100000",
    pay_to="0xe4dc963c56979E0260fc146b87eE24F18220e545",
    max_timeout_seconds=300,
)

PAYLOAD = {
    "signature": "0xdeadbeef",
    "authorization": {
        "from": "0x7052cA449702e5ffafbE3dc63b74C7b7d8aF402B",
        "to": "0xe4dc963c56979E0260fc146b87eE24F18220e545",
        "value": "100000",
        "validAfter": "1761329327",
        "validBefore": "1961829987",
        "nonce": "0xa0c6b1edb9fed5b5cd99626dadf0e60b56013f94839d4fdcfa0117cce1f74485",
    },
}


class TestVerifyEnvelope:
    def setup_method(self):
        self.body = build_verify_request_v2(PAYLOAD, RESOURCE, ACCEPTED)

    def test_resource_and_accepted_are_at_the_top_level(self):
        assert self.body["resource"]["url"] == RESOURCE.url
        assert self.body["accepted"]["network"] == "eip155:8453"

    def test_there_is_no_paymentRequirements_key(self):
        """That key is the v1 envelope; emitting it makes v2 match no variant."""
        assert "paymentRequirements" not in self.body

    def test_payload_is_nested_and_passed_through_verbatim(self):
        assert self.body["paymentPayload"]["payload"] == PAYLOAD

    def test_resource_and_accepted_are_repeated_inside_paymentPayload(self):
        # Redundant on the wire, but PaymentPayloadV2 declares both, so omitting
        # either fails deserialization.
        assert self.body["paymentPayload"]["resource"] == self.body["resource"]
        assert self.body["paymentPayload"]["accepted"] == self.body["accepted"]

    def test_version_is_declared_at_every_level(self):
        assert self.body["x402Version"] == 2
        assert self.body["paymentPayload"]["x402Version"] == 2

    def test_field_names_go_out_camelCase(self):
        """The facilitator reads payTo / maxTimeoutSeconds / mimeType."""
        assert self.body["accepted"]["payTo"].startswith("0x")
        assert self.body["accepted"]["maxTimeoutSeconds"] == 300
        assert self.body["resource"]["mimeType"] == "application/json"

    def test_amount_not_maxAmountRequired(self):
        # v2 renamed it. Sending maxAmountRequired alone reproduces the same
        # unnamed "no variant matched" error.
        assert self.body["accepted"]["amount"] == "100000"
        assert "maxAmountRequired" not in self.body["accepted"]

    def test_network_stays_caip2_never_a_plain_name(self):
        # A plain name inside a v2 request fails exactly as a CAIP-2 id inside a
        # v1 request does. Each version wants its own format.
        assert ":" in self.body["accepted"]["network"]

    def test_optional_extra_is_omitted_not_null(self):
        assert "extra" not in self.body["accepted"]


class TestSettleEnvelope:
    def test_settle_matches_verify(self):
        assert build_settle_request_v2(PAYLOAD, RESOURCE, ACCEPTED) == build_verify_request_v2(
            PAYLOAD, RESOURCE, ACCEPTED
        )


class TestPlainDictsAccepted:
    """Callers echoing a vendor accept verbatim have dicts, not models."""

    def test_dicts_work_and_produce_the_same_wire_form(self):
        from_models = build_verify_request_v2(PAYLOAD, RESOURCE, ACCEPTED)
        from_dicts = build_verify_request_v2(
            PAYLOAD,
            {
                "url": RESOURCE.url,
                "description": RESOURCE.description,
                "mimeType": RESOURCE.mime_type,
            },
            {
                "scheme": "exact",
                "network": "eip155:8453",
                "asset": ACCEPTED.asset,
                "amount": "100000",
                "payTo": ACCEPTED.pay_to,
                "maxTimeoutSeconds": 300,
            },
        )
        assert from_dicts == from_models

    def test_extra_vendor_fields_survive(self):
        """The spec says echo the accept verbatim; extras are tolerated."""
        body = build_verify_request_v2(
            PAYLOAD,
            RESOURCE,
            {
                "scheme": "exact",
                "network": "eip155:8453",
                "asset": ACCEPTED.asset,
                "amount": "100000",
                "payTo": ACCEPTED.pay_to,
                "maxTimeoutSeconds": 300,
                "description": "vendor extra",
            },
        )
        assert body["accepted"]["description"] == "vendor extra"


class TestNoNameCollision:
    """The v2 envelope model must NOT shadow the pre-existing 402-response model.

    `models.PaymentRequirementsV2` already existed and means something entirely
    different: the 402 RESPONSE, carrying an `accepts` array. Naming the single
    chosen accept the same thing would have silently rebound
    `from uvd_x402_sdk import PaymentRequirementsV2` to a different class for
    every existing user — a break with no error message, caught here only
    because the export list was read before publishing.
    """

    def test_PaymentRequirementsV2_still_resolves_to_the_402_response_model(self):
        from uvd_x402_sdk import PaymentRequirementsV2

        assert hasattr(PaymentRequirementsV2, "get_option_for_network")
        assert "accepts" in PaymentRequirementsV2.model_fields

    def test_the_envelope_model_is_exported_under_its_own_name(self):
        from uvd_x402_sdk import AcceptedRequirementsV2

        assert "amount" in AcceptedRequirementsV2.model_fields
        assert "accepts" not in AcceptedRequirementsV2.model_fields
