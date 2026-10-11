"""
FastAPI/Starlette integration for x402 payments.

Provides:
- FastAPIX402: App integration class
- X402Depends: Dependency injection for payment verification
- fastapi_require_payment: Decorator for protected routes
"""

import asyncio
import logging
import re
import time
from decimal import Decimal
from functools import partial, wraps
from ipaddress import AddressValueError, IPv6Address
from typing import Any, Callable, Optional, TypeVar, Union
from urllib.parse import quote, unquote

import httpx

try:
    from fastapi import FastAPI, Request, Response, HTTPException, Depends
    from fastapi.encoders import jsonable_encoder
    from fastapi.responses import JSONResponse, StreamingResponse
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
from uvd_x402_sdk.durable import (
    EVIDENCE_HEADER,
    DurableConfig,
    durable_evidence,
    skip_notice,
    wants_evidence,
)
from uvd_x402_sdk.exceptions import X402Error
from uvd_x402_sdk.models import PaymentResult
from uvd_x402_sdk.response import create_402_response, create_402_headers
from uvd_x402_sdk.receipts import payment_response_headers, validate_purchase_context

logger = logging.getLogger(__name__)

F = TypeVar("F", bound=Callable[..., Any])


#: A bare ``host[:port]``: a registered name or an IP literal, and a port.
#: Nothing that could carry a path, a query, a fragment or userinfo.
_AUTHORITY = re.compile(
    r"(?:[A-Za-z0-9._~%!$&'()*+,;=-]+|\[(?P<ipv6>[0-9A-Fa-f:.]+)\])(?::(?P<port>[0-9]{1,5}))?"
)
#: What stays literal when the path of the URL compared has to be re-encoded
#: (no usable ``raw_path``); the rest is percent-encoded.
_PATH_SAFE = "/!$&'()*+,;=:@-._~"
#: A ``raw_path`` that can stand for the path as the buyer's client sent it:
#: the characters HTTP clients leave literal in a path (``[ ] \ ^ |`` and a
#: stray ``%`` included) and percent escapes; never ``?``, ``#``, a space or a
#: control character, which would end the path or change the request.
_RAW_PATH = re.compile(r"[A-Za-z0-9\-._~!$&'()*+,;=:@/\[\]\\^|%]*")
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

    Scheme, a bare authority (:func:`_authority`), the full path with its
    mount (:func:`_full_request_path`, the same path the binding store's
    resource uses), and the query as received. ``None`` when the authority is
    not a bare ``host[:port]``.

    The path is written as the client sent it: ``scope["raw_path"]`` (or the
    mount followed by it, when the server leaves the mount out), used only when
    it decodes to exactly the full path, which keeps it tied to the path the
    app routes on, and only when it holds nothing that could end the path
    (``_RAW_PATH``: no ``?``, ``#``, space or control character). Otherwise the
    full path is percent-encoded, ``_PATH_SAFE`` left literal.
    """
    authority = _authority(scope)
    if authority is None:
        return None
    full = _full_request_path(scope)
    path = quote(full, safe=_PATH_SAFE)
    raw = scope.get("raw_path")
    if isinstance(raw, bytes):
        text = raw.decode("latin-1")
        mount = quote(scope.get("root_path") or "", safe=_PATH_SAFE)
        for candidate in (text, mount + text):
            if _RAW_PATH.fullmatch(candidate) and unquote(candidate) == full:
                path = candidate
                break
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
    except (ValueError, TypeError, KeyError, httpx.InvalidURL):
        # `httpx.InvalidURL` is not a ValueError: an authority that passes
        # `_AUTHORITY` but that httpx cannot parse (an IPv4 with an octet
        # above 255) was a 500.
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


def _as_bytes(chunk: Any, charset: str) -> bytes:
    """A body chunk as Starlette sends it: buffers as they are, ``str`` encoded."""
    if isinstance(chunk, (bytes, bytearray, memoryview)):
        return bytes(chunk)
    return str(chunk).encode(charset)


def _is_event_stream(response: Response) -> bool:
    kind = response.headers.get("content-type", "")
    return kind.split(";")[0].strip().lower() == "text/event-stream"


async def _buffered(
    response: Response, limit: int, deadline: float
) -> tuple[Response, bytes, Optional[str]]:
    """``response`` with its body in memory, that body, and a skip reason.

    As x402-rs ``buffer_body`` and the TypeScript post-hook: a response without
    a readable body (``FileResponse``) or a ``text/event-stream`` is not read
    (``not_capturable``); one that announces (``Content-Length``) more than
    ``limit`` is not read (``too_large``); one that goes over ``limit`` or past
    ``deadline`` (``time.monotonic()``) while read, or whose stream fails, is
    not kept: the answer streams the chunks already read ahead of the untouched
    rest (a failure is raised again after them), with ``too_large`` /
    ``timeout`` / ``anchor_failed``. Never a byte less.
    """
    body = getattr(response, "body", None)
    if isinstance(body, (bytes, bytearray, memoryview)):
        return response, bytes(body), None
    iterable = getattr(response, "body_iterator", None)
    if iterable is None or _is_event_stream(response):
        return response, b"", "not_capturable"
    announced = response.headers.get("content-length", "")
    if announced.isdigit() and int(announced) > limit:
        return response, b"", "too_large"
    iterator = iterable.__aiter__()
    chunks: list[bytes] = []
    size = 0

    def again_with(rest: Any) -> Response:
        again: Response = StreamingResponse(
            rest, status_code=response.status_code, background=response.background
        )
        again.raw_headers = list(response.raw_headers)
        return again

    async def resumed(pending: Optional["asyncio.Future[Any]"]) -> Any:
        for seen in chunks:
            yield seen
        if pending is not None:
            try:
                yield await pending
            except StopAsyncIteration:
                return
        async for more in iterator:
            yield more

    try:
        while True:
            pending = asyncio.ensure_future(iterator.__anext__())
            done, _ = await asyncio.wait({pending}, timeout=max(deadline - time.monotonic(), 0))
            if not done:
                return again_with(resumed(pending)), b"", "timeout"
            try:
                chunk = pending.result()
            except StopAsyncIteration:
                break
            data = _as_bytes(chunk, response.charset)
            chunks.append(data)
            size += len(data)
            if size > limit:
                return again_with(resumed(None)), b"", "too_large"
    except Exception as failure:  # noqa: BLE001 - delivered as it would be without the hook
        broken = failure

        async def read_then_fail() -> Any:
            for seen in chunks:
                yield seen
            raise broken

        return again_with(read_then_fail()), b"", "anchor_failed"
    whole = b"".join(chunks)
    again = Response(
        content=whole, status_code=response.status_code, background=response.background
    )
    again.raw_headers = list(response.raw_headers)
    return again, whole, None


#: Set in the ASGI scope by :class:`_RecordsRouteFailure` when the route raised.
_ROUTE_FAILED = "uvd_x402.route_failed"


class _RecordsRouteFailure:
    """The app behind :class:`X402Middleware`, marking a failure in the scope.

    ``BaseHTTPMiddleware`` ends ``call_next``'s stream without an error when the
    route raises mid-stream (it raises later), so the bytes read look like a
    whole body; this mark is how the hook tells them apart.
    """

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        try:
            await self.app(scope, receive, send)
        except BaseException:
            scope[_ROUTE_FAILED] = True
            raise


async def _with_durable_evidence(
    durable: Optional[DurableConfig],
    client: X402Client,
    result: Any,
    payment_header: str,
    response: Response,
    method: str,
    scope: Optional[Any] = None,
) -> Response:
    """The DX402 post-hook (:mod:`uvd_x402_sdk.durable`): runs after the handler
    answered, on a payment that already settled. Off without ``durable``.

    It never raises: the payment is settled by now, so a failure of the hook
    itself is a skip notice on the response, never a 500 in its place.
    """
    if durable is None:
        return response
    current = response
    try:
        has_header = EVIDENCE_HEADER in response.headers
        if not wants_evidence(durable, result, response.status_code, method, has_header):
            return response
        deadline = time.monotonic() + durable.timeout
        current, body, skipped = await _buffered(response, durable.max_body_bytes, deadline)
        if skipped is None and scope is not None and scope.get(_ROUTE_FAILED):
            skipped = "anchor_failed"
        try:
            payee: Optional[str] = client.config.get_recipient(result.network)
        except Exception:  # noqa: BLE001 - durable.payee or an empty payee
            payee = None
        # Off the event loop, and not waited past the deadline: a thread left
        # behind finds the deadline gone and starts no anchor.
        future = asyncio.get_running_loop().run_in_executor(
            None,
            partial(
                durable_evidence,
                durable,
                result,
                payment_header,
                body,
                current.status_code,
                payee,
                skipped,
                deadline,
            ),
        )
        done, _ = await asyncio.wait({future}, timeout=max(deadline - time.monotonic(), 0))
        value = future.result() if done else skip_notice("timeout")
        if value is not None:
            current.headers[EVIDENCE_HEADER] = value
    except Exception:  # noqa: BLE001 - evidence is never a gate on the sale
        logger.warning("DX402 post-hook failed; delivered without evidence", exc_info=True)
        try:
            current.headers[EVIDENCE_HEADER] = skip_notice("anchor_failed")
        except Exception:  # noqa: BLE001 - the response goes out as it is
            pass
    return current


def fastapi_require_payment(
    amount_usd: Union[Decimal, float, str],
    config: X402Config,
    message: Optional[str] = None,
    binding_store: Optional[BindingStore] = None,
    durable: Optional[DurableConfig] = None,
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
        durable: DX402 durable evidence of the response the route returned
            (:mod:`uvd_x402_sdk.durable`), in ``X-Durable-Evidence``. Off by
            default; never changes the body, the status or the settlement.

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
                if durable is not None:
                    if not isinstance(response, Response) and wants_evidence(
                        durable, result, 200, request.method
                    ):
                        response = JSONResponse(jsonable_encoder(response))
                    if isinstance(response, Response):
                        response = await _with_durable_evidence(
                            durable, client, result, payment_header, response, request.method
                        )
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

    ``durable`` (:class:`~uvd_x402_sdk.durable.DurableConfig`, off by default)
    anchors DX402 durable evidence of the route's response once the payment
    settled and the route answered, in ``X-Durable-Evidence``; the body, the
    status and the settlement are delivered as they are, whatever it does.

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
        durable: Optional[DurableConfig] = None,
    ) -> None:
        super().__init__(app if durable is None else _RecordsRouteFailure(app))
        self._durable = durable
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
            return await _with_durable_evidence(
                self._durable,
                self._client,
                result,
                payment_header,
                response,
                request.method,
                request.scope,
            )

        except HTTPException as e:
            # `_receipt_context`'s 400 receipt_context_mismatch. A middleware
            # runs outside FastAPI's exception handlers, so it answers the 400
            # itself; left to propagate it was a 500. (A route's own
            # HTTPException never gets here: the app answers it downstream.)
            return JSONResponse(
                status_code=e.status_code, content={"detail": e.detail}, headers=e.headers
            )
        except X402Error as e:
            status, content, headers = _payment_error(e)
            return JSONResponse(status_code=status, content=content, headers=headers)
