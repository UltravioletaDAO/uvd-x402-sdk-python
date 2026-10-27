"""The third adversarial review of 0.101.0: each finding, reproduced and pinned.

1 (P1) a row bound to its rater: a rotated key read another account's state and
"proved" a landed rating absent; 2 (P1) a null ``eth_getCode`` of the payee
taken for "no code"; 3 (P2) ``pay_and_prove`` without ``on_signed``; 4 (P2) a
``!forgetme`` on a started row that had not signed its transfer yet; 5 (P2)
Karma Hello's ``ipfs://`` evidence; 6 (P2) each field of the signer's
transaction, a removed log and ``requireCanonical``, each with a double that
models it; 7 (P2) a proven expiry is failed for a human unless the consumer
opts in; 8 (P3) the RPC's JSON bounded, its chain id read on every path, any
16 hex of the ref. The scenarios are functions so that
``tests/test_reputation_mutations.py`` runs the same ones against mutants.
"""

from __future__ import annotations

from typing import Callable, Optional

import httpx
import pytest

pytest.importorskip("eth_account")

from tests.reputation_doubles import (  # noqa: E402
    SUBJECT,
    USDC_BASE,
    Clock,
    Env,
    FakeChain,
    fixed_account,
    make_entry,
)
from tests.test_reputation_flow import ratings_on_chain  # noqa: E402
from tests.test_reputation_payment import CountingSigner  # noqa: E402
from uvd_x402_sdk.reputation import (  # noqa: E402
    AccountSigner,
    HttpJsonRpc,
    InMemoryRatingOutbox,
    PaymentPending,
    PaymentRejected,
    ReputationError,
    RpcError,
    SQLiteRatingOutbox,
    pay_and_prove,
    sign_payment,
    submit_with_reconcile,
)
from uvd_x402_sdk.reputation.submit import _Run  # noqa: E402


@pytest.fixture(params=["memory", "sqlite"])
def env(request, tmp_path):
    outbox = (
        InMemoryRatingOutbox()
        if request.param == "memory"
        else SQLiteRatingOutbox(str(tmp_path / "outbox.sqlite"))
    )
    environment = Env(outbox)
    environment.own()
    yield environment
    environment.facilitator.assert_consumed()
    if request.param == "sqlite":
        outbox.close()


class Counting(AccountSigner):
    """A rater key that counts every signature it is asked for."""

    def __init__(self, account) -> None:
        super().__init__(account)
        self.signatures = 0

    def sign_hash(self, digest: bytes) -> str:
        self.signatures += 1
        return super().sign_hash(digest)

    def sign_typed_data(self, typed_data: dict) -> str:
        self.signatures += 1
        return super().sign_typed_data(typed_data)

    def sign_transaction(self, tx: dict) -> str:
        self.signatures += 1
        return super().sign_transaction(tx)


def _run(env: Env, signer) -> _Run:
    return _Run(
        env.outbox,
        env.relay,
        signer,
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
        max_relay_rounds=1,
        max_prepare_refusals=3,
    )


# -- 1 (P1) a row is moved only by its rater -----------------------------------------
async def first_rating_in_flight(env: Env, *, amount: int = 0):
    """A first rating relayed (it landed) whose answer was lost, with the logs
    node far behind: only the rater's state can tell it landed."""
    env.facilitator.submit_modes = ["lost"]
    env.chain.logs_lag, env.chain.logs_lag_calls = 10**6, 10**6
    entry = make_entry(amount=amount)
    env.outbox.enqueue(entry)
    await env.drive(entry.ref, until=("submitting",), ticks=5, step=1)
    row = env.outbox.get(entry.ref)
    assert row.status == "submitting" and row.authorization is not None
    return row


async def rotate_mid_flight(env: Env) -> tuple:
    """The operator rotates the rater's key with that rating in flight and the
    worker keeps ticking past the deadline: the code each tick raised (None if
    it raised nothing), the row before and after, the new key."""
    before = await first_rating_in_flight(env)
    rotated = Counting(fixed_account("22"))
    env.worker.signer = rotated
    codes = []
    for _ in range(12):
        env.clock.now += 200
        try:
            await env.worker.tick()
            codes.append(None)
        except ReputationError as exc:
            codes.append(exc.code)
    return codes, before, env.outbox.get(before.ref), rotated


async def test_a_rotated_rater_key_never_rates_a_moment_twice():
    env = Env(InMemoryRatingOutbox())
    env.own()
    original = env.worker.signer
    codes, before, after, rotated = await rotate_mid_flight(env)
    assert codes == ["rater_mismatch"] * 12
    # Refused before anything: no write, no signature, no read of the chain.
    assert after.version == before.version and after.status == "submitting"
    assert rotated.signatures == 0 and len(env.facilitator.prepares) == 1
    calls = len(env.chain.calls)
    with pytest.raises(ReputationError):
        await env.worker.tick()
    assert len(env.chain.calls) == calls
    # The row's key is back: the rating it already made is found, and only it.
    env.worker.signer = original
    env.chain.logs_lag_calls = 0
    row = await env.drive(before.ref, step=600)
    assert row.status == "confirmed"
    assert len(ratings_on_chain(env)) == 1 and len(env.facilitator.prepares) == 1


async def rotate_while_paying(env: Env) -> tuple:
    """The key is rotated while a row waits in ``paying`` with nothing signed
    (a gas spike): only the worker's own check stands between the new key and
    a transfer. The code the next tick raised, the new key, the broadcasts."""
    env.chain.base_fee = 10**15
    entry = make_entry()
    env.outbox.enqueue(entry)
    await env.worker.tick()
    assert env.outbox.get(entry.ref).payment is None
    env.chain.base_fee = 5_000_000
    rotated = Counting(fixed_account("22"))
    env.worker.signer = rotated
    env.clock.now += 200
    try:
        await env.worker.tick()
        code = None
    except ReputationError as exc:
        code = exc.code
    return code, rotated, list(env.chain.broadcasts)


async def test_a_rotated_key_never_pays_for_a_row_another_key_started():
    env = Env(InMemoryRatingOutbox())
    env.own()
    code, rotated, broadcasts = await rotate_while_paying(env)
    assert code == "rater_mismatch"
    assert rotated.signatures == 0 and broadcasts == []


@pytest.mark.parametrize("cleared", [False, True])
async def test_submit_with_reconcile_refuses_a_signer_that_is_not_the_rows_rater(cleared):
    env = Env(InMemoryRatingOutbox())
    env.own()
    row = await first_rating_in_flight(env)
    signer = Counting(fixed_account("22"))
    if cleared:  # a started row that does not say who its rater is
        row = env.outbox.transition(row.ref, "submitting", rater=None)
        signer = env.signer
    calls = len(env.chain.calls)
    with pytest.raises(ReputationError) as caught:
        await submit_with_reconcile(
            row, outbox=env.outbox, relay=env.relay, signer=signer, rpc=env.chain
        )
    assert caught.value.code == ("rater_unknown" if cleared else "rater_mismatch")
    assert len(env.chain.calls) == calls and env.outbox.get(row.ref).version == row.version


async def absent_for_a_rotated_signer(env: Env, run_class=_Run) -> Optional[bool]:
    """``relay_absent`` of a landed first rating, asked with another key."""
    row = await first_rating_in_flight(env)
    env.clock.now = row.deadline + 600  # the safe block is past deadline + margin
    run = run_class(
        env.outbox,
        env.relay,
        Counting(fixed_account("22")),
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
        max_relay_rounds=1,
        max_prepare_refusals=3,
    )
    header = await env.chain.request("eth_getBlockByNumber", ["safe", False])
    return await run.relay_absent(row, header)


async def test_the_proof_of_absence_reads_the_rows_rater_never_the_signers():
    env = Env(InMemoryRatingOutbox())
    env.own()
    assert await absent_for_a_rotated_signer(env) is False  # it landed, at the row's rater


async def test_a_row_submitted_without_the_worker_records_its_rater_with_submitting():
    env = Env(InMemoryRatingOutbox())
    env.own()
    entry = make_entry(amount=0)
    env.outbox.enqueue(entry)
    env.outbox.transition(
        entry.ref,
        "queued",
        "paid",
        started_at=env.clock.now,
        feedback_uri="https://credits.example/evidence/doc.json",
        feedback_hash="0x" + "ab" * 32,
    )
    row = await submit_with_reconcile(
        env.outbox.get(entry.ref),
        outbox=env.outbox,
        relay=env.relay,
        signer=env.signer,
        rpc=env.chain,
        breaker=env.breaker,
        now=env.clock.now,
    )
    assert row.status == "submitted" and row.rater == env.account.address


async def test_the_rater_is_recorded_when_the_row_starts_and_survives_a_restart(tmp_path):
    path = str(tmp_path / "outbox.sqlite")
    env = Env(SQLiteRatingOutbox(path))
    env.own()
    env.chain.auto_mine = False  # stays paying
    entry = make_entry()
    env.outbox.enqueue(entry)
    await env.worker.tick()
    assert env.outbox.get(entry.ref).rater == env.account.address
    env.outbox.close()
    reborn = SQLiteRatingOutbox(path)
    assert reborn.get(entry.ref).rater == env.account.address
    reborn.close()


# -- 2 (P1) a null eth_getCode of the payee is no answer ---------------------------------
@pytest.mark.parametrize("answer", [None, "", 0, False, "0xzz", "6080604052"])
async def test_a_code_read_that_is_not_hex_never_pays_a_contract(answer):
    clock = Clock()
    chain = FakeChain(clock)
    signer = CountingSigner(fixed_account())
    chain.code[SUBJECT.lower()] = "0x6080604052"  # the payee is a contract
    real = chain._eth_getCode
    chain._eth_getCode = lambda address, tag: (
        answer if address.lower() == SUBJECT.lower() else real(address, tag)
    )
    with pytest.raises(PaymentPending) as caught:
        await sign_payment(signer, SUBJECT, 1000, chain, max_amount=10_000)
    assert caught.value.code == "rpc_unreadable" and signer.transactions == 0


async def test_a_null_code_read_of_the_payee_never_pays_a_contract():
    clock = Clock()
    chain = FakeChain(clock)
    signer = CountingSigner(fixed_account())
    chain.code[SUBJECT.lower()] = "0x6080604052"
    chain.null_code_reads = 1  # one node of a balanced RPC answers null
    with pytest.raises(PaymentPending) as caught:
        await sign_payment(signer, SUBJECT, 1000, chain, max_amount=10_000)
    assert caught.value.code == "rpc_unreadable" and signer.transactions == 0


async def test_end_to_end_a_null_code_read_never_pays_a_contract_owner():
    env = Env(InMemoryRatingOutbox())
    env.own()
    env.chain.code[SUBJECT.lower()] = "0x6080604052"
    env.chain.null_code_reads = 1
    entry = make_entry()
    env.outbox.enqueue(entry)
    row = await env.drive(entry.ref, step=50)
    assert env.chain.broadcasts == []
    assert row.status == "failed" and row.error == "payee_is_contract"


# -- 3 (P2) a new transfer is signed only with somewhere to persist it ---------------------
async def test_pay_and_prove_without_on_signed_never_pays_twice():
    clock = Clock()
    chain = FakeChain(clock)
    signer = CountingSigner(fixed_account())
    with pytest.raises(TypeError):
        await pay_and_prove(
            signer, SUBJECT, 1000, chain, max_amount=10_000, sleep=clock.sleep, clock=clock
        )
    assert signer.transactions == 0 and chain.broadcasts == []
    # The retry the docstring promises: what on_signed kept, re-sent as is.
    kept: list = []
    chain.receipts_lag_calls, chain.tx_by_hash_lags = 10**6, False  # mined, receipts behind
    with pytest.raises(PaymentPending):
        await pay_and_prove(
            signer,
            SUBJECT,
            1000,
            chain,
            max_amount=10_000,
            on_signed=kept.append,
            wait_seconds=20,
            sleep=clock.sleep,
            clock=clock,
        )
    chain.receipts_lag_calls = 0
    await pay_and_prove(
        signer,
        SUBJECT,
        1000,
        chain,
        max_amount=10_000,
        signed=kept[0],
        wait_seconds=60,
        sleep=clock.sleep,
        clock=clock,
    )
    transfers = [t for t in chain.txs.values() if t.nonce >= 0]
    assert len(transfers) == 1 and signer.transactions == 1


# -- 4 (P2) !forgetme before the transfer is signed ------------------------------------------
async def forget_before_signing(env: Env):
    """The row started and waits in paying with nothing signed (a gas spike);
    the viewer asks to be forgotten; gas comes back."""
    env.chain.base_fee = 10**15
    entry = make_entry()
    env.outbox.enqueue(entry)
    await env.worker.tick()
    row = env.outbox.get(entry.ref)
    assert row.status == "paying" and row.payment is None, (row.status, row.error)
    assert env.chain.broadcasts == []
    assert env.outbox.forget("viewer-1").in_flight == 1  # flagged, not deleted: it started
    env.chain.base_fee = 5_000_000
    return await env.drive(entry.ref, step=200)


async def test_forget_on_an_unsigned_paying_row_never_pays(env):
    row = await forget_before_signing(env)
    assert env.chain.broadcasts == []  # no transfer to the viewer's wallet, no new record
    assert row.status == "failed" and row.error == "forgotten"
    assert env.served == {} and env.facilitator.prepares == []


# -- 5 (P2) Karma Hello's evidence lives on IPFS ---------------------------------------------
KH_URI = "ipfs://bafkrei" + "a" * 52  # ipfs_store.py: "ipfs://" + cid


async def ipfs_evidence(env: Env):
    env.worker.publish_evidence = lambda entry, proof: (KH_URI, "0x" + "ab" * 32)
    entry = make_entry()
    env.outbox.enqueue(entry)
    return await env.drive(entry.ref, step=50, ticks=20)


async def test_karma_hellos_ipfs_evidence_uri_is_accepted():
    env = Env(InMemoryRatingOutbox())
    env.own()
    row = await ipfs_evidence(env)
    assert row.status == "confirmed" and row.feedback_uri == KH_URI, (row.status, row.error)
    assert len(set(env.chain.broadcasts)) == 1 and len(ratings_on_chain(env)) == 1


# -- 6 (P2) what the signer returns, a removed log, requireCanonical ------------------------
def _amount_x1000(tx: dict) -> None:
    tx["data"] = tx["data"][:-64] + format(int(tx["data"][-64:], 16) * 1000, "064x")


#: One change per field ``_check_signed_transaction`` compares.
TAMPERS: dict = {
    "data": _amount_x1000,
    "gas": lambda tx: tx.update(gas=tx["gas"] + 1),
    "maxFeePerGas": lambda tx: tx.update(maxFeePerGas=tx["maxFeePerGas"] + 1),
    "maxPriorityFeePerGas": lambda tx: tx.update(
        maxPriorityFeePerGas=tx["maxPriorityFeePerGas"] + 1
    ),
    "value": lambda tx: tx.update(value=1),
    "nonce": lambda tx: tx.update(nonce=tx["nonce"] + 1),
    "chainId": lambda tx: tx.update(chainId=1),
    "accessList": lambda tx: tx.update(accessList=[{"address": USDC_BASE, "storageKeys": []}]),
}


class Tampering:
    """A remote signer that signs, with the rater's own key, a transaction
    other than the one it was handed."""

    def __init__(self, account, change: Callable[[dict], None]) -> None:
        self._account, self._change = account, change
        self.address = account.address

    def sign_transaction(self, tx: dict) -> str:
        tx = dict(tx)
        self._change(tx)
        return "0x" + bytes(self._account.sign_transaction(tx).raw_transaction).hex()


@pytest.mark.parametrize("field", sorted(TAMPERS))
async def test_a_signer_returning_another_transaction_is_refused_and_nothing_is_sent(field):
    clock = Clock()
    chain = FakeChain(clock)
    kept: list = []
    with pytest.raises(PaymentRejected) as caught:
        await pay_and_prove(
            Tampering(fixed_account(), TAMPERS[field]),
            SUBJECT,
            1000,
            chain,
            max_amount=10_000,
            on_signed=kept.append,
            sleep=clock.sleep,
            clock=clock,
        )
    assert caught.value.code == "payment_signature"
    assert kept == [] and chain.broadcasts == []


async def removed_log_payment(pay=pay_and_prove):
    """The transfer is mined, but the node reports its log as removed."""
    clock = Clock()
    chain = FakeChain(clock)
    return await pay(
        AccountSigner(fixed_account()),
        SUBJECT,
        1000,
        chain,
        max_amount=10_000,
        on_signed=lambda signed: chain.removed_logs.add(signed.transaction_hash),
        sleep=clock.sleep,
        clock=clock,
    )


async def test_a_removed_transfer_log_never_proves_a_payment():
    with pytest.raises(PaymentRejected) as caught:
        await removed_log_payment()
    assert caught.value.code == "payment_transfer_missing"


async def orphaned_safe_block(env: Env):
    """Past the deadline, ``safe`` is answered by a node on a fork a reorg
    dropped, whose state predates the relay (no delegation, nonce unbumped):
    only ``requireCanonical`` keeps that state from "proving" the landed
    rating absent."""
    row = await first_rating_in_flight(env, amount=1000)
    relay_block = max(t.block for t in env.chain.txs.values() if t.nonce < 0)
    env.chain.orphan_safe_from = relay_block - 1
    for _ in range(12):
        env.clock.now += 200
        await env.worker.tick()
    return env.outbox.get(row.ref)


async def test_state_at_an_orphaned_safe_block_is_no_verdict():
    env = Env(InMemoryRatingOutbox())
    env.own()
    row = await orphaned_safe_block(env)
    assert row.status == "submitting" and row.error == "relay_state_unknown"
    assert env.chain.orphans  # the double did serve the orphaned header
    env.chain.orphan_safe_from, env.chain.logs_lag_calls = None, 0
    row = await env.drive(row.ref, step=600)
    assert row.status == "confirmed" and len(ratings_on_chain(env)) == 1


# -- 7 (P2) a proven expiry is a human's decision ---------------------------------------------
async def proven_expired(env: Env):
    """The facilitator refuses every submit until the deadline is far behind."""
    env.facilitator.refuse_all_submits = True
    entry = make_entry()
    env.outbox.enqueue(entry)
    return await env.drive(entry.ref, ticks=60, step=300)


async def test_a_signature_proven_expired_is_failed_for_a_human_by_default():
    env = Env(InMemoryRatingOutbox())
    env.own()
    row = await proven_expired(env)
    assert row.status == "failed" and row.error == "relay_expired_unrelayed"
    assert len(env.facilitator.prepares) == 1  # never a second signature for the moment
    assert ratings_on_chain(env) == [] and len(set(env.chain.broadcasts)) == 1


# -- 8 (P3) the RPC's JSON, its chain id, the ref -----------------------------------------------
DEEP_ANSWER = '{"jsonrpc":"2.0","id":1,"result":' + "[" * 70 + "]" * 70 + "}"


async def deep_rpc_answer(rpc_class=HttpJsonRpc):
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, content=DEEP_ANSWER.encode())
        )
    )
    try:
        return await rpc_class("https://rpc.example/KEY", client=client).request(
            "eth_getLogs", [{}]
        )
    finally:
        await client.aclose()


async def test_an_rpc_answer_nested_past_the_ceiling_is_unreadable():
    with pytest.raises(RpcError) as caught:
        await deep_rpc_answer()
    assert caught.value.code == "rpc_unreadable"


async def another_chain_at_start(env: Env):
    """The RPC serves another chain; the row pays nothing (``amount == 0``),
    so no payment would ever read the chain id."""
    env.chain.chain_id = 1
    entry = make_entry(amount=0)
    env.outbox.enqueue(entry)
    await env.worker.tick()
    return env.outbox.get(entry.ref)


async def test_an_rpc_on_another_chain_starts_no_row():
    env = Env(InMemoryRatingOutbox())
    env.own()
    row = await another_chain_at_start(env)
    assert row.status == "queued" and row.error == "chain_mismatch"
    assert env.facilitator.prepares == [] and env.breaker.is_open()


async def another_chain_at_prepare(env: Env):
    """A paid row whose RPC is switched to another chain before it prepares."""
    env.facilitator.prepare_modes = ["503"]
    entry = make_entry(amount=0)
    env.outbox.enqueue(entry)
    await env.worker.tick()
    assert env.outbox.get(entry.ref).status == "paid"
    env.chain.chain_id = 1
    env.breaker._open_until = 0.0
    env.outbox.transition(entry.ref, "paid", next_attempt=0.0)
    await env.worker.tick()
    return env.outbox.get(entry.ref)


async def test_an_rpc_on_another_chain_signs_no_rating():
    env = Env(InMemoryRatingOutbox())
    env.own()
    row = await another_chain_at_prepare(env)
    assert row.status == "paid" and row.error == "chain_mismatch" and row.signature is None
    assert env.facilitator.submits == [] and env.breaker.is_open()


async def ref_window_in_a_tag(env: Env):
    entry = make_entry(("s", "window"))
    entry.tag2 = "credits-roll|" + entry.ref[20:36]  # not the prefix: 16 hex from the middle
    env.outbox.enqueue(entry)
    return await env.drive(entry.ref, ticks=2)


async def test_any_16_hex_of_the_ref_in_a_public_field_is_refused():
    env = Env(InMemoryRatingOutbox())
    env.own()
    row = await ref_window_in_a_tag(env)
    assert row.status == "local_only" and row.error == "public_field_identifies"
    assert env.chain.broadcasts == []
