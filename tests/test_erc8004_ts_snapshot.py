"""Exercise the snapshot's runtime inputs without npm or a fixture rewrite."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")


def test_snapshot_queries_caip2_even_when_the_type_does_not_name_the_alias():
    if NODE is None:
        pytest.skip("Node is required to run the TypeScript snapshot helper")
    script = r"""
const { networkAnswers } = await import(process.argv[1]);
const converted = [];
const aliases = { 'base-mainnet': 'base', 'eip155:8453': 'base',
  'eip155:46630': 'robinhood-testnet' };
const wireNetwork = (n) => aliases[n] ?? n;
const backend = {
  ERC8004_CONTRACTS: {
    base: { identityRegistry: 'evm-registry' },
    'base-mainnet': { identityRegistry: 'evm-registry' },
    'robinhood-testnet': { identityRegistry: 'evm-registry' },
    'ethereum-sepolia': { identityRegistry: 'evm-registry' },
    solana: { agentRegistryProgram: 'solana-program' },
  },
  wireNetwork,
  supportsRelayedFeedback: (n) => wireNetwork(n) === 'base',
  supportsSolanaFeedback: (n) => wireNetwork(n) === 'solana',
};
const chainToCAIP2 = (n) => {
  converted.push(n);
  return { base: 'eip155:8453', 'base-mainnet': 'eip155:8453',
    'robinhood-testnet': 'eip155:46630' }[n] ?? n;
};
// No CAIP-2 members in this declaration: the runtime inputs must still include them.
const answers = networkAnswers(backend, ['base', 'base-mainnet', 'solana'], chainToCAIP2);
console.log(JSON.stringify({ answers, converted }));
"""
    result = subprocess.run(
        [
            NODE,
            "--input-type=module",
            "-e",
            script,
            (ROOT / "scripts/erc8004_ts_snapshot_helpers.mjs").as_uri(),
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    data = json.loads(result.stdout)
    assert set(data["converted"]) == {
        "base",
        "base-mainnet",
        "robinhood-testnet",
        "ethereum-sepolia",
    }
    answers = data["answers"]
    expected = {
        "base": "base",
        "base-mainnet": "base",
        "solana": "solana",
        "robinhood-testnet": "robinhood-testnet",
        "ethereum-sepolia": "ethereum-sepolia",
        "eip155:8453": "base",
        "eip155:46630": "robinhood-testnet",
    }
    assert answers["wireNetwork"] == expected
    assert answers["supportsRelayedFeedback"] == {n: wire == "base" for n, wire in expected.items()}
    assert answers["supportsSolanaFeedback"] == {
        n: wire == "solana" for n, wire in expected.items()
    }
