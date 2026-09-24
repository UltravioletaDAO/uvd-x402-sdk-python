"""fetch() signs exactly the offer: its token and its atomic amount.

``fetch()`` turns the offer's atomic amount into a price with ``token_decimals``
and ``create_authorization()`` turns that price back into base units with the
decimals of the token it signs, which come from the network registry. When the
two conversions disagree, the signed amount is not the one the offer asks, and
not the one ``max_amount`` and the purchase policy were checked against. They
disagree when ``token_decimals`` is not the signing token's decimals, and when
the offer has more digits than a ``Decimal`` division keeps (28).

The token has the same shape: ``create_authorization()`` signs the registry's
token for ``token_type`` whatever ``asset`` the offer names, while the policy
judged the offer's asset.

``fetch()`` now compares both after the ceiling and the policy, whose own
refusals keep coming first, and before anything is signed, and raises
``ValueError`` when either differs: what the policy approved is what signs.
With the registry's decimals and the offer's own token nothing changes.

These tests go in through ``fetch()`` against a mocked seller and assert on what
was signed, what the policy approved and whether a paid request was ever sent.
"""
from __future__ import annotations

import base64
import json
from typing import Any

import httpx
import pytest
from eth_account import Account
from eth_account.messages import encode_typed_data

from uvd_x402_sdk import client as client_module
from uvd_x402_sdk.client import X402Client
from uvd_x402_sdk.exceptions import PolicyRefusedError
from uvd_x402_sdk.networks import get_network
from uvd_x402_sdk.networks.base import TokenConfig
from uvd_x402_sdk.policy import PolicyApproval, PurchasePolicy, TokenAsset

BASE = get_network("base")
assert BASE is not None
USDC = BASE.usdc_address
EURC = BASE.tokens["eurc"].address
SELLER = "0x000000000000000000000000000000000000dEaD"
PAYER = "0x1111111111111111111111111111111111111111"
URL = "https://api.example.com/data"
SIGNATURE = "0x" + "11" * 65


def challenge(amount: int, version: int, asset: str | None = USDC) -> dict[str, Any]:
    """The seller's 402 for ``amount`` base units of ``asset`` on Base."""
    if version == 1:
        entry: dict[str, Any] = {
            "scheme": "exact",
            "network": "base",
            "maxAmountRequired": str(amount),
            "resource": URL,
            "description": "test resource",
            "payTo": SELLER,
        }
        if asset is not None:
            entry["asset"] = asset
        return {"x402Version": 1, "accepts": [entry]}
    return {
        "x402Version": 2,
        "resource": {"url": URL, "description": "test resource", "mimeType": "application/json"},
        "accepts": [
            {
                "scheme": "exact",
                "network": "eip155:8453",
                "amount": str(amount),
                "asset": asset,
                "payTo": SELLER,
                "maxTimeoutSeconds": 60,
                "extra": domain_of(asset),
            }
        ],
    }


def domain_of(asset: str | None) -> dict[str, str]:
    """The EIP-712 name and version a seller puts in ``extra`` for ``asset``."""
    token = BASE.tokens["eurc"]
    if asset == EURC:
        return {"name": token.name, "version": token.version}
    return {"name": BASE.usdc_domain_name, "version": BASE.usdc_domain_version}


class Seller:
    """Answers 402 until it gets a payment; keeps every request."""

    def __init__(self, amount: int, version: int, asset: str | None = USDC) -> None:
        self.body = challenge(amount, version, asset)
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if "X-PAYMENT" not in request.headers:
            return httpx.Response(402, json=self.body)
        return httpx.Response(200, json={"ok": True})

    def paid(self) -> list[dict[str, Any]]:
        """The authorizations of the paid requests."""
        return [
            json.loads(base64.b64decode(r.headers["X-PAYMENT"]))["payload"]["authorization"]
            for r in self.requests
            if "X-PAYMENT" in r.headers
        ]


class Recorder:
    """An external signer (``connect_with_signer``) that keeps each message."""

    address = PAYER

    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []
        self.tokens: list[str] = []

    def sign_typed_data(
        self, domain: dict[str, Any], types: dict[str, Any], message: dict[str, Any]
    ) -> str:
        self.messages.append(message)
        self.tokens.append(domain["verifyingContract"])
        return SIGNATURE


class RecordingPolicy:
    """A purchase policy that keeps the amount of every offer it approved."""

    def __init__(self, policy: PurchasePolicy) -> None:
        self.policy = policy
        self.approved: list[int] = []

    def evaluate(self, *args: Any, **kwargs: Any) -> Any:
        decision = self.policy.evaluate(*args, **kwargs)
        if isinstance(decision, PolicyApproval):
            self.approved.append(decision.amount)
        return decision


def budget(per_payment: int, asset: str = USDC) -> PurchasePolicy:
    return PurchasePolicy(per_payment={TokenAsset("base", asset): per_payment})


class Purchase:
    """One ``fetch()`` against a seller asking ``amount`` base units of ``asset``."""

    def __init__(
        self,
        amount: int,
        version: int,
        policy: PurchasePolicy | None = None,
        asset: str | None = USDC,
    ) -> None:
        self.seller = Seller(amount, version, asset)
        self.signer = Recorder()
        self.policy = RecordingPolicy(policy or PurchasePolicy.permissive())
        self.client = X402Client(recipient_evm=SELLER, verify_facilitator_support=False)
        self.client.connect_with_signer(self.signer, chain_name="base")

    def run(self, **kwargs: Any) -> httpx.Response:
        with httpx.Client(transport=httpx.MockTransport(self.seller.handler)) as http:
            return self.client.fetch(URL, http_client=http, policy=self.policy, **kwargs)

    def signed(self) -> list[int]:
        return [int(m["value"]) for m in self.signer.messages]


# ── the offer's amount, as it is ─────────────────────────────────────────────

AMOUNTS = (1, 10_000, 199_999, 200_001, 12_345_678_901_234_567_890_123)


class TestTheOfferAmountIsSigned:
    @pytest.mark.parametrize("version", [1, 2])
    @pytest.mark.parametrize("amount", AMOUNTS)
    @pytest.mark.parametrize("decimals", [None, 6], ids=["default", "explicit-6"])
    def test_with_the_registry_decimals_the_offer_amount_is_signed(
        self, amount: int, version: int, decimals: int | None
    ) -> None:
        purchase = Purchase(amount, version)
        kwargs = {} if decimals is None else {"token_decimals": decimals}
        assert purchase.run(**kwargs).status_code == 200
        # The policy approved, the payer signed and the seller received one number.
        assert purchase.policy.approved == [amount]
        assert purchase.signed() == [amount]
        assert [int(a["value"]) for a in purchase.seller.paid()] == [amount]
        assert purchase.signer.tokens == [USDC]

    def test_within_a_budget_that_covers_it(self) -> None:
        purchase = Purchase(10_000, 1, budget(20_000))
        assert purchase.run(max_amount="0.02").status_code == 200
        assert purchase.policy.approved == purchase.signed() == [10_000]

    def test_a_real_signature_covers_the_offer_amount(self) -> None:
        key = Account.create()
        seller = Seller(10_000, 2)
        client = X402Client(recipient_evm=SELLER, verify_facilitator_support=False)
        client.connect_with_private_key(key.key.hex(), chain_name="base")
        with httpx.Client(transport=httpx.MockTransport(seller.handler)) as http:
            assert client.fetch(URL, http_client=http).status_code == 200
        (authorization,) = seller.paid()
        assert authorization["value"] == "10000"
        payment = json.loads(base64.b64decode(seller.requests[-1].headers["X-PAYMENT"]))
        transfer = [
            {"name": "from", "type": "address"},
            {"name": "to", "type": "address"},
            {"name": "value", "type": "uint256"},
            {"name": "validAfter", "type": "uint256"},
            {"name": "validBefore", "type": "uint256"},
            {"name": "nonce", "type": "bytes32"},
        ]
        domain = {
            "name": BASE.usdc_domain_name,
            "version": BASE.usdc_domain_version,
            "chainId": BASE.chain_id,
            "verifyingContract": USDC,
        }
        message = encode_typed_data(domain, {"TransferWithAuthorization": transfer}, authorization)
        signer = Account.recover_message(message, signature=payment["payload"]["signature"])
        assert signer == key.address


# ── a conversion that would sign another amount is refused ──────────────────


def refused(purchase: Purchase, **kwargs: Any) -> str:
    """fetch() raised ValueError with nothing signed and nothing paid."""
    with pytest.raises(ValueError) as raised:
        purchase.run(**kwargs)
    assert purchase.signer.messages == []
    assert purchase.seller.paid() == []
    assert len(purchase.seller.requests) == 1  # the probe only
    return str(raised.value)


class TestAnotherAmountIsRefused:
    @pytest.mark.parametrize("version", [1, 2])
    def test_token_decimals_below_the_registry(self, version: int) -> None:
        purchase = Purchase(10_000, version, budget(20_000))
        message = refused(purchase, token_decimals=2)
        assert "token_decimals=2" in message and "6 decimals" in message
        assert "10000" in message and "100000000" in message

    @pytest.mark.parametrize("version", [1, 2])
    def test_token_decimals_above_the_registry(self, version: int) -> None:
        purchase = Purchase(10_000, version)
        message = refused(purchase, token_decimals=8)
        assert "token_decimals=8" in message and "10000" in message

    @pytest.mark.parametrize("version", [1, 2])
    def test_token_decimals_that_leave_no_whole_base_unit(self, version: int) -> None:
        purchase = Purchase(10_000, version)
        assert "token_decimals=18" in refused(purchase, token_decimals=18)

    @pytest.mark.parametrize("version", [1, 2])
    def test_an_amount_longer_than_a_decimal_division_keeps(self, version: int) -> None:
        """30 digits: the price rounds to 28 and would sign 10 ** 30."""
        purchase = Purchase(10**30 - 1, version)
        assert str(10**30 - 1) in refused(purchase)

    def test_the_policy_approves_the_offer_and_nothing_else_is_signed(self) -> None:
        purchase = Purchase(10_000, 1, budget(20_000))
        refused(purchase, token_decimals=2, max_amount="1000")
        # The policy judged the offer's own amount; that is not what would sign.
        assert purchase.policy.approved == [10_000]

    def test_a_refusal_of_the_policy_comes_first(self) -> None:
        """The policy's contract keeps its codes and its order."""
        purchase = Purchase(10_000, 1, budget(5_000))
        with pytest.raises(PolicyRefusedError) as raised:
            purchase.run(token_decimals=2)
        assert raised.value.refusal_code == "per-payment-limit"
        assert purchase.signer.messages == []


# ── the offer's token ────────────────────────────────────────────────────────


class TestTheOfferTokenIsSigned:
    @pytest.mark.parametrize("version", [1, 2])
    def test_an_offer_in_another_token_is_refused_before_signing(self, version: int) -> None:
        purchase = Purchase(10_000, version, budget(20_000, EURC), asset=EURC)
        message = refused(purchase)
        assert EURC in message and USDC in message and "token_type='usdc'" in message
        # The policy judged the offer's token; USDC would have signed.
        assert purchase.policy.approved == [10_000]

    @pytest.mark.parametrize("version", [1, 2])
    def test_the_offer_token_with_its_token_type_is_signed(self, version: int) -> None:
        purchase = Purchase(10_000, version, budget(20_000, EURC), asset=EURC)
        assert purchase.run(token_type="eurc").status_code == 200
        assert purchase.signer.tokens == [EURC]
        assert purchase.policy.approved == purchase.signed() == [10_000]

    @pytest.mark.parametrize("version", [1, 2])
    def test_the_asset_is_compared_as_an_address(self, version: int) -> None:
        """Hex in any case is the same address."""
        purchase = Purchase(10_000, version, asset=USDC.lower())
        assert purchase.run().status_code == 200
        assert purchase.signer.tokens == [USDC]
        assert purchase.signed() == [10_000]

    def test_an_offer_that_names_no_asset_is_paid_in_usdc_as_before(self) -> None:
        purchase = Purchase(10_000, 1, asset=None)
        assert purchase.run().status_code == 200
        assert purchase.signer.tokens == [USDC]
        assert purchase.signed() == [10_000]


# ── the mutations: the check taken away turns its cases red ────────────────


class TestMutations:
    def test_without_the_check_another_amount_is_signed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(client_module, "_signing_token", lambda *_: None)
        purchase = Purchase(10_000, 1, budget(20_000))
        assert purchase.run(token_decimals=2).status_code == 200
        # The policy approved one number and the payer signed another.
        assert purchase.policy.approved == [10_000]
        assert purchase.signed() == [100_000_000]

    def test_a_check_with_other_decimals_than_the_registry_signs_another_amount(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        token = TokenConfig(address=USDC, decimals=8, name="USD Coin", version="2")
        monkeypatch.setattr(client_module, "_signing_token", lambda *_: token)
        purchase = Purchase(10_000, 2)
        assert purchase.run(token_decimals=8).status_code == 200
        assert purchase.policy.approved == [10_000]
        assert purchase.signed() == [100]

    def test_without_the_token_comparison_another_token_is_signed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(client_module, "canonical_address", lambda address: "")
        purchase = Purchase(10_000, 1, budget(20_000, EURC), asset=EURC)
        assert purchase.run().status_code == 200
        # The policy approved the offer's token and the payer signed another.
        assert purchase.policy.approved == [10_000]
        assert purchase.signer.tokens == [USDC]
