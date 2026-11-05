"""Bounded, isolated source mutants for the OWS signing and retry guards.

Only child memory is mutated; the tracked worktree must be clean. Every child
has a 120s timeout. Collection/import failures do not count as killed mutants.
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
MODULES = ("near_ows", "near_transactions", "near_storage", "near_escrow")


def mutants(name, source):
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.If)
            and len(node.body) == 1
            and isinstance(node.body[0], (ast.Raise, ast.Return))
        ):
            continue
        # Existing storage builders/delegate guards have their own mutation suites.
        if name == "near_storage" and node.lineno < source[
            : source.index("def sign_storage_transaction")
        ].count("\n"):
            continue
        if name == "near_escrow":
            continue
        changed = copy.deepcopy(tree)
        for target in ast.walk(changed):
            if isinstance(target, ast.If) and target.lineno == node.lineno:
                target.test = ast.Constant(False)
        yield f"{name} guard {ast.unparse(node.test)}", ast.unparse(
            ast.fix_missing_locations(changed)
        )
    sites = {
        "near_ows": [
            (
                "Ed25519PublicKey.from_public_bytes(self._public_key).verify(signature, digest)",
                "pass",
            ),
            ("self._network, payload.hex(), **kwargs", "self._network, bytes(32).hex(), **kwargs"),
            ('raise ValueError("OWS NEAR signing failed") from None', "raise"),
        ],
        "near_transactions": [
            ("Ed25519PublicKey.from_public_bytes(public_key).verify(signature, digest)", "pass"),
            (
                "sign_transaction(wire) if sign_transaction is not None else signer.sign(digest)",
                "signer.sign(digest)",
            ),
            ("for attempt in range(max_attempts):", "for attempt in range(1):"),
            (
                "for attempt in range(max_attempts):",
                "for attempt in range(max_attempts):\n"
                "            if attempt:\n"
                "                altered = raw[:-1] + bytes([raw[-1] ^ 1])\n"
                "                body = json.loads(content)\n"
                '                body["params"] = [base64.b64encode(altered).decode("ascii")]\n'
                "                content = json.dumps(body).encode()",
            ),
            ("response.status_code == 429", "False"),
            (
                'return base64.b64encode(raw).decode("ascii") == status["SuccessValue"]',
                "return True",
            ),
            ("response.status_code >= 500", "False"),
            ('cause.get("name") == "TIMEOUT_ERROR"', "False"),
        ],
        "near_storage": [
            ('type(args["registration_only"]) is bool and descriptor == expected', "True")
        ],
        "near_escrow": [("_check_signer_network(options.signer, options.network)", "pass")],
    }
    for old, new in sites[name]:
        assert source.count(old) == 1, (name, old, "mutation site moved")
        yield f"{name}: {old}", source.replace(old, new)


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
    # Rebind imports made before the mutant was installed (including private helpers).
    for loaded_name, loaded in list(sys.modules.items()):
        if not loaded_name.startswith("uvd_x402_sdk") or loaded is module:
            continue
        for key, value in list(vars(loaded).items()):
            if key in vars(original) and value is vars(original)[key] and key in vars(module):
                setattr(loaded, key, vars(module)[key])
    raise SystemExit(
        pytest.main(
            [
                "-q",
                "--disable-warnings",
                "--tb=short",
                "tests/test_near_ows.py",
                "tests/test_near_transactions.py",
                "tests/test_near_erc8004.py",
            ]
        )
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
        "AWS_EC2_METADATA_DISABLED": "true",
        "HTTPS_PROXY": "http://127.0.0.1:9",
        "HTTP_PROXY": "http://127.0.0.1:9",
        "NO_PROXY": "127.0.0.1,localhost",
        "PYTHONUTF8": "1",
    }
    for name in ("AWS_PROFILE", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"):
        env.pop(name, None)
    killed = total = 0
    for name in MODULES:
        source = (ROOT / f"src/uvd_x402_sdk/{name}.py").read_text(encoding="utf-8-sig")
        for label, changed in mutants(name, source):
            total += 1
            try:
                result = subprocess.run(
                    [sys.executable, __file__, "--child"],
                    input=json.dumps([name, changed]),
                    env=env,
                    cwd=ROOT,
                    text=True,
                    encoding="utf-8",
                    capture_output=True,
                    timeout=120,
                )
                ok = (
                    result.returncode == 1
                    and " failed" in result.stdout
                    and "ERROR collecting" not in result.stdout
                )
                detail = result.stdout[-4000:] + result.stderr[-1000:]
            except subprocess.TimeoutExpired:
                ok, detail = True, "timeout 120s"
            killed += ok
            print(f'M{total}: {"KILLED" if ok else "SURVIVED/ERROR"}: {label}', flush=True)
            if not ok:
                print(detail, flush=True)
    print(f"{killed}/{total} killed; no source files mutated", flush=True)
    raise SystemExit(0 if killed == total else 1)


if __name__ == "__main__":
    child() if "--child" in sys.argv else main()
