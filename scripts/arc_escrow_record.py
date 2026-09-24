"""Record the Arc escrow fixture from the chain: ``tests/fixtures/arc-escrow-d.json``.

Reads only (``eth_chainId``, ``eth_blockNumber``, ``eth_getCode``, ``eth_call``)
against the public Arc RPCs, one request at a time with a pause of at least
1.3 s, and stops at the first HTTP 429 or JSON-RPC error. Sends no transaction.

What it records, on ``arc`` (5042) and ``arc-testnet`` (5042002):

- the code (size and keccak) at each escrow contract address the SDK registers
  for Arc, and at the default operator; the full code of the operator factory
  on 5042, which ``tests/test_arc_escrow.py`` searches for the operator's
  selectors;
- ``computeAddress(arg)`` on the factory: the calldata sent and the address
  returned;
- ``AuthCaptureEscrow.getHash`` of the pre-auth vector's ``paymentInfo`` (payer
  zeroed, the EIP-3009 nonce) and of the client vector's (with its payer), and
  ``paymentState`` of the latter: the exact calldata and the raw answers.

Addresses: BackTrackCo/x402r-sdk ``packages/core/src/config/index.ts`` @ bbfec12c
and BackTrackCo/x402r-contracts ``deployments/canonical-v1.0.1.json`` /
``canonical-v1.0.2.json`` @ c5223eaa.

Usage: python scripts/arc_escrow_record.py [--force]
"""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import httpx
from eth_abi import encode
from eth_utils import keccak, to_checksum_address

REPO = Path(__file__).resolve().parent.parent
FIXTURE = REPO / "tests" / "fixtures" / "arc-escrow-d.json"

RPCS = {
    5042: "https://rpc.mainnet.arc.io",
    5042002: "https://rpc.testnet.arc.io",
}
# The Arc RPCs answer 403 to a default User-Agent.
HEADERS = {"User-Agent": "uvd-x402-sdk-python/arc-escrow-recorder"}
PAUSE_S = 1.5

CONTRACTS = {
    "escrow": "0xBdEA0D1bcC5966192B070Fdf62aB4EF5b4420cff",
    "operator_factory": "0xc24153B7ED8DC03e551F29DDEeA5CadFe57e2716",
    "token_collector": "0x0E3dF9510de65469C4518D7843919c0b8C7A7757",
    "protocol_fee_config": "0xBe2d24614F339a1eB103A399F93AA2a39Ca815Bc",
    "refund_request": "0xe971C674fD5c3462023f3F891dF6289DFbC9CEFC",
    "usdc": "0x3600000000000000000000000000000000000000",
}
DEFAULT_OPERATOR = "0x0258472A1410Ac3Ad720f1BC83f22B3c0af1Fd9D"
PAYMENT_INFO_TYPEHASH = "ae68ac7ce30c86ece8196b61a7c486d8f0061f575037fbd34e7fe4e2820c6591"

# The argument of computeAddress, as data.
COMPUTE_ADDRESS_ARG = (
    "0xaE07cEB6b395BC685a776a0b4c489E8d9cE9A6ad",
    "0x25cA273d6f5508f06ed186680D305DC32a997461",
    "0x0000000000000000000000000000000000000000",
    "0x0000000000000000000000000000000000000000",
    "0xf50fD76d66c80AEb216c0C5879376C980a2B62eF",
    "0x0000000000000000000000000000000000000000",
    "0xd8023a72f29Bb1AB782c69744893Dea2836cb69C",
    "0x0000000000000000000000000000000000000000",
    "0x402ef720D202cb4BCbfb3Ee6577b204cA06786B9",
    "0x0000000000000000000000000000000000000000",
    "0x0000000000000000000000000000000000000000",
    "0x0000000000000000000000000000000000000000",
)
COMPUTE_ADDRESS_SIG = "computeAddress((" + ",".join(["address"] * 12) + "))"

PI_ABI = "(address,address,address,address,uint120,uint48,uint48,uint48,uint16,uint16,address,uint256)"
GET_HASH_SIG = f"getHash({PI_ABI})"
PAYMENT_STATE_SIG = "paymentState(bytes32)"

# Pre-auth vector: build_escrow_pre_auth frozen at these values.
PRE_AUTH_KEY_HEX = "42" * 32  # synthetic test key, never held funds
PRE_AUTH = {
    "network": "arc",
    "now": 1760000000,
    "salt": "0x" + "a7" * 32,
    "signer": "synthetic test key 0x42 * 32, never held funds",
    "worker": "0x1111111111111111111111111111111111111111",
    "bounty_usd": "0.10",
    "tier": "micro",
}

# Client vector: the PaymentInfo AdvancedEscrowClient builds the tuple from.
CLIENT_KEY_HEX = "4b" * 32  # synthetic test key, never held funds (as in the snapshot)
CLIENT_PI = {
    "operator": DEFAULT_OPERATOR,
    "receiver": "0x1111111111111111111111111111111111111111",
    "token": CONTRACTS["usdc"],
    "max_amount": 5_000_000,
    "pre_approval_expiry": 1760003600,
    "authorization_expiry": 1760007200,
    "refund_expiry": 1760090000,
    "min_fee_bps": 0,
    "max_fee_bps": 1800,
    "fee_receiver": DEFAULT_OPERATOR,
    "salt": "0x" + "5a" * 32,
}

_last_request = 0.0
_requests = 0


def rpc(url: str, method: str, params: list):
    global _last_request, _requests
    wait = PAUSE_S - (time.monotonic() - _last_request)
    if wait > 0:
        time.sleep(wait)
    _last_request = time.monotonic()
    _requests += 1
    body = {"jsonrpc": "2.0", "id": _requests, "method": method, "params": params}
    response = httpx.post(url, json=body, headers=HEADERS, timeout=30)
    if response.status_code == 429:
        sys.exit(f"429 from {url} on {method}: stopping (request #{_requests}).")
    response.raise_for_status()
    payload = response.json()
    if "error" in payload:
        sys.exit(f"JSON-RPC error from {url} on {method}: {payload['error']}")
    return payload["result"]


def selector(signature: str) -> bytes:
    return keccak(text=signature)[:4]


def pi_tuple(pi: dict, payer: str) -> tuple:
    return (
        to_checksum_address(pi["operator"]),
        to_checksum_address(payer),
        to_checksum_address(pi["receiver"]),
        to_checksum_address(pi["token"]),
        int(pi["maxAmount"] if "maxAmount" in pi else pi["max_amount"]),
        int(pi["preApprovalExpiry"] if "preApprovalExpiry" in pi else pi["pre_approval_expiry"]),
        int(pi["authorizationExpiry"] if "authorizationExpiry" in pi else pi["authorization_expiry"]),
        int(pi["refundExpiry"] if "refundExpiry" in pi else pi["refund_expiry"]),
        int(pi["minFeeBps"] if "minFeeBps" in pi else pi["min_fee_bps"]),
        int(pi["maxFeeBps"] if "maxFeeBps" in pi else pi["max_fee_bps"]),
        to_checksum_address(pi["feeReceiver"] if "feeReceiver" in pi else pi["fee_receiver"]),
        int(str(pi["salt"]), 16),
    )


def build_pre_auth_payment_info() -> dict:
    """The paymentInfo build_escrow_pre_auth signs, frozen at PRE_AUTH."""
    import uvd_x402_sdk.escrow_signing as es
    from uvd_x402_sdk.wallet import EnvKeyAdapter

    wallet = EnvKeyAdapter(private_key="0x" + PRE_AUTH_KEY_HEX)
    config = {
        "escrow": {
            "payment_info_typehash": "0x" + PAYMENT_INFO_TYPEHASH,
            "networks": {
                PRE_AUTH["network"]: {
                    "chain_id": 5042,
                    "operator": DEFAULT_OPERATOR,
                    "escrow": CONTRACTS["escrow"],
                    "token_collector": CONTRACTS["token_collector"],
                    "usdc": CONTRACTS["usdc"],
                    "usdc_domain_name": "USDC",
                    "usdc_domain_version": "2",
                }
            },
        }
    }
    with mock.patch.object(es.time, "time", lambda: PRE_AUTH["now"]), mock.patch.object(
        es.secrets, "token_hex", lambda n=32: PRE_AUTH["salt"][2:]
    ):
        header = es.build_escrow_pre_auth(
            config,
            PRE_AUTH["network"],
            wallet.get_address(),
            PRE_AUTH["worker"],
            PRE_AUTH["bounty_usd"],
            None,
            wallet,
            tier=PRE_AUTH["tier"],
        )
    return json.loads(header)["payload"]["paymentInfo"]


def call(url: str, to: str, data: bytes) -> dict:
    data_hex = "0x" + data.hex()
    result = rpc(url, "eth_call", [{"to": to, "data": data_hex}, "latest"])
    return {"to": to, "data": data_hex, "result": result}


def code_of(url: str, address: str) -> tuple[dict, str]:
    code = rpc(url, "eth_getCode", [address, "latest"])
    raw = bytes.fromhex(code.removeprefix("0x"))
    return {"address": address, "size": len(raw), "keccak": "0x" + keccak(raw).hex()}, code


def strip_long_hex(value):
    """Store hex of 32 bytes or more without 0x (repo convention for fixtures)."""
    if isinstance(value, str) and value.startswith("0x") and len(value) >= 66:
        body = value[2:]
        if all(c in "0123456789abcdef" for c in body):
            return body
    if isinstance(value, dict):
        return {k: strip_long_hex(v) for k, v in value.items()}
    if isinstance(value, list):
        return [strip_long_hex(v) for v in value]
    return value


def main() -> None:
    if FIXTURE.exists() and "--force" not in sys.argv[1:]:
        sys.exit(f"{FIXTURE.name} exists; pass --force to measure again.")

    from eth_account import Account

    pre_auth_pi = build_pre_auth_payment_info()
    client_payer = Account.from_key("0x" + CLIENT_KEY_HEX).address

    started = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    chains: dict = {}
    factory_code_5042 = ""
    for chain_id, url in RPCS.items():
        got_id = int(rpc(url, "eth_chainId", []), 16)
        if got_id != chain_id:
            sys.exit(f"{url} answered chainId {got_id}, expected {chain_id}")
        entry: dict = {"rpc": url, "chain_id": got_id}
        entry["block"] = int(rpc(url, "eth_blockNumber", []), 16)

        codes = {}
        for name, address in CONTRACTS.items():
            codes[name], raw_code = code_of(url, address)
            if name == "operator_factory" and chain_id == 5042:
                factory_code_5042 = raw_code
        codes["default_operator"], _ = code_of(url, DEFAULT_OPERATOR)
        entry["code"] = codes

        entry["compute_address"] = call(
            url,
            CONTRACTS["operator_factory"],
            selector(COMPUTE_ADDRESS_SIG)
            + encode(["(" + ",".join(["address"] * 12) + ")"], [COMPUTE_ADDRESS_ARG]),
        )
        entry["pre_auth_get_hash"] = call(
            url,
            CONTRACTS["escrow"],
            selector(GET_HASH_SIG)
            + encode([PI_ABI], [pi_tuple(pre_auth_pi, "0x" + "00" * 20)]),
        )
        state_get_hash = call(
            url,
            CONTRACTS["escrow"],
            selector(GET_HASH_SIG) + encode([PI_ABI], [pi_tuple(CLIENT_PI, client_payer)]),
        )
        entry["client_get_hash"] = state_get_hash
        entry["client_payment_state"] = call(
            url,
            CONTRACTS["escrow"],
            selector(PAYMENT_STATE_SIG)
            + bytes.fromhex(state_get_hash["result"].removeprefix("0x")),
        )
        chains[str(chain_id)] = entry

    fixture = {
        "_note": [
            "Recorded by scripts/arc_escrow_record.py from the public Arc RPCs.",
            "Real answers, never edited by hand. Reads only; no transaction.",
            "Hex values of 32 bytes or more are stored without the 0x prefix.",
        ],
        "recorded_at": started,
        "requests": _requests,
        "contracts": CONTRACTS,
        "default_operator": DEFAULT_OPERATOR,
        "payment_info_typehash": PAYMENT_INFO_TYPEHASH,
        "pre_auth": {**PRE_AUTH, "payment_info": pre_auth_pi},
        "client": {"payer": client_payer, "payment_info": CLIENT_PI},
        "chains": chains,
        "operator_factory_code_5042": factory_code_5042,
    }
    FIXTURE.write_text(json.dumps(strip_long_hex(fixture), indent=2) + "\n", encoding="utf-8")
    print(f"wrote {FIXTURE} ({_requests} requests, started {started})")


if __name__ == "__main__":
    main()
