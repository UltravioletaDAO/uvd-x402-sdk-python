"""Whether a NEW rating may start now: caps, health and a circuit breaker.

The governor only decides about rows that have not started. Work already in
flight (a payment broadcast, a submit sent) is always driven to its verdict:
stopping it halfway is what loses money or duplicates a rating.

**Every cap is a parameter** (:class:`Caps`, no defaults): each product sets
its own, from its own config, and the same value is never typed twice. Karma
Hello's lifetime ceiling (74.000, the owner's order of 2026-10-26, provisional)
is Karma Hello's config, not a constant here. What the caps count is the
outbox itself (:meth:`~uvd_x402_sdk.reputation.outbox.RatingOutbox.usage`:
rows that started, tombstones included), so a restart does not reset them and
a ``!forgetme`` does not either: what is on-chain stays counted.

**Fail closed.** A health probe that raises or answers "unknown", an outbox
that cannot be read, an open breaker: no new rating. The breaker opens after
``failure_threshold`` consecutive failures for ``open_seconds``; a 429 opens it
until its ``Retry-After`` (the facilitator's daily write cap answers 429 until
00:00 UTC); a 503 (writes disabled, writer lease) opens it at once.

Caps that protect the subject or the stream are terminal for the row
(``local_only``: the moment stays local); the global daily ones defer it to the
next UTC day, like Karma Hello's daily limit.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from dataclasses import dataclass, fields
from typing import Any, Callable, Optional, Union

from uvd_x402_sdk.reputation._redact import log_event
from uvd_x402_sdk.reputation.outbox import OutboxEntry

DAY_SECONDS = 86400


def utc_day_start(now: float) -> float:
    return float(int(now // DAY_SECONDS) * DAY_SECONDS)


@dataclass(frozen=True)
class Caps:
    """Ceilings on NEW ratings. All required; non-negative ints.

    ``per_subject_*`` count ratings TO one subject wallet (one rater writing
    over and over about one person is what describe.net reads as a campaign:
    keep ``per_subject_lifetime`` under its ``campaign_per_rater``). ``lifetime``
    counts every rating this outbox ever started. ``per_day_spend`` bounds the
    anchor payments of a UTC day, in atomic units. ``max_value`` bounds the
    rating (a single rater's 100 reads as wash trading).
    """

    per_subject_per_stream: int
    per_subject_per_day: int
    per_subject_lifetime: int
    per_stream: int
    per_day: int
    lifetime: int
    per_day_spend: int
    max_value: int

    def __post_init__(self) -> None:
        for item in fields(self):
            value = getattr(self, item.name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"Caps.{item.name} must be a non-negative int")
        if self.max_value > 100:
            raise ValueError("Caps.max_value must be within 0..100")


@dataclass(frozen=True)
class HealthReport:
    """``ok=None`` means "could not tell", and is treated as not healthy."""

    ok: Optional[bool]
    reason: str = ""


HealthProbe = Callable[[], Union[HealthReport, bool, None]]


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: Optional[str] = None
    terminal: bool = False
    defer_until: Optional[float] = None


class CircuitBreaker:
    """Consecutive failures open it; a success closes it; a trip sets a time."""

    def __init__(
        self,
        *,
        failure_threshold: int = 2,
        open_seconds: float = 3600.0,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if failure_threshold < 1 or open_seconds <= 0:
            raise ValueError("failure_threshold >= 1 and open_seconds > 0")
        self.failure_threshold = failure_threshold
        self.open_seconds = open_seconds
        self._clock = clock
        self._failures = 0
        self._open_until = 0.0
        self.reason: Optional[str] = None

    @property
    def open_until(self) -> float:
        return self._open_until

    def is_open(self, now: Optional[float] = None) -> bool:
        return (self._clock() if now is None else now) < self._open_until

    def record_success(self) -> None:
        self._failures = 0

    def trip(self, reason: str, seconds: Optional[float] = None) -> None:
        until = self._clock() + (self.open_seconds if seconds is None else max(0.0, seconds))
        self._open_until = max(self._open_until, until)
        self.reason = reason
        log_event(30, "reputation.breaker_open", reason=reason, seconds=int(until - self._clock()))

    def record_failure(self, reason: str) -> None:
        self._failures += 1
        if self._failures >= self.failure_threshold:
            self.trip(reason)

    def record_http(
        self, status_code: Optional[int], retry_after: Optional[int], reason: str
    ) -> None:
        """Classify a facilitator error answer (``None`` = no answer at all)."""
        if status_code == 429:
            self.trip(reason, float(retry_after) if retry_after is not None else None)
        elif status_code == 503:
            self.trip(reason, float(retry_after) if retry_after is not None else None)
        else:
            self.record_failure(reason)


def shared_addresses(directory: Mapping[str, str]) -> frozenset:
    """Wallets (lowercased) that more than one account of ``directory`` claims.

    ``directory`` maps an account (a login, a user id) to the wallet it
    declared. A wallet several accounts point at is either a sybil
    concentration or a family sharing one; nothing tells them apart, so a
    rating to it is refused (``subject_excluded``) until one proves possession.
    """
    seen: dict[str, int] = {}
    for wallet in directory.values():
        key = str(wallet).strip().lower()
        seen[key] = seen.get(key, 0) + 1
    return frozenset(wallet for wallet, count in seen.items() if count > 1)


class Governor:
    """``Governor(caps, health).admit(entry, outbox)`` -> :class:`Decision`.

    ``excluded`` (a set of lowercased wallets, or a predicate over a wallet)
    refuses subjects outright, e.g. :func:`shared_addresses`.
    """

    def __init__(
        self,
        caps: Caps,
        health: HealthProbe,
        *,
        breaker: Optional[CircuitBreaker] = None,
        excluded: Union[Callable[[str], bool], frozenset, set, None] = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if not isinstance(caps, Caps):
            raise TypeError("Governor needs a Caps")
        if not callable(health):
            raise TypeError("Governor needs a health probe (fail closed: no probe, no writes)")
        self.caps = caps
        self.health = health
        self.breaker = breaker or CircuitBreaker(clock=clock)
        if excluded is None or callable(excluded):
            self._excluded = excluded
        else:
            wallets = frozenset(str(w).strip().lower() for w in excluded)
            self._excluded = lambda wallet: str(wallet).strip().lower() in wallets
        self._clock = clock

    def _health(self) -> Optional[str]:
        try:
            report = self.health()
        except Exception as exc:  # noqa: BLE001 - an unreadable health is not healthy
            return f"health_unreadable:{type(exc).__name__}"
        if isinstance(report, HealthReport):
            if report.ok is True:
                return None
            return "health_" + (report.reason or ("unknown" if report.ok is None else "unhealthy"))
        if report is True:
            return None
        return "health_unknown" if report is None else "health_unhealthy"

    def ready(self, now: Optional[float] = None) -> Decision:
        """Breaker closed and health good: may anything be SIGNED now?

        Also asked by the worker before it signs the payment of a row that
        started (and counts against the caps) but has not signed it yet.
        """
        clock = self._clock() if now is None else now
        if self.breaker.is_open(clock):
            return Decision(False, "breaker_open", defer_until=self.breaker.open_until)
        problem = self._health()
        if problem is not None:
            return Decision(False, problem, defer_until=clock + 60.0)
        return Decision(True)

    def admit(self, entry: OutboxEntry, outbox: Any, now: Optional[float] = None) -> Decision:
        """May ``entry`` (not started yet) start now?"""
        clock = self._clock() if now is None else now
        caps = self.caps
        ready = self.ready(clock)
        if not ready.allowed:
            return ready
        if self._excluded is not None:
            try:
                excluded = bool(self._excluded(entry.subject.lower()))
            except Exception:  # noqa: BLE001 - cannot tell: not this time
                return Decision(False, "exclusion_unreadable", defer_until=clock + 60.0)
            if excluded:
                return Decision(False, "subject_excluded", terminal=True)
        if entry.value > caps.max_value:
            return Decision(False, "cap_value", terminal=True)
        day = utc_day_start(clock)
        try:
            total = outbox.usage()
            subject = outbox.usage(subject=entry.subject)
            subject_stream = outbox.usage(subject=entry.subject, stream_id=entry.stream_id)
            stream = outbox.usage(stream_id=entry.stream_id)
            subject_day = outbox.usage(subject=entry.subject, since=day)
            today = outbox.usage(since=day)
        except Exception as exc:  # noqa: BLE001 - never write blind
            return Decision(
                False, f"state_unreadable:{type(exc).__name__}", defer_until=clock + 60.0
            )
        terminal_checks = (
            ("cap_lifetime", total.count >= caps.lifetime),
            ("cap_subject_lifetime", subject.count >= caps.per_subject_lifetime),
            ("cap_subject_stream", subject_stream.count >= caps.per_subject_per_stream),
            ("cap_stream", stream.count >= caps.per_stream),
            ("cap_subject_day", subject_day.count >= caps.per_subject_per_day),
        )
        for reason, hit in terminal_checks:
            if hit:
                return Decision(False, reason, terminal=True)
        tomorrow = day + DAY_SECONDS
        if today.count >= caps.per_day:
            return Decision(False, "cap_day", defer_until=tomorrow)
        if today.amount + entry.amount > caps.per_day_spend:
            return Decision(False, "cap_day_spend", defer_until=tomorrow)
        return Decision(True)
