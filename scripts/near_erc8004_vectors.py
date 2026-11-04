"""Record public NEAR ERC-8004 views and historical outcomes; never send transactions.

Run explicitly with --record. Normal tests only consume the checked-in file.
Requests are serialized and spaced by at least 1.1 seconds across both networks.
Volatile query block metadata is excluded. JSON uses two spaces, UTF-8 and LF.
"""

from __future__ import annotations

import argparse
import base64
import json
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests/fixtures/near-erc8004.json"
NETWORKS = {
    "mainnet": {
        "identity": "identity-registry.erc-8004.near",
        "reputation": "reputation-registry.erc-8004.near",
        "verifier": "erc8004-verifier.arras-escrow.near",
        "payer": "fe0a574258f8277099875e46d4ca0043cdb6274ff2501b375a0d3d64bc211079",
        "worker": "826e8c640ba0ee77bf255ecb332dfaca73fc1e3aef2dbf370b5ffeb30f9ac7af",
        "payment_id": "EbkEtGaM7ZwY6aHSdHSqxdFx9cJV13KPT7fYBWjxbfd9",
        "transactions": [
            ("register", "EFEP8ySmwnySWwtc3FeezvETBR1GadLt4ZQGyydu76uQ"),
            ("nft_transfer", "5LX8yTQUvMD7uYSmKg4TQQ2HFPAjJmtiSRFFoVU67s4"),
            ("give_feedback_with_proof", "Cxcj2Bo1ZkhAeBiu9bFaKY2iFvsPrYy9bBzvYyVHWTfg"),
        ],
    },
    "testnet": {
        "identity": "identity-registry.erc-8004.testnet",
        "reputation": "reputation-registry.erc-8004.testnet",
        "verifier": "erc8004-verifier.arras-dev.testnet",
        "payer": "payer-demo.arras-dev.testnet",
        "worker": "worker-demo.arras-dev.testnet",
        "payment_id": "5RbdKfx4giehMpctWhSvzDrrTycMuWv83cLzqPA3dfKA",
        "transactions": [
            ("register", "2aBxPraCa84y7JjoL9NevoRMVhmVf7Darx2PAdFgZzDY"),
            ("nft_transfer", "DQn4xqxk1rAxu15dY1MFzKmpgN9Lh2ksWRd2sVQ3TSuU"),
            ("give_feedback_with_proof", "9LNL3dyMVAn81LiDcYipLKM1HXjDVgcSw79hj5vB9h6Z"),
        ],
    },
}


def compact(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


class Recorder:
    def __init__(self):
        self.last = 0.0

    def rpc(self, network, method, params):
        if method not in ("query", "EXPERIMENTAL_tx_status"):
            raise ValueError("read-only RPC methods required")
        if method == "query" and params.get("request_type") != "call_function":
            raise ValueError("only contract views are recorded")
        time.sleep(max(0.0, 1.1 - (time.monotonic() - self.last)))
        self.last = time.monotonic()
        body = compact({"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
        request = urllib.request.Request(
            f"https://rpc.{network}.near.org",
            data=body.encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read(8 * 1024 * 1024 + 1)
        if len(raw) > 8 * 1024 * 1024:
            raise ValueError("RPC response too large")
        result = json.loads(raw)
        if "error" in result:
            raise ValueError(result["error"])
        return result["result"]

    def record(self):
        networks = []
        for network, row in NETWORKS.items():
            calls = []
            cases = [(role, "get_config", {}) for role in ("identity", "reputation", "verifier")]
            cases += [
                ("identity", "nft_token", {"token_id": "0"}),
                (
                    "reputation",
                    "read_feedback",
                    {"agent_id": "0", "client_address": row["payer"], "feedback_index": "1"},
                ),
                (
                    "reputation",
                    "read_feedback_proof",
                    {"agent_id": "0", "client_address": row["payer"], "feedback_index": "1"},
                ),
                (
                    "reputation",
                    "get_verified_summary",
                    {"agent_id": "0", "verifier": row["verifier"], "context": "em"},
                ),
                (
                    "reputation",
                    "list_verified_feedback",
                    {"agent_id": "0", "verifier": row["verifier"]},
                ),
                ("reputation", "read_all_feedback", {"agent_id": "0"}),
                (
                    "reputation",
                    "get_proof_feedback",
                    {
                        "verifier": row["verifier"],
                        "proof": row["payment_id"],
                        "client_address": row["payer"],
                    },
                ),
            ]
            for role, method, args in cases:
                args_json = compact(args)
                result = self.rpc(
                    network,
                    "query",
                    {
                        "request_type": "call_function",
                        "finality": "final",
                        "account_id": row[role],
                        "method_name": method,
                        "args_base64": base64.b64encode(args_json.encode()).decode(),
                    },
                )
                value = json.loads(bytes(result["result"]))
                calls.append(
                    {
                        "contract": row[role],
                        "method": method,
                        "args": args,
                        "args_json": args_json,
                        "args_hex": args_json.encode().hex(),
                        "result": value,
                    }
                )
                print(network, role, method, compact(value), flush=True)
            transactions = []
            for method, tx_hash in row["transactions"]:
                result = self.rpc(network, "EXPERIMENTAL_tx_status", [tx_hash, row["payer"]])
                transactions.append(
                    {"method": method, "transaction_hash": tx_hash, "result": result}
                )
                print(network, method, tx_hash, compact(result["status"]), flush=True)
            networks.append(
                {"network": "near:" + network, "calls": calls, "transactions": transactions}
            )
        return {"format": "near-erc8004-v1", "networks": networks}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--record", action="store_true")
    group.add_argument("--compare", type=Path, help="Offline byte comparison with the TS fixture")
    parser.add_argument("--output", type=Path, default=FIXTURE)
    args = parser.parse_args()
    if args.compare is not None:
        if args.output.read_bytes() != args.compare.read_bytes():
            raise SystemExit("NEAR ERC-8004 fixtures differ")
        print("NEAR ERC-8004 fixtures are byte-identical")
        return
    value = Recorder().record()
    args.output.write_bytes(
        (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    )


if __name__ == "__main__":
    main()
