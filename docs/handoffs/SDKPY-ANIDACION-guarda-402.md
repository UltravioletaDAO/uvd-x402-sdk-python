# SDKPY-ANIDACION: size and nesting guard on a third party's 402, in the SDK

## Estado

- **Hecho**: `uvd_x402_sdk.bounded_json` (`loads_bounded`, `json_depth_within`,
  `JSONBoundsError`, `MAX_CHALLENGE_BYTES` 512 KiB, `MAX_JSON_DEPTH` 64),
  wired into `payment_challenge_from` (header base64 / raw, body str / bytes)
  and `X402Client.fetch` (402 body). `tests/test_bounded_json.py`: downstream
  cases (header / BOM / UTF-16/32 / misleading charset matrix, depth limit,
  literal brackets, junk base64, invalid bytes), fuzz against `json.loads`,
  100000-bracket cases in a subprocess. Version 0.98.0, CHANGELOG, CLAUDE.md.
- **Falta**: nothing in this repo. Downstream adoption (replace the local
  guard with `uvd_x402_sdk.loads_bounded` / `payment_challenge_from`) is a
  separate change in the consumer, after 0.98.0 is published.
- **Proximo paso**: review of the PR; publish 0.98.0 is the owner's call.

## Measured on the base (0.97.0, origin/main 3c81050)

Python 3.11, `import uvd_x402_sdk` (imports `py_ecc`, recursion limit
100000). `payment_challenge_from` with `"[" * 1000` and `"[" * 100_000`:
base64 header, raw header and bytes body all ended the interpreter with
exit 139 (SIGSEGV). `X402Client.fetch` parsed the 402 with `resp.json()`,
the same `json.loads(resp.content)`.

## Decisions

- **Where**: a module of its own next to `policy.py` / `dx402.py`, exported
  from the package root. `policy.parse_accepts` takes parsed JSON; the
  parsing happens in `payment_challenge_from` and `fetch`, so that is where
  the guard runs.
- **Ceilings**: 512 KiB and 64 levels, the values the downstream reader
  already enforced. Both are overridable per call (keyword-only).
- **Header over the size ceiling** is not base64-decoded at all; the body is
  still tried. A `dict` body was parsed by the caller and is taken as given.
- **Charset**: never consulted. Bytes are decoded with
  `json.detect_encoding`, what `json.loads` does.
