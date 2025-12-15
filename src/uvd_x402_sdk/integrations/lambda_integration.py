"""
AWS Lambda integration for x402 payments.

Provides:
- LambdaX402: Helper class for Lambda handlers
- lambda_handler: Decorator for protecting Lambda functions
"""

import json
import logging
from decimal import Decimal
from functools import wraps
from typing import Any, Callable, Dict, Optional, TypeVar, Union

from uvd_x402_sdk.client import X402Client
from uvd_x402_sdk.config import X402Config
from uvd_x402_sdk.exceptions import X402Error
from uvd_x402_sdk.models import PaymentResult
from uvd_x402_sdk.response import create_402_response, create_402_headers

logger = logging.getLogger(__name__)

F = TypeVar("F", bound=Callable[..., Any])

# Lambda event/response types
LambdaEvent = Dict[str, Any]
LambdaContext = Any
LambdaResponse = Dict[str, Any]


def _get_header(event: LambdaEvent, header_name: str) -> Optional[str]:
    """
    Get header from Lambda event.

    Handles both API Gateway REST API and HTTP API formats.
    """
    headers = event.get("headers", {})
    if not headers:
        return None

    # Try exact match
    if header_name in headers:
        return headers[header_name]

    # Try lowercase (HTTP API normalizes to lowercase)
    lower_name = header_name.lower()
    if lower_name in headers:
        return headers[lower_name]

    # Try case-insensitive search
    for key, value in headers.items():
        if key.lower() == lower_name:
            return value

    return None


def _create_lambda_response(
    status_code: int,
    body: Any,
    headers: Optional[Dict[str, str]] = None,
) -> LambdaResponse:
    """Create a Lambda response in API Gateway format."""
    response: LambdaResponse = {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Headers": "Content-Type,X-PAYMENT,Authorization",
            **(headers or {}),
        },
    }

    if isinstance(body, str):
        response["body"] = body
    else:
        response["body"] = json.dumps(body)

    return response


class LambdaX402:
    """
    Helper class for x402 payments in AWS Lambda.

    Example:
        >>> from uvd_x402_sdk.integrations import LambdaX402
        >>>
        >>> x402 = LambdaX402(
        ...     recipient_evm="0xYourWallet...",
        ...     recipient_solana="YourSolanaWallet...",
        ... )
        >>>
        >>> def handler(event, context):
        ...     # Check for payment requirement
        ...     body = json.loads(event.get("body", "{}"))
        ...     price = calculate_price(body)
        ...
        ...     # Process payment
        ...     result = x402.process_or_require(event, price)
        ...
        ...     # If result is a response dict, payment is required
        ...     if "statusCode" in result:
        ...         return result
        ...
        ...     # Payment verified - result is PaymentResult
        ...     return {
        ...         "statusCode": 200,
        ...         "body": json.dumps({"payer": result.payer_address})
        ...     }
    """

    def __init__(
        self,
        config: Optional[X402Config] = None,
        recipient_evm: str = "",
        recipient_solana: str = "",
        recipient_near: str = "",
        recipient_stellar: str = "",
        **kwargs: Any,
    ) -> None:
        """
        Initialize Lambda x402 helper.

        Args:
            config: X402Config object
            recipient_evm: EVM recipient address
            recipient_solana: Solana recipient address
            recipient_near: NEAR recipient account
            recipient_stellar: Stellar recipient address
            **kwargs: Additional config parameters
        """
        if config:
            self._config = config
        else:
            self._config = X402Config(
                recipient_evm=recipient_evm,
                recipient_solana=recipient_solana,
                recipient_near=recipient_near,
                recipient_stellar=recipient_stellar,
                **kwargs,
            )
        self._client = X402Client(config=self._config)

    @property
    def client(self) -> X402Client:
        """Get the x402 client."""
        return self._client

    @property
    def config(self) -> X402Config:
        """Get the x402 config."""
        return self._config

    def get_payment_header(self, event: LambdaEvent) -> Optional[str]:
        """Get X-PAYMENT header from Lambda event."""
        return _get_header(event, "X-PAYMENT")

    def create_402_response(
        self,
        amount_usd: Union[Decimal, float, str],
        message: Optional[str] = None,
    ) -> LambdaResponse:
        """
        Create a 402 Payment Required response.

        Args:
            amount_usd: Required payment amount
            message: Custom message

        Returns:
            Lambda response dict with 402 status
        """
        body = create_402_response(
            amount_usd=Decimal(str(amount_usd)),
            config=self._config,
            message=message,
        )
        return _create_lambda_response(402, body, create_402_headers())

    def process_payment(
        self,
        event: LambdaEvent,
        expected_amount_usd: Union[Decimal, float, str],
    ) -> PaymentResult:
        """
        Process x402 payment from Lambda event.

        Args:
            event: Lambda event containing X-PAYMENT header
            expected_amount_usd: Expected payment amount

        Returns:
            PaymentResult on success

        Raises:
            X402Error: If payment verification/settlement fails
        """
        payment_header = self.get_payment_header(event)
        if not payment_header:
            raise X402Error("Missing X-PAYMENT header", code="PAYMENT_REQUIRED")

        return self._client.process_payment(
            x_payment_header=payment_header,
            expected_amount_usd=Decimal(str(expected_amount_usd)),
        )

    def process_or_require(
        self,
        event: LambdaEvent,
        amount_usd: Union[Decimal, float, str],
        message: Optional[str] = None,
    ) -> Union[PaymentResult, LambdaResponse]:
        """
        Process payment or return 402 response.

        This is the main method for Lambda handlers. It:
        1. Checks for X-PAYMENT header
        2. If missing, returns 402 response
        3. If present, processes payment and returns PaymentResult

        Args:
            event: Lambda event
            amount_usd: Required payment amount
            message: Custom 402 message

        Returns:
            PaymentResult if payment verified, LambdaResponse if 402 needed
        """
        payment_header = self.get_payment_header(event)

        if not payment_header:
            logger.info(f"No payment header, returning 402 for ${amount_usd}")
            return self.create_402_response(amount_usd, message)

        try:
            result = self._client.process_payment(
                x_payment_header=payment_header,
                expected_amount_usd=Decimal(str(amount_usd)),
            )
            logger.info(f"Payment processed: {result.payer_address} paid ${amount_usd}")
            return result

        except X402Error as e:
            logger.warning(f"Payment failed: {e.message}")
            body = create_402_response(
                amount_usd=Decimal(str(amount_usd)),
                config=self._config,
                message=message,
            )
            body["error"] = e.message
            body["details"] = e.details
            return _create_lambda_response(402, body, create_402_headers())


def lambda_handler(
    amount_usd: Optional[Union[Decimal, float, str]] = None,
    amount_callback: Optional[Callable[[LambdaEvent], Decimal]] = None,
    config: Optional[X402Config] = None,
    recipient_address: Optional[str] = None,
    message: Optional[str] = None,
) -> Callable[[F], F]:
    """
    Decorator for Lambda handlers requiring x402 payment.

    The decorator handles the payment flow and injects PaymentResult
    into the handler's kwargs.

    Args:
        amount_usd: Fixed payment amount
        amount_callback: Function to calculate dynamic amount from event
        config: X402Config object
        recipient_address: EVM recipient (convenience arg)
        message: Custom 402 message

    Example (fixed amount):
        >>> @lambda_handler(amount_usd="1.00", recipient_address="0x...")
        >>> def handler(event, context, payment_result=None):
        ...     return {
        ...         "statusCode": 200,
        ...         "body": json.dumps({"payer": payment_result.payer_address})
        ...     }

    Example (dynamic pricing):
        >>> def calculate_price(event):
        ...     body = json.loads(event.get("body", "{}"))
        ...     pixels = body.get("pixels", 1)
        ...     return Decimal(str(pixels * 0.01))  # $0.01 per pixel
        >>>
        >>> @lambda_handler(amount_callback=calculate_price, recipient_address="0x...")
        >>> def handler(event, context, payment_result=None):
        ...     return {"statusCode": 200, "body": "..."}
    """
    _config = config or X402Config(recipient_evm=recipient_address or "")
    x402 = LambdaX402(config=_config)

    def decorator(func: F) -> F:
        @wraps(func)
        def wrapper(event: LambdaEvent, context: LambdaContext) -> LambdaResponse:
            # Determine amount
            if amount_callback:
                required_amount = amount_callback(event)
            elif amount_usd:
                required_amount = Decimal(str(amount_usd))
            else:
                raise ValueError("Either amount_usd or amount_callback is required")

            # Process or require payment
            result = x402.process_or_require(event, required_amount, message)

            # If it's a response dict, return it (402)
            if isinstance(result, dict) and "statusCode" in result:
                return result

            # Payment verified - call handler
            return func(event, context, payment_result=result)

        return wrapper  # type: ignore

    return decorator
