"""Arc joins ERC-8004 (0.90.0), and it is the ONLY thing that moves.

Payments on Arc shipped in 0.84.0; ERC-8004 on Arc did not, although the
facilitator serves it (identity and reputation on both networks since x402-rs
2.37.0, the rater-authored relay on mainnet since 2.38.0). The TypeScript SDK
added it in 2.98.0. What is pinned here:

1. ``arc`` and ``arc-testnet`` carry the registries the facilitator names in
   ARC_MAINNET_CONTRACTS / ARC_TESTNET_CONTRACTS. Each address was read
   on-chain on 2026-09-23 before it was written down (``eth_getCode``: a
   130-byte EIP-1967 proxy; ``getVersion()`` = 2.0.0).
2. ``arc`` is on the relayed rail; ``arc-testnet`` is not. The facilitator
   answers ``prepare`` on arc with the v4 delegate and refuses arc-testnet with
   a 400: no delegate was deployed there, and mainnet having one says nothing
   about testnet.
3. Every network 0.89.0 exported is exactly what it was. The snapshots below
   are the lists as 0.89.0 built them, written out rather than derived from
   the code under test, so an edit that "tidies" another entry fails here
   instead of shipping.
"""

from typing import get_args

from uvd_x402_sdk.erc8004 import (
    ERC8004_CONTRACTS,
    RELAYED_FEEDBACK_NETWORKS,
    SOLANA_FEEDBACK_NETWORKS,
    Erc8004ContractAddresses,
    Erc8004Network,
    supports_relayed_feedback,
    supports_solana_feedback,
)

MAINNET = Erc8004ContractAddresses(
    identity_registry="0x8004A169FB4a3325136EB29fA0ceB6D2e539a432",
    reputation_registry="0x8004BAa17C55a88189AE136b182e5fdA19dE9b63",
    validation_registry="0x8004Cc8439f36fd5F9F049D9fF86523Df6dAAB58",
)
TESTNET = Erc8004ContractAddresses(
    identity_registry="0x8004A818BFB912233c491871b3d84c89A494BD9e",
    reputation_registry="0x8004B663056A597Dffe9eCcC1965A193B7388713",
    validation_registry="0x8004Cb1BF31DAf7788923b405b754f57acEB4272",
)
SKALE_MAINNET = Erc8004ContractAddresses(
    identity_registry=MAINNET.identity_registry,
    reputation_registry=MAINNET.reputation_registry,
)
SOLANA = Erc8004ContractAddresses(
    agent_registry_program="8oo4dC4JvBLwy5tGgiH3WwK4B9PWxL9Z4XjA2jzkQMbQ",
    atom_engine_program="AToMw53aiPQ8j7iHVb4fGt6nzUNxUhcPc3tbPBZuzVVb",
)

# ``ERC8004_CONTRACTS`` as 0.89.0 exported it: 22 keys.
CONTRACTS_0_89_0 = {
    "ethereum": MAINNET,
    "base": MAINNET,
    "polygon": MAINNET,
    "arbitrum": MAINNET,
    "optimism": MAINNET,
    "celo": MAINNET,
    "bsc": MAINNET,
    "monad": MAINNET,
    "avalanche": MAINNET,
    "scroll": MAINNET,
    "skale-base": SKALE_MAINNET,
    "base-mainnet": MAINNET,
    "ethereum-sepolia": TESTNET,
    "base-sepolia": TESTNET,
    "polygon-amoy": TESTNET,
    "arbitrum-sepolia": TESTNET,
    "optimism-sepolia": TESTNET,
    "celo-sepolia": TESTNET,
    "avalanche-fuji": TESTNET,
    "skale-base-sepolia": TESTNET,
    "solana": SOLANA,
    "solana-devnet": SOLANA,
}

# ``Erc8004Network`` as 0.89.0 declared it: the same 22 names.
NETWORKS_0_89_0 = set(CONTRACTS_0_89_0)

# ``RELAYED_FEEDBACK_NETWORKS`` as 0.89.0 exported it.
RELAYED_0_89_0 = {
    "base", "ethereum", "polygon", "arbitrum", "optimism", "celo", "bsc", "monad",
    "base-sepolia",
}

# ``SOLANA_FEEDBACK_NETWORKS`` as 0.89.0 exported it.
SOLANA_0_89_0 = {"solana", "solana-devnet"}


# -- Arc in the ERC-8004 table ------------------------------------------------


def test_arc_names_the_canonical_mainnet_registries():
    assert ERC8004_CONTRACTS["arc"] == MAINNET


def test_arc_testnet_names_the_canonical_testnet_registries():
    # Unlike SKALE, the validation registry is deployed here too, so leaving it
    # out would be the omission.
    assert ERC8004_CONTRACTS["arc-testnet"] == TESTNET


def test_both_arc_networks_are_erc8004_networks():
    assert {"arc", "arc-testnet"} <= set(get_args(Erc8004Network))


# -- Arc on the relayed feedback rail -----------------------------------------


def test_arc_is_on_the_relayed_rail():
    assert "arc" in RELAYED_FEEDBACK_NETWORKS
    assert supports_relayed_feedback("arc") is True


def test_arc_testnet_is_not_on_the_relayed_rail():
    # ``POST /feedback/evm/prepare`` on arc-testnet, facilitator 2.39.1:
    # 400 "relayed feedback is not available on arc-testnet: no FeedbackDelegate
    # is deployed there yet". Serving reads is not serving the relay.
    assert "arc-testnet" not in RELAYED_FEEDBACK_NETWORKS
    assert supports_relayed_feedback("arc-testnet") is False


def test_neither_arc_network_is_routed_to_the_solana_rail():
    assert supports_solana_feedback("arc") is False
    assert supports_solana_feedback("arc-testnet") is False


def test_the_closing_check_prints_true_false(capsys):
    # The exact line run against the published package after release.
    from uvd_x402_sdk.erc8004 import supports_relayed_feedback as s

    print(s("arc"), s("arc-testnet"))
    assert capsys.readouterr().out == "True False\n"


# -- nothing but Arc moved since 0.89.0 ---------------------------------------


def test_the_table_adds_exactly_arc_and_arc_testnet():
    assert set(ERC8004_CONTRACTS) - set(CONTRACTS_0_89_0) == {"arc", "arc-testnet"}


def test_the_table_removes_nothing():
    assert set(CONTRACTS_0_89_0) - set(ERC8004_CONTRACTS) == set()


def test_every_0_89_0_entry_is_unchanged():
    for network, contracts in CONTRACTS_0_89_0.items():
        assert ERC8004_CONTRACTS[network] == contracts, network


def test_the_network_type_adds_exactly_arc_and_arc_testnet():
    assert set(get_args(Erc8004Network)) == NETWORKS_0_89_0 | {"arc", "arc-testnet"}


def test_the_relayed_rail_adds_exactly_arc():
    assert set(RELAYED_FEEDBACK_NETWORKS) == RELAYED_0_89_0 | {"arc"}


def test_the_solana_rail_is_untouched():
    assert set(SOLANA_FEEDBACK_NETWORKS) == SOLANA_0_89_0
