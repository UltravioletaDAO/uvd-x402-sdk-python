"""
Tests for XRPL (XRP Ledger) t54 payload model and parse helpers.

Mirrors the per-chain payload-model coverage for Stellar/NEAR/Sui:
- XRPLPayloadContent parses { "signedTxBlob": "..." }
- populate_by_name works (signed_tx_blob)
- PaymentPayload.get_xrpl_payload() returns the parsed content
- Network identifiers ("xrpl-mainnet"/"xrpl-testnet") and 6-decimal drops
"""

import pytest

from uvd_x402_sdk.models import (
    XRPLPayloadContent,
    PayloadContent,
    PaymentPayload,
)
from uvd_x402_sdk.networks import NetworkType, get_network
from uvd_x402_sdk.networks.xrpl import (
    drops_to_xrp,
    xrp_to_drops,
    is_valid_xrpl_address,
    get_xrpl_fee_payer,
)
from uvd_x402_sdk.facilitator import (
    XRPL_FEE_PAYER_MAINNET,
    XRPL_FEE_PAYER_TESTNET,
)


# ---------------------------------------------------------------------------
# Payload model
# ---------------------------------------------------------------------------


def test_xrpl_payload_content_parses_camelcase():
    """The wire field is exactly `signedTxBlob` (camelCase)."""
    content = XRPLPayloadContent.model_validate({"signedTxBlob": "ABC123"})
    assert content.signed_tx_blob == "ABC123"


def test_xrpl_payload_content_populate_by_name():
    """The python attribute name (signed_tx_blob) is also accepted."""
    content = XRPLPayloadContent(signed_tx_blob="ABC123")
    assert content.signed_tx_blob == "ABC123"


def test_xrpl_payload_content_round_trips_alias():
    content = XRPLPayloadContent.model_validate({"signedTxBlob": "DEADBEEF"})
    dumped = content.model_dump(by_alias=True)
    assert dumped == {"signedTxBlob": "DEADBEEF"}


def test_xrpl_payload_rejects_missing_field():
    with pytest.raises(Exception):
        XRPLPayloadContent.model_validate({})


def test_xrpl_in_payload_content_union():
    assert XRPLPayloadContent in PayloadContent.__args__


# ---------------------------------------------------------------------------
# get_xrpl_payload() parse helper (mirrors get_stellar_payload)
# ---------------------------------------------------------------------------


def test_get_xrpl_payload_mainnet():
    payload = PaymentPayload.model_validate(
        {
            "x402Version": 1,
            "scheme": "exact",
            "network": "xrpl-mainnet",
            "payload": {"signedTxBlob": "ABC123"},
        }
    )
    content = payload.get_xrpl_payload()
    assert isinstance(content, XRPLPayloadContent)
    assert content.signed_tx_blob == "ABC123"


def test_get_xrpl_payload_testnet():
    payload = PaymentPayload.model_validate(
        {
            "network": "xrpl-testnet",
            "payload": {"signedTxBlob": "FEED01"},
        }
    )
    assert payload.get_xrpl_payload().signed_tx_blob == "FEED01"


# ---------------------------------------------------------------------------
# Network registration / family / decimals
# ---------------------------------------------------------------------------


def test_xrpl_networks_registered():
    mainnet = get_network("xrpl-mainnet")
    testnet = get_network("xrpl-testnet")
    assert mainnet is not None
    assert testnet is not None
    assert mainnet.network_type == NetworkType.XRPL
    assert testnet.network_type == NetworkType.XRPL


def test_xrpl_uses_six_decimals():
    assert get_network("xrpl-mainnet").usdc_decimals == 6


def test_drops_xrp_conversion():
    assert xrp_to_drops(1) == 1_000_000
    assert drops_to_xrp(1_000_000) == 1.0


def test_xrpl_fee_payers():
    assert get_xrpl_fee_payer("xrpl-mainnet") == XRPL_FEE_PAYER_MAINNET
    assert get_xrpl_fee_payer("xrpl-testnet") == XRPL_FEE_PAYER_TESTNET


def test_is_valid_xrpl_address():
    assert is_valid_xrpl_address(XRPL_FEE_PAYER_MAINNET)
    assert not is_valid_xrpl_address("0xdeadbeef")
