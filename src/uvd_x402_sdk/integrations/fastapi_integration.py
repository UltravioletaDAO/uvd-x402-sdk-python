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
    from fastapi import FastAPI, Request, Response, HTTPException, Depends
    from fastapi.responses import JSONResponse
    from starlette.concurrency import run_in_threadpool
    from starlette.middleware.base import BaseHTTPMiddleware
except ImportError:
    raise ImportError(
        "FastAPI is required for FastAPI integration. "
        "Install with: pip install uvd-x402-sdk[fastapi]"
    )

from uvd_x402_sdk.client import X402Client, _undelivered_response
from uvd_x402_sdk.config import X402Config
from uvd_x402_sdk.exceptions import X402Error
from uvd_x402_sdk.models import PaymentResult
from uvd_x402_sdk.response import create_402_response, create_402_headers
from uvd_x402_sdk.receipts import payment_response_headers, validate_purchase_context

F = TypeVar("F", bound=Callable[..., Any])


async def _receipt_context(request: Request) -> Optional[str]:
    header = request.headers.get("X-UVD-Purchase")
    if header is None:
        return None
    try:
        return validate_purchase_context(header, request.method, str(request.url), await request.body())
    except (ValueError, TypeError, KeyError):
        raise HTTPException(status_code=400, detail="receipt_context_mismatch")


def _receipt_error_headers(error: X402Error) -> dict[str, str]:
    receipt = getattr(error, "receipt", None)
    if receipt is None:
        return create_402_headers()
    return {**create_402_headers(), **payment_response_headers({"success": False, "receipt": receipt.model_dump()})}


def _payment_error_status(error: X402Error) -> int:
    receipt = getattr(error, "receipt", None)
    return 503 if (receipt and receipt.status in ("unknown", "pending")) or getattr(error, "retryable", False) else 402


def _payment_error(error: X402Error) -> tuple[int, Any, dict[str, str]]:
    """Status, body and headers for a payment that was not delivered on.

    An authorization the facilitator already admitted for another request is
    409, or 503 + Retry-After while it is still in flight; a failure without a
    verdict (a timeout, a settle still in flight, a store the facilitator could
    not read) is 503 + Retry-After. Never a 402 for either, which would ask the
    buyer for a second payment. Every rejection keeps the answer it had.
    """
    answer = _undelivered_response(error)
    if answer is not None:
        return answer
    return _payment_error_status(error), error.to_dict(), _receipt_error_headers(error)


async def _process_payment(
    client: X402Client, request: Request, payment_header: str, amount: Decimal
) -> PaymentResult:
    """``process_payment`` off the event loop.

    process_payment does blocking HTTP (sync httpx: verify up to 30s + settle
    up to 90s on L2s). Called directly inside an async entry point it would
    freeze the whole event loop for every request on the server, /health
    included, while one payment settles.

    Each request is one handling with a fresh key, so its only purchase
    binding from outside is the buyer's ``X-UVD-Purchase``. A replayed settle
    that reaches it without one is another request's purchase, and
    process_payment raises instead of returning it.
    """
    return await run_in_threadpool(
        client.process_payment,
        x_payment_header=payment_header,
        expected_amount_usd=amount,
        receipt_context=await _receipt_context(request),
    )


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

        async def dependency(request: Request, response: Response = None) -> PaymentResult:
            payment_header = request.headers.get("PAYMENT-SIGNATURE") or request.headers.get("X-PAYMENT")

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
                result = await _process_payment(
                    self._client, request, payment_header, required_amount
                )
                if response is not None and getattr(result, "receipt", None):
                    response.headers.update(payment_response_headers(result))
                return result
            except X402Error as e:
                status, detail, headers = _payment_error(e)
                raise HTTPException(status_code=status, detail=detail, headers=headers)

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

    async def __call__(self, request: Request, response: Response = None) -> PaymentResult:
        """Process payment when used as dependency."""
        payment_header = request.headers.get("PAYMENT-SIGNATURE") or request.headers.get("X-PAYMENT")

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
            result = await _process_payment(self._client, request, payment_header, self._amount)
            if response is not None and getattr(result, "receipt", None):
                response.headers.update(payment_response_headers(result))
            return result
        except X402Error as e:
            status, detail, headers = _payment_error(e)
            raise HTTPException(status_code=status, detail=detail, headers=headers)


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
            payment_header = request.headers.get("PAYMENT-SIGNATURE") or request.headers.get("X-PAYMENT")

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
                result = await _process_payment(client, request, payment_header, required_amount)
                # Store result in request state
                request.state.payment_result = result
                response = await func(request, *args, **kwargs)
                if getattr(result, "receipt", None):
                    headers = payment_response_headers(result)
                    if isinstance(response, Response):
                        response.headers.update(headers)
                    else:
                        response = JSONResponse(response, headers=headers)
                return response

            except X402Error as e:
                status, content, headers = _payment_error(e)
                return JSONResponse(status_code=status, content=content, headers=headers)

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
        payment_header = request.headers.get("PAYMENT-SIGNATURE") or request.headers.get("X-PAYMENT")

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
            result = await _process_payment(self._client, request, payment_header, required_amount)
            request.state.payment_result = result
            response = await call_next(request)
            if getattr(result, "receipt", None):
                response.headers.update(payment_response_headers(result))
            return response

        except X402Error as e:
            status, content, headers = _payment_error(e)
            return JSONResponse(status_code=status, content=content, headers=headers)
