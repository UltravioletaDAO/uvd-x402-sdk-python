"""Record the SDK's own escrow pre-auth vectors: ``tests/fixtures/escrow-preauth-networks.json``.

One vector per chain of ``escrow_signing.VERIFIED_USDC_DOMAINS`` (the chains on
which the SDK knows the USDC EIP-712 domain), plus a few variants on Base for
the paths the builder already covers: the ``standard`` tier without a deadline,
a float with noise (``0.3 - 0.1``), the deposit limit, lower-case addresses
and the two EIP-7702 dialects. Each vector pins, byte for byte, what
``build_escrow_pre_auth`` produced when it was recorded: the
``X-Payment-Auth`` header (its sha256 and its parsed JSON), the EIP-3009 nonce
(``AuthCaptureEscrow.getHash``), the EIP-712 digest that was signed and the
signature.

Offline: no RPC, no facilitator. The network configs come from this SDK's own
tables (``advanced_escrow.ESCROW_CONTRACTS`` for the escrow, the token
collector and USDC; ``VERIFIED_USDC_DOMAINS`` for the domain). The operator is
synthetic and different on every chain (the chain id written as an address,
the recipe of Karmakadabra's vectors): the live operator comes from the
marketplace's payment config, and the nonce hashes whatever operator it gets.
The builder refuses an operator other than the chain's row of
``escrow_contracts.ESCROW_OPERATORS``, so each case registers its synthetic
operator there for the build (as ``tests/test_escrow_vectors.py`` does).

The signing key is a throwaway made with ``Account.create()`` the first time
the file was written; it never held funds and is stored in the fixture (without
``0x``, like every hex value of 32 bytes or more, so no secret scanner reads it
as a live key). A re-run reuses it, so the output is deterministic
(``--check``). ``--new-key`` makes another one, which changes every signature:
only on purpose.

Recording again is NOT how a red test gets fixed. The file pins what the SDK
signs; if a change to the SDK moves one of these bytes, the change is what is
wrong, unless the point of the change was to move them.

Usage: python scripts/escrow_preauth_vectors.py [--check] [--new-key]
Exit codes: 0 ok, 1 --check found drift, 2 refused.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

from eth_account import Account
from eth_account.messages import encode_typed_data
from eth_utils import keccak, to_checksum_address

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from uvd_x402_sdk import escrow_signing as es  # noqa: E402
from uvd_x402_sdk.erc7702 import SMA_WRAP_TARGETS  # noqa: E402
from uvd_x402_sdk.escrow_contracts import ESCROW_CONTRACTS, ESCROW_OPERATORS  # noqa: E402
from uvd_x402_sdk.networks import get_network_by_chain_id  # noqa: E402
from uvd_x402_sdk.wallet import EnvKeyAdapter  # noqa: E402

OUT_PATH = REPO / "tests" / "fixtures" / "escrow-preauth-networks.json"

# Frozen inputs. Different from the Execution Market and Karmakadabra vectors on
# purpose: a third, independent recording of the same builder.
NOW = 1790000000
SALT_HEX = "c3" * 32
DEADLINE = NOW + 3 * 86400
AMOUNT_USD = "0.25"
TIER = "micro"
RECEIVER = to_checksum_address("0x" + "5e" * 20)
# Canonical typehash, written the way the marketplace serves it.
TYPEHASH = "0x" + keccak(
    text=(
        "PaymentInfo(address operator,address payer,address receiver,"
        "address token,uint120 maxAmount,uint48 preApprovalExpiry,"
        "uint48 authorizationExpiry,uint48 refundExpiry,uint16 minFeeBps,"
        "uint16 maxFeeBps,address feeReceiver,uint256 salt)"
    )
).hex().removeprefix("0x")
# A delegate that is not an Alchemy SMA (takes the plain ECDSA via ERC-1271).
PLAIN_1271_DELEGATE = to_checksum_address("0x" + "d1" * 20)

_LONG_HEX = re.compile(r"0x([0-9a-f]{64,})")
_LEAK = re.compile(r"0x[0-9a-fA-F]{64}")


def _operator_for(chain_id: int) -> str:
    return to_checksum_address("0x" + format(int(chain_id), "040x"))


def _network_block(chain_id: int) -> tuple[str, dict[str, Any]]:
    network = get_network_by_chain_id(chain_id)
    if network is None:
        raise SystemExit(f"refused: chain {chain_id} is not in the network registry")
    contracts = ESCROW_CONTRACTS.get(chain_id)
    if contracts is None:
        raise SystemExit(f"refused: chain {chain_id} has no escrow contracts in the SDK")
    name, version = es.VERIFIED_USDC_DOMAINS[chain_id]
    return network.name, {
        "chain_id": chain_id,
        "operator": _operator_for(chain_id),
        "escrow": contracts["escrow"],
        "token_collector": contracts["token_collector"],
        "usdc": contracts["usdc"],
        "usdc_domain_name": name,
        "usdc_domain_version": version,
    }


def _cases() -> list[dict[str, Any]]:
    cases = []
    for chain_id in sorted(es.VERIFIED_USDC_DOMAINS):
        network, block = _network_block(chain_id)
        cases.append(
            {
                "id": network,
                "network": network,
                "network_config": block,
                "amount_usd": AMOUNT_USD,
                "deadline": DEADLINE,
                "tier": TIER,
                "delegate": None,
            }
        )
    base_network, base_block = _network_block(8453)
    variants = [
        {
            "id": "base/standard-no-deadline",
            "tier": "standard",
            "deadline": None,
            "amount_usd": "1.5",
        },
        {"id": "base/float-noise", "amount_usd": 0.3 - 0.1},
        {"id": "base/deposit-limit", "amount_usd": "100"},
        {"id": "base/lowercase-inputs", "lowercase": True},
        {"id": "base/delegated-sma", "delegate": sorted(SMA_WRAP_TARGETS)[0]},
        {"id": "base/delegated-plain-1271", "delegate": PLAIN_1271_DELEGATE},
    ]
    for variant in variants:
        case = {
            "network": base_network,
            "network_config": dict(base_block),
            "amount_usd": AMOUNT_USD,
            "deadline": DEADLINE,
            "tier": TIER,
            "delegate": None,
        }
        case.update(variant)
        cases.append(case)
    return cases


def payment_config(case: dict[str, Any]) -> dict[str, Any]:
    """The GET /h2a/payment-config shape the builder reads, for one case."""
    block = dict(case["network_config"])
    if case.get("lowercase"):
        block = {
            k: (v.lower() if isinstance(v, str) and v.startswith("0x") else v)
            for k, v in block.items()
        }
    return {
        "escrow": {
            "payment_info_typehash": TYPEHASH,
            "min_fee_bps": 0,
            "max_fee_bps": 1800,
            "deposit_limit_usd": 100,
            "networks": {case["network"]: block},
        }
    }


def receive_digest(block: dict[str, Any], authorization: dict[str, Any]) -> str:
    """EIP-712 digest of the ReceiveWithAuthorization the header carries.

    Rebuilt from the wire (the header's ``authorization``) and the network
    block, with eth-account only: keccak(0x1901 || domainSeparator ||
    hashStruct). It does not read the typed data the SDK built.
    """
    signable = encode_typed_data(
        domain_data={
            "name": block["usdc_domain_name"],
            "version": block["usdc_domain_version"],
            "chainId": int(block["chain_id"]),
            "verifyingContract": to_checksum_address(block["usdc"]),
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
    return "0x" + keccak(
        b"\x19" + signable.version + signable.header + signable.body
    ).hex().removeprefix("0x")


class _Recording:
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


def build(case: dict[str, Any], key: str, module: Any = es) -> tuple[str, list[dict[str, Any]]]:
    """Run ``build_escrow_pre_auth`` with time and salt frozen; return header + typed data."""
    wallet = _Recording(key)
    payer = wallet.get_address()
    receiver = RECEIVER
    if case.get("lowercase"):
        payer, receiver = payer.lower(), receiver.lower()
    resolver = None
    if case.get("delegate"):
        target = case["delegate"]
        resolver = lambda address, network: target  # noqa: E731
    block = case["network_config"]
    with (
        mock.patch.object(module, "time", SimpleNamespace(time=lambda: NOW)),
        mock.patch.object(
            module, "secrets", SimpleNamespace(token_hex=lambda n=32: SALT_HEX[: 2 * n])
        ),
        mock.patch.dict(ESCROW_OPERATORS, {block["chain_id"]: block["operator"]}),
    ):
        header = module.build_escrow_pre_auth(
            payment_config(case),
            case["network"],
            payer,
            receiver,
            case["amount_usd"],
            case["deadline"],
            wallet,
            tier=case["tier"],
            delegation_resolver=resolver,
        )
    return header, wallet.typed


def _strip(value: Any) -> Any:
    """Long hex without 0x (the house rule for fixtures)."""
    if isinstance(value, str):
        m = _LONG_HEX.fullmatch(value)
        return m.group(1) if m else value
    if isinstance(value, list):
        return [_strip(v) for v in value]
    if isinstance(value, dict):
        return {k: _strip(v) for k, v in value.items()}
    return value


def record(key: str) -> dict[str, Any]:
    signer = Account.from_key(key).address
    vectors = []
    for case in _cases():
        header, _ = build(case, key)
        wrapper = json.loads(header)
        authorization = wrapper["payload"]["authorization"]
        digest = receive_digest(case["network_config"], authorization)
        nonce = es.compute_escrow_nonce(
            case["network_config"]["chain_id"],
            case["network_config"]["escrow"],
            TYPEHASH,
            wrapper["payload"]["paymentInfo"],
        )
        if nonce != authorization["nonce"]:
            raise SystemExit(f"{case['id']}: nonce does not recompute — generator bug")
        if case.get("delegate") not in SMA_WRAP_TARGETS:
            # Plain ECDSA over the digest (the SMA dialect wraps another one).
            recovered = Account._recover_hash(
                bytes.fromhex(digest[2:]),
                signature=bytes.fromhex(wrapper["payload"]["signature"][2:]),
            )
            if recovered != signer:
                raise SystemExit(f"{case['id']}: signature does not recover to the signer")
        entry = {k: v for k, v in case.items() if k != "lowercase"}
        if case.get("lowercase"):
            entry["lowercase_inputs"] = True
        entry["expected"] = {
            "nonce": nonce,
            "digest": digest,
            "signature": wrapper["payload"]["signature"],
            "header_sha256": hashlib.sha256(header.encode("utf-8")).hexdigest(),
            "wrapper": wrapper,
        }
        vectors.append(entry)
    return {
        "_note": [
            "Escrow pre-auth vectors recorded from uvd_x402_sdk.escrow_signing",
            "(build_escrow_pre_auth) by scripts/escrow_preauth_vectors.py. One per chain of",
            "VERIFIED_USDC_DOMAINS plus variants on Base. Never edited by hand; a red test",
            "is fixed in the code, not here.",
            "Hex values of 32 bytes or more are stored WITHOUT 0x; loaders re-prefix them.",
            "signer_private_key is a throwaway made with Account.create(): it never held funds.",
            "The payer is the signer's address; operators are synthetic (the chain id as an",
            "address).",
        ],
        "generator": "scripts/escrow_preauth_vectors.py",
        "signer_private_key": key.removeprefix("0x"),
        "signer_address": signer,
        "frozen": {
            "now": NOW,
            "salt": SALT_HEX,
            "receiver": RECEIVER,
            "payment_info_typehash": _strip(TYPEHASH),
            "min_fee_bps": 0,
            "max_fee_bps": 1800,
            "deposit_limit_usd": 100,
        },
        "vectors": [_strip(v) for v in vectors],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--check", action="store_true", help="regenerate in memory; exit 1 on drift"
    )
    parser.add_argument(
        "--new-key", action="store_true", help="make a new throwaway key (moves every signature)"
    )
    args = parser.parse_args()

    if args.new_key and args.check:
        print("refused: --check and --new-key together make no sense")
        return 2
    if OUT_PATH.exists() and not args.new_key:
        key = "0x" + json.loads(OUT_PATH.read_text(encoding="utf-8"))["signer_private_key"]
    elif args.check:
        print(f"--check: {OUT_PATH} does not exist yet")
        return 1
    else:
        key = Account.create().key.hex()
        key = key if key.startswith("0x") else "0x" + key

    text = json.dumps(record(key), indent=2) + "\n"
    if _LEAK.search(text):
        print("refused: the output carries a 0x + 64 hex literal")
        return 2
    if args.check:
        if OUT_PATH.read_text(encoding="utf-8") != text:
            print(f"--check: DRIFT against {OUT_PATH}")
            return 1
        print(f"--check: OK ({OUT_PATH.name} is what the SDK signs today)")
        return 0
    OUT_PATH.write_text(text, encoding="utf-8")
    print(f"wrote {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
