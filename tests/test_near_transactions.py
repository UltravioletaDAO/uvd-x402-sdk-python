"""The broadcast journal is replayed byte-for-byte, including after lost answers."""

import asyncio
import base64
import hashlib
import json

import httpx
import pytest

from tests.test_near_ows import LOCAL, signed_tx, transactions
from uvd_x402_sdk.near_transactions import NearTransactionError, send_near_transaction
from uvd_x402_sdk.solana_signing import _b58encode

SIGNED = signed_tx("usdc", transactions()[1][1], LOCAL)
HASH = _b58encode(hashlib.sha256(base64.b64decode(SIGNED)[:-65]).digest())
URL = "https://rpc.test.invalid/"


def outcome(**changes):
    result = dict(transaction={"hash": HASH}, status={"SuccessValue": ""}, receipts_outcome=[])
    result.update(changes)
    return dict(jsonrpc="2.0", id="near-broadcast", result=result)


def corrupt_gzip(status, *, decoded_by_client=True):
    """An answer whose gzip body cannot be decoded.

    With ``stream=`` the client decodes the body after the status line, as with a
    real socket. With ``content=`` httpx decodes while building the Response, so
    the DecodingError leaves the transport itself and the status is never seen.
    """
    headers = {"Content-Encoding": "gzip"}
    if decoded_by_client:
        return httpx.Response(status, headers=headers, stream=httpx.ByteStream(b"not gzip"))
    return httpx.Response(status, headers=headers, content=b"not gzip")


@pytest.mark.parametrize(
    "first", ["timeout", "transport", "500", "429", "rpc_timeout", "corrupt_503"]
)
async def test_accepted_but_answer_lost_retries_identical_bytes(first):
    sent = []
    landed = set()

    async def transport(request):
        sent.append(request.content)
        payload = json.loads(request.content)
        assert payload["method"] == "broadcast_tx_commit"
        assert payload["params"] == [SIGNED]
        # Model a chain accepting the first request BEFORE its answer is lost.
        landed.add(payload["params"][0])
        if len(sent) == 1:
            if first == "timeout":
                raise httpx.ReadTimeout("lost answer")
            if first == "transport":
                raise httpx.ConnectError("offline")
            if first == "rpc_timeout":
                return httpx.Response(
                    200,
                    json={
                        "jsonrpc": "2.0",
                        "id": "near-broadcast",
                        "error": {"cause": {"name": "TIMEOUT_ERROR"}},
                    },
                )
            if first == "corrupt_503":
                return corrupt_gzip(503)
            return httpx.Response(int(first))
        return httpx.Response(200, json=outcome())

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        result = await send_near_transaction(SIGNED, rpc_url=URL, client=client, retry_delay=0)
    assert result["transaction"]["hash"] == HASH
    assert len(sent) == 2 and sent[0] == sent[1]
    assert landed == {SIGNED}


async def test_restart_replays_persisted_bytes_and_never_has_a_signer():
    sent = []

    async def transport(request):
        sent.append(request.content)
        raise httpx.ReadTimeout("accepted, answer unavailable")

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        for _ in range(2):
            with pytest.raises(NearTransactionError, match="unresolved") as exc:
                await send_near_transaction(SIGNED, rpc_url=URL, client=client, retry_delay=0)
            assert exc.value.transaction_hash == HASH and exc.value.uncertain
    assert len(sent) == 6 and len(set(sent)) == 1
    with pytest.raises(TypeError):
        await send_near_transaction(SIGNED, rpc_url=URL, signer=LOCAL)


@pytest.mark.parametrize(
    "reply",
    [
        httpx.Response(400),
        httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": "near-broadcast",
                "error": {"cause": {"name": "INVALID_TRANSACTION"}},
            },
        ),
        httpx.Response(200, json={"jsonrpc": "2.0", "id": "near-broadcast", "error": None}),
        httpx.Response(200, json=[]),
        httpx.Response(200, content=b"garbage"),
        httpx.Response(200, json=outcome(transaction={"hash": "foreign"})),
        httpx.Response(200, json=outcome(status="Started")),
        httpx.Response(200, json={**outcome(), "id": "another-request"}),
    ],
)
async def test_refusal_or_unreadable_outcome_never_rebuilds_or_retries(reply):
    sent = []

    def transport(request):
        sent.append(request.content)
        return reply

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        with pytest.raises(NearTransactionError) as exc:
            await send_near_transaction(SIGNED, rpc_url=URL, client=client, retry_delay=0)
        assert exc.value.transaction_hash == HASH and exc.value.uncertain
    assert len(sent) == 1


@pytest.mark.parametrize(
    "status,sends,reason",
    [
        (200, 1, "unreadable RPC response"),
        (503, 3, "broadcast unresolved"),
        (429, 3, "broadcast unresolved"),
        (403, 1, "RPC HTTP refusal"),
    ],
)
async def test_undecodable_body_is_judged_by_the_status_it_came_with(status, sends, reason):
    # The RPC already holds the bytes: the answer must never escape as an httpx error.
    sent = []

    def transport(request):
        sent.append(request.content)
        return corrupt_gzip(status)

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        with pytest.raises(NearTransactionError, match=reason) as exc:
            await send_near_transaction(SIGNED, rpc_url=URL, client=client, retry_delay=0)
    assert exc.value.transaction_hash == HASH and exc.value.uncertain
    assert len(sent) == sends and len(set(sent)) == 1
    assert json.loads(sent[0])["params"] == [SIGNED]


@pytest.mark.parametrize(
    "case",
    ["decoded_in_transport_200", "decoded_in_transport_503", "redirect_loop", "client_hook"],
)
async def test_any_httpx_error_after_sending_is_uncertain_and_not_retried(case):
    sent = []

    async def raise_for_status(response):
        response.raise_for_status()

    def transport(request):
        sent.append(request.content)
        if case == "redirect_loop":
            return httpx.Response(307, headers={"Location": URL})
        if case == "client_hook":
            return httpx.Response(503)
        return corrupt_gzip(int(case[-3:]), decoded_by_client=False)

    options = dict(transport=httpx.MockTransport(transport))
    if case == "redirect_loop":
        options["follow_redirects"] = True
    if case == "client_hook":
        options["event_hooks"] = {"response": [raise_for_status]}
    async with httpx.AsyncClient(**options) as client:
        with pytest.raises(NearTransactionError, match="unreadable RPC response") as exc:
            await send_near_transaction(SIGNED, rpc_url=URL, client=client, retry_delay=0)
    assert exc.value.transaction_hash == HASH and exc.value.uncertain
    assert exc.value.__suppress_context__
    # A redirect loop re-sends the same body (307) until httpx gives up; nothing else repeats.
    assert len(sent) == (21 if case == "redirect_loop" else 1) and len(set(sent)) == 1


async def test_final_execution_failure_is_returned_once():
    sent = []
    result = outcome(status={"Failure": {"ActionError": {"index": 0}}})

    def transport(request):
        sent.append(request.content)
        return httpx.Response(200, json=result)

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        assert await send_near_transaction(SIGNED, rpc_url=URL, client=client) == result["result"]
    assert len(sent) == 1


async def test_total_attempt_deadline_is_enforced():
    async def transport(request):
        await asyncio.sleep(10)

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        with pytest.raises(NearTransactionError, match="unresolved"):
            await asyncio.wait_for(
                send_near_transaction(
                    SIGNED, rpc_url=URL, client=client, max_attempts=1, timeout=0.01
                ),
                timeout=1,
            )


@pytest.mark.parametrize(
    "value",
    [
        None,
        b"bytes",
        "",
        "not base64",
        SIGNED + "\n",
        base64.b64encode(bytes(65)).decode(),
        base64.b64encode(b"x" * 100).decode(),
    ],
)
async def test_bad_signed_bytes_are_refused_before_io(value):
    def transport(request):
        pytest.fail("invalid input reached RPC")

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        with pytest.raises(ValueError):
            await send_near_transaction(value, rpc_url=URL, client=client)


@pytest.mark.parametrize(
    "opts",
    [
        dict(max_attempts=0),
        dict(max_attempts=4),
        dict(max_attempts=True),
        dict(timeout=0),
        dict(timeout=121),
        dict(timeout=True),
        dict(timeout=float("nan")),
        dict(timeout=None),
        dict(retry_delay=-1),
        dict(retry_delay=6),
        dict(retry_delay=True),
        dict(retry_delay=float("inf")),
        dict(retry_delay=None),
    ],
)
async def test_retry_budget_guards(opts):
    def transport(request):
        pytest.fail("invalid budget reached RPC")

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        with pytest.raises(ValueError):
            await send_near_transaction(SIGNED, rpc_url=URL, client=client, **opts)


async def test_http_refusal_reason_is_not_reported_as_malformed_json():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(403))
    ) as client:
        with pytest.raises(NearTransactionError, match="RPC HTTP refusal"):
            await send_near_transaction(SIGNED, rpc_url=URL, client=client)


@pytest.mark.parametrize(
    "status",
    [
        None,
        {},
        {"SuccessValue": None},
        {"Failure": None},
        {"Failure": {}},
        {"SuccessValue": "", "Failure": {"x": 1}},
        {"SuccessValue": "not base64"},
        {"SuccessValue": "YQ==\n"},
        {"SuccessValue": "YR=="},
        {"SuccessValue": "💥"},
        {"SuccessValue": "", "unexpected": True},
    ],
)
async def test_malformed_terminal_status_stays_uncertain(status):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=outcome(status=status)))
    ) as client:
        with pytest.raises(NearTransactionError, match="unconfirmed RPC outcome") as exc:
            await send_near_transaction(SIGNED, rpc_url=URL, client=client)
        assert exc.value.uncertain


@pytest.mark.parametrize("changes", [dict(jsonrpc="1.0"), dict(error=None)])
async def test_invalid_jsonrpc_envelope_stays_uncertain(changes):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json={**outcome(), **changes}))
    ) as client:
        with pytest.raises(NearTransactionError, match="invalid RPC response"):
            await send_near_transaction(SIGNED, rpc_url=URL, client=client)
