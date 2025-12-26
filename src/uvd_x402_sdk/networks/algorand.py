"""
Algorand network configurations.

This module supports Algorand blockchain networks:
- Algorand mainnet
- Algorand testnet

Algorand uses ASA (Algorand Standard Assets) for USDC:
- Mainnet USDC ASA ID: 31566704
- Testnet USDC ASA ID: 10458941

Payment Flow:
1. User creates a signed ASA transfer transaction via Pera Wallet
2. Transaction transfers USDC from user to recipient
3. Facilitator submits the pre-signed transaction on-chain
4. User pays ZERO transaction fees (facilitator covers fees)

Transaction Structure:
- ASA TransferAsset transaction
- Signed by user wallet (Pera Wallet)
- Facilitator submits the signed transaction

Address Format:
- Algorand addresses are 58 characters, base32 encoded
- Example: AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAY5HFKQ
"""

import base64
import re
from typing import Any, Dict, Optional

from uvd_x402_sdk.networks.base import (
    NetworkConfig,
    NetworkType,
    register_network,
)


# =============================================================================
# Algorand Networks Configuration
# =============================================================================

# Algorand Mainnet
ALGORAND = NetworkConfig(
    name="algorand",
    display_name="Algorand",
    network_type=NetworkType.ALGORAND,
    chain_id=0,  # Non-EVM, no chain ID
    usdc_address="31566704",  # USDC ASA ID on mainnet
    usdc_decimals=6,
    usdc_domain_name="",  # Not applicable for Algorand
    usdc_domain_version="",
    rpc_url="https://mainnet-api.algonode.cloud",
    enabled=True,
    extra_config={
        # ASA (Algorand Standard Asset) details
        "usdc_asa_id": 31566704,
        # Block explorer
        "explorer_url": "https://allo.info",
        # Indexer endpoint (for account queries)
        "indexer_url": "https://mainnet-idx.algonode.cloud",
        # Network identifier
        "genesis_id": "mainnet-v1.0",
        # Genesis hash (for CAIP-2)
        "genesis_hash": "wGHE2Pwdvd7S12BL5FaOP20EGYesN73ktiC1qzkkit8=",
    },
)

# Algorand Testnet
ALGORAND_TESTNET = NetworkConfig(
    name="algorand-testnet",
    display_name="Algorand Testnet",
    network_type=NetworkType.ALGORAND,
    chain_id=0,  # Non-EVM, no chain ID
    usdc_address="10458941",  # USDC ASA ID on testnet
    usdc_decimals=6,
    usdc_domain_name="",  # Not applicable for Algorand
    usdc_domain_version="",
    rpc_url="https://testnet-api.algonode.cloud",
    enabled=True,
    extra_config={
        # ASA (Algorand Standard Asset) details
        "usdc_asa_id": 10458941,
        # Block explorer
        "explorer_url": "https://testnet.allo.info",
        # Indexer endpoint (for account queries)
        "indexer_url": "https://testnet-idx.algonode.cloud",
        # Network identifier
        "genesis_id": "testnet-v1.0",
        # Genesis hash
        "genesis_hash": "SGO1GKSzyE7IEPItTxCByw9x8FmnrCDexi9/cOUJOiI=",
    },
)

# Register Algorand networks
register_network(ALGORAND)
register_network(ALGORAND_TESTNET)


# =============================================================================
# Algorand-specific utilities
# =============================================================================


def is_algorand_network(network_name: str) -> bool:
    """
    Check if a network is Algorand.

    Args:
        network_name: Network name to check

    Returns:
        True if network is Algorand (mainnet or testnet)
    """
    from uvd_x402_sdk.networks.base import get_network, NetworkType

    network = get_network(network_name)
    if not network:
        return False
    return network.network_type == NetworkType.ALGORAND


def get_algorand_networks() -> list:
    """
    Get all registered Algorand networks.

    Returns:
        List of Algorand NetworkConfig instances
    """
    from uvd_x402_sdk.networks.base import list_networks, NetworkType

    return [
        n for n in list_networks(enabled_only=True)
        if n.network_type == NetworkType.ALGORAND
    ]


def is_valid_algorand_address(address: str) -> bool:
    """
    Validate an Algorand address format.

    Algorand addresses are 58 characters, base32 encoded (RFC 4648).
    They consist of uppercase letters A-Z and digits 2-7.

    Args:
        address: Address to validate

    Returns:
        True if valid Algorand address format
    """
    if not address or not isinstance(address, str):
        return False

    # Algorand addresses are exactly 58 characters
    if len(address) != 58:
        return False

    # Base32 alphabet (RFC 4648): A-Z and 2-7
    base32_pattern = re.compile(r'^[A-Z2-7]+$')
    return bool(base32_pattern.match(address))


def validate_algorand_payload(payload: Dict[str, Any]) -> bool:
    """
    Validate an Algorand payment payload structure.

    The payload must contain:
    - from: Sender's Algorand address
    - to: Recipient's Algorand address
    - amount: Amount in base units (microUSDC)
    - assetId: ASA ID for USDC
    - signedTxn: Base64-encoded signed transaction

    Args:
        payload: Payload dictionary from x402 payment

    Returns:
        True if valid, raises ValueError if invalid
    """
    required_fields = ["from", "to", "amount", "assetId", "signedTxn"]

    for field in required_fields:
        if field not in payload:
            raise ValueError(f"Algorand payload missing '{field}' field")

    # Validate addresses
    if not is_valid_algorand_address(payload["from"]):
        raise ValueError(f"Invalid 'from' address: {payload['from']}")
    if not is_valid_algorand_address(payload["to"]):
        raise ValueError(f"Invalid 'to' address: {payload['to']}")

    # Validate amount
    try:
        amount = int(payload["amount"])
        if amount <= 0:
            raise ValueError(f"Amount must be positive: {amount}")
    except (ValueError, TypeError) as e:
        raise ValueError(f"Invalid amount: {payload['amount']}") from e

    # Validate assetId
    try:
        asset_id = int(payload["assetId"])
        if asset_id <= 0:
            raise ValueError(f"Asset ID must be positive: {asset_id}")
    except (ValueError, TypeError) as e:
        raise ValueError(f"Invalid assetId: {payload['assetId']}") from e

    # Validate signedTxn is valid base64
    try:
        signed_txn = payload["signedTxn"]
        tx_bytes = base64.b64decode(signed_txn)
        if len(tx_bytes) < 50:
            raise ValueError(f"Signed transaction too short: {len(tx_bytes)} bytes")
    except Exception as e:
        raise ValueError(f"Invalid signedTxn (not valid base64): {e}") from e

    return True


def get_explorer_tx_url(network_name: str, tx_id: str) -> Optional[str]:
    """
    Get block explorer URL for a transaction.

    Args:
        network_name: Network name ('algorand' or 'algorand-testnet')
        tx_id: Transaction ID

    Returns:
        Explorer URL or None if network not found
    """
    from uvd_x402_sdk.networks.base import get_network

    network = get_network(network_name)
    if not network or network.network_type != NetworkType.ALGORAND:
        return None

    explorer_url = network.extra_config.get("explorer_url", "https://allo.info")
    return f"{explorer_url}/tx/{tx_id}"


def get_explorer_address_url(network_name: str, address: str) -> Optional[str]:
    """
    Get block explorer URL for an address.

    Args:
        network_name: Network name ('algorand' or 'algorand-testnet')
        address: Algorand address

    Returns:
        Explorer URL or None if network not found
    """
    from uvd_x402_sdk.networks.base import get_network

    network = get_network(network_name)
    if not network or network.network_type != NetworkType.ALGORAND:
        return None

    explorer_url = network.extra_config.get("explorer_url", "https://allo.info")
    return f"{explorer_url}/account/{address}"


def get_usdc_asa_id(network_name: str) -> Optional[int]:
    """
    Get the USDC ASA ID for an Algorand network.

    Args:
        network_name: Network name ('algorand' or 'algorand-testnet')

    Returns:
        USDC ASA ID or None if network not found
    """
    from uvd_x402_sdk.networks.base import get_network

    network = get_network(network_name)
    if not network or network.network_type != NetworkType.ALGORAND:
        return None

    # Try extra_config first, then fall back to usdc_address
    asa_id = network.extra_config.get("usdc_asa_id")
    if asa_id:
        return int(asa_id)

    # Parse from usdc_address (which stores the ASA ID as string)
    try:
        return int(network.usdc_address)
    except (ValueError, TypeError):
        return None
