# `/settle` and `/verify` answers of x402-rs 2.40.0

Each JSON file is one answer the facilitator sends, recorded from its own response
code at `UltravioletaDAO/x402-rs` commit `8ee44114` (2.40.0): `status`, the
`Retry-After` header (`retryAfter`, `null` when absent) and the exact `body` bytes.
Nothing here is written by hand. The inputs are the fixtures x402-rs's own tests
already use (`chain_failure_response_tests`, `settlement_unconfirmed_response_tests`,
`writer_forward_pool_tests`, and the mock chain of `proof_of_payment_tests`, which
settles a transfer with the `8004-reputation` extension and emits the proof).

| File | Produced by |
|---|---|
| `settle_success_with_proof.json` | `EvmProvider::settle` against the mock chain, `extra["8004-reputation"]` set; the bytes `post_settle` sends (`serde_json::to_string(&valid_response)`) |
| `settle_success_without_proof.json` | the same settle without the extension |
| `settle_mined_reverted.json` | the `SettleResponse` of a receipt with `status = 0` |
| `forward_failed.json` / `forward_unconfirmed.json` | `forward_failure_response`, hop not delivered / delivered |
| `upstream_rpc_unavailable.json`, `facilitator_signer_unfunded.json`, `broadcast_uncertain.json`, `contract_call_failed.json` | `IntoResponse for FacilitatorLocalError::ContractCall` |
| `settlement_unconfirmed.json` | `IntoResponse for FacilitatorLocalError::SettlementUnconfirmed` |
| `receipt_store_unavailable.json` | `receipts::unavailable` |
| `verify_invalid_signature.json` | the `/verify` rejection of an invalid signature |

The `(ref: …)` uuids and the random payer address change on every run.

To record them again, from a checkout of x402-rs:

```bash
mkdir /tmp/x402rs && git archive 8ee44114 | tar -x -C /tmp/x402rs
cd /tmp/x402rs && git apply -p2 /path/to/this/dir/capture.patch
SDK_CAPTURE_DIR=/path/to/this/dir cargo test --locked -p x402-rs --lib -- \
  sdk_capture the_proof_a_settle_emits a_settle_that_asks_for_no_proof --nocapture
```

`capture.patch` adds two test modules that only write files, and one call in each of
the two proof tests. It changes no facilitator behaviour.
