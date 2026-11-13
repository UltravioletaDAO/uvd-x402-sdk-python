# NEAR ERC-8004

Version 0.108.0 adds a separate client for the NEAR identity and reputation
registries. Install `uvd-x402-sdk[near]` for local Ed25519 signing. Wallet builders
and views work with the base package. The earlier Arras registry client keeps
its existing API.

## Deployments

| Network | Identity | Reputation |
| --- | --- | --- |
| `near:mainnet` | `identity-registry.erc-8004.near` | `reputation-registry.erc-8004.near` |
| `near:testnet` | `identity-registry.erc-8004.testnet` | `reputation-registry.erc-8004.testnet` |

| Network | Arras escrow | ERC-8004 verifier of its payments |
| --- | --- | --- |
| `near:mainnet` | `v1.arras-escrow.near` | `erc8004-verifier.arras-escrow.near` |
| `near:mainnet` | `v2.arras-escrow.near` | `payment-proof.arras-escrow.near` (0.114.0) |
| `near:testnet` | `arras-v1b-1101.testnet` | `erc8004-verifier.arras-dev.testnet` |

**One verifier per Arras escrow.** A verifier checks payments of one escrow contract, so a task is
rated with the verifier of the escrow it was paid in; the other verifier reads only its own escrow
and answers `E12` (no such payment there), the registry returns the deposit and the rating is lost
without an error.
`NEAR_ERC8004_ESCROW_VERIFIERS[network]` has one row per contract of
`near_escrow.NEAR_ESCROW_CONTRACTS[network]`, the table above. `payment-proof.arras-escrow.near`
is keyless on mainnet since 2026-11-13 (code sha256
`ea430d5fbb3bbe3b59adf5c2cd7010e00f295e6be5a37eee935c560c5431e5a0`, the reproducible wasm of arras
`40eba87e`, `contracts/erc8004-payment-proof`); its `get_config` reads `v2.arras-escrow.near`.
A verifier is trusted by its account in this table, never by its `get_config` (both answer
`"contract": "erc8004-arras-verifier"`, and anyone can deploy a contract that answers the same).
`near_erc8004_verifier(network, escrow)` returns the verifier of an escrow and raises `ValueError`
for an unregistered escrow or one whose row is `None` (a row is filled only once its verifier is
deployed). `give_feedback_with_proof`, the `new_feedback` reader and the `verify_feedback_proof`
outcome parser accept exactly the non-`None` verifiers of the network. A verifier config is bound
to the escrow it reads: `client.get_config("verifier", verifier=...)` reads the verifier asked for
(default `NEAR_ERC8004_CONTRACTS[network]["verifier"]`, v1's), refuses an unregistered one before
any RPC, and refuses an answer that reads another verifier's escrow; `verify_feedback_proof` built
with that config goes to that escrow's verifier, with that config's gas floor. A verifier that
does not answer raises: no config, nothing to sign. `NEAR_ERC8004_CONTRACTS[network]["verifier"]`
and `NEAR_ERC8004_ESCROWS[network]` stay v1's pair.

**The payment's escrow, `escrow=` (0.114.0).** A payment id does not say which escrow holds it, so
the SDK can refuse a crossed verifier only when told the escrow. Pass the escrow the task was paid
in as `escrow=` and the verifier must be that escrow's, before any RPC or key:
`build_erc8004_give_feedback_with_proof` / `build_erc8004_verify_feedback_proof` /
`build_erc8004_call`, `erc8004_args_json`, `sign_erc8004_transaction`,
`client.get_config("verifier", escrow=...)` (which then reads that escrow's verifier) and the four
views keyed by a verifier (`get_verified_summary`, `list_verified_feedback`,
`get_proof_feedback`, `is_proof_pending`). The escrow is checked, never sent: the descriptor, its
arguments and its signature are those of the same call without it. A builder method other than the
two proof methods, or a config role other than `verifier`, raises with `escrow=`.

Without `escrow=` a call is the 0.113.0 call, and the SDK cannot see a crossing: any registered
verifier of the network is accepted, so since 0.114.0 a v1 payment sent with
`payment-proof.arras-escrow.near` builds and signs (and is lost on chain), as a v2 payment sent with
v1's verifier did before. The direct `client.view(...)` entry point and `read_feedback_proof` take
no `escrow=`; the views keep accepting any account as `verifier`, as in 0.113.0.

`agent_registry(network)` returns the registration-file identifier, for example
`near:mainnet:identity-registry.erc-8004.near`. These are application identifiers;
the helper does not register a CAIP namespace. Validation is not exposed.

## Execution Market pays the registration

```python
from uvd_x402_sdk import (
    NearErc8004Client,
    build_erc8004_register,
    build_erc8004_nft_transfer,
    parse_erc8004_outcome,
)

network = "near:mainnet"
client = NearErc8004Client(network)
identity = client.get_config("identity")
registration = build_erc8004_register(
    network=network,
    signer_id="sponsor.near",
    config=identity,
    agent_uri="https://workers.example/worker.json",
)
# Give registration to the sponsor's wallet. Persist and confirm its outcome.
# registration_outcome is the final RPC transaction result from that wallet.

def transfer_registered_worker(registration_outcome, worker_account):
    result = parse_erc8004_outcome(
        registration_outcome, network=network, method="register"
    )
    if result.status != "accepted":
        raise RuntimeError("Registration did not succeed")
    return build_erc8004_nft_transfer(
        network=network,
        signer_id="sponsor.near",
        config=identity,
        receiver_id=worker_account,
        token_id=result.value,
    )
```

The sponsor signs both descriptors. Transfer makes the worker the owner and
clears `agentWallet`. The worker can subsequently call
`build_erc8004_set_agent_wallet(agent_id=..., new_wallet=..., deadline=..., ...)`.
The deadline is a canonical decimal string of Unix **nanoseconds**, within five
minutes of the current clock. The configured window can be shorter. Signing
checks the window again. A wallet change may be pending until the new wallet
accepts it; inspect the boolean result and `get_pending_agent_wallet`.

A worker registering directly signs only `register` and starts as both owner and
agent wallet. Registration has no implicit retry or deduplication in this SDK.

## Rate a released payment and display verified reputation

```python
from uvd_x402_sdk import (
    build_erc8004_give_feedback_with_proof,
    near_erc8004_verifier,
)

# The verifier of the Arras escrow the task was paid in (from the task's own record):
# v1.arras-escrow.near -> erc8004-verifier.arras-escrow.near,
# v2.arras-escrow.near -> payment-proof.arras-escrow.near.
task_escrow = "v1.arras-escrow.near"
verifier = near_erc8004_verifier(network, task_escrow)
reputation = client.get_config("reputation")

def rate_worker(agent_id, payment_id, payer_account):
    return build_erc8004_give_feedback_with_proof(
        network=network,
        signer_id=payer_account,
        config=reputation,
        agent_id=agent_id,
        value="90",
        value_decimals=0,
        tag1="starred",
        verifier=verifier,
        proof=payment_id,
        # Checked, never sent: another escrow's verifier is refused before signing.
        escrow=task_escrow,
    )

summary = client.get_verified_summary("0", verifier, context="em")
# count, summary_value and weight_sum remain exact decimal strings.
references = client.list_verified_feedback("0", verifier, from_index=0, limit=100)
```

The payer rates the worker's agent ID; the worker can rate the payer's agent ID.
The payment must have released funds, and the verifier must identify the other
party as the rated agent's owner or agent wallet. A proof can be used once per
verifier, payment and client. Compacted escrow payments are no longer available
for verification. Ordinary `give_feedback` does not enter verified summaries.
`weight_sum` is released atomic token units; it is not a price or a weighted
rating. `get_summary` requires a nonempty list of clients chosen by the caller.

Use `parse_erc8004_outcome(..., method="give_feedback_with_proof")` after the
wallet returns a final outcome. It distinguishes `accepted` (decimal index),
`rejected` (`Erc8004Rejection`) and `error` (`Erc8004PanicCode`). A successful NEAR
transaction with a null feedback result can be a refunded callback rejection.
Pending or inconsistent outcomes raise `ValueError`. `give_feedback_with_proof`
accepts only a registered verifier of the network (`NEAR_ERC8004_ESCROW_VERIFIERS`:
on mainnet `erc8004-verifier.arras-escrow.near` and `payment-proof.arras-escrow.near`);
another account, including the other network's verifier, raises `ValueError` before
signing. A `new_feedback` event with another proof verifier is skipped and cannot
substantiate an accepted proof-feedback outcome. The builder does not see the
payment behind `proof`: give it the task's escrow (`escrow=`) and a verifier of the
other escrow raises before signing; without it, choosing the verifier of the
task's escrow is the caller's, with `near_erc8004_verifier`, and a v2 payment sent
to v1's verifier (or the reverse) is `E12` on chain: the rating is lost.

A proof the verifier refuses is an error code, never "unrecognized": a direct
`verify_feedback_proof` that fails parses as `error` with its `Erc8004PanicCode`
(`ARV_E01`..`ARV_E16`; `E16`, v2's verifier only, is a dispute that expired in the
receiver's favour, which is not a proof). Through `give_feedback_with_proof` the
registry logs its own rejection (`E45_VERIFICADOR_FALLO`) and refunds the deposit:
`rejected` with `Erc8004Rejection.E45`.

## Configuration and spending limits

Every builder requires an `Erc8004Config` from `client.get_config(role)`, or from
`parse_erc8004_config(raw, network=..., role=...)` for an already captured trusted
RPC response. Snapshots are immutable and network-bound; builders and signing
revalidate them. Refresh configuration deliberately when preparing new work.
For the verifier role, `client.get_config("verifier", verifier=account)` and
`parse_erc8004_config(raw, ..., role="verifier", verifier=account)` also bind the
snapshot to the account it was read from (its `escrow` must be that verifier's
row); without `verifier=`, a captured snapshot is bound by its `escrow` alone.
`client.get_config("verifier", escrow=task_escrow)` reads the verifier of that
escrow (and refuses a `verifier=` that is not it).

| Operation | Deposit selection | Gas selection |
| --- | --- | --- |
| Register | `register_deposit` from identity config | 30 TGas |
| Feedback | `feedback_deposit` from reputation config | at least configured minimum, recommended 60 TGas |
| Feedback with proof | `feedback_proof_deposit` from reputation config | at least configured minimum, recommended 150 TGas |
| URI/metadata update, approval, response | caller supplies explicit storage budget, at least 1 yocto | 30 TGas |
| Wallet changes, transfer, revoke | exactly 1 yocto | 30 TGas; transfer-call uses configured minimum and 60 TGas recommendation |
| Verifier preflight descriptor | zero | configured minimum and 50 TGas recommendation |

SDK ceilings are the recorded configuration maxima plus 5%: 0.1394295 NEAR for
registration, 0.0062055 NEAR for feedback and 0.015456 NEAR for proof feedback.
These are ceilings, never a replacement for configuration. Storage-growth
budgets are capped by the validated identity registration budget or reputation
proof budget, respectively. This conservative ceiling is not a storage quote:
the contract charges actual growth and returns the remainder. Use 1 yocto only
when growth is known to be zero. Gas is capped at 300 TGas per descriptor.
Configuration minima must be at least 50 TGas for transfer-call, 40 TGas for
feedback, 120 TGas for proof feedback and 25 TGas for verifier preflight;
smaller minima are rejected when parsing or revalidating the snapshot.

The verifier's `verify_feedback_proof` performs cross-contract callbacks. It is
available as an offline builder, `build_erc8004_verify_feedback_proof`, and is
intentionally absent from the RPC view allowlist. Executing it requires a signed
gas-consuming transaction even though its attached deposit is zero.

## ABI and API details

All builders have the prefix `build_erc8004_`, followed by the contract method:
`register`, `set_agent_uri`, `set_metadata`, `set_agent_wallet`,
`accept_agent_wallet`, `unset_agent_wallet`, `nft_transfer`, `nft_transfer_call`,
`nft_approve`, `nft_revoke`, `nft_revoke_all`, `give_feedback`,
`give_feedback_with_proof`, `revoke_feedback`, `append_response`, and
`verify_feedback_proof`. `build_erc8004_call` accepts explicit `method` and `args`
for the same closed set. All share `network`, `signer_id`, `config`, optional
`deposit` and `gas`, and an optional `now_ns` clock for deterministic testing.

IDs, feedback indices, deadlines, deposits and gas are canonical unsigned decimal
strings. `value` is signed, bounded by an absolute value of `10^38`; neither `+1`,
`01` nor `-0` is valid. `value_decimals` is a JSON integer from 0 through 18. Summary
values can have 56 digits and stay strings. The native NEP-171 `approval_id` and
reputation pagination `from_index` are JSON u64 numbers; NFT pagination uses a
U128 decimal string. `limit` is a number from 0 through 100. No page is fetched
implicitly.

URIs use a scheme and RFC 3986 ASCII characters. Encode Unicode and spaces
before calling; valid input bytes are preserved. Percent sequences are not
decoded or rewritten. Empty optional URIs and `new_uri=""` are supported.
Response URIs must be nonempty. Tags accept up to 64 UTF-8 bytes without Unicode
control characters. Hashes are lowercase 64-character hex without `0x`.
Metadata entries contain a key and canonical base64 value (up to 1024 decoded
bytes); at most four entries can accompany registration. `agentWallet` is
reserved for wallet operations. Optional `None` arguments are omitted.

The client exposes all identity and reputation views under their snake_case
contract names; `get_agent` aliases `nft_token`. `get_config` and `get_version`
take a role because both registries expose them. `view(role, method, **args)` is
an explicit allowlisted entry point. Views reject malformed fields, preserve
exact integer strings and bound JSON size/depth. Inject a synchronous
`rpc(method, params)` callable returning the complete JSON-RPC envelope to use a
custom endpoint or replay fixtures. The injected transport must bound its own
HTTP response; the client always bounds the decoded result bytes.

`parse_erc8004_receipt_events(receipts_outcome, network)` checks successful
receipt status, network and the emitting contract's role. Event batches contain
exactly one data item. It preserves receipt order, including wallet clearing
before an NFT transfer. Unknown or malformed events are skipped. The lower-level
`parse_erc8004_events` requires an explicit executor and assumes success has
already been checked. These helpers trust the supplied RPC's provenance fields;
they do not authenticate a node or prove settlement.

When consuming `new_feedback` or `read_feedback_proof`, count a proof as verified
Execution Market reputation only when it is non-null, `proof.verifier` is a
registered verifier of the network (a non-`None` value of
`NEAR_ERC8004_ESCROW_VERIFIERS[network]`) and `proof.context == "em"`. Apply both
filters even when indexing parsed events. `read_feedback_proof` returns the
recorded proof without restricting its verifier or context; a non-null proof
alone does not establish verified EM reputation. For aggregate reads, use
`get_verified_summary` with each registered verifier and `context="em"`, as in
the example above; each verifier has its own summary.

## Local signing

```python
from uvd_x402_sdk import sign_erc8004_transaction

# signer implements NearLocalSigner: public_key bytes and sign(digest) bytes.
# next_nonce and recent_block_hash come from the caller's trusted chain client.
def sign_registration(signer, next_nonce, recent_block_hash):
    return sign_erc8004_transaction(
        registration, signer, next_nonce, recent_block_hash,
        network=network, config=identity,
    )
```

The helper rebuilds the descriptor before reading the public key, serializes a
NEAR V0 transaction, verifies the returned Ed25519 signature and returns base64.
Check FullAccess permission separately and persist signed bytes before submitting
through your wallet or chain client. The SDK does not fetch a nonce, submit a
transaction or retry a write.

## Recorded vectors and offline checks

`tests/fixtures/near-erc8004.json` contains public views from both networks and
six historical registration/transfer/proof-feedback outcomes. Every call has
`args`, compact `args_json`, `args_hex` and the decoded `result`; transaction
cases preserve the RPC outcome and exact event logs. No signing secret is stored.

Explicitly regenerate with `python scripts/near_erc8004_vectors.py --record`.
Only contract views and `EXPERIMENTAL_tx_status` are permitted; requests are
serialized at least 1.1 seconds apart. Tests never refresh recordings. Compare a
TypeScript fixture offline with `--compare <fixture>`; mismatched bytes fail.
Run the focused tests and `python scripts/near_erc8004_mutations.py` offline.
The mutation runner requires a clean worktree and bounds every child process.
