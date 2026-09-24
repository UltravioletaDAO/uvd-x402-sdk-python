"""The buyer's X-UVD-Purchase is checked against the URL of THIS request, built from the ASGI scope.

The FastAPI integration compares the purchase context's URL with the request:
scheme, a bare ``host[:port]`` authority, the full path (mount included) and
the query, read from the scope (``_request_url``) and never from
``request.url``. An authority that is not a bare ``host[:port]`` matches
nothing: ``400 receipt_context_mismatch``, and the facilitator is not called.

Each request is an ASGI scope built by hand, so ``path``, ``root_path`` and
``Host`` are exactly what each test says; the facilitator is
``tests/receipt_rail.py`` over a local socket. No ``from __future__ import
annotations``: FastAPI reads the endpoints' annotations at runtime.
"""
import asyncio
import json
from decimal import Decimal
from typing import Any, Optional
from urllib.parse import unquote

import httpx
import pytest

from tests.receipt_rail import RECIPIENT, Facilitator, x_payment
from uvd_x402_sdk import X402Config
from uvd_x402_sdk.models import PaymentResult
from uvd_x402_sdk.receipts import PurchaseContext

PRICE = Decimal("0.01")
#: Paths the middleware protects, decoded: the ones below and the paths of
#: ``TARGETS`` as a server decodes them.
PROTECTED = (
    "/paid", "/other", "/api/paid",
    "/x[1]", "/a|b", "/a^b", "/a\\b", "/p%q", "/a%41", "/plain",
)


def _config(rail: Facilitator) -> X402Config:
    return X402Config(facilitator_url=rail.url, recipient_evm=RECIPIENT)


def dependency(rail: Facilitator) -> Any:
    from fastapi import Depends, FastAPI

    from uvd_x402_sdk.integrations.fastapi_integration import FastAPIX402

    app = FastAPI()
    requirement = FastAPIX402(app, config=_config(rail)).require_payment(PRICE)

    @app.get("/{rest:path}")
    async def paid(rest: str, payment: PaymentResult = Depends(requirement)):
        return {"served": rest}

    return app


def x402_depends(rail: Facilitator) -> Any:
    from fastapi import Depends, FastAPI

    from uvd_x402_sdk.integrations.fastapi_integration import X402Depends

    app = FastAPI()
    requirement = X402Depends(config=_config(rail), amount_usd=PRICE)

    @app.get("/{rest:path}")
    async def paid(rest: str, payment: PaymentResult = Depends(requirement)):
        return {"served": rest}

    return app


def decorator(rail: Facilitator) -> Any:
    from fastapi import FastAPI, Request

    from uvd_x402_sdk.integrations.fastapi_integration import fastapi_require_payment

    app = FastAPI()

    @app.get("/{rest:path}")
    @fastapi_require_payment(amount_usd=PRICE, config=_config(rail))
    async def paid(request: Request, rest: str):
        return {"served": rest}

    return app


def middleware(rail: Facilitator) -> Any:
    from fastapi import FastAPI

    from uvd_x402_sdk.integrations.fastapi_integration import X402Middleware

    app = FastAPI()

    @app.get("/{rest:path}")
    async def paid(rest: str):
        return {"served": rest}

    app.add_middleware(
        X402Middleware, config=_config(rail), protected_paths={p: PRICE for p in PROTECTED}
    )
    return app


MOUNTS = [dependency, x402_depends, decorator, middleware]
mounts = pytest.mark.parametrize("mount", MOUNTS, ids=lambda mount: mount.__name__)


@pytest.fixture
def rail():
    pytest.importorskip("fastapi")
    opened = Facilitator("receipts")
    yield opened
    opened.close()


def _context(url: str) -> str:
    """The X-UVD-Purchase a buyer's client sends for a GET of ``url``."""
    context = PurchaseContext()
    context.bind(httpx.Request("GET", url))
    return context.header()


def _get(
    app: Any,
    target: str,
    context: str,
    *,
    host: Optional[str] = "testserver",
    root_path: str = "",
    server: Optional[tuple] = ("testserver", 80),
    scheme: str = "http",
) -> tuple:
    """One GET of ``target`` (path and query) as an HTTP client sends it:
    ``raw_path`` and ``query_string`` are what httpx puts on the wire, and
    ``path`` is ``raw_path`` decoded, as an ASGI server hands it on."""
    raw = httpx.Request("GET", f"{scheme}://testserver{target}").url.raw_path
    raw_path, _, query = raw.partition(b"?")
    headers = [(b"x-payment", x_payment().encode()), (b"x-uvd-purchase", context.encode())]
    if host is not None:
        headers.append((b"host", host.encode("latin-1")))
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": scheme,
        "path": unquote(raw_path.decode("ascii")),
        "raw_path": raw_path,
        "root_path": root_path,
        "query_string": query,
        "headers": headers,
        "server": server,
        "client": ("127.0.0.1", 50000),
    }
    sent: list = []

    async def main() -> None:
        asked: list = []
        answered = asyncio.Event()

        async def receive() -> dict:
            if not asked:
                asked.append(True)
                return {"type": "http.request", "body": b"", "more_body": False}
            await answered.wait()
            return {"type": "http.disconnect"}

        async def send(message: dict) -> None:
            sent.append(message)
            if message["type"] == "http.response.body" and not message.get("more_body"):
                answered.set()

        await asyncio.wait_for(app(scope, receive, send), timeout=10)

    asyncio.run(main())
    status = next(m["status"] for m in sent if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return status, json.loads(body or b"null")


def _mismatch(body: Any) -> bool:
    return isinstance(body, dict) and body.get("detail") == "receipt_context_mismatch"


@mounts
def test_the_buyers_context_for_this_request_is_accepted(rail, mount):
    status, body = _get(mount(rail), "/paid", _context("http://testserver/paid"))

    assert status == 200, body
    assert rail.executed == 1


@mounts
def test_a_context_for_another_url_is_refused_before_the_facilitator(rail, mount):
    status, body = _get(mount(rail), "/other", _context("http://testserver/paid"))

    assert (status, _mismatch(body)) == (400, True), body
    assert rail.calls == []


#: Host values that are not a bare ``host[:port]``.
NOT_BARE = [
    "testserver/paid#",
    "testserver/paid?",
    "testserver/paid",
    "testserver#",
    "user@testserver",
    "testserver:99999",
    "testserver:80/x",
    "[zz::1]",
    "[1::2::3]",
    "1.2.3.999",
    "test server",
]


@mounts
@pytest.mark.parametrize("host", NOT_BARE, ids=repr)
def test_an_authority_that_is_not_bare_matches_no_context(rail, mount, host):
    """Whatever URL the context names: here, the URL a client would write for
    that ``Host`` and this path, when it can write one at all."""
    try:
        url = str(httpx.URL(f"http://{host}/other"))
    except (httpx.InvalidURL, ValueError):
        url = "http://testserver/other"
    status, body = _get(mount(rail), "/other", _context(url), host=host)

    assert (status, _mismatch(body)) == (400, True), body
    assert rail.calls == []


@mounts
def test_mounted_with_the_mount_inside_path_the_url_carries_it(rail, mount):
    status, body = _get(
        mount(rail), "/api/paid", _context("http://testserver/api/paid"), root_path="/api"
    )

    assert status == 200, body


@mounts
def test_mounted_by_a_server_that_leaves_the_mount_out_of_path_the_url_carries_it(rail, mount):
    status, body = _get(
        mount(rail), "/paid", _context("http://testserver/api/paid"), root_path="/api"
    )

    assert status == 200, body


@pytest.mark.parametrize(
    "host, server, url",
    [
        (None, ("testserver", 80), "http://testserver/paid"),
        (None, ("testserver", 8080), "http://testserver:8080/paid"),
        ("[::1]:8080", None, "http://[::1]:8080/paid"),
        ("TestServer:8080", None, "http://testserver:8080/paid"),
    ],
    ids=["no-host-default-port", "no-host-other-port", "ipv6", "case"],
)
def test_the_authority_as_a_client_writes_it(rail, host, server, url):
    """No ``Host`` header: the server address, as Starlette falls back to it.
    An IP literal and a host in capitals are bare authorities too."""
    status, body = _get(dependency(rail), "/paid", _context(url), host=host, server=server)

    assert status == 200, body


def test_without_a_host_or_a_server_address_no_context_matches(rail):
    status, body = _get(
        dependency(rail), "/paid", _context("http://testserver/paid"), host=None, server=None
    )

    assert (status, _mismatch(body)) == (400, True), body
    assert rail.calls == []


#: Paths and queries an HTTP client writes as they are: characters it leaves
#: literal (``[ ] \\ ^ |``, a stray ``%``), escapes it keeps (``%5B``,
#: ``%2541``) and a query with a ``/`` in it.
TARGETS = [
    "/x[1]",
    "/a|b",
    "/a^b",
    "/a\\b",
    "/p%q",
    "/x%5B1%5D",
    "/a%2541",
    "/plain?q=1&r=/s",
]


@mounts
@pytest.mark.parametrize("target", TARGETS, ids=repr)
def test_the_context_a_client_wrote_for_this_url_is_accepted(rail, mount, target):
    """The URL compared is the one the client sent (``raw_path`` when it
    decodes to the path the app routes on), not a re-encoding of it."""
    status, body = _get(mount(rail), target, _context(f"http://testserver{target}"))

    assert status == 200, body
    assert rail.executed == 1


@mounts
def test_the_scheme_is_the_requests(rail, mount):
    status, body = _get(
        mount(rail), "/paid", _context("https://testserver/paid"),
        scheme="https", server=("testserver", 443),
    )
    assert status == 200, body


@mounts
def test_the_query_is_part_of_the_url(rail, mount):
    status, body = _get(mount(rail), "/plain?q=1", _context("http://testserver/plain?q=2"))

    assert (status, _mismatch(body)) == (400, True), body
    assert rail.calls == []
