"""FastAPI integration: blocking payment I/O must run off the event loop.

Why this exists: ``X402Client.process_payment`` does blocking HTTP with a sync
``httpx.Client`` — verify waits up to 30s and settle up to 90s on L2s, plus a
30s fallback. Every async entry point of the FastAPI integration used to call
it directly, so while ONE payment settled, the server's event loop was frozen
solid: no /health, no other requests, nothing. A handful of concurrent
requests carrying schema-valid-but-bogus X-PAYMENT headers was enough to take
a paid API offline (found by tarotof's api, which had to bypass the
integration and call the client via ``asyncio.to_thread`` itself).

The fix routes ``process_payment`` through ``starlette.concurrency
.run_in_threadpool`` in all four entry points (``FastAPIX402.require_payment``,
``X402Depends``, ``fastapi_require_payment``, ``X402Middleware``).

The heartbeat test is the one that bites: a task ticking every 20ms on the
same loop must keep ticking while a deliberately slow payment (400ms of
``time.sleep``) processes. With the old in-loop call the heartbeat counts 0
ticks and the test fails; with the threadpool it counts well over 5.
"""
from __future__ import annotations

import asyncio
import threading
import time
from decimal import Decimal

import httpx
import pytest

fastapi = pytest.importorskip("fastapi")

from fastapi import Depends, FastAPI, Request  # noqa: E402

from uvd_x402_sdk import X402Client  # noqa: E402
from uvd_x402_sdk.config import X402Config  # noqa: E402
from uvd_x402_sdk.exceptions import X402Error  # noqa: E402
from uvd_x402_sdk.integrations.fastapi_integration import (  # noqa: E402
    FastAPIX402,
    X402Depends,
    X402Middleware,
    fastapi_require_payment,
)

RECIPIENT = "0x" + "11" * 20
SENTINEL = object()  # stands in for PaymentResult; the integration only passes it through


def _config() -> X402Config:
    return X402Config(recipient_evm=RECIPIENT, supported_networks=["base"])


def _record_thread(record: dict):
    """A process_payment double that records which thread ran it."""

    def fake(self, x_payment_header, expected_amount_usd, **kwargs):
        record["thread"] = threading.get_ident()
        record["header"] = x_payment_header
        return SENTINEL

    return fake


def _request_with_payment() -> Request:
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/paid",
        "query_string": b"",
        "headers": [(b"x-payment", b"sobre-de-prueba")],
    }
    return Request(scope)


def test_dependency_does_not_block_the_event_loop(monkeypatch):
    def slow(self, x_payment_header, expected_amount_usd, **kwargs):
        time.sleep(0.4)
        return SENTINEL

    monkeypatch.setattr(X402Client, "process_payment", slow)

    app = FastAPI()
    x402 = FastAPIX402(app, config=_config())

    @app.post("/paid")
    async def paid(payment=Depends(x402.require_payment(Decimal("1.00")))):
        return {"ok": payment is SENTINEL}

    async def main():
        beats = 0
        done = asyncio.Event()

        async def heartbeat():
            nonlocal beats
            while not done.is_set():
                beats += 1
                await asyncio.sleep(0.02)

        hb = asyncio.create_task(heartbeat())
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            response = await c.post("/paid", headers={"X-PAYMENT": "sobre"})
        done.set()
        await hb
        return beats, response

    beats, response = asyncio.run(main())
    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert beats >= 5, (
        f"the event loop only beat {beats} times during a 400ms payment: "
        "process_payment is blocking the loop again"
    )


def test_x402_depends_runs_payment_off_the_loop(monkeypatch):
    record: dict = {}
    monkeypatch.setattr(X402Client, "process_payment", _record_thread(record))

    dep = X402Depends(config=_config(), amount_usd="1.00")
    result = asyncio.run(dep(_request_with_payment()))

    assert result is SENTINEL
    assert record["header"] == "sobre-de-prueba"
    assert record["thread"] != threading.get_ident()


def test_decorator_runs_payment_off_the_loop(monkeypatch):
    record: dict = {}
    monkeypatch.setattr(X402Client, "process_payment", _record_thread(record))

    @fastapi_require_payment(amount_usd="1.00", config=_config())
    async def endpoint(request: Request):
        return {"paid": request.state.payment_result is SENTINEL}

    result = asyncio.run(endpoint(_request_with_payment()))

    assert result == {"paid": True}
    assert record["thread"] != threading.get_ident()


def test_middleware_runs_payment_off_the_loop(monkeypatch):
    record: dict = {}
    monkeypatch.setattr(X402Client, "process_payment", _record_thread(record))

    app = FastAPI()

    @app.post("/paid")
    async def paid(request: Request):
        return {"paid": request.state.payment_result is SENTINEL}

    app.add_middleware(
        X402Middleware,
        config=_config(),
        protected_paths={"/paid": Decimal("1.00")},
    )

    async def main():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            return await c.post("/paid", headers={"X-PAYMENT": "sobre"})

    response = asyncio.run(main())
    assert response.status_code == 200
    assert response.json() == {"paid": True}
    assert record["thread"] != threading.get_ident()


def test_x402_error_from_the_threadpool_still_becomes_a_402(monkeypatch):
    def failing(self, x_payment_header, expected_amount_usd, **kwargs):
        raise X402Error("firma invalida", code="PAYMENT_VERIFICATION_FAILED")

    monkeypatch.setattr(X402Client, "process_payment", failing)

    app = FastAPI()
    x402 = FastAPIX402(app, config=_config())

    @app.post("/paid")
    async def paid(payment=Depends(x402.require_payment(Decimal("1.00")))):
        return {"ok": True}  # pragma: no cover - the dependency raises first

    async def main():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            return await c.post("/paid", headers={"X-PAYMENT": "sobre"})

    response = asyncio.run(main())
    assert response.status_code == 402
    assert response.json()["detail"]["error"] == "PAYMENT_VERIFICATION_FAILED"


def test_missing_header_still_answers_402_without_touching_the_client(monkeypatch):
    def exploding(self, x_payment_header, expected_amount_usd, **kwargs):
        raise AssertionError("process_payment must not run without a header")

    monkeypatch.setattr(X402Client, "process_payment", exploding)

    app = FastAPI()
    x402 = FastAPIX402(app, config=_config())

    @app.post("/paid")
    async def paid(payment=Depends(x402.require_payment(Decimal("1.00")))):
        return {"ok": True}  # pragma: no cover

    async def main():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            return await c.post("/paid")

    response = asyncio.run(main())
    assert response.status_code == 402
    assert "error" in response.json()["detail"] or "recipient" in response.json()["detail"]
