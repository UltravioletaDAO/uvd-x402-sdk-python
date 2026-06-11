"""
Tests for the RETAINER tier (Execution Market fixed-term contracts).

RETAINER contracts fund one escrow per epoch, so authorizationExpiry cannot
be static: each epoch escrow needs
    auth_seconds = (seconds until ITS epoch ends) + review window + dispute buffer
built with retainer_timings() and passed to build_payment_info(timings=...).

Covers the MASTER_PLAN_RETAINERS Task 0.2 validation matrix: epoch lengths of
1, 7 and 30 days across terms of 1-12 months, with no uint48 overflow.

Run with: pytest tests/test_retainer_tier.py
"""

import time

import pytest

from uvd_x402_sdk.advanced_escrow import (
    AdvancedEscrowClient,
    RETAINER_AUTH_CAP_SECONDS,
    RETAINER_PRE_APPROVAL_SECONDS,
    RETAINER_REFUND_AFTER_AUTH_SECONDS,
    TIER_TIMINGS,
    TaskTier,
    UINT48_MAX,
    retainer_timings,
)

DAY = 86_400
# EM defaults (platform_config retainers.* — MASTER_PLAN_RETAINERS Task 0.3):
REVIEW_WINDOW_SECONDS = 168 * 3600  # 7 days
DISPUTE_BUFFER_SECONDS = 30 * DAY

# Plan validation matrix: epochs of 1, 7 and 30 days in terms of 1-12 months.
EPOCH_LENGTHS_DAYS = (1, 7, 30)
TERMS_MONTHS = tuple(range(1, 13))


def _epochs_for_term(epoch_length_days: int, term_months: int) -> int:
    """Number of epochs that fit a term of N months (30-day months)."""
    return max(1, (term_months * 30) // epoch_length_days)


def _epoch_auth_seconds(epoch_number: int, epoch_length_days: int) -> int:
    """auth offset for epoch k (1-based): end of ITS epoch + review + buffer."""
    return (
        epoch_number * epoch_length_days * DAY
        + REVIEW_WINDOW_SECONDS
        + DISPUTE_BUFFER_SECONDS
    )


class TestRetainerTier:
    """The tier itself: enum member + TIER_TIMINGS entry."""

    def test_retainer_in_task_tier(self):
        assert TaskTier.RETAINER.value == "retainer"

    def test_tier_timings_pre_is_72h(self):
        assert TIER_TIMINGS[TaskTier.RETAINER]["pre"] == RETAINER_PRE_APPROVAL_SECONDS
        assert RETAINER_PRE_APPROVAL_SECONDS == 72 * 3600

    def test_tier_timings_auth_refund_are_dynamic(self):
        """None = must be computed per epoch with retainer_timings()."""
        assert TIER_TIMINGS[TaskTier.RETAINER]["auth"] is None
        assert TIER_TIMINGS[TaskTier.RETAINER]["refund"] is None

    def test_cap_is_400_days(self):
        assert RETAINER_AUTH_CAP_SECONDS == 400 * DAY

    def test_refund_window_is_30_days(self):
        assert RETAINER_REFUND_AFTER_AUTH_SECONDS == 30 * DAY

    def test_static_tiers_unchanged(self):
        """Adding RETAINER must not disturb the existing tiers."""
        assert TIER_TIMINGS[TaskTier.MICRO] == {"pre": 3600, "auth": 7200, "refund": 86400}
        assert TIER_TIMINGS[TaskTier.STANDARD] == {"pre": 7200, "auth": 86400, "refund": 604800}
        assert TIER_TIMINGS[TaskTier.PREMIUM] == {"pre": 14400, "auth": 172800, "refund": 1209600}
        assert TIER_TIMINGS[TaskTier.ENTERPRISE] == {"pre": 86400, "auth": 604800, "refund": 2592000}


class TestRetainerTimings:
    """retainer_timings() — the per-epoch expiry calculator."""

    @pytest.mark.parametrize("epoch_length_days", EPOCH_LENGTHS_DAYS)
    @pytest.mark.parametrize("term_months", TERMS_MONTHS)
    def test_full_plan_matrix(self, epoch_length_days, term_months):
        """Epochs of 1/7/30 days across 1-12 month terms, every epoch escrow."""
        epochs_total = _epochs_for_term(epoch_length_days, term_months)
        previous_auth = 0
        for epoch_number in range(1, epochs_total + 1):
            auth_seconds = _epoch_auth_seconds(epoch_number, epoch_length_days)
            t = retainer_timings(auth_seconds)
            # Shape and invariants per epoch:
            assert t["pre"] == RETAINER_PRE_APPROVAL_SECONDS
            assert t["auth"] == auth_seconds
            assert t["refund"] == auth_seconds + RETAINER_REFUND_AFTER_AUTH_SECONDS
            # The ceremony window must close before any epoch can expire:
            assert t["pre"] < t["auth"] < t["refund"]
            # Later epochs expire strictly later (vault unwinds in order):
            assert t["auth"] > previous_auth
            previous_auth = t["auth"]

    def test_longest_term_fits_the_cap(self):
        """12 months of 30-day epochs is the worst case and must fit 400d."""
        worst = _epoch_auth_seconds(12, 30)  # 360d + 7d review + 30d buffer = 397d
        assert worst == 397 * DAY
        assert retainer_timings(worst)["auth"] == worst

    def test_rejects_auth_below_pre_approval_window(self):
        """The vault cannot expire before the funding ceremony closes."""
        with pytest.raises(ValueError, match="pre-approval"):
            retainer_timings(RETAINER_PRE_APPROVAL_SECONDS)

    def test_rejects_auth_above_cap(self):
        """Beyond 400d fails loudly — clamping would let the payer reclaim
        the escrow (post-authorizationExpiry safety valve) before the epoch
        closes."""
        with pytest.raises(ValueError, match="400 days"):
            retainer_timings(RETAINER_AUTH_CAP_SECONDS + 1)

    def test_cap_boundary_is_inclusive(self):
        t = retainer_timings(RETAINER_AUTH_CAP_SECONDS)
        assert t["auth"] == RETAINER_AUTH_CAP_SECONDS


class TestBuildPaymentInfoRetainer:
    """build_payment_info(tier=RETAINER, timings=...) — absolute expiries."""

    @pytest.fixture()
    def client(self):
        # Dummy key built at runtime (never a literal key in the repo).
        return AdvancedEscrowClient(
            private_key="0x" + "11" * 32,
            rpc_url="http://localhost:9",  # never contacted by build_payment_info
            chain_id=8453,
        )

    RECEIVER = "0x1234567890123456789012345678901234567890"

    def test_retainer_without_timings_fails_loudly(self, client):
        with pytest.raises(ValueError, match="retainer_timings"):
            client.build_payment_info(self.RECEIVER, 1_000_000, tier=TaskTier.RETAINER)

    @pytest.mark.parametrize("epoch_length_days", EPOCH_LENGTHS_DAYS)
    @pytest.mark.parametrize("term_months", (1, 6, 12))
    def test_expiries_no_uint48_overflow(self, client, epoch_length_days, term_months):
        """Absolute expiries for the LAST epoch of each term fit uint48."""
        epochs_total = _epochs_for_term(epoch_length_days, term_months)
        auth_seconds = _epoch_auth_seconds(epochs_total, epoch_length_days)
        before = int(time.time())
        pi = client.build_payment_info(
            self.RECEIVER,
            1_000_000,
            tier=TaskTier.RETAINER,
            timings=retainer_timings(auth_seconds),
        )
        after = int(time.time())
        assert pi.refund_expiry <= UINT48_MAX
        assert pi.pre_approval_expiry < pi.authorization_expiry < pi.refund_expiry
        # Offsets anchored at "now":
        assert before + auth_seconds <= pi.authorization_expiry <= after + auth_seconds
        assert (
            pi.refund_expiry - pi.authorization_expiry
            == RETAINER_REFUND_AFTER_AUTH_SECONDS
        )
        assert (
            pi.pre_approval_expiry - before
            >= RETAINER_PRE_APPROVAL_SECONDS
        )

    def test_uint48_guard_raises_on_overflow(self, client):
        """A pathological timings dict cannot smuggle a >uint48 expiry on-chain."""
        with pytest.raises(ValueError, match="uint48"):
            client.build_payment_info(
                self.RECEIVER,
                1_000_000,
                tier=TaskTier.RETAINER,
                timings={"pre": 3600, "auth": UINT48_MAX, "refund": UINT48_MAX + 1},
            )

    def test_static_tiers_still_work_without_timings(self, client):
        """Regression: existing callers (no timings kwarg) are untouched."""
        pi = client.build_payment_info(self.RECEIVER, 1_000_000, tier=TaskTier.STANDARD)
        t = TIER_TIMINGS[TaskTier.STANDARD]
        assert pi.authorization_expiry - pi.pre_approval_expiry == t["auth"] - t["pre"]
        assert pi.refund_expiry - pi.authorization_expiry == t["refund"] - t["auth"]
