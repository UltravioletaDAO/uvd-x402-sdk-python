"""Smoke test: ``pip install uvd-x402-sdk[fastapi]`` in a FRESH venv can verify ERC-8128.

Why a fresh venv and not the test suite: the suite runs with ``signer`` installed,
so ``eth-account`` is always there and a missing dependency in the ``fastapi``
extra is invisible to it. ``verify_request`` imports ``eth_account`` lazily,
inside the recovery step, so a server installed with only ``[fastapi]`` imported
fine and died with ``ModuleNotFoundError`` on the first signed request it had to
check -- at runtime, not at import.

The check runs the shipped ERC-8128 verify vectors (``run_conformance("verify")``),
which exercise ``verify_request`` end to end, recovery included, with no network.

    python scripts/smoke_fastapi_extra.py              # installs this checkout
    python scripts/smoke_fastapi_extra.py dist/x.whl   # installs a built wheel

Exit 0 when the verifier runs and every vector passes. Needs Python 3.10+ and
network access for pip.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
import venv
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

CHECK = """
import importlib.util
import sys

# Only the fastapi extra: if web3 or the signer's siblings came along, the
# check would pass for the wrong reason.
if importlib.util.find_spec("web3") is not None:
    print("web3 is installed: this venv is not a fastapi-only install")
    sys.exit(2)

from uvd_x402_sdk.erc8128 import run_conformance

report = run_conformance(only="verify")
print(f"verify vectors: {report.passed} passed, {len(report.failed)} failed")
sys.exit(1 if report.failed or report.passed == 0 else 0)
"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "target",
        nargs="?",
        default=str(REPO),
        help="what to install with [fastapi]: a checkout directory or a built wheel",
    )
    args = parser.parse_args(argv)

    with tempfile.TemporaryDirectory(prefix="x402-fastapi-", ignore_cleanup_errors=True) as tmp:
        venv.create(tmp, with_pip=True)
        bindir = "Scripts" if os.name == "nt" else "bin"
        python = str(Path(tmp) / bindir / ("python.exe" if os.name == "nt" else "python"))
        print(f"installing {args.target}[fastapi] into a fresh venv", flush=True)
        subprocess.run(
            [python, "-m", "pip", "install", "--quiet", f"{args.target}[fastapi]"],
            check=True,
        )
        # cwd outside the checkout, so the import resolves to what pip installed.
        return subprocess.run([python, "-c", CHECK], cwd=tmp).returncode


if __name__ == "__main__":
    sys.exit(main())
