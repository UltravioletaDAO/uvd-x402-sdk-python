

# ---- the release window must outlast the review (prod incident 2026-08-19) ----


def _client():
    from uvd_x402_sdk.advanced_escrow import AdvancedEscrowClient

    c = AdvancedEscrowClient.__new__(AdvancedEscrowClient)
    c.contracts = {"operator": "0x" + "11" * 20, "usdc": "0x" + "22" * 20}
    return c


def test_micro_tier_release_window_outlasts_a_real_review():
    """A 2-hour release window is not survivable by any real approval loop.

    MICRO's raw tier window is 7200s. Measured in production: a release attempted
    26.2 HOURS after `authorizationExpiry` reverted with
    `AfterAuthorizationExpiry`, the worker went unpaid, and the escrow could only
    be moved by the payer's `reclaim()`. 8 escrows stuck on one network in 24h.
    """
    import time

    from uvd_x402_sdk.advanced_escrow import TaskTier
    from uvd_x402_sdk.escrow_signing import REVIEW_WINDOW_SEC

    now = int(time.time())
    pi = _client().build_payment_info("0x" + "33" * 20, 20_000, tier=TaskTier.MICRO)

    assert pi.authorization_expiry - now >= REVIEW_WINDOW_SEC
    # the exact case that failed in production
    assert pi.authorization_expiry > now + int(26.2 * 3600)


def test_deadline_pushes_the_window_out_not_in():
    """A later deadline must extend the release window, never shorten it."""
    import time

    from uvd_x402_sdk.advanced_escrow import TaskTier

    now = int(time.time())
    c = _client()
    base = c.build_payment_info("0x" + "33" * 20, 20_000, tier=TaskTier.MICRO)
    later = c.build_payment_info(
        "0x" + "33" * 20, 20_000, tier=TaskTier.MICRO, deadline=now + 5 * 86400
    )
    assert later.authorization_expiry > base.authorization_expiry


def test_refund_window_always_opens_after_the_release_window_closes():
    """refund_expiry <= authorization_expiry would lock funds with no way out."""
    import time

    from uvd_x402_sdk.advanced_escrow import TaskTier

    c = _client()
    for tier in TaskTier:
        pi = c.build_payment_info("0x" + "33" * 20, 20_000, tier=tier)
        assert pi.pre_approval_expiry <= pi.authorization_expiry, tier
        assert pi.refund_expiry > pi.authorization_expiry, tier
