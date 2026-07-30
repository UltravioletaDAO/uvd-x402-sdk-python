"""
Contract tests for the live traffic stream (GET /events, SSE).

The wire fixtures below are the real framing the facilitator emits, captured
from https://facilitator.ultravioletadao.xyz/events on 2026-07-28 (v1.59.5):
an `event:` name that IS the operation, a JSON `data:` line, and bare `:`
comments as keepalives on an idle rail.

The keepalive case matters more than it looks: on a quiet rail those comments
are the ONLY thing on the wire for minutes at a time, so a parser that treats
them as data, or that blocks waiting for a real event, breaks in production and
nowhere else.
"""

import pytest

from uvd_x402_sdk.events import (
    EVENT_KINDS,
    TrafficEvent,
    TrafficEventStream,
    _sse_events,
)

SETTLE_FRAME = [
    "event: settle",
    'data: {"ts":1785256783513,"kind":"settle","network":"base","ok":true,'
    '"payer":"0x87228cF28dd82546d76249A8Bb92AdEa9258F404",'
    '"tx":"0xdeadbeef","amount":"100000","asset":"0x8335"}',
    "",
]

VERIFY_FRAME = [
    "event: verify",
    'data: {"ts":1785256783000,"kind":"verify","network":"skale-base","ok":false,'
    '"payer":"0xabc","amount":"100000","asset":"0x8335"}',
    "",
]

# What an idle rail actually looks like. Measured: ~1.3 settles/min, with
# 35-minute stretches of nothing but these.
KEEPALIVES = [":", "", ":", "", ":", ""]


def _events(lines):
    return list(_sse_events(lines))


def test_parses_a_settle_frame():
    frames = _events(SETTLE_FRAME)
    assert len(frames) == 1
    assert frames[0]["event"] == "settle"


def test_keepalive_comments_are_not_events():
    """An idle stream must yield nothing, not empty or malformed events."""
    assert _events(KEEPALIVES) == []


def test_keepalives_do_not_break_a_following_event():
    """The parser must survive minutes of silence and still deliver the next one."""
    frames = _events(KEEPALIVES + SETTLE_FRAME + KEEPALIVES)
    assert [f["event"] for f in frames] == ["settle"]


def test_unknown_sse_fields_are_ignored_not_fatal():
    """`id:`/`retry:` are legal SSE; a facilitator adding them must not break us."""
    frames = _events(["id: 7", "retry: 3000"] + SETTLE_FRAME)
    assert len(frames) == 1


def test_carriage_returns_are_stripped():
    """SSE framing is CRLF-legal; \\r must not end up inside the JSON."""
    frames = _events([line + "\r" for line in SETTLE_FRAME])
    assert frames[0]["data"].startswith("{")


class TestTrafficEvent:
    def test_settle_carries_a_tx(self):
        stream = TrafficEventStream()
        event = stream._to_event(_events(SETTLE_FRAME)[0])
        assert event is not None
        assert event.kind == "settle"
        assert event.network == "base"
        assert event.ok is True
        assert event.tx == "0xdeadbeef"

    def test_verify_has_no_tx(self):
        """Nothing settled yet, so there is no hash. Absent, not null."""
        stream = TrafficEventStream()
        event = stream._to_event(_events(VERIFY_FRAME)[0])
        assert event is not None
        assert event.kind == "verify"
        assert event.tx is None

    def test_ts_is_epoch_millis_not_seconds(self):
        """Reading it as seconds lands in the year 58,000 — silently."""
        stream = TrafficEventStream()
        event = stream._to_event(_events(SETTLE_FRAME)[0])
        assert event.timestamp.year == 2026

    def test_minimal_detail_mode_parses(self):
        """With X402_EVENTS_DETAIL=minimal every optional field is omitted."""
        stream = TrafficEventStream()
        frame = {"event": "settle", "data": '{"ts":1785256783513,"kind":"settle",'
                                            '"network":"base","ok":true}'}
        event = stream._to_event(frame)
        assert event is not None
        assert event.payer is None and event.amount is None and event.asset is None

    def test_a_malformed_frame_is_skipped_not_raised(self):
        """One bad message must never tear down a long-lived connection."""
        stream = TrafficEventStream()
        assert stream._to_event({"event": "settle", "data": "not json"}) is None
        assert stream._to_event({"event": "settle", "data": "[1,2,3]"}) is None


class TestClientSideFilters:
    """The facilitator has NO server-side filter by network — this is ours."""

    def _event(self, network, kind="settle"):
        return TrafficEvent(ts=1785256783513, kind=kind, network=network, ok=True)

    def test_network_filter_keeps_and_drops(self):
        stream = TrafficEventStream(networks=["base", "polygon"])
        assert stream._wanted(self._event("base")) is True
        assert stream._wanted(self._event("celo")) is False

    def test_network_filter_matches_the_canonical_slug(self):
        """'skale' is accepted INBOUND but never emitted; events say skale-base."""
        assert TrafficEventStream(networks=["skale-base"])._wanted(
            self._event("skale-base")
        ) is True
        assert TrafficEventStream(networks=["skale"])._wanted(
            self._event("skale-base")
        ) is False

    def test_kind_filter(self):
        stream = TrafficEventStream(kinds=["settle"])
        assert stream._wanted(self._event("base", "settle")) is True
        assert stream._wanted(self._event("base", "verify")) is False

    def test_no_filters_lets_everything_through(self):
        stream = TrafficEventStream()
        assert stream._wanted(self._event("anything")) is True


def test_read_timeout_is_disabled():
    """A finite read timeout would kill a healthy connection on a quiet rail."""
    assert TrafficEventStream()._timeout.read is None


def test_url_is_built_from_base():
    assert TrafficEventStream("https://example.com/").url == "https://example.com/events"


def test_event_kinds_match_the_facilitator():
    assert EVENT_KINDS == ("verify", "settle")


class TestRicherMetadata:
    """Fields added 2026-07-30 so an event says WHAT was bought, not just how much."""

    FRAME = {
        "event": "settle",
        "data": '{"ts":1785432522148,"kind":"settle","network":"base","ok":true,'
                '"payer":"0xe4dc","tx":"0xd8c1","amount":"1000000","asset":"0x8335",'
                '"resource":"https://api.example.com/premium","payTo":"0xseller",'
                '"description":"Premium feed","scheme":"exact"}',
    }

    def test_parses_the_endpoint_and_seller(self):
        event = TrafficEventStream()._to_event(self.FRAME)
        assert event is not None
        assert event.resource == "https://api.example.com/premium"
        assert event.pay_to == "0xseller"
        assert event.scheme == "exact"

    def test_payTo_arrives_camelCase_on_the_wire(self):
        # The facilitator emits payTo; the Python attribute is pay_to. Reading
        # the snake_case key off the wire would silently yield None.
        event = TrafficEventStream()._to_event(self.FRAME)
        assert event.pay_to is not None

    def test_events_without_the_new_fields_still_parse(self):
        """minimal detail mode, and any consumer pinned to an older facilitator."""
        event = TrafficEventStream()._to_event(
            {"event": "settle", "data": '{"ts":1,"kind":"settle","network":"base","ok":true}'}
        )
        assert event is not None
        assert event.resource is None and event.pay_to is None
