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
from uvd_x402_sdk.exceptions import RegistrationPendingError


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

    with pytest.raises(RegistrationPendingError) as excinfo:
        await _client(transport).wait_for_registration(
            "reg_42", poll_interval=0.01, timeout=0.05
        )

    # The job id must be reachable as an attribute. Buried in the message, a
    # caller cannot resume polling without parsing a string, and the fallback
    # they reach for instead is re-registering.
    assert excinfo.value.job_id == "reg_42"
    assert excinfo.value.last_status == "pending"
    assert excinfo.value.retryable is True
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


@pytest.mark.asyncio
async def test_async_transport_returns_the_same_shape_as_sync() -> None:
    """The migration must not force callers to rewrite anything downstream.

    ``use_async_transport=True`` changes how the wait is carried out, not what
    comes back: still a ``RegisterAgentResponse`` with ``agent_id``.
    """
    responses = [
        httpx.Response(202, json={"jobId": "reg_42", "status": "pending"}),
        httpx.Response(
            200,
            json={
                "jobId": "reg_42",
                "status": "done",
                "network": "solana",
                "agentId": "247Y4QLwz9ZbcuHR2nX2EQLZHCsMs1GTqvgd6fpdn85Q",
                "transaction": "4jz6...",
                "transferTransaction": "27Vv...",
                "owner": "6xNPewUdKRbEZDReQdpyfNUdgNg8QRc8Mt263T5GZSRv",
            },
        ),
    ]

    result = await _client(
        httpx.MockTransport(lambda _: responses.pop(0))
    ).register_agent(
        "solana",
        "https://example.com/agent.json",
        use_async_transport=True,
        poll_interval=0.01,
    )

    assert result.success is True
    assert result.agent_id == "247Y4QLwz9ZbcuHR2nX2EQLZHCsMs1GTqvgd6fpdn85Q"
    assert result.transaction == "4jz6..."
    assert result.transfer_transaction == "27Vv..."
    assert result.owner == "6xNPewUdKRbEZDReQdpyfNUdgNg8QRc8Mt263T5GZSRv"
    assert result.network == "solana"


@pytest.mark.asyncio
async def test_async_transport_maps_a_failed_job_to_an_unsuccessful_response() -> None:
    responses = [
        httpx.Response(202, json={"jobId": "reg_9", "status": "pending"}),
        httpx.Response(
            200,
            json={
                "jobId": "reg_9",
                "status": "failed",
                "network": "base",
                "error": "insufficient gas",
            },
        ),
    ]

    result = await _client(
        httpx.MockTransport(lambda _: responses.pop(0))
    ).register_agent(
        "base-mainnet", "ipfs://Qm", use_async_transport=True, poll_interval=0.01
    )

    assert result.success is False
    assert result.error == "insufficient gas"
    assert result.agent_id is None


@pytest.mark.asyncio
async def test_async_transport_timeout_surfaces_the_job_id_not_a_failure() -> None:
    """A timeout must not be flattened into ``success=False``.

    Returning an unsuccessful response here would tell the caller the
    registration did not happen, when the mint may well be about to land — the
    exact conflation that leads to minting a duplicate.
    """
    responses = [httpx.Response(202, json={"jobId": "reg_7", "status": "pending"})]

    def handler(request: httpx.Request) -> httpx.Response:
        if responses:
            return responses.pop(0)
        return httpx.Response(200, json={"jobId": "reg_7", "status": "pending"})

    with pytest.raises(RegistrationPendingError) as excinfo:
        await _client(httpx.MockTransport(handler)).register_agent(
            "solana",
            "https://example.com/agent.json",
            use_async_transport=True,
            poll_interval=0.01,
            async_timeout=0.05,
        )

    assert excinfo.value.job_id == "reg_7"
