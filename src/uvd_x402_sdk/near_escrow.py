"""Arras v1: offline NEP-366 signing, in atomic units and nanoseconds.

Only registered escrow contracts are signed. Read operator scope and advertised
support from trusted facilitator ``/supported`` configuration.
No helper submits a transaction, reads an access key or allocates a nonce. Pass the
NEXT nonce and an expiry block from the selected key; persist the signed payment
before submitting it. Lifecycle callers should use a function-call key restricted
to this escrow and the methods they need (with a bounded gas allowance).

``authorize_from_budget`` is intentionally unavailable until v2 freezes its ABI.
Signing needs the ``near`` extra; message/id builders work on a base install.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Protocol, Union

from .networks.near import (
    NEAR,
    NEP366_PREFIX,
    BorshSerializer,
    serialize_delegate_action,
    serialize_signed_delegate_action,
)
from .solana_signing import Ed25519Signer, _b58decode, _b58encode

__all__ = [
    "NearLocalSigner",
    "NearEd25519Signer",
    "NearEscrowDelegateOptions",
    "NEAR_ESCROW_MIN_GAS",
    "NEAR_ESCROW_MAX_GAS",
    "NEAR_ESCROW_TOKENS",
    "NEAR_ESCROW_CONTRACTS",
    "build_escrow_msg",
    "payment_id_preimage",
    "compute_payment_id",
    "build_escrow_authorization_delegate",
    "validate_escrow_authorization_delegate",
    "sign_escrow_authorization",
    "sign_escrow_lifecycle",
    "release",
    "refund",
    "reclaim",
    "withdraw",
    "withdraw_for",
    "AuthorizeFromBudget",
    "authorize_from_budget",
]

Unsigned = Union[int, str]
NEAR_ESCROW_MIN_GAS = 38_000_000_000_000
NEAR_ESCROW_MAX_GAS = 100_000_000_000_000
NEAR_ESCROW_TOKENS: Mapping[str, str] = MappingProxyType(
    {
        "near:mainnet": NEAR.usdc_address,
        "near:testnet": "3e2210e1184b45b64c8a434c0a7e7b23cc04ea7eb7a6c3c32520d03d4afcb8af",
    }
)
# Arras v1 deployments, isolated by network.
NEAR_ESCROW_CONTRACTS: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        "near:testnet": ("arras-v1b-1101.testnet",),
        "near:mainnet": ("v1.arras-escrow.near",),
    }
)
_LIFECYCLE_GAS = {
    "release": (3_000_000_000_000, 30_000_000_000_000),
    "refund": (3_000_000_000_000, 30_000_000_000_000),
    "reclaim": (3_000_000_000_000, 30_000_000_000_000),
    "withdraw": (29_000_000_000_000, 50_000_000_000_000),
    "withdraw_for": (29_000_000_000_000, 50_000_000_000_000),
}


def _uint(value: Unsigned, bits: int, name: str) -> int:
    if type(value) is str:
        if len(value) > 39 or re.fullmatch(r"0|[1-9][0-9]*", value) is None:
            raise ValueError(f"{name}: expected an exact unsigned integer")
        value = int(value)
    if type(value) is not int or not 0 <= value < 1 << bits:
        raise ValueError(f"{name}: out of u{bits} range")
    return value


def _account(value: object, name: str) -> None:
    if (
        not isinstance(value, str)
        or not 2 <= len(value) <= 64
        or re.fullmatch(r"[a-z0-9]+(?:[._-][a-z0-9]+)*", value) is None
    ):
        raise ValueError(f"{name}: invalid NEAR account")


def _hex(value: str, size: int, name: str) -> bytes:
    if not isinstance(value, str) or re.fullmatch(rf"[0-9a-f]{{{size * 2}}}", value) is None:
        raise ValueError(f"{name}: expected {size} bytes of lowercase hex without 0x")
    return bytes.fromhex(value)


def _json(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _msg_values(p: Mapping[str, Any]) -> tuple:
    if not isinstance(p, Mapping):
        raise ValueError("msg must be a mapping")
    required = {
        "operator_id",
        "receiver",
        "release_deadline",
        "reclaim_after",
        "fee_bps_max",
        "terms_hash",
        "salt",
    }
    if not required <= p.keys():
        raise ValueError("msg is missing required fields")
    operator = p["operator_id"]
    if not isinstance(operator, str) or re.fullmatch(r"[a-z0-9_-]{1,32}", operator) is None:
        raise ValueError("operator_id: expected [a-z0-9_-]{1,32}")
    _account(p["receiver"], "receiver")
    release_at = _uint(p["release_deadline"], 64, "release_deadline")
    reclaim_at = _uint(p["reclaim_after"], 64, "reclaim_after")
    if release_at >= reclaim_at:
        raise ValueError("release_deadline must precede reclaim_after")
    if type(p["fee_bps_max"]) is not int:
        raise ValueError("fee_bps_max: expected an integer")
    fee = _uint(p["fee_bps_max"], 16, "fee_bps_max")
    return (
        release_at,
        reclaim_at,
        fee,
        _hex(p["terms_hash"], 32, "terms_hash"),
        _hex(p["salt"], 16, "salt"),
    )


def build_escrow_msg(params: Mapping[str, Any]) -> str:
    """Canonical Arras JSON. Deadlines are u64 nanoseconds, encoded as strings.

    Fields: operator_id/receiver/terms_hash/salt (str), release_deadline and
    reclaim_after (int or canonical decimal str), fee_bps_max (int, u16).
    Other mapping fields are ignored, allowing the same frozen vector for the id.
    """
    release_at, reclaim_at, fee, _, _ = _msg_values(params)
    return _json(
        {
            "v": 1,
            "action": "authorize",
            "operator_id": params["operator_id"],
            "receiver": params["receiver"],
            "release_deadline": str(release_at),
            "reclaim_after": str(reclaim_at),
            "fee_bps_max": fee,
            "terms_hash": params["terms_hash"],
            "salt": params["salt"],
        }
    )


def payment_id_preimage(params: Mapping[str, Any]) -> bytes:
    """Rust Borsh preimage; msg fields plus token/payer (str), amount (u128)."""
    release_at, reclaim_at, fee, terms_hash, salt = _msg_values(params)
    _account(params["token"], "token")
    _account(params["payer"], "payer")
    ser = BorshSerializer().write_u8(1)
    for field in ("operator_id", "token", "payer", "receiver"):
        ser.write_string(params[field])
    ser.write_u128(_uint(params["amount"], 128, "amount"))
    ser.write_u64(release_at).write_u64(reclaim_at)
    ser.write_fixed_bytes(fee.to_bytes(2, "little"))
    return ser.write_fixed_bytes(terms_hash).write_fixed_bytes(salt).get_bytes()


def compute_payment_id(params: Mapping[str, Any]) -> str:
    """base58(sha256(payment_id_preimage)); canonical ids have 32..44 characters."""
    return _b58encode(hashlib.sha256(payment_id_preimage(params)).digest())


class NearLocalSigner(Protocol):
    """Sign the NEP-366 SHA-256 digest directly, without a NEP-413 envelope."""

    @property
    def public_key(self) -> bytes:
        """Raw 32-byte Ed25519 public key."""
        ...

    def sign(self, digest: bytes) -> bytes:
        """Return the raw 64-byte Ed25519 signature."""
        ...


class NearEd25519Signer:
    """Local signer: a 32-byte seed or NEAR ed25519:base58(seed||public_key).

    The 64-byte NEAR form checks its public half. Errors never echo secret input.
    Requires ``pip install 'uvd-x402-sdk[near]'`` (cryptography).
    """

    __slots__ = ("_signer",)

    def __init__(self, secret_key: bytes | str) -> None:
        if isinstance(secret_key, str):
            try:
                if not secret_key.startswith("ed25519:") or len(secret_key) > 96:
                    raise ValueError
                raw = _b58decode(secret_key[8:])
                if len(raw) != 64:
                    raise ValueError
            except ValueError:
                raise ValueError("expected a NEAR ed25519: secret key of 64 bytes") from None
        elif isinstance(secret_key, bytes) and len(secret_key) == 32:
            raw = secret_key
        else:
            raise ValueError("expected a 32-byte seed or NEAR ed25519: secret key")
        try:
            self._signer = Ed25519Signer(raw)
        except ImportError:
            raise ImportError(
                "NearEd25519Signer needs cryptography; install the 'near' extra: "
                "pip install 'uvd-x402-sdk[near]'"
            ) from None

    @property
    def public_key(self) -> bytes:
        return _b58decode(self._signer.pubkey)

    def sign(self, digest: bytes) -> bytes:
        return self._signer.sign_message(digest)


@dataclass(frozen=True)
class NearEscrowDelegateOptions:
    """Explicit trusted network/contract and selected access-key context.

    ``nonce`` is the NEXT access-key nonce; ``max_block_height`` is a block height,
    not Unix time. Gas is integer gas units (1 TGas = 10**12), never a float.
    Use a distinct nonce for each lifecycle call; this SDK allocates none.
    """

    network: str
    signer: NearLocalSigner
    sender_id: str
    escrow_contract: str
    nonce: Unsigned
    max_block_height: Unsigned
    gas: Unsigned | None = None


def _options(options: NearEscrowDelegateOptions) -> None:
    if options.network not in NEAR_ESCROW_TOKENS:
        raise ValueError("unsupported NEAR network: use near:mainnet or near:testnet")
    _account(options.sender_id, "sender_id")
    _account(options.escrow_contract, "escrow_contract")
    if options.escrow_contract not in NEAR_ESCROW_CONTRACTS[options.network]:
        raise ValueError("escrow_contract is not registered for this network")
    nonce = _uint(options.nonce, 64, "nonce")
    height = _uint(options.max_block_height, 64, "max_block_height")
    if nonce == 0:
        raise ValueError("nonce must be positive")
    if nonce >= height * 1_000_000:
        raise ValueError("nonce must be below max_block_height * 1_000_000")


def build_escrow_authorization_delegate(
    options: NearEscrowDelegateOptions,
    *,
    token: str,
    amount: Unsigned,
    msg: Mapping[str, Any],
) -> dict:
    """Build the single FT function call. No key is used by this builder."""
    _options(options)
    if token != NEAR_ESCROW_TOKENS[options.network]:
        raise ValueError("token does not match network USDC")
    canonical_msg = build_escrow_msg(msg)
    if msg["receiver"] in (options.sender_id, options.escrow_contract):
        raise ValueError("receiver equals payer or escrow")
    n = _uint(amount, 128, "amount")
    if n == 0:
        raise ValueError("amount must be positive")
    return {
        "sender_id": options.sender_id,
        "receiver_id": token,
        "nonce": options.nonce,
        "max_block_height": options.max_block_height,
        "actions": [
            {
                "method_name": "ft_transfer_call",
                "args": _json(
                    {"receiver_id": options.escrow_contract, "amount": str(n), "msg": canonical_msg}
                ),
                "gas": NEAR_ESCROW_MAX_GAS if options.gas is None else options.gas,
                "deposit": 1,
            }
        ],
    }


def validate_escrow_authorization_delegate(
    delegate: Mapping[str, Any],
    options: NearEscrowDelegateOptions,
    *,
    token: str,
    amount: Unsigned,
    msg: Mapping[str, Any],
) -> None:
    """Bind all intent fields, exactly one action, 1 yocto and 38..100 TGas."""
    expected = build_escrow_authorization_delegate(options, token=token, amount=amount, msg=msg)
    if (
        delegate["sender_id"] != expected["sender_id"]
        or delegate["receiver_id"] != expected["receiver_id"]
        or _uint(delegate["nonce"], 64, "nonce") != _uint(expected["nonce"], 64, "nonce")
        or _uint(delegate["max_block_height"], 64, "max_block_height")
        != _uint(expected["max_block_height"], 64, "max_block_height")
    ):
        raise ValueError("delegate does not match authorization")
    if len(delegate["actions"]) != 1:
        raise ValueError("expected exactly one action")
    action = delegate["actions"][0]
    if (
        action["method_name"] != "ft_transfer_call"
        or action["args"] != expected["actions"][0]["args"]
    ):
        raise ValueError("FT method or arguments mismatch")
    if _uint(action["deposit"], 128, "deposit") != 1:
        raise ValueError("FT deposit must be 1 yocto")
    gas = _uint(action["gas"], 64, "gas")
    if not NEAR_ESCROW_MIN_GAS <= gas <= NEAR_ESCROW_MAX_GAS:
        raise ValueError("FT gas outside 38..100 TGas")


def _sign_delegate(options: NearEscrowDelegateOptions, delegate: dict) -> dict:
    # Snapshot every byte BEFORE invoking a caller-supplied signer.
    from .near_transactions import _check_signer_network

    _check_signer_network(options.signer, options.network)
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    public_key = options.signer.public_key
    if not isinstance(public_key, bytes) or len(public_key) != 32:
        raise ValueError("expected 32-byte Ed25519 public key")
    action = delegate["actions"][0]
    call = (
        BorshSerializer()
        .write_u8(2)
        .write_string(action["method_name"])
        .write_bytes(action["args"].encode("utf-8"))
        .write_u64(_uint(action["gas"], 64, "gas"))
        .write_u128(_uint(action["deposit"], 128, "deposit"))
        .get_bytes()
    )
    wire = serialize_delegate_action(
        delegate["sender_id"],
        delegate["receiver_id"],
        call,
        _uint(delegate["nonce"], 64, "nonce"),
        _uint(delegate["max_block_height"], 64, "max_block_height"),
        public_key,
    )
    digest = hashlib.sha256(NEP366_PREFIX + wire).digest()
    signature = options.signer.sign(digest)
    if not isinstance(signature, bytes) or len(signature) != 64:
        raise ValueError("invalid Ed25519 delegate signature")
    try:
        Ed25519PublicKey.from_public_bytes(public_key).verify(signature, digest)
    except InvalidSignature:
        raise ValueError("invalid Ed25519 delegate signature") from None
    signed = serialize_signed_delegate_action(wire, signature)
    return {
        "x402Version": 2,
        "scheme": "escrow",
        "network": options.network,
        "payload": {"signedDelegateAction": base64.b64encode(signed).decode("ascii")},
    }


def sign_escrow_authorization(
    options: NearEscrowDelegateOptions,
    *,
    token: str,
    amount: Unsigned,
    msg: Mapping[str, Any],
) -> dict:
    """Return x402 payment {x402Version:2, scheme:escrow, network, payload}.

    Pair with requirements.extra.nearEscrow = {contract, operatorId, msg,
    paymentId}; msg is build_escrow_msg(msg), paymentId is compute_payment_id
    over those terms plus token, payer=options.sender_id, amount.
    """
    delegate = build_escrow_authorization_delegate(options, token=token, amount=amount, msg=msg)
    validate_escrow_authorization_delegate(delegate, options, token=token, amount=amount, msg=msg)
    return _sign_delegate(options, delegate)


def sign_escrow_lifecycle(
    options: NearEscrowDelegateOptions,
    method: str,
    args: Mapping[str, Any] | None = None,
) -> dict:
    """Sign one allowed v1 method with deposit zero and its gas floor/cap."""
    _options(options)
    if method not in _LIFECYCLE_GAS:
        raise ValueError("escrow method not allowed")
    args = {} if args is None else args
    if not isinstance(args, Mapping):
        raise ValueError("lifecycle args must be a mapping")
    minimum, maximum = _LIFECYCLE_GAS[method]
    gas = _uint(maximum if options.gas is None else options.gas, 64, "gas")
    if not minimum <= gas <= maximum:
        raise ValueError("lifecycle gas outside method limit")
    allowed = (
        {"payment_id", "amount"}
        if method == "release"
        else (
            set()
            if method == "withdraw"
            else {"account"} if method == "withdraw_for" else {"payment_id"}
        )
    )
    if not args.keys() <= allowed:
        raise ValueError("unexpected lifecycle argument")
    params: dict = {}
    if method == "withdraw_for":
        _account(args.get("account"), "account")
        params["account"] = args["account"]
    elif method != "withdraw":
        payment_id = args.get("payment_id")
        try:
            if not isinstance(payment_id, str) or not 32 <= len(payment_id) <= 44:
                raise ValueError
            raw = _b58decode(payment_id)
            if len(raw) != 32 or _b58encode(raw) != payment_id:
                raise ValueError
        except ValueError:
            raise ValueError("invalid payment_id") from None
        params["payment_id"] = payment_id
        if method == "release":
            amount = args.get("amount")
            if amount is not None and _uint(amount, 128, "amount") == 0:
                raise ValueError("release amount must be positive")
            params["amount"] = None if amount is None else str(_uint(amount, 128, "amount"))
    delegate = {
        "sender_id": options.sender_id,
        "receiver_id": options.escrow_contract,
        "nonce": options.nonce,
        "max_block_height": options.max_block_height,
        "actions": [{"method_name": method, "args": _json(params), "gas": gas, "deposit": 0}],
    }
    return _sign_delegate(options, delegate)


def release(
    options: NearEscrowDelegateOptions, payment_id: str, amount: Unsigned | None = None
) -> dict:
    """Credit receiver/fee claimables; no token is transferred yet."""
    return sign_escrow_lifecycle(options, "release", {"payment_id": payment_id, "amount": amount})


def refund(options: NearEscrowDelegateOptions, payment_id: str) -> dict:
    """Credit the payer with the remaining locked amount."""
    return sign_escrow_lifecycle(options, "refund", {"payment_id": payment_id})


def reclaim(options: NearEscrowDelegateOptions, payment_id: str) -> dict:
    """Payer reclaims after reclaim_after; the chain checks the deadline."""
    return sign_escrow_lifecycle(options, "reclaim", {"payment_id": payment_id})


def withdraw(options: NearEscrowDelegateOptions) -> dict:
    """Withdraw the sender's claimable USDC. Arras v1 has no token argument."""
    return sign_escrow_lifecycle(options, "withdraw")


def withdraw_for(options: NearEscrowDelegateOptions, account: str) -> dict:
    """Withdraw to the named account, which may also be the caller."""
    return sign_escrow_lifecycle(options, "withdraw_for", {"account": account})


class AuthorizeFromBudget(Protocol):
    """v2 adapter boundary. Wire format and authorization policy are not frozen."""

    def authorize_from_budget(self, request: Mapping[str, Any]) -> dict: ...


def authorize_from_budget(options: NearEscrowDelegateOptions, request: Mapping[str, Any]) -> dict:
    """Reserved v2 entry point. Always raises before accessing the signer."""
    raise NotImplementedError("Arras v2 budget ABI and golden vectors are not frozen")
