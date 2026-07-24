"""
Casper Network configurations.

This module supports the Casper blockchain for x402 payments using CEP-18
token authorizations:
- Casper (mainnet, CAIP-2 casper:casper)
- Casper Testnet (CAIP-2 casper:casper-test)

Casper uses the x402 "exact" scheme with v2 (CAIP-2) network identifiers,
settling in wCSPR (Wrapped CSPR), a CEP-18 fungible token with 9 decimals
("motes": 1 CSPR = 1,000,000,000 motes).

Payment flow (mirrors EIP-3009 transferWithAuthorization on EVM):
1. User signs an EIP-712 typed-data authorization (from, to, value,
   validAfter, validBefore, nonce) with their Casper key
   (casper-ecosystem/casper-eip-712 typed-data specification)
2. The signed authorization + public key are sent to the facilitator
3. Facilitator submits a `transfer_with_authorization` deploy to the
   CEP-18 contract and pays the gas (in CSPR)
4. User pays ZERO gas - the facilitator sponsors the deploy

Casper x402 payments are settled by the dedicated Casper facilitator at
https://x402-facilitator.cspr.cloud (see docs.cspr.cloud), built on the
make-software/casper-x402 reference implementation.

Address formats:
- Public keys: hex, "01"-prefixed ed25519 (66 chars) or "02"-prefixed
  secp256k1 (68 chars)
- Authorization from/to: 66-hex-char addresses with "00" (account-hash)
  or "01" (hash) prefix
- Asset: 64-hex-char CEP-18 contract package hash (no "hash-" prefix)
"""

import re
from typing import Optional

from uvd_x402_sdk.networks.base import (
    NetworkConfig,
    NetworkType,
    TokenConfig,
    register_network,
)

# =============================================================================
# Casper Constants
# =============================================================================

# Dedicated Casper x402 facilitator (CSPR.cloud)
CASPER_FACILITATOR_URL = "https://x402-facilitator.cspr.cloud"

# wCSPR (Wrapped CSPR) CEP-18 contract package hashes (64 hex chars, no "hash-" prefix)
WCSPR_CONTRACT_PACKAGE_MAINNET = "8df5d26790e18cf0404502c62ce5dc9025800ad6975c97466e20506c39c505b6"
WCSPR_CONTRACT_PACKAGE_TESTNET = "3d80df21ba4ee4d66a2a1f60c32570dd5685e4b279f6538162a5fd1314847c1e"

# CSPR/wCSPR use 9 decimals (motes): 1 CSPR = 1,000,000,000 motes
CASPER_DECIMALS = 9
MOTES_PER_CSPR = 1_000_000_000

# Address validation patterns (matches make-software/casper-x402)
# Authorization addresses: 66 hex chars with "00" (account-hash) or "01" (hash) prefix
_CASPER_ADDRESS_RE = re.compile(r"^(00|01)[0-9a-fA-F]{64}$")
# Public keys: "01" + 64 hex (ed25519) or "02" + 66 hex (secp256k1)
_CASPER_PUBLIC_KEY_RE = re.compile(r"^(01[0-9a-fA-F]{64}|02[0-9a-fA-F]{66})$")
# CEP-18 contract package hash: 64 hex chars
_CONTRACT_PACKAGE_HASH_RE = re.compile(r"^[0-9a-fA-F]{64}$")


# =============================================================================
# Casper Networks Configuration
# =============================================================================

# Casper Mainnet
CASPER = NetworkConfig(
    name="casper",
    display_name="Casper",
    network_type=NetworkType.CASPER,
    chain_id=0,  # Non-EVM, no chain ID
    usdc_address=WCSPR_CONTRACT_PACKAGE_MAINNET,  # Settlement asset: wCSPR (CEP-18)
    usdc_decimals=CASPER_DECIMALS,  # 9 decimals (motes)
    usdc_domain_name="Wrapped CSPR",  # EIP-712 domain name (casper-eip-712)
    usdc_domain_version="1",
    rpc_url="https://node.mainnet.casper.network/rpc",
    enabled=True,
    tokens={
        "usdc": TokenConfig(
            address=WCSPR_CONTRACT_PACKAGE_MAINNET,
            decimals=CASPER_DECIMALS,
            name="Wrapped CSPR",
            version="1",
        ),
    },
    extra_config={
        # Settlement asset symbol (wCSPR, CEP-18 fungible token)
        "settlement_asset": "WCSPR",
        # wCSPR CEP-18 contract package hash
        "wcspr_contract_package": WCSPR_CONTRACT_PACKAGE_MAINNET,
        # Chain name used in deploy payloads
        "chain_name": "casper",
        # Dedicated Casper facilitator
        "facilitator_url": CASPER_FACILITATOR_URL,
        # Block explorer
        "explorer_url": "https://cspr.live",
        # x402 v2 network identifier (CAIP-2)
        "x402_network": "casper:casper",
    },
)

# Casper Testnet
CASPER_TESTNET = NetworkConfig(
    name="casper-testnet",
    display_name="Casper Testnet",
    network_type=NetworkType.CASPER,
    chain_id=0,  # Non-EVM, no chain ID
    usdc_address=WCSPR_CONTRACT_PACKAGE_TESTNET,  # Settlement asset: wCSPR (CEP-18)
    usdc_decimals=CASPER_DECIMALS,  # 9 decimals (motes)
    usdc_domain_name="Wrapped CSPR",
    usdc_domain_version="1",
    rpc_url="https://node.testnet.casper.network/rpc",
    enabled=True,
    tokens={
        "usdc": TokenConfig(
            address=WCSPR_CONTRACT_PACKAGE_TESTNET,
            decimals=CASPER_DECIMALS,
            name="Wrapped CSPR",
            version="1",
        ),
    },
    extra_config={
        "settlement_asset": "WCSPR",
        "wcspr_contract_package": WCSPR_CONTRACT_PACKAGE_TESTNET,
        "chain_name": "casper-test",
        "facilitator_url": CASPER_FACILITATOR_URL,
        "explorer_url": "https://testnet.cspr.live",
        "x402_network": "casper:casper-test",
    },
)

# Register Casper networks
register_network(CASPER)
register_network(CASPER_TESTNET)


# =============================================================================
# Casper-specific utilities
# =============================================================================


def motes_to_cspr(motes: int) -> float:
    """
    Convert motes (9 decimals) to CSPR amount.

    Args:
        motes: Amount in motes

    Returns:
        CSPR amount (1 CSPR = 1,000,000,000 motes)
    """
    return motes / MOTES_PER_CSPR


def cspr_to_motes(cspr: float) -> int:
    """
    Convert a CSPR amount to motes (9 decimals).

    Args:
        cspr: CSPR amount

    Returns:
        Amount in motes (1 CSPR = 1,000,000,000 motes)
    """
    return int(cspr * MOTES_PER_CSPR)


def is_valid_casper_public_key(public_key: str) -> bool:
    """
    Validate a Casper public key format.

    Casper public keys are hex strings with an algorithm prefix:
    - "01" + 64 hex chars: ed25519 (66 chars total)
    - "02" + 66 hex chars: secp256k1 (68 chars total)

    Args:
        public_key: Public key to validate

    Returns:
        True if the value is a valid Casper public key

    Example:
        >>> is_valid_casper_public_key("01" + "ab" * 32)
        True
        >>> is_valid_casper_public_key("02" + "ab" * 33)
        True
    """
    if not isinstance(public_key, str):
        return False
    return bool(_CASPER_PUBLIC_KEY_RE.match(public_key))


def is_valid_casper_address(address: str) -> bool:
    """
    Validate a Casper authorization address format.

    Casper x402 authorization addresses are 66 hex characters prefixed with
    "00" (account-hash) or "01" (hash), matching the make-software/casper-x402
    ExactCasperAuthorization from/to fields.

    Args:
        address: Address to validate

    Returns:
        True if the value is a valid Casper address
    """
    if not isinstance(address, str):
        return False
    return bool(_CASPER_ADDRESS_RE.match(address))


def is_valid_contract_package_hash(value: str) -> bool:
    """
    Validate a CEP-18 contract package hash (used as the x402 asset field).

    Contract package hashes are 64 hex characters without the "hash-" prefix.

    Args:
        value: Contract package hash to validate

    Returns:
        True if the value is a valid contract package hash
    """
    if not isinstance(value, str):
        return False
    return bool(_CONTRACT_PACKAGE_HASH_RE.match(value))


def is_casper_network(network_name: str) -> bool:
    """
    Check if a network is Casper-based.

    Args:
        network_name: Network name to check

    Returns:
        True if network uses Casper
    """
    from uvd_x402_sdk.networks.base import get_network

    network = get_network(network_name)
    if not network:
        return False
    return network.network_type == NetworkType.CASPER


def get_casper_networks() -> list:
    """
    Get all registered Casper networks.

    Returns:
        List of Casper NetworkConfig instances
    """
    from uvd_x402_sdk.networks.base import list_networks

    return [n for n in list_networks(enabled_only=True) if n.network_type == NetworkType.CASPER]


def get_casper_facilitator_url(network_name: str = "casper") -> str:
    """
    Get the dedicated Casper facilitator URL.

    Casper payments are verified and settled by the CSPR.cloud facilitator,
    which submits the CEP-18 transfer_with_authorization deploy and pays gas.
    Both mainnet and testnet use the same facilitator endpoint.

    Args:
        network_name: Network name ('casper' or 'casper-testnet')

    Returns:
        Facilitator URL for Casper networks

    Example:
        >>> get_casper_facilitator_url("casper")
        'https://x402-facilitator.cspr.cloud'
    """
    return CASPER_FACILITATOR_URL


def get_wcspr_contract_package(network_name: str = "casper") -> str:
    """
    Get the wCSPR CEP-18 contract package hash for a Casper network.

    Args:
        network_name: Network name ('casper', 'casper-mainnet', 'casper-testnet')

    Returns:
        wCSPR contract package hash (64 hex chars, no "hash-" prefix)

    Example:
        >>> get_wcspr_contract_package("casper")
        '8df5d26790e18cf0404502c62ce5dc9025800ad6975c97466e20506c39c505b6'
    """
    network_lower = network_name.lower()
    if "testnet" in network_lower or "test" in network_lower.split(":")[-1]:
        return WCSPR_CONTRACT_PACKAGE_TESTNET
    return WCSPR_CONTRACT_PACKAGE_MAINNET


def get_casper_chain_name(network_name: str = "casper") -> str:
    """
    Get the Casper chain name used in deploy payloads.

    Args:
        network_name: Network name ('casper' or 'casper-testnet')

    Returns:
        Chain name ('casper' for mainnet, 'casper-test' for testnet)
    """
    network_lower = network_name.lower()
    if "testnet" in network_lower or "test" in network_lower.split(":")[-1]:
        return "casper-test"
    return "casper"


def validate_casper_payload(payload: dict) -> bool:
    """
    Validate a Casper payment payload structure.

    The payload must contain (make-software/casper-x402 ExactCasperPayload):
    - signature: 65-byte EIP-712 signature as a hex string
    - publicKey: Full public key hex of the payer
    - authorization: dict with from, to, value, validAfter, validBefore, nonce

    Args:
        payload: Payload dictionary from x402 payment

    Returns:
        True if valid, raises ValueError if invalid
    """
    required_fields = ["signature", "publicKey", "authorization"]
    for field_name in required_fields:
        if field_name not in payload:
            raise ValueError(f"Casper payload missing '{field_name}' field")

    if not is_valid_casper_public_key(payload["publicKey"]):
        raise ValueError(f"Invalid publicKey: {payload['publicKey']}")

    authorization = payload["authorization"]
    if not isinstance(authorization, dict):
        raise ValueError("Casper authorization must be an object")

    auth_fields = ["from", "to", "value", "validAfter", "validBefore", "nonce"]
    for field_name in auth_fields:
        if field_name not in authorization:
            raise ValueError(f"Casper authorization missing '{field_name}' field")

    if not is_valid_casper_address(authorization["from"]):
        raise ValueError(f"Invalid 'from' address: {authorization['from']}")

    if not is_valid_casper_address(authorization["to"]):
        raise ValueError(f"Invalid 'to' address: {authorization['to']}")

    try:
        value = int(authorization["value"])
        if value <= 0:
            raise ValueError(f"Value must be positive: {value}")
    except (TypeError, ValueError) as e:
        raise ValueError(f"Invalid value: {e}")

    return True


def format_casper_amount(cspr_amount: float, decimals: int = CASPER_DECIMALS) -> int:
    """
    Convert a CSPR/wCSPR amount to token base units (motes).

    Args:
        cspr_amount: Amount in CSPR (e.g., 10.5)
        decimals: Token decimals (9 for CSPR/wCSPR)

    Returns:
        Amount in base units (motes)
    """
    return int(cspr_amount * (10**decimals))


def parse_casper_amount(base_units: int, decimals: int = CASPER_DECIMALS) -> float:
    """
    Convert token base units (motes) to a CSPR/wCSPR amount.

    Args:
        base_units: Amount in base units (motes)
        decimals: Token decimals (9 for CSPR/wCSPR)

    Returns:
        Amount in CSPR
    """
    return base_units / (10**decimals)


def get_optional_casper_network(network_name: str) -> Optional[NetworkConfig]:
    """
    Get a Casper NetworkConfig by name, if it is a Casper network.

    Args:
        network_name: Network name to look up

    Returns:
        NetworkConfig if the network exists and is Casper-based, None otherwise
    """
    from uvd_x402_sdk.networks.base import get_network

    network = get_network(network_name)
    if network and network.network_type == NetworkType.CASPER:
        return network
    return None
