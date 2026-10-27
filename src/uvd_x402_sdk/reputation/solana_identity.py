"""The Solana identity (8004-solana) of a party to be rated, minted once.

A rating on Solana needs the RATED party to own an identity of QuantuLabs'
8004-solana registry (a Metaplex Core asset, its ``AgentAccount`` PDA and its
ATOM stats). The facilitator mints one, pays its rent (about 0.0134 SOL,
13,402,400 lamports as x402-rs ``erc8004/solana_mint.rs`` prices it) and hands
it to a base58 ``recipient`` in the same transaction (``POST /register``,
``network: solana``). Karma Kadabra did this in its own code
(``agents_sdk/rep_solana.py::identidad_del_calificado``) and Execution Market
in another (``integrations/erc8004/reputation_target.py::_ensure_identity`` +
``identity.py::register_worker_gasless``), each with its own guard against a
duplicate, and EM's guard read the identity with an EVM ``eth_call`` against a
Solana RPC, so it never minted. This is the one implementation:
:func:`ensure_solana_identity`.

What it does, in this order, and why each step is there:

1. **Validates the address** (:func:`validate_solana_address`): 32-44 base58
   characters, exactly as given, decoding to 32 bytes that are not all zero.
2. **Joins a call already in flight** for the same address on the same event
   loop: two callers at once get one answer and at most one mint. A call of
   another loop (another thread) is not joined; the budget keeps it to one.
3. **Reads the identity on Solana** (:func:`check_solana_identity`, the
   facilitator's ``GET /identity/<network>/owner/<address>``, a
   ``getProgramAccounts`` scan). Never with an EVM read. Owned -> nothing is
   minted. Only the owner lookup's own 404 (its body carries ``balance: "0"``)
   is "owns nothing"; a 503, a 429, a timeout, a 404 from anything else, or a
   200 naming another owner is NO VERDICT, and nothing is spent on it.
4. **Takes a slot of the caller's budget** (:class:`MintBudget`): at most
   :data:`SOLANA_IDENTITY_DAILY_MINT_CAP` mint requests per UTC day and
   network, at most :data:`MAX_MINT_ATTEMPTS_PER_ADDRESS_PER_DAY` per address
   in 24 hours, none while another request for the address is in flight, and
   none for :data:`MINT_HOLD_SECONDS` after a request that may have spent.
5. **Sends** ``POST /register`` with the caller's ``agent_uri`` (the same URI
   each time for the same address: that is how the facilitator recognises a
   retry of a half-minted identity and finishes it instead of minting another)
   and classifies the answer from ``mint.status``, the HTTP status and the
   facilitator's error codes (:data:`SolanaIdentityStatus`).
6. **Gives the slot back only when the answer proves nothing reached the
   chain** (a refusal the facilitator makes before it signs: 400, 403, 408,
   409, 429, 503 with a JSON body and no ``mint.status`` other than
   ``not_minted``; or no connection at all). Anything else keeps it.
7. **Reads the identity again** after a mint the facilitator calls complete.

The requests go through the :class:`~uvd_x402_sdk.erc8004.Erc8004Client`'s own
HTTP client, base URL and stack key, not through ``register_agent``: that
method turns a timeout, a refused connection and every body that is not a
``RegisterAgentResponse`` (the 429s, the writer-lease 503) into the same
``success=False``, and those are exactly the cases that decide whether a slot
was spent.

No key is involved anywhere (the facilitator signs and pays), and no log line
or ``repr`` carries a full address or URL. Needs no extra: httpx only.

The budget is the caller's and has no default, on purpose: an implicit
per-process counter is how a "hard" 25 a day becomes 25 per worker, and it
forgets on a restart the hold that keeps a lagging lookup from minting twice.
:class:`InMemoryMintBudget` is that counter, for one long-lived process; a
deployment with more than one process, or that restarts, passes a store they
share and that persists (for Execution Market, its ``erc8004_registrations``
claim table). A shared store must key the address exactly as given: base58 is
case-sensitive, and lowercasing it (as an EVM claim does) merges different
addresses. The in-flight join is per event loop.

Accepted on purpose: the lookup runs before the reservation, so a call for an
address that already owns one never touches the budget (and never reads
``quota_exceeded`` for it); the price is that a call stalled for longer than
the hold between the two acts on that lookup. The hold runs on the caller's
``clock`` (wall time, so a shared store can compare it across processes).
"""

from __future__ import annotations

import asyncio
import functools
import math
import re
import threading
import time
from collections.abc import Awaitable
from dataclasses import dataclass
from typing import Any, Callable, Literal, Optional, Protocol

import httpx

from uvd_x402_sdk.bounded_json import loads_bounded
from uvd_x402_sdk.erc8004 import Erc8004Client, _parse_retry_after_header
from uvd_x402_sdk.reputation._redact import log_event, redact_http_client_logs
from uvd_x402_sdk.solana_signing import _B58_ALPHABET, _b58decode

__all__ = [
    "MAX_MINT_ATTEMPTS_PER_ADDRESS_PER_DAY",
    "MINT_HOLD_SECONDS",
    "SOLANA_IDENTITY_DAILY_MINT_CAP",
    "SOLANA_IDENTITY_NETWORKS",
    "InMemoryMintBudget",
    "MintBudget",
    "ReserveVerdict",
    "SolanaIdentityCheck",
    "SolanaIdentityResult",
    "SolanaIdentityStatus",
    "check_solana_identity",
    "ensure_solana_identity",
    "validate_solana_address",
]

#: The networks whose ``POST /register`` mints an 8004-solana identity: the
#: two Solana names of :data:`uvd_x402_sdk.erc8004.Erc8004Network`.
SOLANA_IDENTITY_NETWORKS = frozenset({"solana", "solana-devnet"})

#: Mint requests per UTC day and network, for every address together (devnet
#: does not use mainnet's). The owner's decision
#: of 2026-10-27 ("La plataforma, tope 25/dia"): the platform pays, through the
#: facilitator's fee payer, about 0.0134 SOL (~US$ 1.34 at SOL = US$ 100) per
#: identity, so at most ~0.335 SOL (~US$ 33.50) a day, well under the
#: facilitator's own 150 Solana writes a day (x402-rs
#: ``erc8004/daily_cap.rs``), which ratings share. A caller may ask for LESS
#: (``daily_cap=0`` mints nothing); more is refused: raising it is a release.
SOLANA_IDENTITY_DAILY_MINT_CAP = 25

#: Mint requests per address that may have spent, in any 24 hours (a rolling
#: window, not the UTC day: around midnight a calendar day would let twice as
#: many through). Execution Market's ``_MINT_MAX_ATTEMPTS_PER_DAY`` over the
#: last day (``integrations/erc8004/identity.py::_claim_mint_slot``), brought
#: here with its value and its window: a mint and a retry that finishes it
#: fit, a loop on one address does not.
MAX_MINT_ATTEMPTS_PER_ADDRESS_PER_DAY = 3

#: Seconds after a mint request that may have spent during which no other is
#: sent for the address. Execution Market's ``_MINT_PENDING_TTL_S``: a
#: transaction whose blockhash has expired (about 90 s) can no longer land, and
#: an owner lookup lagging behind a mint that did land would otherwise read
#: "owns nothing" and mint a second identity.
MINT_HOLD_SECONDS = 30 * 60

_PUBKEY_LEN = 32
_MIN_BASE58_LEN = 32  # 32 zero bytes are 32 "1"s; any other key is longer
_MAX_BASE58_LEN = 44  # solana-program's MAX_BASE58_LEN
_BASE58 = frozenset(_B58_ALPHABET)
_AGENT_URI_SCHEMES = ("https://", "ipfs://")
# The facilitator's answers to these two routes are a few hundred bytes.
_MAX_ANSWER_BYTES = 64 * 1024
_SECONDS_PER_DAY = 86_400
_DEFAULT_BACKOFF = 0.5
# Longest a request may take, start to end (a total deadline, not httpx's
# per-phase timeouts): a third of the hold, so a mint request still in flight
# can never outlive the reservation that covers it.
_MAX_TIMEOUT = MINT_HOLD_SECONDS / 3

SolanaIdentityStatus = Literal[
    "created",
    "already_exists",
    "pending",
    "quota_exceeded",
    "unavailable",
    "uncertain",
    "refused",
]
"""What :func:`ensure_solana_identity` found or did.

- ``created``: this call minted the identity and the facilitator confirmed it
  delivered (``mint.status == "complete"``, owner = the recipient).
- ``already_exists``: the owner lookup shows the address owns one; nothing sent.
- ``pending``: a mint for the address started and did not finish: the
  facilitator holds it (``pending_stats`` / ``pending_transfer``; the same
  request later finishes it, it does not mint another), or another call is
  minting it now (``in_flight``). Ask again later.
- ``quota_exceeded``: a daily cap was reached (this module's, per address, or
  the facilitator's 429 ``erc8004_daily_write_limit``). Nothing minted.
- ``unavailable``: the facilitator refused before the chain for a reason that
  passes (503, a 429 rate limit, no connection, the budget unreadable).
  Nothing minted; safe to ask again.
- ``uncertain``: this call cannot say whether the address has an identity: the
  owner lookup gave no verdict (nothing was sent), or a mint request went out
  and its outcome is unknown (timeout, 500, 502). Never minted over; the
  address is held for :data:`MINT_HOLD_SECONDS` after a request.
- ``refused``: the facilitator refused this request and repeating it gets the
  same answer (400, 403, 409 ``held_identity_not_resumable``). Nothing minted.
"""

ReserveVerdict = Literal["granted", "quota_exceeded", "address_exhausted", "in_flight", "held"]
""":meth:`MintBudget.reserve`'s answer. Only ``granted`` lets a request out."""


# -- the address ---------------------------------------------------------------------


def validate_solana_address(value: Any, what: str = "recipient") -> str:
    """``value`` if it is a Solana address the facilitator can hand an identity to.

    32 to 44 characters of the base58 alphabet, taken exactly as given (no
    trimming: the address is also the key of the in-flight join and of the
    budget, and base58 is case-sensitive), decoding to exactly 32 bytes that
    are not all zero (the facilitator refuses the zero key, and no transfer
    can reach it). Raises ``TypeError`` / ``ValueError`` otherwise.
    """
    if not isinstance(value, str):
        raise TypeError(f"{what} must be a base58 string, got {type(value).__name__}")
    if not _MIN_BASE58_LEN <= len(value) <= _MAX_BASE58_LEN:
        raise ValueError(f"{what} must be 32-44 base58 characters, got {len(value)}")
    if not set(value) <= _BASE58:
        raise ValueError(f"{what} has a character outside the base58 alphabet")
    raw = _b58decode(value)
    if len(raw) != _PUBKEY_LEN:
        raise ValueError(f"{what} decodes to {len(raw)} bytes, expected {_PUBKEY_LEN}")
    if raw == bytes(_PUBKEY_LEN):
        raise ValueError(f"{what} is the zero address")
    return value


def _is_address(value: Any) -> bool:
    try:
        validate_solana_address(value)
    except (TypeError, ValueError):
        return False
    return True


def _short(address: Optional[str]) -> Optional[str]:
    """First four and last four characters: enough to correlate two log lines."""
    if not isinstance(address, str) or len(address) <= 10:
        return address
    return address[:4] + "..." + address[-4:]


# -- the results ---------------------------------------------------------------------


@dataclass(frozen=True, repr=False)
class SolanaIdentityCheck:
    """The owner lookup's verdict: ``registered``, ``absent`` or ``unknown``.

    Only ``absent`` lets :func:`ensure_solana_identity` spend. ``reason`` is a
    stable lower_snake code; ``status_code`` / ``retry_after`` are the HTTP
    answer's (``None`` when none came back).
    """

    state: Literal["registered", "absent", "unknown"]
    reason: str
    agent_id: Optional[str] = None
    status_code: Optional[int] = None
    retry_after: Optional[int] = None

    def __repr__(self) -> str:
        return (
            f"SolanaIdentityCheck(state={self.state!r}, reason={self.reason!r}, "
            f"agent_id={_short(self.agent_id)!r})"
        )


@dataclass(frozen=True, repr=False)
class SolanaIdentityResult:
    """What :func:`ensure_solana_identity` found or did. Branch on ``status``.

    ``reason`` is a stable lower_snake code (the facilitator's ``errorCode``
    when it gave one). ``request_sent`` says whether a ``POST /register`` went
    out in this call (so whether anything could have been spent);
    ``confirmed``, on ``created``, whether the owner lookup after the mint
    already shows it. ``mint_status`` is the facilitator's ``mint.status`` as
    received. ``repr`` shortens the addresses.
    """

    status: SolanaIdentityStatus
    reason: str
    recipient: str
    network: str
    agent_id: Optional[str] = None
    transaction: Optional[str] = None
    mint_status: Optional[str] = None
    error_code: Optional[str] = None
    status_code: Optional[int] = None
    retry_after: Optional[int] = None
    request_sent: bool = False
    confirmed: bool = False

    @property
    def has_identity(self) -> bool:
        """Whether ``agent_id`` is an identity the recipient owns now."""
        return self.status in ("created", "already_exists")

    def __repr__(self) -> str:
        return (
            f"SolanaIdentityResult(status={self.status!r}, reason={self.reason!r}, "
            f"recipient={_short(self.recipient)!r}, network={self.network!r}, "
            f"agent_id={_short(self.agent_id)!r}, request_sent={self.request_sent!r})"
        )


# -- the budget ----------------------------------------------------------------------


class MintBudget(Protocol):
    """Where mint requests are counted. Every method is atomic.

    ``key`` is ``"<network>:<address>"``, the address exactly as given. The
    policy numbers come from the caller on every call; the budget only counts.

    - :meth:`reserve`, checked in this order: a request reserved for the key
      and never kept nor released (its process died, its loop closed) for
      ``hold_seconds`` or more is taken as kept at ``reserved_at``: it may
      have spent, and its hold is over. Then ``in_flight`` if the key has a
      reserved request; ``held`` if it has a kept one whose hold has not
      ended; ``address_exhausted`` if the key has ``per_address_cap`` requests
      reserved in the last 24 hours; ``quota_exceeded`` if the keys of the
      same network have ``daily_cap`` reserved on this UTC day; otherwise
      record a reserved request at ``now`` and answer ``granted``.
    - :meth:`keep`: the request reserved for ``key`` at ``reserved_at`` (the
      ``now`` of its :meth:`reserve`) may have spent. It stays counted and
      holds the key until ``now + hold_seconds``, also when it had already
      been taken as kept.
    - :meth:`release`: that request proved nothing was sent. Forget it.

    Both act on THAT request only, never on another one of the key: a caller
    that comes back after its request expired must not undo a newer one.
    """

    async def reserve(
        self,
        key: str,
        *,
        now: float,
        daily_cap: int,
        per_address_cap: int,
        hold_seconds: float,
    ) -> ReserveVerdict: ...

    async def keep(
        self, key: str, *, reserved_at: float, now: float, hold_seconds: float
    ) -> None: ...

    async def release(self, key: str, *, reserved_at: float, now: float) -> None: ...


def _utc_day(now: float) -> int:
    return int(now // _SECONDS_PER_DAY)


@dataclass
class _Attempt:
    reserved_at: float
    hold_until: Optional[float] = None  # None while reserved


def _network_of(key: str) -> str:
    return key.split(":", 1)[0]


class InMemoryMintBudget:
    """:class:`MintBudget` in this process's memory (thread-safe).

    For ONE long-lived process. Two processes with one each have two daily
    caps, and a restart forgets the holds: a deployment like that passes a
    store its processes share and that persists.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._attempts: dict[str, list[_Attempt]] = {}

    async def reserve(
        self,
        key: str,
        *,
        now: float,
        daily_cap: int,
        per_address_cap: int,
        hold_seconds: float,
    ) -> ReserveVerdict:
        today = _utc_day(now)
        network = _network_of(key)
        with self._lock:
            self._forget_past(now)
            mine = self._attempts.setdefault(key, [])
            for a in mine:
                if a.hold_until is None and now >= a.reserved_at + hold_seconds:
                    a.hold_until = a.reserved_at + hold_seconds
            if any(a.hold_until is None for a in mine):
                return "in_flight"
            if any(now < a.hold_until for a in mine if a.hold_until is not None):
                return "held"
            if sum(1 for a in mine if now - a.reserved_at < _SECONDS_PER_DAY) >= per_address_cap:
                return "address_exhausted"
            used = sum(
                1
                for other, each in self._attempts.items()
                if _network_of(other) == network
                for a in each
                if _utc_day(a.reserved_at) == today
            )
            if used >= daily_cap:
                return "quota_exceeded"
            mine.append(_Attempt(reserved_at=now))
            return "granted"

    async def keep(
        self, key: str, *, reserved_at: float, now: float, hold_seconds: float
    ) -> None:
        with self._lock:
            for attempt in self._attempts.get(key, []):
                if attempt.reserved_at == reserved_at:
                    attempt.hold_until = now + hold_seconds
                    return

    async def release(self, key: str, *, reserved_at: float, now: float) -> None:
        with self._lock:
            mine = self._attempts.get(key, [])
            for index, attempt in enumerate(mine):
                if attempt.reserved_at == reserved_at:
                    del mine[index]
                    return

    def used_today(self, now: float, network: Optional[str] = None) -> int:
        """Requests reserved on ``now``'s UTC day, of ``network`` (``None``: all)."""
        today = _utc_day(now)
        with self._lock:
            return sum(
                1
                for key, each in self._attempts.items()
                if network is None or _network_of(key) == network
                for a in each
                if _utc_day(a.reserved_at) == today
            )

    def _forget_past(self, now: float) -> None:
        """Drop requests older than a day whose hold is over: they count for nothing."""
        for key in list(self._attempts):
            kept = [
                a
                for a in self._attempts[key]
                if a.hold_until is None
                or now < a.hold_until
                or now - a.reserved_at < _SECONDS_PER_DAY
            ]
            if kept:
                self._attempts[key] = kept
            else:
                del self._attempts[key]


# -- reading the facilitator's answers -----------------------------------------------


def _json_object(content: bytes) -> Optional[dict]:
    """The answer as a JSON object, or ``None`` (not JSON, too big, not an object)."""
    try:
        value = loads_bounded(content, max_bytes=_MAX_ANSWER_BYTES)
    except (ValueError, TypeError, UnicodeDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _text(value: Any) -> Optional[str]:
    return value if isinstance(value, str) and value else None


_CODE = re.compile(r"[a-z][a-z0-9_]{0,63}")


def _code(value: Any) -> Optional[str]:
    """A facilitator code as it may enter ``reason``, ``repr`` and a log line:
    lower_snake only. Anything else (an address, a URL, prose) is not one; an
    empty string is no code at all, so it hides nothing behind it."""
    if not isinstance(value, str) or not value:
        return None
    if _CODE.fullmatch(value) is None:
        return "facilitator_error"
    return value


def lookup_verdict(
    status_code: int, body: Optional[dict], recipient: str, network: str
) -> SolanaIdentityCheck:
    """The verdict of one ``GET /identity/<network>/owner/<recipient>`` answer.

    Pure, and the contract the TypeScript twin must reproduce
    (``tests/vectors/reputation/solana-identity.json``, ``lookup``).
    """
    if status_code == 200:
        if body is None:
            return SolanaIdentityCheck("unknown", "lookup_unreadable", status_code=200)
        if "network" in body and body["network"] != network:
            return SolanaIdentityCheck("unknown", "lookup_network_mismatch", status_code=200)
        if body.get("owner") != recipient:
            return SolanaIdentityCheck("unknown", "lookup_owner_mismatch", status_code=200)
        agent_id = body.get("agentId")
        if not _is_address(agent_id):
            return SolanaIdentityCheck("unknown", "lookup_unreadable", status_code=200)
        return SolanaIdentityCheck("registered", "owner_lookup", agent_id, status_code=200)
    if status_code == 404:
        # The owner lookup's own "owns nothing" carries the balance it read, as
        # the STRING "0" (x402-rs get_identity_by_owner_solana). A 404 without
        # it is a route the facilitator does not serve, or not the facilitator
        # at all: no verdict. Not the number 0 either: JSON.parse cannot tell
        # 0 from 0.0, so the TypeScript twin could not draw that line.
        balance = body.get("balance") if body is not None else None
        if balance == "0":
            return SolanaIdentityCheck("absent", "owner_not_found", status_code=404)
        return SolanaIdentityCheck("unknown", "lookup_not_a_verdict", status_code=404)
    if status_code == 503:
        return SolanaIdentityCheck("unknown", "lookup_inconclusive", status_code=503)
    if status_code == 429:
        return SolanaIdentityCheck("unknown", "lookup_rate_limited", status_code=429)
    return SolanaIdentityCheck("unknown", f"lookup_http_{status_code}", status_code=status_code)


_LOOKUP_RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})


async def check_solana_identity(
    recipient: str,
    *,
    client: Erc8004Client,
    network: str = "solana",
    timeout: float = 10.0,
    attempts: int = 2,
    max_backoff: float = 5.0,
    sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
) -> SolanaIdentityCheck:
    """Does ``recipient`` own an 8004-solana identity on ``network``? Read on Solana.

    The facilitator's ``GET /identity/<network>/owner/<recipient>`` (a
    ``getProgramAccounts`` scan of the registry by owner): never an EVM read,
    which cannot answer for Solana. Retried up to ``attempts`` times on an
    answer with no verdict that may pass (429, 5xx, no answer), waiting the
    ``Retry-After`` when it is at most ``max_backoff`` seconds and giving up
    when it is longer. Never raises for what the network does; the arguments
    are checked first (``TypeError`` / ``ValueError``).
    """
    validate_solana_address(recipient)
    network = _check_network(network)
    _check_client(client)
    timeout = _timeout("timeout", timeout)
    attempts = _attempts("attempts", attempts)
    max_backoff = _non_negative("max_backoff", max_backoff)
    redact_http_client_logs()
    url = f"{client.base_url}/identity/{network}/owner/{recipient}"
    asked = 0
    while True:
        asked += 1
        try:
            response = await asyncio.wait_for(
                client._client.get(url, timeout=timeout, **client._stack_key_kwargs()),
                timeout,
            )
        except Exception:  # noqa: BLE001 - no answer is no verdict
            check = SolanaIdentityCheck("unknown", "lookup_no_answer")
            wait = _backoff(None, max_backoff)
        else:
            verdict = lookup_verdict(
                response.status_code, _json_object(response.content), recipient, network
            )
            retry_after = _parse_retry_after_header(response.headers.get("retry-after"))
            check = SolanaIdentityCheck(
                verdict.state,
                verdict.reason,
                verdict.agent_id,
                status_code=response.status_code,
                retry_after=retry_after,
            )
            if check.state != "unknown" or response.status_code not in _LOOKUP_RETRY_STATUSES:
                return check
            wait = _backoff(retry_after, max_backoff)
        if wait is None or asked >= attempts:
            return check
        await sleep(wait)


def _backoff(retry_after: Optional[int], max_backoff: float) -> Optional[float]:
    """Seconds to wait before asking again, or ``None`` when the answer asked for
    longer than ``max_backoff`` (that wait is the caller's, not this call's)."""
    if retry_after is None:
        return min(_DEFAULT_BACKOFF, max_backoff)
    if retry_after > max_backoff:
        return None
    return float(retry_after)


@dataclass(frozen=True)
class _RegisterVerdict:
    status: SolanaIdentityStatus
    reason: str
    releases: bool = False  # the answer proves nothing reached the chain
    retry: bool = False  # another POST in this call is safe and may help
    mint_status: Optional[str] = None
    error_code: Optional[str] = None
    agent_id: Optional[str] = None
    transaction: Optional[str] = None
    reported: bool = True  # a ``created`` the facilitator described in full


# Refusals the facilitator makes before it signs anything, that pass: the
# request may be sent again in the same call (x402-rs handlers.rs
# ``refuse_registration`` / ``run_solana_registration``; the writer lease; the
# admission layer of rate_policy.rs, which answers before any route runs).
_TRANSIENT_REFUSALS = frozenset(
    {
        "owner_lookup_inconclusive",
        "registry_unavailable",
        "recipient_screening_unavailable",
        "stats_lookup_inconclusive",
        "writer_lease_unavailable",
        "overloaded",
        "rate_limited",
        "too_many_concurrent_requests",
        "request_timeout",
    }
)
# Statuses the /register route answers only before it reaches the chain; the
# 408 is the admission layer's "the body did not arrive; nothing was done".
_PRE_CHAIN_STATUSES = frozenset({400, 403, 408, 409, 429, 503})
_PENDING_MINT = frozenset({"pending_stats", "pending_transfer"})


def register_verdict(status_code: int, body: Optional[dict], recipient: str) -> _RegisterVerdict:
    """The verdict of one ``POST /register`` answer (Solana).

    Pure, and the contract the TypeScript twin must reproduce
    (``tests/vectors/reputation/solana-identity.json``, ``register``).
    """
    mint = body.get("mint") if body is not None else None
    mint = mint if isinstance(mint, dict) else {}
    mint_status = _code(mint.get("status"))
    error_code = _code(mint.get("errorCode")) or _code((body or {}).get("errorCode"))
    agent_id = (body or {}).get("agentId")
    agent_id = agent_id if _is_address(agent_id) else None
    transaction = _text((body or {}).get("transaction"))
    common: dict[str, Any] = {
        "mint_status": mint_status,
        "error_code": error_code,
        "agent_id": agent_id,
    }

    if body is None:
        # Not the facilitator's JSON (a proxy's page, a truncated body): it
        # proves nothing either way.
        return _RegisterVerdict("uncertain", "unreadable_answer")
    if status_code in _PRE_CHAIN_STATUSES:
        pre_chain = body.get("success") is not True and mint_status in (None, "not_minted")
        if not pre_chain:
            return _RegisterVerdict("uncertain", "inconsistent_answer", **common)
        code = _code(body.get("code"))
        if status_code == 429:
            if code == "erc8004_daily_write_limit":
                return _RegisterVerdict("quota_exceeded", "facilitator_daily_cap", True, **common)
            reason = code if code == "too_many_concurrent_requests" else "rate_limited"
            return _RegisterVerdict("unavailable", reason, True, True, **common)
        if status_code in (408, 503):
            if error_code is not None:
                reason = error_code
            elif _text(body.get("reason")):
                reason = "writer_lease_unavailable"
            elif code is not None:
                reason = code
            else:
                reason = "request_timeout" if status_code == 408 else "service_unavailable"
            retry = reason in _TRANSIENT_REFUSALS
            return _RegisterVerdict("unavailable", reason, True, retry, **common)
        if status_code == 409:
            return _RegisterVerdict("refused", error_code or "conflict", True, **common)
        return _RegisterVerdict("refused", error_code or f"http_{status_code}", True, **common)

    if status_code == 200:
        if body.get("success") is not True:
            return _RegisterVerdict("uncertain", "inconsistent_answer", **common)
        if not mint:
            # Success with no mint report: a facilitator before 2.17.0, whose
            # success meant "the first of three transactions landed". The owner
            # lookup after it decides.
            return _RegisterVerdict(
                "created", "minted", transaction=transaction, reported=False, **common
            )
        if mint_status != "complete":
            return _RegisterVerdict("uncertain", "inconsistent_answer", **common)
        if body.get("owner") != recipient:
            return _RegisterVerdict("uncertain", "owner_mismatch", **common)
        if agent_id is None:
            return _RegisterVerdict("uncertain", "unreadable_answer", **common)
        return _RegisterVerdict("created", "minted", transaction=transaction, **common)

    if status_code == 500 and mint_status in _PENDING_MINT and agent_id is not None:
        # The identity exists in the facilitator's name. The same request
        # finishes it; it does not mint another (x402-rs solana_mint.rs
        # ``decide_mint``).
        return _RegisterVerdict("pending", mint_status, **common)
    if status_code == 502 and body.get("reason") == "forward_unconfirmed":
        return _RegisterVerdict("uncertain", "forward_unconfirmed", **common)
    # A 500 ``not_minted`` included: the facilitator sends with
    # ``send_and_confirm``, and a confirmation that timed out is an error there
    # while the transaction can still land.
    return _RegisterVerdict("uncertain", error_code or f"http_{status_code}", **common)


def _transport_verdict(exc: BaseException) -> _RegisterVerdict:
    """No HTTP answer. Only a connection never made proves nothing was sent."""
    if isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)):
        return _RegisterVerdict("unavailable", "no_connection", True, True)
    return _RegisterVerdict("uncertain", "no_answer")


# -- arguments -----------------------------------------------------------------------


def _check_network(network: Any) -> str:
    if network not in SOLANA_IDENTITY_NETWORKS:
        raise ValueError(f"network must be one of {sorted(SOLANA_IDENTITY_NETWORKS)}")
    return str(network)


def _check_client(client: Any) -> None:
    if not isinstance(client, Erc8004Client):
        raise TypeError("client must be an Erc8004Client")


def _check_budget(budget: Any) -> None:
    if not all(callable(getattr(budget, name, None)) for name in ("reserve", "keep", "release")):
        raise TypeError("budget must be a MintBudget (reserve, keep, release)")


def _check_agent_uri(agent_uri: Any) -> str:
    if not isinstance(agent_uri, str):
        raise TypeError("agent_uri must be a string")
    scheme = next((s for s in _AGENT_URI_SCHEMES if agent_uri.startswith(s)), None)
    if scheme is None or len(agent_uri) == len(scheme):
        raise ValueError("agent_uri must be an https:// or ipfs:// URI")
    if any(ch.isspace() or ord(ch) < 0x20 or ord(ch) == 0x7F for ch in agent_uri):
        raise ValueError("agent_uri must not contain whitespace or control characters")
    return agent_uri


def _check_daily_cap(daily_cap: Any) -> int:
    if type(daily_cap) is not int:
        raise TypeError("daily_cap must be an int")
    if not 0 <= daily_cap <= SOLANA_IDENTITY_DAILY_MINT_CAP:
        raise ValueError(f"daily_cap must be 0..{SOLANA_IDENTITY_DAILY_MINT_CAP}")
    return daily_cap


def _attempts(name: str, value: Any) -> int:
    if type(value) is not int:
        raise TypeError(f"{name} must be an int")
    if not 1 <= value <= 5:
        raise ValueError(f"{name} must be 1..5")
    return value


def _timeout(name: str, value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} must be a number")
    if not (math.isfinite(value) and 0 < value <= _MAX_TIMEOUT):
        raise ValueError(f"{name} must be more than 0 and at most {_MAX_TIMEOUT:g} seconds")
    return float(value)


def _non_negative(name: str, value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} must be a number")
    if not (math.isfinite(value) and value >= 0):
        raise ValueError(f"{name} must be a finite number >= 0")
    return float(value)


# -- the helper ----------------------------------------------------------------------

_IN_FLIGHT_LOCK = threading.Lock()
_IN_FLIGHT: dict[tuple[asyncio.AbstractEventLoop, str], asyncio.Task] = {}

_RESERVE_REFUSALS: dict[str, tuple[SolanaIdentityStatus, str]] = {
    "quota_exceeded": ("quota_exceeded", "client_daily_cap"),
    "address_exhausted": ("quota_exceeded", "address_daily_cap"),
    "in_flight": ("pending", "in_flight"),
    "held": ("uncertain", "recent_request"),
}


async def ensure_solana_identity(
    recipient: str,
    *,
    client: Erc8004Client,
    agent_uri: str,
    budget: MintBudget,
    network: str = "solana",
    daily_cap: int = SOLANA_IDENTITY_DAILY_MINT_CAP,
    lookup_timeout: float = 10.0,
    register_timeout: float = 60.0,
    lookup_attempts: int = 2,
    register_attempts: int = 2,
    max_backoff: float = 5.0,
    clock: Callable[[], float] = time.time,
    sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
) -> SolanaIdentityResult:
    """Make sure ``recipient`` owns an 8004-solana identity on ``network``; mint it once.

    Args:
        recipient: The base58 address that must own the identity, exactly as
            given (:func:`validate_solana_address`). For a person, an address
            they PROVED they hold: the identity is theirs for ever.
        client: The facilitator, an :class:`~uvd_x402_sdk.erc8004.Erc8004Client`
            (its HTTP client, base URL and stack key are used as they are).
        agent_uri: ``https://`` or ``ipfs://``; what the world reads the
            identity as. The SAME URI every time for the same recipient: the
            facilitator recognises a retry of a half-minted identity by it.
        budget: Where mint requests are counted (:class:`MintBudget`), and
            the same one for every call that shares the cap. No default: an
            :class:`InMemoryMintBudget` kept for the life of ONE process, or a
            store that every process shares and that survives a restart.
        network: ``"solana"`` or ``"solana-devnet"``.
        daily_cap: Mint requests per UTC day on ``network``, 0 to
            :data:`SOLANA_IDENTITY_DAILY_MINT_CAP` (more is refused).
        lookup_timeout, register_timeout: Seconds per request, start to end
            (at most a third of :data:`MINT_HOLD_SECONDS`, so a request in
            flight never outlives its reservation). A mint waits for its
            confirmation on the facilitator side, hence the longer one.
        lookup_attempts, register_attempts: Requests per call, 1 to 5. A mint
            request is repeated only after an answer that proves nothing was
            sent and that may pass (a 503 the facilitator gives before signing,
            a 429 rate limit, no connection).
        max_backoff: Longest wait between two requests of one call; an answer
            asking for longer ends the call with its ``retry_after``.
        clock, sleep: Injected by tests.

    Returns:
        A :class:`SolanaIdentityResult`. Never raises for what the facilitator
        or the network does; invalid arguments raise ``TypeError`` /
        ``ValueError`` before any request.

    A second call for the same address while one is in flight on the same
    event loop gets that call's result, whatever its other arguments.
    """
    validate_solana_address(recipient)
    network = _check_network(network)
    _check_client(client)
    agent_uri = _check_agent_uri(agent_uri)
    _check_budget(budget)
    daily_cap = _check_daily_cap(daily_cap)
    options: dict[str, Any] = {
        "lookup_timeout": _timeout("lookup_timeout", lookup_timeout),
        "register_timeout": _timeout("register_timeout", register_timeout),
        "lookup_attempts": _attempts("lookup_attempts", lookup_attempts),
        "register_attempts": _attempts("register_attempts", register_attempts),
        "max_backoff": _non_negative("max_backoff", max_backoff),
    }
    redact_http_client_logs()
    key = f"{network}:{recipient}"
    loop = asyncio.get_running_loop()
    # Joined only on the same loop. A call of another loop is not joined nor
    # turned away: it goes through the lookup and the budget, whose in-flight
    # and expiry rules decide, so a loop that stopped mid-call (and will never
    # finish its task) blocks the address for the hold, not for ever.
    slot = (loop, key)
    with _IN_FLIGHT_LOCK:
        task = _IN_FLIGHT.get(slot)
        if task is None or task.done():
            task = loop.create_task(
                _ensure_once(
                    recipient,
                    key,
                    client=client,
                    agent_uri=agent_uri,
                    network=network,
                    budget=budget,
                    daily_cap=daily_cap,
                    clock=clock,
                    sleep=sleep,
                    **options,
                )
            )
            _IN_FLIGHT[slot] = task
            task.add_done_callback(functools.partial(_forget_in_flight, slot))
    return await asyncio.shield(task)


def _forget_in_flight(slot: tuple[asyncio.AbstractEventLoop, str], task: asyncio.Task) -> None:
    with _IN_FLIGHT_LOCK:
        if _IN_FLIGHT.get(slot) is task:
            del _IN_FLIGHT[slot]


async def _ensure_once(
    recipient: str,
    key: str,
    *,
    client: Erc8004Client,
    agent_uri: str,
    network: str,
    budget: MintBudget,
    daily_cap: int,
    lookup_timeout: float,
    register_timeout: float,
    lookup_attempts: int,
    register_attempts: int,
    max_backoff: float,
    clock: Callable[[], float],
    sleep: Callable[[float], Awaitable[Any]],
) -> SolanaIdentityResult:
    def result(status: SolanaIdentityStatus, reason: str, **fields: Any) -> SolanaIdentityResult:
        return _finish(SolanaIdentityResult(status, reason, recipient, network, **fields))

    async def read() -> SolanaIdentityCheck:
        return await check_solana_identity(
            recipient,
            client=client,
            network=network,
            timeout=lookup_timeout,
            attempts=lookup_attempts,
            max_backoff=max_backoff,
            sleep=sleep,
        )

    check = await read()
    if check.state == "registered":
        return result("already_exists", check.reason, agent_id=check.agent_id)
    if check.state != "absent":
        return result(
            "uncertain",
            check.reason,
            status_code=check.status_code,
            retry_after=check.retry_after,
        )

    payload = {"x402Version": 1, "network": network, "agentUri": agent_uri, "recipient": recipient}
    url = f"{client.base_url}/register"
    sent = False
    asked = 0
    while True:
        asked += 1
        reserved_at = clock()
        try:
            verdict = await budget.reserve(
                key,
                now=reserved_at,
                daily_cap=daily_cap,
                per_address_cap=MAX_MINT_ATTEMPTS_PER_ADDRESS_PER_DAY,
                hold_seconds=MINT_HOLD_SECONDS,
            )
        except Exception:  # noqa: BLE001 - an unreadable budget is no licence to spend
            return result("unavailable", "budget_unavailable", request_sent=sent)
        if verdict != "granted":
            status, reason = _RESERVE_REFUSALS.get(verdict, ("unavailable", "budget_unavailable"))
            return result(status, reason, request_sent=sent)

        status_code: Optional[int] = None
        retry_after: Optional[int] = None
        released = False
        try:
            try:
                response = await asyncio.wait_for(
                    client._client.post(
                        url, json=payload, timeout=register_timeout, **client._stack_key_kwargs()
                    ),
                    register_timeout,
                )
            except Exception as exc:  # noqa: BLE001 - classified, never raised
                answer = _transport_verdict(exc)
                delivered = not answer.releases  # only a connection never made releases
            else:
                status_code = response.status_code
                retry_after = _parse_retry_after_header(response.headers.get("retry-after"))
                answer = register_verdict(status_code, _json_object(response.content), recipient)
                delivered = True
            sent = sent or delivered
            if answer.releases:
                await _resolve(budget.release(key, reserved_at=reserved_at, now=clock()))
                released = True
        finally:
            if not released:
                await _resolve(
                    budget.keep(
                        key, reserved_at=reserved_at, now=clock(), hold_seconds=MINT_HOLD_SECONDS
                    )
                )

        if not answer.retry or asked >= register_attempts:
            break
        wait = _backoff(retry_after, max_backoff)
        if wait is None:
            break
        await sleep(wait)

    fields: dict[str, Any] = {
        "agent_id": answer.agent_id,
        "transaction": answer.transaction,
        "mint_status": answer.mint_status,
        "error_code": answer.error_code,
        "status_code": status_code,
        "retry_after": retry_after,
        "request_sent": sent,
    }
    if answer.status != "created":
        return result(answer.status, answer.reason, **fields)

    after = await read()
    if after.state == "registered":
        fields["agent_id"] = answer.agent_id or after.agent_id
        return result("created", "minted", confirmed=True, **fields)
    if answer.reported:
        return result("created", "minted", **fields)
    return result("uncertain", "success_without_mint_report", **fields)


async def _resolve(step: Awaitable[Any]) -> None:
    """Keep or release a slot. A budget that fails here is left as it is: a
    reserved slot it still holds blocks the address, the safe direction."""
    try:
        await step
    except Exception:  # noqa: BLE001
        log_event(40, "solana_identity.budget_write_failed")


def _finish(outcome: SolanaIdentityResult) -> SolanaIdentityResult:
    level = 20 if outcome.status in ("created", "already_exists") else 30
    log_event(
        level,
        f"solana_identity.{outcome.status}",
        network=outcome.network,
        reason=outcome.reason,
        recipient=_short(outcome.recipient),
        agent_id=_short(outcome.agent_id),
        request_sent=outcome.request_sent,
    )
    return outcome
