"""Every middleware and decorator of the SDK, and an X-PAYMENT that was already used.

Each entry point runs in its own framework against ``tests/receipt_rail.py``
(a real socket). What is pinned, per entry point:

* a first payment is delivered;
* the same X-PAYMENT in a NEW request is not delivered again, whether the
  facilitator refuses it (2.39.0: ``authorization_already_settled``) or
  answers with the original settle and ``Idempotent-Replayed: true`` (2.36.0
  to 2.38.0 did not tie the replay to the binding): 409, never 402;
* while the first payment is still in flight: 503 + ``Retry-After``, never 402;
* a settle that outlives its timeout: awaited within the same handling and
  delivered once, or 503 + ``Retry-After`` past the budget, never 402;
* a store the facilitator cannot read: 503 + ``Retry-After``, and the same
  X-PAYMENT later is delivered once;
* without the guard in the settle handling, the replayed settle IS delivered
  (the mutation that proves the test above discriminates);
* on a network without receipts, the answer each entry point gave before.

With the buyer's ``X-UVD-Purchase``, which only the FastAPI integration
forwards, a resumed purchase is delivered: the facilitator matched the
capability.

No ``from __future__ import annotations`` here: FastAPI reads the endpoints'
annotations at runtime, and the framework types are imported inside each
factory.
"""
import asyncio
import base64
import json
import threading
import time
from decimal import Decimal
from typing import Any, Callable, Dict, Optional, Tuple

import httpx
import pytest

import uvd_x402_sdk.client as client_module
from tests.receipt_rail import RECIPIENT, Facilitator, x_payment
from uvd_x402_sdk import X402Client, X402Config
from uvd_x402_sdk.models import PaymentResult
from uvd_x402_sdk.receipts import PurchaseContext

PRICE = Decimal("0.01")
URL = "http://testserver/paid"

#: (status, JSON body, headers with lowercase names)
Answer = Tuple[int, Dict[str, Any], Dict[str, str]]


class Site:
    """One entry point, mounted on a fresh app for one facilitator."""

    def __init__(self, call: Callable[[Dict[str, str]], Answer]) -> None:
        self._call = call
        self.delivered = 0

    def get(self, payment: str, **headers: str) -> Answer:
        return self._call({"X-PAYMENT": payment, **headers})


def _config(rail: Facilitator) -> X402Config:
    return X402Config(facilitator_url=rail.url, recipient_evm=RECIPIENT)


def _lower(headers: Any) -> Dict[str, str]:
    return {str(k).lower(): v for k, v in dict(headers).items()}


def _reason(body: Dict[str, Any]) -> Optional[str]:
    inner = body.get("detail", body)
    return inner.get("reason") if isinstance(inner, dict) else None


# -- FastAPI ------------------------------------------------------------------


def _asgi(app: Any) -> Callable[[Dict[str, str]], Answer]:
    def call(headers: Dict[str, str]) -> Answer:
        async def main() -> httpx.Response:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
                return await c.get("/paid", headers=headers)

        response = asyncio.run(main())
        return response.status_code, response.json(), _lower(response.headers)

    return call


def fastapi_dependency(rail: Facilitator) -> Site:
    pytest.importorskip("fastapi")
    from fastapi import Depends, FastAPI

    from uvd_x402_sdk.integrations.fastapi_integration import FastAPIX402

    app = FastAPI()
    integration = FastAPIX402(app, config=_config(rail))
    site = Site(_asgi(app))

    @app.get("/paid")
    async def paid(payment: PaymentResult = Depends(integration.require_payment(PRICE))):
        site.delivered += 1
        return {"delivered": True}

    return site


def fastapi_x402_depends(rail: Facilitator) -> Site:
    pytest.importorskip("fastapi")
    from fastapi import Depends, FastAPI

    from uvd_x402_sdk.integrations.fastapi_integration import X402Depends

    app = FastAPI()
    requirement = X402Depends(config=_config(rail), amount_usd=PRICE)
    site = Site(_asgi(app))

    @app.get("/paid")
    async def paid(payment: PaymentResult = Depends(requirement)):
        site.delivered += 1
        return {"delivered": True}

    return site


def fastapi_decorator(rail: Facilitator) -> Site:
    pytest.importorskip("fastapi")
    from fastapi import FastAPI, Request

    from uvd_x402_sdk.integrations.fastapi_integration import fastapi_require_payment

    app = FastAPI()
    site = Site(_asgi(app))

    @app.get("/paid")
    @fastapi_require_payment(amount_usd=PRICE, config=_config(rail))
    async def paid(request: Request):
        site.delivered += 1
        return {"delivered": True}

    return site


def fastapi_middleware(rail: Facilitator) -> Site:
    pytest.importorskip("fastapi")
    from fastapi import FastAPI

    from uvd_x402_sdk.integrations.fastapi_integration import X402Middleware

    app = FastAPI()
    site = Site(_asgi(app))

    @app.get("/paid")
    async def paid():
        site.delivered += 1
        return {"delivered": True}

    app.add_middleware(X402Middleware, config=_config(rail), protected_paths={"/paid": PRICE})
    return site


# -- Flask --------------------------------------------------------------------


def _wsgi(app: Any) -> Callable[[Dict[str, str]], Answer]:
    def call(headers: Dict[str, str]) -> Answer:
        response = app.test_client().get("/paid", headers=headers)
        return response.status_code, response.get_json(), _lower(response.headers)

    return call


def flask_extension(rail: Facilitator) -> Site:
    pytest.importorskip("flask")
    from flask import Flask

    from uvd_x402_sdk.integrations.flask_integration import FlaskX402

    app = Flask(__name__)
    x402 = FlaskX402(app, config=_config(rail))
    site = Site(_wsgi(app))

    @app.route("/paid")
    @x402.require_payment(amount_usd=PRICE)
    def paid():
        site.delivered += 1
        return {"delivered": True}

    return site


def flask_decorator(rail: Facilitator) -> Site:
    pytest.importorskip("flask")
    from flask import Flask

    from uvd_x402_sdk.integrations.flask_integration import FlaskX402, flask_require_payment

    app = Flask(__name__)
    FlaskX402(app, config=_config(rail))
    site = Site(_wsgi(app))

    @app.route("/paid")
    @flask_require_payment(amount_usd=PRICE)
    def paid():
        site.delivered += 1
        return {"delivered": True}

    return site


# -- Django -------------------------------------------------------------------


def _django() -> None:
    """Skip without Django; configure it once for the whole process."""
    django = pytest.importorskip("django")
    from django.conf import settings

    if not settings.configured:
        settings.configure(
            DEBUG=False, ALLOWED_HOSTS=["*"], INSTALLED_APPS=[], MIDDLEWARE=[],
            DEFAULT_CHARSET="utf-8",
        )
        django.setup()


def _django_call(handler: Callable[[Any], Any]) -> Callable[[Dict[str, str]], Answer]:
    _django()
    from django.test import RequestFactory

    def call(headers: Dict[str, str]) -> Answer:
        meta = {"HTTP_" + name.upper().replace("-", "_"): value for name, value in headers.items()}
        response = handler(RequestFactory().get("/paid", **meta))
        return response.status_code, json.loads(response.content), _lower(response.headers)

    return call


def django_middleware(rail: Facilitator) -> Site:
    _django()
    from django.http import JsonResponse
    from django.test import override_settings

    from uvd_x402_sdk.integrations.django_integration import DjangoX402Middleware

    site = Site(lambda headers: (0, {}, {}))

    def view(request: Any) -> Any:
        site.delivered += 1
        return JsonResponse({"delivered": True})

    with override_settings(
        X402_FACILITATOR_URL=rail.url,
        X402_RECIPIENT_EVM=RECIPIENT,
        X402_PROTECTED_PATHS={"/paid": str(PRICE)},
    ):
        middleware = DjangoX402Middleware(view)
    site._call = _django_call(middleware)
    return site


def django_decorator(rail: Facilitator) -> Site:
    _django()
    from django.http import JsonResponse

    from uvd_x402_sdk.integrations.django_integration import django_require_payment

    site = Site(lambda headers: (0, {}, {}))

    @django_require_payment(amount_usd=PRICE, config=_config(rail))
    def view(request: Any) -> Any:
        site.delivered += 1
        return JsonResponse({"delivered": True})

    site._call = _django_call(view)
    return site


def django_view(rail: Facilitator) -> Site:
    _django()
    from django.http import JsonResponse
    from django.views import View

    from uvd_x402_sdk.integrations.django_integration import X402PaymentView

    site = Site(lambda headers: (0, {}, {}))

    class Paid(X402PaymentView, View):
        x402_amount = PRICE
        x402_config = _config(rail)

        def get(self, request: Any) -> Any:
            site.delivered += 1
            return JsonResponse({"delivered": True})

    site._call = _django_call(Paid.as_view())
    return site


# -- AWS Lambda ---------------------------------------------------------------


def _lambda_answer(result: Any) -> Answer:
    return result["statusCode"], json.loads(result["body"]), _lower(result["headers"])


def lambda_process_or_require(rail: Facilitator) -> Site:
    from uvd_x402_sdk.integrations.lambda_integration import LambdaX402

    x402 = LambdaX402(config=_config(rail))
    site = Site(lambda headers: (0, {}, {}))

    def call(headers: Dict[str, str]) -> Answer:
        result = x402.process_or_require({"headers": headers}, PRICE)
        if isinstance(result, PaymentResult):
            site.delivered += 1
            return 200, {"delivered": True}, {}
        return _lambda_answer(result)

    site._call = call
    return site


def lambda_decorator(rail: Facilitator) -> Site:
    from uvd_x402_sdk.integrations.lambda_integration import lambda_handler

    site = Site(lambda headers: (0, {}, {}))

    @lambda_handler(amount_usd=PRICE, config=_config(rail))
    def handler(event: Any, context: Any, payment_result: Any = None) -> Dict[str, Any]:
        site.delivered += 1
        return {"statusCode": 200, "headers": {}, "body": json.dumps({"delivered": True})}

    site._call = lambda headers: _lambda_answer(handler({"headers": headers}, None))
    return site


# -- The generic decorator (uvd_x402_sdk.decorators), on Flask ----------------


def generic_decorator(rail: Facilitator) -> Site:
    pytest.importorskip("flask")
    from flask import Flask

    from uvd_x402_sdk.decorators import configure_x402, require_payment

    configure_x402(config=_config(rail))
    app = Flask(__name__)
    site = Site(_wsgi(app))

    @app.route("/paid")
    @require_payment(amount_usd=PRICE)
    def paid(payment_result: Any = None):
        site.delivered += 1
        return {"delivered": True}

    return site


#: entry point -> the status it answered, before and after this change, to a
#: bare resend on a network without receipts (an opaque 400 from the chain).
SITES = {
    fastapi_dependency: 402,
    fastapi_x402_depends: 402,
    fastapi_decorator: 402,
    fastapi_middleware: 402,
    flask_extension: 402,
    flask_decorator: 402,
    django_middleware: 402,
    django_decorator: 402,
    django_view: 402,
    lambda_process_or_require: 402,
    lambda_decorator: 402,
    generic_decorator: 400,
}
FASTAPI = [fastapi_dependency, fastapi_x402_depends, fastapi_decorator, fastapi_middleware]


@pytest.fixture
def rails():
    opened = []

    def open_rail(mode: str = "receipts", **holds) -> Facilitator:
        rail = Facilitator(mode, **holds)
        opened.append(rail)
        return rail

    yield open_rail
    for rail in opened:
        rail.close()


all_sites = pytest.mark.parametrize("mount", list(SITES), ids=lambda mount: mount.__name__)


@all_sites
@pytest.mark.parametrize("mode", ["receipts", "receipts-2.38", "legacy"])
def test_a_first_payment_is_delivered(rails, mount, mode):
    site = mount(rails(mode))
    status, body, _ = site.get(x_payment())

    assert (status, site.delivered) == (200, 1), body


@all_sites
@pytest.mark.parametrize("mode", ["receipts", "receipts-2.38"])
def test_the_same_x_payment_in_a_new_request_is_not_delivered_again(rails, mount, mode):
    """``receipts``: the rail refuses the new request's /verify. ``receipts-2.38``:
    the rail hands it the original settle, 200 with Idempotent-Replayed, and
    the settle handling refuses it: a fresh key, no X-UVD-Purchase, and no
    attempt of its own that could have admitted the payment."""
    rail = rails(mode)
    site = mount(rail)
    site.get(x_payment())

    status, body, headers = site.get(x_payment())

    assert status == 409, body
    assert _reason(body) == "authorization_already_settled"
    assert site.delivered == 1 and rail.executed == 1
    assert "payment-response" in headers  # the receipt, for whoever holds the payment


@all_sites
def test_without_the_guard_the_replayed_settle_would_be_delivered(rails, mount, monkeypatch):
    """The mutation: the test above is red without the guard in the handling."""
    rail = rails("receipts-2.38")
    site = mount(rail)
    site.get(x_payment())
    monkeypatch.setattr(client_module._Binding, "refuse_foreign_replay", lambda *args: None)

    status, _, _ = site.get(x_payment())

    assert (status, site.delivered) == (200, 2)


@all_sites
def test_while_the_first_payment_is_in_flight_the_answer_is_503_with_retry_after(rails, mount):
    rail = rails(hold_before_confirm=1.0)
    site = mount(rail)
    worker = threading.Thread(
        target=lambda: X402Client(recipient_address=RECIPIENT, facilitator_url=rail.url)
        .settle_payment(X402Client(recipient_address=RECIPIENT).extract_payload(x_payment()), PRICE)
    )
    worker.start()
    time.sleep(0.2)
    try:
        status, body, headers = site.get(x_payment())
    finally:
        worker.join()

    assert status == 503, body
    assert _reason(body) == "authorization_in_flight"
    assert int(headers["retry-after"]) > 0
    assert site.delivered == 0 and rail.executed == 1


@all_sites
def test_legacy_a_bare_resend_keeps_the_answer_it_had(rails, mount):
    rail = rails("legacy")
    site = mount(rail)
    site.get(x_payment())

    status, body, _ = site.get(x_payment())

    assert status == SITES[mount], body
    assert site.delivered == 1


@pytest.fixture
def slow_settle(monkeypatch):
    """Every client times its settle out at 0.3s and pauses 0.1s between the
    fallback's asks: the settle below takes longer than that."""
    monkeypatch.setattr(X402Client, "_get_settle_timeout", lambda self, network: 0.3)
    monkeypatch.setattr(client_module, "_IN_FLIGHT_POLL_MAX_INTERVAL_SECONDS", 0.1)


@all_sites
def test_a_settle_that_outlives_its_timeout_is_awaited_and_delivered_once(
    rails, mount, slow_settle
):
    """The settle times out while the payment is in flight. The fallback's
    resend, under the same key, gets this handling's own
    ``202 settlement_in_progress``; it asks again within the budget, and the
    same request is delivered once. (0.89.0's first cut read the 202 as a
    timeout and every entry point answered 402 over a payment that moved.)"""
    rail = rails(hold_before_confirm=1.0)
    site = mount(rail)

    status, body, _ = site.get(x_payment())

    assert (status, site.delivered) == (200, 1), body
    assert rail.executed == 1 and rail.moved == 1


@all_sites
def test_a_settle_still_in_flight_past_the_budget_is_503_with_retry_after(
    rails, mount, slow_settle, monkeypatch
):
    monkeypatch.setattr(client_module, "SETTLE_IN_FLIGHT_POLL_SECONDS", 0.3)
    rail = rails(hold_before_confirm=1.5)
    site = mount(rail)

    status, body, headers = site.get(x_payment())

    assert status == 503, body
    assert _reason(body) == "settlement_in_progress"
    assert int(headers["retry-after"]) > 0
    assert "payment-response" in headers  # the pending receipt
    assert site.delivered == 0 and rail.executed == 1


@pytest.mark.parametrize("mount", FASTAPI, ids=lambda mount: mount.__name__)
def test_a_settle_still_in_flight_resumed_with_x_uvd_purchase_is_delivered_once(
    rails, mount, slow_settle, monkeypatch
):
    """Past the budget the buyer gets 503; resending the same request with its
    X-UVD-Purchase after the settle confirms is the same purchase, and it is
    delivered once."""
    monkeypatch.setattr(client_module, "SETTLE_IN_FLIGHT_POLL_SECONDS", 0.3)
    rail = rails(hold_before_confirm=1.5)
    site = mount(rail)
    context = PurchaseContext()
    context.bind(httpx.Request("GET", URL))

    first, _, _ = site.get(x_payment(), **{"X-UVD-Purchase": context.header()})
    time.sleep(1.5)  # the settle confirms
    again, body, _ = site.get(x_payment(), **{"X-UVD-Purchase": context.header()})

    assert (first, again) == (503, 200), body
    assert site.delivered == 1 and rail.executed == 1 and rail.moved == 1


@all_sites
@pytest.mark.parametrize("mode", ["receipts", "legacy"])
def test_a_store_the_facilitator_cannot_read_is_503_and_the_same_payment_is_delivered_later(
    rails, mount, mode
):
    """``503 receipt_store_unavailable`` / ``503 idempotency_store_unavailable``
    (the key is on by default): nothing moved and there is no verdict. The
    buyer presents the same X-PAYMENT again, never signs another one."""
    rail = rails(mode, store_down=True)
    site = mount(rail)

    status, body, headers = site.get(x_payment())
    assert status == 503, body
    assert int(headers["retry-after"]) > 0
    assert site.delivered == 0 and rail.moved == 0

    rail.store_down = False
    status, body, _ = site.get(x_payment())
    assert (status, site.delivered, rail.moved) == (200, 1, 1), body


@pytest.mark.parametrize("mount", FASTAPI, ids=lambda mount: mount.__name__)
@pytest.mark.parametrize("mode", ["receipts", "receipts-2.38"])
def test_with_the_buyers_x_uvd_purchase_a_resumed_purchase_is_delivered(rails, mount, mode):
    """The facilitator matched the buyer's capability: this is the buyer's own
    purchase, resumed after a lost response, and it gets its answer back."""
    rail = rails(mode)
    site = mount(rail)
    context = PurchaseContext()
    context.bind(httpx.Request("GET", URL))

    first, _, _ = site.get(x_payment(), **{"X-UVD-Purchase": context.header()})
    again, body, headers = site.get(x_payment(), **{"X-UVD-Purchase": context.header()})

    assert (first, again) == (200, 200), body
    assert site.delivered == 2 and rail.executed == 1
    if "payment-response" in headers:
        propagated = base64.b64decode(headers["payment-response"]).decode()
        assert json.loads(propagated)["idempotent_replayed"] is True
        assert all(key not in propagated for key in rail.keys("/settle"))


@pytest.mark.parametrize("mount", FASTAPI, ids=lambda mount: mount.__name__)
def test_without_the_x_uvd_purchase_a_payment_made_with_one_is_not_delivered(rails, mount):
    rail = rails()
    site = mount(rail)
    context = PurchaseContext()
    context.bind(httpx.Request("GET", URL))
    site.get(x_payment(), **{"X-UVD-Purchase": context.header()})

    status, body, _ = site.get(x_payment())

    assert status == 409, body
    assert site.delivered == 1
