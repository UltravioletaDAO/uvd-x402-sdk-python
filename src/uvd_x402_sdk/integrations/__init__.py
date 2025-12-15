"""
Framework integrations for the x402 SDK.

This module provides ready-to-use integrations for popular Python web frameworks:

- **Flask**: Decorator and middleware for Flask apps
- **FastAPI**: Dependency and middleware for FastAPI/Starlette apps
- **Django**: Middleware and decorator for Django apps
- **AWS Lambda**: Handler wrapper for Lambda functions

Each integration provides the same core functionality but adapts to the
idioms and patterns of its target framework.
"""

# Framework availability flags
FLASK_AVAILABLE = False
FASTAPI_AVAILABLE = False
DJANGO_AVAILABLE = False

try:
    from uvd_x402_sdk.integrations.flask_integration import (
        FlaskX402,
        flask_require_payment,
    )
    FLASK_AVAILABLE = True
except ImportError:
    FlaskX402 = None  # type: ignore
    flask_require_payment = None  # type: ignore

try:
    from uvd_x402_sdk.integrations.fastapi_integration import (
        FastAPIX402,
        X402Depends,
        fastapi_require_payment,
    )
    FASTAPI_AVAILABLE = True
except ImportError:
    FastAPIX402 = None  # type: ignore
    X402Depends = None  # type: ignore
    fastapi_require_payment = None  # type: ignore

try:
    from uvd_x402_sdk.integrations.django_integration import (
        DjangoX402Middleware,
        django_require_payment,
    )
    DJANGO_AVAILABLE = True
except ImportError:
    DjangoX402Middleware = None  # type: ignore
    django_require_payment = None  # type: ignore

from uvd_x402_sdk.integrations.lambda_integration import (
    LambdaX402,
    lambda_handler,
)

__all__ = [
    # Flask
    "FlaskX402",
    "flask_require_payment",
    "FLASK_AVAILABLE",
    # FastAPI
    "FastAPIX402",
    "X402Depends",
    "fastapi_require_payment",
    "FASTAPI_AVAILABLE",
    # Django
    "DjangoX402Middleware",
    "django_require_payment",
    "DJANGO_AVAILABLE",
    # AWS Lambda
    "LambdaX402",
    "lambda_handler",
]
