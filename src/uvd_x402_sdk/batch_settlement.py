"""x402 ``batch-settlement`` on EVM, BUYER side: the client that signs a deposit and cumulative vouchers.

The scheme (coinbase/x402, ``specs/schemes/batch-settlement/scheme_batch_settlement_evm.md``) lets a buyer open
a payment channel once and pay each later request with an off-chain voucher. This module is the part a buyer
needs, and nothing of the seller or the facilitator:

* :func:`compute_channel_id` -- the chain-bound EIP-712 hash of the channel config.
* :func:`sign_voucher` -- the cumulative ``Voucher`` typed data.
* :func:`build_deposit_payload` -- ERC-3009 ``ReceiveWithAuthorization`` to the deposit collector + a voucher.
* :class:`BatchSettlementClient` -- decides deposit vs voucher, keeps the channel state, and updates it from the
  seller's response the way the spec says (from its own previous state plus ``chargedAmount``, never by copying
  the seller's ``channelState``).

It signs through the SDK's :class:`~uvd_x402_sdk.wallet.WalletAdapter` (``sign_typed_data``), so a key in the
process, a Paybox wallet or any other adapter works the same; the key never has to be loaded here.

WHAT IS NOT HERE, on purpose: the Permit2 deposit path (``assetTransferMethod: "permit2"`` raises
:class:`BatchSettlementError`, it is never guessed), the cooperative refund, and the corrective-402 resync. USDC
settles through ERC-3009, which is the default the spec names.

MONEY RULES this module enforces, because a 402 is somebody else's input:

* A deposit is capped by ``max_deposit`` (default 1 USDC in atomic units of the asset, i.e. ``1_000_000`` for a
  6-decimals token). A seller whose ``minDeposit`` is above the cap is refused, not obeyed.
* The seller's ``extra.minDeposit`` is only a target; the amount is never below what the next voucher needs.
* Only ``eip155:*`` networks and only ``scheme == "batch-settlement"`` are signed.
"""

from __future__ import annotations

import base64
import json
import os
import secrets as _secrets
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Protocol

SCHEME = "batch-settlement"

# Deterministic CREATE2 addresses, the same on every supported EVM chain (spec, "Contract addresses").
BATCH_SETTLEMENT_ADDRESS = "0x4020074e9dF2ce1deE5A9C1b5c3f541D02a10003"
ERC3009_DEPOSIT_COLLECTOR_ADDRESS = "0x4020806089470a89826cB9fB1f4059150b550004"
PERMIT2_DEPOSIT_COLLECTOR_ADDRESS = "0x4020425FAf3B746C082C2f942b4E5159887B0005"

BATCH_SETTLEMENT_DOMAIN_NAME = "x402 Batch Settlement"
BATCH_SETTLEMENT_DOMAIN_VERSION = "1"

MIN_WITHDRAW_DELAY = 900
MAX_WITHDRAW_DELAY = 2_592_000

ZERO_ADDRESS = "0x" + "0" * 40
ZERO_BYTES32 = "0x" + "0" * 64

#: Default ceiling for ONE deposit, in atomic units. A 402 must not be able to lock unbounded escrow (spec).
DEFAULT_MAX_DEPOSIT = 1_000_000
#: Used when the seller announces no ``minDeposit``: the deposit target is this many requests.
DEFAULT_DEPOSIT_MULTIPLIER = 5

CHANNEL_CONFIG_TYPES: Dict[str, list] = {
    "ChannelConfig": [
        {"name": "payer", "type": "address"},
        {"name": "payerAuthorizer", "type": "address"},
        {"name": "receiver", "type": "address"},
        {"name": "receiverAuthorizer", "type": "address"},
        {"name": "token", "type": "address"},
        {"name": "withdrawDelay", "type": "uint40"},
        {"name": "salt", "type": "bytes32"},
    ],
}
VOUCHER_TYPES: Dict[str, list] = {
    "Voucher": [
        {"name": "channelId", "type": "bytes32"},
        {"name": "maxClaimableAmount", "type": "uint128"},
    ],
}
RECEIVE_AUTHORIZATION_TYPES: Dict[str, list] = {
    "ReceiveWithAuthorization": [
        {"name": "from", "type": "address"},
        {"name": "to", "type": "address"},
        {"name": "value", "type": "uint256"},
        {"name": "validAfter", "type": "uint256"},
        {"name": "validBefore", "type": "uint256"},
        {"name": "nonce", "type": "bytes32"},
    ],
}


class BatchSettlementError(ValueError):
    """The 402 or the channel state cannot be signed as asked. Never retried blindly."""


# ---------------------------------------------------------------------------
# Plain helpers
# ---------------------------------------------------------------------------


def _crypto():
    try:
        from eth_abi import encode as abi_encode
        from eth_account.messages import encode_typed_data
        from eth_utils import keccak, to_checksum_address
    except ImportError as exc:  # pragma: no cover - environment
        raise ImportError(
            "batch-settlement signing needs the 'signer' extra: pip install 'uvd-x402-sdk[signer]'"
        ) from exc
    return abi_encode, encode_typed_data, keccak, to_checksum_address


def _chain_id(network: str) -> int:
    if not isinstance(network, str) or not network.startswith("eip155:"):
        raise BatchSettlementError(f"batch-settlement signs only eip155 networks, got {network!r}")
    try:
        return int(network.split(":", 1)[1])
    except ValueError as exc:
        raise BatchSettlementError(f"bad network {network!r}") from exc


def _int(value: Any, what: str) -> int:
    if isinstance(value, bool):
        raise BatchSettlementError(f"{what} must be an integer")
    if isinstance(value, int):
        n = value
    elif isinstance(value, str) and value.strip().isdigit():
        n = int(value.strip())
    elif isinstance(value, str) and value.lower().startswith("0x"):
        n = int(value, 16)
    else:
        raise BatchSettlementError(f"{what} must be an integer, got {value!r}")
    if n < 0:
        raise BatchSettlementError(f"{what} must not be negative")
    return n


def _bytes32(value: Any, what: str) -> bytes:
    if isinstance(value, (bytes, bytearray)):
        raw = bytes(value)
    else:
        text = str(value)
        raw = bytes.fromhex(text[2:] if text.lower().startswith("0x") else text)
    if len(raw) != 32:
        raise BatchSettlementError(f"{what} must be 32 bytes")
    return raw


@dataclass(frozen=True)
class ChannelConfig:
    """The immutable channel identity. ``channelId = EIP712Hash(ChannelConfig)``."""

    payer: str
    payer_authorizer: str
    receiver: str
    receiver_authorizer: str
    token: str
    withdraw_delay: int
    salt: str = ZERO_BYTES32

    def message(self) -> Dict[str, Any]:
        _, _, _, checksum = _crypto()
        return {
            "payer": checksum(self.payer),
            "payerAuthorizer": checksum(self.payer_authorizer),
            "receiver": checksum(self.receiver),
            "receiverAuthorizer": checksum(self.receiver_authorizer),
            "token": checksum(self.token),
            "withdrawDelay": int(self.withdraw_delay),
            "salt": _bytes32(self.salt, "salt"),
        }

    def to_dict(self) -> Dict[str, Any]:
        _, _, _, checksum = _crypto()
        return {
            "payer": checksum(self.payer),
            "payerAuthorizer": checksum(self.payer_authorizer),
            "receiver": checksum(self.receiver),
            "receiverAuthorizer": checksum(self.receiver_authorizer),
            "token": checksum(self.token),
            "withdrawDelay": int(self.withdraw_delay),
            "salt": "0x" + _bytes32(self.salt, "salt").hex(),
        }


def batch_settlement_domain(chain_id: int) -> Dict[str, Any]:
    _, _, _, checksum = _crypto()
    return {
        "name": BATCH_SETTLEMENT_DOMAIN_NAME,
        "version": BATCH_SETTLEMENT_DOMAIN_VERSION,
        "chainId": int(chain_id),
        "verifyingContract": checksum(BATCH_SETTLEMENT_ADDRESS),
    }


def compute_channel_id(config: ChannelConfig, network: str) -> str:
    """The chain-bound channel id (``0x`` + 32 bytes): keccak(0x1901 || domainSeparator || structHash)."""
    _, encode_typed_data, keccak, _ = _crypto()
    signable = encode_typed_data(
        domain_data=batch_settlement_domain(_chain_id(network)),
        message_types=CHANNEL_CONFIG_TYPES,
        message_data=config.message(),
    )
    return "0x" + keccak(b"\x19" + signable.version + signable.header + signable.body).hex()


def erc3009_deposit_nonce(channel_id: str, salt: str) -> str:
    """The ERC-3009 nonce the deposit collector expects: ``keccak256(abi.encode(bytes32 channelId, uint256 salt))``."""
    abi_encode, _, keccak, _ = _crypto()
    return "0x" + keccak(abi_encode(["bytes32", "uint256"], [_bytes32(channel_id, "channel_id"), _int(salt, "salt")])).hex()


def _sign(wallet: Any, domain: Mapping[str, Any], types: Mapping[str, Any], primary: str, message: Mapping[str, Any]) -> str:
    signed = wallet.sign_typed_data(
        {"domain": dict(domain), "types": dict(types), "primaryType": primary, "message": dict(message)}
    )
    sig = signed["signature"] if isinstance(signed, Mapping) else str(signed)
    return sig if sig.startswith("0x") else "0x" + sig


def sign_voucher(wallet: Any, channel_id: str, max_claimable_amount: int, network: str) -> Dict[str, str]:
    """Sign a cumulative ``Voucher``: the seller may claim up to ``max_claimable_amount`` from the channel."""
    message = {
        "channelId": _bytes32(channel_id, "channel_id"),
        "maxClaimableAmount": _int(max_claimable_amount, "max_claimable_amount"),
    }
    signature = _sign(wallet, batch_settlement_domain(_chain_id(network)), VOUCHER_TYPES, "Voucher", message)
    return {
        "channelId": "0x" + message["channelId"].hex(),
        "maxClaimableAmount": str(message["maxClaimableAmount"]),
        "signature": signature,
    }


def _sign_deposit_authorization(
    wallet: Any,
    domain: Mapping[str, Any],
    message: Mapping[str, Any],
    network: str,
    payer: str,
    delegation_resolver: Any,
) -> str:
    """Sign the ERC-3009 ``ReceiveWithAuthorization``, wrapping it for a delegated (EIP-7702) payer.

    The token contract verifies this signature against the PAYER. A payer whose EOA carries EIP-7702 code
    (every Paybox wallet after its first money operation) is checked through ERC-1271, so a raw ECDSA
    signature from it is unsettleable. With a ``delegation_resolver`` (the SDK's ``DelegationResolver``:
    ``(address, network) -> target | True | False | None``) the signature goes through the same wrap the SDK
    already uses for ``TransferWithAuthorization`` (:func:`uvd_x402_sdk.erc7702.sign_eip3009_for_delegated`),
    and only for the delegate targets that need it. ``None`` (the chain could not be read) is NOT "plain":
    it refuses. Without a resolver the payer is taken as a plain EOA, as before; a caller whose wallet may
    be delegated must pass one.
    """
    if delegation_resolver is None:
        return _sign(wallet, domain, RECEIVE_AUTHORIZATION_TYPES, "ReceiveWithAuthorization", message)
    from .erc7702 import (
        eip712_digest,
        needs_account_wrap,
        resolve_delegation,
        sign_eip3009_for_delegated,
    )

    delegated, target = resolve_delegation(payer, network, delegation_resolver)
    if delegated is None:
        raise BatchSettlementError(
            "cannot tell whether the payer is EIP-7702 delegated (the chain could not be read): "
            "refusing to sign a deposit authorization that may be unsettleable"
        )
    if delegated and (target is None or needs_account_wrap(target)):
        digest = eip712_digest(dict(domain), RECEIVE_AUTHORIZATION_TYPES, dict(message))
        return sign_eip3009_for_delegated(
            wallet=wallet, inner_digest=digest, chain_id=_chain_id(network), account=payer
        )
    return _sign(wallet, domain, RECEIVE_AUTHORIZATION_TYPES, "ReceiveWithAuthorization", message)


def build_deposit_payload(
    wallet: Any,
    requirements: Mapping[str, Any],
    config: ChannelConfig,
    deposit_amount: int,
    max_claimable_amount: int,
    *,
    voucher_wallet: Any = None,
    deposit_salt: Optional[str] = None,
    now: Optional[int] = None,
    delegation_resolver: Any = None,
) -> Dict[str, Any]:
    """The inner ``payload`` of a ``deposit``: an ERC-3009 authorization to the collector plus the first voucher."""
    _, _, _, checksum = _crypto()
    extra = requirements.get("extra") or {}
    name, version = extra.get("name"), extra.get("version")
    if not name or not version:
        raise BatchSettlementError("requirements.extra needs the token's EIP-712 name and version")
    network = str(requirements["network"])
    channel_id = compute_channel_id(config, network)
    salt = deposit_salt or "0x" + _secrets.token_bytes(32).hex()
    valid_before = int(now if now is not None else time.time()) + _int(requirements.get("maxTimeoutSeconds", 3600), "maxTimeoutSeconds")
    domain = {
        "name": name,
        "version": version,
        "chainId": _chain_id(network),
        "verifyingContract": checksum(requirements["asset"]),
    }
    message = {
        "from": checksum(config.payer),
        "to": checksum(ERC3009_DEPOSIT_COLLECTOR_ADDRESS),
        "value": _int(deposit_amount, "deposit_amount"),
        "validAfter": 0,
        "validBefore": valid_before,
        "nonce": _bytes32(erc3009_deposit_nonce(channel_id, salt), "nonce"),
    }
    auth_signature = _sign_deposit_authorization(
        wallet, domain, message, network, config.payer, delegation_resolver
    )
    voucher = sign_voucher(voucher_wallet or wallet, channel_id, max_claimable_amount, network)
    return {
        "type": "deposit",
        "channelConfig": config.to_dict(),
        "voucher": voucher,
        "deposit": {
            "amount": str(int(deposit_amount)),
            "authorization": {
                "erc3009Authorization": {
                    "validAfter": "0",
                    "validBefore": str(valid_before),
                    "salt": salt,
                    "signature": auth_signature,
                }
            },
        },
    }


def build_voucher_payload(
    wallet: Any, config: ChannelConfig, network: str, max_claimable_amount: int
) -> Dict[str, Any]:
    channel_id = compute_channel_id(config, network)
    return {
        "type": "voucher",
        "channelConfig": config.to_dict(),
        "voucher": sign_voucher(wallet, channel_id, max_claimable_amount, network),
    }


def payment_payload(
    requirements: Mapping[str, Any], inner: Mapping[str, Any], resource: Optional[Mapping[str, Any]] = None
) -> Dict[str, Any]:
    """The x402 v2 ``PaymentPayload`` envelope around an inner deposit/voucher payload."""
    out: Dict[str, Any] = {"x402Version": 2}
    if resource is not None:
        out["resource"] = dict(resource)
    out["accepted"] = dict(requirements)
    out["payload"] = dict(inner)
    return out


def payment_header(payload: Mapping[str, Any]) -> str:
    """The base64 value for the ``PAYMENT-SIGNATURE`` request header."""
    return base64.b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode()


# ---------------------------------------------------------------------------
# Channel state storage
# ---------------------------------------------------------------------------


class ChannelStorage(Protocol):
    """Where the buyer keeps its channels. A Fargate task dies between beats: persist somewhere that lives on."""

    def get(self, channel_id: str) -> Optional[Dict[str, str]]: ...

    def set(self, channel_id: str, state: Dict[str, str]) -> None: ...


class MemoryChannelStorage:
    def __init__(self) -> None:
        self._data: Dict[str, Dict[str, str]] = {}

    def get(self, channel_id: str) -> Optional[Dict[str, str]]:
        v = self._data.get(channel_id.lower())
        return dict(v) if v else None

    def set(self, channel_id: str, state: Dict[str, str]) -> None:
        self._data[channel_id.lower()] = dict(state)


class FileChannelStorage:
    """One JSON file for all channels, replaced atomically (a half-written file would lose the cumulative)."""

    def __init__(self, path: "str | os.PathLike[str]") -> None:
        self._path = Path(path)

    def _read(self) -> Dict[str, Dict[str, str]]:
        try:
            return json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}

    def get(self, channel_id: str) -> Optional[Dict[str, str]]:
        return self._read().get(channel_id.lower())

    def set(self, channel_id: str, state: Dict[str, str]) -> None:
        data = self._read()
        data[channel_id.lower()] = dict(state)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(self._path.parent), prefix=self._path.name + ".")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        os.replace(tmp, self._path)


# ---------------------------------------------------------------------------
# The client
# ---------------------------------------------------------------------------


@dataclass
class PreparedPayment:
    """What :meth:`BatchSettlementClient.create_payment` returns: the payload, and what to confirm afterwards."""

    payload: Dict[str, Any]
    channel_id: str
    request_amount: int
    deposit_amount: Optional[int] = None

    def header(self) -> str:
        return payment_header(self.payload)


@dataclass
class BatchSettlementClient:
    """Signs ``batch-settlement`` payments for one wallet and keeps its channels' state.

    ``wallet`` is a :class:`~uvd_x402_sdk.wallet.WalletAdapter` (or anything with ``get_address()`` and
    ``sign_typed_data(typed_data)``). ``voucher_wallet`` is an optional second key that signs vouchers; its address
    becomes the channel's ``payerAuthorizer`` so the seller can verify vouchers without an RPC call.
    """

    wallet: Any
    storage: ChannelStorage = field(default_factory=MemoryChannelStorage)
    salt: str = ZERO_BYTES32
    voucher_wallet: Any = None
    max_deposit: int = DEFAULT_MAX_DEPOSIT
    deposit_multiplier: int = DEFAULT_DEPOSIT_MULTIPLIER
    #: ``(address, network) -> target | True | False | None``: pass it when the payer may be an EIP-7702
    #: delegated account (a Paybox wallet), so the deposit authorization is wrapped for ERC-1271 when needed.
    delegation_resolver: Any = None

    def _address(self, wallet: Any) -> str:
        return wallet.get_address() if hasattr(wallet, "get_address") else wallet.address

    def channel_config(self, requirements: Mapping[str, Any]) -> ChannelConfig:
        _, _, _, checksum = _crypto()
        extra = requirements.get("extra") or {}
        authorizer = extra.get("receiverAuthorizer")
        if not authorizer or checksum(authorizer) == checksum(ZERO_ADDRESS):
            raise BatchSettlementError("requirements.extra.receiverAuthorizer must be a non-zero address")
        delay = extra.get("withdrawDelay")
        if isinstance(delay, bool) or not isinstance(delay, int):
            delay = MIN_WITHDRAW_DELAY
        if not MIN_WITHDRAW_DELAY <= delay <= MAX_WITHDRAW_DELAY:
            raise BatchSettlementError(f"withdrawDelay {delay} is outside {MIN_WITHDRAW_DELAY}..{MAX_WITHDRAW_DELAY}")
        payer = self._address(self.wallet)
        authorizer_of_vouchers = self._address(self.voucher_wallet) if self.voucher_wallet is not None else payer
        return ChannelConfig(
            payer=payer,
            payer_authorizer=authorizer_of_vouchers,
            receiver=requirements["payTo"],
            receiver_authorizer=authorizer,
            token=requirements["asset"],
            withdraw_delay=delay,
            salt=self.salt,
        )

    def _deposit_amount(self, requirements: Mapping[str, Any], needed: int, request_amount: int) -> int:
        extra = requirements.get("extra") or {}
        announced = extra.get("minDeposit")
        target = None
        if announced is not None:
            try:
                target = _int(announced, "minDeposit")
            except BatchSettlementError:
                target = None
            if target is not None and target < request_amount:
                target = None  # the spec: a conforming minDeposit is >= amount; otherwise ignore it
        if target is None:
            target = self.deposit_multiplier * request_amount
        amount = max(needed, target)
        if amount > self.max_deposit:
            if needed > self.max_deposit:
                raise BatchSettlementError(f"the next voucher needs a deposit of {needed}, above max_deposit {self.max_deposit}")
            amount = self.max_deposit
        return amount

    def create_payment(
        self, requirements: Mapping[str, Any], resource: Optional[Mapping[str, Any]] = None, *, now: Optional[int] = None
    ) -> PreparedPayment:
        """Sign a deposit (first request or top-up) or a voucher for one paid request."""
        if requirements.get("scheme") != SCHEME:
            raise BatchSettlementError(f"not a batch-settlement offer: {requirements.get('scheme')!r}")
        network = str(requirements.get("network"))
        _chain_id(network)
        extra = requirements.get("extra") or {}
        method = extra.get("assetTransferMethod") or "eip3009"
        if method != "eip3009":
            raise BatchSettlementError(f"assetTransferMethod {method!r} is not supported (only eip3009)")
        config = self.channel_config(requirements)
        channel_id = compute_channel_id(config, network)
        state = self.storage.get(channel_id) or {}
        balance = int(state.get("balance") or 0)
        charged = int(state.get("chargedCumulativeAmount") or 0)
        request_amount = _int(requirements["amount"], "amount")
        max_claimable = charged + request_amount
        deposit: Optional[int] = None
        if balance == 0 or max_claimable > balance:
            deposit = self._deposit_amount(requirements, max_claimable - balance, request_amount)
            inner = build_deposit_payload(
                self.wallet, requirements, config, deposit, max_claimable, voucher_wallet=self.voucher_wallet, now=now,
                delegation_resolver=self.delegation_resolver,
            )
        else:
            inner = build_voucher_payload(self.voucher_wallet or self.wallet, config, network, max_claimable)
        return PreparedPayment(
            payload=payment_payload(requirements, inner, resource),
            channel_id=channel_id,
            request_amount=request_amount,
            deposit_amount=deposit,
        )

    def confirm(self, prepared: PreparedPayment, response_extra: Optional[Mapping[str, Any]] = None) -> bool:
        """Record a SUCCESSFUL paid response. The new cumulative is the previous one plus ``chargedAmount``.

        The seller's ``extra.channelState`` is untrusted and never copied: it is only compared with the number
        the buyer computed itself, and a mismatch leaves the local state untouched (the next request would
        then get a corrective 402). Returns whether the state was written.
        """
        extra = response_extra or {}
        raw = extra.get("chargedAmount", "0")
        try:
            charged = _int(raw, "chargedAmount")
        except BatchSettlementError:
            return False
        if charged > prepared.request_amount:
            return False
        state = self.storage.get(prepared.channel_id) or {}
        previous = int(state.get("chargedCumulativeAmount") or 0)
        nxt = previous + charged
        seller = (extra.get("channelState") or {}).get("chargedCumulativeAmount")
        if seller is not None:
            try:
                if _int(seller, "chargedCumulativeAmount") != nxt:
                    return False
            except BatchSettlementError:
                return False
        if previous == 0 and charged == 0 and prepared.deposit_amount is None:
            return False
        new = dict(state)
        new["chargedCumulativeAmount"] = str(nxt)
        if prepared.deposit_amount is not None:
            new["balance"] = str(int(state.get("balance") or 0) + prepared.deposit_amount)
        self.storage.set(prepared.channel_id, new)
        return True


__all__ = [
    "SCHEME",
    "BATCH_SETTLEMENT_ADDRESS",
    "ERC3009_DEPOSIT_COLLECTOR_ADDRESS",
    "BatchSettlementClient",
    "BatchSettlementError",
    "ChannelConfig",
    "FileChannelStorage",
    "MemoryChannelStorage",
    "PreparedPayment",
    "build_deposit_payload",
    "build_voucher_payload",
    "compute_channel_id",
    "erc3009_deposit_nonce",
    "payment_header",
    "payment_payload",
    "sign_voucher",
]
