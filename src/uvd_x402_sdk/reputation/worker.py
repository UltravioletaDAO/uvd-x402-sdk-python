"""The whole rail in one loop: what a new program wires to rate on-chain.

``ReputationWorker.tick()`` takes the next due row of the outbox and moves it
as far as it can go:

``queued``
    :meth:`Governor.admit` (caps, health, breaker, excluded subjects) ->
    the RPC serves the row's chain -> the subject must OWN the agent
    (``ownerOf``, read from the chain: paying and rating an agent someone else
    owns would be a rating about the wrong person) -> ``paying`` with the
    signer's address as the row's ``rater`` (the row now counts against the
    caps).
``paying``
    :func:`pay_and_prove` with the persisted signed transfer, if any; the
    transfer is persisted (``require_unset``: at most once) before it is
    broadcast -> the consumer publishes the evidence -> ``paid``. A
    ``!forgetme`` that arrives before the transfer is signed fails the row
    instead.
``paid`` / ``submitting`` / ``submitted``
    :func:`submit_with_reconcile`.

A row that started is moved only by its rater: ``tick`` raises
:class:`~uvd_x402_sdk.reputation.RaterMismatch` (nothing signed, nothing
written) when the signer is another key. Rotating the rater's key means
letting its rows settle first, or settling them by hand.

A semaphore of one: while a row is ``paying`` or ``submitting``, no new row
starts (two transfers signed from one "pending" nonce replace each other, and
a new transfer moves the account nonce a first rating's EIP-7702
authorization was signed over). Run ONE worker per outbox, with one ``tick``
at a time; the outbox's compare-and-set makes a second one harmless, not
useful.

With ``amount == 0`` the worker pays nothing: the row is rated without a
proof, or with one made elsewhere and enqueued in ``proof`` (an x402
settlement, Karma Hello's USDC reward), which is rebuilt from the chain
(:func:`prove_payment`: the block's own timestamp, the ``paymentHash``
recomputed) and must name the rater as payer and the subject as payee.

The consumer brings what is product-specific: the outbox, the governor's caps
and health, the signer, the facilitator client, the RPC, and
``publish_evidence(entry, proof) -> (uri, content_hash)``, which builds the
document with :func:`~uvd_x402_sdk.reputation.evidence.evidence_document` and
serves its ``body`` at ``uri`` (``https://``, ``http://`` or ``ipfs://``: the
facilitator puts any of them in the typed data and refuses none, x402-rs
``erc8004/proof.rs::fetch_and_hash_document``).
"""

from __future__ import annotations

import asyncio
import inspect
import re
import time
from collections.abc import Awaitable
from typing import Any, Callable, Optional

from uvd_x402_sdk.erc8004 import ProofOfPayment
from uvd_x402_sdk.reputation._chains import identity_registry
from uvd_x402_sdk.reputation._errors import OutboxConflict, PaymentPending, PaymentRejected
from uvd_x402_sdk.reputation._redact import log_event, redact_http_client_logs
from uvd_x402_sdk.reputation.governor import Governor
from uvd_x402_sdk.reputation.outbox import (
    FAILED,
    LOCAL_ONLY,
    PAID,
    PAYING,
    QUEUED,
    SUBMITTED,
    SUBMITTING,
    OutboxEntry,
)
from uvd_x402_sdk.reputation.payment import GasPolicy, _check_chain, pay_and_prove, prove_payment
from uvd_x402_sdk.reputation.preparation import BYTES32_RE
from uvd_x402_sdk.reputation.signing import signer_address
from uvd_x402_sdk.reputation.submit import (
    DEFAULT_EXPIRY_BLOCK_TAG,
    DEFAULT_MAX_PREPARE_REFUSALS,
    DEFAULT_MAX_RELAY_ROUNDS,
    DEFAULT_RETRY_DELAYS,
    DEFAULT_UNKNOWN_AFTER,
    row_rater,
    submit_with_reconcile,
)

_OWNER_OF = "0x6352211e"  # ownerOf(uint256)
#: Payment refusals that are not about the row: the configuration or the
#: chain changed, so no new row should start either.
_SYSTEMIC_PAYMENT = frozenset(
    {"chain_mismatch", "unknown_network", "payment_signature", "payment_unavailable", "gas_ceiling"}
)
#: What the evidence may be served at. ``ipfs://`` is Karma Hello's
#: (``ipfs_store.py``); the facilitator fetches only http(s) to audit the
#: anchor and reports any other scheme as unreachable, never as a refusal.
_URI_RE = re.compile(r"^(?:https?|ipfs)://\S+$")
#: Hex characters of the ref that, found anywhere in a public field, link the
#: rating back to the message.
_REF_WINDOW = 16


async def _maybe_await(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


class ReputationWorker:
    """One outbox, one rater, one network.

    The rater is enforced per row (``OutboxEntry.rater``, :class:`RaterMismatch`).
    Every write is a compare-and-set on the row's version: a second worker on
    the same outbox gets an :class:`OutboxConflict` (logged, the row re-read)
    instead of overwriting the first one's state.
    """

    def __init__(
        self,
        *,
        outbox: Any,
        governor: Governor,
        signer: Any,
        relay: Any,
        rpc: Any,
        publish_evidence: Callable[[OutboxEntry, Optional[ProofOfPayment]], Any],
        max_amount: int,
        confirmations: int = 2,
        gas: GasPolicy = GasPolicy(),
        payment_wait_seconds: float = 30.0,
        retry_delays: tuple = DEFAULT_RETRY_DELAYS,
        expiry_margin: float = 60.0,
        expiry_block_tag: str = DEFAULT_EXPIRY_BLOCK_TAG,
        scan_window: int = 2000,
        max_windows: int = 5,
        unknown_after: float = DEFAULT_UNKNOWN_AFTER,
        max_relay_rounds: int = DEFAULT_MAX_RELAY_ROUNDS,
        max_prepare_refusals: int = DEFAULT_MAX_PREPARE_REFUSALS,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.outbox, self.governor, self.signer = outbox, governor, signer
        self.relay, self.rpc, self.publish_evidence = relay, rpc, publish_evidence
        self.max_amount, self.confirmations, self.gas = max_amount, confirmations, gas
        self.payment_wait_seconds, self.retry_delays = payment_wait_seconds, tuple(retry_delays)
        self.submit_options = {
            "expiry_margin": expiry_margin,
            "expiry_block_tag": expiry_block_tag,
            "scan_window": scan_window,
            "max_windows": max_windows,
            "unknown_after": unknown_after,
            "max_relay_rounds": max_relay_rounds,
            "max_prepare_refusals": max_prepare_refusals,
        }
        self._clock, self._sleep = clock, sleep
        redact_http_client_logs()  # the facilitator's and the RPC's URLs

    # -- writes ------------------------------------------------------------------
    def _move(self, entry: OutboxEntry, to: Optional[str] = None, **changes: Any) -> OutboxEntry:
        changes.setdefault("updated_at", self._clock())
        return self.outbox.transition(
            entry.ref, entry.status, to, expect_version=entry.version, **changes
        )

    def _wait(self, entry: OutboxEntry, error: str, until: Optional[float] = None) -> OutboxEntry:
        delay = self.retry_delays[min(entry.attempts, len(self.retry_delays) - 1)]
        return self._move(
            entry,
            attempts=entry.attempts + 1,
            error=error,
            next_attempt=until if until is not None else self._clock() + delay,
        )

    def _fail(self, entry: OutboxEntry, error: str) -> OutboxEntry:
        log_event(40, "reputation.failed", ref=entry.ref, error=error)
        entry = self._move(entry, FAILED, error=error)
        if entry.forget_requested:
            self.outbox.tombstone(entry.ref)
        return entry

    # -- the loop ------------------------------------------------------------------
    async def tick(self) -> Optional[OutboxEntry]:
        """Advance the next due row; ``None`` when there is nothing to do now."""
        now = self._clock()
        rows = self.outbox.due(now, limit=1)
        if not rows:
            return None
        entry = rows[0]
        if entry.status in (PAYING, PAID, SUBMITTING, SUBMITTED):
            # Before anything is read, signed or written: RaterMismatch.
            row_rater(entry, self.signer)
        try:
            if entry.status == QUEUED:
                if self.outbox.count([PAYING, SUBMITTING]):
                    # A semaphore of one: two transfers signed from one
                    # "pending" nonce replace each other, and a new transfer
                    # also moves the account nonce a first rating's EIP-7702
                    # authorization (still in flight) was signed over.
                    return None
                return await self._start(entry, now)
            if entry.status == PAYING:
                return await self._pay(entry)
            if entry.status in (PAID, SUBMITTING, SUBMITTED):
                return await self._submit(entry)
        except OutboxConflict as exc:
            log_event(30, "reputation.outbox_conflict", ref=entry.ref, code=exc.code)
            return self.outbox.get(entry.ref)
        return entry

    async def _owner_matches(self, entry: OutboxEntry) -> Optional[bool]:
        registry = identity_registry(entry.network)
        if registry is None:
            return False
        data = _OWNER_OF + format(entry.agent_id, "064x")
        try:
            result = await self.rpc.request("eth_call", [{"to": registry, "data": data}, "latest"])
            owner = int(str(result), 16)
        except Exception as exc:  # noqa: BLE001 - classified below
            # `ownerOf` of an agent that does not exist REVERTS: a definite "not
            # this subject's", not a missing verdict to retry forever.
            detail = str(getattr(exc, "detail", "") or "").lower()
            return False if "revert" in detail else None
        return owner == int(entry.subject, 16)

    async def _start(self, entry: OutboxEntry, now: float) -> OutboxEntry:
        decision = self.governor.admit(entry, self.outbox, now)
        if not decision.allowed:
            reason = decision.reason or "refused"
            if decision.terminal:
                log_event(20, "reputation.local_only", ref=entry.ref, reason=reason)
                return self._move(entry, LOCAL_ONLY, error=reason)
            return self._wait(entry, reason, decision.defer_until)
        if self._identifies(entry, entry.tag1, entry.tag2, entry.endpoint):
            # Tags and endpoint go on-chain for ever too.
            return self._move(entry, LOCAL_ONLY, error="public_field_identifies")
        try:
            # Every read that follows (the owner, the proof, the rater's state)
            # means something only on the row's chain.
            await _check_chain(self.rpc, entry.network)
        except PaymentPending as pending:
            return self._wait(entry, pending.code)
        except PaymentRejected as rejected:
            if rejected.code != "chain_mismatch":  # a network with no chain: never on-chain
                return self._move(entry, LOCAL_ONLY, error=rejected.code)
            # Systemic: the configuration points at another chain's RPC.
            self.governor.breaker.trip(f"payment:{rejected.code}")
            return self._wait(entry, rejected.code)
        rater = signer_address(self.signer)
        owner = await self._owner_matches(entry)
        if owner is None:
            return self._wait(entry, "rpc_unavailable")
        if not owner:
            return self._move(entry, LOCAL_ONLY, error="agent_owner_mismatch")
        if entry.amount == 0:
            # No anchor payment by this worker. Either none (``proof`` empty) or
            # one made elsewhere (an x402 settlement, Karma Hello's reward):
            # then the claimed proof is rebuilt from the CHAIN (the block's
            # timestamp, the paymentHash recomputed), its payer must be the
            # rater, and one payment proves ONE rating per agent here (the
            # facilitator refuses a reused proof only when it enforces proofs,
            # and a row that keeps being refused would block the rail).
            proof = None
            if entry.proof:
                try:
                    proof = await self._reprove(entry)
                except PaymentPending as pending:
                    return self._wait(entry, pending.code)
                except PaymentRejected as rejected:
                    return self._fail(entry, rejected.code)
                anchor = proof.transaction_hash.lower()
                if any(
                    row.ref != entry.ref and row.agent_id == entry.agent_id
                    for row in self.outbox.anchor_rows(anchor)
                ):
                    return self._fail(entry, "proof_reused")
            published = await self._publish(entry, proof)
            if published is None:
                return self._wait(entry, "evidence_unpublished")
            uri, digest = published
            entry = self._move(
                entry,
                PAID,
                started_at=now,
                rater=rater,
                feedback_uri=uri,
                feedback_hash=digest,
                proof=proof.model_dump(by_alias=True) if proof else None,
                anchor_tx=proof.transaction_hash.lower() if proof else None,
                error=None,
            )
            return await self._submit(entry)
        entry = self._move(entry, PAYING, started_at=now, rater=rater, error=None)
        return await self._pay(entry)

    async def _reprove(self, entry: OutboxEntry) -> ProofOfPayment:
        try:
            claimed = ProofOfPayment.model_validate(entry.proof)
        except Exception:  # noqa: BLE001 - an unreadable claim proves nothing
            raise PaymentRejected("proof_shape", "the enqueued proof is not a proof") from None
        if claimed.payer.lower() != signer_address(self.signer).lower():
            raise PaymentRejected("payer_is_not_rater", "the facilitator refuses such a proof")
        if claimed.payee.lower() != entry.subject.lower():
            raise PaymentRejected("payee_is_not_subject", "the proof paid someone else")
        if not claimed.amount.isdigit():
            raise PaymentRejected("proof_shape", "the proof's amount is not atomic units")
        return await prove_payment(
            self.rpc,
            network=entry.network,
            transaction_hash=claimed.transaction_hash,
            payer=claimed.payer,
            payee=claimed.payee,
            amount=int(claimed.amount),
            confirmations=self.confirmations,
        )

    @staticmethod
    def _identifies(entry: OutboxEntry, *public: str) -> bool:
        """Does a public string carry 16 consecutive hex of the ref, or the viewer?

        The ref is sha256 of ids anyone in the chat can see; on-chain it links
        the rating back to the message and its author, salt or no salt. Any 16
        of its characters do, not only the first 16.
        """
        text = " ".join(public).lower()
        ref = entry.ref.lower()
        viewer = (entry.viewer_id or "").lower()
        return any(
            ref[start : start + _REF_WINDOW] in text
            for start in range(max(1, len(ref) - _REF_WINDOW + 1))
        ) or (len(viewer) >= 6 and viewer in text)

    async def _publish(
        self, entry: OutboxEntry, proof: Optional[ProofOfPayment]
    ) -> Optional[tuple]:
        try:
            uri, digest = await _maybe_await(self.publish_evidence(entry, proof))
        except Exception as exc:  # noqa: BLE001 - retried; the payment stays proven
            log_event(30, "reputation.evidence_failed", ref=entry.ref, error=type(exc).__name__)
            return None
        if (
            not isinstance(uri, str)
            or not _URI_RE.fullmatch(uri)
            or not isinstance(digest, str)
            or (not BYTES32_RE.fullmatch(digest))
        ):
            log_event(40, "reputation.evidence_invalid", ref=entry.ref)
            return None
        # The URI goes on-chain for ever (the NewFeedback event): name the
        # document by its content hash, or by something random.
        if self._identifies(entry, uri):
            log_event(40, "reputation.evidence_uri_identifies", ref=entry.ref)
            return None
        return uri, "0x" + digest[2:].lower()

    async def _pay(self, entry: OutboxEntry) -> OutboxEntry:
        if entry.payment is None:
            # Started but nothing signed yet (it waited, e.g. on gas): a
            # !forgetme ends it here, before a transfer to the viewer's wallet
            # makes a new public record; an open breaker or a failing health
            # stops it like any new rating, and a rating in flight keeps the
            # account nonce its authorization names.
            if entry.forget_requested:
                return self._fail(entry, "forgotten")
            ready = self.governor.ready(self._clock())
            if not ready.allowed:
                return self._wait(entry, ready.reason or "not_ready", ready.defer_until)
            if self.outbox.count([SUBMITTING]):
                return self._wait(entry, "rating_in_flight")
        version = entry.version

        def persist(signed: Any) -> None:
            self.outbox.transition(
                entry.ref,
                PAYING,
                require_unset=("payment",),
                expect_version=version,
                payment=signed.to_dict(),
                updated_at=self._clock(),
            )

        try:
            proof = await pay_and_prove(
                self.signer,
                entry.subject,
                entry.amount,
                self.rpc,
                network=entry.network,
                max_amount=self.max_amount,
                confirmations=self.confirmations,
                gas=self.gas,
                signed=entry.payment,
                on_signed=persist,
                wait_seconds=self.payment_wait_seconds,
                sleep=self._sleep,
                clock=self._clock,
            )
        except PaymentPending as pending:
            return self._wait(self.outbox.get(entry.ref) or entry, pending.code)
        except PaymentRejected as rejected:
            if rejected.code in _SYSTEMIC_PAYMENT:
                self.governor.breaker.trip(f"payment:{rejected.code}")
            log_event(40, "reputation.payment_failed", ref=entry.ref, code=rejected.code)
            return self._fail(self.outbox.get(entry.ref) or entry, rejected.code)
        entry = self.outbox.get(entry.ref) or entry
        if entry.forget_requested:
            return self._fail(entry, "forgotten")
        published = await self._publish(entry, proof)
        if published is None:
            return self._wait(entry, "evidence_unpublished")
        uri, digest = published
        entry = self._move(
            entry,
            PAID,
            proof=proof.model_dump(by_alias=True),
            anchor_tx=proof.transaction_hash.lower(),
            feedback_uri=uri,
            feedback_hash=digest,
            error=None,
        )
        return await self._submit(entry)

    async def _submit(self, entry: OutboxEntry) -> OutboxEntry:
        return await submit_with_reconcile(
            entry,
            outbox=self.outbox,
            relay=self.relay,
            signer=self.signer,
            rpc=self.rpc,
            breaker=self.governor.breaker,
            confirmations=self.confirmations,
            now=self._clock(),
            retry_delays=self.retry_delays,
            **self.submit_options,
        )


__all__ = ["ReputationWorker"]
