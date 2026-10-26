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
from tests.test_reputation_signing import Lying, high_s, verified_for  # noqa: E402
from uvd_x402_sdk.reputation import (  # noqa: E402
    AccountSigner,
    EvidenceRejected,
    InMemoryRatingOutbox,
    PaymentRejected,
    PreparationRejected,
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
        "        if on_signed is not None:\n            await _maybe_await(on_signed(payment))\n",
        "        await broadcast(rpc, payment)\n"
        "        if on_signed is not None:\n            await _maybe_await(on_signed(payment))\n",
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
