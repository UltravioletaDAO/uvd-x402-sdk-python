"""``X-UVD-Stack-Key``: a service of Ultravioleta DAO presents its key to the
facilitator of Ultravioleta DAO, and to nobody else; a key read badly never
breaks a payment.

Two kinds of transport, each for what it can prove:

* A real socket to a local stand-in for the facilitator (``127.0.0.1``, named
  in ``stack_key_hosts``: plain http is accepted only there). A header value
  with a carriage return, a line feed or surrounding whitespace is refused by
  h11 BEFORE anything is sent (``httpx.LocalProtocolError``, with the value in
  its message), and ``httpx.MockTransport`` never runs h11, so a mocked
  transport would pass the very key that breaks every ``/verify`` and
  ``/settle`` in production.
* ``httpx.MockTransport`` for the host rule, because it lets the requests name
  the real hosts (``https://facilitator.ultravioletadao.xyz`` and third
  parties) with nothing leaving the process.

The keys are synthetic, built here with the shape of a key and the value of
none: ``uvdsk_`` and base64url characters.
"""
from __future__ import annotations

import base64
import copy
import dataclasses
import inspect
import json
import logging
import pickle
import socket
import threading
import time
import uuid
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import httpx
import pytest

from uvd_x402_sdk import X402Client, X402Config
from uvd_x402_sdk import stack_key as stack_key_module
from uvd_x402_sdk.discovery import BazaarClient
from uvd_x402_sdk.dx402 import anchor_evidence, available_backends
from uvd_x402_sdk.erc8004 import Erc8004Client
from uvd_x402_sdk.escrow import EscrowClient
from uvd_x402_sdk.events import TrafficEventStream
from uvd_x402_sdk.exceptions import FacilitatorError, StackKeyRedirectError
from uvd_x402_sdk.receipts import PurchaseContext, get_receipt
from uvd_x402_sdk.stack_key import stack_key_allowed

# The contract's names, written out: the tests pin them, not the SDK's constants.
HEADER = "X-UVD-Stack-Key"
ENV = "UVD_STACK_KEY"
HOUSE = "https://facilitator.ultravioletadao.xyz"
# The local stand-in: plain http is accepted only to a loopback host listed here.
LOCAL = ["127.0.0.1"]

_BODY = "Synthetic-Test-Key_" * 10  # base64url characters only


def _key(length: int) -> str:
    """A synthetic key with ``length`` characters after ``uvdsk_``."""
    return "uvdsk_" + _BODY[:length]


KEY = _key(48)
# What must never appear in an error, a log, a repr or a warning.
SECRET = "Synthetic-Test-Key"

RECIPIENT = "0x1234567890123456789012345678901234567890"
PAYER = "0x" + "ab" * 20
PRICE = Decimal("0.01")
X_PAYMENT = base64.b64encode(
    json.dumps(
        {
            "x402Version": 1,
            "scheme": "exact",
            "network": "base",
            "payload": {
                "signature": "0xsig",
                "authorization": {
                    "from": PAYER,
                    "to": RECIPIENT,
                    "value": "10000",
                    "validAfter": "0",
                    "validBefore": "9999999999",
                    "nonce": "0x01",
                },
            },
        }
    ).encode("utf-8")
).decode("ascii")


def _answer(method: str, path: str) -> tuple[int, Any]:
    """Enough of the facilitator for every call to finish."""
    if path == "/verify":
        return 200, {"isValid": True, "payer": PAYER}
    if path == "/settle":
        return 200, {"success": True, "transaction": "0xf00d", "network": "base", "payer": PAYER}
    if path == "/accepts":
        return 200, {"accepts": []}
    if path == "/supported":
        return 200, {"kinds": [{"x402Version": 1, "scheme": "exact", "network": "base"}]}
    if method == "POST":
        return 200, {"success": True, "network": "base"}
    return 200, {}


class _Recorder:
    """The ``X-UVD-Stack-Key`` values of every request, in arrival order."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []

    def keys(self, path: str | None = None, host: str | None = None) -> list[list[str]]:
        return [
            r["stack_key"]
            for r in self.requests
            if (path is None or r["path"] == path) and (host is None or r["host"] == host)
        ]


class _DualStack(ThreadingHTTPServer):
    """Listens on ``::`` and IPv4, so ``http://localhost:<port>`` reaches it."""

    address_family = socket.AF_INET6

    def server_bind(self) -> None:
        self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        super().server_bind()


class _Facilitator(_Recorder):
    """A local stand-in over a real socket (h11 runs on every request).

    ``dual_stack`` makes it reachable as ``localhost`` (``localhost_url``), a
    host that ``LOCAL`` does not list. ``redirect_to`` answers every request
    with ``redirect_status`` to that base URL, path kept.
    """

    def __init__(self, dual_stack: bool = False) -> None:
        super().__init__()
        self.settle_delays: list[float] = []  # seconds, popped per /settle
        self.fail_verify = False
        self.redirect_to: str | None = None
        self.redirect_status = 302
        local = self

        class Handler(BaseHTTPRequestHandler):
            def _serve(self, method: str) -> None:
                length = int(self.headers.get("Content-Length", "0") or 0)
                if length:
                    self.rfile.read(length)
                path = self.path.split("?", 1)[0]
                local.requests.append(
                    {
                        "method": method,
                        "host": "127.0.0.1",
                        "path": path,
                        "stack_key": self.headers.get_all(HEADER) or [],
                    }
                )
                if local.redirect_to is not None:
                    self.send_response(local.redirect_status)
                    self.send_header("Location", local.redirect_to + self.path)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                status, body = local.answer(method, path)
                data = json.dumps(body).encode("utf-8")
                try:
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                except OSError:
                    pass  # the client gave up (the settle timeout test)

            def do_GET(self) -> None:  # noqa: N802 - http.server's name
                self._serve("GET")

            def do_POST(self) -> None:  # noqa: N802 - http.server's name
                self._serve("POST")

            def log_message(self, *args: object) -> None:
                pass

        if dual_stack:
            self._server: ThreadingHTTPServer = _DualStack(("::", 0), Handler)
        else:
            self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        port = self._server.server_address[1]
        self.url = f"http://127.0.0.1:{port}"
        self.localhost_url = f"http://localhost:{port}"
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def answer(self, method: str, path: str) -> tuple[int, Any]:
        if path == "/verify" and self.fail_verify:
            return 500, {"error": "internal_error"}
        if path == "/settle" and self.settle_delays:
            time.sleep(self.settle_delays.pop(0))
        return _answer(method, path)

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


class _Mocked(_Recorder):
    """Any host, https included, answered in process: nothing leaves it."""

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(
            {
                "method": request.method,
                "host": request.url.host,
                "path": request.url.path,
                "stack_key": request.headers.get_list(HEADER),
            }
        )
        # By the last segment: a routed facilitator lives under a path prefix.
        status, body = _answer(request.method, "/" + request.url.path.rsplit("/", 1)[-1])
        return httpx.Response(status, json=body)

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handler))

    def async_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler))


@pytest.fixture
def facilitator():
    local = _Facilitator()
    yield local
    local.close()


@pytest.fixture
def mocked():
    return _Mocked()


@pytest.fixture(autouse=True)
def _fresh_process(monkeypatch):
    """Each test is a process of its own: no warning given yet, no key in env."""
    monkeypatch.setattr(stack_key_module, "_warned", False)
    monkeypatch.setattr(stack_key_module, "_warned_host", False)
    monkeypatch.delenv(ENV, raising=False)


def _seller(facilitator: _Facilitator, **config: Any) -> X402Client:
    config.setdefault("stack_key_hosts", LOCAL)
    return X402Client(recipient_address=RECIPIENT, facilitator_url=facilitator.url, **config)


def _house_seller(mocked: _Mocked, **config: Any) -> X402Client:
    """A client of ``facilitator.ultravioletadao.xyz`` (or ``facilitator_url``)."""
    return X402Client(recipient_address=RECIPIENT, http_client=mocked.client(), **config)


def _warnings(caplog) -> list[logging.LogRecord]:
    return [
        r
        for r in caplog.records
        if r.name == "uvd_x402_sdk.stack_key" and r.levelno == logging.WARNING
    ]


def _everything_logged(caplog) -> str:
    return "\n".join(f"{r.getMessage()} {r.args!r}" for r in caplog.records)


# -- A key configured well is on every request to the facilitator


def test_verify_and_settle_carry_the_key(facilitator):
    result = _seller(facilitator, stack_key=KEY).process_payment(X_PAYMENT, PRICE)

    assert result.success
    assert facilitator.keys("/verify") == [[KEY]]
    assert facilitator.keys("/settle") == [[KEY]]


def test_the_settle_resent_after_a_timeout_carries_the_key(facilitator, monkeypatch):
    seller = _seller(facilitator, stack_key=KEY)
    monkeypatch.setattr(seller, "_get_settle_timeout", lambda network: 0.3)
    facilitator.settle_delays.append(1.5)

    result = seller.process_payment(X_PAYMENT, PRICE)

    assert result.success
    assert facilitator.keys("/settle") == [[KEY], [KEY]]


def test_accepts_and_every_read_of_the_facilitator_carry_the_key(facilitator):
    seller = _seller(facilitator, stack_key=KEY, supported_networks=["base"])

    seller.negotiate_accepts(
        [{"scheme": "exact", "network": "base", "maxAmountRequired": "1", "payTo": RECIPIENT}]
    )
    seller.get_version()
    seller.get_supported()
    seller.get_stats()
    seller.get_transactions()
    seller.get_blacklist()
    assert seller.health_check()
    seller.verify_routes()

    paths = [r["path"] for r in facilitator.requests]
    assert paths == [
        "/accepts", "/version", "/supported", "/api/stats",
        "/transactions", "/blacklist", "/health", "/supported",
    ]
    assert facilitator.keys() == [[KEY]] * len(paths)


@pytest.mark.parametrize("unset", [None, "", "  \r\n"], ids=["none", "empty", "blank"])
def test_without_a_key_nothing_is_sent_and_nothing_is_said(facilitator, caplog, unset):
    caplog.set_level(logging.DEBUG)

    _seller(facilitator, stack_key=unset).process_payment(X_PAYMENT, PRICE)
    _seller(facilitator).get_version()

    assert facilitator.keys() == [[], [], []]
    assert _warnings(caplog) == []


class _BareHttpClient:
    """A caller's double of the HTTP client, as old as the SDK: its ``get``
    knows no ``headers`` keyword (the shape of ``tests/test_facilitator_routing.py``)."""

    def __init__(self) -> None:
        self.urls: list[str] = []

    def get(self, url, timeout=None):
        self.urls.append(url)
        return httpx.Response(200, json={}, request=httpx.Request("GET", url))


def test_without_a_key_to_send_no_headers_keyword_is_added():
    """Without a key (or with one the host may not get), every GET is made
    exactly as before: a double that knows no ``headers`` keyword still works."""
    for config in ({}, {"stack_key": KEY, "facilitator_url": "https://facilitator.example"}):
        bare = _BareHttpClient()
        client = X402Client(recipient_address=RECIPIENT, http_client=bare, **config)
        client.get_version()
        client.get_supported()
        client.get_blacklist()
        assert client.health_check()
        assert len(bare.urls) == 4

    assert available_backends(HOUSE, client=_BareHttpClient()) == []


# -- A key read badly is trimmed, or left out; the payment never breaks


@pytest.mark.parametrize(
    "read",
    [KEY + "\r\n", KEY + "\n", KEY + "\r", " \t" + KEY + " \r\n"],
    ids=["crlf", "lf", "cr", "surrounding-whitespace"],
)
def test_a_key_read_with_whitespace_around_it_is_sent_trimmed(facilitator, caplog, read):
    caplog.set_level(logging.DEBUG)
    seller = _seller(facilitator, stack_key=read)

    result = seller.process_payment(X_PAYMENT, PRICE)

    assert result.success
    assert seller.config.stack_key == KEY
    assert facilitator.keys() == [[KEY], [KEY]]
    assert _warnings(caplog) == []


def test_a_key_assigned_after_construction_is_still_checked_before_sending(facilitator):
    seller = _seller(facilitator)
    seller.config.stack_key = KEY + "\r\n"  # bypasses the check at construction

    result = seller.process_payment(X_PAYMENT, PRICE)

    assert result.success
    assert facilitator.keys() == [[KEY], [KEY]]


UNUSABLE = {
    "newline-inside": KEY[:20] + "\n" + KEY[20:],
    "space-inside": KEY[:20] + " " + KEY[20:],
    "42-characters": _key(42),
    "129-characters": _key(129),
    "prefix-in-capitals": "UVDSK_" + _BODY[:48],
    "prefix-with-a-dash": "uvdsk-" + _BODY[:48],
    "no-prefix": _BODY[:48],
    "quoted": '"' + KEY + '"',
    "base64-padding": KEY + "=",
    "base64-plus": KEY[:-1] + "+",
    "junk-after-a-valid-key": KEY + "!",
    "bearer": "Bearer " + KEY,
    "bytes": KEY.encode("ascii"),
    "byte-order-mark-at-the-end": KEY + "﻿",  # only a leading one goes
}


@pytest.mark.parametrize("value", list(UNUSABLE.values()), ids=list(UNUSABLE))
def test_an_unusable_key_is_not_sent_and_the_payment_goes_through(facilitator, caplog, value):
    caplog.set_level(logging.DEBUG)
    seller = _seller(facilitator, stack_key=value)

    result = seller.process_payment(X_PAYMENT, PRICE)

    assert result.success
    assert seller.config.stack_key is None
    assert facilitator.keys() == [[], []]
    (warning,) = _warnings(caplog)
    assert SECRET not in warning.getMessage()
    assert SECRET not in _everything_logged(caplog)


def test_the_warning_is_given_once_per_process(facilitator, caplog):
    caplog.set_level(logging.DEBUG)
    bad = KEY[:20] + "\n" + KEY[20:]

    first = _seller(facilitator, stack_key=bad)
    second = _seller(facilitator, stack_key="uvdsk_short")
    Erc8004Client(base_url=facilitator.url, stack_key=bad, stack_key_hosts=LOCAL)
    first.process_payment(X_PAYMENT, PRICE)
    second.get_version()
    first.config.stack_key = bad  # checked again at send, still not repeated
    first.get_version()

    assert len(_warnings(caplog)) == 1
    assert facilitator.keys() == [[], [], [], []]


@pytest.mark.parametrize("length", [43, 128])
def test_the_shortest_and_the_longest_key_are_sent(facilitator, length):
    _seller(facilitator, stack_key=_key(length)).process_payment(X_PAYMENT, PRICE)

    assert facilitator.keys() == [[_key(length)], [_key(length)]]


# -- Only to a facilitator of Ultravioleta DAO: https to the house host, or an
#    added one; plain http only to a listed loopback host


ALLOWED = [
    (HOUSE + "/verify", None, True),
    ("https://FACILITATOR.UltravioletaDAO.xyz/verify", None, True),
    (HOUSE + ":8443/verify", None, True),
    (HOUSE, ["staging-facilitator.example"], True),  # the list adds, never removes
    ("https://staging-facilitator.example/x", ["staging-facilitator.example"], True),
    ("https://staging-facilitator.example/x", [" Staging-Facilitator.Example "], True),
    ("https://staging-facilitator.example/x", "staging-facilitator.example", True),
    ("http://127.0.0.1:8080/x", ["127.0.0.1"], True),
    ("http://localhost:8080/x", ["localhost"], True),
    ("https://10.0.0.5/x", ["10.0.0.5"], True),
    ("http://facilitator.ultravioletadao.xyz/verify", None, False),
    ("http://facilitator.ultravioletadao.xyz/verify", ["facilitator.ultravioletadao.xyz"], False),
    ("https://facilitator.example/verify", None, False),
    ("https://facilitator.example/verify", ["staging-facilitator.example"], False),
    ("https://facilitator.ultravioletadao.xyz.evil.example/verify", None, False),
    ("https://evil.example/facilitator.ultravioletadao.xyz", None, False),
    ("https://facilitator.ultravioletadao.xyz@evil.example/verify", None, False),
    ("https://sub.facilitator.ultravioletadao.xyz/verify", None, False),
    ("https://xfacilitator.ultravioletadao.xyz/verify", None, False),
    ("http://127.0.0.1:8080/x", None, False),
    ("http://127.0.0.1:8080/x", ["localhost"], False),
    ("http://10.0.0.5/x", ["10.0.0.5"], False),
    ("ftp://facilitator.ultravioletadao.xyz/x", None, False),
    ("facilitator.ultravioletadao.xyz/verify", None, False),
    ("", None, False),
]


@pytest.mark.parametrize("url,hosts,allowed", ALLOWED)
def test_where_the_key_may_travel(url, hosts, allowed):
    assert stack_key_allowed(url, hosts) is allowed


def test_the_key_reaches_the_house_facilitator_over_https(mocked):
    seller = _house_seller(mocked, stack_key=KEY)

    assert seller.process_payment(X_PAYMENT, PRICE).success
    seller.get_version()

    assert [r["host"] for r in mocked.requests] == ["facilitator.ultravioletadao.xyz"] * 3
    assert mocked.keys() == [[KEY]] * 3


@pytest.mark.parametrize("source", ["env", "option"])
@pytest.mark.parametrize(
    "hosts", [None, ["staging-facilitator.example"]], ids=["default", "listed"]
)
def test_another_host_never_gets_the_key(mocked, caplog, monkeypatch, source, hosts):
    caplog.set_level(logging.DEBUG)
    if source == "env":
        monkeypatch.setenv(ENV, KEY)
        monkeypatch.setenv("X402_RECIPIENT_EVM", RECIPIENT)
        monkeypatch.setenv("X402_FACILITATOR_URL", "https://facilitator.example")
        config = X402Config.from_env()
        config.stack_key_hosts = hosts
    else:
        config = X402Config(
            recipient_evm=RECIPIENT,
            facilitator_url="https://facilitator.example",
            stack_key=KEY,
            stack_key_hosts=hosts,
        )
    seller = X402Client(config=config, http_client=mocked.client())

    assert seller.process_payment(X_PAYMENT, PRICE).success

    assert mocked.keys() == [[], []]
    (warning,) = _warnings(caplog)
    assert warning.getMessage().startswith(
        "stack key not sent: https://facilitator.example is not a house facilitator"
    )
    assert SECRET not in _everything_logged(caplog)


@pytest.mark.parametrize(
    "hosts", [None, ["facilitator.ultravioletadao.xyz"]], ids=["default", "listed"]
)
def test_plain_http_to_the_house_host_never_gets_the_key(mocked, caplog, hosts):
    caplog.set_level(logging.DEBUG)
    seller = _house_seller(
        mocked,
        stack_key=KEY,
        stack_key_hosts=hosts,
        facilitator_url="http://facilitator.ultravioletadao.xyz",
    )

    assert seller.process_payment(X_PAYMENT, PRICE).success

    assert mocked.keys() == [[], []]
    (warning,) = _warnings(caplog)
    message = warning.getMessage()
    assert "http://facilitator.ultravioletadao.xyz is not a house facilitator" in message


@pytest.mark.parametrize(
    "url,hosts,sent",
    [
        ("http://127.0.0.1:8080", None, False),
        ("http://127.0.0.1:8080", ["localhost"], False),
        ("http://127.0.0.1:8080", ["127.0.0.1"], True),
        ("http://localhost:8080", ["localhost"], True),
    ],
)
def test_plain_http_to_loopback_needs_the_explicit_list(mocked, url, hosts, sent):
    _house_seller(mocked, stack_key=KEY, stack_key_hosts=hosts, facilitator_url=url).get_version()

    assert mocked.keys() == [[KEY] if sent else []]


def test_an_added_host_gets_the_key_and_the_house_keeps_it(mocked):
    staging = ["staging-facilitator.example"]

    _house_seller(
        mocked, stack_key=KEY, stack_key_hosts=staging,
        facilitator_url="https://staging-facilitator.example",
    ).get_version()
    _house_seller(mocked, stack_key=KEY, stack_key_hosts=staging).get_version()

    assert [r["host"] for r in mocked.requests] == [
        "staging-facilitator.example", "facilitator.ultravioletadao.xyz",
    ]
    assert mocked.keys() == [[KEY], [KEY]]


@pytest.mark.parametrize(
    "url",
    [
        "https://facilitator.ultravioletadao.xyz.evil.example",
        "https://evil.example/facilitator.ultravioletadao.xyz",
        "https://facilitator.ultravioletadao.xyz@evil.example",
        "https://sub.facilitator.ultravioletadao.xyz",
        "https://xfacilitator.ultravioletadao.xyz",
    ],
    ids=["suffix", "path", "userinfo", "subdomain", "prefix"],
)
def test_a_lookalike_of_the_house_host_never_gets_the_key(mocked, url):
    _house_seller(mocked, stack_key=KEY, facilitator_url=url).get_version()

    assert mocked.keys() == [[]]


def test_the_host_warning_is_given_once_and_names_only_the_origin(mocked, caplog):
    caplog.set_level(logging.DEBUG)

    first = _house_seller(
        mocked, stack_key=KEY, facilitator_url="https://operator:hunter2@evil.example/base"
    )
    first.get_version()
    first.get_blacklist()
    _house_seller(mocked, stack_key=KEY, facilitator_url="https://other.example").get_version()

    assert mocked.keys() == [[], [], []]
    (warning,) = _warnings(caplog)
    message = warning.getMessage()
    assert message.startswith("stack key not sent: https://evil.example is not a house facilitator")
    assert "hunter2" not in message and "/base" not in message
    assert SECRET not in _everything_logged(caplog)


def test_a_third_party_facilitator_routed_by_network_never_gets_the_key(mocked):
    seller = _house_seller(
        mocked,
        stack_key=KEY,
        supported_networks=["base", "avalanche"],
        facilitator_by_network={"base": "https://api.cdp.example/x402", "*": HOUSE},
    )

    seller.process_payment(X_PAYMENT, PRICE)
    seller.get_supported(network="base")
    assert seller.health_check(network="base")
    seller.negotiate_accepts([{"scheme": "exact", "network": "avalanche"}])
    assert seller.health_check(network="avalanche")

    assert mocked.keys(host="api.cdp.example") == [[], [], [], []]
    assert mocked.keys(host="facilitator.ultravioletadao.xyz") == [[KEY], [KEY]]


@pytest.mark.parametrize(
    "facilitator_url,resend_to,sent",
    [
        (HOUSE, "https://api.cdp.example/x402", False),
        ("https://api.cdp.example/x402", HOUSE, True),
    ],
    ids=["house-config-third-party-resend", "third-party-config-house-resend"],
)
def test_the_settle_resend_is_judged_on_the_url_it_goes_to(
    mocked, facilitator_url, resend_to, sent
):
    """The resend after a timeout, called without the settle's headers, still
    decides on the facilitator it is sent to, not on ``facilitator_url``."""
    seller = _house_seller(
        mocked,
        stack_key=KEY,
        facilitator_url=facilitator_url,
        supported_networks=["base"],
    )

    seller._check_settle_fallback({"x402Version": 1}, 1.0, resend_to)

    assert [r["host"] for r in mocked.requests] == [httpx.URL(resend_to).host]
    assert mocked.keys() == [[KEY] if sent else []]


async def test_erc8004_sends_the_key_to_the_house_and_to_no_other_host(mocked):
    house = Erc8004Client(stack_key=KEY)
    other = Erc8004Client(base_url="https://facilitator.example", stack_key=KEY)
    house._client = other._client = mocked.async_client()

    await _call(house, ERC8004_CALLS, "get_identity")
    await _call(other, ERC8004_CALLS, "get_identity")

    assert mocked.keys(host="facilitator.ultravioletadao.xyz") == [[KEY]]
    assert mocked.keys(host="facilitator.example") == [[]]


def test_a_seller_reached_through_fetch_never_gets_the_key(facilitator):
    """The seller shares the facilitator's host here: only a per-request header,
    never a default of the HTTP client, keeps the key from it."""
    eth_account = pytest.importorskip("eth_account")
    seller_site = _Facilitator()
    try:
        buyer = _seller(facilitator, stack_key=KEY)
        ephemeral = eth_account.Account.create()  # never funded, never shown
        buyer.connect_with_private_key("0x" + bytes(ephemeral.key).hex(), chain_name="base")

        response = buyer.fetch(f"{seller_site.url}/resource")

        assert response.status_code == 200
        assert seller_site.keys() == [[]]
        assert facilitator.requests == []
    finally:
        seller_site.close()


# -- Where the key comes from, and where it never shows


def test_from_env_reads_uvd_stack_key_and_a_config_built_by_hand_does_not(monkeypatch):
    monkeypatch.setenv(ENV, KEY + "\r\n")
    monkeypatch.setenv("X402_RECIPIENT_EVM", RECIPIENT)

    assert X402Config.from_env().stack_key == KEY
    assert X402Config(recipient_evm=RECIPIENT).stack_key is None


def test_the_key_is_not_in_repr_str_or_to_dict():
    config = X402Config(recipient_evm=RECIPIENT, stack_key=KEY)
    stream = TrafficEventStream(stack_key=KEY)
    shown = [
        repr(config), str(config), json.dumps(config.to_dict()),
        repr(stream.headers),
    ]
    for client in (
        X402Client(config=config), Erc8004Client(stack_key=KEY), EscrowClient(stack_key=KEY),
        BazaarClient(stack_key=KEY), stream,
    ):
        shown += [repr(client), str(client)]

    assert config.stack_key == KEY
    assert all(SECRET not in text for text in shown)


def test_the_key_is_not_in_a_facilitator_error_nor_in_the_logs(facilitator, caplog):
    caplog.set_level(logging.DEBUG)
    seller = _seller(facilitator, stack_key=KEY)

    seller.process_payment(X_PAYMENT, PRICE)
    facilitator.fail_verify = True
    with pytest.raises(FacilitatorError) as caught:
        seller.process_payment(X_PAYMENT, PRICE)

    error = caught.value
    assert facilitator.keys() == [[KEY], [KEY], [KEY]]
    for text in (str(error), repr(error), json.dumps(error.to_dict(), default=str)):
        assert SECRET not in text
    assert SECRET not in _everything_logged(caplog)


# -- ERC-8004: every read and write of the facilitator, the relayed ratings included

RATER = "0x" + "11" * 20
SOLANA_AGENT = "11111111111111111111111111111111"
SOLANA_RATER = "So11111111111111111111111111111111111111112"
SIGNATURE = "0x" + "22" * 65

# Every public coroutine of Erc8004Client that talks to the facilitator, with
# arguments it sends as they are. `resolve_agent_uri` fetches the agent's own
# URL (below); `wait_for_registration` is `get_register_status` in a loop.
ERC8004_CALLS: dict[str, tuple] = {
    "get_identity": (("base", 1), {}),
    "get_identity_by_owner": (("base", RATER), {}),
    "get_reputation": (("base", 1), {}),
    "submit_feedback": (("base", 1, 90), {}),
    "prepare_relayed_feedback": (("base", 1, RATER, 90), {}),
    "submit_relayed_feedback": (
        ("base", 1, RATER, 90), {"deadline": 1, "nonce": "0x01", "signature": SIGNATURE}
    ),
    "prepare_solana_feedback": (("solana", SOLANA_AGENT, SOLANA_RATER, 90), {}),
    "submit_solana_feedback": (
        ("solana", SOLANA_AGENT, SOLANA_RATER, 90), {"transaction": "AQ=="}
    ),
    "revoke_feedback": (("base", 1, 0), {}),
    "get_feedback_metadata": ((), {}),
    "prepare_relayed_response": (("base", 1, RATER, RATER, 0, "https://example.invalid/r"), {}),
    "submit_relayed_response": (
        ("base", 1, RATER, RATER, 0, "https://example.invalid/r"),
        {"deadline": 1, "nonce": "0x01", "signature": SIGNATURE},
    ),
    "append_response": (("base", 1, 0, "thanks"), {}),
    "register_agent": (("base", "https://example.invalid/agent.json"), {}),
    "register_agent_async": (("base", "https://example.invalid/agent.json"), {}),
    "get_register_status": (("job-1",), {}),
    "get_register_info": ((), {}),
    "get_identity_metadata": (("base", 1, "k"), {}),
    "get_identity_total_supply": (("base",), {}),
}


def _public_coroutines(cls) -> set:
    return {
        name
        for name, member in inspect.getmembers(cls, inspect.iscoroutinefunction)
        if not name.startswith("_")
    }


def test_the_erc8004_table_covers_every_route_of_the_client():
    not_the_facilitator = {"resolve_agent_uri", "wait_for_registration"}
    assert _public_coroutines(Erc8004Client) - not_the_facilitator == set(ERC8004_CALLS)


async def _call(client: Any, calls: dict, name: str) -> None:
    args, kwargs = calls[name]
    try:
        await getattr(client, name)(*args, **kwargs)
    except Exception:  # noqa: BLE001 - only the request that left matters here
        pass


@pytest.mark.parametrize("name", list(ERC8004_CALLS))
async def test_every_erc8004_route_carries_the_key(facilitator, name):
    client = Erc8004Client(base_url=facilitator.url, stack_key=KEY, stack_key_hosts=LOCAL)

    await _call(client, ERC8004_CALLS, name)

    assert facilitator.requests, f"{name} sent nothing"
    assert facilitator.keys() == [[KEY]] * len(facilitator.requests)


@pytest.mark.parametrize("name", ["prepare_relayed_feedback", "submit_relayed_feedback"])
async def test_the_relayed_rating_path_with_a_key_read_badly(facilitator, caplog, name):
    caplog.set_level(logging.DEBUG)
    args, kwargs = ERC8004_CALLS[name]

    trimmed = Erc8004Client(
        base_url=facilitator.url, stack_key=KEY + "\r\n", stack_key_hosts=LOCAL
    )
    answer = await getattr(trimmed, name)(*args, **kwargs)
    assert answer.success
    assert facilitator.keys() == [[KEY]]

    unusable = Erc8004Client(
        base_url=facilitator.url, stack_key=KEY[:20] + "\n" + KEY[20:], stack_key_hosts=LOCAL
    )
    answer = await getattr(unusable, name)(*args, **kwargs)
    assert answer.success
    assert facilitator.keys() == [[KEY], []]
    (warning,) = _warnings(caplog)
    assert SECRET not in _everything_logged(caplog)


async def test_without_a_key_erc8004_sends_none(facilitator):
    client = Erc8004Client(base_url=facilitator.url, stack_key_hosts=LOCAL)

    for name in ERC8004_CALLS:
        await _call(client, ERC8004_CALLS, name)

    assert len(facilitator.requests) >= len(ERC8004_CALLS)
    assert all(keys == [] for keys in facilitator.keys())


async def test_the_agent_uri_fetched_by_resolve_agent_uri_never_gets_the_key(facilitator):
    """Same host as the facilitator here: only a per-request header keeps the
    key from a URL the agent's owner chose."""
    agent_site = _Facilitator()
    try:
        client = Erc8004Client(base_url=facilitator.url, stack_key=KEY, stack_key_hosts=LOCAL)

        await _call(client, ERC8004_CALLS, "get_identity")
        try:
            await client.resolve_agent_uri(f"{agent_site.url}/agent.json")
        except Exception:  # noqa: BLE001 - `{}` is not a registration file
            pass

        assert facilitator.keys() == [[KEY]]
        assert agent_site.keys() == [[]]
    finally:
        agent_site.close()


# -- Every other client of the SDK that talks to the facilitator

ESCROW_CALLS: dict[str, tuple] = {
    "create_escrow": (("payment-header", {}), {}),
    "get_escrow": (("e1",), {}),
    "release": (("e1",), {}),
    "request_refund": (("e1", "not delivered"), {}),
    "approve_refund": (("r1",), {}),
    "reject_refund": (("r1", "delivered"), {}),
    "get_refund": (("r1",), {}),
    "open_dispute": (("e1", "not delivered"), {}),
    "submit_evidence": (("d1", "logs"), {}),
    "get_dispute": (("d1",), {}),
    "list_escrows": ((), {}),
    "get_escrow_state": (("base", PAYER, RECIPIENT, "0x01"), {}),
    "health_check": ((), {}),
}

BAZAAR_CALLS: dict[str, tuple] = {
    "list_resources": ((), {}),
    "register_resource": (("https://example.invalid/resource",), {}),
}


def test_the_escrow_and_bazaar_tables_cover_every_route_of_their_clients():
    assert _public_coroutines(EscrowClient) == set(ESCROW_CALLS)
    assert _public_coroutines(BazaarClient) == set(BAZAAR_CALLS)


@pytest.mark.parametrize("name", list(ESCROW_CALLS))
async def test_every_escrow_route_carries_the_key(facilitator, name):
    client = EscrowClient(base_url=facilitator.url, stack_key=KEY, stack_key_hosts=LOCAL)

    await _call(client, ESCROW_CALLS, name)

    assert facilitator.requests, f"{name} sent nothing"
    assert facilitator.keys() == [[KEY]] * len(facilitator.requests)


@pytest.mark.parametrize("name", list(BAZAAR_CALLS))
async def test_every_bazaar_route_carries_the_key(facilitator, name):
    client = BazaarClient(base_url=facilitator.url, stack_key=KEY, stack_key_hosts=LOCAL)

    await _call(client, BAZAAR_CALLS, name)

    assert facilitator.requests, f"{name} sent nothing"
    assert facilitator.keys() == [[KEY]] * len(facilitator.requests)


async def test_escrow_and_bazaar_send_no_key_without_one(facilitator):
    escrow = EscrowClient(base_url=facilitator.url, stack_key_hosts=LOCAL)
    bazaar = BazaarClient(base_url=facilitator.url, stack_key_hosts=LOCAL)

    for name in ESCROW_CALLS:
        await _call(escrow, ESCROW_CALLS, name)
    for name in BAZAAR_CALLS:
        await _call(bazaar, BAZAAR_CALLS, name)

    assert len(facilitator.requests) == len(ESCROW_CALLS) + len(BAZAAR_CALLS)
    assert all(keys == [] for keys in facilitator.keys())


async def test_the_escrow_api_default_host_does_not_get_the_key(mocked):
    """``EscrowClient`` defaults to the Escrow API, not the facilitator."""
    client = EscrowClient(stack_key=KEY)
    client._client = mocked.async_client()

    await _call(client, ESCROW_CALLS, "get_escrow")

    assert mocked.keys() == [[]]


ADVANCED_ESCROW_CALLS = [
    "authorize", "release_via_facilitator", "refund_via_facilitator", "query_escrow_state",
]


def _advanced_escrow(facilitator: _Facilitator, **kwargs: Any):
    pytest.importorskip("web3")
    eth_account = pytest.importorskip("eth_account")
    from uvd_x402_sdk.advanced_escrow import AdvancedEscrowClient

    ephemeral = eth_account.Account.create()  # never funded, never shown
    return AdvancedEscrowClient(
        private_key="0x" + bytes(ephemeral.key).hex(),
        chain_id=8453,
        rpc_url="http://127.0.0.1:9",
        facilitator_url=facilitator.url,
        **kwargs,
    )


@pytest.mark.parametrize("name", ADVANCED_ESCROW_CALLS)
def test_every_escrow_settle_and_the_state_query_carry_the_key(facilitator, name):
    client = _advanced_escrow(facilitator, stack_key=KEY, stack_key_hosts=LOCAL)
    info = client.build_payment_info("0x" + "33" * 20, 10_000)

    getattr(client, name)(info)

    assert [r["path"] for r in facilitator.requests] == [
        "/escrow/state" if name == "query_escrow_state" else "/settle"
    ]
    assert facilitator.keys() == [[KEY]]


def test_the_escrow_settles_send_no_key_without_one(facilitator):
    client = _advanced_escrow(facilitator, stack_key_hosts=LOCAL)
    info = client.build_payment_info("0x" + "33" * 20, 10_000)

    for name in ADVANCED_ESCROW_CALLS:
        getattr(client, name)(info)

    assert facilitator.keys() == [[]] * len(ADVANCED_ESCROW_CALLS)


async def test_the_traffic_stream_carries_the_key_and_keeps_it_out_of_its_headers(facilitator):
    stream = TrafficEventStream(base_url=facilitator.url, stack_key=KEY, stack_key_hosts=LOCAL)

    assert list(stream) == []
    async with TrafficEventStream(
        base_url=facilitator.url, stack_key=KEY, stack_key_hosts=LOCAL
    ) as astream:
        assert [event async for event in astream] == []
    with TrafficEventStream(base_url=facilitator.url, stack_key_hosts=LOCAL) as keyless:
        assert list(keyless) == []

    assert [r["path"] for r in facilitator.requests] == ["/events"] * 3
    assert facilitator.keys() == [[KEY], [KEY], []]
    assert HEADER not in stream.headers


def test_dx402_anchor_and_backends_carry_the_key(facilitator):
    pytest.importorskip("cryptography")
    common = dict(
        payment_id_value="0x" + "ab" * 32,
        network="base",
        tx_hash="0x" + "cd" * 32,
        payer="0x" + "11" * 20,
        payee="0x" + "22" * 20,
        payer_key=bytes(range(32)),
        facilitator=facilitator.url,
    )

    anchor_evidence(b"response body", stack_key=KEY, stack_key_hosts=LOCAL, **common)
    available_backends(facilitator.url, stack_key=KEY, stack_key_hosts=LOCAL)
    with httpx.Client() as own:  # the caller's client, the other branch
        anchor_evidence(
            b"response body", stack_key=KEY, stack_key_hosts=LOCAL, client=own, **common
        )
        available_backends(facilitator.url, stack_key=KEY, stack_key_hosts=LOCAL, client=own)
    anchor_evidence(b"response body", stack_key_hosts=LOCAL, **common)
    available_backends(facilitator.url, stack_key_hosts=LOCAL)

    assert [r["path"] for r in facilitator.requests] == ["/dx402/anchor", "/dx402/stats"] * 3
    assert facilitator.keys() == [[KEY], [KEY], [KEY], [KEY], [], []]


def test_dx402_with_a_requests_session_carries_the_key(facilitator):
    requests = pytest.importorskip("requests")
    with requests.Session() as session:
        available_backends(facilitator.url, stack_key=KEY, stack_key_hosts=LOCAL, client=session)

    assert facilitator.keys("/dx402/stats") == [[KEY]]


def test_a_receipt_lookup_carries_the_key(facilitator):
    for key in (KEY, None):
        with httpx.Client() as http:
            try:
                get_receipt(
                    http, str(uuid.uuid4()), PurchaseContext(),
                    issuer=facilitator.url, stack_key=key, stack_key_hosts=LOCAL,
                )
            except ValueError:
                pass  # `{}` holds no receipt; the request is what is checked

    assert [r["path"].rsplit("/", 1)[0] for r in facilitator.requests] == ["/receipts"] * 2
    assert facilitator.keys() == [[KEY], []]


# -- The key does not follow redirects (tests/test_stack_key_redirect.py has
#    more): a request carrying it goes with redirects off,
#    whatever the HTTP client would do, and a redirect answered to it is an
#    error; the key is never sent again


@pytest.fixture
def detour():
    """A listed house (127.0.0.1) that redirects everything to ``localhost``,
    which ``LOCAL`` does not list."""
    house = _Facilitator()
    elsewhere = _Facilitator(dual_stack=True)
    house.redirect_to = elsewhere.localhost_url
    yield house, elsewhere
    house.close()
    elsewhere.close()


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_x402client_raises_on_a_redirect_and_never_follows_it(detour, status):
    house, elsewhere = detour
    house.redirect_status = status
    with httpx.Client(follow_redirects=True) as follows:
        seller = X402Client(
            recipient_address=RECIPIENT, facilitator_url=house.url, stack_key=KEY,
            stack_key_hosts=LOCAL, http_client=follows, supported_networks=["base"],
        )
        payload = seller.extract_payload(X_PAYMENT)
        calls = [
            lambda: seller.verify_payment(payload, PRICE),
            lambda: seller.settle_payment(payload, PRICE),
            lambda: seller.negotiate_accepts([{"scheme": "exact", "network": "base"}]),
            seller.get_version,
            seller.get_supported,
            seller.get_stats,
            seller.get_blacklist,
            seller.verify_routes,
        ]
        for call in calls:
            with pytest.raises(StackKeyRedirectError) as caught:
                call()
            assert caught.value.status_code == status
            assert SECRET not in str(caught.value)
        assert seller.health_check() is False

    assert elsewhere.requests == []
    assert house.keys() == [[KEY]] * (len(calls) + 1)


def test_the_settle_resend_raises_nothing_new_and_does_not_follow(detour):
    house, elsewhere = detour
    with httpx.Client(follow_redirects=True) as follows:
        seller = X402Client(
            recipient_address=RECIPIENT, facilitator_url=house.url, stack_key=KEY,
            stack_key_hosts=LOCAL, http_client=follows,
        )
        assert seller._check_settle_fallback({"x402Version": 1}, 1.0, house.url) is None

    assert elsewhere.requests == []
    assert house.keys() == [[KEY]]


async def test_the_async_clients_raise_on_a_redirect_and_never_follow_it(detour):
    house, elsewhere = detour
    erc8004 = Erc8004Client(base_url=house.url, stack_key=KEY, stack_key_hosts=LOCAL)
    escrow = EscrowClient(base_url=house.url, stack_key=KEY, stack_key_hosts=LOCAL)
    bazaar = BazaarClient(base_url=house.url, stack_key=KEY, stack_key_hosts=LOCAL)
    raising = [
        lambda: erc8004.get_identity("base", 1),
        lambda: escrow.get_escrow("e1"),
        lambda: escrow.release("e1"),
        lambda: bazaar.list_resources(),
        lambda: bazaar.register_resource("https://example.invalid/resource"),
    ]
    for call in raising:
        with pytest.raises(StackKeyRedirectError):
            await call()
    # An ERC-8004 write answers every failure with success=False, by contract.
    writes = [
        lambda: erc8004.prepare_relayed_feedback("base", 1, RATER, 90),
        lambda: erc8004.submit_relayed_feedback(
            "base", 1, RATER, 90, deadline=1, nonce="0x01", signature=SIGNATURE
        ),
    ]
    for call in writes:
        answer = await call()
        assert answer.success is False
        assert "does not follow redirects" in answer.error
    assert await escrow.health_check() is False

    assert elsewhere.requests == []
    assert house.keys() == [[KEY]] * (len(raising) + len(writes) + 1)


async def test_erc8004_does_not_follow_even_through_a_client_that_does(detour):
    house, elsewhere = detour
    client = Erc8004Client(base_url=house.url, stack_key=KEY, stack_key_hosts=LOCAL)
    client._client = httpx.AsyncClient(follow_redirects=True)

    for name in ("get_identity", "get_reputation", "prepare_relayed_feedback"):
        await _call(client, ERC8004_CALLS, name)

    assert elsewhere.requests == []
    assert house.keys() == [[KEY]] * 3


def test_the_escrow_settles_and_state_refuse_a_redirect(detour):
    house, elsewhere = detour
    client = _advanced_escrow(house, stack_key=KEY, stack_key_hosts=LOCAL)
    info = client.build_payment_info("0x" + "33" * 20, 10_000)

    for name in ("authorize", "release_via_facilitator", "refund_via_facilitator"):
        result = getattr(client, name)(info)
        assert result.success is False
        assert "does not follow redirects" in result.error
    with pytest.raises(StackKeyRedirectError):
        client.query_escrow_state(info)

    assert elsewhere.requests == []
    assert house.keys() == [[KEY]] * 4


async def test_the_event_stream_refuses_a_redirect(detour):
    house, elsewhere = detour

    with pytest.raises(StackKeyRedirectError):
        list(TrafficEventStream(base_url=house.url, stack_key=KEY, stack_key_hosts=LOCAL))
    with pytest.raises(StackKeyRedirectError):
        async with TrafficEventStream(
            base_url=house.url, stack_key=KEY, stack_key_hosts=LOCAL
        ) as stream:
            [event async for event in stream]
    # Even through clients that follow redirects.
    following = TrafficEventStream(base_url=house.url, stack_key=KEY, stack_key_hosts=LOCAL)
    following._client = httpx.Client(follow_redirects=True)
    with pytest.raises(StackKeyRedirectError):
        list(following)
    async_following = TrafficEventStream(
        base_url=house.url, stack_key=KEY, stack_key_hosts=LOCAL
    )
    async_following._aclient = httpx.AsyncClient(follow_redirects=True)
    with pytest.raises(StackKeyRedirectError):
        [event async for event in async_following]

    assert elsewhere.requests == []
    assert house.keys() == [[KEY]] * 4


def test_receipts_and_dx402_refuse_a_redirect_through_clients_that_follow(detour):
    requests = pytest.importorskip("requests")
    pytest.importorskip("cryptography")
    house, elsewhere = detour
    common = dict(
        payment_id_value="0x" + "ab" * 32,
        network="base",
        tx_hash="0x" + "cd" * 32,
        payer="0x" + "11" * 20,
        payee="0x" + "22" * 20,
        payer_key=bytes(range(32)),
        facilitator=house.url,
    )
    refused = {"v": 1, "skipped": "anchor_failed", "status": 302, "error": "stack_key_redirect"}

    with httpx.Client(follow_redirects=True) as follows, requests.Session() as session:
        for http in (follows, session):
            with pytest.raises(StackKeyRedirectError):
                get_receipt(
                    http, str(uuid.uuid4()), PurchaseContext(),
                    issuer=house.url, stack_key=KEY, stack_key_hosts=LOCAL,
                )
        for client in (None, follows, session):
            answer = anchor_evidence(
                b"response body", stack_key=KEY, stack_key_hosts=LOCAL, client=client, **common
            )
            assert answer == refused
            backends = available_backends(
                house.url, stack_key=KEY, stack_key_hosts=LOCAL, client=client
            )
            assert backends == []

    assert elsewhere.requests == []
    assert house.keys() == [[KEY]] * 8


def test_without_a_key_a_redirect_is_handled_as_before(detour):
    """No key: a client that follows redirects follows them, and one that does
    not gets the error it always got, not the stack key's."""
    house, elsewhere = detour
    with httpx.Client(follow_redirects=True) as follows:
        seller = X402Client(
            recipient_address=RECIPIENT, facilitator_url=house.url, http_client=follows
        )
        assert seller.get_version() == {}

    default = X402Client(recipient_address=RECIPIENT, facilitator_url=house.url)
    with pytest.raises(FacilitatorError) as caught:
        default.get_version()
    assert not isinstance(caught.value, StackKeyRedirectError)

    assert house.keys() == [[], []]
    assert elsewhere.keys() == [[]]


# -- The gate reads the host the HTTP library will contact, with its own parser

DISAGREEING = [
    ("https://FACILITATOR.ULTRAVIOLETADAO.XYZ./v", True),  # capitals and a final dot
    ("https://facilitator.ultravioletadao.xyz.:8443/v", True),  # and a port
    ("https://evil.example@facilitator.ultravioletadao.xyz/v", True),  # user info
    ("https://facilitator.ultravioletadao.xyz@evil.example/v", False),
    # httpx and urlsplit read the house, urllib3 (a requests session) evil.example
    ("https://evil.example\\@facilitator.ultravioletadao.xyz/v", False),
    ("https://facilitator.ultravioletadao.xyz\\@evil.example/v", False),
    ("https://facilitator.ultravioletadao.xyz .evil.example/v", False),
    ("https://facilitator.ultravioletadao.xyz /v", False),
    (" https://facilitator.ultravioletadao.xyz/v", False),
    ("https://evil.example\t@facilitator.ultravioletadao.xyz/v", False),
    ("https://facilitator.ultravioletadao.xyz:443:80/v", False),
    ("https://facilitator.ultravioletadao.xyz。evil.example/v", False),
]


@pytest.mark.parametrize("url,allowed", DISAGREEING)
def test_the_gate_reads_the_url_as_the_library_that_sends_it(url, allowed):
    assert stack_key_allowed(url) is allowed


@pytest.mark.parametrize("url,allowed", DISAGREEING)
def test_through_httpx_the_key_goes_only_to_the_house_host_it_contacts(mocked, url, allowed):
    try:
        _house_seller(mocked, stack_key=KEY, facilitator_url=url).get_version()
    except Exception:  # noqa: BLE001 - a URL httpx cannot read sends nothing
        pass

    assert [bool(keys) for keys in mocked.keys()] == ([allowed] if mocked.requests else [])
    for request in mocked.requests:
        if request["stack_key"]:
            assert request["host"].rstrip(".") == "facilitator.ultravioletadao.xyz"


def test_through_requests_a_url_it_reads_as_another_host_does_not_carry_the_key():
    """``http://localhost:<p>\\@127.0.0.1:<q>``: httpx and urlsplit read the
    listed 127.0.0.1, urllib3 connects to localhost. Decided on either of the
    first two alone, the key would ride a requests session to localhost."""
    requests = pytest.importorskip("requests")
    house = _Facilitator()
    elsewhere = _Facilitator(dual_stack=True)
    try:
        port = elsewhere.url.rsplit(":", 1)[1]
        url = f"http://localhost:{port}\\@{house.url[len('http://'):]}"
        with requests.Session() as session:
            available_backends(url, stack_key=KEY, stack_key_hosts=LOCAL, client=session)

        assert elsewhere.keys() == [[]]  # it went there, without the key
        assert house.requests == []
    finally:
        house.close()
        elsewhere.close()


# -- No dump of a config (or of a client) carries the key; a byte order mark goes


def test_no_dump_of_a_config_carries_the_key():
    config = X402Config(recipient_evm=RECIPIENT, stack_key=KEY)
    pickled = pickle.dumps(config)
    dumps = [
        repr(dataclasses.asdict(config)),
        json.dumps(dataclasses.asdict(config), default=str),
        repr(vars(config)),
        str(vars(config)),
    ]
    for holder in (
        Erc8004Client(stack_key=KEY), EscrowClient(stack_key=KEY),
        BazaarClient(stack_key=KEY), TrafficEventStream(stack_key=KEY),
    ):
        dumps.append(repr(vars(holder)))

    assert all(SECRET not in text for text in dumps)
    assert SECRET.encode() not in pickled
    # The bytes this test just produced, nothing from outside.
    assert pickle.loads(pickled).stack_key is None  # noqa: S301
    assert copy.deepcopy(config).stack_key is None
    # What is not a dump keeps it.
    assert config.stack_key == KEY and str(config.stack_key) == KEY
    assert dataclasses.replace(config).stack_key == KEY
    assert copy.copy(config).stack_key == KEY


@pytest.mark.parametrize(
    "read",
    ["﻿" + KEY, "﻿" + KEY + "\r\n", " ﻿" + KEY + " \r\n"],
    ids=["bom", "bom-crlf", "space-bom-crlf"],
)
def test_a_key_read_with_a_byte_order_mark_is_sent_without_it(facilitator, caplog, read):
    caplog.set_level(logging.DEBUG)
    seller = _seller(facilitator, stack_key=read)

    assert seller.process_payment(X_PAYMENT, PRICE).success

    assert seller.config.stack_key == KEY
    assert facilitator.keys() == [[KEY], [KEY]]
    assert _warnings(caplog) == []
