"""
Custom exceptions for the x402 SDK.

These exceptions provide clear, actionable error messages for different
failure scenarios in the payment flow.
"""

from typing import Optional, List, Dict, Any


class X402Error(Exception):
    """Base exception for all x402 SDK errors."""

    def __init__(
        self,
        message: str,
        code: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.message = message
        self.code = code or "X402_ERROR"
        self.details = details or {}
        super().__init__(self.message)

    def to_dict(self) -> Dict[str, Any]:
        """Convert exception to dictionary for API responses."""
        return {
            "error": self.code,
            "message": self.message,
            "details": self.details,
        }


class PaymentRequiredError(X402Error):
    """
    Raised when a payment is required but not provided.

    This should trigger a 402 Payment Required response.
    """

    def __init__(
        self,
        message: str = "Payment required",
        amount_usd: Optional[str] = None,
        recipient: Optional[str] = None,
        supported_networks: Optional[List[str]] = None,
    ) -> None:
        details = {}
        if amount_usd:
            details["amount"] = amount_usd
        if recipient:
            details["recipient"] = recipient
        if supported_networks:
            details["supportedNetworks"] = supported_networks

        super().__init__(
            message=message,
            code="PAYMENT_REQUIRED",
            details=details,
        )
        self.amount_usd = amount_usd
        self.recipient = recipient
        self.supported_networks = supported_networks


class PaymentVerificationError(X402Error):
    """
    Raised when payment verification fails.

    Common causes:
    - Invalid signature
    - Amount mismatch
    - Wrong recipient
    - Expired payment authorization
    """

    def __init__(
        self,
        message: str,
        reason: Optional[str] = None,
        errors: Optional[List[str]] = None,
    ) -> None:
        details = {}
        if reason:
            details["reason"] = reason
        if errors:
            details["errors"] = errors

        super().__init__(
            message=message,
            code="PAYMENT_VERIFICATION_FAILED",
            details=details,
        )
        self.reason = reason
        self.errors = errors or []


class PaymentSettlementError(X402Error):
    """
    Raised when payment settlement fails on-chain.

    Common causes:
    - Insufficient USDC balance
    - Nonce already used
    - Authorization expired
    - Network congestion/timeout
    """

    def __init__(
        self,
        message: str,
        network: Optional[str] = None,
        tx_hash: Optional[str] = None,
        reason: Optional[str] = None,
    ) -> None:
        details = {}
        if network:
            details["network"] = network
        if tx_hash:
            details["transactionHash"] = tx_hash
        if reason:
            details["reason"] = reason

        super().__init__(
            message=message,
            code="PAYMENT_SETTLEMENT_FAILED",
            details=details,
        )
        self.network = network
        self.tx_hash = tx_hash
        self.reason = reason


class UnsupportedNetworkError(X402Error):
    """
    Raised when an unsupported network is specified.

    Use `register_network()` to add custom network support.
    """

    def __init__(
        self,
        network: str,
        supported_networks: Optional[List[str]] = None,
    ) -> None:
        super().__init__(
            message=f"Unsupported network: {network}",
            code="UNSUPPORTED_NETWORK",
            details={
                "requestedNetwork": network,
                "supportedNetworks": supported_networks or [],
            },
        )
        self.network = network
        self.supported_networks = supported_networks or []


class InvalidPayloadError(X402Error):
    """
    Raised when the X-PAYMENT header payload is invalid.

    Common causes:
    - Invalid base64 encoding
    - Invalid JSON format
    - Missing required fields
    - Invalid x402 version
    """

    def __init__(
        self,
        message: str,
        field: Optional[str] = None,
        expected: Optional[str] = None,
        received: Optional[str] = None,
    ) -> None:
        details = {}
        if field:
            details["field"] = field
        if expected:
            details["expected"] = expected
        if received:
            details["received"] = received

        super().__init__(
            message=message,
            code="INVALID_PAYLOAD",
            details=details,
        )
        self.field = field
        self.expected = expected
        self.received = received


class ConfigurationError(X402Error):
    """
    Raised when SDK configuration is invalid or missing.
    """

    def __init__(self, message: str, config_key: Optional[str] = None) -> None:
        super().__init__(
            message=message,
            code="CONFIGURATION_ERROR",
            details={"configKey": config_key} if config_key else {},
        )
        self.config_key = config_key


class FacilitatorError(X402Error):
    """
    Raised when the facilitator returns an error.

    Contains the raw error response from the facilitator for debugging.
    """

    def __init__(
        self,
        message: str,
        status_code: Optional[int] = None,
        response_body: Optional[str] = None,
    ) -> None:
        super().__init__(
            message=message,
            code="FACILITATOR_ERROR",
            details={
                "statusCode": status_code,
                "response": response_body,
            },
        )
        self.status_code = status_code
        self.response_body = response_body


class TimeoutError(X402Error):
    """
    Raised when a facilitator request times out.

    Settlement operations can take up to 55 seconds on congested networks.
    """

    def __init__(
        self,
        operation: str,
        timeout_seconds: float,
    ) -> None:
        super().__init__(
            message=f"{operation} timed out after {timeout_seconds}s",
            code="TIMEOUT",
            details={
                "operation": operation,
                "timeoutSeconds": timeout_seconds,
            },
        )
        self.operation = operation
        self.timeout_seconds = timeout_seconds
