"""Async registration: start, poll, and never confuse a timeout with a failure.

A synchronous register waits on a mint receipt, which on a congested chain
outlives client and proxy timeouts. The timed-out call is genuinely ambiguous —
the mint may well have landed — and retrying it is how five duplicate agents
once got minted. The async flow exists so the caller holds a job id instead of a
guess.

`wait_for_registration` therefore raises on timeout rather than returning the
last non-terminal status: returning `pending` invites a caller to read it as
"did not happen".
"""

from __future__ import annotations

import httpx
import pytest

from uvd_x402_sdk.erc8004 import Erc8004Client, RegisterJobResponse
from uvd_x402_sdk.exceptions import TimeoutError as SdkTimeoutError


def _client(transport: httpx.MockTransport) -> Erc8004Client:
    client = Erc8004Client()
    client._client = httpx.AsyncClient(transport=transport)
    return client


@pytest.mark.asyncio
async def test_async_register_sends_prefer_header_and_returns_job() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(request.headers)
        return httpx.Response(202, json={"jobId": "reg_42", "status": "pending"})

    job = await _client(httpx.MockTransport(handler)).register_agent_async(
        "solana", "https://example.com/agent.json"
    )

    assert seen.get("prefer") == "respond-async"
    assert job.job_id == "reg_42"
    assert job.is_terminal is False
    # The whole point: no agent id yet, and the caller must not invent one.
    assert job.agent_id is None


@pytest.mark.asyncio
async def test_wait_returns_the_terminal_job() -> None:
    responses = [
        httpx.Response(200, json={"jobId": "reg_42", "status": "pending"}),
        httpx.Response(
            200,
            json={
                "jobId": "reg_42",
                "status": "done",
                "network": "solana",
                "agentId": "247Y4QLwz9ZbcuHR2nX2EQLZHCsMs1GTqvgd6fpdn85Q",
                "transaction": "5hBk...",
            },
        ),
    ]

    job = await _client(
        httpx.MockTransport(lambda _: responses.pop(0))
    ).wait_for_registration("reg_42", poll_interval=0.01, timeout=5.0)

    assert job.status == "done"
    assert job.agent_id == "247Y4QLwz9ZbcuHR2nX2EQLZHCsMs1GTqvgd6fpdn85Q"


@pytest.mark.asyncio
async def test_wait_raises_on_timeout_instead_of_returning_pending() -> None:
    transport = httpx.MockTransport(
        lambda _: httpx.Response(200, json={"jobId": "reg_42", "status": "pending"})
    )

    with pytest.raises(SdkTimeoutError) as excinfo:
        await _client(transport).wait_for_registration(
            "reg_42", poll_interval=0.01, timeout=0.05
        )

    # The message must steer away from re-registering.
    assert "rather than registering again" in str(excinfo.value)


@pytest.mark.asyncio
async def test_failed_job_is_terminal_and_carries_the_error() -> None:
    transport = httpx.MockTransport(
        lambda _: httpx.Response(
            200, json={"jobId": "reg_9", "status": "failed", "error": "insufficient gas"}
        )
    )

    job = await _client(transport).wait_for_registration(
        "reg_9", poll_interval=0.01, timeout=5.0
    )

    assert job.status == "failed"
    assert job.is_terminal is True
    assert job.error == "insufficient gas"


def test_mint_confirmed_carries_an_agent_but_is_not_terminal() -> None:
    job = RegisterJobResponse.model_validate(
        {"jobId": "reg_7", "status": "mint_confirmed", "agentId": 17}
    )

    assert job.agent_id == 17
    assert job.is_terminal is False
