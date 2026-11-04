# c0-sdk-py-near-registry

Python SDK 0.107.0, from `origin/main` e118463037d9814400fb64dcd3f364157e4992ab (0.106.0).
Interface reference: Arras PR #7, `docs/handoffs/registry-v1.md` at 651fa341;
validation, errors, view shapes and exact event examples checked against its source.
The private checkout stayed outside this repository. No chain calls were made.

## Delivered surface

- `near_registry.py`: immutable per-network registry/escrow tables; static ABI
  validation; four wallet-selector builders; ordinary NEAR V0 signing with
  Ed25519 signature verification and descriptor revalidation before key access.
- `NearRegistryClient`: all nine views, injectable synchronous JSON-RPC transport,
  bounded decoding, complete shape checks and preserved U64/U128 strings.
- Error enum and final execution outcome parsing; strict four-event parsing;
  deterministic filtered/weighted reputation summaries and display sanitation.
- Root exports, usage guide, 0.107.0 manifest, changelog, independent fixture
  generator and guard mutation runner.
- `near_escrow.py` and `near_events.py` are unchanged. Their signer/account/uint/
  compact JSON/Borsh and strict field validators are reused.

## Integration decisions

The wallet shape follows the existing `NearStorageTransaction`: `signerId`,
`receiverId`, `actions`, `params.methodName`, `params.args`, `params.gas`,
`params.deposit`. ABI JSON args themselves remain snake_case and are emitted in
handoff order, without absent optional fields. Explicit `None` is omitted.

`update` defaults to REGISTER_DEPOSIT but permits >=1 yocto, as the contract does.
Actual growth, registration state and payment eligibility require contract state
and are not promised by an offline builder. State-dependent panic codes are in
the enum and outcome parser. The caller must check the selected key is FullAccess;
no offline signature can establish its access-key permission.

Testnet is empty and refused. Mainnet's configured registry was not deployed at
implementation time. Publishing and deployment remain owner-operated steps.

## Fixed parity inputs

`tests/fixtures/near-registry.json` uses these exact inputs:

- network `near:mainnet`, receiver `registry.arras-escrow.near`, signer `payer.near`;
  Ed25519 seed = 32 bytes of 0x01; public key is derived; nonce 1;
  block hash = base58 of 32 bytes of 0x02.
- register: `agent_uri=https://agente.example/card.json`, metadata absent,
  deposit `17510000000000000000000`, gas `30000000000000`.
- give_feedback: escrow `v1.arras-escrow.near`, payment_id = 32 copies of `1`,
  score 90, tag1 `quality`, tag2 `successRate`,
  feedback_uri `https://agente.example/f/1.json`, feedback_hash = 32 copies of `ab`,
  deposit `18640000000000000000000`, gas `100000000000000`.
- revoke_feedback: the same escrow and payment_id, deposit `1`, gas `30000000000000`.

Outputs include each args object's UTF-8 hex; signed register/give_feedback Borsh
base64; all four exact contract event logs and parsed JSON. The fixture generator
uses `struct` and cryptography independently of SDK serialization. Tests also
independently rebuild the transaction bytes and verify signatures.
The TypeScript worker's registry fixture is a separate deliverable for c0der to
compare; this change does not modify its worktree or claim that comparison ran.

## Verification and self-review

Tests use closed HTTP(S) proxies and no production credentials. Validation cases
cover exact limits/+1, lowercase/uppercase, whitespace, `0x`/`0X`, null/empty,
ASCII/Unicode/UTF-8 bytes, bool vs int, u32/u64/u128 limits, mixed-case schemes,
Windows/POSIX-like links, exact receiver and escrow isolation, malformed RPC
containers, partial view records, invalid event batches, pending outcomes,
all known panic/rejection codes, floor rounding and totals above U128.

`python scripts/near_registry_mutations.py` disables every direct rejection guard
and each input validation call independently, adds boundary/signature/table/
aggregation mutations, and disables each view schema. Every subprocess is limited
to 120 seconds; source files are never modified. A clean tree is mandatory.

The registry suite passes 402 tests; the earlier focused NEAR/version run passes
935. All **97/97 mutations are killed** (M1-M97), none by timeout. The initial
90/97 round identified seven missing isolated cases; all seven were added without
removing mutations or changing production code.

The fixture check and formatting/lint pass. Both signed parity transactions also
match `@near-js/transactions` and `@near-js/crypto` 2.5.1 byte for byte; all four
event strings match the contract's exact-format test. CI cross-language passes
512 checks, and the NEAR golden-flow conformance passes against TypeScript main
f19902b78ab230cdd4c3e8809caf450a7436de28 (2.110.0), built with Node 20.20.2.

`preci.py --ejecutar` exits 0 with no cheap steps selected for this repository;
the actual CI test/conformance commands are run explicitly. `import ows` cannot
run on Windows (1.4.2 ships only Linux/macOS wheels); its existing platform skips
remain and the real-library gate is left to CI Linux. Full-suite totals and
baseline differences are recorded in the PR and local delivery report.

Complete offline suite: base **8008 passed, 76 skipped** (895.51s); branch
**8397 passed, 76 skipped** (907.27s). No new failures or skips. The branch suite
was collected before the 14 final guard-isolation cases were added; those cases
pass in the final 402-test registry run, with unchanged production source.
The package was reinstalled offline and reports 0.107.0.

No subagents were used (the specific assignment requires none).
No merge of the PR, deploy or package publication was performed.
