"""Bounded source mutations for the NEAR golden-flow guards; no files are mutated.

Requires a clean tracked worktree. Every pytest child has a 120 second timeout.
Only an assertion/test failure or timeout counts as killed, never collection errors.
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
MODULES = ("near_events", "near_exact", "near_nep413", "near_storage")


def mutations(name, source):
    tree = ast.parse(source)
    # Every direct fail-before-key guard in the two signers and storage registry.
    if name != "near_events":
        guards = [
            n
            for n in ast.walk(tree)
            if isinstance(n, ast.If) and len(n.body) == 1 and isinstance(n.body[0], ast.Raise)
        ]
        for target in sorted(guards, key=lambda n: n.lineno):
            # Internal storage target checks are unreachable behind public registries.
            # Registry and input validation are mutated at the public API boundary.
            if name == "near_storage" and target.lineno < source[
                : source.index("def build_arras")
            ].count("\n"):
                continue
            mutant = copy.deepcopy(tree)
            for node in ast.walk(mutant):
                if isinstance(node, ast.If) and node.lineno == target.lineno:
                    node.test = ast.Constant(value=False)
                    break
            yield f"{name} guard L{target.lineno}: {ast.unparse(target.test)}", ast.unparse(
                ast.fix_missing_locations(mutant)
            )
    sites = {
        "near_exact": [
            ('_account(sender, "sender_id")', "pass"),
            ('_account(pay_to, "pay_to")', "pass"),
            ('"amount": str(value)', '"amount": amount'),
            ('"deposit": 1', '"deposit": 0'),
            ('"ft_transfer"', '"ft_transfer_call"'),
            ('payment["scheme"] = "exact"', 'payment["scheme"] = "escrow"'),
            ("nonce >= height * 1_000_000", "nonce > height * 1_000_000"),
        ],
        "near_nep413": [
            ('_account(account_id, "account_id")', "pass"),
            ("2**31 + 413", "2**30 + 413"),
            (
                (
                    "digest = hashlib.sha256(nep413_preimage(message, nonce, "
                    "recipient, callback_url)).digest()"
                ),
                "digest = nep413_preimage(message, nonce, recipient, callback_url)",
            ),
            ("ser.write_u8(0 if callback_url is None else 1)", "ser.write_u8(0)"),
            ("Ed25519PublicKey.from_public_bytes(public_key).verify(signature, digest)", "pass"),
        ],
        "near_storage": [
            ("if amount == 0:", "if False:"),
            ('_account(payer_id, "payer_id")', "pass"),
            ('_account(account_id, "account_id")', "pass"),
            ("escrow_contract, deposit, False)", "escrow_contract, deposit, True)"),
            (
                "NEAR_ESCROW_TOKENS[network], deposit, True)",
                "NEAR_ESCROW_TOKENS[network], deposit, False)",
            ),
            ('"deposit": str(amount)', '"deposit": deposit'),
            ('"30000000000000"', '"30000000000001"'),
        ],
        "near_events": [
            ("not isinstance(logs, Iterable) or isinstance(logs, (str, bytes, dict))", "False"),
            ('not isinstance(log, str) or not log.startswith("EVENT_JSON:")', "False"),
            ("not isinstance(value, dict)", "False"),
            ('not isinstance(value.get("event"), str)', "False"),
            ("isinstance(value, dict)\n            and all(", "True\n            and all("),
            ("key not in value or validate(value[key])", "True"),
            ("return isinstance(value, str)", "return True"),
            ("return type(value) is bool", "return True"),
            ("if type(value) is not str:", "if False:"),
            ('_uint(value, bits, "event integer")', "pass"),
            ("return type(value) is int and 0 <= value <= 65535", "return True"),
            (
                "return isinstance(value, list) and all(_string(item) for item in value)",
                "return True",
            ),
            (
                "all(key in value and validate(value[key]) for key, validate in required.items())",
                "True",
            ),
            ('value.get("standard") != "arras"', "False"),
            ('value.get("version") != "1.0.0"', "False"),
            ('value["event"] not in _CHECKS', "False"),
            ('not isinstance(value.get("data"), list)', "False"),
            ('not all(_CHECKS[value["event"]](item) for item in value["data"])', "False"),
            ("value = loads_bounded(log[11:])", 'value = __import__("json").loads(log[11:])'),
        ],
    }
    for old, new in sites[name]:
        assert source.count(old) == 1, ("mutation site moved", name, old, source.count(old))
        yield f"{name}: {old} -> {new}", source.replace(old, new)


def child():
    import pytest

    import uvd_x402_sdk as package

    name, source = json.load(sys.stdin)
    full = f"uvd_x402_sdk.{name}"
    original = sys.modules[full]
    module = types.ModuleType(full)
    module.__file__ = str(ROOT / f"src/uvd_x402_sdk/{name}.py")
    module.__package__ = "uvd_x402_sdk"
    sys.modules[full] = module
    exec(compile(source, module.__file__, "exec"), module.__dict__)
    setattr(package, name, module)
    for key in package.__all__:
        if hasattr(original, key) and getattr(package, key) is getattr(original, key):
            setattr(package, key, getattr(module, key))
    raise SystemExit(
        pytest.main(["-q", "--tb=short", "--disable-warnings", "tests/test_near_kk.py"])
    )


def main():
    status = subprocess.check_output(
        ["git", "status", "--porcelain", "--untracked-files=no"], cwd=ROOT, text=True
    )
    if status.strip():
        raise SystemExit("Commit tracked changes before mutation testing")
    env = {
        **os.environ,
        "AWS_SHARED_CREDENTIALS_FILE": "NUL" if os.name == "nt" else "/dev/null",
        "HTTP_PROXY": "http://127.0.0.1:9",
        "HTTPS_PROXY": "http://127.0.0.1:9",
        "NO_PROXY": "127.0.0.1,localhost",
    }
    env.pop("AWS_PROFILE", None)
    killed = total = 0
    for name in MODULES:
        source = (ROOT / f"src/uvd_x402_sdk/{name}.py").read_text(encoding="utf-8-sig")
        for label, mutant in mutations(name, source):
            total += 1
            try:
                result = subprocess.run(
                    [sys.executable, __file__, "--child"],
                    cwd=ROOT,
                    env=env,
                    input=json.dumps([name, mutant]),
                    text=True,
                    encoding="utf-8",
                    capture_output=True,
                    timeout=120,
                )
                ok = result.returncode == 1 and " failed" in result.stdout
                outcome = "KILLED" if ok else "SURVIVED/ERROR"
            except subprocess.TimeoutExpired:
                ok, outcome = True, "KILLED (timeout 120s)"
            killed += ok
            print(f"M{total}: {outcome}: {label}", flush=True)
            if not ok:
                print(result.stdout[-5000:] + result.stderr[-1000:], flush=True)
    print(f"{killed}/{total} mutations killed; tracked source untouched", flush=True)
    raise SystemExit(0 if killed == total else 1)


if __name__ == "__main__":
    child() if "--child" in sys.argv else main()
