"""The CAIP-2 name of every EVM network of ERC-8004 is an alias (0.110.0).

An alias is not a Robinhood special case (c0der's decision on the refutation of
the TypeScript twin, uvd-x402-sdk-typescript PR #70): either every EVM network
of the ERC-8004 table takes its ``eip155:<chain id>`` name, or none does; every
one does. The facilitator parses only the plain names (facilitator 2.55.0:
``400 "Invalid network: eip155:4663"``), so the SDK rewrites the CAIP-2 name
before the wire, as it rewrites ``base-mainnet``.

Pinned here, walking the whole table:

1. The chain ids. They are read from the payment registry wherever it carries the
   network, and from ``_CHAIN_IDS_NOT_IN_THE_PAYMENT_REGISTRY`` (exactly the
   networks it lacks) otherwise. Both are checked against the facilitator's ids
   as RECORDED in ``tests/fixtures/x402rs-evm-chain-ids.json`` (x402-rs
   ``src/network.rs`` + ``src/chain/evm.rs`` at the commit it cites, written by
   ``scripts/x402rs_evm_chain_ids.py``, never by hand; the eight outside the
   registry were also confirmed with ``eth_chainId`` on 2026-11-04), and against
   the relay table of the rating rail.
2. Every alias resolves locally like its network, and every read and write sends
   the plain name.
3. Nothing else is an alias: another form of the same chain id, a chain id the
   payment registry knows but ERC-8004 does not, Solana.
4. ``Erc8004Network`` names exactly the derived aliases.
"""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, get_args

import httpx
import pytest

from uvd_x402_sdk.erc8004 import (
    _CHAIN_IDS_NOT_IN_THE_PAYMENT_REGISTRY,
    _ERC8004_CHAIN_IDS,
    _ERC8004_NETWORK_ALIASES,
    ERC8004_CONTRACTS,
    Erc8004Client,
    Erc8004Network,
    _wire,
    build_erc8004_payment_requirements,
    supports_relayed_feedback,
    supports_solana_feedback,
)
from uvd_x402_sdk.networks.base import get_network, normalize_network, to_caip2_network

FIXTURE_PATH = Path(__file__).resolve().parent / "fixtures" / "x402rs-evm-chain-ids.json"

#: The facilitator's chain ids, as recorded from x402-rs at one commit.
FACILITATOR = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
FACILITATOR_CHAIN_IDS: dict[str, int] = FACILITATOR["chainIds"]

EVM_NETWORKS = sorted(
    name
    for name, contracts in ERC8004_CONTRACTS.items()
    if contracts.identity_registry is not None and name != "base-mainnet"
)

#: (alias, network) for every EVM network of the table, the id taken from the
#: facilitator's recording (a network missing there fails at collection).
ALIASES = [(f"eip155:{FACILITATOR_CHAIN_IDS[n]}", n) for n in EVM_NETWORKS]
ALIAS_IDS = [a for a, _ in ALIASES]

#: Strings that look like an alias and are not one.
NOT_ALIASES = [
    "EIP155:8453", "Eip155:8453", "eip155:08453", "eip155:8453 ", " eip155:8453", "eip155:8453\n",
    "eip155: 8453", "eip155:0x2105", "eip155:0X2105", "eip155:+8453", "eip155:-8453",
    "eip155:8453.0", "eip155:8_453", "eip155:84530", "eip155:845", "eip155:01", "eip155:0",
    "eip155:", "eip155", "8453", "eip155:8453:base", "caip2:eip155:8453",
    "eip155:" + "".join(chr(0x0660 + int(d)) for d in "8453"),  # Arabic-Indic digits
    "eip155" + chr(0xFF1A) + "8453",  # full-width colon
    "eip155:8453" + chr(0x200B),  # zero-width space
    # Chain ids the payment registry knows and ERC-8004 does not serve.
    "eip155:999", "eip155:130", "eip155:177", "eip155:133",
    # Celo's retired testnet, and a chain the facilitator serves for payments only.
    "eip155:44787", "eip155:1329",
    # Solana is no EVM network: its CAIP-2 names are not aliases here.
    "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp", "solana:EtWTRABZaYq6iMfeYKouRu166VU2xqa1",
    "solana:mainnet", "solana:devnet",
]


@pytest.fixture
def offline_client() -> Erc8004Client:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"a local lookup sent {request.method} {request.url}")

    client = Erc8004Client(base_url="https://facilitator.example")
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(refuse))
    return client


# -- the chain ids ------------------------------------------------------------


def test_every_evm_network_of_the_table_has_a_chain_id():
    assert sorted(_ERC8004_CHAIN_IDS) == EVM_NETWORKS


def test_the_fixture_is_the_recorded_facilitator():
    source = FACILITATOR["source"]
    assert source["repository"] == "UltravioletaDAO/x402-rs"
    assert source["commit"] == "3ed3a956473abd2c52098bbe77a1a397318a7508"
    assert source["version"] == "2.55.0"
    assert source["files"] == ["src/network.rs", "src/chain/evm.rs"]
    assert FACILITATOR["generatedBy"] == (
        "python scripts/x402rs_evm_chain_ids.py --x402rs <x402-rs checkout> "
        f"--ref {source['commit']}"
    )


def test_the_chain_ids_are_the_facilitators():
    # Every EVM network of the table, the eight of the supplement included.
    for network, chain_id in _ERC8004_CHAIN_IDS.items():
        assert FACILITATOR_CHAIN_IDS[network] == chain_id, network


def test_the_supplement_is_the_facilitators_too():
    for network, chain_id in _CHAIN_IDS_NOT_IN_THE_PAYMENT_REGISTRY.items():
        assert FACILITATOR_CHAIN_IDS[network] == chain_id, network


def test_no_two_networks_share_a_chain_id():
    assert len(set(_ERC8004_CHAIN_IDS.values())) == len(_ERC8004_CHAIN_IDS)


@pytest.mark.parametrize("network", EVM_NETWORKS)
def test_the_payment_registry_is_the_source_where_it_has_the_network(network):
    registered = get_network(network)
    alias = f"eip155:{_ERC8004_CHAIN_IDS[network]}"
    if registered is None:
        assert _CHAIN_IDS_NOT_IN_THE_PAYMENT_REGISTRY[network] == _ERC8004_CHAIN_IDS[network]
    else:
        # A network the registry carries must not be written twice.
        assert network not in _CHAIN_IDS_NOT_IN_THE_PAYMENT_REGISTRY
        assert registered.chain_id == _ERC8004_CHAIN_IDS[network]
        assert to_caip2_network(network) == alias
        assert normalize_network(alias) == network


def test_the_supplement_holds_exactly_the_networks_the_registry_lacks():
    lacking = {n for n in EVM_NETWORKS if get_network(n) is None}
    assert set(_CHAIN_IDS_NOT_IN_THE_PAYMENT_REGISTRY) == lacking


def test_a_deprecated_name_never_takes_a_chain_id(monkeypatch):
    # Were the payment registry to resolve "base-mainnet", the deprecated name
    # would take Base's chain id and "eip155:8453" would be rewritten to it: a
    # 400 at the facilitator. The derivation skips deprecated names for that.
    from uvd_x402_sdk import erc8004

    real = erc8004.get_network
    monkeypatch.setattr(
        erc8004, "get_network", lambda n: real("base" if n == "base-mainnet" else n)
    )
    assert erc8004._erc8004_chain_ids() == dict(_ERC8004_CHAIN_IDS)


def test_the_relay_table_of_the_rating_rail_agrees():
    from uvd_x402_sdk.reputation._chains import RELAY_CHAIN_IDS

    for network, chain_id in RELAY_CHAIN_IDS.items():
        assert _ERC8004_CHAIN_IDS[network] == chain_id, network


# -- the aliases --------------------------------------------------------------


def test_the_aliases_are_base_mainnet_and_one_caip2_name_per_evm_network():
    assert dict(_ERC8004_NETWORK_ALIASES) == {"base-mainnet": "base", **dict(ALIASES)}


def test_no_alias_points_at_solana():
    assert not {"solana", "solana-devnet"} & set(_ERC8004_NETWORK_ALIASES.values())


def test_the_network_type_names_exactly_the_aliases():
    caip2 = {n for n in get_args(Erc8004Network) if ":" in n}
    assert caip2 == {a for a in _ERC8004_NETWORK_ALIASES if a.startswith("eip155:")}
    assert set(_ERC8004_NETWORK_ALIASES) <= set(get_args(Erc8004Network))


def test_no_alias_is_a_table_key():
    assert not set(_ERC8004_NETWORK_ALIASES) - {"base-mainnet"} & set(ERC8004_CONTRACTS)


@pytest.mark.parametrize("alias, network", ALIASES, ids=ALIAS_IDS)
def test_every_alias_resolves_like_its_network(offline_client, alias, network):
    assert _wire(alias) == network
    assert offline_client.is_available(alias) is True
    assert offline_client.get_contracts(alias) is ERC8004_CONTRACTS[network]
    assert supports_relayed_feedback(alias) is supports_relayed_feedback(network)
    assert supports_solana_feedback(alias) is False


@pytest.mark.parametrize("alias, network", ALIASES, ids=ALIAS_IDS)
def test_every_alias_reaches_the_rating_rail_as_its_network(alias, network):
    from uvd_x402_sdk.reputation._chains import (
        identity_registry,
        relay_chain_id,
        reputation_registry,
    )

    assert relay_chain_id(alias) == relay_chain_id(network)
    assert reputation_registry(alias) == reputation_registry(network)
    assert identity_registry(alias) == identity_registry(network)


@pytest.mark.parametrize("network", NOT_ALIASES, ids=repr)
def test_nothing_else_is_an_alias(offline_client, network):
    assert _wire(network) == network
    assert offline_client.is_available(network) is False
    assert offline_client.get_contracts(network) is None
    assert supports_relayed_feedback(network) is False


# -- what reaches the facilitator ---------------------------------------------

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

#: Plain name of every network the double parses: the table's, but the
#: deprecated alias (the facilitator answers "Invalid network: base-mainnet").
PARSED = set(ERC8004_CONTRACTS) - {"base-mainnet"}


def _network_of(request: httpx.Request) -> str:
    """The network a request names: the path segment of a read, the body's
    ``network`` of a write."""
    if request.method == "GET":
        segments = request.url.path.split("/")
        assert segments[1] in ("identity", "reputation"), request.url.path
        return segments[2]
    return json.loads(request.content)["network"]


class ParsingFacilitator:
    """Parses networks the way the facilitator does: a name it does not know is
    ``400 Invalid network``; on one it knows, it answers 404, so a test sees
    which of the two the SDK's request got."""

    def __init__(self) -> None:
        self.networks: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        network = _network_of(request)
        self.networks.append(network)
        if network not in PARSED:
            return httpx.Response(400, json={"error": f"Invalid network: {network}"})
        return httpx.Response(404, json={"error": f"nothing on {network} in this double"})

    def client(self) -> Erc8004Client:
        client = Erc8004Client(base_url="https://facilitator.example")
        client._client = httpx.AsyncClient(transport=httpx.MockTransport(self))
        return client


#: (what the caller writes, the plain name): every EVM network in both spellings.
SPELLINGS = [(n, n) for n in EVM_NETWORKS] + ALIASES
SPELLING_IDS = [s for s, _ in SPELLINGS]


@pytest.mark.parametrize("write", list(WRITES), ids=list(WRITES))
@pytest.mark.parametrize("spelling, network", SPELLINGS, ids=SPELLING_IDS)
async def test_every_write_sends_the_plain_name(write, spelling, network):
    facilitator = ParsingFacilitator()
    result = await WRITES[write](facilitator.client(), spelling)
    assert facilitator.networks == [network]
    assert result.success is False
    assert result.status_code == 404
    assert "Invalid network" not in result.error


@pytest.mark.parametrize("read", list(READS), ids=list(READS))
@pytest.mark.parametrize("spelling, network", SPELLINGS, ids=SPELLING_IDS)
async def test_every_read_sends_the_plain_name(read, spelling, network):
    facilitator = ParsingFacilitator()
    with pytest.raises(httpx.HTTPStatusError) as caught:
        await READS[read](facilitator.client(), spelling)
    assert facilitator.networks == [network]
    assert caught.value.response.status_code == 404


async def test_submit_feedback_rewrites_the_deprecated_base_alias_too():
    # submit_feedback was the one write that sent the caller's spelling as is.
    facilitator = ParsingFacilitator()
    result = await facilitator.client().submit_feedback("base-mainnet", 42, 95, tag1="quality")
    assert facilitator.networks == ["base"]
    assert result.status_code == 404


@pytest.mark.parametrize("spelling, network", SPELLINGS, ids=SPELLING_IDS)
def test_payment_requirements_name_the_plain_network(spelling, network):
    requirements = build_erc8004_payment_requirements(
        "1.00", RATER, "https://api.example.com/x", network=spelling
    )
    assert requirements["network"] == network


# -- the rating rail keeps its chain check under an alias ---------------------


def test_a_relayed_prepare_validates_under_the_alias_of_its_network():
    pytest.importorskip("eth_account")
    from tests.test_erc8004_robinhood import _relayed_base_case
    from uvd_x402_sdk.reputation import PreparationRejected, RatingParams, validate_preparation

    case = _relayed_base_case()
    assert case["network"] == "base"
    params = case["params"]
    rating = RatingParams(
        agent_id=params["agentId"],
        value=params["value"],
        tag1=params["tag1"],
        tag2=params["tag2"],
        endpoint=params["endpoint"],
        feedback_uri=params["feedbackURI"],
        feedback_hash=params["feedbackHash"],
    )

    def validate(network: str):
        return validate_preparation(
            case["prepare"], rating, case["rater"], network=network, now=case["now"]
        )

    # The same verdict and the same digest to sign; only the echo of the name
    # the caller wrote differs (VerifiedPreparation.network).
    under_alias, under_name = validate("eip155:8453"), validate("base")
    assert under_alias.network == "eip155:8453"
    assert dataclasses.replace(under_alias, network="base") == under_name
    assert under_alias.chain_id == 8453
    # The alias of another chain is that chain: the prepare for Base is refused.
    for other in ("eip155:1", "eip155:84532"):
        with pytest.raises(PreparationRejected) as caught:
            validate(other)
        assert caught.value.code == "chain_mismatch", other
