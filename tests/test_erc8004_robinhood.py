"""Robinhood Chain joins ERC-8004 (0.110.0), and it is the ONLY network that moves.

Payments on Robinhood (Paxos USDG) shipped before; ERC-8004 there did not.
Execution Market rates on Robinhood Chain through this module. The TypeScript
SDK makes the same change (``uvd-x402-sdk-typescript`` PR #70). What is pinned
here, offline:

1. ``robinhood`` (chain 4663) and ``robinhood-testnet`` (chain 46630) carry the
   singleton identity, reputation and validation registries. Each address was
   read on-chain on 2026-11-04 before it was written down (``eth_chainId``
   0x1237 / 0xb626; ``eth_getCode``: a 130-byte EIP-1967 proxy; ``getVersion()``
   = 2.0.0; identity and reputation run the implementations Base / Base Sepolia
   run; each validation registry's ``getIdentityRegistry()`` is the identity
   registry of its network).
2. Their CAIP-2 names (``eip155:4663``, ``eip155:46630``) resolve to the same
   entries, as the CAIP-2 name of every EVM network does
   (tests/test_erc8004_caip2_aliases.py walks the whole table, reads and writes
   included). Exact strings only; no other spelling becomes Robinhood.
3. Neither network is on a feedback rail (no ``FeedbackDelegate``, not Solana),
   so preparation and evidence refuse both. The worker also refuses queued
   rows before RPC or anchor-payment signing (test_reputation_networks.py).
   0.110.0 added no escrow row for either chain; the release after it adds the
   mainnet's (x402r's canonical set with USDG, tests/test_robinhood_escrow.py),
   and the testnet still has none.
4. Every network 0.107.0 exported is exactly what it was. The snapshots below
   are the lists as 0.107.0 built them, written out rather than derived from the
   code under test.

The facilitator serves ERC-8004 on Robinhood from the x402-rs release that adds
it; 2.55.0 did not yet (``400 "ERC-8004 is not supported on network robinhood"``,
2026-11-04).
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any, get_args

import httpx
import pytest

from uvd_x402_sdk.erc8004 import (
    ERC8004_CONTRACTS,
    RELAYED_FEEDBACK_NETWORKS,
    SOLANA_FEEDBACK_NETWORKS,
    Erc8004Client,
    Erc8004ContractAddresses,
    Erc8004Network,
    _wire,
    supports_relayed_feedback,
    supports_solana_feedback,
)
from uvd_x402_sdk.escrow_contracts import (
    ESCROW_CONTRACTS,
    ESCROW_MAX_FEE_BPS,
    ESCROW_OPERATORS,
)
from uvd_x402_sdk.networks.base import (
    get_network,
    get_network_by_chain_id,
    normalize_network,
    to_caip2_network,
)

VECTORS = Path(__file__).resolve().parent / "vectors" / "reputation"

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

#: (what the caller writes, the name the facilitator parses, its registries, chain id)
SPELLINGS = [
    ("robinhood", "robinhood", MAINNET, 4663),
    ("eip155:4663", "robinhood", MAINNET, 4663),
    ("robinhood-testnet", "robinhood-testnet", TESTNET, 46630),
    ("eip155:46630", "robinhood-testnet", TESTNET, 46630),
]
SPELLING_IDS = [s[0] for s in SPELLINGS]

#: Strings that are NOT Robinhood: case, whitespace, other CAIP-2 forms of the
#: same chain id, look-alikes, separators, and names a dict or object has.
NOT_ALIASES = [
    "", "Robinhood", "ROBINHOOD", " robinhood", "robinhood ", "robinhood\n",
    "robinhood-testnet ", "Robinhood-Testnet", "robinhood_testnet", "robinhoodtestnet",
    "EIP155:4663", "Eip155:4663", "eip155:04663", "eip155:4663 ", " eip155:46630",
    "eip155:4663\n", "eip155: 4663", "eip155:0x1237", "eip155:0X1237", "eip155:4663.0",
    "eip155:466300", "eip155:466", "eip155:", "eip155", "4663", "46630",
    "caip2:eip155:4663", "eip155:4663/robinhood",
    "robinh" + chr(0x043E) + "od",  # Cyrillic o
    "robinhood" + chr(0x200B),  # zero-width space
    chr(0xFF52) + "obinhood",  # full-width r
    "robinhood/mainnet", "robinhood\\mainnet",
    "unknown", "get", "keys", "__class__", "__dict__",
]

#: Not strings at all: an alias table must not raise nor resolve them.
NOT_STRINGS = [None, 4663, 46630, b"robinhood", b"eip155:4663", ["robinhood"], {"robinhood": 1}]

# -- 0.107.0, written out -----------------------------------------------------

_SKALE_0_107_0 = Erc8004ContractAddresses(
    identity_registry="0x8004A169FB4a3325136EB29fA0ceB6D2e539a432",
    reputation_registry="0x8004BAa17C55a88189AE136b182e5fdA19dE9b63",
)
_SOLANA_0_107_0 = Erc8004ContractAddresses(
    agent_registry_program="8oo4dC4JvBLwy5tGgiH3WwK4B9PWxL9Z4XjA2jzkQMbQ",
    atom_engine_program="AToMw53aiPQ8j7iHVb4fGt6nzUNxUhcPc3tbPBZuzVVb",
)

#: ``ERC8004_CONTRACTS`` as 0.107.0 exported it: 24 keys.
CONTRACTS_0_107_0 = {
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
    "skale-base": _SKALE_0_107_0,
    "arc": MAINNET,
    "base-mainnet": MAINNET,
    "ethereum-sepolia": TESTNET,
    "base-sepolia": TESTNET,
    "polygon-amoy": TESTNET,
    "arbitrum-sepolia": TESTNET,
    "optimism-sepolia": TESTNET,
    "celo-sepolia": TESTNET,
    "avalanche-fuji": TESTNET,
    "skale-base-sepolia": TESTNET,
    "arc-testnet": TESTNET,
    "solana": _SOLANA_0_107_0,
    "solana-devnet": _SOLANA_0_107_0,
}

#: ``Erc8004Network`` as 0.107.0 declared it: the same 24 names.
NETWORKS_0_107_0 = set(CONTRACTS_0_107_0)

#: The CAIP-2 names ``Erc8004Network`` gains with this release: one per EVM
#: network (tests/test_erc8004_caip2_aliases.py derives the same set).
CAIP2_NAMES = {
    "eip155:1", "eip155:8453", "eip155:137", "eip155:42161", "eip155:10", "eip155:42220",
    "eip155:56", "eip155:143", "eip155:43114", "eip155:534352", "eip155:1187947933",
    "eip155:5042", "eip155:4663",
    "eip155:11155111", "eip155:84532", "eip155:80002", "eip155:421614", "eip155:11155420",
    "eip155:11142220", "eip155:43113", "eip155:324705682", "eip155:5042002", "eip155:46630",
}

#: ``RELAYED_FEEDBACK_NETWORKS`` and ``SOLANA_FEEDBACK_NETWORKS`` as 0.107.0 exported them.
RELAYED_0_107_0 = {
    "base", "ethereum", "polygon", "arbitrum", "optimism", "celo", "bsc", "monad", "arc",
    "base-sepolia",
}
SOLANA_0_107_0 = {"solana", "solana-devnet"}


@pytest.fixture
def offline_client() -> Erc8004Client:
    """A client whose every request fails the test: local lookups only."""

    def refuse(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"a local lookup sent {request.method} {request.url}")

    client = Erc8004Client(base_url="https://facilitator.example")
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(refuse))
    return client


# -- the registries -----------------------------------------------------------


def test_robinhood_names_the_canonical_mainnet_registries():
    assert ERC8004_CONTRACTS["robinhood"] == MAINNET


def test_robinhood_testnet_names_the_canonical_testnet_registries():
    # Validation included: unlike SKALE, the canonical address has code here.
    assert ERC8004_CONTRACTS["robinhood-testnet"] == TESTNET
    assert ERC8004_CONTRACTS["robinhood-testnet"].validation_registry is not None


def test_robinhood_carries_no_solana_program():
    for name in ("robinhood", "robinhood-testnet"):
        assert ERC8004_CONTRACTS[name].agent_registry_program is None, name
        assert ERC8004_CONTRACTS[name].atom_engine_program is None, name


def test_all_four_spellings_are_erc8004_networks():
    assert {"robinhood", "robinhood-testnet", "eip155:4663", "eip155:46630"} <= set(
        get_args(Erc8004Network)
    )


def test_the_caip2_names_have_no_entry_of_their_own():
    # The table is keyed by the names the facilitator parses (the TypeScript
    # table too); an alias is resolved before the lookup, not stored twice.
    assert "eip155:4663" not in ERC8004_CONTRACTS
    assert "eip155:46630" not in ERC8004_CONTRACTS


@pytest.mark.parametrize("network, canonical, contracts, chain_id", SPELLINGS, ids=SPELLING_IDS)
def test_every_spelling_resolves_to_its_registries(
    offline_client, network, canonical, contracts, chain_id
):
    assert _wire(network) == canonical
    assert offline_client.is_available(network) is True
    assert offline_client.get_contracts(network) == contracts
    assert offline_client.get_contracts(network) is ERC8004_CONTRACTS[canonical]


@pytest.mark.parametrize("network, canonical, contracts, chain_id", SPELLINGS, ids=SPELLING_IDS)
def test_the_aliases_are_the_payment_registrys_names(network, canonical, contracts, chain_id):
    # One chain, one name: the alias is the CAIP-2 name the payment registry
    # gives the network, and it resolves back to the same plain name there.
    assert to_caip2_network(canonical) == f"eip155:{chain_id}"
    assert normalize_network(f"eip155:{chain_id}") == canonical
    assert normalize_network(network) == canonical
    registered = get_network(canonical)
    assert registered is not None and registered.chain_id == chain_id
    by_id = get_network_by_chain_id(chain_id)
    assert by_id is not None and by_id.name == canonical


@pytest.mark.parametrize("network", NOT_ALIASES, ids=repr)
def test_no_other_string_becomes_a_registered_network(offline_client, network):
    assert _wire(network) == network
    assert offline_client.is_available(network) is False
    assert offline_client.get_contracts(network) is None


@pytest.mark.parametrize("network", NOT_STRINGS, ids=repr)
def test_a_value_that_is_not_a_string_is_no_network(offline_client, network):
    assert _wire(network) is network
    assert offline_client.is_available(network) is False
    assert offline_client.get_contracts(network) is None


def test_the_deprecated_base_alias_still_resolves(offline_client):
    assert _wire("base-mainnet") == "base"
    assert offline_client.is_available("base-mainnet") is True
    assert offline_client.get_contracts("base-mainnet") == ERC8004_CONTRACTS["base"]
    assert ERC8004_CONTRACTS["base-mainnet"] == ERC8004_CONTRACTS["base"]


# -- no rail; escrow on the mainnet only ---------------------------------------


@pytest.mark.parametrize("network, canonical, contracts, chain_id", SPELLINGS, ids=SPELLING_IDS)
def test_neither_network_is_on_a_feedback_rail(network, canonical, contracts, chain_id):
    # No FeedbackDelegate is deployed on either chain: an invented one would be
    # a type-4 transaction to an account with no code, which "succeeds".
    assert supports_relayed_feedback(network) is False
    assert supports_solana_feedback(network) is False
    assert canonical not in RELAYED_FEEDBACK_NETWORKS
    assert canonical not in SOLANA_FEEDBACK_NETWORKS


def _relayed_base_case() -> dict[str, Any]:
    cases = json.loads((VECTORS / "relayed-feedback.json").read_bytes().decode("utf-8"))
    return copy.deepcopy(next(c for c in cases["cases"] if c["name"] == "base-first-rating"))


@pytest.mark.parametrize("network, canonical, contracts, chain_id", SPELLINGS, ids=SPELLING_IDS)
def test_the_rating_rail_refuses_robinhood_before_signing(network, canonical, contracts, chain_id):
    pytest.importorskip("eth_account")
    from uvd_x402_sdk.reputation import PreparationRejected, RatingParams, validate_preparation
    from uvd_x402_sdk.reputation._chains import relay_chain_id, reputation_registry

    # The registry is found through the SDK's one table, yet the rail stays shut:
    # the gate is the relay's chain id, and Robinhood has none.
    assert reputation_registry(network) == contracts.reputation_registry
    assert relay_chain_id(network) is None

    case = _relayed_base_case()
    params = case["params"]
    prepare = dict(case["prepare"], chainId=chain_id)
    with pytest.raises(PreparationRejected) as caught:
        validate_preparation(
            prepare,
            RatingParams(
                agent_id=params["agentId"],
                value=params["value"],
                tag1=params["tag1"],
                tag2=params["tag2"],
                endpoint=params["endpoint"],
                feedback_uri=params["feedbackURI"],
                feedback_hash=params["feedbackHash"],
            ),
            case["rater"],
            network=network,
            now=case["now"],
        )
    assert caught.value.code == "unknown_network"


@pytest.mark.parametrize("network, canonical, contracts, chain_id", SPELLINGS, ids=SPELLING_IDS)
def test_the_evidence_document_refuses_robinhood(network, canonical, contracts, chain_id):
    pytest.importorskip("eth_account")
    from uvd_x402_sdk.erc8004 import ProofOfPayment
    from uvd_x402_sdk.reputation import EvidenceRejected, evidence_document

    vectors = json.loads((VECTORS / "evidence-document.json").read_bytes().decode("utf-8"))
    case = next(c for c in vectors["rejected"] if c["name"] == "network-without-relay")
    assert case["code"] == "unknown_network"
    inputs = case["inputs"]
    with pytest.raises(EvidenceRejected) as caught:
        evidence_document(
            network=network,
            agent_id=inputs["agentId"],
            rater=inputs["rater"],
            subject=inputs["subject"],
            value=inputs["value"],
            tag1=inputs["tag1"],
            tag2=inputs["tag2"],
            endpoint=inputs["endpoint"],
            created_at=inputs["createdAt"],
            salt=bytes.fromhex(inputs["saltHex"]),
            private=inputs["private"],
            wallet_binding=inputs["walletBinding"],
            proof=ProofOfPayment.model_validate(inputs["proof"]) if inputs["proof"] else None,
            issuer=inputs["issuer"],
            context=inputs["context"],
        )
    assert caught.value.code == "unknown_network"


def test_the_mainnet_has_escrow_rows_and_the_testnet_none():
    # The mainnet's rows are pinned against the chain in test_robinhood_escrow.
    # The testnet has none: build_escrow_pre_auth refuses a chain without a row
    # in each table (test_escrow_signing_rows), and Execution Market's operator
    # has no code there.
    assert 4663 in ESCROW_CONTRACTS
    assert 4663 in ESCROW_OPERATORS
    assert 4663 in ESCROW_MAX_FEE_BPS
    assert 46630 not in ESCROW_CONTRACTS
    assert 46630 not in ESCROW_OPERATORS
    assert 46630 not in ESCROW_MAX_FEE_BPS


# -- nothing but Robinhood moved since 0.107.0 ---------------------------------


def test_the_table_adds_exactly_robinhood_and_robinhood_testnet():
    assert set(ERC8004_CONTRACTS) - set(CONTRACTS_0_107_0) == {"robinhood", "robinhood-testnet"}


def test_the_table_removes_nothing():
    assert set(CONTRACTS_0_107_0) - set(ERC8004_CONTRACTS) == set()


def test_every_0_107_0_entry_is_unchanged():
    for network, contracts in CONTRACTS_0_107_0.items():
        assert ERC8004_CONTRACTS[network] == contracts, network


def test_the_network_type_adds_robinhood_and_the_caip2_names():
    assert set(get_args(Erc8004Network)) == (
        NETWORKS_0_107_0 | {"robinhood", "robinhood-testnet"} | CAIP2_NAMES
    )


def test_every_0_107_0_name_goes_to_the_wire_as_before():
    for network in NETWORKS_0_107_0:
        assert _wire(network) == ("base" if network == "base-mainnet" else network), network


def test_the_rails_are_untouched():
    assert set(RELAYED_FEEDBACK_NETWORKS) == RELAYED_0_107_0
    assert set(SOLANA_FEEDBACK_NETWORKS) == SOLANA_0_107_0


def test_the_closing_check_prints_the_registries(capsys):
    # The line to run against the published package after release.
    print(
        ERC8004_CONTRACTS[_wire("eip155:4663")].identity_registry,
        ERC8004_CONTRACTS[_wire("eip155:46630")].validation_registry,
    )
    assert capsys.readouterr().out == (
        "0x8004A169FB4a3325136EB29fA0ceB6D2e539a432 0x8004Cb1BF31DAf7788923b405b754f57acEB4272\n"
    )
