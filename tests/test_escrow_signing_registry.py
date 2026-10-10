"""``build_escrow_pre_auth`` against the SDK's own escrow tables.

Every address an escrow pre-auth commits to comes from the marketplace's
payment config. Each one is checked against ``uvd_x402_sdk.escrow_contracts``
before anything is signed, the way the USDC domain is checked against
``VERIFIED_USDC_DOMAINS``:

=================  ==========================================  ======================
config key         where the signature carries it              checked against
=================  ==========================================  ======================
``chain_id``       ``domain.chainId``, the nonce, the network  the ``network`` name
``escrow``         the nonce (``AuthCaptureEscrow.getHash``)   ``ESCROW_CONTRACTS``
``token_collector``  ``message.to`` / ``authorization.to``       ``ESCROW_CONTRACTS``
``usdc``           ``domain.verifyingContract``, ``token``     ``ESCROW_CONTRACTS``
``operator``       ``paymentInfo.operator`` / ``feeReceiver``  ``ESCROW_OPERATORS``
``max_fee_bps``    ``paymentInfo.maxFeeBps`` (ceiling)         ``ESCROW_MAX_FEE_BPS``
=================  ==========================================  ======================

For each address, on every chain of ``VERIFIED_USDC_DOMAINS``: the table's
value is signed, and a changed one is refused with nothing signed; the fee
ceiling is signed and one bps above it is refused. A chain without a row is
signed with a warning (same policy as the domain).
``EXECUTION_MARKET_NETWORKS`` is Execution Market's config of each of its
escrow networks: every one of them signs with no warning.
``TestGuardMutations`` removes each guard from a copy of the module's source
and checks that the refusal it owns turns into a signature.
"""

from __future__ import annotations

import ast
import inspect
import json
import logging
from pathlib import Path
from typing import Any
from unittest import mock

import pytest
from eth_utils import to_checksum_address

import uvd_x402_sdk.advanced_escrow as ae
import uvd_x402_sdk.escrow_contracts as ec
import uvd_x402_sdk.escrow_signing as es
from tests.test_escrow_vectors import mutant
from uvd_x402_sdk.erc7702 import SMA_WRAP_TARGETS
from uvd_x402_sdk.escrow_contracts import (
    ESCROW_CONTRACTS,
    ESCROW_MAX_FEE_BPS,
    ESCROW_OPERATORS,
)
from uvd_x402_sdk.escrow_signing import (
    DEFAULT_MAX_FEE_BPS,
    ESCROW_PAYMENT_INFO_TYPEHASH,
    OPERATOR_FEE_BPS,
    VERIFIED_USDC_DOMAINS,
    compute_escrow_nonce,
)
from uvd_x402_sdk.networks import get_network_by_chain_id

PAYER = "0x" + "a1" * 20
WORKER = "0x" + "5e" * 20
CHAINS = sorted(VERIFIED_USDC_DOMAINS)
CHAIN_IDS = [get_network_by_chain_id(c).name for c in CHAINS]
WITH_OPERATOR = sorted(c for c in CHAINS if c in ESCROW_OPERATORS)
ADDRESS_KEYS = ("escrow", "token_collector", "usdc")
# Rows in ESCROW_CONTRACTS, but no operator, no fee ceiling, no verified domain.
TESTNETS = [("base-sepolia", 84532), ("ethereum-sepolia", 11155111)]
TESTNET_IDS = [n for n, _ in TESTNETS]
# The chains where AdvancedEscrowClient picks an operator when none is given
# (as in 0.95.0). The operator rows of the other chains did not change the client.
CLIENT_DEFAULT_CHAINS = {8453, 1187947933, 5042, 5042002}
FIXTURES = Path(__file__).resolve().parent / "fixtures"


class _Wallet:
    """Signs nothing real; records what it was asked to sign."""

    def __init__(self) -> None:
        self.signed: list[dict[str, Any]] = []

    def get_address(self) -> str:
        return PAYER

    def sign_typed_data(self, typed: dict[str, Any]) -> dict[str, str]:
        self.signed.append(typed)
        return {"signature": "0x" + "11" * 65}


def network_of(chain_id: int) -> str:
    return get_network_by_chain_id(chain_id).name


def table_block(chain_id: int) -> dict[str, Any]:
    """The network block of a payment config, from the SDK's tables only."""
    contracts = ESCROW_CONTRACTS[chain_id]
    name, version = VERIFIED_USDC_DOMAINS.get(chain_id, ("USD Coin", "2"))
    return {
        "chain_id": chain_id,
        "operator": ESCROW_OPERATORS.get(chain_id, "0x" + format(chain_id, "040x")),
        "escrow": contracts["escrow"],
        "token_collector": contracts["token_collector"],
        "usdc": contracts["usdc"],
        "usdc_domain_name": name,
        "usdc_domain_version": version,
    }


def config(network: str, block: dict[str, Any], **escrow: Any) -> dict[str, Any]:
    return {
        "escrow": {
            "payment_info_typehash": ESCROW_PAYMENT_INFO_TYPEHASH,
            "networks": {network: block},
            **escrow,
        }
    }


def build(
    network: str, block: dict[str, Any], module: Any = es, **escrow: Any
) -> tuple[dict, _Wallet]:
    wallet = _Wallet()
    header = module.build_escrow_pre_auth(
        config(network, block, **escrow), network, PAYER, WORKER, "0.25", None, wallet
    )
    return json.loads(header), wallet


def changed(address: str) -> str:
    """The same address with its last hex digit moved by one."""
    last = format((int(address[-1], 16) + 1) % 16, "x")
    return address[:-1] + last


def refused(
    network: str, block: dict[str, Any], match: str, module: Any = es, **escrow: Any
) -> None:
    wallet = _Wallet()
    with pytest.raises(ValueError, match=match):
        module.build_escrow_pre_auth(
            config(network, block, **escrow), network, PAYER, WORKER, "0.25", None, wallet
        )
    assert wallet.signed == [], "nothing may be signed before the refusal"


# ── the tables ───────────────────────────────────────────────────────────────


def test_escrow_contracts_imports_nothing():
    """escrow_signing reads it on an install without web3."""
    tree = ast.parse(Path(ec.__file__).read_text(encoding="utf-8"))
    imports = [n for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom))]
    assert [(n.module, [a.name for a in n.names]) for n in imports] == [
        ("__future__", ["annotations"])
    ]


def test_advanced_escrow_reads_the_same_registry_object():
    assert ae.ESCROW_CONTRACTS is ec.ESCROW_CONTRACTS
    assert es.ESCROW_CONTRACTS is ec.ESCROW_CONTRACTS
    assert es.ESCROW_OPERATORS is ec.ESCROW_OPERATORS


def test_every_operator_row_is_a_chain_of_the_registry():
    assert set(ESCROW_OPERATORS) <= set(ESCROW_CONTRACTS)


def test_every_chain_with_a_verified_domain_has_an_operator_row():
    """The testnet rows of ESCROW_CONTRACTS have none."""
    assert set(ESCROW_OPERATORS) == set(VERIFIED_USDC_DOMAINS)


@pytest.mark.parametrize("chain_id", sorted(ESCROW_CONTRACTS))
def test_the_client_default_operator_is_the_row_where_the_client_has_one(chain_id):
    """Where AdvancedEscrowClient has a default operator it is the row; elsewhere it still asks."""
    kwargs = {"private_key": "0x" + "4b" * 32, "chain_id": chain_id, "rpc_url": "http://127.0.0.1:9"}
    if chain_id in CLIENT_DEFAULT_CHAINS:
        client = ae.AdvancedEscrowClient(**kwargs)
        assert client.contracts["operator"] == ESCROW_OPERATORS[chain_id]
    else:
        with pytest.raises(ValueError, match="operator_address is required"):
            ae.AdvancedEscrowClient(**kwargs)


@pytest.mark.parametrize("chain_id", sorted(CLIENT_DEFAULT_CHAINS))
def test_the_client_reads_its_default_operator_from_the_table(chain_id):
    """Its default chains are these, and their operator is whatever the row says."""
    assert ae._DEFAULT_OPERATOR_CHAINS == CLIENT_DEFAULT_CHAINS
    assert ae._DEFAULT_OPERATOR_CHAINS <= set(ESCROW_OPERATORS)
    other = to_checksum_address("0x" + "0d" * 20)
    with mock.patch.dict(ESCROW_OPERATORS, {chain_id: other}):
        client = ae.AdvancedEscrowClient(
            private_key="0x" + "4b" * 32, chain_id=chain_id, rpc_url="http://127.0.0.1:9"
        )
    assert client.contracts["operator"] == other


def test_the_client_fee_default_is_why_the_other_rows_are_not_client_defaults():
    """build_payment_info's max_fee_bps default does not cover the operator's fee.

    With Execution Market's operator as a silent default on another chain, the
    client would lock funds that its release can never move (maxFeeBps below
    the 1300 bps fee). If this default ever covers it, the other rows of
    ESCROW_OPERATORS can become client defaults too.
    """
    pi = ae.PaymentInfo(
        operator=ESCROW_OPERATORS[1], receiver=WORKER, token=ESCROW_CONTRACTS[1]["usdc"],
        max_amount=1, pre_approval_expiry=1, authorization_expiry=2, refund_expiry=3,
    )
    assert pi.max_fee_bps < OPERATOR_FEE_BPS
    build_payment_info = inspect.signature(ae.AdvancedEscrowClient.build_payment_info)
    assert build_payment_info.parameters["max_fee_bps"].default < OPERATOR_FEE_BPS


def test_the_arc_operator_is_the_recorded_compute_address():
    recorded = json.loads((FIXTURES / "arc-escrow-d.json").read_text(encoding="utf-8"))
    assert ESCROW_OPERATORS[5042] == ESCROW_OPERATORS[5042002] == recorded["default_operator"]
    for chain in ("5042", "5042002"):
        result = recorded["chains"][chain]["compute_address"]["result"]
        assert int(result, 16) == int(recorded["default_operator"], 16)


# Execution Market's PaymentOperators on the other escrow mainnets, written out
# a second time on purpose: the table is their only other copy in this repo, so
# without this an edit of a row to another valid address would turn nothing red.
# Source: EM's mcp_server/integrations/x402/network_registry.py at f75d4171.
EM_OPERATORS_F75D4171 = {
    1: "0x69B67962ffb7c5C7078ff348a87DF604dfA8001b",
    10: "0xC2377a9Db1de2520BD6b2756eD012f4E82F7938e",
    137: "0xB87F1ECC85f074e50df3DD16A1F40e4e1EC4102e",
    143: "0x9620Dbe2BB549E1d080Dc8e7982623A9e1Df8cC3",
    42161: "0xC2377a9Db1de2520BD6b2756eD012f4E82F7938e",
    42220: "0xC2377a9Db1de2520BD6b2756eD012f4E82F7938e",
    43114: "0xC2377a9Db1de2520BD6b2756eD012f4E82F7938e",
}


def test_the_other_operator_rows_are_execution_markets_at_f75d4171():
    rows = {c: a for c, a in ESCROW_OPERATORS.items() if c not in CLIENT_DEFAULT_CHAINS}
    assert rows == EM_OPERATORS_F75D4171
    for address in rows.values():
        assert to_checksum_address(address) == address


def test_the_base_operator_is_execution_markets():
    em = json.loads((FIXTURES / "escrow-preauth.json").read_text(encoding="utf-8"))
    assert em["network_config"]["operator"] == ESCROW_OPERATORS[8453]
    assert ae.BASE_MAINNET_CONTRACTS["operator"] == ESCROW_OPERATORS[8453]


# Execution Market's config of each network of its registry with an escrow and
# an operator (execution-market, mcp_server/integrations/x402/network_registry.py
# at f75d4171), with the keys build_escrow_pre_auth reads, taken from the
# registry the way mcp_server/api/h2a.py takes them: the network's ``operator``
# and ``escrow``, ``x402r_infra.tokenCollector``, and the address, name and
# version of its USDC (h2a.py also serves ``tokens`` and ``tier_timings``,
# which the pre-auth does not check). Nine of them are what GET
# /api/v1/h2a/payment-config serves in ``escrow.networks`` (each while
# EM_ENABLED_NETWORKS enables it). ``skale`` is not: it is retired in EM
# (ADR-009, 2026-08-24) and h2a.py skips it, but its registry entry is still
# the operator and contracts of the escrows in flight there, which is what the
# SDK's SKALE rows hold. The escrow block serves ``min_fee_bps`` 0 and ``max_fee_bps``
# 1800 (``EXECUTION_MARKET_ESCROW``).
def _em_network(
    chain_id: int, *, operator: str, escrow: str, collector: str, usdc: str, domain_name: str
) -> dict[str, Any]:
    """One block of ``escrow.networks``, with the keys build_escrow_pre_auth reads."""
    return {
        "chain_id": chain_id,
        "operator": operator,
        "escrow": escrow,
        "token_collector": collector,
        "usdc": usdc,
        "usdc_domain_name": domain_name,
        "usdc_domain_version": "2",
    }


EXECUTION_MARKET_NETWORKS: dict[str, dict[str, Any]] = {
    "base": _em_network(
        8453,
        operator="0x271f9fa7f8907aCf178CCFB470076D9129D8F0Eb",
        escrow="0xb9488351E48b23D798f24e8174514F28B741Eb4f",
        collector="0x48ADf6E37F9b31dC2AAD0462C5862B5422C736B8",
        usdc="0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
        domain_name="USD Coin",
    ),
    "ethereum": _em_network(
        1,
        operator="0x69B67962ffb7c5C7078ff348a87DF604dfA8001b",
        escrow="0x9D4146EF898c8E60B3e865AE254ef438E7cEd2A0",
        collector="0x206D4DbB6E7b876e4B5EFAAD2a04e7d7813FB6ba",
        usdc="0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
        domain_name="USD Coin",
    ),
    "polygon": _em_network(
        137,
        operator="0xB87F1ECC85f074e50df3DD16A1F40e4e1EC4102e",
        escrow="0x32d6AC59BCe8DFB3026F10BcaDB8D00AB218f5b6",
        collector="0xc1256Bb30bd0cdDa07D8C8Cf67a59105f2EA1b98",
        usdc="0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359",
        domain_name="USD Coin",
    ),
    "arbitrum": _em_network(
        42161,
        operator="0xC2377a9Db1de2520BD6b2756eD012f4E82F7938e",
        escrow="0x320a3c35F131E5D2Fb36af56345726B298936037",
        collector="0x230fd3A171750FA45db2976121376b7F47Cba308",
        usdc="0xaf88d065e77c8cC2239327C5EDb3A432268e5831",
        domain_name="USD Coin",
    ),
    "celo": _em_network(
        42220,
        operator="0xC2377a9Db1de2520BD6b2756eD012f4E82F7938e",
        escrow="0x320a3c35F131E5D2Fb36af56345726B298936037",
        collector="0x230fd3A171750FA45db2976121376b7F47Cba308",
        usdc="0xcebA9300f2b948710d2653dD7B07f33A8B32118C",
        domain_name="USDC",
    ),
    "monad": _em_network(
        143,
        operator="0x9620Dbe2BB549E1d080Dc8e7982623A9e1Df8cC3",
        escrow="0x320a3c35F131E5D2Fb36af56345726B298936037",
        collector="0x230fd3A171750FA45db2976121376b7F47Cba308",
        usdc="0x754704Bc059F8C67012fEd69BC8A327a5aafb603",
        domain_name="USDC",
    ),
    "avalanche": _em_network(
        43114,
        operator="0xC2377a9Db1de2520BD6b2756eD012f4E82F7938e",
        escrow="0x320a3c35F131E5D2Fb36af56345726B298936037",
        collector="0x230fd3A171750FA45db2976121376b7F47Cba308",
        usdc="0xB97EF9Ef8734C71904D8002F8b6Bc66Dd9c48a6E",
        domain_name="USD Coin",
    ),
    "optimism": _em_network(
        10,
        operator="0xC2377a9Db1de2520BD6b2756eD012f4E82F7938e",
        escrow="0x320a3c35F131E5D2Fb36af56345726B298936037",
        collector="0x230fd3A171750FA45db2976121376b7F47Cba308",
        usdc="0x0b2C639c533813f4Aa9D7837CAf62653d097Ff85",
        domain_name="USD Coin",
    ),
    "skale": _em_network(
        1187947933,
        operator="0x43E46d4587fCCc382285C52012227555ed78D183",
        escrow="0xBC151792f80C0EB1973d56b0235e6bee2A60e245",
        collector="0x9A12A116a44636F55c9e135189A1321Abcfe2f30",
        usdc="0x85889c8c714505E0c94b30fcfcF64fE3Ac8FCb20",
        domain_name="Bridged USDC (SKALE Bridge)",
    ),
    "arc": _em_network(
        5042,
        operator="0x0258472A1410Ac3Ad720f1BC83f22B3c0af1Fd9D",
        escrow="0xBdEA0D1bcC5966192B070Fdf62aB4EF5b4420cff",
        collector="0x0E3dF9510de65469C4518D7843919c0b8C7A7757",
        usdc="0x3600000000000000000000000000000000000000",
        domain_name="USDC",
    ),
}
EXECUTION_MARKET_ESCROW = {"min_fee_bps": 0, "max_fee_bps": 1800, "deposit_limit_usd": 100}


@pytest.mark.parametrize("network", sorted(EXECUTION_MARKET_NETWORKS))
def test_the_rows_are_execution_markets_config(network):
    block = EXECUTION_MARKET_NETWORKS[network]
    assert ESCROW_OPERATORS[block["chain_id"]] == block["operator"]
    contracts = ESCROW_CONTRACTS[block["chain_id"]]
    assert {k: contracts[k] for k in ADDRESS_KEYS} == {k: block[k] for k in ADDRESS_KEYS}
    assert VERIFIED_USDC_DOMAINS[block["chain_id"]] == (
        block["usdc_domain_name"],
        block["usdc_domain_version"],
    )


@pytest.mark.parametrize("network", sorted(EXECUTION_MARKET_NETWORKS))
def test_execution_markets_config_signs_with_no_warning(network, caplog):
    """Every value of its config has a row here, and every row agrees with it."""
    caplog.set_level(logging.WARNING, logger="uvd_x402_sdk.escrow_signing")
    block = EXECUTION_MARKET_NETWORKS[network]
    wrapper, wallet = build(network, dict(block), **EXECUTION_MARKET_ESCROW)
    assert len(wallet.signed) == 1
    payment_info = wrapper["payload"]["paymentInfo"]
    assert payment_info["operator"] == payment_info["feeReceiver"] == block["operator"]
    assert payment_info["maxFeeBps"] == EXECUTION_MARKET_ESCROW["max_fee_bps"]
    assert wrapper["paymentRequirements"]["network"] == f"eip155:{block['chain_id']}"
    assert [r.getMessage() for r in caplog.records] == []


@pytest.mark.parametrize("network", sorted(EXECUTION_MARKET_NETWORKS))
def test_execution_markets_config_with_another_operator_is_refused(network):
    block = dict(EXECUTION_MARKET_NETWORKS[network])
    block["operator"] = changed(block["operator"])
    refused(network, block, "Escrow operator mismatch", **EXECUTION_MARKET_ESCROW)


@pytest.mark.parametrize("network", sorted(EXECUTION_MARKET_NETWORKS))
def test_execution_markets_config_one_bps_above_the_ceiling_is_refused(network):
    escrow = dict(EXECUTION_MARKET_ESCROW, max_fee_bps=EXECUTION_MARKET_ESCROW["max_fee_bps"] + 1)
    block = dict(EXECUTION_MARKET_NETWORKS[network])
    refused(network, block, "above this SDK's ceiling", **escrow)


# ── one test per address: signs with the table, refuses it changed ──────────


@pytest.mark.parametrize("chain_id", CHAINS, ids=CHAIN_IDS)
def test_the_table_is_signed(chain_id):
    contracts = ESCROW_CONTRACTS[chain_id]
    wrapper, wallet = build(network_of(chain_id), table_block(chain_id))
    [typed] = wallet.signed
    authorization = wrapper["payload"]["authorization"]
    payment_info = wrapper["payload"]["paymentInfo"]

    assert typed["domain"]["chainId"] == chain_id
    assert wrapper["paymentRequirements"]["network"] == f"eip155:{chain_id}"
    assert typed["domain"]["verifyingContract"] == contracts["usdc"]
    assert payment_info["token"] == contracts["usdc"]
    assert typed["message"]["to"] == authorization["to"] == contracts["token_collector"]
    assert authorization["nonce"] == compute_escrow_nonce(
        chain_id, contracts["escrow"], ESCROW_PAYMENT_INFO_TYPEHASH, payment_info
    )
    if chain_id in ESCROW_OPERATORS:
        assert payment_info["operator"] == payment_info["feeReceiver"] == ESCROW_OPERATORS[chain_id]


@pytest.mark.parametrize("key", ADDRESS_KEYS)
@pytest.mark.parametrize("chain_id", CHAINS, ids=CHAIN_IDS)
def test_a_changed_registry_address_is_refused(chain_id, key):
    block = table_block(chain_id)
    block[key] = changed(block[key])
    refused(network_of(chain_id), block, f"Escrow address mismatch .* asserts {key} ")


@pytest.mark.parametrize("chain_id", WITH_OPERATOR, ids=[network_of(c) for c in WITH_OPERATOR])
def test_a_changed_operator_is_refused(chain_id):
    block = table_block(chain_id)
    block["operator"] = changed(block["operator"])
    refused(network_of(chain_id), block, "Escrow operator mismatch")


@pytest.mark.parametrize("chain_id", CHAINS, ids=CHAIN_IDS)
def test_another_chains_whole_config_under_this_name_is_refused(chain_id):
    """Every address and the domain consistent with the OTHER chain: still refused."""
    for other in CHAINS:
        if other != chain_id:
            refused(network_of(chain_id), table_block(other), "Chain id mismatch")


@pytest.mark.parametrize("network,chain_id", TESTNETS, ids=TESTNET_IDS)
def test_a_chain_without_an_operator_row_signs_the_configs_with_a_warning(
    network, chain_id, caplog
):
    caplog.set_level(logging.WARNING, logger="uvd_x402_sdk.escrow_signing")
    block = table_block(chain_id)
    block["operator"] = to_checksum_address("0x" + "0e" * 20)
    wrapper, wallet = build(network, block)
    assert len(wallet.signed) == 1
    assert wrapper["payload"]["paymentInfo"]["operator"] == block["operator"]
    assert any("ESCROW_OPERATORS" in r.getMessage() for r in caplog.records)


# ── the fee ceiling ──────────────────────────────────────────────────────────


def test_the_fee_ceiling_is_1800_bps_on_every_chain_with_an_operator_row():
    assert set(ESCROW_MAX_FEE_BPS) == set(ESCROW_OPERATORS)
    assert set(ESCROW_MAX_FEE_BPS.values()) == {1800}
    # A config without max_fee_bps signs DEFAULT_MAX_FEE_BPS: within every ceiling.
    assert DEFAULT_MAX_FEE_BPS <= min(ESCROW_MAX_FEE_BPS.values())


@pytest.mark.parametrize("chain_id", CHAINS, ids=CHAIN_IDS)
def test_the_fee_ceiling_is_signed(chain_id):
    ceiling = ESCROW_MAX_FEE_BPS[chain_id]
    wrapper, wallet = build(network_of(chain_id), table_block(chain_id), max_fee_bps=ceiling)
    assert len(wallet.signed) == 1
    assert wrapper["payload"]["paymentInfo"]["maxFeeBps"] == ceiling


@pytest.mark.parametrize("chain_id", CHAINS, ids=CHAIN_IDS)
def test_one_bps_above_the_fee_ceiling_is_refused(chain_id):
    above = ESCROW_MAX_FEE_BPS[chain_id] + 1
    refused(
        network_of(chain_id),
        table_block(chain_id),
        f"maxFeeBps={above} .* above this SDK's ceiling",
        max_fee_bps=above,
    )


def test_a_fee_above_the_ceiling_written_as_a_string_is_refused():
    refused("base", table_block(8453), "above this SDK's ceiling", max_fee_bps="1801")


@pytest.mark.parametrize(
    "max_fee_bps",
    [1801.0, 10_000, 65_535, 65_536],
    ids=["float", "100-percent", "uint16-max", "above-uint16"],
)
def test_any_other_spelling_above_the_ceiling_is_refused(max_fee_bps):
    refused("base", table_block(8453), "above this SDK's ceiling", max_fee_bps=max_fee_bps)


def test_a_fee_written_as_a_string_is_signed_as_the_number():
    """Not the default (1800): a string read as the number, not ignored."""
    wrapper, _ = build("base", table_block(8453), max_fee_bps="1500")
    assert wrapper["payload"]["paymentInfo"]["maxFeeBps"] == 1500


def test_a_missing_max_fee_bps_signs_the_default_within_the_ceiling():
    wrapper, _ = build("base", table_block(8453))
    assert wrapper["payload"]["paymentInfo"]["maxFeeBps"] == DEFAULT_MAX_FEE_BPS


def test_the_operator_fee_floor_still_applies_under_the_ceiling():
    wrapper, _ = build("base", table_block(8453), max_fee_bps=OPERATOR_FEE_BPS)
    assert wrapper["payload"]["paymentInfo"]["maxFeeBps"] == OPERATOR_FEE_BPS
    refused("base", table_block(8453), "cannot cover", max_fee_bps=OPERATOR_FEE_BPS - 1)


@pytest.mark.parametrize("network,chain_id", TESTNETS, ids=TESTNET_IDS)
def test_a_chain_without_a_fee_ceiling_signs_with_a_warning(network, chain_id, caplog):
    caplog.set_level(logging.WARNING, logger="uvd_x402_sdk.escrow_signing")
    wrapper, wallet = build(network, table_block(chain_id), max_fee_bps=2500)
    assert len(wallet.signed) == 1
    assert wrapper["payload"]["paymentInfo"]["maxFeeBps"] == 2500
    assert any("ESCROW_MAX_FEE_BPS" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("spelling", ["lower", "upper"])
def test_the_table_address_in_another_case_is_the_same_address(spelling):
    block = table_block(8453)
    for key in (*ADDRESS_KEYS, "operator"):
        hex_part = block[key][2:]
        block[key] = "0x" + (hex_part.lower() if spelling == "lower" else hex_part.upper())
    wrapper, _ = build("base", block)
    assert wrapper["payload"]["authorization"]["to"] == ESCROW_CONTRACTS[8453]["token_collector"]


@pytest.mark.parametrize("network,chain_id", TESTNETS, ids=TESTNET_IDS)
def test_a_registry_testnet_is_signed_with_its_rows(network, chain_id, caplog):
    """No network name and no verified domain here, but a row in ESCROW_CONTRACTS."""
    caplog.set_level(logging.WARNING, logger="uvd_x402_sdk.escrow_signing")
    wrapper, _ = build(network, table_block(chain_id))
    assert wrapper["paymentRequirements"]["network"] == f"eip155:{chain_id}"
    collector = ESCROW_CONTRACTS[chain_id]["token_collector"]
    assert wrapper["payload"]["authorization"]["to"] == collector
    assert any("UNVERIFIED chain" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("key", ADDRESS_KEYS)
@pytest.mark.parametrize("network,chain_id", TESTNETS, ids=TESTNET_IDS)
def test_a_changed_registry_address_is_refused_on_a_testnet(network, chain_id, key):
    block = table_block(chain_id)
    block[key] = changed(block[key])
    refused(network, block, f"Escrow address mismatch .* asserts {key} ")


def test_a_chain_without_any_row_signs_with_both_warnings(caplog):
    caplog.set_level(logging.WARNING, logger="uvd_x402_sdk.escrow_signing")
    block = {
        "chain_id": 999999,
        "operator": "0x" + "01" * 20,
        "escrow": "0x" + "02" * 20,
        "token_collector": "0x" + "03" * 20,
        "usdc": "0x" + "04" * 20,
        "usdc_domain_name": "USD Coin",
        "usdc_domain_version": "2",
    }
    _, wallet = build("red-nueva", block)
    assert len(wallet.signed) == 1
    messages = " ".join(r.getMessage() for r in caplog.records)
    assert "no row in ESCROW_CONTRACTS" in messages
    assert "ESCROW_OPERATORS" in messages


@pytest.mark.parametrize("network", ["eip155:8453", "EIP155:8453", " eip155:8453 "])
def test_a_caip2_name_of_the_same_chain_signs(network):
    wrapper, _ = build(network, table_block(8453))
    assert wrapper["paymentRequirements"]["network"] == "eip155:8453"


@pytest.mark.parametrize("network", ["eip155:10", "EIP155:10", " eip155:10 "])
def test_a_caip2_name_of_another_chain_is_refused(network):
    refused(network, table_block(8453), "Chain id mismatch")


@pytest.mark.parametrize(
    "network",
    ["eip155:", "eip155:base", "eip155:0x2105", "eip155:-8453", "eip155:\uff18\uff14\uff15\uff13"],
    ids=["empty", "name", "hex", "negative", "fullwidth-digits"],
)
def test_an_eip155_name_without_a_decimal_id_is_refused(network):
    refused(network, table_block(8453), "names no EVM chain id")


@pytest.mark.parametrize("network", ["Base", " base ", "BASE"])
def test_another_spelling_of_a_known_name_is_still_checked(network):
    refused(network, table_block(10), "Chain id mismatch")


def test_an_alias_resolves_to_its_chain():
    """``skale`` is the network registry's alias of ``skale-base`` (Karmakadabra's name)."""
    wrapper, _ = build("skale", table_block(1187947933))
    assert wrapper["paymentRequirements"]["network"] == "eip155:1187947933"
    refused("skale", table_block(8453), "Chain id mismatch")


@pytest.mark.parametrize("network", ["solana", "stellar", "xrpl"])
def test_a_non_evm_network_is_refused(network):
    refused(network, table_block(8453), "Chain id mismatch")


def test_a_chain_id_written_as_a_decimal_string_is_the_same_chain():
    block = table_block(8453)
    block["chain_id"] = "8453"
    wrapper, _ = build("base", block)
    assert wrapper["paymentRequirements"]["network"] == "eip155:8453"


def test_a_bool_chain_id_is_refused():
    """``True`` would be read as chain 1."""
    block = table_block(1)
    block["chain_id"] = True
    refused("red-nueva", block, "Invalid chain_id")


def test_an_address_that_is_not_one_is_refused_before_signing():
    block = table_block(8453)
    block["token_collector"] = " " + block["token_collector"]
    refused("base", block, "hex string")


def test_execution_markets_base_config_signs_as_before():
    """The fixture of tests/test_escrow_signing.py, against the table as it is."""
    em = json.loads((FIXTURES / "escrow-preauth.json").read_text(encoding="utf-8"))
    block = {k: em["network_config"][k] for k in es.REQUIRED_NETWORK_KEYS}
    wrapper, _ = build(em["network"], block)
    assert wrapper["payload"]["paymentInfo"]["operator"] == ESCROW_OPERATORS[8453]


# ── a delegated payer: the same refusals, before the dialect is chosen ──────

# The resolver's verdicts: SMA wrap (legacy bool and the Alchemy target), a
# 1271 delegate that takes the plain signature, and a plain EOA.
DELEGATIONS = {
    "sma-bool": lambda address, network: True,
    "sma-target": lambda address, network: sorted(SMA_WRAP_TARGETS)[0],
    "plain-1271": lambda address, network: "0x" + "d1" * 20,
    "not-delegated": lambda address, network: False,
}


def _refused_delegated(resolver: Any, block: dict[str, Any], match: str, **escrow: Any) -> None:
    wallet = _Wallet()
    with pytest.raises(ValueError, match=match):
        es.build_escrow_pre_auth(
            config("base", block, **escrow), "base", PAYER, WORKER, "0.25", None, wallet,
            delegation_resolver=resolver,
        )
    assert wallet.signed == [], "nothing may be signed before the refusal"


@pytest.mark.parametrize("delegation", sorted(DELEGATIONS))
def test_a_delegated_payer_still_signs_the_table(delegation):
    wallet = _Wallet()
    es.build_escrow_pre_auth(
        config("base", table_block(8453)), "base", PAYER, WORKER, "0.25", None, wallet,
        delegation_resolver=DELEGATIONS[delegation],
    )
    assert len(wallet.signed) == 1


@pytest.mark.parametrize("delegation", sorted(DELEGATIONS))
def test_a_delegated_payer_gets_the_fee_ceiling_refusal(delegation):
    _refused_delegated(
        DELEGATIONS[delegation], table_block(8453), "above this SDK's ceiling", max_fee_bps=5000
    )


@pytest.mark.parametrize("delegation", sorted(DELEGATIONS))
def test_a_delegated_payer_gets_the_address_refusal(delegation):
    block = table_block(8453)
    block["token_collector"] = changed(block["token_collector"])
    _refused_delegated(DELEGATIONS[delegation], block, "Escrow address mismatch")


# ── each guard removed: its refusal becomes a signature ─────────────────────


def _neutral_case(guard: str) -> tuple[str, dict[str, Any], dict[str, Any]]:
    """The network, network block and escrow keys each guard exists to refuse."""
    network, block = _neutral_block(guard)
    escrow = {"max_fee_bps": ESCROW_MAX_FEE_BPS[8453] + 1} if guard == "max_fee_bps" else {}
    return network, block, escrow


def _neutral_block(guard: str) -> tuple[str, dict[str, Any]]:
    if guard == "max_fee_bps":
        return "base", table_block(8453)
    if guard == "chain_id":
        return "base", table_block(10)
    if guard == "caip2":
        return "eip155:10", table_block(8453)
    if guard == "caip2_id":
        return "eip155:\uff18\uff14\uff15\uff13", table_block(8453)
    if guard == "bool":
        block = table_block(1)
        block["chain_id"] = True
        return "red-nueva", block
    if guard == "operator":
        block = table_block(8453)
        block["operator"] = changed(block["operator"])
        return "base", block
    block = table_block(8453)
    block[guard] = changed(block[guard])
    return "base", block


GUARD_MUTATIONS = {
    "chain_id": (
        "    if registered is not None and registered != chain_id:\n",
        "    if False:\n",
    ),
    "caip2": (
        '    if name.startswith("eip155:"):\n',
        '    if False:\n',
    ),
    "caip2_id": (
        "        if not (reference.isascii() and reference.isdigit()):\n",
        "        if False:\n",
    ),
    "bool": (
        '    if isinstance(net["chain_id"], bool):\n',
        "    if False:\n",
    ),
    "operator": (
        '    elif to_checksum_address(net["operator"]) != to_checksum_address(operator):\n',
        "    elif False:\n",
    ),
    "escrow": (
        '_REGISTERED_ADDRESS_KEYS = ("escrow", "token_collector", "usdc")\n',
        '_REGISTERED_ADDRESS_KEYS = ("token_collector", "usdc")\n',
    ),
    "token_collector": (
        '_REGISTERED_ADDRESS_KEYS = ("escrow", "token_collector", "usdc")\n',
        '_REGISTERED_ADDRESS_KEYS = ("escrow", "usdc")\n',
    ),
    "usdc": (
        '_REGISTERED_ADDRESS_KEYS = ("escrow", "token_collector", "usdc")\n',
        '_REGISTERED_ADDRESS_KEYS = ("escrow", "token_collector")\n',
    ),
    "max_fee_bps": (
        "    elif max_fee_bps > ceiling:\n",
        "    elif False:\n",
    ),
}


class TestGuardMutations:
    @pytest.mark.parametrize("guard", sorted(GUARD_MUTATIONS))
    def test_the_unmutated_source_refuses_the_case(self, guard):
        network, block, escrow = _neutral_case(guard)
        with pytest.raises(ValueError):
            build(network, block, **escrow)

    @pytest.mark.parametrize("guard", sorted(GUARD_MUTATIONS))
    def test_without_the_guard_the_case_is_signed(self, guard):
        old, new = GUARD_MUTATIONS[guard]
        module = mutant(old, new)
        network, block, escrow = _neutral_case(guard)
        _, wallet = build(network, block, module=module, **escrow)
        assert len(wallet.signed) == 1

    @pytest.mark.parametrize("guard", ["escrow", "token_collector", "usdc"])
    def test_dropping_one_address_leaves_the_other_two_guarded(self, guard):
        old, new = GUARD_MUTATIONS[guard]
        module = mutant(old, new)
        for other in ADDRESS_KEYS:
            if other != guard:
                network, block, escrow = _neutral_case(other)
                refused(network, block, "Escrow address mismatch", module=module, **escrow)
