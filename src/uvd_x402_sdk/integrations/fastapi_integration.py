"""
FastAPI/Starlette integration for x402 payments.

Provides:
- FastAPIX402: App integration class
- X402Depends: Dependency injection for payment verification
- fastapi_require_payment: Decorator for protected routes
"""

import re
from decimal import Decimal
from functools import wraps
from ipaddress import AddressValueError, IPv6Address
from typing import Any, Callable, Optional, TypeVar, Union
from urllib.parse import quote

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

from uvd_x402_sdk.bindings import (
    BindingStore,
    check_binding_config,
    process_payment_bound,
    purchase_resource,
)
from uvd_x402_sdk.client import X402Client, _undelivered_response
from uvd_x402_sdk.config import X402Config
from uvd_x402_sdk.exceptions import X402Error
from uvd_x402_sdk.models import PaymentResult
from uvd_x402_sdk.response import create_402_response, create_402_headers
from uvd_x402_sdk.receipts import payment_response_headers, validate_purchase_context

F = TypeVar("F", bound=Callable[..., Any])


#: A bare ``host[:port]``: a registered name or an IP literal, and a port.
#: Nothing that could carry a path, a query, a fragment or userinfo.
_AUTHORITY = re.compile(
    r"(?:[A-Za-z0-9._~%!$&'()*+,;=-]+|\[(?P<ipv6>[0-9A-Fa-f:.]+)\])(?::(?P<port>[0-9]{1,5}))?"
)
#: What stays literal in the path of the URL compared; the rest is
#: percent-encoded, as the buyer's HTTP client sent it.
_PATH_SAFE = "/!$&'()*+,;=:@-._~"
_DEFAULT_PORTS = {"http": 80, "https": 443}


def _authority(scope: Any) -> Optional[str]:
    """The request's ``host[:port]``, or ``None`` when it is not a bare one.

    The ``Host`` header when there is one; otherwise the server address, as
    Starlette falls back to it.
    """
    host: Optional[str] = None
    for key, value in scope.get("headers") or ():
        if key.lower() == b"host":
            host = value.decode("latin-1")
            break
    if host is None:
        server = scope.get("server")
        if not server:
            return None
        name, port = server
        if ":" in name and not name.startswith("["):
            name = f"[{name}]"
        default = _DEFAULT_PORTS.get(scope.get("scheme", "http"))
        host = name if port in (None, default) else f"{name}:{port}"
    match = _AUTHORITY.fullmatch(host)
    if match is None:
        return None
    if match["port"] is not None and int(match["port"]) > 65535:
        return None
    if match["ipv6"] is not None:
        try:
            IPv6Address(match["ipv6"])
        except AddressValueError:
            return None
    return host


def _request_url(scope: Any) -> Optional[str]:
    """The URL of this request as the buyer's client wrote it, from the ASGI scope.

    Scheme, a bare authority (:func:`_authority`), the full path, mount
    included (:func:`_requested_paths`), percent-encoded, and the query as
    received. ``None`` when the authority is not a bare ``host[:port]``.
    """
    authority = _authority(scope)
    if authority is None:
        return None
    path = quote(_requested_paths(scope)[0], safe=_PATH_SAFE)
    url = f"{scope.get('scheme', 'http')}://{authority}{path}"
    query = (scope.get("query_string") or b"").decode("latin-1")
    return f"{url}?{query}" if query else url


async def _receipt_context(request: Request) -> Optional[str]:
    """The buyer's ``X-UVD-Purchase``, checked against THIS request.

    Its URL is compared with the URL of the request built from the ASGI scope
    (:func:`_request_url`), not with ``request.url``; an authority that is not
    a bare ``host[:port]`` matches nothing. Any mismatch is ``400
    receipt_context_mismatch``, before the facilitator is called.
    """
    header = request.headers.get("X-UVD-Purchase")
    if header is None:
        return None
    url = _request_url(request.scope)
    try:
        if url is None:
            raise ValueError("the request's authority is not a bare host[:port]")
        return validate_purchase_context(header, request.method, url, await request.body())
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

    An authorization the facilitator already admitted for another request, or
    says was already used, is 409 (503 + Retry-After while it is still in
    flight); a payment that may have moved (a transaction broadcast without a
    verdict, such as ``502 settlement_unconfirmed``, a 5xx the facilitator said
    not to retry, or a settle that failed after a valid verify) is 500, with the
    transaction to check when there is one; a failure without
    a verdict (a timeout, a settle still in flight, a store the facilitator
    could not read) is 503 + Retry-After. Never a 402 for any of them, which
    would ask the buyer for a second payment. Every rejection keeps the answer
    it had.
    """
    answer = _undelivered_response(error)
    if answer is not None:
        return answer
    return _payment_error_status(error), error.to_dict(), _receipt_error_headers(error)


async def _process_payment(
    client: X402Client,
    request: Request,
    payment_header: str,
    amount: Decimal,
    binding_store: Optional[BindingStore] = None,
) -> PaymentResult:
    """``process_payment`` off the event loop.

    process_payment does blocking HTTP (sync httpx: verify up to 30s + settle
    up to 90s on L2s). Called directly inside an async entry point it would
    freeze the whole event loop for every request on the server, /health
    included, while one payment settles.

    Without ``binding_store`` each request is one handling with a fresh key,
    so its only purchase binding from outside is the buyer's
    ``X-UVD-Purchase``. A replayed settle that reaches it without one is
    another request's purchase, and process_payment raises instead of
    returning it.

    With one, the payment's key is the one the store persisted for it
    (:func:`~uvd_x402_sdk.bindings.process_payment_bound`), for the resource
    this request buys: method, the full path with its mount
    (:func:`_full_request_path`), query and body, read from the ASGI scope the
    app routes on. The mount matters: two apps mounted at ``/a`` and ``/b``
    that share one store sell ``/a/x`` and ``/b/x``, not ``/x`` twice. Never
    from ``request.url``: Starlette rebuilds it from the DECODED path (a
    ``%23`` in a segment becomes a ``#`` that cuts it) and, in some releases,
    from the ``Host`` header. The store's I/O runs off the event loop too.
    """
    receipt_context = await _receipt_context(request)
    if binding_store is None:
        return await run_in_threadpool(
            client.process_payment,
            x_payment_header=payment_header,
            expected_amount_usd=amount,
            receipt_context=receipt_context,
        )
    resource = purchase_resource(
        request.method,
        _full_request_path(request.scope),
        request.scope.get("query_string", b"").decode("latin-1"),
        await request.body(),
    )
    return await run_in_threadpool(
        process_payment_bound,
        client,
        binding_store,
        payment_header,
        amount,
        resource,
        receipt_context=receipt_context,
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
        binding_store: Optional[BindingStore] = None,
        **kwargs: Any,
    ) -> None:
        """
        Initialize FastAPI x402 integration.

        Args:
            app: FastAPI application (optional)
            config: X402Config object
            recipient_address: Default recipient for EVM chains
            binding_store: Where each payment's key is persisted before the
                facilitator is called (:mod:`uvd_x402_sdk.bindings`), so the
                resend of a payment whose process died after the settle
                recovers it. Within the window a byte-identical resend runs
                the route again with ``payment.idempotent_replayed`` true:
                check it where the route has side effects or answers per
                caller. Without a store, nothing changes.
            **kwargs: Additional config parameters
        """
        self._config = config or X402Config(
            recipient_evm=recipient_address or "",
            **kwargs,
        )
        self._client = X402Client(config=self._config)
        if binding_store is not None:
            check_binding_config(self._config)
        self._binding_store = binding_store

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
                    self._client, request, payment_header, required_amount, self._binding_store
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
        binding_store: Optional[BindingStore] = None,
    ) -> None:
        self._config = config
        self._client = X402Client(config=config)
        self._amount = Decimal(str(amount_usd))
        self._message = message
        if binding_store is not None:
            check_binding_config(config)
        self._binding_store = binding_store

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
            result = await _process_payment(
                self._client, request, payment_header, self._amount, self._binding_store
            )
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
    binding_store: Optional[BindingStore] = None,
) -> Callable[[F], F]:
    """
    Decorator for FastAPI routes requiring payment.

    Alternative to dependency injection for simpler cases.

    Args:
        amount_usd: Required payment amount
        config: X402Config with recipient addresses
        message: Custom 402 message
        binding_store: Persist each payment's key before the facilitator is
            called (:mod:`uvd_x402_sdk.bindings`). Without one, nothing changes.

    Example:
        >>> @app.get("/resource")
        >>> @fastapi_require_payment(amount_usd="1.00", config=config)
        >>> async def resource(request: Request):
        ...     # Payment already verified
        ...     return {"success": True}
    """
    required_amount = Decimal(str(amount_usd))
    client = X402Client(config=config)
    if binding_store is not None:
        check_binding_config(config)

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
                result = await _process_payment(
                    client, request, payment_header, required_amount, binding_store
                )
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


def _full_request_path(scope: Any) -> str:
    """The full path this request asks for, mount (``root_path``) included.

    The ASGI specification puts ``root_path`` inside ``path`` (current
    servers, Starlette 0.33 and later), compared by whole segments; an older
    server, or a ``Mount`` before Starlette 0.33, leaves it out of ``path``,
    and then it is ``root_path + path``. The same full path ``protected_paths``
    reads first (:func:`_requested_paths`), stated on its own so that no caller
    depends on the order of that tuple.
    """
    path: str = scope["path"]
    root_path: str = scope.get("root_path") or ""
    if not root_path or path == root_path or path.startswith(root_path + "/"):
        return path
    return root_path + path


def _middleware_rereads_the_body() -> bool:
    """Can a route read the body a ``BaseHTTPMiddleware`` already read?

    Measured: Starlette 0.27.0 cannot (the route waits for ever), 0.28.0 can.
    FastAPI 0.100.x pins Starlette below 0.28. A version string this cannot
    read is taken as a later release.
    """
    import starlette

    try:
        major, minor = (int(part) for part in starlette.__version__.split(".")[:2])
    except ValueError:
        return True
    return (major, minor) >= (0, 28)


def _requested_paths(scope: Any) -> tuple[str, ...]:
    """The path this request asks for, as ``protected_paths`` names routes.

    Read from the ASGI scope the application routes on (``scope["path"]``,
    already decoded, and ``scope["root_path"]``), never from ``request.url``,
    which Starlette rebuilds from the ``Host`` header and a re-parsed path.
    The first reading is always the full path, mount included.

    Under a mount (``root_path``) a protected path may be written with or
    without it, and either one matches:

    * The ASGI specification puts ``root_path`` inside ``path`` (current
      servers do), compared by whole segments: the readings are ``path`` and
      the path inside the mount, ``path[len(root_path):]`` or ``/`` (the path
      Starlette routes on, ``get_route_path``).
    * A server that passes the mount only in ``root_path``: ``root_path +
      path`` and ``path`` as given.
    """
    path: str = scope["path"]
    root_path: str = scope.get("root_path") or ""
    if not root_path:
        return (path,)
    if path == root_path or path.startswith(root_path + "/"):
        return (path, path[len(root_path):] or "/")
    return (root_path + path, path)


class X402Middleware(BaseHTTPMiddleware):
    """
    Middleware that automatically handles x402 payments for configured paths.

    ``protected_paths`` are matched against the path of the ASGI scope,
    independent of the ``Host`` header, exactly (case and a trailing slash
    count). Under a mount (``root_path``) a protected path may be written with
    or without it.

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
        binding_store: Optional[BindingStore] = None,
    ) -> None:
        super().__init__(app)
        self._config = config
        self._client = X402Client(config=config)
        self._protected_paths = protected_paths
        if binding_store is not None:
            check_binding_config(config)
            if not _middleware_rereads_the_body():
                raise ValueError(
                    "X402Middleware with a binding_store needs Starlette 0.28 or later: before "
                    "it, BaseHTTPMiddleware cannot hand the body it read to the route, which "
                    "then waits for it after the payment settled. Use FastAPIX402."
                    "require_payment or X402Depends, or upgrade Starlette."
                )
        self._binding_store = binding_store

    async def dispatch(self, request: Request, call_next: Any) -> Any:
        # Check if path is protected
        path = next(
            (p for p in _requested_paths(request.scope) if p in self._protected_paths), None
        )
        if path is None:
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
            result = await _process_payment(
                self._client, request, payment_header, required_amount, self._binding_store
            )
            request.state.payment_result = result
            response = await call_next(request)
            if getattr(result, "receipt", None):
                response.headers.update(payment_response_headers(result))
            return response

        except HTTPException as e:
            # `_receipt_context`'s 400 receipt_context_mismatch. A middleware
            # runs outside FastAPI's exception handlers, so it answers the 400
            # itself; left to propagate it was a 500. (A route's own
            # HTTPException never gets here: the app answers it downstream.)
            return JSONResponse(status_code=e.status_code, content={"detail": e.detail}, headers=e.headers)
        except X402Error as e:
            status, content, headers = _payment_error(e)
            return JSONResponse(status_code=status, content=content, headers=headers)
