"""The public API of ``uvd_x402_sdk.escrow_signing``: what Execution Market and
Karmakadabra import once they delete their copies of the escrow signature.

Pinned here, so a change that would break one of those imports turns red in
this repo before it reaches theirs:

- ``__all__`` exactly, every name in it defined, and the top-level re-exports
  being the same objects;
- the parameters of every public function (name, kind, default), and the
  positional order Execution Market's copy was called with;
- the constants that enter the signature: the ``PaymentInfo`` struct, its
  ABI tuple and its typehash, equal to ``advanced_escrow``'s;
- one test per chain of ``VERIFIED_USDC_DOMAINS``, through the public names
  only, with a throwaway key from ``Account.create()``: the signature recovers
  to the payer under that chain's domain, and the other spelling of USDC is
  refused;
- the typehash guard taken from Karmakadabra's copy, and its mutation.
"""

from __future__ import annotations

import inspect
import json
import re
from typing import Any

import pytest
from eth_account import Account
from eth_account.messages import encode_typed_data
from eth_utils import keccak

import uvd_x402_sdk
import uvd_x402_sdk.escrow_signing as es
from tests.test_escrow_vectors import mutant
from uvd_x402_sdk.advanced_escrow import ESCROW_CONTRACTS
from uvd_x402_sdk.advanced_escrow import PAYMENT_INFO_TYPEHASH as ADVANCED_TYPEHASH
from uvd_x402_sdk.escrow_signing import (
    ESCROW_PAYMENT_INFO_TYPEHASH,
    PAYMENT_INFO_ABI,
    PAYMENT_INFO_TYPE,
    RECEIVE_WITH_AUTHORIZATION_TYPES,
    VERIFIED_USDC_DOMAINS,
    build_escrow_pre_auth,
    compute_escrow_nonce,
)
from uvd_x402_sdk.networks import get_network_by_chain_id
from uvd_x402_sdk.wallet import EnvKeyAdapter

PUBLIC = [
    "build_escrow_pre_auth",
    "compute_escrow_nonce",
    "build_lifecycle_typed_data",
    "build_lifecycle_auth",
    "lifecycle_auth_from_signature",
    "RECEIVE_WITH_AUTHORIZATION_TYPES",
    "PAYMENT_INFO_TYPE",
    "PAYMENT_INFO_ABI",
    "ESCROW_PAYMENT_INFO_TYPEHASH",
    "LIFECYCLE_ACTIONS",
    "LIFECYCLE_DEFAULT_DEADLINE_SECS",
    "LIFECYCLE_DOMAIN_NAME",
    "LIFECYCLE_DOMAIN_VERSION",
    "LIFECYCLE_MAX_DEADLINE_SECS",
    "LIFECYCLE_ORDER_TYPES",
    "LIFECYCLE_PRIMARY_TYPE",
    "VERIFIED_USDC_DOMAINS",
    "REQUIRED_NETWORK_KEYS",
    "ESCROW_DEPOSIT_LIMIT_USD",
    "OPERATOR_FEE_BPS",
    "DEFAULT_MIN_FEE_BPS",
    "DEFAULT_MAX_FEE_BPS",
    "USDC_DECIMALS",
    "ESCROW_TIER_WINDOWS",
    "REVIEW_WINDOW_SEC",
    "REFUND_WINDOW_SEC",
]

TOP_LEVEL = [
    "build_escrow_pre_auth",
    "compute_escrow_nonce",
    "build_lifecycle_typed_data",
    "build_lifecycle_auth",
    "lifecycle_auth_from_signature",
    "ESCROW_PAYMENT_INFO_TYPEHASH",
    "RECEIVE_WITH_AUTHORIZATION_TYPES",
    "VERIFIED_USDC_DOMAINS",
    "LIFECYCLE_ACTIONS",
    "LIFECYCLE_DEFAULT_DEADLINE_SECS",
    "LIFECYCLE_DOMAIN_NAME",
    "LIFECYCLE_DOMAIN_VERSION",
    "LIFECYCLE_MAX_DEADLINE_SECS",
    "LIFECYCLE_ORDER_TYPES",
    "LIFECYCLE_PRIMARY_TYPE",
]

_E = inspect.Parameter.empty
_POS = inspect.Parameter.POSITIONAL_OR_KEYWORD

# (name, kind, default) per parameter, in order.
SIGNATURES: dict[str, list[tuple[str, Any, Any]]] = {
    "build_escrow_pre_auth": [
        ("payment_config", _POS, _E),
        ("network", _POS, _E),
        ("payer", _POS, _E),
        ("receiver", _POS, _E),
        ("amount_usd", _POS, _E),
        ("deadline", _POS, _E),
        ("wallet", _POS, _E),
        ("tier", _POS, "micro"),
        ("delegation_resolver", _POS, None),
    ],
    "compute_escrow_nonce": [
        ("chain_id", _POS, _E),
        ("escrow_address", _POS, _E),
        ("payment_info_typehash", _POS, _E),
        ("payment_info", _POS, _E),
    ],
    "build_lifecycle_typed_data": [
        ("action", _POS, _E),
        ("payment_info", _POS, _E),
        ("payer", _POS, _E),
        ("amount", _POS, _E),
        ("chain_id", _POS, _E),
        ("deadline", _POS, _E),
        ("nonce", _POS, _E),
    ],
    "build_lifecycle_auth": [
        ("action", _POS, _E),
        ("payment_info", _POS, _E),
        ("payer", _POS, _E),
        ("amount", _POS, _E),
        ("chain_id", _POS, _E),
        ("wallet", _POS, _E),
        ("deadline", _POS, None),
        ("nonce", _POS, None),
        ("now", _POS, None),
    ],
    "lifecycle_auth_from_signature": [
        ("typed_data", _POS, _E),
        ("signature", _POS, _E),
        ("signer", _POS, _E),
    ],
}


# ── the list, the names, the signatures ──────────────────────────────────────


def test_all_is_the_pinned_public_surface():
    assert es.__all__ == PUBLIC


@pytest.mark.parametrize("name", PUBLIC)
def test_every_public_name_is_defined(name):
    assert hasattr(es, name)


@pytest.mark.parametrize("name", TOP_LEVEL)
def test_the_top_level_export_is_the_same_object(name):
    assert name in uvd_x402_sdk.__all__
    assert getattr(uvd_x402_sdk, name) is getattr(es, name)


@pytest.mark.parametrize("name", sorted(SIGNATURES))
def test_the_parameters_do_not_move(name):
    params = inspect.signature(getattr(es, name)).parameters.values()
    assert [(p.name, p.kind, p.default) for p in params] == SIGNATURES[name]


@pytest.mark.parametrize("name", sorted(SIGNATURES))
def test_every_public_function_documents_itself(name):
    doc = inspect.getdoc(getattr(es, name)) or ""
    assert len(doc.splitlines()) > 3


def test_the_module_says_what_is_stable():
    assert "Public API (stable)" in (es.__doc__ or "")


def test_execution_markets_positional_call_still_binds():
    """em_plugin_sdk called its copy with these eight, in this order."""
    sig = inspect.signature(build_escrow_pre_auth)
    bound = sig.bind("cfg", "base", "0xpayer", "0xreceiver", "0.10", None, "wallet", "micro")
    assert list(bound.arguments) == [
        "payment_config",
        "network",
        "payer",
        "receiver",
        "amount_usd",
        "deadline",
        "wallet",
        "tier",
    ]


def test_the_private_names_karmakadabra_reads_are_kept():
    assert es._PAYMENT_INFO_ABI is PAYMENT_INFO_ABI
    assert es._REQUIRED_NETWORK_KEYS is es.REQUIRED_NETWORK_KEYS


# ── the struct the nonce hashes ─────────────────────────────────────────────


def test_the_typehash_is_the_keccak_of_the_struct_and_advanced_escrows():
    assert ESCROW_PAYMENT_INFO_TYPEHASH == "0x" + keccak(text=PAYMENT_INFO_TYPE).hex().removeprefix(
        "0x"
    )
    assert ESCROW_PAYMENT_INFO_TYPEHASH == "0x" + ADVANCED_TYPEHASH.hex()


def test_the_abi_tuple_is_the_types_of_the_struct():
    fields = re.fullmatch(r"PaymentInfo\((.*)\)", PAYMENT_INFO_TYPE).group(1).split(",")
    assert PAYMENT_INFO_ABI == "(" + ",".join(f.split(" ")[0] for f in fields) + ")"


def test_the_signed_type_is_receive_with_authorization_in_eip3009_order():
    assert RECEIVE_WITH_AUTHORIZATION_TYPES == {
        "ReceiveWithAuthorization": [
            {"name": "from", "type": "address"},
            {"name": "to", "type": "address"},
            {"name": "value", "type": "uint256"},
            {"name": "validAfter", "type": "uint256"},
            {"name": "validBefore", "type": "uint256"},
            {"name": "nonce", "type": "bytes32"},
        ]
    }


# ── one test per network ─────────────────────────────────────────────────────


def _config(chain_id: int, domain: tuple[str, str] | None = None, typehash: Any = None) -> dict:
    """A payment config for one chain, from the SDK's own tables and public names."""
    network = get_network_by_chain_id(chain_id)
    contracts = ESCROW_CONTRACTS[chain_id]
    name, version = domain or VERIFIED_USDC_DOMAINS[chain_id]
    return {
        "escrow": {
            "payment_info_typehash": typehash or ESCROW_PAYMENT_INFO_TYPEHASH,
            "networks": {
                network.name: {
                    "chain_id": chain_id,
                    "operator": "0x" + format(chain_id, "040x"),
                    "escrow": contracts["escrow"],
                    "token_collector": contracts["token_collector"],
                    "usdc": contracts["usdc"],
                    "usdc_domain_name": name,
                    "usdc_domain_version": version,
                }
            },
        }
    }


class _Wallet:
    def __init__(self) -> None:
        self.inner = EnvKeyAdapter(private_key="0x" + Account.create().key.hex().removeprefix("0x"))
        self.signed: list[dict] = []

    def get_address(self) -> str:
        return self.inner.get_address()

    def sign_typed_data(self, typed: dict) -> Any:
        self.signed.append(typed)
        return self.inner.sign_typed_data(typed)


WORKER = "0x" + "5e" * 20


def _build(chain_id: int, wallet: _Wallet, **config: Any) -> dict:
    network = get_network_by_chain_id(chain_id).name
    header = build_escrow_pre_auth(
        _config(chain_id, **config), network, wallet.get_address(), WORKER, "0.25", None, wallet
    )
    return json.loads(header)


@pytest.mark.parametrize(
    "chain_id",
    sorted(VERIFIED_USDC_DOMAINS),
    ids=[get_network_by_chain_id(c).name for c in sorted(VERIFIED_USDC_DOMAINS)],
)
def test_the_public_api_signs_for_the_payer_under_that_chains_domain(chain_id):
    wallet = _Wallet()
    wrapper = _build(chain_id, wallet)
    contracts = ESCROW_CONTRACTS[chain_id]
    authorization = wrapper["payload"]["authorization"]
    name, version = VERIFIED_USDC_DOMAINS[chain_id]

    assert wrapper["paymentRequirements"]["network"] == f"eip155:{chain_id}"
    assert authorization["to"] == contracts["token_collector"]
    assert authorization["value"] == "250000"
    assert authorization["nonce"] == compute_escrow_nonce(
        chain_id,
        contracts["escrow"],
        ESCROW_PAYMENT_INFO_TYPEHASH,
        wrapper["payload"]["paymentInfo"],
    )
    signable = encode_typed_data(
        domain_data={
            "name": name,
            "version": version,
            "chainId": chain_id,
            "verifyingContract": contracts["usdc"],
        },
        message_types=RECEIVE_WITH_AUTHORIZATION_TYPES,
        message_data={
            "from": authorization["from"],
            "to": authorization["to"],
            "value": int(authorization["value"]),
            "validAfter": int(authorization["validAfter"]),
            "validBefore": int(authorization["validBefore"]),
            "nonce": bytes.fromhex(authorization["nonce"][2:]),
        },
    )
    signature = wrapper["payload"]["signature"]
    assert Account.recover_message(signable, signature=signature) == wallet.get_address()


@pytest.mark.parametrize(
    "chain_id",
    sorted(VERIFIED_USDC_DOMAINS),
    ids=[get_network_by_chain_id(c).name for c in sorted(VERIFIED_USDC_DOMAINS)],
)
def test_the_other_spelling_of_usdc_is_refused_on_that_chain(chain_id):
    name, version = VERIFIED_USDC_DOMAINS[chain_id]
    other = "USDC" if name == "USD Coin" else "USD Coin"
    wallet = _Wallet()
    with pytest.raises(ValueError, match="EIP-712 domain mismatch"):
        _build(chain_id, wallet, domain=(other, version))
    assert wallet.signed == []


@pytest.mark.parametrize("chain_id", sorted(VERIFIED_USDC_DOMAINS))
def test_the_verified_domain_is_the_network_registrys(chain_id):
    network = get_network_by_chain_id(chain_id)
    assert VERIFIED_USDC_DOMAINS[chain_id] == (
        network.usdc_domain_name,
        network.usdc_domain_version,
    )


# ── the typehash guard (from Karmakadabra's copy) ───────────────────────────

def _next_hex(digit: str) -> str:
    return format((int(digit, 16) + 1) % 16, "x")


_CANONICAL_HEX = ESCROW_PAYMENT_INFO_TYPEHASH.removeprefix("0x")

WRONG_TYPEHASHES = [
    "0x" + "ab" * 32,
    # One field renamed: the typehash of another struct.
    "0x" + keccak(text=PAYMENT_INFO_TYPE.replace("salt", "nonce")).hex().removeprefix("0x"),
    # One hex digit off at each end: a comparison of a prefix, or of a suffix,
    # lets one of these through.
    "0x" + _CANONICAL_HEX[:-1] + _next_hex(_CANONICAL_HEX[-1]),
    "0x" + _next_hex(_CANONICAL_HEX[0]) + _CANONICAL_HEX[1:],
]


@pytest.mark.parametrize(
    "typehash", WRONG_TYPEHASHES, ids=["filler", "other-struct", "last-digit", "first-digit"]
)
def test_another_typehash_is_refused_before_signing(typehash):
    wallet = _Wallet()
    with pytest.raises(ValueError, match="payment_info_typehash"):
        _build(8453, wallet, typehash=typehash)
    assert wallet.signed == []


@pytest.mark.parametrize(
    "spelling",
    [
        ESCROW_PAYMENT_INFO_TYPEHASH,
        ESCROW_PAYMENT_INFO_TYPEHASH.removeprefix("0x"),
        "0x" + ESCROW_PAYMENT_INFO_TYPEHASH[2:].upper(),
        "0X" + ESCROW_PAYMENT_INFO_TYPEHASH[2:],
        " \t" + ESCROW_PAYMENT_INFO_TYPEHASH + " \n",
    ],
    ids=["0x", "bare", "upper-hex", "0X", "surrounding-whitespace"],
)
def test_the_canonical_typehash_is_accepted_as_the_marketplace_writes_it(spelling):
    wallet = _Wallet()
    wrapper = _build(8453, wallet, typehash=spelling)
    assert len(wallet.signed) == 1
    assert wrapper["payload"]["authorization"]["nonce"] == compute_escrow_nonce(
        8453,
        ESCROW_CONTRACTS[8453]["escrow"],
        ESCROW_PAYMENT_INFO_TYPEHASH,
        wrapper["payload"]["paymentInfo"],
    )


def test_without_the_guard_another_typehash_is_signed():
    """The mutation: the guard removed, the filler typehash gets a signature."""
    module = mutant(
        "    if str(typehash).strip().lower().removeprefix(\"0x\") != (",
        "    if False and str(typehash).strip().lower().removeprefix(\"0x\") != (",
    )
    wallet = _Wallet()
    header = module.build_escrow_pre_auth(
        _config(8453, typehash=WRONG_TYPEHASHES[0]),
        "base",
        wallet.get_address(),
        WORKER,
        "0.25",
        None,
        wallet,
    )
    assert len(wallet.signed) == 1
    assert json.loads(header)["payload"]["signature"]
