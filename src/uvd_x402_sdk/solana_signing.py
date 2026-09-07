"""Rater-authored ERC-8004 feedback on Solana: the half the rater signs.

``POST /feedback`` puts the FACILITATOR's keypair in account 0 of the
program's ``give_feedback`` instruction -- the account the program declares
``[signer, writable] client (feedback author / fee payer)``. So the chain
records the facilitator as the author of the rating, not whoever made it.
Solana takes several signers per transaction natively, which is why the fix
needs no delegation and no program change: **the rater signs as ``client``
while the facilitator stays the fee payer.**

The facilitator serves that as two calls -- ``/feedback/solana/prepare`` hands
back an UNSIGNED transaction, ``/feedback/solana/submit`` co-signs and sends
it. This module is what goes between them, and it does one thing: put the
rater's ed25519 signature in the rater's slot **without touching a single byte
of the message**.

That last clause is the whole contract. The facilitator does not sign what it
is given: it rebuilds the message from the declared parameters and refuses
anything that is not byte-for-byte what it would have offered (``x402-rs``,
``src/erc8004/solana.rs`` ``accept_rater_signed_transaction``). So this module
never re-serialises the message -- it carries the exact bytes that came off
the wire and only rewrites the signature array. A client that decoded the
message into structs and re-encoded it would produce something that looks
identical and can still trip a canonicalisation difference, and the only
symptom would be ``400 submitted transaction does not match the one this
facilitator built``.

Usage::

    from uvd_x402_sdk import Ed25519Signer, sign_solana_feedback_transaction

    signer = Ed25519Signer(os.environ["RATER_SECRET_KEY"])
    prep = await client.prepare_solana_feedback(
        network="solana", agent_id=ASSET, rater=signer.pubkey,
        value=87, score=95, tag1="quality",
    )
    signed = sign_solana_feedback_transaction(prep.transaction, signer.pubkey, signer)
    result = await client.submit_solana_feedback(
        network="solana", agent_id=ASSET, rater=signer.pubkey,
        value=87, score=95, tag1="quality", transaction=signed,
    )

Signing needs an ed25519 backend: ``pip install 'uvd-x402-sdk[solana]'``.
Everything else here -- decoding, the account table, the signer index -- is
stdlib and works on a base install.
"""

from __future__ import annotations

import base64
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Optional, Protocol, Union, runtime_checkable

__all__ = [
    "Ed25519Signer",
    "SolanaFeedbackTransaction",
    "SolanaSigner",
    "decode_solana_transaction",
    "sign_solana_feedback_transaction",
]

_B58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"

#: Length of an ed25519 signature, and of the slot it occupies in the
#: transaction's signature array.
SIGNATURE_LEN = 64
#: Length of a Solana public key.
PUBKEY_LEN = 32


def _b58decode(value: str) -> bytes:
    """Decode base58 (Bitcoin alphabet), the encoding Solana uses everywhere.

    Pure python on purpose: it is fifteen lines, and making it an optional
    dependency would mean one of the two branches never runs in CI.
    """
    if not value:
        raise ValueError("empty base58 string")
    num = 0
    for ch in value:
        idx = _B58_ALPHABET.find(ch)
        if idx < 0:
            raise ValueError(f"invalid base58 character {ch!r}")
        num = num * 58 + idx
    body = num.to_bytes((num.bit_length() + 7) // 8, "big") if num else b""
    leading = len(value) - len(value.lstrip("1"))
    return b"\x00" * leading + body


def _b58encode(raw: bytes) -> str:
    num = int.from_bytes(raw, "big")
    out = ""
    while num:
        num, rem = divmod(num, 58)
        out = _B58_ALPHABET[rem] + out
    return "1" * (len(raw) - len(raw.lstrip(b"\x00"))) + out


def _decode_pubkey(value: str, what: str) -> bytes:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{what} must be a base58 Solana pubkey, got {value!r}")
    raw = _b58decode(value.strip())
    if len(raw) != PUBKEY_LEN:
        raise ValueError(
            f"{what} decodes to {len(raw)} bytes, expected {PUBKEY_LEN}: {value!r}"
        )
    return raw


def _read_compact_u16(buf: bytes, offset: int):
    """Solana's compact-u16 (`shortvec`) length prefix: 1-3 bytes, 7 bits each."""
    value = 0
    shift = 0
    for _ in range(3):
        if offset >= len(buf):
            raise ValueError("truncated transaction: compact-u16 length ran off the end")
        byte = buf[offset]
        offset += 1
        value |= (byte & 0x7F) << shift
        if byte & 0x80 == 0:
            return value, offset
        shift += 7
    raise ValueError("malformed transaction: compact-u16 longer than three bytes")


def _write_compact_u16(value: int) -> bytes:
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


@runtime_checkable
class SolanaSigner(Protocol):
    """Anything that can sign a message as a Solana account.

    Deliberately two members wide, so a browser wallet, a custodian, an HSM or
    :class:`Ed25519Signer` all satisfy it. ``sign_message`` receives the
    transaction MESSAGE bytes -- raw, with no envelope and no prefix. There is
    no ``personal_sign`` ambiguity to get wrong on this path: what gets signed
    is exactly what the runtime hashes.
    """

    @property
    def pubkey(self) -> str:
        """The signer's base58 public key."""
        ...

    def sign_message(self, message: bytes) -> bytes:
        """Return the 64-byte ed25519 signature over ``message``."""
        ...


def _normalise_secret(secret_key: Union[bytes, bytearray, Sequence, str]):
    """Accept the three shapes a Solana secret key actually comes in.

    ``solana-keygen`` writes a JSON array of 64 ints, wallets export base58 of
    those same 64 bytes, and a raw 32-byte seed is what most libraries take.
    The 64-byte form is ``seed || pubkey``, so it carries its own checksum --
    which is checked, because a key pasted one byte short still parses and
    then signs as somebody else.

    Nothing here ever reaches an error message.
    """
    if isinstance(secret_key, str):
        raw = _b58decode(secret_key.strip())
    elif isinstance(secret_key, (bytes, bytearray)):
        raw = bytes(secret_key)
    elif isinstance(secret_key, Sequence):
        try:
            raw = bytes(bytearray(secret_key))
        except (TypeError, ValueError) as exc:
            raise ValueError("secret key sequence must hold byte-sized ints") from exc
    else:
        raise TypeError(
            "secret key must be bytes, a sequence of ints, or a base58 string"
        )

    if len(raw) == 32:
        return raw, None
    if len(raw) == 64:
        return raw[:32], raw[32:]
    raise ValueError(f"secret key must be 32 or 64 bytes, got {len(raw)}")


class Ed25519Signer:
    """A :class:`SolanaSigner` backed by a raw ed25519 key.

    Requires ``cryptography``: ``pip install 'uvd-x402-sdk[solana]'``.

    The key never leaves this object: it is not kept as bytes, not in
    ``repr``, and not in any error raised from here. Load it from the
    environment or a secrets manager, never from a literal.
    """

    __slots__ = ("_key", "_pubkey")

    def __init__(self, secret_key: Union[bytes, bytearray, Sequence, str]) -> None:
        try:
            from cryptography.hazmat.primitives.asymmetric.ed25519 import (
                Ed25519PrivateKey,
            )
            from cryptography.hazmat.primitives.serialization import (
                Encoding,
                PublicFormat,
            )
        except ImportError as exc:  # pragma: no cover - depends on the install
            raise ImportError(
                "Ed25519Signer needs cryptography; install the 'solana' extra: "
                "pip install 'uvd-x402-sdk[solana]'"
            ) from exc

        seed, declared_pubkey = _normalise_secret(secret_key)
        self._key = Ed25519PrivateKey.from_private_bytes(seed)
        derived = self._key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        if declared_pubkey is not None and declared_pubkey != derived:
            raise ValueError(
                "the 64-byte secret key's public half does not match its private "
                "half -- the key is truncated or corrupted"
            )
        self._pubkey = _b58encode(derived)

    @property
    def pubkey(self) -> str:
        return self._pubkey

    def sign_message(self, message: bytes) -> bytes:
        return self._key.sign(message)

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return f"Ed25519Signer(pubkey={self._pubkey})"


@dataclass(frozen=True)
class SolanaFeedbackTransaction:
    """A decoded Solana transaction that keeps its message as raw bytes.

    ``message`` is never rebuilt from the parsed fields. The facilitator
    compares the submitted message against the one it would have built and
    refuses a mismatch, so carrying the original bytes through is not an
    optimisation -- it is the only thing that makes a re-encode safe.
    """

    signatures: tuple
    """One 64-byte slot per required signature, in account order. An unsigned
    slot is 64 zero bytes."""
    message: bytes
    """The message, exactly as it arrived. This is what gets signed."""
    num_required_signatures: int
    account_keys: tuple
    """Every account in the message, base58, in order. The first
    ``num_required_signatures`` of them are the signers; index 0 is the fee
    payer, which on this rail is the facilitator."""

    def signer_index(self, pubkey: str) -> int:
        """Which signature slot belongs to ``pubkey``.

        Mirrors the facilitator's own check: being present in the account
        table is not enough, the account has to sit inside the signer prefix.
        """
        _decode_pubkey(pubkey, "pubkey")
        target = pubkey.strip()
        try:
            index = self.account_keys.index(target)
        except ValueError:
            raise ValueError(
                f"{target} is not an account of this transaction, so it is not a "
                "required signer -- prepare was called with a different rater"
            ) from None
        if index >= self.num_required_signatures:
            raise ValueError(
                f"{target} is account {index} of this transaction but only the "
                f"first {self.num_required_signatures} are signers"
            )
        return index

    def with_signature(self, pubkey: str, signature: bytes) -> "SolanaFeedbackTransaction":
        """Return a copy carrying ``signature`` in ``pubkey``'s slot.

        Every other slot is left exactly as it was -- the fee payer's stays
        empty on purpose, that one is the facilitator's to fill.
        """
        if len(signature) != SIGNATURE_LEN:
            raise ValueError(
                f"an ed25519 signature is {SIGNATURE_LEN} bytes, got {len(signature)}"
            )
        index = self.signer_index(pubkey)
        signatures = list(self.signatures)
        signatures[index] = bytes(signature)
        return SolanaFeedbackTransaction(
            signatures=tuple(signatures),
            message=self.message,
            num_required_signatures=self.num_required_signatures,
            account_keys=self.account_keys,
        )

    def encode(self) -> str:
        """Back to base64 of the wire format, message bytes untouched."""
        out = bytearray(_write_compact_u16(len(self.signatures)))
        for signature in self.signatures:
            out += signature
        out += self.message
        return base64.b64encode(bytes(out)).decode("ascii")


def decode_solana_transaction(encoded: Optional[str]) -> SolanaFeedbackTransaction:
    """Parse the base64 transaction ``/feedback/solana/prepare`` returns.

    Reads only what the signing step needs -- the signature array, the header
    and the account table -- and keeps the message whole.

    Raises ``ValueError`` on anything it cannot account for, including a
    versioned (v0) message: the facilitator builds a legacy one, and silently
    mis-parsing a v0 header would put the signature in the wrong slot, which
    reads on the wire as a rating signed by nobody.
    """
    if not isinstance(encoded, str) or not encoded.strip():
        raise ValueError("no transaction to sign (prepare returned none)")
    try:
        raw = base64.b64decode(encoded.strip(), validate=True)
    except Exception as exc:
        raise ValueError("transaction is not valid base64") from exc

    count, offset = _read_compact_u16(raw, 0)
    end = offset + SIGNATURE_LEN * count
    if end > len(raw):
        raise ValueError(
            f"truncated transaction: {count} signature slots do not fit in "
            f"{len(raw)} bytes"
        )
    signatures = tuple(
        raw[offset + SIGNATURE_LEN * i : offset + SIGNATURE_LEN * (i + 1)]
        for i in range(count)
    )
    message = raw[end:]
    if len(message) < 3:
        raise ValueError("truncated transaction: no message after the signatures")
    if message[0] & 0x80:
        raise ValueError(
            "this is a versioned (v0) Solana message; the facilitator builds a "
            "legacy one, so a v0 message did not come from "
            "/feedback/solana/prepare"
        )

    num_required = message[0]
    if num_required != count:
        raise ValueError(
            f"malformed transaction: the message requires {num_required} "
            f"signatures but the array carries {count} slots"
        )
    if num_required == 0:
        raise ValueError("malformed transaction: a message with no signers")

    key_count, key_offset = _read_compact_u16(message, 3)
    key_end = key_offset + PUBKEY_LEN * key_count
    if key_end > len(message):
        raise ValueError(
            f"truncated transaction: {key_count} account keys do not fit in the message"
        )
    if key_count < num_required:
        raise ValueError(
            f"malformed transaction: {num_required} required signatures but only "
            f"{key_count} accounts"
        )
    account_keys = tuple(
        _b58encode(
            message[key_offset + PUBKEY_LEN * i : key_offset + PUBKEY_LEN * (i + 1)]
        )
        for i in range(key_count)
    )

    return SolanaFeedbackTransaction(
        signatures=signatures,
        message=message,
        num_required_signatures=num_required,
        account_keys=account_keys,
    )


def sign_solana_feedback_transaction(
    transaction: Optional[str],
    rater: str,
    signer: Any,
) -> str:
    """Sign the prepared feedback transaction as the rater.

    Step 2 of three: ``prepare`` -> **this** -> ``submit``. What comes out is
    the same transaction with one more signature on it, ready to hand to
    ``Erc8004Client.submit_solana_feedback``. The fee payer's slot stays
    empty; the facilitator fills that one itself, after it has verified this
    signature and before it pays anything.

    Args:
        transaction: The base64 blob from
            ``PrepareSolanaFeedbackResponse.transaction``.
        rater: The base58 pubkey the chain will record as the author. Must be
            the one ``prepare`` was called with -- the message is built around
            it, so a different rater is not a different signature, it is a
            transaction this account cannot sign at all.
        signer: A :class:`SolanaSigner` for ``rater``.

    Returns:
        Base64 of the rater-signed transaction.

    Raises:
        ValueError: If the blob is not the transaction ``prepare`` returns, if
            ``rater`` is not one of its signers, or if ``signer`` holds a
            different key than ``rater``. Each of those would otherwise
            surface as a facilitator 400 one round trip later, and from there
            the three are indistinguishable.
    """
    tx = decode_solana_transaction(transaction)

    if isinstance(rater, str):
        rater = rater.strip()
    _decode_pubkey(rater, "rater")

    signer_pubkey = getattr(signer, "pubkey", None)
    if not isinstance(signer_pubkey, str):
        raise TypeError(
            "signer must expose a base58 `pubkey` property and a `sign_message` "
            "method (see SolanaSigner)"
        )
    if signer_pubkey.strip() != rater:
        raise ValueError(
            f"the signer holds {signer_pubkey.strip()} but the transaction was "
            f"prepared for {rater}; the facilitator verifies the rater's slot "
            "before it co-signs, so this is refused rather than mis-attributed"
        )

    signature = signer.sign_message(tx.message)
    if not isinstance(signature, (bytes, bytearray)) or len(signature) != SIGNATURE_LEN:
        got = len(signature) if hasattr(signature, "__len__") else "?"
        raise ValueError(
            f"the signer returned {got} bytes; an ed25519 signature is {SIGNATURE_LEN}"
        )
    return tx.with_signature(rater, bytes(signature)).encode()
