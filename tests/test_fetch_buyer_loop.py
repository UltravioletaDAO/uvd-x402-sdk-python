"""Buyer loop: X402Client.fetch() pays a 402 and retries.

The seller side of x402 was already in the SDK (verify/settle) and so was the
payer-side signer (create_authorization). What was missing was the loop that
ties them together on the buyer's side: request -> 402 -> sign -> retry. These
tests exercise that loop against a mocked httpx transport, with a real local
key doing the signing, so the X-PAYMENT header is genuinely produced.
"""

import base64
import json
from decimal import Decimal

import httpx
import pytest

from uvd_x402_sdk.client import X402Client
from uvd_x402_sdk.exceptions import (
    PaymentExceedsMaxError,
    NoAcceptablePaymentError,
)

# Anvil test key #0 — public, for tests only, never a real wallet.
TEST_KEY = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
USDC_BASE = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
SELLER = "0x000000000000000000000000000000000000dEaD"


BUYER_CFG = {"recipient_evm": SELLER, "verify_facilitator_support": False}


def _client():
    # A buyer still has to satisfy X402Config's recipient requirement today
    # (it was built for sellers); passing SELLER is a placeholder. Relaxing that
    # for buyer-only use is a follow-up noted in the PR.
    c = X402Client(**BUYER_CFG)
    c.connect_with_private_key(TEST_KEY, chain_name="base")
    return c


def _402_body_v1(amount_base="10000"):  # 10000 base units = 0.01 USDC (6 dp)
    return {
        "x402Version": 1,
        "accepts": [
            {
                "scheme": "exact",
                "network": "base",
                "maxAmountRequired": amount_base,
                "resource": "https://api.example.com/data",
                "description": "test resource",
                "payTo": SELLER,
                "asset": USDC_BASE,
            }
        ],
    }


def _transport(handler):
    return httpx.MockTransport(handler)


def test_pays_a_402_and_retries_with_x_payment():
    """A 402 is answered by signing an X-PAYMENT and retrying; the retry's
    header is a real base64 x402 payload paying the advertised seller."""
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        header = request.headers.get("X-PAYMENT")
        if header is None:
            return httpx.Response(402, json=_402_body_v1())
        seen["header"] = header
        return httpx.Response(200, json={"ok": True})

    c = _client()
    with httpx.Client(transport=_transport(handler)) as http:
        resp = c.fetch("https://api.example.com/data", max_amount="0.05", http_client=http)

    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    # The retry carried a real, decodable x402 payload paying the seller.
    payload = json.loads(base64.b64decode(seen["header"]))
    assert payload["payload"]["authorization"]["to"].lower() == SELLER.lower()
    assert payload["payload"]["authorization"]["value"] == "10000"


def test_max_amount_is_a_hard_ceiling():
    """A price above max_amount raises instead of silently signing."""
    def handler(request):
        return httpx.Response(402, json=_402_body_v1(amount_base="10000000"))  # 10 USDC

    c = _client()
    with httpx.Client(transport=_transport(handler)) as http:
        with pytest.raises(PaymentExceedsMaxError):
            c.fetch("https://api.example.com/data", max_amount="0.05", http_client=http)


def test_non_402_passes_through_untouched():
    """A first-try 200 is returned as-is; fetch only pays when asked."""
    def handler(request):
        assert "X-PAYMENT" not in request.headers  # never paid
        return httpx.Response(200, json={"free": True})

    c = _client()
    with httpx.Client(transport=_transport(handler)) as http:
        resp = c.fetch("https://api.example.com/free", max_amount="0.05", http_client=http)
    assert resp.json() == {"free": True}


def test_no_option_within_ceiling_raises_no_acceptable():
    """If every option is above the ceiling, the loop refuses (does not pay)."""
    def handler(request):
        return httpx.Response(402, json=_402_body_v1(amount_base="5000000"))  # 5 USDC

    c = _client()
    with httpx.Client(transport=_transport(handler)) as http:
        with pytest.raises((NoAcceptablePaymentError, PaymentExceedsMaxError)):
            c.fetch("https://api.example.com/data", max_amount="0.01", http_client=http)


def test_selects_cheapest_option_by_default():
    """Given several options, the default selector picks the cheapest one."""
    body = {
        "x402Version": 1,
        "accepts": [
            {"network": "base", "maxAmountRequired": "50000", "payTo": SELLER,
             "asset": USDC_BASE, "resource": "r", "description": "d"},
            {"network": "base", "maxAmountRequired": "10000", "payTo": SELLER,
             "asset": USDC_BASE, "resource": "r", "description": "d"},
        ],
    }

    def handler(request):
        header = request.headers.get("X-PAYMENT")
        if header is None:
            return httpx.Response(402, json=body)
        payload = json.loads(base64.b64decode(header))
        # Cheapest = 10000 base units.
        assert payload["payload"]["authorization"]["value"] == "10000"
        return httpx.Response(200, json={"ok": True})

    c = _client()
    with httpx.Client(transport=_transport(handler)) as http:
        resp = c.fetch("https://api.example.com/data", max_amount="1", http_client=http)
    assert resp.status_code == 200


def test_requires_a_connected_signer():
    """fetch without a connected wallet is a programming error, not a payment."""
    c = X402Client(recipient_evm=SELLER, verify_facilitator_support=False)
    with pytest.raises(RuntimeError):
        c.fetch("https://api.example.com/data", max_amount="0.05")
