"""
Network configurations for x402 payments.

This module provides configuration for all supported blockchain networks,
including USDC contract addresses, RPC URLs, and network-specific parameters.

The SDK supports 27 blockchain networks across 8 network families:
- 15 EVM networks: Base, Ethereum, Polygon, Arbitrum, Optimism, Avalanche,
                   Celo, HyperEVM, Unichain, Monad, Scroll, SKALE Base,
                   SKALE Base Sepolia, Robinhood, Robinhood Testnet
- 2 SVM networks: Solana, Fogo
- 1 NEAR network: NEAR Protocol
- 1 Stellar network: Stellar
- 2 Algorand networks: Algorand mainnet, Algorand testnet
- 2 Sui networks: Sui mainnet, Sui testnet
- 2 XRPL networks: XRP Ledger mainnet, XRP Ledger testnet (native XRP)
- 2 Casper networks: Casper mainnet, Casper testnet (wCSPR, CEP-18)

(13 EVM mainnets + 2 EVM testnets)

Multi-token support:
- USDC: All EVM chains except Robinhood
- EURC: Ethereum, Base, Avalanche
- AUSD: Ethereum, Arbitrum, Avalanche, Polygon, Monad, Sui
- PYUSD: Ethereum
- USDG: Robinhood, Robinhood Testnet (Paxos Global Dollar)

You can register custom networks using `register_network()`.
"""

from uvd_x402_sdk.networks.base import (
    NetworkConfig,
    NetworkType,
    # Token types (multi-stablecoin support)
    TokenType,
    TokenConfig,
    ALL_TOKEN_TYPES,
    get_network,
    get_network_by_chain_id,
    register_network,
    list_networks,
    get_supported_chain_ids,
    get_supported_network_names,
    SUPPORTED_NETWORKS,
    # Token helper functions
    get_token_config,
    get_supported_tokens,
    is_token_supported,
    get_networks_by_token,
    # CAIP-2 utilities (x402 v2)
    parse_caip2_network,
    to_caip2_network,
    is_caip2_format,
    normalize_network,
)

# Import all default network configurations
from uvd_x402_sdk.networks import evm, solana, near, stellar, algorand, sui, xrpl, casper

__all__ = [
    # Core
    "NetworkConfig",
    "NetworkType",
    # Token types (multi-stablecoin support)
    "TokenType",
    "TokenConfig",
    "ALL_TOKEN_TYPES",
    # Registry functions
    "get_network",
    "get_network_by_chain_id",
    "register_network",
    "list_networks",
    "get_supported_chain_ids",
    "get_supported_network_names",
    "SUPPORTED_NETWORKS",
    # Token helper functions
    "get_token_config",
    "get_supported_tokens",
    "is_token_supported",
    "get_networks_by_token",
    # CAIP-2 utilities (x402 v2)
    "parse_caip2_network",
    "to_caip2_network",
    "is_caip2_format",
    "normalize_network",
]
