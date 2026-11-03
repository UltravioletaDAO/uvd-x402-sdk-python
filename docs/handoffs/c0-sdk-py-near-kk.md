# c0-sdk-py-near-kk — offline NEAR golden flow (0.106.0)

Stack this branch after c0-sdk-py-near-r2 (0.105.0). It consumes that branch's
immutable per-network Arras deployment registry rather than defining another.

- Public `sign_nep413` / `nep413_preimage`: official NEP-413 example, Unicode,
  callback absent/empty distinction, independent Ed25519 verification.
- Public `sign_exact_near_delegate`: one native-USDC `ft_transfer`, 1 yoctoNEAR,
  <=30 TGas, exact integer encoding and nonce ceiling; reuses the NEP-366 encoder.
- Public payer-funded Arras/USDC NEP-145 builders. Arras validates the network
  registry before building. Caller signs a normal transaction and pays gas/deposit.
- Public typed `parse_arras_events`: all eleven v1 Rust variants, bounded JSON,
  unknown/malformed logs skipped atomically per batch. Check receipt executor and
  outcome separately; parsing is not proof of successful settlement.
- EM#659 can replace its local storage/event copies with these SDK helpers.
- Cross-language CI installs the near extra and executes near_kk_parity.py against
  the TypeScript checkout; fixture bytes must match with LF endings.

Offline callers still own FullAccess-key checks, live nonce allocation, expiry
freshness, recipient/code policy, budgets and the NEP-413 challenge/nonce/account
binding. No helper sends transactions or loads keys from an external store.
OWS real-vault tests skip on Windows with the existing explicit reason because the
pinned library has no Windows wheel; Linux CI keeps its real-library import gate.

Validation (PowerShell, no production network or credentials):

| Check | Local result |
| --- | --- |
| Complete base | 7,672 passed, 76 skipped, zero failures on origin/main 4d22320 |
| Final test coverage | 8,008 passed, 76 skipped, zero failures; all 8,084 final collected cases covered by the complete suite plus final NEAR rerun |
| Golden-flow tests | 194 passed on Python 3.11.4 and installed wheel on 3.9.24 |
| Independent protocol verification | Official NEP-413 example, hand-built Borsh and Ed25519 verification; full NEP-366 wire decoder |
| NEAR runtime parity | 3 NEP-413 preimages/signatures, 2 exact delegates, 3 storage calls, 11 event variants match TS |
| Existing escrow parity | 42 delegates across both networks and 54 pre-signature refusals match TS |
| General cross-language driver | 512 checks passed with Node 20.20.2 |
| Source mutation runner | 54/54 killed, no timeouts; tracked source untouched |
| Wheel compatibility | Base import/builders work without cryptography on Python 3.9; signer tests pass with it |
| Lint/format | Ruff 0.17.3 and Black 26.10.1 pass for the new Python files |
| Secret scan | Gitleaks 8.30.1, three-dot diff against origin/main: zero findings |
| Pre-CI | exit 0; no cheap steps recognized, so the heavy jobs were run explicitly |

The first mutation round exposed a masked payer-validator test: the default
beneficiary copied the invalid payer, causing a different check to reject it.
The final suite isolates an invalid payer with a valid beneficiary; that mutation
now fails. The mutation runner also exercises the exact log prefix with valid
JSON tails and JSON bounds on otherwise valid envelopes.

One independent adversarial Codex review (gpt-6.1-terra/xhigh) found no P1/P2;
its CI-coverage observation led to adding the NEAR runtime driver to the job.
The tool did not expose subagent token usage. The strict fixture check needs LF
bytes, as enforced by both repositories' .gitattributes.

Final stack: rebased onto origin/0xultravioleta/c0-sdk-py-near-r2 at 3d50a26
(PR #79), with origin/main 4d22320 already included. merge-tree against r2 is
clean. The final remote rebase changed only six lines of r2's handoff; runtime
sources are identical to those used by the full-suite run. After adding nine
mutation-isolation cases, the final NEAR rerun passed 465 tests; auditing both
JUnit files against the final collection found no uncovered or failing cases.