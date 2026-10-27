"""ensure_solana_identity: read on Solana, mint once, never over a doubt, under a daily cap.

Against ``tests/solana_identity_doubles.FakeRegistry`` (the facilitator's
owner lookup and ``POST /register`` for Solana, as x402-rs 2.51.2 answers them),
with the network closed and no SOL. ``fake.minted`` counts the identities that
reached the chain, which is the number every money test is about.
"""

from __future__ import annotations

import asyncio
import logging
import typing

import httpx
import pytest

from tests.solana_identity_doubles import (
    AGENT_URI,
    LOOKUP_INCONCLUSIVE,
    LOOKUP_ROUTE_NOT_FOUND,
    OTHER,
    OWNER,
    Clock,
    FakeRegistry,
    Sleeps,
    address,
    agent_uri_refused,
    complete,
    daily_cap_reached,
    fee_payer_empty,
    forward_unconfirmed,
    held_not_resumable,
    landed_but_not_minted,
    lands_then,
    lookup_owned_by,
    lookup_status,
    pending_transfer,
    raises,
    rate_limited,
    registry_unavailable,
    screening_unavailable,
    success_for,
    success_without_report,
    writer_lease,
)
from uvd_x402_sdk import reputation as rep
from uvd_x402_sdk.erc8004 import Erc8004Network
from uvd_x402_sdk.reputation import solana_identity as si
from uvd_x402_sdk.solana_signing import _b58encode


@pytest.fixture
def fake() -> FakeRegistry:
    return FakeRegistry()


@pytest.fixture
def budget() -> rep.InMemoryMintBudget:
    return rep.InMemoryMintBudget()


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def sleeps() -> Sleeps:
    return Sleeps()


@pytest.fixture
def ensure(fake, budget, clock, sleeps):
    """``ensure_solana_identity`` against the double, with this test's budget and clock."""

    async def call(recipient: str = OWNER, **kwargs):
        kwargs.setdefault("client", fake.client())
        kwargs.setdefault("agent_uri", AGENT_URI)
        kwargs.setdefault("budget", budget)
        kwargs.setdefault("clock", clock)
        kwargs.setdefault("sleep", sleeps)
        return await rep.ensure_solana_identity(recipient, **kwargs)

    return call


# -- the address, and the other arguments, before any request ------------------------
VALID = [
    OWNER,
    OTHER,
    "8oo4dC4JvBLwy5tGgiH3WwK4B9PWxL9Z4XjA2jzkQMbQ",  # the registry program
    _b58encode(b"\x00" * 31 + b"\x01"),  # 32 chars, the shortest a non-zero key has
    _b58encode(b"\xff" * 32),  # 44 chars
]
INVALID = {
    "zero": "1" * 32,
    "leading-space": " " + OWNER,
    "trailing-newline": OWNER + "\n",
    "zero-char": OWNER[:-1] + "0",
    "capital-o": OWNER[:-1] + "O",
    "capital-i": OWNER[:-1] + "I",
    "lowercase-l": OWNER[:-1] + "l",
    "fullwidth-digit": OWNER[:-1] + "１",
    "31-chars": OWNER[:31],
    "45-chars": OWNER + "1",
    "33-bytes": "1" + _b58encode(b"\xff" * 32),
    "44-chars-33-bytes": "z" * 44,
    "32-chars-24-bytes": "2" * 32,
    "evm-address": "0x" + "ab" * 20,
    "empty": "",
}


@pytest.mark.parametrize("value", VALID)
def test_valid_addresses_pass_as_they_are(value):
    assert rep.validate_solana_address(value) == value


@pytest.mark.parametrize("value", list(INVALID.values()), ids=list(INVALID))
def test_invalid_addresses_are_refused(value):
    with pytest.raises(ValueError):
        rep.validate_solana_address(value)


@pytest.mark.parametrize("value", [None, b"6xNP", 42, ["x"]])
def test_a_non_string_address_is_a_type_error(value):
    with pytest.raises(TypeError):
        rep.validate_solana_address(value)


@pytest.mark.parametrize("value", list(INVALID.values()), ids=list(INVALID))
async def test_an_invalid_recipient_sends_nothing(ensure, fake, value):
    with pytest.raises(ValueError):
        await ensure(value)
    assert fake.lookups == 0 and fake.posts == []


@pytest.mark.parametrize(
    "agent_uri",
    ["", "https://", "ipfs://", "http://execution.market/a", "HTTPS://execution.market/a",
     "https://execution.market/a b", "https://execution.market/a\n", " https://x.y/a",
     "ftp://x.y/a"],
)
async def test_an_agent_uri_that_is_not_https_or_ipfs_sends_nothing(ensure, fake, agent_uri):
    with pytest.raises(ValueError):
        await ensure(agent_uri=agent_uri)
    assert fake.lookups == 0 and fake.posts == []


@pytest.mark.parametrize("network", ["base", "solana-mainnet", "SOLANA", "eip155:8453", ""])
async def test_a_network_that_is_not_solana_sends_nothing(ensure, fake, network):
    with pytest.raises(ValueError):
        await ensure(network=network)
    assert fake.lookups == 0 and fake.posts == []


@pytest.mark.parametrize("cap", [26, 1000, -1])
async def test_the_daily_cap_cannot_be_raised_above_25(ensure, fake, cap):
    with pytest.raises(ValueError):
        await ensure(daily_cap=cap)
    assert fake.lookups == 0


@pytest.mark.parametrize("cap", [True, 25.0, "25"])
async def test_the_daily_cap_is_an_int(ensure, cap):
    with pytest.raises(TypeError):
        await ensure(daily_cap=cap)


@pytest.mark.parametrize("name", ["lookup_attempts", "register_attempts"])
@pytest.mark.parametrize("value", [0, 6])
async def test_attempts_are_bounded(ensure, name, value):
    with pytest.raises(ValueError):
        await ensure(**{name: value})


@pytest.mark.parametrize("value", [0, -1, float("inf"), float("nan")])
async def test_timeouts_are_positive_and_finite(ensure, value):
    with pytest.raises(ValueError):
        await ensure(register_timeout=value)


async def test_the_client_must_be_an_erc8004_client(ensure):
    with pytest.raises(TypeError):
        await ensure(client=object())


def test_the_networks_are_the_solana_names_of_erc8004():
    solana = {n for n in typing.get_args(Erc8004Network) if n.startswith("solana")}
    assert rep.SOLANA_IDENTITY_NETWORKS == solana == {"solana", "solana-devnet"}


def test_the_policy_numbers():
    assert rep.SOLANA_IDENTITY_DAILY_MINT_CAP == 25
    assert rep.MAX_MINT_ATTEMPTS_PER_ADDRESS_PER_DAY == 3
    assert rep.MINT_HOLD_SECONDS == 1800


# -- read first: an identity that exists is never minted again -----------------------
async def test_an_existing_identity_is_returned_and_nothing_is_minted(ensure, fake, budget, clock):
    asset = fake.give(OWNER)
    minted_before = fake.minted
    result = await ensure()
    assert result.status == "already_exists" and result.has_identity
    assert result.agent_id == asset and result.reason == "owner_lookup"
    assert fake.posts == [] and fake.minted == minted_before
    assert not result.request_sent
    assert budget.used_today(clock()) == 0


async def test_the_lookup_is_the_solana_owner_route(ensure, fake):
    fake.give(OWNER)
    client = fake.client()
    seen: list[str] = []
    original = client._client.get

    async def spy(url, **kwargs):
        seen.append(str(url))
        return await original(url, **kwargs)

    client._client.get = spy
    await ensure(client=client)
    assert seen == [f"https://facilitator.test/identity/solana/owner/{OWNER}"]


async def test_a_missing_identity_is_minted_once_and_confirmed(ensure, fake, budget, clock):
    result = await ensure()
    assert result.status == "created" and result.reason == "minted"
    assert result.confirmed and result.request_sent and result.has_identity
    assert result.agent_id == fake.owned[OWNER][0]
    assert result.mint_status == "complete"
    assert fake.minted == 1
    assert fake.posts == [
        {"x402Version": 1, "network": "solana", "agentUri": AGENT_URI, "recipient": OWNER}
    ]
    assert budget.used_today(clock()) == 1
    again = await ensure()
    assert again.status == "already_exists" and fake.minted == 1 and len(fake.posts) == 1


async def test_devnet_goes_to_the_devnet_routes(budget, clock, sleeps):
    fake = FakeRegistry(network="solana-devnet")
    result = await rep.ensure_solana_identity(
        OWNER, client=fake.client(), agent_uri=AGENT_URI, network="solana-devnet",
        budget=budget, clock=clock, sleep=sleeps,
    )
    assert result.status == "created" and result.network == "solana-devnet"
    assert fake.posts[0]["network"] == "solana-devnet"


async def test_a_mint_the_lookup_does_not_show_yet_is_not_minted_again(ensure, fake, clock):
    fake.hidden.add(OWNER)  # the owner lookup lags behind the chain
    first = await ensure()
    assert first.status == "created" and not first.confirmed
    second = await ensure()
    assert second.status == "uncertain" and second.reason == "recent_request"
    assert len(fake.posts) == 1 and fake.minted == 1
    clock.t += rep.MINT_HOLD_SECONDS + 1
    fake.hidden.clear()
    assert (await ensure()).status == "already_exists"
    assert fake.minted == 1


# -- the facilitator's refusals and what they cost ----------------------------------
async def test_409_is_refused_released_and_not_repeated(ensure, fake, budget, clock):
    fake.register_script = [held_not_resumable]
    result = await ensure()
    assert result.status == "refused"
    assert result.reason == result.error_code == "held_identity_not_resumable"
    assert result.status_code == 409 and result.mint_status == "not_minted"
    assert len(fake.posts) == 1 and fake.minted == 0
    assert budget.used_today(clock()) == 0  # refused before the chain: the slot is back
    fake.register_script = [held_not_resumable]
    assert (await ensure()).status == "refused"  # no hold after a refusal


async def test_400_is_refused_with_the_facilitator_code(ensure, fake, budget, clock):
    fake.register_script = [agent_uri_refused]
    result = await ensure()
    assert (result.status, result.reason) == ("refused", "agent_uri_tunnel")
    assert budget.used_today(clock()) == 0 and fake.minted == 0


async def test_503_empty_fee_payer_is_unavailable_and_not_retried(ensure, fake, budget, clock):
    fake.register_script = [fee_payer_empty, complete]
    result = await ensure()
    assert (result.status, result.reason) == ("unavailable", "fee_payer_insufficient_balance")
    assert len(fake.posts) == 1 and fake.minted == 0
    assert budget.used_today(clock()) == 0


@pytest.mark.parametrize("refusal", [registry_unavailable, screening_unavailable])
async def test_a_503_that_passes_is_sent_again_once(ensure, fake, sleeps, refusal):
    fake.register_script = [refusal, complete]
    result = await ensure()
    assert result.status == "created" and fake.minted == 1
    assert len(fake.posts) == 2 and sleeps.waits == [0.5]


async def test_retries_are_bounded(ensure, fake, budget, clock, sleeps):
    fake.register_script = [registry_unavailable] * 10
    result = await ensure(register_attempts=3)
    assert (result.status, result.reason) == ("unavailable", "registry_unavailable")
    assert len(fake.posts) == 3 and fake.minted == 0 and sleeps.waits == [0.5, 0.5]
    assert budget.used_today(clock()) == 0


async def test_lookup_retries_are_bounded(ensure, fake, sleeps):
    fake.lookup_script = [LOOKUP_INCONCLUSIVE] * 10
    result = await ensure(lookup_attempts=3)
    assert (result.status, result.reason) == ("uncertain", "lookup_inconclusive")
    assert fake.lookups == 3 and sleeps.waits == [0.5, 0.5] and fake.posts == []


async def test_the_writer_lease_503_waits_its_retry_after(ensure, fake, sleeps):
    fake.register_script = [writer_lease("5"), complete]
    result = await ensure()
    assert result.status == "created" and sleeps.waits == [5.0]


async def test_a_retry_after_longer_than_max_backoff_ends_the_call(ensure, fake, sleeps):
    fake.register_script = [writer_lease("5"), complete]
    result = await ensure(max_backoff=1.0)
    assert (result.status, result.reason) == ("unavailable", "writer_lease_unavailable")
    assert result.retry_after == 5 and sleeps.waits == [] and len(fake.posts) == 1


async def test_429_rate_limited_is_unavailable_and_retried(ensure, fake, sleeps):
    fake.register_script = [rate_limited("1"), complete]
    result = await ensure()
    assert result.status == "created" and len(fake.posts) == 2 and sleeps.waits == [1.0]


async def test_429_facilitator_daily_cap_is_quota_exceeded(ensure, fake, budget, clock):
    fake.register_script = [daily_cap_reached, complete]
    result = await ensure()
    assert (result.status, result.reason) == ("quota_exceeded", "facilitator_daily_cap")
    assert result.retry_after == 40000 and len(fake.posts) == 1 and fake.minted == 0
    assert budget.used_today(clock()) == 0


async def test_no_connection_is_unavailable_released_and_retried(ensure, fake, budget, clock):
    fake.register_script = [raises(httpx.ConnectError("refused")), complete]
    result = await ensure()
    assert result.status == "created" and len(fake.posts) == 2 and fake.minted == 1
    assert budget.used_today(clock()) == 1


async def test_no_connection_twice(ensure, fake, budget, clock):
    fake.register_script = [raises(httpx.ConnectError("refused"))] * 2
    result = await ensure()
    assert (result.status, result.reason) == ("unavailable", "no_connection")
    assert budget.used_today(clock()) == 0
    assert not result.request_sent  # no connection: nothing went out


def _admission(status: int, code: str):
    def answer(f, b):
        return httpx.Response(status, json={"error": code, "code": code},
                              headers={"Retry-After": "1"})
    return answer


@pytest.mark.parametrize(
    "status, code",
    [(503, "overloaded"), (429, "too_many_concurrent_requests"), (408, "request_timeout")],
)
async def test_the_admission_layer_refusals_are_released_and_retried(
    ensure, fake, budget, clock, status, code
):
    fake.register_script = [_admission(status, code), _admission(status, code)]
    result = await ensure()
    assert (result.status, result.reason) == ("unavailable", code)
    assert len(fake.posts) == 2 and fake.minted == 0
    assert budget.used_today(clock()) == 0


async def test_a_facilitator_code_that_is_not_a_code_never_reaches_repr_or_logs(
    ensure, fake, caplog
):
    caplog.set_level(logging.DEBUG)

    def leaky(f, b):
        return httpx.Response(400, json={"success": False, "errorCode": f"bad {OWNER}",
                                         "mint": {"status": "not_minted",
                                                  "errorCode": f"https://x.test/{OWNER}"}})

    fake.register_script = [leaky]
    result = await ensure()
    assert (result.status, result.reason) == ("refused", "facilitator_error")
    assert result.error_code == "facilitator_error"
    assert OWNER not in repr(result) and OWNER not in caplog.text


# -- uncertain: a request went out and nobody knows what it did ----------------------
@pytest.mark.parametrize(
    "answer, reason",
    [
        (lands_then(httpx.ReadTimeout("read")), "no_answer"),
        (lands_then(httpx.RemoteProtocolError("server disconnected")), "no_answer"),
        (landed_but_not_minted, "mint_transaction_failed"),
        (forward_unconfirmed, "forward_unconfirmed"),
    ],
    ids=["read-timeout", "disconnected", "500-not-minted", "502-forward-unconfirmed"],
)
async def test_an_unknown_outcome_is_uncertain_and_never_sent_again(
    ensure, fake, budget, clock, answer, reason
):
    fake.hidden.add(OWNER)  # it landed, and the lookup does not show it yet
    fake.register_script = [answer, complete, complete]
    result = await ensure()
    assert (result.status, result.reason) == ("uncertain", reason)
    assert result.request_sent and not result.has_identity
    assert len(fake.posts) == 1 and fake.minted == 1
    assert budget.used_today(clock()) == 1  # it may have spent: the slot is kept
    again = await ensure()
    assert (again.status, again.reason) == ("uncertain", "recent_request")
    assert len(fake.posts) == 1 and fake.minted == 1
    fake.hidden.clear()
    assert (await ensure()).status == "already_exists"


async def test_a_success_naming_another_owner_is_uncertain(ensure, fake):
    fake.register_script = [success_for(OTHER)]
    result = await ensure()
    assert (result.status, result.reason) == ("uncertain", "owner_mismatch")


async def test_a_success_whose_mint_is_not_complete_is_uncertain(ensure, fake):
    def contradiction(f, b):
        asset = f.give(b["recipient"])
        return httpx.Response(
            200,
            json={"success": True, "agentId": asset, "owner": b["recipient"],
                  "network": "solana", "mint": {"status": "pending_transfer"}},
        )

    fake.register_script = [contradiction]
    result = await ensure()
    assert (result.status, result.reason) == ("uncertain", "inconsistent_answer")


async def test_a_success_without_mint_report_is_decided_by_the_lookup(ensure, fake):
    fake.register_script = [success_without_report]
    result = await ensure()
    assert result.status == "created" and result.confirmed


async def test_a_success_without_mint_report_the_lookup_does_not_show(ensure, fake):
    fake.hidden.add(OWNER)
    fake.register_script = [success_without_report]
    result = await ensure()
    assert (result.status, result.reason) == ("uncertain", "success_without_mint_report")


async def test_an_unreadable_answer_is_uncertain(ensure, fake, budget, clock):
    fake.register_script = [
        lambda f, b: httpx.Response(503, content=b"<html>Service Unavailable</html>")
    ]
    result = await ensure()
    assert (result.status, result.reason) == ("uncertain", "unreadable_answer")
    assert budget.used_today(clock()) == 1


async def test_a_refusal_status_with_a_mint_in_progress_is_kept(ensure, fake, budget, clock):
    def contradiction(f, b):
        return httpx.Response(
            503, json={"success": False, "mint": {"status": "pending_transfer"}}
        )

    fake.register_script = [contradiction]
    result = await ensure()
    assert (result.status, result.reason) == ("uncertain", "inconsistent_answer")
    assert budget.used_today(clock()) == 1


# -- pending: the facilitator holds it, the same request finishes it ----------------
async def test_pending_transfer_then_the_same_request_resumes_it(ensure, fake, clock):
    fake.register_script = [pending_transfer]
    first = await ensure()
    assert (first.status, first.reason) == ("pending", "pending_transfer")
    assert first.agent_id is not None and not first.has_identity
    assert fake.minted == 1
    clock.t += rep.MINT_HOLD_SECONDS + 1
    second = await ensure()
    assert second.status == "created" and second.agent_id == first.agent_id
    assert fake.minted == 1 and fake.posts[0] == fake.posts[1]


# -- the lookup gives no verdict: nothing is spent -----------------------------------
@pytest.mark.parametrize(
    "lookup, reason",
    [
        (LOOKUP_INCONCLUSIVE, "lookup_inconclusive"),
        (httpx.ReadTimeout("read"), "lookup_no_answer"),
        (httpx.ConnectError("refused"), "lookup_no_answer"),
        (LOOKUP_ROUTE_NOT_FOUND, "lookup_not_a_verdict"),
        (lookup_status(404, b"Not Found"), "lookup_not_a_verdict"),
        (lookup_status(404, {"error": "x", "balance": False}), "lookup_not_a_verdict"),
        (lookup_status(404, {"error": "x", "balance": "1"}), "lookup_not_a_verdict"),
        (lookup_owned_by(OTHER), "lookup_owner_mismatch"),
        (lookup_status(200, {"agentId": "nope", "owner": OWNER, "network": "solana"}),
         "lookup_unreadable"),
        (lookup_status(200, {"agentId": address(9), "owner": OWNER, "network": "base"}),
         "lookup_network_mismatch"),
        (lookup_status(200, b"not json"), "lookup_unreadable"),
        (lookup_status(400, {"error": "Invalid Solana address"}), "lookup_http_400"),
        (lookup_status(500, {"error": "No Solana provider"}), "lookup_http_500"),
    ],
    ids=["503", "timeout", "connect", "route-404", "html-404", "bool-balance",
         "balance-1", "other-owner", "bad-agent-id", "other-network", "200-not-json",
         "400", "500"],
)
async def test_a_lookup_without_a_verdict_mints_nothing(
    ensure, fake, budget, clock, lookup, reason
):
    fake.lookup_script = [lookup, lookup]
    result = await ensure()
    assert (result.status, result.reason) == ("uncertain", reason)
    assert fake.posts == [] and fake.minted == 0 and not result.request_sent
    assert budget.used_today(clock()) == 0


async def test_a_huge_answer_is_not_a_verdict(ensure, fake):
    padded = {"error": "none", "balance": "0", "padding": "x" * (70 * 1024)}
    fake.lookup_script = [lookup_status(404, padded)]
    result = await ensure(lookup_attempts=1)
    assert (result.status, result.reason) == ("uncertain", "lookup_not_a_verdict")
    assert fake.posts == []


def test_an_address_is_refused_before_it_is_decoded(monkeypatch):
    def decode(value):
        raise AssertionError("decoded")

    monkeypatch.setattr(si, "_b58decode", decode)
    for value in ("2" * 45, OWNER[:-1] + "0", "2" * 10_000):
        with pytest.raises(ValueError):
            si.validate_solana_address(value)


async def test_an_inconclusive_lookup_is_asked_again_once(ensure, fake, sleeps):
    fake.lookup_script = [LOOKUP_INCONCLUSIVE]
    result = await ensure()
    assert result.status == "created" and fake.lookups == 3 and sleeps.waits == [0.5]


@pytest.mark.parametrize("balance", [0, 0.0, "0.0", " 0", None])
async def test_only_the_string_zero_balance_is_a_verdict(ensure, fake, balance):
    # x402-rs writes "0" for Solana; JSON.parse cannot tell 0 from 0.0.
    fake.lookup_script = [lookup_status(404, {"error": "none", "balance": balance})] * 2
    result = await ensure()
    assert (result.status, result.reason) == ("uncertain", "lookup_not_a_verdict")
    assert fake.posts == []


async def test_a_long_retry_after_on_the_lookup_ends_the_call(ensure, fake, sleeps):
    fake.lookup_script = [lookup_status(429, {"code": "rate_limited"}, {"Retry-After": "30"})]
    result = await ensure()
    assert (result.status, result.reason) == ("uncertain", "lookup_rate_limited")
    assert result.retry_after == 30 and sleeps.waits == [] and fake.lookups == 1


async def test_check_solana_identity_alone(fake):
    fake.give(OWNER)
    client = fake.client()
    found = await rep.check_solana_identity(OWNER, client=client)
    assert found.state == "registered" and found.agent_id == fake.owned[OWNER][0]
    absent = await rep.check_solana_identity(OTHER, client=client)
    assert (absent.state, absent.reason, absent.status_code) == ("absent", "owner_not_found", 404)


# -- caps ----------------------------------------------------------------------------
async def test_the_client_daily_cap_counts_every_address(ensure, fake, budget, clock):
    for n in (1, 2):
        assert (await ensure(address(n), daily_cap=2)).status == "created"
    third = await ensure(address(3), daily_cap=2)
    assert (third.status, third.reason) == ("quota_exceeded", "client_daily_cap")
    assert len(fake.posts) == 2 and fake.minted == 2 and not third.request_sent
    clock.t += 86_400  # the next UTC day
    assert (await ensure(address(3), daily_cap=2)).status == "created"


async def test_the_default_cap_is_25(ensure, fake):
    for n in range(1, 26):
        assert (await ensure(address(n))).status == "created"
    assert (await ensure(address(26))).reason == "client_daily_cap"
    assert fake.minted == 25


async def test_a_zero_cap_mints_nothing(ensure, fake):
    result = await ensure(daily_cap=0)
    assert (result.status, result.reason) == ("quota_exceeded", "client_daily_cap")
    assert fake.posts == []


async def test_refusals_do_not_use_the_cap(ensure, fake):
    fake.register_script = [held_not_resumable, fee_payer_empty, daily_cap_reached]
    for n in (1, 2, 3):
        await ensure(address(n), daily_cap=1)
    assert (await ensure(address(4), daily_cap=1)).status == "created"


async def test_three_requests_per_address_per_day(ensure, fake, clock):
    fake.register_script = [pending_transfer, pending_transfer, pending_transfer, complete]
    for _ in range(3):
        assert (await ensure()).status == "pending"
        clock.t += rep.MINT_HOLD_SECONDS + 1
    fourth = await ensure()
    assert (fourth.status, fourth.reason) == ("quota_exceeded", "address_daily_cap")
    assert len(fake.posts) == 3
    clock.t += 86_400
    assert (await ensure()).status == "created"


async def test_the_per_address_window_is_24_hours_not_the_calendar_day(ensure, fake, clock):
    clock.t = 1_790_035_200.0 - 3_600  # 23:00 UTC
    fake.register_script = [pending_transfer] * 4
    for _ in range(3):
        assert (await ensure()).status == "pending"
        clock.t += rep.MINT_HOLD_SECONDS + 1  # the third lands just after midnight
    fourth = await ensure()
    assert (fourth.status, fourth.reason) == ("quota_exceeded", "address_daily_cap")
    assert len(fake.posts) == 3


async def test_devnet_does_not_use_up_mainnet(fake, budget, clock, sleeps):
    devnet = FakeRegistry(network="solana-devnet")
    common = dict(agent_uri=AGENT_URI, budget=budget, clock=clock, sleep=sleeps, daily_cap=1)
    assert (await rep.ensure_solana_identity(
        address(1), client=devnet.client(), network="solana-devnet", **common
    )).status == "created"
    assert (await rep.ensure_solana_identity(
        address(1), client=fake.client(), **common
    )).status == "created"
    assert budget.used_today(clock(), "solana") == budget.used_today(clock(), "solana-devnet") == 1


async def test_a_reservation_never_resolved_frees_the_address_after_the_hold(
    ensure, fake, budget, clock
):
    key = f"solana:{OWNER}"
    await budget.reserve(key, now=clock(), daily_cap=25, per_address_cap=3, hold_seconds=1800)
    assert (await ensure()).reason == "in_flight"
    clock.t += rep.MINT_HOLD_SECONDS
    result = await ensure()
    assert result.status == "created" and len(fake.posts) == 1
    assert budget.used_today(clock()) == 2  # the orphan may have spent: it still counts


# -- one call per address in flight --------------------------------------------------
async def test_two_calls_at_once_send_one_mint(fake, budget, clock, sleeps):
    fake.gate = asyncio.Event()
    client = fake.client()

    async def call():
        return await rep.ensure_solana_identity(
            OWNER, client=client, agent_uri=AGENT_URI, budget=budget, clock=clock, sleep=sleeps
        )

    first = asyncio.ensure_future(call())
    second = asyncio.ensure_future(call())
    while not fake.posts:
        await asyncio.sleep(0)
    for _ in range(5):
        await asyncio.sleep(0)
    fake.gate.set()
    a, b = await asyncio.gather(first, second)
    assert a is b and a.status == "created"
    assert len(fake.posts) == 1 and fake.minted == 1 and fake.lookups == 2


async def test_a_cancelled_caller_does_not_cancel_the_mint(fake, budget, clock, sleeps):
    fake.gate = asyncio.Event()
    client = fake.client()
    kwargs = dict(client=client, agent_uri=AGENT_URI, budget=budget, clock=clock, sleep=sleeps)
    first = asyncio.ensure_future(rep.ensure_solana_identity(OWNER, **kwargs))
    while not fake.posts:
        await asyncio.sleep(0)
    first.cancel()
    joined = asyncio.ensure_future(rep.ensure_solana_identity(OWNER, **kwargs))
    await asyncio.sleep(0)
    fake.gate.set()
    result = await joined
    assert result.status == "created" and len(fake.posts) == 1
    assert budget.used_today(clock()) == 1


async def test_a_request_in_flight_elsewhere_is_pending(ensure, fake, budget, clock):
    key = f"solana:{OWNER}"
    assert await budget.reserve(
        key, now=clock(), daily_cap=25, per_address_cap=3, hold_seconds=1800
    ) == "granted"
    result = await ensure()
    assert (result.status, result.reason) == ("pending", "in_flight")
    assert fake.posts == []


class _Running:
    """A task of another event loop (a thread of the same process), still running."""

    def done(self) -> bool:
        return False


async def test_a_call_stuck_on_another_loop_blocks_the_address_for_the_hold_only(
    ensure, fake, budget, clock
):
    # Another thread's loop reserved, then stopped mid-POST and never ran again
    # (not closed): its task never finishes and its reservation never resolves.
    stopped = asyncio.new_event_loop()
    key = f"solana:{OWNER}"
    await budget.reserve(key, now=clock(), daily_cap=25, per_address_cap=3, hold_seconds=1800)
    si._IN_FLIGHT[(stopped, key)] = _Running()
    try:
        first = await ensure()
        assert (first.status, first.reason) == ("pending", "in_flight")  # the budget's verdict
        assert fake.lookups == 1 and fake.posts == []  # not joined, not turned away
        clock.t += 2 * 86_400
        assert (await ensure()).status == "created" and len(fake.posts) == 1
    finally:
        si._IN_FLIGHT.pop((stopped, key), None)
        stopped.close()


def test_two_threads_two_loops_one_budget_one_mint(fake, budget, clock):
    import threading

    fake.thread_gate = threading.Event()  # the first POST waits; the second meets its reservation
    results: list = []

    def call() -> None:
        results.append(asyncio.run(rep.ensure_solana_identity(
            OWNER, client=fake.client(), agent_uri=AGENT_URI, budget=budget, clock=clock,
            sleep=Sleeps(),
        )))

    first = threading.Thread(target=call)
    first.start()
    while not fake.posts:
        threading.Event().wait(0.01)
    call()  # this thread, its own loop, while the other POST is in flight
    assert results[0].reason == "in_flight"
    fake.thread_gate.set()
    first.join(10)
    assert results[1].status == "created" and len(fake.posts) == 1 and fake.minted == 1


@pytest.mark.parametrize("late", ["release", "keep"])
async def test_a_late_answer_acts_on_its_own_reservation_only(budget, clock, late):
    key, policy = f"solana:{OWNER}", dict(daily_cap=25, per_address_cap=3, hold_seconds=1800)
    a = clock()
    assert await budget.reserve(key, now=a, **policy) == "granted"
    clock.t += 1800  # A's process froze: its reservation is taken as spent, hold over
    b = clock()
    assert await budget.reserve(key, now=b, **policy) == "granted"
    if late == "release":  # A comes back: nothing was sent. B must stay in flight.
        await budget.release(key, reserved_at=a, now=clock())
        assert await budget.reserve(key, now=clock(), **policy) == "in_flight"
    else:  # B was refused before the chain; then A comes back: it may have spent.
        await budget.release(key, reserved_at=b, now=clock())
        clock.t += 1
        await budget.keep(key, reserved_at=a, now=clock(), hold_seconds=1800)
        assert await budget.reserve(key, now=clock(), **policy) == "held"


@pytest.mark.parametrize("name", ["lookup_timeout", "register_timeout"])
async def test_a_timeout_longer_than_a_third_of_the_hold_is_refused(ensure, fake, name):
    with pytest.raises(ValueError):
        await ensure(**{name: rep.MINT_HOLD_SECONDS / 3 + 1})
    assert fake.lookups == 0


async def test_the_timeout_is_a_total_deadline(ensure, fake, budget, clock):
    fake.gate = asyncio.Event()  # never set: the POST hangs below httpx's per-phase timeouts
    result = await ensure(register_timeout=0.05)
    assert (result.status, result.reason) == ("uncertain", "no_answer")
    assert result.request_sent and budget.used_today(clock()) == 1


async def test_request_sent_does_not_trust_a_code_named_like_ours(ensure, fake):
    fake.register_script = [
        lambda f, b: httpx.Response(500, json={"success": False, "errorCode": "no_connection"})
    ]
    result = await ensure()
    assert (result.status, result.reason) == ("uncertain", "no_connection")
    assert result.request_sent  # it went out and may have spent


async def test_an_empty_code_hides_nothing(ensure, fake):
    def empty(f, b):
        return httpx.Response(503, json={"success": False, "errorCode": "registry_unavailable",
                                         "mint": {"status": "not_minted", "errorCode": ""}})

    fake.register_script = [empty, complete]
    result = await ensure()
    assert result.status == "created" and len(fake.posts) == 2


class BrokenBudget:
    def __init__(self, fail: str) -> None:
        self.fail = fail
        self.inner = rep.InMemoryMintBudget()

    async def reserve(self, key, **kw):
        if self.fail == "reserve":
            raise RuntimeError("store down")
        return await self.inner.reserve(key, **kw)

    async def keep(self, key, **kw):
        if self.fail == "keep":
            raise RuntimeError("store down")
        return await self.inner.keep(key, **kw)

    async def release(self, key, **kw):
        if self.fail == "release":
            raise RuntimeError("store down")
        return await self.inner.release(key, **kw)


async def test_an_unreadable_budget_spends_nothing(ensure, fake):
    result = await ensure(budget=BrokenBudget("reserve"))
    assert (result.status, result.reason) == ("unavailable", "budget_unavailable")
    assert fake.posts == []


async def test_a_budget_that_fails_to_keep_still_answers(ensure, fake):
    result = await ensure(budget=BrokenBudget("keep"))
    assert result.status == "created" and fake.minted == 1


async def test_a_budget_that_fails_to_release_blocks_the_address(ensure, fake, clock):
    broken = BrokenBudget("release")
    fake.register_script = [held_not_resumable]
    assert (await ensure(budget=broken)).status == "refused"
    again = await ensure(budget=broken)
    assert (again.status, again.reason) == ("pending", "in_flight")
    assert len(fake.posts) == 1


async def test_the_budget_is_the_callers_and_has_no_default(fake, clock, sleeps):
    kwargs = dict(client=fake.client(), agent_uri=AGENT_URI, clock=clock, sleep=sleeps)
    with pytest.raises(TypeError):
        await rep.ensure_solana_identity(OWNER, **kwargs)
    for budget in (None, object()):
        with pytest.raises(TypeError):
            await rep.ensure_solana_identity(OWNER, budget=budget, **kwargs)
    assert fake.lookups == 0


# -- what it says about itself ------------------------------------------------------
async def test_no_full_address_in_logs_or_repr(ensure, fake, caplog):
    caplog.set_level(logging.DEBUG)
    fake.register_script = [pending_transfer]
    result = await ensure()
    text = caplog.text + repr(result) + repr(await rep.check_solana_identity(
        OWNER, client=fake.client()))
    assert OWNER not in text and AGENT_URI not in text
    assert result.agent_id not in text
    assert OWNER[:4] + "..." + OWNER[-4:] in text


async def test_the_http_client_logs_are_redacted(ensure):
    await ensure()
    assert any(
        type(f).__name__ == "_RedactingFilter" for f in logging.getLogger("httpx").filters
    )


async def test_the_stack_key_travels_on_both_requests(fake, budget, clock, sleeps):
    key = "uvdsk_" + "Synthetic-Test-Key_" * 3
    client = fake.client(stack_key=key, stack_key_hosts=["facilitator.test"])
    await rep.ensure_solana_identity(
        OWNER, client=client, agent_uri=AGENT_URI, budget=budget, clock=clock, sleep=sleeps
    )
    assert [h.get("x-uvd-stack-key") for h in fake.headers] == [key, key, key]
