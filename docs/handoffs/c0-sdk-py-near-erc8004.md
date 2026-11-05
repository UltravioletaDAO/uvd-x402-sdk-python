# NEAR ERC-8004 implementation handoff

Base: `origin/main` at `f76c6d84a04a26909cc9f511d94b0f6fc2379aeb` (0.107.0).

## Inventory

The existing `near_registry.py` implements a separate registry protocol. Its
wallet-selector descriptor shape, ordinary NEAR V0 transaction signing pattern,
injectable JSON-RPC transport and receipt provenance checks provide the model
for the new client. Its files and EVM ERC-8004 implementation remain unchanged.

Reusable primitives from `near_escrow.py`: `NearLocalSigner`, NEAR account
validation, bounded unsigned integer validation and compact JSON. Reuse
`BorshSerializer` from `networks.near`, strict record/boolean predicates from
`near_events`, the canonical base58 decoder and `loads_bounded`.

The new module needs independent identity/reputation/verifier deployment tables,
config-derived storage deposits and gas with SDK ceilings, canonical signed
values, nanosecond wallet deadlines, NEP-171 identity transfers, proof-backed
feedback and complete view validation. Its event parser must associate each event
with the correct contract role as well as the selected network and successful
receipt. Callback refusals must remain distinct from successful feedback IDs.

Fixtures will be public RPC recordings of both deployments and the existing
registration, transfer and proof-feedback transactions. Tests must replay the
recordings offline. No transaction submission is part of this implementation.

## Implemented surface

The client covers 16 offline transaction methods and all identity/reputation
views, plus verifier configuration. Wallet signing requires the selected network
and a config snapshot. Deposits come from validated configuration and are capped
at recorded maxima plus 5%; growth operations require an explicit budget. Inputs,
outputs, receipt emitters and callback outcomes have independent validation.

The generator recorded 20 public views and six historical transaction outcomes
on 2026-11-04. The new fixture includes real on-chain events and exact view
arguments. `--compare` checks a supplied TypeScript fixture byte for byte without
network access. Byte equality was measured against the TypeScript ERC-8004
fixture in PR #71 at head `2542ad95008dc9e93c16dc9ccc47fe0437754b7a`:
both files have SHA-256
`07e8a80665811afe9ad6c95bf9a811b3cd4b31cb81a9f7ec57caadf7331a7b6c`.

`docs/near-erc8004.md` documents the sponsor registration/transfer flow, proof
feedback, verified summaries, explicit storage budgets and local signing. The
package exports the new surface and declares 0.108.0. No existing registry/EVM
module or fixture is modified.

## Verification

Python 3.11.4 and ruff 0.17.3 from a fresh CI-extras environment; Node 20.20.2.
Tests ran with closed HTTP(S) proxies. The complete isolated base suite passed
8597 tests with 76 skips; the final suite passed 9141 tests with the same 76
skips and zero failures (978.43 seconds). The 543 focused tests pass with 96%
branch-aware coverage. All 102 guard mutations are killed, with no timeouts.

Cross-language conformance against TS main f15b4295 passes all 512 checks.
ERC-8128 vector comparison, NEAR golden-flow conformance and legacy registry
parity against CI's pinned b4b7030 fixture pass. Lint and formatting pass for
all six new Python files; the package export file adds no lint findings.
The three-point diff passes the secret scanner and whitespace check.

`preci.py --repo . --base origin/main --ejecutar` exits 0 with no cheap steps
selected; the CI commands were therefore run independently. OWS 1.4.2 has no
Windows wheel, so its Linux import gate cannot be reproduced on this host.
The Linux CI gate remains unchanged.

Fixture: 212059 bytes; SHA-256
`07e8a80665811afe9ad6c95bf9a811b3cd4b31cb81a9f7ec57caadf7331a7b6c`.
Byte-identical to `src/providers/near/vectors/near-erc8004.json` in TypeScript
PR #71 at head `2542ad95008dc9e93c16dc9ccc47fe0437754b7a`, with the same SHA-256
above. The offline `--compare` check passes. Existing cross-language checks pass
locally.
