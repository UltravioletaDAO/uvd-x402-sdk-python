"""Smoke test for the Solana rater-authored feedback rail, against the live facilitator.

Exercises `prepare` -> sign -> (would submit) end to end with an **ephemeral
rater**: a keypair generated in this process, holding nothing, discarded when
it exits. `POST /feedback/solana/prepare` writes nothing on-chain and costs
nothing -- it reads the registry's collection pubkey and a blockhash -- so this
is safe to run against production.

**It deliberately stops before `submit`.** That call is an on-chain write the
facilitator pays for. Pass `--submit` only if you mean to spend its fee and put
a rating on-chain, and only with a rater you control.

    python examples/solana_feedback_smoke.py
    python examples/solana_feedback_smoke.py --agent <asset-pubkey>

Needs `pip install 'uvd-x402-sdk[solana]'`.
"""

import argparse
import asyncio
import os
import sys

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from uvd_x402_sdk.erc8004 import Erc8004Client, supports_solana_feedback
from uvd_x402_sdk.solana_signing import (
    Ed25519Signer,
    _b58decode,
    decode_solana_transaction,
    sign_solana_feedback_transaction,
)

FACILITATOR = os.environ.get(
    "X402_FACILITATOR_URL", "https://facilitator.ultravioletadao.xyz"
)
# An agent that exists on Solana. Override with --agent to rate a real one.
DEFAULT_AGENT = "247Y4QLwz9ZbcuHR2nX2EQLZHCsMs1GTqvgd6fpdn85Q"


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agent", default=DEFAULT_AGENT, help="agent asset pubkey")
    parser.add_argument("--network", default="solana", choices=["solana", "solana-devnet"])
    parser.add_argument(
        "--submit",
        action="store_true",
        help="actually send it: an on-chain write the facilitator pays for",
    )
    args = parser.parse_args()

    if not supports_solana_feedback(args.network):
        print(f"[FAIL] {args.network} is not on the Solana feedback rail")
        return 1

    # Ephemeral: no funds, no file, gone when the process exits.
    signer = Ed25519Signer(os.urandom(32))
    print(f"facilitator : {FACILITATOR}")
    print(f"network     : {args.network}")
    print(f"agent       : {args.agent}")
    print(f"rater       : {signer.pubkey}  (ephemeral, holds nothing)")

    feedback = dict(
        agent_id=args.agent,
        rater=signer.pubkey,
        value=87,
        value_decimals=0,
        score=95,
        tag1="quality",
        tag2="smoke",
    )

    async with Erc8004Client(base_url=FACILITATOR) as client:
        prep = await client.prepare_solana_feedback(network=args.network, **feedback)
        if not prep.success:
            print(f"[FAIL] prepare: {prep.error}")
            return 1

        tx = decode_solana_transaction(prep.transaction)
        rater_index = tx.signer_index(signer.pubkey)
        print()
        print(f"fee payer   : {prep.fee_payer}  (account 0, the facilitator)")
        print(f"blockhash   : {prep.blockhash}  (valid to height {prep.last_valid_block_height})")
        print(f"accounts    : {len(tx.account_keys)}")
        print(f"signers     : {tx.num_required_signatures}")
        print(f"rater slot  : {rater_index}")
        print(f"unsigned    : {sum(1 for s in tx.signatures if set(s) == {0})} empty slots")

        if prep.fee_payer == prep.rater:
            print("[FAIL] the facilitator put itself in the rater's slot")
            return 1

        signed = sign_solana_feedback_transaction(prep.transaction, signer.pubkey, signer)
        after = decode_solana_transaction(signed)

        # The three checks the facilitator runs before it co-signs.
        if after.message != tx.message:
            print("[FAIL] signing changed the message; submit would be refused")
            return 1
        if after.signatures[0] != b"\x00" * 64:
            print("[FAIL] the fee payer's slot is not ours to fill")
            return 1
        Ed25519PublicKey.from_public_bytes(_b58decode(signer.pubkey)).verify(
            after.signatures[rater_index], after.message
        )

        print()
        print("[OK] message unchanged, rater slot signed, fee payer slot left empty")
        print("[OK] signature verifies over the message, as the facilitator will check")

        if not args.submit:
            print()
            print("stopping before submit (on-chain write, facilitator pays the fee).")
            print("re-run with --submit and a rater you control to actually rate.")
            return 0

        result = await client.submit_solana_feedback(
            network=args.network, transaction=signed, **feedback
        )
        if not result.success:
            print(f"[FAIL] submit: {result.error}")
            return 1
        print(f"[OK] submitted: {result.transaction}")
        return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
