"""EM/KK 2026-08-22 — a settle failure must always be able to name itself.

Three integrators independently reported "the approve returns an empty string".
Two defects in `_settle_via_facilitator` produced it:

1. ``result.get("errorReason", result.get("error", "Unknown error"))`` — the
   default of ``dict.get`` does NOT fire when the key is PRESENT and empty,
   and the facilitator sends ``errorReason: ""``.
2. ``str(e)`` on an ``httpx.HTTPStatusError`` is "Client error '400 Bad
   Request' for url ..." and DROPS the body — the only place the facilitator
   says why. On a ``ReadTimeout`` it is the empty string outright.
"""

import httpx
import pytest

from uvd_x402_sdk.advanced_escrow import AdvancedEscrowClient


class _Resp:
    def __init__(self, payload, status_code=200, text=""):
        self._payload = payload
        self.status_code = status_code
        self.text = text or str(payload)

    def json(self):
        return self._payload


@pytest.fixture
def client():
    return AdvancedEscrowClient(
        private_key="0x" + "11" * 32,
        chain_id=8453,
        rpc_url="http://localhost:9",  # never contacted: settle is mocked
    )


def _settle(client, monkeypatch, *, payload=None, raises=None):
    def _post(*a, **k):
        if raises is not None:
            raise raises
        return _Resp(payload)

    monkeypatch.setattr(httpx, "post", _post)
    return client._settle_via_facilitator("release", _pi(), None)


def _pi():
    from uvd_x402_sdk.advanced_escrow import PaymentInfo

    return PaymentInfo(
        operator="0x" + "11" * 20,
        receiver="0x" + "33" * 20,
        token="0x" + "44" * 20,
        max_amount=10_000,
        pre_approval_expiry=1,
        authorization_expiry=2,
        refund_expiry=3,
    )


def test_present_but_empty_error_reason_still_names_something(client, monkeypatch):
    r = _settle(
        client, monkeypatch, payload={"success": False, "errorReason": "", "code": 3}
    )
    assert r.success is False
    assert r.error, "an empty error is exactly the bug"
    assert "refused with no reason" in r.error
    # It must say what DID come back, so the caller has a thread to pull.
    assert "errorReason" in r.error and "code" in r.error


def test_a_real_reason_is_passed_through_untouched(client, monkeypatch):
    r = _settle(
        client, monkeypatch, payload={"success": False, "errorReason": "nonce reused"}
    )
    assert r.error == "nonce reused"


def test_http_status_error_keeps_the_body(client, monkeypatch):
    resp = httpx.Response(
        400,
        text='{"errorReason":"AfterAuthorizationExpiry"}',
        request=httpx.Request("POST", "https://f.example/settle"),
    )
    exc = httpx.HTTPStatusError("Client error '400 Bad Request'", request=resp.request, response=resp)
    r = _settle(client, monkeypatch, raises=exc)
    assert r.success is False
    # The status line alone is not actionable; the body is where the reason is.
    assert "AfterAuthorizationExpiry" in r.error


def test_a_message_less_exception_still_says_something(client, monkeypatch):
    r = _settle(client, monkeypatch, raises=httpx.ReadTimeout(""))
    assert r.success is False
    assert r.error
    assert "ReadTimeout" in r.error


def test_success_is_untouched(client, monkeypatch):
    r = _settle(client, monkeypatch, payload={"success": True, "transaction": "0xabc"})
    assert r.success is True and r.transaction_hash == "0xabc"
