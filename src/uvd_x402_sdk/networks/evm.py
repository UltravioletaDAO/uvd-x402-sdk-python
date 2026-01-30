"""
EVM network configurations.

This module defines configurations for all supported EVM-compatible chains.
Each chain uses ERC-3009 TransferWithAuthorization for USDC transfers.

Important EIP-712 domain considerations:
- Most chains use 'USD Coin' as the domain name
- Celo, HyperEVM, Unichain, Monad use 'USDC' as the domain name

Multi-token support:
- USDC: All chains (6 decimals)
- EURC: Ethereum, Base, Avalanche (6 decimals)
- AUSD: Ethereum, Arbitrum, Avalanche, Polygon, Monad (6 decimals)
- PYUSD: Ethereum (6 decimals)
- USDT: Arbitrum, Optimism, Celo (6 decimals) - USDT0 omnichain via LayerZero
"""

from uvd_x402_sdk.networks.base import (
    NetworkConfig,
    NetworkType,
    TokenConfig,
    register_network,
)

# =============================================================================
# EVM Networks Configuration
# =============================================================================

# Base (Layer 2) - supports USDC, EURC
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
    tokens={
        "usdc": TokenConfig(
            address="0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
            decimals=6,
            name="USD Coin",
            version="2",
        ),
        "eurc": TokenConfig(
            address="0x60a3E35Cc302bFA44Cb288Bc5a4F316Fdb1adb42",
            decimals=6,
            name="EURC",
            version="2",
        ),
    },
)

# Ethereum Mainnet - supports USDC, EURC, AUSD, PYUSD
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
    tokens={
        "usdc": TokenConfig(
            address="0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
            decimals=6,
            name="USD Coin",
            version="2",
        ),
        "eurc": TokenConfig(
            address="0x1aBaEA1f7C830bD89Acc67eC4af516284b1bC33c",
            decimals=6,
            name="Euro Coin",
            version="2",
        ),
        "ausd": TokenConfig(
            address="0x00000000eFE302BEAA2b3e6e1b18d08D69a9012a",
            decimals=6,
            name="Agora Dollar",
            version="1",
        ),
        "pyusd": TokenConfig(
            address="0x6c3ea9036406852006290770BEdFcAbA0e23A0e8",
            decimals=6,
            name="PayPal USD",
            version="1",
        ),
    },
)

# Polygon (PoS) - supports USDC, AUSD
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
    tokens={
        "usdc": TokenConfig(
            address="0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359",
            decimals=6,
            name="USD Coin",
            version="2",
        ),
        "ausd": TokenConfig(
            address="0x00000000eFE302BEAA2b3e6e1b18d08D69a9012a",
            decimals=6,
            name="Agora Dollar",
            version="1",
        ),
    },
)

# Arbitrum One - supports USDC, AUSD, USDT
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
    tokens={
        "usdc": TokenConfig(
            address="0xaf88d065e77c8cC2239327C5EDb3A432268e5831",
            decimals=6,
            name="USD Coin",
            version="2",
        ),
        "ausd": TokenConfig(
            address="0x00000000eFE302BEAA2b3e6e1b18d08D69a9012a",
            decimals=6,
            name="Agora Dollar",
            version="1",
        ),
        "usdt": TokenConfig(
            address="0xFd086bC7CD5C481DCC9C85ebE478A1C0b69FCbb9",
            decimals=6,
            name="USD₮0",
            version="1",
        ),
    },
)

# Optimism - supports USDC, USDT
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
    tokens={
        "usdc": TokenConfig(
            address="0x0b2C639c533813f4Aa9D7837CAf62653d097Ff85",
            decimals=6,
            name="USD Coin",
            version="2",
        ),
        "usdt": TokenConfig(
            address="0x01bff41798a0bcf287b996046ca68b395dbc1071",
            decimals=6,
            name="USD₮0",
            version="1",
        ),
    },
)

# Avalanche C-Chain - supports USDC, EURC, AUSD
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
    tokens={
        "usdc": TokenConfig(
            address="0xB97EF9Ef8734C71904D8002F8b6Bc66Dd9c48a6E",
            decimals=6,
            name="USD Coin",
            version="2",
        ),
        "eurc": TokenConfig(
            address="0xC891EB4cbdEFf6e073e859e987815Ed1505c2ACD",
            decimals=6,
            name="Euro Coin",
            version="2",
        ),
        "ausd": TokenConfig(
            address="0x00000000eFE302BEAA2b3e6e1b18d08D69a9012a",
            decimals=6,
            name="Agora Dollar",
            version="1",
        ),
    },
)

# Celo - supports USDC, USDT
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
    tokens={
        "usdc": TokenConfig(
            address="0xcebA9300f2b948710d2653dD7B07f33A8B32118C",
            decimals=6,
            name="USDC",  # Celo uses "USDC" not "USD Coin"
            version="2",
        ),
        "usdt": TokenConfig(
            address="0x48065fbBE25f71C9282ddf5e1cD6D6A887483D5e",
            decimals=6,
            name="Tether USD",  # Celo USDT uses "Tether USD"
            version="1",
        ),
    },
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

# Monad - supports USDC, AUSD
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
    tokens={
        "usdc": TokenConfig(
            address="0x754704bc059f8c67012fed69bc8a327a5aafb603",
            decimals=6,
            name="USDC",
            version="2",
        ),
        "ausd": TokenConfig(
            address="0x00000000eFE302BEAA2b3e6e1b18d08D69a9012a",
            decimals=6,
            name="Agora Dollar",
            version="1",
        ),
    },
)

# Scroll (zkEVM Layer 2)
SCROLL = NetworkConfig(
    name="scroll",
    display_name="Scroll",
    network_type=NetworkType.EVM,
    chain_id=534352,
    usdc_address="0x06eFdBFf2a14a7c8E15944D1F4A48F9F95F663A4",
    usdc_decimals=6,
    usdc_domain_name="USD Coin",
    usdc_domain_version="2",
    rpc_url="https://rpc.scroll.io",
    enabled=True,
    tokens={
        "usdc": TokenConfig(
            address="0x06eFdBFf2a14a7c8E15944D1F4A48F9F95F663A4",
            decimals=6,
            name="USD Coin",
            version="2",
        ),
    },
)

# SKALE (L3 on Base, gasless with sFUEL)
# NOTE: SKALE uses 'USDC' (not 'USD Coin') for EIP-712 domain name
SKALE = NetworkConfig(
    name="skale",
    display_name="SKALE",
    network_type=NetworkType.EVM,
    chain_id=1187947933,
    usdc_address="0x85889c8c714505E0c94b30fcfcF64fE3Ac8FCb20",
    usdc_decimals=6,
    usdc_domain_name="USDC",  # SKALE uses "USDC"
    usdc_domain_version="2",
    rpc_url="https://mainnet.skalenodes.com/v1/honorable-steel-rasalhague",
    enabled=True,
    tokens={
        "usdc": TokenConfig(
            address="0x85889c8c714505E0c94b30fcfcF64fE3Ac8FCb20",
            decimals=6,
            name="USDC",
            version="2",
        ),
    },
)

# SKALE Testnet
SKALE_TESTNET = NetworkConfig(
    name="skale-testnet",
    display_name="SKALE Testnet",
    network_type=NetworkType.EVM,
    chain_id=324705682,
    usdc_address="0x2e08028E3C4c2356572E096d8EF835cD5C6030bD",
    usdc_decimals=6,
    usdc_domain_name="USDC",  # SKALE testnet uses "USDC"
    usdc_domain_version="2",
    rpc_url="https://testnet.skalenodes.com/v1/lanky-ill-funny-testnet",
    enabled=True,
    tokens={
        "usdc": TokenConfig(
            address="0x2e08028E3C4c2356572E096d8EF835cD5C6030bD",
            decimals=6,
            name="USDC",
            version="2",
        ),
    },
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
    SCROLL,
    SKALE,
    SKALE_TESTNET,
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
    usdc_domain_networks = {"celo", "hyperevm", "unichain", "monad", "skale", "skale-testnet"}

    if network_name.lower() in usdc_domain_networks:
        return "USDC"
    return "USD Coin"


def get_token_decimals(network_name: str) -> int:
    """
    Get USDC token decimals for a network.

    Args:
        network_name: Network identifier

    Returns:
        Number of decimals (6 for all supported chains)
    """
    return 6
