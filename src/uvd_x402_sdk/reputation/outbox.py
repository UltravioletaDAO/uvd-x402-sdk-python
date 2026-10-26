"""The rating outbox: one row per rated thing, persisted BEFORE anything moves.

The state machine is Karma Hello's (``services/reputation/live_worker.py``,
``process`` / ``reconcile``), written down once::

    queued -> paying(tx) -> paid -> submitting(nonce, deadline) -> submitted(tx) -> confirmed
       |         |           |            |                            |
       |         +-> failed  +-> failed   +-> paid  (only once the     +-> failed
       +-> local_only                     |          deadline provably
       +-> paid (no anchor payment)       |          passed unrelayed)
       +-> failed                         +-> failed

* ``ref`` is the UNIQUE key (:func:`~uvd_x402_sdk.reputation.ref.message_ref`):
  the same moment enqueued twice, or again after a restart, is one row.
* Every write is a compare-and-set on the current status
  (:meth:`RatingOutbox.transition`): two workers cannot both move a row, and a
  stale worker cannot rewrite a newer state. ``require_unset`` adds "and this
  field is still empty", which is how the signed payment is saved at most once.
* ``submitting`` is written BEFORE the submit is sent, and nothing goes back
  from it to ``paid`` except the proven-expired path in
  :mod:`~uvd_x402_sdk.reputation.submit`: two nonces are two identical ratings
  on-chain.
* :meth:`RatingOutbox.forget` is ``!forgetme``: what has not started is
  deleted; what has started keeps a tombstone without the viewer (the caps must
  keep counting what is already on-chain, and the subject's wallet is public
  there anyway); what is in flight is flagged and tombstoned when it settles.

Two adapters: :class:`InMemoryRatingOutbox` (tests, one process) and
:class:`SQLiteRatingOutbox` (a file; survives a restart).
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass, fields, replace
from typing import Any, Optional

try:
    from typing import Protocol, runtime_checkable
except ImportError:  # pragma: no cover
    from typing_extensions import Protocol, runtime_checkable  # type: ignore[assignment]

from uvd_x402_sdk.reputation._errors import OutboxConflict
from uvd_x402_sdk.reputation.preparation import ADDRESS_RE

QUEUED = "queued"
PAYING = "paying"
PAID = "paid"
SUBMITTING = "submitting"
SUBMITTED = "submitted"
CONFIRMED = "confirmed"
FAILED = "failed"
LOCAL_ONLY = "local_only"

STATUSES = (QUEUED, PAYING, PAID, SUBMITTING, SUBMITTED, CONFIRMED, FAILED, LOCAL_ONLY)
TERMINAL = frozenset({CONFIRMED, FAILED, LOCAL_ONLY})
#: Rows a worker still has to move, in the order it should look at them:
#: work already in flight first, new work last.
ACTIVE = (SUBMITTED, SUBMITTING, PAYING, PAID, QUEUED)
IN_FLIGHT = frozenset({PAYING, SUBMITTING, SUBMITTED})

#: from -> the statuses a row may move to.
TRANSITIONS = {
    QUEUED: frozenset({PAYING, PAID, LOCAL_ONLY, FAILED}),
    PAYING: frozenset({PAID, FAILED}),
    PAID: frozenset({SUBMITTING, FAILED}),
    SUBMITTING: frozenset({SUBMITTED, PAID, FAILED}),
    SUBMITTED: frozenset({CONFIRMED, FAILED}),
    CONFIRMED: frozenset(),
    FAILED: frozenset(),
    LOCAL_ONLY: frozenset(),
}


def check_transition(current: str, target: str) -> None:
    """Raise :class:`OutboxConflict` unless ``current -> target`` is allowed."""
    if target != current and target not in TRANSITIONS.get(current, frozenset()):
        raise OutboxConflict("transition_not_allowed", f"{current} -> {target}")


@dataclass(repr=False)
class OutboxEntry:
    """One rating, from the moment it is decided to its confirmation.

    Its ``repr`` names the row and its state only: a ``print(row)`` must not
    put the signed payment, the signature, the viewer or a wallet on a stream.
    """

    ref: str
    viewer_id: Optional[str]
    stream_id: str
    subject: str
    network: str
    agent_id: int
    value: int
    tag1: str
    tag2: str
    endpoint: str
    amount: int
    created_at: float
    status: str = QUEUED
    started_at: Optional[float] = None
    attempts: int = 0
    next_attempt: float = 0.0
    error: Optional[str] = None
    payment: Optional[dict] = None
    proof: Optional[dict] = None
    feedback_uri: Optional[str] = None
    feedback_hash: Optional[str] = None
    nonce: Optional[str] = None
    deadline: Optional[int] = None
    signature: Optional[str] = None
    authorization: Optional[dict] = None
    scan_from_block: Optional[int] = None
    rating_tx: Optional[str] = None
    feedback_index: Optional[int] = None
    forget_requested: bool = False
    updated_at: float = 0.0
    #: How many times this row was prepared and signed (``paid -> submitting``).
    relay_rounds: int = 0
    #: The anchor payment's transaction hash, lowercased: one proof, one row.
    anchor_tx: Optional[str] = None
    #: Bumped by every write. A compare-and-set on the status alone is open to
    #: ABA (``submitting(n1) -> paid -> submitting(n2)`` looks unchanged to a
    #: worker that read ``n1``); on the version it is not.
    version: int = 0

    def __repr__(self) -> str:
        return (
            f"OutboxEntry(ref={self.ref[:12]}..., status={self.status}, "
            f"error={self.error}, attempts={self.attempts})"
        )

    def __post_init__(self) -> None:
        if not isinstance(self.ref, str) or not self.ref:
            raise ValueError("OutboxEntry.ref is required")
        if self.status not in STATUSES:
            raise ValueError(f"OutboxEntry.status {self.status!r} is not a status")
        if isinstance(self.amount, bool) or not isinstance(self.amount, int) or self.amount < 0:
            raise ValueError("OutboxEntry.amount must be a non-negative int of atomic units")
        if (
            isinstance(self.agent_id, bool)
            or not isinstance(self.agent_id, int)
            or self.agent_id < 0
        ):
            raise ValueError("OutboxEntry.agent_id must be a non-negative int")
        if (
            isinstance(self.value, bool)
            or not isinstance(self.value, int)
            or not 0 <= self.value <= 100
        ):
            raise ValueError("OutboxEntry.value must be an int within 0..100")
        if not isinstance(self.subject, str) or not ADDRESS_RE.fullmatch(self.subject):
            raise ValueError("OutboxEntry.subject must be the subject's 0x address")
        if not isinstance(self.stream_id, str) or not isinstance(self.network, str):
            raise ValueError("OutboxEntry.stream_id and network are strings")


_FIELD_NAMES = tuple(f.name for f in fields(OutboxEntry))
_UPDATABLE = frozenset(_FIELD_NAMES) - {"ref", "status", "version"}
_JSON_FIELDS = frozenset({"payment", "proof", "authorization"})
#: What a tombstone keeps: enough for the caps to keep counting.
_TOMBSTONE_KEEP = frozenset(
    {
        "ref",
        "stream_id",
        "subject",
        "network",
        "agent_id",
        "amount",
        "created_at",
        "status",
        "started_at",
        "rating_tx",
        "feedback_index",
        "error",
        "updated_at",
        "value",
        "anchor_tx",
        "relay_rounds",
        "version",
    }
)


@dataclass(frozen=True)
class Usage:
    """Rows that started (consumed quota) matching a filter, and their amounts."""

    count: int
    amount: int


@dataclass(frozen=True)
class ForgetReport:
    deleted: int
    tombstoned: int
    in_flight: int


@runtime_checkable
class RatingOutbox(Protocol):
    """The port. Both adapters implement it; a consumer may bring its own."""

    def enqueue(self, entry: OutboxEntry) -> bool:
        """Insert ``entry``; ``False`` (and no change) when its ``ref`` exists."""
        ...

    def get(self, ref: str) -> Optional[OutboxEntry]: ...

    def transition(
        self,
        ref: str,
        expect: str,
        to: Optional[str] = None,
        *,
        require_unset: Sequence[str] = (),
        expect_version: Optional[int] = None,
        **changes: Any,
    ) -> OutboxEntry:
        """Compare-and-set: only if the row is in ``expect``, every field of
        ``require_unset`` is still ``None`` and (when given) its version is
        ``expect_version``. Every write bumps the version. Raises
        :class:`OutboxConflict`."""
        ...

    def due(self, now: float, limit: int = 10) -> list[OutboxEntry]:
        """Active rows with ``next_attempt <= now``, in-flight first."""
        ...

    def count(self, statuses: Iterable[str]) -> int: ...

    def usage(
        self,
        *,
        since: Optional[float] = None,
        subject: Optional[str] = None,
        stream_id: Optional[str] = None,
    ) -> Usage:
        """Started rows (``started_at`` set) matching every given filter."""
        ...

    def forget(self, viewer_id: str) -> ForgetReport: ...

    def tombstone(self, ref: str) -> None:
        """Strip a settled row down to what the caps need (after a forget)."""
        ...

    def anchor_rows(self, anchor_tx: str) -> list[OutboxEntry]:
        """Every row whose anchor payment is ``anchor_tx`` (any status)."""
        ...


def _apply(
    entry: OutboxEntry,
    expect: str,
    to: Optional[str],
    require_unset: Sequence[str],
    changes: dict,
    expect_version: Optional[int] = None,
) -> tuple[OutboxEntry, dict]:
    """The row after the change, and the fields that change (status and version
    included)."""
    if entry.status != expect:
        raise OutboxConflict("status_changed", f"{entry.ref[:12]} is {entry.status}, not {expect}")
    if expect_version is not None and entry.version != expect_version:
        raise OutboxConflict("version_changed", f"{entry.ref[:12]} was written meanwhile")
    target = to or expect
    check_transition(entry.status, target)
    unknown = set(changes) - _UPDATABLE
    if unknown:
        raise ValueError(f"not outbox fields: {sorted(unknown)}")
    for name in require_unset:
        if getattr(entry, name) is not None:
            raise OutboxConflict("field_already_set", name)
    if target != expect:
        # `attempts` counts the tries of the CURRENT status: progress resets it.
        changes = {"attempts": 0, **changes}
    changes = {**changes, "version": entry.version + 1}
    return replace(entry, status=target, **changes), {"status": target, **changes}


def _tombstoned(entry: OutboxEntry) -> OutboxEntry:
    kept = {name: getattr(entry, name) for name in _TOMBSTONE_KEEP}
    kept["version"] = entry.version + 1
    return OutboxEntry(
        viewer_id=None, tag1="", tag2="", endpoint="", forget_requested=False, **kept
    )


def _forget_plan(entry: OutboxEntry) -> str:
    """``delete`` | ``tombstone`` | ``fail`` (paid, never submitted) | ``flag``."""
    if entry.started_at is None:
        return "delete"
    if entry.status in IN_FLIGHT:
        return "flag"
    if entry.status == PAID:
        return "fail"
    return "tombstone"


class InMemoryRatingOutbox:
    """The outbox in a dict, behind a lock. Same semantics as the SQLite one."""

    def __init__(self) -> None:
        self._rows: dict[str, OutboxEntry] = {}
        self._lock = threading.Lock()

    def enqueue(self, entry: OutboxEntry) -> bool:
        with self._lock:
            if entry.ref in self._rows:
                return False
            self._rows[entry.ref] = replace(entry)
            return True

    def get(self, ref: str) -> Optional[OutboxEntry]:
        with self._lock:
            row = self._rows.get(ref)
            return replace(row) if row else None

    def transition(
        self,
        ref: str,
        expect: str,
        to: Optional[str] = None,
        *,
        require_unset: Sequence[str] = (),
        expect_version: Optional[int] = None,
        **changes: Any,
    ) -> OutboxEntry:
        with self._lock:
            row = self._rows.get(ref)
            if row is None:
                raise OutboxConflict("row_missing", ref[:12])
            updated, _ = _apply(row, expect, to, require_unset, changes, expect_version)
            self._rows[ref] = updated
            return replace(updated)

    def anchor_rows(self, anchor_tx: str) -> list[OutboxEntry]:
        wanted = anchor_tx.lower()
        with self._lock:
            return [replace(r) for r in self._rows.values() if (r.anchor_tx or "") == wanted]

    def due(self, now: float, limit: int = 10) -> list[OutboxEntry]:
        with self._lock:
            rows = [r for r in self._rows.values() if r.status in ACTIVE and r.next_attempt <= now]
        rows.sort(key=lambda r: (ACTIVE.index(r.status), r.next_attempt, r.created_at, r.ref))
        return [replace(r) for r in rows[:limit]]

    def count(self, statuses: Iterable[str]) -> int:
        wanted = set(statuses)
        with self._lock:
            return sum(1 for r in self._rows.values() if r.status in wanted)

    def usage(
        self,
        *,
        since: Optional[float] = None,
        subject: Optional[str] = None,
        stream_id: Optional[str] = None,
    ) -> Usage:
        with self._lock:
            rows = [
                r
                for r in self._rows.values()
                if r.started_at is not None
                and (since is None or r.started_at >= since)
                and (subject is None or r.subject.lower() == subject.lower())
                and (stream_id is None or r.stream_id == stream_id)
            ]
        return Usage(count=len(rows), amount=sum(r.amount for r in rows))

    def forget(self, viewer_id: str) -> ForgetReport:
        deleted = tombstoned = in_flight = 0
        with self._lock:
            for ref, row in list(self._rows.items()):
                if row.viewer_id is None or row.viewer_id != viewer_id:
                    continue
                plan = _forget_plan(row)
                if plan == "delete":
                    del self._rows[ref]
                    deleted += 1
                elif plan == "flag":
                    self._rows[ref] = replace(
                        row, viewer_id=None, forget_requested=True, version=row.version + 1
                    )
                    in_flight += 1
                else:
                    if plan == "fail":
                        row = replace(row, status=FAILED, error="forgotten")
                    self._rows[ref] = _tombstoned(row)
                    tombstoned += 1
        return ForgetReport(deleted=deleted, tombstoned=tombstoned, in_flight=in_flight)

    def tombstone(self, ref: str) -> None:
        with self._lock:
            row = self._rows.get(ref)
            if row is not None:
                self._rows[ref] = _tombstoned(row)


_COLUMNS = {
    "ref": "TEXT PRIMARY KEY",
    "viewer_id": "TEXT",
    "stream_id": "TEXT NOT NULL",
    "subject": "TEXT NOT NULL",
    "network": "TEXT NOT NULL",
    "agent_id": "TEXT NOT NULL",
    "value": "INTEGER NOT NULL",
    "tag1": "TEXT NOT NULL",
    "tag2": "TEXT NOT NULL",
    "endpoint": "TEXT NOT NULL",
    "amount": "TEXT NOT NULL",
    "created_at": "REAL NOT NULL",
    "status": "TEXT NOT NULL",
    "started_at": "REAL",
    "attempts": "INTEGER NOT NULL",
    "next_attempt": "REAL NOT NULL",
    "error": "TEXT",
    "payment": "TEXT",
    "proof": "TEXT",
    "feedback_uri": "TEXT",
    "feedback_hash": "TEXT",
    "nonce": "TEXT",
    "deadline": "INTEGER",
    "signature": "TEXT",
    "authorization": "TEXT",
    "scan_from_block": "INTEGER",
    "rating_tx": "TEXT",
    "feedback_index": "INTEGER",
    "forget_requested": "INTEGER NOT NULL",
    "updated_at": "REAL NOT NULL",
    "relay_rounds": "INTEGER NOT NULL",
    "anchor_tx": "TEXT",
    "version": "INTEGER NOT NULL",
}
#: uint256 values do not fit SQLite's INTEGER: stored as decimal text.
_BIG_INT_FIELDS = frozenset({"agent_id", "amount"})
SQLITE_SCHEMA_VERSION = 1


class SQLiteRatingOutbox:
    """The outbox in one SQLite table (``reputation_outbox``), UNIQUE on ``ref``.

    Every write is one statement (``INSERT OR IGNORE``, ``UPDATE ... WHERE
    status = ?``), so a compare-and-set is atomic across processes sharing the
    file. The schema version lives in its own table (``reputation_outbox_meta``),
    NOT in ``PRAGMA user_version``, which belongs to whoever owns the file (a
    consumer may keep its own tables and migrations in it); a file written by
    another version of this table is refused rather than guessed at.
    """

    TABLE = "reputation_outbox"
    META = "reputation_outbox_meta"

    def __init__(self, path: str) -> None:
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._db.execute("PRAGMA busy_timeout = 5000")
        columns = ", ".join(f'"{name}" {kind}' for name, kind in _COLUMNS.items())
        self._db.execute("BEGIN IMMEDIATE")
        try:
            self._db.execute(
                f'CREATE TABLE IF NOT EXISTS {self.META} ("key" TEXT PRIMARY KEY, "value" TEXT)'
            )
            row = self._db.execute(
                f'SELECT "value" FROM {self.META} WHERE "key" = \'schema_version\''
            ).fetchone()
            if row is None:
                self._db.execute(f"CREATE TABLE IF NOT EXISTS {self.TABLE} ({columns})")
                self._db.execute(
                    f"CREATE INDEX IF NOT EXISTS {self.TABLE}_due "
                    f"ON {self.TABLE} (status, next_attempt)"
                )
                self._db.execute(
                    f"CREATE INDEX IF NOT EXISTS {self.TABLE}_anchor ON {self.TABLE} (anchor_tx)"
                )
                self._db.execute(
                    f'INSERT INTO {self.META} ("key", "value") VALUES (\'schema_version\', ?)',
                    [str(SQLITE_SCHEMA_VERSION)],
                )
            elif row[0] != str(SQLITE_SCHEMA_VERSION):
                raise RuntimeError(f"outbox schema {row[0]} is not {SQLITE_SCHEMA_VERSION}")
            self._db.execute("COMMIT")
        except BaseException:
            self._db.execute("ROLLBACK")
            self._db.close()
            raise

    def close(self) -> None:
        self._db.close()

    @staticmethod
    def _to_row(entry: OutboxEntry) -> dict:
        row = asdict(entry)
        for name in _JSON_FIELDS:
            if row[name] is not None:
                row[name] = json.dumps(row[name], sort_keys=True, separators=(",", ":"))
        for name in _BIG_INT_FIELDS:
            row[name] = str(row[name])
        row["forget_requested"] = int(bool(row["forget_requested"]))
        return row

    @staticmethod
    def _from_row(values: Sequence[Any]) -> OutboxEntry:
        row = dict(zip(_COLUMNS, values))
        for name in _JSON_FIELDS:
            if row[name] is not None:
                row[name] = json.loads(row[name])
        for name in _BIG_INT_FIELDS:
            row[name] = int(row[name])
        row["forget_requested"] = bool(row["forget_requested"])
        return OutboxEntry(**row)

    def _select(self, where: str, params: Sequence[Any]) -> list[OutboxEntry]:
        names = ", ".join(f'"{n}"' for n in _COLUMNS)
        cursor = self._db.execute(f"SELECT {names} FROM {self.TABLE} WHERE {where}", params)
        return [self._from_row(values) for values in cursor.fetchall()]

    def enqueue(self, entry: OutboxEntry) -> bool:
        row = self._to_row(entry)
        names = ", ".join(f'"{n}"' for n in row)
        marks = ", ".join("?" for _ in row)
        with self._lock:
            cursor = self._db.execute(
                f"INSERT OR IGNORE INTO {self.TABLE} ({names}) VALUES ({marks})", list(row.values())
            )
        return cursor.rowcount == 1

    def get(self, ref: str) -> Optional[OutboxEntry]:
        with self._lock:
            rows = self._select('"ref" = ?', [ref])
        return rows[0] if rows else None

    def transition(
        self,
        ref: str,
        expect: str,
        to: Optional[str] = None,
        *,
        require_unset: Sequence[str] = (),
        expect_version: Optional[int] = None,
        **changes: Any,
    ) -> OutboxEntry:
        with self._lock:
            rows = self._select('"ref" = ?', [ref])
            if not rows:
                raise OutboxConflict("row_missing", ref[:12])
            read = rows[0]
            updated, changed = _apply(read, expect, to, require_unset, changes, expect_version)
            row = self._to_row(updated)
            # Only the columns this call changes: a concurrent writer of other
            # columns is not clobbered with a stale copy. The WHERE is the
            # compare-and-set itself: the status, the version this process read
            # (another process may have written in between), the fields that
            # must still be empty.
            written = list(changed)
            sets = ", ".join(f'"{n}" = ?' for n in written)
            guards = " AND ".join(
                ['"ref" = ?', '"status" = ?', '"version" = ?']
                + [f'"{n}" IS NULL' for n in require_unset]
            )
            cursor = self._db.execute(
                f"UPDATE {self.TABLE} SET {sets} WHERE {guards}",
                [row[n] for n in written] + [ref, expect, read.version],
            )
            if cursor.rowcount != 1:
                raise OutboxConflict("status_changed", f"{ref[:12]} moved under us")
        return updated

    def anchor_rows(self, anchor_tx: str) -> list[OutboxEntry]:
        with self._lock:
            return self._select('"anchor_tx" = ?', [anchor_tx.lower()])

    def due(self, now: float, limit: int = 10) -> list[OutboxEntry]:
        order = " ".join(f"WHEN '{s}' THEN {i}" for i, s in enumerate(ACTIVE))
        marks = ", ".join("?" for _ in ACTIVE)
        with self._lock:
            return self._select(
                f'"status" IN ({marks}) AND "next_attempt" <= ? '
                f'ORDER BY CASE "status" {order} END, "next_attempt", "created_at", "ref" LIMIT ?',
                [*ACTIVE, now, int(limit)],
            )

    def count(self, statuses: Iterable[str]) -> int:
        wanted = list(statuses)
        if not wanted:
            return 0
        marks = ", ".join("?" for _ in wanted)
        with self._lock:
            return int(
                self._db.execute(
                    f'SELECT COUNT(*) FROM {self.TABLE} WHERE "status" IN ({marks})', wanted
                ).fetchone()[0]
            )

    def usage(
        self,
        *,
        since: Optional[float] = None,
        subject: Optional[str] = None,
        stream_id: Optional[str] = None,
    ) -> Usage:
        clauses: list[str] = ['"started_at" IS NOT NULL']
        params: list[Any] = []
        if since is not None:
            clauses.append('"started_at" >= ?')
            params.append(since)
        if subject is not None:
            clauses.append('lower("subject") = ?')
            params.append(subject.lower())
        if stream_id is not None:
            clauses.append('"stream_id" = ?')
            params.append(stream_id)
        with self._lock:
            amounts = [
                int(value)
                for (value,) in self._db.execute(
                    f'SELECT "amount" FROM {self.TABLE} WHERE {" AND ".join(clauses)}', params
                ).fetchall()
            ]
        return Usage(count=len(amounts), amount=sum(amounts))

    def forget(self, viewer_id: str) -> ForgetReport:
        deleted = tombstoned = in_flight = 0
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                for row in self._select('"viewer_id" = ?', [viewer_id]):
                    plan = _forget_plan(row)
                    if plan == "delete":
                        self._db.execute(f'DELETE FROM {self.TABLE} WHERE "ref" = ?', [row.ref])
                        deleted += 1
                    elif plan == "flag":
                        self._db.execute(
                            f'UPDATE {self.TABLE} SET "viewer_id" = NULL, "forget_requested" = 1, '
                            f'"version" = "version" + 1 WHERE "ref" = ?',
                            [row.ref],
                        )
                        in_flight += 1
                    else:
                        if plan == "fail":
                            row = replace(row, status=FAILED, error="forgotten")
                        self._replace(_tombstoned(row))
                        tombstoned += 1
                self._db.execute("COMMIT")
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
        return ForgetReport(deleted=deleted, tombstoned=tombstoned, in_flight=in_flight)

    def _replace(self, entry: OutboxEntry) -> None:
        row = self._to_row(entry)
        sets = ", ".join(f'"{n}" = ?' for n in row if n != "ref")
        self._db.execute(
            f'UPDATE {self.TABLE} SET {sets} WHERE "ref" = ?',
            [v for n, v in row.items() if n != "ref"] + [entry.ref],
        )

    def tombstone(self, ref: str) -> None:
        with self._lock:
            rows = self._select('"ref" = ?', [ref])
            if rows:
                self._replace(_tombstoned(rows[0]))


__all__ = [
    "ACTIVE",
    "CONFIRMED",
    "FAILED",
    "IN_FLIGHT",
    "LOCAL_ONLY",
    "PAID",
    "PAYING",
    "QUEUED",
    "STATUSES",
    "SUBMITTED",
    "SUBMITTING",
    "TERMINAL",
    "TRANSITIONS",
    "ForgetReport",
    "InMemoryRatingOutbox",
    "OutboxEntry",
    "RatingOutbox",
    "SQLiteRatingOutbox",
    "Usage",
    "check_transition",
]
