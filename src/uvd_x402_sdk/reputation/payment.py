"""The anchor payment of a rating, and its proof.

The facilitator accepts a rating's ``proof`` only when (x402-rs
``src/erc8004/proof.rs::verify_proof_of_payment``): the payer IS the rater,
the payee owns the agent, the transaction succeeded in the declared block with
an exact ``Transfer`` of a known token, it is at most 7 days old, and its
``paymentHash`` recomputes. It refuses a proof already used for the agent only
while it enforces proofs (``ERC8004_REQUIRE_PROOF``, off by default:
``proof.rs::is_proof_required``); the worker refuses it itself (one anchor
transaction, one row per agent). So each rating carries its own
micro-transfer from the rater to the subject.

:func:`pay_and_prove` makes that transfer and returns the
:class:`~uvd_x402_sdk.erc8004.ProofOfPayment` built from the CHAIN (the
timestamp of the block, not a wall clock; the ``paymentHash`` recomputed here
exactly as the facilitator does). The signed transaction is handed to
``on_signed`` (required: persist it there) BEFORE it is broadcast, and a retry
passes it back as ``signed=``: the same bytes are re-sent, never a new
transaction. The
only way that same payment is ever replaced is a nonce the chain has already
consumed with something else, and then it is refused, not re-signed.

:func:`prove_payment` is the verification half alone, ported from Karma
Hello's ``live_worker.verify_payment``: receipt status, block, an exact
``Transfer``, confirmations, the block hash read back.

The RPC is anything with ``async request(method, params)``;
:class:`HttpJsonRpc` is a minimal one that only speaks the read methods plus
``eth_sendRawTransaction`` and never puts its URL in an error.
"""

from __future__ import annotations

import asyncio
import inspect
import math
import re
import time
from collections.abc import Awaitable
from dataclasses import dataclass
from typing import Any, Callable, Optional, Union

try:
    from typing import Protocol, runtime_checkable
except ImportError:  # pragma: no cover
    from typing_extensions import Protocol, runtime_checkable  # type: ignore[assignment]

from uvd_x402_sdk.bounded_json import loads_bounded
from uvd_x402_sdk.erc8004 import ProofOfPayment, _wire
from uvd_x402_sdk.reputation._errors import PaymentPending, PaymentRejected
from uvd_x402_sdk.reputation._redact import log_event, redact, redact_http_client_logs
from uvd_x402_sdk.reputation.preparation import ADDRESS_RE, BYTES32_RE, checksum

TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
_TRANSFER_SELECTOR = "a9059cbb"  # transfer(address,uint256)
_ZERO_ADDRESS = "0x" + "0" * 40
#: EIP-7702 delegation designator: an EOA with this code is still an EOA.
_DELEGATION_PREFIX = "0xef0100"
_CODE_RE = re.compile(r"0x[0-9a-fA-F]*")

READ_METHODS = frozenset(
    {
        "eth_chainId",
        "eth_blockNumber",
        "eth_call",
        "eth_estimateGas",
        "eth_gasPrice",
        "eth_maxPriorityFeePerGas",
        "eth_getBalance",
        "eth_getBlockByNumber",
        "eth_getCode",
        "eth_getLogs",
        "eth_getTransactionByHash",
        "eth_getTransactionCount",
        "eth_getTransactionReceipt",
    }
)
WRITE_METHODS = frozenset({"eth_sendRawTransaction"})
#: Ceiling on one JSON-RPC answer (an ``eth_getLogs`` window included). The
#: depth is bounded too (:func:`~uvd_x402_sdk.bounded_json.loads_bounded`).
MAX_RPC_RESPONSE_BYTES = 16 * 1024 * 1024


class RpcError(RuntimeError):
    """A JSON-RPC call failed. ``code`` is stable; the URL is never in it."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(code)


@runtime_checkable
class JsonRpc(Protocol):
    async def request(self, method: str, params: list) -> Any:
        """The ``result`` of the call; :class:`RpcError` on any failure."""
        ...


class HttpJsonRpc:
    """JSON-RPC over HTTP with a fixed method allowlist.

    429 is retried with exponential backoff (up to ``retries``); anything else
    raises :class:`RpcError` whose message is a code, never the URL (an RPC
    URL may carry an API key, even in the host).
    """

    def __init__(
        self,
        url: str,
        *,
        timeout: float = 20.0,
        retries: int = 3,
        client: Any = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.__url = url
        redact_http_client_logs()  # httpx would log this URL, key included
        self._timeout = timeout
        self._retries = retries
        self._client = client
        self._sleep = sleep

    def __repr__(self) -> str:
        return "<HttpJsonRpc>"

    async def request(self, method: str, params: list) -> Any:
        if method not in READ_METHODS and method not in WRITE_METHODS:
            raise RpcError("rpc_method_not_allowed", method)
        import httpx

        body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        client = self._client or httpx.AsyncClient(timeout=self._timeout)
        try:
            for attempt in range(self._retries + 1):
                try:
                    response = await client.post(self.__url, json=body)
                except httpx.HTTPError:
                    raise RpcError("rpc_transport_failed") from None
                if response.status_code != 429 or attempt == self._retries:
                    break
                await self._sleep(2.0**attempt)
            if response.status_code >= 400:
                raise RpcError(f"rpc_http_{response.status_code}")
            try:
                # A third party's JSON: size and nesting bounded before parsing.
                payload = loads_bounded(response.content, max_bytes=MAX_RPC_RESPONSE_BYTES)
            except ValueError:
                raise RpcError("rpc_unreadable") from None
        finally:
            if self._client is None:
                await client.aclose()
        if not isinstance(payload, dict):
            raise RpcError("rpc_unreadable")
        if payload.get("error") is not None:
            error = payload["error"]
            message = error.get("message", "") if isinstance(error, dict) else str(error)
            raise RpcError("rpc_error", str(message)[:200])
        return payload.get("result")


def _hex_int(value: Any, where: str) -> int:
    if isinstance(value, str) and value[:2].lower() == "0x":
        try:
            return int(value, 16)
        except ValueError:
            pass
    raise PaymentPending("rpc_unreadable", f"{where} is not a hex quantity")


def _topic(address: str) -> str:
    return "0x" + address.lower()[2:].rjust(64, "0")


def payment_hash(
    transaction_hash: str, block_number: int, payer: str, payee: str, amount: int
) -> str:
    """The proof's ``paymentHash``, exactly as the facilitator recomputes it.

    ``keccak(tx_hash || u64_be(block) || utf8(payer) || utf8(payee) ||
    u256_be(amount))`` with the addresses in EIP-55 form (x402-rs
    ``src/erc8004/types.rs::compute_payment_hash``; alloy's ``Address`` displays
    checksummed). Pinned against the facilitator's own vector.
    """
    from eth_utils import keccak

    if not isinstance(transaction_hash, str) or not BYTES32_RE.fullmatch(transaction_hash):
        raise ValueError("payment_hash: transaction_hash must be 0x + 64 hex")
    if isinstance(block_number, bool) or not 0 <= block_number < 2**64:
        raise ValueError("payment_hash: block_number must be a uint64")
    if isinstance(amount, bool) or not isinstance(amount, int) or not 0 <= amount < 2**256:
        raise ValueError("payment_hash: amount must be a uint256")
    data = (
        bytes.fromhex(transaction_hash[2:])
        + block_number.to_bytes(8, "big")
        + checksum(payer).encode("utf-8")
        + checksum(payee).encode("utf-8")
        + amount.to_bytes(32, "big")
    )
    return "0x" + bytes(keccak(data)).hex()


def recompute_payment_hash(proof: ProofOfPayment) -> str:
    """``payment_hash`` of a proof's own fields (compare with ``proof.payment_hash``)."""
    return payment_hash(
        proof.transaction_hash, proof.block_number, proof.payer, proof.payee, int(proof.amount)
    )


def _usdc(network: str) -> str:
    from uvd_x402_sdk.networks import get_network

    config = get_network(_wire(network))
    address = getattr(config, "usdc_address", None) if config else None
    if not address or not ADDRESS_RE.fullmatch(str(address)):
        raise PaymentRejected("unknown_network", f"no USDC registered for {network!r}")
    return checksum(str(address))


def _chain_id(network: str) -> int:
    from uvd_x402_sdk.networks import get_network

    config = get_network(_wire(network))
    chain_id = getattr(config, "chain_id", None) if config else None
    if not isinstance(chain_id, int) or chain_id <= 0:
        raise PaymentRejected("unknown_network", f"no chain id registered for {network!r}")
    return chain_id


async def _rpc(rpc: Any, method: str, params: list) -> Any:
    try:
        return await rpc.request(method, params)
    except PaymentPending:
        raise
    except RpcError as exc:
        raise PaymentPending("rpc_unavailable", exc.code) from None
    except Exception as exc:  # noqa: BLE001 - an unreadable chain is no verdict
        raise PaymentPending("rpc_unavailable", type(exc).__name__) from None


async def _check_chain(rpc: Any, network: str) -> int:
    expected = _chain_id(network)
    actual = _hex_int(await _rpc(rpc, "eth_chainId", []), "eth_chainId")
    if actual != expected:
        raise PaymentRejected("chain_mismatch", f"the RPC serves chain {actual}, not {expected}")
    return expected


async def prove_payment(
    rpc: Any,
    *,
    network: str,
    transaction_hash: str,
    payer: str,
    payee: str,
    amount: int,
    confirmations: int = 2,
) -> ProofOfPayment:
    """Build the proof of an EXISTING transfer from what the chain says.

    Raises :class:`PaymentPending` while there is no verdict (no receipt yet,
    not enough confirmations, an unreadable RPC, a block hash that moved under
    a reorg) and :class:`PaymentRejected` for a definite no (reverted, no exact
    ``Transfer`` of the network's USDC from ``payer`` to ``payee`` for
    ``amount``).
    """
    if not isinstance(transaction_hash, str) or not BYTES32_RE.fullmatch(transaction_hash):
        raise PaymentRejected("payment_shape", "transaction_hash must be 0x + 64 hex")
    if isinstance(amount, bool) or not isinstance(amount, int) or amount <= 0:
        raise PaymentRejected("payment_amount", "amount must be a positive int of atomic units")
    payer, payee = checksum(payer), checksum(payee)
    token = _usdc(network)
    await _check_chain(rpc, network)
    receipt = await _rpc(rpc, "eth_getTransactionReceipt", [transaction_hash])
    if not receipt:
        raise PaymentPending("payment_pending", "no receipt yet")
    if str(receipt.get("transactionHash", "")).lower() != transaction_hash.lower():
        raise PaymentPending("rpc_unreadable", "the receipt is for another transaction")
    if _hex_int(receipt.get("status"), "status") != 1:
        raise PaymentRejected("payment_reverted", "the payment reverted")
    block = _hex_int(receipt.get("blockNumber"), "blockNumber")
    height = _hex_int(await _rpc(rpc, "eth_blockNumber", []), "eth_blockNumber")
    if height - block + 1 < confirmations:
        raise PaymentPending(
            "payment_awaiting_confirmations", f"{height - block + 1} of {confirmations}"
        )
    expected_topics = [TRANSFER_TOPIC, _topic(payer), _topic(payee)]
    matches = [
        log
        for log in receipt.get("logs") or []
        if not log.get("removed")
        and str(log.get("address", "")).lower() == token.lower()
        and [str(t).lower() for t in log.get("topics") or []] == expected_topics
        and _hex_int(log.get("data"), "log.data") == amount
    ]
    if len(matches) != 1:
        raise PaymentRejected("payment_transfer_missing", "no single exact Transfer in the receipt")
    header = await _rpc(rpc, "eth_getBlockByNumber", [receipt["blockNumber"], False])
    if (
        not header
        or str(header.get("hash", "")).lower() != str(receipt.get("blockHash", "")).lower()
    ):
        raise PaymentPending("payment_reorg", "the block hash moved; read again")
    return ProofOfPayment(
        transaction_hash=transaction_hash.lower(),
        block_number=block,
        network=_wire(network),
        payer=payer,
        payee=payee,
        amount=str(amount),
        token=token,
        timestamp=_hex_int(header.get("timestamp"), "timestamp"),
        payment_hash=payment_hash(transaction_hash.lower(), block, payer, payee, amount),
    )


@dataclass(frozen=True)
class GasPolicy:
    """Ceilings on what one anchor transfer may cost. Parameters, not constants.

    ``max_gas`` bounds the gas limit (an ERC-20 transfer is ~35k-65k);
    ``max_total_fee_wei`` bounds ``gas * maxFeePerGas``. Above either, nothing
    is signed: a spiking base fee defers the payment (:class:`PaymentPending`)
    rather than paying it.
    """

    max_gas: int = 120_000
    max_total_fee_wei: int = 10**14
    gas_multiplier_percent: int = 120


@dataclass(frozen=True, repr=False)
class SignedPayment:
    """A signed anchor transfer. Persist it before broadcasting it."""

    network: str
    payer: str
    payee: str
    token: str
    amount: int
    nonce: int
    raw: str
    transaction_hash: str

    def __repr__(self) -> str:
        return f"SignedPayment(tx={redact(self.transaction_hash)}, nonce={self.nonce})"

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SignedPayment:
        return cls(**{key: data[key] for key in cls.__dataclass_fields__})


def _transfer_calldata(payee: str, amount: int) -> str:
    return "0x" + _TRANSFER_SELECTOR + payee.lower()[2:].rjust(64, "0") + format(amount, "064x")


def _check_signed_transaction(raw: str, payer: str, tx: dict) -> str:
    """Decode what the signer returned: it must be OUR transaction, from the payer."""
    from eth_account import Account
    from eth_account.typed_transactions import TypedTransaction
    from eth_utils import keccak
    from hexbytes import HexBytes

    try:
        data = bytes.fromhex(raw[2:] if raw[:2].lower() == "0x" else raw)
        decoded = TypedTransaction.from_bytes(HexBytes(data)).as_dict()
        sender = Account.recover_transaction(data)
    except Exception:  # noqa: BLE001 - anything unreadable is not our transaction
        raise PaymentRejected(
            "payment_signature", "the signed transaction does not decode"
        ) from None
    if str(sender).lower() != payer.lower():
        raise PaymentRejected("payment_signature", "the transaction is not signed by the payer")
    to_field = decoded.get("to")
    to_hex = (
        "0x" + bytes(to_field).hex() if isinstance(to_field, (bytes, bytearray)) else str(to_field)
    )
    data_field = decoded.get("data")
    data_hex = (
        "0x" + bytes(data_field).hex()
        if isinstance(data_field, (bytes, bytearray))
        else str(data_field)
    )
    to_hex = to_hex if to_hex[:2].lower() == "0x" else "0x" + to_hex
    data_hex = data_hex if data_hex[:2].lower() == "0x" else "0x" + data_hex
    if (
        int(decoded.get("type", -1)) != 2
        or int(decoded.get("chainId", -1)) != tx["chainId"]
        or int(decoded.get("nonce", -1)) != tx["nonce"]
        or to_hex.lower() != tx["to"].lower()
        or data_hex.lower() != tx["data"].lower()
        or int(decoded.get("value", -1)) != 0
        or int(decoded.get("gas", -1)) != tx["gas"]
        or int(decoded.get("maxFeePerGas", -1)) != tx["maxFeePerGas"]
        or int(decoded.get("maxPriorityFeePerGas", -1)) != tx["maxPriorityFeePerGas"]
        or tuple(decoded.get("accessList") or ()) != ()
    ):
        raise PaymentRejected("payment_signature", "the signer returned another transaction")
    return "0x" + bytes(keccak(data)).hex()


async def sign_payment(
    signer: Any,
    payee: str,
    amount: int,
    rpc: Any,
    *,
    network: str = "base",
    max_amount: int,
    gas: GasPolicy = GasPolicy(),
) -> SignedPayment:
    """Build and sign the USDC transfer ``rater -> payee`` without sending it."""
    from uvd_x402_sdk.reputation.signing import signer_address

    if isinstance(amount, bool) or not isinstance(amount, int) or amount <= 0:
        raise PaymentRejected("payment_amount", "amount must be a positive int of atomic units")
    if isinstance(max_amount, bool) or not isinstance(max_amount, int) or amount > max_amount:
        raise PaymentRejected("payment_amount", "amount above max_amount")
    if (
        not isinstance(payee, str)
        or not ADDRESS_RE.fullmatch(payee)
        or payee.lower() == _ZERO_ADDRESS
    ):
        raise PaymentRejected("payee_invalid", "the payee is not an address")
    payer = signer_address(signer)
    payee = checksum(payee)
    if payee.lower() == payer.lower():
        raise PaymentRejected("payee_is_payer", "a rater does not pay (or rate) itself")
    sign_transaction = getattr(signer, "sign_transaction", None)
    if not callable(sign_transaction):
        raise PaymentRejected("payment_unavailable", "the signer cannot sign a transaction")
    token = _usdc(network)
    chain_id = await _check_chain(rpc, network)
    code = await _rpc(rpc, "eth_getCode", [payee, "latest"])
    if not isinstance(code, str) or not _CODE_RE.fullmatch(code):
        # A null (one node of a balanced RPC) is no answer, never "no code".
        raise PaymentPending("rpc_unreadable", "eth_getCode is not hex")
    code = code.lower()
    if code not in ("0x", "0x0") and not (code.startswith(_DELEGATION_PREFIX) and len(code) == 48):
        raise PaymentRejected("payee_is_contract", "funds are never sent to a contract")
    data = _transfer_calldata(payee, amount)
    estimate = _hex_int(
        await _rpc(rpc, "eth_estimateGas", [{"from": payer, "to": token, "data": data}]),
        "eth_estimateGas",
    )
    gas_limit = math.ceil(estimate * gas.gas_multiplier_percent / 100)
    if gas_limit > gas.max_gas:
        raise PaymentRejected("gas_ceiling", f"gas {gas_limit} above {gas.max_gas}")
    latest = await _rpc(rpc, "eth_getBlockByNumber", ["latest", False])
    base_fee = _hex_int((latest or {}).get("baseFeePerGas"), "baseFeePerGas")
    priority = _hex_int(await _rpc(rpc, "eth_maxPriorityFeePerGas", []), "priority fee")
    max_fee = 2 * base_fee + priority
    if gas_limit * max_fee > gas.max_total_fee_wei:
        raise PaymentPending("gas_too_expensive", f"{gas_limit * max_fee} wei")
    nonce = _hex_int(
        await _rpc(rpc, "eth_getTransactionCount", [payer, "pending"]), "eth_getTransactionCount"
    )
    tx = {
        "type": 2,
        "chainId": chain_id,
        "nonce": nonce,
        "to": token,
        "value": 0,
        "data": data,
        "gas": gas_limit,
        "maxFeePerGas": max_fee,
        "maxPriorityFeePerGas": priority,
    }
    raw = sign_transaction(dict(tx))
    raw = raw if isinstance(raw, str) else "0x" + bytes(raw).hex()
    raw = raw if raw[:2].lower() == "0x" else "0x" + raw
    tx_hash = _check_signed_transaction(raw, payer, tx)
    return SignedPayment(
        network=_wire(network),
        payer=payer,
        payee=payee,
        token=token,
        amount=amount,
        nonce=nonce,
        raw=raw,
        transaction_hash=tx_hash,
    )


#: Answers to a re-send that are not a failure of THIS send: the bytes are
#: already there, or their nonce is already used, which the proof loop then
#: resolves (mined = ours, a receipt; consumed by another = never minable).
_ALREADY_KNOWN = (
    "already known",
    "known transaction",
    "already imported",
    "alreadyknown",
    "nonce too low",
)


async def broadcast(rpc: Any, signed: SignedPayment) -> None:
    """Send the signed bytes. Re-sending the same bytes is always safe."""
    try:
        await rpc.request("eth_sendRawTransaction", [signed.raw])
    except Exception as exc:  # noqa: BLE001 - classified below, never re-signed
        detail = str(getattr(exc, "detail", "") or exc).lower()
        if any(word in detail for word in _ALREADY_KNOWN):
            return
        known = None
        try:
            known = await rpc.request("eth_getTransactionByHash", [signed.transaction_hash])
        except Exception:  # noqa: BLE001
            known = None
        if known:
            return
        raise PaymentPending("broadcast_failed", type(exc).__name__) from None


async def _maybe_await(value: Any) -> None:
    if inspect.isawaitable(value):
        await value


async def pay_and_prove(
    signer: Any,
    payee: str,
    amount: int,
    rpc: Any,
    *,
    network: str = "base",
    max_amount: int,
    confirmations: int = 2,
    gas: GasPolicy = GasPolicy(),
    signed: Optional[Union[SignedPayment, dict]] = None,
    on_signed: Optional[Callable[[SignedPayment], Any]] = None,
    wait_seconds: float = 60.0,
    poll_seconds: float = 2.0,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> ProofOfPayment:
    """Pay ``amount`` (atomic USDC units) from the rater to ``payee``; prove it.

    Without ``signed`` a new transfer is signed, handed to ``on_signed`` (which
    must persist it, and may raise to stop: then nothing is sent) and only then
    broadcast. ``on_signed`` is REQUIRED then (``TypeError`` before anything is
    signed): without the persisted bytes a retry could only sign a second
    transfer. With ``signed`` (what ``on_signed`` persisted) the SAME bytes are
    re-sent: a retry never signs a second payment.

    Returns the proof once confirmed. Raises :class:`PaymentPending` when the
    wait ends without a verdict (call again with ``signed=``) and
    :class:`PaymentRejected` for a definite no, including
    ``payment_nonce_consumed``: the chain used the transfer's nonce for
    something else, so this transfer can never be mined.
    """
    if signed is None:
        if on_signed is None:
            raise TypeError("pay_and_prove: on_signed is required to sign a new transfer")
        payment = await sign_payment(
            signer, payee, amount, rpc, network=network, max_amount=max_amount, gas=gas
        )
        await _maybe_await(on_signed(payment))
        log_event(20, "reputation.payment_signed", tx=payment.transaction_hash, payee=payment.payee)
    else:
        payment = signed if isinstance(signed, SignedPayment) else SignedPayment.from_dict(signed)
        if (
            payment.payee.lower() != str(payee).lower()
            or payment.amount != amount
            or payment.network != _wire(network)
        ):
            raise PaymentRejected("payment_mismatch", "the persisted payment is for another rating")
    await broadcast(rpc, payment)
    deadline = clock() + wait_seconds
    suspicions = 0
    while True:
        try:
            return await prove_payment(
                rpc,
                network=network,
                transaction_hash=payment.transaction_hash,
                payer=payment.payer,
                payee=payment.payee,
                amount=payment.amount,
                confirmations=confirmations,
            )
        except PaymentPending as pending:
            if pending.code == "payment_pending":
                consumed = await _nonce_consumed(rpc, payment, max(confirmations, 3))
                suspicions = suspicions + 1 if consumed else 0
                if suspicions >= NONCE_CONSUMED_READS:
                    raise PaymentRejected(
                        "payment_nonce_consumed", "the nonce was used by another transaction"
                    ) from None
            if clock() >= deadline:
                raise
        await sleep(poll_seconds)


#: Consecutive reads that must all show the nonce consumed by another
#: transaction before the payment is declared unminable. A load-balanced RPC
#: can answer the nonce from a node ahead and the receipt from one behind: a
#: single read is not a verdict.
NONCE_CONSUMED_READS = 5


async def _nonce_consumed(rpc: Any, payment: SignedPayment, depth: int) -> bool:
    """Is the payment's nonce used, ``depth`` blocks deep, by something else?

    True only if the account's nonce at ``latest - depth`` is past the
    payment's, and neither the receipt nor ``eth_getTransactionByHash`` shows
    the payment mined. Any unreadable answer is False (no verdict).
    """
    try:
        height = _hex_int(await _rpc(rpc, "eth_blockNumber", []), "eth_blockNumber")
        mined = _hex_int(
            await _rpc(
                rpc, "eth_getTransactionCount", [payment.payer, hex(max(0, height - depth))]
            ),
            "eth_getTransactionCount",
        )
        if mined <= payment.nonce:
            return False
        known = await _rpc(rpc, "eth_getTransactionByHash", [payment.transaction_hash])
        if isinstance(known, dict) and known.get("blockNumber"):
            return False  # ours, mined: the receipt is just late
        return not await _rpc(rpc, "eth_getTransactionReceipt", [payment.transaction_hash])
    except PaymentPending:
        return False
