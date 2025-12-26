"""
Algorand network configurations for x402 payments.

This module supports Algorand blockchain networks:
- Algorand mainnet (network: "algorand-mainnet" or "algorand")
- Algorand testnet (network: "algorand-testnet")

Algorand uses ASA (Algorand Standard Assets) for USDC:
- Mainnet USDC ASA ID: 31566704
- Testnet USDC ASA ID: 10458941

Payment Flow (GoPlausible x402-avm Atomic Group Spec):
1. Client creates an ATOMIC GROUP of TWO transactions:
   - Transaction 0 (fee tx): Zero-amount payment FROM facilitator TO facilitator
     This transaction pays fees for both txns. Client creates this UNSIGNED.
   - Transaction 1 (payment tx): ASA transfer FROM client TO merchant
     Client SIGNS this transaction.
2. Both transactions share a GROUP ID computed by Algorand SDK.
3. Fee pooling: Transaction 0's fee covers Transaction 1's fee (gasless).
4. Facilitator completes: Signs transaction 0 and submits the atomic group.

Payload Format:
{
    "x402Version": 1,
    "scheme": "exact",
    "network": "algorand-mainnet",
    "payload": {
        "paymentIndex": 1,
        "paymentGroup": [
            "<base64-msgpack-UNSIGNED-fee-tx>",
            "<base64-msgpack-SIGNED-asa-transfer>"
        ]
    }
}

Address Format:
- Algorand addresses are 58 characters, base32 encoded
- Example: AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAY5HFKQ

Dependencies:
- algosdk (optional): Required for building atomic groups
  Install with: pip install py-algorand-sdk
"""

import base64
import re
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

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
        # x402 network name (facilitator expects this format)
        "x402_network": "algorand-mainnet",
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
        # x402 network name (facilitator expects this format)
        "x402_network": "algorand-testnet",
    },
)

# Register Algorand networks
register_network(ALGORAND)
register_network(ALGORAND_TESTNET)


# =============================================================================
# Algorand Payment Payload (x402-avm Atomic Group Spec)
# =============================================================================


@dataclass
class AlgorandPaymentPayload:
    """
    Algorand payment payload for x402 atomic group format.

    Attributes:
        payment_index: Index of the payment transaction in the group (typically 1)
        payment_group: List of base64-encoded msgpack transactions
                      [0] = unsigned fee transaction
                      [1] = signed ASA transfer transaction
    """

    payment_index: int
    payment_group: List[str]

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "paymentIndex": self.payment_index,
            "paymentGroup": self.payment_group,
        }


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
    Validate an Algorand payment payload structure (x402-avm atomic group format).

    The payload must contain:
    - paymentIndex: Index of the payment transaction (typically 1)
    - paymentGroup: List of base64-encoded msgpack transactions
      - [0]: Unsigned fee transaction (facilitator -> facilitator)
      - [1]: Signed ASA transfer (client -> merchant)

    Args:
        payload: Payload dictionary from x402 payment (the inner "payload" field)

    Returns:
        True if valid, raises ValueError if invalid

    Example:
        >>> payload = {
        ...     "paymentIndex": 1,
        ...     "paymentGroup": [
        ...         "base64-unsigned-fee-tx...",
        ...         "base64-signed-payment-tx..."
        ...     ]
        ... }
        >>> validate_algorand_payload(payload)
        True
    """
    # Check required fields
    if "paymentIndex" not in payload:
        raise ValueError("Algorand payload missing 'paymentIndex' field")
    if "paymentGroup" not in payload:
        raise ValueError("Algorand payload missing 'paymentGroup' field")

    # Validate paymentIndex
    payment_index = payload["paymentIndex"]
    if not isinstance(payment_index, int) or payment_index < 0:
        raise ValueError(f"paymentIndex must be a non-negative integer: {payment_index}")

    # Validate paymentGroup
    payment_group = payload["paymentGroup"]
    if not isinstance(payment_group, list):
        raise ValueError("paymentGroup must be a list")

    if len(payment_group) < 2:
        raise ValueError(
            f"paymentGroup must contain at least 2 transactions, got {len(payment_group)}"
        )

    if payment_index >= len(payment_group):
        raise ValueError(
            f"paymentIndex ({payment_index}) out of range for paymentGroup "
            f"(length {len(payment_group)})"
        )

    # Validate each transaction in the group is valid base64
    for i, txn_b64 in enumerate(payment_group):
        if not isinstance(txn_b64, str):
            raise ValueError(f"paymentGroup[{i}] must be a string")

        try:
            txn_bytes = base64.b64decode(txn_b64)
            if len(txn_bytes) < 50:
                raise ValueError(
                    f"paymentGroup[{i}] transaction too short: {len(txn_bytes)} bytes"
                )
        except Exception as e:
            raise ValueError(
                f"paymentGroup[{i}] is not valid base64: {e}"
            ) from e

    return True


def get_x402_network_name(network_name: str) -> str:
    """
    Get the x402 network name for an Algorand network.

    The facilitator expects "algorand-mainnet" or "algorand-testnet".

    Args:
        network_name: SDK network name ('algorand' or 'algorand-testnet')

    Returns:
        x402 network name ('algorand-mainnet' or 'algorand-testnet')
    """
    from uvd_x402_sdk.networks.base import get_network

    network = get_network(network_name)
    if not network:
        # Default mapping
        if network_name == "algorand":
            return "algorand-mainnet"
        return network_name

    return network.extra_config.get("x402_network", network_name)


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


# =============================================================================
# Atomic Group Builder (requires algosdk)
# =============================================================================


def build_atomic_group(
    sender_address: str,
    recipient_address: str,
    amount: int,
    asset_id: int,
    facilitator_address: str,
    sign_transaction: Callable,
    algod_client: Optional[Any] = None,
    suggested_params: Optional[Any] = None,
) -> AlgorandPaymentPayload:
    """
    Build an Algorand atomic group for x402 payment.

    This creates the two-transaction atomic group required by the facilitator:
    - Transaction 0: Unsigned fee payment (facilitator -> facilitator, 0 amount)
    - Transaction 1: Signed ASA transfer (sender -> recipient)

    Requires: pip install py-algorand-sdk

    Args:
        sender_address: Client's Algorand address
        recipient_address: Merchant's Algorand address (from payTo)
        amount: Amount in micro-units (1 USDC = 1,000,000)
        asset_id: USDC ASA ID (31566704 mainnet, 10458941 testnet)
        facilitator_address: Facilitator address (from extra.feePayer)
        sign_transaction: Function that signs a transaction.
                         Signature: (transaction) -> SignedTransaction
                         Can use algosdk's transaction.sign(private_key)
        algod_client: Optional AlgodClient for getting suggested params.
                     If not provided, suggested_params must be given.
        suggested_params: Optional SuggestedParams. If not provided,
                         algod_client.suggested_params() is called.

    Returns:
        AlgorandPaymentPayload with paymentIndex and paymentGroup

    Raises:
        ImportError: If algosdk is not installed
        ValueError: If neither algod_client nor suggested_params provided

    Example:
        >>> from algosdk import transaction
        >>> from algosdk.v2client import algod
        >>>
        >>> client = algod.AlgodClient("", "https://mainnet-api.algonode.cloud")
        >>> payload = build_atomic_group(
        ...     sender_address="SENDER...",
        ...     recipient_address="MERCHANT...",
        ...     amount=1000000,  # 1 USDC
        ...     asset_id=31566704,
        ...     facilitator_address="FACILITATOR...",
        ...     sign_transaction=lambda txn: txn.sign(private_key),
        ...     algod_client=client,
        ... )
    """
    try:
        from algosdk import encoding, transaction
    except ImportError as e:
        raise ImportError(
            "algosdk is required for building atomic groups. "
            "Install with: pip install py-algorand-sdk"
        ) from e

    # Get suggested params
    if suggested_params is None:
        if algod_client is None:
            raise ValueError(
                "Either algod_client or suggested_params must be provided"
            )
        suggested_params = algod_client.suggested_params()

    # Transaction 0: Fee payment (facilitator -> facilitator, 0 amount)
    # This transaction pays fees for both txns in the group
    fee_txn = transaction.PaymentTxn(
        sender=facilitator_address,
        receiver=facilitator_address,  # self-transfer
        amt=0,
        sp=suggested_params,
    )
    # Cover both transactions (1000 microAlgos each = 2000 total)
    fee_txn.fee = 2000

    # Transaction 1: ASA transfer (client -> merchant)
    payment_txn = transaction.AssetTransferTxn(
        sender=sender_address,
        receiver=recipient_address,
        amt=amount,
        index=asset_id,
        sp=suggested_params,
    )
    # Fee paid by transaction 0
    payment_txn.fee = 0

    # Assign group ID to both transactions
    group_id = transaction.calculate_group_id([fee_txn, payment_txn])
    fee_txn.group = group_id
    payment_txn.group = group_id

    # Encode fee transaction (UNSIGNED - facilitator will sign)
    unsigned_fee_txn_bytes = encoding.msgpack_encode(fee_txn)
    unsigned_fee_txn_base64 = base64.b64encode(unsigned_fee_txn_bytes).decode("utf-8")

    # Sign and encode payment transaction
    signed_payment_txn = sign_transaction(payment_txn)
    signed_payment_txn_bytes = encoding.msgpack_encode(signed_payment_txn)
    signed_payment_txn_base64 = base64.b64encode(signed_payment_txn_bytes).decode("utf-8")

    return AlgorandPaymentPayload(
        payment_index=1,  # Index of the payment transaction
        payment_group=[
            unsigned_fee_txn_base64,   # Transaction 0: unsigned fee tx
            signed_payment_txn_base64,  # Transaction 1: signed payment tx
        ],
    )


def create_private_key_signer(private_key: str) -> Callable:
    """
    Create a transaction signer from a private key.

    Args:
        private_key: Algorand private key (base64 encoded)

    Returns:
        Function that signs transactions

    Example:
        >>> signer = create_private_key_signer(my_private_key)
        >>> payload = build_atomic_group(..., sign_transaction=signer)
    """
    def sign(txn: Any) -> Any:
        return txn.sign(private_key)
    return sign


def build_x402_payment_request(
    payload: AlgorandPaymentPayload,
    network: str = "algorand-mainnet",
    scheme: str = "exact",
    version: int = 1,
) -> Dict[str, Any]:
    """
    Build a complete x402 payment request for Algorand.

    Args:
        payload: AlgorandPaymentPayload from build_atomic_group()
        network: Network name ("algorand-mainnet" or "algorand-testnet")
        scheme: Payment scheme (default "exact")
        version: x402 version (default 1)

    Returns:
        Complete x402 payment request dictionary

    Example:
        >>> payload = build_atomic_group(...)
        >>> request = build_x402_payment_request(payload)
        >>> # Send as X-PAYMENT header (base64 encoded JSON)
        >>> import json, base64
        >>> header = base64.b64encode(json.dumps(request).encode()).decode()
    """
    return {
        "x402Version": version,
        "scheme": scheme,
        "network": network,
        "payload": payload.to_dict(),
    }
