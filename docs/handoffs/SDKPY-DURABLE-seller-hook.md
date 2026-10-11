# SDKPY-DURABLE - durable evidence in the FastAPI seller middleware

## Estado

- Hecho: `src/uvd_x402_sdk/durable.py` (`DurableConfig`, `durable_evidence`), wired into
  `fastapi_require_payment` and `X402Middleware` (`durable=`, off by default);
  `tests/test_fastapi_durable.py` (offline); CHANGELOG and CLAUDE.md notes.
- Falta: CI of the PR.
- Proximo paso: review of the PR; wiring Flask / Django / Lambda is a separate task.

## Medido contra origin/main 3c8105051

- `anchor_evidence` seals/anchors and returns a skip instead of raising: confirmed.
- FastAPI did not call it and `require_payment` took only amount/message: confirmed.
- Baseline suite (Python 3.11.15, network closed): 5514 passed, 1 skipped, 0 failed.

## Decisiones

- The option is exposed only where the route's response exists after the settle
  (`fastapi_require_payment`, `X402Middleware`). The dependencies
  (`FastAPIX402.require_payment`, `X402Depends`) run before the route; capturing a body
  there would be simulated, so they do not take it.
- The payer public key is a resolver in the config (`payer_key(result, x_payment)`);
  the SDK does not derive or read any key here. Without it: `no_payer_key`.
- Body limit 32 MiB (`DEFAULT_MAX_BODY_BYTES`), as the Rust middleware.
