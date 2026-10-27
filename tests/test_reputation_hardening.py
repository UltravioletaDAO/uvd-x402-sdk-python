"""What the adversarial review of 0.101.0 found, each one pinned.

Every test names the finding it closes (P1-1 ... P3). The two P1 and the P2
that move money have their scenario reproduced here with the doubles, and a
mutation of the guard in ``tests/test_reputation_mutations.py`` or in the
file-level run of the PR.
"""

from __future__ import annotations

import logging
import sqlite3

import httpx
import pytest

pytest.importorskip("eth_account")

from tests.reputation_doubles import (  # noqa: E402
    MIXED_SUBJECT,
    SUBJECT,
    Clock,
    Env,
    FakeChain,
    fixed_account,
    generous_caps,
    make_entry,
)
from uvd_x402_sdk.reputation import (  # noqa: E402
    AccountSigner,
    EvidenceRejected,
    Governor,
    HttpJsonRpc,
    InMemoryRatingOutbox,
    OutboxConflict,
    PaymentPending,
    RatingParams,
    SQLiteRatingOutbox,
    evidence_document,
    matching_feedback,
    prove_payment,
    shared_addresses,
    sign_payment,
    submit_with_reconcile,
)

VARIANTS = (MIXED_SUBJECT, MIXED_SUBJECT.lower(), "0x" + MIXED_SUBJECT[2:].upper())


@pytest.fixture(params=["memory", "sqlite"])
def outbox(request, tmp_path):
    if request.param == "memory":
        yield InMemoryRatingOutbox()
    else:
        box = SQLiteRatingOutbox(str(tmp_path / "o.sqlite"))
        yield box
        box.close()


@pytest.fixture
def env():
    environment = Env(InMemoryRatingOutbox())
    environment.own()
    yield environment
    environment.facilitator.assert_consumed()


# -- P1-2: the evidence URI is on-chain for ever ------------------------------------
async def test_p1_2_a_uri_that_carries_the_ref_or_the_viewer_is_refused(env):
    original = env.worker.publish_evidence

    def leaky(entry, proof):
        uri, digest = original(entry, proof)
        return f"https://credits.example/evidence/{entry.ref}.json", digest

    env.worker.publish_evidence = leaky
    entry = make_entry()
    env.outbox.enqueue(entry)
    await env.worker.tick()
    row = env.outbox.get(entry.ref)
    assert row.status == "paying" and row.error == "evidence_unpublished"
    assert env.facilitator.prepares == []

    def names_the_viewer(entry, proof):
        uri, digest = original(entry, proof)
        return f"https://credits.example/{entry.viewer_id}/{digest[2:]}.json", digest

    env.worker.publish_evidence = names_the_viewer
    long_viewer = make_entry(("s", "2"), viewer_id="40912334")
    env.outbox.transition(entry.ref, "paying", next_attempt=10**12)
    env.outbox.enqueue(long_viewer)
    # the first row still holds the semaphore; release it to let the second run
    env.outbox.transition(entry.ref, "paying", "failed", error="test")
    await env.worker.tick()
    assert env.outbox.get(long_viewer.ref).error == "evidence_unpublished"


# -- P2-1: a lagging receipt is not a consumed nonce (see test_reputation_payment) --


# -- P2-2: httpx writes the full RPC URL at INFO ---------------------------------------
async def test_p2_2_the_http_client_logs_no_url(caplog):
    from uvd_x402_sdk.reputation import _redact

    # The filter is process-wide and idempotent: take it off first, so it is
    # THIS constructor that has to put it back.
    for name in _redact._HTTP_LOGGERS:
        logging.getLogger(name).removeFilter(_redact._HTTP_FILTER)
    caplog.set_level(logging.DEBUG)
    url = "https://KEY456.quiknode.example/v2/KEY123/"
    answer = {"jsonrpc": "2.0", "id": 1, "result": "0x2105"}
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=answer))
    )
    assert await HttpJsonRpc(url, client=client).request("eth_chainId", []) == "0x2105"
    await client.aclose()
    text = "\n".join(record.getMessage() for record in caplog.records)
    assert any(record.name == "httpx" for record in caplog.records), "httpx logged nothing"
    assert "KEY123" not in text and "KEY456" not in text


# -- P2-3: a started row that has not signed its payment obeys the breaker ----------
async def test_p2_3_an_unsigned_paying_row_waits_for_an_open_breaker_and_bad_health(env):
    env.chain.base_fee = 10**12  # too expensive: the row starts, waits, signs nothing
    entry = make_entry()
    env.outbox.enqueue(entry)
    await env.worker.tick()
    row = env.outbox.get(entry.ref)
    assert row.status == "paying" and row.payment is None and row.error == "gas_too_expensive"
    env.chain.base_fee = 5_000_000
    env.breaker.trip("refused:delegate_mismatch", 3600)
    env.outbox.transition(entry.ref, "paying", next_attempt=0.0)
    await env.worker.tick()
    assert env.chain.broadcasts == []
    assert env.outbox.get(entry.ref).error == "breaker_open"
    env.breaker._open_until = 0.0
    env.governor.health = lambda: False
    env.outbox.transition(entry.ref, "paying", next_attempt=0.0)
    await env.worker.tick()
    assert env.chain.broadcasts == [] and env.outbox.get(entry.ref).error == "health_unhealthy"
    env.governor.health = lambda: True
    assert (await env.drive(entry.ref)).status == "confirmed"


# -- P2-4: addresses with letters --------------------------------------------------------
def test_p2_4_usage_counts_a_subject_in_any_case(outbox):
    for index, subject in enumerate(VARIANTS):
        entry = make_entry(("s", str(index)), subject=subject)
        outbox.enqueue(entry)
        outbox.transition(entry.ref, "queued", "paying", started_at=1.0)
    for subject in VARIANTS:
        assert outbox.usage(subject=subject).count == 3
        assert outbox.usage(subject=subject, stream_id="stream-1").count == 3


def test_p2_4_the_subject_caps_and_exclusions_ignore_case():
    outbox = InMemoryRatingOutbox()
    for index in range(2):
        entry = make_entry(("s", str(index)), subject=VARIANTS[index + 1])
        outbox.enqueue(entry)
        outbox.transition(entry.ref, "queued", "paying", started_at=1_790_000_000.0)
    clock = Clock(1_790_000_000)
    capped = Governor(generous_caps(per_subject_lifetime=2), lambda: True, clock=clock)
    decision = capped.admit(make_entry(("n", "1"), subject=MIXED_SUBJECT), outbox, clock.now)
    assert decision.reason == "cap_subject_lifetime"
    assert shared_addresses({"a": VARIANTS[0], "b": VARIANTS[2]}) == frozenset(
        {MIXED_SUBJECT.lower()}
    )
    for excluded in ({VARIANTS[2]}, shared_addresses({"a": VARIANTS[0], "b": VARIANTS[1]})):
        gov = Governor(generous_caps(), lambda: True, excluded=excluded, clock=clock)
        assert (
            gov.admit(make_entry(subject=MIXED_SUBJECT), outbox, clock.now).reason
            == "subject_excluded"
        )
    seen = []
    gov = Governor(
        generous_caps(), lambda: True, excluded=lambda w: seen.append(w) or False, clock=clock
    )
    gov.admit(make_entry(subject=MIXED_SUBJECT), InMemoryRatingOutbox(), clock.now)
    assert seen == [MIXED_SUBJECT.lower()]  # the predicate gets one spelling


# -- P2-4: the SQL compare-and-set itself, not the Python pre-check -------------------------
def _stale(box: SQLiteRatingOutbox, rows: list):
    box._select = lambda where, params: [rows[0]]  # what this process read before


def test_p2_4_the_sql_update_refuses_a_stale_status(tmp_path):
    path = str(tmp_path / "o.sqlite")
    a, b = SQLiteRatingOutbox(path), SQLiteRatingOutbox(path)
    entry = make_entry()
    a.enqueue(entry)
    stale = b.get(entry.ref)
    a.transition(entry.ref, "queued", "paying", started_at=1.0)
    _stale(b, [stale])
    with pytest.raises(OutboxConflict) as caught:
        b.transition(entry.ref, "queued", "paying", started_at=2.0)
    assert caught.value.code == "status_changed"
    assert a.get(entry.ref).started_at == 1.0
    a.close(), b.close()


def test_p2_4_the_sql_update_refuses_a_second_signed_payment(tmp_path):
    path = str(tmp_path / "o.sqlite")
    a, b = SQLiteRatingOutbox(path), SQLiteRatingOutbox(path)
    entry = make_entry()
    a.enqueue(entry)
    a.transition(entry.ref, "queued", "paying", started_at=1.0)
    stale = b.get(entry.ref)  # payment still empty here
    a.transition(entry.ref, "paying", require_unset=("payment",), payment={"raw": "0xaa"})
    _stale(b, [stale])
    with pytest.raises(OutboxConflict):
        b.transition(entry.ref, "paying", require_unset=("payment",), payment={"raw": "0xbb"})
    assert a.get(entry.ref).payment == {"raw": "0xaa"}
    a.close(), b.close()


# -- P2-4: reconciliation and payment guards that had no test ---------------------------------
PARAMS = RatingParams(
    agent_id=18896,
    value=90,
    tag1="credits_roll:idea",
    tag2="credits-roll|moment",
    endpoint="https://www.twitch.tv/somechannel",
    feedback_uri="https://credits.example/evidence/x.json",
    feedback_hash="0x" + "5a" * 32,
)
REGISTRY = "0x8004BAa17C55a88189AE136b182e5fdA19dE9b63"


def _log(chain: FakeChain, **overrides):
    fields = dict(
        agent_id=PARAMS.agent_id,
        value=PARAMS.value,
        tag1=PARAMS.tag1,
        tag2=PARAMS.tag2,
        endpoint=PARAMS.endpoint,
        feedback_uri=PARAMS.feedback_uri,
        feedback_hash=PARAMS.feedback_hash,
    )
    fields.update(overrides)
    tx = chain.mine_feedback(fixed_account().address, feedback_index=7, **fields)
    return chain.txs[tx].logs[0]


@pytest.mark.parametrize(
    "override",
    [
        dict(value=91),
        dict(tag2="other"),
        dict(endpoint="x"),
        dict(feedback_uri="https://other"),
        dict(feedback_hash="0x" + "5b" * 32),
    ],
)
def test_p2_4_matching_feedback_reads_the_data_not_only_the_topics(override):
    chain = FakeChain(Clock())
    assert (
        matching_feedback(
            _log(chain), registry=REGISTRY, rater=fixed_account().address, params=PARAMS
        )
        == 7
    )
    assert (
        matching_feedback(
            _log(chain, **override), registry=REGISTRY, rater=fixed_account().address, params=PARAMS
        )
        is None
    )


def test_p2_4_matching_feedback_topics_registry_and_removed():
    chain = FakeChain(Clock())
    log = _log(chain)
    rater = fixed_account().address
    assert (
        matching_feedback(dict(log, removed=True), registry=REGISTRY, rater=rater, params=PARAMS)
        is None
    )
    assert matching_feedback(log, registry="0x" + "11" * 20, rater=rater, params=PARAMS) is None
    assert matching_feedback(log, registry=REGISTRY, rater=SUBJECT, params=PARAMS) is None
    topics = list(log["topics"])
    topics[3] = "0x" + "00" * 32
    assert (
        matching_feedback(dict(log, topics=topics), registry=REGISTRY, rater=rater, params=PARAMS)
        is None
    )


async def _paid(chain: FakeChain, clock: Clock):
    signer = AccountSigner(fixed_account())
    signed = await sign_payment(signer, SUBJECT, 1000, chain, max_amount=10_000)
    await chain.request("eth_sendRawTransaction", [signed.raw])
    clock.now += 10
    return signed


async def test_p2_4_a_block_hash_that_moved_is_no_verdict():
    clock = Clock()
    chain = FakeChain(clock)
    signed = await _paid(chain, clock)
    chain.moved_hashes[chain.txs[signed.transaction_hash].block] = "0x" + "99" * 32
    with pytest.raises(PaymentPending) as caught:
        await prove_payment(
            chain,
            network="base",
            transaction_hash=signed.transaction_hash,
            payer=signed.payer,
            payee=SUBJECT,
            amount=1000,
        )
    assert caught.value.code == "payment_reorg"


async def test_p2_4_a_receipt_for_another_transaction_is_no_verdict():
    clock = Clock()
    chain = FakeChain(clock)
    signed = await _paid(chain, clock)
    chain.receipt_hash_override[signed.transaction_hash] = "0x" + "77" * 32
    with pytest.raises(PaymentPending) as caught:
        await prove_payment(
            chain,
            network="base",
            transaction_hash=signed.transaction_hash,
            payer=signed.payer,
            payee=SUBJECT,
            amount=1000,
        )
    assert caught.value.code == "rpc_unreadable"


async def test_p2_4_a_rating_waits_for_its_confirmations(env):
    entry = make_entry()
    env.outbox.enqueue(entry)
    await env.drive(entry.ref, until=("submitted",), ticks=5, step=1)
    assert env.outbox.get(entry.ref).error == "relay_receipt_pending"
    env.clock.now += 2  # the relay's block exists: one confirmation of two
    env.outbox.transition(entry.ref, "submitted", next_attempt=0.0)
    await env.worker.tick()
    row = env.outbox.get(entry.ref)
    assert row.status == "submitted" and row.error == "relay_awaiting_confirmations"
    assert (await env.drive(entry.ref)).status == "confirmed"


async def test_p2_4_an_external_proof_that_paid_someone_else_is_refused(env):
    from tests.test_reputation_flow import TestWithoutAnAnchorPayment

    claimed = await TestWithoutAnAnchorPayment()._settled_elsewhere(env)
    entry = make_entry(amount=0)
    entry.proof = dict(claimed, payee="0x" + "44" * 20)
    env.outbox.enqueue(entry)
    row = await env.drive(entry.ref)
    assert row.status == "failed" and row.error == "payee_is_not_subject"


async def test_p2_4_a_flagged_paid_row_is_never_prepared(env):
    entry = make_entry(amount=0)
    env.outbox.enqueue(entry)
    env.outbox.transition(
        entry.ref,
        "queued",
        "paid",
        started_at=1.0,
        forget_requested=True,
        feedback_uri="https://credits.example/e.json",
        feedback_hash="0x" + "5a" * 32,
    )
    row = await submit_with_reconcile(
        env.outbox.get(entry.ref),
        outbox=env.outbox,
        relay=env.relay,
        signer=env.signer,
        rpc=env.chain,
        now=env.clock.now,
    )
    assert row.status == "failed" and row.error == "forgotten"
    assert env.facilitator.prepares == []


async def test_p2_4_nothing_is_resent_after_the_deadline(env):
    env.facilitator.submit_modes = ["400", "400"]
    entry = make_entry()
    env.outbox.enqueue(entry)
    row = await env.drive(entry.ref, until=("submitting",), ticks=5, step=1)
    env.clock.now = row.deadline + 10  # past the deadline, inside the expiry margin
    env.outbox.transition(entry.ref, "submitting", next_attempt=0.0)
    await env.worker.tick()
    assert len(env.facilitator.submits) == 1  # the refused one; nothing after the deadline
    env.facilitator.submit_modes.clear()
    row = await env.drive(entry.ref, step=300)
    # Proven expired: failed for a human (the default), never prepared again.
    assert row.status == "failed" and row.error == "relay_expired_unrelayed"
    assert len(env.facilitator.prepares) == 1 and len(env.facilitator.submits) == 1


async def test_p1_1_a_rater_undelegated_after_the_relay_is_no_verdict_not_a_re_prepare(env):
    first, second = make_entry(("s", "1")), make_entry(("s", "2"))
    env.outbox.enqueue(first)
    assert (await env.drive(first.ref)).status == "confirmed"
    env.facilitator.submit_modes = ["lost"]
    env.chain.logs_lag, env.chain.logs_lag_calls = 1000, 10**6
    env.outbox.enqueue(second)
    await env.drive(second.ref, until=("submitting",), ticks=5, step=1)
    # The operator's emergency exit: the rater points its account at address(0).
    env.chain.undelegate(env.account.address)
    for _ in range(12):
        env.clock.now += 200
        await env.worker.tick()
    row = env.outbox.get(second.ref)
    assert row.status == "submitting" and row.error == "relay_state_unknown"
    assert len(env.facilitator.prepares) == 2  # never prepared again
    env.chain.logs_lag_calls = 0
    assert (await env.drive(second.ref, step=600)).status == "confirmed"
    assert len(env.facilitator.prepares) == 2


async def test_p2_3_a_paid_row_does_not_prepare_while_the_breaker_is_open(env):
    env.facilitator.prepare_modes = ["503"]
    entry = make_entry()
    env.outbox.enqueue(entry)
    await env.worker.tick()
    assert env.breaker.is_open() and len(env.facilitator.prepares) == 1
    env.outbox.transition(entry.ref, "paid", next_attempt=0.0)
    await env.worker.tick()
    row = env.outbox.get(entry.ref)
    assert len(env.facilitator.prepares) == 1 and row.error.startswith("breaker_open")
    assert row.next_attempt == env.breaker.open_until
    assert (await env.drive(entry.ref, step=600)).status == "confirmed"


# -- P3: the 7702 nonce is the chain's, not the facilitator's word ---------------------------
async def test_p3_an_authorization_over_a_nonce_the_chain_does_not_hold_is_not_signed(env):
    def wrong_nonce(answer):
        if answer["accountNonce"] is not None:
            answer["accountNonce"] = answer["accountNonce"] + 5

    env.facilitator.tamper = wrong_nonce
    entry = make_entry()
    env.outbox.enqueue(entry)
    await env.worker.tick()
    row = env.outbox.get(entry.ref)
    assert row.status == "paid" and row.error == "account_nonce_moved"
    assert row.signature is None and env.facilitator.submits == []
    env.facilitator.tamper = None
    assert (await env.drive(entry.ref)).status == "confirmed"


async def test_p3_repeated_submit_refusals_open_the_breaker(env):
    env.facilitator.submit_modes = ["400", "400"]
    entry = make_entry()
    env.outbox.enqueue(entry)
    await env.drive(entry.ref, until=("submitting",), ticks=5, step=1)
    env.clock.now += 20
    env.outbox.transition(entry.ref, "submitting", next_attempt=0.0)
    await env.worker.tick()
    assert env.breaker.is_open()
    env.facilitator.submit_modes.clear()
    assert (await env.drive(entry.ref, step=600)).status == "confirmed"


# -- P3: evidence leaks in other cases and languages -------------------------------------------
TEXT = "Una Idea Brillante Para El Stream"


def _evidence(**overrides):
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
        private={"message": TEXT, "login": "andre"},
    )
    args.update(overrides)
    return evidence_document(**args)


@pytest.mark.parametrize(
    "key", ["mensaje", "Texto", "autor", "nombre_usuario", "comentario", "correo"]
)
def test_p3_spanish_keys_are_private(key):
    with pytest.raises(EvidenceRejected) as caught:
        _evidence(context={key: "x"})
    assert caught.value.code == "private_field"


@pytest.mark.parametrize(
    "overrides",
    [
        dict(tag2="u|" + TEXT.upper()),
        dict(tag2="u|" + TEXT.lower()),
        dict(context={"note": "Andre said hi"}),
        dict(issuer={"name": "ANDRE"}),
    ],
)
def test_p3_leaks_are_found_in_any_case_and_short_logins_as_words(overrides):
    with pytest.raises(EvidenceRejected) as caught:
        _evidence(**overrides)
    assert caught.value.code == "private_value_leaked"


def test_p3_a_short_login_inside_a_word_or_a_tag_is_not_a_leak():
    _evidence(tag1="andrews:idea", context={"note": "sandrea"})


# -- P3: reprs, the consumer's SQLite file, the [all] extra ------------------------------------
def test_p3_reprs_carry_no_secret():
    entry = make_entry(viewer_id="viewer-secret-id")
    entry.signature, entry.payment = "0x" + "ab" * 65, {"raw": "0x" + "cd" * 80}
    text = repr(entry)
    assert "viewer-secret-id" not in text and "ab" * 65 not in text and SUBJECT not in text
    from uvd_x402_sdk.reputation import SignedPayment

    signed = SignedPayment(
        network="base",
        payer=SUBJECT,
        payee=SUBJECT,
        token=SUBJECT,
        amount=1,
        nonce=0,
        raw="0x" + "cd" * 80,
        transaction_hash="0x" + "ef" * 32,
    )
    assert "cd" * 80 not in repr(signed) and "ef" * 32 not in repr(signed)


def test_p3_the_outbox_leaves_the_consumers_user_version_alone(tmp_path):
    path = str(tmp_path / "app.sqlite")
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE moments (id TEXT)")
    db.execute("PRAGMA user_version = 5")
    db.commit()
    db.close()
    SQLiteRatingOutbox(path).close()
    SQLiteRatingOutbox(path).close()
    db = sqlite3.connect(path)
    assert db.execute("PRAGMA user_version").fetchone()[0] == 5
    db.execute("UPDATE reputation_outbox_meta SET value = '7' WHERE key = 'schema_version'")
    db.commit()
    db.close()
    with pytest.raises(RuntimeError):
        SQLiteRatingOutbox(path)


def test_p3_the_all_extra_does_not_force_eth_account_013_on_web3_6_users():
    import re
    from pathlib import Path

    text = (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(encoding="utf-8")
    all_line = re.search(r'"uvd-x402-sdk\[(.*?)\]"', text).group(1)
    assert "reputation" not in all_line.split(",")
