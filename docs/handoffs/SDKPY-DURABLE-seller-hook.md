# SDKPY-DURABLE - durable evidence in the FastAPI seller middleware

## Estado

- Hecho: `src/uvd_x402_sdk/durable.py` (`DurableConfig`, `durable_evidence`), wired into
  `fastapi_require_payment` and `X402Middleware` (`durable=`, off by default);
  `tests/test_fastapi_durable.py` (offline); CHANGELOG and CLAUDE.md notes.
- Ronda 2 (parity note from the TS twin, uvd-x402-sdk-typescript #52/#53): kept no anchor
  on receipt pending/unknown with success:true, and `no_payer_key`; added tests for anchor after
  the one settle, per-request isolation, and a failed stream that keeps the bytes read
  (`_buffered` + `_RecordsRouteFailure`). Announced `Content-Length` over the limit is not read.
- Ronda 3 (REF-SDKPY-71, comment 6110745949): a body the hook cannot read (decorator +
  `FileResponse`) and `text/event-stream` pass through with `not_capturable`; memoryview /
  bytearray chunks are copied as bytes; `wants_evidence` skips replays, `HEAD` and a route's
  own `X-Durable-Evidence`; status 2xx only; no fixed `pointer` (inline sealed blob per sale);
  one `timeout` (5 s) over capture + anchor (`timeout` notice); `payer_key` / `max_body_bytes` /
  `timeout` checked when built; default limit 64 KiB (`ANCHOR_MAX_REQUEST_BYTES`); the
  post-hook never raises; decorator dict returns go through `jsonable_encoder`. Tests for
  headers + `PAYMENT-RESPONSE` + `BackgroundTask` on whole / stream / too_large, F5, F15.
- Falta: CI of the round-3 push.
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
- Body limit 64 KiB (`DEFAULT_MAX_BODY_BYTES = ANCHOR_MAX_REQUEST_BYTES`, as the TS SDK) since
  ronda 3: the sealed body goes inline, so a larger one could never be anchored (was 32 MiB,
  the Rust middleware's, which uploads through its own sink).
