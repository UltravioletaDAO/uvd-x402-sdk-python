"""v0.71.0: build_uvd_feedback_params — the TAGGING-UVD convention, fail-loud.

Why this exists (D7 audit E5, measured 2026-08-30): every DAO emitter
hand-builds tag1/tag2 today, and the reader resolves roles by ALLOWLIST — a
typo does not error anywhere, it publishes as `direction: null` and the rating
silently loses its axis. This builder turns that silent loss into a ValueError
at emit time. The role set mirrors the reader (describe-net rating_roles.ROLES,
5 ratified roles as of 2026-08-29) and is PINNED here: extending it is a
release decision announced in #agents, not a convenience edit.
"""

import pytest

from uvd_x402_sdk import (
    UVD_FEEDBACK_ROLES,
    UVD_PRODUCTS,
    build_uvd_feedback_params,
)

BASE = dict(agent_id=2106, value=95, role="worker_rating", category="research", product="em")


class TestHappyPath:
    def test_tags_built_per_convention(self):
        fp = build_uvd_feedback_params(
            **BASE, context="0xe4dc963c",
            feedback_uri="https://execution.market/feedback/1",
        )
        assert fp.tag1 == "worker_rating:research"
        assert fp.tag2 == "em|0xe4dc963c"
        assert fp.feedback_uri == "https://execution.market/feedback/1"

    def test_empty_context_emits_bare_product(self):
        fp = build_uvd_feedback_params(**BASE)
        assert fp.tag2 == "em"

    def test_all_ratified_roles_pass(self):
        for role in UVD_FEEDBACK_ROLES:
            fp = build_uvd_feedback_params(**{**BASE, "role": role})
            assert fp.tag1.startswith(role + ":")


class TestTheSetsArePinned:
    """Mirror discipline of the reader: a role enters when EMITTED and
    ratified. Red here = you are changing a cross-project contract — announce
    in #agents and cite the emission count, same as rating_roles.ROLES."""

    def test_roles(self):
        assert UVD_FEEDBACK_ROLES == frozenset(
            {
                "worker_rating",
                "agent_rating",
                "executor_rating",
                "requester_rating",
                "buyer_rating",
                # seller_rating stays OUT: zero emissions (reader rule).
            }
        )

    def test_products(self):
        assert UVD_PRODUCTS == frozenset({"em", "kk", "mesh", "dn"})


class TestFailLoud:
    @pytest.mark.parametrize(
        "bad",
        [
            {"role": "dexter"},           # the exact typo class the reader nulls
            {"role": "seller_rating"},    # promised, not ratified
            {"product": "acme"},
            {"category": ""},
            {"category": "a:b"},          # tag1's own separator
            {"category": "a|b"},          # tag2's separator inside tag1 field
            {"context": "a|b"},
            {"context": "a:b"},
            {"feedback_uri": "ipfs://QmX"},   # no host -> provenance lost
            {"feedback_uri": "http://x.co"},  # https required
            {"feedback_uri": "https://"},     # host missing
            {"feedback_hash": "0x123"},
            {"value_decimals": 19},
            {"value_decimals": -1},
        ],
    )
    def test_raises(self, bad):
        with pytest.raises(ValueError):
            build_uvd_feedback_params(**{**BASE, **bad})
