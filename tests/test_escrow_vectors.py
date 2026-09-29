"""The escrow EIP-3009 signature, pinned byte for byte against three recordings.

The same signature (``ReceiveWithAuthorization`` over the
``AuthCaptureEscrow.getHash`` nonce) lived in three copies: this SDK's
``escrow_signing``, Execution Market's ``em_plugin_sdk.escrow_signing`` and
Karmakadabra's ``agents_sdk.escrow_signing``. Each copy pinned itself with its
own vectors. This file replays all of them against the SDK, so a copy can be
deleted in favour of an import only if the SDK signs what it signed:

- ``tests/fixtures/escrow-preauth.json``: byte-identical copy of Execution
  Market's ``shared/test-vectors/escrow-preauth.json`` (dashboard, em-mobile
  and em-plugin-sdk). Base is replayed in ``tests/test_escrow_signing.py``;
  here, its ``additional_networks`` (Arc). Re-copy from there, never edit.
- ``tests/fixtures/escrow-golden-vectors-kk.json``: byte-identical copy of
  Karmakadabra's ``tests/sdk/fixtures/escrow_golden_vectors.json`` (ten
  chains). Signed there with the repeat-0x11 throwaway key, which is built at
  runtime here as there, never written as a literal. Re-copy, never edit.
- ``tests/fixtures/escrow-preauth-networks.json``: this SDK's own, recorded by
  ``scripts/escrow_preauth_vectors.py`` before any of this module changed:
  one vector per chain of ``VERIFIED_USDC_DOMAINS`` and variants on Base. It
  pins the whole ``X-Payment-Auth`` header (sha256 of its bytes), the nonce,
  the EIP-712 digest and the signature. Never edited by hand.

``TestMutations`` loads mutated copies of the module's source (the EIP-712
domain, the order of the signed fields, the chain id) and checks that exactly
the vectors of the chains each mutation touches stop reproducing.

Hex values of 32 bytes or more are stored without ``0x`` in the fixtures;
``_hydrate`` re-prefixes them.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import re
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any
from unittest import mock

import pytest
from eth_account import Account
from eth_account.messages import encode_typed_data
from eth_utils import keccak

import uvd_x402_sdk.escrow_signing as es
from uvd_x402_sdk.advanced_escrow import ESCROW_CONTRACTS
from uvd_x402_sdk.advanced_escrow import PAYMENT_INFO_TYPEHASH as ADVANCED_TYPEHASH
from uvd_x402_sdk.wallet import EnvKeyAdapter

FIXTURES = Path(__file__).resolve().parent / "fixtures"
_LONG_HEX = re.compile(r"^[0-9a-f]{64,}$")


def _hydrate(value: Any) -> Any:
    if isinstance(value, str) and _LONG_HEX.fullmatch(value):
        return "0x" + value
    if isinstance(value, list):
        return [_hydrate(v) for v in value]
    if isinstance(value, dict):
        return {k: _hydrate(v) for k, v in value.items()}
    return value


def _load(name: str) -> dict[str, Any]:
    return _hydrate(json.loads((FIXTURES / name).read_text(encoding="utf-8")))


EM = _load("escrow-preauth.json")
KK = _load("escrow-golden-vectors-kk.json")
SDK = _load("escrow-preauth-networks.json")

SDK_VECTORS = {v["id"]: v for v in SDK["vectors"]}
SDK_NETWORK_VECTORS = {i: v for i, v in SDK_VECTORS.items() if "/" not in i}
KK_KEY = "0x" + "11" * 32  # Karmakadabra's throwaway, built at runtime as there


class _Recording:
    """EnvKeyAdapter that keeps every typed data it was asked to sign."""

    def __init__(self, key: str) -> None:
        self.inner = EnvKeyAdapter(private_key=key)
        self.typed: list[dict[str, Any]] = []

    def get_address(self) -> str:
        return self.inner.get_address()

    def sign_message(self, message: Any) -> Any:
        return self.inner.sign_message(message)

    def sign_typed_data(self, typed: dict[str, Any]) -> Any:
        self.typed.append(typed)
        return self.inner.sign_typed_data(typed)


def _frozen_build(module: ModuleType, now: int, salt: str, *args: Any, **kwargs: Any) -> str:
    salt_hex = salt.removeprefix("0x")
    with (
        mock.patch.object(module, "time", SimpleNamespace(time=lambda: now)),
        mock.patch.object(
            module, "secrets", SimpleNamespace(token_hex=lambda n=32: salt_hex[: 2 * n])
        ),
    ):
        return module.build_escrow_pre_auth(*args, **kwargs)


def _receive_digest(block: dict[str, Any], authorization: dict[str, Any]) -> bytes:
    """EIP-712 digest of the ReceiveWithAuthorization on the wire, eth-account only."""
    signable = encode_typed_data(
        domain_data={
            "name": block["usdc_domain_name"],
            "version": block["usdc_domain_version"],
            "chainId": int(block["chain_id"]),
            "verifyingContract": block["usdc"],
        },
        message_types={
            "ReceiveWithAuthorization": [
                {"name": "from", "type": "address"},
                {"name": "to", "type": "address"},
                {"name": "value", "type": "uint256"},
                {"name": "validAfter", "type": "uint256"},
                {"name": "validBefore", "type": "uint256"},
                {"name": "nonce", "type": "bytes32"},
            ]
        },
        message_data={
            "from": authorization["from"],
            "to": authorization["to"],
            "value": int(authorization["value"]),
            "validAfter": int(authorization["validAfter"]),
            "validBefore": int(authorization["validBefore"]),
            "nonce": bytes.fromhex(authorization["nonce"].removeprefix("0x")),
        },
    )
    return keccak(b"\x19" + signable.version + signable.header + signable.body)


def _sha256(header: str) -> str:
    """sha256 of the header's bytes, 0x-prefixed as ``_hydrate`` reads it."""
    return "0x" + hashlib.sha256(header.encode("utf-8")).hexdigest()


# ── the SDK's own vectors ────────────────────────────────────────────────────


def _sdk_config(vector: dict[str, Any]) -> dict[str, Any]:
    frozen = SDK["frozen"]
    block = dict(vector["network_config"])
    if vector.get("lowercase_inputs"):
        block = {
            k: (v.lower() if isinstance(v, str) and v.startswith("0x") else v)
            for k, v in block.items()
        }
    return {
        "escrow": {
            "payment_info_typehash": frozen["payment_info_typehash"],
            "min_fee_bps": frozen["min_fee_bps"],
            "max_fee_bps": frozen["max_fee_bps"],
            "deposit_limit_usd": frozen["deposit_limit_usd"],
            "networks": {vector["network"]: block},
        }
    }


def build_sdk_vector(
    vector: dict[str, Any], module: ModuleType = es, config: dict[str, Any] | None = None
) -> tuple[str, _Recording]:
    wallet = _Recording(SDK["signer_private_key"])
    payer, receiver = wallet.get_address(), SDK["frozen"]["receiver"]
    if vector.get("lowercase_inputs"):
        payer, receiver = payer.lower(), receiver.lower()
    target = vector.get("delegate")
    header = _frozen_build(
        module,
        SDK["frozen"]["now"],
        SDK["frozen"]["salt"],
        config or _sdk_config(vector),
        vector["network"],
        payer,
        receiver,
        vector["amount_usd"],
        vector["deadline"],
        wallet,
        tier=vector["tier"],
        delegation_resolver=(lambda address, network: target) if target else None,
    )
    return header, wallet


def sdk_vector_reproduces(vector: dict[str, Any], module: ModuleType = es) -> bool:
    try:
        header, _ = build_sdk_vector(vector, module)
    except ValueError:
        return False
    return _sha256(header) == vector["expected"]["header_sha256"]


class TestSdkVectors:
    def test_one_vector_per_chain_with_a_verified_domain(self):
        chains = {v["network_config"]["chain_id"] for v in SDK_NETWORK_VECTORS.values()}
        assert chains == set(es.VERIFIED_USDC_DOMAINS)

    @pytest.mark.parametrize("network", sorted(SDK_NETWORK_VECTORS))
    def test_the_vector_is_built_from_the_sdk_tables(self, network):
        block = SDK_NETWORK_VECTORS[network]["network_config"]
        contracts = ESCROW_CONTRACTS[block["chain_id"]]
        assert (block["escrow"], block["token_collector"], block["usdc"]) == (
            contracts["escrow"],
            contracts["token_collector"],
            contracts["usdc"],
        )
        assert (block["usdc_domain_name"], block["usdc_domain_version"]) == (
            es.VERIFIED_USDC_DOMAINS[block["chain_id"]]
        )

    @pytest.mark.parametrize("vector_id", sorted(SDK_VECTORS))
    def test_the_header_is_the_recorded_one_byte_for_byte(self, vector_id):
        vector = SDK_VECTORS[vector_id]
        header, _ = build_sdk_vector(vector)
        assert json.loads(header) == vector["expected"]["wrapper"]
        assert _sha256(header) == vector["expected"]["header_sha256"]

    @pytest.mark.parametrize("vector_id", sorted(SDK_VECTORS))
    def test_the_nonce_is_the_escrow_get_hash(self, vector_id):
        vector = SDK_VECTORS[vector_id]
        block = vector["network_config"]
        payment_info = vector["expected"]["wrapper"]["payload"]["paymentInfo"]
        nonce = es.compute_escrow_nonce(
            block["chain_id"], block["escrow"], SDK["frozen"]["payment_info_typehash"], payment_info
        )
        assert nonce == vector["expected"]["nonce"]
        assert vector["expected"]["wrapper"]["payload"]["authorization"]["nonce"] == nonce

    @pytest.mark.parametrize("vector_id", sorted(SDK_VECTORS))
    def test_the_signed_digest_is_the_recorded_one(self, vector_id):
        vector = SDK_VECTORS[vector_id]
        _, wallet = build_sdk_vector(vector)
        authorization = vector["expected"]["wrapper"]["payload"]["authorization"]
        digest = _receive_digest(vector["network_config"], authorization)
        assert "0x" + digest.hex() == vector["expected"]["digest"]
        signed = wallet.typed[0]
        if signed["primaryType"] == "ReceiveWithAuthorization":
            # What the wallet was handed hashes to the same digest.
            signable = encode_typed_data(
                domain_data=signed["domain"],
                message_types=signed["types"],
                message_data=signed["message"],
            )
            assert keccak(b"\x19" + signable.version + signable.header + signable.body) == digest

    @pytest.mark.parametrize(
        "vector_id",
        sorted(i for i, v in SDK_VECTORS.items() if v.get("delegate") is None or "plain" in i),
    )
    def test_the_plain_signature_recovers_to_the_signer_over_that_digest(self, vector_id):
        """Integrity of the fixture itself, without the SDK."""
        vector = SDK_VECTORS[vector_id]
        authorization = vector["expected"]["wrapper"]["payload"]["authorization"]
        digest = _receive_digest(vector["network_config"], authorization)
        signature = bytes.fromhex(vector["expected"]["signature"].removeprefix("0x"))
        assert Account._recover_hash(digest, signature=signature) == SDK["signer_address"]

    def test_the_variants_that_should_not_move_the_bytes_do_not(self):
        base = SDK_VECTORS["base"]["expected"]["header_sha256"]
        assert SDK_VECTORS["base/lowercase-inputs"]["expected"]["header_sha256"] == base
        assert SDK_VECTORS["base/delegated-plain-1271"]["expected"]["header_sha256"] == base

    def test_float_noise_is_signed_as_the_settle_rounds_it(self):
        value = SDK_VECTORS["base/float-noise"]["expected"]["wrapper"]["payload"]["authorization"][
            "value"
        ]
        assert SDK_VECTORS["base/float-noise"]["amount_usd"] == 0.3 - 0.1
        assert value == "200000"

    def test_the_sma_dialect_wraps_the_plain_signature(self):
        signature = SDK_VECTORS["base/delegated-sma"]["expected"]["signature"]
        assert len(bytes.fromhex(signature.removeprefix("0x"))) == 7 + 65
        assert signature != SDK_VECTORS["base"]["expected"]["signature"]

    def test_every_chain_signs_a_different_nonce_and_signature(self):
        nonces = {v["expected"]["nonce"] for v in SDK_NETWORK_VECTORS.values()}
        signatures = {v["expected"]["signature"] for v in SDK_NETWORK_VECTORS.values()}
        assert len(nonces) == len(signatures) == len(SDK_NETWORK_VECTORS)


# ── Karmakadabra's vectors ───────────────────────────────────────────────────


def build_kk_vector(name: str, module: ModuleType = es) -> dict[str, Any]:
    vector = KK["vectors"][name]
    params = KK["generation_params"]
    config = {
        "escrow": {
            "payment_info_typehash": KK["payment_info_typehash"],
            "networks": {
                name: {
                    "chain_id": vector["chain_id"],
                    "operator": vector["paymentInfo"]["operator"],
                    "escrow": vector["escrow"],
                    "token_collector": vector["token_collector"],
                    "usdc": vector["usdc"],
                    "usdc_domain_name": vector["domain"]["name"],
                    "usdc_domain_version": vector["domain"]["version"],
                }
            },
        }
    }
    header = _frozen_build(
        module,
        params["frozen_epoch"],
        params["frozen_salt"],
        config,
        name,
        params["signer"],
        params["receiver"],
        params["amount_usd"],
        params["deadline"],
        EnvKeyAdapter(private_key=KK_KEY),
        tier=params["tier"],
    )
    return json.loads(header)


def kk_vector_reproduces(name: str, module: ModuleType = es) -> bool:
    vector = KK["vectors"][name]
    try:
        wrapper = build_kk_vector(name, module)
    except ValueError:
        return False
    return (
        wrapper["payload"]["authorization"]["nonce"] == vector["expected_nonce"]
        and wrapper["payload"]["signature"] == vector["expected_signature"]
        and wrapper["payload"]["paymentInfo"] == vector["paymentInfo"]
        and wrapper["paymentRequirements"]["network"] == f"eip155:{vector['chain_id']}"
    )


class TestKarmakadabraVectors:
    def test_the_throwaway_key_is_the_one_that_signed_them(self):
        assert Account.from_key(KK_KEY).address == KK["generation_params"]["signer"]

    @pytest.mark.parametrize("network", sorted(KK["vectors"]))
    def test_the_sdk_signs_what_karmakadabra_pinned(self, network):
        vector = KK["vectors"][network]
        wrapper = build_kk_vector(network)
        assert wrapper["payload"]["authorization"]["nonce"] == vector["expected_nonce"]
        assert wrapper["payload"]["signature"] == vector["expected_signature"]
        assert wrapper["payload"]["paymentInfo"] == vector["paymentInfo"]
        assert wrapper["paymentRequirements"]["network"] == f"eip155:{vector['chain_id']}"

    @pytest.mark.parametrize("network", sorted(KK["vectors"]))
    def test_its_domain_is_the_one_the_sdk_verified(self, network):
        vector = KK["vectors"][network]
        assert es.VERIFIED_USDC_DOMAINS[vector["chain_id"]] == (
            vector["domain"]["name"],
            vector["domain"]["version"],
        )

    def test_its_typehash_is_the_canonical_one(self):
        assert KK["payment_info_typehash"] == "0x" + ADVANCED_TYPEHASH.hex()


# ── Execution Market's additional networks ──────────────────────────────────


@pytest.mark.parametrize("network", sorted(EM["additional_networks"]))
def test_the_sdk_signs_what_execution_market_pinned_on(network):
    extra = EM["additional_networks"][network]
    frozen = EM["frozen_build"]
    block = dict(extra["network_config"])
    config = {
        "escrow": {
            "payment_info_typehash": block.pop("payment_info_typehash"),
            "min_fee_bps": block.pop("min_fee_bps"),
            "max_fee_bps": block.pop("max_fee_bps"),
            "deposit_limit_usd": EM["deposit_limit_usd"],
            "tier_timings": EM["escrow_tier_windows"],
            "networks": {extra["network"]: block},
        }
    }
    wallet = _Recording(frozen["signer_private_key"])
    header = _frozen_build(
        es,
        frozen["now"],
        frozen["salt"],
        config,
        extra["network"],
        EM["payer"],
        EM["worker"],
        EM["bounty_usd"],
        frozen["deadline"],
        wallet,
        tier=frozen["tier"],
    )
    expected = extra["frozen_build"]
    assert json.loads(header) == expected["expected_wrapper"]
    [typed] = wallet.typed
    assert typed["domain"] == expected["expected_typed_data"]["domain"]
    assert typed["primaryType"] == expected["expected_typed_data"]["primaryType"]


# ── the struct the nonce hashes ─────────────────────────────────────────────

_PAYMENT_INFO_FIELDS = (
    ("address", "operator"),
    ("address", "payer"),
    ("address", "receiver"),
    ("address", "token"),
    ("uint120", "maxAmount"),
    ("uint48", "preApprovalExpiry"),
    ("uint48", "authorizationExpiry"),
    ("uint48", "refundExpiry"),
    ("uint16", "minFeeBps"),
    ("uint16", "maxFeeBps"),
    ("address", "feeReceiver"),
    ("uint256", "salt"),
)


def test_the_typehash_is_the_keccak_of_the_payment_info_struct():
    preimage = "PaymentInfo(" + ",".join(f"{t} {n}" for t, n in _PAYMENT_INFO_FIELDS) + ")"
    assert keccak(text=preimage) == ADVANCED_TYPEHASH


def test_the_abi_tuple_encodes_exactly_the_struct_fields():
    assert es._PAYMENT_INFO_ABI == "(" + ",".join(t for t, _ in _PAYMENT_INFO_FIELDS) + ")"


def test_the_nonce_commits_to_the_chain_and_to_the_escrow():
    payment_info = SDK_VECTORS["base"]["expected"]["wrapper"]["payload"]["paymentInfo"]
    typehash = SDK["frozen"]["payment_info_typehash"]
    escrow = SDK_VECTORS["base"]["network_config"]["escrow"]
    on_base = es.compute_escrow_nonce(8453, escrow, typehash, payment_info)
    assert es.compute_escrow_nonce(10, escrow, typehash, payment_info) != on_base
    assert es.compute_escrow_nonce(8453, "0x" + "00" * 19 + "01", typehash, payment_info) != on_base


# ── mutations: each one must make exactly the vectors it touches fail ────────

_SOURCE = Path(es.__file__).read_text(encoding="utf-8")


def mutant(old: str, new: str) -> ModuleType:
    """A copy of ``escrow_signing`` with ``old`` replaced by ``new`` (exactly once)."""
    assert _SOURCE.count(old) == 1, f"mutation site not found exactly once: {old!r}"
    spec = importlib.util.spec_from_loader("uvd_x402_sdk._escrow_signing_mutant", loader=None)
    module = importlib.util.module_from_spec(spec)
    module.__package__ = "uvd_x402_sdk"
    module.__file__ = es.__file__
    exec(compile(_SOURCE.replace(old, new), es.__file__, "exec"), module.__dict__)
    return module


def _failing(module: ModuleType) -> tuple[set[str], set[str]]:
    sdk = {i for i, v in SDK_VECTORS.items() if not sdk_vector_reproduces(v, module)}
    kk = {n for n in KK["vectors"] if not kk_vector_reproduces(n, module)}
    return sdk, kk


ALL_SDK = set(SDK_VECTORS)
ALL_KK = set(KK["vectors"])
SDK_ON_BASE = {i for i, v in SDK_VECTORS.items() if v["network_config"]["chain_id"] == 8453}
SDK_NOT_USD_COIN = {
    i for i, v in SDK_VECTORS.items() if v["network_config"]["usdc_domain_name"] != "USD Coin"
}
KK_NOT_USD_COIN = {n for n, v in KK["vectors"].items() if v["domain"]["name"] != "USD Coin"}

_TYPES_BLOCK = (
    '        {"name": "from", "type": "address"},\n'
    '        {"name": "to", "type": "address"},\n'
    '        {"name": "value", "type": "uint256"},\n'
    '        {"name": "validAfter", "type": "uint256"},\n'
    '        {"name": "validBefore", "type": "uint256"},\n'
    '        {"name": "nonce", "type": "bytes32"},\n'
    "    ],\n"
    "}\n"
    "\n"
    "# ABI type of the paymentInfo tuple"
)


def _swap(block: str, a: str, b: str) -> str:
    return block.replace(a, "\0").replace(b, a).replace("\0", b)


MUTATIONS = {
    # The 'USD Coin' fallback: right on the chains that call USDC that, wrong elsewhere.
    "domain name hard-coded": (
        '"name": net["usdc_domain_name"],',
        '"name": "USD Coin",',
        SDK_NOT_USD_COIN,
        KK_NOT_USD_COIN,
    ),
    "domain version hard-coded": (
        '"version": net["usdc_domain_version"],',
        '"version": "1",',
        ALL_SDK,
        ALL_KK,
    ),
    "validAfter and validBefore swapped in the signed type": (
        _TYPES_BLOCK,
        _swap(
            _TYPES_BLOCK,
            '{"name": "validAfter", "type": "uint256"}',
            '{"name": "validBefore", "type": "uint256"}',
        ),
        ALL_SDK,
        ALL_KK,
    ),
    "from and to swapped in the signed type": (
        _TYPES_BLOCK,
        _swap(
            _TYPES_BLOCK, '{"name": "from", "type": "address"}', '{"name": "to", "type": "address"}'
        ),
        ALL_SDK,
        ALL_KK,
    ),
    "chainId of the domain hard-coded to Base": (
        '"chainId": int(net["chain_id"]),',
        '"chainId": 8453,',
        ALL_SDK - SDK_ON_BASE,
        ALL_KK - {"base"},
    ),
    "chain id of the nonce hard-coded to Base": (
        "[chain_id, to_checksum_address(escrow_address), pi_hash],",
        "[8453, to_checksum_address(escrow_address), pi_hash],",
        ALL_SDK - SDK_ON_BASE,
        ALL_KK - {"base"},
    ),
    "network of the requirements hard-coded to Base": (
        '"network": f"eip155:{int(net[\'chain_id\'])}",',
        '"network": "eip155:8453",',
        ALL_SDK - SDK_ON_BASE,
        ALL_KK - {"base"},
    ),
    "payer not zeroed in the nonce": (
        "ZERO_ADDRESS,  # payer = 0 for the payer-agnostic hash",
        'to_checksum_address(payment_info["receiver"]),',
        ALL_SDK,
        ALL_KK,
    ),
}


class TestMutations:
    def test_the_unmutated_source_reproduces_every_vector(self):
        assert _failing(mutant("ZERO_ADDRESS = ", "ZERO_ADDRESS = ")) == (set(), set())

    @pytest.mark.parametrize("name", sorted(MUTATIONS))
    def test_the_mutation_fails_exactly_the_vectors_it_touches(self, name):
        old, new, sdk_expected, kk_expected = MUTATIONS[name]
        sdk_failing, kk_failing = _failing(mutant(old, new))
        assert sdk_failing == sdk_expected
        assert kk_failing == kk_expected
        assert sdk_failing  # a mutation that fails nothing guards nothing

    @pytest.mark.parametrize("network", sorted(SDK_NETWORK_VECTORS))
    def test_another_chain_id_in_the_config_does_not_reproduce_the_vector(self, network):
        vector = SDK_NETWORK_VECTORS[network]
        config = _sdk_config(vector)
        block = config["escrow"]["networks"][vector["network"]]
        block["chain_id"] = block["chain_id"] + 1
        try:
            header, _ = build_sdk_vector(vector, config=config)
        except ValueError:
            return  # refused: also not the vector
        assert _sha256(header) != vector["expected"]["header_sha256"]
