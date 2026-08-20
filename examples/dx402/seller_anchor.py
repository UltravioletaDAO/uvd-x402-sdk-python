"""Seller side: seal a paid response and anchor it, in one call.

Run:  python examples/dx402/seller_anchor.py

Nothing here talks to a real facilitator -- a stub stands in for it, so this
file runs in CI and rots loudly instead of silently. Point `facilitator=` at a
real one to use it for real.
"""

import json
import os

from uvd_x402_sdk import anchor_evidence, evidence_header


def main() -> None:
    # What the seller has after settlement: the body it is about to deliver,
    # and the payer's public key. The key is not registered anywhere -- it comes
    # out of the payment signature itself, which is the whole idea.
    body = b'{"forecast":"rain","confidence":0.82}'
    payer_key = os.urandom(32)  # X25519, 32 bytes. In real life: derived from the payment.

    result = anchor_evidence(
        body,
        payment_id_value="0x" + "ab" * 32,
        network="base",
        tx_hash="0x" + "cd" * 32,
        payer="0x" + "11" * 20,
        payee="0x" + "22" * 20,
        payer_key=payer_key,
        # Optional. Omit and the facilitator picks its default; ask
        # available_backends() what a given one offers.
        storage="s3",
        client=_StubFacilitator(),
    )

    # It NEVER raises. Every failure comes back as a skip, because evidence is
    # an addition to the payment path and must never be a gate in front of it.
    if result.get("skipped"):
        print(f"no evidence this time: {result['skipped']} "
              f"(status={result.get('status')}, error={result.get('error')})")
        print("the sale still went through -- that is the point")
        return

    print("anchored:", json.dumps(result, indent=2)[:300])
    print()
    print("verified:", result.get("verified"), "  <- the chain confirmed authorship")
    print("signed  :", result.get("signed"), "  <- your signature was accepted")
    if not result.get("verified"):
        print("reason  :", result.get("notVerifiedReason"))
        print("          send proof_of_payment to reach verified -- see verified_anchor.py")

    # Attach this to the response the buyer receives.
    print()
    print("X-Durable-Evidence:", evidence_header(result)[:64], "...")


class _StubFacilitator:
    """Stands in for the facilitator so this example runs offline."""

    status_code = 201

    def post(self, _url, json=None, **_kw):
        self._sent = json or {}
        return self

    def json(self):
        return {
            "v": 1,
            "paymentId": self._sent.get("paymentId"),
            "pointer": f"s3+https://facilitator.test/dx402/blob/{self._sent.get('paymentId')}",
            "backend": self._sent.get("storage", "s3"),
            "contentHash": self._sent.get("contentHash"),
            "verified": False,
            "signed": False,
            "notVerifiedReason": "dx402_proof_missing",
        }


if __name__ == "__main__":
    main()
