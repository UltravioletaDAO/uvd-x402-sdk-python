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


# ---------------------------------------------------------------------------
# Generation and operator ABI
# ---------------------------------------------------------------------------

PI_ABI = (
    "(address,address,address,address,uint120,uint48,uint48,uint48,"
    "uint16,uint16,address,uint256)"
)
FACTORY_CODE = bytes.fromhex(FIXTURE["operator_factory_code_5042"].removeprefix("0x"))
PUSH4 = bytes([0x63])


def _selector(entry: dict) -> bytes:
    def canonical(param: dict) -> str:
        if param["type"] == "tuple":
            return "(" + ",".join(canonical(c) for c in param["components"]) + ")"
        return param["type"]

    signature = entry["name"] + "(" + ",".join(canonical(p) for p in entry["inputs"]) + ")"
    return keccak(text=signature)[:4]


def test_every_escrow_chain_has_an_explicit_generation():
    assert set(ae.ESCROW_GENERATIONS) == set(ae.ESCROW_CONTRACTS)
    assert set(ae.ESCROW_GENERATIONS.values()) <= {"v1", "v2", "v3"}


def test_generations_and_the_abi_each_one_gets():
    for chain_id in ARC_CHAINS:
        assert ae.get_escrow_generation(chain_id) == "v3"
        assert ae.get_operator_abi(chain_id) is ae.OPERATOR_ABI_V3
        assert _client(chain_id).generation == "v3"
    assert ae.get_escrow_generation(1187947933) == "v2"
    assert ae.get_operator_abi(1187947933) is ae.OPERATOR_ABI_V2
    assert ae.get_escrow_generation(8453) == "v1"
    assert ae.get_operator_abi(8453) is ae.OPERATOR_ABI
    # A chain outside the registry (explicit contracts) keeps the legacy ABI.
    assert ae.get_escrow_generation(999999) == "v1"
    # Kept for importers, and derived: only the "v2" chains.
    assert ae.CREATE3_CHAIN_IDS == {1187947933}
    assert ae.CREATE3_CHAIN_IDS == {
        c for c, g in ae.ESCROW_GENERATIONS.items() if g == "v2"
    }


def test_v3_selectors_are_the_ones_measured():
    by_name = {entry["name"]: _selector(entry).hex() for entry in ae.OPERATOR_ABI_V3}
    assert by_name["capture"] == "f12b86f6"
    assert by_name["void"] == "c3c5090e"
    assert by_name["FEE_RECEIVER"] == "d3e78e4d"


@pytest.mark.parametrize("entry", ae.OPERATOR_ABI_V3, ids=lambda e: e["name"])
def test_every_v3_function_is_in_the_recorded_factory_code(entry):
    assert PUSH4 + _selector(entry) in FACTORY_CODE, entry["name"]


@pytest.mark.parametrize(
    "entry",
    [e for e in ae.OPERATOR_ABI + ae.OPERATOR_ABI_V2],
    ids=lambda e: f"{e['name']}/{len(e['inputs'])}",
)
def test_no_legacy_operator_function_is_in_that_code(entry):
    # release / refundInEscrow / refundPostEscrow / the 4-argument charge:
    # a call built from either legacy ABI would hit no function on Arc.
    assert _selector(entry) not in FACTORY_CODE, entry["name"]


def test_the_v3_error_for_nothing_to_void_is_not_the_partial_one():
    assert not issubclass(ae.EscrowNothingToVoidError, ValueError)


# ---------------------------------------------------------------------------
# v3 calls
# ---------------------------------------------------------------------------

from web3 import Web3  # noqa: E402
from web3.providers.base import BaseProvider  # noqa: E402


class _RecordedChain(BaseProvider):
    """Answers eth_call only for the given (to, data); any other RPC fails.

    A send needs eth_getTransactionCount / eth_gasPrice /
    eth_sendRawTransaction first, so a refusal that let one through fails here
    even if ``_send_tx`` were not replaced.
    """

    def __init__(self, chain_id: int, answers: dict):
        super().__init__()
        self.chain_id = chain_id
        self.answers = {(to.lower(), data.lower()): res for (to, data), res in answers.items()}
        self.methods: list[str] = []

    def make_request(self, method, params):
        self.methods.append(method)
        if method == "eth_chainId":
            result = hex(self.chain_id)
        elif method == "eth_call":
            key = (params[0]["to"].lower(), params[0]["data"].lower())
            if key not in self.answers:
                raise AssertionError(f"eth_call not recorded: {key}")
            result = self.answers[key]
        else:
            raise AssertionError(f"unexpected RPC {method}")
        return {"jsonrpc": "2.0", "id": len(self.methods), "result": result}


def _state_word(capturable: int, refundable: int = 0) -> str:
    """A paymentState answer for a test double (the recorded one is all zeros)."""
    return "0x" + encode(["bool", "uint120", "uint120"], [True, capturable, refundable]).hex()


def _arc_client(chain_id: int, payment_state: str | None = None):
    """Client on ``chain_id`` whose chain answers getHash / paymentState only.

    getHash is the answer recorded from Arc for the fixture's client
    PaymentInfo; paymentState is the recorded one unless the test gives its own.
    """
    recorded = FIXTURE["chains"][str(chain_id)]
    get_hash, state = recorded["client_get_hash"], recorded["client_payment_state"]
    chain = _RecordedChain(
        chain_id,
        {
            (get_hash["to"], get_hash["data"]): get_hash["result"],
            (state["to"], state["data"]): payment_state or state["result"],
        },
    )
    client = _client(chain_id)
    assert client.payer == FIXTURE["client"]["payer"]
    client.w3 = Web3(chain)
    sent: list[str] = []

    def record_tx(func_call):
        sent.append(func_call._encode_transaction_data())
        return ae.TransactionResult(success=True)

    client._send_tx = record_tx
    return client, chain, sent


def _client_pi() -> ae.PaymentInfo:
    return ae.PaymentInfo(**FIXTURE["client"]["payment_info"])


def _calldata(name: str, types: list, values: list) -> str:
    entry = next(e for e in ae.OPERATOR_ABI_V3 if e["name"] == name)
    return "0x" + (_selector(entry) + encode(types, values)).hex()


@pytest.mark.parametrize("chain_id", ARC_CHAINS)
@pytest.mark.parametrize("amount", [None, 2_000_000])
def test_release_is_capture_with_empty_data(chain_id, amount):
    client, _, sent = _arc_client(chain_id)
    pi = _client_pi()

    client.release(pi, amount)

    expected_amount = amount or pi.max_amount
    assert sent == [
        _calldata("capture", [PI_ABI, "uint256", "bytes"],
                  [client._build_tuple(pi), expected_amount, b""])
    ]


@pytest.mark.parametrize("chain_id", ARC_CHAINS)
def test_nothing_to_void_reads_the_real_state_and_sends_nothing(chain_id):
    client, chain, sent = _arc_client(chain_id)  # recorded paymentState: zeros

    with pytest.raises(ae.EscrowNothingToVoidError) as caught:
        client.refund_in_escrow(_client_pi())

    assert caught.value.payment_info_hash == (
        FIXTURE["chains"][str(chain_id)]["client_get_hash"]["result"]
    )
    assert sent == []
    assert set(chain.methods) <= {"eth_call", "eth_chainId"}
    assert chain.methods.count("eth_call") == 2


@pytest.mark.parametrize("chain_id", ARC_CHAINS)
@pytest.mark.parametrize("amount", [2_000_000, 6_000_000, 4_999_999])
def test_an_amount_other_than_the_capturable_one_is_refused_without_tx(chain_id, amount):
    client, chain, sent = _arc_client(chain_id, _state_word(5_000_000))

    with pytest.raises(ValueError, match="whole capturableAmount"):
        client.refund_in_escrow(_client_pi(), amount)

    assert sent == []
    assert set(chain.methods) <= {"eth_call", "eth_chainId"}


@pytest.mark.parametrize("chain_id", ARC_CHAINS)
@pytest.mark.parametrize("amount", [None, 5_000_000])
def test_the_whole_capturable_amount_is_a_void(chain_id, amount):
    client, _, sent = _arc_client(chain_id, _state_word(5_000_000))
    pi = _client_pi()

    result = client.refund_in_escrow(pi, amount)

    assert result.success
    assert sent == [
        _calldata("void", [PI_ABI, "bytes"], [client._build_tuple(pi), b""])
    ]


@pytest.mark.parametrize("chain_id", ARC_CHAINS)
def test_partial_release_then_void_of_the_rest(chain_id):
    """The partial-release flow: capture part, then void what remains."""
    pi = _client_pi()
    client, _, sent = _arc_client(chain_id, _state_word(5_000_000))
    client.release(pi, 3_000_000)

    after, _, sent_after = _arc_client(chain_id, _state_word(2_000_000, 3_000_000))
    after.refund_in_escrow(pi, 2_000_000)

    pt = client._build_tuple(pi)
    assert sent == [_calldata("capture", [PI_ABI, "uint256", "bytes"], [pt, 3_000_000, b""])]
    assert sent_after == [_calldata("void", [PI_ABI, "bytes"], [pt, b""])]


@pytest.mark.parametrize("chain_id", ARC_CHAINS)
def test_refund_post_escrow_is_refund(chain_id):
    client, _, sent = _arc_client(chain_id)
    pi = _client_pi()
    collector = "0x" + "0c" * 20

    client.refund_post_escrow(pi, 1_000_000, token_collector=collector, collector_data=b"\x01")

    assert sent == [
        _calldata(
            "refund",
            [PI_ABI, "uint256", "address", "bytes"],
            [client._build_tuple(pi), 1_000_000, to_checksum_address(collector), b"\x01"],
        )
    ]


@pytest.mark.parametrize("chain_id", ARC_CHAINS)
def test_charge_is_refused_before_signing(chain_id):
    client, chain, sent = _arc_client(chain_id)

    def must_not_sign(auth):
        raise AssertionError("charge() signed on a v3 chain")

    client._sign_erc3009 = must_not_sign
    with pytest.raises(ValueError, match="not available for the v3 operator"):
        client.charge(_client_pi())
    assert sent == [] and chain.methods == []


# ---------------------------------------------------------------------------
# Pre-auth (sign-on-assignment): build_escrow_pre_auth / compute_escrow_nonce
# ---------------------------------------------------------------------------

import logging  # noqa: E402
from unittest import mock  # noqa: E402

from eth_account import Account  # noqa: E402
from eth_account.messages import encode_typed_data  # noqa: E402

import uvd_x402_sdk.escrow_signing as es  # noqa: E402
from uvd_x402_sdk.networks import get_network_by_chain_id  # noqa: E402
from uvd_x402_sdk.wallet import EnvKeyAdapter  # noqa: E402

PRE_AUTH = FIXTURE["pre_auth"]
PRE_AUTH_KEY = "0x" + "42" * 32  # synthetic, never held funds (as recorded)


class _RecordingWallet:
    """EnvKeyAdapter that keeps the typed data it was asked to sign."""

    def __init__(self):
        self.inner = EnvKeyAdapter(private_key=PRE_AUTH_KEY)
        self.typed = []

    def get_address(self):
        return self.inner.get_address()

    def sign_message(self, message):
        return self.inner.sign_message(message)

    def sign_typed_data(self, typed_data):
        self.typed.append(typed_data)
        return self.inner.sign_typed_data(typed_data)


def _arc_payment_config(chain_id: int, domain=None) -> dict:
    """The escrow payment config for Arc, built from this SDK's own registry."""
    contracts = ae.get_escrow_contracts(chain_id)
    network = get_network_by_chain_id(chain_id)
    name, version = domain or (network.usdc_domain_name, network.usdc_domain_version)
    return {
        "escrow": {
            "payment_info_typehash": "0x" + ae.PAYMENT_INFO_TYPEHASH.hex(),
            "networks": {
                network.name: {
                    "chain_id": chain_id,
                    "operator": _client(chain_id).contracts["operator"],
                    "escrow": contracts["escrow"],
                    "token_collector": contracts["token_collector"],
                    "usdc": contracts["usdc"],
                    "usdc_domain_name": name,
                    "usdc_domain_version": version,
                }
            },
        }
    }


def _build_frozen(wallet, config, network="arc") -> dict:
    with mock.patch.object(es.time, "time", lambda: PRE_AUTH["now"]), mock.patch.object(
        es.secrets, "token_hex", lambda n=32: PRE_AUTH["salt"].removeprefix("0x")
    ):
        header = es.build_escrow_pre_auth(
            config,
            network,
            wallet.get_address(),
            PRE_AUTH["worker"],
            PRE_AUTH["bounty_usd"],
            None,
            wallet,
            tier=PRE_AUTH["tier"],
        )
    return json.loads(header)


@pytest.mark.parametrize("chain_id", ARC_CHAINS)
def test_arc_usdc_domain_is_verified(chain_id):
    network = get_network_by_chain_id(chain_id)
    assert es.VERIFIED_USDC_DOMAINS[chain_id] == ("USDC", "2")
    assert es.VERIFIED_USDC_DOMAINS[chain_id] == (
        network.usdc_domain_name,
        network.usdc_domain_version,
    )


def test_pre_auth_on_arc_signs_the_nonce_the_arc_escrow_computes(caplog):
    wallet = _RecordingWallet()

    with caplog.at_level(logging.WARNING, logger="uvd_x402_sdk.escrow_signing"):
        header = _build_frozen(wallet, _arc_payment_config(5042))

    payload = header["payload"]
    assert payload["paymentInfo"] == PRE_AUTH["payment_info"]
    assert payload["authorization"]["nonce"] == (
        FIXTURE["chains"]["5042"]["pre_auth_get_hash"]["result"]
    )
    assert payload["authorization"]["to"] == FIXTURE["contracts"]["token_collector"]
    assert header["paymentRequirements"]["network"] == "eip155:5042"
    # Verified domain: no "signing escrow on UNVERIFIED chain" warning.
    assert not [r for r in caplog.records if "UNVERIFIED" in r.getMessage()]

    [typed] = wallet.typed
    assert typed["domain"] == {
        "name": "USDC",
        "version": "2",
        "chainId": 5042,
        "verifyingContract": FIXTURE["contracts"]["usdc"],
    }
    signable = encode_typed_data(
        domain_data=typed["domain"],
        message_types=typed["types"],
        message_data=typed["message"],
    )
    assert Account.recover_message(signable, signature=payload["signature"]) == (
        wallet.get_address()
    )


@pytest.mark.parametrize("chain_id", ARC_CHAINS)
def test_compute_escrow_nonce_equals_the_escrow_get_hash(chain_id):
    nonce = es.compute_escrow_nonce(
        chain_id,
        ae.ESCROW_CONTRACTS[chain_id]["escrow"],
        "0x" + ae.PAYMENT_INFO_TYPEHASH.hex(),
        PRE_AUTH["payment_info"],
    )
    assert nonce == FIXTURE["chains"][str(chain_id)]["pre_auth_get_hash"]["result"]


def test_a_wrong_domain_for_arc_is_refused_before_signing():
    wallet = _RecordingWallet()

    with pytest.raises(ValueError, match="EIP-712 domain mismatch"):
        _build_frozen(wallet, _arc_payment_config(5042, domain=("USD Coin", "2")))
    assert wallet.typed == []
