"""The whole rail, end to end, against doubles of the chain and the facilitator.

Run with the network closed and no real key (the doubles in
``tests/reputation_doubles.py``). Every scenario asserts the two things that
cost money or reputation when they go wrong: how many payments were
broadcast, and how many ratings landed on the chain.
"""

from __future__ import annotations

import pytest

pytest.importorskip("eth_account")

from tests.reputation_doubles import (  # noqa: E402
    OTHER_SUBJECT,
    SUBJECT,
    Env,
    fixed_account,
    generous_caps,
    make_entry,
)
from uvd_x402_sdk.erc8004 import ProofOfPayment  # noqa: E402
from uvd_x402_sdk.reputation import (  # noqa: E402
    InMemoryRatingOutbox,
    SQLiteRatingOutbox,
    content_hash,
    recompute_payment_hash,
    shared_addresses,
)
from uvd_x402_sdk.reputation.submit import NEW_FEEDBACK_TOPIC  # noqa: E402


@pytest.fixture(params=["memory", "sqlite"])
def outbox(request, tmp_path):
    if request.param == "memory":
        yield InMemoryRatingOutbox()
    else:
        box = SQLiteRatingOutbox(str(tmp_path / "outbox.sqlite"))
        yield box
        box.close()


@pytest.fixture
def env(outbox):
    environment = Env(outbox)
    environment.own()
    yield environment
    environment.facilitator.assert_consumed()


def ratings_on_chain(env: Env) -> list:
    return [
        log
        for tx in env.chain.txs.values()
        for log in tx.logs
        if log["topics"][0] == NEW_FEEDBACK_TOPIC
    ]


def payments_broadcast(env: Env) -> set:
    return {h for h in env.chain.broadcasts}


async def test_one_moment_is_paid_once_rated_once_and_confirmed(env):
    entry = make_entry(created_at=env.clock.now)
    assert env.outbox.enqueue(entry)
    row = await env.drive(entry.ref)

    assert row.status == "confirmed", row.error
    assert len(payments_broadcast(env)) == 1
    assert len(ratings_on_chain(env)) == 1
    assert len(env.facilitator.prepares) == 1 and len(env.facilitator.submits) == 1
    # The rater is the on-chain author (topic 2), never the facilitator.
    log = ratings_on_chain(env)[0]
    assert log["topics"][2] == "0x" + env.account.address.lower()[2:].rjust(64, "0")
    # The proof is built from the chain: block timestamp, paymentHash recomputed.
    proof = ProofOfPayment.model_validate(row.proof)
    assert proof.payer == env.account.address and proof.payee == SUBJECT
    assert proof.timestamp == env.chain.timestamp(proof.block_number)
    assert recompute_payment_hash(proof) == proof.payment_hash
    # The served evidence is what the chain committed to.
    assert content_hash(env.served[row.feedback_uri]) == row.feedback_hash
    # The first rating delegates the rater (EIP-7702); it was verified by the double.
    assert env.facilitator.submits[0]["authorization"]["address"].lower() == (
        "0x260d3d0258680aa458d0ebb8bcae8a2f68bf6163"
    )


async def test_the_second_rating_of_a_delegated_rater_carries_no_authorization(env):
    first, second = make_entry(("s", "1")), make_entry(("s", "2"))
    env.outbox.enqueue(first)
    env.outbox.enqueue(second)
    assert (await env.drive(first.ref)).status == "confirmed"
    assert (await env.drive(second.ref)).status == "confirmed"
    assert "authorization" in env.facilitator.submits[0]
    assert "authorization" not in env.facilitator.submits[1]
    assert len(ratings_on_chain(env)) == 2


async def test_double_enqueue_is_one_row_one_payment_one_rating(env):
    entry = make_entry()
    assert env.outbox.enqueue(entry) is True
    assert env.outbox.enqueue(make_entry()) is False  # same ref, a restart re-enqueueing it
    row = await env.drive(entry.ref)
    assert row.status == "confirmed"
    await env.drive(entry.ref, ticks=5)  # more ticks change nothing
    assert len(payments_broadcast(env)) == 1
    assert len(ratings_on_chain(env)) == 1


class Crash(BaseException):
    """A process dying: not an Exception, so nothing in the module catches it."""


async def test_a_restart_after_submitting_was_persisted_resends_the_same_nonce(tmp_path):
    path = str(tmp_path / "outbox.sqlite")
    env = Env(SQLiteRatingOutbox(path))
    env.own()
    entry = make_entry()
    env.outbox.enqueue(entry)
    original = env.relay.submit_relayed_feedback

    async def dies(*args, **kwargs):
        raise Crash()

    env.relay.submit_relayed_feedback = dies
    with pytest.raises(Crash):
        await env.drive(entry.ref)
    assert env.outbox.get(entry.ref).status == "submitting"
    nonce = env.outbox.get(entry.ref).nonce
    env.outbox.close()

    # A new process: a new outbox object on the same file; the chain and the
    # facilitator are the same world.
    reborn = SQLiteRatingOutbox(path)
    env.worker.outbox = env.outbox = reborn
    env.relay.submit_relayed_feedback = original
    row = await env.drive(entry.ref)
    assert row.status == "confirmed"
    assert len(env.facilitator.prepares) == 1  # never prepared again
    assert env.facilitator.submits[-1]["nonce"] == nonce
    assert len(ratings_on_chain(env)) == 1
    assert len(payments_broadcast(env)) == 1
    reborn.close()


async def test_a_restart_while_paying_rebroadcasts_the_same_transfer(tmp_path):
    path = str(tmp_path / "outbox.sqlite")
    env = Env(SQLiteRatingOutbox(path))
    env.own()
    env.chain.auto_mine = False  # broadcast, not mined: the process dies waiting
    entry = make_entry()
    env.outbox.enqueue(entry)
    await env.worker.tick()
    row = env.outbox.get(entry.ref)
    assert row.status == "paying" and row.payment is not None
    signed_hash = row.payment["transaction_hash"]
    env.outbox.close()

    env.chain.auto_mine = True
    env.chain.mempool.clear()  # the node forgot it (restart, eviction)
    reborn = SQLiteRatingOutbox(path)
    env.worker.outbox = reborn
    env.outbox = reborn
    row = await env.drive(entry.ref)
    assert row.status == "confirmed"
    assert set(env.chain.broadcasts) == {signed_hash}  # the same bytes, never a new transfer
    assert len(ratings_on_chain(env)) == 1
    reborn.close()


async def test_a_lost_submit_answer_reconciles_from_the_chain_without_preparing_again(env):
    env.facilitator.submit_modes = ["lost"]
    entry = make_entry()
    env.outbox.enqueue(entry)
    row = await env.drive(entry.ref)
    assert row.status == "confirmed"
    assert len(env.facilitator.prepares) == 1
    assert len(env.facilitator.submits) == 1  # found on-chain: nothing re-sent
    assert len(ratings_on_chain(env)) == 1


async def test_an_uncertain_200_reconciles_from_the_chain(env):
    env.facilitator.submit_modes = ["uncertain"]
    entry = make_entry()
    env.outbox.enqueue(entry)
    row = await env.drive(entry.ref)
    assert row.status == "confirmed"
    assert len(env.facilitator.prepares) == 1 and len(ratings_on_chain(env)) == 1


async def test_a_429_opens_the_breaker_until_retry_after_and_resends_the_same_nonce(env):
    env.facilitator.submit_modes = ["429"]
    entry = make_entry()
    env.outbox.enqueue(entry)
    await env.drive(entry.ref, until=("submitting",), ticks=10, step=1)
    assert env.breaker.is_open()
    assert 0 < env.breaker.open_until - env.clock.now <= 30
    row = await env.drive(entry.ref)
    assert row.status == "confirmed"
    assert len(env.facilitator.prepares) == 1
    assert env.facilitator.submits[0]["nonce"] == env.facilitator.submits[1]["nonce"]
    assert len(ratings_on_chain(env)) == 1


async def test_a_503_on_prepare_opens_the_breaker_and_no_new_rating_starts(env):
    env.facilitator.prepare_modes = ["503"]
    first, second = make_entry(("s", "1")), make_entry(("s", "2"))
    env.outbox.enqueue(first)
    await env.worker.tick()
    assert env.outbox.get(first.ref).status == "paid"
    assert env.breaker.is_open()
    env.outbox.enqueue(second)
    await env.worker.tick()  # the breaker is open: the paid row waits, the new one does not start
    assert env.outbox.get(second.ref).status == "queued"
    assert len(payments_broadcast(env)) == 1
    assert (await env.drive(first.ref, step=600)).status == "confirmed"


async def test_an_unrelayed_signature_is_prepared_again_only_once_its_deadline_passed(env):
    env.worker.submit_options["max_relay_rounds"] = 3  # the consumer opted in
    # Every submit refused until the deadline is behind the chain.
    env.facilitator.submit_modes = ["400"] * 3
    entry = make_entry()
    env.outbox.enqueue(entry)
    row = await env.drive(entry.ref, until=("submitting",), ticks=5, step=1)
    first_deadline = row.deadline
    for _ in range(3):
        env.clock.now += 200
        await env.worker.tick()
        current = env.outbox.get(entry.ref)
        if env.chain.timestamp(env.chain.latest()) <= first_deadline + 60:
            assert len(env.facilitator.prepares) == 1, "prepared again while the old one could land"
    row = await env.drive(entry.ref, step=300)
    assert row.status == "confirmed", current.error
    assert len(env.facilitator.prepares) == 2
    second = env.facilitator.prepares[1]
    assert second is not env.facilitator.prepares[0]
    assert len(ratings_on_chain(env)) == 1
    assert len(payments_broadcast(env)) == 1  # the payment is reused: it proves for 7 days


async def test_a_lagging_logs_node_past_the_deadline_never_causes_a_second_rating(env):
    """The refuter's P1: the relay landed, the answer was lost, and the node that
    answers eth_getLogs is far behind until after the deadline. An empty log
    answer is not absence: the spent nonce, read at a block, says it landed."""
    env.facilitator.submit_modes = ["lost"]
    env.chain.logs_lag, env.chain.logs_lag_calls = 1000, 10**6
    entry = make_entry()
    env.outbox.enqueue(entry)
    await env.drive(entry.ref, until=("submitting",), ticks=5, step=1)
    deadline = env.outbox.get(entry.ref).deadline
    for _ in range(12):  # well past deadline + margin, logs still "empty"
        env.clock.now += 200
        await env.worker.tick()
    row = env.outbox.get(entry.ref)
    assert env.chain.timestamp(env.chain.latest()) > deadline + 60
    assert row.status == "submitting" and row.error == "relay_landed_unindexed"
    assert len(env.facilitator.prepares) == 1
    env.chain.logs_lag_calls = 0  # the node caught up
    row = await env.drive(entry.ref, step=600)
    assert row.status == "confirmed"
    assert len(env.facilitator.prepares) == 1 and len(ratings_on_chain(env)) == 1


async def test_a_delegated_raters_unrelayed_rating_is_prepared_again_after_its_deadline(env):
    env.worker.submit_options["max_relay_rounds"] = 3  # the consumer opted in
    first, second = make_entry(("s", "1")), make_entry(("s", "2"))
    env.outbox.enqueue(first)
    assert (await env.drive(first.ref)).status == "confirmed"
    env.facilitator.submit_modes = ["400"] * 3
    env.outbox.enqueue(second)
    row = await env.drive(second.ref, step=300)
    assert row.status == "confirmed"
    assert len(env.facilitator.prepares) == 3  # first, second, second again
    assert len(ratings_on_chain(env)) == 2


async def test_a_submitting_row_before_its_deadline_is_never_prepared_again(env):
    env.facilitator.submit_modes = ["400", "400"]
    entry = make_entry()
    env.outbox.enqueue(entry)
    await env.drive(entry.ref, until=("submitting",), ticks=5, step=1)
    env.clock.now += 120
    await env.worker.tick()
    row = env.outbox.get(entry.ref)
    assert row.status == "submitting"
    assert len(env.facilitator.prepares) == 1
    assert len(env.facilitator.submits) == 2  # the same body re-sent
    assert env.facilitator.submits[0]["nonce"] == env.facilitator.submits[1]["nonce"]
    assert (await env.drive(entry.ref)).status == "confirmed"


async def test_a_prepare_the_module_will_not_sign_stops_everything(env):
    def foreign_delegate(answer):
        answer["delegate"] = "0x" + "de" * 20

    env.facilitator.tamper = foreign_delegate
    entry = make_entry()
    env.outbox.enqueue(entry)
    await env.worker.tick()
    row = env.outbox.get(entry.ref)
    assert row.status == "paid" and row.error == "refused:delegate_mismatch"
    assert row.signature is None and env.facilitator.submits == []
    assert env.breaker.is_open()


async def test_a_reverted_payment_fails_the_row_and_nothing_is_prepared(env):
    env.chain.revert_next = True
    entry = make_entry()
    env.outbox.enqueue(entry)
    row = await env.drive(entry.ref)
    assert row.status == "failed" and row.error == "payment_reverted"
    assert env.facilitator.prepares == []


async def test_a_subject_that_does_not_own_the_agent_is_never_paid(env):
    env.chain.owners[18896] = OTHER_SUBJECT
    entry = make_entry()
    env.outbox.enqueue(entry)
    row = await env.drive(entry.ref)
    assert row.status == "local_only" and row.error == "agent_owner_mismatch"
    assert env.chain.broadcasts == [] and env.facilitator.prepares == []


async def test_an_agent_that_does_not_exist_is_a_no_and_an_rpc_outage_is_a_wait(env):
    gone = make_entry(("s", "gone"), agent_id=999_999)  # ownerOf reverts: no such agent
    env.outbox.enqueue(gone)
    row = await env.drive(gone.ref, ticks=3)
    assert row.status == "local_only" and row.error == "agent_owner_mismatch"
    entry = make_entry(("s", "outage"))
    env.outbox.enqueue(entry)
    env.chain.failures["eth_call"] = 1
    await env.worker.tick()
    row = env.outbox.get(entry.ref)
    assert row.status == "queued" and row.error == "rpc_unavailable"
    assert env.chain.broadcasts == []
    assert (await env.drive(entry.ref)).status == "confirmed"


async def test_a_shared_address_is_excluded_for_every_account_that_claims_it(outbox):
    directory = {"viewer_a": SUBJECT, "viewer_b": SUBJECT.lower(), "viewer_c": OTHER_SUBJECT}
    env = Env(outbox, excluded=shared_addresses(directory))
    env.own()
    env.own(1, OTHER_SUBJECT)
    a = make_entry(("s", "a"), viewer_id="viewer_a")
    b = make_entry(("s", "b"), viewer_id="viewer_b")
    c = make_entry(("s", "c"), viewer_id="viewer_c", subject=OTHER_SUBJECT, agent_id=1)
    for entry in (a, b, c):
        env.outbox.enqueue(entry)
    assert (await env.drive(a.ref)).error == "subject_excluded"
    assert (await env.drive(b.ref)).error == "subject_excluded"
    assert (await env.drive(c.ref)).status == "confirmed"
    assert len(payments_broadcast(env)) == 1


async def test_the_caps_count_ratings_to_the_subject_across_viewers(outbox):
    env = Env(outbox, caps=generous_caps(per_subject_per_stream=1))
    env.own()
    first = make_entry(("s", "1"), viewer_id="login_a")
    second = make_entry(("s", "2"), viewer_id="login_b")  # another account, same wallet
    env.outbox.enqueue(first)
    env.outbox.enqueue(second)
    assert (await env.drive(first.ref)).status == "confirmed"
    row = await env.drive(second.ref)
    assert row.status == "local_only" and row.error == "cap_subject_stream"
    assert len(payments_broadcast(env)) == 1


async def test_no_new_row_starts_while_a_rating_is_in_flight(env):
    env.facilitator.submit_modes = ["400"]
    first, second = make_entry(("s", "1")), make_entry(("s", "2"))
    env.outbox.enqueue(first)
    await env.drive(first.ref, until=("submitting",), ticks=5, step=1)
    env.outbox.enqueue(second)
    env.outbox.transition(first.ref, "submitting", next_attempt=env.clock.now + 10**6)
    assert await env.worker.tick() is None
    assert env.outbox.get(second.ref).status == "queued" and len(env.chain.broadcasts) == 1
    env.outbox.transition(first.ref, "submitting", next_attempt=0.0)
    assert (await env.drive(first.ref)).status == "confirmed"
    assert (await env.drive(second.ref)).status == "confirmed"


class TestWithoutAnAnchorPayment:
    async def test_no_proof_at_all(self, env):
        entry = make_entry(amount=0)
        env.outbox.enqueue(entry)
        row = await env.drive(entry.ref)
        assert row.status == "confirmed" and row.proof is None
        assert env.chain.broadcasts == []
        assert "proof" not in env.facilitator.prepares[0]["feedback"]

    async def _settled_elsewhere(self, env, payer_signer=None):
        from uvd_x402_sdk.reputation import sign_payment

        signer = payer_signer or env.signer
        signed = await sign_payment(signer, SUBJECT, 30_000, env.chain, max_amount=10**6)
        await env.chain.request("eth_sendRawTransaction", [signed.raw])
        env.clock.now += 10
        block = env.chain.txs[signed.transaction_hash].block
        from uvd_x402_sdk.reputation import payment_hash

        return {
            "transactionHash": signed.transaction_hash,
            "blockNumber": block,
            "network": "base",
            "payer": signed.payer,
            "payee": SUBJECT,
            "amount": "30000",
            "token": signed.token,
            "timestamp": 1,  # a settlement's wall clock, not the block's
            "paymentHash": payment_hash(
                signed.transaction_hash, block, signed.payer, SUBJECT, 30_000
            ),
        }

    async def test_a_proof_made_elsewhere_is_rebuilt_from_the_chain(self, env):
        claimed = await self._settled_elsewhere(env)
        entry = make_entry(amount=0)
        entry.proof = claimed
        env.outbox.enqueue(entry)
        row = await env.drive(entry.ref)
        assert row.status == "confirmed"
        assert row.proof["timestamp"] == env.chain.timestamp(claimed["blockNumber"]) != 1
        assert row.proof["paymentHash"] == claimed["paymentHash"]
        assert len(env.chain.broadcasts) == 1  # the settlement's; the worker paid nothing

    async def test_a_proof_paid_by_someone_else_is_refused(self, env):
        from uvd_x402_sdk.reputation import AccountSigner

        claimed = await self._settled_elsewhere(env, AccountSigner(fixed_account("22")))
        entry = make_entry(amount=0)
        entry.proof = claimed
        env.outbox.enqueue(entry)
        row = await env.drive(entry.ref)
        assert row.status == "failed" and row.error == "payer_is_not_rater"
        assert env.facilitator.prepares == []

    async def test_a_proof_whose_transfer_is_not_on_chain_is_refused(self, env):
        claimed = await self._settled_elsewhere(env)
        claimed["amount"] = "31000"
        entry = make_entry(amount=0)
        entry.proof = claimed
        env.outbox.enqueue(entry)
        row = await env.drive(entry.ref)
        assert row.status == "failed" and row.error == "payment_transfer_missing"


class TestForget:
    async def test_rows_in_every_state(self, env):
        refs = {
            name: make_entry(("s", name)).ref
            for name in ("queued", "confirmed", "paid", "submitting")
        }
        env.outbox.enqueue(make_entry(("s", "confirmed")))
        assert (await env.drive(refs["confirmed"])).status == "confirmed"
        env.facilitator.prepare_modes = ["503"]
        env.outbox.enqueue(make_entry(("s", "paid")))
        await env.drive(refs["paid"], until=("paid",), ticks=5, step=1)
        # Park it, and let the next row through the breaker the 503 opened.
        env.outbox.transition(refs["paid"], "paid", next_attempt=env.clock.now + 10**6)
        env.breaker._open_until = 0.0
        env.facilitator.submit_modes = ["400"]
        env.outbox.enqueue(make_entry(("s", "submitting")))
        await env.drive(refs["submitting"], until=("submitting",), ticks=5, step=1)
        env.outbox.enqueue(make_entry(("s", "queued")))
        assert env.outbox.get(refs["queued"]).status == "queued"

        report = env.outbox.forget("viewer-1")
        assert (report.deleted, report.tombstoned, report.in_flight) == (1, 2, 1)
        assert env.outbox.get(refs["queued"]) is None
        confirmed = env.outbox.get(refs["confirmed"])
        assert (
            confirmed.viewer_id is None and confirmed.tag1 == "" and confirmed.status == "confirmed"
        )
        paid = env.outbox.get(refs["paid"])
        assert paid.status == "failed" and paid.error == "forgotten" and paid.viewer_id is None
        flagged = env.outbox.get(refs["submitting"])
        assert flagged.forget_requested and flagged.viewer_id is None
        # The tombstones keep counting: a second forget finds nothing, the caps still see 3.
        assert env.outbox.forget("viewer-1").deleted == 0
        assert env.outbox.usage(subject=SUBJECT).count == 3
        # The in-flight one cannot be recalled: it is driven to its verdict, then stripped.
        prepares = len(env.facilitator.prepares)
        settled = await env.drive(refs["submitting"])
        assert settled.status == "confirmed" and settled.viewer_id is None and settled.tag1 == ""
        assert len(env.facilitator.prepares) == prepares  # never re-prepared
        assert env.outbox.get(refs["paid"]).status == "failed"  # a forgotten paid row never submits

    async def test_a_row_forgotten_while_paying_is_never_rated(self, env):
        env.chain.auto_mine = False
        entry = make_entry()
        env.outbox.enqueue(entry)
        await env.worker.tick()
        assert env.outbox.get(entry.ref).status == "paying"
        report = env.outbox.forget("viewer-1")
        assert report.in_flight == 1
        env.chain.auto_mine = True
        env.chain.mempool.clear()
        row = await env.drive(entry.ref)
        assert row.status == "failed" and row.error == "forgotten"
        assert env.facilitator.prepares == [] and ratings_on_chain(env) == []
        assert env.served == {}  # paid, but its evidence is never published
