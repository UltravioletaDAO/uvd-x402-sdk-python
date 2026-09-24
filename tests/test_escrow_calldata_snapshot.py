"""Non-regression snapshot of the escrow client on every chain it knew in 0.90.1.

PROVENANCE of ``tests/fixtures/escrow-calldata-snapshot.json``: recorded by
running THIS file as a script (``python tests/test_escrow_calldata_snapshot.py
--record``) on the 0.90.1 tree, with ``src/`` untouched. The recorder refuses
to overwrite an existing fixture. It is never edited and never re-recorded:
every test below compares what the client produces TODAY against what it
produced then, byte for byte.

What it pins, for every chain of ``ESCROW_CONTRACTS`` as of 0.90.1:

- the contracts ``AdvancedEscrowClient`` resolves (registry + default operator);
- the operator ABI each chain gets (``get_operator_abi``);
- the exact calldata of ``release()`` and ``refund_in_escrow()``, with the
  default amount and with an explicit one;
- the exact ``/settle`` body of ``authorize()``: escrow nonce and EIP-3009
  signature, so the escrow address, the collector and the USDC domain too;

plus the tables themselves (``ESCROW_CONTRACTS``, ``ESCROW_CHAIN_NAMES``,
``BASE_MAINNET_CONTRACTS``, ``CREATE3_CHAIN_IDS``, ``VERIFIED_USDC_DOMAINS``,
``PAYMENT_INFO_TYPEHASH``). A chain added later is not in the fixture and is not
compared here; a chain that was there must come out identical.

Base (8453) and SKALE Base (1187947933) use their built-in default operator;
every other chain takes the explicit ``EXPLICIT_OPERATOR`` below, because the
client requires one there.

Signer: a synthetic test key (0x4b repeated 32 times) that never held funds.
The signature is RFC 6979 deterministic: same key and typed data, same bytes.
Nothing is sent: ``_send_tx`` and ``httpx.post`` are replaced by recorders.

Hex values of 32 bytes or more are stored WITHOUT the ``0x`` prefix (the
convention of ``escrow-preauth.json``: secret scanners block a literal ``0x`` +
64 hex chars). ``_hydrate`` re-prefixes them on load.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from unittest import mock

import pytest

FIXTURE_PATH = (
    Path(__file__).resolve().parent / "fixtures" / "escrow-calldata-snapshot.json"
)

SIGNER_KEY = "0x" + "4b" * 32  # synthetic, never held funds
RECEIVER = "0x1111111111111111111111111111111111111111"
EXPLICIT_OPERATOR = "0x" + "0e" * 20
UNREACHABLE_RPC = "http://127.0.0.1:9"  # never contacted: nothing is sent
DEFAULT_OPERATOR_CHAINS = (8453, 1187947933)

MAX_AMOUNT = 5_000_000
PARTIAL_RELEASE = 2_000_000
PARTIAL_REFUND = 3_000_000
PRE_APPROVAL_EXPIRY = 1760003600
AUTHORIZATION_EXPIRY = 1760007200
REFUND_EXPIRY = 1760090000
MIN_FEE_BPS = 0
MAX_FEE_BPS = 1800
SALT = "0x" + "5a" * 32

_LONG_HEX = re.compile(r"^[0-9a-f]{64,}$")
_PREFIXED_LONG_HEX = re.compile(r"^0x[0-9a-f]{64,}$")


def _hydrate(value):
    """Re-prefix long hex values (stored 0x-less to dodge secret scanners)."""
    if isinstance(value, str) and _LONG_HEX.fullmatch(value):
        return "0x" + value
    if isinstance(value, list):
        return [_hydrate(v) for v in value]
    if isinstance(value, dict):
        return {k: _hydrate(v) for k, v in value.items()}
    return value


def _dehydrate(value):
    """Inverse of ``_hydrate``; refuses a bare long hex it could not restore."""
    if isinstance(value, str):
        if _LONG_HEX.fullmatch(value):
            raise ValueError(f"bare long hex would be re-prefixed on load: {value}")
        if _PREFIXED_LONG_HEX.fullmatch(value):
            return value[2:]
        return value
    if isinstance(value, list):
        return [_dehydrate(v) for v in value]
    if isinstance(value, dict):
        return {k: _dehydrate(v) for k, v in value.items()}
    return value


class _Posted:
    status_code = 200

    def json(self):
        return {"success": True, "transaction": "0x" + "00" * 32}


def capture_chain(chain_id: int) -> dict:
    """Run the client's escrow calls on ``chain_id`` and return what they emit."""
    import uvd_x402_sdk.advanced_escrow as ae

    kwargs = {}
    if chain_id not in DEFAULT_OPERATOR_CHAINS:
        kwargs["operator_address"] = EXPLICIT_OPERATOR
    client = ae.AdvancedEscrowClient(
        private_key=SIGNER_KEY,
        chain_id=chain_id,
        rpc_url=UNREACHABLE_RPC,
        **kwargs,
    )

    pi = ae.PaymentInfo(
        operator=client.contracts["operator"],
        receiver=RECEIVER,
        token=client.contracts["usdc"],
        max_amount=MAX_AMOUNT,
        pre_approval_expiry=PRE_APPROVAL_EXPIRY,
        authorization_expiry=AUTHORIZATION_EXPIRY,
        refund_expiry=REFUND_EXPIRY,
        min_fee_bps=MIN_FEE_BPS,
        max_fee_bps=MAX_FEE_BPS,
        fee_receiver=client.contracts["operator"],
        salt=SALT,
    )

    sent: list[str] = []

    def record_tx(func_call):
        sent.append(func_call._encode_transaction_data())
        return ae.TransactionResult(success=True)

    client._send_tx = record_tx
    client.release(pi)
    client.release(pi, PARTIAL_RELEASE)
    client.refund_in_escrow(pi)
    client.refund_in_escrow(pi, PARTIAL_REFUND)
    assert len(sent) == 4, sent

    posted: list[dict] = []

    def record_post(url, json=None, timeout=None):
        posted.append({"url": url, "body": json})
        return _Posted()

    with mock.patch.object(ae.httpx, "post", record_post):
        result = client.authorize(pi)
    assert result.success and len(posted) == 1, (result, posted)

    abi = ae.get_operator_abi(chain_id)
    abi_name = "OPERATOR_ABI_V2" if abi == ae.OPERATOR_ABI_V2 else "OPERATOR_ABI"
    assert abi == getattr(ae, abi_name)

    return {
        "contracts": dict(client.contracts),
        "operator_abi": abi_name,
        "calldata": {
            "release": sent[0],
            "release_partial": sent[1],
            "refund_in_escrow": sent[2],
            "refund_in_escrow_partial": sent[3],
        },
        "authorize": posted[0],
    }


def _tables(chain_ids) -> dict:
    import uvd_x402_sdk.advanced_escrow as ae
    from uvd_x402_sdk.escrow_signing import VERIFIED_USDC_DOMAINS

    return {
        "ESCROW_CONTRACTS": {str(c): dict(ae.ESCROW_CONTRACTS[c]) for c in chain_ids},
        "ESCROW_CHAIN_NAMES": {str(c): ae.ESCROW_CHAIN_NAMES[c] for c in chain_ids},
        "BASE_MAINNET_CONTRACTS": dict(ae.BASE_MAINNET_CONTRACTS),
        "CREATE3_CHAIN_IDS": sorted(ae.CREATE3_CHAIN_IDS),
        "VERIFIED_USDC_DOMAINS": {
            str(c): list(v) for c, v in sorted(VERIFIED_USDC_DOMAINS.items())
        },
        "PAYMENT_INFO_TYPEHASH": "0x" + ae.PAYMENT_INFO_TYPEHASH.hex(),
    }


def _record() -> None:
    import subprocess

    import uvd_x402_sdk
    import uvd_x402_sdk.advanced_escrow as ae

    if FIXTURE_PATH.exists():
        sys.exit(f"{FIXTURE_PATH.name} exists and is never re-recorded.")
    repo = Path(__file__).resolve().parent.parent
    dirty = subprocess.run(
        ["git", "status", "--porcelain", "--", "src"],
        cwd=repo, capture_output=True, text=True, check=True,
    ).stdout.strip()
    if dirty:
        sys.exit(f"src/ has changes; record from an untouched tree:\n{dirty}")
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo, capture_output=True, text=True, check=True,
    ).stdout.strip()

    chain_ids = sorted(ae.ESCROW_CONTRACTS)
    snapshot = {
        "_note": [
            "Recorded by tests/test_escrow_calldata_snapshot.py --record.",
            "Never edited, never re-recorded. See that file's docstring.",
            "Hex values of 32 bytes or more are stored without the 0x prefix.",
        ],
        "recorded_from": {"commit": head, "version": uvd_x402_sdk.__version__},
        "signer": "synthetic test key 0x4b * 32, never held funds",
        "tables": _tables(chain_ids),
        "abis": {"OPERATOR_ABI": ae.OPERATOR_ABI, "OPERATOR_ABI_V2": ae.OPERATOR_ABI_V2},
        "chains": {str(c): capture_chain(c) for c in chain_ids},
    }
    FIXTURE_PATH.write_text(
        json.dumps(_dehydrate(snapshot), indent=2) + "\n", encoding="utf-8"
    )
    print(f"wrote {FIXTURE_PATH} ({len(chain_ids)} chains, commit {head})")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def _fixture() -> dict:
    return _hydrate(json.loads(FIXTURE_PATH.read_text(encoding="utf-8")))


if __name__ != "__main__":
    SNAPSHOT = _fixture()
    SNAPSHOT_CHAINS = sorted(int(c) for c in SNAPSHOT["chains"])
else:  # pragma: no cover - recorder mode
    SNAPSHOT = {}
    SNAPSHOT_CHAINS = []


def test_snapshot_covers_the_chains_that_matter():
    assert 8453 in SNAPSHOT_CHAINS and 1187947933 in SNAPSHOT_CHAINS
    assert SNAPSHOT["chains"]["8453"]["operator_abi"] == "OPERATOR_ABI"
    assert SNAPSHOT["chains"]["1187947933"]["operator_abi"] == "OPERATOR_ABI_V2"


def test_tables_of_every_snapshot_chain_are_unchanged():
    now = _tables(SNAPSHOT_CHAINS)
    then = SNAPSHOT["tables"]

    assert now["ESCROW_CONTRACTS"] == then["ESCROW_CONTRACTS"]
    assert now["ESCROW_CHAIN_NAMES"] == then["ESCROW_CHAIN_NAMES"]
    assert now["BASE_MAINNET_CONTRACTS"] == then["BASE_MAINNET_CONTRACTS"]
    assert now["CREATE3_CHAIN_IDS"] == then["CREATE3_CHAIN_IDS"]
    assert now["PAYMENT_INFO_TYPEHASH"] == then["PAYMENT_INFO_TYPEHASH"]
    for chain, domain in then["VERIFIED_USDC_DOMAINS"].items():
        assert now["VERIFIED_USDC_DOMAINS"][chain] == domain, chain


def test_the_two_operator_abis_are_unchanged():
    import uvd_x402_sdk.advanced_escrow as ae

    assert ae.OPERATOR_ABI == SNAPSHOT["abis"]["OPERATOR_ABI"]
    assert ae.OPERATOR_ABI_V2 == SNAPSHOT["abis"]["OPERATOR_ABI_V2"]


@pytest.mark.parametrize("chain_id", SNAPSHOT_CHAINS)
def test_contracts_abi_and_calldata_are_byte_identical(chain_id):
    then = SNAPSHOT["chains"][str(chain_id)]
    now = capture_chain(chain_id)

    assert now["contracts"] == then["contracts"]
    assert now["operator_abi"] == then["operator_abi"]
    assert now["calldata"] == then["calldata"]


@pytest.mark.parametrize("chain_id", SNAPSHOT_CHAINS)
def test_authorize_settle_body_is_byte_identical(chain_id):
    then = SNAPSHOT["chains"][str(chain_id)]["authorize"]
    now = capture_chain(chain_id)["authorize"]

    assert now["url"] == then["url"]
    # Key order included: this is the order the body goes out on the wire.
    assert json.dumps(now["body"]) == json.dumps(then["body"])


if __name__ == "__main__":  # pragma: no cover
    if sys.argv[1:] != ["--record"]:
        sys.exit("usage: python tests/test_escrow_calldata_snapshot.py --record")
    _record()
