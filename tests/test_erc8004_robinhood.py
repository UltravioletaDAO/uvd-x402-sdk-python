"""Robinhood Chain joins ERC-8004 (Unreleased), and it is the ONLY thing that moves.

Payments on Robinhood (Paxos USDG) shipped before; ERC-8004 there did not.
Execution Market rates on Robinhood Chain through this module. The TypeScript
SDK makes the same change (``uvd-x402-sdk-typescript`` PR #70). What is pinned
here, offline:

1. ``robinhood`` (chain 4663) and ``robinhood-testnet`` (chain 46630) carry the
   singleton registries. Each address was read on-chain on 2026-11-04 before it
   was written down (``eth_chainId`` 0x1237 / 0xb626; ``eth_getCode``: a 130-byte
   EIP-1967 proxy; ``getVersion()`` = 2.0.0; identity and reputation run the
   implementations Base / Base Sepolia run). Mainnet: identity, reputation and
   validation. Testnet: identity and reputation, the deployment this entry was
   asked for and the one the TypeScript SDK carries.
2. The CAIP-2 names the payment side of this SDK gives these chains
   (``eip155:4663``, ``eip155:46630``) resolve to the same entries and are
   rewritten to the plain name before the wire, like ``base-mainnet``: the
   facilitator answers ``400 "Invalid network: eip155:4663"``. Exact strings
   only; no other spelling becomes Robinhood.
3. Neither network is on a feedback rail (no ``FeedbackDelegate``, not Solana),
   so the rating rail refuses both before anything is signed, and no escrow row
   exists for either chain (x402r has no escrow on Robinhood).
4. Facilitator 2.55.0 does not serve ERC-8004 on either network yet: every read
   and write answers ``400 "ERC-8004 is not supported on network robinhood"``
   (measured 2026-11-04). The double below answers exactly that, and the SDK
   gets that answer only when it sent the plain name.
5. Every network 0.107.0 exported is exactly what it was. The snapshots below
   are the lists as 0.107.0 built them, written out rather than derived from the
   code under test.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Awaitable, Callable
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
    build_erc8004_payment_requirements,
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
ROBINHOOD_TESTNET = Erc8004ContractAddresses(
    identity_registry="0x8004A818BFB912233c491871b3d84c89A494BD9e",
    reputation_registry="0x8004B663056A597Dffe9eCcC1965A193B7388713",
)

#: (what the caller writes, the name the facilitator parses, its registries, chain id)
SPELLINGS = [
    ("robinhood", "robinhood", MAINNET, 4663),
    ("eip155:4663", "robinhood", MAINNET, 4663),
    ("robinhood-testnet", "robinhood-testnet", ROBINHOOD_TESTNET, 46630),
    ("eip155:46630", "robinhood-testnet", ROBINHOOD_TESTNET, 46630),
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

_MAINNET_0_107_0 = MAINNET
_TESTNET_0_107_0 = Erc8004ContractAddresses(
    identity_registry="0x8004A818BFB912233c491871b3d84c89A494BD9e",
    reputation_registry="0x8004B663056A597Dffe9eCcC1965A193B7388713",
    validation_registry="0x8004Cb1BF31DAf7788923b405b754f57acEB4272",
)
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
    "ethereum": _MAINNET_0_107_0,
    "base": _MAINNET_0_107_0,
    "polygon": _MAINNET_0_107_0,
    "arbitrum": _MAINNET_0_107_0,
    "optimism": _MAINNET_0_107_0,
    "celo": _MAINNET_0_107_0,
    "bsc": _MAINNET_0_107_0,
    "monad": _MAINNET_0_107_0,
    "avalanche": _MAINNET_0_107_0,
    "scroll": _MAINNET_0_107_0,
    "skale-base": _SKALE_0_107_0,
    "arc": _MAINNET_0_107_0,
    "base-mainnet": _MAINNET_0_107_0,
    "ethereum-sepolia": _TESTNET_0_107_0,
    "base-sepolia": _TESTNET_0_107_0,
    "polygon-amoy": _TESTNET_0_107_0,
    "arbitrum-sepolia": _TESTNET_0_107_0,
    "optimism-sepolia": _TESTNET_0_107_0,
    "celo-sepolia": _TESTNET_0_107_0,
    "avalanche-fuji": _TESTNET_0_107_0,
    "skale-base-sepolia": _TESTNET_0_107_0,
    "arc-testnet": _TESTNET_0_107_0,
    "solana": _SOLANA_0_107_0,
    "solana-devnet": _SOLANA_0_107_0,
}

#: ``Erc8004Network`` as 0.107.0 declared it: the same 24 names.
NETWORKS_0_107_0 = set(CONTRACTS_0_107_0)

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


def test_robinhood_testnet_names_the_identity_and_reputation_registries():
    assert ERC8004_CONTRACTS["robinhood-testnet"] == ROBINHOOD_TESTNET
    # The TypeScript SDK carries the same two; the parity test pins both tables
    # equal, so a validation registry joins in both SDKs or in neither.
    assert ERC8004_CONTRACTS["robinhood-testnet"].validation_registry is None


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


# -- no rail, no escrow -------------------------------------------------------


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


def test_no_escrow_row_exists_on_either_chain():
    # x402r has no escrow on Robinhood: build_escrow_pre_auth refuses a chain
    # without a row in each table (test_escrow_signing_rows), and none is added.
    for chain_id in (4663, 46630):
        assert chain_id not in ESCROW_CONTRACTS
        assert chain_id not in ESCROW_OPERATORS
        assert chain_id not in ESCROW_MAX_FEE_BPS


# -- what reaches the facilitator ---------------------------------------------

#: The ERC-8004 networks facilitator 2.55.0 serves (GET /feedback ->
#: supportedNetworks, 2026-11-04): the same 23 as 2.39.1.
SERVED_BY_2_55_0 = {
    "ethereum", "base", "polygon", "arbitrum", "optimism", "celo", "bsc", "monad",
    "avalanche", "scroll", "skale-base", "arc",
    "ethereum-sepolia", "base-sepolia", "polygon-amoy", "arbitrum-sepolia",
    "optimism-sepolia", "celo-sepolia", "avalanche-fuji", "skale-base-sepolia", "arc-testnet",
    "solana", "solana-devnet",
}
#: Names facilitator 2.55.0 parses as a network but serves no ERC-8004 on.
KNOWN_BY_2_55_0 = {"robinhood", "robinhood-testnet"}

RATER = "0x" + "11" * 20
SOLANA_RATER = "9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM"

Call = Callable[[Erc8004Client, str], Awaitable[Any]]

#: Every write of Erc8004Client (the ten of test_erc8004_retry_after.py).
WRITES: dict[str, Call] = {
    "submit_feedback": lambda c, n: c.submit_feedback(n, 1, 90),
    "prepare_relayed_feedback": lambda c, n: c.prepare_relayed_feedback(n, 1, RATER, 90),
    "submit_relayed_feedback": lambda c, n: c.submit_relayed_feedback(
        n, 1, RATER, 90, deadline=1, nonce="0x01", signature="0x02"
    ),
    "prepare_solana_feedback": lambda c, n: c.prepare_solana_feedback(n, "agent", SOLANA_RATER, 90),
    "submit_solana_feedback": lambda c, n: c.submit_solana_feedback(
        n, "agent", SOLANA_RATER, 90, transaction="AQID"
    ),
    "revoke_feedback": lambda c, n: c.revoke_feedback(n, 1, 1),
    "prepare_relayed_response": lambda c, n: c.prepare_relayed_response(
        n, 1, RATER, RATER, 1, "ipfs://response"
    ),
    "submit_relayed_response": lambda c, n: c.submit_relayed_response(
        n, 1, RATER, RATER, 1, "ipfs://response", deadline=1, nonce="0x01", signature="0x02"
    ),
    "append_response": lambda c, n: c.append_response(n, 1, 1, "thanks"),
    "register_agent": lambda c, n: c.register_agent(n, "ipfs://agent"),
}

#: The reads, and the one write that raises its HTTP error.
READS: dict[str, Call] = {
    "get_identity": lambda c, n: c.get_identity(n, 42),
    "get_identity_by_owner": lambda c, n: c.get_identity_by_owner(n, RATER),
    "get_reputation": lambda c, n: c.get_reputation(n, 42),
    "get_identity_metadata": lambda c, n: c.get_identity_metadata(n, 42, "agentWallet"),
    "get_identity_total_supply": lambda c, n: c.get_identity_total_supply(n),
    "register_agent_async": lambda c, n: c.register_agent_async(n, "ipfs://agent"),
}


def _network_of(request: httpx.Request) -> str:
    """The network a request names: the path segment of a read, the body's
    ``network`` of a write."""
    if request.method == "GET":
        segments = request.url.path.split("/")
        assert segments[1] in ("identity", "reputation"), request.url.path
        return segments[2]
    return json.loads(request.content)["network"]


class Facilitator255:
    """Answers ERC-8004 reads and writes the way facilitator 2.55.0 does."""

    def __init__(self) -> None:
        self.networks: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        network = _network_of(request)
        self.networks.append(network)
        if network in KNOWN_BY_2_55_0:
            return httpx.Response(
                400,
                json={
                    "error": f"ERC-8004 is not supported on network {network}",
                    "supportedNetworks": sorted(SERVED_BY_2_55_0),
                },
            )
        if network not in SERVED_BY_2_55_0:
            return httpx.Response(400, json={"error": f"Invalid network: {network}"})
        raise AssertionError(f"this double serves no ERC-8004 network: {network}")

    def client(self) -> Erc8004Client:
        client = Erc8004Client(base_url="https://facilitator.example")
        client._client = httpx.AsyncClient(transport=httpx.MockTransport(self))
        return client


@pytest.mark.parametrize("write", list(WRITES), ids=list(WRITES))
@pytest.mark.parametrize("network, canonical, contracts, chain_id", SPELLINGS, ids=SPELLING_IDS)
async def test_every_write_sends_the_plain_name(write, network, canonical, contracts, chain_id):
    facilitator = Facilitator255()
    result = await WRITES[write](facilitator.client(), network)
    assert facilitator.networks == [canonical]
    assert result.success is False
    assert result.status_code == 400
    assert f"ERC-8004 is not supported on network {canonical}" in result.error
    assert "Invalid network" not in result.error


@pytest.mark.parametrize("read", list(READS), ids=list(READS))
@pytest.mark.parametrize("network, canonical, contracts, chain_id", SPELLINGS, ids=SPELLING_IDS)
async def test_every_read_sends_the_plain_name(read, network, canonical, contracts, chain_id):
    facilitator = Facilitator255()
    with pytest.raises(httpx.HTTPStatusError) as caught:
        await READS[read](facilitator.client(), network)
    assert facilitator.networks == [canonical]
    assert caught.value.response.status_code == 400
    assert caught.value.response.json()["error"] == (
        f"ERC-8004 is not supported on network {canonical}"
    )


async def test_submit_feedback_rewrites_the_deprecated_base_alias_too():
    # submit_feedback was the one write that sent the caller's spelling as is.
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"success": True, "network": "base"})

    client = Erc8004Client(base_url="https://facilitator.example")
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    result = await client.submit_feedback("base-mainnet", 42, 95, tag1="quality")
    assert result.success is True
    assert [body["network"] for body in seen] == ["base"]
    assert seen[0]["feedback"]["agentId"] == 42


@pytest.mark.parametrize("network, canonical, contracts, chain_id", SPELLINGS, ids=SPELLING_IDS)
def test_payment_requirements_name_the_plain_network(network, canonical, contracts, chain_id):
    requirements = build_erc8004_payment_requirements(
        "1.00", RATER, "https://api.example.com/x", network=network
    )
    assert requirements["network"] == canonical


# -- nothing but Robinhood moved since 0.107.0 ---------------------------------


def test_the_table_adds_exactly_robinhood_and_robinhood_testnet():
    assert set(ERC8004_CONTRACTS) - set(CONTRACTS_0_107_0) == {"robinhood", "robinhood-testnet"}


def test_the_table_removes_nothing():
    assert set(CONTRACTS_0_107_0) - set(ERC8004_CONTRACTS) == set()


def test_every_0_107_0_entry_is_unchanged():
    for network, contracts in CONTRACTS_0_107_0.items():
        assert ERC8004_CONTRACTS[network] == contracts, network


def test_the_network_type_adds_exactly_the_four_robinhood_spellings():
    assert set(get_args(Erc8004Network)) == NETWORKS_0_107_0 | {
        "robinhood", "robinhood-testnet", "eip155:4663", "eip155:46630",
    }


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
        ERC8004_CONTRACTS["robinhood-testnet"].reputation_registry,
    )
    assert capsys.readouterr().out == (
        "0x8004A169FB4a3325136EB29fA0ceB6D2e539a432 0x8004B663056A597Dffe9eCcC1965A193B7388713\n"
    )
