# c0-sdk-py-near-r2 handoff

Arras escrow 0.105.0. Public API: [near-escrow.md](../near-escrow.md).
The immutable NEAR_ESCROW_CONTRACTS registry contains arras-v1b-1101.testnet on near:testnet
and v1.arras-escrow.near on near:mainnet. Authorization and every lifecycle method reject
other contracts before reading public_key or signing. NEAR_ESCROW_TOKENS is immutable too.
Delegates require nonce < max_block_height * 1_000_000 with exact integers.

The six RSDKPY-78 follow-ups are covered: integer amounts and deadlines stay decimal strings
in signed JSON; release/refund guide examples use the capturer key and its own nonces;
the missing dependency error names the near extra; runtime account validation accepts object
and checks str (resolving the NEAR mypy error); token registry mutation fails; withdraw_for
correctly documents that the destination may be the caller.

The frozen Rust fixture is unchanged, SHA256
fe81bfc72bd1e90a5afb86fb00eece59cbd78fde983eda397c797bf65351709b.
V-1..V-3 msg/preimage/IDs remain identical. Runtime parity against TypeScript 8b15f0f compares
42 signed delegates (21 per network), both registries and 54 refusals before key access.
The TypeScript-owned runner also passes with 21 delegates and 5 refusals.

Local verification uses PowerShell, Python 3.11.14 (CI) / 3.9.24 (wheel), Node 20.20.2 (CI),
Ruff 0.17.3 and Black 26.10.1. The focused NEAR suite has 271 passing tests. Base origin/main
4d22320 has 7672 passing tests, 76 skips and no failures. Full final suite, mutations,
CI job commands and adversarial review are recorded in the pull request's verification table.
Windows has no OWS 1.4.2 wheel, so its mandatory Linux import cannot run locally; the CI gate
is unchanged. Mypy has 170 preexisting errors versus 171 in base, with none in near_escrow.py.

Offline tests use closed proxies except loopback and an empty AWS credentials file, no profile.
No RPC, transaction, merge of a PR, deploy or publish is performed by this worker.
c0der controls the release after review. Existing commits b14f2ab, 9ccd948 and 1924d91 are preserved.
