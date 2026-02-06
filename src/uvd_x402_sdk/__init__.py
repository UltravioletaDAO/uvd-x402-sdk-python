"""
uvd-x402-sdk: Python SDK for x402 payments via Ultravioleta DAO facilitator.

This SDK enables developers to easily integrate x402 cryptocurrency payments
into their Python applications with support for 21 blockchain networks across
6 network types (EVM, SVM, NEAR, Stellar, Algorand, Sui).

The SDK automatically handles facilitator configuration - users don't need to
configure fee payer addresses or other facilitator details manually.

Supports both x402 v1 and v2 protocols:
- v1: network as string ("base", "solana")
- v2: network as CAIP-2 ("eip155:8453", "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp")

New features:
- ERC-8004 Trustless Agents: Submit reputation feedback with proof of payment
- Escrow & Refund: Hold payments in escrow with dispute resolution

Example usage:
    from uvd_x402_sdk import X402Client, require_payment

    # Create a client
    client = X402Client(
        recipient_address="0xYourWallet...",
        facilitator_url="https://facilitator.ultravioletadao.xyz"
    )

    # Verify and settle a payment
    result = client.process_payment(
        x_payment_header=request.headers.get("X-PAYMENT"),
        expected_amount_usd=Decimal("10.00")
    )

    # Or use the decorator
    @require_payment(amount_usd=Decimal("1.00"))
    def protected_endpoint():
        return {"message": "Payment verified!"}

Supported Networks (21 total):
- EVM (13): Base, Ethereum, Polygon, Arbitrum, Optimism, Avalanche, Celo,
            HyperEVM, Unichain, Monad, Scroll, SKALE, SKALE Testnet
- SVM (2): Solana, Fogo
- NEAR (1): NEAR Protocol
- Stellar (1): Stellar
- Algorand (2): Algorand mainnet, Algorand testnet
- Sui (2): Sui mainnet, Sui testnet
"""

__version__ = "0.8.1"
__author__ = "Ultravioleta DAO"

from uvd_x402_sdk.client import X402Client
from uvd_x402_sdk.config import X402Config, NetworkConfig, MultiPaymentConfig
from uvd_x402_sdk.decorators import require_payment, x402_required, configure_x402
from uvd_x402_sdk.exceptions import (
    X402Error,
    PaymentRequiredError,
    PaymentVerificationError,
    PaymentSettlementError,
    UnsupportedNetworkError,
    InvalidPayloadError,
    FacilitatorError,
    ConfigurationError,
    TimeoutError as X402TimeoutError,
)
from uvd_x402_sdk.models import (
    # Payload models
    PaymentPayload,
    EVMPayloadContent,
    SVMPayloadContent,
    SolanaPayloadContent,  # Alias for backward compatibility
    NEARPayloadContent,
    StellarPayloadContent,
    SuiPayloadContent,
    # Requirements models (v1)
    PaymentRequirements,
    # Requirements models (v2)
    PaymentOption,
    PaymentRequirementsV2,
    # Request/Response models
    VerifyRequest,
    VerifyResponse,
    SettleRequest,
    SettleResponse,
    PaymentResult,
)
from uvd_x402_sdk.networks import (
    SUPPORTED_NETWORKS,
    get_network,
    get_network_by_chain_id,
    register_network,
    list_networks,
    get_supported_chain_ids,
    get_supported_network_names,
    NetworkType,
    # Token types (multi-stablecoin support)
    TokenType,
    TokenConfig,
    ALL_TOKEN_TYPES,
    get_token_config,
    get_supported_tokens,
    is_token_supported,
    get_networks_by_token,
    # CAIP-2 utilities (v2 support)
    parse_caip2_network,
    to_caip2_network,
    is_caip2_format,
    normalize_network,
)
from uvd_x402_sdk.response import (
    # v1 response helpers
    create_402_response,
    create_402_headers,
    payment_required_response,
    Payment402Builder,
    # v2 response helpers
    create_402_response_v2,
    create_402_headers_v2,
    payment_required_response_v2,
    Payment402BuilderV2,
)
from uvd_x402_sdk.facilitator import (
    # Facilitator URL and constants
    DEFAULT_FACILITATOR_URL,
    get_facilitator_url,
    # Fee payer addresses by chain
    ALGORAND_FEE_PAYER_MAINNET,
    ALGORAND_FEE_PAYER_TESTNET,
    SOLANA_FEE_PAYER_MAINNET,
    SOLANA_FEE_PAYER_DEVNET,
    FOGO_FEE_PAYER_MAINNET,
    FOGO_FEE_PAYER_TESTNET,
    NEAR_FEE_PAYER_MAINNET,
    NEAR_FEE_PAYER_TESTNET,
    STELLAR_FEE_PAYER_MAINNET,
    STELLAR_FEE_PAYER_TESTNET,
    SUI_FEE_PAYER_MAINNET,
    SUI_FEE_PAYER_TESTNET,
    # EVM facilitator addresses (for reference)
    EVM_FACILITATOR_MAINNET,
    EVM_FACILITATOR_TESTNET,
    # Helper functions
    get_fee_payer,
    get_facilitator_address,
    requires_fee_payer,
    get_all_fee_payers,
    build_payment_info,
)

# ERC-8004 Trustless Agents support
from uvd_x402_sdk.erc8004 import (
    Erc8004Client,
    ERC8004_EXTENSION_ID,
    ERC8004_CONTRACTS,
    ProofOfPayment,
    AgentIdentity,
    AgentRegistrationFile,
    ReputationSummary,
    FeedbackEntry,
    FeedbackParams,
    FeedbackRequest,
    FeedbackResponse,
    ReputationResponse,
    SettleResponseWithProof,
    build_erc8004_payment_requirements,
)

# Escrow & Refund support
from uvd_x402_sdk.escrow import (
    EscrowClient,
    EscrowPayment,
    EscrowStatus,
    RefundRequest,
    RefundStatus,
    Dispute,
    DisputeOutcome,
    ReleaseConditions,
    RefundResponse,
    EscrowListResponse,
    can_release_escrow,
    can_refund_escrow,
    is_escrow_expired,
    escrow_time_remaining,
)

# Advanced Escrow (PaymentOperator - on-chain escrow)
# Requires: eth_abi, eth_account, web3, httpx
try:
    from uvd_x402_sdk.advanced_escrow import (
        AdvancedEscrowClient,
        PaymentInfo,
        TaskTier,
        AuthorizationResult,
        TransactionResult,
        TIER_TIMINGS,
        BASE_MAINNET_CONTRACTS,
        OPERATOR_ABI,
        DEPOSIT_LIMIT_USDC,
    )
    ADVANCED_ESCROW_AVAILABLE = True
except ImportError:
    ADVANCED_ESCROW_AVAILABLE = False

__all__ = [
    # Version
    "__version__",
    # Main client
    "X402Client",
    # Configuration
    "X402Config",
    "NetworkConfig",
    "MultiPaymentConfig",
    # Decorators
    "require_payment",
    "x402_required",
    "configure_x402",
    # Exceptions
    "X402Error",
    "PaymentRequiredError",
    "PaymentVerificationError",
    "PaymentSettlementError",
    "UnsupportedNetworkError",
    "InvalidPayloadError",
    "FacilitatorError",
    "ConfigurationError",
    "X402TimeoutError",
    # Payload models
    "PaymentPayload",
    "EVMPayloadContent",
    "SVMPayloadContent",
    "SolanaPayloadContent",
    "NEARPayloadContent",
    "StellarPayloadContent",
    "SuiPayloadContent",
    # Requirements models
    "PaymentRequirements",
    "PaymentOption",
    "PaymentRequirementsV2",
    # Request/Response models
    "VerifyRequest",
    "VerifyResponse",
    "SettleRequest",
    "SettleResponse",
    "PaymentResult",
    # Networks
    "SUPPORTED_NETWORKS",
    "get_network",
    "get_network_by_chain_id",
    "register_network",
    "list_networks",
    "get_supported_chain_ids",
    "get_supported_network_names",
    "NetworkType",
    # Token types (multi-stablecoin support)
    "TokenType",
    "TokenConfig",
    "ALL_TOKEN_TYPES",
    "get_token_config",
    "get_supported_tokens",
    "is_token_supported",
    "get_networks_by_token",
    # CAIP-2 utilities
    "parse_caip2_network",
    "to_caip2_network",
    "is_caip2_format",
    "normalize_network",
    # Response helpers (v1)
    "create_402_response",
    "create_402_headers",
    "payment_required_response",
    "Payment402Builder",
    # Response helpers (v2)
    "create_402_response_v2",
    "create_402_headers_v2",
    "payment_required_response_v2",
    "Payment402BuilderV2",
    # Facilitator constants and helpers
    "DEFAULT_FACILITATOR_URL",
    "get_facilitator_url",
    "ALGORAND_FEE_PAYER_MAINNET",
    "ALGORAND_FEE_PAYER_TESTNET",
    "SOLANA_FEE_PAYER_MAINNET",
    "SOLANA_FEE_PAYER_DEVNET",
    "FOGO_FEE_PAYER_MAINNET",
    "FOGO_FEE_PAYER_TESTNET",
    "NEAR_FEE_PAYER_MAINNET",
    "NEAR_FEE_PAYER_TESTNET",
    "STELLAR_FEE_PAYER_MAINNET",
    "STELLAR_FEE_PAYER_TESTNET",
    "SUI_FEE_PAYER_MAINNET",
    "SUI_FEE_PAYER_TESTNET",
    "EVM_FACILITATOR_MAINNET",
    "EVM_FACILITATOR_TESTNET",
    "get_fee_payer",
    "get_facilitator_address",
    "requires_fee_payer",
    "get_all_fee_payers",
    "build_payment_info",
    # ERC-8004 Trustless Agents
    "Erc8004Client",
    "ERC8004_EXTENSION_ID",
    "ERC8004_CONTRACTS",
    "ProofOfPayment",
    "AgentIdentity",
    "AgentRegistrationFile",
    "ReputationSummary",
    "FeedbackEntry",
    "FeedbackParams",
    "FeedbackRequest",
    "FeedbackResponse",
    "ReputationResponse",
    "SettleResponseWithProof",
    "build_erc8004_payment_requirements",
    # Escrow & Refund
    "EscrowClient",
    "EscrowPayment",
    "EscrowStatus",
    "RefundRequest",
    "RefundStatus",
    "Dispute",
    "DisputeOutcome",
    "ReleaseConditions",
    "RefundResponse",
    "EscrowListResponse",
    "can_release_escrow",
    "can_refund_escrow",
    "is_escrow_expired",
    "escrow_time_remaining",
    # Advanced Escrow (PaymentOperator) - available when eth_abi/web3/httpx installed
    "ADVANCED_ESCROW_AVAILABLE",
    "AdvancedEscrowClient",
    "PaymentInfo",
    "TaskTier",
    "AuthorizationResult",
    "TransactionResult",
    "TIER_TIMINGS",
    "BASE_MAINNET_CONTRACTS",
    "OPERATOR_ABI",
]

# Conditionally remove Advanced Escrow names from __all__ if not available
if not ADVANCED_ESCROW_AVAILABLE:
    _advanced_names = {
        "AdvancedEscrowClient", "PaymentInfo", "TaskTier",
        "AuthorizationResult", "TransactionResult", "TIER_TIMINGS",
        "BASE_MAINNET_CONTRACTS", "OPERATOR_ABI",
    }
    __all__ = [n for n in __all__ if n not in _advanced_names]
