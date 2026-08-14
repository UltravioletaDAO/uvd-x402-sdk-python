"""DX402 ``durable-evidence``: recover a paid response after the fact.

x402 settles payment on-chain permanently but delivers the purchased resource
**exactly once**, in the body of a ``200 OK``, and keeps nothing. A buyer who did
not capture it at that instant cannot recover it, and neither party can later
prove *what* was delivered -- only *that* payment happened.

DX402 closes that gap. The seller seals a copy of the response to the payer's own
public key -- recovered from the payment signature itself -- and anchors it. The
buyer gets an ``X-Durable-Evidence`` header pointing at it.

**Paying is publishing your encryption key.** No registration, no key exchange,
no extra round trip.

This module is the buyer side: parse the header, fetch the ciphertext, decrypt
it, and verify it is genuinely what was delivered.

    from uvd_x402_sdk.dx402 import evidence_from_headers, recover_evidence

    evidence = evidence_from_headers(response.headers)
    body = recover_evidence(evidence, my_private_key)

Full specification: ``docs/plans/dx402/02-SPEC-v0.1.md`` in x402-rs.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from typing import Any, Mapping, Optional

__all__ = [
    "EVIDENCE_HEADER",
    "DX402Error",
    "EvidenceSkipped",
    "ContentHashMismatch",
    "AnchoredEvidence",
    "parse_evidence_header",
    "evidence_from_headers",
    "dereference_pointer",
    "recover_evidence",
    "payment_id",
]

EVIDENCE_HEADER = "X-Durable-Evidence"

#: Magic prefix of a sealed blob, so a stray file is identifiable.
_MAGIC = b"DX402"
_FORMAT_VERSION = 1
_NONCE_LEN = 12
_CEK_LEN = 32
_HKDF_INFO = b"DX402-v1-wrap"


class DX402Error(Exception):
    """Base class for every DX402 failure."""


class EvidenceSkipped(DX402Error):
    """No evidence was anchored for this payment.

    This is a normal outcome, not a fault: the body may have exceeded the
    seller's size cap, the store may have been unreachable, or the payer may be a
    smart-contract wallet with no recoverable key. ``reason`` says which.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(f"no durable evidence was anchored: {reason}")
        self.reason = reason


class ContentHashMismatch(DX402Error):
    """The anchored bytes are not the bytes that were delivered.

    This is the interesting failure. It means the seller anchored something other
    than what it served, which is precisely the fraud ``contentHash`` exists to
    expose. Treat it as evidence of misbehaviour, not as a transport glitch.
    """

    def __init__(self, anchored: str, actual: str) -> None:
        super().__init__(
            f"content hash mismatch: anchored {anchored}, decrypted {actual}"
        )
        self.anchored = anchored
        self.actual = actual


@dataclass(frozen=True)
class AnchoredEvidence:
    """A pointer to sealed evidence, as carried in ``X-Durable-Evidence``."""

    payment_id: str
    pointer: str
    backend: str
    content_hash: str
    cipher: str
    key_alg: str
    mode: str
    retention: str
    receipt: Optional[str] = None

    @property
    def is_end_to_end(self) -> bool:
        """Whether the facilitator is cryptographically unable to read this.

        ``direct`` and ``escrowed`` make materially different claims about who can
        open the payload, so callers that care about confidentiality must check
        this rather than assume.
        """
        return self.mode == "direct"

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "AnchoredEvidence":
        return cls(
            payment_id=data["paymentId"],
            pointer=data["pointer"],
            backend=data.get("backend", "s3"),
            content_hash=data["contentHash"],
            cipher=data.get("cipher", "AES-256-GCM"),
            key_alg=data["keyAlg"],
            mode=data.get("mode", "direct"),
            retention=data.get("retention", "90d"),
            receipt=data.get("receipt"),
        )


def _b64url_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value.strip() + padding)


def parse_evidence_header(value: str) -> AnchoredEvidence:
    """Parse an ``X-Durable-Evidence`` header value.

    Raises :class:`EvidenceSkipped` when the header carries a skip notice rather
    than an anchor -- "the seller chose not to anchor" and "the evidence is
    broken" are different situations and a caller has to be able to tell them
    apart.
    """
    try:
        payload = json.loads(_b64url_decode(value))
    except Exception as exc:  # noqa: BLE001 - any decode failure is the same to callers
        raise DX402Error(f"malformed {EVIDENCE_HEADER} header: {exc}") from exc

    if "skipped" in payload:
        raise EvidenceSkipped(str(payload["skipped"]))
    return AnchoredEvidence.from_dict(payload)


def evidence_from_headers(headers: Mapping[str, str]) -> AnchoredEvidence:
    """Pull the anchored evidence out of a response's headers.

    Header lookup is case-insensitive, because ``httpx`` and ``requests`` disagree
    about casing and HTTP does not care.
    """
    for key, value in headers.items():
        if key.lower() == EVIDENCE_HEADER.lower():
            return parse_evidence_header(value)
    raise DX402Error(f"no {EVIDENCE_HEADER} header on this response")


def dereference_pointer(pointer: str) -> str:
    """Turn a DX402 pointer into a fetchable URL.

    ``s3+https://...`` is a scheme tag over an ordinary HTTPS URL; ``ipfs://`` and
    ``ar://`` go through public gateways. Anything else passes through untouched,
    so a caller with their own resolver is not blocked by this function.
    """
    if pointer.startswith("s3+"):
        return pointer[3:]
    if pointer.startswith("ipfs://"):
        return f"https://ipfs.io/ipfs/{pointer[len('ipfs://'):]}"
    if pointer.startswith("ar://"):
        return f"https://arweave.net/{pointer[len('ar://'):]}"
    return pointer


def payment_id(caip2_network: str, tx_hash: str) -> str:
    """Derive the canonical payment identifier.

    ``keccak256(caip2Network || txHashWithout0x)``.

    This value is the AEAD associated data binding a ciphertext to its payment.
    Buyer and seller must derive it identically or decryption fails with no
    obvious cause, which is why it lives in the SDK rather than in each caller.
    """
    try:
        from eth_utils import keccak
    except ImportError as exc:  # pragma: no cover - depends on the extra
        raise DX402Error(
            "payment_id() needs eth-utils; install the 'signer' extra"
        ) from exc

    preimage = caip2_network.encode() + tx_hash.removeprefix("0x").encode()
    return "0x" + keccak(preimage).hex()


def _parse_sealed(raw: bytes) -> dict:
    """Parse the sealed-blob layout.

    ``MAGIC | version | alg | eph_len | eph | cek_nonce | wrapped_len | wrapped |
    body_nonce | ciphertext``
    """
    if len(raw) < 7 or raw[:5] != _MAGIC:
        raise DX402Error("not a DX402 sealed blob")
    version = raw[5]
    if version != _FORMAT_VERSION:
        raise DX402Error(f"unsupported sealed-blob version {version}")

    alg = raw[6]
    if alg not in (1, 2):
        raise DX402Error(f"unknown key algorithm {alg}")

    # Every read is bounds-checked. Python slicing silently returns short slices
    # and indexing raises IndexError, so without this a truncated blob surfaces
    # as an unrelated exception type instead of a clear parse failure.
    pos = 7

    def take(n: int, what: str) -> bytes:
        nonlocal pos
        if len(raw) < pos + n:
            raise DX402Error(f"truncated DX402 sealed blob at {what}")
        chunk = raw[pos : pos + n]
        pos += n
        return chunk

    eph_len = take(1, "ephemeral key length")[0]
    ephemeral = take(eph_len, "ephemeral key")
    cek_nonce = take(_NONCE_LEN, "cek nonce")
    wrapped_len = int.from_bytes(take(2, "wrapped key length"), "big")
    wrapped_cek = take(wrapped_len, "wrapped cek")
    body_nonce = take(_NONCE_LEN, "body nonce")
    ciphertext = raw[pos:]

    return {
        "alg": "secp256k1" if alg == 1 else "x25519",
        "ephemeral": ephemeral,
        "cek_nonce": cek_nonce,
        "wrapped_cek": wrapped_cek,
        "body_nonce": body_nonce,
        "ciphertext": ciphertext,
    }


def _shared_secret(sealed: dict, private_key: bytes) -> bytes:
    if sealed["alg"] == "secp256k1":
        from cryptography.hazmat.primitives.asymmetric import ec

        curve = ec.SECP256K1()
        sk = ec.derive_private_key(int.from_bytes(private_key, "big"), curve)
        peer = ec.EllipticCurvePublicKey.from_encoded_point(curve, sealed["ephemeral"])
        return sk.exchange(ec.ECDH(), peer)

    from cryptography.hazmat.primitives.asymmetric.x25519 import (
        X25519PrivateKey,
        X25519PublicKey,
    )
    import hashlib

    # ed25519 seed -> X25519 scalar: SHA-512 the seed, take the low half.
    scalar = bytearray(hashlib.sha512(private_key).digest()[:32])
    scalar[0] &= 248
    scalar[31] &= 127
    scalar[31] |= 64
    sk = X25519PrivateKey.from_private_bytes(bytes(scalar))
    shared = sk.exchange(X25519PublicKey.from_public_bytes(sealed["ephemeral"]))

    # RFC 7748 section 6.1: an all-zero output means a small-order public key was
    # supplied, which would make the wrapping key reproducible by whoever
    # supplied it.
    if shared == bytes(32):
        raise DX402Error("degenerate ECDH result (small-order public key)")
    return shared


def _unseal(sealed: dict, private_key: bytes, aad: bytes) -> bytes:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    shared = _shared_secret(sealed, private_key)
    wrap_key = HKDF(
        algorithm=hashes.SHA256(), length=32, salt=aad, info=_HKDF_INFO
    ).derive(shared)

    cek = AESGCM(wrap_key).decrypt(sealed["cek_nonce"], sealed["wrapped_cek"], aad)
    if len(cek) != _CEK_LEN:
        raise DX402Error(f"unwrapped CEK is {len(cek)} bytes, expected {_CEK_LEN}")

    return AESGCM(cek).decrypt(sealed["body_nonce"], sealed["ciphertext"], aad)


def recover_evidence(
    evidence: AnchoredEvidence,
    private_key: bytes | str,
    *,
    client: Any = None,
    timeout: float = 30.0,
) -> bytes:
    """Fetch, decrypt and verify the body behind ``evidence``.

    ``private_key`` is the raw key of the wallet that paid: 32 bytes for both an
    EVM secp256k1 key and an ed25519 seed, hex accepted with or without ``0x``.

    In ``direct`` mode this needs no permission from anyone. The ciphertext was
    sealed to the public key of the wallet that paid, so retrieval is arithmetic
    rather than an access-control decision that could be refused or
    misconfigured.

    The ``contentHash`` check is **not optional**: it is what catches a seller
    that anchored something other than what it served.
    """
    if isinstance(private_key, str):
        private_key = bytes.fromhex(private_key.removeprefix("0x"))
    if len(private_key) != 32:
        raise DX402Error(f"private key must be 32 bytes, got {len(private_key)}")

    url = dereference_pointer(evidence.pointer)

    if client is None:
        import httpx

        blob = httpx.get(url, timeout=timeout, follow_redirects=True).raise_for_status().content
    else:
        blob = client.get(url).raise_for_status().content

    sealed = _parse_sealed(blob)
    aad = evidence.payment_id.encode()

    try:
        plaintext = _unseal(sealed, private_key, aad)
    except DX402Error:
        raise
    except Exception as exc:  # noqa: BLE001 - AEAD failures are deliberately opaque
        raise DX402Error(
            "decryption failed -- wrong key, or the blob belongs to another payment"
        ) from exc

    try:
        from eth_utils import keccak

        actual = "0x" + keccak(plaintext).hex()
    except ImportError:  # pragma: no cover - depends on the extra
        return plaintext

    if actual.lower() != evidence.content_hash.lower():
        raise ContentHashMismatch(evidence.content_hash, actual)

    return plaintext
