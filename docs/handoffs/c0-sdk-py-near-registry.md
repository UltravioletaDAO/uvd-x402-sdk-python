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
  bounded decoding, complete shape/content checks and preserved U64/U128 strings.
- Error enum and final execution outcome parsing; strict four-event parsing;
  receipt parsing restricted to the registered executor on the selected network;
  deterministic filtered/weighted summaries and Unicode 17.0.0 display sanitation.
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
The fixture is now byte-identical to TypeScript PR #69 at b4b703061:
6,784 bytes, SHA-256 `80eeb72f0ee5a4a933b8592f88387d6c4a1f2234a97b02a1f94969878294be38`.
CI runs `scripts/near_registry_parity.py` against a separate checkout of that
reviewed commit; its other cross-language checks keep following TS main.
The independent serializer reproduces the same `calls`, `args_json`, decimal
string nonce and `events[{log, parsed}]` format.

## Verification and self-review (round 1)

Python 3.11.4, Node 20.20.2, ruff 0.17.3. Tests ran with closed HTTP(S) proxies.
Base e118463: **8008 passed, 76 skipped**, zero failures.
Final branch: **8535 passed, 76 skipped**, zero failures.
No new failures or skips. Focused registry and parity tests: **526 passed**.

The mutation runner kills **118/118** mutations (M1-M118), with no timeouts.
It includes canonical signing of reordered/null args (M95/R9), exact event
version (M96/R11), E06 precedence (M97/R13), content validators, receipt emitter
and network isolation, Unicode removal and the cross-SDK byte comparison.
Four additional in-memory mutations of predicate/type/surrogate error handling
also fail their regression tests. Each child is bounded to 120 seconds; source
files are never modified and the tracked tree stays clean.

Boundary tests cover exact limits/+1, uppercase/whitespace, empty/null values,
ASCII/UTF-8 lengths, malformed RPC receipts, lookalike executors, wrong networks,
invalid event batches and the refuter's Unicode cases. One independent
adversarial reviewer found no P1/P2: 2,790 content cases against the real TS
module give identical verdicts, and all 1,114,112 Unicode positions give the same
6,319 removed points (SHA-256 pinned in the test).

The fixture check and formatting/lint pass. The new package export block adds
no ruff findings: 43 baseline findings remain outside it. Cross-language
conformance passes **512 checks**; NEAR golden-flow conformance and ERC-8128
fixture checks pass against TypeScript main f19902b (2.110.0), built with Node 20.

`preci.py --ejecutar` exits 0 with no cheap steps selected for this repository;
the actual CI test/conformance commands ran explicitly. `import ows` cannot run
on Windows (1.4.2 ships only Linux/macOS wheels); the existing platform skips
remain and the real-library gate is unchanged for CI Linux.

No merge of the PR, deploy or package publication was performed.

## Round 2: receipt execution status

`parse_registry_receipt_events` now checks both the registered executor and
`outcome.status`. A string `SuccessValue` or `SuccessReceiptId` is required;
any `Failure` key discards the entire receipt, including null/false failures
and ambiguous success/failure objects. Unknown, absent, null and malformed
statuses are skipped. Empty success strings and extra non-Failure fields are
accepted, matching TypeScript PR #69 at `14d7196` (the owner's later parity
requirement supersedes the earlier one-key suggestion). The log-only helper's
docstring also names both prerequisites.

Python 3.11.14, Node 20.20.2: base **8008 passed, 76 skipped**, original head
**8535 passed, 76 skipped**, final **8597 passed, 76 skipped**, no failures.
The 62 new tests give 52 failures before the fix and all pass after it; the
focused suite has 588 passing cases. M1-M128 all die, including the 10 new
status mutations M119-M128; no timeouts. The tests retain the previous R9/R11/R13
regressions and successful receipt/log ordering through rejected receipts.

Actual TS/Python status comparison: 4,464 cases, zero differences after the fix
(712 before). An independent adversarial review found no P1/P2: 4,226 additional
JSON/batch/network/executor cases agree, 14 Failure/order invariants pass, and
five alternative mutations die. All tests and mutations run with closed
HTTP(S) proxies and bounded subprocess timeouts.

After integrating `origin/main` e118463, the 512 cross-language checks, NEAR
golden-flow conformance, ERC-8128 vectors, registry byte parity and independent
serializer pass. The registry fixture and its CI pin are unchanged. OWS still
has no Windows wheel; its Linux CI gate remains intact. No PR merge, deployment
or package publication is part of this round.
