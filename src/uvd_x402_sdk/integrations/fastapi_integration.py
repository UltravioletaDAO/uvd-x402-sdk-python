"""
FastAPI/Starlette integration for x402 payments.

Provides:
- FastAPIX402: App integration class
- X402Depends: Dependency injection for payment verification
- fastapi_require_payment: Decorator for protected routes
"""

from decimal import Decimal
from functools import wraps
from typing import Any, Callable, Optional, TypeVar, Union

try:
    from fastapi import FastAPI, Request, HTTPException, Depends
    from fastapi.responses import JSONResponse
    from starlette.middleware.base import BaseHTTPMiddleware
except ImportError:
    raise ImportError(
        "FastAPI is required for FastAPI integration. "
        "Install with: pip install uvd-x402-sdk[fastapi]"
    )

from uvd_x402_sdk.client import X402Client
from uvd_x402_sdk.config import X402Config
from uvd_x402_sdk.exceptions import X402Error
from uvd_x402_sdk.models import PaymentResult
from uvd_x402_sdk.response import create_402_response, create_402_headers

F = TypeVar("F", bound=Callable[..., Any])


class FastAPIX402:
    """
    FastAPI integration for x402 payments.

    Example:
        >>> from fastapi import FastAPI
        >>> from uvd_x402_sdk.integrations import FastAPIX402
        >>>
        >>> app = FastAPI()
        >>> x402 = FastAPIX402(app, recipient_address="0xYourWallet...")
        >>>
        >>> @app.get("/premium")
        >>> async def premium(payment: PaymentResult = Depends(x402.require_payment(1.00))):
        ...     return {"payer": payment.payer_address}
    """

    def __init__(
        self,
        app: Optional[FastAPI] = None,
        config: Optional[X402Config] = None,
        recipient_address: Optional[str] = None,
        **kwargs: Any,
    ) -> None:
        """
        Initialize FastAPI x402 integration.

        Args:
            app: FastAPI application (optional)
            config: X402Config object
            recipient_address: Default recipient for EVM chains
            **kwargs: Additional config parameters
        """
        self._config = config or X402Config(
            recipient_evm=recipient_address or "",
            **kwargs,
        )
        self._client = X402Client(config=self._config)

        if app is not None:
            self.init_app(app)

    def init_app(self, app: FastAPI) -> None:
        """
        Initialize with FastAPI app.

        Stores client in app.state for access in routes.
        """
        app.state.x402_client = self._client
        app.state.x402_config = self._config

    @property
    def client(self) -> X402Client:
        """Get the x402 client."""
        return self._client

    @property
    def config(self) -> X402Config:
        """Get the x402 config."""
        return self._config

    def require_payment(
        self,
        amount_usd: Union[Decimal, float, str],
        message: Optional[str] = None,
    ) -> Callable[..., PaymentResult]:
        """
        Create a FastAPI dependency that requires payment.

        Args:
            amount_usd: Required payment amount in USD
            message: Custom message for 402 response

        Returns:
            Dependency function that returns PaymentResult

        Example:
            >>> @app.post("/api/premium")
            >>> async def premium(
            ...     request: Request,
            ...     payment: PaymentResult = Depends(x402.require_payment(5.00))
            ... ):
            ...     return {"payer": payment.payer_address}
        """
        required_amount = Decimal(str(amount_usd))

        async def dependency(request: Request) -> PaymentResult:
            payment_header = request.headers.get("X-PAYMENT")

            if not payment_header:
                response_body = create_402_response(
                    amount_usd=required_amount,
                    config=self._config,
                    message=message,
                )
                raise HTTPException(
                    status_code=402,
                    detail=response_body,
                    headers=create_402_headers(),
                )

            try:
                return self._client.process_payment(
                    x_payment_header=payment_header,
                    expected_amount_usd=required_amount,
                )
            except X402Error as e:
                raise HTTPException(
                    status_code=402,
                    detail=e.to_dict(),
                    headers=create_402_headers(),
                )

        return dependency


class X402Depends:
    """
    Reusable FastAPI dependency for x402 payments.

    This provides a cleaner syntax for dependency injection.

    Example:
        >>> x402_payment = X402Depends(
        ...     config=X402Config(recipient_evm="0x..."),
        ...     amount_usd=Decimal("1.00")
        ... )
        >>>
        >>> @app.get("/resource")
        >>> async def resource(payment: PaymentResult = Depends(x402_payment)):
        ...     return {"payer": payment.payer_address}
    """

    def __init__(
        self,
        config: X402Config,
        amount_usd: Union[Decimal, float, str],
        message: Optional[str] = None,
    ) -> None:
        self._config = config
        self._client = X402Client(config=config)
        self._amount = Decimal(str(amount_usd))
        self._message = message

    async def __call__(self, request: Request) -> PaymentResult:
        """Process payment when used as dependency."""
        payment_header = request.headers.get("X-PAYMENT")

        if not payment_header:
            response_body = create_402_response(
                amount_usd=self._amount,
                config=self._config,
                message=self._message,
            )
            raise HTTPException(
                status_code=402,
                detail=response_body,
                headers=create_402_headers(),
            )

        try:
            return self._client.process_payment(
                x_payment_header=payment_header,
                expected_amount_usd=self._amount,
            )
        except X402Error as e:
            raise HTTPException(
                status_code=402,
                detail=e.to_dict(),
                headers=create_402_headers(),
            )


def fastapi_require_payment(
    amount_usd: Union[Decimal, float, str],
    config: X402Config,
    message: Optional[str] = None,
) -> Callable[[F], F]:
    """
    Decorator for FastAPI routes requiring payment.

    Alternative to dependency injection for simpler cases.

    Args:
        amount_usd: Required payment amount
        config: X402Config with recipient addresses
        message: Custom 402 message

    Example:
        >>> @app.get("/resource")
        >>> @fastapi_require_payment(amount_usd="1.00", config=config)
        >>> async def resource(request: Request):
        ...     # Payment already verified
        ...     return {"success": True}
    """
    required_amount = Decimal(str(amount_usd))
    client = X402Client(config=config)

    def decorator(func: F) -> F:
        @wraps(func)
        async def wrapper(request: Request, *args: Any, **kwargs: Any) -> Any:
            payment_header = request.headers.get("X-PAYMENT")

            if not payment_header:
                response_body = create_402_response(
                    amount_usd=required_amount,
                    config=config,
                    message=message,
                )
                return JSONResponse(
                    status_code=402,
                    content=response_body,
                    headers=create_402_headers(),
                )

            try:
                result = client.process_payment(
                    x_payment_header=payment_header,
                    expected_amount_usd=required_amount,
                )
                # Store result in request state
                request.state.payment_result = result
                return await func(request, *args, **kwargs)

            except X402Error as e:
                return JSONResponse(
                    status_code=402,
                    content=e.to_dict(),
                    headers=create_402_headers(),
                )

        return wrapper  # type: ignore

    return decorator


class X402Middleware(BaseHTTPMiddleware):
    """
    Middleware that automatically handles x402 payments for configured paths.

    Example:
        >>> from uvd_x402_sdk.integrations.fastapi_integration import X402Middleware
        >>>
        >>> app.add_middleware(
        ...     X402Middleware,
        ...     config=config,
        ...     protected_paths={
        ...         "/api/premium": Decimal("5.00"),
        ...         "/api/basic": Decimal("1.00"),
        ...     }
        ... )
    """

    def __init__(
        self,
        app: Any,
        config: X402Config,
        protected_paths: dict[str, Decimal],
    ) -> None:
        super().__init__(app)
        self._config = config
        self._client = X402Client(config=config)
        self._protected_paths = protected_paths

    async def dispatch(self, request: Request, call_next: Any) -> Any:
        path = request.url.path

        # Check if path is protected
        if path not in self._protected_paths:
            return await call_next(request)

        required_amount = self._protected_paths[path]
        payment_header = request.headers.get("X-PAYMENT")

        if not payment_header:
            response_body = create_402_response(
                amount_usd=required_amount,
                config=self._config,
            )
            return JSONResponse(
                status_code=402,
                content=response_body,
                headers=create_402_headers(),
            )

        try:
            result = self._client.process_payment(
                x_payment_header=payment_header,
                expected_amount_usd=required_amount,
            )
            request.state.payment_result = result
            return await call_next(request)

        except X402Error as e:
            return JSONResponse(
                status_code=402,
                content=e.to_dict(),
                headers=create_402_headers(),
            )
