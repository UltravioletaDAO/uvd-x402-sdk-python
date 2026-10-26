"""The second adversarial review of 0.101.0: each finding, reproduced and pinned.

R2-P1-A a ``null`` code read taken for "no code"; R2-P1-B the emergency
undelegation of a first rating; R2-P1-C ABA on the status compare-and-set;
R2-P2-A a row the facilitator refuses for good holding the rail; R2-P2-B a
``delegated=true`` that is not; R2-P2-C a failing ``eth_getLogs`` blocking the
expiry; R2-P2-D the semaphore between a transfer and a first authorization; and
the P3 the review listed. The scenarios run on the doubles of
``tests/reputation_doubles.py``; their mutants are in the PR's file-level run.
"""

from __future__ import annotations

import pytest

pytest.importorskip("eth_account")

from tests.reputation_doubles import (  # noqa: E402
    Env,
    fixed_account,
    make_entry,
)
from tests.test_reputation_flow import TestWithoutAnAnchorPayment, ratings_on_chain  # noqa: E402
from uvd_x402_sdk.reputation import (  # noqa: E402
    InMemoryRatingOutbox,
    OutboxConflict,
    SQLiteRatingOutbox,
    redact,
)
from uvd_x402_sdk.reputation.submit import _Run  # noqa: E402


@pytest.fixture
def env():
    environment = Env(InMemoryRatingOutbox())
    environment.own()
    yield environment
    environment.facilitator.assert_consumed()


async def _first_rating_landed_answer_lost(env, entry=None):
    """A first rating relayed (the authorization applied) whose answer was lost,
    with the logs node far behind: only state can tell it landed."""
    env.facilitator.submit_modes = ["lost"]
    env.chain.logs_lag, env.chain.logs_lag_calls = 10**6, 10**6
    entry = entry or make_entry()
    env.outbox.enqueue(entry)
    await env.drive(entry.ref, until=("submitting",), ticks=5, step=1)
    assert env.outbox.get(entry.ref).authorization is not None  # a first rating
    return entry


async def _past_the_deadline(env, ref, rounds=12):
    for _ in range(rounds):
        env.clock.now += 200
        await env.worker.tick()
    return env.outbox.get(ref)


async def _then_the_logs_catch_up(env, ref):
    env.chain.logs_lag_calls = 0
    env.chain.null_code_reads = env.chain.state_lag = 0
    return await env.drive(ref, step=600)


# -- R2-P1-A -------------------------------------------------------------------------
async def test_r2_p1_a_a_null_code_read_is_no_verdict(env):
    entry = await _first_rating_landed_answer_lost(env)
    env.chain.null_code_reads = 10**6
    row = await _past_the_deadline(env, entry.ref)
    assert row.status == "submitting" and row.error == "relay_state_unknown"
    assert len(env.facilitator.prepares) == 1
    assert (await _then_the_logs_catch_up(env, entry.ref)).status == "confirmed"
    assert len(ratings_on_chain(env)) == 1


async def test_r2_p1_a_state_is_read_at_the_block_not_from_a_lagging_head(env):
    """The node answering state is behind the relay: by hash it errors; at
    "latest" it would answer from before the rating (no code, nonce unbumped)."""
    entry = await _first_rating_landed_answer_lost(env)
    env.chain.state_lag = 10**4
    row = await _past_the_deadline(env, entry.ref)
    assert row.status == "submitting" and row.error == "relay_state_unknown"
    assert len(env.facilitator.prepares) == 1
    assert (await _then_the_logs_catch_up(env, entry.ref)).status == "confirmed"
    assert len(ratings_on_chain(env)) == 1


# -- R2-P1-B -------------------------------------------------------------------------
async def test_r2_p1_b_an_undelegated_first_rating_is_no_verdict_and_never_redelegated(env):
    entry = await _first_rating_landed_answer_lost(env)
    env.chain.undelegate(env.account.address)  # the plan's emergency exit (2)
    row = await _past_the_deadline(env, entry.ref)
    assert row.status == "submitting" and row.error == "relay_state_unknown"
    assert len(env.facilitator.prepares) == 1  # never prepared, never re-delegated
    assert (await _then_the_logs_catch_up(env, entry.ref)).status == "confirmed"
    assert len(ratings_on_chain(env)) == 1


async def test_r2_p2_b_a_stalled_unknown_is_failed_for_a_human_and_frees_the_rail():
    env = Env(InMemoryRatingOutbox())
    env.own()
    env.worker.submit_options["unknown_after"] = 3000.0
    entry = await _first_rating_landed_answer_lost(env)
    env.chain.undelegate(env.account.address)
    row = await _past_the_deadline(env, entry.ref, rounds=30)
    assert row.status == "failed" and row.error == "relay_state_unknown"
    assert len(env.facilitator.prepares) == 1
    env.chain.logs_lag_calls = 0
    submits = len(env.facilitator.submits)
    second = make_entry(("s", "2"))
    env.outbox.enqueue(second)
    row = await env.drive(second.ref, until=("paid",), ticks=10, step=60)
    # The rail moved (paid), and the undelegated rater is not delegated again
    # behind the operator's back: the facilitator says "delegated", the chain
    # says no code, nothing is signed.
    assert row.status == "paid" and row.error == "delegation_state_changed"
    assert len(env.facilitator.submits) == submits
    env.facilitator.assert_consumed()


# -- R2-P1-C -------------------------------------------------------------------------
@pytest.mark.parametrize("kind", ["memory", "sqlite"])
async def test_r2_p1_c_a_stale_worker_cannot_overwrite_a_newer_submitting(kind, tmp_path):
    outbox = (
        InMemoryRatingOutbox() if kind == "memory" else SQLiteRatingOutbox(str(tmp_path / "o.db"))
    )
    env = Env(outbox)
    env.own()
    env.facilitator.submit_modes = ["400"]
    entry = make_entry()
    env.outbox.enqueue(entry)
    await env.drive(entry.ref, until=("submitting",), ticks=5, step=1)
    stale = env.outbox.get(entry.ref)  # W2 reads submitting(n1)...
    # ... and while its RPC is slow, W1 expires n1, prepares n2 and n2 lands.
    env.clock.now = stale.deadline + 200
    row = await env.drive(entry.ref)
    assert row.status == "confirmed" and len(env.facilitator.prepares) == 2
    # W2 now decides about n1 ("never landed", which is true) and writes.
    run = _Run(
        env.outbox,
        env.relay,
        env.signer,
        env.chain,
        env.breaker,
        2,
        env.clock.now,
        expiry_margin=60.0,
        scan_window=2000,
        max_windows=5,
        delays=(15.0,),
        expiry_block_tag="safe",
        unknown_after=86400.0,
        max_relay_rounds=3,
        max_prepare_refusals=3,
    )
    with pytest.raises(OutboxConflict):
        run.move(stale, "paid", nonce=None, error="relay_expired_unrelayed")
    assert env.outbox.get(entry.ref).status == "confirmed"
    assert len(ratings_on_chain(env)) == 1
    env.facilitator.assert_consumed()
    if kind == "sqlite":
        outbox.close()


@pytest.mark.parametrize("kind", ["memory", "sqlite"])
def test_r2_p1_c_expect_version_refuses_a_stale_copy_with_the_same_status(kind, tmp_path):
    """The ABA case reduced: same status, the row written in between."""
    outbox = (
        InMemoryRatingOutbox() if kind == "memory" else SQLiteRatingOutbox(str(tmp_path / "v.db"))
    )
    entry = make_entry()
    outbox.enqueue(entry)
    outbox.transition(entry.ref, "queued", "paying", started_at=1.0)
    stale = outbox.get(entry.ref)
    outbox.transition(entry.ref, "paying", attempts=3)  # same status, version moved
    with pytest.raises(OutboxConflict) as caught:
        outbox.transition(entry.ref, "paying", "paid", expect_version=stale.version, proof={"x": 1})
    assert caught.value.code == "version_changed"
    assert outbox.get(entry.ref).status == "paying"
    if kind == "sqlite":
        outbox.close()


def test_r2_p1_c_the_sql_update_compares_the_version_not_only_the_status(tmp_path):
    """Same status, newer version: the stale reader's UPDATE must match no row."""
    path = str(tmp_path / "o.sqlite")
    a, b = SQLiteRatingOutbox(path), SQLiteRatingOutbox(path)
    entry = make_entry()
    a.enqueue(entry)
    a.transition(entry.ref, "queued", "paying", started_at=1.0)
    stale = b.get(entry.ref)  # paying, version 1
    a.transition(entry.ref, "paying", attempts=5)  # still paying, version 2
    b._select = lambda where, params: [stale]
    with pytest.raises(OutboxConflict):
        b.transition(entry.ref, "paying", "paid", proof={"x": 1})
    assert a.get(entry.ref).status == "paying" and a.get(entry.ref).attempts == 5
    a.close(), b.close()


# -- R2-P2-A -------------------------------------------------------------------------
async def test_r2_p2_a_a_submit_refused_for_good_fails_the_row_and_frees_the_rail(env):
    env.facilitator.refuse_all_submits = True
    poison, good = make_entry(("s", "poison")), make_entry(("s", "good"))
    env.outbox.enqueue(poison)
    env.outbox.enqueue(good)
    row = await env.drive(poison.ref, ticks=200, step=600)
    assert row.status == "failed" and row.error == "relay_refused_repeatedly"
    assert len(env.facilitator.prepares) == 3  # DEFAULT_MAX_RELAY_ROUNDS
    env.facilitator.refuse_all_submits = False
    env.breaker._open_until = 0.0
    assert (await env.drive(good.ref, step=600)).status == "confirmed"


async def test_r2_p2_a_a_prepare_refused_for_good_fails_the_row(env):
    env.facilitator.prepare_modes = ["400", "400", "400"]
    entry = make_entry()
    env.outbox.enqueue(entry)
    row = await env.drive(entry.ref, ticks=200, step=600)
    assert row.status == "failed" and row.error == "prepare_refused_400"
    assert env.facilitator.submits == []


async def test_r2_p2_a_one_external_proof_rates_one_agent_once(env):
    claimed = await TestWithoutAnAnchorPayment()._settled_elsewhere(env)
    first, second = make_entry(("s", "1"), amount=0), make_entry(("s", "2"), amount=0)
    first.proof, second.proof = dict(claimed), dict(claimed)
    env.outbox.enqueue(first)
    assert (await env.drive(first.ref)).status == "confirmed"
    env.outbox.enqueue(second)
    row = await env.drive(second.ref)
    assert row.status == "failed" and row.error == "proof_reused"
    assert len(env.facilitator.prepares) == 1


# -- R2-P2-B -------------------------------------------------------------------------
async def test_r2_p2_b_a_delegated_claim_the_chain_denies_is_not_signed(env):
    def claims_delegated(answer):
        answer["delegated"], answer["accountNonce"] = True, None

    env.facilitator.tamper = claims_delegated
    entry = make_entry()
    env.outbox.enqueue(entry)
    await env.worker.tick()
    row = env.outbox.get(entry.ref)
    assert row.status == "paid" and row.error == "delegation_state_changed"
    assert row.signature is None and env.facilitator.submits == []
    env.facilitator.tamper = None
    assert (await env.drive(entry.ref)).status == "confirmed"


async def test_r2_p2_b_an_undelegated_claim_for_a_delegated_rater_is_not_signed(env):
    first, second = make_entry(("s", "1")), make_entry(("s", "2"))
    env.outbox.enqueue(first)
    assert (await env.drive(first.ref)).status == "confirmed"
    rater = env.account.address

    def claims_undelegated(answer):
        answer["delegated"] = False
        answer["accountNonce"] = env.chain.sender_nonce(rater, pending=True)

    env.facilitator.tamper = claims_undelegated
    env.outbox.enqueue(second)
    await env.worker.tick()
    row = env.outbox.get(second.ref)
    assert row.status == "paid" and row.error == "delegation_state_changed"
    assert len(env.facilitator.submits) == 1
    env.facilitator.tamper = None
    assert (await env.drive(second.ref)).status == "confirmed"


# -- R2-P2-C -------------------------------------------------------------------------
async def test_r2_p2_c_a_failing_get_logs_does_not_block_the_expiry(env):
    env.facilitator.refuse_all_submits = True
    entry = make_entry()
    env.outbox.enqueue(entry)
    await env.drive(entry.ref, until=("submitting",), ticks=5, step=1)
    env.chain.failures["eth_getLogs"] = 10**6  # e.g. a range limit the RPC enforces
    row = await env.drive(entry.ref, until=("paid",), step=300)
    assert row.status == "paid" and row.error == "relay_expired_unrelayed"
    env.facilitator.refuse_all_submits = False
    row = await env.drive(entry.ref, step=600)
    assert row.status == "confirmed" and len(env.facilitator.prepares) == 2
    assert len(ratings_on_chain(env)) == 1


async def test_r2_p2_c_inside_the_expiry_margin_nothing_is_prepared_again(env):
    env.facilitator.submit_modes = ["400", "400"]
    entry = make_entry()
    env.outbox.enqueue(entry)
    row = await env.drive(entry.ref, until=("submitting",), ticks=5, step=1)
    # The head is past deadline + margin (60 s); the safe block, 10 s behind, is
    # not: the expiry is decided at the safe block.
    env.clock.now = row.deadline + 65
    env.outbox.transition(entry.ref, "submitting", next_attempt=0.0)
    await env.worker.tick()
    row = env.outbox.get(entry.ref)
    assert row.status == "submitting" and row.error != "relay_expired_unrelayed"
    assert len(env.facilitator.prepares) == 1
    env.facilitator.submit_modes.clear()
    assert (await env.drive(entry.ref, step=300)).status == "confirmed"


# -- R2-P2-D -------------------------------------------------------------------------
async def test_r2_p2_d_no_transfer_is_signed_while_a_rating_is_in_flight(env):
    env.facilitator.submit_modes = ["400"]
    first, second = make_entry(("s", "1")), make_entry(("s", "2"))
    env.outbox.enqueue(first)
    await env.drive(first.ref, until=("submitting",), ticks=5, step=1)
    env.outbox.transition(first.ref, "submitting", next_attempt=10**12)
    env.outbox.enqueue(second)
    env.outbox.transition(second.ref, "queued", "paying", started_at=env.clock.now)
    broadcasts = len(env.chain.broadcasts)
    await env.worker.tick()
    row = env.outbox.get(second.ref)
    assert row.error == "rating_in_flight" and len(env.chain.broadcasts) == broadcasts
    env.outbox.transition(first.ref, "submitting", next_attempt=0.0)
    assert (await env.drive(first.ref, step=300)).status == "confirmed"
    assert (await env.drive(second.ref, step=300)).status == "confirmed"


async def test_r2_p2_d_no_first_authorization_is_signed_while_a_transfer_is_in_flight(env):
    env.facilitator.prepare_modes = ["503"]
    first = make_entry(("s", "1"))
    env.outbox.enqueue(first)
    await env.worker.tick()  # paid, prepare refused: nothing signed
    second = make_entry(("s", "2"))
    env.outbox.enqueue(second)
    env.outbox.transition(
        second.ref, "queued", "paying", started_at=env.clock.now, next_attempt=10**12
    )
    env.breaker._open_until = 0.0
    env.outbox.transition(first.ref, "paid", next_attempt=0.0)
    await env.worker.tick()
    row = env.outbox.get(first.ref)
    assert row.status == "paid" and row.error == "payment_in_flight" and row.signature is None
    env.outbox.transition(second.ref, "paying", next_attempt=0.0)
    assert (await env.drive(second.ref, step=60)).status == "confirmed"
    assert (await env.drive(first.ref, step=60)).status == "confirmed"


# -- P3 ------------------------------------------------------------------------------
def test_r2_p3_httpcore_server_hostname_is_redacted():
    assert "KEY" not in redact("start_tls.started server_hostname='KEY.quiknode.pro' timeout=5")


async def test_r2_p3_a_replaced_relay_transaction_is_found_by_its_event(env):
    entry = make_entry()
    env.outbox.enqueue(entry)
    await env.drive(entry.ref, until=("submitted",), ticks=5, step=1)
    real = env.outbox.get(entry.ref).rating_tx
    env.outbox.transition(entry.ref, "submitted", rating_tx="0x" + "ee" * 32, next_attempt=0.0)
    row = await env.drive(entry.ref)
    assert row.status == "confirmed" and row.rating_tx == real


async def test_r2_p3_a_prefix_of_the_ref_in_the_uri_or_a_tag_is_refused(env):
    original = env.worker.publish_evidence

    def prefixed(entry, proof):
        uri, digest = original(entry, proof)
        return f"https://credits.example/{entry.ref[:20]}/{digest[2:]}.json", digest

    env.worker.publish_evidence = prefixed
    entry = make_entry()
    env.outbox.enqueue(entry)
    await env.worker.tick()
    assert env.outbox.get(entry.ref).error == "evidence_unpublished"
    env.outbox.transition(entry.ref, "paying", "failed", error="test")
    tagged = make_entry(("s", "tag"))
    tagged.tag2 = "credits-roll|" + tagged.ref[:16]
    env.outbox.enqueue(tagged)
    row = await env.drive(tagged.ref, ticks=2)
    assert row.status == "local_only" and row.error == "public_field_identifies"
    assert fixed_account()  # the rater never signed for either
