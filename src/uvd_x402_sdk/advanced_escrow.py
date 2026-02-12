"""
Advanced Escrow client for x402 PaymentOperator integration.

This module provides the 5 Advanced Escrow flows via the PaymentOperator contract,
plus 3 gasless facilitator-proxied methods for release, refund, and state queries:

On-chain flows (require gas + operator private key):
1. AUTHORIZE  - Lock funds in escrow (via facilitator)
2. RELEASE    - Capture escrowed funds to receiver (on-chain)
3. REFUND IN ESCROW - Return escrowed funds to payer (on-chain)
4. CHARGE     - Direct instant payment without escrow (on-chain)
5. REFUND POST ESCROW - Dispute refund after release (NOT FUNCTIONAL - tokenCollector not implemented)

Gasless facilitator-proxied flows (facilitator pays gas, v1.32.0+):
6. RELEASE VIA FACILITATOR    - Gasless release through POST /settle action:"release"
7. REFUND VIA FACILITATOR     - Gasless refund through POST /settle action:"refundInEscrow"
8. QUERY ESCROW STATE         - Read-only escrow state via POST /escrow/state (no gas)

Contract deposit limit: $100 USDC per deposit (enforced on-chain).
Dispute resolution: use refund_in_escrow() (keep funds in escrow, arbiter decides).

Contract mapping:
    operator.authorize()        -> escrow.authorize()   (lock funds)
    operator.release()          -> escrow.capture()      (pay receiver)
    operator.refundInEscrow()   -> escrow.partialVoid()  (refund payer)
    operator.charge()           -> escrow.charge()       (direct payment)
    operator.refundPostEscrow() -> escrow.refund()       (dispute refund)

Example:
    >>> from uvd_x402_sdk.advanced_escrow import AdvancedEscrowClient
    >>>
    >>> client = AdvancedEscrowClient(
    ...     facilitator_url="https://facilitator.ultravioletadao.xyz",
    ...     rpc_url="https://mainnet.base.org",
    ...     private_key="0x...",
    ...     chain_id=8453,
    ... )
    >>>
    >>> # Build payment info
    >>> pi = client.build_payment_info(
    ...     receiver="0xWorker...",
    ...     amount=5_000_000,  # $5 USDC
    ...     tier=TaskTier.STANDARD,
    ... )
    >>>
    >>> # Lock funds in escrow
    >>> auth = client.authorize(pi)
    >>>
    >>> # After work is done, release to worker (GASLESS)
    >>> tx = client.release_via_facilitator(auth.payment_info)
    >>>
    >>> # Or cancel and refund (GASLESS)
    >>> tx = client.refund_via_facilitator(auth.payment_info)
    >>>
    >>> # Query escrow state (read-only, no gas)
    >>> state = client.query_escrow_state(auth.payment_info)
    >>> print(state["capturableAmount"], state["refundableAmount"])
"""

import secrets
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

import httpx
from eth_abi import encode
from eth_account import Account
from eth_account.messages import encode_typed_data
from web3 import Web3


# ============================================================
# Constants
# ============================================================

PAYMENT_INFO_TYPEHASH = bytes.fromhex(
    "ae68ac7ce30c86ece8196b61a7c486d8f0061f575037fbd34e7fe4e2820c6591"
)

ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"

# Contract deposit limit (enforced by PaymentOperator condition).
# As of 2026-02-03, commerce-payments contracts enforce $100 max per deposit.
DEPOSIT_LIMIT_USDC = 100_000_000  # $100 in atomic units (6 decimals)

# ============================================================
# Multi-chain Escrow Contract Registry
# ============================================================
# Contract addresses from x402r-sdk (A1igator/multichain-config).
# Keys:
#   escrow           - AuthCaptureEscrow contract
#   operator_factory - PaymentOperatorFactory contract
#   token_collector  - TokenCollector contract
#   protocol_fee_config - ProtocolFeeConfig contract
#   refund_request   - RefundRequest contract
#   usdc             - USDC token address

ESCROW_CONTRACTS: dict[int, dict[str, str]] = {
    # ----- Testnets -----
    84532: {  # Base Sepolia
        "escrow": "0x29025c0E9D4239d438e169570818dB9FE0A80873",
        "operator_factory": "0x97d53e63A9CB97556c00BeFd325AF810c9b267B2",
        "token_collector": "0x5cA789000070DF15b4663DB64a50AeF5D49c5Ee0",
        "protocol_fee_config": "0x8F96C493bAC365E41f0315cf45830069EBbDCaCe",
        "refund_request": "0x1C2Ab244aC8bDdDB74d43389FF34B118aF2E90F4",
        "usdc": "0x036CbD53842c5426634e7929541eC2318f3dCF7e",
    },
    11155111: {  # Ethereum Sepolia
        "escrow": "0x320a3c35F131E5D2Fb36af56345726B298936037",
        "operator_factory": "0x32d6AC59BCe8DFB3026F10BcaDB8D00AB218f5b6",
        "token_collector": "0x230fd3A171750FA45db2976121376b7F47Cba308",
        "protocol_fee_config": "0xD979dBfBdA5f4b16AAF60Eaab32A44f352076838",
        "refund_request": "0xc1256Bb30bd0cdDa07D8C8Cf67a59105f2EA1b98",
        "usdc": "0x1c7D4B196Cb0C7B01d743Fbc6116a902379C7238",
    },
    # ----- Mainnets -----
    8453: {  # Base Mainnet
        "escrow": "0xb9488351E48b23D798f24e8174514F28B741Eb4f",
        "operator_factory": "0x3D0837fF8Ea36F417261577b9BA568400A840260",
        "token_collector": "0x48ADf6E37F9b31dC2AAD0462C5862B5422C736B8",
        "protocol_fee_config": "0x59314674BAbb1a24Eb2704468a9cCdD50668a1C6",
        "refund_request": "0x35fb2EFEfAc3Ee9f6E52A9AAE5C9655bC08dEc00",
        "usdc": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
    },
    1: {  # Ethereum Mainnet
        "escrow": "0xc1256Bb30bd0cdDa07D8C8Cf67a59105f2EA1b98",
        "operator_factory": "0xed02d3E5167BCc9582D851885A89b050AB816a56",
        "token_collector": "0xE78648e7af7B1BaDE717FF6E410B922F92adE80f",
        "protocol_fee_config": "0xb33D6502EdBbC47201cd1E53C49d703EC0a660b8",
        "refund_request": "0xc9BbA6A2CF9838e7Dd8c19BC8B3BAC620B9D8178",
        "usdc": "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
    },
    137: {  # Polygon
        "escrow": "0x32d6AC59BCe8DFB3026F10BcaDB8D00AB218f5b6",
        "operator_factory": "0xb33D6502EdBbC47201cd1E53C49d703EC0a660b8",
        "token_collector": "0xc1256Bb30bd0cdDa07D8C8Cf67a59105f2EA1b98",
        "protocol_fee_config": "0xE78648e7af7B1BaDE717FF6E410B922F92adE80f",
        "refund_request": "0xed02d3E5167BCc9582D851885A89b050AB816a56",
        "usdc": "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359",
    },
    42161: {  # Arbitrum
        "escrow": "0x320a3c35F131E5D2Fb36af56345726B298936037",
        "operator_factory": "0x32d6AC59BCe8DFB3026F10BcaDB8D00AB218f5b6",
        "token_collector": "0x230fd3A171750FA45db2976121376b7F47Cba308",
        "protocol_fee_config": "0xD979dBfBdA5f4b16AAF60Eaab32A44f352076838",
        "refund_request": "0xc1256Bb30bd0cdDa07D8C8Cf67a59105f2EA1b98",
        "usdc": "0xaf88d065e77c8cC2239327C5EDb3A432268e5831",
    },
    42220: {  # Celo
        "escrow": "0x320a3c35F131E5D2Fb36af56345726B298936037",
        "operator_factory": "0x32d6AC59BCe8DFB3026F10BcaDB8D00AB218f5b6",
        "token_collector": "0x230fd3A171750FA45db2976121376b7F47Cba308",
        "protocol_fee_config": "0xD979dBfBdA5f4b16AAF60Eaab32A44f352076838",
        "refund_request": "0xc1256Bb30bd0cdDa07D8C8Cf67a59105f2EA1b98",
        "usdc": "0xcebA9300f2b948710d2653dD7B07f33A8B32118C",
    },
    143: {  # Monad
        "escrow": "0x320a3c35F131E5D2Fb36af56345726B298936037",
        "operator_factory": "0x32d6AC59BCe8DFB3026F10BcaDB8D00AB218f5b6",
        "token_collector": "0x230fd3A171750FA45db2976121376b7F47Cba308",
        "protocol_fee_config": "0xD979dBfBdA5f4b16AAF60Eaab32A44f352076838",
        "refund_request": "0xc1256Bb30bd0cdDa07D8C8Cf67a59105f2EA1b98",
        "usdc": "0x754704Bc059F8C67012fEd69BC8A327a5aafb603",
    },
    43114: {  # Avalanche
        "escrow": "0x320a3c35F131E5D2Fb36af56345726B298936037",
        "operator_factory": "0x32d6AC59BCe8DFB3026F10BcaDB8D00AB218f5b6",
        "token_collector": "0x230fd3A171750FA45db2976121376b7F47Cba308",
        "protocol_fee_config": "0xD979dBfBdA5f4b16AAF60Eaab32A44f352076838",
        "refund_request": "0xc1256Bb30bd0cdDa07D8C8Cf67a59105f2EA1b98",
        "usdc": "0xB97EF9Ef8734C71904D8002F8b6Bc66Dd9c48a6E",
    },
}

# Human-readable chain names for diagnostics and error messages.
ESCROW_CHAIN_NAMES: dict[int, str] = {
    84532: "Base Sepolia",
    11155111: "Ethereum Sepolia",
    8453: "Base Mainnet",
    1: "Ethereum Mainnet",
    137: "Polygon",
    42161: "Arbitrum",
    42220: "Celo",
    143: "Monad",
    43114: "Avalanche",
}

# Base Mainnet contract addresses (default, backward-compatible).
# NOTE: "operator" is a legacy key pointing to a previously deployed
# PaymentOperator instance.  New integrations should use "operator_factory"
# from ESCROW_CONTRACTS and deploy their own operator via the factory.
BASE_MAINNET_CONTRACTS = {
    "operator": "0xa06958D93135BEd7e43893897C0d9fA931EF051C",
    "escrow": "0xb9488351E48b23D798f24e8174514F28B741Eb4f",
    "operator_factory": "0x3D0837fF8Ea36F417261577b9BA568400A840260",
    "token_collector": "0x48ADf6E37F9b31dC2AAD0462C5862B5422C736B8",
    "protocol_fee_config": "0x59314674BAbb1a24Eb2704468a9cCdD50668a1C6",
    "refund_request": "0x35fb2EFEfAc3Ee9f6E52A9AAE5C9655bC08dEc00",
    "usdc": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
}


def get_escrow_contracts(chain_id: int) -> dict[str, str]:
    """
    Look up escrow contract addresses for a given chain ID.

    Args:
        chain_id: EVM chain ID (e.g. 8453 for Base Mainnet)

    Returns:
        Dict of contract name -> address

    Raises:
        ValueError: If chain_id is not in the registry
    """
    if chain_id not in ESCROW_CONTRACTS:
        supported = ", ".join(
            f"{cid} ({ESCROW_CHAIN_NAMES.get(cid, 'unknown')})"
            for cid in sorted(ESCROW_CONTRACTS)
        )
        raise ValueError(
            f"No escrow contracts for chain {chain_id}. "
            f"Supported chains: {supported}"
        )
    return ESCROW_CONTRACTS[chain_id]


def get_supported_escrow_chains() -> list[int]:
    """Return sorted list of chain IDs that have escrow contracts deployed."""
    return sorted(ESCROW_CONTRACTS.keys())


def is_escrow_supported(chain_id: int) -> bool:
    """Check whether escrow contracts are deployed on the given chain."""
    return chain_id in ESCROW_CONTRACTS

# PaymentOperator ABI (minimal, for the 5 functions we need)
OPERATOR_ABI = [
    {
        "type": "function",
        "name": "release",
        "inputs": [
            {
                "name": "paymentInfo",
                "type": "tuple",
                "components": [
                    {"name": "operator", "type": "address"},
                    {"name": "payer", "type": "address"},
                    {"name": "receiver", "type": "address"},
                    {"name": "token", "type": "address"},
                    {"name": "maxAmount", "type": "uint120"},
                    {"name": "preApprovalExpiry", "type": "uint48"},
                    {"name": "authorizationExpiry", "type": "uint48"},
                    {"name": "refundExpiry", "type": "uint48"},
                    {"name": "minFeeBps", "type": "uint16"},
                    {"name": "maxFeeBps", "type": "uint16"},
                    {"name": "feeReceiver", "type": "address"},
                    {"name": "salt", "type": "uint256"},
                ],
            },
            {"name": "amount", "type": "uint256"},
        ],
        "outputs": [],
        "stateMutability": "nonpayable",
    },
    {
        "type": "function",
        "name": "refundInEscrow",
        "inputs": [
            {
                "name": "paymentInfo",
                "type": "tuple",
                "components": [
                    {"name": "operator", "type": "address"},
                    {"name": "payer", "type": "address"},
                    {"name": "receiver", "type": "address"},
                    {"name": "token", "type": "address"},
                    {"name": "maxAmount", "type": "uint120"},
                    {"name": "preApprovalExpiry", "type": "uint48"},
                    {"name": "authorizationExpiry", "type": "uint48"},
                    {"name": "refundExpiry", "type": "uint48"},
                    {"name": "minFeeBps", "type": "uint16"},
                    {"name": "maxFeeBps", "type": "uint16"},
                    {"name": "feeReceiver", "type": "address"},
                    {"name": "salt", "type": "uint256"},
                ],
            },
            {"name": "amount", "type": "uint120"},
        ],
        "outputs": [],
        "stateMutability": "nonpayable",
    },
    {
        "type": "function",
        "name": "charge",
        "inputs": [
            {
                "name": "paymentInfo",
                "type": "tuple",
                "components": [
                    {"name": "operator", "type": "address"},
                    {"name": "payer", "type": "address"},
                    {"name": "receiver", "type": "address"},
                    {"name": "token", "type": "address"},
                    {"name": "maxAmount", "type": "uint120"},
                    {"name": "preApprovalExpiry", "type": "uint48"},
                    {"name": "authorizationExpiry", "type": "uint48"},
                    {"name": "refundExpiry", "type": "uint48"},
                    {"name": "minFeeBps", "type": "uint16"},
                    {"name": "maxFeeBps", "type": "uint16"},
                    {"name": "feeReceiver", "type": "address"},
                    {"name": "salt", "type": "uint256"},
                ],
            },
            {"name": "amount", "type": "uint256"},
            {"name": "tokenCollector", "type": "address"},
            {"name": "collectorData", "type": "bytes"},
        ],
        "outputs": [],
        "stateMutability": "nonpayable",
    },
    {
        "type": "function",
        "name": "refundPostEscrow",
        "inputs": [
            {
                "name": "paymentInfo",
                "type": "tuple",
                "components": [
                    {"name": "operator", "type": "address"},
                    {"name": "payer", "type": "address"},
                    {"name": "receiver", "type": "address"},
                    {"name": "token", "type": "address"},
                    {"name": "maxAmount", "type": "uint120"},
                    {"name": "preApprovalExpiry", "type": "uint48"},
                    {"name": "authorizationExpiry", "type": "uint48"},
                    {"name": "refundExpiry", "type": "uint48"},
                    {"name": "minFeeBps", "type": "uint16"},
                    {"name": "maxFeeBps", "type": "uint16"},
                    {"name": "feeReceiver", "type": "address"},
                    {"name": "salt", "type": "uint256"},
                ],
            },
            {"name": "amount", "type": "uint256"},
            {"name": "tokenCollector", "type": "address"},
            {"name": "collectorData", "type": "bytes"},
        ],
        "outputs": [],
        "stateMutability": "nonpayable",
    },
]


# ============================================================
# Types
# ============================================================


class TaskTier(str, Enum):
    """Chamba task tier determines timing parameters."""

    MICRO = "micro"  # $0.50-$5: 1h accept, 2h complete, 24h dispute
    STANDARD = "standard"  # $5-$50: 2h accept, 24h complete, 7d dispute
    PREMIUM = "premium"  # $50-$200: 4h accept, 48h complete, 14d dispute
    ENTERPRISE = "enterprise"  # $200+: 24h accept, 7d complete, 30d dispute


TIER_TIMINGS = {
    TaskTier.MICRO: {"pre": 3600, "auth": 7200, "refund": 86400},
    TaskTier.STANDARD: {"pre": 7200, "auth": 86400, "refund": 604800},
    TaskTier.PREMIUM: {"pre": 14400, "auth": 172800, "refund": 1209600},
    TaskTier.ENTERPRISE: {"pre": 86400, "auth": 604800, "refund": 2592000},
}


@dataclass
class PaymentInfo:
    """PaymentInfo struct matching the on-chain PaymentOperator contract."""

    operator: str
    receiver: str
    token: str
    max_amount: int
    pre_approval_expiry: int
    authorization_expiry: int
    refund_expiry: int
    min_fee_bps: int = 0
    max_fee_bps: int = 800
    fee_receiver: str = ""
    salt: str = field(default_factory=lambda: "0x" + secrets.token_hex(32))

    def __post_init__(self):
        if not self.fee_receiver:
            self.fee_receiver = self.operator


@dataclass
class AuthorizationResult:
    """Result of an AUTHORIZE operation."""

    success: bool
    transaction_hash: Optional[str] = None
    payment_info: Optional[PaymentInfo] = None
    salt: Optional[str] = None
    error: Optional[str] = None


@dataclass
class TransactionResult:
    """Result of an on-chain transaction."""

    success: bool
    transaction_hash: Optional[str] = None
    gas_used: Optional[int] = None
    error: Optional[str] = None


# ============================================================
# Client
# ============================================================


class AdvancedEscrowClient:
    """
    Client for x402 Advanced Escrow (PaymentOperator) operations.

    Provides the 5 on-chain escrow flows:
    - authorize(): Lock funds in escrow via facilitator
    - release(): Capture escrowed funds to receiver (requires gas)
    - refund_in_escrow(): Return escrowed funds to payer (requires gas)
    - charge(): Direct instant payment (no escrow, requires gas)
    - refund_post_escrow(): Dispute refund after release (requires gas)

    Plus 3 gasless facilitator-proxied flows (v1.32.0+):
    - release_via_facilitator(): Gasless release through the facilitator
    - refund_via_facilitator(): Gasless refund through the facilitator
    - query_escrow_state(): Read-only escrow state query (no gas)
    """

    def __init__(
        self,
        private_key: str,
        *,
        facilitator_url: str = "https://facilitator.ultravioletadao.xyz",
        rpc_url: str = "https://mainnet.base.org",
        chain_id: int = 8453,
        contracts: Optional[dict] = None,
        operator_address: Optional[str] = None,
        gas_limit: int = 300000,
    ):
        """
        Initialize the Advanced Escrow client.

        Args:
            private_key: Hex-encoded private key for signing
            facilitator_url: Facilitator endpoint URL
            rpc_url: JSON-RPC endpoint for the target chain
            chain_id: EVM chain ID (e.g. 8453 for Base, 1 for Ethereum)
            contracts: Explicit contract address dict (overrides registry lookup).
                       Must contain at least "operator", "escrow", "token_collector", "usdc".
            operator_address: PaymentOperator instance address. Required when using
                              the multi-chain registry (which only provides the factory).
                              Ignored when explicit ``contracts`` dict is passed.
            gas_limit: Gas limit for on-chain transactions
        """
        self.private_key = private_key
        self.facilitator_url = facilitator_url.rstrip("/")
        self.chain_id = chain_id
        self.gas_limit = gas_limit

        if contracts is not None:
            # Caller supplied an explicit contracts dict - use as-is.
            self.contracts = contracts
        elif chain_id in ESCROW_CONTRACTS:
            # Resolve from the multi-chain registry.
            registry = ESCROW_CONTRACTS[chain_id]
            if operator_address is None and chain_id == 8453:
                # Backward-compatible default: use the legacy Base Mainnet
                # PaymentOperator instance when no operator is specified.
                operator_address = BASE_MAINNET_CONTRACTS["operator"]
            if operator_address is None:
                chain_name = ESCROW_CHAIN_NAMES.get(chain_id, str(chain_id))
                raise ValueError(
                    f"operator_address is required for chain {chain_name} "
                    f"(chain_id={chain_id}). Deploy a PaymentOperator via the "
                    f"factory at {registry['operator_factory']} and pass its "
                    f"address here."
                )
            self.contracts = {
                "operator": operator_address,
                "escrow": registry["escrow"],
                "operator_factory": registry["operator_factory"],
                "token_collector": registry["token_collector"],
                "protocol_fee_config": registry["protocol_fee_config"],
                "refund_request": registry["refund_request"],
                "usdc": registry["usdc"],
            }
        else:
            # Fall back to the legacy Base Mainnet default.
            self.contracts = BASE_MAINNET_CONTRACTS

        self.w3 = Web3(Web3.HTTPProvider(rpc_url))
        self.account = Account.from_key(private_key)
        self.payer = self.account.address

        self.operator_contract = self.w3.eth.contract(
            address=Web3.to_checksum_address(self.contracts["operator"]),
            abi=OPERATOR_ABI,
        )

    def _compute_nonce(self, payment_info: PaymentInfo) -> str:
        """Compute the correct nonce (with PAYMENT_INFO_TYPEHASH)."""
        salt = payment_info.salt
        if isinstance(salt, str):
            salt = int(salt, 16) if salt.startswith("0x") else int(salt)

        pi_tuple = (
            Web3.to_checksum_address(payment_info.operator),
            ZERO_ADDRESS,  # payer = 0 for payer-agnostic hash
            Web3.to_checksum_address(payment_info.receiver),
            Web3.to_checksum_address(payment_info.token),
            payment_info.max_amount,
            payment_info.pre_approval_expiry,
            payment_info.authorization_expiry,
            payment_info.refund_expiry,
            payment_info.min_fee_bps,
            payment_info.max_fee_bps,
            Web3.to_checksum_address(payment_info.fee_receiver),
            salt,
        )

        encoded_with_typehash = encode(
            [
                "bytes32",
                "(address,address,address,address,uint120,uint48,uint48,uint48,uint16,uint16,address,uint256)",
            ],
            [PAYMENT_INFO_TYPEHASH, pi_tuple],
        )
        pi_hash = Web3.keccak(encoded_with_typehash)

        final_encoded = encode(
            ["uint256", "address", "bytes32"],
            [self.chain_id, Web3.to_checksum_address(self.contracts["escrow"]), pi_hash],
        )
        raw = Web3.keccak(final_encoded).hex()
        # HexBytes.hex() already includes 0x in web3 >= 6.x / hexbytes >= 1.x
        return raw if raw.startswith("0x") else "0x" + raw

    def _sign_erc3009(self, auth: dict) -> str:
        """Sign ReceiveWithAuthorization for ERC-3009."""
        domain = {
            "name": "USD Coin",
            "version": "2",
            "chainId": self.chain_id,
            "verifyingContract": Web3.to_checksum_address(self.contracts["usdc"]),
        }
        types = {
            "ReceiveWithAuthorization": [
                {"name": "from", "type": "address"},
                {"name": "to", "type": "address"},
                {"name": "value", "type": "uint256"},
                {"name": "validAfter", "type": "uint256"},
                {"name": "validBefore", "type": "uint256"},
                {"name": "nonce", "type": "bytes32"},
            ],
        }
        # Convert nonce to bytes for bytes32 encoding (eth_account >= 0.10.0
        # requires bytes, not hex strings, for bytes32 fields)
        nonce = auth["nonce"]
        if isinstance(nonce, str):
            nonce = bytes.fromhex(nonce.removeprefix("0x"))

        message = {
            "from": Web3.to_checksum_address(auth["from"]),
            "to": Web3.to_checksum_address(auth["to"]),
            "value": int(auth["value"]),
            "validAfter": int(auth["validAfter"]),
            "validBefore": int(auth["validBefore"]),
            "nonce": nonce,
        }
        signable = encode_typed_data(domain_data=domain, message_types=types, message_data=message)
        signed = self.account.sign_message(signable)
        sig_hex = signed.signature.hex()
        # HexBytes.hex() may include 0x prefix in newer versions
        return sig_hex if sig_hex.startswith("0x") else "0x" + sig_hex

    def _build_tuple(self, pi: PaymentInfo) -> tuple:
        """Build the on-chain PaymentInfo tuple."""
        if isinstance(pi.salt, int):
            salt_int = pi.salt
        elif isinstance(pi.salt, str):
            salt_int = int(pi.salt, 16) if pi.salt.startswith("0x") else int(pi.salt, 16) if all(c in "0123456789abcdefABCDEF" for c in pi.salt) else int(pi.salt)
        else:
            salt_int = int(pi.salt)
        return (
            Web3.to_checksum_address(pi.operator),
            Web3.to_checksum_address(self.payer),
            Web3.to_checksum_address(pi.receiver),
            Web3.to_checksum_address(pi.token),
            pi.max_amount,
            pi.pre_approval_expiry,
            pi.authorization_expiry,
            pi.refund_expiry,
            pi.min_fee_bps,
            pi.max_fee_bps,
            Web3.to_checksum_address(pi.fee_receiver),
            salt_int,
        )

    def _send_tx(self, func_call) -> TransactionResult:
        """Build, sign, and send a transaction."""
        try:
            gas_price = self.w3.eth.gas_price
            tx = func_call.build_transaction({
                "from": self.payer,
                "nonce": self.w3.eth.get_transaction_count(self.payer),
                "gas": self.gas_limit,
                "maxFeePerGas": gas_price * 2,
                "maxPriorityFeePerGas": gas_price,
            })
            signed = self.w3.eth.account.sign_transaction(tx, self.private_key)
            tx_hash = self.w3.eth.send_raw_transaction(signed.raw_transaction)
            receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)

            if receipt["status"] != 1:
                return TransactionResult(success=False, transaction_hash=tx_hash.hex(), gas_used=receipt["gasUsed"], error="Transaction reverted")

            return TransactionResult(success=True, transaction_hash=tx_hash.hex(), gas_used=receipt["gasUsed"])
        except Exception as e:
            return TransactionResult(success=False, error=str(e))

    def build_payment_info(
        self,
        receiver: str,
        amount: int,
        *,
        tier: TaskTier = TaskTier.STANDARD,
        salt: Optional[str] = None,
        min_fee_bps: int = 0,
        max_fee_bps: int = 800,
    ) -> PaymentInfo:
        """
        Build a PaymentInfo struct with appropriate timing for the task tier.

        Args:
            receiver: Worker's wallet address
            amount: Amount in token atomic units (e.g., 5_000_000 for $5 USDC)
            tier: Task tier (determines timing parameters)
            salt: Random salt (auto-generated if not provided)
            min_fee_bps: Minimum fee in basis points
            max_fee_bps: Maximum fee in basis points
        """
        now = int(time.time())
        t = TIER_TIMINGS[tier]

        return PaymentInfo(
            operator=self.contracts["operator"],
            receiver=receiver,
            token=self.contracts["usdc"],
            max_amount=amount,
            pre_approval_expiry=now + t["pre"],
            authorization_expiry=now + t["auth"],
            refund_expiry=now + t["refund"],
            min_fee_bps=min_fee_bps,
            max_fee_bps=max_fee_bps,
            fee_receiver=self.contracts["operator"],
            salt=salt or ("0x" + secrets.token_hex(32)),
        )

    def authorize(self, payment_info: PaymentInfo) -> AuthorizationResult:
        """
        AUTHORIZE: Lock funds in escrow via the facilitator.

        This sends an ERC-3009 ReceiveWithAuthorization to the facilitator,
        which calls PaymentOperator.authorize() on-chain.

        Args:
            payment_info: PaymentInfo struct with timing and amount

        Returns:
            AuthorizationResult with transaction hash
        """
        nonce = self._compute_nonce(payment_info)

        auth = {
            "from": self.payer,
            "to": self.contracts["token_collector"],
            "value": str(payment_info.max_amount),
            "validAfter": "0",
            "validBefore": str(payment_info.pre_approval_expiry),
            "nonce": nonce,
        }
        signature = self._sign_erc3009(auth)

        pi_dict = {
            "operator": payment_info.operator,
            "receiver": payment_info.receiver,
            "token": payment_info.token,
            "maxAmount": str(payment_info.max_amount),
            "preApprovalExpiry": payment_info.pre_approval_expiry,
            "authorizationExpiry": payment_info.authorization_expiry,
            "refundExpiry": payment_info.refund_expiry,
            "minFeeBps": payment_info.min_fee_bps,
            "maxFeeBps": payment_info.max_fee_bps,
            "feeReceiver": payment_info.fee_receiver,
            "salt": payment_info.salt,
        }

        payload = {
            "x402Version": 2,
            "scheme": "escrow",
            "payload": {
                "authorization": auth,
                "signature": signature,
                "paymentInfo": pi_dict,
            },
            "paymentRequirements": {
                "scheme": "escrow",
                "network": f"eip155:{self.chain_id}",
                "maxAmountRequired": str(payment_info.max_amount),
                "asset": self.contracts["usdc"],
                "payTo": payment_info.receiver,
                "extra": {
                    "escrowAddress": self.contracts["escrow"],
                    "operatorAddress": self.contracts["operator"],
                    "tokenCollector": self.contracts["token_collector"],
                },
            },
        }

        try:
            response = httpx.post(
                f"{self.facilitator_url}/settle",
                json=payload,
                timeout=120,
            )
            result = response.json()

            if result.get("success"):
                return AuthorizationResult(
                    success=True,
                    transaction_hash=result.get("transaction"),
                    payment_info=payment_info,
                    salt=payment_info.salt,
                )
            else:
                return AuthorizationResult(success=False, error=result.get("errorReason"))
        except Exception as e:
            return AuthorizationResult(success=False, error=str(e))

    def release(self, payment_info: PaymentInfo, amount: Optional[int] = None) -> TransactionResult:
        """
        RELEASE: Capture escrowed funds to receiver (worker gets paid).

        Calls PaymentOperator.release() -> escrow.capture()

        Args:
            payment_info: PaymentInfo from the authorize step
            amount: Amount to release (defaults to max_amount)
        """
        pt = self._build_tuple(payment_info)
        amt = amount or payment_info.max_amount
        return self._send_tx(self.operator_contract.functions.release(pt, amt))

    def refund_in_escrow(self, payment_info: PaymentInfo, amount: Optional[int] = None) -> TransactionResult:
        """
        REFUND IN ESCROW: Return escrowed funds to payer (cancel task).

        Calls PaymentOperator.refundInEscrow() -> escrow.partialVoid()

        Args:
            payment_info: PaymentInfo from the authorize step
            amount: Amount to refund (defaults to max_amount)
        """
        pt = self._build_tuple(payment_info)
        amt = amount or payment_info.max_amount
        return self._send_tx(self.operator_contract.functions.refundInEscrow(pt, amt))

    # ----------------------------------------------------------------
    # Gasless facilitator-proxied methods (v1.32.0+)
    # ----------------------------------------------------------------

    def _payment_info_to_camel_dict(self, pi: PaymentInfo) -> dict:
        """Convert a PaymentInfo dataclass to a camelCase dict for the facilitator API."""
        return {
            "operator": pi.operator,
            "receiver": pi.receiver,
            "token": pi.token,
            "maxAmount": str(pi.max_amount),
            "preApprovalExpiry": pi.pre_approval_expiry,
            "authorizationExpiry": pi.authorization_expiry,
            "refundExpiry": pi.refund_expiry,
            "minFeeBps": pi.min_fee_bps,
            "maxFeeBps": pi.max_fee_bps,
            "feeReceiver": pi.fee_receiver,
            "salt": pi.salt,
        }

    def _settle_via_facilitator(
        self, action: str, payment_info: PaymentInfo, amount: Optional[int] = None
    ) -> TransactionResult:
        """
        Internal: send a gasless settle request to the facilitator.

        Args:
            action: The settle action ("release" or "refundInEscrow")
            payment_info: PaymentInfo from the authorize step
            amount: Amount in atomic units (defaults to max_amount)
        """
        amt = amount if amount is not None else payment_info.max_amount

        payload = {
            "x402Version": 2,
            "scheme": "escrow",
            "action": action,
            "payload": {
                "paymentInfo": self._payment_info_to_camel_dict(payment_info),
                "payer": self.payer,
                "amount": str(amt),
            },
            "paymentRequirements": {
                "scheme": "escrow",
                "network": f"eip155:{self.chain_id}",
                "extra": {
                    "escrowAddress": self.contracts["escrow"],
                    "operatorAddress": self.contracts["operator"],
                    "tokenCollector": self.contracts["token_collector"],
                },
            },
        }

        try:
            response = httpx.post(
                f"{self.facilitator_url}/settle",
                json=payload,
                timeout=120,
            )
            result = response.json()

            if result.get("success"):
                return TransactionResult(
                    success=True,
                    transaction_hash=result.get("transaction"),
                )
            else:
                return TransactionResult(
                    success=False,
                    error=result.get("errorReason", result.get("error", "Unknown error")),
                )
        except Exception as e:
            return TransactionResult(success=False, error=str(e))

    def release_via_facilitator(
        self, payment_info: PaymentInfo, amount: Optional[int] = None
    ) -> TransactionResult:
        """
        RELEASE via facilitator (GASLESS) - Send escrowed funds to receiver.

        Unlike release() which requires gas + operator private key, this method
        goes through the facilitator which pays all gas fees.

        Requires facilitator v1.32.0+ with escrow settle support.

        Args:
            payment_info: PaymentInfo from the authorize step
            amount: Amount to release in atomic units (defaults to max_amount)

        Returns:
            TransactionResult with the on-chain transaction hash
        """
        return self._settle_via_facilitator("release", payment_info, amount)

    def refund_via_facilitator(
        self, payment_info: PaymentInfo, amount: Optional[int] = None
    ) -> TransactionResult:
        """
        REFUND via facilitator (GASLESS) - Return escrowed funds to payer.

        Unlike refund_in_escrow() which requires gas + operator private key, this
        method goes through the facilitator which pays all gas fees.

        Requires facilitator v1.32.0+ with escrow settle support.

        Args:
            payment_info: PaymentInfo from the authorize step
            amount: Amount to refund in atomic units (defaults to max_amount)

        Returns:
            TransactionResult with the on-chain transaction hash
        """
        return self._settle_via_facilitator("refundInEscrow", payment_info, amount)

    def query_escrow_state(self, payment_info: PaymentInfo) -> dict:
        """
        Query escrow state from the AuthCaptureEscrow contract (read-only, no gas).

        Calls the facilitator's POST /escrow/state endpoint which reads on-chain
        state without submitting a transaction.

        Requires facilitator v1.32.0+ with escrow state query support.

        Args:
            payment_info: PaymentInfo from the authorize step

        Returns:
            Dict with escrow state fields, e.g.::

                {
                    "capturableAmount": "5000000",
                    "refundableAmount": "5000000",
                    "hasCollectedPayment": false
                }

        Raises:
            RuntimeError: If the facilitator returns an error
            httpx.HTTPError: On network/transport errors
        """
        payload = {
            "paymentInfo": self._payment_info_to_camel_dict(payment_info),
            "payer": self.payer,
            "network": f"eip155:{self.chain_id}",
            "extra": {
                "escrowAddress": self.contracts["escrow"],
                "operatorAddress": self.contracts["operator"],
                "tokenCollector": self.contracts["token_collector"],
            },
        }

        response = httpx.post(
            f"{self.facilitator_url}/escrow/state",
            json=payload,
            timeout=30,
        )
        result = response.json()

        if "error" in result or "errorReason" in result:
            raise RuntimeError(
                f"Escrow state query failed: {result.get('errorReason', result.get('error'))}"
            )

        return result

    def charge(self, payment_info: PaymentInfo, amount: Optional[int] = None) -> TransactionResult:
        """
        CHARGE: Direct instant payment (no escrow hold).

        Calls PaymentOperator.charge() -> escrow.charge()
        Funds go directly from payer to receiver.

        Args:
            payment_info: PaymentInfo with receiver and amount
            amount: Amount to charge (defaults to max_amount)
        """
        nonce = self._compute_nonce(payment_info)
        amt = amount or payment_info.max_amount

        auth = {
            "from": self.payer,
            "to": self.contracts["token_collector"],
            "value": str(amt),
            "validAfter": "0",
            "validBefore": str(payment_info.pre_approval_expiry),
            "nonce": nonce,
        }
        signature = self._sign_erc3009(auth)
        collector_data = bytes.fromhex(signature[2:])

        pt = self._build_tuple(payment_info)
        return self._send_tx(
            self.operator_contract.functions.charge(
                pt, amt,
                Web3.to_checksum_address(self.contracts["token_collector"]),
                collector_data,
            )
        )

    def refund_post_escrow(
        self,
        payment_info: PaymentInfo,
        amount: Optional[int] = None,
        token_collector: str = ZERO_ADDRESS,
        collector_data: bytes = b"",
    ) -> TransactionResult:
        """
        REFUND POST ESCROW: Dispute refund after funds were released.

        Calls PaymentOperator.refundPostEscrow() -> escrow.refund()

        WARNING: NOT FUNCTIONAL IN PRODUCTION (as of 2026-02-03).
        The protocol team has not implemented the required tokenCollector
        contract. This call will fail on-chain.

        For dispute resolution, use refund_in_escrow() instead: keep funds
        in escrow and refund before releasing. This guarantees funds are
        available and under arbiter control.

        Kept for future use when tokenCollector is implemented.

        Args:
            payment_info: PaymentInfo from the original authorization
            amount: Amount to refund (defaults to max_amount)
            token_collector: Address of token collector for refund sourcing
            collector_data: Data for the token collector
        """
        pt = self._build_tuple(payment_info)
        amt = amount or payment_info.max_amount
        return self._send_tx(
            self.operator_contract.functions.refundPostEscrow(
                pt, amt,
                Web3.to_checksum_address(token_collector),
                collector_data,
            )
        )
