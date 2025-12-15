"""
SDK configuration classes.

This module provides configuration management for the x402 SDK,
including facilitator settings, recipient addresses, and timeouts.

Supports:
- x402 v1 and v2 protocols
- Multi-network payment options
- Per-network recipient configuration
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any, Literal
import os


@dataclass
class NetworkRecipientConfig:
    """
    Configuration for a specific network's recipient address.

    Use this to specify different recipient addresses for different networks.
    """

    recipient: str
    enabled: bool = True


# Alias for backward compatibility
NetworkConfig = NetworkRecipientConfig


@dataclass
class MultiPaymentConfig:
    """
    Configuration for multi-payment support.

    Allows users to offer multiple networks for payment acceptance.
    """

    networks: List[str] = field(default_factory=list)
    default_network: Optional[str] = None

    def __post_init__(self) -> None:
        if self.networks and not self.default_network:
            self.default_network = self.networks[0]


@dataclass
class X402Config:
    """
    Main SDK configuration.

    Attributes:
        facilitator_url: URL of the x402 facilitator service
        recipient_evm: Default recipient address for EVM chains
        recipient_solana: Recipient address for Solana/SVM chains (also used for Fogo)
        recipient_near: Recipient account for NEAR
        recipient_stellar: Recipient address for Stellar
        facilitator_solana: Solana/SVM facilitator address (fee payer)
        verify_timeout: Timeout for verify requests (seconds)
        settle_timeout: Timeout for settle requests (seconds)
        supported_networks: List of enabled network names
        network_configs: Per-network recipient overrides
        resource_url: Resource URL sent to facilitator
        description: Description sent to facilitator
        x402_version: Protocol version to use (1, 2, or "auto")
        multi_payment: Multi-payment configuration for accepting multiple networks
    """

    facilitator_url: str = "https://facilitator.ultravioletadao.xyz"

    # Recipient addresses per network type
    recipient_evm: str = ""
    recipient_solana: str = ""  # Also used for Fogo and other SVM chains
    recipient_near: str = ""
    recipient_stellar: str = ""

    # Solana/SVM facilitator (fee payer) - same for all SVM chains
    facilitator_solana: str = "F742C4VfFLQ9zRQyithoj5229ZgtX2WqKCSFKgH2EThq"

    # Timeouts
    verify_timeout: float = 30.0
    settle_timeout: float = 55.0  # Must be < Lambda timeout (60s)

    # Network configuration - All 15 networks (14 enabled by default)
    supported_networks: List[str] = field(default_factory=lambda: [
        # EVM chains (11 total, 10 enabled)
        "base", "ethereum", "polygon", "arbitrum", "optimism",
        "avalanche", "celo", "hyperevm", "unichain", "monad",
        # bsc disabled: Binance-Peg USDC doesn't support ERC-3009
        # SVM chains (2 total, 2 enabled)
        "solana", "fogo",
        # NEAR (1 total, 1 enabled)
        "near",
        # Stellar (1 total, 1 enabled)
        "stellar",
    ])

    # Per-network recipient overrides
    network_configs: Dict[str, NetworkRecipientConfig] = field(default_factory=dict)

    # Facilitator request metadata
    resource_url: str = ""
    description: str = "x402 payment"

    # x402 protocol version: 1, 2, or "auto" (detect from payload)
    x402_version: Literal[1, 2, "auto"] = "auto"

    # Multi-payment configuration
    multi_payment: Optional[MultiPaymentConfig] = None

    def __post_init__(self) -> None:
        """Validate configuration after initialization."""
        if not self.facilitator_url:
            raise ValueError("facilitator_url is required")

        # At least one recipient is required
        if not any([
            self.recipient_evm,
            self.recipient_solana,
            self.recipient_near,
            self.recipient_stellar,
        ]):
            raise ValueError("At least one recipient address is required")

    @classmethod
    def from_env(cls) -> "X402Config":
        """
        Create configuration from environment variables.

        Environment variables:
            X402_FACILITATOR_URL: Facilitator URL
            X402_RECIPIENT_EVM: EVM recipient address
            X402_RECIPIENT_SOLANA: Solana recipient address
            X402_RECIPIENT_NEAR: NEAR recipient account
            X402_RECIPIENT_STELLAR: Stellar recipient address
            X402_FACILITATOR_SOLANA: Solana fee payer address
            X402_VERIFY_TIMEOUT: Verify request timeout
            X402_SETTLE_TIMEOUT: Settle request timeout
            X402_RESOURCE_URL: Resource URL for facilitator
            X402_DESCRIPTION: Description for facilitator
        """
        return cls(
            facilitator_url=os.environ.get(
                "X402_FACILITATOR_URL",
                "https://facilitator.ultravioletadao.xyz",
            ),
            recipient_evm=os.environ.get("X402_RECIPIENT_EVM", ""),
            recipient_solana=os.environ.get("X402_RECIPIENT_SOLANA", ""),
            recipient_near=os.environ.get("X402_RECIPIENT_NEAR", ""),
            recipient_stellar=os.environ.get("X402_RECIPIENT_STELLAR", ""),
            facilitator_solana=os.environ.get(
                "X402_FACILITATOR_SOLANA",
                "F742C4VfFLQ9zRQyithoj5229ZgtX2WqKCSFKgH2EThq",
            ),
            verify_timeout=float(os.environ.get("X402_VERIFY_TIMEOUT", "30")),
            settle_timeout=float(os.environ.get("X402_SETTLE_TIMEOUT", "55")),
            resource_url=os.environ.get("X402_RESOURCE_URL", ""),
            description=os.environ.get("X402_DESCRIPTION", "x402 payment"),
        )

    def get_recipient(self, network: str) -> str:
        """
        Get recipient address for a specific network.

        First checks network_configs for overrides, then falls back to
        network-type default.

        Args:
            network: Network name (e.g., 'base', 'solana', 'fogo')

        Returns:
            Recipient address for the network
        """
        # Check for network-specific override
        if network in self.network_configs:
            return self.network_configs[network].recipient

        # Fall back to network-type default
        from uvd_x402_sdk.networks import get_network, NetworkType

        network_config = get_network(network)
        if not network_config:
            return self.recipient_evm  # Default to EVM

        # SVM chains (Solana, Fogo, etc.) use the same recipient
        if NetworkType.is_svm(network_config.network_type):
            return self.recipient_solana
        elif network_config.network_type == NetworkType.NEAR:
            return self.recipient_near
        elif network_config.network_type == NetworkType.STELLAR:
            return self.recipient_stellar
        else:
            return self.recipient_evm

    def is_network_enabled(self, network: str) -> bool:
        """
        Check if a network is enabled.

        Args:
            network: Network name

        Returns:
            True if network is in supported_networks and not disabled
        """
        if network not in self.supported_networks:
            return False

        # Check network-specific config
        if network in self.network_configs:
            return self.network_configs[network].enabled

        return True

    def get_supported_chain_ids(self) -> List[int]:
        """
        Get list of supported EVM chain IDs.

        Returns:
            List of chain IDs for enabled EVM networks
        """
        from uvd_x402_sdk.networks import get_network, NetworkType

        chain_ids = []
        for network_name in self.supported_networks:
            network = get_network(network_name)
            if network and network.network_type == NetworkType.EVM and network.chain_id > 0:
                if self.is_network_enabled(network_name):
                    chain_ids.append(network.chain_id)

        return chain_ids

    def to_dict(self) -> Dict[str, Any]:
        """Convert configuration to dictionary."""
        return {
            "facilitator_url": self.facilitator_url,
            "recipient_evm": self.recipient_evm,
            "recipient_solana": self.recipient_solana,
            "recipient_near": self.recipient_near,
            "recipient_stellar": self.recipient_stellar,
            "facilitator_solana": self.facilitator_solana,
            "verify_timeout": self.verify_timeout,
            "settle_timeout": self.settle_timeout,
            "supported_networks": self.supported_networks,
            "resource_url": self.resource_url,
            "description": self.description,
        }
