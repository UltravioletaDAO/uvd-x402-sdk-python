"""Reproduce public, synthetic registry parity vectors without SDK serialization.

Inputs fixed by the registry handoff; event strings are the contract's four
formato_exacto_de_cada_evento examples. --check detects fixture drift, offline.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import struct
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

ROOT = Path(__file__).resolve().parents[1]

EVENT_LOGS = [  # Exact contract event strings.
    # ruff: noqa: E501
    'EVENT_JSON:{"standard":"arras-registry","version":"1.0.0","event":"agent_registered","data":[{"account_id":"agente.near","agent_uri":"https://agente.example/card.json","metadata":null,"at":"5"}]}',
    'EVENT_JSON:{"standard":"arras-registry","version":"1.0.0","event":"agent_updated","data":[{"account_id":"agente.near","agent_uri":"ipfs://bafy","metadata":"{\\"k\\":\\"v\\"}","at":"6"}]}',
    'EVENT_JSON:{"standard":"arras-registry","version":"1.0.0","event":"feedback_given","data":[{"feedback_id":0,"rater":"payer.near","ratee":"worker.near","ratee_role":"receiver","escrow":"v1.arras-escrow.near","payment_id":"DN5eMA3sNES7xsoy6qvy24cVF6Dmwjxpkvd2b4e8P38M","operator_id":"em","released":"1000000","score":90,"tag1":"quality","tag2":null,"feedback_uri":null,"feedback_hash":null,"created_at":"7"}]}',
    'EVENT_JSON:{"standard":"arras-registry","version":"1.0.0","event":"feedback_revoked","data":[{"feedback_id":0,"rater":"payer.near","ratee":"worker.near","escrow":"v1.arras-escrow.near","payment_id":"x","revoked_at":"8"}]}',
]


def base58(raw):
    alphabet = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
    n = int.from_bytes(raw, "big")
    result = ""
    while n:
        n, digit = divmod(n, 58)
        result = alphabet[digit] + result
    return "1" * (len(raw) - len(raw.lstrip(bytes([0])))) + result


def blob(value):
    raw = value.encode() if isinstance(value, str) else value
    return struct.pack("<I", len(raw)) + raw


def vectors():
    key = Ed25519PrivateKey.from_private_bytes(bytes([1]) * 32)
    public = key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    receiver = "registry.arras-escrow.near"
    payment = {"escrow": "v1.arras-escrow.near", "payment_id": "1" * 32}
    calls = {
        "register": {"agent_uri": "https://agente.example/card.json"},
        "give_feedback": {
            **payment,
            "score": 90,
            "tag1": "quality",
            "tag2": "successRate",
            "feedback_uri": "https://agente.example/f/1.json",
            "feedback_hash": "ab" * 32,
        },
        "revoke_feedback": payment,
    }
    result = {
        "inputs": {
            "network": "near:mainnet",
            "receiver_id": receiver,
            "signer_id": "payer.near",
            "seed_byte": 1,
            "seed_length": 32,
            "public_key": "ed25519:" + base58(public),
            "nonce": 1,
            "block_hash": base58(bytes([2]) * 32),
        },
        "vectors": {},
    }
    for method, args in calls.items():
        deposit = {
            "register": "17510000000000000000000",
            "give_feedback": "18640000000000000000000",
            "revoke_feedback": "1",
        }[method]
        gas = "100000000000000" if method == "give_feedback" else "30000000000000"
        raw = json.dumps(args, separators=(",", ":"), ensure_ascii=False).encode()
        row = {"args": args, "deposit": deposit, "gas": gas, "args_hex": raw.hex()}
        if method != "revoke_feedback":
            wire = (
                blob("payer.near")
                + bytes([0])
                + public
                + struct.pack("<Q", 1)
                + blob(receiver)
                + bytes([2]) * 32
                + struct.pack("<I", 1)
                + bytes([2])
                + blob(method)
                + blob(raw)
                + struct.pack("<Q", int(gas))
                + int(deposit).to_bytes(16, "little")
            )
            signed = wire + bytes([0]) + key.sign(hashlib.sha256(wire).digest())
            row["signed_transaction_base64"] = base64.b64encode(signed).decode()
        result["vectors"][method] = row
    result["event_logs"] = EVENT_LOGS
    result["events"] = [json.loads(log[11:]) for log in EVENT_LOGS]
    return json.dumps(result, ensure_ascii=False, indent=2) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    path = ROOT / "tests/fixtures/near-registry.json"
    expected = vectors().encode()
    if args.check:
        if path.read_bytes() != expected:
            raise SystemExit("Registry parity fixture drift")
        print("Registry fixture matches independent serializer")
    else:
        path.write_bytes(expected)


if __name__ == "__main__":
    main()
