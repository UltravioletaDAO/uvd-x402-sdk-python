"""Record the shared reputation vectors: ``tests/vectors/reputation/*.json``.

The CONTRACT between the Python module (``uvd_x402_sdk.reputation``) and its
TypeScript twin (``uvd-x402-sdk/reputation``): the same inputs must give the
same digest, the same signature and EIP-7702 authorization, the same refusal
code, the same evidence bytes and hash, the same ``paymentHash``, the same
``message_ref`` and the same verdicts of ``ensure_solana_identity`` on each
facilitator answer. The TypeScript worker consumes these files as they are.

Every value is cross-checked BEFORE it is written against an implementation
that is not the module's: the typed-data digest with eth-account's encoder
over the facilitator-shaped typed data, the authorization with eth-account's
``sign_authorization``, the signature with eth-account's prehash signing, the
``paymentHash`` against the facilitator's own captured vector
(``tests/fixtures/facilitator-settle-2.40.0``), the giveFeedback digest
against the one read from the DEPLOYED v4 delegate on Base. A disagreement
refuses to write (exit 2).

The signing key is the fixed test key ``0x`` + ``"11" * 32`` (``signerSeedByte``
in the files), built at runtime, never written: it never held funds.

Recording again is NOT how a red test gets fixed: if a change moves one of
these bytes, the change is what is wrong, unless moving them was its point.

Usage: python scripts/reputation_vectors.py [--check]
Exit codes: 0 ok, 1 --check found drift, 2 refused.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import hmac
import json
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from eth_account import Account  # noqa: E402
from eth_account.messages import encode_typed_data  # noqa: E402
from eth_utils import keccak, to_checksum_address  # noqa: E402

from uvd_x402_sdk.erc8004 import ProofOfPayment  # noqa: E402
from uvd_x402_sdk.reputation import (  # noqa: E402
    AccountSigner,
    PreparationRejected,
    RatingParams,
    build_relay_authorization,
    evidence_document,
    message_ref,
    payment_hash,
    relayed_feedback_digest,
    sign_relayed_feedback,
    validate_preparation,
)
from uvd_x402_sdk.reputation._chains import RELAY_CHAIN_IDS, RELAY_DELEGATES  # noqa: E402
from uvd_x402_sdk.reputation._errors import EvidenceRejected  # noqa: E402
from uvd_x402_sdk.reputation.payment import _transfer_calldata  # noqa: E402

OUT = REPO / "tests" / "vectors" / "reputation"
SEED = "11"
REGISTRY = "0x8004BAa17C55a88189AE136b182e5fdA19dE9b63"
USDC_BASE = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
SUBJECT = "0x2222222222222222222222222222222222222222"
NOW = 1_790_000_000


class Refused(Exception):
    pass


def account() -> Any:
    return Account.from_key("0x" + SEED * 32)


def facilitator_typed_data(
    chain_id: int, rater: str, params: dict, deadline: int, nonce: str
) -> dict:
    """As x402-rs ``give_feedback_typed_data`` emits it (uints as strings)."""
    return {
        "domain": {
            "name": "FeedbackDelegate",
            "version": "1",
            "chainId": chain_id,
            "verifyingContract": to_checksum_address(rater),
        },
        "primaryType": "RelayedGiveFeedback",
        "types": {
            "EIP712Domain": [
                {"name": "name", "type": "string"},
                {"name": "version", "type": "string"},
                {"name": "chainId", "type": "uint256"},
                {"name": "verifyingContract", "type": "address"},
            ],
            "RelayedGiveFeedback": [
                {"name": n, "type": t}
                for n, t in (
                    ("registry", "address"),
                    ("agentId", "uint256"),
                    ("value", "int128"),
                    ("valueDecimals", "uint8"),
                    ("tag1", "string"),
                    ("tag2", "string"),
                    ("endpoint", "string"),
                    ("feedbackURI", "string"),
                    ("feedbackHash", "bytes32"),
                    ("deadline", "uint256"),
                    ("nonce", "bytes32"),
                )
            ],
        },
        "message": {
            "registry": REGISTRY,
            "agentId": str(params["agentId"]),
            "value": str(params["value"]),
            "valueDecimals": 0,
            "tag1": params["tag1"],
            "tag2": params["tag2"],
            "endpoint": params["endpoint"],
            "feedbackURI": params["feedbackURI"],
            "feedbackHash": params["feedbackHash"],
            "deadline": str(deadline),
            "nonce": nonce,
        },
    }


def eth_account_digest(typed: dict) -> str:
    signable = encode_typed_data(full_message=typed)
    return "0x" + bytes(keccak(b"\x19" + signable.version + signable.header + signable.body)).hex()


def rating(params: dict) -> RatingParams:
    return RatingParams(
        agent_id=params["agentId"],
        value=params["value"],
        tag1=params["tag1"],
        tag2=params["tag2"],
        endpoint=params["endpoint"],
        feedback_uri=params["feedbackURI"],
        feedback_hash=params["feedbackHash"],
    )


RELAY_CASES = [
    {
        "name": "base-first-rating",
        "network": "base",
        "params": {
            "agentId": 18896,
            "value": 90,
            "tag1": "credits_roll:idea",
            "tag2": "credits-roll|moment",
            "endpoint": "https://www.twitch.tv/somechannel",
            "feedbackURI": "https://credits.example/evidence/" + "5a" * 32 + ".json",
            "feedbackHash": "0x" + "5a" * 32,
        },
        "nonce": "0x" + "00" * 31 + "01",
        "delegated": False,
        "accountNonce": 0,
    },
    {
        "name": "base-delegated",
        "network": "base",
        "params": {
            "agentId": 18896,
            "value": 85,
            "tag1": "credits_roll:assist",
            "tag2": "credits-roll|moment",
            "endpoint": "https://www.twitch.tv/somechannel",
            "feedbackURI": "https://credits.example/evidence/second.json",
            "feedbackHash": "0x" + "6b" * 32,
        },
        "nonce": "0x" + "00" * 31 + "02",
        "delegated": True,
        "accountNonce": None,
    },
    {
        "name": "base-unicode-tags",
        "network": "base",
        "params": {
            "agentId": 7,
            "value": 80,
            "tag1": "requester_rating:diseño",
            "tag2": "karma-hello|comentario \U0001f3ac",
            "endpoint": "https://kick.com/cañal",
            "feedbackURI": "ipfs://bafy-example",
            "feedbackHash": "0x" + "7c" * 32,
        },
        "nonce": "0x" + "ab" * 32,
        "delegated": True,
        "accountNonce": None,
    },
    {
        "name": "arc-first-rating",
        "network": "arc",
        "params": {
            "agentId": 1,
            "value": 95,
            "tag1": "quality",
            "tag2": "api",
            "endpoint": "https://agent.example",
            "feedbackURI": "https://example.com/f.json",
            "feedbackHash": "0x" + "00" * 32,
        },
        "nonce": "0x" + "42" * 32,
        "delegated": False,
        "accountNonce": 7,
    },
]


def relay_vectors() -> dict:
    acct = account()
    signer = AccountSigner(acct)
    cases = []
    for case in RELAY_CASES:
        chain_id = RELAY_CHAIN_IDS[case["network"]]
        deadline = NOW + 900
        typed = facilitator_typed_data(
            chain_id, acct.address, case["params"], deadline, case["nonce"]
        )
        digest = eth_account_digest(typed)
        prepare = {
            "success": True,
            "delegate": RELAY_DELEGATES[chain_id],
            "digest": digest,
            "typedData": typed,
            "deadline": deadline,
            "nonce": case["nonce"],
            "delegated": case["delegated"],
            "accountNonce": case["accountNonce"],
            "chainId": chain_id,
            "network": case["network"],
        }
        verified = validate_preparation(
            prepare, rating(case["params"]), acct.address, network=case["network"], now=NOW
        )
        if verified.digest != digest:
            raise Refused(f"{case['name']}: module digest != eth-account digest")
        signature = sign_relayed_feedback(verified, signer)
        reference = acct.unsafe_sign_hash(bytes.fromhex(digest[2:]))
        if signature != "0x" + bytes(reference.signature).hex():
            raise Refused(f"{case['name']}: module signature != eth-account signature")
        authorization = build_relay_authorization(verified, signer)
        auth_json = None
        auth_digest = None
        if not case["delegated"]:
            ref = acct.sign_authorization(
                {
                    "chainId": chain_id,
                    "address": RELAY_DELEGATES[chain_id],
                    "nonce": case["accountNonce"],
                }
            )
            auth_json = authorization.model_dump(by_alias=True)
            expected = {
                "chainId": chain_id,
                "address": RELAY_DELEGATES[chain_id],
                "nonce": case["accountNonce"],
                "yParity": ref.y_parity,
                "r": "0x" + ref.r.to_bytes(32, "big").hex(),
                "s": "0x" + ref.s.to_bytes(32, "big").hex(),
            }
            if auth_json != expected:
                raise Refused(f"{case['name']}: module authorization != eth-account authorization")
            auth_digest = "0x" + bytes(ref.authorization_hash).hex()
        elif authorization is not None:
            raise Refused(f"{case['name']}: a delegated rater got an authorization")
        cases.append(
            {
                "name": case["name"],
                "network": case["network"],
                "now": NOW,
                "signerSeedByte": SEED,
                "rater": acct.address,
                "params": case["params"],
                "prepare": prepare,
                "expected": {
                    "digest": digest,
                    "typedDataToSign": verified.typed_data,
                    "signature": signature,
                    "authorizationDigest": auth_digest,
                    "authorization": auth_json,
                },
            }
        )
    deployed = {
        "agentId": 2106,
        "value": 95,
        "tag1": "quality",
        "tag2": "api",
        "endpoint": "https://agent.example",
        "feedbackURI": "https://execution.market/feedback/abc",
        "feedbackHash": "0x" + "00" * 32,
    }
    account_is_delegate = RELAY_DELEGATES[8453]
    digest = (
        "0x"
        + relayed_feedback_digest(
            8453, account_is_delegate, REGISTRY, rating(deployed), 1_787_662_172, "0x" + "42" * 32
        ).hex()
    )
    if digest != "0xa7a2a62dd18c44c79f7c252d628f32b3c46234b20d237866154dbe118ebaf6e1":
        raise Refused("the giveFeedback digest drifted from the deployed v4 contract")
    return {
        "description": "Relayed ERC-8004 rating: facilitator prepare -> digest, signature, EIP-7702 "
        "authorization. Signer key = '0x' + signerSeedByte * 32 (test key, no funds). "
        "typedDataToSign is what the module hands a wallet (types without EIP712Domain).",
        "cases": cases,
        "deployedContract": {
            "source": "x402-rs relay_v4.rs::the_vector_matches_the_deployed_v4_contract (eth_call on "
            "Base, 2026-08-25): the view called on the delegate itself, so the account is "
            "the delegate",
            "chainId": 8453,
            "account": account_is_delegate,
            "registry": REGISTRY,
            "params": deployed,
            "deadline": 1_787_662_172,
            "nonce": "0x" + "42" * 32,
            "domainSeparator": "0xae792427891b13b5663e321e5c8802c304437aad75338e22a4b8e1a47243d09d",
            "digest": digest,
        },
    }


# --- rejections ----------------------------------------------------------------
def _ops(*ops: tuple) -> list:
    out = []
    for item in ops:
        if item[0] == "set":
            out.append({"op": "set", "path": list(item[1]), "value": item[2]})
        else:
            out.append({"op": "delete", "path": list(item[1])})
    return out


MUTATIONS = [
    ("prepare-failed", _ops(("set", ["success"], False)), "prepare_failed", None),
    ("chain-id-zero", _ops(("set", ["chainId"], 0)), "no_chain", None),
    ("chain-id-other", _ops(("set", ["chainId"], 1)), "chain_mismatch", None),
    ("asked-another-network", [], "chain_mismatch", {"network": "ethereum"}),
    ("asked-unknown-network", [], "unknown_network", {"network": "avalanche"}),
    (
        "delegate-not-allowlisted",
        _ops(("set", ["delegate"], "0x" + "de" * 20)),
        "delegate_mismatch",
        None,
    ),
    (
        "delegate-of-another-chain",
        _ops(("set", ["delegate"], RELAY_DELEGATES[1])),
        "delegate_mismatch",
        None,
    ),
    ("typed-data-missing", _ops(("set", ["typedData"], None)), "typed_data_missing", None),
    ("typed-data-extra-key", _ops(("set", ["typedData", "extra"], 1)), "typed_data_shape", None),
    (
        "primary-type-revoke",
        _ops(("set", ["typedData", "primaryType"], "RelayedRevokeFeedback")),
        "primary_type",
        None,
    ),
    (
        "types-without-domain",
        _ops(("delete", ["typedData", "types", "EIP712Domain"])),
        "types_mismatch",
        None,
    ),
    (
        "types-value-int256",
        _ops(("set", ["typedData", "types", "RelayedGiveFeedback", 2, "type"], "int256")),
        "types_mismatch",
        None,
    ),
    (
        "types-extra-struct",
        _ops(("set", ["typedData", "types", "Permit"], [])),
        "types_mismatch",
        None,
    ),
    (
        "domain-name-permit",
        _ops(("set", ["typedData", "domain", "name"], "USD Coin")),
        "domain_mismatch",
        None,
    ),
    (
        "domain-version",
        _ops(("set", ["typedData", "domain", "version"], "2")),
        "domain_mismatch",
        None,
    ),
    (
        "domain-extra-key",
        _ops(("set", ["typedData", "domain", "salt"], "0x" + "00" * 32)),
        "domain_mismatch",
        None,
    ),
    (
        "domain-chain-id",
        _ops(("set", ["typedData", "domain", "chainId"], 1)),
        "domain_chain_id",
        None,
    ),
    (
        "verifying-contract-is-delegate",
        _ops(("set", ["typedData", "domain", "verifyingContract"], RELAY_DELEGATES[8453])),
        "domain_verifying_contract",
        None,
    ),
    (
        "message-extra-field",
        _ops(("set", ["typedData", "message", "extra"], "x")),
        "message_shape",
        None,
    ),
    (
        "registry-is-usdc",
        _ops(("set", ["typedData", "message", "registry"], USDC_BASE)),
        "registry",
        None,
    ),
    ("value-changed", _ops(("set", ["typedData", "message", "value"], "99")), "value", None),
    (
        "value-decimals-2",
        _ops(("set", ["typedData", "message", "valueDecimals"], 2)),
        "value",
        None,
    ),
    (
        "agent-id-changed",
        _ops(("set", ["typedData", "message", "agentId"], "18897")),
        "message_binding",
        None,
    ),
    (
        "tag1-changed",
        _ops(("set", ["typedData", "message", "tag1"], "attacker")),
        "message_binding",
        None,
    ),
    (
        "tag2-changed",
        _ops(("set", ["typedData", "message", "tag2"], "false issuer")),
        "message_binding",
        None,
    ),
    (
        "endpoint-changed",
        _ops(("set", ["typedData", "message", "endpoint"], "https://evil.example")),
        "message_binding",
        None,
    ),
    (
        "feedback-uri-changed",
        _ops(("set", ["typedData", "message", "feedbackURI"], "https://evil.example")),
        "message_binding",
        None,
    ),
    (
        "feedback-hash-changed",
        _ops(("set", ["typedData", "message", "feedbackHash"], "0x" + "ee" * 32)),
        "message_binding",
        None,
    ),
    (
        "message-nonce-not-prepare-nonce",
        _ops(("set", ["typedData", "message", "nonce"], "0x" + "99" * 32)),
        "message_binding",
        None,
    ),
    (
        "message-deadline-not-prepare-deadline",
        _ops(("set", ["typedData", "message", "deadline"], str(NOW + 901))),
        "message_binding",
        None,
    ),
    (
        "deadline-too-far",
        _ops(
            ("set", ["deadline"], NOW + 3601),
            ("set", ["typedData", "message", "deadline"], str(NOW + 3601)),
        ),
        "deadline",
        None,
    ),
    (
        "deadline-too-close",
        _ops(
            ("set", ["deadline"], NOW + 15),
            ("set", ["typedData", "message", "deadline"], str(NOW + 15)),
        ),
        "deadline",
        None,
    ),
    ("digest-not-ours", _ops(("set", ["digest"], "0x" + "01" * 32)), "digest_mismatch", None),
    ("digest-missing", _ops(("set", ["digest"], None)), "digest_mismatch", None),
    (
        "first-rating-without-account-nonce",
        _ops(("set", ["accountNonce"], None)),
        "account_nonce",
        None,
    ),
    ("account-nonce-beyond-uint64", _ops(("set", ["accountNonce"], 2**64)), "account_nonce", None),
    ("another-rater", [], "domain_verifying_contract", {"rater": SUBJECT}),
    # A trailing newline is a different string: Python's `$` would accept it,
    # JavaScript's would not. Both refuse.
    (
        "agent-id-trailing-newline",
        _ops(("set", ["typedData", "message", "agentId"], "18896\n")),
        "message_binding",
        None,
    ),
    (
        "delegate-trailing-newline",
        _ops(("set", ["delegate"], RELAY_DELEGATES[8453] + "\n")),
        "delegate_mismatch",
        None,
    ),
    (
        "agent-id-hex",
        _ops(("set", ["typedData", "message", "agentId"], "0x49d0")),
        "message_binding",
        None,
    ),
    (
        "agent-id-leading-space",
        _ops(("set", ["typedData", "message", "agentId"], " 18896")),
        "message_binding",
        None,
    ),
]

ACCEPTED = [
    ("delegate-lowercase", _ops(("set", ["delegate"], RELAY_DELEGATES[8453].lower()))),
    ("digest-uppercase-hex", _ops(("set", ["digest"], None))),  # filled in below
    (
        "message-uints-as-json-numbers",
        _ops(
            ("set", ["typedData", "message", "agentId"], 18896),
            ("set", ["typedData", "message", "value"], 90),
            ("set", ["typedData", "message", "deadline"], NOW + 900),
        ),
    ),
    ("domain-chain-id-as-string", _ops(("set", ["typedData", "domain", "chainId"], "8453"))),
    ("registry-lowercase", _ops(("set", ["typedData", "message", "registry"], REGISTRY.lower()))),
]


def apply_ops(document: dict, ops: list) -> dict:
    out = copy.deepcopy(document)
    for op in ops:
        target = out
        for key in op["path"][:-1]:
            target = target[key]
        if op["op"] == "set":
            target[op["path"][-1]] = op["value"]
        else:
            del target[op["path"][-1]]
    return out


def rejection_vectors(base_case: dict) -> dict:
    prepare = base_case["prepare"]
    rejected, accepted = [], []
    for name, ops, code, overrides in MUTATIONS:
        overrides = overrides or {}
        mutated = apply_ops(prepare, ops)
        try:
            validate_preparation(
                mutated,
                rating(base_case["params"]),
                overrides.get("rater", base_case["rater"]),
                network=overrides.get("network", base_case["network"]),
                now=NOW,
            )
        except PreparationRejected as exc:
            if exc.code != code:
                raise Refused(f"{name}: refused as {exc.code}, expected {code}")
        else:
            raise Refused(f"{name}: was accepted")
        rejected.append({"name": name, "ops": ops, "code": code, **overrides})
    for name, ops in ACCEPTED:
        if name == "digest-uppercase-hex":
            ops = _ops(("set", ["digest"], "0x" + prepare["digest"][2:].upper()))
        validate_preparation(
            apply_ops(prepare, ops),
            rating(base_case["params"]),
            base_case["rater"],
            network=base_case["network"],
            now=NOW,
        )
        accepted.append({"name": name, "ops": ops})
    return {
        "description": "validate_preparation over the base-first-rating prepare of relayed-feedback.json: "
        "apply each case's ops (set/delete at a JSON path) and expect the code of the FIRST "
        "failing check (the order of validate_preparation is part of the contract). "
        "'network' / 'rater' override what the caller asked for. 'accepted' must pass.",
        "base": base_case["name"],
        "now": NOW,
        "rejected": rejected,
        "accepted": accepted,
    }


# --- message refs ----------------------------------------------------------------
REF_CASES = [
    ("credits-roll:v1", ["stream-1", "msg-1"], "Credits Roll moment_ref"),
    ("credits-roll:v1", ["40912334", "c6a1b1f2-6f3b-4c1e-9a52-2b8a3b2c1d0e"], "Twitch ids"),
    (
        "base",
        ["0x" + "12" * 20, "log-123"],
        "= Karma Hello operation_id (live.py initial_feedback)",
    ),
    ("credits-roll:v1", [42, "m"], "an int part is written in decimal"),
    ("credits-roll:v1", ["momento-ñ", "\U0001f3ac"], "non-ASCII parts hash their UTF-8"),
]
REF_REJECTED = [
    ("credits-roll:v1", ["a|b", "c"], "separator"),
    ("credits-roll|v1", ["a"], "separator"),
    ("credits-roll:v1", ["", "c"], "empty"),
    ("credits-roll:v1", ["   "], "empty"),
    ("credits-roll:v1", [], "no_parts"),
    ("", ["a"], "empty"),
    ("credits-roll:v1", [True], "type"),
    ("credits-roll:v1", [None], "type"),
]


def ref_vectors() -> dict:
    cases = []
    for namespace, parts, note in REF_CASES:
        ref = message_ref(namespace, *parts)
        joined = "|".join([namespace, *[str(p) for p in parts]])
        if ref != hashlib.sha256(joined.encode("utf-8")).hexdigest():
            raise Refused("message_ref != sha256 of the join")
        cases.append({"namespace": namespace, "parts": parts, "ref": ref, "note": note})
    rejected = []
    for namespace, parts, reason in REF_REJECTED:
        try:
            message_ref(namespace, *parts)
        except ValueError:
            rejected.append({"namespace": namespace, "parts": parts, "reason": reason})
        else:
            raise Refused(f"message_ref accepted {parts!r}")
    kh = hashlib.sha256("base|{}|{}".format("0x" + "12" * 20, "log-123").encode()).hexdigest()
    if cases[2]["ref"] != kh:
        raise Refused("message_ref no longer reproduces Karma Hello's operation_id")
    return {
        "description": "message_ref(namespace, *parts) = sha256(utf8('|'.join([namespace, *parts]))) as "
        "64 lowercase hex. Ints are written in decimal. Refused (ValueError): a '|' in any "
        "part or the namespace, an empty or blank part, no parts, a part that is not a "
        "string or an int (booleans included).",
        "cases": cases,
        "rejected": rejected,
    }


# --- payment ----------------------------------------------------------------------
def payment_vectors() -> dict:
    fixture = json.loads(
        (
            REPO / "tests/fixtures/facilitator-settle-2.40.0/settle_success_with_proof.json"
        ).read_text(encoding="utf-8")
    )
    captured = json.loads(fixture["body"])["proofOfPayment"]
    hashes = []
    inputs = [
        (
            "facilitator-2.40.0",
            captured["transactionHash"],
            captured["blockNumber"],
            captured["payer"],
            captured["payee"],
            int(captured["amount"]),
            captured["paymentHash"],
        ),
        ("anchor-0.001-usdc", "0x" + "c3" * 32, 53_125_001, account().address, SUBJECT, 1000, None),
        (
            "addresses-lowercase",
            "0x" + "c3" * 32,
            53_125_001,
            account().address.lower(),
            SUBJECT.lower(),
            1000,
            None,
        ),
    ]
    for name, tx, block, payer, payee, amount, known in inputs:
        value = payment_hash(tx, block, payer, payee, amount)
        if known is not None and value != known:
            raise Refused(f"{name}: payment_hash != the facilitator's")
        hashes.append(
            {
                "name": name,
                "transactionHash": tx,
                "blockNumber": block,
                "payer": payer,
                "payee": payee,
                "amount": str(amount),
                "paymentHash": value,
            }
        )
    if hashes[1]["paymentHash"] != hashes[2]["paymentHash"]:
        raise Refused("paymentHash depends on the case of the addresses")
    receipt_tx = "0x" + "c3" * 32
    proof = ProofOfPayment(
        transaction_hash=receipt_tx,
        block_number=53_125_001,
        network="base",
        payer=account().address,
        payee=to_checksum_address(SUBJECT),
        amount="1000",
        token=USDC_BASE,
        timestamp=1_790_000_002,
        payment_hash=hashes[1]["paymentHash"],
    )
    return {
        "description": "paymentHash = keccak(txHash32 || u64be(blockNumber) || utf8(EIP-55 payer) || "
        "utf8(EIP-55 payee) || u256be(amount)), as x402-rs types.rs::compute_payment_hash "
        "(the first case IS the facilitator's). transferCalldata is the anchor transfer the "
        "rater signs (to = the network's USDC, value 0).",
        "paymentHash": hashes,
        "transferCalldata": [
            {
                "payee": SUBJECT,
                "amount": "1000",
                "data": _transfer_calldata(to_checksum_address(SUBJECT), 1000),
            }
        ],
        "proof": proof.model_dump(by_alias=True),
    }


# --- evidence ---------------------------------------------------------------------
def evidence_vectors(proof: dict) -> dict:
    salt = bytes.fromhex("ab" * 32)
    rater = account().address
    moment = message_ref("credits-roll:v1", "stream-1", "msg-1")
    base_inputs = {
        "network": "base",
        "agentId": 18896,
        "rater": rater,
        "subject": SUBJECT,
        "value": 90,
        "tag1": "credits_roll:idea",
        "tag2": "credits-roll|moment",
        "endpoint": "https://www.twitch.tv/somechannel",
        "createdAt": NOW,
        "saltHex": "ab" * 32,
        "private": {
            "moment_ref": moment,
            "user_id": "40912334",
            "login": "some_viewer",
            "message": "una idea brillante para el stream",
        },
        "walletBinding": "self_declared_unverified",
        "proof": proof,
        "issuer": {"name": "Credits Roll", "product": "credits-roll"},
        "context": {"momentType": "idea"},
    }
    unicode_inputs = dict(
        base_inputs,
        tag1="credits_roll:riff ñ",
        tag2="credits-roll|\U0001f3ac",
        walletBinding="eip191_verified",
        proof=None,
        context=None,
        private={"moment_ref": moment},
    )

    def build(inputs: dict) -> Any:
        return evidence_document(
            network=inputs["network"],
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

    cases = []
    for name, inputs in (
        ("credits-roll-moment", base_inputs),
        ("unicode-no-proof", unicode_inputs),
    ):
        built = build(inputs)
        for label, value in inputs["private"].items():
            expected = (
                "0x" + hmac.new(salt, (label + "|" + value).encode(), hashlib.sha256).hexdigest()
            )
            if built.document["commitments"][label] != expected:
                raise Refused(f"{name}: commitment of {label} != HMAC-SHA256")
            if len(value) >= 8 and value in built.body.decode("utf-8"):
                raise Refused(f"{name}: the private {label} is in the body")
        if built.content_hash != "0x" + bytes(keccak(built.body)).hex():
            raise Refused(f"{name}: content_hash != keccak(body)")
        cases.append(
            {
                "name": name,
                "inputs": inputs,
                "body": built.body.decode("utf-8"),
                "contentHash": built.content_hash,
            }
        )
    rejected_inputs = [
        (
            "context-carries-the-message",
            dict(base_inputs, context={"message": "hola"}),
            "private_field",
        ),
        (
            "context-author-any-case",
            dict(base_inputs, context={"Author-Name": "x", "Author": "y"}),
            "private_field",
        ),
        ("nested-login", dict(base_inputs, context={"viewer": {"login": "x"}}), "private_field"),
        (
            "tag2-carries-the-text",
            dict(base_inputs, tag2="credits-roll|una idea brillante para el stream"),
            "private_value_leaked",
        ),
        ("context-float", dict(base_inputs, context={"score": 0.5}), "float_in_evidence"),
        ("binding-unknown", dict(base_inputs, walletBinding="verified"), "wallet_binding"),
        ("network-without-relay", dict(base_inputs, network="avalanche"), "unknown_network"),
        ("value-100-plus", dict(base_inputs, value=101), "value"),
    ]
    rejected = []
    for name, inputs, code in rejected_inputs:
        try:
            build(inputs)
        except EvidenceRejected as exc:
            if exc.code != code:
                raise Refused(f"{name}: refused as {exc.code}, expected {code}")
        else:
            raise Refused(f"{name}: was accepted")
        rejected.append({"name": name, "inputs": inputs, "code": code})
    return {
        "description": "evidence_document: body = canonical JSON (sorted keys, no whitespace, UTF-8, "
        "ensure_ascii off, no floats) of the document; contentHash = keccak(body) = the "
        "feedbackHash. 'private' values enter only as commitments "
        "HMAC-SHA256(salt, label|value); none of them, nor the keys of PRIVATE_KEYS, may "
        "appear in the public part.",
        "cases": cases,
        "rejected": rejected,
    }


# --- the Solana identity of a rated party -----------------------------------------
B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
SOL_OWNER = "6xNPewUdKRbEZDReQdpyfNUdgNg8QRc8Mt263T5GZSRv"
SOL_OTHER = "247Y4QLwz9ZbcuHR2nX2EQLZHCsMs1GTqvgd6fpdn85Q"
SOL_ASSET = "8oo4dC4JvBLwy5tGgiH3WwK4B9PWxL9Z4XjA2jzkQMbQ"
SOL_FEE_PAYER = "F742C4VfFLQ9zRQyithoj5229ZgtX2WqKCSFKgH2EThq"
# SOL_OWNER with its last letter upper-cased: base58 is case-sensitive, so it is
# ANOTHER valid address, and an owner compared without case would take it.
SOL_OWNER_CASE = SOL_OWNER[:-1] + SOL_OWNER[-1].upper()


def independent_b58decode(text: str) -> bytes:
    """Base58 by digit arithmetic, not the module's big-int: the cross-check."""
    out = [0]
    for ch in text:
        carry = B58.index(ch)
        for i in range(len(out)):
            carry += out[i] * 58
            out[i] = carry & 0xFF
            carry >>= 8
        while carry:
            out.append(carry & 0xFF)
            carry >>= 8
    body = bytes(reversed(out)).lstrip(b"\x00")
    return b"\x00" * (len(text) - len(text.lstrip("1"))) + body


SOL_VALID = [SOL_OWNER, SOL_OTHER, SOL_ASSET, SOL_FEE_PAYER, SOL_OWNER_CASE, "1" * 31 + "2",
             "z" * 43]
SOL_REJECTED = [
    ("1" * 32, "zero address"),
    (" " + SOL_OWNER, "leading space"),
    (SOL_OWNER + "\n", "trailing newline"),
    (SOL_OWNER[:-1] + "0", "0 is not base58"),
    (SOL_OWNER[:-1] + "O", "O is not base58"),
    (SOL_OWNER[:-1] + "I", "I is not base58"),
    (SOL_OWNER[:-1] + "l", "l is not base58"),
    (SOL_OWNER[:31], "31 characters"),
    (SOL_OWNER + "1", "45 characters"),
    ("z" * 44, "44 characters, 33 bytes"),
    ("2" * 32, "32 characters, 24 bytes"),
    ("0x" + "ab" * 20, "an EVM address"),
    ("", "empty"),
]


def _mint(status: str, code: Any = None, **extra: Any) -> dict:
    report = {"status": status, "resumed": False, "atomic": True, "metadataSkipped": False}
    if code:
        report["errorCode"] = code
    report.update(extra)
    return report


def _refused(code: str, top_level: bool = False) -> dict:
    body = {"success": False, "agentId": None, "transaction": None,
            "transferTransaction": None, "owner": None, "error": "refused",
            "network": "solana", "mint": _mint("not_minted", code, atomic=False)}
    if top_level:
        body["errorCode"] = code
    return body


_SIG = "5" * 88
# (name, status, body or None for an answer that is not a JSON object, expected)
SOL_LOOKUP = [
    ("owns-one", 200, {"agentId": SOL_ASSET, "owner": SOL_OWNER, "agentUri": "https://a.example/x",
                       "network": "solana", "balance": "1"},
     ("registered", "owner_lookup", SOL_ASSET)),
    ("owns-none", 404, {"error": "does not own any agent", "balance": "0"},
     ("absent", "owner_not_found", None)),
    ("404-number-balance", 404, {"error": "none", "balance": 0},
     ("unknown", "lookup_not_a_verdict", None)),
    ("404-float-balance", 404, {"error": "none", "balance": 0.0},
     ("unknown", "lookup_not_a_verdict", None)),
    ("route-not-found", 404, {"error": "Not Found", "code": "not_found"},
     ("unknown", "lookup_not_a_verdict", None)),
    ("404-not-json", 404, None, ("unknown", "lookup_not_a_verdict", None)),
    ("404-bool-balance", 404, {"balance": False}, ("unknown", "lookup_not_a_verdict", None)),
    ("404-balance-1", 404, {"balance": "1"}, ("unknown", "lookup_not_a_verdict", None)),
    ("inconclusive", 503, {"error": "getProgramAccounts failed", "retryable": True},
     ("unknown", "lookup_inconclusive", None)),
    ("rate-limited", 429, {"code": "rate_limited"}, ("unknown", "lookup_rate_limited", None)),
    ("another-owner", 200, {"agentId": SOL_ASSET, "owner": SOL_OTHER, "network": "solana"},
     ("unknown", "lookup_owner_mismatch", None)),
    ("owner-differs-only-in-case", 200,
     {"agentId": SOL_ASSET, "owner": SOL_OWNER_CASE, "network": "solana"},
     ("unknown", "lookup_owner_mismatch", None)),
    ("another-network", 200, {"agentId": SOL_ASSET, "owner": SOL_OWNER, "network": "base"},
     ("unknown", "lookup_network_mismatch", None)),
    ("bad-agent-id", 200, {"agentId": "x", "owner": SOL_OWNER, "network": "solana"},
     ("unknown", "lookup_unreadable", None)),
    ("200-not-json", 200, None, ("unknown", "lookup_unreadable", None)),
    ("bad-address", 400, {"error": "Invalid Solana address"}, ("unknown", "lookup_http_400", None)),
    ("server-error", 500, {"error": "No Solana provider"}, ("unknown", "lookup_http_500", None)),
]
_COMPLETE = {"success": True, "agentId": SOL_ASSET, "transaction": _SIG,
             "transferTransaction": _SIG, "owner": SOL_OWNER, "network": "solana",
             "mint": _mint("complete", statsTransaction=_SIG)}
# expected = (status, reason, releasesSlot, retryInCall, mintStatus, errorCode, agentId)
SOL_REGISTER = [
    ("complete", 200, _COMPLETE,
     ("created", "minted", False, False, "complete", None, SOL_ASSET)),
    ("complete-for-another-owner", 200, {**_COMPLETE, "owner": SOL_OTHER},
     ("uncertain", "owner_mismatch", False, False, "complete", None, SOL_ASSET)),
    ("complete-owner-differs-only-in-case", 200, {**_COMPLETE, "owner": SOL_OWNER_CASE},
     ("uncertain", "owner_mismatch", False, False, "complete", None, SOL_ASSET)),
    ("complete-without-agent-id", 200, {**_COMPLETE, "agentId": None},
     ("uncertain", "unreadable_answer", False, False, "complete", None, None)),
    ("success-not-complete", 200, {**_COMPLETE, "mint": _mint("pending_transfer")},
     ("uncertain", "inconsistent_answer", False, False, "pending_transfer", None, SOL_ASSET)),
    ("success-without-mint-report", 200, {k: v for k, v in _COMPLETE.items() if k != "mint"},
     ("created", "minted", False, False, None, None, SOL_ASSET)),
    ("200-not-success", 200, {**_COMPLETE, "success": False},
     ("uncertain", "inconsistent_answer", False, False, "complete", None, SOL_ASSET)),
    ("pending-transfer", 500, {"success": False, "agentId": SOL_ASSET, "owner": SOL_FEE_PAYER,
                               "network": "solana", "mint": _mint("pending_transfer",
                                                                  "transfer_failed")},
     ("pending", "pending_transfer", False, False, "pending_transfer", "transfer_failed",
      SOL_ASSET)),
    ("pending-stats", 500, {"success": False, "agentId": SOL_ASSET, "network": "solana",
                            "mint": _mint("pending_stats", "initialize_stats_failed")},
     ("pending", "pending_stats", False, False, "pending_stats", "initialize_stats_failed",
      SOL_ASSET)),
    ("500-not-minted", 500, _refused("mint_transaction_failed"),
     ("uncertain", "mint_transaction_failed", False, False, "not_minted",
      "mint_transaction_failed", None)),
    ("502-forward-unconfirmed", 502, {"reason": "forward_unconfirmed", "retryable": False},
     ("uncertain", "forward_unconfirmed", False, False, None, None, None)),
    ("504", 504, {"error": "gateway timeout"},
     ("uncertain", "http_504", False, False, None, None, None)),
    ("201-complete", 201, _COMPLETE,
     ("uncertain", "http_201", False, False, "complete", None, SOL_ASSET)),
    ("202", 202, {"jobId": "x", "status": "pending"},
     ("uncertain", "http_202", False, False, None, None, None)),
    ("404-on-post", 404, {"error": "Not Found", "code": "not_found"},
     ("uncertain", "http_404", False, False, None, None, None)),
    ("400-code-that-is-not-a-code", 400, {"success": False, "errorCode": "bad " + SOL_OWNER},
     ("refused", "facilitator_error", True, False, None, "facilitator_error", None)),
    ("503-stats-lookup", 503, _refused("stats_lookup_inconclusive"),
     ("unavailable", "stats_lookup_inconclusive", True, True, "not_minted",
      "stats_lookup_inconclusive", None)),
    ("503-overloaded", 503, {"error": "at its ceiling", "code": "overloaded"},
     ("unavailable", "overloaded", True, True, None, None, None)),
    ("429-too-many-concurrent", 429, {"error": "in flight", "code": "too_many_concurrent_requests"},
     ("unavailable", "too_many_concurrent_requests", True, True, None, None, None)),
    ("408-body-timeout", 408, {"error": "body did not arrive", "code": "request_timeout"},
     ("unavailable", "request_timeout", True, True, None, None, None)),
    ("409-held", 409, _refused("held_identity_not_resumable"),
     ("refused", "held_identity_not_resumable", True, False, "not_minted",
      "held_identity_not_resumable", None)),
    ("400-agent-uri", 400, _refused("agent_uri_tunnel", top_level=True),
     ("refused", "agent_uri_tunnel", True, False, "not_minted", "agent_uri_tunnel", None)),
    ("400-parse", 400, {"success": False, "error": "Invalid request format", "network": "ethereum"},
     ("refused", "http_400", True, False, None, None, None)),
    ("403-blocked", 403, _refused("recipient_blocked", top_level=True),
     ("refused", "recipient_blocked", True, False, "not_minted", "recipient_blocked", None)),
    ("503-fee-payer", 503, _refused("fee_payer_insufficient_balance"),
     ("unavailable", "fee_payer_insufficient_balance", True, False, "not_minted",
      "fee_payer_insufficient_balance", None)),
    ("503-registry", 503, _refused("registry_unavailable"),
     ("unavailable", "registry_unavailable", True, True, "not_minted", "registry_unavailable",
      None)),
    ("503-owner-lookup", 503, _refused("owner_lookup_inconclusive"),
     ("unavailable", "owner_lookup_inconclusive", True, True, "not_minted",
      "owner_lookup_inconclusive", None)),
    ("503-screening", 503, _refused("recipient_screening_unavailable", top_level=True),
     ("unavailable", "recipient_screening_unavailable", True, True, "not_minted",
      "recipient_screening_unavailable", None)),
    ("503-writer-lease", 503, {"error": "no writer lease", "reason": "forward_failed"},
     ("unavailable", "writer_lease_unavailable", True, True, None, None, None)),
    ("503-with-mint-in-progress", 503, {"success": False, "mint": _mint("pending_transfer")},
     ("uncertain", "inconsistent_answer", False, False, "pending_transfer", None, None)),
    ("503-marked-with-mint-in-progress", 503,
     {"success": False, "errorCode": "fee_payer_insufficient_balance",
      "mint": _mint("pending_transfer")},
     ("uncertain", "inconsistent_answer", False, False, "pending_transfer",
      "fee_payer_insufficient_balance", None)),
    ("503-not-json", 503, None, ("uncertain", "unreadable_answer", False, False, None, None, None)),
    # 408 / 429 / 503 release only with the facilitator's mark: a gateway answers
    # them too, maybe after it forwarded the request.
    ("503-unmarked", 503, {"message": "Service Unavailable"},
     ("uncertain", "unmarked_answer", False, False, None, None, None)),
    ("429-unmarked", 429, {"message": "Too Many Requests"},
     ("uncertain", "unmarked_answer", False, False, None, None, None)),
    ("408-unmarked", 408, {"message": "Request Timeout"},
     ("uncertain", "unmarked_answer", False, False, None, None, None)),
    ("503-code-that-is-not-a-code", 503, {"code": "ServiceUnavailable"},
     ("uncertain", "unmarked_answer", False, False, None, None, None)),
    ("503-marked-by-not-minted-alone", 503, {"success": False, "mint": {"status": "not_minted"}},
     ("unavailable", "service_unavailable", True, False, "not_minted", None, None)),
    ("503-success-true", 503, {**_refused("registry_unavailable"), "success": True},
     ("uncertain", "inconsistent_answer", False, False, "not_minted", "registry_unavailable",
      None)),
    ("409-success-true", 409, {**_refused("held_identity_not_resumable"), "success": True},
     ("uncertain", "inconsistent_answer", False, False, "not_minted",
      "held_identity_not_resumable", None)),
    ("429-rate-limited", 429, {"error": "Too Many Requests", "code": "rate_limited"},
     ("unavailable", "rate_limited", True, True, None, None, None)),
    ("429-daily-cap", 429, {"success": False, "code": "erc8004_daily_write_limit",
                            "network": "solana"},
     ("quota_exceeded", "facilitator_daily_cap", True, False, None, None, None)),
]
# (failure, what it means, expected (status, reason, releasesSlot, retryInCall)).
# The failure names a MEANING, not an httpx class: the TypeScript twin maps its
# own fetch errors onto these (a connection never made vs. a request that may
# have reached the server).
SOL_TRANSPORT = [
    ("connection_refused", "the TCP/TLS connection was never established",
     ("unavailable", "no_connection", True, True)),
    ("connect_timeout", "timed out establishing the connection",
     ("unavailable", "no_connection", True, True)),
    ("pool_timeout", "timed out waiting for a free connection of the client's pool",
     ("unavailable", "no_connection", True, True)),
    ("read_timeout", "the request went out and no answer came in time",
     ("uncertain", "no_answer", False, False)),
    ("write_timeout", "timed out while sending the request (part of it may be out)",
     ("uncertain", "no_answer", False, False)),
    ("disconnected", "the server closed the connection without an answer",
     ("uncertain", "no_answer", False, False)),
    ("proxy_error", "a proxy in between failed (the request may have gone through it)",
     ("uncertain", "no_answer", False, False)),
]
# The rules of the budget itself (InMemoryMintBudget), step by step on a fresh one.
# T0 is 23:00 UTC, so a step at T0 + 3600 or later is the next UTC day; D1 is that
# day's 00:00 UTC.
T0 = 1_790_035_200 - 3_600
D1 = 1_790_035_200
_P = {"dailyCap": 25, "perAddressCap": 3, "holdSeconds": 1800}
_A, _B, _C = "solana:" + SOL_OWNER, "solana:" + SOL_OTHER, "solana:" + SOL_ASSET


def _r(key: str, now: int, expect: str) -> dict:
    return {"op": "reserve", "key": key, "now": now, "expect": expect}


def _k(key: str, reserved_at: int, now: int) -> dict:
    return {"op": "keep", "key": key, "reservedAt": reserved_at, "now": now}


def _x(key: str, reserved_at: int, now: int) -> dict:
    return {"op": "release", "key": key, "reservedAt": reserved_at, "now": now}


def _u(now: int, network: str, expect: int) -> dict:
    return {"op": "usedToday", "now": now, "network": network, "expect": expect}


SOL_BUDGET = [
    ("in-flight-then-held-then-free", _P, [
        _r(_A, T0, "granted"), _r(_A, T0 + 10, "in_flight"), _k(_A, T0, T0 + 20),
        _r(_A, T0 + 30, "held"), _r(_A, T0 + 1820, "granted")]),
    ("a-release-frees-at-once", _P, [
        _r(_A, T0, "granted"), _x(_A, T0, T0 + 1), _r(_A, T0 + 2, "granted"),
        _u(T0 + 2, "solana", 1)]),
    ("an-orphan-expires-after-the-hold-counted-as-spent", _P, [
        _r(_A, T0, "granted"), _r(_A, T0 + 1799, "in_flight"), _r(_A, T0 + 1800, "granted"),
        _u(T0 + 1800, "solana", 2)]),
    ("a-late-release-names-its-request", _P, [
        _r(_A, T0, "granted"), _r(_A, T0 + 1800, "granted"), _x(_A, T0, T0 + 1801),
        _r(_A, T0 + 1802, "in_flight")]),
    ("a-late-keep-holds-again", _P, [
        _r(_A, T0, "granted"), _r(_A, T0 + 1800, "granted"), _x(_A, T0 + 1800, T0 + 1800),
        _k(_A, T0, T0 + 1801), _r(_A, T0 + 1802, "held")]),
    ("three-per-address-in-any-24-hours", _P, [
        _r(_A, T0, "granted"), _k(_A, T0, T0),
        _r(_A, T0 + 1801, "granted"), _k(_A, T0 + 1801, T0 + 1801),
        _r(_A, T0 + 3602, "granted"), _k(_A, T0 + 3602, T0 + 3602),
        _r(_A, T0 + 5403, "address_exhausted"), _r(_A, T0 + 86_400, "granted")]),
    ("the-daily-cap-is-per-network", {**_P, "dailyCap": 2}, [
        _r(_A, T0, "granted"), _r(_B, T0, "granted"), _r(_C, T0, "quota_exceeded"),
        _r("solana-devnet:" + SOL_OWNER, T0, "granted"),
        _u(T0, "solana", 2), _u(T0, "solana-devnet", 1)]),
    ("the-daily-cap-starts-over-at-00-utc", {**_P, "dailyCap": 1}, [
        _r(_A, T0, "granted"), _r(_B, T0 + 1, "quota_exceeded"), _r(_B, T0 + 3600, "granted")]),
    # The cap's window is the UTC calendar day: not an hour (01:30 and 23:30 of the
    # same day are refused), not a rolling 24 hours (00:00 of the next day, 23.5 h
    # after the first two, is granted), not another time zone.
    ("the-daily-cap-is-the-utc-day", {**_P, "dailyCap": 2}, [
        _r(_A, D1 + 1_800, "granted"), _r(_B, D1 + 1_800, "granted"),
        _r(_C, D1 + 5_400, "quota_exceeded"), _r(_C, D1 + 84_600, "quota_exceeded"),
        _r(_C, D1 + 86_399, "quota_exceeded"), _r(_C, D1 + 86_400, "granted"),
        _u(D1 + 86_400, "solana", 1)]),
]


async def _replay_budget(module: Any, policy: dict, steps: list) -> None:
    budget = module.InMemoryMintBudget()
    for step in steps:
        if step["op"] == "reserve":
            got = await budget.reserve(
                step["key"], now=step["now"], daily_cap=policy["dailyCap"],
                per_address_cap=policy["perAddressCap"], hold_seconds=policy["holdSeconds"],
            )
        elif step["op"] == "keep":
            got = await budget.keep(step["key"], reserved_at=step["reservedAt"],
                                    now=step["now"], hold_seconds=policy["holdSeconds"])
        elif step["op"] == "release":
            got = await budget.release(step["key"], reserved_at=step["reservedAt"],
                                       now=step["now"])
        else:
            got = budget.used_today(step["now"], step["network"])
        if "expect" in step and got != step["expect"]:
            raise Refused(f"budget step {step!r} gave {got!r}")


# What a MintBudget.reserve verdict other than "granted" makes of the call.
SOL_RESERVE = {
    "quota_exceeded": ("quota_exceeded", "client_daily_cap"),
    "address_exhausted": ("quota_exceeded", "address_daily_cap"),
    "in_flight": ("pending", "in_flight"),
    "held": ("uncertain", "recent_request"),
}


def solana_identity_vectors() -> dict:
    import httpx

    from uvd_x402_sdk.reputation import solana_identity as si

    for value in SOL_VALID:
        raw = independent_b58decode(value)
        if len(raw) != 32 or raw == bytes(32) or si.validate_solana_address(value) != value:
            raise Refused(f"valid address disagrees: {value!r}")
    if independent_b58decode(SOL_OWNER_CASE) == independent_b58decode(SOL_OWNER):
        raise Refused("the case-flipped owner is the same key")
    rejected = []
    for value, reason in SOL_REJECTED:
        try:
            si.validate_solana_address(value)
        except ValueError:
            rejected.append({"value": value, "reason": reason})
        else:
            raise Refused(f"validate_solana_address accepted {value!r}")
    lookup = []
    for name, status, body, (state, reason, agent_id) in SOL_LOOKUP:
        got = si.lookup_verdict(status, body, SOL_OWNER, "solana")
        if (got.state, got.reason, got.agent_id) != (state, reason, agent_id):
            raise Refused(f"lookup {name}: {got!r}")
        lookup.append({"name": name, "statusCode": status, "body": body,
                       "expected": {"state": state, "reason": reason, "agentId": agent_id}})
    register = []
    for name, status, body, expected in SOL_REGISTER:
        got = si.register_verdict(status, body, SOL_OWNER)
        values = (got.status, got.reason, got.releases, got.retry, got.mint_status,
                  got.error_code, got.agent_id)
        if values != expected:
            raise Refused(f"register {name}: {values!r}")
        keys = ("status", "reason", "releasesSlot", "retryInCall", "mintStatus", "errorCode",
                "agentId")
        register.append({"name": name, "statusCode": status, "body": body,
                         "expected": dict(zip(keys, expected))})
    failures = {
        "connection_refused": httpx.ConnectError("x"),
        "connect_timeout": httpx.ConnectTimeout("x"),
        "pool_timeout": httpx.PoolTimeout("x"),
        "read_timeout": httpx.ReadTimeout("x"),
        "write_timeout": httpx.WriteTimeout("x"),
        "disconnected": httpx.RemoteProtocolError("x"),
        "proxy_error": httpx.ProxyError("x"),
    }
    transport = []
    for failure, meaning, expected in SOL_TRANSPORT:
        got = si._transport_verdict(failures[failure])
        if (got.status, got.reason, got.releases, got.retry) != expected:
            raise Refused(f"transport {failure}: {got!r}")
        keys = ("status", "reason", "releasesSlot", "retryInCall")
        transport.append(
            {"failure": failure, "meaning": meaning, "expected": dict(zip(keys, expected))}
        )
    if si._RESERVE_REFUSALS != SOL_RESERVE:
        raise Refused(f"reserve mapping moved: {si._RESERVE_REFUSALS!r}")
    reserve = [
        {"verdict": verdict, "expected": {"status": status, "reason": reason}}
        for verdict, (status, reason) in SOL_RESERVE.items()
    ]
    import asyncio

    budget = []
    for name, policy, steps in SOL_BUDGET:
        asyncio.run(_replay_budget(si, policy, steps))
        budget.append({"name": name, "policy": policy, "steps": steps})
    policy = {
        "dailyMintCap": si.SOLANA_IDENTITY_DAILY_MINT_CAP,
        "maxMintAttemptsPerAddressPerDay": si.MAX_MINT_ATTEMPTS_PER_ADDRESS_PER_DAY,
        "mintHoldSeconds": si.MINT_HOLD_SECONDS,
        "networks": sorted(si.SOLANA_IDENTITY_NETWORKS),
    }
    if policy != {"dailyMintCap": 25, "maxMintAttemptsPerAddressPerDay": 3,
                  "mintHoldSeconds": 1800, "networks": ["solana", "solana-devnet"]}:
        raise Refused(f"policy moved: {policy!r}")
    return {
        "description": "ensure_solana_identity: the verdicts the TypeScript twin must reproduce. "
        "'addresses': validate_solana_address (32-44 base58 characters taken as given, 32 "
        "bytes, not zero). 'lookup': GET /identity/<network>/owner/<recipient> for recipient "
        "= 'recipient' on 'solana'; only 'absent' lets a mint out; body null = an answer that "
        "is not a JSON object; only the STRING balance '0' is a verdict. 'register': POST "
        "/register for that recipient, answers as x402-rs 2.51.2 can give them; releasesSlot "
        "= the answer proves nothing reached the chain (the slot is given back), retryInCall "
        "= it may be sent again in the same call; a facilitator code that is not lower_snake "
        "becomes 'facilitator_error'; a 408 / 429 / 503 releases only with the facilitator's "
        "mark (a lower_snake errorCode, mint.errorCode, code or reason, or mint.status "
        "'not_minted'); owners compare exactly (base58 is case-sensitive). 'transport': no "
        "HTTP answer, by meaning. 'reserve': what "
        "a MintBudget refusal makes of the call. 'budget': the rules of InMemoryMintBudget, "
        "step by step on a fresh one (reserve answers 'expect'; keep / release name their "
        "request by reservedAt; usedToday counts one network). 'policy': dailyMintCap is the "
        "owner's decision of 2026-10-27 (per UTC day and network); maxMintAttemptsPerAddressPerDay "
        "(in any 24 hours) and mintHoldSeconds are Execution Market's claim-table values.",
        "recipient": SOL_OWNER,
        "policy": policy,
        "addresses": {"valid": SOL_VALID, "rejected": rejected},
        "lookup": lookup,
        "register": register,
        "transport": transport,
        "reserve": reserve,
        "budget": budget,
    }


def render() -> dict:
    relay = relay_vectors()
    payment = payment_vectors()
    return {
        "message-ref.json": ref_vectors(),
        "relayed-feedback.json": relay,
        "prepare-rejections.json": rejection_vectors(relay["cases"][0]),
        "payment-proof.json": payment,
        "evidence-document.json": evidence_vectors(payment["proof"]),
        "solana-identity.json": solana_identity_vectors(),
    }


def dump(value: Any) -> str:
    return json.dumps(value, indent=2, ensure_ascii=False) + "\n"


def main(argv: Any = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="exit 1 if a file would change")
    args = parser.parse_args(argv)
    try:
        files = {name: dump(value) for name, value in render().items()}
    except Refused as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    drift = []
    for name, text in files.items():
        path = OUT / name
        current = path.read_bytes().decode("utf-8") if path.exists() else None
        if current != text:
            drift.append(name)
            if not args.check:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(text.encode("utf-8"))
    if args.check and drift:
        print("drift: " + ", ".join(drift), file=sys.stderr)
        return 1
    for name in sorted(files):
        digest = (
            hashlib.sha256((OUT / name).read_bytes()).hexdigest() if (OUT / name).exists() else "-"
        )
        print(f"{digest}  tests/vectors/reputation/{name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
