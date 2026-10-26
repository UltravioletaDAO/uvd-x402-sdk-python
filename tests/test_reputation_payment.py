"""The anchor payment: signed once, persisted before it is sent, proven from the chain.

Against :class:`tests.reputation_doubles.FakeChain` (no network). The
``paymentHash`` is pinned against the facilitator's own captured vector.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

pytest.importorskip("eth_account")

from tests.reputation_doubles import (  # noqa: E402
    SUBJECT,
    USDC_BASE,
    Clock,
    FakeChain,
    fixed_account,
)
from uvd_x402_sdk.reputation import (  # noqa: E402
    AccountSigner,
    GasPolicy,
    HttpJsonRpc,
    PaymentPending,
    PaymentRejected,
    RpcError,
    pay_and_prove,
    payment_hash,
    prove_payment,
    recompute_payment_hash,
    sign_payment,
)

REPO = Path(__file__).resolve().parents[1]


class CountingSigner(AccountSigner):
    def __init__(self, account) -> None:
        super().__init__(account)
        self.transactions = 0

    def sign_transaction(self, tx: dict) -> str:
        self.transactions += 1
        return super().sign_transaction(tx)


@pytest.fixture
def world():
    clock = Clock()
    chain = FakeChain(clock)
    return clock, chain, CountingSigner(fixed_account())


def test_the_payment_hash_is_the_facilitators():
    captured = json.loads(
        json.loads(
            (
                REPO / "tests/fixtures/facilitator-settle-2.40.0/settle_success_with_proof.json"
            ).read_text(encoding="utf-8")
        )["body"]
    )["proofOfPayment"]
    assert (
        payment_hash(
            captured["transactionHash"],
            captured["blockNumber"],
            captured["payer"],
            captured["payee"],
            int(captured["amount"]),
        )
        == captured["paymentHash"]
    )
    # Any case of the addresses: the facilitator re-displays them checksummed.
    assert (
        payment_hash(
            captured["transactionHash"],
            captured["blockNumber"],
            captured["payer"].lower(),
            captured["payee"].upper().replace("0X", "0x"),
            int(captured["amount"]),
        )
        == captured["paymentHash"]
    )


@pytest.mark.parametrize(
    "bad",
    [
        dict(transaction_hash="0x1234"),
        dict(block_number=-1),
        dict(block_number=2**64),
        dict(amount=-1),
        dict(amount=True),
        dict(payer="nope"),
    ],
)
def test_the_payment_hash_refuses_what_the_facilitator_cannot_parse(bad):
    args = dict(
        transaction_hash="0x" + "c3" * 32, block_number=1, payer=SUBJECT, payee=SUBJECT, amount=1
    )
    args.update(bad)
    with pytest.raises(ValueError):
        payment_hash(**args)


async def test_pay_and_prove_persists_before_it_broadcasts_and_proves_from_the_chain(world):
    clock, chain, signer = world
    seen_at_persist = []

    def persist(signed):
        seen_at_persist.append(list(chain.broadcasts))

    proof = await pay_and_prove(
        signer,
        SUBJECT,
        1000,
        chain,
        max_amount=10_000,
        on_signed=persist,
        sleep=clock.sleep,
        clock=clock,
    )
    assert seen_at_persist == [[]]  # persisted while nothing had been sent
    assert len(chain.broadcasts) == 1 and signer.transactions == 1
    assert proof.payer == fixed_account().address and proof.payee == SUBJECT
    assert proof.token == USDC_BASE and proof.amount == "1000" and proof.network == "base"
    assert proof.timestamp == chain.timestamp(proof.block_number)  # the block's, not a wall clock
    assert recompute_payment_hash(proof) == proof.payment_hash


async def test_a_failing_persist_sends_nothing(world):
    clock, chain, signer = world

    def persist(signed):
        raise RuntimeError("disk full")

    with pytest.raises(RuntimeError):
        await pay_and_prove(
            signer,
            SUBJECT,
            1000,
            chain,
            max_amount=10_000,
            on_signed=persist,
            sleep=clock.sleep,
            clock=clock,
        )
    assert chain.broadcasts == []


async def test_a_retry_resends_the_same_bytes_and_signs_nothing(world):
    clock, chain, signer = world
    chain.auto_mine = False
    kept = []
    with pytest.raises(PaymentPending):
        await pay_and_prove(
            signer,
            SUBJECT,
            1000,
            chain,
            max_amount=10_000,
            on_signed=kept.append,
            wait_seconds=5,
            sleep=clock.sleep,
            clock=clock,
        )
    chain.auto_mine, chain.mempool = True, set()
    proof = await pay_and_prove(
        signer,
        SUBJECT,
        1000,
        chain,
        max_amount=10_000,
        signed=kept[0].to_dict(),
        sleep=clock.sleep,
        clock=clock,
    )
    assert signer.transactions == 1
    assert set(chain.broadcasts) == {kept[0].transaction_hash}
    assert proof.transaction_hash == kept[0].transaction_hash


async def test_already_known_is_a_successful_resend(world):
    clock, chain, signer = world
    kept = []
    await pay_and_prove(
        signer,
        SUBJECT,
        1000,
        chain,
        max_amount=10_000,
        on_signed=kept.append,
        sleep=clock.sleep,
        clock=clock,
    )
    proof = await pay_and_prove(
        signer,
        SUBJECT,
        1000,
        chain,
        max_amount=10_000,
        signed=kept[0],
        sleep=clock.sleep,
        clock=clock,
    )
    assert proof.transaction_hash == kept[0].transaction_hash and signer.transactions == 1


async def test_a_persisted_payment_for_another_rating_is_refused(world):
    clock, chain, signer = world
    kept = []
    await pay_and_prove(
        signer,
        SUBJECT,
        1000,
        chain,
        max_amount=10_000,
        on_signed=kept.append,
        sleep=clock.sleep,
        clock=clock,
    )
    with pytest.raises(PaymentRejected) as caught:
        await pay_and_prove(
            signer,
            SUBJECT,
            2000,
            chain,
            max_amount=10_000,
            signed=kept[0],
            sleep=clock.sleep,
            clock=clock,
        )
    assert caught.value.code == "payment_mismatch"


async def test_a_nonce_the_chain_consumed_elsewhere_is_refused_never_resigned(world):
    clock, chain, signer = world
    chain.auto_mine = False
    kept = []
    with pytest.raises(PaymentPending):
        await pay_and_prove(
            signer,
            SUBJECT,
            1000,
            chain,
            max_amount=10_000,
            on_signed=kept.append,
            wait_seconds=4,
            sleep=clock.sleep,
            clock=clock,
        )
    chain.mempool.clear()
    chain.foreign_nonces[fixed_account().address.lower()] = 1  # another tx took nonce 0
    with pytest.raises(PaymentRejected) as caught:
        await pay_and_prove(
            signer,
            SUBJECT,
            1000,
            chain,
            max_amount=10_000,
            signed=kept[0],
            wait_seconds=30,
            sleep=clock.sleep,
            clock=clock,
        )
    assert caught.value.code == "payment_nonce_consumed"
    assert signer.transactions == 1


async def test_a_receipt_from_a_lagging_node_is_not_a_consumed_nonce(world):
    """The refuter's P2: the nonce from a node ahead, the receipt from one behind.
    The payment is ours and mined; the row must wait, not fail."""
    clock, chain, signer = world
    kept = []
    # Receipts answered by a node behind for 8 polls: polls 0-3 read one receipt
    # each (the payment is not 3 blocks deep yet, nothing else is read), polls 4-7
    # read two and all look "consumed" (the nonce 3 blocks deep is past the
    # payment's): 4 suspicious reads, one short of the verdict. Poll 8 proves.
    chain.receipts_lag_calls = 12
    proof = await pay_and_prove(
        signer,
        SUBJECT,
        1000,
        chain,
        max_amount=10_000,
        on_signed=kept.append,
        wait_seconds=60,
        sleep=clock.sleep,
        clock=clock,
    )
    assert proof.transaction_hash == kept[0].transaction_hash


async def test_a_payment_another_node_shows_mined_is_never_consumed(world):
    """Receipts from a node behind for a long time, but eth_getTransactionByHash
    from one up to date that shows the payment mined: never a verdict of
    "consumed", however many reads (mutation M21b)."""
    clock, chain, signer = world
    chain.receipts_lag_calls, chain.tx_by_hash_lags = 30, False
    proof = await pay_and_prove(
        signer,
        SUBJECT,
        1000,
        chain,
        max_amount=10_000,
        wait_seconds=120,
        sleep=clock.sleep,
        clock=clock,
    )
    assert proof.amount == "1000" and chain.receipts_lag_calls == 0


@pytest.mark.parametrize(
    "amount,max_amount,code",
    [
        (0, 10, "payment_amount"),
        (11, 10, "payment_amount"),
        (True, 10, "payment_amount"),
        (1.5, 10, "payment_amount"),
    ],
)
async def test_the_amount_is_whole_positive_and_under_its_ceiling(world, amount, max_amount, code):
    clock, chain, signer = world
    with pytest.raises(PaymentRejected) as caught:
        await sign_payment(signer, SUBJECT, amount, chain, max_amount=max_amount)
    assert caught.value.code == code and signer.transactions == 0


@pytest.mark.parametrize(
    "code_on_chain,allowed",
    [
        ("0x", True),
        ("0xef0100" + "ab" * 20, True),  # an EIP-7702 delegated EOA is still an EOA
        ("0x6080604052", False),  # a contract
        ("0xef0100" + "ab" * 19, False),  # not a well-formed designator
    ],
)
async def test_funds_never_go_to_a_contract(world, code_on_chain, allowed):
    clock, chain, signer = world
    chain.code[SUBJECT.lower()] = code_on_chain
    if allowed:
        assert (
            await sign_payment(signer, SUBJECT, 1000, chain, max_amount=10_000)
        ).payee == SUBJECT
    else:
        with pytest.raises(PaymentRejected) as caught:
            await sign_payment(signer, SUBJECT, 1000, chain, max_amount=10_000)
        assert caught.value.code == "payee_is_contract"


@pytest.mark.parametrize(
    "payee,code",
    [
        ("0x" + "00" * 20, "payee_invalid"),
        ("nope", "payee_invalid"),
        (None, "payee_invalid"),
    ],
)
async def test_the_payee_is_an_address(world, payee, code):
    clock, chain, signer = world
    with pytest.raises(PaymentRejected) as caught:
        await sign_payment(signer, payee, 1000, chain, max_amount=10_000)
    assert caught.value.code == code


async def test_a_rater_never_pays_itself(world):
    clock, chain, signer = world
    with pytest.raises(PaymentRejected) as caught:
        await sign_payment(signer, fixed_account().address.lower(), 1000, chain, max_amount=10_000)
    assert caught.value.code == "payee_is_payer"


async def test_the_gas_ceilings(world):
    clock, chain, signer = world
    chain.gas_estimate = 200_000
    with pytest.raises(PaymentRejected) as caught:
        await sign_payment(signer, SUBJECT, 1000, chain, max_amount=10_000)
    assert caught.value.code == "gas_ceiling"
    chain.gas_estimate = 60_000
    chain.base_fee = 10**12  # a spike: deferred, never paid
    with pytest.raises(PaymentPending) as pending:
        await sign_payment(signer, SUBJECT, 1000, chain, max_amount=10_000, gas=GasPolicy())
    assert pending.value.code == "gas_too_expensive" and signer.transactions == 0


async def test_a_signer_that_returns_another_transaction_is_refused(world):
    clock, chain, signer = world
    other = AccountSigner(fixed_account())

    class Substitutes:
        def get_address(self):
            return fixed_account().address

        def sign_transaction(self, tx):
            return other.sign_transaction(dict(tx, to=SUBJECT))

    with pytest.raises(PaymentRejected) as caught:
        await sign_payment(Substitutes(), SUBJECT, 1000, chain, max_amount=10_000)
    assert caught.value.code == "payment_signature"


async def test_the_same_transaction_signed_by_another_key_is_refused(world):
    """A custodian that signs exactly the requested transaction, but from another
    account: every field matches, only the sender does not (mutation M15d)."""
    clock, chain, signer = world
    stranger = AccountSigner(fixed_account("22"))

    class OtherKey:
        def get_address(self):
            return fixed_account().address

        def sign_transaction(self, tx):
            return stranger.sign_transaction(tx)

    with pytest.raises(PaymentRejected) as caught:
        await sign_payment(OtherKey(), SUBJECT, 1000, chain, max_amount=10_000)
    assert caught.value.code == "payment_signature"
    assert chain.broadcasts == []


async def test_the_rpc_must_serve_the_network(world):
    clock, chain, signer = world
    chain.chain_id = 1
    with pytest.raises(PaymentRejected) as caught:
        await sign_payment(signer, SUBJECT, 1000, chain, max_amount=10_000)
    assert caught.value.code == "chain_mismatch"


class TestProve:
    async def _paid(self, world, amount=1000):
        clock, chain, signer = world
        signed = await sign_payment(signer, SUBJECT, amount, chain, max_amount=10_000)
        await chain.request("eth_sendRawTransaction", [signed.raw])
        return signed

    async def test_no_receipt_yet_is_pending(self, world):
        clock, chain, _ = world
        signed = await self._paid(world)
        with pytest.raises(PaymentPending) as caught:
            await prove_payment(
                chain,
                network="base",
                transaction_hash=signed.transaction_hash,
                payer=signed.payer,
                payee=SUBJECT,
                amount=1000,
            )
        assert caught.value.code == "payment_pending"

    async def test_too_few_confirmations_is_pending(self, world):
        clock, chain, _ = world
        signed = await self._paid(world)
        clock.now += 2
        with pytest.raises(PaymentPending) as caught:
            await prove_payment(
                chain,
                network="base",
                transaction_hash=signed.transaction_hash,
                payer=signed.payer,
                payee=SUBJECT,
                amount=1000,
                confirmations=3,
            )
        assert caught.value.code == "payment_awaiting_confirmations"

    async def test_a_reverted_payment_is_refused(self, world):
        clock, chain, _ = world
        chain.revert_next = True
        signed = await self._paid(world)
        clock.now += 10
        with pytest.raises(PaymentRejected) as caught:
            await prove_payment(
                chain,
                network="base",
                transaction_hash=signed.transaction_hash,
                payer=signed.payer,
                payee=SUBJECT,
                amount=1000,
            )
        assert caught.value.code == "payment_reverted"

    @pytest.mark.parametrize("claim", [dict(amount=1001), dict(payee="0x" + "33" * 20)])
    async def test_a_transfer_that_is_not_exactly_the_claim_is_refused(self, world, claim):
        clock, chain, _ = world
        signed = await self._paid(world)
        clock.now += 10
        args = dict(
            transaction_hash=signed.transaction_hash, payer=signed.payer, payee=SUBJECT, amount=1000
        )
        args.update(claim)
        with pytest.raises(PaymentRejected) as caught:
            await prove_payment(chain, network="base", **args)
        assert caught.value.code == "payment_transfer_missing"

    async def test_an_unreadable_rpc_is_no_verdict(self, world):
        clock, chain, _ = world
        signed = await self._paid(world)
        clock.now += 10
        chain.failures["eth_getTransactionReceipt"] = 1
        with pytest.raises(PaymentPending) as caught:
            await prove_payment(
                chain,
                network="base",
                transaction_hash=signed.transaction_hash,
                payer=signed.payer,
                payee=SUBJECT,
                amount=1000,
            )
        assert caught.value.code == "rpc_unavailable"


class TestHttpJsonRpc:
    async def test_only_the_allowlisted_methods(self):
        rpc = HttpJsonRpc("https://rpc.example/KEY")
        for method in ("eth_sign", "eth_sendTransaction", "personal_sign", "eth_accounts"):
            with pytest.raises(RpcError) as caught:
                await rpc.request(method, [])
            assert caught.value.code == "rpc_method_not_allowed"

    async def test_429_is_retried_and_errors_never_carry_the_url(self):
        url = "https://secret-host.example/KEY123/"
        answers = [
            httpx.Response(429),
            httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": "0x2105"}),
        ]

        def handler(request):
            return answers.pop(0)

        slept = []

        async def sleep(seconds):
            slept.append(seconds)

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        rpc = HttpJsonRpc(url, client=client, sleep=sleep)
        assert await rpc.request("eth_chainId", []) == "0x2105"
        assert slept == [1.0]
        failing = HttpJsonRpc(
            url,
            client=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(500))),
        )
        with pytest.raises(RpcError) as caught:
            await failing.request("eth_chainId", [])
        assert "KEY123" not in str(caught.value) and "secret-host" not in str(caught.value)
        assert "KEY123" not in repr(rpc)
        await client.aclose()

    async def test_a_transport_error_is_a_code(self):
        def handler(request):
            raise httpx.ConnectError("boom https://secret-host.example/KEY123/")

        rpc = HttpJsonRpc(
            "https://secret-host.example/KEY123/",
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )
        with pytest.raises(RpcError) as caught:
            await rpc.request("eth_chainId", [])
        assert caught.value.code == "rpc_transport_failed" and "KEY123" not in str(caught.value)
