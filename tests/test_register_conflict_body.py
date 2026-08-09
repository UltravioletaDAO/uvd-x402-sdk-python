"""A 409 from POST /register must survive as data, not as a string.

The facilitator's in-flight lock answers a SYNCHRONOUS register with 409 and a
structured RegisterAgentResponse: it carries the agent id and tx of the run
already underway, plus a "poll GET /register/status/{jobId}" hint. Flattening
that into `Facilitator error: 409 - <text>` left the caller with a bare failure,
which is precisely the shape that invites a retry — and retrying a mint is how
duplicate agents get created.
"""

import httpx
import pytest

from uvd_x402_sdk.erc8004 import Erc8004Client


def _client(handler):
    client = Erc8004Client()
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return client


@pytest.mark.asyncio
async def test_409_keeps_the_agent_id_of_the_run_already_in_flight():
    body = {
        "success": False,
        "agentId": 2106,
        "transaction": "0x" + "ab" * 32,
        "owner": "0x1234567890123456789012345678901234567890",
        "error": (
            "A registration for this agent is already in progress; retry later "
            "or poll GET /register/status/{jobId}"
        ),
        "network": "base-mainnet",
    }
    result = await _client(lambda r: httpx.Response(409, json=body)).register_agent(
        "base-mainnet", "https://example.com/agent.json"
    )

    assert result.success is False
    assert result.agent_id == 2106, "the id the facilitator handed back was dropped"
    assert result.transaction == body["transaction"]
    assert "already in progress" in result.error


@pytest.mark.asyncio
async def test_a_4xx_body_can_never_claim_success():
    body = {"success": True, "agentId": 1, "network": "base-mainnet"}
    result = await _client(lambda r: httpx.Response(409, json=body)).register_agent(
        "base-mainnet", "https://example.com/agent.json"
    )

    assert result.success is False
    assert result.error  # something explanatory, never empty


@pytest.mark.asyncio
async def test_400_surfaces_the_facilitator_message_not_raw_text():
    body = {"success": False, "error": "invalid agentUri", "network": "base-mainnet"}
    result = await _client(lambda r: httpx.Response(400, json=body)).register_agent(
        "base-mainnet", "not-a-uri"
    )

    assert result.success is False
    assert result.error == "invalid agentUri"


@pytest.mark.asyncio
async def test_non_json_error_still_degrades_to_the_flattened_string():
    result = await _client(
        lambda r: httpx.Response(502, text="upstream down")
    ).register_agent("base-mainnet", "https://example.com/agent.json")

    assert result.success is False
    assert "502" in result.error
    assert "upstream down" in result.error
