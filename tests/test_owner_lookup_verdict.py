"""A 503 from the owner lookup must never be readable as "owns nothing".

The facilitator answers 404 for "this address owns no agent" and 503 for "I
could not find out" — usually an RPC failure behind it. Collapsing the two is
how a transient failure becomes a permanent wrong answer: the caller persists
"not registered", stops asking, and on a registration path mints a second agent
for an owner who already has one, burning gas and leaving an orphan.

The SDK used to call `raise_for_status()` for both, so the only way to tell them
apart was inspecting the status on the raised error — easy to skip, and its
docstring documented the 404 alone.
"""

from __future__ import annotations

import httpx
import pytest

from uvd_x402_sdk.erc8004 import Erc8004Client
from uvd_x402_sdk.exceptions import LookupInconclusiveError

_OWNER = "6xNPewUdKRbEZDReQdpyfNUdgNg8QRc8Mt263T5GZSRv"
_AGENT = "247Y4QLwz9ZbcuHR2nX2EQLZHCsMs1GTqvgd6fpdn85Q"


def _client(handler: httpx.MockTransport) -> Erc8004Client:
    client = Erc8004Client()
    client._client = httpx.AsyncClient(transport=handler)
    return client


@pytest.mark.asyncio
async def test_503_raises_retryable_and_not_a_not_found() -> None:
    transport = httpx.MockTransport(
        lambda _: httpx.Response(
            503, json={"error": "Could not determine agent ID", "retryable": True}
        )
    )

    with pytest.raises(LookupInconclusiveError) as excinfo:
        await _client(transport).get_identity_by_owner("solana", _OWNER)

    assert excinfo.value.status_code == 503
    assert excinfo.value.retryable is True
    # Must not be reachable by code catching only the "no agent" case.
    assert not isinstance(excinfo.value, httpx.HTTPStatusError)


@pytest.mark.asyncio
async def test_404_stays_a_plain_http_error() -> None:
    transport = httpx.MockTransport(
        lambda _: httpx.Response(404, json={"error": "does not own any agent"})
    )

    with pytest.raises(httpx.HTTPStatusError) as excinfo:
        await _client(transport).get_identity_by_owner("solana", _OWNER)

    assert excinfo.value.response.status_code == 404


@pytest.mark.asyncio
async def test_solana_success_parses() -> None:
    # Shape captured from mainnet on facilitator v1.72.0.
    transport = httpx.MockTransport(
        lambda _: httpx.Response(
            200,
            json={
                "agentId": _AGENT,
                "owner": _OWNER,
                "agentUri": "https://example.com/agent.json",
                "network": "solana",
                "balance": "1",
            },
        )
    )

    result = await _client(transport).get_identity_by_owner("solana", _OWNER)

    assert result.agent_id == _AGENT
    assert result.balance == "1"
