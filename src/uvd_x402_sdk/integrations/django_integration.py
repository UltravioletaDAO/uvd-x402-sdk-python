"""
Django integration for x402 payments.

Provides:
- DjangoX402Middleware: Middleware for protecting views
- django_require_payment: Decorator for view functions
"""

from decimal import Decimal
from functools import wraps
from typing import Any, Callable, Dict, List, Optional, TypeVar, Union
import json

try:
    from django.http import JsonResponse, HttpRequest, HttpResponse
    from django.conf import settings
except ImportError:
    raise ImportError(
        "Django is required for Django integration. "
        "Install with: pip install uvd-x402-sdk[django]"
    )

from uvd_x402_sdk.client import X402Client
from uvd_x402_sdk.config import X402Config
from uvd_x402_sdk.exceptions import X402Error
from uvd_x402_sdk.models import PaymentResult
from uvd_x402_sdk.response import create_402_response, create_402_headers

F = TypeVar("F", bound=Callable[..., Any])


def _get_config_from_settings() -> X402Config:
    """Get x402 configuration from Django settings."""
    return X402Config(
        facilitator_url=getattr(
            settings,
            "X402_FACILITATOR_URL",
            "https://facilitator.ultravioletadao.xyz",
        ),
        recipient_evm=getattr(settings, "X402_RECIPIENT_EVM", ""),
        recipient_solana=getattr(settings, "X402_RECIPIENT_SOLANA", ""),
        recipient_near=getattr(settings, "X402_RECIPIENT_NEAR", ""),
        recipient_stellar=getattr(settings, "X402_RECIPIENT_STELLAR", ""),
        facilitator_solana=getattr(
            settings,
            "X402_FACILITATOR_SOLANA",
            "F742C4VfFLQ9zRQyithoj5229ZgtX2WqKCSFKgH2EThq",
        ),
    )


class DjangoX402Middleware:
    """
    Django middleware for x402 payment protection.

    Configure protected paths in Django settings:

        # settings.py
        X402_RECIPIENT_EVM = "0xYourWallet..."
        X402_PROTECTED_PATHS = {
            "/api/premium/": "5.00",
            "/api/basic/": "1.00",
        }

    Example:
        # settings.py
        MIDDLEWARE = [
            ...
            'uvd_x402_sdk.integrations.django_integration.DjangoX402Middleware',
        ]
    """

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response
        self._config = _get_config_from_settings()
        self._client = X402Client(config=self._config)
        self._protected_paths: Dict[str, Decimal] = {}

        # Load protected paths from settings
        paths_setting = getattr(settings, "X402_PROTECTED_PATHS", {})
        for path, amount in paths_setting.items():
            self._protected_paths[path] = Decimal(str(amount))

    def __call__(self, request: HttpRequest) -> HttpResponse:
        # Check if path is protected
        path = request.path

        # Try exact match first, then prefix match
        required_amount = None
        for protected_path, amount in self._protected_paths.items():
            if path == protected_path or path.startswith(protected_path):
                required_amount = amount
                break

        if required_amount is None:
            return self.get_response(request)

        # Get payment header (Django uses HTTP_X_PAYMENT format)
        payment_header = request.META.get("HTTP_X_PAYMENT")

        if not payment_header:
            response_body = create_402_response(
                amount_usd=required_amount,
                config=self._config,
            )
            response = JsonResponse(response_body, status=402)
            for key, value in create_402_headers().items():
                response[key] = value
            return response

        try:
            result = self._client.process_payment(
                x_payment_header=payment_header,
                expected_amount_usd=required_amount,
            )
            # Store result on request for view access
            request.payment_result = result  # type: ignore
            return self.get_response(request)

        except X402Error as e:
            response = JsonResponse(e.to_dict(), status=402)
            for key, value in create_402_headers().items():
                response[key] = value
            return response


def django_require_payment(
    amount_usd: Union[Decimal, float, str],
    config: Optional[X402Config] = None,
    message: Optional[str] = None,
) -> Callable[[F], F]:
    """
    Decorator that requires payment for a Django view.

    Args:
        amount_usd: Required payment amount in USD
        config: X402Config (uses Django settings if not provided)
        message: Custom message for 402 response

    Example:
        >>> from uvd_x402_sdk.integrations import django_require_payment
        >>>
        >>> @django_require_payment(amount_usd="1.00")
        >>> def my_view(request):
        ...     # Access payment result via request.payment_result
        ...     return JsonResponse({"payer": request.payment_result.payer_address})
    """
    required_amount = Decimal(str(amount_usd))
    _config = config or _get_config_from_settings()
    _client = X402Client(config=_config)

    def decorator(func: F) -> F:
        @wraps(func)
        def wrapper(request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
            # Get payment header
            payment_header = request.META.get("HTTP_X_PAYMENT")

            if not payment_header:
                response_body = create_402_response(
                    amount_usd=required_amount,
                    config=_config,
                    message=message,
                )
                response = JsonResponse(response_body, status=402)
                for key, value in create_402_headers().items():
                    response[key] = value
                return response

            try:
                result = _client.process_payment(
                    x_payment_header=payment_header,
                    expected_amount_usd=required_amount,
                )
                # Store result on request
                request.payment_result = result  # type: ignore
                return func(request, *args, **kwargs)

            except X402Error as e:
                response = JsonResponse(e.to_dict(), status=402)
                for key, value in create_402_headers().items():
                    response[key] = value
                return response

        return wrapper  # type: ignore

    return decorator


class X402PaymentView:
    """
    Mixin for Django class-based views requiring payment.

    Example:
        >>> from django.views import View
        >>> from uvd_x402_sdk.integrations.django_integration import X402PaymentView
        >>>
        >>> class PremiumAPIView(X402PaymentView, View):
        ...     x402_amount = Decimal("5.00")
        ...
        ...     def get(self, request):
        ...         return JsonResponse({"payer": request.payment_result.payer_address})
    """

    x402_amount: Decimal = Decimal("1.00")
    x402_message: Optional[str] = None
    x402_config: Optional[X402Config] = None

    def dispatch(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        config = self.x402_config or _get_config_from_settings()
        client = X402Client(config=config)

        payment_header = request.META.get("HTTP_X_PAYMENT")

        if not payment_header:
            response_body = create_402_response(
                amount_usd=self.x402_amount,
                config=config,
                message=self.x402_message,
            )
            response = JsonResponse(response_body, status=402)
            for key, value in create_402_headers().items():
                response[key] = value
            return response

        try:
            result = client.process_payment(
                x_payment_header=payment_header,
                expected_amount_usd=self.x402_amount,
            )
            request.payment_result = result  # type: ignore
            return super().dispatch(request, *args, **kwargs)  # type: ignore

        except X402Error as e:
            response = JsonResponse(e.to_dict(), status=402)
            for key, value in create_402_headers().items():
                response[key] = value
            return response
