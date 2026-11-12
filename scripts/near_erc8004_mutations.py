"""Run isolated ERC-8004 guard mutations with a 120-second deadline per child.

The worktree must be clean. Mutated source is loaded only in child memory;
collection errors do not count as killed guards. No RPC is used.
"""

from __future__ import annotations

import ast
import copy
import json
import os
import subprocess
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "src/uvd_x402_sdk/near_erc8004.py"


def mutations(source):
    tree = ast.parse(source)
    sites = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "_require"
    ]
    for target in sorted(sites, key=lambda n: n.lineno):
        mutant = copy.deepcopy(tree)
        for n in ast.walk(mutant):
            if (
                isinstance(n, ast.Call)
                and n.lineno == target.lineno
                and n.col_offset == target.col_offset
            ):
                n.args[0] = ast.Constant(True)
                break
        yield f"guard L{target.lineno}", ast.unparse(ast.fix_missing_locations(mutant))
    for name in (
        "_decimal",
        "_signed",
        "_number",
        "_acc",
        "_text",
        "_uri",
        "_key",
        "_base64",
        "_hash",
        "_label",
        "_proof",
        "_tag",
        "_token",
        "_all_feedback",
        "_result_matches",
    ):
        mutant = copy.deepcopy(tree)
        target = next(n for n in mutant.body if isinstance(n, ast.FunctionDef) and n.name == name)
        target.body = [ast.Return(ast.Constant(True))]
        yield name, ast.unparse(ast.fix_missing_locations(mutant))
    for old, new in [
        (
            'url = "https://rpc." + self.network.split(":")[1] + ".near.org"',
            'url = "https://rpc.mainnet.near.org"',
        ),
        ("_require(len(codes) == 1,", "_require(len(codes) >= 1,"),
        (
            '"feedback_index": lambda v: _decimal(v, positive=True),\n}',
            '"feedback_index": lambda v: _decimal(v, positive=False),\n}',
        ),
        ('and data["proof"]["verifier"] not in _verifiers(network)', "and False"),
        # Arras v2 has no verifier: a v1 verifier in its row must turn a test red.
        (
            '"v2.arras-escrow.near": None,',
            '"v2.arras-escrow.near": "erc8004-verifier.arras-escrow.near",',
        ),
        # One verifier per escrow: each site that picks the verifier, made to pick v1's.
        (
            "return rows.get(escrow) if isinstance(escrow, str) else None",
            "return rows.get(escrow, NEAR_ERC8004_CONTRACTS[network]['verifier'])"
            " if isinstance(escrow, str) else None",
        ),
        (
            "return rows.get(escrow) if isinstance(escrow, str) else None",
            "return rows.get(escrow)",
        ),
        (
            'receiver = _escrow_verifier(network, cfg["escrow"])'
            ' if role == "verifier" else contracts[role]',
            "receiver = contracts[role]",
        ),
        (
            '"account_id": contracts[role] if account is None else account,',
            '"account_id": contracts[role],',
        ),
        (
            'account = contracts["verifier"] if verifier is None else verifier',
            'account = contracts["verifier"]',
        ),
        ("            verifier=account,\n", "            verifier=None,\n"),
        (
            'receivers = _verifiers(network) if role == "verifier" else {contracts[role]}',
            "receivers = {contracts[role]}",
        ),
        ('"min_gas_nft_transfer_call": 50 * TGAS,', '"min_gas_nft_transfer_call": 1,'),
        ('"min_gas_give_feedback": 40 * TGAS,', '"min_gas_give_feedback": 1,'),
        (
            '"min_gas_give_feedback_with_proof": 120 * TGAS,',
            '"min_gas_give_feedback_with_proof": 1,',
        ),
        ('"min_gas_verify": 25 * TGAS,', '"min_gas_verify": 1,'),
        (
            '"register_deposit": 132790000000000000000000 * 105 // 100,',
            '"register_deposit": 132790000000000000000000 * 200 // 100,',
        ),
        (
            '"feedback_deposit": 5910000000000000000000 * 105 // 100,',
            '"feedback_deposit": 5910000000000000000000 * 200 // 100,',
        ),
        (
            '"feedback_proof_deposit": 14720000000000000000000 * 105 // 100,',
            '"feedback_proof_deposit": 14720000000000000000000 * 200 // 100,',
        ),
        (
            "maximum = min(int(cfg[ceiling_field]), ERC8004_DEPOSIT_CEILINGS[ceiling_field])",
            "maximum = ERC8004_DEPOSIT_CEILINGS[ceiling_field]",
        ),
        ('and "Failure" not in status', "and True"),
        ('and _successful(outcome.get("status"))', "and True"),
        ("executor_id != contracts[role]", "False"),
        ('event.get("version") != "1.0.0"', "False"),
        ('event.get("standard") != standard', "False"),
        ('len(event["data"]) != 1', 'len(event["data"]) < 1'),
        ('not check(event["data"][0])', "False"),
        ('if receipt["executor_id"] != contracts["reputation"]:', "if False:"),
        ("return loads_bounded(_json(result))", "return result"),
    ]:
        if source.count(old) != 1:
            raise RuntimeError("mutation site moved: " + old)
        yield old, source.replace(old, new)


def child():
    import pytest

    import uvd_x402_sdk

    sys.path.insert(0, str(ROOT))
    short_name, source = json.load(sys.stdin)
    name = "uvd_x402_sdk." + short_name
    module = types.ModuleType(name)
    module.__file__ = str(SOURCE)
    module.__package__ = "uvd_x402_sdk"
    sys.modules[name] = module
    exec(compile(source, str(SOURCE), "exec"), module.__dict__)
    setattr(uvd_x402_sdk, short_name, module)
    if short_name == "near_transactions":
        uvd_x402_sdk.near_erc8004._sign_function_call_transaction = (
            module._sign_function_call_transaction
        )
    raise SystemExit(
        pytest.main(
            [
                "-q",
                "--tb=short",
                "--disable-warnings",
                str(ROOT / "tests/test_near_erc8004.py"),
                str(ROOT / "tests/test_near_erc8004_receipts.py"),
            ]
        )
    )


def main():
    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=ROOT, capture_output=True, text=True, timeout=30
    )
    if status.returncode or status.stdout.strip():
        raise SystemExit("Mutations require a clean worktree")
    env = {
        **os.environ,
        "HTTPS_PROXY": "http://127.0.0.1:9",
        "HTTP_PROXY": "http://127.0.0.1:9",
        "NO_PROXY": "127.0.0.1,localhost",
    }
    survivors = []
    rows = [
        ("near_erc8004", label, source)
        for label, source in mutations(SOURCE.read_text(encoding="utf-8"))
    ]
    signing = (ROOT / "src/uvd_x402_sdk/near_transactions.py").read_text(encoding="utf-8")
    for old, new in [
        ("Ed25519PublicKey.from_public_bytes(public_key).verify(signature, digest)", "pass"),
        (".write_u8(2)", ".write_u8(0)"),
    ]:
        if signing.count(old) != 1:
            raise RuntimeError("mutation site moved: " + old)
        rows.append(("near_transactions", old, signing.replace(old, new)))
    for number, (name, label, source) in enumerate(rows, 1):
        compile(source, str(SOURCE), "exec")
        try:
            result = subprocess.run(
                [sys.executable, str(Path(__file__).resolve()), "--child"],
                input=json.dumps([name, source]),
                text=True,
                encoding="utf-8",
                capture_output=True,
                cwd=ROOT,
                env=env,
                timeout=120,
            )
            killed = (
                result.returncode == 1
                and " failed" in result.stdout
                and "ERROR collecting" not in result.stdout
            )
            if not killed:
                survivors.append(number)
                print(result.stdout[-1000:], result.stderr[-200:], flush=True)
            verdict = "KILLED" if killed else "SURVIVED/INVALID"
        except subprocess.TimeoutExpired:
            verdict = "KILLED (timeout 120s)"
        print(f"M{number}: {verdict}: {label}", flush=True)
    print(f"{len(rows) - len(survivors)}/{len(rows)} killed; source unchanged", flush=True)
    raise SystemExit(bool(survivors))


if __name__ == "__main__":
    child() if "--child" in sys.argv else main()
