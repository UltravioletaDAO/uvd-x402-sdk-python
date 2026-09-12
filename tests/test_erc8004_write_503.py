"""An ERC-8004 WRITE that reached no verdict must not read as a refusal.

These routes return a response object instead of raising, so a 503 from the EVM
writer lease used to arrive as `success=False` with a string -- byte-identical in
shape to a 400 "your feedback is invalid". The two demand opposite recoveries.

On `register_agent` the difference is not cosmetic. Reading a 503 as
"registration failed" and registering again is exactly the sequence that once
minted five duplicate agents.
"""

import httpx
import pytest

from uvd_x402_sdk.erc8004 import Erc8004Client
from uvd_x402_sdk.exceptions import (
    LookupInconclusiveError,
    RegistrationPendingError,
    WriterUnavailableError,
)

LEASE_503 = {"error": "writer lease unavailable", "reason": "holder_unknown"}
AMBIGUOUS_503 = {"error": "forward failed", "reason": "forward_failed"}


def _client(handler):
    client = Erc8004Client()
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return client


# ---------------------------------------------------------------------------
# feedback
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_feedback_503_is_marked_retryable_and_names_the_reason():
    result = await _client(
        lambda r: httpx.Response(503, json=LEASE_503, headers={"Retry-After": "5"})
    ).submit_feedback("base", 1, value=90, score=90)

    assert result.success is False
    assert result.retryable is True, "a 503 is no verdict, not a refusal"
    assert result.reason == "holder_unknown"
    assert result.retry_after == 5.0
    assert result.safe_to_retry is True


@pytest.mark.asyncio
async def test_feedback_400_stays_final():
    """The refinement must not turn decisions into no-verdicts."""
    result = await _client(
        lambda r: httpx.Response(400, json={"error": "score out of range"})
    ).submit_feedback("base", 1, value=90, score=90)

    assert result.success is False
    assert result.retryable is False
    assert result.safe_to_retry is False


@pytest.mark.asyncio
async def test_feedback_retry_after_is_clamped():
    """A misconfigured facilitator cannot park a caller for an hour."""
    from uvd_x402_sdk.exceptions import MAX_RETRY_AFTER_SECONDS

    result = await _client(
        lambda r: httpx.Response(503, json=LEASE_503, headers={"Retry-After": "3600"})
    ).submit_feedback("base", 1, value=90, score=90)

    assert result.retry_after == MAX_RETRY_AFTER_SECONDS


# ---------------------------------------------------------------------------
# register -- the duplicate-mint path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_register_503_never_ran_is_safe_to_resend():
    result = await _client(
        lambda r: httpx.Response(503, json=LEASE_503)
    ).register_agent("base", "https://example.com/agent.json")

    assert result.success is False
    assert result.retryable is True
    assert result.safe_to_retry is True, "returned before the holder was touched"


@pytest.mark.asyncio
async def test_register_503_forward_failed_is_never_safe_to_resend():
    """The holder may have MINTED and only the reply was lost.

    Resolve with get_identity_by_owner() -- re-POSTing is what produced five
    duplicate agents.
    """
    result = await _client(
        lambda r: httpx.Response(503, json=AMBIGUOUS_503)
    ).register_agent("base", "https://example.com/agent.json", recipient="0x" + "11" * 20)

    assert result.retryable is True
    assert result.safe_to_retry is False
    assert result.reason == "forward_failed"


@pytest.mark.asyncio
async def test_register_transport_failure_is_transient_and_ambiguous():
    """A read timeout is `forward_failed` by another route: the mint may have landed."""

    def boom(request):
        raise httpx.ReadTimeout("timed out", request=request)

    result = await _client(boom).register_agent("base", "https://example.com/agent.json")

    assert result.success is False
    assert result.retryable is True
    assert result.safe_to_retry is False


@pytest.mark.asyncio
async def test_register_409_conflict_is_still_final():
    """An in-flight lock is a decision, and its body must survive (regression)."""
    body = {"success": False, "agentId": 2106, "error": "already in progress", "network": "base"}
    result = await _client(lambda r: httpx.Response(409, json=body)).register_agent(
        "base", "https://example.com/agent.json"
    )

    assert result.agent_id == 2106
    assert result.retryable is False
    assert result.safe_to_retry is False


# ---------------------------------------------------------------------------
# async register transport
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_async_register_503_is_named_not_a_bare_http_error():
    """Nothing was created, so there is no job to poll -- but the request was
    never refused either. A named exception says which."""
    with pytest.raises(WriterUnavailableError) as excinfo:
        await _client(
            lambda r: httpx.Response(503, json=LEASE_503, headers={"Retry-After": "5"})
        ).register_agent_async("base", "https://example.com/agent.json")

    assert excinfo.value.reason == "holder_unknown"
    assert excinfo.value.safe_to_retry is True
    assert excinfo.value.retry_after == 5.0


@pytest.mark.asyncio
async def test_register_status_503_is_inconclusive_not_gone():
    """404 is "the job is unknown". 503 is "I could not tell" -- and a caller
    that collapses them registers a second agent for a job still running."""
    with pytest.raises(LookupInconclusiveError):
        await _client(lambda r: httpx.Response(503, json=LEASE_503)).get_register_status("job-1")


@pytest.mark.asyncio
async def test_a_flaky_status_poll_does_not_abort_the_wait():
    """One inconclusive poll must not surface as an exception on a live job.

    Escaping here hands the caller an error for a registration that is very
    likely still running, and the reflex answer to that is to register again.
    """
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(503, json=LEASE_503)
        return httpx.Response(
            200,
            json={"jobId": "job-1", "status": "done", "agentId": 77, "network": "base"},
        )

    job = await _client(handler).wait_for_registration("job-1", poll_interval=0, timeout=10)

    assert calls["n"] == 2
    assert job.status == "done"
    assert job.agent_id == 77


@pytest.mark.asyncio
async def test_a_permanently_inconclusive_poll_ends_as_pending_not_as_failure():
    """Still not a failure: the job id is what the caller needs to resume."""
    with pytest.raises(RegistrationPendingError) as excinfo:
        await _client(
            lambda r: httpx.Response(503, json=LEASE_503)
        ).wait_for_registration("job-9", poll_interval=0, timeout=0)

    assert excinfo.value.job_id == "job-9"
    assert excinfo.value.retryable is True
