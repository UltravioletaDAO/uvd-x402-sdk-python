"""Record the shared reputation vectors: ``tests/vectors/reputation/*.json``.

The CONTRACT between the Python module (``uvd_x402_sdk.reputation``) and its
TypeScript twin (``uvd-x402-sdk/reputation``): the same inputs must give the
same digest, the same signature and EIP-7702 authorization, the same refusal
code, the same evidence bytes and hash, the same ``paymentHash`` and the same
``message_ref``. The TypeScript worker consumes these files as they are.

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


def render() -> dict:
    relay = relay_vectors()
    payment = payment_vectors()
    return {
        "message-ref.json": ref_vectors(),
        "relayed-feedback.json": relay,
        "prepare-rejections.json": rejection_vectors(relay["cases"][0]),
        "payment-proof.json": payment,
        "evidence-document.json": evidence_vectors(payment["proof"]),
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
