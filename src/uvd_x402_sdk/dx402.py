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
#: One recipient (the payer). Still emitted for that case, so readers already
#: deployed keep working.
_FORMAT_V1 = 1
#: Several recipients. A v2 blob is a positive signal that somebody besides the
#: payer can open it.
_FORMAT_V2 = 2

#: Roles a recipient can hold, in wire order.
ROLE_PAYER, ROLE_SELLER, ROLE_AUDITOR = 0, 1, 2
_ROLE_NAMES = {ROLE_PAYER: "payer", ROLE_SELLER: "seller", ROLE_AUDITOR: "auditor"}
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
    """Parse the sealed-blob layout. Accepts v1 and v2.

    v1: ``MAGIC | 1 | alg | ephLen | eph | cekNonce | wrappedLen | wrapped |
        bodyNonce | ciphertext``
    v2: ``MAGIC | 2 | count | count x (role | alg | ephLen | eph | cekNonce |
        wrappedLen | wrapped) | bodyNonce | ciphertext``

    Returns ``{"recipients": [...], "body_nonce": ..., "ciphertext": ...}``.
    Every read is bounds-checked, so a truncated blob is a clear parse failure
    rather than an unrelated exception type.
    """
    pos = 0

    def take(n: int, what: str) -> bytes:
        nonlocal pos
        if len(raw) < pos + n:
            raise DX402Error(f"truncated DX402 sealed blob at {what}")
        chunk = raw[pos : pos + n]
        pos += n
        return chunk

    if take(len(_MAGIC), "magic") != _MAGIC:
        raise DX402Error("not a DX402 sealed blob")

    version = take(1, "version")[0]
    if version == _FORMAT_V1:
        count = 1
    elif version == _FORMAT_V2:
        count = take(1, "recipient count")[0]
    else:
        raise DX402Error(f"unsupported sealed-blob version {version}")
    if count == 0:
        # An envelope nobody can open is not evidence.
        raise DX402Error("DX402 sealed blob has no recipients")

    recipients = []
    for _ in range(count):
        role = ROLE_PAYER if version == _FORMAT_V1 else take(1, "role")[0]
        alg = take(1, "alg")[0]
        if alg not in (1, 2):
            raise DX402Error(f"unknown key algorithm {alg}")
        eph_len = take(1, "ephemeral key length")[0]
        recipients.append(
            {
                "role": role,
                "role_name": _ROLE_NAMES.get(role, f"unknown({role})"),
                "alg": "secp256k1" if alg == 1 else "x25519",
                "ephemeral": take(eph_len, "ephemeral key"),
                "cek_nonce": take(_NONCE_LEN, "cek nonce"),
                "wrapped_cek": take(
                    int.from_bytes(take(2, "wrapped key length"), "big"), "wrapped cek"
                ),
            }
        )

    body_nonce = take(_NONCE_LEN, "body nonce")
    return {
        "recipients": recipients,
        "body_nonce": body_nonce,
        "ciphertext": raw[pos:],
    }


def sealed_roles(raw: bytes) -> list[str]:
    """Who can open this blob.

    Worth surfacing: a buyer has to be able to see that the seller -- or a
    designated auditor -- also holds a key to what they bought.
    """
    return [r["role_name"] for r in _parse_sealed(raw)["recipients"]]


def _shared_secret(sealed: dict, private_key: bytes) -> bytes:
    """`sealed` here is ONE recipient slot, not the whole envelope."""
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
    """Open an envelope with whichever recipient slot belongs to `private_key`.

    Tries every slot on a matching curve: a holder does not necessarily know
    which one is theirs, and in a multi-recipient envelope the payer is not
    always first. A slot that does not open is skipped, not reported -- "that
    one was not for me" is not an error.
    """
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    want = "secp256k1" if len(private_key) == 32 else None  # both are 32 bytes
    saw_candidate = False

    for recipient in sealed["recipients"]:
        try:
            shared = _shared_secret(recipient, private_key)
        except DX402Error:
            raise
        except Exception:
            continue

        saw_candidate = True
        wrap_key = HKDF(
            algorithm=hashes.SHA256(), length=32, salt=aad, info=_HKDF_INFO
        ).derive(shared)
        try:
            cek = AESGCM(wrap_key).decrypt(
                recipient["cek_nonce"], recipient["wrapped_cek"], aad
            )
        except Exception:
            continue  # not our slot

        if len(cek) != _CEK_LEN:
            raise DX402Error(f"unwrapped CEK is {len(cek)} bytes, expected {_CEK_LEN}")
        return AESGCM(cek).decrypt(sealed["body_nonce"], sealed["ciphertext"], aad)

    _ = (want, saw_candidate)
    raise DX402Error(
        "no recipient slot opened -- wrong key, or the blob belongs to another payment"
    )


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


# ============================================================================
# Seller side -- sealing and anchoring
# ============================================================================
#
# The buyer half above recovers evidence. This half PRODUCES it, and it belongs
# to whoever holds the plaintext: the resource server, right after settlement.
#
# The facilitator is deliberately not involved here. It only ever sees /verify
# and /settle, never a response body, so sealing cannot happen there -- see
# docs/plans/dx402/00-RESEARCH.md in x402-rs.

_P25519 = 2**255 - 19


def _ed25519_pubkey_to_x25519(pubkey: bytes) -> bytes:
    """Map an ed25519 public key to its X25519 (Montgomery u) form.

    Birational map: ``u = (1 + y) / (1 - y) mod p``.

    An ed25519 public key is a compressed Edwards point -- little-endian ``y``
    with the sign bit of ``x`` in the top bit, which is discarded here because
    the u-coordinate does not depend on it.
    """
    if len(pubkey) != 32:
        raise DX402Error(f"ed25519 public key must be 32 bytes, got {len(pubkey)}")

    y = int.from_bytes(pubkey, "little") & ((1 << 255) - 1)

    denom = (1 - y) % _P25519
    if denom == 0:
        # y == 1 is the identity element, and it has no Montgomery u.
        raise DX402Error("degenerate ed25519 public key (identity element)")

    u = ((1 + y) * pow(denom, _P25519 - 2, _P25519)) % _P25519
    return u.to_bytes(32, "little")


def payer_key_from_solana_address(address: str) -> bytes:
    """Derive the encryption target from a Solana (or Fogo) address.

    On ed25519 chains the address **is** the public key, so this needs no
    signature and no lookup -- which is what makes those chains the cheapest
    case for DX402.
    """
    try:
        import base58
    except ImportError:
        # base58 is a small pure-python decode; avoid a dependency for it.
        alphabet = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
        num = 0
        for ch in address:
            idx = alphabet.find(ch)
            if idx < 0:
                raise DX402Error(f"invalid base58 character {ch!r} in address")
            num = num * 58 + idx
        raw = num.to_bytes(32, "big") if num.bit_length() <= 256 else b""
        leading = len(address) - len(address.lstrip("1"))
        decoded = b"\x00" * leading + raw.lstrip(b"\x00")
        decoded = decoded.rjust(32, b"\x00")
        return _ed25519_pubkey_to_x25519(decoded)
    return _ed25519_pubkey_to_x25519(base58.b58decode(address))


def payer_key_from_ed25519_pubkey(pubkey: bytes) -> bytes:
    """Derive the encryption target from a raw ed25519 public key.

    Covers NEAR access keys, Stellar ``G...`` addresses and Algorand addresses
    once decoded to their 32 raw bytes.
    """
    return _ed25519_pubkey_to_x25519(pubkey)


def payer_key_from_evm_signature(signature: bytes | str, digest: bytes) -> bytes:
    """Recover an EVM payer's secp256k1 public key from their payment signature.

    ``digest`` is the EIP-712 digest the payer actually signed -- for an
    EIP-3009 authorization, the ``TransferWithAuthorization`` hash under the
    token's own domain.

    Getting that digest wrong does not raise: it recovers a *different, perfectly
    valid* public key, and the body would be sealed to a stranger while every log
    line said success. The token's EIP-712 domain name varies per chain and even
    flips between a chain's mainnet and testnet, so derive it from the same table
    the facilitator uses rather than assuming.

    Returns the SEC1-compressed public key (33 bytes).
    """
    try:
        from eth_keys import KeyAPI
    except ImportError as exc:
        raise DX402Error(
            "payer_key_from_evm_signature() needs eth-keys; install the 'dx402' extra"
        ) from exc

    if isinstance(signature, str):
        signature = bytes.fromhex(signature.removeprefix("0x"))
    if len(signature) != 65:
        raise DX402Error(f"signature must be 65 bytes, got {len(signature)}")

    v = signature[64]
    if v in (27, 28):
        v -= 27
    elif v >= 35:
        v = (v - 35) % 2
    if v not in (0, 1):
        raise DX402Error(f"invalid recovery id {signature[64]}")

    keys = KeyAPI()
    sig = keys.Signature(vrs=(v, int.from_bytes(signature[:32], "big"),
                              int.from_bytes(signature[32:64], "big")))
    pub = sig.recover_public_key_from_msg_hash(digest)

    # eth-keys hands back the uncompressed 64-byte X||Y form; compress it to the
    # 33-byte SEC1 encoding the wire format uses.
    raw = pub.to_bytes()
    x, y = raw[:32], raw[32:]
    prefix = b"\x03" if y[-1] & 1 else b"\x02"
    return prefix + x


def seal_evidence(
    body: bytes,
    payer_key: bytes,
    payment_id_value: str,
) -> bytes:
    """Seal ``body`` so that only the holder of the payer's private key can read it.

    ``payer_key`` is either a 33-byte SEC1-compressed secp256k1 key (EVM, XRPL)
    or a 32-byte X25519 key from one of the ``payer_key_from_*`` helpers.

    ``payment_id_value`` is bound in as AEAD associated data, which is what stops
    a ciphertext from being replayed as the evidence for a different payment.
    Derive it with :func:`payment_id` so both sides agree; deriving it
    differently makes decryption fail with no obvious cause.

    Returns the bytes to upload. Nothing here talks to the network.
    """
    import os

    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    aad = payment_id_value.encode()
    cek = os.urandom(_CEK_LEN)
    body_nonce = os.urandom(_NONCE_LEN)
    cek_nonce = os.urandom(_NONCE_LEN)

    ciphertext = AESGCM(cek).encrypt(body_nonce, body, aad)

    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    if len(payer_key) == 33:
        from cryptography.hazmat.primitives.asymmetric import ec

        alg_byte = 1
        curve = ec.SECP256K1()
        eph = ec.generate_private_key(curve)
        peer = ec.EllipticCurvePublicKey.from_encoded_point(curve, payer_key)
        shared = eph.exchange(ec.ECDH(), peer)
        ephemeral = eph.public_key().public_bytes(
            encoding=Encoding.X962, format=PublicFormat.CompressedPoint
        )
    elif len(payer_key) == 32:
        from cryptography.hazmat.primitives.asymmetric.x25519 import (
            X25519PrivateKey,
            X25519PublicKey,
        )

        alg_byte = 2
        eph = X25519PrivateKey.generate()
        shared = eph.exchange(X25519PublicKey.from_public_bytes(payer_key))
        ephemeral = eph.public_key().public_bytes(
            encoding=Encoding.Raw, format=PublicFormat.Raw
        )

        # RFC 7748 section 6.1. A small-order payer key would drive the shared
        # secret to a constant that whoever supplied that key could reproduce.
        if shared == bytes(32):
            raise DX402Error("degenerate ECDH result (small-order public key)")
    else:
        raise DX402Error(
            f"payer key must be 33 bytes (secp256k1) or 32 (X25519), got {len(payer_key)}"
        )

    wrap_key = HKDF(
        algorithm=hashes.SHA256(), length=32, salt=aad, info=_HKDF_INFO
    ).derive(shared)
    wrapped_cek = AESGCM(wrap_key).encrypt(cek_nonce, cek, aad)

    out = bytearray()
    out += _MAGIC
    out.append(_FORMAT_V1)
    out.append(alg_byte)
    out.append(len(ephemeral))
    out += ephemeral
    out += cek_nonce
    out += len(wrapped_cek).to_bytes(2, "big")
    out += wrapped_cek
    out += body_nonce
    out += ciphertext
    return bytes(out)


def content_hash(body: bytes) -> str:
    """keccak256 of a body, ``0x``-prefixed.

    Over the **plaintext**, deliberately. Over the ciphertext it would only prove
    the blob was not corrupted in storage; over the plaintext it proves the blob
    decrypts to exactly what was delivered -- the check that catches a seller
    anchoring something other than what it served.
    """
    try:
        from eth_utils import keccak
    except ImportError as exc:
        raise DX402Error("content_hash() needs eth-utils; install the 'dx402' extra") from exc
    return "0x" + keccak(body).hex()
