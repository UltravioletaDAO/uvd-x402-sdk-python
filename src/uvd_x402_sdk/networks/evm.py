"""
EVM network configurations.

This module defines configurations for all supported EVM-compatible chains.
Each chain uses ERC-3009 TransferWithAuthorization for USDC transfers.

Important EIP-712 domain considerations:
- Most chains use 'USD Coin' as the domain name
- Celo, HyperEVM, Unichain, Monad use 'USDC' as the domain name
- BSC USDC uses 18 decimals (not standard 6)
"""

from uvd_x402_sdk.networks.base import (
    NetworkConfig,
    NetworkType,
    register_network,
)

# =============================================================================
# EVM Networks Configuration
# =============================================================================

# Base (Layer 2)
BASE = NetworkConfig(
    name="base",
    display_name="Base",
    network_type=NetworkType.EVM,
    chain_id=8453,
    usdc_address="0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
    usdc_decimals=6,
    usdc_domain_name="USD Coin",
    usdc_domain_version="2",
    rpc_url="https://mainnet.base.org",
    enabled=True,
)

# Ethereum Mainnet
ETHEREUM = NetworkConfig(
    name="ethereum",
    display_name="Ethereum",
    network_type=NetworkType.EVM,
    chain_id=1,
    usdc_address="0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
    usdc_decimals=6,
    usdc_domain_name="USD Coin",
    usdc_domain_version="2",
    rpc_url="https://eth.llamarpc.com",
    enabled=True,
)

# Polygon (PoS)
POLYGON = NetworkConfig(
    name="polygon",
    display_name="Polygon",
    network_type=NetworkType.EVM,
    chain_id=137,
    usdc_address="0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359",
    usdc_decimals=6,
    usdc_domain_name="USD Coin",
    usdc_domain_version="2",
    rpc_url="https://polygon-rpc.com",
    enabled=True,
)

# Arbitrum One
ARBITRUM = NetworkConfig(
    name="arbitrum",
    display_name="Arbitrum One",
    network_type=NetworkType.EVM,
    chain_id=42161,
    usdc_address="0xaf88d065e77c8cC2239327C5EDb3A432268e5831",
    usdc_decimals=6,
    usdc_domain_name="USD Coin",
    usdc_domain_version="2",
    rpc_url="https://arb1.arbitrum.io/rpc",
    enabled=True,
)

# Optimism
OPTIMISM = NetworkConfig(
    name="optimism",
    display_name="Optimism",
    network_type=NetworkType.EVM,
    chain_id=10,
    usdc_address="0x0b2C639c533813f4Aa9D7837CAf62653d097Ff85",
    usdc_decimals=6,
    usdc_domain_name="USD Coin",
    usdc_domain_version="2",
    rpc_url="https://mainnet.optimism.io",
    enabled=True,
)

# Avalanche C-Chain
AVALANCHE = NetworkConfig(
    name="avalanche",
    display_name="Avalanche C-Chain",
    network_type=NetworkType.EVM,
    chain_id=43114,
    usdc_address="0xB97EF9Ef8734C71904D8002F8b6Bc66Dd9c48a6E",
    usdc_decimals=6,
    usdc_domain_name="USD Coin",
    usdc_domain_version="2",
    rpc_url="https://avalanche-c-chain-rpc.publicnode.com",
    enabled=True,
)

# Celo
# NOTE: Celo uses 'USDC' (not 'USD Coin') for EIP-712 domain name
CELO = NetworkConfig(
    name="celo",
    display_name="Celo",
    network_type=NetworkType.EVM,
    chain_id=42220,
    usdc_address="0xcebA9300f2b948710d2653dD7B07f33A8B32118C",
    usdc_decimals=6,
    usdc_domain_name="USDC",  # Different from other chains!
    usdc_domain_version="2",
    rpc_url="https://forno.celo.org",
    enabled=True,
)

# HyperEVM (Hyperliquid)
# NOTE: HyperEVM uses 'USDC' (not 'USD Coin') for EIP-712 domain name
HYPEREVM = NetworkConfig(
    name="hyperevm",
    display_name="HyperEVM",
    network_type=NetworkType.EVM,
    chain_id=999,
    usdc_address="0xb88339CB7199b77E23DB6E890353E22632Ba630f",
    usdc_decimals=6,
    usdc_domain_name="USDC",  # Different from other chains!
    usdc_domain_version="2",
    rpc_url="https://rpc.hyperliquid.xyz/evm",
    enabled=True,
)

# Unichain
# NOTE: Unichain uses 'USDC' (not 'USD Coin') for EIP-712 domain name
UNICHAIN = NetworkConfig(
    name="unichain",
    display_name="Unichain",
    network_type=NetworkType.EVM,
    chain_id=130,
    usdc_address="0x078d782b760474a361dda0af3839290b0ef57ad6",
    usdc_decimals=6,
    usdc_domain_name="USDC",  # Different from other chains!
    usdc_domain_version="2",
    rpc_url="https://unichain-rpc.publicnode.com",
    enabled=True,
)

# Monad
# NOTE: Monad uses 'USDC' (not 'USD Coin') for EIP-712 domain name
MONAD = NetworkConfig(
    name="monad",
    display_name="Monad",
    network_type=NetworkType.EVM,
    chain_id=143,
    usdc_address="0x754704bc059f8c67012fed69bc8a327a5aafb603",
    usdc_decimals=6,
    usdc_domain_name="USDC",  # Different from other chains!
    usdc_domain_version="2",
    rpc_url="https://rpc.monad.xyz",
    enabled=True,
)

# BNB Smart Chain (BSC)
# NOTE: BSC USDC uses 18 decimals (not 6 like other chains)
# NOTE: Binance-Peg USDC doesn't support ERC-3009 - DISABLED
BSC = NetworkConfig(
    name="bsc",
    display_name="BNB Smart Chain",
    network_type=NetworkType.EVM,
    chain_id=56,
    usdc_address="0x8AC76a51cc950d9822D68b83fE1Ad97B32Cd580d",
    usdc_decimals=18,  # Different from other chains!
    usdc_domain_name="USD Coin",
    usdc_domain_version="2",
    rpc_url="https://binance.llamarpc.com",
    enabled=False,  # Disabled: Binance-Peg USDC doesn't support ERC-3009
)

# =============================================================================
# Register all EVM networks
# =============================================================================

_EVM_NETWORKS = [
    BASE,
    ETHEREUM,
    POLYGON,
    ARBITRUM,
    OPTIMISM,
    AVALANCHE,
    CELO,
    HYPEREVM,
    UNICHAIN,
    MONAD,
    BSC,
]

for network in _EVM_NETWORKS:
    register_network(network)


def get_usdc_domain_name(network_name: str) -> str:
    """
    Get the correct EIP-712 domain name for USDC on a network.

    Args:
        network_name: Network identifier

    Returns:
        Domain name string ('USD Coin' or 'USDC')
    """
    # Networks that use 'USDC' instead of 'USD Coin'
    usdc_domain_networks = {"celo", "hyperevm", "unichain", "monad"}

    if network_name.lower() in usdc_domain_networks:
        return "USDC"
    return "USD Coin"


def get_token_decimals(network_name: str) -> int:
    """
    Get USDC token decimals for a network.

    Args:
        network_name: Network identifier

    Returns:
        Number of decimals (6 for most chains, 18 for BSC)
    """
    if network_name.lower() == "bsc":
        return 18
    return 6
