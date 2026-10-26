# `uvd_x402_sdk.reputation` — on-chain reputation, one implementation

ERC-8004 ratings **signed by the rater**, **anchored by a payment**, **relayed by the facilitator**
(`/feedback/evm/prepare` + `/submit`, the facilitator pays the gas, the chain records the rater as the
author). Extracted from Karma Hello's per-comment rating worker, which runs this on Base in production,
with the signing guards of Karma Kadabra's `rating_rail` and Execution Market's `relayed-rating`. A new
program installs reputation instead of writing it a fifth time; the TypeScript SDK carries the same
contract (`uvd-x402-sdk/reputation`), pinned to this one by shared vectors.

```bash
pip install "uvd-x402-sdk[reputation]"     # eth-account >= 0.13
```

## A program wired in fifteen lines

```python
import asyncio
from uvd_x402_sdk import reputation as rep
caps = rep.Caps(per_subject_per_stream=3, per_subject_per_day=3, per_subject_lifetime=15, per_stream=25,
                per_day=60, lifetime=74_000, per_day_spend=60_000, max_value=95)  # your config
outbox = rep.SQLiteRatingOutbox("reputation.sqlite")
worker = rep.ReputationWorker(outbox=outbox, governor=rep.Governor(caps, health=rater_is_funded),
    signer=rep.AccountSigner.from_key(key_from_your_secret_store()), relay=rep.Erc8004Client(),
    rpc=rep.HttpJsonRpc(base_rpc_url), publish_evidence=publish, max_amount=1_000)  # 0.001 USDC
outbox.enqueue(rep.OutboxEntry(ref=rep.message_ref("my-app:v1", stream_id, message_id),
    viewer_id=viewer_id, stream_id=stream_id, subject=viewer_wallet, network="base", value=90,
    agent_id=viewer_agent_id, tag1="my_app:idea", tag2="my-app|moment", endpoint=channel_url,
    amount=1_000, created_at=now))                       # the handler only enqueues: milliseconds
async def run():                                         # its own loop, one tick at a time
    while True: await worker.tick(); await asyncio.sleep(5)
asyncio.run(run())
```

What you bring, because it is yours:

- **the key**, from your secret store (this module never reads the environment, never logs a key);
- **`rater_is_funded()`**, the health probe: `True` to write, `False` or `None` to stop (fail closed);
- **`publish(entry, proof) -> (uri, content_hash)`**: build the evidence and serve its exact bytes:

```python
def publish(entry, proof):
    built = rep.evidence_document(
        network=entry.network, agent_id=entry.agent_id, rater=RATER, subject=entry.subject,
        value=entry.value, tag1=entry.tag1, tag2=entry.tag2, endpoint=entry.endpoint,
        created_at=int(entry.created_at), salt=salt_of(entry.viewer_id), proof=proof,
        private={"moment_ref": entry.ref, "message": chat_text_of(entry.ref)},
    )
    # The URI is on-chain for ever: name the file by its hash, NEVER by entry.ref
    # (sha256 of ids anyone in the chat sees; the worker refuses such a URI).
    uri = f"https://my-app.example/evidence/{built.content_hash[2:]}.json"
    serve(uri, built.body)          # the facilitator hashes what it fetches
    return uri, built.content_hash
```

- **the subject's `agentId`** (the module checks on-chain that `subject` owns it before paying anything).

The handler that decides a rating only **enqueues** (one SQLite row, milliseconds). Nothing waits on the
chain; `tick()` runs in its own loop, one at a time.

## The pieces

| Piece | What it guarantees | Ported from |
|---|---|---|
| `message_ref(namespace, *parts)` | `sha256("ns\|a\|b")` hex, the outbox's UNIQUE key; refuses a `\|` or an empty part (two moments must never collide) | Karma Hello `initial_feedback` / Credits Roll `moment_ref` |
| `RatingParams`, `validate_preparation(prep, params, rater, *, network)` | Refuses to sign anything but the v4 `RelayedGiveFeedback` of this rater, on the chain asked for, with exactly these fields; delegate allowlist per chain id; digest rebuilt here (EIP-712 by hand) and compared | KH `live.py`, KK `rating_rail._mensaje_atado`, EM `verifyRelayedRatingPrepare` |
| `sign_relayed_feedback(verified, signer)` | Signs the REBUILT typed data (or its digest); low-s; recovers to the rater before it leaves | KK `firmar_rating`, EM `signRelayedRating` |
| `build_relay_authorization(verified, signer)` | EIP-7702 authorization over the allowlist row (never a hash from the network), `keccak(0x05 ‖ rlp([chain, delegate, nonce]))`, byte-identical to eth-account; `None` once delegated | KK `armar_authorization` |
| `pay_and_prove(signer, payee, amount, rpc, *, max_amount)` | USDC transfer rater → subject, persisted (`on_signed`) BEFORE it is broadcast, retried with the SAME bytes; proof built from the chain (block timestamp, `paymentHash` recomputed as the facilitator does); never to a contract | KH `verify_payment` |
| `RatingOutbox`, `InMemoryRatingOutbox`, `SQLiteRatingOutbox` | One row per ref; every write a compare-and-set; a signed payment saved at most once | KH Mongo outbox |
| `Governor(caps, health)`, `Caps`, `CircuitBreaker` | Caps are parameters (no defaults); health, breaker, unreadable state: no new rating | design G3/G4 |
| `evidence_document(...)` | Canonical JSON, `content_hash` = `feedbackHash`; the person enters only as salted commitments | KH `evidence.py` **minus** `message`/`author` |
| `submit_with_reconcile(entry, ...)` | Persists `submitting` before the submit; never prepares again until the old signature provably expired | KH `process` / `reconcile` |
| `ReputationWorker` | All of the above in one `tick()` | KH `live_worker.Worker` |

## The state machine

```
queued -> paying(tx) -> paid -> submitting(nonce, deadline) -> submitted(tx) -> confirmed
  |          |           |           |                              |
  |          +-> failed  +-> failed  +-> paid (deadline provably     +-> failed
  +-> local_only (a cap)             |    passed, nothing on-chain)
  +-> paid (amount 0)                +-> failed
```

- `submitting` is written **before** the submit is sent. From then on the row is only reconciled
  (`NewFeedback` logs scanned, always from the block read before the submit) and the SAME body re-sent
  while the deadline is ahead (the delegate's nonce is single-use: at most one lands). Once the `safe`
  block is past the deadline the signature can never land (`FeedbackDelegate`: `block.timestamp >
  deadline` reverts) unless it already did, and that is decided on **state read AT that block, by its
  hash** (EIP-1898), not on an empty log answer or a `null` (a load-balanced RPC answers from nodes
  behind): the rater's code, `nonceUsed(nonce)`, and for a first rating the account nonce (an applied
  authorization bumps it). Spent: it landed, keep looking for its event. Provably unspent: back to
  `paid`. Cannot tell: wait, and after `unknown_after` (24 h) `failed` for a human. Two nonces would be
  two ratings.
- Every outbox write is a compare-and-set on the row's `version`: a worker that read `submitting(n1)`
  cannot overwrite `submitting(n2)` another one wrote meanwhile.
- A semaphore of one: no new row starts while one is `paying` or `submitting`; no transfer is signed
  while a rating is `submitting`, and no first authorization while a transfer is `paying` (each takes
  the account nonce the other names). A row that started but has not signed its payment obeys the
  breaker and the health probe like a new one (`Governor.ready()`); a `paid` row does not prepare
  while the breaker is open.
- Before anything is signed the rater's code is read from the chain: a facilitator that says
  "delegated" about a rater with no designator, or "not delegated" about one with it, gets nothing.
  The authorization of a first rating is signed over the account nonce the chain shows (`pending`).
  The emergency exit (an authorization to `address(0)`, plan section 2) is the operator's: **stop the
  worker first**. A row in flight then waits (`relay_state_unknown`) rather than being prepared again,
  but a facilitator that reports the undelegated rater as not delegated would get, on the next rating,
  a new authorization that delegates the account again.
- A row the facilitator keeps refusing does not hold the rail: a definite 4xx on its prepare
  `max_prepare_refusals` times, or `max_relay_rounds` (3) signatures that expired unrelayed, make it
  `failed`.
- A payment is declared unminable (`payment_nonce_consumed`) only after five consecutive reads show its
  nonce used three blocks deep by something else and the payment nowhere: one read from a node ahead
  and one from a node behind is not a verdict.
- `amount == 0`: no anchor payment by the worker; a `proof` enqueued with the row (an x402 settlement,
  a reward paid elsewhere) is rebuilt from the chain and must name the rater as payer and the subject
  as payee; one proof rates one agent once (`proof_reused`).

## Caps, health, breaker

`Caps` has no defaults on purpose: `per_subject_per_stream`, `per_subject_per_day`,
`per_subject_lifetime` (keep it under describe.net's `campaign_per_rater`), `per_stream`, `per_day`,
`lifetime` (Karma Hello's is 74,000, the owner's order of 2026-10-26, provisional: **its** config),
`per_day_spend` (atomic units), `max_value` (a single rater's 100 reads as wash trading). What they count
is the outbox itself, tombstones included: a restart or a `!forgetme` does not reset them. Subject and
stream caps make the row `local_only`; the global daily ones defer it to the next UTC day.

The breaker opens after consecutive failures, on a 429 until its `Retry-After` (the facilitator's daily
write cap answers 429 until 00:00 UTC), on a 503, and on a prepare this module refuses to sign.
`shared_addresses(directory)` lists wallets claimed by more than one account: pass it as
`Governor(..., excluded=...)` and none of them is rated until one proves possession.

## `!forgetme`

`outbox.forget(viewer_id)`: rows that never started are deleted; rows that started keep a tombstone
without the viewer (the caps keep counting what is on-chain; the wallet is public there anyway); a `paid`
row never submitted becomes `failed(forgotten)`; rows in flight are flagged and stripped once they settle
(an on-chain rating cannot be recalled). The rest is yours: destroy the viewer's salt (the commitments
become unlinkable) and stop serving the evidence (the anchor becomes unreachable). Say before consent that
what is anchored stays.

## Evidence

Public: the subject's wallet and `walletBinding`, the agent, the rater, value and tags, the endpoint, the
date, the payment proof. Never: the chat text, the author, the login, the user id. Those go in `private`
and come out as `HMAC-SHA256(salt, label|value)`. A key that names them (`message`, `chatText`, `author`,
`login`, `userId`, `mensaje`, `autor`, `usuario`...) anywhere in the document is refused, and so is a
private value found, in any case, inside a tag, the endpoint, the issuer or `context` (values of 8+
characters as a substring, shorter ones down to 4 as a whole word in the issuer and `context`).

The `feedbackURI`, the tags and the endpoint are on-chain for ever: name the document by its content
hash or something random, never by `entry.ref` (sha256 of ids anyone in the chat sees) or the viewer;
the worker refuses any of them carrying the ref (16 hex characters of it are enough) or a viewer id of
6+ characters.

## Signers

Anything with `get_address()` (or `address`) and `sign_hash(digest)`; `sign_typed_data(typed_data)` is used
for the rating when present (a wallet shows named fields). `sign_transaction(tx)` is needed only to pay.
`AccountSigner` wraps an eth-account account and has all three. The SDK's `EnvKeyAdapter` signs the
rating (typed data) but has no `sign_hash`, so it cannot sign the EIP-7702 authorization of a first
rating: use it only for a rater already delegated, or use `AccountSigner`. A remote signer that returns a
high-s signature, another key's signature or another transaction is refused before anything is sent.
The authorization of a first rating is signed over the account nonce read from the chain
(`eth_getTransactionCount(rater, "pending")`), never the facilitator's word alone: an EIP-7702
authorization does not expire, it is valid while that nonce is the account's.

## What it does not do

Register the subject's identity (`Erc8004Client.register_agent`), read reputation (that is
`uvd-describe-sdk`), host the evidence, or revoke (there is no relayed revocation).

## Shared vectors

`tests/vectors/reputation/*.json` — message refs, prepare -> digest / signature / authorization, the
refusal codes of `validate_preparation` (the FIRST failing check is reported; the order is part of the
contract), `paymentHash`, evidence bytes and hashes. Written only by `python scripts/reputation_vectors.py`
(cross-checked against eth-account, the facilitator's captured proof and the deployed contract; `--check`
exits 1 on drift); their sha256 are pinned in `tests/test_reputation_vectors.py`. The TypeScript twin
consumes them as they are.

## Error codes

`PreparationRejected` (a `ValueError`: permanent), `SignatureRejected`, `PaymentRejected` (definite),
`PaymentPending` (no verdict: retry with the same transaction), `EvidenceRejected`, `OutboxConflict`. Each
carries `.code`; branch on it, never on the message. Messages and logs redact addresses, hashes,
signatures, proofs and URLs, and `HttpJsonRpc` / `ReputationWorker` install the same redaction on the
`httpx` / `httpcore` loggers (httpx writes the full request URL at INFO, API key included). The `repr`
of an outbox row or a signed payment names the row, never its signature, payment or viewer.
