"""Each guard of ensure_solana_identity is the thing that refuses: remove it and the harm happens.

The repo's idiom (``tests/test_reputation_mutations.py::mutant``): a copy of
``uvd_x402_sdk.reputation.solana_identity`` with ONE guard replaced, compiled
from its source, and the case the guard exists for run against it. Every case
asserts that the module as shipped does NOT let the harm through and that the
mutant DOES, so a guard that stops guarding, or a test that stops reaching it,
turns this file red. A mutation site must appear exactly once in the source.

The harm is counted on the facilitator double (``fake.minted``: identities that
reached the chain; ``fake.posts``: mint requests that went out) or is a wrong
answer a consumer would act on.
"""

from __future__ import annotations

import importlib
import importlib.util
import logging
import sys
from pathlib import Path
from types import ModuleType

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
    complete,
    daily_cap_reached,
    fee_payer_empty,
    landed_but_not_minted,
    lands_then,
    lookup_owned_by,
    lookup_status,
    raises,
    registry_unavailable,
    success_for,
    success_without_report,
    writer_lease,
)

NAME = "uvd_x402_sdk.reputation.solana_identity"
ORIGINAL = importlib.import_module(NAME)


def mutant(old: str, new: str) -> ModuleType:
    """A copy of the module with ``old`` -> ``new`` (once)."""
    source = Path(ORIGINAL.__file__).read_text(encoding="utf-8")
    assert source.count(old) == 1, f"mutation site not found exactly once: {old!r}"
    spec = importlib.util.spec_from_loader("uvd_x402_sdk.reputation._mutant_solana_identity",
                                           loader=None)
    module = importlib.util.module_from_spec(spec)
    module.__package__ = "uvd_x402_sdk.reputation"
    module.__file__ = ORIGINAL.__file__
    # dataclasses resolve deferred annotations through sys.modules[cls.__module__].
    sys.modules[spec.name] = module
    exec(compile(source.replace(old, new), ORIGINAL.__file__, "exec"), module.__dict__)
    return module


class Run:
    """One scenario against one module (the shipped one or a mutant)."""

    def __init__(self, module: ModuleType) -> None:
        self.m = module
        self.fake = FakeRegistry()
        self.client = self.fake.client()
        self.budget = module.InMemoryMintBudget()
        self.clock = Clock()
        self.sleeps = Sleeps()

    async def ensure(self, recipient: str = OWNER, **kwargs):
        kwargs.setdefault("client", self.client)
        kwargs.setdefault("agent_uri", AGENT_URI)
        kwargs.setdefault("budget", self.budget)
        kwargs.setdefault("clock", self.clock)
        kwargs.setdefault("sleep", self.sleeps)
        return await self.m.ensure_solana_identity(recipient, **kwargs)

    async def refused(self, **kwargs) -> bool:
        """Whether the call refuses its arguments before any request."""
        try:
            await self.ensure(**kwargs)
        except (TypeError, ValueError):
            return True
        return False


# -- each scenario returns True when the harm happened ------------------------------
async def mints_on_an_inconclusive_lookup(r: Run) -> bool:
    r.fake.lookup_script = [LOOKUP_INCONCLUSIVE, LOOKUP_INCONCLUSIVE]
    await r.ensure()
    return r.fake.minted > 0


async def mints_on_a_route_404(r: Run) -> bool:
    r.fake.lookup_script = [LOOKUP_ROUTE_NOT_FOUND]
    await r.ensure()
    return r.fake.minted > 0


async def mints_on_a_huge_404(r: Run) -> bool:
    padded = {"error": "none", "balance": "0", "padding": "x" * (70 * 1024)}
    r.fake.lookup_script = [lookup_status(404, padded)]
    await r.ensure(lookup_attempts=1)
    return r.fake.minted > 0


async def takes_another_owners_identity(r: Run) -> bool:
    r.fake.lookup_script = [lookup_owned_by(OTHER)]
    return (await r.ensure()).status == "already_exists"


async def takes_another_networks_identity(r: Run) -> bool:
    body = {"agentId": address(9), "owner": OWNER, "network": "base", "balance": "1"}
    r.fake.lookup_script = [lookup_status(200, body)]
    return (await r.ensure()).status == "already_exists"


async def takes_a_garbage_agent_id(r: Run) -> bool:
    body = {"agentId": "not-an-asset", "owner": OWNER, "network": "solana", "balance": "1"}
    r.fake.lookup_script = [lookup_status(200, body)]
    return (await r.ensure()).status == "already_exists"


async def mints_again_over_an_existing_identity(r: Run) -> bool:
    r.fake.give(OWNER)
    result = await r.ensure()
    return r.fake.minted > 1 or result.status != "already_exists"


async def goes_past_the_daily_cap(r: Run) -> bool:
    for n in (1, 2, 3):
        await r.ensure(address(n), daily_cap=2)
    return r.fake.minted > 2


async def accepts_a_cap_above_25(r: Run) -> bool:
    return not await r.refused(daily_cap=26)


async def goes_past_three_per_address(r: Run) -> bool:
    from tests.solana_identity_doubles import pending_transfer

    r.fake.register_script = [pending_transfer] * 4
    for _ in range(4):
        await r.ensure()
        r.clock.t += r.m.MINT_HOLD_SECONDS + 1
    return len(r.fake.posts) > 3


async def mints_twice_after_a_lost_answer(r: Run) -> bool:
    r.fake.hidden.add(OWNER)
    r.fake.register_script = [lands_then(httpx.ReadTimeout("read")), complete]
    await r.ensure()
    await r.ensure()
    return r.fake.minted > 1


async def mints_while_another_process_is_minting(r: Run) -> bool:
    await r.budget.reserve(f"solana:{OWNER}", now=r.clock(), daily_cap=25, per_address_cap=3,
                           hold_seconds=1800)
    await r.ensure()
    return len(r.fake.posts) > 0


async def two_calls_at_once_mint_twice(r: Run) -> bool:
    import asyncio

    # Two budgets that do not see each other (two callers of one process, each
    # with its own): only the in-flight join keeps it to one mint.
    r.fake.gate = asyncio.Event()
    first = asyncio.ensure_future(r.ensure(budget=r.m.InMemoryMintBudget()))
    second = asyncio.ensure_future(r.ensure(budget=r.m.InMemoryMintBudget()))
    while not r.fake.posts:
        await asyncio.sleep(0)
    for _ in range(20):
        await asyncio.sleep(0)
    r.fake.gate.set()
    await asyncio.gather(first, second)
    return r.fake.minted > 1


async def a_spent_slot_is_given_back(r: Run) -> bool:
    r.fake.register_script = [landed_but_not_minted, complete]
    await r.ensure(address(1), daily_cap=1)
    await r.ensure(address(2), daily_cap=1)
    return r.fake.minted > 1


async def a_refusal_with_a_mint_in_progress_is_given_back(r: Run) -> bool:
    def contradiction(f, b):
        return httpx.Response(503, json={"success": False, "mint": {"status": "pending_transfer"}})

    r.fake.register_script = [contradiction]
    await r.ensure()
    return r.budget.used_today(r.clock()) == 0


async def a_500_not_minted_is_sent_again(r: Run) -> bool:
    r.fake.register_script = [landed_but_not_minted, complete]
    await r.ensure()
    return r.fake.minted > 1


async def a_read_timeout_is_sent_again(r: Run) -> bool:
    r.fake.register_script = [lands_then(httpx.ReadTimeout("read")), complete]
    await r.ensure()
    return r.fake.minted > 1


async def waits_longer_than_max_backoff(r: Run) -> bool:
    r.fake.register_script = [writer_lease("5"), complete]
    await r.ensure(max_backoff=1.0)
    return any(w > 1.0 for w in r.sleeps.waits)


async def retries_without_bound(r: Run) -> bool:
    r.fake.register_script = [registry_unavailable] * 6
    await r.ensure(register_attempts=2)
    return len(r.fake.posts) > 2


async def looks_up_without_bound(r: Run) -> bool:
    r.fake.lookup_script = [LOOKUP_INCONCLUSIVE] * 6
    await r.ensure(lookup_attempts=2)
    return r.fake.lookups > 2


async def the_facilitator_daily_cap_is_not_a_quota(r: Run) -> bool:
    r.fake.register_script = [daily_cap_reached]
    return (await r.ensure()).status != "quota_exceeded"


async def an_empty_fee_payer_is_asked_again(r: Run) -> bool:
    r.fake.register_script = [fee_payer_empty, fee_payer_empty]
    await r.ensure()
    return len(r.fake.posts) > 1


async def created_for_another_owner(r: Run) -> bool:
    r.fake.register_script = [success_for(OTHER)]
    return (await r.ensure()).status == "created"


async def created_from_a_mint_not_complete(r: Run) -> bool:
    def contradiction(f, b):
        return httpx.Response(200, json={"success": True, "agentId": address(7), "owner": OWNER,
                                         "network": "solana", "mint": {"status": "pending_stats"}})

    r.fake.register_script = [contradiction]
    return (await r.ensure()).status == "created"


async def created_without_a_report_nor_a_lookup(r: Run) -> bool:
    r.fake.hidden.add(OWNER)
    r.fake.register_script = [success_without_report]
    return (await r.ensure()).status == "created"


async def sends_for_the_zero_address(r: Run) -> bool:
    return not await r.refused(recipient="1" * 32)


async def sends_for_an_address_of_33_bytes(r: Run) -> bool:
    return not await r.refused(recipient="z" * 44)


def _decodes(r: Run, value: str) -> bool:
    """Whether validating ``value`` reaches the base58 decoder."""

    def decode(raw):
        raise AssertionError("decoded")

    original, r.m._b58decode = r.m._b58decode, decode
    try:
        r.m.validate_solana_address(value)
    except AssertionError:
        return True
    except ValueError:
        return False
    finally:
        r.m._b58decode = original
    return True


async def decodes_a_long_address(r: Run) -> bool:
    return _decodes(r, "2" * 45)


async def decodes_outside_the_alphabet(r: Run) -> bool:
    return _decodes(r, OWNER[:-1] + "0")


async def sends_for_an_evm_network(r: Run) -> bool:
    return not await r.refused(network="base")


async def sends_an_http_agent_uri(r: Run) -> bool:
    return not await r.refused(agent_uri="http://execution.market/agents/x")


async def sends_an_agent_uri_with_a_newline(r: Run) -> bool:
    return not await r.refused(agent_uri=AGENT_URI + "\n")


async def logs_the_full_address(r: Run) -> bool:
    records: list[str] = []

    class Keep(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record.getMessage())

    logger = logging.getLogger("uvd_x402_sdk.reputation")
    handler, level = Keep(), logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    try:
        await r.ensure()
    finally:
        logger.removeHandler(handler)
        logger.setLevel(level)
    return any(OWNER in line for line in records)


async def repr_shows_the_full_address(r: Run) -> bool:
    return OWNER in repr(await r.ensure())


async def an_orphan_reservation_blocks_for_ever(r: Run) -> bool:
    await r.budget.reserve(f"solana:{OWNER}", now=r.clock(), daily_cap=25, per_address_cap=3,
                           hold_seconds=1800)
    r.clock.t += 3 * 86_400
    await r.ensure()
    return r.fake.posts == []


_POLICY = dict(daily_cap=25, per_address_cap=3, hold_seconds=1800)


async def a_late_release_undoes_a_newer_request(r: Run) -> bool:
    key, a = f"solana:{OWNER}", r.clock()
    await r.budget.reserve(key, now=a, **_POLICY)
    r.clock.t += 1800
    await r.budget.reserve(key, now=r.clock(), **_POLICY)  # B, in flight
    await r.budget.release(key, reserved_at=a, now=r.clock())  # A's late answer
    return await r.budget.reserve(key, now=r.clock(), **_POLICY) == "granted"


async def a_late_keep_does_not_hold(r: Run) -> bool:
    key, a = f"solana:{OWNER}", r.clock()
    await r.budget.reserve(key, now=a, **_POLICY)
    r.clock.t += 1800
    b = r.clock()
    await r.budget.reserve(key, now=b, **_POLICY)
    await r.budget.release(key, reserved_at=b, now=r.clock())
    r.clock.t += 1
    await r.budget.keep(key, reserved_at=a, now=r.clock(), hold_seconds=1800)  # A may have spent
    return await r.budget.reserve(key, now=r.clock(), **_POLICY) == "granted"


async def accepts_a_timeout_longer_than_the_hold(r: Run) -> bool:
    return not await r.refused(register_timeout=3600)


async def a_hanging_post_outlives_its_deadline(r: Run) -> bool:
    import asyncio

    r.fake.gate = asyncio.Event()  # never set
    try:
        await asyncio.wait_for(r.ensure(register_timeout=0.05), 2)
    except asyncio.TimeoutError:
        return True
    return False


async def an_empty_code_hides_the_real_one(r: Run) -> bool:
    def empty(f, b):
        return httpx.Response(503, json={"success": False, "errorCode": "registry_unavailable",
                                         "mint": {"status": "not_minted", "errorCode": ""}})

    r.fake.register_script = [empty, complete]
    return (await r.ensure()).reason == "facilitator_error"


async def devnet_uses_up_mainnet(r: Run) -> bool:
    await r.budget.reserve(f"solana-devnet:{OWNER}", now=r.clock(), daily_cap=1,
                           per_address_cap=3, hold_seconds=1800)
    return (await r.ensure(daily_cap=1)).status == "quota_exceeded"


async def four_requests_across_midnight(r: Run) -> bool:
    from tests.solana_identity_doubles import pending_transfer

    r.clock.t = 1_790_035_200.0 - 3_600  # 23:00 UTC
    r.fake.register_script = [pending_transfer] * 4
    for _ in range(4):
        await r.ensure()
        r.clock.t += r.m.MINT_HOLD_SECONDS + 1
    return len(r.fake.posts) > 3


async def a_code_carries_the_address(r: Run) -> bool:
    def leaky(f, b):
        return httpx.Response(400, json={"success": False, "errorCode": f"bad {OWNER}"})

    r.fake.register_script = [leaky]
    return OWNER in repr(await r.ensure())


async def says_sent_with_no_connection(r: Run) -> bool:
    r.fake.register_script = [raises(httpx.ConnectError("x"))] * 2
    return (await r.ensure()).request_sent


CASES = [
    ("M1-mint-on-inconclusive-lookup", '    if check.state != "absent":\n',
     "    if False:\n", mints_on_an_inconclusive_lookup),
    ("M2-registered-short-circuit", '    if check.state == "registered":\n',
     "    if False:\n", mints_again_over_an_existing_identity),
    ("M3-404-needs-the-balance", '        if balance == "0":\n', "        if True:\n",
     mints_on_a_route_404),
    ("M4-bounded-answer", "        value = loads_bounded(content, max_bytes=_MAX_ANSWER_BYTES)\n",
     "        value = loads_bounded(content, max_bytes=None)\n", mints_on_a_huge_404),
    ("M5-lookup-owner",
     '        if body.get("owner") != recipient:\n            return SolanaIdentityCheck(',
     "        if False:\n            return SolanaIdentityCheck(", takes_another_owners_identity),
    ("M6-lookup-network", '        if "network" in body and body["network"] != network:\n',
     "        if False:\n", takes_another_networks_identity),
    ("M7-lookup-agent-id", "        if not _is_address(agent_id):\n", "        if False:\n",
     takes_a_garbage_agent_id),
    ("M8-daily-cap", "            if used >= daily_cap:\n", "            if False:\n",
     goes_past_the_daily_cap),
    ("M9-cap-ceiling", "    if not 0 <= daily_cap <= SOLANA_IDENTITY_DAILY_MINT_CAP:\n",
     "    if not 0 <= daily_cap:\n", accepts_a_cap_above_25),
    ("M10-per-address-cap", ") >= per_address_cap:\n", ") >= per_address_cap + 10:\n",
     goes_past_three_per_address),
    ("M11-hold",
     "            if any(now < a.hold_until for a in mine if a.hold_until is not None):\n",
     "            if False:\n", mints_twice_after_a_lost_answer),
    ("M12-budget-in-flight", "            if any(a.hold_until is None for a in mine):\n",
     "            if False:\n", mints_while_another_process_is_minting),
    ("M13-in-flight-join", "        if task is None or task.done():\n", "        if True:\n",
     two_calls_at_once_mint_twice),
    ("M14-release-only-pre-chain", "            if answer.releases:\n", "            if True:\n",
     a_spent_slot_is_given_back),
    ("M15-pre-chain-needs-not-minted",
     ' is not True and mint_status in (None, "not_minted")\n',
     " is not True\n",
     a_refusal_with_a_mint_in_progress_is_given_back),
    ("M16-500-is-uncertain",
     '    return _RegisterVerdict("uncertain", error_code or f"http_{status_code}", **common)\n',
     '    return _RegisterVerdict("unavailable", "x", True, True, **common)\n',
     a_500_not_minted_is_sent_again),
    ("M17-only-no-connection-releases",
     "    if isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)):\n",
     "    if True:\n", a_read_timeout_is_sent_again),
    ("M18-retry-after-bound", "    if retry_after > max_backoff:\n", "    if False:\n",
     waits_longer_than_max_backoff),
    ("M19-register-attempts", "        if not answer.retry or asked >= register_attempts:\n",
     "        if not answer.retry:\n", retries_without_bound),
    ("M20-lookup-attempts", "        if wait is None or asked >= attempts:\n",
     "        if wait is None:\n", looks_up_without_bound),
    ("M21-facilitator-daily-cap", '            if code == "erc8004_daily_write_limit":\n',
     "            if False:\n", the_facilitator_daily_cap_is_not_a_quota),
    ("M22-only-transient-503-retried", "            retry = reason in _TRANSIENT_REFUSALS\n",
     "            retry = True\n", an_empty_fee_payer_is_asked_again),
    ("M23-register-owner",
     '        if body.get("owner") != recipient:\n            return _RegisterVerdict(',
     "        if False:\n            return _RegisterVerdict(", created_for_another_owner),
    ("M24-register-complete", '        if mint_status != "complete":\n', "        if False:\n",
     created_from_a_mint_not_complete),
    ("M25-unreported-needs-the-lookup", "    if answer.reported:\n", "    if True:\n",
     created_without_a_report_nor_a_lookup),
    ("M26-zero-address", "    if raw == bytes(_PUBKEY_LEN):\n", "    if False:\n",
     sends_for_the_zero_address),
    ("M27-decoded-length", "    if len(raw) != _PUBKEY_LEN:\n", "    if False:\n",
     sends_for_an_address_of_33_bytes),
    ("M28-length-before-decoding",
     "    if not _MIN_BASE58_LEN <= len(value) <= _MAX_BASE58_LEN:\n", "    if False:\n",
     decodes_a_long_address),
    ("M29-alphabet-before-decoding", "    if not set(value) <= _BASE58:\n", "    if False:\n",
     decodes_outside_the_alphabet),
    ("M30-network", "    if network not in SOLANA_IDENTITY_NETWORKS:\n", "    if False:\n",
     sends_for_an_evm_network),
    ("M31-agent-uri-scheme", "    if scheme is None or len(agent_uri) == len(scheme):\n",
     "    if False:\n", sends_an_http_agent_uri),
    ("M32-agent-uri-whitespace",
     "    if any(ch.isspace() or ord(ch) < 0x20 or ord(ch) == 0x7F for ch in agent_uri):\n",
     "    if False:\n", sends_an_agent_uri_with_a_newline),
    ("M33-log-redaction", "        recipient=_short(outcome.recipient),\n",
     "        recipient=outcome.recipient,\n", logs_the_full_address),
    ("M34-repr-redaction", '            f"recipient={_short(self.recipient)!r}, network=',
     '            f"recipient={self.recipient!r}, network=', repr_shows_the_full_address),
    ("M35-orphan-reservation-expires",
     "                if a.hold_until is None and now >= a.reserved_at + hold_seconds:\n",
     "                if False:\n", an_orphan_reservation_blocks_for_ever),
    ("M36-release-names-its-request",
     "if attempt.reserved_at == reserved_at:\n                    del mine[index]",
     "if attempt.hold_until is None:\n                    del mine[index]",
     a_late_release_undoes_a_newer_request),
    ("M37-keep-names-its-request",
     "if attempt.reserved_at == reserved_at:\n                    attempt.hold_until",
     "if attempt.hold_until is None:\n                    attempt.hold_until",
     a_late_keep_does_not_hold),
    ("M41-timeout-cap", "    if not (math.isfinite(value) and 0 < value <= _MAX_TIMEOUT):\n",
     "    if not (math.isfinite(value) and 0 < value):\n", accepts_a_timeout_longer_than_the_hold),
    ("M42-total-deadline", "                    register_timeout,\n                )\n",
     "                    None,\n                )\n", a_hanging_post_outlives_its_deadline),
    ("M43-empty-code-is-none", "    if not isinstance(value, str) or not value:\n",
     "    if not isinstance(value, str):\n", an_empty_code_hides_the_real_one),
    ("M44-cap-per-network", "                if _network_of(other) == network\n",
     "                if True\n", devnet_uses_up_mainnet),
    ("M38-per-address-window-is-24h", "if now - a.reserved_at < _SECONDS_PER_DAY) >=",
     "if _utc_day(a.reserved_at) == today) >=", four_requests_across_midnight),
    ("M39-codes-are-codes", "    if _CODE.fullmatch(value) is None:\n", "    if False:\n",
     a_code_carries_the_address),
    ("M40-request-sent-needs-a-connection",
     "                delivered = not answer.releases  # only a connection never made releases\n",
     "                delivered = True\n", says_sent_with_no_connection),
]


@pytest.mark.parametrize("old, new, harm", [c[1:] for c in CASES], ids=[c[0] for c in CASES])
async def test_the_guard_is_what_refuses(old, new, harm):
    assert not await harm(Run(ORIGINAL)), "the module as shipped lets the harm through"
    assert await harm(Run(mutant(old, new))), "the mutant does not: the guard is not what refuses"
