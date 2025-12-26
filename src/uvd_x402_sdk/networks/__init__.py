"""
Network configurations for x402 payments.

This module provides configuration for all supported blockchain networks,
including USDC contract addresses, RPC URLs, and network-specific parameters.

The SDK supports 16 mainnet networks out of the box:
- 10 EVM chains: Base, Ethereum, Polygon, Arbitrum, Optimism, Avalanche,
                 Celo, HyperEVM, Unichain, Monad
- 2 SVM chains: Solana, Fogo
- 1 NEAR: NEAR Protocol
- 1 Stellar: Stellar
- 2 Algorand: Algorand mainnet and testnet

Multi-token support (EVM chains only):
- USDC: All chains
- EURC: Ethereum, Base, Avalanche
- AUSD: Ethereum, Arbitrum, Avalanche, Polygon, Monad
- PYUSD: Ethereum

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
from uvd_x402_sdk.networks import evm, solana, near, stellar, algorand

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
