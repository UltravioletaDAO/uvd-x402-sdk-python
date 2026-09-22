"""The README prints what get_networks_by_token("eurc") returns.

That example said "Ethereum, Base, Avalanche C-Chain" for a year and was still
saying it after 0.86.0 registered EURC on Arc mainnet and testnet, so a reader
concluded EURC did not work on Arc. Pin the printed output to the registry.
"""
import re
from pathlib import Path

from uvd_x402_sdk.networks.base import get_networks_by_token


def test_readme_eurc_example_output_matches_registry():
    readme = (Path(__file__).resolve().parents[1] / "README.md").read_text(encoding="utf-8")
    match = re.search(r"# Output: EURC available on: (.+)", readme)
    assert match, "README lost the get_networks_by_token('eurc') example"
    printed = [name.strip() for name in match.group(1).split(",")]
    registry = [network.display_name for network in get_networks_by_token("eurc")]
    assert "Arc" in registry and "Arc Testnet" in registry
    assert printed == registry
