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
network access. The concurrent TypeScript ERC-8004 fixture has not yet been
available for comparison; this remains a coordination item.

`docs/near-erc8004.md` documents the sponsor registration/transfer flow, proof
feedback, verified summaries, explicit storage budgets and local signing. The
package exports the new surface and declares 0.108.0. No existing registry/EVM
module or fixture is modified.

## Verification in progress

Python 3.11.4 and ruff 0.17.3 from a fresh CI-extras environment; Node 20.20.2.
The original base suite reached 83% without failures before its 900-second limit;
a complete run is underway in a separate checkout of the exact base commit.
OWS 1.4.2 has no Windows wheel, so its Linux import gate is not reproducible here.
The existing platform skips remain unchanged.

Cross-language conformance against TS main f15b4295: 512 checks pass. ERC-8128
vector comparison, NEAR golden-flow conformance and legacy registry parity against
CI's pinned b4b7030 fixture pass. The new focused suite and mutation evidence are
reported in the PR after final verification.
