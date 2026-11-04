"""Require the registry fixture to be the same bytes in both SDK checkouts."""

from __future__ import annotations

import argparse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def check_parity(ts_root: Path) -> None:
    ours = ROOT / "tests/fixtures/near-registry.json"
    theirs = ts_root.resolve() / "src/providers/near/vectors/near-registry.json"
    if ours.read_bytes() != theirs.read_bytes():
        raise SystemExit("NEAR registry fixture differs between the SDKs")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ts-root", required=True, type=Path)
    args = parser.parse_args()
    check_parity(args.ts_root)
    print("NEAR registry fixture is byte-identical in both SDKs")


if __name__ == "__main__":
    main()
