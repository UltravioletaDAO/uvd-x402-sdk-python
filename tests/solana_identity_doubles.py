"""A double of the facilitator's two Solana identity routes, with no network and no SOL.

``FakeRegistry`` answers ``GET /identity/<network>/owner/<address>`` and
``POST /register`` the way x402-rs 2.51.2 does (``src/handlers.rs``
``get_identity_by_owner_solana`` and ``run_solana_registration``,
``src/erc8004/solana_mint.rs`` ``SolanaMintReport``), behind an
``httpx.MockTransport`` plugged into a real ``Erc8004Client``. It keeps what is
ON CHAIN (who owns which asset, what the facilitator holds) apart from what the
owner lookup SHOWS (``hidden``: an RPC lagging behind a mint), counts the
identities it mints, resumes an identity it holds only as ``decide_mint`` does
(same agentUri and recipient), and plays scripted answers: a scripted mode may
land a mint and still answer a timeout or a 500, which is the case a client
must not mint again over.
"""

from __future__ import annotations

import asyncio
import json
import threading
from typing import Any, Callable, Optional

import httpx

from uvd_x402_sdk.erc8004 import Erc8004Client
from uvd_x402_sdk.solana_signing import _b58encode

BASE_URL = "https://facilitator.test"
FEE_PAYER = _b58encode(bytes([0xFE]) * 32)
OWNER = "6xNPewUdKRbEZDReQdpyfNUdgNg8QRc8Mt263T5GZSRv"
OTHER = "247Y4QLwz9ZbcuHR2nX2EQLZHCsMs1GTqvgd6fpdn85Q"
AGENT_URI = "https://execution.market/agents/" + OWNER


def address(n: int) -> str:
    """A valid, distinct base58 address per ``n`` (1..250)."""
    return _b58encode(bytes([n]) * 32)


class Clock:
    def __init__(self, t: float = 1_790_000_000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


class Sleeps:
    """``sleep`` double: records each wait, never waits."""

    def __init__(self) -> None:
        self.waits: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.waits.append(seconds)


Answer = Callable[["FakeRegistry", dict], httpx.Response]


class FakeRegistry:
    def __init__(self, network: str = "solana") -> None:
        self.network = network
        self.owned: dict[str, list[str]] = {}  # owner -> assets, on chain
        # asset held by the fee payer -> (its recipient, its agentUri)
        self.held: dict[str, tuple[str, str]] = {}
        self.hidden: set[str] = set()  # owners the lookup does not show yet
        self.lookup_script: list[Any] = []
        self.register_script: list[Any] = []
        self.lookups = 0
        self.posts: list[dict] = []
        self.minted = 0
        self.headers: list[httpx.Headers] = []
        self.gate: Optional[asyncio.Event] = None
        self.thread_gate: Optional[threading.Event] = None  # for a POST from another thread
        self._serial = 0

    # -- the chain ---------------------------------------------------------------
    def new_asset(self) -> str:
        self._serial += 1
        self.minted += 1
        return address(100 + self._serial)

    def give(self, owner: str, asset: Optional[str] = None) -> str:
        asset = asset or self.new_asset()
        self.owned.setdefault(owner, []).append(asset)
        return asset

    # -- the client --------------------------------------------------------------
    def client(self, **kwargs: Any) -> Erc8004Client:
        client = Erc8004Client(base_url=BASE_URL, **kwargs)
        client._client = httpx.AsyncClient(transport=httpx.MockTransport(self.handler))
        return client

    async def handler(self, request: httpx.Request) -> httpx.Response:
        self.headers.append(request.headers)
        path = request.url.path
        prefix = f"/identity/{self.network}/owner/"
        if request.method == "GET" and path.startswith(prefix):
            self.lookups += 1
            owner = path[len(prefix):]
            if self.lookup_script:
                step = self.lookup_script.pop(0)
                if isinstance(step, BaseException):
                    raise step
                return step(self, owner)
            return self.lookup(owner)
        if request.method == "POST" and path == "/register":
            body = json.loads(request.content)
            self.posts.append(body)
            if self.gate is not None:
                await self.gate.wait()
            if self.thread_gate is not None:
                await asyncio.get_running_loop().run_in_executor(None, self.thread_gate.wait, 10)
            step = self.register_script.pop(0) if self.register_script else complete
            return step(self, body)
        return httpx.Response(404, json={"error": "Not Found", "code": "not_found"})

    def lookup(self, owner: str) -> httpx.Response:
        assets = self.owned.get(owner, [])
        if not assets or owner in self.hidden:
            return httpx.Response(
                404,
                json={
                    "error": f"Address {owner} does not own any agent on {self.network}",
                    "balance": "0",
                },
            )
        return httpx.Response(
            200,
            json={
                "agentId": assets[0],
                "owner": owner,
                "agentUri": AGENT_URI,
                "network": self.network,
                "balance": str(len(assets)),
            },
        )


# -- what POST /register answers (Solana) ------------------------------------------
def _report(status: str, error_code: Optional[str] = None, **extra: Any) -> dict:
    report = {"status": status, "resumed": False, "atomic": True, "metadataSkipped": False}
    if error_code:
        report["errorCode"] = error_code
    report.update(extra)
    return report


def _refusal(fake: FakeRegistry, status: int, code: str, *, top_level: bool = False,
             headers: Optional[dict] = None) -> httpx.Response:
    body: dict = {
        "success": False,
        "agentId": None,
        "transaction": None,
        "transferTransaction": None,
        "owner": None,
        "error": f"refused: {code}",
        "network": fake.network,
        "mint": _report("not_minted", code, atomic=False),
    }
    if top_level:  # handlers.rs register_refusal adds these two
        body["errorCode"] = code
        if status == 503:
            body["retryable"] = True
    return httpx.Response(status, json=body, headers=headers)


def _decide_mint(fake: FakeRegistry, body: dict) -> tuple[Optional[str], bool]:
    """x402-rs ``solana_mint.rs::decide_mint``: (asset to resume, refuse).

    A held identity is resumed only for the same agentUri AND the recipient it
    was minted for; the same agentUri for another recipient is a 409; another
    agentUri (or none) is a fresh mint, paid again.
    """
    uri = body.get("agentUri") or ""
    if not uri:
        return None, False
    not_resumable = False
    for asset, (recipient, held_uri) in fake.held.items():
        if held_uri != uri:
            continue
        if recipient == body["recipient"]:
            return asset, False
        not_resumable = True
    return None, not_resumable


def complete(fake: FakeRegistry, body: dict) -> httpx.Response:
    recipient = body["recipient"]
    resumed, refuse = _decide_mint(fake, body)
    if refuse:
        return held_not_resumable(fake, body)
    if resumed is not None:
        del fake.held[resumed]
        asset = fake.give(recipient, resumed)
    else:
        asset = fake.give(recipient)
    signature = "5" * 88
    return httpx.Response(
        200,
        json={
            "success": True,
            "agentId": asset,
            "transaction": signature,
            "transferTransaction": signature,
            "owner": recipient,
            "network": fake.network,
            "mint": _report("complete", statsTransaction=signature, resumed=resumed is not None),
        },
    )


def pending_transfer(fake: FakeRegistry, body: dict) -> httpx.Response:
    resumed, refuse = _decide_mint(fake, body)
    if refuse:
        return held_not_resumable(fake, body)
    asset = resumed or fake.new_asset()
    fake.held[asset] = (body["recipient"], body.get("agentUri") or "")
    return httpx.Response(
        500,
        json={
            "success": False,
            "agentId": asset,
            "transaction": "4" * 88,
            "transferTransaction": None,
            "owner": FEE_PAYER,
            "error": "Identity exists and is initialized but is still held by the facilitator.",
            "network": fake.network,
            "mint": _report("pending_transfer", "transfer_failed", atomic=False),
        },
    )


def held_not_resumable(fake: FakeRegistry, body: dict) -> httpx.Response:
    return _refusal(fake, 409, "held_identity_not_resumable")


def fee_payer_empty(fake: FakeRegistry, body: dict) -> httpx.Response:
    return _refusal(fake, 503, "fee_payer_insufficient_balance")


def registry_unavailable(fake: FakeRegistry, body: dict) -> httpx.Response:
    return _refusal(fake, 503, "registry_unavailable")


def screening_unavailable(fake: FakeRegistry, body: dict) -> httpx.Response:
    return _refusal(fake, 503, "recipient_screening_unavailable", top_level=True)


def agent_uri_refused(fake: FakeRegistry, body: dict) -> httpx.Response:
    return _refusal(fake, 400, "agent_uri_tunnel", top_level=True)


def writer_lease(retry_after: str = "5") -> Answer:
    def answer(fake: FakeRegistry, body: dict) -> httpx.Response:
        return httpx.Response(
            503,
            json={"error": "this instance does not hold the EVM writer lease; retry",
                  "reason": "forward_failed"},
            headers={"Retry-After": retry_after},
        )
    return answer


def rate_limited(retry_after: str = "1") -> Answer:
    def answer(fake: FakeRegistry, body: dict) -> httpx.Response:
        return httpx.Response(
            429,
            json={"error": "Too Many Requests! Wait for 1s", "code": "rate_limited"},
            headers={"Retry-After": retry_after},
        )
    return answer


def daily_cap_reached(fake: FakeRegistry, body: dict) -> httpx.Response:
    return httpx.Response(
        429,
        json={
            "success": False,
            "error": "The daily ERC-8004 write limit for this network has been reached.",
            "code": "erc8004_daily_write_limit",
            "network": fake.network,
        },
        headers={"Retry-After": "40000"},
    )


def lands_then(exc: BaseException) -> Answer:
    """The mint lands on chain, and the answer is lost."""
    def answer(fake: FakeRegistry, body: dict) -> httpx.Response:
        fake.give(body["recipient"])
        raise exc
    return answer


def raises(exc: BaseException) -> Answer:
    def answer(fake: FakeRegistry, body: dict) -> httpx.Response:
        raise exc
    return answer


def landed_but_not_minted(fake: FakeRegistry, body: dict) -> httpx.Response:
    """``send_and_confirm`` timed out waiting; the transaction still landed."""
    fake.give(body["recipient"])
    return _refusal(fake, 500, "mint_transaction_failed")


def forward_unconfirmed(fake: FakeRegistry, body: dict) -> httpx.Response:
    fake.give(body["recipient"])
    return httpx.Response(
        502,
        json={"error": "the EVM writer lease holder received this write and its answer was "
                       "lost", "reason": "forward_unconfirmed", "retryable": False},
    )


def success_for(owner: str) -> Answer:
    """A complete mint that names another owner."""
    def answer(fake: FakeRegistry, body: dict) -> httpx.Response:
        response = complete(fake, {**body, "recipient": owner})
        return response
    return answer


def success_without_report(fake: FakeRegistry, body: dict) -> httpx.Response:
    asset = fake.give(body["recipient"])
    return httpx.Response(
        200,
        json={"success": True, "agentId": asset, "transaction": "3" * 88,
              "owner": body["recipient"], "network": fake.network},
    )


# -- what the owner lookup answers when it has no verdict ---------------------------
def lookup_status(status: int, body: Any, headers: Optional[dict] = None):
    def answer(fake: FakeRegistry, owner: str) -> httpx.Response:
        if isinstance(body, (bytes, str)):
            return httpx.Response(status, content=body, headers=headers)
        return httpx.Response(status, json=body, headers=headers)
    return answer


LOOKUP_INCONCLUSIVE = lookup_status(
    503, {"error": "Could not determine agent ID: getProgramAccounts failed", "retryable": True}
)
LOOKUP_ROUTE_NOT_FOUND = lookup_status(404, {"error": "Not Found", "code": "not_found"})


def lookup_owned_by(owner: str):
    def answer(fake: FakeRegistry, asked: str) -> httpx.Response:
        return httpx.Response(
            200,
            json={"agentId": address(99), "owner": owner, "agentUri": AGENT_URI,
                  "network": fake.network, "balance": "1"},
        )
    return answer
