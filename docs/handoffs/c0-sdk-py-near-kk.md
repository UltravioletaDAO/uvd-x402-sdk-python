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

Validation evidence is recorded below when the final union has been checked.
