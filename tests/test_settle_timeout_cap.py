"""``X402Config.max_settle_timeout``: a ceiling on the settle timeout.

A network the SDK knows carries its own settle timeout (``settle_timeout_seconds``:
900 on Ethereum L1, 90 elsewhere) and it wins over ``settle_timeout``, which is
only the fallback for a network without one. ``max_settle_timeout`` caps
whichever of the two applies; ``None`` leaves every network exactly as before.
"""
from __future__ import annotations

import math
from decimal import Decimal

import httpx
import pytest

from tests.receipt_rail import RECIPIENT as RAIL_RECIPIENT
from tests.receipt_rail import Facilitator, x_payment
from uvd_x402_sdk import X402Client, X402Config, is_transient_error
from uvd_x402_sdk.bindings import binding_window_seconds
from uvd_x402_sdk.exceptions import TimeoutError as X402TimeoutError
from uvd_x402_sdk.models import PaymentPayload
from uvd_x402_sdk.networks import get_network, list_networks, normalize_network

RECIPIENT = "0x1234567890123456789012345678901234567890"
FACILITATOR = "https://facilitator.test"
PRICE = Decimal("0.01")
#: Two ways a network is unknown: a name the registry does not hold, and a
#: CAIP-2 id that ``normalize_network`` refuses (``ValueError``).
UNKNOWN = "not-a-network"
UNKNOWN_CAIP2 = "eip155:999999"

#: Every network the registry knows, disabled ones included.
NETWORKS = list_networks(enabled_only=False)


def _client(**config) -> X402Client:
    return X402Client(recipient_address=RECIPIENT, **config)


def _payload(network: str) -> PaymentPayload:
    return PaymentPayload(
        x402Version=1,
        scheme="exact",
        network=network,
        payload={
            "signature": "0xsig",
            "authorization": {
                "from": "0xSender",
                "to": RECIPIENT,
                "value": "10000",
                "validAfter": "0",
                "validBefore": "9999999999",
                "nonce": "0x01",
            },
        },
    )


def test_the_registry_still_has_the_timeouts_these_tests_are_about():
    assert get_network("ethereum").settle_timeout_seconds == 900.0
    assert get_network("base").settle_timeout_seconds == 90.0
    assert get_network(UNKNOWN) is None
    with pytest.raises(ValueError):
        normalize_network(UNKNOWN_CAIP2)
    assert all(network.settle_timeout_seconds > 0 for network in NETWORKS)


# ---------------------------------------------------------------------------
# Which timeout a settle gets
# ---------------------------------------------------------------------------


def test_without_a_ceiling_every_network_keeps_its_own_timeout():
    client = _client()

    assert client.config.max_settle_timeout is None
    assert {n.name: client._get_settle_timeout(n.name) for n in NETWORKS} == {
        n.name: n.settle_timeout_seconds for n in NETWORKS
    }
    assert client._get_settle_timeout("base") == 90.0
    assert client._get_settle_timeout("ethereum") == 900.0
    assert client._get_settle_timeout("eip155:1") == 900.0
    assert client._get_settle_timeout(UNKNOWN) == 55.0
    assert client._get_settle_timeout(UNKNOWN_CAIP2) == 55.0


def test_a_ceiling_below_the_network_timeout_caps_it_on_every_network():
    client = _client(max_settle_timeout=50)

    assert {n.name: client._get_settle_timeout(n.name) for n in NETWORKS} == {
        n.name: 50.0 for n in NETWORKS
    }
    assert client._get_settle_timeout("base") == 50.0
    assert client._get_settle_timeout("ethereum") == 50.0
    assert client._get_settle_timeout("eip155:1") == 50.0
    assert client._get_settle_timeout(UNKNOWN) == 50.0, "the fallback is capped too"
    assert client._get_settle_timeout(UNKNOWN_CAIP2) == 50.0, "the fallback is capped too"


def test_a_ceiling_above_the_network_timeout_leaves_the_network_timeout():
    client = _client(max_settle_timeout=120)

    assert client._get_settle_timeout("base") == 90.0
    assert client._get_settle_timeout("ethereum") == 120.0
    assert client._get_settle_timeout(UNKNOWN) == 55.0
    assert client._get_settle_timeout(UNKNOWN_CAIP2) == 55.0
    assert _client(max_settle_timeout=1000)._get_settle_timeout("ethereum") == 900.0


def test_settle_timeout_is_still_only_the_fallback():
    """Raising ``settle_timeout`` does not move a network that has its own, with
    or without a ceiling: only the ceiling lowers a network's timeout."""
    raised = _client(settle_timeout=500)
    assert raised._get_settle_timeout("base") == 90.0
    assert raised._get_settle_timeout("ethereum") == 900.0
    assert raised._get_settle_timeout(UNKNOWN) == 500.0

    capped = _client(settle_timeout=500, max_settle_timeout=200)
    assert capped._get_settle_timeout("base") == 90.0
    assert capped._get_settle_timeout("ethereum") == 200.0
    assert capped._get_settle_timeout(UNKNOWN) == 200.0


# ---------------------------------------------------------------------------
# What the POST /settle is sent with
# ---------------------------------------------------------------------------


class _Facilitator:
    """Records the timeout httpx applies to every ``/settle`` it is sent.

    ``time_out_first`` makes the first ``/settle`` time out; the fallback's
    resend then gets the opaque ``400`` of an authorization the chain already
    executed, so nothing confirms it and the SDK raises its timeout.
    """

    def __init__(self, network: str, time_out_first: bool = False) -> None:
        self.network = network
        self.time_out_first = time_out_first
        self.settle_timeouts: list = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path != "/settle":
            return httpx.Response(404, json={})
        self.settle_timeouts.append(request.extensions["timeout"])
        if self.time_out_first:
            if len(self.settle_timeouts) == 1:
                raise httpx.ReadTimeout("held past the timeout", request=request)
            return httpx.Response(400, json={"error": "contract_call_failed (ref: local)"})
        return httpx.Response(
            200,
            json={
                "success": True,
                "transaction": "0x" + "ab" * 32,
                "network": self.network,
                "payer": "0xSender",
            },
        )


def _seller(facilitator: _Facilitator, **config) -> X402Client:
    return _client(
        facilitator_url=FACILITATOR,
        http_client=httpx.Client(transport=httpx.MockTransport(facilitator.handler)),
        **config,
    )


def _every_phase(seconds: float) -> dict:
    return {"connect": seconds, "read": seconds, "write": seconds, "pool": seconds}


@pytest.mark.parametrize(
    "network, ceiling, expected",
    [
        ("base", None, 90.0),
        ("ethereum", None, 900.0),
        ("base", 50, 50.0),
        ("ethereum", 50, 50.0),
        ("base", 120, 90.0),
        ("ethereum", 120, 120.0),
    ],
)
def test_the_settle_is_sent_with_the_capped_timeout(network, ceiling, expected):
    """Per request, on every phase: httpx replaces the client's default timeout
    (``read=settle_timeout``) with it, so the ceiling is what the settle waits."""
    facilitator = _Facilitator(network)

    settled = _seller(facilitator, max_settle_timeout=ceiling).settle_payment(
        _payload(network), PRICE
    )

    assert settled.success
    assert facilitator.settle_timeouts == [_every_phase(expected)]


def test_a_settle_cut_short_by_the_ceiling_reports_the_ceiling_and_stays_transient():
    """The timeout the SDK raises names the ceiling, not the network's 900 s,
    and it is transient: the paywall answers 503, never 402. The fallback's
    resend keeps its own timeout, which the ceiling does not cap."""
    facilitator = _Facilitator("ethereum", time_out_first=True)

    with pytest.raises(X402TimeoutError) as caught:
        _seller(facilitator, max_settle_timeout=50).settle_payment(_payload("ethereum"), PRICE)

    assert caught.value.timeout_seconds == 50.0
    assert is_transient_error(caught.value)
    first, fallback = facilitator.settle_timeouts
    assert first == _every_phase(50.0)
    assert fallback == _every_phase(30.0)


@pytest.mark.parametrize(
    "ceiling, settle_requests",
    [
        (None, 1),  # the network's 90 s: the held answer arrives
        (5.0, 1),  # a ceiling above the hold changes nothing
        (0.3, 2),  # the settle gives up at 0.3 s and the fallback recovers it
    ],
)
def test_over_a_real_socket_the_settle_stops_waiting_at_the_ceiling(ceiling, settle_requests):
    """The receipt rail confirms the payment and sits on the answer for 0.8 s.
    With a ceiling below that, the settle stops waiting and the fallback's
    resend, under the same key, gets the admitted settle back."""
    rail = Facilitator("receipts", hold_after_confirm=0.8)
    try:
        client = X402Client(
            recipient_address=RAIL_RECIPIENT,
            facilitator_url=rail.url,
            max_settle_timeout=ceiling,
        )
        settled = client.settle_payment(client.extract_payload(x_payment()), PRICE)
    finally:
        rail.close()

    assert settled.success and settled.get_transaction_hash() == "0xf00d1"
    assert rail.executed == 1 and rail.moved == 1
    assert len(rail.keys("/settle")) == settle_requests
    assert settled.idempotent_replayed is (settle_requests == 2)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value", [0, 0.0, -1, -0.5, math.inf, -math.inf, math.nan, True, "50"]
)
def test_an_invalid_ceiling_is_refused_at_construction(value):
    with pytest.raises(ValueError, match="max_settle_timeout"):
        X402Config(recipient_evm=RECIPIENT, max_settle_timeout=value)
    with pytest.raises(ValueError, match="max_settle_timeout"):
        _client(max_settle_timeout=value)


def test_a_valid_ceiling_is_kept_as_seconds():
    assert X402Config(recipient_evm=RECIPIENT).max_settle_timeout is None
    whole = X402Config(recipient_evm=RECIPIENT, max_settle_timeout=50)
    assert whole.max_settle_timeout == 50.0 and isinstance(whole.max_settle_timeout, float)
    assert X402Config(recipient_evm=RECIPIENT, max_settle_timeout=0.3).max_settle_timeout == 0.3


# ---------------------------------------------------------------------------
# What the ceiling does not touch
# ---------------------------------------------------------------------------


def test_the_ceiling_does_not_shorten_the_life_of_a_payment_binding():
    """A settle the seller stopped waiting for can still complete at the
    facilitator, and the resend that recovers its answer comes later: the key
    has to live at least as long as without the ceiling."""
    timeouts = {"recipient_evm": RECIPIENT, "verify_timeout": 100, "settle_timeout": 200}

    assert binding_window_seconds(X402Config(**timeouts)) == 900
    assert binding_window_seconds(X402Config(**timeouts, max_settle_timeout=50)) == 900
