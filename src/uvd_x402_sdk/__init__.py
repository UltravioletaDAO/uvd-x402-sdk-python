"""
uvd-x402-sdk: Python SDK for x402 payments via Ultravioleta DAO facilitator.

This SDK enables developers to easily integrate x402 cryptocurrency payments
into their Python applications with support for 15 blockchain networks across
4 network types (EVM, SVM, NEAR, Stellar).

Supports both x402 v1 and v2 protocols:
- v1: network as string ("base", "solana")
- v2: network as CAIP-2 ("eip155:8453", "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp")

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

Supported Networks (15 total, 14 enabled):
- EVM (11): Base, Ethereum, Polygon, Arbitrum, Optimism, Avalanche, Celo,
           HyperEVM, Unichain, Monad, BSC (disabled)
- SVM (2): Solana, Fogo
- NEAR (1): NEAR Protocol
- Stellar (1): Stellar
"""

__version__ = "0.2.0"
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
]
