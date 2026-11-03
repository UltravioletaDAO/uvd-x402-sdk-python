"""One in-memory source mutation per Arras guard, with real pytest refusals.

No tracked file is modified. Requires a clean worktree. Every child has a 120s
limit; timeout counts as killed, a collection error never does. Run offline.
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
SOURCE = ROOT / "src/uvd_x402_sdk/near_escrow.py"


def mutations(source: str):
    tree = ast.parse(source)
    # Each direct rejection guard is disabled once, independently.
    guards = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.If)
        and len(node.body) == 1
        and isinstance(node.body[0], ast.Raise)
        and node.body[0].exc is not None
    ]
    guards.sort(key=lambda node: node.lineno)
    for target in guards:
        mutant = copy.deepcopy(tree)
        for node in ast.walk(mutant):
            if isinstance(node, ast.If) and node.lineno == target.lineno:
                node.test = ast.Constant(value=False)
                break
        yield f"guard L{target.lineno}: {ast.unparse(target.test)}", ast.unparse(
            ast.fix_missing_locations(mutant)
        )
    replacements = [
        ("NEP366_PREFIX + wire", "wire"),
        ('"amount": str(n)', '"amount": amount'),
        ("str(release_at)", 'params["release_deadline"]'),
        ("str(reclaim_at)", 'params["reclaim_after"]'),
        ('str(_uint(amount, 128, "amount"))', "amount"),
        ("nonce >= height * 1_000_000", "nonce > height * 1_000_000"),
        (
            "NEAR_ESCROW_TOKENS: Mapping[str, str] = MappingProxyType(",
            "NEAR_ESCROW_TOKENS: Mapping[str, str] = dict(",
        ),
        (
            "NEAR_ESCROW_CONTRACTS: Mapping[str, tuple[str, ...]] = MappingProxyType(",
            "NEAR_ESCROW_CONTRACTS: Mapping[str, tuple[str, ...]] = dict(",
        ),
        ('("arras-v1b-1101.testnet",)', '["arras-v1b-1101.testnet"]'),
        ("uvd-x402-sdk[near]'\"", "uvd-x402-sdk[solana]'\""),
        (".write_u8(2)", ".write_u8(0)"),
        (
            '("operator_id", "token", "payer", "receiver")',
            '("operator_id", "payer", "token", "receiver")',
        ),
        ("BorshSerializer().write_u8(1)", "BorshSerializer().write_u8(2)"),
        ("self._signer = Ed25519Signer(raw)", "self._signer = Ed25519Signer(raw[:32])"),
        (
            "isinstance(secret_key, bytes) and len(secret_key) == 32",
            "isinstance(secret_key, bytes) and len(secret_key) in (32, 64)",
        ),
        ("Ed25519PublicKey.from_public_bytes(public_key).verify(signature, digest)", "pass"),
        ('"deposit": 0}', '"deposit": 1}'),
        (
            'raise NotImplementedError("Arras v2 budget ABI and golden vectors are not frozen")',
            "return {}",
        ),
    ]
    for old, new in replacements:
        assert source.count(old) == 1, ("mutation site moved", old, source.count(old))
        yield old + " -> " + new, source.replace(old, new)


def child():
    import pytest

    import uvd_x402_sdk

    source = json.load(sys.stdin)
    name = "uvd_x402_sdk.near_escrow"
    module = types.ModuleType(name)
    module.__file__ = str(SOURCE)
    module.__package__ = "uvd_x402_sdk"
    sys.modules[name] = module
    exec(compile(source, str(SOURCE), "exec"), module.__dict__)
    uvd_x402_sdk.near_escrow = module
    raise SystemExit(
        pytest.main(
            ["-q", "--tb=short", "--disable-warnings", str(ROOT / "tests/test_near_escrow.py")]
        )
    )


def main():
    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=ROOT, capture_output=True, text=True, timeout=30
    )
    if status.returncode or status.stdout.strip():
        raise SystemExit("Mutations require a clean worktree")
    env = dict(os.environ)
    env.update(
        AWS_SHARED_CREDENTIALS_FILE="NUL" if os.name == "nt" else "/dev/null",
        AWS_EC2_METADATA_DISABLED="true",
        HTTPS_PROXY="http://127.0.0.1:9",
        HTTP_PROXY="http://127.0.0.1:9",
        NO_PROXY="127.0.0.1,localhost",
    )
    env.pop("AWS_PROFILE", None)
    count = 0
    for count, (label, source) in enumerate(mutations(SOURCE.read_text(encoding="utf-8-sig")), 1):
        compile(source, str(SOURCE), "exec")
        try:
            result = subprocess.run(
                [sys.executable, str(Path(__file__).resolve()), "--child"],
                input=json.dumps(source),
                text=True,
                encoding="utf-8",
                capture_output=True,
                cwd=ROOT,
                env=env,
                timeout=120,
            )
            if (
                result.returncode != 1
                or " failed" not in result.stdout
                or "ERROR collecting" in result.stdout
            ):
                print(result.stdout[-4000:], result.stderr[-1000:])
                raise SystemExit(f"M{count} SURVIVED or did not run correctly: {label}")
            verdict = "KILLED (pytest exit 1)"
        except subprocess.TimeoutExpired:
            verdict = "KILLED (timeout 120s / 124)"
        print(f"M{count}: {verdict}: {label}", flush=True)
    print(f"{count}/{count} mutations killed; source untouched")


if __name__ == "__main__":
    child() if "--child" in sys.argv else main()
