"""The ERC-8004 lists of this SDK are the ones the TypeScript SDK publishes.

Execution Market and KarmaKadabra use this package; other integrators use
``uvd-x402-sdk`` on npm. Both decide from these lists which network can hold a
reputation and which one takes a rater-authored rating. The two SDKs are edited
by hand in two repos, so they can drift silently: a network can be routable in
one runtime and refused before the request in the other.

PROVENANCE of ``tests/fixtures/erc8004-ts.json``: generated from the PUBLISHED
npm package, never edited by hand. Its ``source`` block names the release and
the tarball's integrity hash. Regenerate or check it with::

    node scripts/erc8004_ts_snapshot.mjs --version 2.98.0   # rewrite the fixture
    node scripts/erc8004_ts_snapshot.mjs --check            # exit 1 if it differs from npm

When the TypeScript SDK changes a list, regenerate the fixture against its new
release and bump ``TS_RELEASE`` below. These tests then fail until this package
carries the same change.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, get_args

import pytest

from uvd_x402_sdk.erc8004 import (
    ERC8004_CONTRACTS,
    RELAYED_FEEDBACK_NETWORKS,
    SOLANA_FEEDBACK_NETWORKS,
    Erc8004ContractAddresses,
    Erc8004Network,
    _wire,
    supports_relayed_feedback,
    supports_solana_feedback,
)

FIXTURE_PATH = Path(__file__).resolve().parent / "fixtures" / "erc8004-ts.json"

# The release the fixture was taken from. Bumped together with the fixture.
TS_RELEASE = "2.98.0"

TS = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
NAMES = sorted(TS["wireNetwork"])


def _camel(field: str) -> str:
    head, *rest = field.split("_")
    return head + "".join(word.capitalize() for word in rest)


def _as_ts(contracts: Erc8004ContractAddresses) -> dict[str, Any]:
    """The Python model in the shape the TypeScript table uses: camelCase keys,
    unset registries left out rather than written as ``undefined``."""
    return {_camel(k): v for k, v in contracts.model_dump().items() if v is not None}


def test_the_fixture_is_the_pinned_published_release():
    source = TS["source"]
    assert source["package"] == "uvd-x402-sdk"
    assert source["version"] == TS_RELEASE
    assert source["resolved"] == (
        f"https://registry.npmjs.org/uvd-x402-sdk/-/uvd-x402-sdk-{TS_RELEASE}.tgz"
    )
    assert source["integrity"].startswith("sha512-")
    assert TS["generatedBy"] == f"node scripts/erc8004_ts_snapshot.mjs --version {TS_RELEASE}"


def test_the_network_type_names_the_same_networks():
    python, ts = set(get_args(Erc8004Network)), set(TS["erc8004Network"])
    assert python == ts, (
        f"only in Python: {sorted(python - ts)}; only in TypeScript: {sorted(ts - python)}"
    )


def test_the_contract_table_has_the_same_networks():
    python, ts = set(ERC8004_CONTRACTS), set(TS["erc8004Contracts"])
    assert python == ts, (
        f"only in Python: {sorted(python - ts)}; only in TypeScript: {sorted(ts - python)}"
    )


@pytest.mark.parametrize("network", sorted(TS["erc8004Contracts"]))
def test_every_network_has_the_same_addresses(network):
    # Exact strings, checksum case included: both tables are written by hand.
    assert network in ERC8004_CONTRACTS, network
    assert _as_ts(ERC8004_CONTRACTS[network]) == TS["erc8004Contracts"][network]


def test_the_relayed_rail_serves_the_same_networks():
    python, ts = set(RELAYED_FEEDBACK_NETWORKS), set(TS["relayedFeedbackNetworks"])
    assert python == ts, (
        f"only in Python: {sorted(python - ts)}; only in TypeScript: {sorted(ts - python)}"
    )


def test_the_solana_rail_serves_the_same_networks():
    assert set(SOLANA_FEEDBACK_NETWORKS) == set(TS["solanaFeedbackNetworks"])


@pytest.mark.parametrize("network", NAMES)
def test_the_helpers_answer_like_the_typescript_ones(network):
    # Behaviour, not only list contents: the base-mainnet alias goes through
    # the wire rewrite in both runtimes before the list is consulted.
    assert _wire(network) == TS["wireNetwork"][network]
    assert supports_relayed_feedback(network) is TS["supportsRelayedFeedback"][network]
    assert supports_solana_feedback(network) is TS["supportsSolanaFeedback"][network]
