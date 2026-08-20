"""
uvd-x402-sdk: Python SDK for x402 payments via Ultravioleta DAO facilitator.

This SDK enables developers to easily integrate x402 cryptocurrency payments
into their Python applications with support for 23 blockchain networks across
7 network types (EVM, SVM, NEAR, Stellar, Algorand, Sui, XRPL).

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

Supported Networks (25 total):
- EVM (15): Base, Ethereum, Polygon, Arbitrum, Optimism, Avalanche, Celo,
            HyperEVM, Unichain, Monad, Scroll, SKALE Base, SKALE Base Sepolia,
            Robinhood, Robinhood Testnet
- SVM (2): Solana, Fogo
- NEAR (1): NEAR Protocol
- Stellar (1): Stellar
- Algorand (2): Algorand mainnet, Algorand testnet
- Sui (2): Sui mainnet, Sui testnet
- XRPL (2): XRP Ledger mainnet, XRP Ledger testnet (native XRP)
"""

def _resolve_version() -> str:
    """Read the version from installed package metadata.

    Hardcoding it here meant it drifted from ``pyproject.toml`` whenever a
    release bumped only one of the two: 0.39.0 and 0.40.0 both shipped with
    ``__version__`` still reading "0.38.0". That is not cosmetic - anything
    gating a feature on the version (``__version__ >= "0.40.0"`` to decide
    whether ``score`` is supported) silently gets the wrong answer.

    Reading the metadata means the two cannot disagree again. The fallback
    only applies to a source checkout that was never installed.
    """
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version("uvd-x402-sdk")
    except PackageNotFoundError:
        return "0.0.0.dev0"


__version__ = _resolve_version()
__author__ = "Ultravioleta DAO"

from uvd_x402_sdk.client import X402Client
from uvd_x402_sdk.config import (
    X402Config,
    NetworkConfig,
    MultiPaymentConfig,
    FACILITATOR_FALLBACK_KEY,
)
from uvd_x402_sdk.decorators import require_payment, x402_required, configure_x402
from uvd_x402_sdk.exceptions import (
    X402Error,
    PaymentRequiredError,
    PaymentVerificationError,
    PaymentSettlementError,
    UnsupportedNetworkError,
    InvalidPayloadError,
    FacilitatorError,
    LookupInconclusiveError,
    RegistrationPendingError,
    ConfigurationError,
    TimeoutError as X402TimeoutError,
)
from uvd_x402_sdk.models import (
    # Payload models
    PaymentPayload,
    EVMPayloadContent,
    SVMPayloadContent,
    SolanaPayloadContent,  # Alias for backward compatibility
    SettlementAccountPayload,  # Crossmint custodial wallets
    NEARPayloadContent,
    StellarPayloadContent,
    SuiPayloadContent,
    XRPLPayloadContent,
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
    bazaar_extension,
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
    XRPL_FEE_PAYER_MAINNET,
    XRPL_FEE_PAYER_TESTNET,
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
    AgentId,
    ProofOfPayment,
    AgentIdentity,
    AgentRegistrationFile,
    ReputationSummary,
    AtomStats,
    FeedbackEntry,
    FeedbackParams,
    FeedbackRequest,
    FeedbackResponse,
    ReputationResponse,
    SettleResponseWithProof,
    MetadataEntryParam,
    RegisterAgentResponse,
    RegisterJobResponse,
    RegisterJobStatus,
    IdentityByOwnerResponse,
    IdentityMetadataResponse,
    IdentityTotalSupplyResponse,
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

# Bazaar Discovery
from uvd_x402_sdk.discovery import (
    BazaarClient,
    DiscoveryCuration,
    DiscoveryHealth,
    DiscoveryPagination,
    DiscoveryResource,
    DiscoveryResponse,
    HEALTH_FILTERS,
    MAX_SEARCH_LEN,
    TIER_FILTERS,
)

# Live traffic stream (GET /events, SSE)
from uvd_x402_sdk.events import (
    EVENT_KINDS,
    TrafficEvent,
    TrafficEventStream,
)

# x402 v2 request envelopes (/verify, /settle)
from uvd_x402_sdk.envelope_v2 import (
    AcceptedRequirementsV2,
    ResourceInfoV2,
    build_settle_request_v2,
    build_verify_request_v2,
)

# Wallet Adapters
from uvd_x402_sdk.wallet import (
    WalletAdapter,
    EnvKeyAdapter,
    OWSWalletAdapter,
    EIP3009Params,
    EIP3009Authorization,
    SignedTypedData,
)

# ERC-8128 Signed HTTP Requests (RFC 9421) — wallet-signed API auth.
# The submodule carries the rest (pure core, conformance vectors, presets):
#   from uvd_x402_sdk.erc8128 import build_signature_base, run_conformance, ...
from uvd_x402_sdk.erc8128 import (
    ERC8128_ERROR_RETRYABLE,
    ERC8128_ERROR_STATUS,
    POLICY_PRESETS,
    Erc8128Error,
    NoncePolicy,
    NonceStore,
    VerifiableRequest,
    VerifyPolicy,
    VerifyResult,
    fetch_nonce,
    fetch_nonce_sync,
    policy_from_preset,
    run_conformance,
    sign_request,
    verify_request,
)

# Escrow pre-auth builder (ADR-002 sign-on-assignment) — X-Payment-Auth header
from uvd_x402_sdk.dx402 import (
    EVIDENCE_HEADER,
    AnchoredEvidence,
    ContentHashMismatch,
    DX402Error,
    EvidenceSkipped,
    content_hash,
    dereference_pointer,
    evidence_from_headers,
    parse_evidence_header,
    payer_key_from_ed25519_pubkey,
    payer_key_from_evm_signature,
    payer_key_from_solana_address,
    payment_id,
    recover_evidence,
    seal_evidence,
    sealed_roles,
    # The seller half. Missing here, `anchor_evidence` was reachable only as
    # `from uvd_x402_sdk.dx402 import ...` -- so the one call that exists
    # precisely so nobody hand-builds a digest was the one you had to know about
    # to find. Same for the helpers under it.
    ANCHOR_MAX_REQUEST_BYTES,
    anchor_digest,
    anchor_evidence,
    available_backends,
    evidence_header,
    seal_evidence_to,
    sign_anchor_ed25519,
    sign_anchor_evm,
)
from uvd_x402_sdk.money_safety import SAFE_TO_FALLBACK, is_fallback_safe
from uvd_x402_sdk.erc7702 import (
    DelegationResolver,
    delegate_target,
    is_delegated,
    rpc_delegation_resolver,
    sign_eip3009_for_delegated,
    wrap_signature,
)
from uvd_x402_sdk.escrow_signing import (
    build_escrow_pre_auth,
    compute_escrow_nonce,
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
        ESCROW_CONTRACTS,
        ESCROW_CHAIN_NAMES,
        OPERATOR_ABI,
        OPERATOR_ABI_V2,
        CREATE3_CHAIN_IDS,
        get_operator_abi,
        DEPOSIT_LIMIT_USDC,
        get_escrow_contracts,
        get_supported_escrow_chains,
        is_escrow_supported,
    )
    ADVANCED_ESCROW_AVAILABLE = True
except ImportError:
    ADVANCED_ESCROW_AVAILABLE = False

__all__ = [
    "available_backends",
    "ANCHOR_MAX_REQUEST_BYTES",
    "anchor_digest",
    "anchor_evidence",
    "evidence_header",
    "seal_evidence_to",
    "sign_anchor_ed25519",
    "sign_anchor_evm",
    # DX402 durable-evidence
    "EVIDENCE_HEADER",
    "AnchoredEvidence",
    "ContentHashMismatch",
    "DX402Error",
    "EvidenceSkipped",
    "dereference_pointer",
    "evidence_from_headers",
    "parse_evidence_header",
    "payment_id",
    "recover_evidence",
    # DX402 seller side
    "seal_evidence",
    "content_hash",
    "payer_key_from_solana_address",
    "payer_key_from_ed25519_pubkey",
    "payer_key_from_evm_signature",
    "sealed_roles",
    "bazaar_extension",
    "SAFE_TO_FALLBACK",
    "is_fallback_safe",
    "DelegationResolver",
    "delegate_target",
    "is_delegated",
    "rpc_delegation_resolver",
    "sign_eip3009_for_delegated",
    "wrap_signature",
    "wrap_signature",
    "sign_eip3009_for_delegated",
    "rpc_delegation_resolver",
    "is_delegated",
    "delegate_target",
    "DelegationResolver",
    # Version
    "__version__",
    # Main client
    "X402Client",
    # Configuration
    "X402Config",
    "NetworkConfig",
    "MultiPaymentConfig",
    "FACILITATOR_FALLBACK_KEY",
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
    "LookupInconclusiveError",
    "RegistrationPendingError",
    "ConfigurationError",
    "X402TimeoutError",
    # Payload models
    "PaymentPayload",
    "EVMPayloadContent",
    "SVMPayloadContent",
    "SolanaPayloadContent",
    "SettlementAccountPayload",
    "NEARPayloadContent",
    "StellarPayloadContent",
    "SuiPayloadContent",
    "XRPLPayloadContent",
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
    "XRPL_FEE_PAYER_MAINNET",
    "XRPL_FEE_PAYER_TESTNET",
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
    "AgentId",
    "ProofOfPayment",
    "AgentIdentity",
    "AgentRegistrationFile",
    "ReputationSummary",
    "AtomStats",
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
    # Bazaar Discovery
    "BazaarClient",
    "DiscoveryCuration",
    "DiscoveryHealth",
    "DiscoveryPagination",
    "DiscoveryResource",
    "DiscoveryResponse",
    "HEALTH_FILTERS",
    "MAX_SEARCH_LEN",
    "TIER_FILTERS",
    # Live traffic stream (GET /events, SSE)
    "EVENT_KINDS",
    "TrafficEvent",
    "TrafficEventStream",
    # x402 v2 request envelopes
    "AcceptedRequirementsV2",
    "ResourceInfoV2",
    "build_verify_request_v2",
    "build_settle_request_v2",
    # Wallet Adapters
    "WalletAdapter",
    "EnvKeyAdapter",
    "OWSWalletAdapter",
    "EIP3009Params",
    "EIP3009Authorization",
    "SignedTypedData",
    # ERC-8128 Signed HTTP Requests (RFC 9421)
    "sign_request",
    "fetch_nonce",
    "fetch_nonce_sync",
    "verify_request",
    "VerifiableRequest",
    "VerifyPolicy",
    "VerifyResult",
    "NoncePolicy",
    "NonceStore",
    "POLICY_PRESETS",
    "policy_from_preset",
    "Erc8128Error",
    "ERC8128_ERROR_STATUS",
    "ERC8128_ERROR_RETRYABLE",
    "run_conformance",
    # Escrow pre-auth builder (ADR-002 sign-on-assignment)
    "build_escrow_pre_auth",
    "compute_escrow_nonce",
    # Advanced Escrow (PaymentOperator) - available when eth_abi/web3/httpx installed
    "ADVANCED_ESCROW_AVAILABLE",
    "AdvancedEscrowClient",
    "PaymentInfo",
    "TaskTier",
    "AuthorizationResult",
    "TransactionResult",
    "TIER_TIMINGS",
    "BASE_MAINNET_CONTRACTS",
    "ESCROW_CONTRACTS",
    "ESCROW_CHAIN_NAMES",
    "OPERATOR_ABI",
    "OPERATOR_ABI_V2",
    "CREATE3_CHAIN_IDS",
    "get_operator_abi",
    "DEPOSIT_LIMIT_USDC",
    "get_escrow_contracts",
    "get_supported_escrow_chains",
    "is_escrow_supported",
]

# Conditionally remove Advanced Escrow names from __all__ if not available
if not ADVANCED_ESCROW_AVAILABLE:
    _advanced_names = {
        "AdvancedEscrowClient", "PaymentInfo", "TaskTier",
        "AuthorizationResult", "TransactionResult", "TIER_TIMINGS",
        "BASE_MAINNET_CONTRACTS", "ESCROW_CONTRACTS", "ESCROW_CHAIN_NAMES",
        "OPERATOR_ABI", "OPERATOR_ABI_V2", "CREATE3_CHAIN_IDS",
        "get_operator_abi", "DEPOSIT_LIMIT_USDC",
        "get_escrow_contracts", "get_supported_escrow_chains",
        "is_escrow_supported",
    }
    __all__ = [n for n in __all__ if n not in _advanced_names]
