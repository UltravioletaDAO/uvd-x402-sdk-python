"""The buyer's policy: the contract the facilitator fixed in its P3 phase.

Same contract as `x402-rs` 2.25.0 (`crates/x402-reqwest/src/policy.rs`) and the
TypeScript SDK, so a refusal code means the same thing in all three. These tests
pin the parts a reimplementation gets wrong quietly: the ORDER (the first
failing check is the one reported, and a caller branches on it), that evaluating
does not spend, that a copy spends from the same purse, and that an address is
canonicalised by family rather than lowercased.

The wiring into the real buyer path lives in `test_policy_real_path.py`; a unit
test cannot tell a policy that is consulted from one that merely exists.
"""

import copy
import threading

import pytest

from uvd_x402_sdk.policy import (
    OFFER_VALIDITY_EXTENSION,
    REFUSAL_CODES,
    AdvertisedQuote,
    Offer,
    PurchasePolicy,
    TokenAsset,
    UnreadableOffer,
    canonical_address,
    no_readable_offer,
    offer_valid_until,
    parse_accepts,
)

USDC_BASE = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
EURC_BASE = "0x60a3E35Cc302bFA44Cb288Bc5a4F316Fdb1adb42"
PAYEE = "0xe4dc963c56979E0260fc146b87eE24F18220e545"
OTHER_PAYEE = "0x000000000000000000000000000000000000dEaD"
# A real Solana mint and a real-shaped base58 payee: case is a SYMBOL here.
USDC_SOLANA = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
SOLANA_PAYEE = "7xKXtg2CW87d97TXJSDpbD5jBkheTqA83TZRuJosgAsU"

USDC = TokenAsset("base", USDC_BASE)
EURC = TokenAsset("base", EURC_BASE)
NOW = 1_757_500_000


def offer(
    amount=10_000,
    asset=USDC_BASE,
    pay_to=PAYEE,
    network="base",
    scheme="exact",
):
    return Offer(
        scheme=scheme,
        network=network,
        asset=asset,
        amount=amount,
        pay_to=pay_to,
    )


# =============================================================================
# The order of evaluation IS the contract
# =============================================================================


def test_the_six_codes_are_a_closed_vocabulary_in_order():
    assert REFUSAL_CODES == (
        "no-readable-offer",
        "offer-expired",
        "recipient-not-permitted",
        "asset-not-budgeted",
        "per-payment-limit",
        "cumulative-limit",
    )


def _policy_failing_everything():
    """A policy and an offer that violate every check at once, so each test can
    remove one cause and watch the NEXT one surface -- which is the only way to
    prove an order rather than a set."""
    return PurchasePolicy(
        per_payment={USDC: 1},
        cumulative={USDC: 1},
        only_pay=[OTHER_PAYEE],
    )


def test_nothing_readable_wins_over_everything():
    """First in the order: a challenge that carried offers, none of them ones
    this build can read."""
    decision = _policy_failing_everything().evaluate(
        None,
        NOW,
        valid_until=NOW - 1,
        unreadable=[UnreadableOffer(scheme="batch-settlement")],
    )
    assert decision.code == "no-readable-offer"
    assert decision.offered == ("batch-settlement",)


def test_expiry_wins_over_the_recipient_the_asset_and_the_ceilings():
    """Second: terms that have lapsed are not terms, whatever they say. This
    offer ALSO pays a forbidden recipient, in an unbudgeted asset, over both
    ceilings -- and `offer-expired` is what gets reported."""
    decision = _policy_failing_everything().evaluate(
        offer(asset=EURC_BASE, amount=999_999),
        NOW,
        valid_until=NOW - 1,
    )
    assert decision.code == "offer-expired"
    assert decision.valid_until == NOW - 1
    assert decision.now == NOW


def test_the_recipient_wins_over_the_asset_and_the_ceilings():
    decision = _policy_failing_everything().evaluate(
        offer(asset=EURC_BASE, amount=999_999), NOW
    )
    assert decision.code == "recipient-not-permitted"
    assert decision.pay_to == PAYEE


def test_an_unbudgeted_asset_wins_over_the_ceilings():
    """Fourth, and BEFORE the ceilings on purpose: the ceilings are a map, and a
    map has no opinion about a key it does not hold. The caller has to hear
    "budget that asset", not "raise a ceiling that does not exist"."""
    policy = PurchasePolicy(per_payment={USDC: 1}, cumulative={USDC: 1})
    decision = policy.evaluate(offer(asset=EURC_BASE, amount=999_999), NOW)
    assert decision.code == "asset-not-budgeted"
    assert decision.asset == EURC


def test_the_per_payment_ceiling_wins_over_the_cumulative_one():
    policy = PurchasePolicy(per_payment={USDC: 1}, cumulative={USDC: 1})
    decision = policy.evaluate(offer(amount=999_999), NOW)
    assert decision.code == "per-payment-limit"
    assert decision.requested == 999_999
    assert decision.allowed == 1


def test_the_cumulative_limit_is_last():
    policy = PurchasePolicy(per_payment={USDC: 1_000_000}, cumulative={USDC: 15_000})
    policy.record_spend(USDC, 10_000)
    decision = policy.evaluate(offer(amount=10_000), NOW)
    assert decision.code == "cumulative-limit"
    assert (decision.spent, decision.would_total, decision.allowed) == (
        10_000,
        20_000,
        15_000,
    )


def test_every_refusal_carries_the_numbers_that_caused_it():
    policy = PurchasePolicy(per_payment={USDC: 1})
    payload = policy.evaluate(offer(amount=999), NOW).to_dict()
    assert payload["code"] == "per-payment-limit"
    assert payload["requested"] == "999" and payload["allowed"] == "1"
    assert payload["asset"] == {"network": "base", "address": USDC_BASE.lower()}
    assert payload["payTo"] == PAYEE
    # Amounts travel as strings: an atomic amount can outgrow a JSON double, and
    # a budget silently rounded is a budget that does not hold.
    assert isinstance(payload["requested"], str)


# =============================================================================
# Rule 1: evaluating does not spend
# =============================================================================


def test_evaluating_does_not_spend():
    """Signing can still fail and a settlement can still be refused. A limit
    that counted attempts would lock a caller out of money it never spent."""
    policy = PurchasePolicy(per_payment={USDC: 1_000_000}, cumulative={USDC: 20_000})

    for _ in range(5):
        assert policy.evaluate(offer(amount=10_000), NOW).approved

    assert policy.spent(USDC) == 0


def test_only_record_spend_moves_the_total():
    policy = PurchasePolicy(cumulative={USDC: 100_000})
    policy.record_spend(USDC, 10_000)
    policy.record_spend(USDC, 5_000)
    assert policy.spent(USDC) == 15_000
    assert policy.spent(EURC) == 0


def test_the_cumulative_limit_counts_what_was_recorded():
    policy = PurchasePolicy(cumulative={USDC: 25_000})
    assert policy.evaluate(offer(amount=20_000), NOW).approved
    policy.record_spend(USDC, 20_000)
    assert policy.evaluate(offer(amount=20_000), NOW).code == "cumulative-limit"
    # And what remains still goes through.
    assert policy.evaluate(offer(amount=5_000), NOW).approved


def test_a_negative_spend_is_refused_rather_than_handing_budget_back():
    policy = PurchasePolicy(cumulative={USDC: 100})
    with pytest.raises(ValueError):
        policy.record_spend(USDC, -50)
    assert policy.spent(USDC) == 0


# =============================================================================
# Rule 2: a policy is never widened from inside an evaluation
# =============================================================================


def test_no_method_raises_a_limit():
    """There is deliberately no setter and no builder that widens a policy. If
    one is ever added, this is the test that has to be deleted on purpose."""
    policy = PurchasePolicy(per_payment={USDC: 1})
    widening = [
        name
        for name in dir(policy)
        if name
        in {
            "allow_unlisted_assets",
            "set_per_payment",
            "set_cumulative",
            "raise_limit",
            "widen",
            "update",
            "add_recipient",
        }
    ]
    assert widening == [], f"a method that widens the policy appeared: {widening}"


def test_the_limits_handed_out_are_read_only():
    policy = PurchasePolicy(per_payment={USDC: 1}, cumulative={USDC: 1})
    with pytest.raises(TypeError):
        policy.per_payment[USDC] = 10_000_000
    with pytest.raises(TypeError):
        policy.cumulative[USDC] = 10_000_000
    assert policy.evaluate(offer(amount=2), NOW).code == "per-payment-limit"


# =============================================================================
# A copy spends from the same purse
# =============================================================================


def test_a_copy_spends_from_the_same_purse():
    """A client is copied per request. If each copy carried its own total, a
    cumulative limit would mean nothing."""
    policy = PurchasePolicy(cumulative={USDC: 15_000})
    twin = copy.copy(policy)

    policy.record_spend(USDC, 10_000)

    assert twin.spent(USDC) == 10_000
    assert twin.evaluate(offer(amount=10_000), NOW).code == "cumulative-limit"


def test_a_deep_copy_shares_the_purse_too():
    """THE trap: `copy.deepcopy` duplicating the running total hands a fresh
    budget to every copy, which is exactly what a cumulative limit exists to
    prevent -- and it looks like a correct deep copy."""
    policy = PurchasePolicy(cumulative={USDC: 15_000})
    twin = copy.deepcopy(policy)

    twin.record_spend(USDC, 10_000)

    assert policy.spent(USDC) == 10_000
    assert policy.evaluate(offer(amount=10_000), NOW).code == "cumulative-limit"


def test_unreadable_state_reports_the_ceiling_never_zero():
    """For money the safe direction is to refuse. Reporting zero spent would
    silently restore the caller's whole budget."""
    policy = PurchasePolicy(cumulative={USDC: 15_000})
    policy.record_spend(USDC, 1_000)

    held = threading.Event()
    release = threading.Event()

    def hold():
        with policy._purse.lock:
            held.set()
            release.wait(10)

    holder = threading.Thread(target=hold, daemon=True)
    holder.start()
    held.wait(5)
    try:
        import uvd_x402_sdk.policy as policy_module

        original = policy_module._PURSE_LOCK_TIMEOUT_SECONDS
        policy_module._PURSE_LOCK_TIMEOUT_SECONDS = 0.05
        try:
            assert policy.spent(USDC) == 15_000  # the ceiling, not 1_000 and not 0
            assert policy.evaluate(offer(amount=1), NOW).code == "cumulative-limit"
        finally:
            policy_module._PURSE_LOCK_TIMEOUT_SECONDS = original
    finally:
        release.set()
        holder.join(5)


def test_a_corrupt_total_reports_the_ceiling_too():
    policy = PurchasePolicy(cumulative={USDC: 15_000})
    policy._purse.totals[USDC] = "not a number"
    assert policy.spent(USDC) == 15_000


# =============================================================================
# Rule 4b: deny an unbudgeted asset by default, permissive by name
# =============================================================================


def test_a_written_policy_denies_an_asset_it_was_never_told_about():
    """A budget in USDC is NOT a budget in any other token, and the signer would
    sign it: it takes the EIP-712 domain from the seller's own `extra`."""
    policy = PurchasePolicy(per_payment={USDC: 1_000_000})
    assert policy.evaluate(offer(asset=EURC_BASE), NOW).code == "asset-not-budgeted"


def test_a_cumulative_ceiling_alone_budgets_the_asset():
    policy = PurchasePolicy(cumulative={EURC: 1_000_000})
    assert policy.evaluate(offer(asset=EURC_BASE), NOW).approved


def test_the_permissive_mode_has_to_be_asked_for_by_name():
    permissive = PurchasePolicy.permissive()
    assert permissive.allows_unlisted_assets is True
    assert permissive.evaluate(offer(asset=EURC_BASE), NOW).approved
    # and the constructor's default is the opposite
    assert PurchasePolicy().allows_unlisted_assets is False
    assert PurchasePolicy().evaluate(offer(), NOW).code == "asset-not-budgeted"


def test_permissive_still_honours_every_other_check():
    """Permissive means "an asset I did not budget is fine", not "anything goes"."""
    policy = PurchasePolicy(
        per_payment={USDC: 100}, only_pay=[OTHER_PAYEE], allow_unlisted_assets=True
    )
    assert policy.evaluate(offer(), NOW).code == "recipient-not-permitted"
    assert policy.evaluate(offer(pay_to=OTHER_PAYEE, amount=101), NOW).code == (
        "per-payment-limit"
    )


def test_a_same_network_ceiling_does_not_cover_another_network():
    """USDC on Base and USDC on Polygon are different money to a spending limit."""
    policy = PurchasePolicy(per_payment={USDC: 1_000_000})
    decision = policy.evaluate(offer(network="polygon"), NOW)
    assert decision.code == "asset-not-budgeted"
    assert decision.asset.network == "polygon"


# =============================================================================
# Rule 4c: canonicalise by family, never with lower()
# =============================================================================


def test_hex_is_folded_and_base58_is_compared_exactly():
    assert canonical_address(PAYEE) == PAYEE.lower()
    assert canonical_address("  " + PAYEE.upper().replace("0X", "0x") + " ") == (
        PAYEE.lower()
    )
    # Base58 keeps every symbol it was written with.
    assert canonical_address(SOLANA_PAYEE) == SOLANA_PAYEE
    assert canonical_address(USDC_SOLANA) == USDC_SOLANA


def test_an_evm_allowlist_matches_whatever_casing_the_seller_used():
    policy = PurchasePolicy(
        per_payment={USDC: 1_000_000}, only_pay=[PAYEE.lower()]
    )
    assert policy.evaluate(offer(pay_to=PAYEE), NOW).approved
    assert policy.evaluate(offer(pay_to=PAYEE.upper().replace("0X", "0x")), NOW).approved


def test_a_base58_allowlist_matches_the_sellers_own_spelling():
    """Lowercasing a base58 address does not produce the same address spelled
    differently -- it produces a string that is not an address, so an allowlist
    written in the seller's spelling would never match and every legitimate
    payment to that payee would be refused."""
    solana_usdc = TokenAsset("solana", USDC_SOLANA)
    policy = PurchasePolicy(
        per_payment={solana_usdc: 1_000_000}, only_pay=[SOLANA_PAYEE]
    )
    decision = policy.evaluate(
        offer(asset=USDC_SOLANA, pay_to=SOLANA_PAYEE, network="solana"), NOW
    )
    assert decision.approved, getattr(decision, "message", "")


def test_a_lowercased_base58_payee_is_not_on_the_allowlist():
    """The dangerous direction: two distinct base58 addresses can fold to the
    same lowercase string, so folding could admit one nobody put on the list."""
    policy = PurchasePolicy(
        per_payment={TokenAsset("solana", USDC_SOLANA): 1_000_000},
        only_pay=[SOLANA_PAYEE],
    )
    decision = policy.evaluate(
        offer(asset=USDC_SOLANA, pay_to=SOLANA_PAYEE.lower(), network="solana"), NOW
    )
    assert decision.code == "recipient-not-permitted"


def test_the_asset_address_is_canonicalised_by_family_too():
    """A budget keyed on a checksummed contract address must match the same
    contract written lowercase, and a base58 mint must not fold."""
    policy = PurchasePolicy(per_payment={TokenAsset("base", USDC_BASE.lower()): 10})
    assert policy.evaluate(offer(asset=USDC_BASE, amount=10), NOW).approved

    solana = PurchasePolicy(per_payment={TokenAsset("solana", USDC_SOLANA): 10})
    assert solana.evaluate(
        offer(asset=USDC_SOLANA.lower(), network="solana", amount=10), NOW
    ).code == "asset-not-budgeted"


# =============================================================================
# Rule 7: one unreadable offer must not take the list with it
# =============================================================================


def test_a_mixed_accepts_keeps_the_readable_offers():
    parsed = parse_accepts(
        {
            "x402Version": 1,
            "accepts": [
                {"scheme": "batch-settlement", "network": "base", "payTo": PAYEE,
                 "maxAmountRequired": "1", "asset": USDC_BASE},
                {"scheme": "exact", "network": "base", "payTo": PAYEE,
                 "maxAmountRequired": "10000", "asset": USDC_BASE},
                {"scheme": "agent-pay", "network": "base", "payTo": PAYEE,
                 "maxAmountRequired": "2", "asset": USDC_BASE},
            ],
        }
    )
    assert len(parsed.offers) == 1
    assert parsed.offers[0].amount == 10_000
    assert parsed.offered_schemes == ("batch-settlement", "agent-pay")


def test_a_refusal_names_what_the_seller_offered():
    refusal = no_readable_offer(
        [UnreadableOffer(scheme="batch-settlement"), UnreadableOffer(scheme=None)]
    )
    assert refusal.code == "no-readable-offer"
    assert refusal.offered == ("batch-settlement",)
    assert "batch-settlement" in refusal.message


def test_a_scheme_name_from_a_stranger_is_bounded():
    """It is somebody else's string and it ends up in an error message."""
    parsed = parse_accepts(
        [{"scheme": "x" * 500, "network": "base", "payTo": PAYEE,
          "maxAmountRequired": "1", "asset": USDC_BASE}]
    )
    assert len(parsed.offered_schemes[0]) == 64


def test_the_known_scheme_names_stay_readable():
    for scheme in ("exact", "upto", "escrow", "commerce", "fhe-transfer"):
        parsed = parse_accepts(
            [{"scheme": scheme, "network": "base", "payTo": PAYEE,
              "maxAmountRequired": "1", "asset": USDC_BASE}]
        )
        assert len(parsed.offers) == 1, scheme


def test_an_offer_missing_its_money_fields_is_unreadable_not_fatal():
    parsed = parse_accepts(
        {
            "accepts": [
                {"scheme": "exact", "network": "base"},          # no payTo, no amount
                {"scheme": "exact", "network": "base", "payTo": PAYEE,
                 "maxAmountRequired": "1.5", "asset": USDC_BASE},  # not atomic
                {"scheme": "exact", "network": "base", "payTo": PAYEE,
                 "maxAmountRequired": "3000", "asset": USDC_BASE},
                "not even an object",
            ]
        }
    )
    assert [o.amount for o in parsed.offers] == [3_000]
    assert len(parsed.unreadable) == 3


def test_the_v2_amount_spelling_is_read_too():
    parsed = parse_accepts(
        {"x402Version": 2, "accepts": [
            {"scheme": "exact", "network": "eip155:8453", "payTo": PAYEE,
             "amount": "10000", "asset": USDC_BASE}
        ]}
    )
    assert parsed.offers[0].amount == 10_000
    # The CAIP-2 dialect resolves to the SDK's canonical name: one chain, one key.
    assert parsed.offers[0].token_asset.network == "base"


def test_a_seller_that_sent_no_offers_is_not_a_seller_with_unreadable_ones():
    """Three things that used to be one error: no offers, unreadable offers, and
    payable ones."""
    assert parse_accepts({"x402Version": 1, "accepts": []}).offers == ()
    assert parse_accepts({"x402Version": 1, "accepts": []}).unreadable == ()


# =============================================================================
# Rules 5 and 6: validity, read from the seller's own declaration
# =============================================================================


def test_valid_until_is_read_from_the_versioned_key():
    extensions = {OFFER_VALIDITY_EXTENSION: {"info": {"validUntil": NOW}}}
    assert offer_valid_until(extensions) == NOW


def test_validity_at_the_exact_instant_still_stands():
    """Rule 6: `validUntil == now` is the LAST instant the offer is in force."""
    policy = PurchasePolicy(per_payment={USDC: 1_000_000})
    assert policy.evaluate(offer(), NOW, valid_until=NOW).approved
    assert policy.evaluate(offer(), NOW + 1, valid_until=NOW).code == "offer-expired"


def test_no_declared_validity_is_not_expired():
    policy = PurchasePolicy(per_payment={USDC: 1_000_000})
    assert policy.evaluate(offer(), NOW, valid_until=None).approved


@pytest.mark.parametrize(
    "extensions",
    [
        {},
        None,
        {"offer-receipt": {"info": {"validUntil": 1}}},        # unversioned key
        {"offer-receipt/2": {"info": {"validUntil": 1}}},      # a version we do not know
        {OFFER_VALIDITY_EXTENSION: {"validUntil": 1}},         # outside the info envelope
        {OFFER_VALIDITY_EXTENSION: {"info": {}}},
        {OFFER_VALIDITY_EXTENSION: {"info": {"validUntil": "1757500000"}}},
        {OFFER_VALIDITY_EXTENSION: {"info": {"validUntil": None}}},
        {OFFER_VALIDITY_EXTENSION: {"info": {"validUntil": True}}},
        {OFFER_VALIDITY_EXTENSION: {"info": {"validUntil": -5}}},
        {OFFER_VALIDITY_EXTENSION: "a string"},
        {OFFER_VALIDITY_EXTENSION: {"info": "a string"}},
    ],
)
def test_unreadable_validity_is_absent_never_zero(extensions):
    """Rule 5, and the reason it is spelled out: "the seller said something we
    could not read" must not become "this offer expired in 1970", which would
    refuse every payment to that seller."""
    assert offer_valid_until(extensions) is None

    policy = PurchasePolicy(per_payment={USDC: 1_000_000})
    decision = policy.evaluate(offer(), NOW, valid_until=offer_valid_until(extensions))
    assert decision.approved, "an unreadable validity was treated as expired"


def test_the_extensions_ride_along_with_the_parsed_challenge():
    parsed = parse_accepts(
        {
            "accepts": [{"scheme": "exact", "network": "base", "payTo": PAYEE,
                         "maxAmountRequired": "1", "asset": USDC_BASE}],
            "extensions": {OFFER_VALIDITY_EXTENSION: {"info": {"validUntil": NOW}}},
        }
    )
    assert parsed.valid_until == NOW


# =============================================================================
# The comparison against the listing reports and never decides
# =============================================================================


def test_a_reprice_is_reported_and_paid():
    policy = PurchasePolicy(per_payment={USDC: 1_000_000})
    decision = policy.evaluate(
        offer(amount=20_000), NOW, quote=AdvertisedQuote(asset=USDC, amount=10_000)
    )
    assert decision.approved
    assert decision.versus_quote.code == "amount-differs"
    assert decision.versus_quote.advertised_amount == 10_000
    assert decision.versus_quote.offered_amount == 20_000


def test_a_price_in_another_asset_is_not_compared_as_a_number():
    """Rule 4: the same number in another currency is not the same price."""
    policy = PurchasePolicy(per_payment={EURC: 1_000_000})
    decision = policy.evaluate(
        offer(asset=EURC_BASE, amount=10_000),
        NOW,
        quote=AdvertisedQuote(asset=USDC, amount=10_000),
    )
    assert decision.approved
    assert decision.versus_quote.code == "different-asset"


def test_matching_and_uncompared_are_distinguishable():
    policy = PurchasePolicy(per_payment={USDC: 1_000_000})
    assert policy.evaluate(offer(), NOW).versus_quote.code == "not-compared"
    assert policy.evaluate(
        offer(amount=10_000), NOW, quote=AdvertisedQuote(asset=USDC, amount=10_000)
    ).versus_quote.code == "matches"


def test_a_listing_never_turns_an_allowed_offer_into_a_refusal():
    """Whatever the catalog said, only the policy decides."""
    policy = PurchasePolicy(per_payment={USDC: 1_000_000})
    for advertised in (1, 10_000, 999_999_999):
        decision = policy.evaluate(
            offer(amount=20_000),
            NOW,
            quote=AdvertisedQuote(asset=USDC, amount=advertised),
        )
        assert decision.approved, advertised


# =============================================================================
# Odds and ends that would bite in production
# =============================================================================


def test_a_raw_accepts_entry_can_be_evaluated_directly():
    policy = PurchasePolicy(per_payment={USDC: 1_000_000})
    decision = policy.evaluate(
        {"scheme": "exact", "network": "base", "payTo": PAYEE,
         "maxAmountRequired": "10000", "asset": USDC_BASE},
        NOW,
    )
    assert decision.approved and decision.amount == 10_000


def test_an_unreadable_entry_handed_straight_to_evaluate_is_named():
    policy = PurchasePolicy(per_payment={USDC: 1_000_000})
    decision = policy.evaluate({"scheme": "batch-settlement"}, NOW)
    assert decision.code == "no-readable-offer"
    assert decision.offered == ("batch-settlement",)


def test_an_amount_larger_than_a_double_survives_intact():
    """18-decimal tokens put real amounts past 2**53. A ceiling that rounded
    would not be the ceiling that was authorised."""
    huge = 12_345_678_901_234_567_890_123
    gho = TokenAsset("base", "0x6Bb7a212910682DCFdbd5BCBb3e28FB4E8da10Ee")
    policy = PurchasePolicy(per_payment={gho: huge})
    assert policy.evaluate(offer(asset=gho.address, amount=huge), NOW).approved
    decision = policy.evaluate(offer(asset=gho.address, amount=huge + 1), NOW)
    assert decision.code == "per-payment-limit"
    assert decision.to_dict()["requested"] == str(huge + 1)


def test_an_offer_with_no_asset_named_cannot_be_budgeted():
    """The buyer loop has always defaulted these to the network's USDC, so they
    stay readable -- but a written policy refuses what the seller never named."""
    parsed = parse_accepts(
        [{"scheme": "exact", "network": "base", "payTo": PAYEE,
          "maxAmountRequired": "10000"}]
    )
    assert len(parsed.offers) == 1
    decision = PurchasePolicy(per_payment={USDC: 1_000_000}).evaluate(
        parsed.offers[0], NOW
    )
    assert decision.code == "asset-not-budgeted"
    assert PurchasePolicy.permissive().evaluate(parsed.offers[0], NOW).approved


def test_an_approval_says_what_it_approved():
    policy = PurchasePolicy(per_payment={USDC: 1_000_000})
    payload = policy.evaluate(offer(amount=10_000), NOW).to_dict()
    assert payload == {
        "approved": True,
        "asset": {"network": "base", "address": USDC_BASE.lower()},
        "amount": "10000",
        "versusQuote": {"code": "not-compared"},
    }


# =============================================================================
# Portability: one chain, one key, whichever dialect the seller speaks
# =============================================================================


def test_the_same_policy_covers_a_v1_name_and_its_caip2_id():
    """`base` and `eip155:8453` are the SAME chain under two dialects, and the
    same seller can answer either. A policy written in one must cover an offer
    priced in the other -- otherwise a v2 challenge is refused with
    `asset-not-budgeted`, a cause that is not true, and the Rust and TypeScript
    buyers pay what this one refuses."""
    written_as_v1 = PurchasePolicy(per_payment={TokenAsset("base", USDC_BASE): 20_000})
    assert written_as_v1.evaluate(offer(network="base"), NOW).approved
    assert written_as_v1.evaluate(offer(network="eip155:8453"), NOW).approved

    # And the other way round: a policy written in CAIP-2 covers a v1 challenge.
    written_as_v2 = PurchasePolicy(
        per_payment={TokenAsset("eip155:8453", USDC_BASE): 20_000}
    )
    assert written_as_v2.evaluate(offer(network="eip155:8453"), NOW).approved
    assert written_as_v2.evaluate(offer(network="base"), NOW).approved


def test_the_two_dialects_are_literally_the_same_budget_key():
    """Not two entries that both happen to pass: ONE key. A cumulative limit
    that counted the dialects separately would hand the caller a second budget
    for the same chain."""
    assert TokenAsset("base", USDC_BASE) == TokenAsset("eip155:8453", USDC_BASE)

    policy = PurchasePolicy(cumulative={TokenAsset("base", USDC_BASE): 15_000})
    policy.record_spend(TokenAsset("eip155:8453", USDC_BASE), 10_000)
    assert policy.spent(TokenAsset("base", USDC_BASE)) == 10_000
    assert policy.evaluate(offer(network="eip155:8453"), NOW).code == (
        "cumulative-limit"
    )


def test_an_alias_resolves_to_the_canonical_name_too():
    policy = PurchasePolicy(per_payment={TokenAsset("skale-base", USDC_BASE): 20_000})
    assert policy.evaluate(offer(network="skale"), NOW).approved


def test_different_chains_stay_different_keys():
    """The unification is per chain, not a free pass: Base is not Polygon, and
    a CAIP-2 id for another chain is not this one."""
    policy = PurchasePolicy(per_payment={TokenAsset("base", USDC_BASE): 20_000})
    assert policy.evaluate(offer(network="polygon"), NOW).code == "asset-not-budgeted"
    assert policy.evaluate(offer(network="eip155:137"), NOW).code == (
        "asset-not-budgeted"
    )


def test_an_unresolvable_dialect_refuses_rather_than_raising():
    """A chain this build does not carry must not abort an evaluation. It falls
    back to the literal, which no budget holds -- so the answer is a refusal
    with a cause, which is the safe direction for money."""
    policy = PurchasePolicy(per_payment={TokenAsset("base", USDC_BASE): 20_000})
    decision = policy.evaluate(offer(network="eip155:999999999"), NOW)
    assert decision.code == "asset-not-budgeted"
    assert decision.asset.network == "eip155:999999999"


# =============================================================================
# Portability: a payment we cannot NAME is a payment we cannot make
# =============================================================================


def test_an_offer_without_a_scheme_is_unreadable():
    """Aligned with Rust, where `Scheme` is a required field and an entry
    without one fails to deserialize.

    **This changed in 0.82.0**: the SDK assumed `exact`, which is a silent way
    to sign an `exact` authorization for an offer that asked for something else.
    Measured against the one real capture in this repo
    (`tests/test_x402_transport.py`, 36 of 36 live resources answering 402 on
    2026-08-20): every seller names it, so the assumption covered nobody.
    """
    parsed = parse_accepts(
        [{"network": "base", "payTo": PAYEE, "maxAmountRequired": "10000",
          "asset": USDC_BASE}]
    )
    assert parsed.offers == ()
    assert len(parsed.unreadable) == 1
    assert parsed.unreadable[0].scheme is None


@pytest.mark.parametrize("scheme", ["", "   ", None, 7, [], {}])
def test_a_scheme_that_is_not_a_name_is_unreadable(scheme):
    parsed = parse_accepts(
        [{"scheme": scheme, "network": "base", "payTo": PAYEE,
          "maxAmountRequired": "10000", "asset": USDC_BASE}]
    )
    assert parsed.offers == (), scheme


def test_a_refusal_counts_the_offers_that_named_no_scheme():
    """`offered[]` carries the NAMED schemes and nothing else -- it is the wire
    vocabulary the contract fixed. But "offered: []" alone is the message that
    sends a caller hunting a bug in its own code, so the unnamed ones are
    counted in the prose."""
    refusal = no_readable_offer(
        [
            UnreadableOffer(scheme="batch-settlement"),
            UnreadableOffer(scheme=None),
            UnreadableOffer(scheme=None),
        ]
    )
    assert refusal.offered == ("batch-settlement",)
    assert "2 offer(s) named no scheme" in refusal.message
