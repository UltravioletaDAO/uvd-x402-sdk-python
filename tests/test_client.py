"""
Tests for the X402Client.

Run with: pytest tests/
"""

import base64
import json
from decimal import Decimal

import pytest

from uvd_x402_sdk import X402Client, X402Config
from uvd_x402_sdk.exceptions import (
    InvalidPayloadError,
    UnsupportedNetworkError,
)
from uvd_x402_sdk.models import PaymentPayload


class TestX402Config:
    """Tests for X402Config."""

    def test_config_requires_recipient(self):
        """Config must have at least one recipient."""
        with pytest.raises(ValueError, match="recipient"):
            X402Config()

    def test_config_with_evm_recipient(self):
        """Config accepts EVM recipient."""
        config = X402Config(recipient_evm="0x1234567890123456789012345678901234567890")
        assert config.recipient_evm == "0x1234567890123456789012345678901234567890"

    def test_config_get_recipient_by_network(self):
        """Get correct recipient for each network type."""
        config = X402Config(
            recipient_evm="0xEVM",
            recipient_solana="SOLANA",
            recipient_stellar="GSTELLAR",
        )
        assert config.get_recipient("base") == "0xEVM"
        assert config.get_recipient("ethereum") == "0xEVM"
        assert config.get_recipient("solana") == "SOLANA"
        assert config.get_recipient("stellar") == "GSTELLAR"


class TestPayloadExtraction:
    """Tests for payload extraction from X-PAYMENT header."""

    @pytest.fixture
    def client(self):
        """Create test client."""
        return X402Client(recipient_address="0x1234567890123456789012345678901234567890")

    def test_extract_valid_evm_payload(self, client):
        """Extract valid EVM payment payload."""
        payload_data = {
            "x402Version": 1,
            "scheme": "exact",
            "network": "base",
            "payload": {
                "signature": "0xabc123...",
                "authorization": {
                    "from": "0xSender",
                    "to": "0xRecipient",
                    "value": "1000000",
                    "validAfter": "0",
                    "validBefore": "9999999999",
                    "nonce": "0x123",
                },
            },
        }
        header = base64.b64encode(json.dumps(payload_data).encode()).decode()
        payload = client.extract_payload(header)

        assert payload.x402Version == 1
        assert payload.scheme == "exact"
        assert payload.network == "base"

    def test_extract_valid_solana_payload(self, client):
        """Extract valid Solana payment payload."""
        payload_data = {
            "x402Version": 1,
            "scheme": "exact",
            "network": "solana",
            "payload": {
                "transaction": "SGVsbG8gV29ybGQ=",  # Base64 encoded
            },
        }
        header = base64.b64encode(json.dumps(payload_data).encode()).decode()
        payload = client.extract_payload(header)

        assert payload.network == "solana"
        solana_payload = payload.get_solana_payload()
        assert solana_payload.transaction == "SGVsbG8gV29ybGQ="

    def test_extract_invalid_base64(self, client):
        """Reject invalid base64 encoding."""
        with pytest.raises(InvalidPayloadError, match="base64"):
            client.extract_payload("not-valid-base64!!!")

    def test_extract_invalid_json(self, client):
        """Reject invalid JSON."""
        header = base64.b64encode(b"not json").decode()
        with pytest.raises(InvalidPayloadError, match="JSON"):
            client.extract_payload(header)

    def test_extract_missing_header(self, client):
        """Reject empty header."""
        with pytest.raises(InvalidPayloadError, match="Missing"):
            client.extract_payload("")

    def test_extract_invalid_version(self, client):
        """Reject unsupported x402 version."""
        payload_data = {
            "x402Version": 2,  # Unsupported
            "scheme": "exact",
            "network": "base",
            "payload": {},
        }
        header = base64.b64encode(json.dumps(payload_data).encode()).decode()
        with pytest.raises(InvalidPayloadError):
            client.extract_payload(header)


class TestNetworkValidation:
    """Tests for network validation."""

    @pytest.fixture
    def client(self):
        """Create test client."""
        return X402Client(recipient_address="0x1234567890123456789012345678901234567890")

    def test_validate_supported_network(self, client):
        """Accept supported networks."""
        # Should not raise
        client.validate_network("base")
        client.validate_network("solana")
        client.validate_network("stellar")

    def test_validate_unsupported_network(self, client):
        """Reject unsupported networks."""
        with pytest.raises(UnsupportedNetworkError) as exc_info:
            client.validate_network("unknown_chain")

        assert "unknown_chain" in str(exc_info.value)
        assert len(exc_info.value.supported_networks) > 0


class TestNetworkRegistry:
    """Tests for network registry."""

    def test_get_network_by_name(self):
        """Get network by name."""
        from uvd_x402_sdk.networks import get_network

        base = get_network("base")
        assert base is not None
        assert base.chain_id == 8453
        assert base.usdc_decimals == 6

    def test_get_network_by_chain_id(self):
        """Get network by chain ID."""
        from uvd_x402_sdk.networks import get_network_by_chain_id

        base = get_network_by_chain_id(8453)
        assert base is not None
        assert base.name == "base"

    def test_list_networks(self):
        """List all networks."""
        from uvd_x402_sdk.networks import list_networks, NetworkType

        # All enabled networks
        networks = list_networks(enabled_only=True)
        assert len(networks) > 5  # At least several networks

        # EVM only
        evm_networks = list_networks(network_type=NetworkType.EVM)
        assert all(n.network_type == NetworkType.EVM for n in evm_networks)

    def test_register_custom_network(self):
        """Register a custom network."""
        from uvd_x402_sdk.networks import (
            NetworkConfig,
            NetworkType,
            register_network,
            get_network,
        )

        custom = NetworkConfig(
            name="testchain",
            display_name="Test Chain",
            network_type=NetworkType.EVM,
            chain_id=99999,
            usdc_address="0xTestUSDC",
            rpc_url="https://rpc.testchain.com",
        )
        register_network(custom)

        retrieved = get_network("testchain")
        assert retrieved is not None
        assert retrieved.chain_id == 99999


class TestPaymentResult:
    """Tests for PaymentResult model."""

    def test_payment_result_serialization(self):
        """PaymentResult can be serialized to JSON."""
        from uvd_x402_sdk.models import PaymentResult

        result = PaymentResult(
            success=True,
            payer_address="0xPayer",
            transaction_hash="0xTxHash",
            network="base",
            amount_usd=Decimal("10.00"),
        )

        json_str = result.model_dump_json()
        data = json.loads(json_str)

        assert data["success"] is True
        assert data["payer_address"] == "0xPayer"
        assert data["amount_usd"] == "10.00"


class TestResponseHelpers:
    """Tests for 402 response creation."""

    def test_create_402_response(self):
        """Create standard 402 response body."""
        from uvd_x402_sdk.response import create_402_response

        config = X402Config(
            recipient_evm="0xRecipient",
            recipient_solana="SolanaRecipient",
        )

        response = create_402_response(
            amount_usd=Decimal("5.00"),
            config=config,
            message="Test payment required",
        )

        assert response["error"] == "Payment required"
        assert response["amount"] == "5.00"
        assert response["token"] == "USDC"
        assert "recipients" in response
        assert "supportedChains" in response
        assert len(response["supportedChains"]) > 0

    def test_payment_402_builder(self):
        """Use builder pattern for 402 response."""
        from uvd_x402_sdk.response import Payment402Builder

        config = X402Config(recipient_evm="0xRecipient")

        response = (
            Payment402Builder(config)
            .amount(Decimal("10.00"))
            .message("Premium access required")
            .networks(["base", "solana"])
            .extra("customField", "customValue")
            .build()
        )

        assert response["amount"] == "10.00"
        assert response["message"] == "Premium access required"
        assert response["customField"] == "customValue"
