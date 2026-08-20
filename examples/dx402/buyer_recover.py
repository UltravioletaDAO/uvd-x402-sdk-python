"""Buyer side: come back with a transaction hash and get the bytes you paid for.

Run:  python examples/dx402/buyer_recover.py

The point of DX402: this needs permission from nobody. The ciphertext was sealed
to the public key of the wallet that paid, so recovery is arithmetic, not an
access-control decision somebody could refuse or misconfigure.
"""

from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from uvd_x402_sdk import AnchoredEvidence, ContentHashMismatch, content_hash, recover_evidence, seal_evidence


def main() -> None:
    # The buyer's payment key. The same one that signed the payment.
    buyer_private = bytes(range(1, 33))
    sk = ec.derive_private_key(int.from_bytes(buyer_private, "big"), ec.SECP256K1())
    buyer_public = sk.public_key().public_bytes(Encoding.X962, PublicFormat.CompressedPoint)

    payment_id = "0x" + "ab" * 32
    delivered = b'{"forecast":"rain","confidence":0.82}'

    # --- what the seller did, months ago --------------------------------
    blob = seal_evidence(delivered, buyer_public, payment_id)

    evidence = AnchoredEvidence(
        payment_id=payment_id,
        pointer="mem://evidence",
        backend="s3",
        content_hash=content_hash(delivered),
        cipher="AES-256-GCM",
        key_alg="ECIES-secp256k1",
        mode="direct",
        retention="90d",
    )

    # --- what the buyer does now ----------------------------------------
    recovered = recover_evidence(evidence, buyer_private, client=_StoredBlob(blob))
    print("recovered:", recovered.decode())
    assert recovered == delivered

    # The contentHash check is NOT optional and NOT decoration: it is the only
    # thing that catches a seller who anchored something other than what it
    # served. `recover_evidence` runs it for you and raises on mismatch.
    tampered = AnchoredEvidence(**{**evidence.__dict__, "content_hash": "0x" + "00" * 32})
    try:
        recover_evidence(tampered, buyer_private, client=_StoredBlob(blob))
        print("BUG: a mismatched contentHash must not pass")
    except ContentHashMismatch as e:
        print("tampering caught:", str(e)[:70])

    # And nobody else can open it -- not the facilitator, not us.
    try:
        recover_evidence(evidence, bytes(range(33, 65)), client=_StoredBlob(blob))
        print("BUG: another wallet must not decrypt")
    except Exception as e:
        print("another wallet cannot open it:", str(e)[:60])


class _StoredBlob:
    """Stands in for wherever the blob lives (S3, IPFS, anywhere)."""

    status_code = 200

    def __init__(self, blob: bytes) -> None:
        self.content = blob

    def get(self, _url, **_kw):
        return self

    def raise_for_status(self):
        return self


if __name__ == "__main__":
    main()
