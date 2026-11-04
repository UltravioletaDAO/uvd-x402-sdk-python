"""No relay means no RPC, signature, payment or evidence, including CAIP-2 aliases."""

from __future__ import annotations

from dataclasses import replace

import pytest

pytest.importorskip("eth_account")

from tests.reputation_doubles import Env, FakeChain, make_entry  # noqa: E402
from tests.test_reputation_payment import CountingSigner  # noqa: E402
from uvd_x402_sdk.erc8004 import _ERC8004_CHAIN_IDS, _wire  # noqa: E402
from uvd_x402_sdk.networks import get_network  # noqa: E402
from uvd_x402_sdk.reputation import InMemoryRatingOutbox  # noqa: E402
from uvd_x402_sdk.reputation._chains import identity_registry, relay_chain_id  # noqa: E402

# All EVM registries without a relay, including those without payment support.
NO_RELAY = {
    "avalanche": 43114,
    "scroll": 534352,
    "skale-base": 1187947933,
    "skale-base-sepolia": 324705682,
    "arc-testnet": 5042002,
    "robinhood": 4663,
    "robinhood-testnet": 46630,
    "ethereum-sepolia": 11155111,
    "polygon-amoy": 80002,
    "arbitrum-sepolia": 421614,
    "optimism-sepolia": 11155420,
    "celo-sepolia": 11142220,
    "avalanche-fuji": 43113,
}
NETWORKS = [
    (spelling, cid) for name, cid in NO_RELAY.items() for spelling in (name, f"eip155:{cid}")
]


def test_every_evm_registry_without_a_relay_has_a_worker_case():
    assert NO_RELAY == {
        name: cid for name, cid in _ERC8004_CHAIN_IDS.items() if relay_chain_id(name) is None
    }


def no_relay_env(network, chain_id, *, amount=1000, worker_class=None):
    """The same payable scenario for the regression and its guard mutant."""
    env = Env(InMemoryRatingOutbox())
    config = get_network(_wire(network))
    token = getattr(config, "usdc_address", None)
    chain_args = {"token": token} if token else {}
    env.chain = FakeChain(
        env.clock, chain_id=chain_id, identity=identity_registry(network), **chain_args
    )
    env.facilitator.chain = env.chain
    env.worker.rpc = env.chain
    env.signer = CountingSigner(env.account)
    env.worker.signer = env.signer
    if worker_class is not None:
        env.worker.__class__ = worker_class
    env.own()
    env.publish_calls = []
    original_publish = env.worker.publish_evidence

    def publish(entry, proof):
        env.publish_calls.append(entry.ref)
        return original_publish(entry, proof)

    env.worker.publish_evidence = publish
    entries = [
        replace(
            make_entry(("no-relay", str(i)), amount=amount, created_at=env.clock.now + i),
            network=network,
        )
        for i in range(2)
    ]
    for entry in entries:
        env.outbox.enqueue(entry)
    return env, entries


async def no_relay_rows(network, chain_id, *, amount=1000, worker_class=None):
    env, entries = no_relay_env(network, chain_id, amount=amount, worker_class=worker_class)
    try:
        for _ in range(6):
            await env.worker.tick()
            env.clock.now += 60
        env.facilitator.assert_consumed()
        return env, [env.outbox.get(entry.ref) for entry in entries]
    finally:
        await env.relay._client.aclose()


@pytest.mark.parametrize("network,chain_id", NETWORKS)
@pytest.mark.parametrize("amount", [0, 1000])
async def test_no_relay_rejects_both_rows_before_rpc_or_signing(network, chain_id, amount):
    env, rows = await no_relay_rows(network, chain_id, amount=amount)
    assert [(row.status, row.error) for row in rows] == [
        ("local_only", "unknown_network"),
        ("local_only", "unknown_network"),
    ]
    assert all(row.payment is None and row.rater is None for row in rows)
    assert env.chain.calls == []
    assert env.chain.broadcasts == []
    assert env.signer.transactions == 0
    assert env.publish_calls == [] and env.served == {}
    assert env.facilitator.prepares == [] and env.facilitator.submits == []
    assert not env.breaker.is_open()


@pytest.mark.parametrize("network", ["base", "base-mainnet", "eip155:8453"])
async def test_a_relay_network_still_pays_and_rates_under_each_spelling(network):
    env = Env(InMemoryRatingOutbox())
    env.own()
    entry = replace(make_entry(created_at=env.clock.now), network=network)
    env.outbox.enqueue(entry)
    async with env.relay:
        row = await env.drive(entry.ref)
    assert row.status == "confirmed"
    assert len(set(env.chain.broadcasts)) == 1
    assert row.payment["network"] == "base"
    assert len(env.served) == 1
    assert len(env.facilitator.prepares) == len(env.facilitator.submits) == 1
    env.facilitator.assert_consumed()


@pytest.mark.parametrize(
    "network",
    [
        "Base",
        " base",
        "base ",
        "EIP155:8453",
        "eip155:08453",
        "eip155:0x2105",
        "eip155:0X2105",
        "eip155:",
        "",
        "eip155:" + chr(0xFF18) + "453",
    ],
)
async def test_a_near_miss_is_refused_before_rpc(network):
    env, rows = await no_relay_rows(network, 8453)
    assert [(row.status, row.error) for row in rows] == [("local_only", "unknown_network")] * 2
    assert env.chain.calls == [] and env.chain.broadcasts == []
    assert env.signer.transactions == 0 and env.publish_calls == []
