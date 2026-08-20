"""Reaching `verified: true` -- the rung only the chain can grant.

Run:  python examples/dx402/verified_anchor.py

Since facilitator 1.87.0 a signature alone does NOT make an anchor final, and
the reason is worth understanding before you copy this.

`verified` used to be decided by checking the seller signature against the
`payee` field IN THE REQUEST -- a value the caller supplies. So proving "I
control the address I typed into my own request" was enough to own a stranger's
evidence permanently. `paymentId` is keccak256 over public data, so any observer
of a settlement could compute it and race the real seller.

Now `verified` comes only from the on-chain gate, which checks the signature
against the payee it READ OFF THE CHAIN -- and checks that the proof is a proof
of THIS payment, not merely of some payment.
"""

import json

from uvd_x402_sdk import anchor_evidence


def main() -> None:
    body = b'{"result":"ok"}'
    tx = "0x" + "cd" * 32

    result = anchor_evidence(
        body,
        payment_id_value="0x" + "ab" * 32,
        network="base",
        tx_hash=tx,
        payer="0x" + "11" * 20,
        payee="0x" + "22" * 20,
        payer_key=bytes(range(32)),
        # THIS is what reaches rung 2. Without it the facilitator has checked no
        # chain, so it records the anchor as provisional and says so.
        proof_of_payment={
            "transactionHash": tx,
            "blockNumber": 21_000_000,
            "network": "base",
            "payer": "0x" + "11" * 20,
            "payee": "0x" + "22" * 20,
            # The NET the payee actually receives. Execution Market payments
            # carry TWO Transfers -- a fee and the net -- and declaring the
            # gross gets you `proof_transfer_not_found`.
            "amount": "1000000",
            "token": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",  # USDC on Base
            "timestamp": 1_787_000_000,
            "paymentHash": "0x" + "ef" * 32,
        },
        signer=lambda digest: "0x" + "11" * 65,  # a custodian would sign here
        client=_StubFacilitator(),
    )

    print(json.dumps(result, indent=2))
    print()
    print("verified:", result.get("verified"))
    print()
    print("Rungs, and what each one actually proves:")
    print("  2 verified  the CHAIN says this address is the payee   -> final")
    print("  1 signed    the claimant controls the address it named -> supersedable by rung 2")
    print("  0 neither   anyone could have written this             -> supersedable by 1 or 2")
    print()
    print("On Solana rung 2 is not reachable yet: the gate cannot read that")
    print("payment, so the honest answer there is `signed: true`.")


class _StubFacilitator:
    status_code = 201

    def post(self, _url, json=None, **_kw):
        self._sent = json or {}
        return self

    def json(self):
        # A real facilitator verifies the proof on-chain before answering this.
        return {
            "v": 1,
            "paymentId": self._sent.get("paymentId"),
            "pointer": "s3+https://facilitator.test/dx402/blob/x",
            "verified": True,
            "signed": True,
        }


if __name__ == "__main__":
    main()
