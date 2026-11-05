"""Generate the public, deterministic NEAR OWS parity vectors (no live keys/RPC)."""

import base64
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests.test_near_ows import BLOCK, LOCAL, SEED, signed_tx, transactions  # noqa: E402
from uvd_x402_sdk.near_nep413 import nep413_preimage, sign_nep413  # noqa: E402


def generate():
    vectors = dict(
        schema="near-ows/1",
        seedRecipe="bytes 0 through 31 (test only)",
        network="near:testnet",
        publicKeyHex=LOCAL.public_key.hex(),
        nonce="7",
        blockHash=BLOCK,
        transactions=[],
        nep413=[],
    )
    for name, tx in transactions():
        signed = signed_tx(name, tx, LOCAL)
        wire = base64.b64decode(signed)[:-65]
        vectors["transactions"].append(
            dict(
                id=name,
                descriptor=tx,
                unsignedHex=wire.hex(),
                digestHex=hashlib.sha256(wire).hexdigest(),
                signedTransactionBase64=signed,
            )
        )
    for callback in (None, "", "https://example.com/callback"):
        args = dict(message="Hola 🌎", nonce=SEED, recipient="example.com", callback_url=callback)
        preimage = nep413_preimage(**args)
        vectors["nep413"].append(
            dict(
                message=args["message"],
                nonceHex=SEED.hex(),
                recipient=args["recipient"],
                callbackUrl=callback,
                preimageHex=preimage.hex(),
                digestHex=hashlib.sha256(preimage).hexdigest(),
                signed=sign_nep413(LOCAL, account_id="payer.testnet", **args),
            )
        )
    return (json.dumps(vectors, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


if __name__ == "__main__":
    path = ROOT / "tests/fixtures/near-ows.json"
    output = generate()
    if "--check" in sys.argv:
        raise SystemExit(0 if path.read_bytes() == output else 1)
    path.write_bytes(output)
