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
    assert sealed["recipients"][0]["alg"] == "secp256k1"
    assert len(sealed["recipients"][0]["ephemeral"]) == 33
    assert _unseal(sealed, SECP256K1_PRIV, PAYMENT_ID.encode()) == BODY


def test_python_decrypts_what_rust_sealed_ed25519():
    sealed = _parse_sealed(_load("ed25519.hex"))
    assert sealed["recipients"][0]["alg"] == "x25519"
    assert len(sealed["recipients"][0]["ephemeral"]) == 32
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
    assert sealed["recipients"][0]["alg"] == "secp256k1"
    assert len(sealed["recipients"][0]["ephemeral"]) == 33
    assert _unseal(sealed, SECP256K1_PRIV, PAYMENT_ID.encode()) == BODY


def test_seal_round_trips_x25519():
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    from uvd_x402_sdk.dx402 import payer_key_from_ed25519_pubkey, seal_evidence

    edsk = Ed25519PrivateKey.from_private_bytes(ED25519_SEED)
    edpub = edsk.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)

    blob = seal_evidence(BODY, payer_key_from_ed25519_pubkey(edpub), PAYMENT_ID)
    sealed = _parse_sealed(blob)
    assert sealed["recipients"][0]["alg"] == "x25519"
    assert len(sealed["recipients"][0]["ephemeral"]) == 32
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


def test_a_v1_blob_reads_as_a_single_payer_recipient():
    """Backward compatibility, stated as a test.

    Every blob anchored before v2 existed is v1. If this ever stopped parsing,
    evidence already in the store would become unreadable.
    """
    from uvd_x402_sdk.dx402 import sealed_roles

    assert sealed_roles(_load("secp256k1.hex")) == ["payer"]


def test_roles_are_visible_without_decrypting():
    """A buyer must be able to see who else holds a key.

    Discovering afterwards that the seller could read the purchase would destroy
    the privacy property, so the roles are readable from the blob itself.
    """
    from uvd_x402_sdk.dx402 import sealed_roles

    roles = sealed_roles(_load("multi.hex")) if _has("multi.hex") else None
    if roles is None:
        pytest.skip("multi-recipient fixture not generated")
    assert roles[0] == "payer"
    assert "seller" in roles


def _has(name: str) -> bool:
    import pathlib

    return (pathlib.Path(__file__).parent / "vectors" / name).exists()


# --- multi-recipient (v2), sealed by RUST --------------------------------------

MULTI_BUYER_PRIV = bytes.fromhex("42" * 32)
MULTI_SELLER_PRIV = bytes.fromhex("55" * 32)


def test_python_opens_a_rust_sealed_bidirectional_envelope_as_the_buyer():
    sealed = _parse_sealed(_load("multi.hex"))
    assert len(sealed["recipients"]) == 2
    assert _unseal(sealed, MULTI_BUYER_PRIV, PAYMENT_ID.encode()) == BODY


def test_python_opens_a_rust_sealed_bidirectional_envelope_as_the_seller():
    """The property that did not exist before v2.

    The seller can now answer a false "that is not what you sent", which it
    could not do when the envelope was sealed to the buyer alone.
    """
    sealed = _parse_sealed(_load("multi.hex"))
    assert _unseal(sealed, MULTI_SELLER_PRIV, PAYMENT_ID.encode()) == BODY


def test_a_stranger_still_cannot_open_the_bidirectional_envelope():
    sealed = _parse_sealed(_load("multi.hex"))
    with pytest.raises(DX402Error):
        _unseal(sealed, bytes.fromhex("77" * 32), PAYMENT_ID.encode())


# --- reportado por KarmaKadabra, 2026-08-17 ------------------------------------


def test_an_empty_solana_address_is_refused():
    """An empty address used to produce a syntactically valid key.

    The pure-python base58 fallback left-padded a short decode up to 32 bytes,
    so `""` yielded `0100...` — a small-order Curve25519 point. Sealing to it did
    fail, but a layer later and as a generic "Error computing shared key", which
    points nowhere near the real cause.
    """
    from uvd_x402_sdk.dx402 import payer_key_from_solana_address

    for bad in ["", "   ", "1", "11"]:
        with pytest.raises(DX402Error):
            payer_key_from_solana_address(bad)


def test_the_seller_side_is_exported():
    """`from ... import *` used to bring back only the buyer half."""
    import uvd_x402_sdk.dx402 as m

    for name in (
        "seal_evidence",
        "content_hash",
        "sealed_roles",
        "payer_key_from_solana_address",
        "payer_key_from_ed25519_pubkey",
        "payer_key_from_evm_signature",
    ):
        assert name in m.__all__, f"{name} missing from __all__"


def test_python_can_now_emit_a_bidirectional_envelope():
    """The gap KarmaKadabra reported: reading v2 worked, writing it did not.

    A Python seller could open a bidirectional envelope but not produce one, so
    it could not keep a readable copy of what it delivered — which is the whole
    point of the seller slot.
    """
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    from uvd_x402_sdk.dx402 import ROLE_PAYER, ROLE_SELLER, seal_evidence_to, sealed_roles

    def pub(priv: bytes) -> bytes:
        sk = ec.derive_private_key(int.from_bytes(priv, "big"), ec.SECP256K1())
        return sk.public_key().public_bytes(Encoding.X962, PublicFormat.CompressedPoint)

    seller_priv = bytes.fromhex("55" * 32)
    blob = seal_evidence_to(
        BODY,
        [(ROLE_PAYER, pub(SECP256K1_PRIV)), (ROLE_SELLER, pub(seller_priv))],
        PAYMENT_ID,
    )

    assert sealed_roles(blob) == ["payer", "seller"]
    sealed = _parse_sealed(blob)
    assert _unseal(sealed, SECP256K1_PRIV, PAYMENT_ID.encode()) == BODY
    assert _unseal(sealed, seller_priv, PAYMENT_ID.encode()) == BODY


def test_a_single_payer_envelope_is_still_v1():
    """Nothing already anchored may become unreadable."""
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    from uvd_x402_sdk.dx402 import ROLE_PAYER, seal_evidence, seal_evidence_to

    sk = ec.derive_private_key(int.from_bytes(SECP256K1_PRIV, "big"), ec.SECP256K1())
    pub = sk.public_key().public_bytes(Encoding.X962, PublicFormat.CompressedPoint)

    assert seal_evidence_to(BODY, [(ROLE_PAYER, pub)], PAYMENT_ID)[5] == 1
    assert seal_evidence(BODY, pub, PAYMENT_ID)[5] == 1


def test_anchor_digest_matches_the_facilitator():
    """Pinned against digests emitted by the facilitator's Rust implementation.

    A digest built slightly differently raises nothing — it yields a signature
    that simply never verifies, and the seller's anchor silently stays
    provisional with no clue why. Only agreement with the verifier establishes
    that they match.
    """
    from uvd_x402_sdk.dx402 import ZERO_ADDRESS, anchor_digest

    evm = anchor_digest(
        "0x" + "11" * 32,
        "0x" + "22" * 32,
        "s3+https://e/x",
        "0x34033041a5944B8F10f8E4D8496Bfb84f1A293A8",
        8453,
    )
    assert "0x" + evm.hex() == (
        "0x5a5d45bf6334e5b5b26edc03861670b937bb9ec7272d8db3babdbfed0f32207c"
    )

    ed = anchor_digest("0x" + "11" * 32, "0x" + "22" * 32, "", ZERO_ADDRESS, 0)
    assert "0x" + ed.hex() == (
        "0x2b931af6cd412f7884f8b84812a30381f946e7f61135b602129c15ad68afa8ed"
    )


def test_anchor_digest_rejects_malformed_input():
    from uvd_x402_sdk.dx402 import ZERO_ADDRESS, anchor_digest

    with pytest.raises(DX402Error):
        anchor_digest("0x00", "0x" + "22" * 32, "", ZERO_ADDRESS, 0)
    with pytest.raises(DX402Error):
        anchor_digest("0x" + "11" * 32, "0x00", "", ZERO_ADDRESS, 0)
    with pytest.raises(DX402Error):
        anchor_digest("0x" + "11" * 32, "0x" + "22" * 32, "", "0x00", 0)


def test_signing_an_anchor_produces_something_the_facilitator_accepts():
    """The full seller path, end to end within reach of a unit test.

    The authoritative check is `tests/dx402_anchor_sig_cross.rs` in x402-rs,
    where Rust verifies these exact signatures.
    """
    from uvd_x402_sdk.dx402 import sign_anchor_ed25519, sign_anchor_evm

    sig = sign_anchor_ed25519(ED25519_SEED, "0x" + "11" * 32, "0x" + "22" * 32)
    assert sig.startswith("0x") and len(sig) == 2 + 128  # 64 bytes

    evm = sign_anchor_evm(
        SECP256K1_PRIV,
        "0x" + "11" * 32,
        "0x" + "22" * 32,
        "s3+https://e/x",
        "0x17c5185167401eD00cF5F5b2fc97D9BBfDb7D025",
        8453,
    )
    assert evm.startswith("0x") and len(evm) == 2 + 130  # 65 bytes


def test_anchor_evidence_never_raises():
    """Evidence is an addition to the payment path, never a gate in front of it.

    An unreachable facilitator must cost the receipt, not the sale.
    """
    from uvd_x402_sdk.dx402 import anchor_evidence

    result = anchor_evidence(
        b"body",
        payment_id_value=PAYMENT_ID,
        network="solana",
        tx_hash="abc",
        payer="p",
        payee="q",
        payer_key=bytes(32),
        facilitator="http://127.0.0.1:1",
    )
    assert result["skipped"] == "anchor_failed"


def test_a_custodial_seller_signs_without_exposing_the_seed():
    """`signer` is a callable, not a key.

    A custodian receives the digest and returns the signature; the seed never
    leaves it. That is the difference between a custodial seller whose anchors
    stay provisional forever and one that can claim them.
    """
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    from uvd_x402_sdk.dx402 import ZERO_ADDRESS, anchor_digest

    seen = {}

    def custodian(digest: bytes) -> str:
        seen["digest"] = digest
        return "0x" + Ed25519PrivateKey.from_private_bytes(ED25519_SEED).sign(digest).hex()

    expected = anchor_digest(PAYMENT_ID, CONTENT_HASH, "", ZERO_ADDRESS, 0)
    sig = custodian(expected)
    assert seen["digest"] == expected
    assert len(bytes.fromhex(sig[2:])) == 64


def test_evidence_header_round_trips():
    from uvd_x402_sdk.dx402 import evidence_header

    header = evidence_header({"v": 1, "skipped": "too_large"})
    with pytest.raises(EvidenceSkipped):
        parse_evidence_header(header)
