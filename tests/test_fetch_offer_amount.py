"""fetch() signs exactly the offer: its token and its atomic amount.

``fetch()`` turns the offer's atomic amount into a price with ``token_decimals``
and ``create_authorization()`` turns that price back into base units with the
decimals of the token it signs, which come from the network registry, on the
offer's own network. The two conversions must give back the offer's amount,
and the token signed must be the one the offer names.

``fetch()`` compares both after the ceiling and the policy, whose own refusals
keep coming first, and before anything is signed, and raises ``ValueError``
when either differs: what the policy approved is what signs. An offer that
names no asset is paid in ``token_type``'s token on the offer's network (USDC
by default), and the policy judges it as that token too. When ``select`` is not
given, an offer this client can sign as offered is preferred over a cheaper one
in another token.

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

from uvd_x402_sdk.client import X402Client
from uvd_x402_sdk.exceptions import PolicyRefusedError
from uvd_x402_sdk.networks import get_network, to_caip2_network
from uvd_x402_sdk.policy import PolicyApproval, PurchasePolicy, TokenAsset

BASE = get_network("base")
ARBITRUM = get_network("arbitrum")
POLYGON = get_network("polygon")
assert BASE is not None and ARBITRUM is not None and POLYGON is not None
USDC = BASE.usdc_address
EURC = BASE.tokens["eurc"].address
USDC_ARBITRUM = ARBITRUM.usdc_address
USDT_ARBITRUM = ARBITRUM.tokens["usdt"].address
USDC_POLYGON = POLYGON.usdc_address
SELLER = "0x000000000000000000000000000000000000dEaD"
PAYER = "0x1111111111111111111111111111111111111111"
URL = "https://api.example.com/data"
SIGNATURE = "0x" + "11" * 65


def accept(amount: int, version: int, network: str = "base", asset: str | None = USDC) -> dict:
    """One ``accepts`` entry for ``amount`` base units of ``asset`` on ``network``."""
    config = get_network(network)
    assert config is not None
    token = next((t for t in config.tokens.values() if t.address == asset), None)
    domain = (
        {"name": token.name, "version": token.version}
        if token is not None
        else {"name": config.usdc_domain_name, "version": config.usdc_domain_version}
    )
    entry: dict[str, Any] = {"scheme": "exact", "payTo": SELLER}
    if version == 1:
        entry.update(
            network=network,
            maxAmountRequired=str(amount),
            resource=URL,
            description="test resource",
        )
    else:
        entry.update(
            network=to_caip2_network(network),
            amount=str(amount),
            maxTimeoutSeconds=60,
            extra=domain,
        )
    if asset is not None:
        entry["asset"] = asset
    return entry


def challenge(entries: list[dict], version: int) -> dict[str, Any]:
    body: dict[str, Any] = {"x402Version": version, "accepts": entries}
    if version == 2:
        body["resource"] = {
            "url": URL,
            "description": "test resource",
            "mimeType": "application/json",
        }
    return body


class Seller:
    """Answers 402 until it gets a payment; keeps every request."""

    def __init__(self, body: dict[str, Any]) -> None:
        self.body = body
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
        self.domains: list[dict[str, Any]] = []

    def sign_typed_data(
        self, domain: dict[str, Any], types: dict[str, Any], message: dict[str, Any]
    ) -> str:
        self.messages.append(message)
        self.domains.append(domain)
        return SIGNATURE

    @property
    def tokens(self) -> list[tuple[int, str]]:
        """(chainId, verifyingContract) of each signature."""
        return [(d["chainId"], d["verifyingContract"]) for d in self.domains]


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


def budget(per_payment: int, *assets: tuple[str, str], **kwargs: Any) -> PurchasePolicy:
    """A ceiling of ``per_payment`` for each ``(network, asset)`` (USDC on base by default)."""
    keys = assets or (("base", USDC),)
    return PurchasePolicy(per_payment={TokenAsset(n, a): per_payment for n, a in keys}, **kwargs)


class Purchase:
    """One ``fetch()`` against a seller, with a buyer connected on base."""

    def __init__(self, body: dict[str, Any], policy: PurchasePolicy | None = None) -> None:
        self.seller = Seller(body)
        self.signer = Recorder()
        self.policy = RecordingPolicy(policy or PurchasePolicy.permissive())
        self.client = X402Client(recipient_evm=SELLER, verify_facilitator_support=False)
        self.client.connect_with_signer(self.signer, chain_name="base")

    @classmethod
    def of(cls, amount: int, version: int, policy: PurchasePolicy | None = None, **where: Any):
        return cls(challenge([accept(amount, version, **where)], version), policy)

    def run(self, **kwargs: Any) -> httpx.Response:
        with httpx.Client(transport=httpx.MockTransport(self.seller.handler)) as http:
            return self.client.fetch(URL, http_client=http, policy=self.policy, **kwargs)

    def signed(self) -> list[int]:
        return [int(m["value"]) for m in self.signer.messages]


def refused(purchase: Purchase, **kwargs: Any) -> str:
    """fetch() raised ValueError with nothing signed and nothing paid."""
    with pytest.raises(ValueError) as raised:
        purchase.run(**kwargs)
    assert purchase.signer.messages == []
    assert purchase.seller.paid() == []
    assert len(purchase.seller.requests) == 1  # the probe only
    return str(raised.value)


def refused_by_policy(purchase: Purchase, **kwargs: Any) -> str:
    """fetch() raised PolicyRefusedError with nothing signed; its code."""
    with pytest.raises(PolicyRefusedError) as raised:
        purchase.run(**kwargs)
    assert purchase.signer.messages == []
    assert purchase.seller.paid() == []
    return str(raised.value.refusal_code)


VERSIONS = pytest.mark.parametrize("version", [1, 2])


# ── the offer's amount ───────────────────────────────────────────────────────

AMOUNTS = (1, 10_000, 199_999, 200_001, 12_345_678_901_234_567_890_123)


class TestTheOfferAmountIsSigned:
    @VERSIONS
    @pytest.mark.parametrize("amount", AMOUNTS)
    @pytest.mark.parametrize("decimals", [None, 6], ids=["default", "explicit-6"])
    def test_with_the_registry_decimals_the_offer_amount_is_signed(
        self, amount: int, version: int, decimals: int | None
    ) -> None:
        purchase = Purchase.of(amount, version)
        kwargs = {} if decimals is None else {"token_decimals": decimals}
        assert purchase.run(**kwargs).status_code == 200
        # One number for the policy, the signature and the seller.
        assert purchase.policy.approved == [amount]
        assert purchase.signed() == [amount]
        assert [int(a["value"]) for a in purchase.seller.paid()] == [amount]
        assert purchase.signer.tokens == [(8453, USDC)]

    def test_within_a_budget_that_covers_it(self) -> None:
        purchase = Purchase.of(30_000, 1, budget(50_000))
        assert purchase.run(max_amount="0.05").status_code == 200
        assert purchase.policy.approved == purchase.signed() == [30_000]

    def test_a_real_signature_covers_the_offer_amount(self) -> None:
        key = Account.create()
        seller = Seller(challenge([accept(10_000, 2)], 2))
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


class TestAnotherAmountIsRefused:
    @VERSIONS
    def test_token_decimals_below_the_registry(self, version: int) -> None:
        message = refused(Purchase.of(30_000, version, budget(50_000)), token_decimals=4)
        assert "token_decimals=4" in message and "6 decimals" in message
        assert "30000 base units" in message and "3000000 base units" in message

    @VERSIONS
    def test_token_decimals_above_the_registry(self, version: int) -> None:
        message = refused(Purchase.of(30_000, version), token_decimals=8)
        assert "token_decimals=8" in message and "would sign 300 base units" in message

    @VERSIONS
    def test_token_decimals_that_round_the_amount_to_zero(self, version: int) -> None:
        message = refused(Purchase.of(30_000, version), token_decimals=18)
        assert "token_decimals=18" in message and "would sign 0 base units" in message

    @VERSIONS
    def test_token_decimals_that_leave_no_whole_base_unit(self, version: int) -> None:
        message = refused(Purchase.of(10_001, version), token_decimals=8)
        assert "would sign no whole number of base units" in message

    @VERSIONS
    def test_an_amount_longer_than_a_decimal_division_keeps(self, version: int) -> None:
        """30 digits: the price rounds to 28 and would sign 10 ** 30."""
        assert str(10**30 - 1) in refused(Purchase.of(10**30 - 1, version))

    def test_the_policy_approves_the_offer_and_nothing_else_is_signed(self) -> None:
        purchase = Purchase.of(30_000, 1, budget(50_000))
        refused(purchase, token_decimals=4, max_amount="10")
        # The policy judged the offer's own amount; nothing else signs.
        assert purchase.policy.approved == [30_000]

    def test_a_refusal_of_the_policy_comes_first(self) -> None:
        """The policy's contract keeps its codes and its order."""
        purchase = Purchase.of(30_000, 1, budget(20_000))
        assert refused_by_policy(purchase, token_decimals=4) == "per-payment-limit"


# ── the offer's token ────────────────────────────────────────────────────────


class TestTheOfferTokenIsSigned:
    @VERSIONS
    def test_an_offer_in_another_token_is_refused_before_signing(self, version: int) -> None:
        purchase = Purchase.of(10_000, version, network="arbitrum", asset=USDT_ARBITRUM)
        message = refused(purchase)
        assert USDT_ARBITRUM in message and USDC_ARBITRUM in message
        assert "token_type='usdc'" in message

    @VERSIONS
    def test_a_token_paid_with_its_own_token_type_is_signed(self, version: int) -> None:
        both = budget(50_000, ("arbitrum", USDC_ARBITRUM), ("arbitrum", USDT_ARBITRUM))
        purchase = Purchase.of(10_000, version, both, network="arbitrum", asset=USDT_ARBITRUM)
        assert purchase.run(token_type="usdt").status_code == 200
        assert purchase.signer.tokens == [(42161, USDT_ARBITRUM)]
        assert purchase.policy.approved == purchase.signed() == [10_000]

    @VERSIONS
    def test_the_asset_is_compared_as_an_address(self, version: int) -> None:
        """Hex in any case is the same address."""
        purchase = Purchase.of(10_000, version, asset=USDC.lower())
        assert purchase.run().status_code == 200
        assert purchase.signer.tokens == [(8453, USDC)]
        assert purchase.signed() == [10_000]

    @VERSIONS
    def test_an_offer_on_another_network_signs_that_network_token(self, version: int) -> None:
        """Connected on base, an offer on polygon is signed on polygon."""
        purchase = Purchase.of(10_000, version, network="polygon", asset=USDC_POLYGON)
        assert purchase.run().status_code == 200
        assert purchase.signer.tokens == [(137, USDC_POLYGON)]

    @VERSIONS
    def test_an_offer_naming_another_network_token_is_refused(self, version: int) -> None:
        """Base's USDC address is not USDC on polygon."""
        message = refused(Purchase.of(10_000, version, network="polygon", asset=USDC))
        assert USDC_POLYGON in message


# ── an offer that names no asset ─────────────────────────────────────────────


class TestAnOfferWithoutAsset:
    @VERSIONS
    def test_is_paid_in_usdc_by_default(self, version: int) -> None:
        purchase = Purchase.of(10_000, version, asset=None)
        assert purchase.run().status_code == 200
        assert purchase.signer.tokens == [(8453, USDC)]
        assert purchase.signed() == [10_000]

    @VERSIONS
    def test_is_paid_in_token_type_token(self, version: int) -> None:
        purchase = Purchase.of(10_000, version, asset=None)
        assert purchase.run(token_type="eurc").status_code == 200
        assert purchase.signer.tokens == [(8453, EURC)]

    @VERSIONS
    def test_is_held_to_the_ceiling_of_the_token_that_signs(self, version: int) -> None:
        """Unlisted assets allowed, a ceiling on USDC: the offer is judged as USDC."""
        policy = budget(40_000, allow_unlisted_assets=True)
        purchase = Purchase.of(900_000, version, policy, asset=None)
        assert refused_by_policy(purchase) == "per-payment-limit"

    @VERSIONS
    def test_within_that_ceiling_is_paid(self, version: int) -> None:
        policy = budget(40_000, allow_unlisted_assets=True)
        purchase = Purchase.of(30_000, version, policy, asset=None)
        assert purchase.run().status_code == 200
        assert purchase.signed() == [30_000]

    @VERSIONS
    def test_a_written_policy_still_refuses_it_by_name_first(self, version: int) -> None:
        purchase = Purchase.of(30_000, version, budget(40_000), asset=None)
        assert refused_by_policy(purchase) == "asset-not-budgeted"


# ── the default selection ────────────────────────────────────────────────────


def offers(version: int, *entries: tuple[int, str]) -> dict[str, Any]:
    """A 402 on arbitrum with one entry per ``(amount, asset)``, in that order."""
    return challenge(
        [accept(amount, version, network="arbitrum", asset=asset) for amount, asset in entries],
        version,
    )


class TestTheDefaultSelection:
    @VERSIONS
    @pytest.mark.parametrize(
        "listed",
        [
            ((9_000, USDT_ARBITRUM), (10_000, USDC_ARBITRUM)),
            ((10_000, USDT_ARBITRUM), (10_000, USDC_ARBITRUM)),
            ((10_000, USDC_ARBITRUM), (9_000, USDT_ARBITRUM)),
        ],
        ids=["cheaper-other-first", "equal-other-first", "own-first"],
    )
    def test_prefers_an_offer_in_the_token_that_signs(self, version: int, listed: tuple) -> None:
        purchase = Purchase(offers(version, *listed))
        assert purchase.run().status_code == 200
        assert purchase.signer.tokens == [(42161, USDC_ARBITRUM)]
        assert purchase.signed() == [10_000]

    @VERSIONS
    def test_the_cheapest_of_those_offers(self, version: int) -> None:
        body = offers(
            version, (5_000, USDT_ARBITRUM), (12_000, USDC_ARBITRUM), (11_000, USDC_ARBITRUM)
        )
        purchase = Purchase(body)
        assert purchase.run().status_code == 200
        assert purchase.signed() == [11_000]

    @VERSIONS
    def test_with_no_offer_it_can_sign_it_still_refuses(self, version: int) -> None:
        purchase = Purchase(offers(version, (9_000, USDT_ARBITRUM), (8_000, USDT_ARBITRUM)))
        assert USDT_ARBITRUM in refused(purchase)
