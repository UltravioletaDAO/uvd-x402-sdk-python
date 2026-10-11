"""DX402 durable evidence in the FastAPI seller middleware (SDKPY-DURABLE).

The post-hook runs on the response the route returned, after the payment
settled, and follows the Rust middleware's contract
(x402-rs ``crates/x402-axum/src/durable.rs``): off by default, never a gate on
the sale, and no evidence for a payment that did not settle.

Offline: the facilitator is ``Scripted`` (127.0.0.1) and the anchor goes to an
``httpx.MockTransport``. The payer's public key is generated per test; no key
is read from anywhere.
"""

import asyncio
import base64
import contextlib
import contextvars
import json
import socket
import threading
import time
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Callable, Optional

import anyio
import httpx
import pytest

pytest.importorskip("fastapi")
pytest.importorskip("cryptography")

from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey  # noqa: E402
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat  # noqa: E402
from fastapi import FastAPI, Request, Response  # noqa: E402
from fastapi.responses import (  # noqa: E402
    FileResponse,
    JSONResponse,
    PlainTextResponse,
    StreamingResponse,
)
from pydantic import BaseModel  # noqa: E402
from starlette.background import BackgroundTask  # noqa: E402

from tests.receipt_rail import PAYER, RECIPIENT, x_payment
from tests.receipt_rail import _receipt as rail_receipt  # noqa: E402
from tests.test_integrations_undelivered import (  # noqa: E402
    MAY_HAVE_SETTLED,
    TX,
    Scripted,
)
from uvd_x402_sdk import X402Config  # noqa: E402
from uvd_x402_sdk.durable import DurableConfig, durable_evidence  # noqa: E402
from uvd_x402_sdk.dx402 import parse_evidence_header, payment_id  # noqa: E402
from uvd_x402_sdk.integrations import fastapi_integration  # noqa: E402
from uvd_x402_sdk.integrations.fastapi_integration import (  # noqa: E402
    X402Middleware,
    fastapi_require_payment,
)
from uvd_x402_sdk.models import PaymentResult  # noqa: E402
from uvd_x402_sdk.receipts import FacilitatorReceipt  # noqa: E402

PRICE = Decimal("0.01")
BODY = b'{"answer":"the paid content \xc3\xb1"}'
ANCHORED = {
    "paymentId": "0x" + "ee" * 32,
    "pointer": "s3://local/evidence/1",
    "backend": "s3",
    "contentHash": "0x" + "aa" * 32,
    "keyAlg": "ECIES-X25519",
    "mode": "direct",
    "retention": "90d",
    "verified": False,
}
FACILITATOR = "http://dx402.test"


@pytest.fixture
def scripted():
    opened = []

    def open_rail(**answers: Any) -> Scripted:
        rail = Scripted(**answers)
        opened.append(rail)
        return rail

    yield open_rail
    for rail in opened:
        rail.close()


def _decode(value: str) -> dict:
    return json.loads(base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)))


def _public_key() -> bytes:
    return X25519PrivateKey.generate().public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)


class Sink:
    """The anchor endpoint: counts the calls and answers as it is told."""

    def __init__(
        self,
        status: int = 200,
        answer: Optional[dict] = None,
        log: Optional[list] = None,
        echo: bool = False,
    ) -> None:
        self.requests: list = []
        self.status = status
        self.answer = ANCHORED if answer is None else answer
        self.log = log
        self.echo = echo
        self.client = httpx.Client(transport=httpx.MockTransport(self._handle))

    def _handle(self, request: httpx.Request) -> httpx.Response:
        sent = json.loads(request.content)
        self.requests.append((str(request.url), sent))
        if self.log is not None:
            self.log.append("anchor")
        answer = {**self.answer, "paymentId": sent["paymentId"]} if self.echo else self.answer
        return httpx.Response(self.status, json=answer)


def _durable(sink: Sink, key: Optional[bytes] = b"", **extra: Any) -> DurableConfig:
    resolved = _public_key() if key == b"" else key
    return DurableConfig(
        payer_key=lambda result, header: resolved,
        facilitator=FACILITATOR,
        client=sink.client,
        **extra,
    )


def _route_body(kind: str) -> Callable[[], Any]:
    def answer() -> Any:
        if kind == "dict":
            return json.loads(BODY)
        if kind == "plain":
            return PlainTextResponse(BODY.decode(), status_code=201)
        return StreamingResponse(iter([BODY[:5], BODY[5:]]), status_code=201)

    return answer


def decorator_app(url: str, durable: Optional[DurableConfig], kind: str = "plain") -> FastAPI:
    app = FastAPI()
    produce = _route_body(kind)
    config = X402Config(facilitator_url=url, recipient_evm=RECIPIENT)

    @app.get("/paid")
    @fastapi_require_payment(amount_usd=PRICE, config=config, durable=durable)
    async def paid(request: Request):
        return produce()

    return app


def middleware_app(url: str, durable: Optional[DurableConfig], kind: str = "plain") -> FastAPI:
    app = FastAPI()
    produce = _route_body(kind)

    @app.get("/paid")
    async def paid():
        return produce()

    config = X402Config(facilitator_url=url, recipient_evm=RECIPIENT)
    app.add_middleware(
        X402Middleware, config=config, protected_paths={"/paid": PRICE}, durable=durable
    )
    return app


MOUNTS = [decorator_app, middleware_app]
mounts = pytest.mark.parametrize("mount", MOUNTS, ids=lambda m: m.__name__)


def _get(app: FastAPI) -> httpx.Response:
    async def main() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
            return await c.get("/paid", headers={"X-PAYMENT": x_payment()})

    return asyncio.run(main())


def _expected_status(kind: str) -> int:
    return 200 if kind == "dict" else 201


# -- the sale with durable on --------------------------------------------------


@mounts
@pytest.mark.parametrize("kind", ["plain", "stream", "dict"])
def test_a_sale_with_durable_anchors_once_and_emits_the_header(scripted, mount, kind):
    rail = scripted()
    sink = Sink()
    response = _get(mount(rail.url, _durable(sink), kind))

    assert response.status_code == _expected_status(kind)
    assert json.loads(response.content) == json.loads(BODY)
    assert rail.calls == ["/verify", "/settle"]
    assert len(sink.requests) == 1
    url, sent = sink.requests[0]
    assert url == f"{FACILITATOR}/dx402/anchor"
    assert sent["txHash"] == TX
    assert sent["payer"] == PAYER
    assert sent["payee"] == RECIPIENT
    assert sent["network"] == "arc"
    assert sent["paymentId"] == payment_id("eip155:5042", TX)
    assert "proofOfPayment" not in sent
    assert _decode(response.headers["x-durable-evidence"]) == ANCHORED
    parse_evidence_header(response.headers["x-durable-evidence"])


@mounts
def test_the_settlement_proof_reaches_the_anchor(scripted, mount):
    rail = scripted()
    sink = Sink()
    proof = {"network": "eip155:5042", "txHash": TX}
    durable = _durable(sink, proof_of_payment=lambda result: proof)
    _get(mount(rail.url, durable))
    assert sink.requests[0][1]["proofOfPayment"] == proof


@mounts
def test_durable_off_makes_no_call_and_no_header(scripted, mount):
    rail = scripted()
    sink = Sink()
    response = _get(mount(rail.url, None))
    assert response.status_code == 201
    assert response.content == BODY
    assert sink.requests == []
    assert "x-durable-evidence" not in response.headers
    assert rail.calls == ["/verify", "/settle"]


# -- a skip keeps body, status and settlement ---------------------------------


def _skip_case(scripted, mount, kind, durable, sink) -> dict:
    rail = scripted()
    response = _get(mount(rail.url, durable, kind))
    assert response.status_code == _expected_status(kind)
    assert json.loads(response.content) == json.loads(BODY)
    assert rail.calls == ["/verify", "/settle"]
    return _decode(response.headers["x-durable-evidence"])


@mounts
@pytest.mark.parametrize("kind", ["plain", "stream"])
def test_a_body_over_the_limit_is_delivered_with_too_large(scripted, mount, kind):
    sink = Sink()
    durable = _durable(sink, max_body_bytes=len(BODY) - 1)
    evidence = _skip_case(scripted, mount, kind, durable, sink)
    assert evidence == {"v": 1, "skipped": "too_large"}
    assert sink.requests == []


@mounts
def test_a_body_at_the_limit_is_anchored(scripted, mount):
    sink = Sink()
    durable = _durable(sink, max_body_bytes=len(BODY))
    evidence = _skip_case(scripted, mount, "stream", durable, sink)
    assert evidence == ANCHORED
    assert len(sink.requests) == 1


@mounts
@pytest.mark.parametrize("key", [None, b""], ids=["none", "empty"])
def test_no_payer_key_is_delivered_with_no_payer_key(scripted, mount, key):
    sink = Sink()
    durable = DurableConfig(
        payer_key=lambda result, header: key, facilitator=FACILITATOR, client=sink.client
    )
    evidence = _skip_case(scripted, mount, "plain", durable, sink)
    assert evidence == {"v": 1, "skipped": "no_payer_key"}
    assert sink.requests == []


@mounts
def test_a_payer_key_resolver_that_raises_is_no_payer_key(scripted, mount):
    sink = Sink()

    def broken(result: Any, header: str) -> bytes:
        raise ValueError("no key for this payload")

    durable = DurableConfig(payer_key=broken, facilitator=FACILITATOR, client=sink.client)
    evidence = _skip_case(scripted, mount, "plain", durable, sink)
    assert evidence == {"v": 1, "skipped": "no_payer_key"}


@pytest.mark.parametrize(
    "bad",
    [{}, {"payer_key": None}, {"payer_key": b"key"}],
    ids=["missing", "none", "not-callable"],
)
def test_a_config_without_a_resolver_is_refused_when_built(bad):
    with pytest.raises(TypeError):
        DurableConfig(**bad)


@mounts
@pytest.mark.parametrize("status", [500, 503, 422])
def test_an_anchor_that_failed_is_delivered_with_anchor_failed(scripted, mount, status):
    sink = Sink(status=status, answer={"error": "nope"})
    evidence = _skip_case(scripted, mount, "plain", _durable(sink), sink)
    assert evidence["skipped"] == "anchor_failed"
    assert len(sink.requests) == 1


@mounts
def test_an_anchor_that_raises_is_delivered_with_anchor_failed(scripted, mount):
    sink = Sink()

    def boom(*args: Any, **kwargs: Any) -> dict:
        raise RuntimeError("sink down")

    evidence = _skip_case(scripted, mount, "plain", _durable(sink, anchor=boom), sink)
    assert evidence == {"v": 1, "skipped": "anchor_failed"}


# -- no settlement, no evidence -----------------------------------------------


@mounts
@pytest.mark.parametrize(
    "verify",
    [(200, {"isValid": False, "invalidReason": "invalid_signature", "payer": PAYER}, {}),
     (400, {"error": "invalid_payload"}, {})],
    ids=["invalid", "400"],
)
def test_a_failed_verify_announces_no_evidence(scripted, mount, verify):
    rail = scripted(verify=verify)
    sink = Sink()
    response = _get(mount(rail.url, _durable(sink)))
    assert response.status_code >= 400
    assert "x-durable-evidence" not in response.headers
    assert sink.requests == []
    assert "/settle" not in rail.calls


@mounts
@pytest.mark.parametrize("case", ["502-settlement-unconfirmed", "500-with-transaction",
                                  "200-reverted-with-transaction"])
def test_a_settle_that_is_unknown_announces_no_evidence(scripted, mount, case):
    rail = scripted(settle=MAY_HAVE_SETTLED[case][0])
    sink = Sink()
    response = _get(mount(rail.url, _durable(sink)))
    assert response.status_code >= 400
    assert "x-durable-evidence" not in response.headers
    assert sink.requests == []


# -- the hook itself ----------------------------------------------------------


def _result(**overrides: Any) -> PaymentResult:
    fields = dict(payer_address=PAYER, transaction_hash=TX, network="arc", amount_usd=PRICE)
    fields.update(overrides)
    return PaymentResult(**fields)


def _receipt(status: str) -> FacilitatorReceipt:
    return FacilitatorReceipt.model_validate(rail_receipt(status, TX, None))


@pytest.mark.parametrize("status", ["pending", "unknown", "rejected"])
def test_a_receipt_that_is_not_settled_gets_no_evidence(status):
    sink = Sink()
    result = _result(receipt=_receipt(status))
    assert durable_evidence(_durable(sink), result, "h", BODY, 200) is None
    assert sink.requests == []


def test_a_confirmed_receipt_gets_evidence():
    sink = Sink()
    value = durable_evidence(_durable(sink), _result(receipt=_receipt("confirmed")), "h", BODY, 200)
    assert _decode(value) == ANCHORED


@pytest.mark.parametrize(
    "result",
    [_result(transaction_hash=None), _result(transaction_hash=""), _result(success=False), None,
     {"transaction_hash": TX}],
    ids=["no-tx", "empty-tx", "not-success", "none", "not-a-result"],
)
def test_without_a_settled_payment_there_is_no_evidence(result):
    sink = Sink()
    assert durable_evidence(_durable(sink), result, "h", BODY, 200) is None
    assert sink.requests == []


@pytest.mark.parametrize("status", [199, 300, 304, 399, 400, 402, 404, 500, 503])
def test_an_error_response_is_not_evidence(status):
    sink = Sink()
    assert durable_evidence(_durable(sink), _result(), "h", BODY, status) is None
    assert sink.requests == []


@pytest.mark.parametrize("status", [200, 201, 204, 299])
def test_a_delivered_response_is_evidence(status):
    sink = Sink()
    assert durable_evidence(_durable(sink), _result(), "h", BODY, status) is not None


def test_off_is_none_and_no_call():
    assert durable_evidence(None, _result(), "h", BODY, 200) is None


@pytest.mark.parametrize("network,caip2", [("arc", "eip155:5042"), ("ARC", "eip155:5042"),
                                           ("eip155:5042", "eip155:5042")])
def test_the_payment_id_is_the_caip2_one(network, caip2):
    sink = Sink()
    durable_evidence(_durable(sink), _result(network=network), "h", BODY, 200)
    assert sink.requests[0][1]["paymentId"] == payment_id(caip2, TX)


def test_the_resolver_gets_the_result_and_the_header():
    sink = Sink()
    seen = []
    key = _public_key()

    def resolve(result: PaymentResult, header: str) -> bytes:
        seen.append((result.transaction_hash, header))
        return key

    durable_evidence(DurableConfig(payer_key=resolve, facilitator=FACILITATOR, client=sink.client),
                     _result(), "the-x-payment", BODY, 200)
    assert seen == [(TX, "the-x-payment")]


def test_a_configured_payee_wins_over_the_recipient():
    sink = Sink()
    durable_evidence(_durable(sink, payee="0xPayee"), _result(), "h", BODY, 200, payee=RECIPIENT)
    assert sink.requests[0][1]["payee"] == "0xPayee"


def test_an_anchor_that_answers_no_dict_is_anchor_failed():
    sink = Sink()
    value = durable_evidence(_durable(sink, anchor=lambda *a, **k: None), _result(), "h", BODY, 200)
    assert _decode(value) == {"v": 1, "skipped": "anchor_failed"}


def test_the_module_reuses_anchor_evidence():
    from uvd_x402_sdk import dx402

    config = DurableConfig(payer_key=lambda result, header: None)
    assert config.anchor is dx402.anchor_evidence
    assert config.max_body_bytes == dx402.ANCHOR_MAX_REQUEST_BYTES == 64 * 1024
    assert config.timeout == 5.0


@mounts
def test_a_stream_whose_first_chunks_fill_the_limit_exactly_is_too_large(scripted, mount):
    """The stream is ``BODY[:5]`` then the rest: at 5 bytes it is not over the
    limit yet, and anchoring those 5 would seal a truncated body."""
    sink = Sink()
    evidence = _skip_case(scripted, mount, "stream", _durable(sink, max_body_bytes=5), sink)
    assert evidence == {"v": 1, "skipped": "too_large"}
    assert sink.requests == []


# -- parity with the TypeScript twin (uvd-x402-sdk-typescript #52/#53) ---------


@mounts
def test_the_anchor_follows_the_one_settle(scripted, mount):
    rail = scripted()
    sink = Sink(log=rail.calls)
    response = _get(mount(rail.url, _durable(sink)))
    assert response.status_code == 201
    assert rail.calls == ["/verify", "/settle", "anchor"]


class _Settles(dict):
    """``/settle`` answers a different transaction on each call."""

    def __init__(self, base: dict, txs: list) -> None:
        super().__init__(base)
        self._txs = list(txs)
        self._lock = threading.Lock()

    def get(self, path: str, default: Any = None) -> Any:
        if path != "/settle":
            return super().get(path, default)
        with self._lock:
            tx = self._txs.pop(0)
        return (200, {"success": True, "transaction": tx, "network": "arc", "payer": PAYER}, {})


def _echo_app(mount_name: str, url: str, durable: DurableConfig) -> FastAPI:
    app = FastAPI()
    config = X402Config(facilitator_url=url, recipient_evm=RECIPIENT)

    async def answer(request: Request) -> Any:
        await asyncio.sleep(0.05)  # both requests are inside the route at once
        return JSONResponse({"tx": request.state.payment_result.transaction_hash})

    if mount_name == "decorator_app":
        app.get("/paid")(fastapi_require_payment(amount_usd=PRICE, config=config,
                                                 durable=durable)(answer))
    else:
        app.get("/paid")(answer)
        app.add_middleware(X402Middleware, config=config, protected_paths={"/paid": PRICE},
                           durable=durable)
    return app


@pytest.mark.parametrize("mount_name", ["decorator_app", "middleware_app"])
def test_concurrent_sales_anchor_each_its_own_settlement(scripted, mount_name):
    rail = scripted()
    tx_a, tx_b = "0x" + "a1" * 32, "0x" + "b2" * 32
    rail.answers = _Settles(rail.answers, [tx_a, tx_b])
    sink = Sink(echo=True)
    app = _echo_app(mount_name, rail.url, _durable(sink))

    async def main() -> list:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
            return await asyncio.gather(
                c.get("/paid", headers={"X-PAYMENT": x_payment("0x01")}),
                c.get("/paid", headers={"X-PAYMENT": x_payment("0x02")}),
            )

    responses = asyncio.run(main())
    delivered = [r.json()["tx"] for r in responses]
    assert sorted(delivered) == sorted([tx_a, tx_b])
    for response, tx in zip(responses, delivered):
        evidence = _decode(response.headers["x-durable-evidence"])
        assert evidence["paymentId"] == payment_id("eip155:5042", tx)
    assert sorted(sent["txHash"] for _, sent in sink.requests) == sorted([tx_a, tx_b])
    assert rail.calls.count("/settle") == 2


def _failing_app(mount_name: str, url: str, durable: Optional[DurableConfig]) -> FastAPI:
    app = FastAPI()
    config = X402Config(facilitator_url=url, recipient_evm=RECIPIENT)

    async def chunks() -> Any:
        yield BODY[:5]
        yield BODY[5:9]
        raise RuntimeError("the stream broke")

    async def answer(request: Request) -> Any:
        return StreamingResponse(chunks(), status_code=201)

    if mount_name == "decorator_app":
        app.get("/paid")(fastapi_require_payment(amount_usd=PRICE, config=config,
                                                 durable=durable)(answer))
    else:
        app.get("/paid")(answer)
        app.add_middleware(X402Middleware, config=config, protected_paths={"/paid": PRICE},
                           durable=durable)
    return app


def _received(app: FastAPI) -> tuple:
    """Status, headers and every byte the client got before the stream broke."""
    got: list = []
    meta: dict = {}

    async def main() -> None:
        # The route's error is not raised at the client: it gets what was sent.
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
            async with c.stream("GET", "/paid", headers={"X-PAYMENT": x_payment()}) as r:
                meta["status"] = r.status_code
                meta["headers"] = _lower_headers(r.headers)
                async for data in r.aiter_raw():
                    got.append(data)

    asyncio.run(main())
    return meta["status"], meta["headers"], b"".join(got)


def _lower_headers(headers: Any) -> dict:
    return {str(k).lower(): v for k, v in headers.items()}


@pytest.mark.parametrize("mount_name", ["decorator_app", "middleware_app"])
def test_a_stream_that_fails_keeps_the_bytes_already_read(scripted, mount_name):
    sink = Sink()
    off_status, _, off_bytes = _received(_failing_app(mount_name, scripted().url, None))
    on_status, headers, on_bytes = _received(
        _failing_app(mount_name, scripted().url, _durable(sink))
    )
    assert (on_status, on_bytes) == (off_status, off_bytes) == (201, BODY[:9])
    assert _decode(headers["x-durable-evidence"]) == {"v": 1, "skipped": "not_capturable"}
    assert sink.requests == []


@mounts
def test_a_body_that_announces_more_than_the_limit_is_not_read(scripted, mount):
    """``Content-Length`` over the limit: ``too_large`` before reading a byte,
    even though these bytes alone would fit."""
    rail = scripted()
    sink = Sink()
    app = FastAPI()
    config = X402Config(facilitator_url=rail.url, recipient_evm=RECIPIENT)
    durable = _durable(sink, max_body_bytes=len(BODY))

    def answer() -> Any:
        return StreamingResponse(iter([BODY]), status_code=201,
                                 headers={"content-length": str(len(BODY) + 1)})

    if mount is decorator_app:
        async def paid(request: Request):
            return answer()
        app.get("/paid")(fastapi_require_payment(amount_usd=PRICE, config=config,
                                                 durable=durable)(paid))
    else:
        app.get("/paid")(answer)
        app.add_middleware(X402Middleware, config=config, protected_paths={"/paid": PRICE},
                           durable=durable)
    response = _get(app)
    assert response.status_code == 201
    assert response.content.startswith(BODY)
    assert _decode(response.headers["x-durable-evidence"]) == {"v": 1, "skipped": "too_large"}
    assert sink.requests == []


def test_a_capture_skip_is_the_notice_only_for_a_settled_delivery():
    sink = Sink()
    assert durable_evidence(_durable(sink), _result(), "h", b"", 201, skipped="too_large") \
        == "eyJ2IjogMSwgInNraXBwZWQiOiAidG9vX2xhcmdlIn0"
    assert durable_evidence(_durable(sink), _result(transaction_hash=None), "h", b"", 201,
                            skipped="too_large") is None
    assert durable_evidence(_durable(sink), _result(), "h", b"", 500, skipped="too_large") is None
    assert durable_evidence(None, _result(), "h", b"", 201, skipped="too_large") is None
    assert sink.requests == []


# -- REF-SDKPY-71: what the hook cannot read, and how it passes it on ---------


def _mount(mount_name: str, url: str, durable: Optional[DurableConfig], answer: Callable,
           methods: Optional[list] = None) -> FastAPI:
    """``answer(request)`` behind the decorator or the middleware."""
    app = FastAPI()
    config = X402Config(facilitator_url=url, recipient_evm=RECIPIENT)
    route = app.api_route("/paid", methods=methods or ["GET"])
    if mount_name == "decorator_app":
        route(fastapi_require_payment(amount_usd=PRICE, config=config, durable=durable)(answer))
    else:
        route(answer)
        app.add_middleware(X402Middleware, config=config, protected_paths={"/paid": PRICE},
                           durable=durable)
    return app


MOUNT_NAMES = pytest.mark.parametrize("mount_name", ["decorator_app", "middleware_app"])


class Recorder:
    """An ``anchor`` that keeps the plaintext it was given."""

    def __init__(self) -> None:
        self.bodies: list = []
        self.kwargs: list = []

    def __call__(self, body: bytes, **kwargs: Any) -> dict:
        self.bodies.append(body)
        self.kwargs.append(kwargs)
        return ANCHORED


def _skip(reason: str) -> dict:
    return {"v": 1, "skipped": reason}


@MOUNT_NAMES
def test_a_file_response_is_delivered_untouched_as_not_capturable(scripted, mount_name, tmp_path):
    served = tmp_path / "paid.bin"
    served.write_bytes(BODY * 3)

    async def answer(request: Request) -> Any:
        return FileResponse(served, media_type="application/octet-stream")

    recorder = Recorder()
    off = _get(_mount(mount_name, scripted().url, None, answer))
    on = _get(_mount(mount_name, scripted().url, _durable(Sink(), anchor=recorder), answer))
    assert (on.status_code, on.content) == (off.status_code, off.content) == (200, BODY * 3)
    assert on.headers["content-length"] == off.headers["content-length"]
    if mount_name == "decorator_app":
        # The route's own FileResponse: no body, no iterator, nothing to read.
        assert _decode(on.headers["x-durable-evidence"]) == _skip("not_capturable")
        assert recorder.bodies == []
    else:
        # Behind BaseHTTPMiddleware the file arrives as a stream of its bytes.
        assert _decode(on.headers["x-durable-evidence"]) == ANCHORED
        assert recorder.bodies == [BODY * 3]


@MOUNT_NAMES
def test_a_hook_that_breaks_is_a_notice_never_a_500(scripted, mount_name, monkeypatch):
    async def broken(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("capture broke")

    monkeypatch.setattr(fastapi_integration, "_buffered", broken)

    async def answer(request: Request) -> Any:
        return PlainTextResponse(BODY.decode(), status_code=201)

    sink = Sink()
    response = _get(_mount(mount_name, scripted().url, _durable(sink), answer))
    assert (response.status_code, response.content) == (201, BODY)
    assert _decode(response.headers["x-durable-evidence"]) == _skip("anchor_failed")
    assert sink.requests == []


def _buffer_chunks() -> list:
    # What Starlette's StreamingResponse sends: bytes, memoryview, str.
    return [memoryview(BODY[:5]), memoryview(bytearray(BODY[5:9])), BODY[9:20],
            BODY[20:].decode()]


@pytest.mark.parametrize("chunk", [b"ab\xc3\xb1", bytearray(b"ab\xc3\xb1"),
                                   memoryview(b"ab\xc3\xb1"), "ab\u00f1"],
                         ids=["bytes", "bytearray", "memoryview", "str"])
def test_a_chunk_is_its_own_bytes(chunk):
    assert fastapi_integration._as_bytes(chunk, "utf-8") == b"ab\xc3\xb1"


@MOUNT_NAMES
@pytest.mark.parametrize("source", ["sync", "async"])
def test_buffer_chunks_are_sealed_and_delivered_as_the_same_bytes(scripted, mount_name, source):
    async def chunks() -> Any:
        for chunk in _buffer_chunks():
            yield chunk

    async def answer(request: Request) -> Any:
        content = iter(_buffer_chunks()) if source == "sync" else chunks()
        return StreamingResponse(content, status_code=201)

    recorder = Recorder()
    off = _get(_mount(mount_name, scripted().url, None, answer))
    on = _get(_mount(mount_name, scripted().url, _durable(Sink(), anchor=recorder), answer))
    assert (on.status_code, on.content) == (off.status_code, off.content) == (201, BODY)
    assert recorder.bodies == [BODY]
    assert _decode(on.headers["x-durable-evidence"]) == ANCHORED


def _call_raw(app: FastAPI, on_body: Callable[[dict], None]) -> dict:
    """Drive the ASGI app directly: unlike ``httpx.ASGITransport`` it sees each
    body message as it is sent."""
    messages: list = []

    async def main() -> None:
        sent = False

        async def receive() -> dict:
            nonlocal sent
            if not sent:
                sent = True
                return {"type": "http.request", "body": b"", "more_body": False}
            await asyncio.sleep(3600)
            return {"type": "http.disconnect"}

        async def send(message: dict) -> None:
            messages.append(message)
            if message["type"] == "http.response.body":
                on_body(message)

        scope = {
            "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
            "method": "GET", "scheme": "http", "path": "/paid", "raw_path": b"/paid",
            "root_path": "", "query_string": b"", "server": ("testserver", 80),
            "client": ("127.0.0.1", 1),
            "headers": [(b"host", b"testserver"), (b"x-payment", x_payment().encode())],
        }
        await asyncio.wait_for(app(scope, receive, send), 10)

    asyncio.run(main())
    start = next(m for m in messages if m["type"] == "http.response.start")
    return {
        "status": start["status"],
        "headers": {k.decode().lower(): v.decode() for k, v in start["headers"]},
        "body": b"".join(m.get("body", b"") for m in messages
                         if m["type"] == "http.response.body"),
    }


@MOUNT_NAMES
@pytest.mark.parametrize("media_type", ["text/event-stream", "Text/Event-Stream"])
def test_an_event_stream_goes_out_as_it_comes_with_not_capturable(scripted, mount_name,
                                                                 media_type):
    first_out = threading.Event()

    async def events() -> Any:
        yield b"data: one\n\n"
        # Only reached once the first event left: a hook that holds the stream
        # back never gets here.
        for _ in range(500):
            if first_out.is_set():
                break
            await asyncio.sleep(0.01)
        else:
            raise AssertionError("the first event was held back")
        yield b"data: two\n\n"

    async def answer(request: Request) -> Any:
        return StreamingResponse(events(), media_type=media_type)

    def on_body(message: dict) -> None:
        if message.get("body"):
            first_out.set()

    sink = Sink()
    got = _call_raw(_mount(mount_name, scripted().url, _durable(sink), answer), on_body)
    assert got["status"] == 200
    assert got["body"] == b"data: one\n\ndata: two\n\n"
    assert _decode(got["headers"]["x-durable-evidence"]) == _skip("not_capturable")
    assert sink.requests == []


def _confirmed_rail(scripted: Callable) -> Scripted:
    body = {"success": True, "transaction": TX, "network": "arc", "payer": PAYER,
            "receipt": rail_receipt("confirmed", TX, None)}
    return scripted(settle=(200, body, {}))


@MOUNT_NAMES
@pytest.mark.parametrize("path", ["whole", "stream", "too_large"])
def test_route_and_payment_headers_and_the_background_task_survive(scripted, mount_name, path):
    ran: list = []

    async def answer(request: Request) -> Any:
        extra = {"X-Route": "kept", "Cache-Control": "private"}
        task = BackgroundTask(ran.append, "ran")
        if path == "whole":
            return PlainTextResponse(BODY.decode(), status_code=201, headers=extra,
                                     background=task)
        return StreamingResponse(iter([BODY[:5], BODY[5:]]), status_code=201, headers=extra,
                                 background=task, media_type="application/json")

    limit = 5 if path == "too_large" else DEFAULT_LIMIT
    off = _get(_mount(mount_name, _confirmed_rail(scripted).url, None, answer))
    assert ran == ["ran"]
    sink = Sink()
    on = _get(_mount(mount_name, _confirmed_rail(scripted).url,
                     _durable(sink, max_body_bytes=limit), answer))
    assert ran == ["ran", "ran"]
    assert (on.status_code, on.content) == (off.status_code, off.content) == (201, BODY)
    for name in ("x-route", "payment-response", "x-payment-response", "cache-control",
                 "access-control-expose-headers", "content-type"):
        assert on.headers.get_list(name) == off.headers.get_list(name), name
    assert "payment-response" in on.headers
    assert on.headers["x-route"] == "kept"
    evidence = _decode(on.headers["x-durable-evidence"])
    if path == "too_large":
        assert evidence == _skip("too_large")
        assert sink.requests == []
    else:
        assert evidence == ANCHORED
        assert len(sink.requests) == 1


DEFAULT_LIMIT = DurableConfig(payer_key=lambda result, header: None).max_body_bytes


def test_a_replayed_payment_gets_no_evidence():
    sink = Sink()
    result = _result(idempotent_replayed=True)
    assert durable_evidence(_durable(sink), result, "h", BODY, 200) is None
    assert durable_evidence(_durable(sink), result, "h", b"", 200, skipped="too_large") is None
    assert sink.requests == []


@MOUNT_NAMES
def test_a_foreign_replay_is_refused_and_gets_no_evidence(scripted, mount_name):
    """A replay this request did not admit never reaches the route."""
    rail = scripted(settle=(200, {"success": True, "transaction": TX, "network": "arc",
                                  "payer": PAYER}, {"Idempotent-Replayed": "true"}))

    async def answer(request: Request) -> Any:
        return PlainTextResponse(BODY.decode(), status_code=201)

    sink = Sink()
    response = _get(_mount(mount_name, rail.url, _durable(sink), answer))
    assert response.status_code >= 400
    assert "x-durable-evidence" not in response.headers
    assert sink.requests == []


def test_the_post_hook_leaves_a_replayed_sale_without_anchor_or_header():
    """The route's own replay (purchase context): delivered, not anchored again."""
    sink = Sink()
    client = type("Client", (), {"config": X402Config(recipient_evm=RECIPIENT)})()
    original = PlainTextResponse(BODY.decode(), status_code=201)
    response = asyncio.run(fastapi_integration._with_durable_evidence(
        _durable(sink), client, _result(idempotent_replayed=True), "h", original, "GET"))
    assert response is original
    assert "x-durable-evidence" not in response.headers
    assert sink.requests == []


@MOUNT_NAMES
def test_the_route_own_evidence_is_kept_and_not_anchored_again(scripted, mount_name):
    own = "the-route-anchored-this-itself"

    async def answer(request: Request) -> Any:
        return PlainTextResponse(BODY.decode(), status_code=201,
                                 headers={"X-Durable-Evidence": own})

    sink = Sink()
    response = _get(_mount(mount_name, scripted().url, _durable(sink), answer))
    assert (response.status_code, response.content) == (201, BODY)
    assert response.headers.get_list("x-durable-evidence") == [own]
    assert sink.requests == []


@MOUNT_NAMES
def test_a_head_request_is_not_anchored(scripted, mount_name):
    async def answer(request: Request) -> Any:
        return PlainTextResponse(BODY.decode(), status_code=200)

    sink = Sink()
    app = _mount(mount_name, scripted().url, _durable(sink), answer, methods=["GET", "HEAD"])

    async def main() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
            return await c.head("/paid", headers={"X-PAYMENT": x_payment()})

    response = asyncio.run(main())
    assert response.status_code == 200
    assert "x-durable-evidence" not in response.headers
    assert sink.requests == []


@pytest.mark.parametrize("method", ["HEAD", "head", "Head"])
def test_wants_evidence_skips_head_in_any_case(method):
    config = _durable(Sink())
    assert durable_evidence is not None
    assert fastapi_integration.wants_evidence(config, _result(), 200, method) is False
    assert fastapi_integration.wants_evidence(config, _result(), 200, "GET") is True
    assert fastapi_integration.wants_evidence(config, _result(), 200, "GET", True) is False


def test_there_is_no_fixed_pointer_and_each_sale_sends_its_own_sealed_blob():
    from dataclasses import fields

    assert "pointer" not in {f.name for f in fields(DurableConfig)}
    sink = Sink()
    config = _durable(sink)
    durable_evidence(config, _result(transaction_hash="0x" + "a1" * 32), "h", b"one", 200)
    durable_evidence(config, _result(transaction_hash="0x" + "b2" * 32), "h", b"two", 200)
    sent = [payload for _, payload in sink.requests]
    assert all("pointer" not in payload and payload["sealed"] for payload in sent)
    assert sent[0]["sealed"] != sent[1]["sealed"]
    assert sent[0]["contentHash"] != sent[1]["contentHash"]


@MOUNT_NAMES
def test_a_stream_that_fails_still_raises_after_the_bytes_read(scripted, mount_name):
    """The route's error reaches the server as it would without the hook."""
    app = _failing_app(mount_name, scripted().url, _durable(Sink()))

    async def main() -> None:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
            await c.get("/paid", headers={"X-PAYMENT": x_payment()})

    with pytest.raises(RuntimeError, match="the stream broke"):
        asyncio.run(main())


@MOUNT_NAMES
def test_the_anchor_runs_off_the_event_loop(scripted, mount_name):
    seen: list = []

    def anchor(body: bytes, **kwargs: Any) -> dict:
        try:
            asyncio.get_running_loop()
            seen.append("on the loop")
        except RuntimeError:
            seen.append("in a thread")
        return ANCHORED

    async def answer(request: Request) -> Any:
        return PlainTextResponse(BODY.decode())

    _get(_mount(mount_name, scripted().url, _durable(Sink(), anchor=anchor), answer))
    assert seen == ["in a thread"]


@MOUNT_NAMES
def test_a_slow_anchor_is_cut_at_the_deadline(scripted, mount_name):
    release = threading.Event()

    def slow(body: bytes, **kwargs: Any) -> dict:
        release.wait(5)
        return ANCHORED

    async def answer(request: Request) -> Any:
        return PlainTextResponse(BODY.decode(), status_code=201)

    app = _mount(mount_name, scripted().url, _durable(Sink(), anchor=slow, timeout=0.2), answer)
    took: list = []

    async def main() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
            started = time.monotonic()
            got = await c.get("/paid", headers={"X-PAYMENT": x_payment()})
            took.append(time.monotonic() - started)
            release.set()  # the thread left behind ends; asyncio.run waits for it
            return got

    response = asyncio.run(main())
    assert took[0] < 3
    assert (response.status_code, response.content) == (201, BODY)
    assert _decode(response.headers["x-durable-evidence"]) == _skip("timeout")


@MOUNT_NAMES
def test_a_slow_stream_is_delivered_whole_with_timeout(scripted, mount_name):
    """The second chunk only comes once the first one left: a capture that
    waits past the deadline for it never gets it."""
    first_out = threading.Event()

    async def chunks() -> Any:
        yield BODY[:5]
        for _ in range(500):
            if first_out.is_set():
                break
            await asyncio.sleep(0.01)
        else:
            raise AssertionError("the capture held the stream past its deadline")
        yield BODY[5:]

    async def answer(request: Request) -> Any:
        return StreamingResponse(chunks(), status_code=201)

    def on_body(message: dict) -> None:
        if message.get("body"):
            first_out.set()

    sink = Sink()
    got = _call_raw(_mount(mount_name, scripted().url, _durable(sink, timeout=0.1), answer),
                    on_body)
    assert (got["status"], got["body"]) == (201, BODY)
    assert _decode(got["headers"]["x-durable-evidence"]) == _skip("timeout")
    assert sink.requests == []


def test_a_deadline_already_gone_starts_no_anchor():
    sink = Sink()
    value = durable_evidence(_durable(sink), _result(), "h", BODY, 200,
                             deadline=time.monotonic() - 1)
    assert _decode(value) == _skip("timeout")
    assert sink.requests == []


def test_the_anchor_timeout_is_what_is_left_of_the_budget():
    recorder = Recorder()
    durable_evidence(_durable(Sink(), anchor=recorder, timeout=4.0), _result(), "h", BODY, 200,
                     deadline=time.monotonic() + 1.0)
    assert 0 < recorder.kwargs[0]["timeout"] <= 1.0
    assert "pointer" not in recorder.kwargs[0]


@pytest.mark.parametrize("size", [0, -1, True, 1.5, "10", None])
def test_a_bad_body_limit_is_refused_when_built(size):
    with pytest.raises(TypeError):
        DurableConfig(payer_key=lambda result, header: None, max_body_bytes=size)


@pytest.mark.parametrize("budget", [0, -1, True, float("nan"), float("inf"), "5", None])
def test_a_bad_timeout_is_refused_when_built(budget):
    with pytest.raises(TypeError):
        DurableConfig(payer_key=lambda result, header: None, timeout=budget)


def test_a_body_over_the_default_limit_is_too_large():
    sink = Sink()
    body = b"x" * (DEFAULT_LIMIT + 1)
    assert _decode(durable_evidence(_durable(sink), _result(), "h", body, 200)) \
        == _skip("too_large")
    assert sink.requests == []


def test_a_decorated_route_returning_a_model_with_dates_is_delivered(scripted):
    at = datetime(2026, 10, 11, tzinfo=timezone.utc)

    async def answer(request: Request) -> Any:
        return {"at": at, "price": PRICE}

    recorder = Recorder()
    off = _get(_mount("decorator_app", scripted().url, None, answer))
    response = _get(_mount("decorator_app", scripted().url, _durable(Sink(), anchor=recorder),
                           answer))
    assert (response.status_code, response.content) == (off.status_code, off.content)
    assert response.json()["at"].startswith("2026-10-11T00:00:00")
    assert _decode(response.headers["x-durable-evidence"]) == ANCHORED
    assert recorder.bodies == [response.content]


# -- VER-SDKPY-71 ------------------------------------------------------------

_TRACE: contextvars.ContextVar = contextvars.ContextVar("trace", default=None)


@MOUNT_NAMES
@pytest.mark.parametrize("held", ["contextvar", "cancel_scope"])
def test_a_stream_that_holds_its_task_across_yields_is_delivered_and_anchored(
    scripted, mount_name, held
):
    """A contextvar set/reset or an anyio cancel scope around a ``yield`` only
    works if one task walks the generator, as Starlette does without durable."""

    async def chunks() -> Any:
        for part in (BODY[:5], BODY[5:12], BODY[12:]):
            if held == "contextvar":
                token = _TRACE.set(part)
                yield part
                _TRACE.reset(token)
            else:
                with anyio.move_on_after(30):
                    yield part

    async def answer(request: Request) -> Any:
        return StreamingResponse(chunks(), status_code=201)

    recorder = Recorder()
    off = _get(_mount(mount_name, scripted().url, None, answer))
    on = _get(_mount(mount_name, scripted().url, _durable(Sink(), anchor=recorder), answer))
    assert (on.status_code, on.content) == (off.status_code, off.content) == (201, BODY)
    assert recorder.bodies == [BODY]
    assert _decode(on.headers["x-durable-evidence"]) == ANCHORED


class _Public(BaseModel):
    answer: str


_DECLARED = {
    "status_code": {"status_code": 201},
    "response_model": {"response_model": _Public},
    "response_class": {"response_class": PlainTextResponse},
}


@pytest.mark.parametrize("receipt", [False, True], ids=["no-receipt", "receipt"])
@pytest.mark.parametrize("declared", sorted(_DECLARED))
def test_a_decorated_return_that_is_not_a_response_is_served_as_the_route_declares(
    scripted, declared, receipt
):
    """Durable on or off, and with a receipt or not, the bytes are FastAPI's
    for this route (declared status, response_model, response_class), and the
    anchor seals those bytes."""

    def build(durable: Optional[DurableConfig]) -> FastAPI:
        app = FastAPI()
        rail = _confirmed_rail(scripted) if receipt else scripted()
        config = X402Config(facilitator_url=rail.url, recipient_evm=RECIPIENT)

        @app.get("/paid", **_DECLARED[declared])
        @fastapi_require_payment(amount_usd=PRICE, config=config, durable=durable)
        async def paid(request: Request) -> Any:
            if declared == "response_class":
                return "plain text answer"
            return {"answer": "the paid content", "hidden": "seller only"}

        return app

    plain = _get(build(None))
    recorder = Recorder()
    on = _get(build(_durable(Sink(), anchor=recorder)))
    for response in (plain, on):
        assert response.status_code == (201 if declared == "status_code" else 200)
        if declared == "response_class":
            assert response.headers["content-type"].startswith("text/plain")
            assert response.content == b"plain text answer"
        else:
            assert response.headers["content-type"] == "application/json"
        if declared == "response_model":
            assert response.json() == {"answer": "the paid content"}
        assert ("payment-response" in response.headers) is receipt
    assert on.content == plain.content
    assert on.headers["content-type"] == plain.headers["content-type"]
    assert _decode(on.headers["x-durable-evidence"]) == ANCHORED
    assert recorder.bodies == [on.content]


@pytest.mark.parametrize("receipt", [False, True], ids=["no-receipt", "receipt"])
def test_an_injected_response_parameter_keeps_its_status_and_headers(scripted, receipt):
    def build(durable: Optional[DurableConfig]) -> FastAPI:
        app = FastAPI()
        rail = _confirmed_rail(scripted) if receipt else scripted()
        config = X402Config(facilitator_url=rail.url, recipient_evm=RECIPIENT)

        @app.get("/paid")
        @fastapi_require_payment(amount_usd=PRICE, config=config, durable=durable)
        async def paid(request: Request, response: Response) -> Any:
            response.status_code = 202
            response.headers["X-Route"] = "kept"
            return {"answer": "the paid content"}

        return app

    plain = _get(build(None))
    on = _get(build(_durable(Sink(), anchor=Recorder())))
    for response in (plain, on):
        assert response.status_code == 202
        assert response.headers["x-route"] == "kept"
        assert response.json() == {"answer": "the paid content"}
    assert _decode(on.headers["x-durable-evidence"]) == ANCHORED


@contextlib.contextmanager
def _served(app: FastAPI) -> Any:
    """``app`` behind a real uvicorn on 127.0.0.1 (a free port), in a thread."""
    import uvicorn

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning", lifespan="off"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    try:
        for _ in range(1000):
            if server.started:
                break
            time.sleep(0.01)
        assert server.started
        yield f"http://127.0.0.1:{sock.getsockname()[1]}"
    finally:
        server.should_exit = True
        thread.join(10)
        sock.close()


@MOUNT_NAMES
def test_under_uvicorn_a_stream_holding_its_task_is_delivered_and_anchored(
    scripted, mount_name
):
    async def chunks() -> Any:
        for part in (BODY[:5], BODY[5:12], BODY[12:]):
            token = _TRACE.set(part)
            with anyio.move_on_after(30):
                yield part
            _TRACE.reset(token)

    async def answer(request: Request) -> Any:
        return StreamingResponse(chunks(), status_code=201)

    def fetch(app: FastAPI) -> httpx.Response:
        with _served(app) as base:
            with httpx.Client(base_url=base, trust_env=False, timeout=10) as client:
                return client.get("/paid", headers={"X-PAYMENT": x_payment()})

    recorder = Recorder()
    off = fetch(_mount(mount_name, scripted().url, None, answer))
    on = fetch(_mount(mount_name, scripted().url, _durable(Sink(), anchor=recorder), answer))
    assert (on.status_code, on.content) == (off.status_code, off.content) == (201, BODY)
    assert recorder.bodies == [BODY]
    assert _decode(on.headers["x-durable-evidence"]) == ANCHORED


@pytest.mark.parametrize("key", ["a-str-key", 7, ["k"]], ids=["str", "int", "list"])
def test_a_payer_key_that_is_not_bytes_is_no_payer_key(key):
    sink = Sink()
    config = DurableConfig(
        payer_key=lambda result, header: key, facilitator=FACILITATOR, client=sink.client
    )
    assert _decode(durable_evidence(config, _result(), "h", BODY, 200)) == _skip("no_payer_key")
    assert sink.requests == []


def test_a_decorated_non_response_with_a_confirmed_receipt_is_served_as_without_durable(
    scripted,
):
    async def answer(request: Request) -> Any:
        return {"answer": "the paid content"}

    recorder = Recorder()
    off = _get(_mount("decorator_app", _confirmed_rail(scripted).url, None, answer))
    on = _get(_mount("decorator_app", _confirmed_rail(scripted).url,
                     _durable(Sink(), anchor=recorder), answer))
    assert (on.status_code, on.content) == (off.status_code, off.content)
    for name in ("content-type", "payment-response", "x-payment-response"):
        assert on.headers.get_list(name) == off.headers.get_list(name), name
    assert "payment-response" in on.headers
    # The receipt already made it a JSONResponse, durable or not: those bytes are anchored.
    assert recorder.bodies == [on.content]
    assert _decode(on.headers["x-durable-evidence"]) == ANCHORED


def test_a_capture_cancelled_mid_stream_closes_the_routes_stream():
    closed: list = []

    async def chunks() -> Any:
        try:
            yield b"a"
            await asyncio.sleep(30)
            yield b"b"
        finally:
            closed.append(True)

    async def main() -> None:
        response = StreamingResponse(chunks())
        deadline = time.monotonic() + 30
        capture = asyncio.ensure_future(fastapi_integration._buffered(response, 100, deadline))
        await asyncio.sleep(0.05)
        capture.cancel()
        with pytest.raises(asyncio.CancelledError):
            await capture
        await asyncio.sleep(0.05)
        assert closed == [True]

    asyncio.run(main())
