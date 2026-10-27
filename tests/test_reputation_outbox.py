"""The outbox: one row per ref, compare-and-set on every write, a forget that
keeps the caps honest. Both adapters run the same tests."""

from __future__ import annotations

import sqlite3

import pytest

from tests.reputation_doubles import SUBJECT, make_entry
from uvd_x402_sdk.reputation import InMemoryRatingOutbox, OutboxConflict, SQLiteRatingOutbox
from uvd_x402_sdk.reputation.outbox import (
    ACTIVE,
    STATUSES,
    TRANSITIONS,
    check_transition,
)


@pytest.fixture(params=["memory", "sqlite"])
def outbox(request, tmp_path):
    if request.param == "memory":
        yield InMemoryRatingOutbox()
    else:
        box = SQLiteRatingOutbox(str(tmp_path / "outbox.sqlite"))
        yield box
        box.close()


def test_the_same_ref_is_one_row_and_the_first_one_wins(outbox):
    first = make_entry(value=80)
    assert outbox.enqueue(first) is True
    assert outbox.enqueue(make_entry(value=95)) is False
    assert outbox.get(first.ref).value == 80


def test_every_write_is_a_compare_and_set(outbox):
    entry = make_entry()
    outbox.enqueue(entry)
    outbox.transition(entry.ref, "queued", "paying", started_at=1.0)
    with pytest.raises(OutboxConflict) as caught:
        outbox.transition(entry.ref, "queued", "paying", started_at=2.0)  # a stale worker
    assert caught.value.code == "status_changed"
    assert outbox.get(entry.ref).started_at == 1.0


@pytest.mark.parametrize("current", STATUSES)
def test_only_the_declared_transitions(current):
    for target in STATUSES:
        if target == current or target in TRANSITIONS[current]:
            check_transition(current, target)
        else:
            with pytest.raises(OutboxConflict):
                check_transition(current, target)


def test_nothing_goes_back_to_queued_or_prepares_again_from_submitted(outbox):
    entry = make_entry()
    outbox.enqueue(entry)
    outbox.transition(entry.ref, "queued", "paying", started_at=1.0)
    outbox.transition(entry.ref, "paying", "paid")
    outbox.transition(entry.ref, "paid", "submitting", nonce="0x" + "01" * 32)
    outbox.transition(entry.ref, "submitting", "submitted", rating_tx="0x" + "02" * 32)
    for target in ("queued", "paying", "paid", "submitting"):
        with pytest.raises(OutboxConflict):
            outbox.transition(entry.ref, "submitted", target)


def test_the_signed_payment_is_saved_at_most_once(outbox):
    entry = make_entry()
    outbox.enqueue(entry)
    outbox.transition(entry.ref, "queued", "paying", started_at=1.0)
    outbox.transition(entry.ref, "paying", require_unset=("payment",), payment={"raw": "0xaa"})
    with pytest.raises(OutboxConflict) as caught:
        outbox.transition(entry.ref, "paying", require_unset=("payment",), payment={"raw": "0xbb"})
    assert caught.value.code == "field_already_set"
    assert outbox.get(entry.ref).payment == {"raw": "0xaa"}


def test_unknown_fields_and_missing_rows(outbox):
    entry = make_entry()
    outbox.enqueue(entry)
    with pytest.raises(ValueError):
        outbox.transition(entry.ref, "queued", bogus=1)
    with pytest.raises(ValueError):  # the status moves only through `to`, checked
        outbox.transition(entry.ref, "queued", status="confirmed")
    with pytest.raises(OutboxConflict) as caught:
        outbox.transition("0" * 64, "queued")
    assert caught.value.code == "row_missing"


def test_attempts_count_the_tries_of_the_current_status(outbox):
    entry = make_entry()
    outbox.enqueue(entry)
    outbox.transition(entry.ref, "queued", attempts=3)
    assert outbox.transition(entry.ref, "queued", "paying").attempts == 0
    assert outbox.get(entry.ref).attempts == 0


def test_due_puts_work_in_flight_first(outbox):
    rows = {}
    for status in ("queued", "paid", "submitting", "paying", "submitted"):
        entry = make_entry(("s", status), created_at=0.0 if status == "queued" else 5.0)
        outbox.enqueue(entry)
        rows[status] = entry.ref
    path = {
        "paying": ["paying"],
        "paid": ["paying", "paid"],
        "submitting": ["paying", "paid", "submitting"],
        "submitted": ["paying", "paid", "submitting", "submitted"],
    }
    for status, steps in path.items():
        current = "queued"
        for step in steps:
            outbox.transition(rows[status], current, step, started_at=1.0)
            current = step
    assert [r.status for r in outbox.due(10.0, limit=10)] == list(ACTIVE)
    outbox.transition(rows["submitted"], "submitted", next_attempt=100.0)
    assert "submitted" not in [r.status for r in outbox.due(10.0, limit=10)]
    assert outbox.count(["paying", "paid"]) == 2


def test_usage_counts_started_rows_by_subject_stream_and_time(outbox):
    for index, (subject, stream, started) in enumerate(
        [
            (SUBJECT, "a", 10.0),
            (SUBJECT.lower(), "a", 20.0),
            (SUBJECT, "b", 30.0),
            ("0x" + "44" * 20, "a", 40.0),
            (SUBJECT, "a", None),
        ]
    ):
        entry = make_entry(("s", str(index)), subject=subject, stream_id=stream, amount=100 + index)
        outbox.enqueue(entry)
        if started is not None:
            outbox.transition(entry.ref, "queued", "paying", started_at=started)
    assert outbox.usage().count == 4
    assert outbox.usage(subject=SUBJECT.upper().replace("0X", "0x")).count == 3
    assert outbox.usage(subject=SUBJECT, stream_id="a").count == 2
    assert outbox.usage(since=25.0).count == 2
    assert outbox.usage(since=25.0).amount == 102 + 103


def test_forget_deletes_what_did_not_start_and_keeps_counting_what_did(outbox):
    queued = make_entry(("s", "q"))
    local = make_entry(("s", "l"))
    confirmed = make_entry(("s", "c"))
    other_viewer = make_entry(("s", "o"), viewer_id="viewer-2")
    for entry in (queued, local, confirmed, other_viewer):
        outbox.enqueue(entry)
    outbox.transition(local.ref, "queued", "local_only", error="cap_stream")
    for step in ("paying", "paid", "submitting", "submitted", "confirmed"):
        current = outbox.get(confirmed.ref).status
        outbox.transition(confirmed.ref, current, step, started_at=5.0, rating_tx="0x" + "0a" * 32)
    report = outbox.forget("viewer-1")
    assert (report.deleted, report.tombstoned, report.in_flight) == (2, 1, 0)
    assert outbox.get(queued.ref) is None and outbox.get(local.ref) is None
    stone = outbox.get(confirmed.ref)
    assert stone.viewer_id is None and stone.tag1 == stone.tag2 == stone.endpoint == ""
    assert stone.rating_tx == "0x" + "0a" * 32 and stone.status == "confirmed"
    assert outbox.usage(subject=SUBJECT).count == 1
    assert outbox.get(other_viewer.ref).viewer_id == "viewer-2"


def test_the_sqlite_outbox_survives_a_restart_and_huge_ints(tmp_path):
    path = str(tmp_path / "o.sqlite")
    box = SQLiteRatingOutbox(path)
    entry = make_entry(agent_id=2**200, amount=2**100)
    box.enqueue(entry)
    box.transition(
        entry.ref,
        "queued",
        "paying",
        started_at=1.0,
        payment={"raw": "0xaa", "nonce": 3},
        proof=None,
    )
    box.close()
    again = SQLiteRatingOutbox(path)
    row = again.get(entry.ref)
    assert row.status == "paying" and row.agent_id == 2**200 and row.amount == 2**100
    assert row.payment == {"raw": "0xaa", "nonce": 3}
    assert again.enqueue(entry) is False
    again.close()


def test_the_sqlite_outbox_refuses_a_schema_it_did_not_write(tmp_path):
    path = str(tmp_path / "o.sqlite")
    SQLiteRatingOutbox(path).close()
    db = sqlite3.connect(path)
    db.execute("UPDATE reputation_outbox_meta SET value = '7' WHERE key = 'schema_version'")
    db.commit()
    db.close()
    with pytest.raises(RuntimeError):
        SQLiteRatingOutbox(path)


def test_two_processes_on_one_file_cannot_both_claim_a_row(tmp_path):
    path = str(tmp_path / "o.sqlite")
    a, b = SQLiteRatingOutbox(path), SQLiteRatingOutbox(path)
    entry = make_entry()
    a.enqueue(entry)
    assert b.enqueue(entry) is False
    a.transition(entry.ref, "queued", "paying", started_at=1.0)
    with pytest.raises(OutboxConflict):
        b.transition(entry.ref, "queued", "paying", started_at=2.0)
    a.close()
    b.close()
