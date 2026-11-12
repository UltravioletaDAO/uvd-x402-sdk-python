"""Arras v1 and v2: offline NEP-366 signing, in atomic units and nanoseconds.

Only registered escrow contracts are signed. Read operator scope and advertised
support from trusted facilitator ``/supported`` configuration.
No helper submits a transaction, reads an access key or allocates a nonce. Pass the
NEXT nonce and an expiry block from the selected key; persist the signed payment
before submitting it. Lifecycle callers should use a function-call key restricted
to this escrow and the methods they need (with a bounded gas allowance).

Arras v2 (``NEAR_ESCROW_V2_CONTRACTS``) takes the v1 ``authorize`` msg plus two
optional fields, ``dispute_deadline`` and ``operator_ticket``; v1 refuses both
(``E03``). ``sign_operator_ticket`` is the operator's ed25519 ticket
(arras ``contracts/escrow-v2/src/ticket.rs``). ``authorize_from_budget`` is not
implemented here yet. Signing needs the ``near`` extra; message/id builders work
on a base install.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import time
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
    "NEAR_ESCROW_V2_CONTRACTS",
    "near_escrow_contract_version",
    "OPERATOR_TICKET_DOMAIN",
    "OperatorTicketSigner",
    "operator_ticket_message",
    "sign_operator_ticket",
    "check_operator_ticket",
    "verify_operator_ticket",
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
# Arras deployments this SDK signs for, isolated by network: v1 first, then v2.
NEAR_ESCROW_CONTRACTS: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        "near:testnet": ("arras-v1b-1101.testnet",),
        "near:mainnet": ("v1.arras-escrow.near", "v2.arras-escrow.near"),
    }
)
# The registered contracts that run Arras v2 (they take dispute_deadline and
# operator_ticket). v2.arras-escrow.near: the reviewed wasm (arras ddc5121, sha256
# 1541525c...fa77), keyless since 2026-11-08 (arras docs/MAINNET.md 8.1, 8.8).
# Testnet has none: the v2 rehearsal contract takes a test token, not Circle's USDC
# (MAINNET.md 8.6), and the earlier one predates the review.
NEAR_ESCROW_V2_CONTRACTS: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        "near:testnet": (),
        "near:mainnet": ("v2.arras-escrow.near",),
    }
)
# arras contracts/escrow-v2/src/ticket.rs:25
OPERATOR_TICKET_DOMAIN = "arras:v2:operator_ticket"
# A base58 64-byte signature has at most 88 characters (ticket.rs:63-66).
_MAX_TICKET_SIG_LEN = 88
# The keys of a v1 authorize msg; v1 refuses any other (deny_unknown_fields, E03).
_V1_MSG_KEYS = frozenset(
    (
        "v",
        "action",
        "operator_id",
        "receiver",
        "release_deadline",
        "reclaim_after",
        "fee_bps_max",
        "terms_hash",
        "salt",
    )
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


def _operator_id(value: object, name: str) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[a-z0-9_-]{1,32}", value) is None:
        raise ValueError(f"{name}: expected [a-z0-9_-]{{1,32}}")
    return value


def _payment_id(value: object) -> str:
    try:
        if not isinstance(value, str) or not 32 <= len(value) <= 44:
            raise ValueError
        raw = _b58decode(value)
        if len(raw) != 32 or _b58encode(raw) != value:
            raise ValueError
    except ValueError:
        raise ValueError("invalid payment_id") from None
    return value


def _ticket_signature(value: object) -> bytes:
    """Canonical base58 of exactly 64 bytes, as ticket.rs:62-73 decodes it."""
    try:
        if not isinstance(value, str) or not 1 <= len(value) <= _MAX_TICKET_SIG_LEN:
            raise ValueError
        raw = _b58decode(value)
        if len(raw) != 64 or _b58encode(raw) != value:
            raise ValueError
    except ValueError:
        raise ValueError("operator_ticket.sig: expected canonical base58 of 64 bytes") from None
    return raw


def _ticket_value(value: object) -> dict:
    """Exactly {expiry, sig}, as msg.rs:28-33 parses it (deny_unknown_fields).

    expiry is the U64 decimal STRING of the wire: a JSON number there is E03.
    """
    if not isinstance(value, Mapping) or value.keys() != {"expiry", "sig"}:
        raise ValueError("operator_ticket: expected exactly {expiry, sig}")
    if not isinstance(value["expiry"], str):
        raise ValueError("operator_ticket.expiry: expected a decimal string")
    expiry = _uint(value["expiry"], 64, "operator_ticket.expiry")
    _ticket_signature(value["sig"])
    return {"expiry": str(expiry), "sig": value["sig"]}


def _msg_v2_fields(p: Mapping[str, Any], release_at: int, reclaim_at: int) -> dict:
    """The v2 fields present (not None), in the order of msg.rs:66-69."""
    fields: dict = {}
    if p.get("dispute_deadline") is not None:
        deadline = _uint(p["dispute_deadline"], 64, "dispute_deadline")
        if deadline < release_at:
            raise ValueError("dispute_deadline before release_deadline (Arras E67)")
        if deadline > reclaim_at:
            raise ValueError("dispute_deadline after reclaim_after (Arras E11)")
        fields["dispute_deadline"] = str(deadline)
    if p.get("operator_ticket") is not None:
        fields["operator_ticket"] = _ticket_value(p["operator_ticket"])
    return fields


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
    _operator_id(p["operator_id"], "operator_id")
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
    Arras v2 only, appended in this order when present and not None:
    dispute_deadline (Unsigned, release_deadline <= it <= reclaim_after) and
    operator_ticket ({expiry, sig}, what sign_operator_ticket returns).
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
            **_msg_v2_fields(params, release_at, reclaim_at),
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


class OperatorTicketSigner(Protocol):
    """The operator's ticket key: Ed25519 over the RAW ticket message, never a hash.

    ``NearEd25519Signer`` (a seed held in memory) satisfies it, and so does any
    external signer with these two members. The SDK never loads a key itself.
    ``OwsNearSigner`` does not: its ``sign`` only takes 32-byte digests and
    refuses the message (ValueError, nothing signed).
    """

    @property
    def public_key(self) -> bytes:
        """Raw 32-byte Ed25519 public key (the operator's ``ticket_pubkey``)."""
        ...

    def sign(self, message: bytes) -> bytes:
        """Return the raw 64-byte Ed25519 signature of ``message``."""
        ...


def _ticket_subject(subject: object) -> tuple[int, tuple[str, ...]]:
    """ticket.rs:30-39: tag 0 payment {payment_id}; tag 1 budget {operator_id, owner, agent}."""
    if isinstance(subject, Mapping) and len(subject) == 1:
        kind, fields = next(iter(subject.items()))
        if not isinstance(fields, Mapping):
            raise ValueError("subject: expected a mapping of fields")
        if kind == "payment" and fields.keys() == {"payment_id"}:
            return 0, (_payment_id(fields["payment_id"]),)
        if kind == "budget" and fields.keys() == {"operator_id", "owner", "agent"}:
            _operator_id(fields["operator_id"], "subject.operator_id")
            _account(fields["owner"], "subject.owner")
            _account(fields["agent"], "subject.agent")
            return 1, (fields["operator_id"], fields["owner"], fields["agent"])
    raise ValueError(
        "subject: expected {'payment': {'payment_id'}} or "
        "{'budget': {'operator_id', 'owner', 'agent'}}"
    )


def _ticket_public_key(value: object) -> bytes:
    if isinstance(value, bytes) and len(value) == 32:
        return value
    try:
        if not isinstance(value, str) or not value.startswith("ed25519:") or len(value) > 52:
            raise ValueError
        raw = _b58decode(value[8:])
        if len(raw) != 32 or _b58encode(raw) != value[8:]:
            raise ValueError
    except ValueError:
        raise ValueError("public_key: expected 32 bytes or ed25519:<base58 of 32 bytes>") from None
    return raw


def _ed25519_valid(public_key: bytes, signature: bytes, message: bytes) -> bool:
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    except ImportError:
        raise ImportError(
            "Arras operator tickets need cryptography: "
            "pip install 'uvd-x402-sdk[near]' (the near extra)"
        ) from None
    try:
        Ed25519PublicKey.from_public_bytes(public_key).verify(signature, message)
    except InvalidSignature:
        return False
    return True


def operator_ticket_message(contract: str, subject: Mapping[str, Any], expiry: Unsigned) -> bytes:
    """The bytes the operator signs: ``borsh(TicketPreimage)`` of ticket.rs:41-59.

    ``OPERATOR_TICKET_DOMAIN`` String, ``contract`` String (the escrow account),
    ``subject`` enum (u8 tag, then its Strings), ``expiry`` u64 LE nanoseconds.
    The contract's views ``ticket_message_payment`` / ``ticket_message_budget``
    (views.rs:214-242) return these same bytes. ``subject`` is
    ``{"payment": {"payment_id": ...}}`` or
    ``{"budget": {"operator_id": ..., "owner": ..., "agent": ...}}``.
    Any valid account is accepted as ``contract``: its name is inside the signed
    bytes, so the ticket is only valid on that contract.
    """
    _account(contract, "contract")
    tag, values = _ticket_subject(subject)
    n = _uint(expiry, 64, "expiry")
    ser = BorshSerializer().write_string(OPERATOR_TICKET_DOMAIN).write_string(contract)
    ser.write_u8(tag)
    for value in values:
        ser.write_string(value)
    return ser.write_u64(n).get_bytes()


def _now(now_ns: Unsigned | None) -> int:
    return time.time_ns() if now_ns is None else _uint(now_ns, 64, "now_ns")


def sign_operator_ticket(
    signer: OperatorTicketSigner,
    *,
    contract: str,
    subject: Mapping[str, Any],
    expiry: Unsigned,
    now_ns: Unsigned | None = None,
) -> dict:
    """Sign an Arras v2 ``operator_ticket``: ``{"expiry": "<ns>", "sig": "<base58>"}``.

    The dict goes as is into an ``authorize`` msg (``msg["operator_ticket"]``).
    Valid on chain while ``now < expiry`` (nanoseconds), E54 after: a ticket
    already expired at ``now_ns`` (default: the wall clock) is refused, which
    also catches an expiry written in seconds. A payment ticket is single use in
    practice (a payment_id never repeats, E13); a budget ticket admits every
    deposit of that (owner, agent) pair until expiry, even after revoke_agent,
    so keep expiry short. Take ``contract`` from your own configuration, never
    from a request. Every input is checked before the signer is touched, and
    the signature is verified before it is returned.
    """
    message = operator_ticket_message(contract, subject, expiry)
    n = _uint(expiry, 64, "expiry")
    if n <= _now(now_ns):
        raise ValueError("expiry: the ticket is already expired at now_ns (Arras E54)")
    public_key = signer.public_key
    if not isinstance(public_key, bytes) or len(public_key) != 32:
        raise ValueError("expected 32-byte Ed25519 public key")
    signature = signer.sign(message)
    if not isinstance(signature, bytes) or len(signature) != 64:
        raise ValueError("invalid Ed25519 ticket signature")
    if not _ed25519_valid(public_key, signature, message):
        raise ValueError("invalid Ed25519 ticket signature")
    return {"expiry": str(n), "sig": _b58encode(signature)}


def check_operator_ticket(
    ticket_pubkey: bytes | str | None,
    *,
    contract: str,
    subject: Mapping[str, Any],
    ticket: object,
    now_ns: Unsigned | None = None,
) -> str | None:
    """What ``check_ticket`` (authorize.rs:265-285) answers: ``None`` if it passes.

    ``ticket_pubkey`` is the operator's, as ``get_operator(...).ticket_pubkey``
    returns it (``ed25519:<base58>``), its 32 bytes, or ``None`` if it has none.
    No key and no ticket passes; a key without a ticket, or a ticket without a
    key, is ``"E53_TICKET_INVALIDO"``; ``now_ns >= expiry`` (default: the wall
    clock) is ``"E54_TICKET_VENCIDO"``, judged before the signature is verified;
    a ticket that is not exactly ``{expiry, sig}`` with a canonical decimal expiry
    and a canonical base58 64-byte signature of this contract, subject and expiry
    is E53. Two differences with the chain, both in tickets the SDK never writes:
    it parses ``expiry`` with ``str::parse`` (``"+1"``, ``"01"`` pass there), and
    it decodes ``sig`` only after E54 (msg.rs:20-21), so an expired ticket with a
    malformed ``sig`` is E54 there and E53 here (as in the TypeScript SDK). A malformed
    key, contract, subject or now_ns (the caller's inputs) raises ValueError; a
    malformed ticket (untrusted) is E53. A pre-check: the chain verifies with
    its own Ed25519 implementation.
    """
    _account(contract, "contract")
    _ticket_subject(subject)
    now = _now(now_ns)
    key = None if ticket_pubkey is None else _ticket_public_key(ticket_pubkey)
    if key is None and ticket is None:
        return None
    if key is None or ticket is None:
        return "E53_TICKET_INVALIDO"
    try:
        fields = _ticket_value(ticket)
    except ValueError:
        return "E53_TICKET_INVALIDO"
    expiry = int(fields["expiry"])
    if now >= expiry:
        return "E54_TICKET_VENCIDO"
    message = operator_ticket_message(contract, subject, expiry)
    if not _ed25519_valid(key, _ticket_signature(fields["sig"]), message):
        return "E53_TICKET_INVALIDO"
    return None


def verify_operator_ticket(
    public_key: bytes | str,
    *,
    contract: str,
    subject: Mapping[str, Any],
    ticket: object,
    now_ns: Unsigned | None = None,
) -> bool:
    """True iff an operator with this ``ticket_pubkey`` accepts ``ticket`` at ``now_ns``.

    ``check_operator_ticket(...) is None`` with a key that must be given: 32 raw
    bytes or ``ed25519:<base58>``. Expired (E54) or not this message's (E53) is
    False. If the operator may have no key any more (the owner can remove it),
    ask ``check_operator_ticket`` with its current ``ticket_pubkey``.
    """
    if public_key is None:
        raise ValueError("public_key: expected the operator ticket_pubkey")
    return (
        check_operator_ticket(
            public_key, contract=contract, subject=subject, ticket=ticket, now_ns=now_ns
        )
        is None
    )


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


def near_escrow_contract_version(network: str, contract: str) -> str:
    """``"v1"`` or ``"v2"`` for a registered Arras contract; ValueError otherwise."""
    if not isinstance(network, str) or network not in NEAR_ESCROW_TOKENS:
        raise ValueError("unsupported NEAR network: use near:mainnet or near:testnet")
    if contract not in NEAR_ESCROW_CONTRACTS[network]:
        raise ValueError("escrow_contract is not registered for this network")
    return "v2" if contract in NEAR_ESCROW_V2_CONTRACTS[network] else "v1"


def _options(options: NearEscrowDelegateOptions) -> str:
    # The network first, as in 0.112.0 and the TypeScript SDK: the same first refusal.
    if options.network not in NEAR_ESCROW_TOKENS:
        raise ValueError("unsupported NEAR network: use near:mainnet or near:testnet")
    _account(options.sender_id, "sender_id")
    _account(options.escrow_contract, "escrow_contract")
    version = near_escrow_contract_version(options.network, options.escrow_contract)
    nonce = _uint(options.nonce, 64, "nonce")
    height = _uint(options.max_block_height, 64, "max_block_height")
    if nonce == 0:
        raise ValueError("nonce must be positive")
    if nonce >= height * 1_000_000:
        raise ValueError("nonce must be below max_block_height * 1_000_000")
    return version


def build_escrow_authorization_delegate(
    options: NearEscrowDelegateOptions,
    *,
    token: str,
    amount: Unsigned,
    msg: Mapping[str, Any],
) -> dict:
    """Build the single FT function call. No key is used by this builder.

    On a v1 contract the msg may not carry dispute_deadline or operator_ticket:
    v1 parses it with deny_unknown_fields and returns the whole amount (E03).
    """
    version = _options(options)
    if token != NEAR_ESCROW_TOKENS[options.network]:
        raise ValueError("token does not match network USDC")
    canonical_msg = build_escrow_msg(msg)
    if version == "v1" and json.loads(canonical_msg).keys() - _V1_MSG_KEYS:
        raise ValueError("dispute_deadline and operator_ticket need an Arras v2 contract")
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
    """Sign one allowed method (the same five on v1 and v2), deposit zero, its gas floor/cap."""
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
        params["payment_id"] = _payment_id(args.get("payment_id"))
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
    """Arras v2 budget adapter boundary (budget.rs:41-85); not implemented in this SDK yet."""

    def authorize_from_budget(self, request: Mapping[str, Any]) -> dict: ...


def authorize_from_budget(options: NearEscrowDelegateOptions, request: Mapping[str, Any]) -> dict:
    """Reserved v2 entry point. Always raises before accessing the signer."""
    raise NotImplementedError("Arras v2 authorize_from_budget is not implemented in this SDK yet")
