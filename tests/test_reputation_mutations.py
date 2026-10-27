"""Each guard is the thing that refuses: remove it and the case goes through.

The repo's idiom (``tests/test_escrow_vectors.py::mutant``): a copy of the
module with one guard replaced, compiled from its source, and the case the
guard exists for run against it. Every test here asserts that the UNMUTATED
module refuses and the MUTANT does not, so a guard that stops guarding (or a
test that stops reaching it) turns this file red.

The spec names seven: low-s, the delegate allowlist, the paymentHash, the
outbox's idempotency, the breaker, the caps and the log redaction. The rest
are the money-and-reputation guards around them.
"""

from __future__ import annotations

import importlib
import importlib.util
import sys
from dataclasses import replace
from pathlib import Path
from types import ModuleType

import pytest

pytest.importorskip("eth_account")

from tests.reputation_doubles import (  # noqa: E402
    SUBJECT,
    Clock,
    Env,
    FakeChain,
    fixed_account,
    generous_caps,
    make_entry,
)
from tests.test_reputation_payment import CountingSigner  # noqa: E402
from tests.test_reputation_round3 import (  # noqa: E402
    TAMPERS,
    Tampering,
    absent_for_a_rotated_signer,
    another_chain_at_prepare,
    another_chain_at_start,
    deep_rpc_answer,
    forget_before_signing,
    ipfs_evidence,
    orphaned_safe_block,
    proven_expired,
    ref_window_in_a_tag,
    removed_log_payment,
    rotate_mid_flight,
)
from tests.test_reputation_signing import Lying, high_s, verified_for  # noqa: E402
from uvd_x402_sdk.reputation import (  # noqa: E402
    AccountSigner,
    EvidenceRejected,
    InMemoryRatingOutbox,
    PaymentPending,
    PaymentRejected,
    PreparationRejected,
    RpcError,
    SignatureRejected,
    validate_preparation,
)
from uvd_x402_sdk.reputation import evidence as evidence_module  # noqa: E402
from uvd_x402_sdk.reputation import submit as submit_module  # noqa: E402


def mutant(module_name: str, old: str, new: str) -> ModuleType:
    """A copy of ``uvd_x402_sdk.reputation.<module_name>`` with ``old`` -> ``new`` (once)."""
    original = importlib.import_module(f"uvd_x402_sdk.reputation.{module_name}")
    source = Path(original.__file__).read_text(encoding="utf-8")
    assert source.count(old) == 1, f"mutation site not found exactly once: {old!r}"
    spec = importlib.util.spec_from_loader(
        f"uvd_x402_sdk.reputation._mutant_{module_name}", loader=None
    )
    module = importlib.util.module_from_spec(spec)
    module.__package__ = "uvd_x402_sdk.reputation"
    module.__file__ = original.__file__
    # dataclasses resolve deferred annotations through sys.modules[cls.__module__].
    sys.modules[spec.name] = module
    exec(compile(source.replace(old, new), original.__file__, "exec"), module.__dict__)
    return module


# -- M1 low-s ---------------------------------------------------------------------
def test_m1_without_the_low_s_guard_a_malleable_signature_goes_out():
    account = fixed_account()
    verified = verified_for(account.address)
    with pytest.raises(SignatureRejected):
        importlib.import_module("uvd_x402_sdk.reputation.signing").sign_relayed_feedback(
            verified, Lying(account, high_s)
        )
    signing = mutant("signing", "    if s > _HALF_N:\n", "    if False:\n")
    assert signing.sign_relayed_feedback(verified, Lying(account, high_s))


# -- M2 the delegate allowlist ------------------------------------------------------
def test_m2_without_the_allowlist_a_foreign_delegate_validates():
    from tests.test_reputation_preparation import KH, NOW, prepare

    answer = prepare()
    answer["delegate"] = "0x" + "de" * 20
    with pytest.raises(PreparationRejected):
        validate_preparation(answer, KH, fixed_account().address, network="base", now=NOW)
    preparation = mutant(
        "preparation",
        "    if delegate is None or not _same_address(parsed.delegate, delegate):\n",
        "    if delegate is None:\n",
    )
    params = preparation.RatingParams(**vars(KH))  # the mutant's own class
    assert preparation.validate_preparation(
        answer, params, fixed_account().address, network="base", now=NOW
    )


def test_m2b_the_signer_rechecks_the_allowlist_of_a_hand_built_preparation():
    account = fixed_account()
    forged = replace(verified_for(account.address), delegate="0x" + "de" * 20)
    with pytest.raises(PreparationRejected):
        importlib.import_module("uvd_x402_sdk.reputation.signing").build_relay_authorization(
            forged, AccountSigner(account)
        )
    signing = mutant(
        "signing",
        "    if not prep.chain_id or allowed is None or allowed.lower() != prep.delegate.lower():\n",
        "    if False:\n",
    )
    assert signing.build_relay_authorization(forged, AccountSigner(account)) is not None


# -- M3 the paymentHash ---------------------------------------------------------------
def test_m3_a_payment_hash_over_lowercase_addresses_is_not_the_facilitators():
    import json

    captured = json.loads(
        json.loads(
            (
                Path(__file__).parent
                / "fixtures/facilitator-settle-2.40.0/settle_success_with_proof.json"
            ).read_text(encoding="utf-8")
        )["body"]
    )["proofOfPayment"]
    args = (
        captured["transactionHash"],
        captured["blockNumber"],
        captured["payer"],
        captured["payee"],
        int(captured["amount"]),
    )
    payment = importlib.import_module("uvd_x402_sdk.reputation.payment")
    assert payment.payment_hash(*args) == captured["paymentHash"]
    broken = mutant(
        "payment",
        '        + checksum(payer).encode("utf-8")\n',
        '        + payer.lower().encode("utf-8")\n',
    )
    assert broken.payment_hash(*args) != captured["paymentHash"]


# -- M4 idempotency -----------------------------------------------------------------------
def test_m4_without_the_unique_ref_a_second_enqueue_replaces_the_row():
    outbox_module = importlib.import_module("uvd_x402_sdk.reputation.outbox")
    box = outbox_module.InMemoryRatingOutbox()
    assert box.enqueue(make_entry(value=80)) and not box.enqueue(make_entry(value=95))
    broken = mutant(
        "outbox", "            if entry.ref in self._rows:\n                return False\n", ""
    )
    box = broken.InMemoryRatingOutbox()
    assert box.enqueue(make_entry(value=80)) and box.enqueue(make_entry(value=95))


def test_m4b_insert_or_replace_would_overwrite_a_row_in_flight(tmp_path):
    broken = mutant("outbox", "INSERT OR IGNORE INTO", "INSERT OR REPLACE INTO")
    box = broken.SQLiteRatingOutbox(str(tmp_path / "o.sqlite"))
    entry = make_entry()
    box.enqueue(entry)
    box.transition(entry.ref, "queued", "paying", started_at=1.0)
    assert box.enqueue(make_entry()) is True  # the re-enqueue wins...
    assert box.get(entry.ref).status == "queued"  # ...and a paid-for row is back to the start
    box.close()


def test_m4c_without_require_unset_a_second_signed_payment_overwrites_the_first():
    broken = mutant("outbox", "    for name in require_unset:\n", "    for name in ():\n")
    box = broken.InMemoryRatingOutbox()
    entry = make_entry()
    box.enqueue(entry)
    box.transition(entry.ref, "queued", "paying", started_at=1.0)
    box.transition(entry.ref, "paying", require_unset=("payment",), payment={"raw": "0xaa"})
    box.transition(entry.ref, "paying", require_unset=("payment",), payment={"raw": "0xbb"})
    assert box.get(entry.ref).payment == {"raw": "0xbb"}


# -- M5 never prepare twice -------------------------------------------------------------
async def test_m5_without_the_expiry_proof_a_live_signature_is_prepared_again(monkeypatch):
    async def prepares_with(module) -> int:
        env = Env(InMemoryRatingOutbox())
        env.own()
        env.worker.submit_options["max_relay_rounds"] = 3  # the opt-in path
        env.facilitator.submit_modes = ["400", "400"]
        entry = make_entry()
        env.outbox.enqueue(entry)
        monkeypatch.setattr(
            "uvd_x402_sdk.reputation.worker.submit_with_reconcile", module.submit_with_reconcile
        )
        await env.drive(entry.ref, until=("submitting",), ticks=5, step=1)
        env.clock.now += 120  # well inside the 900 s deadline
        await env.worker.tick()
        env.facilitator.submit_modes.clear()
        return len(env.facilitator.prepares)

    assert await prepares_with(submit_module) == 1
    broken = mutant(
        "submit",
        "        if header is not None and stamp is not None and stamp > deadline + self.expiry_margin:\n",
        "        if header is not None:\n",
    )
    assert await prepares_with(broken) > 1


async def test_m5b_trusting_empty_logs_instead_of_the_spent_nonce_rates_twice(monkeypatch):
    async def ratings_with(module) -> int:
        from tests.test_reputation_flow import ratings_on_chain

        env = Env(InMemoryRatingOutbox())
        env.own()
        env.worker.submit_options["max_relay_rounds"] = 3  # the opt-in path
        env.facilitator.submit_modes = ["lost"]
        env.chain.logs_lag, env.chain.logs_lag_calls = 1000, 10**6
        monkeypatch.setattr(
            "uvd_x402_sdk.reputation.worker.submit_with_reconcile", module.submit_with_reconcile
        )
        entry = make_entry()
        env.outbox.enqueue(entry)
        await env.drive(entry.ref, until=("submitting",), ticks=5, step=1)
        for _ in range(12):
            env.clock.now += 200
            await env.worker.tick()
        env.chain.logs_lag_calls = 0
        await env.drive(entry.ref, step=600)
        return len(ratings_on_chain(env))

    assert await ratings_with(submit_module) == 1
    broken = mutant("submit", "            return _int(used) == 0\n", "            return True\n")
    assert await ratings_with(broken) == 2


# -- M6 the breaker -------------------------------------------------------------------------
def test_m6_without_the_breaker_check_an_open_breaker_admits():
    governor_module = importlib.import_module("uvd_x402_sdk.reputation.governor")
    clock = Clock(1_790_000_000)
    gov = governor_module.Governor(generous_caps(), lambda: True, clock=clock)
    gov.breaker.trip("x", 600)
    assert not gov.admit(make_entry(), InMemoryRatingOutbox(), clock.now).allowed
    broken = mutant("governor", "        if self.breaker.is_open(clock):\n", "        if False:\n")
    gov = broken.Governor(broken.Caps(**vars(generous_caps())), lambda: True, clock=clock)
    gov.breaker.trip("x", 600)
    assert gov.admit(make_entry(), InMemoryRatingOutbox(), clock.now).allowed


# -- M7 the caps ------------------------------------------------------------------------------
@pytest.mark.parametrize(
    "cap,line",
    [
        (
            "per_subject_per_stream",
            '            ("cap_subject_stream", subject_stream.count >= caps.per_subject_per_stream),\n',
        ),
        ("lifetime", '            ("cap_lifetime", total.count >= caps.lifetime),\n'),
        (
            "per_subject_lifetime",
            '            ("cap_subject_lifetime", subject.count >= caps.per_subject_lifetime),\n',
        ),
    ],
)
def test_m7_an_off_by_one_cap_admits_one_rating_too_many(cap, line):
    outbox = InMemoryRatingOutbox()
    for index in range(3):
        entry = make_entry(("r", str(index)))
        outbox.enqueue(entry)
        outbox.transition(entry.ref, "queued", "paying", started_at=1_790_000_000.0)
    governor_module = importlib.import_module("uvd_x402_sdk.reputation.governor")
    clock = Clock(1_790_000_000)
    caps = generous_caps(**{cap: 3})
    assert (
        not governor_module.Governor(caps, lambda: True, clock=clock)
        .admit(make_entry(("n", "1")), outbox, clock.now)
        .allowed
    )
    broken = mutant("governor", line, line.replace(" >= ", " > "))
    assert (
        broken.Governor(broken.Caps(**vars(caps)), lambda: True, clock=clock)
        .admit(make_entry(("n", "1")), outbox, clock.now)
        .allowed
    )


# -- M8 log redaction ---------------------------------------------------------------------------
def test_m8_without_redaction_a_full_address_reaches_the_log():
    from uvd_x402_sdk.reputation import redact

    text = f"rater {SUBJECT}"
    assert SUBJECT[2:] not in redact(text)
    broken = mutant("_redact", "    text = _LONG_HEX.sub(_short, text)\n", "")
    assert SUBJECT in broken.redact(text)


# -- M9 persist before broadcast ------------------------------------------------------------------
async def test_m9_broadcasting_before_persisting_is_caught():
    payment = importlib.import_module("uvd_x402_sdk.reputation.payment")

    async def broadcasts_seen_at_persist(module) -> list:
        clock = Clock()
        chain = FakeChain(clock)
        seen = []
        await module.pay_and_prove(
            AccountSigner(fixed_account()),
            SUBJECT,
            1000,
            chain,
            max_amount=10_000,
            on_signed=lambda signed: seen.append(len(chain.broadcasts)),
            sleep=clock.sleep,
            clock=clock,
        )
        return seen

    assert await broadcasts_seen_at_persist(payment) == [0]
    broken = mutant(
        "payment",
        "        await _maybe_await(on_signed(payment))\n",
        "        await broadcast(rpc, payment)\n        await _maybe_await(on_signed(payment))\n",
    )
    assert await broadcasts_seen_at_persist(broken) == [1]


# -- M10 / M11 the evidence guards ------------------------------------------------------------------
def _evidence(module, **overrides):
    args = dict(
        network="base",
        agent_id=1,
        rater=fixed_account().address,
        subject=SUBJECT,
        value=90,
        tag1="t",
        tag2="u",
        endpoint="https://e.example",
        created_at=1,
        salt=b"s" * 32,
        private={"message": "una idea brillante para el stream"},
    )
    args.update(overrides)
    return module.evidence_document(**args)


def test_m10_without_the_key_guard_the_text_goes_public_under_a_key():
    with pytest.raises(EvidenceRejected):
        _evidence(evidence_module, context={"chatText": "hola"})
    broken = mutant("evidence", "            if is_private_key(key):\n", "            if False:\n")
    assert b"hola" in _evidence(broken, context={"chatText": "hola"}).body


def test_m11_without_the_leak_check_the_text_rides_in_a_tag():
    leaking = dict(tag2="u|una idea brillante para el stream")
    with pytest.raises(EvidenceRejected):
        _evidence(evidence_module, **leaking)
    broken = mutant(
        "evidence",
        "        if len(value) >= LEAK_CHECK_MIN_LENGTH and (value in free_text or escaped in free_text):\n",
        "        if False:\n",
    )
    assert b"una idea brillante" in _evidence(broken, **leaking).body


def test_m11b_a_case_sensitive_leak_check_misses_the_text_in_capitals():
    shouting = dict(tag2="u|UNA IDEA BRILLANTE PARA EL STREAM")
    with pytest.raises(EvidenceRejected):
        _evidence(evidence_module, **shouting)
    broken = mutant(
        "evidence",
        '    free_text = canonical_json([tag1, tag2, endpoint, issuer, context]).decode("utf-8").lower()\n',
        '    free_text = canonical_json([tag1, tag2, endpoint, issuer, context]).decode("utf-8")\n',
    )
    assert b"UNA IDEA BRILLANTE" in _evidence(broken, **shouting).body


# -- M12 the digest check ------------------------------------------------------------------------------
def test_m12_without_the_digest_check_a_foreign_digest_validates():
    from tests.test_reputation_preparation import KH, NOW, prepare

    answer = prepare()
    answer["digest"] = "0x" + "01" * 32
    with pytest.raises(PreparationRejected):
        validate_preparation(answer, KH, fixed_account().address, network="base", now=NOW)
    broken = mutant("preparation", "    if _bytes32(parsed.digest) != digest:\n", "    if False:\n")
    params = broken.RatingParams(**vars(KH))
    assert broken.validate_preparation(
        answer, params, fixed_account().address, network="base", now=NOW
    )


# -- M13 the owner check ----------------------------------------------------------------------------------
async def test_m13_without_the_owner_check_a_stranger_is_paid(monkeypatch):
    async def broadcasts_with(module) -> int:
        env = Env(InMemoryRatingOutbox())
        env.own(18896, "0x" + "33" * 20)  # someone else owns the agent
        if module is not None:
            env.worker.__class__ = module.ReputationWorker
        entry = make_entry()
        env.outbox.enqueue(entry)
        await env.drive(entry.ref, ticks=3)
        return len(env.chain.broadcasts)

    assert await broadcasts_with(None) == 0
    broken = mutant("worker", "        if not owner:\n", "        if False:\n")
    assert await broadcasts_with(broken) == 1


# -- M14 signature recovery ------------------------------------------------------------------------------
def test_m14_without_the_recovery_check_a_strangers_signature_goes_out():
    account, stranger = fixed_account(), fixed_account("22")

    def strangers(sig: bytes) -> str:
        return "0x" + bytes(stranger.unsafe_sign_hash(b"\x01" * 32).signature).hex()

    verified = verified_for(account.address)
    with pytest.raises(SignatureRejected):
        importlib.import_module("uvd_x402_sdk.reputation.signing").sign_relayed_feedback(
            verified, Lying(account, strangers)
        )
    broken = mutant("signing", "    if recovered.lower() != expected.lower():\n", "    if False:\n")
    assert broken.sign_relayed_feedback(verified, Lying(account, strangers))


# -- M15 no funds to a contract ------------------------------------------------------------------------------
async def test_m15_without_the_code_check_a_contract_is_paid():
    clock = Clock()
    chain = FakeChain(clock)
    chain.code[SUBJECT.lower()] = "0x6080604052"
    payment = importlib.import_module("uvd_x402_sdk.reputation.payment")
    with pytest.raises(PaymentRejected):
        await payment.sign_payment(
            AccountSigner(fixed_account()), SUBJECT, 1000, chain, max_amount=10_000
        )
    broken = mutant(
        "payment", '    if code not in ("0x", "0x0") and not (', "    if False and not ("
    )
    assert await broken.sign_payment(
        AccountSigner(fixed_account()), SUBJECT, 1000, chain, max_amount=10_000
    )


# -- The third review's guards (tests/test_reputation_round3.py runs the same scenarios) --
def _env() -> Env:
    env = Env(InMemoryRatingOutbox())
    env.own()
    return env


def _patch_submit(monkeypatch, module) -> None:
    """Make the worker use a mutant ``submit`` module."""
    monkeypatch.setattr("uvd_x402_sdk.reputation.worker.row_rater", module.row_rater)
    monkeypatch.setattr(
        "uvd_x402_sdk.reputation.worker.submit_with_reconcile", module.submit_with_reconcile
    )


# M16 a row is moved only by its rater
async def test_m16_without_the_rater_guard_a_rotated_key_moves_the_row(monkeypatch):
    codes, _, _, _ = await rotate_mid_flight(_env())
    assert codes == ["rater_mismatch"] * 12
    broken = mutant("submit", "    if entry.rater.lower() != address.lower():\n", "    if False:\n")
    _patch_submit(monkeypatch, broken)
    codes, before, after, _ = await rotate_mid_flight(_env())
    assert "rater_mismatch" not in codes and after.version != before.version


async def test_m16b_reading_the_signers_state_proves_a_landed_rating_absent():
    assert await absent_for_a_rotated_signer(_env()) is False
    broken = mutant(
        "submit",
        "        rater = str(entry.rater)\n        delegate = RELAY_DELEGATES",
        "        rater = signer_address(self.signer)\n        delegate = RELAY_DELEGATES",
    )
    assert await absent_for_a_rotated_signer(_env(), broken._Run) is True


# M17 a payee's code that is not hex
async def test_m17_a_null_code_read_taken_for_no_code_pays_a_contract():
    def contract_behind_a_null_read() -> FakeChain:
        chain = FakeChain(Clock())
        chain.code[SUBJECT.lower()] = "0x6080604052"
        chain.null_code_reads = 1
        return chain

    payment = importlib.import_module("uvd_x402_sdk.reputation.payment")
    with pytest.raises(PaymentPending):
        await payment.sign_payment(
            AccountSigner(fixed_account()),
            SUBJECT,
            1000,
            contract_behind_a_null_read(),
            max_amount=10_000,
        )
    broken = mutant(
        "payment",
        "    if not isinstance(code, str) or not _CODE_RE.fullmatch(code):\n"
        '        # A null (one node of a balanced RPC) is no answer, never "no code".\n'
        '        raise PaymentPending("rpc_unreadable", "eth_getCode is not hex")\n'
        "    code = code.lower()\n",
        '    code = str(code or "0x").lower()\n',
    )
    assert await broken.sign_payment(
        AccountSigner(fixed_account()),
        SUBJECT,
        1000,
        contract_behind_a_null_read(),
        max_amount=10_000,
    )


# M18 a new transfer needs somewhere to persist it
async def test_m18_without_the_on_signed_guard_a_transfer_is_signed_with_nowhere_to_keep_it():
    async def transfers_signed(module) -> int:
        clock = Clock()
        signer = CountingSigner(fixed_account())
        with pytest.raises(TypeError):
            await module.pay_and_prove(
                signer,
                SUBJECT,
                1000,
                FakeChain(clock),
                max_amount=10_000,
                sleep=clock.sleep,
                clock=clock,
            )
        return signer.transactions

    assert await transfers_signed(importlib.import_module("uvd_x402_sdk.reputation.payment")) == 0
    broken = mutant(
        "payment",
        "        if on_signed is None:\n"
        '            raise TypeError("pay_and_prove: on_signed is required to sign a new '
        'transfer")\n',
        "",
    )
    assert await transfers_signed(broken) == 1  # signed, then lost: nothing kept the bytes


# M19 / M20 !forgetme before the transfer is signed, and after it is paid
async def test_m19_without_the_forget_check_an_unsigned_row_pays_after_a_forgetme():
    env = _env()
    await forget_before_signing(env)
    assert env.chain.broadcasts == []
    broken = mutant(
        "worker",
        "            if entry.forget_requested:\n"
        '                return self._fail(entry, "forgotten")\n'
        "            ready = ",
        "            ready = ",
    )
    env = _env()
    env.worker.__class__ = broken.ReputationWorker
    await forget_before_signing(env)
    assert len(set(env.chain.broadcasts)) == 1


async def test_m20_without_the_forget_check_after_paying_the_evidence_is_published():
    async def published(env: Env) -> dict:
        env.chain.auto_mine = False
        entry = make_entry()
        env.outbox.enqueue(entry)
        await env.worker.tick()
        assert env.outbox.get(entry.ref).payment is not None
        env.outbox.forget("viewer-1")
        env.chain.auto_mine, env.chain.mempool = True, set()
        await env.drive(entry.ref)
        return env.served

    assert await published(_env()) == {}
    broken = mutant(
        "worker",
        "        if entry.forget_requested:\n"
        '            return self._fail(entry, "forgotten")\n'
        "        published = await self._publish(entry, proof)",
        "        published = await self._publish(entry, proof)",
    )
    env = _env()
    env.worker.__class__ = broken.ReputationWorker
    assert await published(env) != {}


# M21 Karma Hello's ipfs:// evidence
async def test_m21_an_http_only_uri_check_strands_a_paid_ipfs_row():
    assert (await ipfs_evidence(_env())).status == "confirmed"
    broken = mutant(
        "worker",
        r'_URI_RE = re.compile(r"^(?:https?|ipfs)://\S+$")',
        r'_URI_RE = re.compile(r"^https?://\S+$")',
    )
    env = _env()
    env.worker.__class__ = broken.ReputationWorker
    row = await ipfs_evidence(env)
    assert row.status == "paying" and row.error == "evidence_unpublished"


# M22 each field of the transaction the signer returns
SIGNED_TX_SITES = {
    "chainId": 'int(decoded.get("chainId", -1)) != tx["chainId"]',
    "nonce": 'int(decoded.get("nonce", -1)) != tx["nonce"]',
    "data": 'data_hex.lower() != tx["data"].lower()',
    "value": 'int(decoded.get("value", -1)) != 0',
    "gas": 'int(decoded.get("gas", -1)) != tx["gas"]',
    "maxFeePerGas": 'int(decoded.get("maxFeePerGas", -1)) != tx["maxFeePerGas"]',
    "maxPriorityFeePerGas": (
        'int(decoded.get("maxPriorityFeePerGas", -1)) != tx["maxPriorityFeePerGas"]'
    ),
    "accessList": 'tuple(decoded.get("accessList") or ()) != ()',
}


@pytest.mark.parametrize("field", sorted(SIGNED_TX_SITES))
async def test_m22_each_field_of_the_signers_transaction_is_what_refuses_it(field):
    assert set(SIGNED_TX_SITES) == set(TAMPERS)
    payment = importlib.import_module("uvd_x402_sdk.reputation.payment")
    tampering = Tampering(fixed_account(), TAMPERS[field])
    with pytest.raises(PaymentRejected):
        await payment.sign_payment(tampering, SUBJECT, 1000, FakeChain(Clock()), max_amount=10_000)
    broken = mutant("payment", SIGNED_TX_SITES[field], "False")
    signed = await broken.sign_payment(
        tampering, SUBJECT, 1000, FakeChain(Clock()), max_amount=10_000
    )
    assert signed.raw  # e.g. the amount times 1000, signed by a custodian and accepted


# M23 a removed log
async def test_m23_a_removed_transfer_log_proves_a_payment_without_its_guard():
    with pytest.raises(PaymentRejected):
        await removed_log_payment()
    broken = mutant("payment", 'if not log.get("removed")\n', "if True\n")
    assert (await removed_log_payment(broken.pay_and_prove)).amount == "1000"


# M24 requireCanonical
async def test_m24_state_read_at_an_orphaned_block_proves_a_landed_rating_absent(monkeypatch):
    row = await orphaned_safe_block(_env())
    assert row.status == "submitting" and row.error == "relay_state_unknown"
    broken = mutant("submit", '"requireCanonical": True', '"requireCanonical": False')
    _patch_submit(monkeypatch, broken)
    row = await orphaned_safe_block(_env())
    assert row.status == "failed" and row.error == "relay_expired_unrelayed"  # it had landed


# M25 a proven expiry is a human's decision
async def test_m25_without_the_rounds_check_a_proven_expiry_is_prepared_again(monkeypatch):
    env = _env()
    await proven_expired(env)
    assert len(env.facilitator.prepares) == 1
    broken = mutant(
        "submit",
        "                if entry.relay_rounds >= self.max_relay_rounds:\n",
        "                if False:\n",
    )
    _patch_submit(monkeypatch, broken)
    env = _env()
    await proven_expired(env)
    assert len(env.facilitator.prepares) > 1


# M26 the RPC's JSON
async def test_m26_an_unbounded_parse_takes_any_nesting_from_the_rpc():
    with pytest.raises(RpcError):
        await deep_rpc_answer()
    broken = mutant(
        "payment",
        "payload = loads_bounded(response.content, max_bytes=MAX_RPC_RESPONSE_BYTES)",
        "payload = response.json()",
    )
    assert isinstance(await deep_rpc_answer(broken.HttpJsonRpc), list)


# M27 / M28 the RPC's chain id, when a row starts and before a rating is signed
async def test_m27_without_the_chain_check_at_start_a_row_prepares_on_another_chain():
    env = _env()
    await another_chain_at_start(env)
    assert env.facilitator.prepares == []
    broken = mutant(
        "worker", "            await _check_chain(self.rpc, entry.network)\n", "            pass\n"
    )
    env = _env()
    env.worker.__class__ = broken.ReputationWorker
    await another_chain_at_start(env)
    assert len(env.facilitator.prepares) == 1 and env.facilitator.submits == []


async def test_m28_without_the_chain_check_at_prepare_a_rating_is_signed_on_another_chain(
    monkeypatch,
):
    env = _env()
    await another_chain_at_prepare(env)
    assert env.facilitator.submits == []
    broken = mutant(
        "submit", "        if _int(chain_id) != verified.chain_id:\n", "        if False:\n"
    )
    _patch_submit(monkeypatch, broken)
    env = _env()
    await another_chain_at_prepare(env)
    assert len(env.facilitator.submits) == 1


# M29 any 16 hex of the ref
async def test_m29_a_prefix_only_check_lets_the_middle_of_the_ref_on_chain():
    assert (await ref_window_in_a_tag(_env())).status == "local_only"
    broken = mutant(
        "worker",
        "for start in range(max(1, len(ref) - _REF_WINDOW + 1))",
        "for start in range(1)",
    )
    env = _env()
    env.worker.__class__ = broken.ReputationWorker
    assert (await ref_window_in_a_tag(env)).status != "local_only"
