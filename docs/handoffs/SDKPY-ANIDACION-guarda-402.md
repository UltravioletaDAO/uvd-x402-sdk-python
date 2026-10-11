# SDKPY-ANIDACION: bounded parsing of third-party payment challenges, in the SDK

## Estado

- **Hecho**: `uvd_x402_sdk.bounded_json` (`loads_bounded`, `json_depth_within`,
  `JSONBoundsError`, `MAX_CHALLENGE_BYTES`, `MAX_JSON_DEPTH`), used by
  `payment_challenge_from` (header and body), `X402Client.fetch` (402 body),
  `dx402.parse_evidence_header` and the `receipts` header readers.
  `max_depth` has a hard cap; `payment_challenge_from` validates its limits
  before reading. `tests/test_bounded_json.py`: downstream cases, encodings,
  fuzz against `json.loads`, subprocess cases. Version 0.98.0, CHANGELOG,
  CLAUDE.md. Review round REF-SDKPY-69 applied.
- **Falta**: nothing in this repo. Downstream adoption (replace the local
  guard with `uvd_x402_sdk.loads_bounded` / `payment_challenge_from`) is a
  separate change in the consumer, after 0.98.0 is published.
- **Proximo paso**: review of the PR; publishing 0.98.0 is the owner's call.

## Decisions

- **Where**: a module of its own next to `policy.py` / `dx402.py`, exported
  from the package root. `policy.parse_accepts` takes parsed JSON; the
  parsing happens in `payment_challenge_from` and `fetch`, so that is where
  the bound applies.
- **Ceilings**: the values the downstream reader already enforced, both
  overridable per call (keyword-only); `max_depth` cannot exceed a fixed cap.
- **Header over the size ceiling** is not base64-decoded at all; the body is
  still tried. A `dict` body was parsed by the caller and is taken as given.
- **Charset**: never consulted. Bytes are decoded with
  `json.detect_encoding`, what `json.loads` does.
- **Not in scope**: `X402Client.fetch` downloads the whole 402 body before
  the size ceiling applies (the ceiling bounds the parse, not the download),
  as on `main`.
