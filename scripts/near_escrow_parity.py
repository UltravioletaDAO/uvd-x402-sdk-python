"""Compare actual Python/TypeScript Arras bytes, signatures and wire payments.

Run after npm ci in the TypeScript SDK checkout; needs its H-6 implementation.
No generated expectations: both runtimes consume the frozen Rust vectors.
"""

from __future__ import annotations

import argparse
import json
import subprocess
from dataclasses import replace
from pathlib import Path

from uvd_x402_sdk import near_escrow as sdk


def check(ts_root: Path, node: str = "node") -> None:
    root = Path(__file__).resolve().parents[1]
    fixture = root / "tests/fixtures/near-escrow.json"
    vectors = json.loads(fixture.read_text(encoding="utf-8"))
    v1, _, v3 = vectors["payment_id"]["vectores"]
    cases = []
    for method in ("authorize", "release", "refund", "reclaim", "withdraw", "withdraw_for"):
        for network, contract, nonce, height in (
            ("near:mainnet", "evil-sink.near", "7", "100000000"),
            ("near:mainnet", "arras-v1b-1101.testnet", "7", "100000000"),
            ("near:mainnet", "arras.near", "7", "100000000"),
            ("near:testnet", "evil-sink.testnet", "7", "100000000"),
            ("near:testnet", "arras-v1b-1101.testnet", "100000000", "100"),
            ("near:testnet", "arras-v1b-1101.testnet", "100000001", "100"),
        ):
            v = v3 if network == "near:mainnet" else v1
            cases.append(
                dict(
                    method=method,
                    network=network,
                    contract=contract,
                    nonce=nonce,
                    height=height,
                    vector=v,
                )
            )
    result = subprocess.run(
        [node, str(root / "scripts/near_escrow_parity.mjs"), str(ts_root.resolve())],
        input=json.dumps(dict(vectors=vectors, cases=cases)),
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
        network = "near:testnet"
        options = sdk.NearEscrowDelegateOptions(
            network=network,
            signer=sdk.NearEd25519Signer(bytes(range(32))),
            sender_id=v["payer"],
            escrow_contract=sdk.NEAR_ESCROW_CONTRACTS[network][0],
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
                    options,
                    token=sdk.NEAR_ESCROW_TOKENS[network],
                    amount=int(v["amount"]),
                    msg={
                        **v,
                        "release_deadline": int(v["release_deadline"]),
                        "reclaim_after": int(v["reclaim_after"]),
                    },
                ),
                lifecycle=lifecycle,
            )
        )
    assert actual["results"] == expected, "NEAR wire bytes differ between Python and TypeScript"
    assert actual["contracts"] == {key: list(row) for key, row in sdk.NEAR_ESCROW_CONTRACTS.items()}

    class Spy:
        touched = False

        @property
        def public_key(self):
            self.touched = True
            return bytes(32)

        def sign(self, digest):
            self.touched = True
            return bytes(64)

    refusals = []
    for c in cases:
        spy = Spy()
        o = replace(
            options,
            network=c["network"],
            escrow_contract=c["contract"],
            nonce=c["nonce"],
            max_block_height=c["height"],
            signer=spy,
        )
        try:
            if c["method"] == "authorize":
                sdk.sign_escrow_authorization(
                    o, token=c["vector"]["token"], amount=c["vector"]["amount"], msg=c["vector"]
                )
            else:
                args = (
                    {"account": v1["receiver"]}
                    if c["method"] == "withdraw_for"
                    else {} if c["method"] == "withdraw" else {"payment_id": v1["payment_id"]}
                )
                sdk.sign_escrow_lifecycle(o, c["method"], args)
        except ValueError:
            refusals.append(not spy.touched)
        else:
            refusals.append(False)
    assert actual["refusals"] == refusals == [True] * len(cases)
    print(
        "NEAR parity: V-1..V-3 messages, preimages, ids and 21 signed testnet delegates "
        f"are byte-identical; registry and {len(cases)} pre-signature refusals match"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ts-root", required=True, type=Path)
    parser.add_argument("--node", default="node")
    args = parser.parse_args()
    check(args.ts_root, args.node)
