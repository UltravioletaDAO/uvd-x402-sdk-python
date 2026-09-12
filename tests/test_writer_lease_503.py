"""A 503 is NOT a rejection, at every border the SDK owns.

The rule these tests defend:

    402 means "the payment was REJECTED, sign a new authorization".
    503 means "NO VERDICT was reached, present the SAME credential again".

Collapsing the second into the first makes a buyer pay twice for a payment
nobody ever refused. On the ERC-8004 mint path the same collapse produces a
duplicate agent, which is how five of them once got minted.

Every test here fails against the pre-fix code: the writer-lease exception was
defined but never raised, the ERC-8004 write routes flattened a 503 into a
string, and the escrow path parsed the body before looking at the status.
"""

import json

import httpx
import pytest

from uvd_x402_sdk.client import (
    facilitator_http_error,
    is_transient_error,
    retry_after_seconds,
    transient_503_response,
)
from uvd_x402_sdk.exceptions import (
    MAX_RETRY_AFTER_SECONDS,
    FacilitatorError,
    WriterUnavailableError,
    parse_retry_after,
    write_retry_is_safe,
)


def _response(status: int, body, headers=None) -> httpx.Response:
    """A real httpx.Response, so header casing and .json() behave for real."""
    if isinstance(body, (dict, list)):
        return httpx.Response(status, json=body, headers=headers or {})
    return httpx.Response(status, content=body, headers=headers or {})


LEASE_503 = {"error": "writer lease unavailable", "reason": "holder_unknown"}
AMBIGUOUS_503 = {"error": "forward to holder failed", "reason": "forward_failed"}


# ---------------------------------------------------------------------------
# The exception itself
# ---------------------------------------------------------------------------


def test_writer_unavailable_is_catchable_as_facilitator_error():
    """Every consumer written before this class existed catches FacilitatorError.

    Making the 503 a SIBLING of FacilitatorError would have turned "the SDK now
    names this failure" into "the SDK now escapes your handler" -- a worse bug
    than the one being fixed.
    """
    exc = WriterUnavailableError("no verdict", 503, json.dumps(LEASE_503), reason="holder_unknown")
    assert isinstance(exc, FacilitatorError)
    assert exc.status_code == 503
    assert exc.code == "WRITER_UNAVAILABLE"


def test_a_503_from_verify_or_settle_is_named_not_flattened():
    """The class existed but nothing raised it; a 503 arrived as a generic error."""
    exc = facilitator_http_error("settle failed", _response(503, LEASE_503, {"Retry-After": "5"}))
    assert isinstance(exc, WriterUnavailableError)
    assert exc.reason == "holder_unknown"
    assert exc.retry_after == 5.0
    assert exc.safe_to_retry is True
    assert exc.retryable is True


def test_a_400_is_still_a_plain_facilitator_error():
    """The refinement must not reclassify decisions as no-verdicts."""
    exc = facilitator_http_error("verify failed", _response(400, {"error": "bad signature"}))
    assert type(exc) is FacilitatorError
    assert exc.retryable is False
    assert is_transient_error(exc) is False


def test_forward_failed_is_transient_but_never_safe_to_retry():
    """The forward was ATTEMPTED. The holder may have executed the write."""
    exc = facilitator_http_error("settle failed", _response(503, AMBIGUOUS_503))
    assert exc.retryable is True          # do not answer 402
    assert exc.safe_to_retry is False     # but do not blind-resend a mint either


def test_an_unknown_reason_is_ambiguous_by_construction():
    """A reason this SDK has never heard of must not be guessed optimistically."""
    exc = facilitator_http_error("settle failed", _response(503, {"reason": "brand_new_thing"}))
    assert exc.reason == "brand_new_thing"   # carried through untouched
    assert exc.safe_to_retry is False
    assert write_retry_is_safe("brand_new_thing") is False


def test_a_503_without_a_reason_is_still_no_verdict():
    """An ALB replacing a task answers 503 with HTML and no reason field."""
    exc = facilitator_http_error("settle failed", _response(503, b"<html>503</html>"))
    assert isinstance(exc, WriterUnavailableError)
    assert exc.reason is None
    assert exc.safe_to_retry is False
    assert is_transient_error(exc) is True


# ---------------------------------------------------------------------------
# Retry-After has a ceiling
# ---------------------------------------------------------------------------


def test_retry_after_is_clamped_so_a_bad_facilitator_cannot_park_a_request():
    """`Retry-After: 3600` gets to say "later", not to hold a request for an hour."""
    exc = facilitator_http_error(
        "settle failed", _response(503, LEASE_503, {"Retry-After": "3600"})
    )
    assert exc.retry_after == MAX_RETRY_AFTER_SECONDS
    assert retry_after_seconds(exc) == MAX_RETRY_AFTER_SECONDS

    _, headers = transient_503_response(exc)
    assert float(headers["Retry-After"]) <= MAX_RETRY_AFTER_SECONDS


def test_a_hand_built_error_cannot_smuggle_an_unclamped_retry_after():
    """Clamped in the constructor, not only at the header parse site."""
    exc = FacilitatorError("x", 503, "{}", retry_after=99999)
    assert exc.retry_after == MAX_RETRY_AFTER_SECONDS
    assert exc.details["retryAfter"] == MAX_RETRY_AFTER_SECONDS


@pytest.mark.parametrize(
    "value", [None, "", "  ", "later", "0", "-3", "Wed, 21 Oct 2015 07:28:00 GMT"]
)
def test_unusable_retry_after_values_become_none_not_an_exception(value):
    assert parse_retry_after(value) is None


# ---------------------------------------------------------------------------
# What a paywall answers
# ---------------------------------------------------------------------------


def test_the_503_body_tells_a_server_whether_a_resend_is_safe():
    exc = facilitator_http_error(
        "settle failed", _response(503, AMBIGUOUS_503, {"Retry-After": "5"})
    )
    body, headers = transient_503_response(exc)

    assert body["retryable"] is True
    assert body["reason"] == "forward_failed"
    assert body["safeToRetry"] is False
    assert headers["Retry-After"] == "5"


def test_a_5xx_carrying_a_tx_hash_is_not_retryable():
    """The facilitator can fail AFTER broadcasting. Retrying risks a double-settle."""
    exc = facilitator_http_error(
        "settle failed", _response(500, {"transaction": {"hash": "0x" + "ab" * 32}})
    )
    assert is_transient_error(exc) is False


def test_a_503_carrying_a_tx_hash_is_not_retryable_either():
    """The anti-double-settle guard must survive the WriterUnavailableError refinement."""
    exc = facilitator_http_error(
        "settle failed",
        _response(503, {"reason": "forward_failed", "txHash": "0x" + "cd" * 32}),
    )
    assert isinstance(exc, WriterUnavailableError)
    assert is_transient_error(exc) is False


# ---------------------------------------------------------------------------
# The paywall borders. A middleware that answers 402 here charges twice.
# ---------------------------------------------------------------------------


def test_a_transient_failure_never_reaches_the_buyer_as_402_fastapi():
    """The FastAPI dependency turns X402Error into an HTTPException.

    While that was unconditionally 402, a facilitator with no writer lease told
    every buyer their payment had been REJECTED. They sign a new authorization
    and pay a second time for a purchase nobody refused.
    """
    fastapi = pytest.importorskip("fastapi")

    from uvd_x402_sdk.integrations.fastapi_integration import X402Depends

    dep = X402Depends.__new__(X402Depends)
    dep._amount = 1
    dep._client = _RaisingClient(
        facilitator_http_error("settle failed", _response(503, LEASE_503, {"Retry-After": "5"}))
    )

    with pytest.raises(fastapi.HTTPException) as excinfo:
        _run(dep(_FakeRequest()))

    assert excinfo.value.status_code == 503
    assert excinfo.value.detail["retryable"] is True
    assert excinfo.value.detail["reason"] == "holder_unknown"
    assert excinfo.value.headers["Retry-After"] == "5"


def test_a_real_rejection_is_still_402_fastapi():
    """The pair. A 400 from the facilitator IS a rejection and must stay 402."""
    fastapi = pytest.importorskip("fastapi")

    from uvd_x402_sdk.integrations.fastapi_integration import X402Depends

    dep = X402Depends.__new__(X402Depends)
    dep._amount = 1
    dep._client = _RaisingClient(
        facilitator_http_error("verify failed", _response(400, {"error": "bad signature"}))
    )

    with pytest.raises(fastapi.HTTPException) as excinfo:
        _run(dep(_FakeRequest()))

    assert excinfo.value.status_code == 402


def test_the_lambda_border_answers_503_not_402():
    """Same rule where there is no framework at all."""
    from uvd_x402_sdk.integrations.lambda_integration import LambdaX402

    from uvd_x402_sdk.config import X402Config

    handler = LambdaX402.__new__(LambdaX402)
    handler._client = _RaisingClient(
        facilitator_http_error("settle failed", _response(503, AMBIGUOUS_503))
    )
    handler._config = X402Config(recipient_evm="0x" + "11" * 20)

    response = handler.process_or_require(
        {"headers": {"X-PAYMENT": "irrelevant"}}, amount_usd=1
    )

    assert response["statusCode"] == 503
    body = response["body"]
    if isinstance(body, str):
        body = json.loads(body)
    assert body["retryable"] is True
    assert body["safeToRetry"] is False, "forward_failed may already have settled"


class _RaisingClient:
    """A stand-in X402Client whose payment path always raises one error."""

    def __init__(self, exc):
        self.exc = exc

    def process_payment(self, *args, **kwargs):
        raise self.exc


class _FakeRequest:
    headers = {"X-PAYMENT": "irrelevant"}


def _run(awaitable):
    import asyncio

    return asyncio.run(awaitable) if hasattr(awaitable, "__await__") else awaitable
