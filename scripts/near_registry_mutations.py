"""Kill every registry rejection guard in an isolated, offline pytest process.

Requires a clean worktree; mutates source only in child memory. A 120s timeout
kills a mutant, but collection/import failures never count as a killed guard.
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
SOURCE = ROOT / "src/uvd_x402_sdk/near_registry.py"


def mutations(source):
    tree = ast.parse(source)
    sites = []
    for node in ast.walk(tree):
        if isinstance(node, ast.If) and len(node.body) == 1 and isinstance(node.body[0], ast.Raise):
            sites.append((node, "guard"))
        if (
            isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Name)
            and node.value.func.id
            in ("_require", "_uri", "_metadata", "_tag", "_payment", "_hash", "_escrow", "_account")
        ):
            sites.append((node, "validation"))
    for target, kind in sorted(sites, key=lambda pair: pair[0].lineno):
        mutant = copy.deepcopy(tree)
        for node in ast.walk(mutant):
            if type(node) is type(target) and node.lineno == target.lineno:
                if kind == "guard":
                    node.test = ast.Constant(False)
                else:
                    node.value = ast.Constant(None)
                break
        yield (
            f"{kind} L{target.lineno}: {ast.unparse(target).splitlines()[0]}",
            ast.unparse(ast.fix_missing_locations(mutant)),
        )
    replacements = [
        ("1 <= len(value) <= 512", "1 <= len(value) <= 513"),
        ("1 <= size <= 1024", "1 <= size <= 1025"),
        ("0 <= score <= 100", "0 <= score <= 101"),
        ("gas_units >= MIN_FEEDBACK_GAS", "gas_units >= MIN_FEEDBACK_GAS - 1"),
        ("amount >= int(REGISTER_DEPOSIT)", "amount >= int(REGISTER_DEPOSIT) - 1"),
        ("amount >= int(FEEDBACK_DEPOSIT)", "amount >= int(FEEDBACK_DEPOSIT) - 1"),
        ("hashlib.sha256(wire).digest()", "hashlib.sha256(b'wrong-domain' + wire).digest()"),
        ("Ed25519PublicKey.from_public_bytes(public_key).verify(signature, digest)", "pass"),
        (".write_u8(2)", ".write_u8(0)"),
        (".write_u32(1)", ".write_u32(2)"),
        ('if not f["revoked"]', "if True"),
        ('and f["operator_id"] == operator_id', "and True"),
        ('_view(f, _feedback_view)["escrow"] == escrow', "bool(_view(f, _feedback_view))"),
        ("weighted * 100 // released", "score_sum * 100 // count"),
        ("score_sum * 100 // count", "score_sum * 10 // count"),
        ('("https", "ipfs", "ar")', '("https", "ipfs", "ar", "javascript")'),
        (
            "NEAR_REGISTRY_CONTRACTS: Mapping[str, tuple[str, ...]] = MappingProxyType(",
            "NEAR_REGISTRY_CONTRACTS: Mapping[str, tuple[str, ...]] = dict(",
        ),
        (
            "NEAR_REGISTRY_ESCROWS: Mapping[str, tuple[str, ...]] = MappingProxyType(",
            "NEAR_REGISTRY_ESCROWS: Mapping[str, tuple[str, ...]] = dict(",
        ),
    ]
    # Refuter R9/R11/R13: canonical bytes, exact event version, error precedence.
    replacements += [
        (
            'args = _json(params["args"])',
            'args = _json(descriptor["actions"][0]["params"]["args"])',
        ),
        ('value.get("version") != "1.0.0"', 'not str(value.get("version")).startswith("1.")'),
        (
            "    _require(amount >= 1, RegistryErrorCode.E06)\n"
            "    _uri(agent_uri, RegistryErrorCode.E01)\n    _metadata(metadata)",
            "    _uri(agent_uri, RegistryErrorCode.E01)\n    _metadata(metadata)\n"
            "    _require(amount >= 1, RegistryErrorCode.E06)",
        ),
        ('return _valid(_account, value, "account")', "return True"),
        ("return _valid(_uri, value, RegistryErrorCode.E01)", "return True"),
        ('"metadata": lambda v: _valid(_metadata, v)', '"metadata": lambda v: True'),
        ('"tag1": lambda v: _valid(_tag, v)', '"tag1": lambda v: True'),
        ('"tag2": lambda v: _valid(_tag, v)', '"tag2": lambda v: True'),
        ('"feedback_uri": _nullable(_uri_check)', '"feedback_uri": lambda v: True'),
        ('return len(value.encode("utf-8")) <= 64', "return True"),
        ('return len(value.encode("utf-8")) <= 64', "return len(value) <= 64"),
        ('return len(value.encode("utf-8")) <= 64', 'return len(value.encode("utf-8")) <= 65'),
        ("if not isinstance(receipts_outcome, list):", "if False:"),
        ("if not isinstance(receipt, dict):", "if False:"),
        (
            'if not isinstance(outcome, dict) or outcome.get("executor_id") not in contracts:',
            "if not isinstance(outcome, dict):",
        ),
        (
            "contracts = NEAR_REGISTRY_CONTRACTS[network]\n    result = []",
            'contracts = NEAR_REGISTRY_CONTRACTS["near:mainnet"]\n    result = []',
        ),
        ("not any(lo <= ord(c) <= hi for lo, hi in _DISPLAY_REMOVED_RANGES)", "True"),
    ]
    for old, new in replacements:
        assert source.count(old) == 1, ("mutation site moved", old, source.count(old))
        yield old + " -> " + new, source.replace(old, new)
    # Payment validation appears in both given and revoked events; mutate each
    # site independently, so one event's coverage cannot hide the other's gap.
    old = '"payment_id": lambda v: _valid(_payment, v)'
    start = 0
    while (index := source.find(old, start)) != -1:
        yield (
            f"disable payment content at offset {index}",
            (source[:index] + '"payment_id": lambda v: True' + source[index + len(old) :]),
        )
        start = index + len(old)
    for check in ("_agent_view", "_feedback_view", "_summary_view", "_config_view"):
        yield f"disable {check}", source + f"\n{check} = lambda value: True\n"

    parity_path = ROOT / "scripts/near_registry_parity.py"
    parity_source = parity_path.read_text(encoding="utf-8-sig")
    guard = "if ours.read_bytes() != theirs.read_bytes():"
    assert parity_source.count(guard) == 1, "parity mutation site moved"
    # Import the actual parity module in the child, replacing only its guard.
    # This still changes no files, and runs the same byte-drift regression tests.
    patch = (
        "\nfrom scripts import near_registry_parity as parity\n"
        + "exec(compile("
        + repr(parity_source.replace(guard, "if False:"))
        + ", parity.__file__, 'exec'), parity.__dict__)\n"
    )
    yield "disable cross-SDK fixture byte comparison", source + patch

    # Round 2: every condition of the receipt success filter, plus plausible
    # truthiness, AND and exact-key-count regressions. TS accepts extra fields.
    status_guard = """        if (
            not isinstance(status, dict)
            or "Failure" in status
            or not (
                isinstance(status.get("SuccessValue"), str)
                or isinstance(status.get("SuccessReceiptId"), str)
            )
        ):"""
    for old, new in [
        (status_guard, "        if False:"),
        ("not isinstance(status, dict)", "False"),
        ('or "Failure" in status', "or False"),
        ('or "Failure" in status', 'or bool(status.get("Failure"))'),
        ('isinstance(status.get("SuccessValue"), str)', '"SuccessValue" in status'),
        ('isinstance(status.get("SuccessReceiptId"), str)', '"SuccessReceiptId" in status'),
        ('isinstance(status.get("SuccessValue"), str)', "False"),
        ('isinstance(status.get("SuccessReceiptId"), str)', "False"),
        (
            'or isinstance(status.get("SuccessReceiptId"), str)',
            'and isinstance(status.get("SuccessReceiptId"), str)',
        ),
        ('or "Failure" in status', 'or "Failure" in status or len(status) != 1'),
    ]:
        assert source.count(old) == 1, ("receipt status mutation site moved", old)
        yield "receipt status: " + old + " -> " + new, source.replace(old, new)


def child():
    import pytest

    import uvd_x402_sdk

    sys.path.insert(0, str(ROOT))
    source = json.load(sys.stdin)
    name = "uvd_x402_sdk.near_registry"
    module = types.ModuleType(name)
    module.__file__ = str(SOURCE)
    module.__package__ = "uvd_x402_sdk"
    sys.modules[name] = module
    exec(compile(source, str(SOURCE), "exec"), module.__dict__)
    uvd_x402_sdk.near_registry = module
    raise SystemExit(
        pytest.main(
            [
                "-q",
                "--tb=short",
                "--disable-warnings",
                str(ROOT / "tests/test_near_registry.py"),
                str(ROOT / "tests/test_near_registry_parity.py"),
                str(ROOT / "tests/test_near_registry_receipts.py"),
            ]
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
    survivors = []
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
                survivors.append(f"M{count}: {label}")
                print(result.stdout[-1500:], result.stderr[-500:], flush=True)
                verdict = "SURVIVED or invalid execution"
            else:
                verdict = "KILLED (pytest exit 1)"
        except subprocess.TimeoutExpired:
            verdict = "KILLED (timeout 120s / 124)"
        print(f"M{count}: {verdict}: {label}", flush=True)
    print(f"{count - len(survivors)}/{count} killed; source untouched", flush=True)
    if survivors:
        raise SystemExit("\n".join(survivors))


if __name__ == "__main__":
    child() if "--child" in sys.argv else main()
