"""
Tests for Casper Network payload model and parse helpers.

Mirrors the per-chain payload-model coverage for Stellar/XRPL/Sui:
- CasperPayloadContent parses { signature, publicKey, authorization }
- CasperAuthorization accepts the "from" alias and populate_by_name
- PaymentPayload.get_casper_payload() returns the parsed content
- Network identifiers ("casper"/"casper-testnet", CAIP-2 "casper:casper"/
  "casper:casper-test") and 9-decimal motes
- wCSPR (CEP-18) settlement asset and address validation helpers
"""

import pytest

from uvd_x402_sdk.models import (
    CasperAuthorization,
    CasperPayloadContent,
    PayloadContent,
    PaymentPayload,
)
from uvd_x402_sdk.networks import (
    NetworkType,
    get_network,
    normalize_network,
    parse_caip2_network,
    to_caip2_network,
)
from uvd_x402_sdk.networks.casper import (
    CASPER_FACILITATOR_URL,
    WCSPR_CONTRACT_PACKAGE_MAINNET,
    WCSPR_CONTRACT_PACKAGE_TESTNET,
    cspr_to_motes,
    get_casper_chain_name,
    get_casper_facilitator_url,
    get_wcspr_contract_package,
    is_casper_network,
    is_valid_casper_address,
    is_valid_casper_public_key,
    is_valid_contract_package_hash,
    motes_to_cspr,
    validate_casper_payload,
)

# Sample data (formats from the Casper x402 "exact" scheme)
PAYER_ADDRESS = "00" + "ab" * 32  # account-hash prefixed address
MERCHANT_ADDRESS = "001857b576e2247b68d5bb0dbb6cd70361b056262d0a64d7ded1cdc7326954e344"
ED25519_PUBLIC_KEY = "01" + "cd" * 32
SECP256K1_PUBLIC_KEY = "02" + "ef" * 33
SAMPLE_AUTHORIZATION = {
    "from": PAYER_ADDRESS,
    "to": MERCHANT_ADDRESS,
    "value": "1000000000",
    "validAfter": "0",
    "validBefore": "1893456000",
    "nonce": "aa" * 32,
}
SAMPLE_PAYLOAD = {
    "signature": "0x" + "11" * 65,
    "publicKey": ED25519_PUBLIC_KEY,
    "authorization": SAMPLE_AUTHORIZATION,
}


# ---------------------------------------------------------------------------
# Payload model
# ---------------------------------------------------------------------------


def test_casper_payload_content_parses_wire_format():
    """The wire fields are signature, publicKey and authorization (with `from`)."""
    content = CasperPayloadContent.model_validate(SAMPLE_PAYLOAD)
    assert content.signature == SAMPLE_PAYLOAD["signature"]
    assert content.publicKey == ED25519_PUBLIC_KEY
    assert content.authorization.from_address == PAYER_ADDRESS
    assert content.authorization.to == MERCHANT_ADDRESS
    assert content.authorization.value == "1000000000"


def test_casper_authorization_populate_by_name():
    """The python attribute name (from_address) is also accepted."""
    auth = CasperAuthorization(
        from_address=PAYER_ADDRESS,
        to=MERCHANT_ADDRESS,
        value="500000000",
        validAfter="0",
        validBefore="1893456000",
        nonce="bb" * 32,
    )
    assert auth.from_address == PAYER_ADDRESS


def test_casper_authorization_round_trips_alias():
    auth = CasperAuthorization.model_validate(SAMPLE_AUTHORIZATION)
    dumped = auth.model_dump(by_alias=True)
    assert dumped["from"] == PAYER_ADDRESS
    assert "from_address" not in dumped


def test_casper_payload_rejects_missing_field():
    with pytest.raises(Exception):
        CasperPayloadContent.model_validate({"signature": "0x11"})


def test_casper_in_payload_content_union():
    assert CasperPayloadContent in PayloadContent.__args__


# ---------------------------------------------------------------------------
# get_casper_payload() parse helper (mirrors get_stellar_payload)
# ---------------------------------------------------------------------------


def test_get_casper_payload_mainnet_caip2():
    payload = PaymentPayload.model_validate(
        {
            "x402Version": 2,
            "scheme": "exact",
            "network": "casper:casper",
            "payload": SAMPLE_PAYLOAD,
        }
    )
    content = payload.get_casper_payload()
    assert isinstance(content, CasperPayloadContent)
    assert content.authorization.from_address == PAYER_ADDRESS
    assert payload.get_normalized_network() == "casper"


def test_get_casper_payload_testnet_caip2():
    payload = PaymentPayload.model_validate(
        {
            "x402Version": 2,
            "scheme": "exact",
            "network": "casper:casper-test",
            "payload": SAMPLE_PAYLOAD,
        }
    )
    assert payload.get_casper_payload().publicKey == ED25519_PUBLIC_KEY
    assert payload.get_normalized_network() == "casper-testnet"


def test_get_casper_payload_v1_name():
    payload = PaymentPayload.model_validate(
        {
            "network": "casper",
            "payload": SAMPLE_PAYLOAD,
        }
    )
    assert payload.get_casper_payload().authorization.to == MERCHANT_ADDRESS


# ---------------------------------------------------------------------------
# Network registration / family / decimals
# ---------------------------------------------------------------------------


def test_casper_networks_registered():
    mainnet = get_network("casper")
    testnet = get_network("casper-testnet")
    assert mainnet is not None
    assert testnet is not None
    assert mainnet.network_type == NetworkType.CASPER
    assert testnet.network_type == NetworkType.CASPER


def test_casper_uses_nine_decimals():
    assert get_network("casper").usdc_decimals == 9
    assert get_network("casper-testnet").usdc_decimals == 9


def test_casper_settlement_asset_is_wcspr():
    mainnet = get_network("casper")
    testnet = get_network("casper-testnet")
    assert mainnet.usdc_address == WCSPR_CONTRACT_PACKAGE_MAINNET
    assert testnet.usdc_address == WCSPR_CONTRACT_PACKAGE_TESTNET
    assert mainnet.extra_config["settlement_asset"] == "WCSPR"
    assert is_valid_contract_package_hash(mainnet.usdc_address)
    assert is_valid_contract_package_hash(testnet.usdc_address)


def test_casper_caip2_mappings():
    assert to_caip2_network("casper") == "casper:casper"
    assert to_caip2_network("casper-testnet") == "casper:casper-test"
    assert parse_caip2_network("casper:casper") == "casper"
    assert parse_caip2_network("casper:casper-test") == "casper-testnet"
    assert normalize_network("casper:casper") == "casper"
    assert normalize_network("casper:casper-test") == "casper-testnet"


def test_motes_cspr_conversion():
    assert cspr_to_motes(1) == 1_000_000_000
    assert motes_to_cspr(1_000_000_000) == 1.0
    assert cspr_to_motes(0.5) == 500_000_000


def test_casper_facilitator_url():
    assert CASPER_FACILITATOR_URL == "https://x402-facilitator.cspr.cloud"
    assert get_casper_facilitator_url("casper") == CASPER_FACILITATOR_URL
    assert get_casper_facilitator_url("casper-testnet") == CASPER_FACILITATOR_URL
    assert get_network("casper").extra_config["facilitator_url"] == CASPER_FACILITATOR_URL


def test_get_wcspr_contract_package():
    assert get_wcspr_contract_package("casper") == WCSPR_CONTRACT_PACKAGE_MAINNET
    assert get_wcspr_contract_package("casper-testnet") == WCSPR_CONTRACT_PACKAGE_TESTNET


def test_get_casper_chain_name():
    assert get_casper_chain_name("casper") == "casper"
    assert get_casper_chain_name("casper-testnet") == "casper-test"


def test_is_casper_network():
    assert is_casper_network("casper")
    assert is_casper_network("casper-testnet")
    assert not is_casper_network("base")
    assert not is_casper_network("xrpl-mainnet")


# ---------------------------------------------------------------------------
# Address / key validation
# ---------------------------------------------------------------------------


def test_is_valid_casper_address():
    assert is_valid_casper_address(PAYER_ADDRESS)
    assert is_valid_casper_address(MERCHANT_ADDRESS)
    assert is_valid_casper_address("01" + "ab" * 32)  # hash prefix
    assert not is_valid_casper_address("02" + "ab" * 32)  # bad prefix
    assert not is_valid_casper_address("00" + "ab" * 31)  # too short
    assert not is_valid_casper_address("0xdeadbeef")
    assert not is_valid_casper_address("")


def test_is_valid_casper_public_key():
    assert is_valid_casper_public_key(ED25519_PUBLIC_KEY)  # 01-prefixed ed25519
    assert is_valid_casper_public_key(SECP256K1_PUBLIC_KEY)  # 02-prefixed secp256k1
    assert not is_valid_casper_public_key("01" + "cd" * 33)  # wrong length
    assert not is_valid_casper_public_key("02" + "ef" * 32)  # wrong length
    assert not is_valid_casper_public_key("03" + "cd" * 32)  # bad prefix
    assert not is_valid_casper_public_key("")


def test_is_valid_contract_package_hash():
    assert is_valid_contract_package_hash(WCSPR_CONTRACT_PACKAGE_MAINNET)
    assert not is_valid_contract_package_hash("hash-" + WCSPR_CONTRACT_PACKAGE_MAINNET)
    assert not is_valid_contract_package_hash("ab" * 31)


# ---------------------------------------------------------------------------
# validate_casper_payload
# ---------------------------------------------------------------------------


def test_validate_casper_payload_accepts_valid():
    assert validate_casper_payload(SAMPLE_PAYLOAD) is True


def test_validate_casper_payload_rejects_missing_signature():
    payload = {k: v for k, v in SAMPLE_PAYLOAD.items() if k != "signature"}
    with pytest.raises(ValueError, match="signature"):
        validate_casper_payload(payload)


def test_validate_casper_payload_rejects_bad_public_key():
    payload = dict(SAMPLE_PAYLOAD, publicKey="deadbeef")
    with pytest.raises(ValueError, match="publicKey"):
        validate_casper_payload(payload)


def test_validate_casper_payload_rejects_bad_from_address():
    payload = dict(SAMPLE_PAYLOAD, authorization=dict(SAMPLE_AUTHORIZATION, **{"from": "bad"}))
    with pytest.raises(ValueError, match="from"):
        validate_casper_payload(payload)


def test_validate_casper_payload_rejects_non_positive_value():
    payload = dict(SAMPLE_PAYLOAD, authorization=dict(SAMPLE_AUTHORIZATION, value="0"))
    with pytest.raises(ValueError):
        validate_casper_payload(payload)


# ---------------------------------------------------------------------------
# Config integration
# ---------------------------------------------------------------------------


def test_casper_recipient_in_config():
    from uvd_x402_sdk.config import X402Config

    config = X402Config(recipient_casper=MERCHANT_ADDRESS)
    assert config.get_recipient("casper") == MERCHANT_ADDRESS
    assert config.get_recipient("casper-testnet") == MERCHANT_ADDRESS
    assert config.is_network_enabled("casper")
    assert config.is_network_enabled("casper-testnet")
