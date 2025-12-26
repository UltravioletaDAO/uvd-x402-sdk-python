"""
NEAR Protocol network configuration.

NEAR uses NEP-366 meta-transactions for gasless payments:
1. User creates a DelegateAction containing ft_transfer
2. User signs to create SignedDelegateAction (Borsh serialized)
3. Facilitator wraps in Action::Delegate and submits
4. Facilitator pays all gas - user pays ZERO NEAR

NEP-366 Structure:
- DelegateAction: sender_id, receiver_id, actions, nonce, max_block_height, public_key
- SignedDelegateAction: delegate_action + ed25519 signature
- Hash prefix: 2^30 + 366 = 0x4000016E (little-endian)

Borsh Serialization Format:
- u8: 1 byte
- u32: 4 bytes little-endian
- u64: 8 bytes little-endian
- u128: 16 bytes little-endian
- string: u32 length prefix + UTF-8 bytes
- bytes: u32 length prefix + raw bytes
- fixed_bytes: raw bytes (no prefix)
"""

import base64
import struct
from typing import Optional, Dict, Any

from uvd_x402_sdk.networks.base import (
    NetworkConfig,
    NetworkType,
    register_network,
)

# NEAR fee payer addresses are defined in uvd_x402_sdk.facilitator
# Import here for convenience
try:
    from uvd_x402_sdk.facilitator import (
        NEAR_FEE_PAYER_MAINNET,
        NEAR_FEE_PAYER_TESTNET,
        get_fee_payer,
    )
except ImportError:
    # Fallback if facilitator module not loaded yet
    NEAR_FEE_PAYER_MAINNET = "uvd-facilitator.near"
    NEAR_FEE_PAYER_TESTNET = "uvd-facilitator.testnet"
    get_fee_payer = None  # type: ignore

# NEP-366 hash prefix: (2^30 + 366) = 1073742190
NEP366_PREFIX = ((2**30) + 366).to_bytes(4, 'little')


# NEAR Mainnet
NEAR = NetworkConfig(
    name="near",
    display_name="NEAR Protocol",
    network_type=NetworkType.NEAR,
    chain_id=0,  # Non-EVM, no chain ID
    # Native Circle USDC on NEAR (account ID hash)
    usdc_address="17208628f84f5d6ad33f0da3bbbeb27ffcb398eac501a31bd6ad2011e36133a1",
    usdc_decimals=6,
    usdc_domain_name="",  # Not applicable for NEAR
    usdc_domain_version="",
    rpc_url="https://rpc.mainnet.near.org",
    enabled=True,  # ENABLED: Facilitator supports NEAR via NEP-366
    extra_config={
        # Network identifier
        "network_id": "mainnet",
        # Alternative RPC endpoints (CORS-friendly)
        "rpc_endpoints": [
            "https://near.drpc.org",
            "https://public-rpc.blockpi.io/http/near",
            "https://endpoints.omniatech.io/v1/near/mainnet/public",
            "https://rpc.mainnet.near.org",
            "https://near.lava.build",
        ],
        # Block explorer
        "explorer_url": "https://nearblocks.io",
        # NEP-366 prefix (2^30 + 366 as u32 little-endian)
        "nep366_prefix": NEP366_PREFIX,
        # Default gas for ft_transfer
        "ft_transfer_gas": 30_000_000_000_000,  # 30 TGas
        # Deposit for ft_transfer (1 yoctoNEAR)
        "ft_transfer_deposit": 1,
    },
)

# Register NEAR network
register_network(NEAR)


# =============================================================================
# NEAR-specific utilities
# =============================================================================


def create_ft_transfer_args(
    receiver_id: str,
    amount: int,
    memo: str = "",
) -> dict:
    """
    Create arguments for NEAR ft_transfer function call.

    Args:
        receiver_id: NEAR account ID to receive tokens
        amount: Amount in base units (6 decimals for USDC)
        memo: Optional transfer memo

    Returns:
        Dictionary of ft_transfer arguments
    """
    return {
        "receiver_id": receiver_id,
        "amount": str(amount),
        "memo": memo or None,
    }


def calculate_max_block_height(current_height: int, blocks_valid: int = 1000) -> int:
    """
    Calculate max_block_height for DelegateAction.

    Args:
        current_height: Current block height from RPC
        blocks_valid: Number of blocks the action is valid for (~17 minutes at 1 block/s)

    Returns:
        Max block height for the action
    """
    return current_height + blocks_valid


def base58_decode(encoded: str) -> bytes:
    """
    Decode a base58 string to bytes.

    NEAR public keys use ed25519:base58 format.

    Args:
        encoded: Base58 encoded string (without prefix)

    Returns:
        Decoded bytes
    """
    ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
    BASE = 58

    num = 0
    for char in encoded:
        index = ALPHABET.index(char)
        if index == -1:
            raise ValueError(f"Invalid base58 character: {char}")
        num = num * BASE + index

    # Convert to bytes (32 bytes for ED25519 public key)
    result = []
    while num > 0:
        result.append(num & 0xFF)
        num >>= 8

    # Pad to 32 bytes and reverse
    while len(result) < 32:
        result.append(0)

    return bytes(reversed(result))


def decode_near_public_key(public_key: str) -> bytes:
    """
    Decode a NEAR public key from ed25519:base58 format to raw bytes.

    Args:
        public_key: Public key in format 'ed25519:base58string'

    Returns:
        32 bytes of the public key
    """
    # Remove ed25519: prefix if present
    if public_key.startswith("ed25519:"):
        public_key = public_key[8:]

    return base58_decode(public_key)


# =============================================================================
# NEP-366 Borsh Serialization
# =============================================================================


class BorshSerializer:
    """
    Simple Borsh serializer for NEAR transactions.

    Borsh (Binary Object Representation Serializer for Hashing) is NEAR's
    canonical binary serialization format. This implementation handles
    the subset needed for NEP-366 meta-transactions.
    """

    def __init__(self) -> None:
        self.buffer = bytearray()

    def write_u8(self, value: int) -> "BorshSerializer":
        """Write unsigned 8-bit integer."""
        self.buffer.extend(struct.pack('<B', value))
        return self

    def write_u32(self, value: int) -> "BorshSerializer":
        """Write unsigned 32-bit integer (little-endian)."""
        self.buffer.extend(struct.pack('<I', value))
        return self

    def write_u64(self, value: int) -> "BorshSerializer":
        """Write unsigned 64-bit integer (little-endian)."""
        self.buffer.extend(struct.pack('<Q', value))
        return self

    def write_u128(self, value: int) -> "BorshSerializer":
        """Write unsigned 128-bit integer (little-endian)."""
        low = value & 0xFFFFFFFFFFFFFFFF
        high = value >> 64
        self.buffer.extend(struct.pack('<QQ', low, high))
        return self

    def write_string(self, value: str) -> "BorshSerializer":
        """Write length-prefixed UTF-8 string."""
        encoded = value.encode('utf-8')
        self.write_u32(len(encoded))
        self.buffer.extend(encoded)
        return self

    def write_fixed_bytes(self, data: bytes) -> "BorshSerializer":
        """Write fixed-length bytes (no prefix)."""
        self.buffer.extend(data)
        return self

    def write_bytes(self, data: bytes) -> "BorshSerializer":
        """Write length-prefixed bytes."""
        self.write_u32(len(data))
        self.buffer.extend(data)
        return self

    def get_bytes(self) -> bytes:
        """Get the serialized bytes."""
        return bytes(self.buffer)


def serialize_non_delegate_action(
    receiver_id: str,
    amount: int,
    memo: Optional[str] = None,
) -> bytes:
    """
    Serialize a NonDelegateAction for ft_transfer (NEP-366).

    This creates the action that will be wrapped in a DelegateAction.
    The action type is FunctionCall (type 2).

    Args:
        receiver_id: NEAR account ID to receive tokens
        amount: Amount in raw units (6 decimals for USDC)
        memo: Optional transfer memo

    Returns:
        Borsh-serialized action bytes
    """
    import json

    # Build ft_transfer args
    args: Dict[str, Any] = {
        "receiver_id": receiver_id,
        "amount": str(amount),
    }
    if memo:
        args["memo"] = memo

    args_json = json.dumps(args, separators=(',', ':')).encode('utf-8')

    ser = BorshSerializer()
    ser.write_u8(2)  # FunctionCall action type
    ser.write_string("ft_transfer")  # Method name
    ser.write_bytes(args_json)  # Args as JSON bytes
    ser.write_u64(30_000_000_000_000)  # 30 TGas
    ser.write_u128(1)  # 1 yoctoNEAR deposit (required for ft_transfer)

    return ser.get_bytes()


def serialize_delegate_action(
    sender_id: str,
    receiver_id: str,
    actions_bytes: bytes,
    nonce: int,
    max_block_height: int,
    public_key_bytes: bytes,
) -> bytes:
    """
    Serialize a DelegateAction for NEP-366 meta-transactions.

    Args:
        sender_id: NEAR account ID of the sender
        receiver_id: NEAR contract ID (USDC contract)
        actions_bytes: Borsh-serialized NonDelegateAction
        nonce: Access key nonce + 1
        max_block_height: Block height when action expires
        public_key_bytes: 32-byte ED25519 public key

    Returns:
        Borsh-serialized DelegateAction bytes
    """
    ser = BorshSerializer()
    ser.write_string(sender_id)
    ser.write_string(receiver_id)
    ser.write_u32(1)  # 1 action
    ser.write_fixed_bytes(actions_bytes)
    ser.write_u64(nonce)
    ser.write_u64(max_block_height)
    ser.write_u8(0)  # ED25519 key type
    ser.write_fixed_bytes(public_key_bytes)

    return ser.get_bytes()


def serialize_signed_delegate_action(
    delegate_action_bytes: bytes,
    signature_bytes: bytes,
) -> bytes:
    """
    Serialize a SignedDelegateAction for NEP-366.

    Args:
        delegate_action_bytes: Borsh-serialized DelegateAction
        signature_bytes: 64-byte ED25519 signature

    Returns:
        Borsh-serialized SignedDelegateAction bytes
    """
    ser = BorshSerializer()
    ser.write_fixed_bytes(delegate_action_bytes)
    ser.write_u8(0)  # ED25519 signature type
    ser.write_fixed_bytes(signature_bytes)

    return ser.get_bytes()


# =============================================================================
# NEP-366 Payload Validation
# =============================================================================


def validate_near_payload(payload: Dict[str, Any]) -> bool:
    """
    Validate a NEAR payment payload structure.

    The payload must contain a base64-encoded SignedDelegateAction.

    Args:
        payload: Payload dictionary from x402 payment

    Returns:
        True if valid, raises ValueError if invalid
    """
    if "signedDelegateAction" not in payload:
        raise ValueError("NEAR payload missing 'signedDelegateAction' field")

    signed_delegate_b64 = payload["signedDelegateAction"]

    try:
        # Decode base64
        signed_delegate_bytes = base64.b64decode(signed_delegate_b64)
    except Exception as e:
        raise ValueError(f"Invalid base64 in signedDelegateAction: {e}")

    # Basic length validation
    # Minimum: sender_id (4 + 1) + receiver_id (4 + 1) + actions_count (4) +
    #          action (at least 1) + nonce (8) + max_block_height (8) +
    #          key_type (1) + public_key (32) + sig_type (1) + signature (64)
    min_length = 4 + 1 + 4 + 1 + 4 + 1 + 8 + 8 + 1 + 32 + 1 + 64
    if len(signed_delegate_bytes) < min_length:
        raise ValueError(
            f"SignedDelegateAction too short: {len(signed_delegate_bytes)} bytes, "
            f"minimum {min_length} bytes"
        )

    return True


def is_valid_near_account_id(account_id: str) -> bool:
    """
    Validate a NEAR account ID format.

    NEAR account IDs:
    - Are 2-64 characters
    - Contain only a-z, 0-9, _, -, .
    - Cannot start with _ or -
    - Cannot end with .

    Args:
        account_id: NEAR account ID to validate

    Returns:
        True if valid format
    """
    if not account_id or len(account_id) < 2 or len(account_id) > 64:
        return False

    if account_id.startswith(('_', '-')) or account_id.endswith('.'):
        return False

    allowed = set('abcdefghijklmnopqrstuvwxyz0123456789_-.')
    return all(c in allowed for c in account_id)


def get_near_fee_payer(network_name: str = "near") -> str:
    """
    Get the fee payer account ID for a NEAR network.

    The fee payer is the facilitator account that pays gas fees.
    This account wraps the SignedDelegateAction and submits it.

    Args:
        network_name: Network name ('near' or 'near-testnet')

    Returns:
        Fee payer account ID for the specified network

    Example:
        >>> get_near_fee_payer("near")
        'uvd-facilitator.near'
        >>> get_near_fee_payer("near-testnet")
        'uvd-facilitator.testnet'
    """
    # Use facilitator module if available
    if get_fee_payer is not None:
        fee_payer = get_fee_payer(network_name)
        if fee_payer:
            return fee_payer

    # Fallback to direct lookup
    network_lower = network_name.lower()
    if "testnet" in network_lower:
        return NEAR_FEE_PAYER_TESTNET
    return NEAR_FEE_PAYER_MAINNET
