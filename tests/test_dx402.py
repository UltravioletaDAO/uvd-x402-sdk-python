"""DX402 tests.

The decryption vectors here were produced by the **Rust** implementation in
x402-rs (`tests/dx402_vector_gen.rs`), not by this module. That is deliberate: a
vector generated and checked by the same code proves only that the code is
self-consistent. Three fabricated SHA-256 variants of ERC-8004 SEAL v1 passed CI
for months on exactly that mistake.

Regenerate with:
    cargo test --test dx402_vector_gen -- --nocapture emit_vectors
"""

import pytest

from uvd_x402_sdk.dx402 import (
    EVIDENCE_HEADER,
    AnchoredEvidence,
    DX402Error,
    EvidenceSkipped,
    _parse_sealed,
    _unseal,
    dereference_pointer,
    evidence_from_headers,
    parse_evidence_header,
)

# --- vectors produced by the Rust implementation -------------------------------

PAYMENT_ID = "0x1111111111111111111111111111111111111111111111111111111111111111"
BODY = b"the paid response that must outlive the session"
CONTENT_HASH = "0xfe8b2e5d48e880760dfcbfa8f794555810bb82b2e2b29138caab4bb36b58f748"

SECP256K1_PRIV = bytes.fromhex("42" * 32)
ED25519_SEED = bytes.fromhex("37" * 32)
ED25519_ADDRESS = "3znAGhp6Tk4kmebhXnk9K3jaTMffu82PJfEG91AeRkq2"


def _load(name: str) -> bytes:
    """Read a Rust-generated blob from tests/vectors/."""
    import pathlib

    path = pathlib.Path(__file__).parent / "vectors" / name
    if not path.exists():
        pytest.skip(f"{path} missing; regenerate with the Rust vector generator")
    return bytes.fromhex(path.read_text().strip())


def test_content_hash_matches_the_rust_implementation():
    """Both sides must derive the same hash or the integrity check is useless."""
    from eth_utils import keccak

    assert "0x" + keccak(BODY).hex() == CONTENT_HASH


def test_python_decrypts_what_rust_sealed_secp256k1():
    """Cross-implementation: Rust sealed it, Python opens it."""
    sealed = _parse_sealed(_load("secp256k1.hex"))
    assert sealed["alg"] == "secp256k1"
    assert len(sealed["ephemeral"]) == 33
    assert _unseal(sealed, SECP256K1_PRIV, PAYMENT_ID.encode()) == BODY


def test_python_decrypts_what_rust_sealed_ed25519():
    sealed = _parse_sealed(_load("ed25519.hex"))
    assert sealed["alg"] == "x25519"
    assert len(sealed["ephemeral"]) == 32
    assert _unseal(sealed, ED25519_SEED, PAYMENT_ID.encode()) == BODY


def test_the_wrong_payment_id_fails_to_decrypt():
    """paymentId is the AEAD associated data.

    Lifting a ciphertext from one payment and presenting it as the evidence for
    another must fail, or an anchor proves nothing about which transaction it
    belongs to.
    """
    sealed = _parse_sealed(_load("secp256k1.hex"))
    with pytest.raises(Exception):
        _unseal(sealed, SECP256K1_PRIV, b"0xa-different-payment")


def test_the_wrong_key_fails_to_decrypt():
    sealed = _parse_sealed(_load("secp256k1.hex"))
    with pytest.raises(Exception):
        _unseal(sealed, bytes.fromhex("11" * 32), PAYMENT_ID.encode())


# --- format and header handling ------------------------------------------------


def test_a_non_dx402_blob_is_rejected():
    with pytest.raises(DX402Error):
        _parse_sealed(b"")
    with pytest.raises(DX402Error):
        _parse_sealed(b"NOTDX402whatever")


def test_truncation_errors_rather_than_panicking():
    blob = _load("secp256k1.hex")
    for n in range(len(blob)):
        try:
            _parse_sealed(blob[:n])
        except DX402Error:
            pass
        except Exception as exc:  # noqa: BLE001
            raise AssertionError(f"truncation at {n} raised {type(exc).__name__}") from exc


def test_pointers_dereference_to_fetchable_urls():
    assert dereference_pointer("s3+https://e.example/a.dx402") == "https://e.example/a.dx402"
    assert dereference_pointer("ipfs://bafy1") == "https://ipfs.io/ipfs/bafy1"
    assert dereference_pointer("ar://tx1") == "https://arweave.net/tx1"
    # Unknown schemes pass through rather than being mangled.
    assert dereference_pointer("https://x.example/y") == "https://x.example/y"


def _encode(payload: dict) -> str:
    import base64
    import json

    return base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")


def test_a_skip_notice_is_reported_as_a_skip():
    """'The seller did not anchor' and 'the evidence is broken' differ."""
    header = _encode({"v": 1, "skipped": "too_large"})
    with pytest.raises(EvidenceSkipped) as exc:
        parse_evidence_header(header)
    assert exc.value.reason == "too_large"


def test_an_anchored_header_parses():
    header = _encode(
        {
            "v": 1,
            "paymentId": PAYMENT_ID,
            "pointer": "s3+https://e.example/a.dx402",
            "backend": "s3",
            "contentHash": CONTENT_HASH,
            "cipher": "AES-256-GCM",
            "keyAlg": "ECIES-secp256k1",
            "mode": "direct",
            "retention": "90d",
        }
    )
    evidence = parse_evidence_header(header)
    assert isinstance(evidence, AnchoredEvidence)
    assert evidence.payment_id == PAYMENT_ID
    assert evidence.is_end_to_end


def test_escrowed_mode_is_not_reported_as_end_to_end():
    """direct and escrowed make different confidentiality claims."""
    header = _encode(
        {
            "v": 1,
            "paymentId": PAYMENT_ID,
            "pointer": "s3+https://e.example/a.dx402",
            "contentHash": CONTENT_HASH,
            "keyAlg": "ECIES-secp256k1",
            "mode": "escrowed",
        }
    )
    assert not parse_evidence_header(header).is_end_to_end


def test_header_lookup_is_case_insensitive():
    """httpx and requests disagree about casing; HTTP does not care."""
    header = _encode({"v": 1, "skipped": "disabled"})
    for key in (EVIDENCE_HEADER, EVIDENCE_HEADER.lower(), EVIDENCE_HEADER.upper()):
        with pytest.raises(EvidenceSkipped):
            evidence_from_headers({key: header})


def test_a_missing_header_is_an_error():
    with pytest.raises(DX402Error):
        evidence_from_headers({})


def test_a_malformed_header_is_an_error():
    with pytest.raises(DX402Error):
        parse_evidence_header("!!!not base64!!!")


# ============================================================================
# Seller side -- sealing
# ============================================================================
#
# Note what these do NOT prove on their own: a seal/unseal round trip inside this
# module would pass even if the envelope format or the ed25519->X25519 map were
# wrong, because both halves would share the same mistake. The authoritative
# check is `tests/dx402_cross_seal.rs` in x402-rs, where the RUST implementation
# opens envelopes sealed here.


def test_seal_round_trips_secp256k1():
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    from uvd_x402_sdk.dx402 import seal_evidence

    sk = ec.derive_private_key(int.from_bytes(SECP256K1_PRIV, "big"), ec.SECP256K1())
    pub = sk.public_key().public_bytes(Encoding.X962, PublicFormat.CompressedPoint)

    blob = seal_evidence(BODY, pub, PAYMENT_ID)
    sealed = _parse_sealed(blob)
    assert sealed["alg"] == "secp256k1"
    assert len(sealed["ephemeral"]) == 33
    assert _unseal(sealed, SECP256K1_PRIV, PAYMENT_ID.encode()) == BODY


def test_seal_round_trips_x25519():
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    from uvd_x402_sdk.dx402 import payer_key_from_ed25519_pubkey, seal_evidence

    edsk = Ed25519PrivateKey.from_private_bytes(ED25519_SEED)
    edpub = edsk.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)

    blob = seal_evidence(BODY, payer_key_from_ed25519_pubkey(edpub), PAYMENT_ID)
    sealed = _parse_sealed(blob)
    assert sealed["alg"] == "x25519"
    assert len(sealed["ephemeral"]) == 32
    assert _unseal(sealed, ED25519_SEED, PAYMENT_ID.encode()) == BODY


def test_a_solana_address_derives_the_same_key_as_the_raw_pubkey():
    """On ed25519 chains the address IS the public key."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    from uvd_x402_sdk.dx402 import (
        payer_key_from_ed25519_pubkey,
        payer_key_from_solana_address,
    )

    edsk = Ed25519PrivateKey.from_private_bytes(ED25519_SEED)
    edpub = edsk.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    assert payer_key_from_solana_address(ED25519_ADDRESS) == payer_key_from_ed25519_pubkey(edpub)


def test_the_plaintext_never_appears_in_the_sealed_blob():
    """The blob is what lands in durable storage."""
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    from uvd_x402_sdk.dx402 import seal_evidence

    sk = ec.derive_private_key(int.from_bytes(SECP256K1_PRIV, "big"), ec.SECP256K1())
    pub = sk.public_key().public_bytes(Encoding.X962, PublicFormat.CompressedPoint)

    marker = b"SENSITIVE-MARKER-STRING"
    blob = seal_evidence(marker, pub, PAYMENT_ID)
    assert marker not in blob


def test_two_seals_of_the_same_body_differ():
    """Fresh CEK and nonces every time.

    Identical blobs would let anyone with read access to the store learn that two
    buyers received the same answer.
    """
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    from uvd_x402_sdk.dx402 import seal_evidence

    sk = ec.derive_private_key(int.from_bytes(SECP256K1_PRIV, "big"), ec.SECP256K1())
    pub = sk.public_key().public_bytes(Encoding.X962, PublicFormat.CompressedPoint)

    assert seal_evidence(BODY, pub, PAYMENT_ID) != seal_evidence(BODY, pub, PAYMENT_ID)


def test_a_wrong_sized_payer_key_is_rejected():
    from uvd_x402_sdk.dx402 import seal_evidence

    for bad in (b"", bytes(16), bytes(31), bytes(64)):
        with pytest.raises(DX402Error):
            seal_evidence(BODY, bad, PAYMENT_ID)


def test_seal_binds_the_payment_id():
    """A blob sealed for one payment must not open as evidence for another."""
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    from uvd_x402_sdk.dx402 import seal_evidence

    sk = ec.derive_private_key(int.from_bytes(SECP256K1_PRIV, "big"), ec.SECP256K1())
    pub = sk.public_key().public_bytes(Encoding.X962, PublicFormat.CompressedPoint)

    blob = seal_evidence(BODY, pub, PAYMENT_ID)
    with pytest.raises(Exception):
        _unseal(_parse_sealed(blob), SECP256K1_PRIV, b"0xa-different-payment")


def test_content_hash_matches_the_facilitator():
    from uvd_x402_sdk.dx402 import content_hash

    assert content_hash(BODY) == CONTENT_HASH
