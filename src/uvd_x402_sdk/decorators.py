"""
Decorators for protecting endpoints with x402 payments.

These decorators provide a clean, declarative way to require payment
for specific endpoints across different web frameworks.
"""

from decimal import Decimal
from functools import wraps
from typing import Any, Callable, Optional, TypeVar, Union, Dict

from uvd_x402_sdk.config import X402Config
from uvd_x402_sdk.client import X402Client
from uvd_x402_sdk.exceptions import (
    X402Error,
    PaymentRequiredError,
    InvalidPayloadError,
)
from uvd_x402_sdk.response import create_402_response, create_402_headers

F = TypeVar("F", bound=Callable[..., Any])

# Global client instance (set by configure_x402)
_global_client: Optional[X402Client] = None
_global_config: Optional[X402Config] = None


def configure_x402(
    config: Optional[X402Config] = None,
    recipient_address: Optional[str] = None,
    facilitator_url: str = "https://facilitator.ultravioletadao.xyz",
    **kwargs: Any,
) -> None:
    """
    Configure the global x402 client for decorator usage.

    Call this once during application startup to set up the x402 client
    that decorators will use.

    Args:
        config: Full X402Config object
        recipient_address: Default recipient for EVM chains
        facilitator_url: Facilitator service URL
        **kwargs: Additional config parameters

    Example:
        >>> from uvd_x402_sdk import configure_x402
        >>> configure_x402(
        ...     recipient_address="0xYourWallet...",
        ...     facilitator_url="https://facilitator.ultravioletadao.xyz"
        ... )
    """
    global _global_client, _global_config

    if config:
        _global_config = config
    else:
        _global_config = X402Config(
            facilitator_url=facilitator_url,
            recipient_evm=recipient_address or "",
            **kwargs,
        )

    _global_client = X402Client(config=_global_config)


def get_x402_client() -> X402Client:
    """Get the global x402 client instance."""
    if _global_client is None:
        raise RuntimeError(
            "x402 not configured. Call configure_x402() during application startup."
        )
    return _global_client


def get_x402_config() -> X402Config:
    """Get the global x402 config instance."""
    if _global_config is None:
        raise RuntimeError(
            "x402 not configured. Call configure_x402() during application startup."
        )
    return _global_config


def require_payment(
    amount_usd: Union[Decimal, float, str],
    amount_callback: Optional[Callable[..., Decimal]] = None,
    networks: Optional[list] = None,
    message: Optional[str] = None,
    inject_result: bool = True,
    header_name: str = "X-PAYMENT",
) -> Callable[[F], F]:
    """
    Decorator that requires x402 payment for an endpoint.

    This is the main decorator for protecting endpoints with payments.
    It handles the complete payment flow:
    1. Check for X-PAYMENT header
    2. If missing, return 402 Payment Required
    3. If present, verify and settle payment
    4. Optionally inject PaymentResult into function

    Args:
        amount_usd: Fixed payment amount in USD (or use amount_callback)
        amount_callback: Callable that returns dynamic amount based on request
        networks: Limit to specific networks (default: all enabled)
        message: Custom message for 402 response
        inject_result: Whether to inject PaymentResult as 'payment_result' kwarg
        header_name: Header name for payment (default: X-PAYMENT)

    Returns:
        Decorated function

    Example (Flask):
        >>> @app.route("/api/premium")
        >>> @require_payment(amount_usd=Decimal("1.00"))
        >>> def premium_endpoint(payment_result=None):
        ...     return {"message": f"Paid by {payment_result.payer_address}"}

    Example (Dynamic pricing):
        >>> def calculate_price(request):
        ...     items = request.json.get("items", 1)
        ...     return Decimal(str(items * 0.10))
        >>>
        >>> @require_payment(amount_callback=calculate_price)
        >>> def dynamic_endpoint(payment_result=None):
        ...     return {"message": "Success"}
    """

    def decorator(func: F) -> F:
        @wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            # Get client and config
            client = get_x402_client()
            config = get_x402_config()

            # Framework-agnostic request extraction
            # This will be overridden by framework-specific integrations
            request = _extract_request(*args, **kwargs)
            payment_header = _get_header(request, header_name)

            # Determine amount
            if amount_callback:
                required_amount = Decimal(str(amount_callback(request)))
            else:
                required_amount = Decimal(str(amount_usd))

            # No payment header - return 402
            if not payment_header:
                response_body = create_402_response(
                    amount_usd=required_amount,
                    config=config,
                    message=message,
                )
                return _create_402_response(response_body)

            # Process payment
            try:
                result = client.process_payment(
                    x_payment_header=payment_header,
                    expected_amount_usd=required_amount,
                )

                # Inject result if requested
                if inject_result:
                    kwargs["payment_result"] = result

                return func(*args, **kwargs)

            except X402Error as e:
                # Return appropriate error response
                return _create_error_response(e, required_amount, config)

        return wrapper  # type: ignore

    return decorator


# Alias for backward compatibility and preference
x402_required = require_payment


def _extract_request(*args: Any, **kwargs: Any) -> Any:
    """
    Extract request object from function arguments.

    This is a framework-agnostic implementation that works with:
    - Flask (request from flask.globals)
    - FastAPI/Starlette (request in kwargs)
    - Django (request as first arg)
    - AWS Lambda (event dict as first arg)
    """
    # Check kwargs first
    if "request" in kwargs:
        return kwargs["request"]

    # Check first positional arg
    if args:
        return args[0]

    # Try Flask global
    try:
        from flask import request
        return request
    except ImportError:
        pass

    # No request found
    return None


def _get_header(request: Any, header_name: str) -> Optional[str]:
    """
    Get header value from request object.

    Handles different frameworks' request objects.
    """
    if request is None:
        return None

    # Dict-like (AWS Lambda event)
    if isinstance(request, dict):
        headers = request.get("headers", {})
        # Lambda headers can be case-sensitive or not
        return headers.get(header_name) or headers.get(header_name.lower())

    # Flask/Werkzeug
    if hasattr(request, "headers"):
        headers = request.headers
        if hasattr(headers, "get"):
            return headers.get(header_name)

    # Django
    if hasattr(request, "META"):
        # Django uses HTTP_X_PAYMENT format
        django_header = f"HTTP_{header_name.replace('-', '_').upper()}"
        return request.META.get(django_header)

    return None


def _create_402_response(body: Dict[str, Any]) -> Any:
    """
    Create a 402 response appropriate for the current framework.

    This is a fallback that returns a tuple. Framework-specific
    integrations override this.
    """
    # Try Flask
    try:
        from flask import jsonify, make_response
        response = make_response(jsonify(body), 402)
        for key, value in create_402_headers().items():
            response.headers[key] = value
        return response
    except ImportError:
        pass

    # Try FastAPI/Starlette
    try:
        from starlette.responses import JSONResponse
        return JSONResponse(
            status_code=402,
            content=body,
            headers=create_402_headers(),
        )
    except ImportError:
        pass

    # Try Django
    try:
        from django.http import JsonResponse
        response = JsonResponse(body, status=402)
        for key, value in create_402_headers().items():
            response[key] = value
        return response
    except ImportError:
        pass

    # Fallback: return dict (for AWS Lambda or raw WSGI)
    return {
        "statusCode": 402,
        "headers": {
            "Content-Type": "application/json",
            **create_402_headers(),
        },
        "body": body,
    }


def _create_error_response(
    error: X402Error,
    amount: Decimal,
    config: X402Config,
) -> Any:
    """Create an error response for x402 errors."""
    # Payment verification/settlement failed - return 402
    if isinstance(error, (PaymentRequiredError, InvalidPayloadError)):
        body = create_402_response(amount_usd=amount, config=config)
        body["error"] = error.message
        body["details"] = error.details
        return _create_402_response(body)

    # Other errors - return 400
    body = error.to_dict()

    try:
        from flask import jsonify, make_response
        return make_response(jsonify(body), 400)
    except ImportError:
        pass

    try:
        from starlette.responses import JSONResponse
        return JSONResponse(status_code=400, content=body)
    except ImportError:
        pass

    try:
        from django.http import JsonResponse
        return JsonResponse(body, status=400)
    except ImportError:
        pass

    return {"statusCode": 400, "body": body}
