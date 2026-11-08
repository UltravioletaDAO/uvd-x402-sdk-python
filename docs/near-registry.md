# Arras registry on NEAR

The legacy Arras registry, `registry.arras-escrow.near` on `near:mainnet`,
has been deployed with no access keys since 2026-11-04. The SDK exposes it
through `uvd_x402_sdk.near_registry` and accepts only `v1.arras-escrow.near`
as its payment source. Testnet has no registered deployment and is refused.

Execution Market moved to the NEAR ERC-8004 identity and reputation registries
on 2026-11-06 and no longer uses this registry. Use the
[NEAR ERC-8004 guide](near-erc8004.md) for new integrations. The legacy client
keeps its existing API; the reference below describes that client.

## Offline transactions

```python
from uvd_x402_sdk.near_escrow import NearEd25519Signer
from uvd_x402_sdk.near_registry import (
    build_registry_register, build_registry_update, build_give_feedback,
    build_revoke_feedback, registry_args_json, sign_registry_transaction,
)

register = build_registry_register(
    network="near:mainnet", signer_id="payer.near",
    agent_uri="https://agente.example/card.json",
)
feedback = build_give_feedback(
    network="near:mainnet", signer_id="payer.near",
    escrow="v1.arras-escrow.near", payment_id="1" * 32, score=90,
    tag1="quality", tag2="successRate",
    feedback_uri="https://agente.example/f/1.json", feedback_hash="ab" * 32,
)
args_bytes = registry_args_json(feedback)
# signer = NearEd25519Signer(your_key_from_secure_storage)
# signed_base64 = sign_registry_transaction(feedback, signer, next_nonce, block_hash)
```

The payment id above is synthetic; it is not evidence of a real payment.
Builders return the same wallet-selector shape as `NearStorageTransaction`:
`signerId`, `receiverId`, `actions: [{type: "FunctionCall", params: {methodName,
args, gas, deposit}}]`. Amounts and gas are decimal strings. `registry_args_json`
returns compact UTF-8 in ABI field order. Absent optional fields are omitted;
`metadata=None` in an update clears the old metadata.

`sign_registry_transaction` returns base64 Borsh of an ordinary signed NEAR V0
transaction, ready for the `signed_tx_base64` parameter of `send_tx`. It checks
the descriptor again before reading the key and verifies the returned Ed25519
signature. It neither submits nor queries anything. The caller supplies the
selected key's next nonce and a recent base58 block hash, persists the signed
bytes, and manages confirmation/retries. It asks the signer for
`sign(sha256(wire))`: an `OwsNearSigner` receives that digest through OWS
`sign_message`, not the whole transaction, and the signer's network is not
compared with the transaction's.

**Use a FullAccess key** belonging to `signerId`: a FunctionCall access key cannot
attach these deposits. Permissions and account ownership cannot be inferred
from a public key offline; check them when selecting the key.

| Method | Default deposit (yoctoNEAR) | Default gas |
|---|---:|---:|
| register | 17510000000000000000000 | 30 TGas |
| update | 17510000000000000000000 | 30 TGas |
| give_feedback | 18640000000000000000000 | 100 TGas |
| revoke_feedback | 1, exactly | 30 TGas |

Read `get_config` for the actual register/feedback deposits and
pass them with `deposit=`; values below these SDK defaults are refused.
`give_feedback` requires at least 60 TGas. Gas is in integer gas units (1 TGas =
10**12); all builders cap it at NEAR's 300 TGas transaction maximum. `update`
accepts >=1 yocto and charges actual growth on-chain; the default covers a full
record. Excess storage deposits are refunded by the contract.

Local checks mirror the contract's byte limits and character sets, without
normalization: URI 512 bytes, metadata 1024 UTF-8 bytes without ASCII controls,
tags 64 ASCII bytes, payment id 64 printable ASCII bytes without spaces,
score integer 0..100, hash exactly 64 lowercase hex characters without `0x`.
URI schemes can be mixed case; tags are case-sensitive. Errors include the
contract's `ARRAS_REG_Enn` and `RegistryError.code` enum.
Registration state, actual storage growth, duplicate/pending feedback and
payment eligibility are on-chain checks; offline builders cannot promise them.

## Read views and outcomes

```python
from uvd_x402_sdk.near_registry import NearRegistryClient, parse_give_feedback_outcome

# Constructing the client performs no I/O. These read calls contact RPC only
# when invoked.
client = NearRegistryClient("near:mainnet")
# config = client.get_config()
# agent = client.get_agent("worker.near")
# summary = client.get_summary_by_operator("worker.near", "em")
# page = client.list_feedback("worker.near", from_index=0, limit=50)
# verdict = parse_give_feedback_outcome(final_execution_outcome)
```

Inject `rpc(method, params)` with `NearRegistryClient(rpc=your_transport)`;
it must return the complete JSON-RPC response dict. The default uses httpx and
the SDK network RPC URL with a 30-second timeout. All reads use final finality.
`get_agent`, `get_feedback`, and `get_feedback_for_payment` may return `None`.
`list_agents`, `list_feedback`, `get_summary`, `get_summary_by_operator`,
`get_config`, and `is_feedback_pending` validate their complete response shape and content: NEAR accounts, URIs, metadata,
tags, payment ids and operator ids (at most 64 UTF-8 bytes, empty allowed).
Invalid content raises `ValueError`; fields are never normalized.
U128/U64 fields stay decimal strings; feedback ids and counts are u32 numbers.
Page args are u32 integers; the contract clamps `limit` to 50. Fetch successive
pages once, stopping at an empty page.

`parse_give_feedback_outcome` accepts a final NEAR outcome (or its JSON-RPC
`result` envelope). `FeedbackOutcome.status` is `accepted` with a numeric
`feedback_id`, `rejected` with a `RegistryErrorCode` from a null result's rejection
log (E18/E20..E26), or `error` with a known contract panic code. Missing/pending,
unrecognized and malformed results raise `ValueError`. A transaction-level
success alone does not imply accepted feedback.

`parse_registry_events(logs)` recognizes `agent_registered`, `agent_updated`,
`feedback_given`, `feedback_revoked` under `arras-registry` / `1.0.0`.
Like `parse_arras_events`, it skips unknown or malformed logs, rejects malformed
batches atomically, preserves extra data fields, and bounds JSON before parsing.
Use `parse_registry_receipt_events(result["receipts_outcome"], "near:mainnet")`
for a transaction result from a trusted RPC. It parses only receipts whose
`outcome.executor_id` exactly matches `NEAR_REGISTRY_CONTRACTS` for that network;
identical logs from another contract are ignored. The function also filters by
`outcome.status`: it must be an object with a string `SuccessValue` or
`SuccessReceiptId`, and no `Failure` key. `Failure` always wins, including a null
or empty failure alongside a success field. Failed receipts (including
`LackBalanceForState` or `ExecutionError` after emitting logs), `Unknown`, missing,
null and malformed statuses discard the entire receipt. Empty success strings
are valid; success strings are not decoded, and additional fields are allowed,
matching TypeScript. Successful receipts and their logs keep their input order.
Malformed receipts are skipped, unknown networks raise `ValueError`, and a known
network without a registry returns no events. The low-level
`parse_registry_events(logs)` requires the caller to filter by executor and receipt
success first. Neither helper authenticates the RPC itself or proves a payment
settled.

## Legacy reputation and trust

After an Arras payment releases a positive amount (including partial release
followed by refund/reclaim), the payer can rate the receiver and vice versa.
Neither needs a registry profile first. Legacy clients should rate promptly:
after payment compaction (`reclaim_after + 7 days` at the earliest) the source payment may no
longer exist. One feedback per escrow/payment/rater is allowed; revoking it
removes its contribution but does not free the slot for another rating.

Use the operator-specific on-chain summary for `em`, then fetch the ratee's
feedback detail and call `summarize_feedback(rows, "v1.arras-escrow.near", "em")`.
It filters exact escrow/operator, excludes revoked rows from counts and averages,
counts distinct active raters, and reports matching revoked rows separately.
`released_total` is a decimal string; `average_x100` is floor(score sum * 100 /
count), and `weighted_average_x100` is floor(sum(score * released) * 100 /
sum(released)). Empty sets have `None` averages; zero total weight has a `None`
weighted average. Pass one ratee's complete, deduplicated rows; aggregation is
local and does not fetch or deduplicate pages.

Reputation depends on the escrow's operator administration and on genuine
counterparties. A global average mixes operators; distinct raters and released
amounts provide context, not a guarantee of independent identities. A recreated
NEAR account inherits the name's profile and reputation.

Treat profile text as untrusted display data. `safe_link(uri)` preserves a valid
URI only for `https`, `ipfs`, or `ar`; otherwise it returns `None`.
`display_text(s)` strips Unicode 17.0.0 controls, format/bidi characters,
surrogates and all `Default_Ignorable_Code_Point` ranges. The versioned Unicode
data sources are linked next to the table in `near_registry.py`, independent of
the Unicode version bundled with Python. Render its
result through a text API, with normal HTML escaping when applicable.

## Offline verification

`tests/fixtures/near-registry.json` contains fixed synthetic inputs, exact args
JSON/hex, two signed transactions and the four contract event examples. It is a
byte-identical copy of TypeScript PR #69 at `b4b703061` (SHA-256
`80eeb72f0ee5a4a933b8592f88387d6c4a1f2234a97b02a1f94969878294be38`).
`python scripts/near_registry_parity.py --ts-root <TS checkout>` checks those
bytes; CI pins that fixture's reviewed commit separately while the other
cross-language checks continue to follow TS main. Update the fixture and CI pin
together when adopting a new shared vector.
`python scripts/near_registry_vectors.py --check` verifies the independent
serializer's output. `python scripts/near_registry_mutations.py` requires a clean
worktree, runs each mutant in memory with closed proxies and a 120s timeout, and
fails on any surviving or invalid execution.
