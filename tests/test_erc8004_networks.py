"""The ERC-8004 network table has to agree with what the facilitator accepts.

Two things get checked here that no other test covered:

1. The names in ``Erc8004Network`` are the ones the facilitator parses. The
   table used to name Base ``base-mainnet``, which the facilitator rejects
   outright (``400 {"error": "Invalid network: base-mainnet"}``) -- so the only
   spelling the type offered for Base was the one that could not work.
2. Every mainnet except SKALE Base carries a validation registry. That address
   was deployed after the identity/reputation pair and was simply missing.
"""

from uvd_x402_sdk.erc8004 import (
    ERC8004_CONTRACTS,
    _MAINNET_IDENTITY,
    _MAINNET_REPUTATION,
    _MAINNET_VALIDATION,
    _wire,
)

# Exactly what GET /feedback -> supportedNetworks returns, plus scroll.
FACILITATOR_NETWORKS = {
    "ethereum", "base", "polygon", "arbitrum", "optimism", "celo", "bsc",
    "monad", "avalanche", "scroll", "skale-base",
    "ethereum-sepolia", "base-sepolia", "polygon-amoy", "arbitrum-sepolia",
    "optimism-sepolia", "celo-sepolia", "avalanche-fuji", "skale-base-sepolia",
    "solana", "solana-devnet",
}

EVM_MAINNETS = {
    "ethereum", "base", "polygon", "arbitrum", "optimism", "celo", "bsc",
    "monad", "avalanche", "scroll",
}


def test_every_facilitator_network_is_in_the_table():
    missing = FACILITATOR_NETWORKS - set(ERC8004_CONTRACTS)
    assert not missing, f"networks the facilitator serves but the SDK omits: {missing}"


def test_the_table_invents_no_network_the_facilitator_rejects():
    # "base-mainnet" is the one deliberate extra: a deprecated alias.
    extra = set(ERC8004_CONTRACTS) - FACILITATOR_NETWORKS
    assert extra == {"base-mainnet"}, f"unexpected network names: {extra}"


def test_base_mainnet_is_rewritten_to_the_name_the_facilitator_parses():
    # Passing this through unchanged is a 400 at the edge, not a 404.
    assert _wire("base-mainnet") == "base"


def test_every_other_name_passes_through_untouched():
    for name in FACILITATOR_NETWORKS:
        assert _wire(name) == name


def test_scroll_uses_the_canonical_mainnet_registries():
    scroll = ERC8004_CONTRACTS["scroll"]
    assert scroll.identity_registry == _MAINNET_IDENTITY
    assert scroll.reputation_registry == _MAINNET_REPUTATION
    assert scroll.validation_registry == _MAINNET_VALIDATION


def test_mainnets_carry_the_validation_registry():
    for name in EVM_MAINNETS:
        assert ERC8004_CONTRACTS[name].validation_registry == _MAINNET_VALIDATION, name


def test_skale_base_is_the_one_mainnet_without_a_validation_registry():
    # Not an omission: there is no code at the canonical address on SKALE Base.
    assert ERC8004_CONTRACTS["skale-base"].validation_registry is None
    assert ERC8004_CONTRACTS["skale-base"].identity_registry == _MAINNET_IDENTITY


def test_the_deprecated_alias_still_resolves_to_the_same_contracts():
    assert ERC8004_CONTRACTS["base-mainnet"] == ERC8004_CONTRACTS["base"]
