"""Escrow on Robinhood Chain mainnet (4663): the canonical x402r set, with USDG.

Robinhood Chain had payments (Paxos USDG) and ERC-8004 before; this release adds
its escrow rows, so ``build_escrow_pre_auth`` signs the escrow lock there (the
TypeScript twin's ``buildEscrowPreAuth`` gets the same rows). Pinned here,
offline (no RPC, no facilitator):

1. The rows of 4663 in ``ESCROW_CONTRACTS``, ``ESCROW_OPERATORS``,
   ``ESCROW_MAX_FEE_BPS`` and ``VERIFIED_USDC_DOMAINS``, plus
   ``ESCROW_GENERATIONS`` (``"v3"``) and ``ESCROW_CHAIN_NAMES``. Every address
   is Arc's except the token, USDG ``0x5fc5…d168`` (6 decimals), whose EIP-712
   domain is ``Global Dollar`` / ``1``.
2. Each value against three sources, none derived from the code under test:
   (a) Arc's row of this SDK; (b) the chain, read on 2026-11-05 at L2 block
   106.891.112 (mainnet) and at the testnet's head:
   ``tests/fixtures/robinhood-escrow-chain.json`` keeps the size and keccak256
   of each runtime code and the raw calldata and answer of each call, decoded
   HERE; (c) x402-rs main at ``bc2fe3e5``, transcribed below
   (``X402RS_ROBINHOOD``).
3. The pre-auth signs on ``robinhood`` and on ``eip155:4663`` with the USDG
   domain, and the signature recovers to the payer over that domain only.
4. ``robinhood-testnet`` (46630) has no row: the canonical set has code there
   but Execution Market's operator does not (its code size is 0), and the
   pre-auth refuses it with the error of any chain without rows.
5. ``AdvancedEscrowClient`` on 4663: v3 operator ABI (``release`` is
   ``capture``), USDG domain (version ``1``, never ``2``), and no default
   operator (its ``max_fee_bps`` default of 800 does not cover the operator's
   1300 bps fee).
6. Nothing else moved: every table outside 4663 is what 0.110.0 shipped.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any
from unittest import mock

import pytest
from eth_abi import encode
from eth_account import Account
from eth_account.messages import encode_typed_data
from eth_utils import keccak, to_checksum_address

import uvd_x402_sdk.advanced_escrow as ae
import uvd_x402_sdk.escrow_signing as es
from tests.test_arc_escrow import COMPUTE_ADDRESS_ARG
from tests.test_arc_escrow import FIXTURE as ARC_RECORDING
from tests.test_escrow_signing_registry import EXECUTION_MARKET_ESCROW
from uvd_x402_sdk.escrow_contracts import ESCROW_CONTRACTS, ESCROW_MAX_FEE_BPS, ESCROW_OPERATORS
from uvd_x402_sdk.escrow_signing import (
    ESCROW_PAYMENT_INFO_TYPEHASH,
    RECEIVE_WITH_AUTHORIZATION_TYPES,
    VERIFIED_USDC_DOMAINS,
    compute_escrow_nonce,
)
from uvd_x402_sdk.networks import get_network
from uvd_x402_sdk.wallet import EnvKeyAdapter

ROBINHOOD = 4663
TESTNET = 46630
ARC = 5042
FIXTURES = Path(__file__).resolve().parent / "fixtures"
CHAIN_PATH = FIXTURES / "robinhood-escrow-chain.json"
# The recording is never edited: a changed byte is a different measurement.
CHAIN_SHA256 = "61d2bd4b0ebf0c6130bf7d3151338af781fb78e6c4ed1384aba8deb8aeae06ed"
CHAIN = json.loads(CHAIN_PATH.read_text(encoding="utf-8"))

KEY = "0x" + "4b" * 32  # synthetic, never held funds
WORKER = to_checksum_address("0x" + "5e" * 20)
UNREACHABLE_RPC = "http://127.0.0.1:9"  # never contacted
TABLES = ("ESCROW_CONTRACTS", "ESCROW_OPERATORS", "ESCROW_MAX_FEE_BPS", "VERIFIED_USDC_DOMAINS")

# (c) x402-rs main at bc2fe3e5cb51c17f54510ac83e260f7c98a088c8, transcribed by
# hand (the only hand copy of the row in this repo, on purpose: an edit of the
# table to another valid address turns it red). src/payment_operator/addresses.rs:
# CANONICAL_V1_NETWORKS = [Arc, ArcTestnet, Robinhood]; canonical_v1::ESCROW,
# FACTORY_PAYMENT_OPERATOR, TOKEN_COLLECTOR, PROTOCOL_FEE_CONFIG,
# FACTORY_REFUND_REQUEST; OperatorAddresses::canonical_v1_set().payment_operators;
# ONE_TOKEN_ESCROW_NETWORKS = [Robinhood] with served_escrow_token = USDG.
# src/network.rs: USDG_ROBINHOOD (address, decimals, eip712 name / version).
X402RS_ROBINHOOD: dict[str, Any] = {
    "escrow": "0xBdEA0D1bcC5966192B070Fdf62aB4EF5b4420cff",
    "operator_factory": "0xc24153B7ED8DC03e551F29DDEeA5CadFe57e2716",
    "token_collector": "0x0E3dF9510de65469C4518D7843919c0b8C7A7757",
    "protocol_fee_config": "0xBe2d24614F339a1eB103A399F93AA2a39Ca815Bc",
    "refund_request": "0xe971C674fD5c3462023f3F891dF6289DFbC9CEFC",
    "usdc": "0x5fc5360D0400a0Fd4f2af552ADD042D716F1d168",
    "operator": "0x0258472A1410Ac3Ad720f1BC83f22B3c0af1Fd9D",
    "decimals": 6,
    "domain": ("Global Dollar", "1"),
}
CONTRACT_KEYS = (
    "escrow",
    "operator_factory",
    "token_collector",
    "protocol_fee_config",
    "refund_request",
    "usdc",
)
USDG = X402RS_ROBINHOOD["usdc"]
EM_OPERATOR = X402RS_ROBINHOOD["operator"]

# The tables as 0.110.0 shipped them, chain ids only (their values are pinned by
# tests/fixtures/escrow-calldata-snapshot.json, test_escrow_signing_registry.py
# and test_arc_escrow.py). 4663 is the one chain this change adds.
CONTRACT_CHAINS_0_110_0 = {
    84532, 11155111, 8453, 1, 10, 137, 42161, 42220, 143, 43114, 1187947933, 5042, 5042002,
}
ROW_CHAINS_0_110_0 = CONTRACT_CHAINS_0_110_0 - {84532, 11155111}
GENERATIONS_0_110_0 = {
    84532: "v1", 11155111: "v1", 8453: "v1", 1: "v1", 10: "v1", 137: "v1", 42161: "v1",
    42220: "v1", 143: "v1", 43114: "v1", 1187947933: "v2", 5042: "v3", 5042002: "v3",
}


def _call(to: str, selector: bytes) -> dict[str, Any]:
    """The one mainnet eth_call to ``to`` whose calldata starts with ``selector``."""
    [call] = [
        c for c in CHAIN["mainnet"]["calls"]
        if c["to"] == to and c["data"].startswith(selector.hex())
    ]
    return call


def _code(network: str, address: str) -> dict[str, Any]:
    return CHAIN[network]["code"][address]


def _decode_address(word: str) -> str:
    assert len(word) == 64 and int(word[:24], 16) == 0, word
    return to_checksum_address("0x" + word[24:])


# ── (b) the chain, decoded from the recorded answers ────────────────────────


def test_the_chain_recording_is_pinned():
    assert hashlib.sha256(CHAIN_PATH.read_bytes()).hexdigest() == CHAIN_SHA256


def test_the_rpc_answered_as_robinhood_mainnet_and_testnet():
    assert int(CHAIN["mainnet"]["eth_chainId"], 16) == ROBINHOOD
    assert int(CHAIN["testnet"]["eth_chainId"], 16) == TESTNET
    assert int(CHAIN["mainnet"]["block"], 16) == 106_891_112


def test_the_factory_maps_execution_markets_config_to_the_operator_row():
    """getOperator(config) on the row's factory, with the config Arc's operator came from."""
    signature = "getOperator((" + ",".join(["address"] * 12) + "))"
    selector = keccak(text=signature)[:4]
    call = _call(ESCROW_CONTRACTS[ROBINHOOD]["operator_factory"], selector)
    argument = encode(["(" + ",".join(["address"] * 12) + ")"], [COMPUTE_ADDRESS_ARG])
    assert call["data"] == (selector + argument).hex()
    assert _decode_address(call["result"]) == ESCROW_OPERATORS[ROBINHOOD]


@pytest.mark.parametrize(
    "getter, key", [("ESCROW()", "escrow"), ("PROTOCOL_FEE_CONFIG()", "protocol_fee_config")]
)
def test_the_operator_names_the_rows_contracts(getter, key):
    call = _call(ESCROW_OPERATORS[ROBINHOOD], keccak(text=getter)[:4])
    assert call["data"] == keccak(text=getter)[:4].hex()
    assert _decode_address(call["result"]) == ESCROW_CONTRACTS[ROBINHOOD][key]


@pytest.mark.parametrize("key", ["escrow", "operator_factory", "token_collector"])
def test_the_code_at_the_row_is_the_code_this_sdk_recorded_on_arc(key):
    """Size and keccak256 of the runtime, against tests/fixtures/arc-escrow-d.json (ours)."""
    code = _code("mainnet", ESCROW_CONTRACTS[ROBINHOOD][key])
    arc = ARC_RECORDING["chains"][str(ARC)]["code"][key]
    assert arc["address"] == ESCROW_CONTRACTS[ROBINHOOD][key]
    assert code["size"] == int(arc["size"]) > 0
    assert "0x" + code["keccak"] == arc["keccak"]


def test_the_operator_has_code_on_mainnet():
    assert _code("mainnet", ESCROW_OPERATORS[ROBINHOOD])["size"] > 0


def test_execution_markets_operator_has_no_code_on_the_testnet():
    """Why 46630 gets no row: the canonical set is there, the operator is not."""
    assert _code("testnet", EM_OPERATOR)["size"] == 0
    for key in ("escrow", "operator_factory", "token_collector"):
        address = X402RS_ROBINHOOD[key]
        assert _code("testnet", address) == _code("mainnet", address), key


# ── the rows against (a) Arc and (c) x402-rs ─────────────────────────────────


def test_the_rows_are_x402rs_at_bc2fe3e5():
    assert ESCROW_CONTRACTS[ROBINHOOD] == {k: X402RS_ROBINHOOD[k] for k in CONTRACT_KEYS}
    assert ESCROW_OPERATORS[ROBINHOOD] == X402RS_ROBINHOOD["operator"]
    assert VERIFIED_USDC_DOMAINS[ROBINHOOD] == X402RS_ROBINHOOD["domain"]
    assert ESCROW_MAX_FEE_BPS[ROBINHOOD] == 1800
    for key in CONTRACT_KEYS:
        assert to_checksum_address(ESCROW_CONTRACTS[ROBINHOOD][key]) == (
            ESCROW_CONTRACTS[ROBINHOOD][key]
        ), key
    assert to_checksum_address(ESCROW_OPERATORS[ROBINHOOD]) == ESCROW_OPERATORS[ROBINHOOD]


def test_the_rows_are_arcs_but_the_token_and_its_domain():
    robinhood, arc = ESCROW_CONTRACTS[ROBINHOOD], ESCROW_CONTRACTS[ARC]
    assert set(robinhood) == set(arc)
    assert {k: v for k, v in robinhood.items() if k != "usdc"} == {
        k: v for k, v in arc.items() if k != "usdc"
    }
    assert robinhood["usdc"] != arc["usdc"]
    assert ESCROW_OPERATORS[ROBINHOOD] == ESCROW_OPERATORS[ARC]
    assert ESCROW_MAX_FEE_BPS[ROBINHOOD] == ESCROW_MAX_FEE_BPS[ARC]
    assert ae.get_escrow_generation(ROBINHOOD) == ae.get_escrow_generation(ARC) == "v3"
    assert VERIFIED_USDC_DOMAINS[ROBINHOOD] != VERIFIED_USDC_DOMAINS[ARC]


def test_the_token_is_the_network_registrys_usdg():
    network = get_network("robinhood")
    assert network.chain_id == ROBINHOOD
    assert network.usdc_address == ESCROW_CONTRACTS[ROBINHOOD]["usdc"]
    assert network.usdc_decimals == X402RS_ROBINHOOD["decimals"] == es.USDC_DECIMALS
    assert (network.usdc_domain_name, network.usdc_domain_version) == (
        VERIFIED_USDC_DOMAINS[ROBINHOOD]
    )
    usdg = network.tokens["usdg"]
    assert (usdg.address, usdg.decimals, usdg.name, usdg.version) == (
        USDG, 6, "Global Dollar", "1"
    )


def test_the_generation_and_the_name():
    assert ae.ESCROW_GENERATIONS[ROBINHOOD] == "v3"
    assert ae.get_operator_abi(ROBINHOOD) is ae.OPERATOR_ABI_V3
    assert ae.ESCROW_CHAIN_NAMES[ROBINHOOD] == "Robinhood Chain"
    assert ae.is_escrow_supported(ROBINHOOD) is True
    assert ae.get_escrow_contracts(ROBINHOOD) is ESCROW_CONTRACTS[ROBINHOOD]


# ── nothing else moved ───────────────────────────────────────────────────────


def test_the_tables_add_exactly_robinhood():
    assert set(ESCROW_CONTRACTS) == CONTRACT_CHAINS_0_110_0 | {ROBINHOOD}
    assert set(ae.ESCROW_CHAIN_NAMES) == CONTRACT_CHAINS_0_110_0 | {ROBINHOOD}
    for table in (ESCROW_OPERATORS, ESCROW_MAX_FEE_BPS, VERIFIED_USDC_DOMAINS):
        assert set(table) == ROW_CHAINS_0_110_0 | {ROBINHOOD}


def test_no_other_generation_moved():
    assert {c: g for c, g in ae.ESCROW_GENERATIONS.items() if c != ROBINHOOD} == (
        GENERATIONS_0_110_0
    )
    assert ae.CREATE3_CHAIN_IDS == {1187947933}


def test_the_client_default_operator_chains_did_not_move():
    assert ae._DEFAULT_OPERATOR_CHAINS == {8453, 1187947933, 5042, 5042002}


# ── the pre-auth on Robinhood ────────────────────────────────────────────────


def robinhood_block(**override: Any) -> dict[str, Any]:
    """The network block of a payment config, from the x402-rs transcription."""
    block = {
        "chain_id": ROBINHOOD,
        "operator": EM_OPERATOR,
        "escrow": X402RS_ROBINHOOD["escrow"],
        "token_collector": X402RS_ROBINHOOD["token_collector"],
        "usdc": USDG,
        "usdc_domain_name": X402RS_ROBINHOOD["domain"][0],
        "usdc_domain_version": X402RS_ROBINHOOD["domain"][1],
    }
    block.update(override)
    return block


class _Recording:
    def __init__(self) -> None:
        self.inner = EnvKeyAdapter(private_key=KEY)
        self.typed: list[dict[str, Any]] = []

    def get_address(self) -> str:
        return self.inner.get_address()

    def sign_message(self, message: Any) -> Any:
        return self.inner.sign_message(message)

    def sign_typed_data(self, typed: dict[str, Any]) -> Any:
        self.typed.append(typed)
        return self.inner.sign_typed_data(typed)


NOW = 1_790_000_000


def build(
    network: str, block: dict[str, Any], *, escrow: dict[str, Any] | None = None
) -> tuple[dict[str, Any], _Recording]:
    wallet = _Recording()
    payment_config = {
        "escrow": {
            "payment_info_typehash": ESCROW_PAYMENT_INFO_TYPEHASH,
            **(EXECUTION_MARKET_ESCROW if escrow is None else escrow),
            "networks": {network: block},
        }
    }
    with mock.patch.object(es.time, "time", lambda: NOW):
        header = es.build_escrow_pre_auth(
            payment_config, network, wallet.get_address(), WORKER, "0.25", None, wallet
        )
    return json.loads(header), wallet


def refused(network: str, block: dict[str, Any], match: str, **kwargs: Any) -> str:
    wallet = _Recording()
    payment_config = {
        "escrow": {
            "payment_info_typehash": ESCROW_PAYMENT_INFO_TYPEHASH,
            **EXECUTION_MARKET_ESCROW,
            **kwargs,
            "networks": {network: block},
        }
    }
    with pytest.raises(ValueError, match=match) as refusal:
        es.build_escrow_pre_auth(
            payment_config, network, wallet.get_address(), WORKER, "0.25", None, wallet
        )
    assert wallet.typed == [], "nothing may be signed before the refusal"
    return str(refusal.value)


SPELLINGS = ["robinhood", "eip155:4663", "EIP155:4663", " eip155:4663 "]


@pytest.mark.parametrize("network", SPELLINGS)
def test_the_pre_auth_signs_usdg_on_robinhood(network):
    wrapper, wallet = build(network, robinhood_block())
    [typed] = wallet.typed
    payload = wrapper["payload"]
    payment_info, authorization = payload["paymentInfo"], payload["authorization"]

    assert typed["domain"] == {
        "name": "Global Dollar",
        "version": "1",
        "chainId": ROBINHOOD,
        "verifyingContract": USDG,
    }
    assert typed["primaryType"] == "ReceiveWithAuthorization"
    assert wrapper["paymentRequirements"] == {"scheme": "escrow", "network": "eip155:4663"}
    assert payment_info["token"] == USDG
    assert payment_info["operator"] == payment_info["feeReceiver"] == EM_OPERATOR
    assert payment_info["maxFeeBps"] == EXECUTION_MARKET_ESCROW["max_fee_bps"] == 1800
    assert payment_info["maxAmount"] == authorization["value"] == "250000"  # 6 decimals
    assert authorization["to"] == typed["message"]["to"] == X402RS_ROBINHOOD["token_collector"]
    assert authorization["nonce"] == compute_escrow_nonce(
        ROBINHOOD, X402RS_ROBINHOOD["escrow"], ESCROW_PAYMENT_INFO_TYPEHASH, payment_info
    )


def _signable(domain: tuple[str, str, int, str], authorization: dict[str, Any]) -> Any:
    """The ReceiveWithAuthorization, rebuilt from the wire with eth-account only."""
    name, version, chain_id, token = domain
    return encode_typed_data(
        domain_data={
            "name": name, "version": version, "chainId": chain_id, "verifyingContract": token,
        },
        message_types=RECEIVE_WITH_AUTHORIZATION_TYPES,
        message_data={
            "from": authorization["from"],
            "to": authorization["to"],
            "value": int(authorization["value"]),
            "validAfter": int(authorization["validAfter"]),
            "validBefore": int(authorization["validBefore"]),
            "nonce": bytes.fromhex(authorization["nonce"].removeprefix("0x")),
        },
    )


def test_the_signature_recovers_to_the_payer_over_the_usdg_domain_only():
    wrapper, wallet = build("robinhood", robinhood_block())
    authorization, signature = wrapper["payload"]["authorization"], wrapper["payload"]["signature"]
    usdg = ("Global Dollar", "1", ROBINHOOD, USDG)
    assert Account.recover_message(_signable(usdg, authorization), signature=signature) == (
        wallet.get_address()
    )
    # The same message under another domain is another digest: not the payer.
    for other in [
        ("USD Coin", "2", ROBINHOOD, USDG),
        ("USDC", "2", ROBINHOOD, USDG),
        ("Global Dollar", "2", ROBINHOOD, USDG),
        ("Global Dollar", "1", ARC, USDG),
        ("Global Dollar", "1", ROBINHOOD, ESCROW_CONTRACTS[ARC]["usdc"]),
    ]:
        recovered = Account.recover_message(_signable(other, authorization), signature=signature)
        assert recovered != wallet.get_address(), other


def test_the_fee_ceiling_is_signed_and_one_bps_above_is_refused():
    escrow = dict(EXECUTION_MARKET_ESCROW, max_fee_bps=1800)
    wrapper, _ = build("robinhood", robinhood_block(), escrow=escrow)
    assert wrapper["payload"]["paymentInfo"]["maxFeeBps"] == 1800
    refused("robinhood", robinhood_block(), "maxFeeBps=1801 .* above this SDK's ceiling",
            max_fee_bps=1801)


@pytest.mark.parametrize(
    "domain",
    [
        ("USD Coin", "2"), ("USDC", "2"), ("Global Dollar", "2"), ("global dollar", "1"),
        ("Global Dollar ", "1"), ("USDG", "1"), ("Global Dollar", "1.0"),
    ],
    ids=["usd-coin", "usdc", "version-2", "lowercase", "trailing-space", "symbol", "version-1.0"],
)
def test_another_domain_for_usdg_is_refused(domain):
    block = robinhood_block(usdc_domain_name=domain[0], usdc_domain_version=domain[1])
    refused("robinhood", block, "EIP-712 domain mismatch")


@pytest.mark.parametrize(
    "token",
    [
        ESCROW_CONTRACTS[ARC]["usdc"],  # Arc's USDC: the one address of Arc's row not shared
        "0x7E955252E15c84f5768B83c41a71F9eba181802F",  # USDG on robinhood-testnet
    ],
    ids=["arc-usdc", "testnet-usdg"],
)
def test_another_token_on_robinhood_is_refused(token):
    refused("robinhood", robinhood_block(usdc=token), "Escrow address mismatch .* asserts usdc ")


def test_usdg_under_arc_is_refused():
    block = dict(robinhood_block(), chain_id=ARC, usdc_domain_name="USDC", usdc_domain_version="2")
    refused("arc", block, "Escrow address mismatch .* asserts usdc ")


def test_execution_markets_served_block_needs_the_token_filled_in():
    """EM serves ``usdc: null`` on Robinhood and the token under ``tokens.USDG``.

    Its ``GET /api/v1/h2a/payment-config`` is per token, and Robinhood Chain has
    no USDC, so ``usdc`` is null there. A caller reads the token as
    ``usdc ?? tokens.USDG.address``. As served, the pre-auth refuses the block
    before signing; with the token filled in that way, it signs.
    """
    served = robinhood_block(usdc=None)
    served["tokens"] = {
        "USDG": {
            "address": USDG, "domain_name": "Global Dollar", "domain_version": "1", "decimals": 6,
        }
    }
    incomplete = r"Incomplete escrow config for 'robinhood' \(missing \['usdc'\]\)"
    refused("robinhood", dict(served), incomplete)
    filled = dict(served, usdc=served["tokens"]["USDG"]["address"])
    wrapper, wallet = build("robinhood", filled)
    assert len(wallet.typed) == 1
    assert wrapper["payload"]["paymentInfo"]["token"] == USDG


# ── robinhood-testnet (46630): no row, the error of any chain without rows ──


def _testnet_block() -> dict[str, Any]:
    """The mainnet row moved to 46630: the canonical set is there, with its own USDG."""
    testnet = get_network("robinhood-testnet")
    return robinhood_block(chain_id=TESTNET, usdc=testnet.usdc_address)


def test_the_testnet_is_registered_and_has_no_row_in_any_table():
    assert get_network("robinhood-testnet").chain_id == TESTNET
    for name in TABLES:
        assert TESTNET not in getattr(es, name), name
    assert TESTNET not in ae.ESCROW_GENERATIONS
    assert ae.is_escrow_supported(TESTNET) is False


def _normalized(message: str, chain_id: int, network: str) -> str:
    return message.replace(f"chain {chain_id} ('{network}')", "chain <id> ('<network>')")


@pytest.mark.parametrize("network", ["robinhood-testnet", "eip155:46630"])
def test_the_testnet_is_refused_with_the_error_of_any_chain_without_rows(network):
    message = refused(network, _testnet_block(), "no escrow row for chain 46630")
    for name in TABLES:
        assert name in message
    # Byte for byte the refusal of another registered chain without rows (HyperEVM).
    hyperevm = get_network("hyperevm")
    other = refused(
        "hyperevm",
        robinhood_block(
            chain_id=hyperevm.chain_id,
            usdc=hyperevm.usdc_address,
            usdc_domain_name=hyperevm.usdc_domain_name,
            usdc_domain_version=hyperevm.usdc_domain_version,
        ),
        "no escrow row for chain 999",
    )
    assert _normalized(message, TESTNET, network) == _normalized(other, 999, "hyperevm")


def test_the_testnet_under_the_mainnet_name_is_a_chain_id_mismatch():
    refused("robinhood", _testnet_block(), "Chain id mismatch")


# ── AdvancedEscrowClient on Robinhood ────────────────────────────────────────


def test_the_client_asks_for_the_operator_on_robinhood():
    """Not a client default: build_payment_info's max_fee_bps of 800 cannot pay 1300 bps."""
    with pytest.raises(ValueError, match=r"operator_address is required for chain Robinhood Chain "
                       r"\(chain_id=4663\)"):
        ae.AdvancedEscrowClient(private_key=KEY, chain_id=ROBINHOOD, rpc_url=UNREACHABLE_RPC)


def _client(**kwargs: Any) -> ae.AdvancedEscrowClient:
    return ae.AdvancedEscrowClient(
        chain_id=ROBINHOOD, rpc_url=UNREACHABLE_RPC, operator_address=EM_OPERATOR, **kwargs
    )


def test_the_client_resolves_the_row_and_speaks_v3():
    client = _client(private_key=KEY)
    assert client.contracts == {"operator": EM_OPERATOR, **ESCROW_CONTRACTS[ROBINHOOD]}
    assert client.generation == "v3"
    names = {entry["name"] for entry in client.operator_contract.abi}
    assert {"capture", "void", "refund"} <= names
    assert not {"release", "refundInEscrow"} & names


def _payment_info() -> ae.PaymentInfo:
    return ae.PaymentInfo(
        operator=EM_OPERATOR, receiver=WORKER, token=USDG, max_amount=250_000,
        pre_approval_expiry=NOW + 3600, authorization_expiry=NOW + 7200,
        refund_expiry=NOW + 86400, min_fee_bps=0, max_fee_bps=1800,
        fee_receiver=EM_OPERATOR, salt=7,
    )


def test_release_on_robinhood_is_capture():
    client = _client(private_key=KEY)
    sent: list[str] = []

    def record(func_call: Any) -> ae.TransactionResult:
        sent.append(func_call._encode_transaction_data())
        return ae.TransactionResult(success=True)

    client._send_tx = record  # type: ignore[method-assign]
    pi = _payment_info()
    client.release(pi, 100_000)
    selector = keccak(text=f"capture({es.PAYMENT_INFO_ABI},uint256,bytes)")[:4]
    arguments = encode(
        [es.PAYMENT_INFO_ABI, "uint256", "bytes"], [client._build_tuple(pi), 100_000, b""]
    )
    assert sent == ["0x" + (selector + arguments).hex()]


def test_the_client_signs_the_usdg_domain_and_the_same_nonce_as_the_pre_auth():
    wallet = _Recording()
    client = _client(wallet=wallet)
    pi = _payment_info()
    nonce = client._compute_nonce(pi)
    wire = {
        "operator": pi.operator, "receiver": pi.receiver, "token": pi.token,
        "maxAmount": str(pi.max_amount), "preApprovalExpiry": pi.pre_approval_expiry,
        "authorizationExpiry": pi.authorization_expiry, "refundExpiry": pi.refund_expiry,
        "minFeeBps": pi.min_fee_bps, "maxFeeBps": pi.max_fee_bps,
        "feeReceiver": pi.fee_receiver, "salt": pi.salt,
    }
    assert nonce == compute_escrow_nonce(
        ROBINHOOD, ESCROW_CONTRACTS[ROBINHOOD]["escrow"], ESCROW_PAYMENT_INFO_TYPEHASH, wire
    )
    client._sign_erc3009({
        "from": wallet.get_address(), "to": ESCROW_CONTRACTS[ROBINHOOD]["token_collector"],
        "value": pi.max_amount, "validAfter": 0, "validBefore": pi.pre_approval_expiry,
        "nonce": nonce,
    })
    [typed] = wallet.typed
    assert typed["domain"] == {
        "name": "Global Dollar", "version": "1", "chainId": ROBINHOOD, "verifyingContract": USDG,
    }


class _Settled:
    status_code = 200

    def json(self) -> dict[str, Any]:
        return {"success": True, "transaction": "0x" + "ab" * 32}


def test_authorize_on_robinhood_signs_usdg_version_1_never_2():
    """The /settle body ``authorize()`` posts recovers over ``Global Dollar`` / ``1`` only.

    The TypeScript twin's ``AdvancedEscrowClient.signErc3009`` signed with a
    fixed version ``'2'`` (measured by c0-sdkts-escrow-4663): USDG, version
    ``1``, would never settle that authorization. Here the version comes from
    the network registry (``_sign_erc3009``); this pins it on the raw-key path,
    through the body that goes on the wire. The other chains' bodies are pinned
    by tests/fixtures/escrow-calldata-snapshot.json, unchanged.
    """
    client = _client(private_key=KEY)
    posted: list[dict[str, Any]] = []

    def record_post(url: str, json: Any = None, **_: Any) -> _Settled:
        posted.append(json)
        return _Settled()

    with mock.patch.object(ae.httpx, "post", record_post):
        result = client.authorize(_payment_info())
    assert result.success
    [body] = posted
    authorization, signature = body["payload"]["authorization"], body["payload"]["signature"]
    payer = Account.from_key(KEY).address
    assert authorization["from"] == payer
    version_1 = _signable(("Global Dollar", "1", ROBINHOOD, USDG), authorization)
    version_2 = _signable(("Global Dollar", "2", ROBINHOOD, USDG), authorization)
    assert Account.recover_message(version_1, signature=signature) == payer
    assert Account.recover_message(version_2, signature=signature) != payer
    assert body["paymentRequirements"]["asset"] == USDG
    assert body["paymentRequirements"]["network"] == "eip155:4663"
