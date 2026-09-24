

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


# ---- a chain outside the registry never gets Base's addresses ----

_KEY = "0x" + "4b" * 32  # synthetic, never held funds
_RPC = "http://127.0.0.1:9"  # never contacted


def test_unregistered_chain_without_contracts_is_refused():
    import pytest

    from uvd_x402_sdk.advanced_escrow import AdvancedEscrowClient

    with pytest.raises(ValueError, match="No escrow contracts for chain 999999"):
        AdvancedEscrowClient(private_key=_KEY, chain_id=999999, rpc_url=_RPC)


def test_unregistered_chain_with_explicit_contracts_still_works():
    from uvd_x402_sdk.advanced_escrow import OPERATOR_ABI, AdvancedEscrowClient

    contracts = {
        "operator": "0x" + "0e" * 20,
        "escrow": "0x" + "0a" * 20,
        "token_collector": "0x" + "0c" * 20,
        "usdc": "0x" + "0d" * 20,
    }
    c = AdvancedEscrowClient(
        private_key=_KEY, chain_id=999999, rpc_url=_RPC, contracts=contracts
    )

    assert c.contracts is contracts
    assert c.generation == "v1"
    assert c.operator_contract.abi == OPERATOR_ABI


def test_base_mainnet_is_exactly_as_before():
    """8453 resolves to the same addresses, registry or not.

    The byte-level proof (calldata and /settle body) is
    tests/test_escrow_calldata_snapshot.py; this pins the two ways 8453 can
    resolve: from the registry, and the kept fallback if it ever left it.
    """
    from unittest import mock

    import uvd_x402_sdk.advanced_escrow as ae

    from_registry = ae.AdvancedEscrowClient(private_key=_KEY, chain_id=8453, rpc_url=_RPC)
    assert from_registry.contracts == ae.BASE_MAINNET_CONTRACTS
    default_chain = ae.AdvancedEscrowClient(private_key=_KEY, rpc_url=_RPC)
    assert default_chain.chain_id == 8453
    assert default_chain.contracts == ae.BASE_MAINNET_CONTRACTS

    without_base = {c: v for c, v in ae.ESCROW_CONTRACTS.items() if c != 8453}
    with mock.patch.object(ae, "ESCROW_CONTRACTS", without_base):
        fallback = ae.AdvancedEscrowClient(private_key=_KEY, chain_id=8453, rpc_url=_RPC)
    assert fallback.contracts is ae.BASE_MAINNET_CONTRACTS
