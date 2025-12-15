"""
Stellar network configuration.

Stellar uses Soroban smart contracts for USDC transfers:
1. User signs a SorobanAuthorizationEntry (XDR format)
2. Authorization contains transfer function invocation
3. Facilitator wraps in fee-bump transaction
4. Facilitator pays all XLM fees - user pays ZERO XLM

Stellar USDC Details:
- Uses SAC (Soroban Asset Contract) for the Circle-issued USDC
- 7 decimals (stroops), different from other chains (6 decimals)
- Freighter wallet required for signing (Bitget doesn't support Stellar)
"""

from uvd_x402_sdk.networks.base import (
    NetworkConfig,
    NetworkType,
    register_network,
)

# Stellar Mainnet
STELLAR = NetworkConfig(
    name="stellar",
    display_name="Stellar",
    network_type=NetworkType.STELLAR,
    chain_id=0,  # Non-EVM, no chain ID
    # Soroban Asset Contract (SAC) for USDC
    usdc_address="CCW67TSZV3SSS2HXMBQ5JFGCKJNXKZM7UQUWUZPUTHXSTZLEO7SJMI75",
    usdc_decimals=7,  # Stellar uses 7 decimals (stroops)
    usdc_domain_name="",  # Not applicable for Stellar
    usdc_domain_version="",
    rpc_url="https://horizon.stellar.org",
    enabled=True,
    extra_config={
        # Network passphrase for signing
        "network_passphrase": "Public Global Stellar Network ; September 2015",
        # Soroban RPC endpoint (different from Horizon)
        "soroban_rpc_url": "https://mainnet.sorobanrpc.com",
        # Block explorer
        "explorer_url": "https://stellar.expert/explorer/public",
        # Circle USDC issuer (G... address)
        "usdc_issuer": "GA5ZSEJYB37JRC5AVCIA5MOP4RHTM335X2KGX3IHOJAPP5RE34K4KZVN",
        # Default ledger validity (~5 minutes, ~60 ledgers)
        "default_ledger_validity": 60,
    },
)

# Register Stellar network
register_network(STELLAR)


# =============================================================================
# Stellar-specific utilities
# =============================================================================


def stroops_to_usd(stroops: int) -> float:
    """
    Convert stroops (7 decimals) to USD amount.

    Args:
        stroops: Amount in stroops

    Returns:
        USD amount
    """
    return stroops / 10_000_000


def usd_to_stroops(usd: float) -> int:
    """
    Convert USD amount to stroops (7 decimals).

    Args:
        usd: USD amount

    Returns:
        Amount in stroops
    """
    return int(usd * 10_000_000)


def is_valid_stellar_address(address: str) -> bool:
    """
    Validate a Stellar public key format.

    Args:
        address: Stellar address to validate

    Returns:
        True if valid G... address (56 characters, starts with G)
    """
    return (
        isinstance(address, str)
        and len(address) == 56
        and address.startswith("G")
    )


def is_valid_contract_address(address: str) -> bool:
    """
    Validate a Soroban contract address format.

    Args:
        address: Contract address to validate

    Returns:
        True if valid C... address (56 characters, starts with C)
    """
    return (
        isinstance(address, str)
        and len(address) == 56
        and address.startswith("C")
    )


def calculate_expiration_ledger(current_ledger: int, validity_ledgers: int = 60) -> int:
    """
    Calculate expiration ledger for authorization.

    Args:
        current_ledger: Current ledger sequence from RPC
        validity_ledgers: Number of ledgers the auth is valid (~5 minutes at 5s/ledger)

    Returns:
        Expiration ledger number
    """
    return current_ledger + validity_ledgers
