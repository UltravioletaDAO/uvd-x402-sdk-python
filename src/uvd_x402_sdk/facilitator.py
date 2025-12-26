"""
Facilitator configuration and constants.

This module provides:
- Default facilitator URL
- Fee payer addresses for all supported non-EVM networks
- Helper functions to get the appropriate facilitator address for any network

Users of this SDK should NOT need to configure facilitator details manually.
All addresses are embedded as constants, extracted from the official facilitator.

Facilitator URL: https://facilitator.ultravioletadao.xyz
"""

from typing import Dict, Optional

from uvd_x402_sdk.networks.base import (
    NetworkType,
    get_network,
    is_caip2_format,
    normalize_network,
)


# =============================================================================
# Facilitator URL
# =============================================================================

DEFAULT_FACILITATOR_URL = "https://facilitator.ultravioletadao.xyz"

# Alternative facilitator URLs (for future use)
FACILITATOR_URLS = {
    "production": "https://facilitator.ultravioletadao.xyz",
}


# =============================================================================
# Fee Payer Addresses (Non-EVM chains require fee payer for gasless payments)
# =============================================================================

# Algorand fee payer addresses
ALGORAND_FEE_PAYER_MAINNET = "KIMS5H6QLCUDL65L5UBTOXDPWLMTS7N3AAC3I6B2NCONEI5QIVK7LH2C2I"
ALGORAND_FEE_PAYER_TESTNET = "5DPPDQNYUPCTXRZWRYSF3WPYU6RKAUR25F3YG4EKXQRHV5AUAI62H5GXL4"

# Solana fee payer addresses (also used for Fogo mainnet)
SOLANA_FEE_PAYER_MAINNET = "F742C4VfFLQ9zRQyithoj5229ZgtX2WqKCSFKgH2EThq"
SOLANA_FEE_PAYER_DEVNET = "6xNPewUdKRbEZDReQdpyfNUdgNg8QRc8Mt263T5GZSRv"

# Fogo (SVM) fee payer addresses
FOGO_FEE_PAYER_MAINNET = "F742C4VfFLQ9zRQyithoj5229ZgtX2WqKCSFKgH2EThq"
FOGO_FEE_PAYER_TESTNET = "6xNPewUdKRbEZDReQdpyfNUdgNg8QRc8Mt263T5GZSRv"

# NEAR fee payer addresses (account IDs)
NEAR_FEE_PAYER_MAINNET = "uvd-facilitator.near"
NEAR_FEE_PAYER_TESTNET = "uvd-facilitator.testnet"

# Stellar fee payer addresses (public keys)
STELLAR_FEE_PAYER_MAINNET = "GCHPGXJT2WFFRFCA5TV4G4E3PMMXLNIDUH27PKDYA4QJ2XGYZWGFZNHB"
STELLAR_FEE_PAYER_TESTNET = "GBBFZMLUJEZVI32EN4XA2KPP445XIBTMTRBLYWFIL556RDTHS2OWFQ2Z"


# =============================================================================
# EVM Facilitator Addresses (for reference - EVM uses EIP-3009, no fee payer needed)
# =============================================================================

# EVM facilitator wallet addresses (used for settlement, not fee payment)
EVM_FACILITATOR_MAINNET = "0x103040545AC5031A11E8C03dd11324C7333a13C7"
EVM_FACILITATOR_TESTNET = "0x34033041a5944B8F10f8E4D8496Bfb84f1A293A8"


# =============================================================================
# Network to Fee Payer Mapping
# =============================================================================

# Maps network names to their fee payer addresses
# EVM networks don't have fee payers (they use EIP-3009 transferWithAuthorization)
_FEE_PAYER_BY_NETWORK: Dict[str, str] = {
    # Algorand
    "algorand": ALGORAND_FEE_PAYER_MAINNET,
    "algorand-mainnet": ALGORAND_FEE_PAYER_MAINNET,
    "algorand-testnet": ALGORAND_FEE_PAYER_TESTNET,
    # Solana
    "solana": SOLANA_FEE_PAYER_MAINNET,
    "solana-mainnet": SOLANA_FEE_PAYER_MAINNET,
    "solana-devnet": SOLANA_FEE_PAYER_DEVNET,
    # Fogo (SVM)
    "fogo": FOGO_FEE_PAYER_MAINNET,
    "fogo-mainnet": FOGO_FEE_PAYER_MAINNET,
    "fogo-testnet": FOGO_FEE_PAYER_TESTNET,
    # NEAR
    "near": NEAR_FEE_PAYER_MAINNET,
    "near-mainnet": NEAR_FEE_PAYER_MAINNET,
    "near-testnet": NEAR_FEE_PAYER_TESTNET,
    # Stellar
    "stellar": STELLAR_FEE_PAYER_MAINNET,
    "stellar-mainnet": STELLAR_FEE_PAYER_MAINNET,
    "stellar-testnet": STELLAR_FEE_PAYER_TESTNET,
}

# CAIP-2 format mappings (x402 v2)
_FEE_PAYER_BY_CAIP2: Dict[str, str] = {
    # Algorand
    "algorand:mainnet": ALGORAND_FEE_PAYER_MAINNET,
    "algorand:testnet": ALGORAND_FEE_PAYER_TESTNET,
    # Solana
    "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp": SOLANA_FEE_PAYER_MAINNET,
    "solana:EtWTRABZaYq6iMfeYKouRu166VU2xqa1": SOLANA_FEE_PAYER_DEVNET,
    # Fogo (SVM)
    "fogo:mainnet": FOGO_FEE_PAYER_MAINNET,
    "fogo:testnet": FOGO_FEE_PAYER_TESTNET,
    # NEAR
    "near:mainnet": NEAR_FEE_PAYER_MAINNET,
    "near:testnet": NEAR_FEE_PAYER_TESTNET,
    # Stellar
    "stellar:pubnet": STELLAR_FEE_PAYER_MAINNET,
    "stellar:testnet": STELLAR_FEE_PAYER_TESTNET,
}


# =============================================================================
# Helper Functions
# =============================================================================


def get_fee_payer(network: str) -> Optional[str]:
    """
    Get the fee payer address for a network.

    Fee payers are only needed for non-EVM chains (Algorand, Solana, NEAR, Stellar).
    EVM chains use EIP-3009 transferWithAuthorization which is gasless by design.

    Args:
        network: Network identifier (v1 name or CAIP-2 format)
                Examples: "algorand", "algorand-mainnet", "algorand:mainnet"

    Returns:
        Fee payer address if applicable, None for EVM chains

    Example:
        >>> get_fee_payer("algorand")
        'KIMS5H6QLCUDL65L5UBTOXDPWLMTS7N3AAC3I6B2NCONEI5QIVK7LH2C2I'
        >>> get_fee_payer("solana")
        'F742C4VfFLQ9zRQyithoj5229ZgtX2WqKCSFKgH2EThq'
        >>> get_fee_payer("base")  # EVM chain
        None
    """
    # Check CAIP-2 format first
    if is_caip2_format(network):
        return _FEE_PAYER_BY_CAIP2.get(network)

    # Check v1 network name
    network_lower = network.lower()
    return _FEE_PAYER_BY_NETWORK.get(network_lower)


def get_facilitator_address(network: str) -> Optional[str]:
    """
    Alias for get_fee_payer() for backward compatibility.

    Args:
        network: Network identifier

    Returns:
        Facilitator/fee payer address if applicable
    """
    return get_fee_payer(network)


def requires_fee_payer(network: str) -> bool:
    """
    Check if a network requires a fee payer address.

    Args:
        network: Network identifier (v1 name or CAIP-2 format)

    Returns:
        True if network requires fee payer (non-EVM), False otherwise

    Example:
        >>> requires_fee_payer("algorand")
        True
        >>> requires_fee_payer("base")
        False
    """
    # Try to get network config
    try:
        normalized = normalize_network(network)
        net_config = get_network(normalized)
        if net_config:
            return net_config.network_type != NetworkType.EVM
    except ValueError:
        pass

    # Fall back to checking if we have a fee payer registered
    return get_fee_payer(network) is not None


def get_network_type_from_fee_payer(address: str) -> Optional[NetworkType]:
    """
    Determine network type from a fee payer address format.

    Args:
        address: Fee payer address

    Returns:
        NetworkType if recognizable, None otherwise

    Example:
        >>> get_network_type_from_fee_payer("KIMS5H6QLCUDL65L5UBTOXDPWLMTS7N3AAC3I6B2NCONEI5QIVK7LH2C2I")
        NetworkType.ALGORAND
        >>> get_network_type_from_fee_payer("F742C4VfFLQ9zRQyithoj5229ZgtX2WqKCSFKgH2EThq")
        NetworkType.SVM
    """
    if not address:
        return None

    # Algorand: 58 characters, base32 (A-Z2-7)
    if len(address) == 58 and address.isalnum():
        import re
        if re.match(r'^[A-Z2-7]+$', address):
            return NetworkType.ALGORAND

    # Solana/Fogo: 32-44 characters, base58
    if 32 <= len(address) <= 44:
        # Base58 uses alphanumeric chars except 0, O, I, l
        import re
        if re.match(r'^[A-HJ-NP-Za-km-z1-9]+$', address):
            return NetworkType.SVM

    # NEAR: ends with .near or .testnet
    if address.endswith('.near') or address.endswith('.testnet'):
        return NetworkType.NEAR

    # Stellar: starts with G, 56 characters
    if len(address) == 56 and address.startswith('G'):
        return NetworkType.STELLAR

    return None


def validate_fee_payer_for_network(network: str, address: str) -> bool:
    """
    Validate that a fee payer address format matches the network type.

    Args:
        network: Network identifier
        address: Fee payer address to validate

    Returns:
        True if address format is valid for network

    Example:
        >>> validate_fee_payer_for_network("algorand", "KIMS5H6...")
        True
        >>> validate_fee_payer_for_network("algorand", "F742C4V...")  # Solana address
        False
    """
    try:
        normalized = normalize_network(network)
        net_config = get_network(normalized)
        if not net_config:
            return False

        detected_type = get_network_type_from_fee_payer(address)
        if detected_type is None:
            return False

        # SVM includes both SOLANA and SVM types
        if net_config.network_type in (NetworkType.SVM, NetworkType.SOLANA):
            return detected_type == NetworkType.SVM

        return net_config.network_type == detected_type
    except ValueError:
        return False


def get_all_fee_payers() -> Dict[str, str]:
    """
    Get all registered fee payer addresses.

    Returns:
        Dictionary mapping network names to fee payer addresses

    Example:
        >>> payers = get_all_fee_payers()
        >>> for network, address in payers.items():
        ...     print(f"{network}: {address}")
    """
    return dict(_FEE_PAYER_BY_NETWORK)


def get_facilitator_url() -> str:
    """
    Get the default facilitator URL.

    Returns:
        Facilitator API URL

    Example:
        >>> url = get_facilitator_url()
        >>> print(url)
        'https://facilitator.ultravioletadao.xyz'
    """
    return DEFAULT_FACILITATOR_URL


# =============================================================================
# Payment Info Builder
# =============================================================================


def build_payment_info(
    network: str,
    pay_to: str,
    max_amount_required: str,
    description: str = "",
    resource: str = "",
    asset: Optional[str] = None,
    token_type: str = "usdc",
    extra: Optional[Dict] = None,
) -> Dict:
    """
    Build a payment info dict with all facilitator details pre-configured.

    This function automatically includes the correct fee payer address for
    non-EVM networks, so SDK users don't need to configure this manually.

    Args:
        network: Network identifier (e.g., "algorand", "solana", "base")
        pay_to: Recipient address
        max_amount_required: Maximum amount in token units (string)
        description: Optional payment description
        resource: Optional resource being purchased
        asset: Optional token address (defaults to USDC for network)
        token_type: Token type (default "usdc")
        extra: Additional extra fields to include

    Returns:
        Payment info dictionary ready for 402 response

    Example:
        >>> info = build_payment_info(
        ...     network="algorand",
        ...     pay_to="MERCHANT_ADDRESS...",
        ...     max_amount_required="1000000",
        ...     description="API access"
        ... )
        >>> # info includes facilitator fee payer automatically
    """
    # Get network config for defaults
    try:
        normalized = normalize_network(network)
        net_config = get_network(normalized)
    except ValueError:
        net_config = None

    # Build base payment info
    payment_info: Dict = {
        "network": network,
        "payTo": pay_to,
        "maxAmountRequired": max_amount_required,
    }

    if description:
        payment_info["description"] = description
    if resource:
        payment_info["resource"] = resource

    # Set asset (token address)
    if asset:
        payment_info["asset"] = asset
    elif net_config:
        payment_info["asset"] = net_config.usdc_address

    # Build extra field
    payment_extra: Dict = {}

    # Add token type info
    payment_extra["token"] = token_type

    # Add fee payer for non-EVM networks
    fee_payer = get_fee_payer(network)
    if fee_payer:
        payment_extra["feePayer"] = fee_payer

    # Merge user-provided extra
    if extra:
        payment_extra.update(extra)

    if payment_extra:
        payment_info["extra"] = payment_extra

    return payment_info
