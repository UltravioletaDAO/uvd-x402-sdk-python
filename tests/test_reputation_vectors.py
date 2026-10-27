"""The shared reputation vectors reproduce, and nobody edited them by hand.

``tests/vectors/reputation/*.json`` is the contract with the TypeScript twin
(it consumes them as they are). Written ONLY by
``scripts/reputation_vectors.py``, which cross-checks every value against an
independent implementation before writing. Their sha256 is pinned here: a
change to a file that does not come with a change to this table is an edit by
hand, or a drift of the module.
"""

from __future__ import annotations

import copy
import hashlib
import json
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

pytest.importorskip("eth_account")

from uvd_x402_sdk.erc8004 import ProofOfPayment  # noqa: E402
from uvd_x402_sdk.reputation import (  # noqa: E402
    MAX_MINT_ATTEMPTS_PER_ADDRESS_PER_DAY,
    MINT_HOLD_SECONDS,
    SOLANA_IDENTITY_DAILY_MINT_CAP,
    SOLANA_IDENTITY_NETWORKS,
    AccountSigner,
    EvidenceRejected,
    PreparationRejected,
    RatingParams,
    authorization_digest,
    build_relay_authorization,
    evidence_document,
    message_ref,
    payment_hash,
    recompute_payment_hash,
    relayed_feedback_digest,
    sign_relayed_feedback,
    solana_identity,
    validate_preparation,
    validate_solana_address,
)
from uvd_x402_sdk.reputation.payment import _transfer_calldata  # noqa: E402
from uvd_x402_sdk.reputation.preparation import domain_separator  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
VECTORS = REPO / "tests" / "vectors" / "reputation"

VECTOR_SHA256 = {
    "evidence-document.json": "aa2fa7fd2ac40e1eace9d71bb71f4092c051be7edc8e4b930286bff7f0dd2abb",
    "message-ref.json": "eeff15b79d887149bf995f4e157134c4513267387f18c7f2f942900c69b1dac3",
    "payment-proof.json": "5844804ffc4798a4760fa578098a70a759a9203b9fbe64098a44acdc55a7d71e",
    "prepare-rejections.json": "e957162d1a5b03e420ce04b45527d0afd3954b8c72c8a7876fc78c5ab21d26f0",
    "relayed-feedback.json": "68480ed21a2e340a76b55aaedc9fcf8b9acea11fc483e8c43ea55e98e4fd14ae",
    "solana-identity.json": "7419588c3e90560556dd3158f62b736210e4b49d46869d4ae72f24a0866dcd3c",
}


def load(name: str) -> dict:
    return json.loads((VECTORS / name).read_bytes().decode("utf-8"))


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


def signer_for(seed: str) -> AccountSigner:
    return AccountSigner.from_key("0x" + seed * 32)


def test_the_files_are_exactly_the_recorded_ones():
    on_disk = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in VECTORS.glob("*.json")}
    assert on_disk == VECTOR_SHA256


def test_the_generator_reproduces_every_file_byte_for_byte():
    result = subprocess.run(
        [sys.executable, str(REPO / "scripts" / "reputation_vectors.py"), "--check"],
        capture_output=True,
        text=True,
        timeout=300,
        env={**__import__("os").environ, "PYTHONUTF8": "1"},
    )
    assert result.returncode == 0, result.stderr


class TestMessageRef:
    @pytest.mark.parametrize("case", load("message-ref.json")["cases"], ids=lambda c: c["note"])
    def test_the_ref(self, case):
        assert message_ref(case["namespace"], *case["parts"]) == case["ref"]

    @pytest.mark.parametrize(
        "case", load("message-ref.json")["rejected"], ids=lambda c: c["reason"]
    )
    def test_the_refusal(self, case):
        with pytest.raises(ValueError):
            message_ref(case["namespace"], *case["parts"])


class TestRelayedFeedback:
    @pytest.mark.parametrize(
        "case", load("relayed-feedback.json")["cases"], ids=lambda c: c["name"]
    )
    def test_digest_typed_data_signature_and_authorization(self, case):
        signer = signer_for(case["signerSeedByte"])
        assert signer.get_address() == case["rater"]
        verified = validate_preparation(
            case["prepare"],
            rating(case["params"]),
            case["rater"],
            network=case["network"],
            now=case["now"],
        )
        expected = case["expected"]
        assert verified.digest == expected["digest"]
        assert verified.typed_data == expected["typedDataToSign"]
        assert sign_relayed_feedback(verified, signer) == expected["signature"]
        authorization = build_relay_authorization(verified, signer)
        if expected["authorization"] is None:
            assert authorization is None
        else:
            assert authorization.model_dump(by_alias=True) == expected["authorization"]
            assert (
                "0x"
                + authorization_digest(
                    verified.chain_id, verified.delegate, verified.account_nonce
                ).hex()
                == expected["authorizationDigest"]
            )

    def test_the_digest_of_the_deployed_contract(self):
        d = load("relayed-feedback.json")["deployedContract"]
        assert "0x" + domain_separator(d["chainId"], d["account"]).hex() == d["domainSeparator"]
        digest = relayed_feedback_digest(
            d["chainId"],
            d["account"],
            d["registry"],
            rating(d["params"]),
            d["deadline"],
            d["nonce"],
        )
        assert "0x" + digest.hex() == d["digest"]


def _apply(document: dict, ops: list) -> dict:
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


REJECTIONS = load("prepare-rejections.json")
REJECTION_BASE = next(
    c for c in load("relayed-feedback.json")["cases"] if c["name"] == REJECTIONS["base"]
)


class TestPrepareRejections:
    @pytest.mark.parametrize("case", REJECTIONS["rejected"], ids=lambda c: c["name"])
    def test_refused_with_its_code(self, case):
        base = REJECTION_BASE
        with pytest.raises(PreparationRejected) as caught:
            validate_preparation(
                _apply(base["prepare"], case["ops"]),
                rating(base["params"]),
                case.get("rater", base["rater"]),
                network=case.get("network", base["network"]),
                now=REJECTIONS["now"],
            )
        assert caught.value.code == case["code"]

    @pytest.mark.parametrize("case", REJECTIONS["accepted"], ids=lambda c: c["name"])
    def test_accepted(self, case):
        base = REJECTION_BASE
        validate_preparation(
            _apply(base["prepare"], case["ops"]),
            rating(base["params"]),
            base["rater"],
            network=base["network"],
            now=REJECTIONS["now"],
        )


class TestPayment:
    @pytest.mark.parametrize(
        "case", load("payment-proof.json")["paymentHash"], ids=lambda c: c["name"]
    )
    def test_payment_hash(self, case):
        assert (
            payment_hash(
                case["transactionHash"],
                case["blockNumber"],
                case["payer"],
                case["payee"],
                int(case["amount"]),
            )
            == case["paymentHash"]
        )

    def test_transfer_calldata(self):
        for case in load("payment-proof.json")["transferCalldata"]:
            from eth_utils import to_checksum_address

            assert (
                _transfer_calldata(to_checksum_address(case["payee"]), int(case["amount"]))
                == case["data"]
            )

    def test_the_proof_recomputes(self):
        proof = ProofOfPayment.model_validate(load("payment-proof.json")["proof"])
        assert recompute_payment_hash(proof) == proof.payment_hash


def _evidence(inputs: dict):
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


class TestEvidence:
    @pytest.mark.parametrize(
        "case", load("evidence-document.json")["cases"], ids=lambda c: c["name"]
    )
    def test_the_bytes_and_their_hash(self, case):
        built = _evidence(case["inputs"])
        assert built.body.decode("utf-8") == case["body"]
        assert built.content_hash == case["contentHash"]
        for value in case["inputs"]["private"].values():
            assert value not in case["body"]

    @pytest.mark.parametrize(
        "case", load("evidence-document.json")["rejected"], ids=lambda c: c["name"]
    )
    def test_the_refusal(self, case):
        with pytest.raises(EvidenceRejected) as caught:
            _evidence(case["inputs"])
        assert caught.value.code == case["code"]


SOLANA = load("solana-identity.json")


class TestSolanaIdentity:
    """The verdicts of ensure_solana_identity, read from the file the TS twin reads."""

    @pytest.mark.parametrize("value", SOLANA["addresses"]["valid"])
    def test_a_valid_address(self, value):
        assert validate_solana_address(value) == value

    @pytest.mark.parametrize(
        "case", SOLANA["addresses"]["rejected"], ids=lambda c: c["reason"]
    )
    def test_a_rejected_address(self, case):
        with pytest.raises(ValueError):
            validate_solana_address(case["value"])

    @pytest.mark.parametrize("case", SOLANA["lookup"], ids=lambda c: c["name"])
    def test_the_lookup_verdict(self, case):
        got = solana_identity.lookup_verdict(
            case["statusCode"], case["body"], SOLANA["recipient"], "solana"
        )
        expected = case["expected"]
        assert (got.state, got.reason, got.agent_id) == (
            expected["state"], expected["reason"], expected["agentId"]
        )

    @pytest.mark.parametrize("case", SOLANA["register"], ids=lambda c: c["name"])
    def test_the_register_verdict(self, case):
        got = solana_identity.register_verdict(
            case["statusCode"], case["body"], SOLANA["recipient"]
        )
        expected = case["expected"]
        assert {
            "status": got.status,
            "reason": got.reason,
            "releasesSlot": got.releases,
            "retryInCall": got.retry,
            "mintStatus": got.mint_status,
            "errorCode": got.error_code,
            "agentId": got.agent_id,
        } == expected

    # The Python reading of each transport meaning (the TS twin maps its own).
    TRANSPORT = {
        "connection_refused": httpx.ConnectError("x"),
        "connect_timeout": httpx.ConnectTimeout("x"),
        "pool_timeout": httpx.PoolTimeout("x"),
        "read_timeout": httpx.ReadTimeout("x"),
        "write_timeout": httpx.WriteTimeout("x"),
        "disconnected": httpx.RemoteProtocolError("x"),
        "proxy_error": httpx.ProxyError("x"),
    }

    @pytest.mark.parametrize("case", SOLANA["transport"], ids=lambda c: c["failure"])
    def test_the_transport_verdict(self, case):
        got = solana_identity._transport_verdict(self.TRANSPORT[case["failure"]])
        assert {
            "status": got.status,
            "reason": got.reason,
            "releasesSlot": got.releases,
            "retryInCall": got.retry,
        } == case["expected"]

    @pytest.mark.parametrize("case", SOLANA["budget"], ids=lambda c: c["name"])
    async def test_the_budget_rules(self, case):
        budget, policy = solana_identity.InMemoryMintBudget(), case["policy"]
        for step in case["steps"]:
            if step["op"] == "reserve":
                got = await budget.reserve(
                    step["key"],
                    now=step["now"],
                    daily_cap=policy["dailyCap"],
                    per_address_cap=policy["perAddressCap"],
                    hold_seconds=policy["holdSeconds"],
                )
                assert got == step["expect"], step
            elif step["op"] == "keep":
                await budget.keep(
                    step["key"],
                    reserved_at=step["reservedAt"],
                    now=step["now"],
                    hold_seconds=policy["holdSeconds"],
                )
            elif step["op"] == "release":
                await budget.release(step["key"], reserved_at=step["reservedAt"], now=step["now"])
            else:
                assert budget.used_today(step["now"], step["network"]) == step["expect"], step

    def test_the_reserve_mapping(self):
        assert {
            case["verdict"]: (case["expected"]["status"], case["expected"]["reason"])
            for case in SOLANA["reserve"]
        } == solana_identity._RESERVE_REFUSALS

    def test_the_policy(self):
        assert SOLANA["policy"] == {
            "dailyMintCap": SOLANA_IDENTITY_DAILY_MINT_CAP,
            "maxMintAttemptsPerAddressPerDay": MAX_MINT_ATTEMPTS_PER_ADDRESS_PER_DAY,
            "mintHoldSeconds": MINT_HOLD_SECONDS,
            "networks": sorted(SOLANA_IDENTITY_NETWORKS),
        }
