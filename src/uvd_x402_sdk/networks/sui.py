"""
Sui network configurations.

This module supports Sui blockchain for x402 payments using sponsored transactions:
- Sui (mainnet)
- Sui Testnet

Supported Tokens:
- USDC: Native Sui USDC

All Sui chains use the same payment flow:
1. User creates a programmable transaction for token transfer
2. User signs the transaction
3. Transaction is sent to the facilitator with the user's signature
4. Facilitator sponsors the transaction (pays gas in SUI)
5. Facilitator adds sponsor signature and submits to Sui network

Transaction Structure:
- Programmable Transaction Block (PTB) for USDC transfer
- TransferObjects or SplitCoins + TransferObjects commands
- Facilitator is gas sponsor (user pays ZERO SUI)

Key differences from other chains:
- Uses Move-based programmable transactions
- BCS (Binary Canonical Serialization) encoding
- 66-character addresses (0x + 64 hex)
- Coin types in format: package::module::type
"""

import base64
from typing import Dict, Any, Optional

from uvd_x402_sdk.networks.base import (
    NetworkConfig,
    NetworkType,
    TokenConfig,
    register_network,
)

# Sui fee payer addresses (facilitator wallets)
try:
    from uvd_x402_sdk.facilitator import (
        SUI_FEE_PAYER_MAINNET,
        SUI_FEE_PAYER_TESTNET,
        get_fee_payer,
    )
except ImportError:
    # Fallback if facilitator module not loaded yet
    SUI_FEE_PAYER_MAINNET = "0xe7bbf2b13f7d72714760aa16e024fa1b35a978793f9893d0568a4fbf356a764a"
    SUI_FEE_PAYER_TESTNET = "0xabbd16a2fab2a502c9cfe835195a6fc7d70bfc27cffb40b8b286b52a97006e67"
    get_fee_payer = None  # type: ignore


# =============================================================================
# Sui Networks Configuration
# =============================================================================

# USDC coin types on Sui
SUI_USDC_COIN_TYPE_MAINNET = "0xdba34672e30cb065b1f93e3ab55318768fd6fef66c15942c9f7cb846e2f900e7::usdc::USDC"
SUI_USDC_COIN_TYPE_TESTNET = "0xa1ec7fc00a6f40db9693ad1415d0c193ad3906494428cf252621037bd7117e29::usdc::USDC"

# Sui Mainnet
SUI = NetworkConfig(
    name="sui",
    display_name="Sui",
    network_type=NetworkType.SUI,
    chain_id=0,  # Non-EVM, no chain ID
    usdc_address=SUI_USDC_COIN_TYPE_MAINNET,
    usdc_decimals=6,
    usdc_domain_name="",  # Not applicable for Sui
    usdc_domain_version="",
    rpc_url="https://fullnode.mainnet.sui.io:443",
    enabled=True,
    tokens={
        "usdc": TokenConfig(
            address=SUI_USDC_COIN_TYPE_MAINNET,
            decimals=6,
            name="",  # Not applicable for Sui
            version="",
        ),
    },
    extra_config={
        # Coin type for USDC (package::module::type format)
        "usdc_coin_type": SUI_USDC_COIN_TYPE_MAINNET,
        # Fee payer (facilitator) address
        "fee_payer": SUI_FEE_PAYER_MAINNET,
        # Block explorer
        "explorer_url": "https://suiscan.xyz",
        # Network identifier
        "sui_network": "mainnet",
        # Gas budget (in MIST, 1 SUI = 1e9 MIST)
        "default_gas_budget": 10_000_000,  # 0.01 SUI
    },
)

# Sui Testnet
SUI_TESTNET = NetworkConfig(
    name="sui-testnet",
    display_name="Sui Testnet",
    network_type=NetworkType.SUI,
    chain_id=0,  # Non-EVM, no chain ID
    usdc_address=SUI_USDC_COIN_TYPE_TESTNET,
    usdc_decimals=6,
    usdc_domain_name="",  # Not applicable for Sui
    usdc_domain_version="",
    rpc_url="https://fullnode.testnet.sui.io:443",
    enabled=True,
    tokens={
        "usdc": TokenConfig(
            address=SUI_USDC_COIN_TYPE_TESTNET,
            decimals=6,
            name="",  # Not applicable for Sui
            version="",
        ),
    },
    extra_config={
        # Coin type for USDC (package::module::type format)
        "usdc_coin_type": SUI_USDC_COIN_TYPE_TESTNET,
        # Fee payer (facilitator) address
        "fee_payer": SUI_FEE_PAYER_TESTNET,
        # Block explorer
        "explorer_url": "https://suiscan.xyz/testnet",
        # Network identifier
        "sui_network": "testnet",
        # Gas budget (in MIST, 1 SUI = 1e9 MIST)
        "default_gas_budget": 10_000_000,  # 0.01 SUI
    },
)

# Register Sui networks
register_network(SUI)
register_network(SUI_TESTNET)


# =============================================================================
# Sui-specific utilities
# =============================================================================


def is_sui_network(network_name: str) -> bool:
    """
    Check if a network is Sui-based.

    Args:
        network_name: Network name to check

    Returns:
        True if network uses Sui
    """
    from uvd_x402_sdk.networks.base import get_network, NetworkType

    network = get_network(network_name)
    if not network:
        return False
    return NetworkType.is_sui(network.network_type)


def get_sui_networks() -> list:
    """
    Get all registered Sui networks.

    Returns:
        List of Sui NetworkConfig instances
    """
    from uvd_x402_sdk.networks.base import list_networks, NetworkType

    return [
        n for n in list_networks(enabled_only=True)
        if NetworkType.is_sui(n.network_type)
    ]


def is_valid_sui_address(address: str) -> bool:
    """
    Validate a Sui address format.

    Sui addresses are 66 characters: 0x + 64 hex characters.

    Args:
        address: Address to validate

    Returns:
        True if valid Sui address
    """
    if not address or not isinstance(address, str):
        return False

    # Sui addresses start with 0x and have 64 hex characters after
    if not address.startswith("0x"):
        return False

    hex_part = address[2:]
    if len(hex_part) != 64:
        return False

    # Validate hex characters
    try:
        int(hex_part, 16)
        return True
    except ValueError:
        return False


def is_valid_sui_coin_type(coin_type: str) -> bool:
    """
    Validate a Sui coin type format.

    Sui coin types follow format: package_address::module::type
    Example: 0xdba34672e30cb065b1f93e3ab55318768fd6fef66c15942c9f7cb846e2f900e7::usdc::USDC

    Args:
        coin_type: Coin type to validate

    Returns:
        True if valid coin type format
    """
    if not coin_type or not isinstance(coin_type, str):
        return False

    parts = coin_type.split("::")
    if len(parts) != 3:
        return False

    package_addr, module_name, type_name = parts

    # Validate package address
    if not is_valid_sui_address(package_addr):
        return False

    # Validate module and type names (alphanumeric + underscore)
    import re
    if not re.match(r'^[a-zA-Z_][a-zA-Z0-9_]*$', module_name):
        return False
    if not re.match(r'^[a-zA-Z_][a-zA-Z0-9_]*$', type_name):
        return False

    return True


def validate_sui_payload(payload: Dict[str, Any]) -> bool:
    """
    Validate a Sui payment payload structure.

    The payload must contain:
    - transactionBytes: BCS-encoded TransactionData (base64)
    - senderSignature: User's signature (base64)
    - from: Sender address
    - to: Recipient address
    - amount: Transfer amount (string)

    Args:
        payload: Payload dictionary from x402 payment

    Returns:
        True if valid, raises ValueError if invalid
    """
    required_fields = ["transactionBytes", "senderSignature", "from", "to", "amount"]
    for field in required_fields:
        if field not in payload:
            raise ValueError(f"Sui payload missing '{field}' field")

    # Validate addresses
    if not is_valid_sui_address(payload["from"]):
        raise ValueError(f"Invalid 'from' address: {payload['from']}")

    if not is_valid_sui_address(payload["to"]):
        raise ValueError(f"Invalid 'to' address: {payload['to']}")

    # Validate transactionBytes is valid base64
    try:
        tx_bytes = base64.b64decode(payload["transactionBytes"])
        if len(tx_bytes) < 50:
            raise ValueError(f"Transaction bytes too short: {len(tx_bytes)} bytes")
    except Exception as e:
        raise ValueError(f"Invalid transactionBytes: {e}")

    # Validate senderSignature is valid base64
    try:
        sig_bytes = base64.b64decode(payload["senderSignature"])
        # Sui signatures are typically 64-65 bytes (Ed25519 or Secp256k1)
        if len(sig_bytes) < 64:
            raise ValueError(f"Signature too short: {len(sig_bytes)} bytes")
    except Exception as e:
        raise ValueError(f"Invalid senderSignature: {e}")

    # Validate amount is numeric
    try:
        amount = int(payload["amount"])
        if amount <= 0:
            raise ValueError(f"Amount must be positive: {amount}")
    except (ValueError, TypeError) as e:
        raise ValueError(f"Invalid amount: {e}")

    return True


def get_sui_fee_payer(network_name: str = "sui") -> str:
    """
    Get the fee payer (sponsor) address for a Sui network.

    The fee payer is the facilitator address that sponsors transaction gas.

    Args:
        network_name: Network name ('sui', 'sui-mainnet', 'sui-testnet')

    Returns:
        Fee payer address for the specified network

    Example:
        >>> get_sui_fee_payer("sui")
        '0xe7bbf2b13f7d72714760aa16e024fa1b35a978793f9893d0568a4fbf356a764a'
        >>> get_sui_fee_payer("sui-testnet")
        '0xabbd16a2fab2a502c9cfe835195a6fc7d70bfc27cffb40b8b286b52a97006e67'
    """
    # Use facilitator module if available
    if get_fee_payer is not None:
        fee_payer = get_fee_payer(network_name)
        if fee_payer:
            return fee_payer

    # Fallback to direct lookup
    network_lower = network_name.lower()
    if "testnet" in network_lower:
        return SUI_FEE_PAYER_TESTNET
    return SUI_FEE_PAYER_MAINNET


def get_sui_usdc_coin_type(network_name: str = "sui") -> str:
    """
    Get the USDC coin type for a Sui network.

    Args:
        network_name: Network name ('sui', 'sui-mainnet', 'sui-testnet')

    Returns:
        USDC coin type in package::module::type format

    Example:
        >>> get_sui_usdc_coin_type("sui")
        '0xdba34672e30cb065b1f93e3ab55318768fd6fef66c15942c9f7cb846e2f900e7::usdc::USDC'
    """
    network_lower = network_name.lower()
    if "testnet" in network_lower:
        return SUI_USDC_COIN_TYPE_TESTNET
    return SUI_USDC_COIN_TYPE_MAINNET


# =============================================================================
# Sui Transaction Building Utilities (for reference)
# =============================================================================

# Default gas budget in MIST (1 SUI = 1e9 MIST)
DEFAULT_GAS_BUDGET = 10_000_000  # 0.01 SUI

# Sui object ID length
SUI_OBJECT_ID_LENGTH = 66  # 0x + 64 hex chars

# Sui digest length (base58 encoded)
SUI_DIGEST_LENGTH = 44  # base58 encoded 32-byte digest


def format_sui_amount(usd_amount: float, decimals: int = 6) -> int:
    """
    Convert USD amount to Sui token base units.

    Args:
        usd_amount: Amount in USD (e.g., 10.50)
        decimals: Token decimals (6 for USDC)

    Returns:
        Amount in base units
    """
    return int(usd_amount * (10 ** decimals))


def parse_sui_amount(base_units: int, decimals: int = 6) -> float:
    """
    Convert Sui token base units to USD amount.

    Args:
        base_units: Amount in base units
        decimals: Token decimals (6 for USDC)

    Returns:
        Amount in USD
    """
    return base_units / (10 ** decimals)
