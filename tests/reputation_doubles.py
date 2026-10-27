"""Doubles of the chain and the facilitator for the reputation tests.

No network, no real key. The signer is the fixed test key built at RUNTIME
from a repeated byte (as the KK vectors do), so no file carries a key.

The facilitator double is not a stub that answers "ok": it builds the typed
data the way x402-rs ``relay_v4.rs::give_feedback_typed_data`` does and, on
submit, verifies the rater's signature with eth-account's OWN typed-data
encoder and the EIP-7702 authorization with the ``rlp`` package and
eth-keys: an implementation independent of the one under test, so a digest or
an RLP this module got wrong is refused here, not on-chain.

Both doubles are STRICT: a scripted answer that is never consumed fails the
test that scripted it (:meth:`FakeFacilitator.assert_consumed`, called by the
fixtures at teardown). A double nobody used does not stay green.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

import httpx

from uvd_x402_sdk.erc8004 import Erc8004Client
from uvd_x402_sdk.reputation._chains import RELAY_CHAIN_IDS, RELAY_DELEGATES
from uvd_x402_sdk.reputation.payment import TRANSFER_TOPIC, RpcError
from uvd_x402_sdk.reputation.submit import NEW_FEEDBACK_TOPIC

REGISTRY = "0x8004BAa17C55a88189AE136b182e5fdA19dE9b63"
IDENTITY = "0x8004A169FB4a3325136EB29fA0ceB6D2e539a432"
USDC_BASE = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
GENESIS_BLOCK = 50_000_000
GENESIS_TIME = 1_790_000_000
BLOCK_SECONDS = 2


def fixed_key(byte: str = "11") -> str:
    """The fixed test key ``0x<byte * 32>``, built at runtime."""
    return "0x" + byte * 32


def fixed_account(byte: str = "11"):
    from eth_account import Account

    return Account.from_key(fixed_key(byte))


class Clock:
    def __init__(self, now: float = GENESIS_TIME + 10_000) -> None:
        self.now = float(now)

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += max(seconds, 0.001)


def _hex(value: int) -> str:
    return hex(int(value))


def _topic(address: str) -> str:
    return "0x" + address.lower()[2:].rjust(64, "0")


@dataclass
class _Tx:
    hash: str
    sender: str
    nonce: int
    block: int
    status: int
    logs: list
    to: str = ""


class FakeChain:
    """A JSON-RPC double over virtual blocks: block ``n`` exists once the clock
    reaches ``GENESIS_TIME + (n - GENESIS_BLOCK) * BLOCK_SECONDS``."""

    def __init__(self, clock: Clock, *, chain_id: int = 8453, token: str = USDC_BASE) -> None:
        self.clock = clock
        self.chain_id = chain_id
        self.token = token
        self.txs: dict[str, _Tx] = {}
        self.owners: dict[int, str] = {}
        self.code: dict[str, str] = {}
        #: Nonces consumed by transactions this double did not see (another
        #: process, another wallet software): what makes a payment unminable.
        self.foreign_nonces: dict[str, int] = {}
        #: Broadcast but never mined (``auto_mine = False``).
        self.mempool: set = set()
        self.calls: list[tuple[str, list]] = []
        self.broadcasts: list[str] = []
        self.auto_mine = True
        self.revert_next = False
        #: method -> how many times it still fails (RpcError) before answering.
        self.failures: dict[str, int] = {}
        self.base_fee = 5_000_000
        self.priority_fee = 1_000_000
        self.gas_estimate = 60_000
        #: rater -> (EIP-7702 designator code, block it was set in).
        self.delegations: dict[str, tuple[str, int]] = {}
        #: (rater, relay nonce) -> block it was spent in.
        self.used_nonces: dict[tuple[str, str], int] = {}
        #: rater -> blocks in which an EIP-7702 authorization of theirs applied.
        self.authority_bumps: dict[str, list[int]] = {}
        #: A load-balanced RPC: for the next ``logs_lag_calls`` eth_getLogs, the
        #: node answering is ``logs_lag`` blocks behind and says "nothing" for
        #: the blocks it does not have yet.
        self.logs_lag = 0
        self.logs_lag_calls = 0
        #: block -> the hash a node now reports for it (a reorg moved it).
        self.moved_hashes: dict[int, str] = {}
        #: The next ``receipts_lag_calls`` receipt reads come from a node behind
        #: (it answers null), while the nonce comes from one ahead.
        self.receipts_lag_calls = 0
        #: Whether eth_getTransactionByHash comes from that same lagging node
        #: (False: another node answers it, up to date).
        self.tx_by_hash_lags = True
        #: tx -> the transactionHash a confused node writes in its receipt.
        self.receipt_hash_override: dict[str, str] = {}
        #: The node answering STATE reads (getCode, eth_call, the nonce) is this
        #: many blocks behind: "latest" is its own head, and a block it does not
        #: have (by number or by hash) errors.
        self.state_lag = 0
        #: The node answering eth_getCode answers null (a load-balanced RPC).
        self.null_code_reads = 0
        #: tx -> its receipt's logs come back flagged ``removed`` (a reorg the
        #: node is still unwinding).
        self.removed_logs: set = set()
        #: An orphaned block hash -> the canonical block whose state it shows.
        #: A node answers state at it only when ``requireCanonical`` is false.
        self.orphans: dict[str, int] = {}
        #: When set, ``safe`` is answered by a node on a fork that a reorg then
        #: dropped: the header carries an orphan hash whose state is the
        #: canonical state at this block (before whatever landed after it).
        self.orphan_safe_from: Optional[int] = None

    #: ``safe`` trails the head by this many blocks (an OP-stack chain).
    SAFE_LAG = 5

    def _block(self, tag: Any, *, lag: int = 0) -> int:
        """A block tag (number, ``latest``/``safe``, EIP-1898 ``{"blockHash"}``)
        as a number, for a node ``lag`` blocks behind; one it lacks errors."""
        head = self.latest() - lag
        if isinstance(tag, dict) and tag["blockHash"] in self.orphans:
            if tag.get("requireCanonical"):
                raise RpcError("rpc_error", "hash is not currently canonical")
            return self.orphans[tag["blockHash"]]
        if isinstance(tag, dict):
            number = next(
                (
                    n
                    for n in range(head, GENESIS_BLOCK - 1, -1)
                    if self.header(n)["hash"] == tag["blockHash"]
                ),
                None,
            )
            if number is None:
                raise RpcError("rpc_error", "header not found")
            return number
        if tag in ("latest", "pending"):
            return head
        if tag == "safe":
            return head - self.SAFE_LAG
        number = int(tag, 16)
        if number > head:
            raise RpcError("rpc_error", "header not found")
        return number

    # -- blocks ----------------------------------------------------------------
    def latest(self) -> int:
        return GENESIS_BLOCK + int((self.clock.now - GENESIS_TIME) // BLOCK_SECONDS)

    def timestamp(self, number: int) -> int:
        return GENESIS_TIME + (number - GENESIS_BLOCK) * BLOCK_SECONDS

    def block_hash(self, number: int) -> str:
        from eth_utils import keccak

        return "0x" + bytes(keccak(text=f"block-{number}")).hex()

    def header(self, number: int) -> dict:
        return {
            "number": _hex(number),
            "hash": self.moved_hashes.get(number, self.block_hash(number)),
            "timestamp": _hex(self.timestamp(number)),
            "baseFeePerGas": _hex(self.base_fee),
        }

    # -- writes the doubles make -------------------------------------------------
    def mine_feedback(
        self,
        rater: str,
        *,
        agent_id: int,
        value: int,
        tag1: str,
        tag2: str,
        endpoint: str,
        feedback_uri: str,
        feedback_hash: str,
        feedback_index: int = 1,
        nonce: Optional[str] = None,
        delegate: Optional[str] = None,
    ) -> str:
        """A relayed ``giveFeedback`` landing in the next block; returns its tx hash.

        ``delegate``: the transaction carried the EIP-7702 authorization (the
        rater's code is set in that block). ``nonce``: spent in that block.
        """
        from eth_abi import encode
        from eth_utils import keccak

        block = self.latest() + 1
        if delegate is not None:
            self.delegations[rater.lower()] = ("0xef0100" + delegate.lower()[2:], block)
            self.authority_bumps.setdefault(rater.lower(), []).append(block)
        if nonce is not None and not self.revert_next:
            self.used_nonces[(rater.lower(), nonce.lower())] = block
        tx_hash = (
            "0x"
            + bytes(keccak(text=f"relay-{rater}-{agent_id}-{feedback_uri}-{block}-{nonce}")).hex()
        )
        data = (
            "0x"
            + encode(
                ["uint64", "int128", "uint8", "string", "string", "string", "string", "bytes32"],
                [
                    feedback_index,
                    value,
                    0,
                    tag1,
                    tag2,
                    endpoint,
                    feedback_uri,
                    bytes.fromhex(feedback_hash[2:]),
                ],
            ).hex()
        )
        log = {
            "address": REGISTRY,
            "blockNumber": _hex(block),
            "transactionHash": tx_hash,
            "topics": [
                NEW_FEEDBACK_TOPIC,
                "0x" + format(agent_id, "064x"),
                _topic(rater),
                "0x" + bytes(keccak(text=tag1)).hex(),
            ],
            "data": data,
            "removed": False,
        }
        status = 0 if self.revert_next else 1
        self.revert_next = False
        self.txs[tx_hash] = _Tx(
            tx_hash, rater, -1, block, status, [log] if status else [], REGISTRY
        )
        return tx_hash

    # -- the RPC -------------------------------------------------------------------
    async def request(self, method: str, params: list) -> Any:
        self.calls.append((method, params))
        if self.failures.get(method):
            self.failures[method] -= 1
            raise RpcError("rpc_http_503")
        handler = getattr(self, "_" + method, None)
        if handler is None:
            raise AssertionError(f"FakeChain does not serve {method}")
        return handler(*params)

    def _eth_chainId(self) -> str:
        return _hex(self.chain_id)

    def _eth_blockNumber(self) -> str:
        return _hex(self.latest())

    def _eth_getBlockByNumber(self, number: str, full: bool) -> Optional[dict]:
        if number == "safe" and self.orphan_safe_from is not None:
            header = self.header(self._block(number))
            header["hash"] = "0x" + "0f" * 31 + format(len(self.orphans) % 256, "02x")
            self.orphans[header["hash"]] = self.orphan_safe_from
            return header
        if number in ("latest", "safe"):
            return self.header(self._block(number))
        n = int(number, 16)
        return self.header(n) if n <= self.latest() else None

    def _eth_getCode(self, address: str, tag: Any) -> Optional[str]:
        if self.null_code_reads > 0:
            self.null_code_reads -= 1
            return None
        block = self._block(tag, lag=self.state_lag)
        delegation = self.delegations.get(address.lower())
        if delegation is not None and delegation[1] <= block:
            return delegation[0]
        return self.code.get(address.lower(), "0x")

    def _eth_estimateGas(self, tx: dict) -> str:
        return _hex(self.gas_estimate)

    def _eth_maxPriorityFeePerGas(self) -> str:
        return _hex(self.priority_fee)

    def sender_nonce(self, address: str, *, pending: bool, block: Optional[int] = None) -> int:
        upto = self.latest() if block is None else block
        own = sum(
            1
            for t in self.txs.values()
            if t.sender.lower() == address.lower() and t.nonce >= 0 and (pending or t.block <= upto)
        )
        # Every EIP-7702 authorization applied (a delegation, an undelegation)
        # bumps the authority's nonce, in the block it was applied in.
        bumps = sum(
            1 for b in self.authority_bumps.get(address.lower(), []) if pending or b <= upto
        )
        return own + bumps + self.foreign_nonces.get(address.lower(), 0)

    def undelegate(self, rater: str) -> None:
        """The emergency exit: an authorization to address(0), in the next block."""
        self.delegations.pop(rater.lower(), None)
        self.authority_bumps.setdefault(rater.lower(), []).append(self.latest() + 1)

    def _eth_getTransactionCount(self, address: str, tag: str) -> str:
        if tag == "pending":
            return _hex(self.sender_nonce(address, pending=True))
        return _hex(
            self.sender_nonce(address, pending=False, block=self._block(tag, lag=self.state_lag))
        )

    def _eth_call(self, call: dict, tag: str) -> str:
        from uvd_x402_sdk.reputation.submit import NONCE_USED_SELECTOR

        data = call["data"]
        if call["to"].lower() == IDENTITY.lower() and data.startswith("0x6352211e"):
            agent = int(data[10:], 16)
            if agent not in self.owners:
                raise RpcError("rpc_error", "execution reverted")
            return "0x" + self.owners[agent].lower()[2:].rjust(64, "0")
        if data.startswith(NONCE_USED_SELECTOR):
            block = self._block(tag, lag=self.state_lag)
            rater, nonce = call["to"].lower(), "0x" + data[10:].lower()
            delegation = self.delegations.get(rater)
            if delegation is None or delegation[1] > block:
                return "0x"  # a call to an EOA with no code returns nothing
            spent = self.used_nonces.get((rater, nonce))
            return "0x" + format(
                1 if spent is not None and spent <= block else 0, "064x"
            )  # ABI bool
        raise AssertionError("unexpected eth_call")

    def _eth_sendRawTransaction(self, raw: str) -> str:
        from eth_account import Account
        from eth_account.typed_transactions import TypedTransaction
        from eth_utils import keccak
        from hexbytes import HexBytes

        data = bytes.fromhex(raw[2:])
        tx_hash = "0x" + bytes(keccak(data)).hex()
        self.broadcasts.append(tx_hash)
        if tx_hash in self.txs or tx_hash in self.mempool:
            raise RpcError("rpc_error", "already known")
        decoded = TypedTransaction.from_bytes(HexBytes(data)).as_dict()
        sender = Account.recover_transaction(data)
        expected = self.sender_nonce(sender, pending=True)
        if int(decoded["nonce"]) < expected:
            raise RpcError("rpc_error", "nonce too low")
        if not self.auto_mine:
            self.mempool.add(tx_hash)
            return tx_hash
        assert int(decoded["nonce"]) == expected, "nonce gap in the double"

        def text(value: Any) -> str:
            raw_text = bytes(value).hex() if isinstance(value, (bytes, bytearray)) else str(value)
            return raw_text.lower().removeprefix("0x")

        to = "0x" + text(decoded["to"])
        payload = text(decoded["data"])
        assert payload.startswith("a9059cbb"), "only USDC transfers are expected"
        payee = "0x" + payload[8 + 24 : 8 + 64]
        amount = int(payload[8 + 64 :], 16)
        block = self.latest() + 1
        status = 0 if self.revert_next else 1
        self.revert_next = False
        logs = (
            []
            if not status
            else [
                {
                    "address": self.token,
                    "blockNumber": _hex(block),
                    "transactionHash": tx_hash,
                    "topics": [TRANSFER_TOPIC, _topic(sender), _topic(payee)],
                    "data": _hex(amount),
                    "removed": False,
                }
            ]
        )
        self.txs[tx_hash] = _Tx(tx_hash, sender, int(decoded["nonce"]), block, status, logs, to)
        return tx_hash

    def _eth_getTransactionByHash(self, tx_hash: str) -> Optional[dict]:
        if self.receipts_lag_calls > 0 and self.tx_by_hash_lags:
            return None  # the lagging node has not seen it at all
        tx = self.txs.get(tx_hash.lower())
        if tx is not None:
            mined = tx.block <= self.latest()
            return {"hash": tx_hash, "blockNumber": _hex(tx.block) if mined else None}
        return {"hash": tx_hash, "blockNumber": None} if tx_hash.lower() in self.mempool else None

    def _eth_getTransactionReceipt(self, tx_hash: str) -> Optional[dict]:
        tx = self.txs.get(tx_hash.lower())
        if self.receipts_lag_calls > 0:
            self.receipts_lag_calls -= 1
            return None  # answered by a node that is behind
        if tx is None or tx.block > self.latest():
            return None
        return {
            "transactionHash": self.receipt_hash_override.get(tx.hash, tx.hash),
            "status": _hex(tx.status),
            "blockNumber": _hex(tx.block),
            "blockHash": self.block_hash(tx.block),
            "logs": [
                dict(log, removed=True) if tx.hash in self.removed_logs else dict(log)
                for log in tx.logs
            ],
            "gasUsed": _hex(60_000),
            "effectiveGasPrice": _hex(self.base_fee),
        }

    def _eth_getLogs(self, query: dict) -> list:
        start, end = int(query["fromBlock"], 16), int(query["toBlock"], 16)
        wanted = [t.lower() for t in query.get("topics") or []]
        head = self.latest()
        if self.logs_lag_calls > 0:
            self.logs_lag_calls -= 1
            head -= self.logs_lag  # this node is behind: "nothing" past its head
        out = []
        for tx in self.txs.values():
            if not start <= tx.block <= min(end, head):
                continue
            for log in tx.logs:
                topics = [t.lower() for t in log["topics"]]
                if (
                    log["address"].lower() == query["address"].lower()
                    and topics[: len(wanted)] == wanted
                ):
                    out.append(dict(log))
        return out


@dataclass
class Prepared:
    rater: str
    params: dict
    deadline: int
    nonce: str
    digest: str


@dataclass
class FakeFacilitator:
    """``/feedback/evm/prepare`` + ``/submit`` of x402-rs, over a :class:`FakeChain`.

    ``submit_modes`` scripts the answers to successive submits:

    - ``"ok"``: relays and answers 200 with the transaction;
    - ``"lost"``: relays, then answers 504 (the answer is lost);
    - ``"uncertain"``: relays, answers 200 without a transaction;
    - ``"429"`` / ``"503"`` / ``"400"``: does NOT relay and answers that.

    ``prepare_modes`` likewise (``"ok"``, ``"429"``, ``"503"``). An exhausted
    script answers ``"ok"``; a script not fully consumed fails the test.
    """

    chain: FakeChain
    clock: Clock
    submit_modes: list = field(default_factory=list)
    prepare_modes: list = field(default_factory=list)
    tamper: Optional[Callable[[dict], None]] = None
    #: Refuse every submit with a 400 (a definite, permanent refusal).
    refuse_all_submits: bool = False
    delegated: set = field(default_factory=set)
    prepares: list = field(default_factory=list)
    submits: list = field(default_factory=list)
    _counter: int = 0
    _pending: dict = field(default_factory=dict)
    _used: set = field(default_factory=set)

    def assert_consumed(self) -> None:
        assert not self.submit_modes, f"scripted submits never used: {self.submit_modes}"
        assert not self.prepare_modes, f"scripted prepares never used: {self.prepare_modes}"

    def client(self) -> Erc8004Client:
        client = Erc8004Client(base_url="https://facilitator.test")
        client._client = httpx.AsyncClient(transport=httpx.MockTransport(self.handle))
        return client

    # -- the protocol ----------------------------------------------------------------
    def handle(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if request.url.path == "/feedback/evm/prepare":
            return self._prepare(body)
        if request.url.path == "/feedback/evm/submit":
            return self._submit(body)
        return httpx.Response(404, json={"error": "not found"})

    def _typed_data(
        self, chain_id: int, rater: str, feedback: dict, deadline: int, nonce: str
    ) -> dict:
        from eth_utils import to_checksum_address

        return {
            "domain": {
                "name": "FeedbackDelegate",
                "version": "1",
                "chainId": chain_id,
                "verifyingContract": to_checksum_address(rater),
            },
            "primaryType": "RelayedGiveFeedback",
            "types": {
                "EIP712Domain": [
                    {"name": "name", "type": "string"},
                    {"name": "version", "type": "string"},
                    {"name": "chainId", "type": "uint256"},
                    {"name": "verifyingContract", "type": "address"},
                ],
                "RelayedGiveFeedback": [
                    {"name": n, "type": t}
                    for n, t in (
                        ("registry", "address"),
                        ("agentId", "uint256"),
                        ("value", "int128"),
                        ("valueDecimals", "uint8"),
                        ("tag1", "string"),
                        ("tag2", "string"),
                        ("endpoint", "string"),
                        ("feedbackURI", "string"),
                        ("feedbackHash", "bytes32"),
                        ("deadline", "uint256"),
                        ("nonce", "bytes32"),
                    )
                ],
            },
            "message": {
                "registry": REGISTRY,
                "agentId": str(feedback["agentId"]),
                "value": str(feedback["value"]),
                "valueDecimals": int(feedback.get("valueDecimals", 0)),
                "tag1": feedback.get("tag1", ""),
                "tag2": feedback.get("tag2", ""),
                "endpoint": feedback.get("endpoint", ""),
                "feedbackURI": feedback.get("feedbackUri", ""),
                "feedbackHash": str(feedback.get("feedbackHash", "0x" + "00" * 32)).lower(),
                "deadline": str(deadline),
                "nonce": nonce,
            },
        }

    @staticmethod
    def _digest(typed: dict) -> str:
        from eth_account.messages import encode_typed_data
        from eth_utils import keccak

        signable = encode_typed_data(full_message=typed)
        return (
            "0x" + bytes(keccak(b"\x19" + signable.version + signable.header + signable.body)).hex()
        )

    def _prepare(self, body: dict) -> httpx.Response:
        mode = self.prepare_modes.pop(0) if self.prepare_modes else "ok"
        self.prepares.append(body)
        if mode != "ok":
            return httpx.Response(int(mode), headers={"Retry-After": "30"}, json={"error": mode})
        feedback = body["feedback"]
        chain_id = RELAY_CHAIN_IDS[body["network"]]
        rater = feedback["rater"]
        self._counter += 1
        nonce = "0x" + format(self._counter, "064x")
        deadline = int(self.clock.now) + 900
        typed = self._typed_data(chain_id, rater, feedback, deadline, nonce)
        digest = self._digest(typed)
        self._pending[nonce] = Prepared(rater, feedback, deadline, nonce, digest)
        delegated = rater.lower() in self.delegated or rater.lower() in self.chain.delegations
        answer = {
            "success": True,
            "delegate": RELAY_DELEGATES[chain_id],
            "digest": digest,
            "typedData": typed,
            "deadline": deadline,
            "nonce": nonce,
            "delegated": delegated,
            "accountNonce": None if delegated else self.chain.sender_nonce(rater, pending=False),
            "chainId": chain_id,
            "network": body["network"],
        }
        if self.tamper is not None:
            self.tamper(answer)
        return httpx.Response(200, json=answer)

    def _check_authorization(self, auth: dict, rater: str, chain_id: int) -> bool:
        import rlp
        from eth_keys import keys
        from eth_utils import keccak

        if (
            int(auth["chainId"]) != chain_id
            or auth["address"].lower() != RELAY_DELEGATES[chain_id].lower()
        ):
            return False
        preimage = b"\x05" + rlp.encode(
            [int(auth["chainId"]), bytes.fromhex(auth["address"][2:]), int(auth["nonce"])]
        )
        signature = keys.Signature(
            vrs=(int(auth["yParity"]), int(auth["r"], 16), int(auth["s"], 16))
        )
        recovered = signature.recover_public_key_from_msg_hash(
            keccak(preimage)
        ).to_checksum_address()
        return recovered.lower() == rater.lower()

    def _submit(self, body: dict) -> httpx.Response:
        from eth_keys import keys

        self.submits.append(body)
        if self.refuse_all_submits:
            return httpx.Response(400, json={"error": "proof_replayed"})
        mode = self.submit_modes.pop(0) if self.submit_modes else "ok"
        if mode in ("429", "503", "400"):
            return httpx.Response(int(mode), headers={"Retry-After": "30"}, json={"error": mode})
        feedback = body["feedback"]
        rater = feedback["rater"]
        nonce = str(body["nonce"]).lower()
        prepared = self._pending.get(nonce)
        if prepared is None or nonce in self._used:
            return httpx.Response(400, json={"error": "relay_nonce_unknown_or_used"})
        if int(body["deadline"]) != prepared.deadline or self.clock.now > prepared.deadline:
            return httpx.Response(400, json={"error": "relay_expired"})
        chain_id = RELAY_CHAIN_IDS[body["network"]]
        typed = self._typed_data(chain_id, rater, feedback, int(body["deadline"]), nonce)
        digest = bytes.fromhex(self._digest(typed)[2:])
        sig = bytes.fromhex(body["signature"][2:])
        v = sig[64] - 27 if sig[64] >= 27 else sig[64]
        recovered = keys.Signature(
            vrs=(v, int.from_bytes(sig[:32], "big"), int.from_bytes(sig[32:64], "big"))
        )
        if (
            recovered.recover_public_key_from_msg_hash(digest).to_checksum_address().lower()
            != rater.lower()
        ):
            return httpx.Response(400, json={"error": "relay_bad_signature"})
        on_chain = rater.lower() in self.chain.delegations
        if rater.lower() not in self.delegated and not on_chain:
            auth = body.get("authorization")
            if not auth or not self._check_authorization(auth, rater, chain_id):
                return httpx.Response(400, json={"error": "relay_authorization_required"})
            # An authorization executes only at the account's CURRENT nonce.
            if int(auth["nonce"]) != self.chain.sender_nonce(rater, pending=False):
                return httpx.Response(400, json={"error": "relay_authorization_nonce"})
        self._used.add(nonce)
        self.delegated.add(rater.lower())
        tx = self.chain.mine_feedback(
            rater,
            agent_id=int(feedback["agentId"]),
            value=int(feedback["value"]),
            tag1=feedback.get("tag1", ""),
            tag2=feedback.get("tag2", ""),
            endpoint=feedback.get("endpoint", ""),
            feedback_uri=feedback.get("feedbackUri", ""),
            feedback_hash=str(feedback.get("feedbackHash")),
            nonce=nonce,
            delegate=None if on_chain else RELAY_DELEGATES[chain_id],
        )
        if mode == "lost":
            return httpx.Response(504, json={"error": "gateway timeout"})
        if mode == "uncertain":
            return httpx.Response(200, json={"success": True, "network": body["network"]})
        return httpx.Response(
            200, json={"success": True, "transaction": tx, "network": body["network"]}
        )


# -- a whole environment, for the flow tests ------------------------------------------
SUBJECT = "0x2222222222222222222222222222222222222222"
OTHER_SUBJECT = "0x3333333333333333333333333333333333333333"
#: An address WITH letters, in EIP-55 mixed case: the only kind on which a
#: missing ``.lower()`` shows (``0x2222...`` has no case to get wrong).
MIXED_SUBJECT = (
    "0xABaBaBaBABabABabAbAbABAbABabababaBaBABaB"  # to_checksum_address("0x" + "ab" * 20)
)


def generous_caps(**overrides: int):
    from uvd_x402_sdk.reputation import Caps

    values = dict(
        per_subject_per_stream=100,
        per_subject_per_day=100,
        per_subject_lifetime=100,
        per_stream=100,
        per_day=100,
        lifetime=74_000,
        per_day_spend=10**9,
        max_value=95,
    )
    values.update(overrides)
    return Caps(**values)


def make_entry(
    ref_parts=("stream-1", "msg-1"),
    *,
    viewer_id="viewer-1",
    subject=SUBJECT,
    agent_id=18896,
    value=90,
    amount=1000,
    stream_id="stream-1",
    created_at=0.0,
):
    from uvd_x402_sdk.reputation import OutboxEntry, message_ref

    return OutboxEntry(
        ref=message_ref("credits-roll:v1", *ref_parts),
        viewer_id=viewer_id,
        stream_id=stream_id,
        subject=subject,
        network="base",
        agent_id=agent_id,
        value=value,
        tag1="credits_roll:idea",
        tag2="credits-roll|moment",
        endpoint="https://www.twitch.tv/somechannel",
        amount=amount,
        created_at=created_at,
    )


class Env:
    """Chain + facilitator + outbox + governor + worker, all doubles but the code under test."""

    def __init__(self, outbox, *, caps=None, health=None, excluded=None) -> None:
        from uvd_x402_sdk.reputation import (
            AccountSigner,
            CircuitBreaker,
            Governor,
            ReputationWorker,
            evidence_document,
        )

        self.clock = Clock()
        self.chain = FakeChain(self.clock)
        self.facilitator = FakeFacilitator(self.chain, self.clock)
        self.outbox = outbox
        self.account = fixed_account()
        self.signer = AccountSigner(self.account)
        self.health_calls = 0
        self.served: dict[str, bytes] = {}
        self.salt = bytes.fromhex("ab" * 32)

        def default_health():
            self.health_calls += 1
            return True

        self.breaker = CircuitBreaker(clock=self.clock)
        self.governor = Governor(
            caps or generous_caps(),
            health or default_health,
            breaker=self.breaker,
            excluded=excluded,
            clock=self.clock,
        )

        def publish(entry, proof):
            built = evidence_document(
                network=entry.network,
                agent_id=entry.agent_id,
                rater=self.account.address,
                subject=entry.subject,
                value=entry.value,
                tag1=entry.tag1,
                tag2=entry.tag2,
                endpoint=entry.endpoint,
                created_at=int(entry.created_at),
                salt=self.salt,
                private={"moment_ref": entry.ref, "message": "una idea brillante para el stream"},
                proof=proof,
                issuer={"name": "Credits Roll", "product": "credits-roll"},
            )
            # Named by the content hash: the URI is public for ever, the ref is not.
            uri = f"https://credits.example/evidence/{built.content_hash[2:]}.json"
            self.served[uri] = built.body
            return uri, built.content_hash

        self.relay = self.facilitator.client()
        self.worker = ReputationWorker(
            outbox=outbox,
            governor=self.governor,
            signer=self.signer,
            relay=self.relay,
            rpc=self.chain,
            publish_evidence=publish,
            max_amount=10_000,
            clock=self.clock,
            sleep=self.clock.sleep,
            payment_wait_seconds=10.0,
        )

    def own(self, agent_id: int = 18896, subject: str = SUBJECT) -> None:
        self.chain.owners[agent_id] = subject

    async def drive(
        self,
        ref: str,
        *,
        until=("confirmed", "failed", "local_only"),
        ticks: int = 60,
        step: float = 20.0,
    ):
        """Tick (advancing the clock) until ``ref`` reaches one of ``until``."""
        for _ in range(ticks):
            await self.worker.tick()
            row = self.outbox.get(ref)
            if row is not None and row.status in until:
                return row
            self.clock.now += step
        return self.outbox.get(ref)
