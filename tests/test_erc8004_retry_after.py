"""Every ERC-8004 write reports the HTTP status and ``Retry-After`` it got.

The writes of :class:`Erc8004Client` never raise on an HTTP error: they answer
``success=False`` with ``error="Facilitator error: <status> - <body>"``. That
string used to be ALL the caller got, so a service relaying a facilitator
``429`` to its own caller had to regex the status out of it and could not relay
the ``Retry-After`` at all: the header was dropped.

Pinned here, for each of the ten writes, over ``httpx.MockTransport`` (nothing
leaves the process):

1. ``status_code`` is the answer's status and ``retry_after`` its
   ``Retry-After`` in seconds, NOT clamped: in seconds, or as an HTTP-date
   against a frozen clock; absent or unreadable is ``None``.
2. ``error`` is byte-identical to what it was before those fields existed
   (consumers parse it).
3. With no HTTP answer at all (a connection error) both fields are ``None``.
"""

from __future__ import annotations

import os
import time
from collections.abc import Awaitable, Callable, Iterator
from datetime import datetime, timezone
from typing import Any

import httpx
import pytest

from uvd_x402_sdk import erc8004
from uvd_x402_sdk.erc8004 import (
    Erc8004Client,
    FeedbackResponse,
    PrepareRelayFeedbackResponse,
    PrepareSolanaFeedbackResponse,
    RegisterAgentResponse,
)

RATER = "0x" + "11" * 20
SOLANA_RATER = "9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM"
BODY = "rate limited"

Call = Callable[[Erc8004Client], Awaitable[Any]]

#: One call per write of Erc8004Client that catches httpx.HTTPStatusError.
WRITES: dict[str, Call] = {
    "submit_feedback": lambda c: c.submit_feedback("base", 1, 90),
    "prepare_relayed_feedback": lambda c: c.prepare_relayed_feedback("base", 1, RATER, 90),
    "submit_relayed_feedback": lambda c: c.submit_relayed_feedback(
        "base", 1, RATER, 90, deadline=1, nonce="0x01", signature="0x02"
    ),
    "prepare_solana_feedback": lambda c: c.prepare_solana_feedback(
        "solana", "agent", SOLANA_RATER, 90
    ),
    "submit_solana_feedback": lambda c: c.submit_solana_feedback(
        "solana", "agent", SOLANA_RATER, 90, transaction="AQID"
    ),
    "revoke_feedback": lambda c: c.revoke_feedback("base", 1, 1),
    "prepare_relayed_response": lambda c: c.prepare_relayed_response(
        "base", 1, RATER, RATER, 1, "ipfs://response"
    ),
    "submit_relayed_response": lambda c: c.submit_relayed_response(
        "base", 1, RATER, RATER, 1, "ipfs://response",
        deadline=1, nonce="0x01", signature="0x02",
    ),
    "append_response": lambda c: c.append_response("base", 1, 1, "thanks"),
    "register_agent": lambda c: c.register_agent("base", "ipfs://agent"),
}

RESPONSE_TYPES = (
    FeedbackResponse,
    PrepareRelayFeedbackResponse,
    PrepareSolanaFeedbackResponse,
    RegisterAgentResponse,
)

#: 2026-09-29T12:00:00Z, the frozen "now" of the HTTP-date cases.
NOW = datetime(2026, 9, 29, 12, 0, 0, tzinfo=timezone.utc).timestamp()


def test_the_table_covers_every_write_that_catches_an_http_error():
    """A new write that flattens an HTTP error without a row here fails."""
    import inspect

    source = inspect.getsource(Erc8004Client)
    catching = set()
    for name, member in inspect.getmembers(Erc8004Client, inspect.iscoroutinefunction):
        if "except httpx.HTTPStatusError" in inspect.getsource(member):
            catching.add(name)
    assert catching == set(WRITES)
    assert source.count("except httpx.HTTPStatusError") == source.count(
        "_http_error_fields(e)"
    )


def _client(handler: Callable[[httpx.Request], httpx.Response]) -> Erc8004Client:
    client = Erc8004Client(base_url="https://facilitator.example")
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return client


def _answer(status: int, headers: dict[str, str] | None = None, body: str = BODY):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, headers=headers or {}, text=body)

    return handler


@pytest.fixture
def frozen_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(erc8004, "_now", lambda: NOW)


@pytest.mark.parametrize("write", list(WRITES), ids=list(WRITES))
@pytest.mark.parametrize(
    "status, headers, expected_retry_after",
    [
        (429, {"Retry-After": "120"}, 120),
        # A daily cap answers hours: reported as is, never clamped.
        (429, {"Retry-After": "43200"}, 43200),
        (429, {"Retry-After": "Tue, 29 Sep 2026 12:02:00 GMT"}, 120),
        # The shapes x402-rs sends besides 429 (src/handlers.rs:1352, :1741).
        (503, {"Retry-After": "5"}, 5),
        (502, {"Retry-After": "30"}, 30),
        (503, {}, None),
        (400, {}, None),
        (429, {"Retry-After": "soon"}, None),
        (429, {"Retry-After": "-5"}, None),
        (429, {"Retry-After": "1.5"}, None),
        # Past CPython's int() digit limit: unreadable, not an exception.
        (429, {"Retry-After": "9" * 4301}, None),
    ],
    ids=[
        "429-seconds", "429-hours", "429-http-date", "503-seconds",
        "502-seconds", "503-none", "400", "garbage", "negative", "fraction",
        "4301-digits",
    ],
)
async def test_an_http_error_carries_status_and_retry_after(
    write, status, headers, expected_retry_after, frozen_clock
):
    result = await WRITES[write](_client(_answer(status, headers)))

    assert isinstance(result, RESPONSE_TYPES)
    assert result.success is False
    assert result.status_code == status
    assert result.retry_after == expected_retry_after
    # Byte-identical to the string before the fields existed.
    assert result.error == f"Facilitator error: {status} - {BODY}"


@pytest.mark.parametrize("write", list(WRITES), ids=list(WRITES))
async def test_no_http_answer_leaves_both_fields_none(write):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    result = await WRITES[write](_client(handler))

    assert result.success is False
    assert result.status_code is None
    assert result.retry_after is None
    assert result.error == "connection refused"


@pytest.mark.parametrize("write", ["submit_relayed_feedback", "prepare_relayed_feedback"])
async def test_a_success_carries_neither_field(write):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"Retry-After": "120"},
            json={"success": True, "network": "base"},
        )

    result = await WRITES[write](_client(handler))

    assert result.success is True
    assert result.status_code is None
    assert result.retry_after is None


async def test_register_agent_structured_4xx_body_takes_the_http_answers_fields():
    """The 409 body of an in-flight registration is kept, and the status and
    ``Retry-After`` are the HTTP answer's, not whatever the body says."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            409,
            headers={"Retry-After": "7"},
            json={
                "success": True,
                "agentId": 42,
                "error": "registration in flight",
                "network": "base",
                "status_code": 200,
                "retry_after": 1,
            },
        )

    result = await _client(handler).register_agent("base", "ipfs://agent")

    assert result.success is False
    assert result.agent_id == 42
    assert result.error == "registration in flight"
    assert result.status_code == 409
    assert result.retry_after == 7


@pytest.mark.parametrize(
    "value, expected",
    [
        (None, None),
        ("", None),
        ("  ", None),
        ("0", 0),
        (" 120 ", 120),
        ("Tue, 29 Sep 2026 12:02:00 GMT", 120),
        ("Tuesday, 29-Sep-26 12:02:00 GMT", 120),  # RFC 850, obsolete but valid
        ("Tue, 29 Sep 2026 12:00:00 GMT", 0),
        ("Tue, 29 Sep 2026 11:00:00 GMT", 0),  # already past
        ("Tue, 29 Sep 2026 12:00:00 +0100", 0),  # an hour ago
        ("²", None),  # a digit to str.isdigit, not to HTTP
        ("\u0661\u0662\u0660", None),  # "120" in Arabic-Indic digits: a \d, not an HTTP digit
        ("9" * 4301, None),  # past CPython's int() digit limit
        ("0x10", None),
        ("Tue, 32 Sep 2026 12:00:00 GMT", None),
    ],
)
def test_parse_retry_after_header(value, expected, frozen_clock):
    assert erc8004._parse_retry_after_header(value) == expected


def test_an_http_date_rounds_up_against_a_fractional_clock(monkeypatch):
    """119.6 s away is 120, never 119: a caller must not come back early."""
    monkeypatch.setattr(erc8004, "_now", lambda: NOW + 0.4)
    assert erc8004._parse_retry_after_header("Tue, 29 Sep 2026 12:02:00 GMT") == 120


@pytest.fixture
def local_zone_not_utc() -> Iterator[None]:
    """A local zone other than UTC, so a naive date read as local time shows."""
    if not hasattr(time, "tzset"):
        pytest.skip("time.tzset is not available on this platform")
    previous = os.environ.get("TZ")
    os.environ["TZ"] = "America/Bogota"
    time.tzset()
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = previous
        time.tzset()


def test_a_date_with_minus_zero_zone_is_utc_whatever_the_local_zone(
    frozen_clock, local_zone_not_utc
):
    """``-0000`` parses to a naive datetime; it is UTC, not local time."""
    assert erc8004._parse_retry_after_header("Tue, 29 Sep 2026 13:00:00 -0000") == 3600
