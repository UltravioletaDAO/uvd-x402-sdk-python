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
    "ANCHOR_MAX_REQUEST_BYTES",
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
    # Seller side. Omitted at first, so `from ... import *` brought back only
    # the buyer half of the module -- reported by KarmaKadabra, 2026-08-17.
    "seal_evidence",
    "seal_evidence_to",
    "content_hash",
    "anchor_digest",
    "sign_anchor_ed25519",
    "sign_anchor_evm",
    "ZERO_ADDRESS",
    "anchor_evidence",
    "evidence_header",
    "ROLE_PAYER",
    "ROLE_SELLER",
    "ROLE_AUDITOR",
    "sealed_roles",
    "payer_key_from_solana_address",
    "payer_key_from_ed25519_pubkey",
    "payer_key_from_evm_signature",
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

    Rejects anything that does not decode to exactly 32 bytes. The pure-python
    fallback below used to left-pad a short decode up to 32, so an **empty
    address produced a syntactically valid key** (``0100...``) instead of an
    error. That key is a small-order Curve25519 point, so sealing to it did fail
    -- but a layer later, as a generic "Error computing shared key" that points
    nowhere near the real cause. Reported by KarmaKadabra, 2026-08-17.
    """
    if not address or not address.strip():
        raise DX402Error("empty Solana address")
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
        if num.bit_length() > 256:
            raise DX402Error("Solana address decodes to more than 32 bytes")
        raw = num.to_bytes(32, "big")
        leading = len(address) - len(address.lstrip("1"))
        decoded = b"\x00" * leading + raw.lstrip(b"\x00")
        # Left-padding here is what hid an empty address. A real Solana address
        # is 32 bytes; anything shorter is malformed, not something to pad.
        if len(decoded) != 32:
            raise DX402Error(
                f"Solana address decodes to {len(decoded)} bytes, expected 32"
            )
        return _ed25519_pubkey_to_x25519(decoded)

    decoded = base58.b58decode(address)
    if len(decoded) != 32:
        raise DX402Error(f"Solana address decodes to {len(decoded)} bytes, expected 32")
    return _ed25519_pubkey_to_x25519(decoded)


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


def seal_evidence_to(
    body: bytes,
    recipients: "list[tuple[int, bytes]]",
    payment_id_value: str,
) -> bytes:
    """Seal `body` so every listed recipient can read it, and nobody else.

    `recipients` is a list of `(role, public_key)` where role is
    :data:`ROLE_PAYER`, :data:`ROLE_SELLER` or :data:`ROLE_AUDITOR`.

    The body is encrypted **once**; only the content key is wrapped per
    recipient, so adding the seller costs about sixty bytes rather than a second
    copy of the payload. That is what makes it practical for a seller to keep a
    readable copy of what it delivered — and answer a false "that is not what
    you sent" — instead of paying to anchor evidence it cannot open.

    A single payer recipient is emitted as format **v1, byte-for-byte**, so
    nothing already anchored becomes unreadable and readers still on v1 keep
    working.
    """
    if not recipients:
        raise DX402Error("an envelope with no recipients could never be opened")

    import os

    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    aad = payment_id_value.encode()
    cek = os.urandom(_CEK_LEN)
    body_nonce = os.urandom(_NONCE_LEN)
    ciphertext = AESGCM(cek).encrypt(body_nonce, body, aad)

    wrapped = []
    for role, key in recipients:
        alg_byte, ephemeral, shared = _ecdh_to(key)
        wrap_key = HKDF(
            algorithm=hashes.SHA256(), length=32, salt=aad, info=_HKDF_INFO
        ).derive(shared)
        cek_nonce = os.urandom(_NONCE_LEN)
        wrapped.append(
            (role, alg_byte, ephemeral, cek_nonce, AESGCM(wrap_key).encrypt(cek_nonce, cek, aad))
        )

    out = bytearray()
    out += _MAGIC
    single_payer = len(wrapped) == 1 and wrapped[0][0] == ROLE_PAYER
    if single_payer:
        out.append(_FORMAT_V1)
    else:
        out.append(_FORMAT_V2)
        out.append(len(wrapped))

    for role, alg_byte, ephemeral, cek_nonce, wrapped_cek in wrapped:
        if not single_payer:
            out.append(role)
        out.append(alg_byte)
        out.append(len(ephemeral))
        out += ephemeral
        out += cek_nonce
        out += len(wrapped_cek).to_bytes(2, "big")
        out += wrapped_cek

    out += body_nonce
    out += ciphertext
    return bytes(out)


def _ecdh_to(public_key: bytes):
    """One ephemeral ECDH against `public_key`. Returns (alg_byte, eph_pub, shared)."""
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    if len(public_key) == 33:
        from cryptography.hazmat.primitives.asymmetric import ec

        curve = ec.SECP256K1()
        eph = ec.generate_private_key(curve)
        peer = ec.EllipticCurvePublicKey.from_encoded_point(curve, public_key)
        return (
            1,
            eph.public_key().public_bytes(Encoding.X962, PublicFormat.CompressedPoint),
            eph.exchange(ec.ECDH(), peer),
        )

    if len(public_key) == 32:
        from cryptography.hazmat.primitives.asymmetric.x25519 import (
            X25519PrivateKey,
            X25519PublicKey,
        )

        eph = X25519PrivateKey.generate()
        shared = eph.exchange(X25519PublicKey.from_public_bytes(public_key))
        # RFC 7748 section 6.1: a small-order key would drive the shared secret
        # to a constant that whoever supplied it could reproduce.
        if shared == bytes(32):
            raise DX402Error("degenerate ECDH result (small-order public key)")
        return (
            2,
            eph.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw),
            shared,
        )

    raise DX402Error(
        f"public key must be 33 bytes (secp256k1) or 32 (X25519), got {len(public_key)}"
    )


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


# ============================================================================
# Anchor authorization -- proving the anchor is yours
# ============================================================================
#
# An anchor carrying a valid payee signature is `verified` and final. One
# without is provisional: it still blocks a duplicate, but a verified anchor for
# the same payment supersedes it.
#
# That asymmetry exists because the paymentId claim is permanent. Without it,
# whoever anchored first owned the evidence of a payment forever and the real
# seller was locked out. Reproduced against production by KarmaKadabra,
# 2026-08-18.

#: EIP-712 domain, per the facilitator's `authorization_digest`.
_ANCHOR_DOMAIN_NAME = "DX402 Anchor"
_ANCHOR_DOMAIN_VERSION = "1"

_ANCHOR_TYPE = (
    b"Dx402AnchorAuthorization(bytes32 paymentId,bytes32 contentHash,"
    b"string pointer,address payee)"
)
_EIP712_DOMAIN_TYPE = b"EIP712Domain(string name,string version,uint256 chainId)"


def _keccak(data: bytes) -> bytes:
    try:
        from eth_utils import keccak
    except ImportError as exc:  # pragma: no cover
        raise DX402Error("needs eth-utils; install the 'dx402' extra") from exc
    return keccak(data)


def anchor_digest(
    payment_id: str,
    content_hash: str,
    pointer: str,
    payee: str,
    chain_id: int,
) -> bytes:
    """The 32-byte digest a seller signs to prove an anchor is theirs.

    One canonical message across every curve. `payee` is the EVM address for a
    secp256k1 payee, and the **zero address** for an ed25519 one — an ed25519
    address does not fit the `address` field, and the binding is already
    established by which key verifies the signature.

    `pointer` is whatever you send in the anchor, or the **empty string** when
    you send `sealed` and the facilitator issues the pointer itself: you cannot
    sign a value you have not seen, and in that case the pointer is derived from
    the paymentId, which is already covered.

    Getting this wrong does not raise — it produces a signature that simply never
    verifies, and the anchor stays provisional with no clue why. That is why the
    tests pin it against digests emitted by the facilitator's own Rust
    implementation rather than recomputed here.
    """

    def b32(value: str, field: str) -> bytes:
        raw = bytes.fromhex(value.removeprefix("0x"))
        if len(raw) != 32:
            raise DX402Error(f"{field} must be 32 bytes, got {len(raw)}")
        return raw

    addr = bytes.fromhex(payee.removeprefix("0x"))
    if len(addr) != 20:
        raise DX402Error(f"payee must be a 20-byte address, got {len(addr)}")

    domain_separator = _keccak(
        _keccak(_EIP712_DOMAIN_TYPE)
        + _keccak(_ANCHOR_DOMAIN_NAME.encode())
        + _keccak(_ANCHOR_DOMAIN_VERSION.encode())
        + chain_id.to_bytes(32, "big")
    )

    struct_hash = _keccak(
        _keccak(_ANCHOR_TYPE)
        + b32(payment_id, "paymentId")
        + b32(content_hash, "contentHash")
        + _keccak(pointer.encode())
        + b"\x00" * 12
        + addr
    )

    return _keccak(b"\x19\x01" + domain_separator + struct_hash)


#: The zero address, for the ed25519 form of the digest.
ZERO_ADDRESS = "0x" + "00" * 20


def sign_anchor_ed25519(
    private_key: bytes,
    payment_id: str,
    content_hash: str,
    pointer: str = "",
) -> str:
    """Sign an anchor authorization with a Solana / Stellar ed25519 key.

    A Solana payee cannot produce an EIP-712 signature at all — its address is
    an ed25519 key — so requiring one would leave that chain unable to prove
    authorship even once the on-chain gate is enforced. This closes it today,
    with no RPC.

    `chainId` is 0 and `payee` is the zero address for the ed25519 form; the
    facilitator derives the same digest.
    """
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    except ImportError as exc:  # pragma: no cover
        raise DX402Error("needs cryptography; install the 'dx402' extra") from exc

    if len(private_key) != 32:
        raise DX402Error(f"ed25519 seed must be 32 bytes, got {len(private_key)}")

    digest = anchor_digest(payment_id, content_hash, pointer, ZERO_ADDRESS, 0)
    sk = Ed25519PrivateKey.from_private_bytes(private_key)
    return "0x" + sk.sign(digest).hex()


def sign_anchor_evm(
    private_key: bytes,
    payment_id: str,
    content_hash: str,
    pointer: str,
    payee: str,
    chain_id: int,
) -> str:
    """Sign an anchor authorization with an EVM secp256k1 key.

    `payee` must be the address that received the payment — the facilitator
    recovers the signer and compares.
    """
    try:
        from eth_keys import KeyAPI
    except ImportError as exc:  # pragma: no cover
        raise DX402Error("needs eth-keys; install the 'dx402' extra") from exc

    digest = anchor_digest(payment_id, content_hash, pointer, payee, chain_id)
    sig = KeyAPI().PrivateKey(private_key).sign_msg_hash(digest)
    return "0x" + sig.to_bytes().hex()


# ============================================================================
# The whole seller side, in one call
# ============================================================================



def _is_evm_address(value: str) -> bool:
    """`0x` + 40 hex. An ed25519 payee (Solana, Stellar) never matches."""
    v = (value or "").strip()
    if not v.startswith("0x") or len(v) != 42:
        return False
    try:
        int(v[2:], 16)
    except ValueError:
        return False
    return True


def _chain_id_for(network: str) -> "int | None":
    """The chain id behind a network name or CAIP-2 id, or None if unknown."""
    # `eip155:8453` carries the chain id literally -- read it rather than going
    # through the network table, which has no entry for any EVM TESTNET
    # (measured 2026-08-19: base-sepolia, avalanche-fuji, polygon-amoy and
    # others all resolve to None). Without this, a seller on a testnet cannot
    # produce a verifiable anchor signature at all.
    if network and network.lower().startswith("eip155:"):
        tail = network.split(":", 1)[1]
        if tail.isdigit():
            return int(tail)
    try:
        from uvd_x402_sdk.networks import get_network, parse_caip2_network

        name = network
        if ":" in (network or ""):
            parsed = parse_caip2_network(network)
            name = parsed if isinstance(parsed, str) else getattr(parsed, "name", network)
        cfg = get_network(name)
        chain_id = getattr(cfg, "chain_id", None)
        return int(chain_id) if chain_id else None
    except Exception:  # noqa: BLE001 - an unknown network just means no EVM form
        return None


def _seller_digest_for(
    payment_id_value: str, content_hash_value: str, payee: str, network: str
) -> "bytes | None":
    """The digest the facilitator will ACTUALLY verify, chosen by the payee's curve.

    The gate dispatches on the payee and checks **one** form -- never both. So a
    secp256k1 payee is verified against the digest built with its REAL address and
    chain id, while an ed25519 payee (whose address does not fit the `address`
    field) is verified against the zero address and chain id 0.

    Signing the wrong form raises nothing. It produces a signature that never
    verifies, the anchor silently stays **provisional**, and a provisional anchor
    is superseded by anyone else's -- which is the hijack the signed anchor exists
    to prevent. Reproduced against production by KarmaKadabra, 2026-08-19: with
    everything else identical, the ed25519 form was refused
    (``409 dx402_already_anchored``) and the EVM form superseded the provisional.

    `pointer` stays the empty string on both branches: this call sends `sealed`,
    so the facilitator issues the pointer and you cannot sign what you have not
    seen.
    """
    if _is_evm_address(payee):
        chain_id = _chain_id_for(network)
        if not chain_id:
            # An EVM payee whose chain id we cannot resolve. Falling through to
            # the ed25519 form here is how this exact bug shipped in 0.53.0: it
            # raises nothing and produces a signature that never verifies, so
            # the anchor stays provisional forever with no error anywhere.
            # Measured 2026-08-19: `base-sepolia`, `xdc` and `sei` all resolve
            # to None, so every seller on them was signing the wrong form.
            #
            # Refuse instead. The caller anchors unsigned, which is honest and
            # recoverable, rather than signed-but-worthless, which looks done.
            return None
        return anchor_digest(payment_id_value, content_hash_value, "", payee, chain_id)
    return anchor_digest(payment_id_value, content_hash_value, "", ZERO_ADDRESS, 0)


#: Largest `POST /dx402/anchor` request the facilitator accepts, mirroring its
#: `MAX_REQUEST_BODY_BYTES` (default 64 KiB, an anti-OOM bound on every route).
#: With base64 inflation and ~600 bytes of metadata this leaves ~47 KB of
#: plaintext.
ANCHOR_MAX_REQUEST_BYTES = 64 * 1024


def anchor_evidence(
    body: bytes,
    *,
    payment_id_value: str,
    network: str,
    tx_hash: str,
    payer: str,
    payee: str,
    payer_key: bytes,
    seller_encryption_key: "bytes | None" = None,
    signer: "callable | None" = None,
    retention: str = "90d",
    facilitator: str = "https://facilitator.ultravioletadao.xyz",
    timeout: float = 15.0,
    client: "object | None" = None,
) -> dict:
    """Seal a response body, anchor it, and return the `X-Durable-Evidence` value.

    One call for the whole seller side. It seals, signs, and posts; you attach
    the result to the response.

    **It never raises.** Every failure returns a skip notice, because evidence is
    an addition to the payment path and must never be a gate in front of it — an
    unreachable facilitator or an unsealable body has to cost the receipt, never
    the sale. Check `result.get("skipped")` if you want to know.

    - `seller_encryption_key`: your **public** key, to keep a readable copy so you
      can answer a false "that is not what you sent". It does **not** have to be
      your payment key, and should not be — a custodial payment wallet works fine
      here because this key only ever decrypts.
    - `signer`: `f(digest: bytes) -> str` returning a `0x`-prefixed signature.
      Taking a callable rather than a private key is what lets a custodian sign:
      it receives the digest and returns the signature without the seed ever
      leaving it. Without a signer the anchor is **provisional** — it holds the
      slot but a signed anchor for the same payment supersedes it.
    """
    try:
        recipients = [(ROLE_PAYER, payer_key)]
        if seller_encryption_key:
            recipients.append((ROLE_SELLER, seller_encryption_key))
        blob = seal_evidence_to(body, recipients, payment_id_value)

        digest_hash = content_hash(body)
        payload = {
            "paymentId": payment_id_value,
            "network": network,
            "txHash": tx_hash,
            "payer": payer,
            "payee": payee,
            "sealed": base64.b64encode(blob).decode(),
            "backend": "s3",
            "contentHash": digest_hash,
            "keyAlg": "ECIES-X25519" if len(payer_key) == 32 else "ECIES-secp256k1",
            "mode": "direct",
            "retention": retention,
        }

        unsigned_reason = None
        if signer is not None:
            digest = _seller_digest_for(payment_id_value, digest_hash, payee, network)
            if digest is None:
                # See `_seller_digest_for`: signing the wrong form is worse than
                # not signing, because it looks like the seller did its part.
                unsigned_reason = "unknown_chain_id"
            else:
                payload["sellerSignature"] = signer(digest)

        # Measure the SEALED, serialised request -- not the plaintext.
        # The envelope adds a nonce, the wrapped CEK and its JSON, and the
        # ciphertext travels base64 (4/3). Checking the plaintext lets through
        # bodies the facilitator then rejects, which arrives as a generic
        # failure long after the work of sealing was done.
        # Measured by KarmaKadabra, 2026-08-19: 47 KB of plaintext fits, 48 KB
        # does not.
        if len(json.dumps(payload).encode()) > ANCHOR_MAX_REQUEST_BYTES:
            return {"v": 1, "skipped": "too_large"}

        if client is None:
            import httpx

            response = httpx.post(
                f"{facilitator.rstrip('/')}/dx402/anchor", json=payload, timeout=timeout
            )
        else:
            response = client.post(f"{facilitator.rstrip('/')}/dx402/anchor", json=payload)

        # Carry the facilitator's own diagnosis out rather than flattening every
        # failure to "anchor_failed". A rejected signature answers 422
        # `dx402_signature_not_verified`; erasing that here would reproduce, one
        # layer down, the exact problem that code exists to solve.
        if response.status_code >= 400:
            detail = {}
            try:
                detail = response.json()
            except Exception:  # noqa: BLE001 - a non-JSON error body is still a failure
                pass
            return {
                "v": 1,
                "skipped": "anchor_failed",
                "status": response.status_code,
                "error": detail.get("error"),
            }
        result = response.json()
        if unsigned_reason and isinstance(result, dict):
            result["unsigned"] = unsigned_reason
        return result
    except Exception:  # noqa: BLE001 - a failure here must never fail the sale
        return {"v": 1, "skipped": "anchor_failed"}


def evidence_header(evidence: dict) -> str:
    """Encode an anchor result for the ``X-Durable-Evidence`` response header."""
    return (
        base64.urlsafe_b64encode(json.dumps(evidence).encode()).decode().rstrip("=")
    )
