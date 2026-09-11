"""The policy decides on the path the buyer actually takes, not beside it.

The failure this file exists to prevent is the one the facilitator's own
security review caught in Rust: the policy existed, the seller declared its
validity, and the wiring between them was missing for a whole commit with every
unit test green. So these tests go in through `X402Client.fetch()` -- probe,
402, decide, sign, retry -- against a mocked transport with a real local key, and
assert on whether an `X-PAYMENT` header was ever produced.

Red-proof: deleting the evaluation block from `fetch()` turns
`test_an_expired_offer_is_refused_on_the_path_the_client_takes` and
`test_an_unbudgeted_asset_is_refused_on_the_path_the_client_takes` red -- both
sign and pay instead of refusing.
"""

import time

import httpx
import pytest

from uvd_x402_sdk.client import X402Client
from uvd_x402_sdk.exceptions import (
    NoAcceptablePaymentError,
    PolicyRefusedError,
)
from uvd_x402_sdk.policy import (
    OFFER_VALIDITY_EXTENSION,
    AdvertisedQuote,
    PurchasePolicy,
    TokenAsset,
)

# Anvil test key #0 — public, for tests only, never a real wallet.
TEST_KEY = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
USDC_BASE = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
EURC_BASE = "0x60a3E35Cc302bFA44Cb288Bc5a4F316Fdb1adb42"
SELLER = "0x000000000000000000000000000000000000dEaD"
OTHER_SELLER = "0xe4dc963c56979E0260fc146b87eE24F18220e545"

USDC = TokenAsset("base", USDC_BASE)


def _client(policy=None):
    c = X402Client(
        recipient_evm=SELLER, verify_facilitator_support=False, policy=policy
    )
    c.connect_with_private_key(TEST_KEY, chain_name="base")
    return c


def _402(
    *,
    amount="10000",
    asset=USDC_BASE,
    pay_to=SELLER,
    scheme="exact",
    valid_until=None,
    extra_accepts=(),
):
    body = {
        "x402Version": 1,
        "accepts": [
            {
                "scheme": scheme,
                "network": "base",
                "maxAmountRequired": amount,
                "resource": "https://api.example.com/data",
                "description": "test resource",
                "payTo": pay_to,
                "asset": asset,
            },
            *extra_accepts,
        ],
    }
    if valid_until is not None:
        body["extensions"] = {
            OFFER_VALIDITY_EXTENSION: {"info": {"validUntil": valid_until}}
        }
    return body


class _Seller:
    """A 402 that turns into a 200 once a payment header arrives."""

    def __init__(self, body):
        self.body = body
        self.paid_header = None
        self.calls = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        header = request.headers.get("X-PAYMENT")
        if header is None:
            return httpx.Response(402, json=self.body)
        self.paid_header = header
        return httpx.Response(200, json={"ok": True})


def _fetch(client, seller, **kwargs):
    with httpx.Client(transport=httpx.MockTransport(seller)) as http:
        return client.fetch("https://api.example.com/data", http_client=http, **kwargs)


# =============================================================================
# The two refusals the task names, both through the real path
# =============================================================================


def test_an_expired_offer_is_refused_on_the_path_the_client_takes():
    """A seller's own `validUntil`, read from the challenge's extensions and
    honoured BEFORE signing. Nothing is signed and nothing is sent."""
    seller = _Seller(_402(valid_until=int(time.time()) - 1))
    policy = PurchasePolicy(per_payment={USDC: 1_000_000})

    with pytest.raises(PolicyRefusedError) as excinfo:
        _fetch(_client(policy), seller)

    assert excinfo.value.refusal_code == "offer-expired"
    assert seller.paid_header is None, "an expired offer was signed and paid"
    assert seller.calls == 1, "the paid retry went out anyway"
    # The numbers that caused it travel with the refusal.
    assert excinfo.value.details["validUntil"] == seller.body["extensions"][
        OFFER_VALIDITY_EXTENSION
    ]["info"]["validUntil"]
    assert excinfo.value.details["now"] >= excinfo.value.details["validUntil"]


def test_an_unbudgeted_asset_is_refused_on_the_path_the_client_takes():
    """A budget in USDC is not a budget in EURC.

    The signer would have signed it: it takes the EIP-712 domain from the
    seller's own `extra` and will sign for a token it has never heard of. This
    is the check that stops it, and it runs before the ceilings so the caller
    hears "budget that asset", not "raise a ceiling that does not exist".
    """
    seller = _Seller(_402(asset=EURC_BASE))
    policy = PurchasePolicy(per_payment={USDC: 1_000_000})

    with pytest.raises(PolicyRefusedError) as excinfo:
        _fetch(_client(policy), seller)

    assert excinfo.value.refusal_code == "asset-not-budgeted"
    assert seller.paid_header is None, "an unbudgeted token was signed and paid"
    assert excinfo.value.details["asset"]["address"] == EURC_BASE.lower()


# =============================================================================
# And the payment still happens when the policy covers it
# =============================================================================


def test_an_offer_inside_the_policy_is_paid_without_asking_anyone():
    """Rule 3: no human confirmation hook on this path. The policy already said
    yes, so the buyer pays."""
    seller = _Seller(_402(valid_until=int(time.time()) + 600))
    policy = PurchasePolicy(
        per_payment={USDC: 1_000_000},
        cumulative={USDC: 1_000_000},
        only_pay=[SELLER],
    )

    resp = _fetch(_client(policy), seller)

    assert resp.status_code == 200
    assert seller.paid_header is not None


def test_no_policy_pays_exactly_what_it_paid_before():
    """A caller who never wrote a policy keeps the behaviour they had: the
    client holds `permissive()`, so an unlisted asset is not refused."""
    seller = _Seller(_402(asset=EURC_BASE))

    resp = _fetch(_client(), seller, max_amount="0.05")

    assert resp.status_code == 200
    assert seller.paid_header is not None


def test_a_reprice_above_the_listing_still_pays_if_the_policy_covers_it():
    """A divergence from the catalog is evidence, never a refusal. An agent
    that halts on an ordinary reprice is an agent nobody can leave running."""
    seller = _Seller(_402(amount="20000"))  # catalog said 10000
    policy = PurchasePolicy(per_payment={USDC: 1_000_000})

    resp = _fetch(
        _client(policy),
        seller,
        quote=AdvertisedQuote(asset=USDC, amount=10_000),
    )

    assert resp.status_code == 200
    assert seller.paid_header is not None


# =============================================================================
# The rest of the order, on the real path
# =============================================================================


def test_a_recipient_off_the_allowlist_is_refused_before_signing():
    seller = _Seller(_402(pay_to=OTHER_SELLER))
    policy = PurchasePolicy(per_payment={USDC: 1_000_000}, only_pay=[SELLER])

    with pytest.raises(PolicyRefusedError) as excinfo:
        _fetch(_client(policy), seller)

    assert excinfo.value.refusal_code == "recipient-not-permitted"
    assert seller.paid_header is None


def test_the_per_payment_ceiling_refuses_before_signing():
    seller = _Seller(_402(amount="10000"))
    policy = PurchasePolicy(per_payment={USDC: 9_999})

    with pytest.raises(PolicyRefusedError) as excinfo:
        _fetch(_client(policy), seller)

    assert excinfo.value.refusal_code == "per-payment-limit"
    assert excinfo.value.details["requested"] == "10000"
    assert excinfo.value.details["allowed"] == "9999"
    assert seller.paid_header is None


def test_the_cumulative_limit_bites_on_the_second_call_after_record_spend():
    """The whole loop: pay, record the settled spend, and the next offer that
    would cross the ceiling is refused -- with the same policy object, which is
    where a cumulative limit lives."""
    policy = PurchasePolicy(
        per_payment={USDC: 1_000_000}, cumulative={USDC: 15_000}
    )
    client = _client(policy)

    first = _Seller(_402(amount="10000"))
    assert _fetch(client, first).status_code == 200
    assert first.paid_header is not None

    # Evaluating did NOT spend; the caller records it once the settlement
    # resolved (here: the seller served the paid retry).
    assert policy.spent(USDC) == 0
    policy.record_spend(USDC, 10_000)

    second = _Seller(_402(amount="10000"))
    with pytest.raises(PolicyRefusedError) as excinfo:
        _fetch(client, second)

    assert excinfo.value.refusal_code == "cumulative-limit"
    assert excinfo.value.details["spent"] == "10000"
    assert excinfo.value.details["wouldTotal"] == "20000"
    assert excinfo.value.details["allowed"] == "15000"
    assert second.paid_header is None


def test_a_challenge_of_only_unreadable_offers_names_the_schemes():
    """`no-readable-offer` instead of an empty list. A caller that learns the
    seller wanted `batch-settlement` looks for a facilitator that implements it,
    where "no usable payment options" sends it hunting a bug in its own code."""
    seller = _Seller(
        {
            "x402Version": 1,
            "accepts": [
                {
                    "scheme": "batch-settlement",
                    "network": "base",
                    "maxAmountRequired": "10000",
                    "payTo": SELLER,
                    "asset": USDC_BASE,
                },
                {"scheme": "agent-pay", "network": "base", "payTo": SELLER},
            ],
        }
    )

    with pytest.raises(PolicyRefusedError) as excinfo:
        _fetch(_client(), seller)

    assert excinfo.value.refusal_code == "no-readable-offer"
    assert excinfo.value.details["offered"] == ["batch-settlement", "agent-pay"]
    # Still a NoAcceptablePaymentError, so code written before 0.82.0 catches it.
    assert isinstance(excinfo.value, NoAcceptablePaymentError)
    assert seller.paid_header is None


def test_a_payable_offer_beside_an_unreadable_one_is_still_paid():
    """Rule 7 on the real path: one entry this build cannot name must not make
    the seller unpayable."""
    seller = _Seller(
        _402(
            extra_accepts=[
                {
                    "scheme": "batch-settlement",
                    "network": "base",
                    "maxAmountRequired": "1",
                    "payTo": SELLER,
                    "asset": USDC_BASE,
                }
            ]
        )
    )
    policy = PurchasePolicy(per_payment={USDC: 1_000_000})

    resp = _fetch(_client(policy), seller)

    assert resp.status_code == 200
    assert seller.paid_header is not None


def test_a_seller_with_no_offers_at_all_is_not_a_policy_refusal():
    """Three things that used to be one error stay distinguishable: "the seller
    sent no offers" is still `NoAcceptablePaymentError`, not a policy refusal."""
    seller = _Seller({"x402Version": 1, "accepts": []})

    with pytest.raises(NoAcceptablePaymentError) as excinfo:
        _fetch(_client(), seller)

    assert not isinstance(excinfo.value, PolicyRefusedError)


def test_max_amount_still_raises_its_own_error_and_runs_first():
    """The pre-existing ceiling is untouched: a caller that set `max_amount` and
    no policy keeps exactly the exception it had."""
    from uvd_x402_sdk.exceptions import PaymentExceedsMaxError

    seller = _Seller(_402(amount="10000"))  # 0.01 USDC

    with pytest.raises(PaymentExceedsMaxError):
        _fetch(_client(), seller, max_amount="0.005")

    assert seller.paid_header is None


def test_a_per_call_policy_overrides_the_client_one():
    seller = _Seller(_402(amount="10000"))
    client = _client(PurchasePolicy(per_payment={USDC: 1_000_000}))

    with pytest.raises(PolicyRefusedError) as excinfo:
        _fetch(client, seller, policy=PurchasePolicy(per_payment={USDC: 1}))

    assert excinfo.value.refusal_code == "per-payment-limit"
    assert seller.paid_header is None


# =============================================================================
# Portability on the real path: one policy, both dialects of one chain
# =============================================================================


def _402_v2(amount="10000", network="eip155:8453"):
    """The shape production actually serves. Measured 2026-08-20: 36 of 36 live
    resources answering 402 carry the challenge in the header, in this shape."""
    return {
        "x402Version": 2,
        "error": "Payment required",
        "accepts": [
            {
                "scheme": "exact",
                "network": network,
                "amount": amount,
                "asset": USDC_BASE,
                "payTo": SELLER,
                "maxTimeoutSeconds": 300,
            }
        ],
    }


def test_one_policy_written_as_base_pays_a_v2_challenge():
    """THE portability failure this closes: the policy is written `base`, the
    seller answers a v2 challenge naming `eip155:8453`, and before this the
    buyer refused with `asset-not-budgeted` -- a cause that is not true -- while
    the Rust and TypeScript buyers paid."""
    seller = _Seller(_402_v2())
    policy = PurchasePolicy(per_payment={USDC: 1_000_000}, only_pay=[SELLER])

    resp = _fetch(_client(policy), seller)

    assert resp.status_code == 200
    assert seller.paid_header is not None


def test_the_same_client_and_policy_pay_a_v1_and_a_v2_challenge():
    """Same object, both dialects, and the spend lands in ONE purse -- not two
    budgets for the same chain."""
    policy = PurchasePolicy(per_payment={USDC: 1_000_000}, cumulative={USDC: 25_000})
    client = _client(policy)

    v1 = _Seller(_402(amount="10000"))
    assert _fetch(client, v1).status_code == 200
    policy.record_spend(USDC, 10_000)

    v2 = _Seller(_402_v2(amount="10000"))
    assert _fetch(client, v2).status_code == 200
    policy.record_spend(TokenAsset("eip155:8453", USDC_BASE), 10_000)

    # 20_000 of a 25_000 ceiling is gone, counted once per payment on one key.
    assert policy.spent(USDC) == 20_000
    third = _Seller(_402(amount="10000"))
    with pytest.raises(PolicyRefusedError) as excinfo:
        _fetch(client, third)
    assert excinfo.value.refusal_code == "cumulative-limit"
    assert third.paid_header is None


def test_a_v2_challenge_on_another_chain_is_still_refused():
    """The dialect resolves; it does not wave chains through."""
    seller = _Seller(_402_v2(network="eip155:137"))  # Polygon
    policy = PurchasePolicy(per_payment={USDC: 1_000_000})

    with pytest.raises(PolicyRefusedError) as excinfo:
        _fetch(_client(policy), seller)

    assert excinfo.value.refusal_code == "asset-not-budgeted"
    assert seller.paid_header is None


def test_an_offer_with_no_scheme_is_named_instead_of_signed_as_exact():
    """0.82.0 behaviour change, asserted on purpose so it is not a silent one:
    until now `fetch()` assumed `exact` and signed. A payment we cannot name is
    a payment we cannot make."""
    seller = _Seller(
        {
            "x402Version": 1,
            "accepts": [
                {"network": "base", "maxAmountRequired": "10000", "payTo": SELLER,
                 "asset": USDC_BASE}
            ],
        }
    )

    with pytest.raises(PolicyRefusedError) as excinfo:
        _fetch(_client(), seller)

    assert excinfo.value.refusal_code == "no-readable-offer"
    # Nothing to name: `offered[]` carries named schemes only, so it is absent,
    # and the count lands in the prose instead of leaving a bare "offered: []".
    assert "offered" not in excinfo.value.details
    assert "1 offer(s) named no scheme" in excinfo.value.message
    assert seller.paid_header is None
