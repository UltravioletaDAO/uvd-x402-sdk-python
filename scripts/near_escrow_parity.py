"""Compare actual Python/TypeScript Arras bytes, signatures and wire payments.

Run after npm ci in the TypeScript SDK checkout; needs its H-6 implementation.
No generated expectations: both runtimes consume the frozen Rust vectors.
"""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

from uvd_x402_sdk import near_escrow as sdk


def check(ts_root: Path, node: str = "node") -> None:
    root = Path(__file__).resolve().parents[1]
    fixture = root / "tests/fixtures/near-escrow.json"
    vectors = json.loads(fixture.read_text(encoding="utf-8"))
    result = subprocess.run(
        [node, str(root / "scripts/near_escrow_parity.mjs"), str(ts_root.resolve())],
        input=json.dumps(vectors),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
    )
    if result.returncode:
        raise RuntimeError("TypeScript parity runner failed: " + result.stderr)
    actual = json.loads(result.stdout)
    assert actual["minGas"] == str(sdk.NEAR_ESCROW_MIN_GAS)
    assert actual["maxGas"] == str(sdk.NEAR_ESCROW_MAX_GAS)
    expected = []
    for v in vectors["payment_id"]["vectores"]:
        network = (
            "near:mainnet"
            if v["token"] == sdk.NEAR_ESCROW_TOKENS["near:mainnet"]
            else "near:testnet"
        )
        options = sdk.NearEscrowDelegateOptions(
            network=network,
            signer=sdk.NearEd25519Signer(bytes(range(32))),
            sender_id=v["payer"],
            escrow_contract="arras.near" if network == "near:mainnet" else "arras.testnet",
            nonce="7",
            max_block_height="100000000",
        )
        lifecycle = []
        for method, args in (
            ("release", {"payment_id": v["payment_id"]}),
            ("release", {"payment_id": v["payment_id"], "amount": "400000"}),
            ("refund", {"payment_id": v["payment_id"]}),
            ("reclaim", {"payment_id": v["payment_id"]}),
            ("withdraw", {}),
            ("withdraw_for", {"account": v["receiver"]}),
        ):
            lifecycle.append(sdk.sign_escrow_lifecycle(options, method, args))
        expected.append(
            dict(
                id=v["id"],
                msg=sdk.build_escrow_msg(v),
                preimage=sdk.payment_id_preimage(v).hex(),
                paymentId=sdk.compute_payment_id(v),
                authorization=sdk.sign_escrow_authorization(
                    options, token=v["token"], amount=v["amount"], msg=v
                ),
                lifecycle=lifecycle,
            )
        )
    assert actual["results"] == expected, "NEAR wire bytes differ between Python and TypeScript"
    print(
        "NEAR parity: V-1..V-3 messages, preimages, ids and 21 signed delegates are byte-identical"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ts-root", required=True, type=Path)
    parser.add_argument("--node", default="node")
    args = parser.parse_args()
    check(args.ts_root, args.node)
