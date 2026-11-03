"""Compare the real TS/Python NEAR golden-flow helpers and shared fixture bytes."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ts-root", required=True, type=Path)
    parser.add_argument("--node", default="node")
    args = parser.parse_args()
    ts = args.ts_root.resolve()
    ours = ROOT / "tests/fixtures/near-kk.json"
    theirs = ts / "src/providers/near/vectors/near-kk.json"
    if ours.read_bytes() != theirs.read_bytes():
        raise SystemExit("NEAR golden-flow fixture differs between the SDKs")
    env = {**os.environ, "UVD_X402_PY_ROOT": str(ROOT), "UVD_PYTHON": sys.executable}
    result = subprocess.run(
        [args.node, str(ts / "scripts/xlang/near-kk-conformance.mjs")], cwd=ts, env=env, timeout=120
    )
    raise SystemExit(result.returncode)


if __name__ == "__main__":
    main()
