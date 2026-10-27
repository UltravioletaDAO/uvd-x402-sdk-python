"""Prepare, sign, submit; and reconcile, never prepare twice.

:func:`submit_with_reconcile` drives one outbox row from ``paid`` to
``confirmed`` (or a ``failed`` with a reason):

``paid``
    (breaker closed) prepare -> :func:`validate_preparation` -> the RPC's
    chain id, and for a first rating the account nonce and delegation, read
    from the chain -> sign -> **persist ``submitting`` with the rater, nonce,
    deadline and signature** -> submit. The write must succeed before anything
    is sent: from then on the row holds the one signed rating it may ever
    produce.
``submitting``
    The submit's answer was lost, uncertain or refused. Reconcile: scan the
    registry's ``NewFeedback`` logs, always from the block read before the
    submit. Found: ``submitted``. Not found and the deadline still ahead:
    re-send the SAME body (the delegate's nonce is single-use, so at most one
    of the two lands). Once the chain is past the deadline (plus a margin) the
    signature can never land (``FeedbackDelegate``: ``block.timestamp >
    deadline`` reverts), IF it has not landed already; that is decided on
    STATE read at that block (:meth:`_Run.relay_absent`: the rater's
    delegation and ``nonceUsed(nonce)``), never on an empty log answer, which
    a lagging node gives for blocks it does not have. A spent nonce with no
    event found yet is a rating that landed: the row keeps looking. A
    signature proven never to land is ``failed(relay_expired_unrelayed)`` for
    a human, as in Karma Hello; only a consumer that passes
    ``max_relay_rounds > 1`` gets it prepared again (``paid``). Never a second
    prepare while the first signature may still land, or may have: two nonces
    are two ratings.
``submitted``
    Read the receipt: reverted -> ``failed(relay_reverted)``; no event that is
    exactly this rating -> ``failed(feedback_event_mismatch)``; enough
    confirmations -> ``confirmed`` with the ``feedbackIndex``.

The facilitator's 429 and 503 open the breaker (with ``Retry-After``); every
other failed prepare or submit counts towards its threshold; a prepare this
module refuses to sign, or an RPC serving another chain, opens it at once,
since that is a systemic problem, not this row's.

A row is moved only by its rater (``OutboxEntry.rater``): every state read is
at that address, and a signer with another address is refused
(:class:`~uvd_x402_sdk.reputation.RaterMismatch`) before anything is signed or
written.

Ported from Karma Hello's ``live_worker.Worker.process`` / ``reconcile`` /
``matching_feedback``.
"""

from __future__ import annotations

import re
import time
from typing import Any, Callable, Optional

try:
    from typing import Protocol, runtime_checkable
except ImportError:  # pragma: no cover
    from typing_extensions import Protocol, runtime_checkable  # type: ignore[assignment]

from uvd_x402_sdk.erc8004 import ProofOfPayment, RelayAuthorizationParams
from uvd_x402_sdk.reputation._chains import RELAY_DELEGATES, relay_chain_id, reputation_registry
from uvd_x402_sdk.reputation._errors import (
    PreparationRejected,
    RaterMismatch,
    SignatureRejected,
)
from uvd_x402_sdk.reputation._redact import log_event
from uvd_x402_sdk.reputation.governor import CircuitBreaker
from uvd_x402_sdk.reputation.outbox import (
    CONFIRMED,
    FAILED,
    PAID,
    PAYING,
    SUBMITTED,
    SUBMITTING,
    OutboxEntry,
)
from uvd_x402_sdk.reputation.preparation import RatingParams, checksum, validate_preparation
from uvd_x402_sdk.reputation.signing import (
    build_relay_authorization,
    sign_relayed_feedback,
    signer_address,
)

NEW_FEEDBACK_SIGNATURE = (
    "NewFeedback(uint256,address,uint64,int128,uint8,string,string,string,string,string,bytes32)"
)
NEW_FEEDBACK_TOPIC = "0x6a4a61743519c9d648a14e6493f47dbe3ff1aa29e7785c96c8326a205e58febc"
_TX_HASH_RE = re.compile(r"^0x[0-9a-fA-F]{64}$")
#: Seconds to wait before looking at a row again, by attempt.
DEFAULT_RETRY_DELAYS = (15.0, 120.0, 600.0)
#: Blocks re-scanned behind the cursor, for shallow reorgs.
REORG_OVERLAP = 12
#: ``nonceUsed(bytes32)`` of the FeedbackDelegate (EM ``FeedbackDelegate.sol``,
#: x402-rs ``relay.rs::nonce_already_used``), read on the rater's address.
NONCE_USED_SELECTOR = "0x61a4422b"
#: EIP-7702 delegation designator prefix (``0xef0100`` + the delegate).
_DELEGATION_PREFIX = "0xef0100"


@runtime_checkable
class RelayClient(Protocol):
    """The two calls of :class:`~uvd_x402_sdk.erc8004.Erc8004Client` this needs."""

    async def prepare_relayed_feedback(
        self, network: Any, agent_id: Any, rater: str, value: int, **kwargs: Any
    ) -> Any: ...

    async def submit_relayed_feedback(
        self, network: Any, agent_id: Any, rater: str, value: int, **kwargs: Any
    ) -> Any: ...


def rating_params(entry: OutboxEntry) -> RatingParams:
    """The :class:`RatingParams` a row stands for (its evidence must be set)."""
    if not entry.feedback_uri or not entry.feedback_hash:
        raise ValueError("the row has no published evidence yet")
    return RatingParams(
        agent_id=entry.agent_id,
        value=entry.value,
        tag1=entry.tag1,
        tag2=entry.tag2,
        endpoint=entry.endpoint,
        feedback_uri=entry.feedback_uri,
        feedback_hash=entry.feedback_hash,
    )


def _topic_uint(value: int) -> str:
    return "0x" + format(value, "064x")


def _topic_address(address: str) -> str:
    return "0x" + address.lower()[2:].rjust(64, "0")


def matching_feedback(
    log: dict, *, registry: str, rater: str, params: RatingParams
) -> Optional[int]:
    """The ``feedbackIndex`` if ``log`` is exactly this rating, else ``None``.

    Address, all four topics (``tag1`` is indexed as its keccak) and every
    data field must match; a removed log never does.
    """
    from eth_abi import decode
    from eth_utils import keccak

    topics = [str(t).lower() for t in log.get("topics") or []]
    if (
        log.get("removed")
        or str(log.get("address", "")).lower() != registry.lower()
        or topics
        != [
            NEW_FEEDBACK_TOPIC,
            _topic_uint(params.agent_id),
            _topic_address(rater),
            "0x" + bytes(keccak(text=params.tag1)).hex(),
        ]
    ):
        return None
    try:
        values = decode(
            ["uint64", "int128", "uint8", "string", "string", "string", "string", "bytes32"],
            bytes.fromhex(str(log.get("data", ""))[2:]),
        )
    except Exception:  # noqa: BLE001 - an unreadable log is not this rating
        return None
    expected = (
        params.value,
        0,
        params.tag1,
        params.tag2,
        params.endpoint,
        params.feedback_uri,
        bytes.fromhex(params.feedback_hash[2:]),
    )
    return int(values[0]) if tuple(values[1:]) == expected else None


def _delay(entry: OutboxEntry, delays: tuple) -> float:
    return float(delays[min(entry.attempts, len(delays) - 1)])


def _int(value: Any) -> int:
    if isinstance(value, str) and value[:2].lower() == "0x":
        return int(value, 16)
    raise ValueError("not a hex quantity")


#: Prepare-and-sign rounds a row gets. One: a signature proven never to land
#: is failed for a human, never prepared again (Karma Hello's
#: ``signed_feedback_requires_reconciliation``). Preparing again after that
#: proof is the consumer's decision to make (``max_relay_rounds > 1``), and the
#: proof is only as good as the state it reads, which is the row's rater's.
#: Bounded either way: a submit the facilitator keeps refusing would otherwise
#: cycle (prepare, sign, expire) for ever and hold the semaphore.
DEFAULT_MAX_RELAY_ROUNDS = 1
#: Tries a ``paid`` row gets when the prepare is refused definitely (a 4xx
#: other than 408/409/425/429): nothing was signed, and retrying the same
#: request does not change the answer.
DEFAULT_MAX_PREPARE_REFUSALS = 3
#: Seconds past the deadline a row may stay without a verdict (state
#: unreadable, a landed rating whose event is not found, a receipt that never
#: comes) before it is failed for a human. Failed is terminal: never prepared
#: again. Karma Hello parks such rows after 24 h.
DEFAULT_UNKNOWN_AFTER = 86400.0
#: The block the expiry is decided at. ``safe`` on an OP-stack chain (Base) is
#: past the reorgs the unsafe head still has; a chain whose RPC does not serve
#: the tag answers an error, which is no verdict.
DEFAULT_EXPIRY_BLOCK_TAG = "safe"
_TRANSIENT_4XX = frozenset({408, 409, 425, 429})


def _is_hex(value: Any) -> bool:
    return isinstance(value, str) and value[:2].lower() == "0x"


#: Statuses whose row must already say who its rater is.
_RATER_REQUIRED = frozenset({PAYING, SUBMITTING, SUBMITTED})


def row_rater(entry: OutboxEntry, signer: Any) -> str:
    """The address that moves ``entry``; :class:`RaterMismatch` if ``signer`` is not it.

    A row that has not recorded a rater yet (``queued``, or ``paid`` without
    the worker) takes the signer's, and records it with ``submitting``.
    """
    address = signer_address(signer)
    if entry.rater is None:
        if entry.status in _RATER_REQUIRED:
            raise RaterMismatch("rater_unknown", "the row does not record its rater")
        return address
    if entry.rater.lower() != address.lower():
        raise RaterMismatch(
            "rater_mismatch", "the signer is not the rater this row was started with"
        )
    return checksum(entry.rater)


class _Run:
    """One call of :func:`submit_with_reconcile`: its dependencies and clock.

    Every write is a compare-and-set on the row's VERSION (``move``): a worker
    that read ``submitting(n1)`` cannot overwrite ``submitting(n2)`` written
    meanwhile by another one (ABA on the status alone).
    """

    def __init__(
        self,
        outbox: Any,
        relay: Any,
        signer: Any,
        rpc: Any,
        breaker: CircuitBreaker,
        confirmations: int,
        now: float,
        *,
        expiry_margin: float,
        scan_window: int,
        max_windows: int,
        delays: tuple,
        expiry_block_tag: str,
        unknown_after: float,
        max_relay_rounds: int,
        max_prepare_refusals: int,
    ) -> None:
        self.outbox, self.relay, self.signer, self.rpc = outbox, relay, signer, rpc
        self.breaker, self.confirmations, self.now = breaker, confirmations, now
        self.expiry_margin, self.scan_window, self.max_windows = (
            expiry_margin,
            scan_window,
            max_windows,
        )
        self.delays, self.expiry_block_tag, self.unknown_after = (
            delays,
            expiry_block_tag,
            unknown_after,
        )
        self.max_relay_rounds, self.max_prepare_refusals = max_relay_rounds, max_prepare_refusals

    # -- writes ------------------------------------------------------------------
    def move(self, entry: OutboxEntry, to: Optional[str] = None, **changes: Any) -> OutboxEntry:
        changes.setdefault("updated_at", self.now)
        return self.outbox.transition(
            entry.ref, entry.status, to, expect_version=entry.version, **changes
        )

    def wait(
        self, entry: OutboxEntry, error: Optional[str], until: Optional[float] = None
    ) -> OutboxEntry:
        """Stay in the same status; look again later."""
        return self.move(
            entry,
            attempts=entry.attempts + 1,
            error=error,
            next_attempt=until if until is not None else self.now + _delay(entry, self.delays),
        )

    def fail(self, entry: OutboxEntry, error: str) -> OutboxEntry:
        log_event(40, "reputation.failed", ref=entry.ref, error=error)
        entry = self.move(entry, FAILED, error=error)
        if entry.forget_requested:
            self.outbox.tombstone(entry.ref)
        return entry

    def stalled(self, entry: OutboxEntry) -> bool:
        """Past the deadline by more than ``unknown_after``: time for a human."""
        return self.now > int(entry.deadline or 0) + self.unknown_after

    async def call(self, method: str, params: list) -> Any:
        return await self.rpc.request(method, params)

    def _proof(self, entry: OutboxEntry) -> Optional[ProofOfPayment]:
        return ProofOfPayment.model_validate(entry.proof) if entry.proof else None

    def _wire(self, entry: OutboxEntry) -> dict:
        params = rating_params(entry).to_wire()
        params.pop("agent_id")
        params.pop("value")
        return params

    # -- paid: prepare, check, sign, persist, send -----------------------------------
    async def prepare_and_submit(self, entry: OutboxEntry) -> OutboxEntry:
        if entry.forget_requested:
            return self.fail(entry, "forgotten")
        if self.breaker.is_open(self.now):
            # Nothing signed yet: a row that has not prepared waits with the rest.
            return self.move(
                entry,
                error=f"breaker_open:{self.breaker.reason}",
                next_attempt=self.breaker.open_until,
            )
        params = rating_params(entry)
        # The row's rater, or (a row that never recorded one) the signer, which
        # submit_with_reconcile already checked and the SUBMITTING write records.
        rater = entry.rater or signer_address(self.signer)
        try:
            prep = await self.relay.prepare_relayed_feedback(
                entry.network,
                entry.agent_id,
                rater,
                entry.value,
                proof=self._proof(entry),
                **self._wire(entry),
            )
        except Exception as exc:  # noqa: BLE001 - nothing was signed
            self.breaker.record_failure("prepare_transport")
            return self.wait(entry, f"prepare_transport:{type(exc).__name__}")
        if not getattr(prep, "success", False):
            status = getattr(prep, "status_code", None)
            self.breaker.record_http(status, getattr(prep, "retry_after", None), "prepare_failed")
            definite = isinstance(status, int) and 400 <= status < 500
            if definite and status not in _TRANSIENT_4XX:
                if entry.attempts + 1 >= self.max_prepare_refusals:
                    return self.fail(entry, f"prepare_refused_{status}")
            return self.wait(entry, f"prepare_http_{status if status is not None else 'none'}")
        try:
            verified = validate_preparation(
                prep, params, rater, network=entry.network, now=self.now
            )
        except PreparationRejected as exc:
            # Systemic (a facilitator that changed): stop all new work.
            self.breaker.trip(f"refused:{exc.code}")
            log_event(40, "reputation.prepare_refused", code=exc.code, ref=entry.ref)
            return self.wait(entry, f"refused:{exc.code}")
        # What the facilitator says about the rater's delegation is checked on
        # the chain before anything is signed. A rating "for a delegated rater"
        # that is not delegated cannot land; an EIP-7702 authorization never
        # expires and is valid while the account nonce it names is the
        # account's, so that nonce is the chain's. And it must be THE chain:
        # every read below, and every later proof of absence, is the RPC's.
        try:
            chain_id = await self.call("eth_chainId", [])
            code = await self.call("eth_getCode", [rater, "latest"])
            pending = None
            if not verified.delegated:
                pending = await self.call("eth_getTransactionCount", [rater, "pending"])
        except Exception as exc:  # noqa: BLE001 - nothing signed yet: wait
            return self.wait(entry, f"rpc_unavailable:{type(exc).__name__}")
        designator = _DELEGATION_PREFIX + verified.delegate.lower()[2:]
        if (
            not _is_hex(chain_id)
            or not _is_hex(code)
            or (pending is not None and not _is_hex(pending))
        ):
            return self.wait(entry, "rpc_unreadable")
        if _int(chain_id) != verified.chain_id:
            # Systemic: the configuration points at another chain's RPC.
            self.breaker.trip("refused:chain_mismatch")
            return self.wait(entry, "chain_mismatch")
        if verified.delegated and str(code).lower() != designator:
            return self.wait(entry, "delegation_state_changed")
        if not verified.delegated:
            if _int(pending) != verified.account_nonce:
                return self.wait(entry, "account_nonce_moved")
            if str(code).lower() == designator:
                return self.wait(entry, "delegation_state_changed")
            if self.outbox.count([PAYING]):
                # A transfer signed now would take the nonce the authorization
                # names; one in flight already will.
                return self.wait(entry, "payment_in_flight")
        try:
            signature = sign_relayed_feedback(verified, self.signer)
            authorization = build_relay_authorization(verified, self.signer)
        except (PreparationRejected, SignatureRejected) as exc:
            # Systemic (a facilitator or a signer that changed): stop all new work.
            self.breaker.trip(f"refused:{exc.code}")
            log_event(40, "reputation.prepare_refused", code=exc.code, ref=entry.ref)
            return self.wait(entry, f"refused:{exc.code}")
        try:
            start = _int(await self.call("eth_blockNumber", []))
        except Exception as exc:  # noqa: BLE001 - nothing sent yet: safe to wait
            return self.wait(entry, f"rpc_unavailable:{type(exc).__name__}")
        # The one write that must happen before anything is sent.
        entry = self.move(
            entry,
            SUBMITTING,
            rater=rater,
            nonce=verified.nonce,
            deadline=verified.deadline,
            signature=signature,
            authorization=authorization.model_dump(by_alias=True) if authorization else None,
            scan_from_block=max(0, start - REORG_OVERLAP),
            relay_rounds=entry.relay_rounds + 1,
            error=None,
        )
        log_event(20, "reputation.submitting", ref=entry.ref, deadline=entry.deadline)
        entry = await self.send(entry)
        return await self.reconcile(entry, resend=False)

    async def send(self, entry: OutboxEntry) -> OutboxEntry:
        """Send the persisted submit, as is. Never re-signs anything."""
        authorization = (
            RelayAuthorizationParams.model_validate(entry.authorization)
            if entry.authorization
            else None
        )
        try:
            result = await self.relay.submit_relayed_feedback(
                entry.network,
                entry.agent_id,
                str(entry.rater),
                entry.value,
                deadline=entry.deadline,
                nonce=entry.nonce,
                signature=entry.signature,
                authorization=authorization,
                proof=self._proof(entry),
                **self._wire(entry),
            )
        except Exception as exc:  # noqa: BLE001 - uncertain: reconcile decides
            self.breaker.record_failure("submit_transport")
            return self.wait(entry, f"relay_uncertain:{type(exc).__name__}")
        tx = getattr(result, "transaction", None)
        if getattr(result, "success", False) and isinstance(tx, str) and _TX_HASH_RE.fullmatch(tx):
            self.breaker.record_success()
            log_event(20, "reputation.submitted", ref=entry.ref, tx=tx)
            return self.move(entry, SUBMITTED, rating_tx=tx.lower(), error=None)
        status = getattr(result, "status_code", None)
        # Every non-success feeds the breaker: 429/503 open it at once, and a
        # refusal repeated (a proof the facilitator now enforces, say) opens it
        # after the threshold instead of cycling the rail forever.
        self.breaker.record_http(status, getattr(result, "retry_after", None), "submit_failed")
        return self.wait(entry, f"relay_uncertain_{status if status is not None else 'none'}")

    # -- the chain: events and state -----------------------------------------------------
    async def find_event(self, entry: OutboxEntry, height: int) -> Optional[dict]:
        """The ``NewFeedback`` that is exactly this rating, scanning from the
        block read before the submit (never a cursor an empty answer moved)."""
        registry = checksum(str(reputation_registry(entry.network)))
        rater = str(entry.rater)
        params = rating_params(entry)
        topics = [NEW_FEEDBACK_TOPIC, _topic_uint(params.agent_id), _topic_address(rater)]
        cursor, windows = int(entry.scan_from_block or 0), 0
        while cursor <= height and windows < self.max_windows:
            end = min(cursor + self.scan_window - 1, height)
            logs = await self.call(
                "eth_getLogs",
                [
                    {
                        "address": registry,
                        "fromBlock": hex(cursor),
                        "toBlock": hex(end),
                        "topics": topics,
                    }
                ],
            )
            for log in logs or []:
                if (
                    matching_feedback(log, registry=registry, rater=rater, params=params)
                    is not None
                ):
                    return log
            cursor, windows = end + 1, windows + 1
        return None

    async def relay_absent(self, entry: OutboxEntry, header: dict) -> Optional[bool]:
        """Did the signed rating provably NOT land by the block ``header`` names?

        Decided on STATE read AT that block, addressed by its hash (EIP-1898,
        ``requireCanonical``): a node that does not have it errors instead of
        answering from its own head, and a ``null`` is no answer. Never on an
        empty log query (a load-balanced RPC answers logs from a node behind).

        - code == our delegate's designator: ``nonceUsed(nonce)`` (on the
          RATER's address: under 7702 the delegate's storage is the rater's)
          false -> True, true -> False (it landed).
        - no code: True only if THIS row carried the authorization AND the
          account nonce at that block is not past the one it names (an applied
          authorization bumps it, so does an undelegation). Anything else (the
          rater was delegated before, the nonce moved) is ``None``.
        - foreign code, an unreadable or malformed answer: ``None``.

        Read at the row's rater: a rotated key is another account, with no
        delegation and nonce 0, which "proves" any rating absent.
        """
        rater = str(entry.rater)
        delegate = RELAY_DELEGATES.get(relay_chain_id(entry.network) or -1)
        block_hash = header.get("hash") if isinstance(header, dict) else None
        if delegate is None or not _is_hex(block_hash):
            return None
        at = {"blockHash": block_hash, "requireCanonical": True}
        try:
            code = await self.call("eth_getCode", [rater, at])
            if not _is_hex(code):
                return None
            code = str(code).lower()
            if code in ("0x", "0x0"):
                if not entry.authorization:
                    return None
                count = await self.call("eth_getTransactionCount", [rater, at])
                if not _is_hex(count):
                    return None
                return True if _int(count) <= int(entry.authorization["nonce"]) else None
            if code != _DELEGATION_PREFIX + delegate.lower()[2:]:
                return None
            used = await self.call(
                "eth_call",
                [{"to": rater, "data": NONCE_USED_SELECTOR + str(entry.nonce)[2:]}, at],
            )
            if not _is_hex(used) or len(str(used)) != 66:
                return None
            return _int(used) == 0
        except Exception:  # noqa: BLE001 - no verdict
            return None

    # -- submitted: the receipt ---------------------------------------------------------
    async def confirm(self, entry: OutboxEntry) -> OutboxEntry:
        registry = checksum(str(reputation_registry(entry.network)))
        rater = str(entry.rater)
        params = rating_params(entry)
        try:
            receipt = await self.call("eth_getTransactionReceipt", [entry.rating_tx])
            if not receipt:
                # The facilitator may have replaced its transaction: the event,
                # wherever it is, is the truth.
                latest = await self.call("eth_blockNumber", [])
                found = await self.find_event(entry, _int(latest))
                tx = str((found or {}).get("transactionHash", "")).lower()
                if found is not None and tx and tx != entry.rating_tx:
                    return self.move(entry, rating_tx=tx, error="relay_tx_replaced")
                if self.stalled(entry):
                    return self.fail(entry, "relay_receipt_missing")
                return self.wait(entry, "relay_receipt_pending")
            if _int(receipt.get("status")) != 1:
                log_event(40, "reputation.relay_reverted", ref=entry.ref, tx=entry.rating_tx)
                return self.move(entry, FAILED, error="relay_reverted")
            indexes = [
                index
                for index in (
                    matching_feedback(log, registry=registry, rater=rater, params=params)
                    for log in receipt.get("logs") or []
                )
                if index is not None
            ]
            if not indexes:
                return self.move(entry, FAILED, error="feedback_event_mismatch")
            height = _int(await self.call("eth_blockNumber", []))
            if height - _int(receipt.get("blockNumber")) + 1 < self.confirmations:
                return self.wait(entry, "relay_awaiting_confirmations")
        except Exception as exc:  # noqa: BLE001 - no verdict: look again later
            return self.wait(entry, f"rpc_unavailable:{type(exc).__name__}")
        entry = self.move(entry, CONFIRMED, feedback_index=indexes[0], error=None)
        self.breaker.record_success()
        log_event(20, "reputation.confirmed", ref=entry.ref, tx=entry.rating_tx)
        if entry.forget_requested:
            self.outbox.tombstone(entry.ref)
        return entry

    # -- submitting: reconcile, re-send, or expire --------------------------------------
    async def reconcile(self, entry: OutboxEntry, *, resend: bool) -> OutboxEntry:
        if entry.status == SUBMITTED:
            return await self.confirm(entry)
        if entry.status != SUBMITTING:
            return entry
        found, logs_error = None, None
        try:
            latest = await self.call("eth_getBlockByNumber", ["latest", False])
            found = await self.find_event(entry, _int(latest.get("number")))
        except Exception as exc:  # noqa: BLE001 - logs are a fast path, not the verdict
            logs_error = type(exc).__name__
        if found is not None:
            tx = str(found.get("transactionHash", "")).lower()
            entry = self.move(entry, SUBMITTED, rating_tx=tx, error=None)
            return await self.confirm(entry)
        deadline = int(entry.deadline or 0)
        try:
            header = await self.call("eth_getBlockByNumber", [self.expiry_block_tag, False])
            stamp = _int(header.get("timestamp"))
        except Exception:  # noqa: BLE001 - no expiry decision without the block
            header, stamp = None, None
        if header is not None and stamp is not None and stamp > deadline + self.expiry_margin:
            # The chain (at a block past the reorgs) is past the deadline: the
            # signature can never land (FeedbackDelegate: block.timestamp >
            # deadline reverts) IF it has not landed already. Only state read
            # at that block decides that.
            absent = await self.relay_absent(entry, header)
            if absent is True:
                log_event(30, "reputation.relay_expired", ref=entry.ref, deadline=deadline)
                if entry.relay_rounds >= self.max_relay_rounds:
                    # Proven never to land. By default (one round) a human
                    # decides; nothing is signed again for this moment.
                    return self.fail(entry, "relay_expired_unrelayed")
                return self.move(
                    entry,
                    PAID,
                    nonce=None,
                    deadline=None,
                    signature=None,
                    authorization=None,
                    scan_from_block=None,
                    error="relay_expired_unrelayed",
                    next_attempt=self.now,
                )
            reason = "relay_landed_unindexed" if absent is False else "relay_state_unknown"
            log_event(30, "reputation." + reason, ref=entry.ref)
            if self.stalled(entry):
                return self.fail(entry, reason)
            return self.wait(entry, reason)
        if resend and self.now < deadline:
            entry = await self.send(entry)
            if entry.status == SUBMITTED:
                return await self.confirm(entry)
            return entry
        if logs_error is not None:
            return self.wait(entry, f"rpc_unavailable:{logs_error}")
        return self.wait(entry, entry.error or "relay_unconfirmed")


async def submit_with_reconcile(
    entry: OutboxEntry,
    *,
    outbox: Any,
    relay: Any,
    signer: Any,
    rpc: Any,
    breaker: Optional[CircuitBreaker] = None,
    confirmations: int = 2,
    now: Optional[float] = None,
    clock: Callable[[], float] = time.time,
    expiry_margin: float = 60.0,
    expiry_block_tag: str = DEFAULT_EXPIRY_BLOCK_TAG,
    scan_window: int = 2000,
    max_windows: int = 5,
    retry_delays: tuple = DEFAULT_RETRY_DELAYS,
    unknown_after: float = DEFAULT_UNKNOWN_AFTER,
    max_relay_rounds: int = DEFAULT_MAX_RELAY_ROUNDS,
    max_prepare_refusals: int = DEFAULT_MAX_PREPARE_REFUSALS,
) -> OutboxEntry:
    """Move one row (``paid``, ``submitting`` or ``submitted``) as far as it goes.

    Returns the row as persisted. Safe to call again on any of the three
    statuses, after any crash: a ``submitting`` row is only ever reconciled and
    re-sent, never prepared again (unless ``max_relay_rounds > 1`` and its
    signature provably expired). Every write is a compare-and-set on the row's
    version: if another worker wrote the row meanwhile,
    :class:`~uvd_x402_sdk.reputation.OutboxConflict` is raised and nothing of
    this call's is persisted after that point. A ``signer`` that is not the
    row's rater raises :class:`~uvd_x402_sdk.reputation.RaterMismatch` before
    anything is read, signed or written.
    """
    current = outbox.get(entry.ref) or entry
    row_rater(current, signer)
    run = _Run(
        outbox,
        relay,
        signer,
        rpc,
        breaker or CircuitBreaker(clock=clock),
        confirmations,
        clock() if now is None else now,
        expiry_margin=expiry_margin,
        scan_window=scan_window,
        max_windows=max_windows,
        delays=tuple(retry_delays),
        expiry_block_tag=expiry_block_tag,
        unknown_after=unknown_after,
        max_relay_rounds=max_relay_rounds,
        max_prepare_refusals=max_prepare_refusals,
    )
    if current.status == PAID:
        return await run.prepare_and_submit(current)
    if current.status in (SUBMITTING, SUBMITTED):
        return await run.reconcile(current, resend=True)
    raise ValueError(f"submit_with_reconcile: a {current.status} row has nothing to submit")
