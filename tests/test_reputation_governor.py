"""The governor: every cap is a parameter, every doubt is a no."""

from __future__ import annotations

from dataclasses import fields

import pytest

from tests.reputation_doubles import SUBJECT, Clock, generous_caps, make_entry
from uvd_x402_sdk.reputation import (
    Caps,
    CircuitBreaker,
    Governor,
    HealthReport,
    InMemoryRatingOutbox,
    shared_addresses,
)
from uvd_x402_sdk.reputation.governor import DAY_SECONDS, utc_day_start

NOW = 1_790_000_000 + 3600.0


def started(outbox, count, *, subject=SUBJECT, stream="stream-1", at=NOW, amount=1000, prefix="r"):
    for index in range(count):
        entry = make_entry((prefix, str(index)), subject=subject, stream_id=stream, amount=amount)
        outbox.enqueue(entry)
        outbox.transition(entry.ref, "queued", "paying", started_at=at)


def governor(caps=None, health=lambda: True, **kwargs):
    clock = Clock(NOW)
    return Governor(
        caps or generous_caps(), health, breaker=CircuitBreaker(clock=clock), clock=clock, **kwargs
    )


def test_every_cap_is_required():
    with pytest.raises(TypeError):
        Caps()  # type: ignore[call-arg]
    assert all(f.default is f.default_factory for f in fields(Caps))  # no defaults at all


@pytest.mark.parametrize("bad", [-1, True, 1.5, "3"])
def test_caps_are_non_negative_ints(bad):
    with pytest.raises(ValueError):
        generous_caps(per_stream=bad)


def test_karma_hello_lifetime_is_a_parameter():
    assert generous_caps(lifetime=74_000).lifetime == 74_000


@pytest.mark.parametrize(
    "cap,setup,reason,terminal",
    [
        ("lifetime", dict(count=3, subject="0x" + "44" * 20), "cap_lifetime", True),
        (
            "per_subject_lifetime",
            dict(count=3, at=NOW - 10 * DAY_SECONDS),
            "cap_subject_lifetime",
            True,
        ),
        ("per_subject_per_stream", dict(count=3), "cap_subject_stream", True),
        ("per_stream", dict(count=3, subject="0x" + "44" * 20), "cap_stream", True),
        ("per_subject_per_day", dict(count=3, stream="other"), "cap_subject_day", True),
        ("per_day", dict(count=3, subject="0x" + "44" * 20, stream="other"), "cap_day", False),
    ],
)
def test_each_cap(cap, setup, reason, terminal):
    outbox = InMemoryRatingOutbox()
    started(outbox, **setup)
    entry = make_entry(("new", "1"))
    gov = governor(generous_caps(**{cap: 3}))
    decision = gov.admit(entry, outbox, NOW)
    assert (decision.allowed, decision.reason, decision.terminal) == (False, reason, terminal)
    if not terminal:
        assert decision.defer_until == utc_day_start(NOW) + DAY_SECONDS
    assert governor(generous_caps(**{cap: 4})).admit(entry, outbox, NOW).allowed


def test_the_daily_spend_includes_the_new_payment():
    outbox = InMemoryRatingOutbox()
    started(outbox, 2, amount=1000, subject="0x" + "44" * 20)
    entry = make_entry(("new", "1"), amount=1000)
    assert not governor(generous_caps(per_day_spend=2999)).admit(entry, outbox, NOW).allowed
    assert governor(generous_caps(per_day_spend=3000)).admit(entry, outbox, NOW).allowed


def test_yesterday_does_not_count_today():
    outbox = InMemoryRatingOutbox()
    started(outbox, 3, at=utc_day_start(NOW) - 1, stream="old")
    assert (
        governor(generous_caps(per_subject_per_day=3, per_day=3))
        .admit(make_entry(("new", "1")), outbox, NOW)
        .allowed
    )


def test_the_value_cap():
    decision = governor(generous_caps(max_value=95)).admit(
        make_entry(value=96), InMemoryRatingOutbox(), NOW
    )
    assert (decision.reason, decision.terminal) == ("cap_value", True)


@pytest.mark.parametrize(
    "health,reason",
    [
        (lambda: None, "health_unknown"),
        (lambda: False, "health_unhealthy"),
        (lambda: HealthReport(ok=None), "health_unknown"),
        (lambda: HealthReport(ok=False, reason="balance_low"), "health_balance_low"),
        (lambda: 1 / 0, "health_unreadable:ZeroDivisionError"),
        (lambda: "yes", "health_unhealthy"),
    ],
)
def test_health_fails_closed(health, reason):
    decision = governor(health=health).admit(make_entry(), InMemoryRatingOutbox(), NOW)
    assert (decision.allowed, decision.reason, decision.terminal) == (False, reason, False)


def test_a_governor_without_a_probe_cannot_exist():
    with pytest.raises(TypeError):
        Governor(generous_caps(), None)  # type: ignore[arg-type]


def test_an_unreadable_outbox_is_no_write():
    class Broken(InMemoryRatingOutbox):
        def usage(self, **kwargs):
            raise OSError("disk")

    decision = governor().admit(make_entry(), Broken(), NOW)
    assert (decision.allowed, decision.reason) == (False, "state_unreadable:OSError")


class TestBreaker:
    def test_consecutive_failures_open_it_and_a_success_resets(self):
        clock = Clock(NOW)
        breaker = CircuitBreaker(failure_threshold=2, open_seconds=3600, clock=clock)
        breaker.record_failure("x")
        breaker.record_success()
        breaker.record_failure("x")
        assert not breaker.is_open()
        breaker.record_failure("x")
        assert breaker.is_open() and breaker.open_until == NOW + 3600
        clock.now += 3601
        assert not breaker.is_open()

    @pytest.mark.parametrize(
        "status,retry_after,seconds",
        [(429, 30, 30), (429, None, 3600), (503, 120, 120), (503, None, 3600)],
    )
    def test_429_and_503_open_it_at_once(self, status, retry_after, seconds):
        clock = Clock(NOW)
        breaker = CircuitBreaker(clock=clock)
        breaker.record_http(status, retry_after, "x")
        assert breaker.open_until == NOW + seconds

    def test_a_daily_cap_retry_after_of_hours_is_honoured(self):
        clock = Clock(NOW)
        breaker = CircuitBreaker(clock=clock)
        breaker.record_http(429, 20_000, "daily_cap")
        assert breaker.open_until == NOW + 20_000

    def test_an_open_breaker_defers_every_new_rating(self):
        gov = governor()
        gov.breaker.trip("x", 60)
        decision = gov.admit(make_entry(), InMemoryRatingOutbox(), NOW)
        assert (decision.allowed, decision.reason, decision.defer_until) == (
            False,
            "breaker_open",
            NOW + 60,
        )


class TestExclusions:
    def test_shared_addresses(self):
        directory = {
            "a": SUBJECT,
            "b": SUBJECT.lower(),
            "c": " " + SUBJECT.upper().replace("0X", "0x"),
            "d": "0x" + "44" * 20,
        }
        assert shared_addresses(directory) == frozenset({SUBJECT.lower()})

    @pytest.mark.parametrize(
        "excluded",
        [frozenset({SUBJECT.lower()}), {SUBJECT}, lambda wallet: wallet.lower() == SUBJECT.lower()],
    )
    def test_an_excluded_subject_is_terminal(self, excluded):
        decision = governor(excluded=excluded).admit(make_entry(), InMemoryRatingOutbox(), NOW)
        assert (decision.reason, decision.terminal) == ("subject_excluded", True)

    def test_an_unreadable_exclusion_is_no_write(self):
        decision = governor(excluded=lambda wallet: 1 / 0).admit(
            make_entry(), InMemoryRatingOutbox(), NOW
        )
        assert (decision.allowed, decision.reason, decision.terminal) == (
            False,
            "exclusion_unreadable",
            False,
        )
