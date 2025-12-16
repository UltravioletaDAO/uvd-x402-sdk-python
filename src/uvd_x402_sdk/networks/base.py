"""
Base network configuration and registry.

This module provides the foundation for network configuration, including:
- NetworkConfig dataclass for defining network parameters
- NetworkType enum for categorizing networks
- Global registry for storing and retrieving network configurations
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional, Any


class NetworkType(Enum):
    """
    Network type categorization.

    Different network types use different signature/transaction formats:
    - EVM: EIP-712 signed TransferWithAuthorization (ERC-3009)
    - SVM: Partially-signed VersionedTransaction (SPL token transfer) - Solana, Fogo
    - NEAR: NEP-366 SignedDelegateAction (meta-transaction)
    - STELLAR: Soroban Authorization Entry XDR

    Note: SOLANA is deprecated, use SVM instead for Solana-compatible chains.
    """

    EVM = "evm"
    SVM = "svm"  # Solana Virtual Machine chains (Solana, Fogo, etc.)
    SOLANA = "solana"  # Deprecated: use SVM
    NEAR = "near"
    STELLAR = "stellar"

    @classmethod
    def is_svm(cls, network_type: "NetworkType") -> bool:
        """Check if network type is SVM-compatible (Solana, Fogo, etc.)."""
        return network_type in (cls.SVM, cls.SOLANA)


@dataclass
class NetworkConfig:
    """
    Configuration for a blockchain network supporting x402 payments.

    Attributes:
        name: Lowercase network identifier (e.g., 'base', 'solana')
        display_name: Human-readable name (e.g., 'Base', 'Solana')
        network_type: Type of network (EVM, SOLANA, NEAR, STELLAR)
        chain_id: EVM chain ID (0 for non-EVM networks)
        usdc_address: USDC contract/token address
        usdc_decimals: Number of decimals for USDC (6 for EVM/SVM, 7 for Stellar)
        usdc_domain_name: EIP-712 domain name for USDC (EVM only)
        usdc_domain_version: EIP-712 domain version (EVM only)
        rpc_url: Default RPC endpoint
        enabled: Whether network is currently enabled
        extra_config: Additional network-specific configuration
    """

    name: str
    display_name: str
    network_type: NetworkType
    chain_id: int = 0
    usdc_address: str = ""
    usdc_decimals: int = 6
    usdc_domain_name: str = "USD Coin"
    usdc_domain_version: str = "2"
    rpc_url: str = ""
    enabled: bool = True
    extra_config: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Validate configuration after initialization."""
        if not self.name:
            raise ValueError("Network name is required")
        if not self.usdc_address:
            raise ValueError(f"USDC address is required for network {self.name}")

    def get_token_amount(self, usd_amount: float) -> int:
        """
        Convert USD amount to token base units.

        Args:
            usd_amount: Amount in USD (e.g., 10.50)

        Returns:
            Amount in token base units (e.g., 10500000 for 6 decimals)
        """
        return int(usd_amount * (10**self.usdc_decimals))

    def format_token_amount(self, base_units: int) -> float:
        """
        Convert token base units to USD amount.

        Args:
            base_units: Amount in token base units

        Returns:
            Amount in USD
        """
        return base_units / (10**self.usdc_decimals)


# Global network registry
_NETWORK_REGISTRY: Dict[str, NetworkConfig] = {}


def register_network(config: NetworkConfig) -> None:
    """
    Register a network configuration.

    This allows adding custom networks or overriding built-in configurations.

    Args:
        config: NetworkConfig instance to register

    Example:
        >>> from uvd_x402_sdk.networks import register_network, NetworkConfig, NetworkType
        >>> custom_network = NetworkConfig(
        ...     name="mychain",
        ...     display_name="My Custom Chain",
        ...     network_type=NetworkType.EVM,
        ...     chain_id=12345,
        ...     usdc_address="0x...",
        ... )
        >>> register_network(custom_network)
    """
    _NETWORK_REGISTRY[config.name.lower()] = config


def get_network(name: str) -> Optional[NetworkConfig]:
    """
    Get network configuration by name.

    Args:
        name: Network identifier (case-insensitive)

    Returns:
        NetworkConfig if found, None otherwise
    """
    return _NETWORK_REGISTRY.get(name.lower())


def get_network_by_chain_id(chain_id: int) -> Optional[NetworkConfig]:
    """
    Get network configuration by EVM chain ID.

    Args:
        chain_id: EVM chain ID

    Returns:
        NetworkConfig if found, None otherwise
    """
    for config in _NETWORK_REGISTRY.values():
        if config.chain_id == chain_id and config.network_type == NetworkType.EVM:
            return config
    return None


def list_networks(
    enabled_only: bool = True,
    network_type: Optional[NetworkType] = None,
) -> List[NetworkConfig]:
    """
    List all registered networks.

    Args:
        enabled_only: Only return enabled networks
        network_type: Filter by network type

    Returns:
        List of matching NetworkConfig instances
    """
    networks = list(_NETWORK_REGISTRY.values())

    if enabled_only:
        networks = [n for n in networks if n.enabled]

    if network_type:
        networks = [n for n in networks if n.network_type == network_type]

    return networks


def get_supported_chain_ids() -> List[int]:
    """
    Get list of supported EVM chain IDs.

    Returns:
        List of chain IDs for enabled EVM networks
    """
    return [
        n.chain_id
        for n in _NETWORK_REGISTRY.values()
        if n.enabled and n.network_type == NetworkType.EVM and n.chain_id > 0
    ]


def get_supported_network_names() -> List[str]:
    """
    Get list of supported network names.

    Returns:
        List of network names for enabled networks
    """
    return [n.name for n in _NETWORK_REGISTRY.values() if n.enabled]


# Expose registry for inspection
SUPPORTED_NETWORKS = _NETWORK_REGISTRY


# =============================================================================
# CAIP-2 Utilities (x402 v2 support)
# =============================================================================

# CAIP-2 namespace to network mapping
_CAIP2_NAMESPACE_MAP = {
    "eip155": NetworkType.EVM,
    "solana": NetworkType.SVM,
    "near": NetworkType.NEAR,
    "stellar": NetworkType.STELLAR,
}

# Network name to CAIP-2 format
_NETWORK_TO_CAIP2 = {
    # EVM chains (eip155:chainId)
    "base": "eip155:8453",
    "ethereum": "eip155:1",
    "polygon": "eip155:137",
    "arbitrum": "eip155:42161",
    "optimism": "eip155:10",
    "avalanche": "eip155:43114",
    "celo": "eip155:42220",
    "hyperevm": "eip155:999",
    "unichain": "eip155:130",
    "monad": "eip155:143",
    # SVM chains (solana:genesisHash first 32 chars)
    "solana": "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp",
    "fogo": "solana:fogo",  # Placeholder - update when known
    # NEAR
    "near": "near:mainnet",
    # Stellar
    "stellar": "stellar:pubnet",
}

# CAIP-2 to network name mapping (reverse of above)
_CAIP2_TO_NETWORK = {v: k for k, v in _NETWORK_TO_CAIP2.items()}


def parse_caip2_network(caip2_id: str) -> Optional[str]:
    """
    Parse a CAIP-2 network identifier to network name.

    CAIP-2 format: namespace:reference
    Examples:
        - "eip155:8453" -> "base"
        - "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp" -> "solana"
        - "near:mainnet" -> "near"

    Args:
        caip2_id: CAIP-2 format network identifier

    Returns:
        Network name if recognized, None otherwise
    """
    if not caip2_id or ":" not in caip2_id:
        return None

    # Direct lookup first
    if caip2_id in _CAIP2_TO_NETWORK:
        return _CAIP2_TO_NETWORK[caip2_id]

    # Parse namespace and reference
    parts = caip2_id.split(":", 1)
    if len(parts) != 2:
        return None

    namespace, reference = parts

    # For EIP-155 (EVM), the reference is the chain ID
    if namespace == "eip155":
        try:
            chain_id = int(reference)
            network = get_network_by_chain_id(chain_id)
            return network.name if network else None
        except ValueError:
            return None

    # For other namespaces, check if reference matches known patterns
    # This handles cases like "solana:mainnet" or "near:mainnet"
    if namespace == "solana" and reference in ("mainnet", "mainnet-beta"):
        return "solana"
    if namespace == "near" and reference == "mainnet":
        return "near"
    if namespace == "stellar" and reference in ("pubnet", "mainnet"):
        return "stellar"

    return None


def to_caip2_network(network_name: str) -> Optional[str]:
    """
    Convert network name to CAIP-2 format.

    Args:
        network_name: Network identifier (e.g., 'base', 'solana')

    Returns:
        CAIP-2 format string (e.g., 'eip155:8453'), or None if unknown
    """
    return _NETWORK_TO_CAIP2.get(network_name.lower())


def is_caip2_format(network: str) -> bool:
    """
    Check if a network identifier is in CAIP-2 format.

    Args:
        network: Network identifier to check

    Returns:
        True if CAIP-2 format (contains colon), False if v1 format
    """
    return ":" in network


def normalize_network(network: str) -> str:
    """
    Normalize a network identifier to v1 format (network name).

    Handles both v1 ("base") and v2 CAIP-2 ("eip155:8453") formats.

    Args:
        network: Network identifier in either format

    Returns:
        Normalized network name (v1 format)

    Raises:
        ValueError: If network cannot be parsed
    """
    if is_caip2_format(network):
        normalized = parse_caip2_network(network)
        if normalized is None:
            raise ValueError(f"Unknown CAIP-2 network: {network}")
        return normalized
    return network.lower()
