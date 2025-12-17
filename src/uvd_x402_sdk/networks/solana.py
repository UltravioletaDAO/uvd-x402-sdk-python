"""
Solana Virtual Machine (SVM) network configurations.

This module supports all SVM-compatible chains:
- Solana (mainnet)
- Fogo (fast finality SVM)

All SVM chains use the same payment flow:
1. User creates a partially-signed VersionedTransaction
2. Transaction contains SPL token TransferChecked instruction
3. Facilitator is fee payer (user pays ZERO SOL/tokens)
4. Facilitator co-signs and submits transaction

Transaction Structure (flexible, facilitator v1.9.4+):
- SetComputeUnitLimit instruction (recommended: 20,000 units)
- SetComputeUnitPrice instruction (recommended: 100,000 microLamports)
- TransferChecked (USDC transfer)
- Optional: CreateAssociatedTokenAccount (if recipient ATA doesn't exist)
- Additional instructions may be added by wallets (e.g., Phantom memo)

The facilitator scans for the transfer instruction rather than requiring
fixed positions, allowing wallets like Phantom to add extra instructions.
The full signed transaction is sent to the facilitator, which uses it
exactly as signed (no reconstruction).
"""

import base64
from typing import Dict, Any, Optional

from uvd_x402_sdk.networks.base import (
    NetworkConfig,
    NetworkType,
    register_network,
)


# =============================================================================
# SVM Networks Configuration
# =============================================================================

# Solana Mainnet
SOLANA = NetworkConfig(
    name="solana",
    display_name="Solana",
    network_type=NetworkType.SVM,  # Use SVM type for all Solana-compatible chains
    chain_id=0,  # Non-EVM, no chain ID
    usdc_address="EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC SPL token mint
    usdc_decimals=6,
    usdc_domain_name="",  # Not applicable for SVM
    usdc_domain_version="",
    rpc_url="https://api.mainnet-beta.solana.com",
    enabled=True,
    extra_config={
        # Token program ID (standard SPL token program)
        "token_program_id": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
        # Associated Token Account program
        "ata_program_id": "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL",
        # Default compute units for transfer
        "compute_units": 20000,
        # Priority fee in microLamports (100k for fast landing on mainnet)
        "priority_fee_microlamports": 100_000,
        # Block explorer
        "explorer_url": "https://solscan.io",
        # Genesis hash (first 32 chars for CAIP-2)
        "genesis_hash": "5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp",
        # Network type identifier
        "svm_network": "solana",
    },
)

# Fogo (SVM chain with ultra-fast finality)
FOGO = NetworkConfig(
    name="fogo",
    display_name="Fogo",
    network_type=NetworkType.SVM,
    chain_id=0,  # Non-EVM, no chain ID
    usdc_address="uSd2czE61Evaf76RNbq4KPpXnkiL3irdzgLFUMe3NoG",  # Fogo USDC mint
    usdc_decimals=6,
    usdc_domain_name="",  # Not applicable for SVM
    usdc_domain_version="",
    rpc_url="https://rpc.fogo.nightly.app/",
    enabled=True,
    extra_config={
        # Token program ID (standard SPL token program - same as Solana)
        "token_program_id": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
        # Associated Token Account program (same as Solana)
        "ata_program_id": "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL",
        # Default compute units for transfer
        "compute_units": 20000,
        # Priority fee in microLamports (100k for fast landing)
        "priority_fee_microlamports": 100_000,
        # Block explorer (placeholder - update when available)
        "explorer_url": "https://explorer.fogo.nightly.app",
        # Network type identifier
        "svm_network": "fogo",
        # Fogo-specific: Ultra-fast finality (~400ms)
        "finality_ms": 400,
    },
)

# Register SVM networks
register_network(SOLANA)
register_network(FOGO)


# =============================================================================
# SVM-specific utilities
# =============================================================================


def is_svm_network(network_name: str) -> bool:
    """
    Check if a network is SVM-compatible.

    Args:
        network_name: Network name to check

    Returns:
        True if network uses SVM (Solana, Fogo, etc.)
    """
    from uvd_x402_sdk.networks.base import get_network, NetworkType

    network = get_network(network_name)
    if not network:
        return False
    return NetworkType.is_svm(network.network_type)


def get_svm_networks() -> list:
    """
    Get all registered SVM networks.

    Returns:
        List of SVM NetworkConfig instances
    """
    from uvd_x402_sdk.networks.base import list_networks, NetworkType

    return [
        n for n in list_networks(enabled_only=True)
        if NetworkType.is_svm(n.network_type)
    ]


def get_associated_token_address(owner: str, mint: str) -> str:
    """
    Derive the Associated Token Account (ATA) address for an owner and mint.

    Note: This is a placeholder. For actual derivation, use the solana-py library:

        from solders.pubkey import Pubkey
        from spl.token.constants import TOKEN_PROGRAM_ID, ASSOCIATED_TOKEN_PROGRAM_ID
        from spl.token.instructions import get_associated_token_address

    Args:
        owner: Owner's public key (base58)
        mint: Token mint address (base58)

    Returns:
        Associated token account address (base58)
    """
    # This would require solders/solana-py for actual implementation
    # Returning empty string as placeholder
    raise NotImplementedError(
        "ATA derivation requires solana-py library. "
        "Install with: pip install uvd-x402-sdk[solana]"
    )


def validate_svm_transaction_structure(transaction_base64: str) -> bool:
    """
    Validate that an SVM transaction has the correct structure for x402.

    The facilitator expects (flexible order, Dec 2024+):
    - VersionedTransaction with at least 3 instructions
    - SetComputeUnitLimit instruction (any position)
    - SetComputeUnitPrice instruction (any position)
    - TransferChecked instruction (SPL token)
    - Optional: CreateAssociatedTokenAccount instruction
    - Optional: Additional instructions from wallet (e.g., Phantom memo)

    Note: Wallets like Phantom may add extra instructions during signing.
    The facilitator v1.9.4+ handles this by scanning for the transfer
    instruction rather than requiring fixed positions.

    Args:
        transaction_base64: Base64-encoded serialized transaction

    Returns:
        True if structure is valid

    Raises:
        ValueError: If structure is invalid
    """
    try:
        tx_bytes = base64.b64decode(transaction_base64)
    except Exception as e:
        raise ValueError(f"Invalid base64 transaction: {e}")

    # Basic length validation
    # Minimum: version (1) + header (3) + accounts array (varies) + blockhash (32) + instructions
    if len(tx_bytes) < 50:
        raise ValueError(f"Transaction too short: {len(tx_bytes)} bytes")

    # Full validation requires solders/solana-py
    # For now, we just check basic structure
    return True


def validate_svm_payload(payload: Dict[str, Any]) -> bool:
    """
    Validate an SVM payment payload structure.

    The payload must contain a base64-encoded partially-signed transaction.

    Args:
        payload: Payload dictionary from x402 payment

    Returns:
        True if valid, raises ValueError if invalid
    """
    if "transaction" not in payload:
        raise ValueError("SVM payload missing 'transaction' field")

    transaction_b64 = payload["transaction"]

    try:
        tx_bytes = base64.b64decode(transaction_b64)
    except Exception as e:
        raise ValueError(f"Invalid base64 in transaction: {e}")

    # Basic length validation
    if len(tx_bytes) < 50:
        raise ValueError(f"Transaction too short: {len(tx_bytes)} bytes")

    return True


def is_valid_solana_address(address: str) -> bool:
    """
    Validate a Solana/SVM public key format.

    Solana addresses are base58-encoded 32-byte public keys.

    Args:
        address: Address to validate

    Returns:
        True if valid base58 address
    """
    if not address or not isinstance(address, str):
        return False

    # Base58 alphabet (no 0, O, I, l)
    base58_alphabet = set("123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz")

    if not all(c in base58_alphabet for c in address):
        return False

    # Typical Solana address length is 32-44 characters
    return 32 <= len(address) <= 44


# =============================================================================
# Transaction Building Utilities (for reference)
# =============================================================================

# These constants are useful for building transactions programmatically

# Compute Budget Program
COMPUTE_BUDGET_PROGRAM_ID = "ComputeBudget111111111111111111111111111111"

# SetComputeUnitLimit instruction discriminator
SET_COMPUTE_UNIT_LIMIT_DISCRIMINATOR = 2

# SetComputeUnitPrice instruction discriminator
SET_COMPUTE_UNIT_PRICE_DISCRIMINATOR = 3

# Default values for x402 transactions
DEFAULT_COMPUTE_UNITS = 20000
# Use 100k microlamports/CU for fast landing on mainnet
# Lower values (like 1) cause transactions to be deprioritized and time out
DEFAULT_PRIORITY_FEE_MICROLAMPORTS = 100_000
