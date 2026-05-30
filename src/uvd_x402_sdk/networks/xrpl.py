"""
XRP Ledger (XRPL) network configuration.

XRPL uses native XRP and pre-signed Payment transactions for gasless payments:
1. User signs an XRPL Payment transaction (sending native XRP to the merchant)
2. The signed transaction blob is sent to the facilitator
3. Facilitator submits the transaction and pays the network/relay fee
4. User pays ZERO additional fees - the facilitator sponsors submission

XRPL Asset Details:
- Native asset: XRP (symbol XRP)
- 6 decimals: amounts are denominated in "drops" (1 XRP = 1,000,000 drops)
- NO token contract address - XRP is the native asset of the ledger

XRPL has NO CAIP-2 representation. Use the v1 network strings only:
- "xrpl-mainnet"
- "xrpl-testnet"
"""

from uvd_x402_sdk.networks.base import (
    NetworkConfig,
    NetworkType,
    register_network,
)

# XRPL fee payer addresses are defined in uvd_x402_sdk.facilitator
# Import here for convenience
try:
    from uvd_x402_sdk.facilitator import (
        XRPL_FEE_PAYER_MAINNET,
        XRPL_FEE_PAYER_TESTNET,
        get_fee_payer,
    )
except ImportError:
    # Fallback if facilitator module not loaded yet
    XRPL_FEE_PAYER_MAINNET = "rfADKkVXBNqK3z72tVSS3LVzAR3psYkonp"
    XRPL_FEE_PAYER_TESTNET = "rGhTioKAFHe75KgVnQtacRiKFuPv28Wbwk"
    get_fee_payer = None  # type: ignore


# XRPL Mainnet
XRPL_MAINNET = NetworkConfig(
    name="xrpl-mainnet",
    display_name="XRP Ledger",
    network_type=NetworkType.XRPL,
    chain_id=0,  # Non-EVM, no chain ID
    # Native XRP has no token contract address
    usdc_address="",  # Native asset (XRP), not a token contract
    usdc_decimals=6,  # XRP uses 6 decimals (drops): 1 XRP = 1,000,000 drops
    usdc_domain_name="",  # Not applicable for XRPL
    usdc_domain_version="",
    rpc_url="https://xrplcluster.com",
    enabled=True,
    extra_config={
        # Native asset symbol
        "native_asset": "XRP",
        # Block explorer
        "explorer_url": "https://livenet.xrpl.org/accounts",
        # x402 network name (facilitator expects this format)
        "x402_network": "xrpl-mainnet",
    },
)

# XRPL Testnet
XRPL_TESTNET = NetworkConfig(
    name="xrpl-testnet",
    display_name="XRP Ledger Testnet",
    network_type=NetworkType.XRPL,
    chain_id=0,  # Non-EVM, no chain ID
    usdc_address="",  # Native asset (XRP), not a token contract
    usdc_decimals=6,  # XRP uses 6 decimals (drops): 1 XRP = 1,000,000 drops
    usdc_domain_name="",  # Not applicable for XRPL
    usdc_domain_version="",
    rpc_url="https://s.altnet.rippletest.net:51234",
    enabled=True,
    extra_config={
        "native_asset": "XRP",
        "explorer_url": "https://testnet.xrpl.org/accounts",
        "x402_network": "xrpl-testnet",
    },
)

# Register XRPL networks
register_network(XRPL_MAINNET)
register_network(XRPL_TESTNET)


# =============================================================================
# XRPL-specific utilities
# =============================================================================


def drops_to_xrp(drops: int) -> float:
    """
    Convert drops (6 decimals) to XRP amount.

    Args:
        drops: Amount in drops

    Returns:
        XRP amount (1 XRP = 1,000,000 drops)
    """
    return drops / 1_000_000


def xrp_to_drops(xrp: float) -> int:
    """
    Convert an XRP amount to drops (6 decimals).

    Args:
        xrp: XRP amount

    Returns:
        Amount in drops (1 XRP = 1,000,000 drops)
    """
    return int(xrp * 1_000_000)


def is_valid_xrpl_address(address: str) -> bool:
    """
    Validate an XRPL classic address format.

    XRPL classic addresses:
    - Start with 'r'
    - Are 25-35 characters
    - Use base58 (Ripple alphabet, excludes 0, O, I, l)

    Args:
        address: XRPL address to validate

    Returns:
        True if the address looks like a valid XRPL classic address
    """
    if not isinstance(address, str):
        return False
    if not address.startswith("r"):
        return False
    if not (25 <= len(address) <= 35):
        return False
    ripple_alphabet = set(
        "rpshnaf39wBUDNEGHJKLM4PQRST7VWXYZ2bcdeCg65jkm8oFqi1tuvAxyz"
    )
    return all(c in ripple_alphabet for c in address)


def get_xrpl_fee_payer(network_name: str = "xrpl-mainnet") -> str:
    """
    Get the fee payer (facilitator) address for an XRPL network.

    The fee payer is the facilitator address that submits the pre-signed
    Payment transaction and pays the network/relay fee.

    Args:
        network_name: Network name ('xrpl-mainnet' or 'xrpl-testnet')

    Returns:
        Fee payer address for the specified network

    Example:
        >>> get_xrpl_fee_payer("xrpl-mainnet")
        'rfADKkVXBNqK3z72tVSS3LVzAR3psYkonp'
        >>> get_xrpl_fee_payer("xrpl-testnet")
        'rGhTioKAFHe75KgVnQtacRiKFuPv28Wbwk'
    """
    # Use facilitator module if available
    if get_fee_payer is not None:
        fee_payer = get_fee_payer(network_name)
        if fee_payer:
            return fee_payer

    # Fallback to direct lookup
    network_lower = network_name.lower()
    if "testnet" in network_lower:
        return XRPL_FEE_PAYER_TESTNET
    return XRPL_FEE_PAYER_MAINNET
