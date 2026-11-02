# c0-sdk-py-near handoff

H-7: new offline module uvd_x402_sdk.near_escrow. Public API and facilitator envelope:
[../near-escrow.md](../near-escrow.md). Release target 0.104.0; c0der publishes after merge.
No mainnet/testnet transaction, merge, deploy or publish from this worker.

Sources remeasured: Arras escrow-core/src/id.rs canonical msg and preimage;
escrow-v1/src/lifecycle.rs release/refund/reclaim; escrow-v1/src/withdraw.rs withdraw() and
withdraw_for(account), monotoken. x402-rs origin/main b46ae081, src/chain/near/escrow.rs:
authorize gas 38..100 TGas, lifecycle 3..30, withdrawals 29..50; payload and requirements.extra.nearEscrow.

Frozen vector byte copy is tests/fixtures/near-escrow.json (sha256 in the test and API doc).
Actual TS H-6 checkout cross-check: 3 messages/preimages/ids plus 21 signed delegates identical.
Budget is a Protocol and a fail-closed reserved entry point; v2 ABI/vectors remain upstream work.

Final test table, base failures, mutation results, adversarial review and PR: see the PR body
and uncommitted ENTREGA-c0der.md / ESTADO-c0der.md in the worker root.

Local verification (Python 3.11.4 / Node 20.20.2, no production credentials/network):

| Check | Result |
| --- | --- |
| Frozen Rust vectors and independent NEP-366 wire/envelope tests | 133 passed |
| Actual H-6 TS parity, PR66 d85118ae | V-1..V-3 and 21 signed delegates identical |
| Source guards and serialization mutations | M1..M40 killed, each pytest exit1, no timeouts; source untouched |
| Existing CI cross-language harness against isolated TS main 2016c3e | 512 checks across 9 phases passed |
| Wheel 0.104.0, fresh base then near extra | base builders without crypto; near signing independently verified |
| Ruff0.17.3 / black / pip check | passed |
| Three-dot diff secret scan, gitleaks8.30.1 | zero after exact public USDC account exclusions; defaults retained |

One adversarial refuter (gpt-6.1-sol xhigh) found no other P1/P2 in the signing module.
Its packaging-extra and lifecycle amount-zero documentation findings were fixed and verified.
Agent tokens are not measurable through the available API.

Base coverage: 7615 cases (7539 passed, 76 skips), zero failures. First600s run completed6135;
remainder1490 overlapped10 and passed. Windows cannot run the preexisting mandatory import-ows
step (no Windows wheel); Linux CI retains that gate. Final full suite: 7672 passed, 76 skipped, 0 failures in 797.46s; same skips as base.
No other Python PRs were open when measured. Other team fronts use separate repositories;
merge-tree with TS is inapplicable, so actual byte parity validates that boundary.
