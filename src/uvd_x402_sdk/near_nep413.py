"""Offline NEP-413 message signatures using a caller-selected FullAccess key.

No RPC or transaction is sent. A verifier must bind accountId/publicKey to a
FullAccess key on the intended network and check the challenge, nonce, recipient
and callback. Possession of a signature alone is not proof of account ownership.
"""

from __future__ import annotations

import base64
import hashlib
from typing import TypedDict

from .near_escrow import NearLocalSigner, _account
from .networks.near import BorshSerializer
from .solana_signing import _b58encode

NEP413_PREFIX = (2**31 + 413).to_bytes(4, "little")


class Nep413SignedMessage(TypedDict):
    accountId: str
    publicKey: str
    signature: str


def nep413_preimage(
    message: str,
    nonce: bytes,
    recipient: str,
    callback_url: str | None = None,
) -> bytes:
    """NEP-413 prefix plus Borsh message, fixed nonce, recipient, Option<String>."""
    if not isinstance(message, str):
        raise ValueError("message must be a string")
    if not isinstance(recipient, str) or not recipient or recipient.strip() != recipient:
        raise ValueError("recipient must be nonempty without surrounding whitespace")
    if recipient.lower().removesuffix(".") == "intents.near":
        raise ValueError("intents.near is not an allowed recipient")
    if not isinstance(nonce, bytes) or len(nonce) != 32:
        raise ValueError("nonce must contain 32 bytes")
    if callback_url is not None and not isinstance(callback_url, str):
        raise ValueError("callback_url must be a string")
    ser = (
        BorshSerializer()
        .write_fixed_bytes(NEP413_PREFIX)
        .write_string(message)
        .write_fixed_bytes(nonce)
        .write_string(recipient)
    )
    ser.write_u8(0 if callback_url is None else 1)
    if callback_url is not None:
        ser.write_string(callback_url)
    return ser.get_bytes()


def sign_nep413(
    signer: NearLocalSigner,
    *,
    account_id: str,
    message: str,
    nonce: bytes,
    recipient: str,
    callback_url: str | None = None,
) -> Nep413SignedMessage:
    """Sign SHA-256(preimage); return NEP-413 camelCase fields and base64 signature.

    The caller supplies a FullAccess key; this offline helper cannot read key
    permissions. All input is checked before invoking the signer.
    """
    _account(account_id, "account_id")
    digest = hashlib.sha256(nep413_preimage(message, nonce, recipient, callback_url)).digest()
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    public_key = signer.public_key
    if not isinstance(public_key, bytes) or len(public_key) != 32:
        raise ValueError("expected 32-byte Ed25519 public key")
    signature = signer.sign(digest)
    if not isinstance(signature, bytes) or len(signature) != 64:
        raise ValueError("invalid Ed25519 NEP-413 signature")
    try:
        Ed25519PublicKey.from_public_bytes(public_key).verify(signature, digest)
    except InvalidSignature:
        raise ValueError("invalid Ed25519 NEP-413 signature") from None
    return {
        "accountId": account_id,
        "publicKey": "ed25519:" + _b58encode(public_key),
        "signature": base64.b64encode(signature).decode("ascii"),
    }
