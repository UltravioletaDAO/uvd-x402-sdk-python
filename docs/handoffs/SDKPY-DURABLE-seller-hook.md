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
  post-hook never raises; decorator dict returns went through `jsonable_encoder` (replaced in ronda 4). Tests for
  headers + `PAYMENT-RESPONSE` + `BackgroundTask` on whole / stream / too_large, F5, F15.
- Ronda 4 (VER-SDKPY-71, comment 6113142684): `_buffered` read each `__anext__` in a new
  task, which broke a route generator holding a contextvar token or an anyio cancel scope across
  a `yield` (truncated paid stream in the decorator). Now one task (`pump`) walks the generator
  and feeds a one-slot queue; the capture waits on it with the deadline; a cancelled capture
  cancels the pump. Decorator returns that are not a `Response` are no longer converted by
  durable: FastAPI serializes them (`status_code`, `response_model`, `response_class`) and they
  are not anchored. With a confirmed receipt the pre-existing `JSONResponse` conversion happens
  with or without durable (same bytes; those are anchored) - separate row. A `payer_key` result
  that is not bytes is `no_payer_key`. Tests: contextvar / cancel scope (decorator + middleware,
  durable on vs off), status 201 / response_model / PlainTextResponse (on vs off), confirmed
  receipt + dict, cancelled capture closes the route stream. On base 0c02077: 10 of them red.
- Ronda 4b (c0der, same comment): main merged (#69, 0.98.0) and version 0.99.0. P2 done the
  way asked: `_route_response` builds what FastAPI builds for the route (response_model,
  response_class, status_code, injected `Response`), in the durable path AND the receipt branch
  (which sent `JSONResponse(raw)` before, durable or not); the anchor seals those bytes. A
  failing stream is `not_capturable` (TS name). Regression test under real uvicorn on
  127.0.0.1 (contextvar + anyio cancel scope across yields, decorator + middleware). `uvicorn`
  added to the `dev` extra for that test. The deadline stays on the queue wait, not
  `move_on_after` in the route's task: cancelling there would cut the paid stream on timeout.
  An anchor in flight is bounded by its HTTP timeout (what is left of the budget); a hard abort
  of the thread is not done (follow-up, with a BYO sink).
- Falta: CI of the round-4 push.
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
