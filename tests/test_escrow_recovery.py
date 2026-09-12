"""An expired escrow is recoverable, and the SDK used to say it was not.

The false claim (in `build_payment_info`'s docstring, and repeated in this
suite's own test docstring) was that funds past `authorizationExpiry` "can only
be moved by the payer's reclaim()". Read against the contract:

    partialVoid(...) external nonReentrant
        onlySender(paymentInfo.operator)   <- the FACILITATOR, not the payer
        validAmount(amount)
    ... _sendTokens(operator, token, paymentInfo.payer, amount);
                                    ^ goes to the payer
    -- and no authorizationExpiry check anywhere in the function.
    (AuthCaptureEstrow.sol:336-354 in x402-rs/contracts/lib/commerce-payments)

`reclaim()` IS `onlySender(payer)` and post-expiry only (:361), which is why the
facilitator does not expose it -- but it is a second door, not the only one.
Believing otherwise leaves money in escrows whose payer never comes back.

These tests pin the reachable recovery: `refundInEscrow` -> `partialVoid`, for
exactly `capturableAmount`, gaslessly, past expiry.
"""

import httpx

from uvd_x402_sdk.advanced_escrow import AdvancedEscrowClient, PaymentInfo, TransactionResult


def _client(monkeypatch, post_handler):
    c = AdvancedEscrowClient.__new__(AdvancedEscrowClient)
    c.contracts = {
        "operator": "0x" + "11" * 20,
        "usdc": "0x" + "22" * 20,
        "escrow": "0x" + "33" * 20,
        "token_collector": "0x" + "44" * 20,
    }
    c.facilitator_url = "https://facilitator.example"
    c.payer = "0x" + "55" * 20
    c.chain_id = 8453
    c._is_create3 = False
    monkeypatch.setattr("uvd_x402_sdk.advanced_escrow.httpx.post", post_handler)
    return c


def _expired_payment_info() -> PaymentInfo:
    """A PaymentInfo whose authorizationExpiry is firmly in the past."""
    return PaymentInfo(
        operator="0x" + "11" * 20,
        receiver="0x" + "66" * 20,
        token="0x" + "22" * 20,
        max_amount=5_000_000,
        pre_approval_expiry=1,
        authorization_expiry=2,      # long gone
        refund_expiry=3,
        min_fee_bps=0,
        max_fee_bps=800,
        fee_receiver="0x" + "00" * 20,
        salt="0x" + "77" * 32,
    )


# ---------------------------------------------------------------------------
# B -- the expired escrow comes back without the payer
# ---------------------------------------------------------------------------


def test_refund_all_asks_for_exactly_what_is_left_not_max_amount(monkeypatch):
    """`partialVoid` reverts with PartialVoidExceedsCapturable above capturable.

    Refunding `max_amount` blind is therefore how a partially-captured escrow
    fails to be recovered at all.
    """
    seen = {}

    def post(url, json=None, timeout=None):
        if url.endswith("/escrow/state"):
            return httpx.Response(200, json={"capturableAmount": "1500000"})
        seen["payload"] = json
        return httpx.Response(200, json={"success": True, "transaction": "0x" + "ab" * 32})

    result = _client(monkeypatch, post).refund_all_via_facilitator(_expired_payment_info())

    assert result.success is True
    assert seen["payload"]["action"] == "refundInEscrow"
    assert seen["payload"]["payload"]["amount"] == "1500000"
    assert seen["payload"]["payload"]["amount"] != "5000000"


def test_expiry_does_not_block_the_refund_path(monkeypatch):
    """The recovery is reachable on a PaymentInfo that is already past expiry.

    `partialVoid` checks no expiry; the SDK must not invent one.
    """
    def post(url, json=None, timeout=None):
        if url.endswith("/escrow/state"):
            return httpx.Response(200, json={"capturableAmount": "5000000"})
        return httpx.Response(200, json={"success": True, "transaction": "0x" + "cd" * 32})

    pi = _expired_payment_info()
    assert pi.authorization_expiry < 1_000_000  # unambiguously expired

    result = _client(monkeypatch, post).refund_all_via_facilitator(pi)
    assert result.success is True
    assert result.transaction_hash == "0x" + "cd" * 32


def test_an_already_empty_escrow_is_not_reported_as_a_failure(monkeypatch):
    """`validAmount` rejects zero. "Nothing left to refund" is not a problem."""
    calls = {"settle": 0}

    def post(url, json=None, timeout=None):
        if url.endswith("/escrow/state"):
            return httpx.Response(200, json={"capturableAmount": "0"})
        calls["settle"] += 1
        return httpx.Response(200, json={"success": True})

    result = _client(monkeypatch, post).refund_all_via_facilitator(_expired_payment_info())

    assert result.success is True
    assert result.transaction_hash is None
    assert calls["settle"] == 0, "a zero-amount partialVoid would just revert"


def test_the_docstrings_no_longer_claim_reclaim_is_the_only_way_out():
    """The false statement cost real money; it must not come back.

    Pinned as a test because it is a CLAIM, and a claim regresses as easily as
    code -- this one survived three releases inside a docstring nobody re-read.
    """
    import inspect

    from uvd_x402_sdk import advanced_escrow

    surfaces = [
        advanced_escrow.__doc__ or "",
        inspect.getdoc(advanced_escrow.AdvancedEscrowClient.build_payment_info) or "",
        inspect.getdoc(advanced_escrow.AdvancedEscrowClient.refund_via_facilitator) or "",
        inspect.getdoc(advanced_escrow.AdvancedEscrowClient.refund_in_escrow) or "",
    ]
    joined = " ".join(surfaces).lower()

    assert "only the payer" not in joined
    assert "can only be moved by the payer" not in joined
    # and the true path is named where an operator will look for it
    assert "partialvoid" in joined
    assert "refund_all_via_facilitator" in joined


# ---------------------------------------------------------------------------
# A -- the escrow path must not read a no-verdict as a refusal
# ---------------------------------------------------------------------------


def test_a_503_on_the_escrow_settle_is_transient_not_a_refusal(monkeypatch):
    def post(url, json=None, timeout=None):
        return httpx.Response(
            503,
            json={"error": "writer lease unavailable", "reason": "holder_unknown"},
            headers={"Retry-After": "5"},
        )

    result = _client(monkeypatch, post).refund_via_facilitator(_expired_payment_info())

    assert result.success is False
    assert result.retryable is True
    assert result.reason == "holder_unknown"
    assert result.retry_after == 5.0
    assert result.safe_to_retry is True


def test_an_html_503_no_longer_surfaces_as_a_json_parse_error(monkeypatch):
    """An ALB replacing a task answers HTML.

    `response.json()` used to blow up inside the try and reach the caller as
    `error="Expecting value: line 1 column 1"` -- a no-verdict wearing the
    costume of a malformed reply.
    """
    def post(url, json=None, timeout=None):
        return httpx.Response(503, content=b"<html>503 Service Unavailable</html>")

    result = _client(monkeypatch, post).release_via_facilitator(_expired_payment_info())

    assert result.success is False
    assert result.retryable is True
    assert "Expecting value" not in (result.error or "")
    assert "no verdict" in (result.error or "")


def test_a_400_on_the_escrow_settle_stays_final(monkeypatch):
    def post(url, json=None, timeout=None):
        return httpx.Response(400, json={"success": False, "errorReason": "bad payment info"})

    result = _client(monkeypatch, post).release_via_facilitator(_expired_payment_info())

    assert result.success is False
    assert result.retryable is False
    assert result.error == "bad payment info"


def test_a_transport_failure_is_transient_but_never_safe_to_retry(monkeypatch):
    """The facilitator may have submitted the transaction and lost the reply."""
    def post(url, json=None, timeout=None):
        raise httpx.ReadTimeout("timed out")

    result = _client(monkeypatch, post).release_via_facilitator(_expired_payment_info())

    assert result.success is False
    assert result.retryable is True
    assert result.safe_to_retry is False


def test_transaction_result_defaults_keep_the_on_chain_paths_final():
    """Every on-chain path builds one of these without the new fields."""
    r = TransactionResult(success=False, error="reverted")
    assert r.retryable is False
    assert r.safe_to_retry is False
    assert r.reason is None
    assert r.retry_after is None
