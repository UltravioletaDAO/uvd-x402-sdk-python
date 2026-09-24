"""
Base network configuration and registry.

This module provides the foundation for network configuration, including:
- NetworkConfig dataclass for defining network parameters
- NetworkType enum for categorizing networks
- TokenType for multi-stablecoin support
- Global registry for storing and retrieving network configurations
"""

from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from fractions import Fraction
from typing import Dict, List, Literal, Optional, Any, Union


# =============================================================================
# Token Type Definitions (Multi-Stablecoin Support)
# =============================================================================

# Supported stablecoin token types
# - usdc: USD Coin (Circle) - 6 decimals
# - eurc: Euro Coin (Circle) - 6 decimals
# - ausd: Agora USD (Agora Finance) - 6 decimals
# - pyusd: PayPal USD (PayPal/Paxos) - 6 decimals
# - usdt: Tether USD (USDT0 omnichain via LayerZero) - 6 decimals
# - usdg: Global Dollar (Paxos USDG) - 6 decimals - default asset on Robinhood Chain
TokenType = Literal["usdc", "eurc", "ausd", "pyusd", "usdt", "usdg", "hbar"]

# All supported token types
ALL_TOKEN_TYPES: List[TokenType] = ["usdc", "eurc", "ausd", "pyusd", "usdt", "usdg", "hbar"]


@dataclass
class TokenConfig:
    """
    Configuration for a stablecoin token on a specific network.

    Attributes:
        address: Contract address of the token
        decimals: Number of decimals (6 for all supported stablecoins)
        name: Token name for EIP-712 domain (e.g., "USD Coin" or "USDC")
        version: Token version for EIP-712 domain
    """

    address: str
    decimals: int
    name: str
    version: str
    usd_pegged: bool = True


class NetworkType(Enum):
    """
    Network type categorization.

    Different network types use different signature/transaction formats:
    - EVM: EIP-712 signed TransferWithAuthorization (ERC-3009)
    - SVM: Partially-signed VersionedTransaction (SPL token transfer) - Solana, Fogo
    - NEAR: NEP-366 SignedDelegateAction (meta-transaction)
    - STELLAR: Soroban Authorization Entry XDR
    - ALGORAND: ASA (Algorand Standard Assets) transfer via signed transaction
    - SUI: Sui sponsored transactions (Move-based programmable transactions)
    - XRPL: XRP Ledger pre-signed Payment transaction blobs (native XRP)

    Note: SOLANA is deprecated, use SVM instead for Solana-compatible chains.
    """

    EVM = "evm"
    SVM = "svm"  # Solana Virtual Machine chains (Solana, Fogo, etc.)
    SOLANA = "solana"  # Deprecated: use SVM
    NEAR = "near"
    STELLAR = "stellar"
    ALGORAND = "algorand"  # Algorand ASA transfers
    SUI = "sui"  # Sui Move VM chains (sponsored transactions)
    HEDERA = "hedera"
    XRPL = "xrpl"  # XRP Ledger (native XRP, pre-signed Payment tx blobs)

    @classmethod
    def is_svm(cls, network_type: "NetworkType") -> bool:
        """Check if network type is SVM-compatible (Solana, Fogo, etc.)."""
        return network_type in (cls.SVM, cls.SOLANA)

    @classmethod
    def is_sui(cls, network_type: "NetworkType") -> bool:
        """Check if network type is Sui-based."""
        return network_type == cls.SUI


# How far from a whole number of base units an amount may be and still count as
# float noise. Either bound is enough: 10 ** -6 of a base unit (10 ** -(decimals
# + 6) whole tokens), or 2 ** -49 of the amount, about eight units in the last
# place of a double, because float noise grows with the price. They move money,
# so they live here and not in configuration.
_FLOAT_NOISE_BASE_UNITS = Fraction(1, 10**6)
_FLOAT_NOISE_RELATIVE = Fraction(1, 2**49)


def to_base_units(
    amount: Union[Decimal, float, int, str],
    decimals: int,
    *,
    unit: str = "the token",
) -> int:
    """
    Convert an amount in whole tokens into base units, or refuse.

    A payer signs a whole number of base units. An amount with a real digit
    below one base unit cannot be signed as written: ``0.0000015`` or ``5E-7``
    with 6 decimals. It raises instead of being truncated, because truncating
    charges an amount nobody wrote (``5E-7`` became a price of 0). Trailing
    zeros are not such digits: ``2.010`` is ``2010000`` with 6 decimals.

    Float noise is not such a digit either. A price computed with floats lands
    a few units in the last place of a double away from the price meant
    (``Decimal(str(35 * 0.01))`` is ``0.35000000000000003``, ``Decimal(2.01)``
    is ``2.00999999999999978...``), and it converts to the nearest whole number
    of base units, 350000 and 2010000 with 6 decimals. It counts as noise when
    it is within ``10 ** -(decimals + 6)`` of that number, or within
    ``abs(amount) * 2 ** -49``. The second bound is needed because the noise
    grows with the price: ``n * 0.10`` read this way is ``1e-13`` off for n from
    5123 to 10000, which the first bound alone refuses at 7 decimals. Measured
    on ``n * 0.01``, ``n * 0.07`` and ``n * 0.10`` for n up to 1,000,000, no
    price is refused at 6 or 7 decimals. The cost: a real half base unit is
    rounded too once the price is large enough for the relative bound to
    reach it, from about $281 million at 6 decimals and $28 million at 7.

    The arithmetic is exact at any length (``Fraction``): a ``Decimal`` product
    rounds to the context's 28 digits first. A float or a string is read
    through its decimal form (``str``).

    Args:
        amount: Amount in whole tokens (e.g. ``Decimal("10.50")``)
        decimals: Decimals of the token
        unit: What the base units belong to, for the error message

    Returns:
        The amount in base units (e.g. 10500000 for 6 decimals)

    Raises:
        ValueError: If the amount is not finite, is negative, or has a digit
            below one base unit of a token with ``decimals`` decimals that is
            not float noise.
    """
    value = amount if isinstance(amount, Decimal) else Decimal(str(amount))
    if not value.is_finite():
        raise ValueError(f"amount must be a finite number, got {value}")
    if value < 0:
        raise ValueError(f"amount must not be negative, got {format(value, 'f')}")
    scaled = Fraction(value) * Fraction(10) ** decimals
    units = round(scaled)
    off = abs(scaled - units)
    if off and off >= _FLOAT_NOISE_BASE_UNITS and off > scaled * _FLOAT_NOISE_RELATIVE:
        raise ValueError(
            f"{format(value, 'f')} is not a whole number of base units of {unit} "
            f"({decimals} decimals), so no payer can sign it exactly. Write the "
            f"price with at most {decimals} decimal places."
        )
    return units


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
        default_token: Primary settlement token for the network (defaults to
            'usdc'; e.g. 'usdg' for Robinhood Chain which settles in Paxos USDG).
            The usdc_* fields above hold this token's address/domain.
        tokens: Multi-token configurations (EVM chains only, maps token type to config)
        usd_pegged: Whether the DEFAULT settlement asset is worth one dollar per
            whole unit. True for dollar stablecoin networks (USDC, AUSD,
            PYUSD, USDT, USDG); False for a chain that settles in its own
            volatile native asset — XRPL settles in XRP. Only a pegged asset
            lets a price written in USD become base units, so
            `get_token_amount` refuses when this is False.
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
    settle_timeout_seconds: float = 90.0  # Per-network settle timeout (Eth L1=900, L2s=90)
    default_token: TokenType = "usdc"
    tokens: Dict[TokenType, TokenConfig] = field(default_factory=dict)
    usd_pegged: bool = True
    extra_config: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Validate configuration after initialization."""
        if not self.name:
            raise ValueError("Network name is required")
        # Native-asset chains (e.g. XRPL with native XRP) have no token contract.
        if not self.usdc_address and self.network_type != NetworkType.XRPL:
            raise ValueError(f"USDC address is required for network {self.name}")

    def get_token_amount(self, usd_amount: Union[Decimal, float, int, str]) -> int:
        """
        Convert USD amount to token base units.

        The product is taken in ``Decimal``: a float is read through its
        shortest decimal form (``str``), never scaled as a binary float. Scaled
        as a float, 151 of the 9,999 prices from $0.01 to $99.99 came out one
        base unit short (``int(2.01 * 10**6)`` is ``2009999``) while the payer
        signed the exact amount. A fraction below one base unit raises (see
        :func:`to_base_units`); it used to be dropped, so the seller required
        an amount it never wrote.

        Args:
            usd_amount: Amount in USD (e.g., ``Decimal("10.50")``, ``10.50``
                or ``"10.50"``)

        Returns:
            Amount in token base units (e.g., 10500000 for 6 decimals)

        Raises:
            ValueError: If this network's default settlement asset is not
                pegged to the dollar. Scaling by the decimals only turns
                dollars into base units when one whole unit IS one dollar;
                on XRPL it would charge 1 XRP for a price written as $1.00.
                Also if the amount is not finite, is negative, or has a real
                digit below one base unit (``0.0000015`` with 6 decimals);
                float noise is rounded away (see :func:`to_base_units`).
        """
        if not self.usd_pegged:
            raise ValueError(self.usd_conversion_error())
        return to_base_units(
            usd_amount, self.usdc_decimals, unit=f"{self.default_token.upper()} on {self.name}"
        )

    def usd_conversion_error(self) -> str:
        """The message for refusing to price this network in dollars.

        Shared by the two call sites so the payer and the merchant read the
        same sentence: `get_token_amount` and the client's requirements
        builder.
        """
        symbol = self.extra_config.get("native_asset") or self.default_token.upper()
        return (
            f"{self.name} settles in {symbol}, which is not pegged to the dollar, "
            f"so an amount written in USD cannot be converted with its "
            f"{self.usdc_decimals} decimals: $1.00 would be charged as 1 {symbol}. "
            f"Pass an explicit `asset` naming a dollar-pegged token this "
            f"network's facilitator settles (GET /supported lists them; on XRPL "
            f"that is USDC) together with its `token_decimals`, or price the "
            f"call in {symbol} units yourself."
        )

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

# Alternate spellings that resolve to a registry entry without becoming one:
# a caller may WRITE them, the SDK never puts them on the wire, and they are
# not separate networks (counts and listings stay honest). The facilitator
# keeps the same distinction - its FromStr takes `xrpl-mainnet` while
# everything it publishes says `xrpl` (x402-rs/src/network.rs:251,189).
_NETWORK_ALIASES: dict[str, str] = {
    "hedera": "hedera:mainnet",
    "hedera-testnet": "hedera:testnet",
    "skale": "skale-base",
    "skale-testnet": "skale-base-sepolia",
    # Renamed in 0.77.0: the facilitator advertises the mainnet as `xrpl`.
    "xrpl-mainnet": "xrpl",
}


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
        name: Network identifier (case-insensitive), or a registered alias

    Returns:
        NetworkConfig if found, None otherwise
    """
    key = name.lower()
    if key in _NETWORK_REGISTRY:
        return _NETWORK_REGISTRY[key]
    aliased = _NETWORK_ALIASES.get(key)
    return _NETWORK_REGISTRY.get(aliased) if aliased else None


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
# Token Helper Functions (Multi-Stablecoin Support)
# =============================================================================


def get_token_config(network_name: str, token_type: TokenType = "usdc") -> Optional[TokenConfig]:
    """
    Get token configuration for a specific network and token type.

    Args:
        network_name: Network identifier (e.g., 'base', 'ethereum')
        token_type: Token type (defaults to 'usdc')

    Returns:
        TokenConfig if the token is supported on this network, None otherwise

    Example:
        >>> config = get_token_config('ethereum', 'eurc')
        >>> if config:
        ...     print(f"EURC address: {config.address}")
    """
    network = get_network(network_name)
    if not network:
        return None

    # Check tokens dict first (multi-token support)
    if token_type in network.tokens:
        return network.tokens[token_type]

    # Fall back to the network's primary settlement token, derived from the
    # usdc_* fields. Covers both classic USDC and networks whose default token
    # is not Circle USDC (e.g. Robinhood -> USDG / "Global Dollar").
    if token_type == "usdc" or token_type == network.default_token:
        return TokenConfig(
            address=network.usdc_address,
            decimals=network.usdc_decimals,
            name=network.usdc_domain_name,
            version=network.usdc_domain_version,
        )

    return None


def get_supported_tokens(network_name: str) -> List[TokenType]:
    """
    Get list of supported token types for a network.

    Args:
        network_name: Network identifier

    Returns:
        List of supported TokenType values

    Example:
        >>> tokens = get_supported_tokens('ethereum')
        >>> print(tokens)  # ['usdc', 'eurc', 'ausd', 'pyusd']
    """
    network = get_network(network_name)
    if not network:
        return []

    # Get tokens from the tokens dict
    tokens: List[TokenType] = list(network.tokens.keys())

    # Always include the network's default settlement token (usually USDC,
    # but e.g. USDG on Robinhood) if the network has a primary asset configured
    default_token = network.default_token
    if default_token not in tokens and network.usdc_address:
        tokens.insert(0, default_token)

    return tokens


def is_token_supported(network_name: str, token_type: TokenType) -> bool:
    """
    Check if a specific token is supported on a network.

    Args:
        network_name: Network identifier
        token_type: Token type to check

    Returns:
        True if token is supported, False otherwise

    Example:
        >>> is_token_supported('ethereum', 'eurc')
        True
        >>> is_token_supported('celo', 'eurc')
        False
    """
    return get_token_config(network_name, token_type) is not None


def get_networks_by_token(token_type: TokenType) -> List[NetworkConfig]:
    """
    Get all networks that support a specific token type.

    Args:
        token_type: Token type to search for

    Returns:
        List of NetworkConfig instances that support the token

    Example:
        >>> networks = get_networks_by_token('eurc')
        >>> for n in networks:
        ...     print(n.name)  # ethereum, base, avalanche
    """
    result = []
    for network in _NETWORK_REGISTRY.values():
        if not network.enabled:
            continue
        if is_token_supported(network.name, token_type):
            result.append(network)
    return result


# =============================================================================
# CAIP-2 Utilities (x402 v2 support)
# =============================================================================

# CAIP-2 namespace to network mapping
_CAIP2_NAMESPACE_MAP = {
    "hedera": NetworkType.HEDERA,
    "eip155": NetworkType.EVM,
    "solana": NetworkType.SVM,
    "near": NetworkType.NEAR,
    "stellar": NetworkType.STELLAR,
    "algorand": NetworkType.ALGORAND,
    "sui": NetworkType.SUI,
}

# Network name to CAIP-2 format
_NETWORK_TO_CAIP2 = {
    "hedera:mainnet": "hedera:mainnet",
    "hedera:testnet": "hedera:testnet",
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
    "scroll": "eip155:534352",
    "skale-base": "eip155:1187947933",
    "skale-base-sepolia": "eip155:324705682",
    "robinhood": "eip155:4663",
    "robinhood-testnet": "eip155:46630",
    "arc": "eip155:5042",
    "arc-testnet": "eip155:5042002",
    # SVM chains (solana:genesisHash first 32 chars)
    "solana": "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp",
    "fogo": "solana:fogo",  # Placeholder - update when known
    # NEAR
    "near": "near:mainnet",
    # Stellar
    "stellar": "stellar:pubnet",
    # Algorand
    "algorand": "algorand:mainnet",
    "algorand-testnet": "algorand:testnet",
    # Sui
    "sui": "sui:mainnet",
    "sui-testnet": "sui:testnet",
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
    if namespace == "algorand":
        if reference == "mainnet":
            return "algorand"
        if reference == "testnet":
            return "algorand-testnet"
    if namespace == "sui":
        if reference == "mainnet":
            return "sui"
        if reference == "testnet":
            return "sui-testnet"

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

    Handles both v1 ("base") and v2 CAIP-2 ("eip155:8453") formats,
    plus common aliases (e.g. "skale" -> "skale-base").

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
    lowered = network.lower()
    return _NETWORK_ALIASES.get(lowered, lowered)
