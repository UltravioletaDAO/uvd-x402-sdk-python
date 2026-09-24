"""Escrow on Arc (5042) and Arc Testnet (5042002): the canonical x402r generation.

PROVENANCE of ``tests/fixtures/arc-escrow-d.json``: recorded by
``scripts/arc_escrow_record.py`` from the public Arc RPCs (reads only, one
request at a time). Real answers, never edited by hand; re-measure with
``python scripts/arc_escrow_record.py --force``. It holds the code found at every
address the SDK registers for Arc, the full code of the operator factory on
5042, ``computeAddress`` on that factory, and ``AuthCaptureEscrow.getHash`` /
``paymentState`` for two fixed ``PaymentInfo``.

Hex values of 32 bytes or more are stored WITHOUT the ``0x`` prefix (secret
scanners block a literal ``0x`` + 64 hex chars); ``_hydrate`` re-prefixes them.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from eth_abi import encode
from eth_utils import keccak, to_checksum_address

import uvd_x402_sdk.advanced_escrow as ae

FIXTURE_PATH = Path(__file__).resolve().parent / "fixtures" / "arc-escrow-d.json"
_LONG_HEX = re.compile(r"^[0-9a-f]{64,}$")


def _hydrate(value):
    if isinstance(value, str) and _LONG_HEX.fullmatch(value):
        return "0x" + value
    if isinstance(value, list):
        return [_hydrate(v) for v in value]
    if isinstance(value, dict):
        return {k: _hydrate(v) for k, v in value.items()}
    return value


FIXTURE = _hydrate(json.loads(FIXTURE_PATH.read_text(encoding="utf-8")))
ARC_CHAINS = (5042, 5042002)
UNREACHABLE_RPC = "http://127.0.0.1:9"  # never contacted
CLIENT_KEY = "0x" + "4b" * 32  # synthetic, never held funds

# The argument of computeAddress, as data.
COMPUTE_ADDRESS_ARG = (
    "0xaE07cEB6b395BC685a776a0b4c489E8d9cE9A6ad",
    "0x25cA273d6f5508f06ed186680D305DC32a997461",
    "0x0000000000000000000000000000000000000000",
    "0x0000000000000000000000000000000000000000",
    "0xf50fD76d66c80AEb216c0C5879376C980a2B62eF",
    "0x0000000000000000000000000000000000000000",
    "0xd8023a72f29Bb1AB782c69744893Dea2836cb69C",
    "0x0000000000000000000000000000000000000000",
    "0x402ef720D202cb4BCbfb3Ee6577b204cA06786B9",
    "0x0000000000000000000000000000000000000000",
    "0x0000000000000000000000000000000000000000",
    "0x0000000000000000000000000000000000000000",
)

# Earlier escrow deployments that must never appear in an Arc entry.
NOT_ON_ARC = (
    "0xF8211868187974a7Fb9d99b8fFB171AD70665Dc6",
    "0x0308703621160b894cF045E555686d99ee8bd94E",
    "0x7561DC178D9aD5bc5fb103C01f448A510d2A36D0",
    "0xD8490609d2da0ee626b0e676941b225cbc1A8C08",
    "0x15f36140bC1d444f917D306d0f5be223F55709B6",
    "0xBC151792f80C0EB1973d56b0235e6bee2A60e245",
)


def _client(chain_id: int, **kwargs) -> ae.AdvancedEscrowClient:
    return ae.AdvancedEscrowClient(
        private_key=CLIENT_KEY, chain_id=chain_id, rpc_url=UNREACHABLE_RPC, **kwargs
    )


def _decode_address(word: str) -> str:
    return to_checksum_address("0x" + word.removeprefix("0x")[-40:])


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("chain_id", ARC_CHAINS)
def test_registry_carries_the_canonical_deployment(chain_id):
    assert ae.get_escrow_contracts(chain_id) == FIXTURE["contracts"]


@pytest.mark.parametrize("chain_id", ARC_CHAINS)
def test_every_registered_address_had_code_on_that_chain(chain_id):
    recorded = FIXTURE["chains"][str(chain_id)]
    assert recorded["chain_id"] == chain_id
    for key, address in ae.get_escrow_contracts(chain_id).items():
        code = recorded["code"][key]
        assert code["address"] == address, key
        assert code["size"] > 0, f"{key} {address} had no code on {chain_id}"


def test_both_arc_chains_are_supported_and_named():
    assert ae.is_escrow_supported(5042) is True
    assert ae.is_escrow_supported(5042002) is True
    assert ae.ESCROW_CHAIN_NAMES[5042] == "Arc"
    assert ae.ESCROW_CHAIN_NAMES[5042002] == "Arc Testnet"
    assert {5042, 5042002} <= set(ae.get_supported_escrow_chains())


def test_the_same_code_lives_at_the_same_addresses_on_both_chains():
    main, test = (FIXTURE["chains"][str(c)]["code"] for c in ARC_CHAINS)
    for key in FIXTURE["contracts"]:
        assert main[key]["keccak"] == test[key]["keccak"], key


@pytest.mark.parametrize("chain_id", ARC_CHAINS)
def test_no_earlier_generation_address_in_an_arc_entry(chain_id):
    values = {a.lower() for a in ae.ESCROW_CONTRACTS[chain_id].values()}
    values.add(_client(chain_id).contracts["operator"].lower())
    for address in NOT_ON_ARC:
        assert address.lower() not in values, address


# ---------------------------------------------------------------------------
# Default operator
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("chain_id", ARC_CHAINS)
def test_compute_address_calldata_re_encodes_from_the_argument(chain_id):
    recorded = FIXTURE["chains"][str(chain_id)]["compute_address"]
    signature = "computeAddress((" + ",".join(["address"] * 12) + "))"
    data = keccak(text=signature)[:4] + encode(
        ["(" + ",".join(["address"] * 12) + ")"], [COMPUTE_ADDRESS_ARG]
    )

    assert recorded["to"] == ae.ESCROW_CONTRACTS[chain_id]["operator_factory"]
    assert recorded["data"] == "0x" + data.hex()


@pytest.mark.parametrize("chain_id", ARC_CHAINS)
def test_default_operator_is_the_address_the_factory_computed(chain_id):
    computed = _decode_address(
        FIXTURE["chains"][str(chain_id)]["compute_address"]["result"]
    )

    assert computed == FIXTURE["default_operator"]
    assert _client(chain_id).contracts["operator"] == computed


@pytest.mark.parametrize("chain_id", ARC_CHAINS)
def test_an_explicit_operator_still_wins(chain_id):
    other = "0x" + "0e" * 20
    assert _client(chain_id, operator_address=other).contracts["operator"] == other


# ---------------------------------------------------------------------------
# authorize(): the escrow nonce is what the Arc escrow computes
# ---------------------------------------------------------------------------


def _pre_auth_payment_info() -> ae.PaymentInfo:
    pi = FIXTURE["pre_auth"]["payment_info"]
    return ae.PaymentInfo(
        operator=pi["operator"],
        receiver=pi["receiver"],
        token=pi["token"],
        max_amount=int(pi["maxAmount"]),
        pre_approval_expiry=pi["preApprovalExpiry"],
        authorization_expiry=pi["authorizationExpiry"],
        refund_expiry=pi["refundExpiry"],
        min_fee_bps=pi["minFeeBps"],
        max_fee_bps=pi["maxFeeBps"],
        fee_receiver=pi["feeReceiver"],
        salt=pi["salt"],
    )


@pytest.mark.parametrize("chain_id", ARC_CHAINS)
def test_client_nonce_equals_the_escrow_get_hash(chain_id):
    recorded = FIXTURE["chains"][str(chain_id)]["pre_auth_get_hash"]

    assert recorded["to"] == ae.ESCROW_CONTRACTS[chain_id]["escrow"]
    assert _client(chain_id)._compute_nonce(_pre_auth_payment_info()) == recorded["result"]
