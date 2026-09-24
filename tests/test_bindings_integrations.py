"""The Lambda dies between the settle and the put, and the resend recovers the verdict.

The case a ``BindingStore`` exists for, through the FastAPI and Lambda
integrations of the SDK, against ``tests/receipt_rail.py`` (a facilitator over a
real socket), with the store in memory, over ``FakePostgres``, over moto's
DynamoDB and, when ``UVD_TEST_POSTGRES_DSN`` names one, over a real Postgres
(see ``tests/test_bindings.py``):

  1. instance A binds the payment, verifies and settles; the facilitator moves
     the money, and A dies before delivering or recording anything;
  2. the buyer presents the SAME ``X-PAYMENT`` again, as every 503 of the SDK
     asks, and it lands on instance B: another client, another process, the
     same store (the same database);
  3. B finds the key A persisted before calling the facilitator, and the
     facilitator hands back the original settle under it: delivered once,
     charged once.

Without the persisted key the same death loses the payment (409 with the
receipt rail, 500 without it): that is 0.91.0, and a store that does not
persist, and both are pinned here as the contrast that proves the recovery
comes from the stored key.

"THE LAMBDA DIED" is an exception that is neither an ``httpx`` error nor an
x402 one (``LambdaDied``), raised by instance A's transport once the settle
went out: it crosses the SDK and the integration untouched, as a SIGKILL or a
Lambda timeout would. Nothing here touches a network other than the local
socket, and nothing moves money.

No ``from __future__ import annotations`` here: FastAPI reads the endpoints'
annotations at runtime.
"""
import asyncio
import base64
import json
import os
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from decimal import Decimal
from typing import Any, Callable, Optional

import httpx
import pytest

import uvd_x402_sdk.client as client_module
from tests.binding_doubles import BrokenPool, FakePostgres, moto_dynamodb
from tests.receipt_rail import RECIPIENT, Facilitator, _receipt, x_payment
from uvd_x402_sdk import X402Client, X402Config
from uvd_x402_sdk.bindings import (
    POSTGRES_BINDINGS_SCHEMA,
    Binding,
    DynamoDBBindingStore,
    InMemoryBindingStore,
    PostgresBindingStore,
    binding_window_seconds,
    payment_key,
    process_payment_bound,
    purchase_resource,
)
from uvd_x402_sdk.client import _undelivered_response, is_transient_error, new_idempotency_key
from uvd_x402_sdk.exceptions import (
    PAYMENT_ALREADY_USED,
    PAYMENT_PRESENTED_BEFORE,
    PAYMENT_STORE_UNAVAILABLE,
    FacilitatorError,
    PaymentBindingError,
    PaymentVerificationError,
)
from uvd_x402_sdk.exceptions import (
    TimeoutError as X402TimeoutError,
)
from uvd_x402_sdk.models import PaymentResult
from uvd_x402_sdk.receipts import parse_receipt

PRICE = Decimal("0.01")
#: The routes of every FastAPI app below; the middleware protects them by path.
PATHS = ("/paid", "/other", "/gen/{topic}")
PROTECTED = ("/paid", "/other", "/gen/cats")

#: (status, JSON body, headers with lowercase names)
Answer = tuple[int, dict[str, Any], dict[str, str]]


class LambdaDied(BaseException):
    """The function was cut off mid-payment. ``BaseException`` on purpose: neither
    the SDK (``httpx`` and x402 errors) nor the integrations (``X402Error``)
    catch it, as with a SIGKILL."""


def _is_death(exc: BaseException) -> bool:
    if isinstance(exc, LambdaDied):
        return True
    return any(_is_death(inner) for inner in getattr(exc, "exceptions", ()))


class Death:
    """Kills the next process that settles: ``"after"`` the facilitator answered
    (the money moved) or ``"in-flight"`` (admitted, not confirmed yet)."""

    def __init__(self) -> None:
        self.armed: Optional[str] = None
        self.rail: Optional[Facilitator] = None

    def arm(self, rail: Facilitator, when: str = "after") -> None:
        self.rail, self.armed = rail, when


class _Transport(httpx.BaseTransport):
    def __init__(self, death: Death) -> None:
        self._death = death
        self._inner = httpx.HTTPTransport()

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        if request.url.path != "/settle" or self._death.armed is None:
            return self._inner.handle_request(request)
        when, self._death.armed = self._death.armed, None
        if when == "after":
            self._send(request)
            raise LambdaDied("after the settle")
        threading.Thread(target=self._send, args=(request,), daemon=True).start()
        deadline = time.monotonic() + 5
        while not self._death.rail.keys("/settle"):
            assert time.monotonic() < deadline, "the settle never reached the facilitator"
            time.sleep(0.01)
        raise LambdaDied("with the settle in flight")

    def _send(self, request: httpx.Request) -> None:
        response = self._inner.handle_request(request)
        response.read()
        response.close()


@pytest.fixture
def death(monkeypatch):
    """Every X402Client of the test talks through a transport that can die."""
    armed = Death()
    client = httpx.Client(transport=_Transport(armed))
    monkeypatch.setattr(X402Client, "_get_http_client", lambda self: client)
    yield armed
    client.close()


@pytest.fixture
def rails():
    opened = []

    def open_rail(mode: str = "receipts", **options: Any) -> Facilitator:
        rail = Facilitator(mode, **options)
        opened.append(rail)
        return rail

    yield open_rail
    for rail in opened:
        rail.close()


LIVE_DSN = os.environ.get("UVD_TEST_POSTGRES_DSN")
#: A table of its own, created and dropped around each test, so a DSN that
#: points at a shared database never touches its bindings.
LIVE_TABLE = "uvd_sdk_test_integration_bindings"


@contextmanager
def _live_postgres_store() -> Iterator[Callable[[], PostgresBindingStore]]:
    psycopg = pytest.importorskip("psycopg")
    pool_module = pytest.importorskip("psycopg_pool")
    with psycopg.connect(LIVE_DSN, autocommit=True) as conn:
        conn.execute(POSTGRES_BINDINGS_SCHEMA.replace("payment_bindings", LIVE_TABLE))
    pool = pool_module.ConnectionPool(LIVE_DSN, min_size=1, max_size=4, open=False, timeout=5)
    pool.open(wait=True, timeout=10)
    try:
        yield lambda: PostgresBindingStore(pool, table=LIVE_TABLE)
    finally:
        pool.close()
        with psycopg.connect(LIVE_DSN, autocommit=True) as conn:
            conn.execute(f"DROP TABLE IF EXISTS {LIVE_TABLE}")


@pytest.fixture(params=["memory", "postgres", "dynamodb"] + (["postgres-live"] if LIVE_DSN else []))
def stores(request, monkeypatch):
    """``stores()`` is the store of one more instance, over the same data."""
    if request.param == "memory":
        shared = InMemoryBindingStore()
        yield lambda: shared
    elif request.param == "postgres":
        db = FakePostgres()
        yield lambda: PostgresBindingStore(db)
    elif request.param == "postgres-live":
        with _live_postgres_store() as store:
            yield store
    else:
        with moto_dynamodb(monkeypatch) as client:
            yield lambda: DynamoDBBindingStore(client())


class Forgetful:
    """A store that persists nothing: every request is a first presentation."""

    def bind(self, payment_key: str, resource: str, ttl_seconds: float) -> Optional[Binding]:
        return Binding(resource, new_idempotency_key(), new=True)


class AlwaysNew:
    """Persists the key, but says every presentation is the first one."""

    def __init__(self) -> None:
        self._inner = InMemoryBindingStore()

    def bind(self, payment_key: str, resource: str, ttl_seconds: float) -> Optional[Binding]:
        found = self._inner.bind(payment_key, resource, ttl_seconds)
        return Binding(found.resource, found.idempotency_key, new=True)


class PerResource:
    """One binding per (payment, resource): the rule "one payment buys one
    resource" removed."""

    def __init__(self) -> None:
        self._inner = InMemoryBindingStore()

    def bind(self, payment_key: str, resource: str, ttl_seconds: float) -> Optional[Binding]:
        return self._inner.bind(f"{payment_key}|{resource}", resource, ttl_seconds)


# -- the entry points -----------------------------------------------------------


class Site:
    """One entry point on one "instance": what it delivered, and with what."""

    def __init__(self) -> None:
        self.delivered = 0
        self.results: list[PaymentResult] = []
        self.bodies: list[str] = []
        self._call: Callable[..., Answer] = lambda *_: (0, {}, {})

    def get(
        self,
        payment: str,
        path: str = "/paid",
        body: Optional[bytes] = None,
        host: Optional[str] = None,
    ) -> Answer:
        return self._call(payment, path, body, host)

    def dies(self, payment: str) -> None:
        with pytest.raises(BaseException) as info:
            self.get(payment)
        assert _is_death(info.value), repr(info.value)

    def record(self, result: Optional[PaymentResult], body: str) -> dict[str, Any]:
        self.delivered += 1
        if result is not None:
            self.results.append(result)
        self.bodies.append(body)
        return {"delivered": True}


def _config(rail: Facilitator, **options: Any) -> X402Config:
    return X402Config(facilitator_url=rail.url, recipient_evm=RECIPIENT, **options)


def _lower(headers: Any) -> dict[str, str]:
    return {str(k).lower(): v for k, v in dict(headers).items()}


def _inner(body: dict[str, Any]) -> dict[str, Any]:
    inner = body.get("detail", body)
    return inner if isinstance(inner, dict) else {}


def _reason(body: dict[str, Any]) -> Optional[str]:
    return _inner(body).get("reason")


def _asgi(app: Any) -> Callable[..., Answer]:
    def call(payment: str, path: str, body: Optional[bytes], host: Optional[str]) -> Answer:
        async def main() -> httpx.Response:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
                headers = {"X-PAYMENT": payment, **({"Host": host} if host else {})}
                if body is None:
                    return await c.get(path, headers=headers)
                return await c.post(path, headers=headers, content=body)

        response = asyncio.run(main())
        return response.status_code, response.json(), _lower(response.headers)

    return call


def fastapi_dependency(rail: Facilitator, store: Any) -> Site:
    pytest.importorskip("fastapi")
    from fastapi import Depends, FastAPI, Request

    from uvd_x402_sdk.integrations.fastapi_integration import FastAPIX402

    app = FastAPI()
    requirement = FastAPIX402(app, config=_config(rail), binding_store=store).require_payment(PRICE)
    site = Site()
    for path in PATHS:

        @app.api_route(path, methods=["GET", "POST"])
        async def paid(request: Request, payment: PaymentResult = Depends(requirement)):
            return site.record(payment, (await request.body()).decode())

    site._call = _asgi(app)
    return site


def fastapi_x402_depends(rail: Facilitator, store: Any) -> Site:
    pytest.importorskip("fastapi")
    from fastapi import Depends, FastAPI, Request

    from uvd_x402_sdk.integrations.fastapi_integration import X402Depends

    app = FastAPI()
    requirement = X402Depends(config=_config(rail), amount_usd=PRICE, binding_store=store)
    site = Site()
    for path in PATHS:

        @app.api_route(path, methods=["GET", "POST"])
        async def paid(request: Request, payment: PaymentResult = Depends(requirement)):
            return site.record(payment, (await request.body()).decode())

    site._call = _asgi(app)
    return site


def fastapi_decorator(rail: Facilitator, store: Any) -> Site:
    pytest.importorskip("fastapi")
    from fastapi import FastAPI, Request

    from uvd_x402_sdk.integrations.fastapi_integration import fastapi_require_payment

    app = FastAPI()
    site = Site()
    for path in PATHS:

        @app.api_route(path, methods=["GET", "POST"])
        @fastapi_require_payment(amount_usd=PRICE, config=_config(rail), binding_store=store)
        async def paid(request: Request):
            body = (await request.body()).decode()
            return site.record(request.state.payment_result, body)

    site._call = _asgi(app)
    return site


def fastapi_middleware(rail: Facilitator, store: Any) -> Site:
    pytest.importorskip("fastapi")
    from fastapi import FastAPI, Request

    from uvd_x402_sdk.integrations.fastapi_integration import X402Middleware

    app = FastAPI()
    site = Site()
    for path in PATHS:

        @app.api_route(path, methods=["GET", "POST"])
        async def paid(request: Request):
            body = (await request.body()).decode()
            return site.record(getattr(request.state, "payment_result", None), body)

    app.add_middleware(
        X402Middleware,
        config=_config(rail),
        protected_paths={path: PRICE for path in PROTECTED},
        binding_store=store,
    )
    site._call = _asgi(app)
    return site


def _event(payment: str, path: str, body: Optional[bytes]) -> dict[str, Any]:
    """An API Gateway HTTP API (payload 2.0) event."""
    event: dict[str, Any] = {
        "version": "2.0",
        "rawPath": path,
        "rawQueryString": "",
        "headers": {"x-payment": payment},
        "requestContext": {"http": {"method": "GET" if body is None else "POST", "path": path}},
    }
    if body is not None:
        event["body"] = base64.b64encode(body).decode()
        event["isBase64Encoded"] = True
    return event


def _lambda_answer(result: dict[str, Any]) -> Answer:
    return result["statusCode"], json.loads(result["body"]), _lower(result["headers"])


def lambda_process_or_require(rail: Facilitator, store: Any) -> Site:
    from uvd_x402_sdk.integrations.lambda_integration import LambdaX402

    x402 = LambdaX402(config=_config(rail), binding_store=store)
    site = Site()

    def call(payment: str, path: str, body: Optional[bytes], host: Optional[str]) -> Answer:
        result = x402.process_or_require(_event(payment, path, body), PRICE)
        if isinstance(result, PaymentResult):
            return 200, site.record(result, (body or b"").decode()), {}
        return _lambda_answer(result)

    site._call = call
    return site


def lambda_decorator(rail: Facilitator, store: Any) -> Site:
    from uvd_x402_sdk.integrations.lambda_integration import lambda_handler

    site = Site()

    @lambda_handler(amount_usd=PRICE, config=_config(rail), binding_store=store)
    def handler(event: Any, context: Any, payment_result: Any = None) -> dict[str, Any]:
        body = base64.b64decode(event.get("body") or "").decode()
        delivered = site.record(payment_result, body)
        return {"statusCode": 200, "headers": {}, "body": json.dumps(delivered)}

    def call(payment: str, path: str, body: Optional[bytes], host: Optional[str]) -> Answer:
        return _lambda_answer(handler(_event(payment, path, body), None))

    site._call = call
    return site


ENTRY_POINTS = [
    fastapi_dependency,
    fastapi_x402_depends,
    fastapi_decorator,
    fastapi_middleware,
    lambda_process_or_require,
    lambda_decorator,
]
entry_points = pytest.mark.parametrize("mount", ENTRY_POINTS, ids=lambda mount: mount.__name__)


def _key_not_in(key: str, body: dict[str, Any], headers: dict[str, str]) -> None:
    """The stored key is the seller's: it never reaches the buyer."""
    seen = json.dumps(body)
    for name in ("payment-response", "x-payment-response"):
        if name in headers:
            seen += base64.b64decode(headers[name]).decode()
    assert key not in seen


# -- the closing test -----------------------------------------------------------


@entry_points
@pytest.mark.parametrize("mode", ["receipts", "legacy"])
def test_the_lambda_dies_between_the_settle_and_the_put_and_the_resend_recovers_the_verdict(
    rails, stores, death, mount, mode
):
    rail = rails(mode)
    first = mount(rail, stores())
    death.arm(rail, "after")
    first.dies(x_payment())
    assert (first.delivered, rail.moved) == (0, 1), "charged and not delivered: the case"

    again = mount(rail, stores())  # another instance, the same database
    status, body, headers = again.get(x_payment())

    assert (status, again.delivered) == (200, 1), body
    assert [result.idempotent_replayed for result in again.results] == [True]
    assert rail.moved == 1 and rail.executed == 1, "charged twice"
    keys = rail.keys("/verify") + rail.keys("/settle")
    assert len(keys) == 4, keys
    assert len(set(keys)) == 1, "the resend did not carry the key that admitted it"
    _key_not_in(keys[0], body, headers)


@entry_points
@pytest.mark.parametrize("mode", ["receipts", "legacy"])
@pytest.mark.parametrize("keeper", ["no store, as in 0.91.0", "a store that does not persist"])
def test_without_the_persisted_key_the_same_death_loses_the_payment(
    rails, death, mount, mode, keeper
):
    """The contrast, and the mutation: the recovery above comes from the stored key."""
    make = (lambda: None) if keeper.startswith("no store") else Forgetful
    rail = rails(mode)
    first = mount(rail, make())
    death.arm(rail, "after")
    first.dies(x_payment())

    again = mount(rail, make())
    status, body, _ = again.get(x_payment())

    assert status == (409 if mode == "receipts" else 500), body
    assert again.delivered == 0 and rail.moved == 1


@entry_points
def test_the_lambda_dies_with_the_settle_in_flight_and_the_resend_recovers_it_once_confirmed(
    rails, death, mount
):
    """``202 settlement_in_progress`` under the stored key: 503 + ``Retry-After``
    and the same ``X-PAYMENT`` later, which is delivered once it confirms."""
    rail = rails("receipts", hold_before_confirm=1.0)
    db = FakePostgres()
    first = mount(rail, PostgresBindingStore(db))
    death.arm(rail, "in-flight")
    first.dies(x_payment())

    again = mount(rail, PostgresBindingStore(db))
    status, body, headers = again.get(x_payment())
    assert status == 503, body
    assert _reason(body) == "settlement_in_progress"
    assert int(headers["retry-after"]) > 0
    assert again.delivered == 0

    deadline = time.monotonic() + 5
    while rail.moved == 0:
        assert time.monotonic() < deadline, "the settle never confirmed"
        time.sleep(0.02)
    status, body, _ = again.get(x_payment())
    assert (status, again.delivered) == (200, 1), body
    assert rail.moved == 1 and rail.executed == 1


@entry_points
def test_where_verify_refuses_the_used_authorization_the_resend_is_409_never_402(
    rails, stores, death, mount
):
    """A network without receipts whose ``/verify`` simulates the transfer (EVM):
    the resend never reaches the settle cache, so it cannot be recovered, but it
    was presented here before and may have moved. 409, never the 402 that asks
    for a second payment."""
    rail = rails("legacy", verify_sees_used=True)
    first = mount(rail, stores())
    death.arm(rail, "after")
    first.dies(x_payment())

    again = mount(rail, stores())
    status, body, _ = again.get(x_payment())

    assert status == 409, body
    assert _reason(body) == PAYMENT_PRESENTED_BEFORE
    assert "do not sign another" in _inner(body)["message"]
    assert "accepts" not in json.dumps(body)
    assert again.delivered == 0 and rail.moved == 1


@entry_points
@pytest.mark.parametrize("keeper", ["no store, as in 0.91.0", "a store whose every binding is new"])
def test_where_verify_refuses_it_without_knowing_it_was_presented_it_is_402(
    rails, death, mount, keeper
):
    """The contrast: 0.91.0 answers that resend with a 402, and so does a store
    that loses :attr:`Binding.new`. The 409 above comes from that flag."""
    shared = None if keeper.startswith("no store") else AlwaysNew()
    rail = rails("legacy", verify_sees_used=True)
    first = mount(rail, shared)
    death.arm(rail, "after")
    first.dies(x_payment())

    status, body, _ = mount(rail, shared).get(x_payment())

    assert status == 402, body


# -- the rest of the rule -------------------------------------------------------


@entry_points
def test_a_store_that_cannot_bind_fails_closed_and_the_facilitator_is_not_called(rails, mount):
    """(c) Never "go on without a key": a charge whose key was not stored is one
    the resend cannot recover."""
    rail = rails()
    site = mount(rail, PostgresBindingStore(BrokenPool()))

    status, body, headers = site.get(x_payment())

    assert status == 503, body
    assert _reason(body) == PAYMENT_STORE_UNAVAILABLE
    assert headers["retry-after"] == "5" and _inner(body)["retryable"] is True
    assert "do not sign another" in _inner(body)["message"]
    assert rail.calls == [] and site.delivered == 0


@entry_points
def test_the_same_x_payment_for_another_resource_is_409_and_the_facilitator_is_not_called(
    rails, stores, mount
):
    """(d) Against a facilitator that replays to any resend (x402-rs 2.36 to
    2.38): "one payment buys one resource" cannot depend on it."""
    rail = rails("receipts-2.38")
    site = mount(rail, stores())
    assert site.get(x_payment())[0] == 200
    calls = len(rail.calls)

    status, body, _ = site.get(x_payment(), path="/other")

    assert status == 409, body
    assert _reason(body) == PAYMENT_ALREADY_USED
    assert len(rail.calls) == calls, "the other resource reached the facilitator"
    assert site.delivered == 1 and rail.moved == 1


@entry_points
def test_without_one_owner_per_payment_only_the_fresh_key_guard_is_left(
    rails, mount, monkeypatch
):
    """The mutations of the test above. With a binding per (payment, resource)
    the other resource is a first presentation that reaches the facilitator,
    and what refuses the replay there is the guard of its fresh key. Remove
    that guard too and the same facilitator gives the other resource away."""
    rail = rails("receipts-2.38")
    site = mount(rail, PerResource())
    assert site.get(x_payment())[0] == 200
    calls = len(rail.calls)

    status, body, _ = site.get(x_payment(), path="/other")
    assert status == 409 and _reason(body) == "authorization_already_settled", body
    assert len(rail.calls) > calls, "without an owner, the facilitator decides"

    monkeypatch.setattr(client_module._Binding, "refuse_foreign_replay", lambda *args: None)
    assert site.get(x_payment(), path="/other")[0] == 200
    assert site.delivered == 2 and rail.moved == 1


@entry_points
def test_with_a_store_the_same_x_payment_again_is_the_same_purchase_not_a_second_charge(
    rails, stores, mount
):
    """Within the window, the same ``X-PAYMENT`` for the same resource is the
    buyer's own purchase again (the bearer window of the module docstring):
    the facilitator's replay, never a second settle."""
    rail = rails()
    site = mount(rail, stores())

    assert site.get(x_payment())[0] == 200
    assert site.get(x_payment())[0] == 200

    assert [result.idempotent_replayed for result in site.results] == [False, True]
    assert rail.moved == 1 and rail.executed == 1


@entry_points
def test_the_body_is_part_of_what_is_bought(rails, mount):
    """The same ``X-PAYMENT`` with another body is another purchase (409); with
    the same body it is the same one. The handler still reads the body."""
    rail = rails("receipts-2.38")
    site = mount(rail, InMemoryBindingStore())

    assert site.get(x_payment(), body=b'{"pixel": 1}')[0] == 200
    status, body, _ = site.get(x_payment(), body=b'{"pixel": 2}')
    assert status == 409 and _reason(body) == PAYMENT_ALREADY_USED, body
    assert site.get(x_payment(), body=b'{"pixel": 1}')[0] == 200

    assert site.bodies == ['{"pixel": 1}', '{"pixel": 1}']
    assert rail.moved == 1


@entry_points
@pytest.mark.parametrize("mode", ["receipts", "receipts-2.38", "legacy"])
@pytest.mark.parametrize("path", ["/gen/cats%23other", "/gen/cats%23", "/gen/cats%3Fx=1"])
def test_an_encoded_hash_or_question_mark_in_the_path_is_another_resource(rails, mount, mode, path):
    """Starlette rebuilds ``request.url`` from the DECODED path, where a ``%23``
    becomes a ``#`` that cuts it: ``/gen/cats%23other`` read as ``/gen/cats``,
    and the route ran for the topic ``cats#other`` on the replay of the first
    purchase. The resource comes from what the app routes on instead."""
    rail = rails(mode)
    site = mount(rail, InMemoryBindingStore())
    assert site.get(x_payment(), path="/gen/cats")[0] == 200

    status, body, _ = site.get(x_payment(), path=path)

    assert status == 409 and _reason(body) == PAYMENT_ALREADY_USED, body
    assert site.delivered == 1 and rail.moved == 1


@pytest.mark.parametrize("mount", ENTRY_POINTS[:3], ids=lambda mount: mount.__name__)
def test_the_host_header_does_not_choose_the_resource(rails, mount):
    """Some Starlette releases build ``request.url`` out of the ``Host`` header:
    ``Host: testserver/gen/cats#`` made ``/gen/dogs`` read as ``/gen/cats``.
    (The middleware charges only its ``protected_paths``, and ``/gen/dogs`` is
    not one of them, so it is not in this test.)"""
    rail = rails("receipts-2.38")
    site = mount(rail, InMemoryBindingStore())
    assert site.get(x_payment(), path="/gen/cats")[0] == 200

    status, body, _ = site.get(x_payment(), path="/gen/dogs", host="testserver/gen/cats#")

    assert status in (402, 409) and site.delivered == 1, body
    assert rail.moved == 1


def _reencoded(header: str) -> str:
    """The same signed authorization, other bytes: JSON with other whitespace."""
    same = json.loads(base64.b64decode(header))
    return base64.b64encode(json.dumps(same, indent=1).encode()).decode()


@entry_points
@pytest.mark.parametrize("mode", ["receipts", "receipts-2.38"])
@pytest.mark.parametrize(
    "variant", [_reencoded, lambda header: header + "!"], ids=["json-whitespace", "junk-char"]
)
def test_the_same_authorization_re_encoded_for_another_resource_is_not_delivered(
    rails, stores, mount, mode, variant
):
    """Another byte string is another row and a FIRST presentation, so the
    owner check cannot see it. What refuses it is the fresh key's guard, which
    a first presentation keeps: against x402-rs 2.36-2.38, which replays to any
    resend of the same terms, the store used to switch it off (the replay was
    delivered to ``/other``). 2.39.0 refuses the resend itself."""
    rail = rails(mode)
    site = mount(rail, stores())
    assert site.get(x_payment())[0] == 200

    status, body, _ = site.get(variant(x_payment()), path="/other")

    assert status == 409, body
    assert _reason(body) == "authorization_already_settled"
    assert site.delivered == 1 and rail.moved == 1


@entry_points
def test_a_header_that_is_not_a_payment_never_reaches_the_store(rails, mount):
    """Parsed before ``bind``: garbage gets the answer it always got and writes
    no row that nothing would ever remove."""
    seen = []

    class Counting(InMemoryBindingStore):
        def bind(self, payment_key: str, resource: str, ttl_seconds: float) -> Optional[Binding]:
            seen.append(payment_key)
            return super().bind(payment_key, resource, ttl_seconds)

    rail = rails()
    site = mount(rail, Counting())

    status, _, _ = site.get("not-a-payment")

    assert status == 402 and seen == [] and rail.calls == []


# -- compatibility: no store, no change -----------------------------------------


@entry_points
def test_without_a_store_process_payment_is_called_exactly_as_in_0_91_0(rails, mount, monkeypatch):
    """No binding code runs and the client gets the same call as before: no
    ``idempotency_key``, so each request keeps its own fresh key."""
    from uvd_x402_sdk.integrations import fastapi_integration, lambda_integration

    def must_not_run(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("binding code ran without a store")

    for module in (fastapi_integration, lambda_integration):
        monkeypatch.setattr(module, "process_payment_bound", must_not_run)
        monkeypatch.setattr(module, "purchase_resource", must_not_run)
    calls = []
    real = X402Client.process_payment

    def spy(self: X402Client, *args: Any, **kwargs: Any) -> PaymentResult:
        calls.append((args, sorted(kwargs)))
        return real(self, *args, **kwargs)

    monkeypatch.setattr(X402Client, "process_payment", spy)
    rail = rails()
    site = mount(rail, None)

    assert site.get(x_payment())[0] == 200
    expected = ["expected_amount_usd", "x_payment_header"]
    if mount.__name__.startswith("fastapi"):
        expected = ["expected_amount_usd", "receipt_context", "x_payment_header"]
    assert calls == [((), expected)]


def test_a_store_on_a_client_that_sends_no_key_is_refused_when_the_integration_is_built():
    pytest.importorskip("fastapi")
    from uvd_x402_sdk.integrations.fastapi_integration import (
        FastAPIX402,
        X402Depends,
        X402Middleware,
        fastapi_require_payment,
    )
    from uvd_x402_sdk.integrations.lambda_integration import LambdaX402, lambda_handler

    config = X402Config(recipient_evm=RECIPIENT, send_idempotency_key=False)
    store = InMemoryBindingStore()
    builds = [
        lambda store: FastAPIX402(config=config, binding_store=store),
        lambda store: X402Depends(config=config, amount_usd=PRICE, binding_store=store),
        lambda store: fastapi_require_payment(PRICE, config, binding_store=store),
        lambda store: X402Middleware(None, config, {}, binding_store=store),
        lambda store: LambdaX402(config=config, binding_store=store),
        lambda store: lambda_handler(amount_usd=PRICE, config=config, binding_store=store),
    ]
    for build in builds:
        with pytest.raises(ValueError, match="send_idempotency_key"):
            build(store)
        build(None)  # without a store, the same config builds as before


@pytest.mark.parametrize(
    "version, refused", [("0.27.0", True), ("0.27.1", True), ("0.28.0", False), ("1.7.0", False)]
)
def test_the_middleware_refuses_a_store_where_the_route_cannot_read_the_body(
    monkeypatch, version, refused
):
    """Measured on Starlette 0.27.0: the middleware reads the body for the
    resource, and the route then waits for ever for it, AFTER the payment
    settled. Refused when the middleware is built, before anything is charged;
    0.28.0 hands the body on. Without a store, nothing changes."""
    pytest.importorskip("fastapi")
    import starlette

    from uvd_x402_sdk.integrations.fastapi_integration import X402Middleware

    monkeypatch.setattr(starlette, "__version__", version)
    config = X402Config(recipient_evm=RECIPIENT)
    X402Middleware(None, config, {})
    if refused:
        with pytest.raises(ValueError, match="Starlette 0.28"):
            X402Middleware(None, config, {}, binding_store=InMemoryBindingStore())
    else:
        X402Middleware(None, config, {}, binding_store=InMemoryBindingStore())


# -- process_payment_bound, one branch at a time --------------------------------


#: A header that parses: the store only ever sees payments.
PAY = x_payment("0x0a")


class StubClient(X402Client):
    """The real client up to the facilitator: its verify-and-settle raises the
    given failures in order, then succeeds, and records the binding it got."""

    def __init__(self, *failures: Exception) -> None:
        super().__init__(config=X402Config(recipient_evm=RECIPIENT))
        self.failures = list(failures)
        self.calls: list[dict[str, Any]] = []

    def _handle_payment(self, payload: Any, expected_amount_usd: Any, pay_to: Any, *,
                        binding: Any, **_: Any) -> PaymentResult:
        self.calls.append({
            "idempotency_key": binding.key,
            "receipt_context": binding.receipt_context,
            "brought": binding.brought,
        })
        if self.failures:
            raise self.failures.pop(0)
        return PaymentResult(
            success=True, payer_address="0xSender", network="arc", amount_usd=PRICE
        )


def _opaque_verify_refusal() -> FacilitatorError:
    return FacilitatorError(
        "Facilitator verify failed with status 400",
        status_code=400,
        response_body=json.dumps({"error": "contract_call_failed (ref: x)"}),
        operation="verify",
    )


def test_the_key_the_client_carries_is_the_stored_one_and_only_a_resend_owns_its_replay():
    store = InMemoryBindingStore()
    client = StubClient()
    process_payment_bound(client, store, PAY, PRICE, "GET /paid", receipt_context="ctx")
    process_payment_bound(client, store, PAY, PRICE, "GET /paid")
    first, second = client.calls
    assert first["idempotency_key"] == second["idempotency_key"]
    stored = store.bind(payment_key(PAY), "GET /paid", 1)
    assert first["idempotency_key"] == stored.idempotency_key
    assert first["receipt_context"] == "ctx" and second["receipt_context"] is None
    # The first presentation keeps the fresh key's guard; the resend's key is an
    # earlier handling's, which may have admitted the payment.
    assert (first["brought"], second["brought"]) == (False, True)


def test_the_ttl_is_the_derived_window_unless_given():
    seen = []

    class Spy:
        def bind(self, payment_key: str, resource: str, ttl_seconds: float) -> Optional[Binding]:
            seen.append(ttl_seconds)
            return Binding(resource, new_idempotency_key(), new=True)

    client = StubClient()
    process_payment_bound(client, Spy(), PAY, PRICE,"GET /paid")
    process_payment_bound(client, Spy(), PAY, PRICE,"GET /paid", ttl_seconds=42)
    assert seen == [binding_window_seconds(client.config), 42]


def test_a_rejection_of_a_first_presentation_is_raised_unchanged():
    """What the guard must NOT touch: a first presentation that the facilitator
    refuses is the rejection it always was (402), with nothing of ours moved."""
    refusal = _opaque_verify_refusal()
    with pytest.raises(FacilitatorError) as info:
        process_payment_bound(StubClient(refusal), InMemoryBindingStore(), PAY, PRICE,"GET /paid")
    assert info.value is refusal
    assert _undelivered_response(info.value) is None


def test_a_rejection_of_a_payment_presented_before_is_409_with_its_cause():
    refusal = _opaque_verify_refusal()
    store = InMemoryBindingStore()
    client = StubClient(X402TimeoutError(operation="settle", timeout_seconds=1), refusal)
    with pytest.raises(X402TimeoutError):
        process_payment_bound(client, store, PAY, PRICE,"GET /paid")

    with pytest.raises(PaymentBindingError) as info:
        process_payment_bound(client, store, PAY, PRICE,"GET /paid")

    error = info.value
    assert error.reason == PAYMENT_PRESENTED_BEFORE
    assert error.cause is refusal and error.__cause__ is refusal
    status, body, headers = _undelivered_response(error)
    assert status == 409 and body["reason"] == PAYMENT_PRESENTED_BEFORE
    assert body["retryable"] is False and body["safeToReplay"] is False
    assert body["details"]["cause"]["message"] == refusal.message
    assert "payment-response" not in _lower(headers)
    assert not is_transient_error(error)


def test_a_presented_before_refusal_carries_the_facilitators_receipt():
    receipt = parse_receipt(_receipt("confirmed", "0xf00d1", None))
    refusal = PaymentVerificationError("refused", reason="invalid_signature", receipt=receipt)
    store = InMemoryBindingStore()
    process_payment_bound(StubClient(), store, PAY, PRICE,"GET /paid")

    with pytest.raises(PaymentBindingError) as info:
        process_payment_bound(StubClient(refusal), store, PAY, PRICE,"GET /paid")

    _, _, headers = _undelivered_response(info.value)
    assert "PAYMENT-RESPONSE" in headers


def test_a_failure_that_is_already_not_a_402_is_raised_unchanged_on_a_resend():
    store = InMemoryBindingStore()
    process_payment_bound(StubClient(), store, PAY, PRICE,"GET /paid")
    in_flight = X402TimeoutError(operation="settle", timeout_seconds=1)
    with pytest.raises(X402TimeoutError) as info:
        process_payment_bound(StubClient(in_flight), store, PAY, PRICE,"GET /paid")
    assert info.value is in_flight


def test_a_store_that_raises_fails_closed_without_calling_the_client():
    class Raising:
        def bind(self, payment_key: str, resource: str, ttl_seconds: float) -> Optional[Binding]:
            raise RuntimeError("a store that breaks the protocol")

    client = StubClient()
    with pytest.raises(PaymentBindingError) as info:
        process_payment_bound(client, Raising(), PAY, PRICE,"GET /paid")
    assert info.value.reason == PAYMENT_STORE_UNAVAILABLE and client.calls == []
    assert is_transient_error(info.value)
    status, body, headers = _undelivered_response(info.value)
    assert (status, headers["Retry-After"], body["retryAfter"]) == (503, "5", 5)
    assert body["retryable"] is True and body["safeToReplay"] is False


def test_another_resource_is_refused_before_the_client_is_called():
    store = InMemoryBindingStore()
    client = StubClient()
    process_payment_bound(client, store, PAY, PRICE,"GET /paid")
    with pytest.raises(PaymentBindingError) as info:
        process_payment_bound(client, store, PAY, PRICE,"GET /other")
    assert info.value.reason == PAYMENT_ALREADY_USED and len(client.calls) == 1
    assert not is_transient_error(info.value)
    assert _undelivered_response(info.value)[0] == 409


def test_the_lambda_resource_reads_both_api_gateway_payloads():
    from uvd_x402_sdk.integrations.lambda_integration import _event_resource

    v2 = {
        "rawPath": "/paid",
        "rawQueryString": "b=2&a=1",
        "requestContext": {"http": {"method": "GET"}},
    }
    assert _event_resource(v2) == "GET /paid?b=2&a=1"
    v1 = {"httpMethod": "GET", "path": "/paid", "queryStringParameters": {"b": "2", "a": "1"}}
    assert _event_resource(v1) == "GET /paid?a=1&b=2"
    v1_multi = {
        "httpMethod": "GET",
        "path": "/paid",
        "queryStringParameters": {"a": "3"},
        "multiValueQueryStringParameters": {"b": ["9"], "a": ["1", "3"]},
    }
    assert _event_resource(v1_multi) == "GET /paid?a=1&a=3&b=9"
    # The handler of ``?id=1&id=2`` sees 2 and that of ``?id=2&id=1`` sees 1:
    # two purchases, never one resource.
    swapped = {**v1_multi, "multiValueQueryStringParameters": {"b": ["9"], "a": ["3", "1"]}}
    assert _event_resource(swapped) == "GET /paid?a=3&a=1&b=9"
    encoded = {
        "httpMethod": "POST",
        "path": "/buy",
        "isBase64Encoded": True,
        "body": base64.b64encode(b"x").decode(),
    }
    assert _event_resource(encoded) == purchase_resource("POST", "/buy", "", b"x")
    plain = {**encoded, "isBase64Encoded": False, "body": "x"}
    assert _event_resource(plain) == _event_resource(encoded)


def test_an_odd_lambda_body_is_a_resource_and_not_a_crash():
    """A direct invocation with a dict body, or a body that is not base64: the
    function answers instead of crashing, and each is its own resource."""
    from uvd_x402_sdk.integrations.lambda_integration import _event_resource

    base = {"httpMethod": "POST", "path": "/buy"}
    as_dict = _event_resource({**base, "body": {"pixel": 1}})
    assert as_dict == purchase_resource("POST", "/buy", "", b'{"pixel":1}')
    broken = _event_resource({**base, "isBase64Encoded": True, "body": "abc"})
    assert broken == purchase_resource("POST", "/buy", "", b"abc")
    assert len({as_dict, broken, _event_resource(base)}) == 3


def test_a_header_that_does_not_parse_is_refused_before_the_store():
    from uvd_x402_sdk.exceptions import InvalidPayloadError

    seen = []

    class Spy:
        def bind(self, payment_key: str, resource: str, ttl_seconds: float) -> Optional[Binding]:
            seen.append(payment_key)
            return None

    client = StubClient()
    with pytest.raises(InvalidPayloadError):
        process_payment_bound(client, Spy(), "not-a-payment", PRICE, "GET /paid")
    assert seen == [] and client.calls == []
