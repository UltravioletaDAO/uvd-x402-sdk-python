"""DX402 pointer mode: the seller brings its own storage.

The facilitator has accepted two shapes since v0.1 (`src/dx402/types.rs`,
`AnchorRequest.pointer` / `.sealed`, dispatched in `src/dx402/service.rs`:
`sealed` present -> it hosts and derives the pointer; `sealed` absent and
`pointer` present -> it uses the seller's; neither -> error). This SDK only ever
sent `sealed`, so every seller integrating through it had the response body
travelling inside the anchor request and could not use storage it already owned.

`upload` closes that. It is a CALLABLE for the same reason `signer` is one: the
SDK owns the sealing, so it is the only thing that can hand the seller the exact
bytes the pointer has to resolve to.

The load-bearing detail: the seller signature is over the pointer AS SENT
(`gate.rs` `verify_authorization(..., claim.pointer, ...)`, fed from
`pointer: req.pointer...unwrap_or("")`). Signing "" while sending a pointer
raises nothing and produces an anchor that stays silently provisional -- which
is exactly the hijack a signed anchor exists to prevent.
"""

import base64

import pytest

from uvd_x402_sdk.dx402 import (
    ANCHOR_MAX_REQUEST_BYTES,
    _seller_digest_for,
    anchor_digest,
    anchor_evidence,
)

PAYER_KEY = bytes.fromhex("11" * 32)          # x25519-shaped -> ECIES-X25519
PAYMENT_ID = "0x" + "22" * 32
TX_HASH = "0x" + "33" * 32
PAYER = "0x" + "44" * 20
PAYEE = "0x" + "55" * 20


class _Recorder:
    """Stands in for httpx: records the anchor request, answers success."""

    def __init__(self, status=200, body=None):
        self.status = status
        self.body = body if body is not None else {"v": 1, "pointer": "s3+https://x/y"}
        self.payload = None

    def post(self, url, json=None):
        self.payload = json
        return self

    # httpx.Response surface used by anchor_evidence
    @property
    def status_code(self):
        return self.status

    def json(self):
        return self.body


def _anchor(**kw):
    client = kw.pop("client")
    return anchor_evidence(
        b"the paid response",
        payment_id_value=PAYMENT_ID,
        network="base",
        tx_hash=TX_HASH,
        payer=PAYER,
        payee=PAYEE,
        payer_key=PAYER_KEY,
        client=client,
        **kw,
    )


pytest.importorskip("cryptography")
pytest.importorskip("eth_utils")


# ---------------------------------------------------------------------------
# the two modes
# ---------------------------------------------------------------------------


def test_without_upload_the_ciphertext_still_rides_in_the_request():
    """The existing behaviour is untouched -- this is an additive option."""
    rec = _Recorder()
    _anchor(client=rec)

    assert "sealed" in rec.payload
    assert "pointer" not in rec.payload


def test_upload_sends_a_pointer_and_no_ciphertext():
    seen = {}

    def upload(sealed: bytes) -> str:
        seen["bytes"] = sealed
        return "https://my-bucket.example/evidence/abc"

    rec = _Recorder()
    _anchor(client=rec, upload=upload)

    assert "sealed" not in rec.payload, "the whole point: the blob never travels"
    assert rec.payload["pointer"] == "https://my-bucket.example/evidence/abc"
    assert seen["bytes"], "the SDK still seals -- the buyer has to decrypt it"


def test_the_uploaded_bytes_are_the_sealed_envelope_the_buyer_will_open():
    """Handing back anything else would anchor a receipt for unreadable bytes.

    The buyer runs `_parse_sealed` on whatever the pointer resolves to, so the
    bytes given to `upload` must be exactly that envelope -- not the plaintext,
    and not a re-encoded copy.
    """
    from uvd_x402_sdk.dx402 import _parse_sealed

    captured = {}

    def upload(blob: bytes) -> str:
        captured["blob"] = blob
        return "https://sink.example/p"

    rec = _Recorder()
    _anchor(client=rec, upload=upload)

    envelope = _parse_sealed(captured["blob"])
    assert envelope["recipients"], "sealed to at least the payer"
    assert captured["blob"] != b"the paid response", "never the plaintext"

    # and it is the SAME envelope the sealed-mode branch would have posted
    rec2 = _Recorder()
    _anchor(client=rec2)
    posted = base64.b64decode(rec2.payload["sealed"])
    assert _parse_sealed(posted)["recipients"][0]["alg"] == envelope["recipients"][0]["alg"]


def test_a_pointer_mode_anchor_is_not_measured_against_the_request_bound():
    """The bound is about the REQUEST. With `upload` the blob is not in it."""
    big = b"x" * (ANCHOR_MAX_REQUEST_BYTES * 4)

    rec = _Recorder()
    result = anchor_evidence(
        big,
        payment_id_value=PAYMENT_ID,
        network="base",
        tx_hash=TX_HASH,
        payer=PAYER,
        payee=PAYEE,
        payer_key=PAYER_KEY,
        upload=lambda b: "https://sink.example/blob",
        client=rec,
    )

    assert result.get("skipped") != "too_large"
    assert rec.payload["pointer"] == "https://sink.example/blob"


def test_the_same_body_without_upload_is_skipped_as_too_large():
    """The pair: the bound is real, it just does not apply to pointer mode."""
    big = b"x" * (ANCHOR_MAX_REQUEST_BYTES * 4)

    rec = _Recorder()
    result = anchor_evidence(
        big,
        payment_id_value=PAYMENT_ID,
        network="base",
        tx_hash=TX_HASH,
        payer=PAYER,
        payee=PAYEE,
        payer_key=PAYER_KEY,
        client=rec,
    )

    assert result == {"v": 1, "skipped": "too_large"}
    assert rec.payload is None, "nothing should have been posted"


# ---------------------------------------------------------------------------
# the signature must cover the pointer that is actually sent
# ---------------------------------------------------------------------------


def test_the_seller_signature_is_over_the_pointer_as_sent():
    """Signing "" while sending a pointer produces an anchor that never verifies.

    It raises nothing, so only a test can catch it.
    """
    pointer = "https://sink.example/evidence/xyz"
    signed = {}

    def signer(digest: bytes) -> str:
        signed["digest"] = digest
        return "0x" + "ee" * 65

    rec = _Recorder()
    _anchor(client=rec, upload=lambda b: pointer, signer=signer)

    content_hash = rec.payload["contentHash"]
    expected = anchor_digest(PAYMENT_ID, content_hash, pointer, PAYEE, 8453)
    wrong = anchor_digest(PAYMENT_ID, content_hash, "", PAYEE, 8453)

    assert signed["digest"] == expected
    assert signed["digest"] != wrong


def test_sealed_mode_still_signs_the_empty_pointer():
    """You cannot sign a value the facilitator has not issued yet."""
    signed = {}
    rec = _Recorder()
    _anchor(client=rec, signer=lambda d: signed.setdefault("digest", d) and "0x" or "0x00")

    content_hash = rec.payload["contentHash"]
    assert signed["digest"] == anchor_digest(PAYMENT_ID, content_hash, "", PAYEE, 8453)


def test_seller_digest_for_defaults_to_the_sealed_form():
    """Backwards compatible: the new parameter is optional."""
    a = _seller_digest_for(PAYMENT_ID, "0x" + "66" * 32, PAYEE, "base")
    b = _seller_digest_for(PAYMENT_ID, "0x" + "66" * 32, PAYEE, "base", "")
    assert a == b


def test_an_ed25519_payee_also_signs_over_the_real_pointer():
    """The curve chooses the address/chain form; the pointer is orthogonal."""
    solana_payee = "3znAGhp6Tk4kmebhXnk9K3jaTMffu82PJfEG91AeRkq2"
    ptr = "ipfs://bafyfake"

    got = _seller_digest_for(PAYMENT_ID, "0x" + "66" * 32, solana_payee, "solana", ptr)
    assert got == anchor_digest(PAYMENT_ID, "0x" + "66" * 32, ptr, "0x" + "00" * 20, 0)
    assert got != anchor_digest(PAYMENT_ID, "0x" + "66" * 32, "", "0x" + "00" * 20, 0)


# ---------------------------------------------------------------------------
# an upload failure degrades to a skip -- never to a failed sale
# ---------------------------------------------------------------------------


def test_an_upload_that_raises_becomes_a_skip():
    def upload(_):
        raise OSError("bucket unreachable")

    rec = _Recorder()
    result = _anchor(client=rec, upload=upload)

    assert result["skipped"] == "anchor_failed"
    assert rec.payload is None, "nothing anchored when the bytes never landed"


@pytest.mark.parametrize("bad", [None, "", "   ", 42, b"bytes"])
def test_an_upload_that_returns_no_usable_pointer_becomes_a_skip(bad):
    """A receipt pointing at bytes nobody can fetch is worse than no receipt."""
    rec = _Recorder()
    result = _anchor(client=rec, upload=lambda b: bad)

    assert result["skipped"] == "anchor_failed"
    assert rec.payload is None


def test_a_transient_anchor_failure_is_marked_retryable():
    """A 5xx from /dx402/anchor is no verdict, like everywhere else."""
    rec = _Recorder(status=503, body={"error": "no writer", "reason": "holder_unknown"})
    result = _anchor(client=rec)

    assert result["skipped"] == "anchor_failed"
    assert result["retryable"] is True
    assert result["reason"] == "holder_unknown"


def test_a_rejected_signature_is_not_marked_retryable():
    rec = _Recorder(status=422, body={"error": "dx402_signature_not_verified"})
    result = _anchor(client=rec)

    assert result["skipped"] == "anchor_failed"
    assert "retryable" not in result
    assert result["error"] == "dx402_signature_not_verified"


# ---------------------------------------------------------------------------
# the backend label
# ---------------------------------------------------------------------------


def test_backend_defaults_to_s3_and_can_be_set():
    rec = _Recorder()
    _anchor(client=rec)
    assert rec.payload["backend"] == "s3"

    rec2 = _Recorder()
    _anchor(client=rec2, upload=lambda b: "ipfs://cid", backend="ipfs")
    assert rec2.payload["backend"] == "ipfs"


def test_the_pointer_the_seller_supplies_is_resolvable_by_the_buyer():
    """The buyer half dereferences the pointer verbatim, so it must be fetchable."""
    from uvd_x402_sdk.dx402 import dereference_pointer

    assert dereference_pointer("https://sink.example/b") == "https://sink.example/b"
    assert dereference_pointer("mystore+https://sink.example/b") == "https://sink.example/b"
    assert dereference_pointer("ipfs://cid").endswith("/ipfs/cid")


def test_sealed_payload_is_still_base64_in_sealed_mode():
    """Regression guard on the branch that did not change."""
    rec = _Recorder()
    _anchor(client=rec)
    base64.b64decode(rec.payload["sealed"])
