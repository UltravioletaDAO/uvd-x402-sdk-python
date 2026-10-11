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
import json
import threading
from decimal import Decimal
from typing import Any, Callable, Optional

import httpx
import pytest

pytest.importorskip("fastapi")
pytest.importorskip("cryptography")

from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey  # noqa: E402
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat  # noqa: E402
from fastapi import FastAPI, Request  # noqa: E402
from fastapi.responses import PlainTextResponse, StreamingResponse  # noqa: E402

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


@mounts
def test_without_a_resolver_it_is_no_payer_key(scripted, mount):
    sink = Sink()
    durable = DurableConfig(facilitator=FACILITATOR, client=sink.client)
    evidence = _skip_case(scripted, mount, "plain", durable, sink)
    assert evidence == {"v": 1, "skipped": "no_payer_key"}
    assert sink.requests == []


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


@pytest.mark.parametrize("status", [199, 400, 402, 404, 500, 503])
def test_an_error_response_is_not_evidence(status):
    sink = Sink()
    assert durable_evidence(_durable(sink), _result(), "h", BODY, status) is None
    assert sink.requests == []


@pytest.mark.parametrize("status", [200, 201, 204, 304, 399])
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

    assert DurableConfig().anchor is dx402.anchor_evidence
    assert DurableConfig().max_body_bytes == 32 * 1024 * 1024
    assert DurableConfig().payer_key is None


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
        return {"tx": request.state.payment_result.transaction_hash}

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
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
            async with c.stream("GET", "/paid", headers={"X-PAYMENT": x_payment()}) as r:
                meta["status"] = r.status_code
                meta["headers"] = _lower_headers(r.headers)
                async for data in r.aiter_raw():
                    got.append(data)

    with pytest.raises(RuntimeError, match="the stream broke"):
        asyncio.run(main())
    return meta.get("status"), meta.get("headers", {}), b"".join(got)


def _lower_headers(headers: Any) -> dict:
    return {str(k).lower(): v for k, v in headers.items()}


@pytest.mark.parametrize("mount_name", ["decorator_app", "middleware_app"])
def test_a_stream_that_fails_keeps_the_bytes_already_read(scripted, mount_name):
    sink = Sink()
    off_status, _, off_bytes = _received(_failing_app(mount_name, scripted().url, None))
    on_status, headers, on_bytes = _received(
        _failing_app(mount_name, scripted().url, _durable(sink))
    )
    assert (on_status, on_bytes) == (off_status, off_bytes)
    assert on_bytes.startswith(BODY[:9]) or on_status is None
    if on_status is not None:
        assert _decode(headers["x-durable-evidence"]) == {"v": 1, "skipped": "anchor_failed"}
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
