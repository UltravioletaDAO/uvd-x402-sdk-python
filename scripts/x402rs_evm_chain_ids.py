"""Record the facilitator's EVM chain ids: ``tests/fixtures/x402rs-evm-chain-ids.json``.

The CAIP-2 aliases of ``uvd_x402_sdk.erc8004`` are derived from chain ids: the
payment registry's where it carries the network, and for the eight it lacks
(BSC and seven testnets) ``_CHAIN_IDS_NOT_IN_THE_PAYMENT_REGISTRY``.
``tests/test_erc8004_caip2_aliases.py`` checks every one of them against this
fixture, so an id copied wrong fails there instead of rewriting a name to
another chain.

This script READS two files of an x402-rs checkout at a commit (``git show
<ref>:<path>``; nothing of x402-rs is built or executed):

- ``src/network.rs``: the wire name of each ``Network`` variant, from its
  ``#[serde(rename = "<name>")]`` and from its ``Display`` arm
  (``Network::X => write!(f, "<name>")``). The two must agree, or exit 2.
- ``src/chain/evm.rs``: the chain id of each EVM variant, from
  ``Network::X => Ok(EvmChain::new(value, <id>))``.

It also records the commit and the release in ``VERSION`` at that ref (the
``Cargo.toml`` version is a frozen ``0.0.0``: x402-rs ``src/version.rs``).
Never edit the fixture by hand: re-run this against x402-rs ``origin/main`` and
review the diff.

``--check`` compares the VALUES: a fixture that records an older commit with the
same ids is not drift; it exits 0 and says the pointer is stale.

Usage:
    python scripts/x402rs_evm_chain_ids.py --x402rs <x402-rs checkout> [--ref origin/main] [--check]
Exit codes: 0 ok, 1 --check found drift, 2 could not read x402-rs.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
OUT_PATH = REPO / "tests" / "fixtures" / "x402rs-evm-chain-ids.json"

NETWORK_PATH = "src/network.rs"
EVM_PATH = "src/chain/evm.rs"
VERSION_PATH = "VERSION"
REPOSITORY = "UltravioletaDAO/x402-rs"

# `#[serde(rename = "base-sepolia")]` then, after doc comments or other
# attributes, the variant `BaseSepolia,`.
SERDE_RE = re.compile(
    r'#\[serde\(rename = "([a-z0-9-]+)"\)\]\s*(?:(?:#\[[^\]]*\]|///[^\n]*)\s*)*([A-Z][A-Za-z0-9]*),'
)
DISPLAY_RE = re.compile(r'Network::([A-Z][A-Za-z0-9]*) => write!\(f, "([a-z0-9-]+)"\)')
CHAIN_RE = re.compile(r"Network::([A-Z][A-Za-z0-9]*) => Ok\(EvmChain::new\(value, ([0-9]+)\)\)")
VERSION_RE = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+")


class CannotReadError(Exception):
    """x402-rs's sources are not what this script knows how to read: exit 2."""


def _git(checkout: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(checkout), *args], check=True, capture_output=True, text=True
    ).stdout


def _names(network_rs: str) -> dict[str, str]:
    serde = {variant: name for name, variant in SERDE_RE.findall(network_rs)}
    display = dict(DISPLAY_RE.findall(network_rs))
    if not serde or not display:
        raise CannotReadError(f"{NETWORK_PATH}: no serde renames or Display arms found")
    for variant in set(serde) & set(display):
        if serde[variant] != display[variant]:
            raise CannotReadError(
                f"{NETWORK_PATH}: {variant} is {serde[variant]!r} in serde, "
                f"{display[variant]!r} in Display"
            )
    return {**display, **serde}


def record(checkout: Path, ref: str) -> dict:
    commit = _git(checkout, "rev-parse", f"{ref}^{{commit}}").strip()
    names = _names(_git(checkout, "show", f"{commit}:{NETWORK_PATH}"))
    pairs = CHAIN_RE.findall(_git(checkout, "show", f"{commit}:{EVM_PATH}"))
    if not pairs:
        raise CannotReadError(f"{EVM_PATH}: no `Network::X => Ok(EvmChain::new(value, N))` arm")
    chain_ids: dict[str, int] = {}
    for variant, chain_id in pairs:
        if variant not in names:
            raise CannotReadError(f"{EVM_PATH}: {variant} has no wire name in {NETWORK_PATH}")
        name = names[variant]
        if name in chain_ids and chain_ids[name] != int(chain_id):
            raise CannotReadError(f"{EVM_PATH}: {name} has two chain ids")
        chain_ids[name] = int(chain_id)
    version = VERSION_RE.fullmatch(_git(checkout, "show", f"{commit}:{VERSION_PATH}").strip())
    if version is None:
        raise CannotReadError(f"{VERSION_PATH}: not a release version")
    return {
        "generatedBy": "python scripts/x402rs_evm_chain_ids.py --x402rs <x402-rs checkout> "
        f"--ref {ref}",
        "source": {
            "repository": REPOSITORY,
            "commit": commit,
            "version": version.group(0),
            "files": [NETWORK_PATH, EVM_PATH],
        },
        "chainIds": dict(sorted(chain_ids.items())),
    }


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--x402rs", required=True, type=Path, help="an x402-rs git checkout")
    parser.add_argument("--ref", default="origin/main")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    try:
        fresh = record(args.x402rs, args.ref)
    except (CannotReadError, subprocess.CalledProcessError, OSError) as exc:
        print(f"cannot read x402-rs: {exc}", file=sys.stderr)
        return 2
    text = json.dumps(fresh, indent=2) + "\n"
    if not args.check:
        OUT_PATH.write_text(text, encoding="utf-8")
        print(f"wrote {OUT_PATH.relative_to(REPO)} from {fresh['source']['commit']}")
        return 0
    try:
        recorded = json.loads(OUT_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        print(f"{OUT_PATH.relative_to(REPO)} is missing or unreadable", file=sys.stderr)
        return 1
    if recorded.get("chainIds") != fresh["chainIds"]:
        print(f"drift: the chain ids at {fresh['source']['commit']} differ", file=sys.stderr)
        return 1
    if recorded.get("source", {}).get("commit") != fresh["source"]["commit"]:
        print(f"same ids; the fixture names {recorded['source']['commit']}, "
              f"{args.ref} is {fresh['source']['commit']} (re-run to move the pointer)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
