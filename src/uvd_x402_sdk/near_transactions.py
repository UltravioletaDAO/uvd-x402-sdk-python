"""NEAR V0 signing and broadcasting of already signed, journalled bytes."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import math

import httpx

from .bounded_json import loads_bounded
from .near_escrow import NearLocalSigner, _json, _uint
from .networks.near import BorshSerializer
from .solana_signing import _b58decode, _b58encode


def _check_signer_network(signer: NearLocalSigner, network: str) -> None:
    if getattr(signer, "network", network) != network:
        raise ValueError("signer network mismatch")


def _sign_function_call_transaction(tx, signer, nonce, block_hash, network):
    """Internal: tx has already passed its builder's complete validation."""
    _check_signer_network(signer, network)
    n = _uint(nonce, 64, "nonce")
    if n == 0:
        raise ValueError("nonce must be positive")
    if not isinstance(block_hash, str) or not 32 <= len(block_hash) <= 44:
        raise ValueError("invalid block hash")
    block = _b58decode(block_hash)
    if len(block) != 32:
        raise ValueError("block hash must decode to 32 bytes")
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    params = tx["actions"][0]["params"]
    public_key = signer.public_key
    if not isinstance(public_key, bytes) or len(public_key) != 32:
        raise ValueError("invalid Ed25519 public key")
    wire = (
        BorshSerializer()
        .write_string(tx["signerId"])
        .write_u8(0)
        .write_fixed_bytes(public_key)
        .write_u64(n)
        .write_string(tx["receiverId"])
        .write_fixed_bytes(block)
        .write_u32(1)
        .write_u8(2)
        .write_string(params["methodName"])
        .write_bytes(_json(params["args"]).encode())
        .write_u64(int(params["gas"]))
        .write_u128(int(params["deposit"]))
        .get_bytes()
    )
    digest = hashlib.sha256(wire).digest()
    sign_transaction = getattr(signer, "sign_transaction", None)
    signature = sign_transaction(wire) if sign_transaction is not None else signer.sign(digest)
    if not isinstance(signature, bytes) or len(signature) != 64:
        raise ValueError("invalid Ed25519 signature")
    try:
        Ed25519PublicKey.from_public_bytes(public_key).verify(signature, digest)
    except InvalidSignature:
        raise ValueError("invalid Ed25519 signature") from None
    return base64.b64encode(wire + b"\x00" + signature).decode("ascii")


class NearTransactionError(RuntimeError):
    """Broadcast refused or unresolved. Reconcile this hash; never re-sign on error."""

    def __init__(self, reason: str, transaction_hash: str, *, uncertain: bool):
        super().__init__(reason)
        self.transaction_hash = transaction_hash
        self.uncertain = uncertain


def _final_status(status) -> bool:
    if not isinstance(status, dict):
        return False
    if set(status) == {"Failure"}:
        return isinstance(status["Failure"], dict) and bool(status["Failure"])
    if set(status) != {"SuccessValue"} or not isinstance(status["SuccessValue"], str):
        return False
    try:
        raw = base64.b64decode(status["SuccessValue"], validate=True)
    except (ValueError, UnicodeError):
        return False
    return base64.b64encode(raw).decode("ascii") == status["SuccessValue"]


async def _post(http: httpx.AsyncClient, rpc_url: str, content: bytes, timeout: float):
    """One POST of the journalled content; only a 200's body is read (and decoded).

    A 429/5xx is judged by its status alone, so a body that cannot be decoded
    does not stop the retry its status allows.
    """
    request = http.build_request(
        "POST",
        rpc_url,
        content=content,
        headers={"Content-Type": "application/json"},
        timeout=timeout,
    )
    response = await http.send(request, stream=True)
    try:
        if response.status_code == 200:
            await response.aread()
    finally:
        await response.aclose()
    return response


async def send_near_transaction(
    signed_transaction: str,
    *,
    rpc_url: str,
    client: httpx.AsyncClient | None = None,
    max_attempts: int = 3,
    timeout: float = 30.0,
    retry_delay: float = 1.0,
) -> dict:
    """broadcast_tx_commit with bounded retries of EXACTLY the same signed bytes.

    Persist ``signed_transaction`` in the caller's journal BEFORE this call. On
    restart, pass those same bytes again. This function has no signer, nonce or
    block-hash provider and cannot rebuild a transaction. Transport failures,
    HTTP 429/5xx and RPC TIMEOUT_ERROR may be retried; other RPC errors are not.
    An unresolved result raises NearTransactionError with ``uncertain=True``,
    including any other httpx error once the request may have left (an answer
    that cannot be decoded, a redirect loop): never a bare httpx exception.
    A final on-chain Failure is returned as an outcome, never retried. The caller
    must inspect status and receipts; final inclusion does not imply success.
    """
    if not isinstance(signed_transaction, str):
        raise ValueError("expected a base64 SignedTransaction")
    try:
        raw = base64.b64decode(signed_transaction, validate=True)
    except (ValueError, UnicodeError):
        raise ValueError("expected a base64 SignedTransaction") from None
    if (
        len(raw) <= 65
        or raw[-65] != 0
        or base64.b64encode(raw).decode("ascii") != signed_transaction
    ):
        raise ValueError("expected a canonical Ed25519 SignedTransaction")
    if type(max_attempts) is not int or not 1 <= max_attempts <= 3:
        raise ValueError("max_attempts must be 1..3")
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or not math.isfinite(timeout)
        or not 0 < timeout <= 120
    ):
        raise ValueError("timeout must be in (0, 120]")
    if (
        isinstance(retry_delay, bool)
        or not isinstance(retry_delay, (int, float))
        or not math.isfinite(retry_delay)
        or not 0 <= retry_delay <= 5
    ):
        raise ValueError("retry_delay must be in [0, 5]")
    tx_hash = _b58encode(hashlib.sha256(raw[:-65]).digest())
    # Immutable content is built once, before ANY await or retry.
    content = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": "near-broadcast",
            "method": "broadcast_tx_commit",
            "params": [signed_transaction],
        },
        separators=(",", ":"),
    ).encode()
    owned = client is None
    http = httpx.AsyncClient() if owned else client
    try:
        for attempt in range(max_attempts):
            try:
                response = await asyncio.wait_for(
                    _post(http, rpc_url, content, timeout), timeout=timeout
                )
            except (httpx.TransportError, asyncio.TimeoutError):
                response = None
            except httpx.HTTPError:
                # Sent, and something answered that cannot be used: a 200 body that
                # does not decode, a redirect loop, a client hook's HTTPStatusError.
                # The RPC may hold the bytes: reconcile this hash, never re-sign.
                raise NearTransactionError(
                    "unreadable RPC response", tx_hash, uncertain=True
                ) from None
            retryable = (
                response is None or response.status_code == 429 or (response.status_code >= 500)
            )
            if not retryable:
                if response.status_code != 200:
                    raise NearTransactionError("RPC HTTP refusal", tx_hash, uncertain=True)
                try:
                    body = loads_bounded(response.content)
                except ValueError:
                    raise NearTransactionError(
                        "unreadable RPC response", tx_hash, uncertain=True
                    ) from None
                if (
                    not isinstance(body, dict)
                    or body.get("id") != "near-broadcast"
                    or body.get("jsonrpc") != "2.0"
                    or (("result" in body) == ("error" in body))
                ):
                    raise NearTransactionError("invalid RPC response", tx_hash, uncertain=True)
                if "error" in body:
                    error = body["error"]
                    cause = error.get("cause") if isinstance(error, dict) else None
                    retryable = isinstance(cause, dict) and cause.get("name") == "TIMEOUT_ERROR"
                    if not retryable:
                        # A replay can report InvalidNonce after the first request landed.
                        raise NearTransactionError(
                            "RPC transaction refusal", tx_hash, uncertain=True
                        )
                else:
                    result = body.get("result")
                    tx = result.get("transaction") if isinstance(result, dict) else None
                    status = result.get("status") if isinstance(result, dict) else None
                    if (
                        not isinstance(tx, dict)
                        or tx.get("hash") != tx_hash
                        or not _final_status(status)
                    ):
                        raise NearTransactionError(
                            "unconfirmed RPC outcome", tx_hash, uncertain=True
                        )
                    return result
            if attempt + 1 < max_attempts:
                await asyncio.sleep(retry_delay)
        raise NearTransactionError("broadcast unresolved", tx_hash, uncertain=True)
    finally:
        if owned:
            await http.aclose()
