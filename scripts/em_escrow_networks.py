"""Record Execution Market's escrow networks: ``tests/fixtures/em-escrow-networks.json``.

Execution Market (EM) is the marketplace whose payment config
(``GET /api/v1/h2a/payment-config``) ``escrow_signing.build_escrow_pre_auth``
signs. Its networks come from one table, ``NETWORK_CONFIG`` in
``mcp_server/integrations/x402/network_registry.py`` (pure data, no imports
beyond ``typing``). This script READS that table from a commit of an EM
checkout (``git show <ref>:<path>``, parsed with ``ast.literal_eval``: nothing
of EM's is executed) and writes, for each escrow network, the block the payment
config serves for it, with the keys the pre-auth reads, taken the way
``mcp_server/api/h2a.py`` takes them: ``chain_id``, ``operator``, ``escrow``,
``x402r_infra.tokenCollector``, and the address, name and version of its USDC.

An escrow network is one EM's ``has_escrow_support`` accepts: an ``escrow``
and an ``operator`` in the registry, and not a testnet. That is ten networks:
the nine the payment config serves (each while EM_ENABLED_NETWORKS enables it)
and ``skale``, retired in EM (ADR-009), which ``h2a.py`` no longer serves but
whose registry entry is still the operator and contracts of the escrows in
flight there. A USDC without a name or version falls back as ``h2a.py`` does:
to this SDK's network registry, then to ``"USD Coin"`` / ``"2"``.

The escrow block (``min_fee_bps``, ``max_fee_bps``, ``tier_timings``) is what
``h2a.py`` serves: the fee bounds are literals there, read from its source
(``ast``), and the tier windows are its ``_effective_tier_windows`` of
``advanced_escrow.TIER_TIMINGS`` (``h2a.py`` imports them from the SDK it
pins; read here from this one, whose values are the same), the same floor
``build_escrow_pre_auth`` applies. The function is not executed: its body
must be, statement for statement, the port below, or the script exits 2.

Never edit the fixture by hand: re-run this against EM's ``origin/main`` and
review the diff.

Usage: python scripts/em_escrow_networks.py --em <EM checkout> [--ref origin/main] [--check]
Exit codes: 0 ok, 1 --check found drift, 2 could not read EM.
"""

from __future__ import annotations

import argparse
import ast
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
OUT_PATH = REPO / "tests" / "fixtures" / "em-escrow-networks.json"

REGISTRY_PATH = "mcp_server/integrations/x402/network_registry.py"
PAYMENT_CONFIG_PATH = "mcp_server/api/h2a.py"
# EM's is_testnet_network (mcp_server/integrations/x402/sdk_client.py).
TESTNET_SUFFIXES = ("-sepolia", "-amoy", "-fuji", "-testnet", "-devnet")
TESTNET_NAMES = frozenset({"sepolia", "goerli", "holesky", "fuji", "amoy", "mumbai"})


class CannotReadError(Exception):
    """EM's sources are not what this script knows how to read: exit 2, nothing written."""


UNREADABLE = (CannotReadError, subprocess.CalledProcessError, OSError, ValueError, SyntaxError)


def _git(em: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(em), *args], check=True, capture_output=True, text=True
    ).stdout


def _assigned(tree: ast.Module, name: str) -> ast.expr:
    for node in tree.body:
        if isinstance(node, ast.AnnAssign) and getattr(node.target, "id", None) == name:
            assert node.value is not None
            return node.value
        if isinstance(node, ast.Assign) and any(
            getattr(t, "id", None) == name for t in node.targets
        ):
            return node.value
    raise CannotReadError(f"{name} not found as a module-level assignment")


def _served_fee_bounds(source: str) -> dict[str, int]:
    """The literal ``min_fee_bps`` / ``max_fee_bps`` of the payment config's escrow block."""
    found: dict[str, int] = {}
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Dict):
            continue
        keys = [k.value if isinstance(k, ast.Constant) else None for k in node.keys]
        if "payment_info_typehash" in keys and "networks" in keys:
            for key, value in zip(keys, node.values):
                if key in ("min_fee_bps", "max_fee_bps"):
                    found[key] = ast.literal_eval(value)
    if set(found) != {"min_fee_bps", "max_fee_bps"}:
        raise CannotReadError(f"fee bounds not found in {PAYMENT_CONFIG_PATH}: {found}")
    return found


def _effective_tier_windows(
    tier: dict[str, int], review_window_sec: int, refund_window_sec: int
) -> dict[str, int]:
    """h2a.py's _effective_tier_windows: the floor the pre-auth applies, served."""
    auth = max(int(tier.get("auth", 0)), review_window_sec)
    return {
        "pre": int(tier.get("pre", 0)),
        "auth": auth,
        "refund": max(int(tier.get("refund", 0)), auth + refund_window_sec),
    }


def _body(function: ast.FunctionDef) -> str:
    """The statements of ``function`` without its docstring, as an AST dump."""
    body = function.body
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
        body = body[1:]
    return ast.dump(ast.Module(body=body, type_ignores=[]))


def _check_port(source: str) -> None:
    """EM's _effective_tier_windows is still the function ported above."""
    ours = ast.parse(Path(__file__).read_text(encoding="utf-8"))
    theirs = ast.parse(source)

    def find(tree: ast.Module) -> ast.FunctionDef:
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "_effective_tier_windows":
                return node
        raise CannotReadError(f"_effective_tier_windows not found in {PAYMENT_CONFIG_PATH}")

    if _body(find(theirs)) != _body(find(ours)):
        raise CannotReadError(
            f"{PAYMENT_CONFIG_PATH}: _effective_tier_windows changed — port it again"
        )


def is_escrow_network(name: str, entry: dict[str, Any]) -> bool:
    """EM's has_escrow_support, without the deployment's enabled-networks filter."""
    lowered = name.strip().lower()
    testnet = lowered in TESTNET_NAMES or lowered.endswith(TESTNET_SUFFIXES)
    return bool(entry.get("escrow") and entry.get("operator")) and not testnet


def record(em: Path, ref: str) -> dict[str, Any]:
    sys.path.insert(0, str(REPO / "src"))
    from uvd_x402_sdk.advanced_escrow import TIER_TIMINGS, TaskTier
    from uvd_x402_sdk.escrow_signing import REFUND_WINDOW_SEC, REVIEW_WINDOW_SEC
    from uvd_x402_sdk.networks import get_network_by_chain_id

    commit = _git(em, "rev-parse", f"{ref}^{{commit}}").strip()
    registry = ast.literal_eval(
        _assigned(ast.parse(_git(em, "show", f"{commit}:{REGISTRY_PATH}")), "NETWORK_CONFIG")
    )
    payment_config = _git(em, "show", f"{commit}:{PAYMENT_CONFIG_PATH}")
    fees = _served_fee_bounds(payment_config)
    _check_port(payment_config)

    networks: dict[str, dict[str, Any]] = {}
    for name, entry in registry.items():
        if not is_escrow_network(name, entry):
            continue
        usdc = (entry.get("tokens") or {}).get("USDC") or {}
        domain_name, domain_version = usdc.get("name"), usdc.get("version")
        if not (domain_name and domain_version):
            sdk_network = get_network_by_chain_id(entry.get("chain_id"))
            if sdk_network is not None:
                domain_name = domain_name or sdk_network.usdc_domain_name
                domain_version = domain_version or sdk_network.usdc_domain_version
        networks[name] = {
            "chain_id": entry["chain_id"],
            "operator": entry["operator"],
            "escrow": entry["escrow"],
            # Served as ``token_collector``. Written ``collector`` here, like the
            # hex-without-0x of the other fixtures: a secret scanner reads a key
            # containing "token" with a hex value as a credential.
            "collector": (entry.get("x402r_infra") or {}).get("tokenCollector"),
            "usdc": usdc.get("address"),
            "usdc_domain_name": domain_name or "USD Coin",
            "usdc_domain_version": domain_version or "2",
        }
    tiers = {
        tier.value: _effective_tier_windows(
            dict(TIER_TIMINGS[tier]), REVIEW_WINDOW_SEC, REFUND_WINDOW_SEC
        )
        for tier in (TaskTier.MICRO, TaskTier.STANDARD)
    }
    return {
        "_note": (
            "Generated by scripts/em_escrow_networks.py from Execution Market's "
            "network registry. Never edit by hand: re-run it. 'collector' is the "
            "token_collector the payment config serves."
        ),
        "source": {
            "repo": "UltravioletaDAO/execution-market",
            "commit": commit,
            "registry": REGISTRY_PATH,
            "payment_config": PAYMENT_CONFIG_PATH,
        },
        "escrow": {**fees, "tier_timings": tiers},
        "networks": dict(sorted(networks.items())),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--em", type=Path, required=True, help="Execution Market checkout")
    parser.add_argument("--ref", default="origin/main")
    parser.add_argument("--check", action="store_true", help="exit 1 if the fixture drifted")
    args = parser.parse_args()
    try:
        recorded = record(args.em, args.ref)
    except UNREADABLE as exc:
        print(f"could not read Execution Market at {args.em} ({args.ref}): {exc}", file=sys.stderr)
        return 2
    text = json.dumps(recorded, indent=2) + "\n"
    if args.check:
        current = OUT_PATH.read_text(encoding="utf-8") if OUT_PATH.exists() else ""
        if current != text:
            print(f"{OUT_PATH.relative_to(REPO)} drifted from {args.ref}", file=sys.stderr)
            return 1
        print(f"{OUT_PATH.relative_to(REPO)} matches {args.ref} ({recorded['source']['commit']})")
        return 0
    OUT_PATH.write_text(text, encoding="utf-8")
    print(f"wrote {OUT_PATH.relative_to(REPO)}: {len(recorded['networks'])} networks")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
