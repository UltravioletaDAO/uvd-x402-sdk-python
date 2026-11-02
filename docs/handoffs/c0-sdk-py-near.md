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
