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
=================  ==========================================  ======================

For each address, on every chain of ``VERIFIED_USDC_DOMAINS``: the table's
value is signed, and a changed one is refused with nothing signed. A chain
without a row is signed with a warning (same policy as the domain).
``TestGuardMutations`` removes each guard from a copy of the module's source
and checks that the refusal it owns turns into a signature.
"""

from __future__ import annotations

import ast
import json
import logging
from pathlib import Path
from typing import Any

import pytest
from eth_utils import to_checksum_address

import uvd_x402_sdk.advanced_escrow as ae
import uvd_x402_sdk.escrow_contracts as ec
import uvd_x402_sdk.escrow_signing as es
from tests.test_escrow_vectors import mutant
from uvd_x402_sdk.escrow_contracts import ESCROW_CONTRACTS, ESCROW_OPERATORS
from uvd_x402_sdk.escrow_signing import (
    ESCROW_PAYMENT_INFO_TYPEHASH,
    VERIFIED_USDC_DOMAINS,
    compute_escrow_nonce,
)
from uvd_x402_sdk.networks import get_network_by_chain_id

PAYER = "0x" + "a1" * 20
WORKER = "0x" + "5e" * 20
CHAINS = sorted(VERIFIED_USDC_DOMAINS)
CHAIN_IDS = [get_network_by_chain_id(c).name for c in CHAINS]
WITH_OPERATOR = sorted(c for c in CHAINS if c in ESCROW_OPERATORS)
WITHOUT_OPERATOR = sorted(c for c in CHAINS if c not in ESCROW_OPERATORS)
ADDRESS_KEYS = ("escrow", "token_collector", "usdc")
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


def config(network: str, block: dict[str, Any]) -> dict[str, Any]:
    return {
        "escrow": {
            "payment_info_typehash": ESCROW_PAYMENT_INFO_TYPEHASH,
            "networks": {network: block},
        }
    }


def build(network: str, block: dict[str, Any], module: Any = es) -> tuple[dict, _Wallet]:
    wallet = _Wallet()
    header = module.build_escrow_pre_auth(
        config(network, block), network, PAYER, WORKER, "0.25", None, wallet
    )
    return json.loads(header), wallet


def changed(address: str) -> str:
    """The same address with its last hex digit moved by one."""
    last = format((int(address[-1], 16) + 1) % 16, "x")
    return address[:-1] + last


def refused(network: str, block: dict[str, Any], match: str, module: Any = es) -> None:
    wallet = _Wallet()
    with pytest.raises(ValueError, match=match):
        module.build_escrow_pre_auth(
            config(network, block), network, PAYER, WORKER, "0.25", None, wallet
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


@pytest.mark.parametrize("chain_id", sorted(ESCROW_CONTRACTS))
def test_the_operator_column_is_advanced_escrows_default_operator(chain_id):
    """A row is the operator AdvancedEscrowClient picks with none given; no row, no default."""
    kwargs = {"private_key": "0x" + "4b" * 32, "chain_id": chain_id, "rpc_url": "http://127.0.0.1:9"}
    if chain_id in ESCROW_OPERATORS:
        client = ae.AdvancedEscrowClient(**kwargs)
        assert client.contracts["operator"] == ESCROW_OPERATORS[chain_id]
    else:
        with pytest.raises(ValueError, match="operator_address is required"):
            ae.AdvancedEscrowClient(**kwargs)


def test_the_arc_operator_is_the_recorded_compute_address():
    recorded = json.loads((FIXTURES / "arc-escrow-d.json").read_text(encoding="utf-8"))
    assert ESCROW_OPERATORS[5042] == ESCROW_OPERATORS[5042002] == recorded["default_operator"]
    for chain in ("5042", "5042002"):
        result = recorded["chains"][chain]["compute_address"]["result"]
        assert int(result, 16) == int(recorded["default_operator"], 16)


def test_the_base_operator_is_execution_markets():
    em = json.loads((FIXTURES / "escrow-preauth.json").read_text(encoding="utf-8"))
    assert em["network_config"]["operator"] == ESCROW_OPERATORS[8453]
    assert ae.BASE_MAINNET_CONTRACTS["operator"] == ESCROW_OPERATORS[8453]


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


@pytest.mark.parametrize(
    "chain_id", WITHOUT_OPERATOR, ids=[network_of(c) for c in WITHOUT_OPERATOR]
)
def test_a_chain_without_an_operator_row_signs_the_configs_with_a_warning(chain_id, caplog):
    caplog.set_level(logging.WARNING, logger="uvd_x402_sdk.escrow_signing")
    block = table_block(chain_id)
    block["operator"] = to_checksum_address("0x" + "0e" * 20)
    wrapper, wallet = build(network_of(chain_id), block)
    assert len(wallet.signed) == 1
    assert wrapper["payload"]["paymentInfo"]["operator"] == block["operator"]
    assert any("ESCROW_OPERATORS" in r.getMessage() for r in caplog.records)


# ── edges ────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("spelling", ["lower", "upper"])
def test_the_table_address_in_another_case_is_the_same_address(spelling):
    block = table_block(8453)
    for key in (*ADDRESS_KEYS, "operator"):
        hex_part = block[key][2:]
        block[key] = "0x" + (hex_part.lower() if spelling == "lower" else hex_part.upper())
    wrapper, _ = build("base", block)
    assert wrapper["payload"]["authorization"]["to"] == ESCROW_CONTRACTS[8453]["token_collector"]


TESTNETS = [("base-sepolia", 84532), ("ethereum-sepolia", 11155111)]


@pytest.mark.parametrize("network,chain_id", TESTNETS, ids=[n for n, _ in TESTNETS])
def test_a_registry_testnet_is_signed_with_its_rows(network, chain_id, caplog):
    """No network name and no verified domain here, but a row in ESCROW_CONTRACTS."""
    caplog.set_level(logging.WARNING, logger="uvd_x402_sdk.escrow_signing")
    wrapper, _ = build(network, table_block(chain_id))
    assert wrapper["paymentRequirements"]["network"] == f"eip155:{chain_id}"
    collector = ESCROW_CONTRACTS[chain_id]["token_collector"]
    assert wrapper["payload"]["authorization"]["to"] == collector
    assert any("UNVERIFIED chain" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("key", ADDRESS_KEYS)
@pytest.mark.parametrize("network,chain_id", TESTNETS, ids=[n for n, _ in TESTNETS])
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


# ── each guard removed: its refusal becomes a signature ─────────────────────


def _neutral_case(guard: str) -> tuple[str, dict[str, Any]]:
    """The network and config each guard exists to refuse."""
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
}


class TestGuardMutations:
    @pytest.mark.parametrize("guard", sorted(GUARD_MUTATIONS))
    def test_the_unmutated_source_refuses_the_case(self, guard):
        network, block = _neutral_case(guard)
        with pytest.raises(ValueError):
            build(network, block)

    @pytest.mark.parametrize("guard", sorted(GUARD_MUTATIONS))
    def test_without_the_guard_the_case_is_signed(self, guard):
        old, new = GUARD_MUTATIONS[guard]
        module = mutant(old, new)
        network, block = _neutral_case(guard)
        _, wallet = build(network, block, module=module)
        assert len(wallet.signed) == 1

    @pytest.mark.parametrize("guard", ["escrow", "token_collector", "usdc"])
    def test_dropping_one_address_leaves_the_other_two_guarded(self, guard):
        old, new = GUARD_MUTATIONS[guard]
        module = mutant(old, new)
        for other in ADDRESS_KEYS:
            if other != guard:
                network, block = _neutral_case(other)
                refused(network, block, "Escrow address mismatch", module=module)
