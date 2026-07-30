"""create_authorization can emit the x402 **v2** envelope.

The facilitator advertises 75 of its 114 kinds in v2, and real sellers already require it — but
the client could only ever produce v1. Any caller talking to a v2 resource server had to
hand-build the payload outside the SDK.

Every requirement asserted here was learned the expensive way, against live sellers:

  · `accepted` is echoed VERBATIM. Rebuilding it makes the seller reject a payment that was
    otherwise fine.
  · `resource` is an OBJECT. A bare string matches no variant of the facilitator's
    VerifyRequestEnvelope and fails with the opaque "data did not match any variant".
  · `extensions` MUST be echoed when the server declares them. A strict server rejects the
    payment by RE-SERVING the 402 with no hint — indistinguishable from "you sent nothing".
"""
from __future__ import annotations

import base64
import json

import pytest

from uvd_x402_sdk import X402Client

TEST_KEY = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
TEST_ADDR = "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266"
RECIPIENT = "0x1234567890123456789012345678901234567890"


@pytest.fixture
def client():
    c = X402Client(recipient_address=RECIPIENT)
    c.connect_with_private_key(TEST_KEY, chain_name="base")
    return c


def _accept():
    """An accept object shaped like a real v2 402 — including fields we must NOT drop."""
    return {
        "scheme": "exact",
        "network": "eip155:8453",
        "maxAmountRequired": "10000",
        "asset": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
        "payTo": "0x1111111111111111111111111111111111111111",
        "description": "a paid resource",
        "mimeType": "application/json",
        "maxTimeoutSeconds": 300,
        "extra": {"name": "USDC", "version": "2"},
    }


def _payload(header: str) -> dict:
    return json.loads(base64.b64decode(header))


def _args():
    return {"pay_to": "0x1111111111111111111111111111111111111111",
            "amount_usd": "0.01", "chain_name": "base"}


# ── NO REGRESSION: v1 is the default and unchanged ──────────────────────────
def test_v1_is_still_the_default(client):
    """The whole point: an existing caller that never heard of v2 keeps working."""
    p = _payload(client.create_authorization(**_args()))
    assert p["x402Version"] == 1
    assert p["scheme"] == "exact"
    assert "network" in p          # v1 carries them at the top level
    assert "accepted" not in p


def test_v1_shape_is_byte_identical_to_before(client):
    """Pins the exact v1 envelope. If a future refactor moves a field, this fails here rather
    than at a seller's 402."""
    p = _payload(client.create_authorization(**_args()))
    assert set(p) == {"x402Version", "scheme", "network", "payload"}
    assert set(p["payload"]) == {"signature", "authorization"}
    assert set(p["payload"]["authorization"]) == {
        "from", "to", "value", "validAfter", "validBefore", "nonce"}


# ── v2 ──────────────────────────────────────────────────────────────────────
def test_v2_echoes_the_accept_verbatim(client):
    """VERBATIM: every field the server sent comes back, including ones the SDK ignores.
    Dropping `extra` or `maxTimeoutSeconds` is how a good payment gets refused."""
    a = _accept()
    p = _payload(client.create_authorization(**_args(), x402_version=2, accepted=a))
    assert p["x402Version"] == 2
    assert p["accepted"] == a
    # v2 does NOT carry scheme/network at the top level — they live inside `accepted`
    assert "scheme" not in p and "network" not in p


def test_v2_without_accepted_refuses_loudly(client):
    """Failing here is cheap. Failing at the seller costs a transmitted authorization, which
    is bearer money and cannot be retried."""
    with pytest.raises(ValueError, match="accepted"):
        client.create_authorization(**_args(), x402_version=2)


def test_v2_turns_a_string_resource_into_the_object_the_facilitator_wants(client):
    """A bare URL matches NO variant of the VerifyRequestEnvelope. The fields come from the
    accept, which is what declares them."""
    p = _payload(client.create_authorization(
        **_args(), x402_version=2, accepted=_accept(), resource="https://seller.example/data"))
    r = p["resource"]
    assert isinstance(r, dict)
    assert r == {"url": "https://seller.example/data",
                 "description": "a paid resource",
                 "mimeType": "application/json"}


def test_v2_passes_a_wellformed_resource_object_through_untouched(client):
    """Normalising something that already arrived correct is how a working case breaks."""
    r = {"url": "https://seller.example/data", "description": "custom", "mimeType": "text/csv",
         "extraField": "kept"}
    p = _payload(client.create_authorization(
        **_args(), x402_version=2, accepted=_accept(), resource=r))
    assert p["resource"] == r


def test_v2_falls_back_when_the_accept_declares_no_description(client):
    a = _accept()
    del a["description"]
    del a["mimeType"]
    p = _payload(client.create_authorization(
        **_args(), x402_version=2, accepted=a, resource="https://x/y"))
    assert p["resource"]["description"] == ""
    assert p["resource"]["mimeType"] == "application/json"


def test_v2_echoes_extensions_when_the_server_declares_them(client):
    """Mandatory per spec §5. A strict server rejects a missing echo by re-serving the 402
    with no hint, which reads exactly like 'you sent no payment'."""
    ext = {"someExtension": {"required": True, "data": "abc"}}
    p = _payload(client.create_authorization(
        **_args(), x402_version=2, accepted=_accept(), extensions=ext))
    assert p["extensions"] == ext


def test_v2_omits_optional_fields_when_absent(client):
    """An empty `resource`/`extensions` must not appear at all: some servers validate the
    envelope strictly and a null is not the same as absent."""
    p = _payload(client.create_authorization(**_args(), x402_version=2, accepted=_accept()))
    assert "resource" not in p and "extensions" not in p


def test_the_authorization_and_signature_are_identical_across_versions(client):
    """The version changes the ENVELOPE, never the signed message. If v2 signed something
    different, the same payment would settle on one version and not the other."""
    fijo = dict(**_args())
    v1 = _payload(client.create_authorization(**fijo))
    v2 = _payload(client.create_authorization(**fijo, x402_version=2, accepted=_accept()))
    for k in ("from", "to", "value"):
        assert v1["payload"]["authorization"][k] == v2["payload"]["authorization"][k]
    assert len(v1["payload"]["signature"]) == len(v2["payload"]["signature"]) == 132


def test_v2_still_carries_the_token_block_for_non_usdc(client):
    """The non-USDC token block hangs off payload.payload, which both envelopes share."""
    p = _payload(client.create_authorization(
        pay_to="0x1111111111111111111111111111111111111111", amount_usd="0.01",
        chain_name="base", token_type="eurc", x402_version=2, accepted=_accept()))
    assert p["payload"]["token"]["symbol"] == "EURC"
