"""X402Middleware decides which routes it charges from the ASGI scope path.

What a protected path is compared with is the path the application is asked
for, taken from the scope (``scope["path"]`` and ``scope["root_path"]``). The
``Host`` header plays no part in it, and neither does ``request.url``, which
Starlette rebuilds from ``Host`` and a re-parsed, decoded path.

``root_path``: the ASGI specification puts it inside ``path`` (current
servers do), older servers passed it only in ``root_path``. A protected path is
the full path the client asked for, mount included, and a server that leaves
the mount out of ``path`` is read both ways: ``root_path + path``, and ``path``
as given (what ``request.url.path`` returned there on Starlette releases after
0.27), so a protected path written either way keeps matching.

Every request here carries no payment: a charged route answers 402 before the
facilitator is involved, a route that is not charged reaches its handler. The
scopes are built by hand so that ``path``, ``root_path`` and ``Host`` are
exactly what each test says. Nothing touches a network.
"""
import asyncio
from collections.abc import Iterable
from decimal import Decimal
from typing import Any, Optional

import pytest

from uvd_x402_sdk import X402Config

RECIPIENT = "0x2222222222222222222222222222222222222222"
PRICE = Decimal("0.01")


def _app(protected: Iterable[str]) -> Any:
    pytest.importorskip("fastapi")
    from fastapi import FastAPI

    from uvd_x402_sdk.integrations.fastapi_integration import X402Middleware

    app = FastAPI()

    @app.get("/{rest:path}")
    async def anything(rest: str) -> dict[str, str]:
        return {"served": rest}

    app.add_middleware(
        X402Middleware,
        # Never reached: a request without a payment is answered before.
        config=X402Config(facilitator_url="http://127.0.0.1:9", recipient_evm=RECIPIENT),
        protected_paths={path: PRICE for path in protected},
    )
    return app


def _status(
    app: Any,
    path: str,
    *,
    root_path: str = "",
    host: Optional[str] = "testserver",
    raw_path: Optional[bytes] = None,
) -> int:
    """One GET, with the scope as given: ``path`` decoded, ``root_path`` apart."""
    headers: list[tuple[bytes, bytes]] = []
    if host is not None:
        headers.append((b"host", host.encode("latin-1")))
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": raw_path if raw_path is not None else path.encode("utf-8"),
        "root_path": root_path,
        "query_string": b"",
        "headers": headers,
        "server": ("testserver", 80),
        "client": ("127.0.0.1", 50000),
    }
    sent: list[dict[str, Any]] = []

    async def main() -> None:
        asked: list[bool] = []
        answered = asyncio.Event()

        async def receive() -> dict[str, Any]:
            # The request once; then, as a server does, nothing until the
            # response is out and the client goes away.
            if not asked:
                asked.append(True)
                return {"type": "http.request", "body": b"", "more_body": False}
            await answered.wait()
            return {"type": "http.disconnect"}

        async def send(message: dict[str, Any]) -> None:
            sent.append(message)
            if message["type"] == "http.response.body" and not message.get("more_body"):
                answered.set()

        await asyncio.wait_for(app(scope, receive, send), timeout=10)

    asyncio.run(main())
    return next(m["status"] for m in sent if m["type"] == "http.response.start")


#: Host values that are not a bare ``host[:port]``, next to ones that are.
HOSTS = [
    "testserver",
    "testserver:8080",
    "other.example",
    "testserver/free",
    "testserver/free#",
    "testserver/free?x=1",
    "testserver/paid#",
    "testserver#",
    None,
]


@pytest.mark.parametrize("host", HOSTS, ids=repr)
def test_the_host_header_has_no_say_in_which_routes_are_charged(host):
    app = _app(["/paid"])

    assert _status(app, "/paid", host=host) == 402, "a protected route was not charged"
    assert _status(app, "/free", host=host) == 200, "a route that is not protected was charged"


def test_the_path_is_the_decoded_one_the_application_routes_on():
    """``/p%61id`` is ``/paid`` for the router, and so for the middleware."""
    app = _app(["/paid"])

    assert _status(app, "/paid", raw_path=b"/p%61id") == 402
    assert _status(app, "/free", raw_path=b"/fr%65e") == 200


@pytest.mark.parametrize("host", ["testserver", "testserver/api/free#"], ids=repr)
def test_mounted_under_root_path_with_the_mount_inside_path(host):
    """The ASGI specification's form (current servers): ``path`` carries the
    mount, and the protected path is the full path the client asked for."""
    app = _app(["/api/paid"])

    assert _status(app, "/api/paid", root_path="/api", host=host) == 402
    assert _status(app, "/api/free", root_path="/api", host=host) == 200


@pytest.mark.parametrize("protected", ["/api/paid", "/paid"])
def test_mounted_under_root_path_by_a_server_that_leaves_the_mount_out_of_path(protected):
    """An older server: ``path`` without the mount, ``root_path`` apart. The full
    path (``/api/paid``) matches, and so does the path as given (``/paid``),
    which is what ``request.url.path`` returned there on Starlette after 0.27."""
    app = _app([protected])

    assert _status(app, "/paid", root_path="/api") == 402
    assert _status(app, "/free", root_path="/api") == 200


def test_a_path_that_only_starts_with_the_mount_s_letters_is_not_inside_it():
    """``/apix`` under ``root_path="/api"`` is ``/api/apix``: the mount is
    compared by whole segments."""
    app = _app(["/api/apix"])

    assert _status(app, "/apix", root_path="/api") == 402
    assert _status(app, "/apiy", root_path="/api") == 200


def test_without_a_root_path_only_the_path_counts():
    app = _app(["/api/paid"])

    assert _status(app, "/paid") == 200
    assert _status(app, "/api/paid") == 402


@pytest.mark.parametrize("host", ["testserver", "testserver/api/free#"], ids=repr)
def test_mounted_with_the_mount_inside_path_a_protected_path_may_leave_it_out(host):
    """``FastAPI(root_path=...)``, ``uvicorn --root-path`` and ``app.mount``
    give ``path`` with the mount inside it; the app routes on the path inside
    the mount, and a protected path written that way matches too."""
    app = _app(["/paid", "/"])

    assert _status(app, "/api/paid", root_path="/api", host=host) == 402
    assert _status(app, "/api", root_path="/api", host=host) == 402
    assert _status(app, "/api/free", root_path="/api", host=host) == 200


@pytest.mark.parametrize("path", ["/PAID", "/Paid", "/paid/"])
def test_a_protected_path_is_matched_exactly(path):
    """Case and a trailing slash count: only ``/paid`` is ``/paid``."""
    app = _app(["/paid"])

    assert _status(app, path) == 200
    assert _status(app, "/paid") == 402
