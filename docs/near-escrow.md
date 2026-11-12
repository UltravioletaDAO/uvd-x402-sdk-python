# Arras NEAR escrow

Install `pip install 'uvd-x402-sdk[near]'`. Import from `uvd_x402_sdk.near_escrow`.
Builders need only the base package; signing uses cryptography. All helpers are offline.
They allocate no nonces and send no transactions.

Use contract and operator IDs from trusted `GET /supported` configuration for the selected
`scheme: escrow`, `near:mainnet` or `near:testnet` entry (`extra.nearEscrow.contracts` and
`operatorIds`). `NEAR_ESCROW_CONTRACTS` is an immutable registry of contracts this SDK will sign for,
v1 first: `near:testnet` contains only `arras-v1b-1101.testnet`; `near:mainnet` contains
`v1.arras-escrow.near` and `v2.arras-escrow.near`. `NEAR_ESCROW_V2_CONTRACTS` is the subset that
runs Arras v2: `v2.arras-escrow.near` (the reviewed wasm, arras `ddc5121`, sha256
`1541525c...fa77`, keyless since 2026-11-08), and none on testnet: the v2 rehearsal contract there
takes a test token, not Circle's USDC (arras `docs/MAINNET.md` 8.6), and the earlier one predates the
review. `near_escrow_contract_version(network, contract)` answers `"v1"` or `"v2"` and raises for
anything else. The same shape as the TypeScript SDK's `NEAR_ESCROW_CONTRACTS` /
`NEAR_ESCROW_V2_CONTRACTS`. Authorization and every lifecycle method reject accounts outside the
selected network row before accessing the signer. `NEAR_ESCROW_TOKENS` is immutable too. Check
advertised support before submitting; callers still select the contract explicitly.

## Public API for adapters

`Unsigned` below means `int | str`, with canonical ASCII decimal strings. Bools, floats, signs,
whitespace, leading zeroes and overflow are rejected. All amounts are atomic token units, not USD
floats. Amounts and deadlines passed as integers are encoded as decimal JSON strings.
Release/reclaim timestamps are **nanoseconds** (u64); expiry is a **block height** (u64).

| Name | Arguments | Return |
| --- | --- | --- |
| `NearEd25519Signer` | `secret_key: bytes | str`: raw 32-byte seed, or `ed25519:` + base58 of 64 bytes (`seed || public_key`) | signer |
| `NearLocalSigner` | Protocol: `public_key: bytes` (32 bytes), `sign(digest: bytes) -> bytes` (64 bytes) | signing adapter interface |
| `NearEscrowDelegateOptions` | `network: str`, `signer: NearLocalSigner`, `sender_id: str`, `escrow_contract: str`, `nonce: Unsigned`, `max_block_height: Unsigned`, `gas: Unsigned | None = None` | frozen options |
| `build_escrow_msg` | `params: Mapping[str, Any]` (fields below) | canonical JSON `str` |
| `payment_id_preimage` | same fields plus `token: str`, `payer: str`, `amount: Unsigned` | exact Borsh `bytes` |
| `compute_payment_id` | same as preimage | base58 `str` |
| `build_escrow_authorization_delegate` | `options`, keyword-only `token: str`, `amount: Unsigned`, `msg: Mapping` | unsigned delegate `dict` |
| `validate_escrow_authorization_delegate` | `delegate: Mapping`, `options`, keyword-only `token`, `amount`, `msg` | `None`, raises `ValueError` on a mismatch |
| `sign_escrow_authorization` | `options`, keyword-only `token: str`, `amount: Unsigned`, `msg: Mapping` | payment `dict` |
| `sign_escrow_lifecycle` | `options`, `method: str`, `args: Mapping | None = None` | payment `dict` |
| `release` | `options`, `payment_id: str`, `amount: Unsigned | None = None` | payment `dict` |
| `refund` / `reclaim` | `options`, `payment_id: str` | payment `dict` |
| `withdraw` | `options` | payment `dict` |
| `withdraw_for` | `options`, `account: str` | payment `dict` |
| `authorize_from_budget` | `options`, `request: Mapping` | always raises `NotImplementedError` before using a key |
| `AuthorizeFromBudget` | Protocol: `authorize_from_budget(request: Mapping) -> dict` | v2 adapter boundary |
| `near_escrow_contract_version` | `network: str`, `contract: str` | `"v1"` or `"v2"`; `ValueError` if unregistered |
| `OperatorTicketSigner` | Protocol: `public_key: bytes` (32 bytes), `sign(message: bytes) -> bytes` (64 bytes, Ed25519 of the raw message) | ticket signing interface; `NearEd25519Signer` satisfies it |
| `operator_ticket_message` | `contract: str`, `subject: Mapping`, `expiry: Unsigned` | exact Borsh `bytes` the operator signs |
| `sign_operator_ticket` | `signer`, keyword-only `contract`, `subject`, `expiry`, `now_ns: Unsigned | None = None` (wall clock) | `{"expiry": "<ns>", "sig": "<base58>"}`; refuses `expiry <= now_ns` |
| `check_operator_ticket` | `ticket_pubkey: bytes | str | None`, keyword-only `contract`, `subject`, `ticket`, `now_ns = None` | `None` (accepted), `"E53_TICKET_INVALIDO"` or `"E54_TICKET_VENCIDO"` |
| `verify_operator_ticket` | `public_key: bytes | str`, keyword-only `contract`, `subject`, `ticket`, `now_ns = None` | `bool` |

Message fields: `operator_id`, `receiver`, `terms_hash`, `salt` are strings;
`release_deadline` and `reclaim_after` are `Unsigned`; `fee_bps_max` is an integer u16.
The hashes are exactly 32 and 16 bytes of lowercase hex, without `0x`. Only these fields enter
the canonical JSON, plus the two Arras v2 fields when present and not `None` (below); other
fields of a shared vector/input mapping are ignored.
`release_deadline < reclaim_after` is checked locally. Chain time, balances, caps, storage,
operator membership and live payment state remain chain/facilitator checks.

A payment is `{x402Version: 2, scheme: "escrow", network: "near:mainnet" | "near:testnet",
payload: {signedDelegateAction: "<base64>"}}`. The signature commits to one FunctionCall,
its JSON bytes, gas, deposit, sender, receiver, next key nonce, max block height and public key.
It signs `sha256(u32le(2**30 + 366) || borsh(delegate))`, with Ed25519. Both key type and
signature type are Borsh variant 0. The returned signature is verified locally.

The authorization receiver is the network's native USDC contract; the FT argument's
`receiver_id` is the explicit Arras contract. It attaches **exactly 1 yoctoNEAR**, with
38..100 TGas (default 100). The local signer must use the payer key that can authorize that
deposit. Lifecycle delegates attach **zero**, target Arras directly and allow only these
methods: release/refund/reclaim (3..30 TGas, default 30), withdraw/withdraw_for
(29..50 TGas, default 50). Gas arguments use integer gas units, 1 TGas = 10**12.
These are the current facilitator v1 limits, not automatically refreshed configuration.

Use the operator/capturer's function-call key for release/refund/withdraw_for, restricted to
the selected escrow, method names and a bounded allowance. A local secret does not encode
its on-chain permission; the chain verifies it. Reclaim uses the payer's key; withdraw uses
the balance holder's key. Delegates require `nonce < max_block_height * 1_000_000`; equality and larger nonces are
rejected before accessing a key. Never reuse the same nonce across calls. Persist signed bytes
before submission; submission/receipt reconciliation belongs to the adapter.

## Building a facilitator request

```python
from uvd_x402_sdk import near_escrow as near
from uvd_x402_sdk.envelope_v2 import build_verify_request_v2

# Inject these from trusted configuration and a selected local access key.
options = near.NearEscrowDelegateOptions(
    network=network, signer=near.NearEd25519Signer(secret_key),
    sender_id=payer, escrow_contract=contract,
    nonce=next_nonce, max_block_height=expiry_height,
)
payment = near.sign_escrow_authorization(options, token=token, amount=amount, msg=terms)
payment_id = near.compute_payment_id({**terms, "token": token, "payer": payer, "amount": amount})
accepted = {
    "scheme": "escrow", "network": network, "asset": token, "amount": str(amount),
    "payTo": contract, "maxTimeoutSeconds": 300,
    "extra": {"nearEscrow": {
        "contract": contract, "operatorId": terms["operator_id"],
        "msg": near.build_escrow_msg(terms), "paymentId": payment_id,
    }},
}
body = build_verify_request_v2(
    payload=payment["payload"], resource=resource, accepted=accepted,
)
# body is ready for /verify (and build_settle_request_v2 for /settle).
```

Lifecycle requests MUST set `accepted.amount` to `"0"` (or omit it); a positive amount
is rejected before relay. `extra.nearEscrow` names `contract` and `action` (the exact
method name). Release/refund/reclaim also name `paymentId`:

```python
from dataclasses import replace
from uvd_x402_sdk.envelope_v2 import build_settle_request_v2

# Release/refund use the capturer's key and its own selected access-key context.
# Reserve distinct nonces with your persistent allocator before signing.
capturer_options = near.NearEscrowDelegateOptions(
    network=network, signer=near.NearEd25519Signer(capturer_secret_key),
    sender_id=capturer, escrow_contract=contract,
    nonce=capturer_next_nonce, max_block_height=capturer_expiry_height,
)
released = near.release(capturer_options, payment_id)
lifecycle_accepted = {
    **accepted, "amount": "0", "extra": {"nearEscrow": {
        "contract": contract, "action": "release", "paymentId": payment_id,
    }},
}
release_body = build_settle_request_v2(
    payload=released["payload"], resource=resource, accepted=lifecycle_accepted,
)

# Alternative refund flow, using another nonce reserved for the same capturer key.
refund_options = replace(capturer_options, nonce=capturer_refund_nonce)
refunded = near.refund(refund_options, payment_id)
refund_body = build_settle_request_v2(
    payload=refunded["payload"], resource=resource,
    accepted={**lifecycle_accepted, "extra": {"nearEscrow": {
        "contract": contract, "action": "refund", "paymentId": payment_id,
    }}},
)
```

Release and refund only credit claimable balances. After release, sign separate
`withdraw_for(options_with_next_nonce, worker)` and fee-recipient withdrawals; after a refund,
withdraw for the payer. `withdraw()` takes no token and `withdraw_for(account)` takes no token:
Arras v1 is monotoken. Chain events use `standard: arras`, version `1.0.0`, array `data`.
A signature or successful outer Delegate receipt alone is not proof of escrow authorization.
Use the facilitator's receipt verdict and reconcile payment state/events.

## Arras v2: the operator ticket and the v2 `authorize` msg

Measured on arras `9f8332f` (`contracts/escrow-v2/src`). The v2 `authorize` msg is the v1 msg
plus two optional fields, appended in this order: `dispute_deadline` (u64 ns as a string; absent
means `reclaim_after`) and `operator_ticket` (`{"expiry": "<ns>", "sig": "<base58 of 64 bytes>"}`)
(`msg.rs:53-70`, `msg.rs:87-104`). A v1 msg is a valid v2 msg with the same `payment_id`: neither
field enters the id (`msg.rs:5-6`). v1 parses its msg with `deny_unknown_fields`, so a msg with
either field sent to v1 is refused whole (`E03`): `sign_escrow_authorization` refuses it before the
key on a v1 contract. Locally checked: `release_deadline <= dispute_deadline` (`E67`,
`authorize.rs:221-224`) and `dispute_deadline <= reclaim_after` (the static half of `E11`,
`authorize.rs:212-218`), and the ticket's exact shape. `now < dispute_deadline` stays a chain check.

**When v2 requires the ticket.** `check_ticket` (`authorize.rs:265-285`) decides from the
operator's `ticket_pubkey` (`lib.rs:222-223`):

| Operator `ticket_pubkey` | Ticket in the msg | Result |
| --- | --- | --- |
| set | present, `now < expiry`, signature of this contract + subject + expiry | accepted |
| set | absent | `E53_TICKET_INVALIDO` (`authorize.rs:273`) |
| set | `now >= expiry` | `E54_TICKET_VENCIDO` (`authorize.rs:275-278`) |
| set | malformed or another message's signature | `E53` (`authorize.rs:279-282`) |
| not set | absent | accepted (`authorize.rs:272`) |
| not set | present | `E53`: nobody can verify it (`authorize.rs:273`) |

It runs for every payment by `ft_transfer_call` (`authorize.rs:181-187`, subject
`{"payment": {"payment_id"}}`), every budget deposit (`authorize.rs:489-497`, subject
`{"budget": {"operator_id", "owner", "agent"}}`) and every `authorize_from_budget`
(`budget.rs:240-246`, a payment subject), after `E11`, `E67`, `E12` and `E13` and before `E14`
(`authorize.rs:5-7`). The owner sets, rotates or removes the key with `register_operator` /
`update_operator` (`lib.rs:281-282`, `lib.rs:285-302`). On mainnet, operator `em` of
`v2.arras-escrow.near` has a `ticket_pubkey` (arras `docs/MAINNET.md`, 8.1): every EM payment and
budget deposit there needs EM's ticket. Lifecycle methods never take a ticket. A rejected
`ft_transfer_call` returns the whole amount with the log `arras:rechazo:<CODE>`
(`authorize.rs:1-3`, `error.rs:113-116`).

**The ticket.** `operator_ticket_message(contract, subject, expiry)` is
`borsh(domain, contract, subject, expiry)` with `domain = "arras:v2:operator_ticket"`
(`ticket.rs:25`, `ticket.rs:41-59`): Strings as u32 LE length + UTF-8, the subject as a u8 tag
(0 payment, 1 budget) and its Strings, `expiry` as u64 LE nanoseconds. The contract views
`ticket_message_payment` / `ticket_message_budget` return the same bytes (`views.rs:214-242`).
`sign_operator_ticket` signs those bytes with Ed25519 (no hash) and returns the dict that goes
into the msg; it refuses a ticket already expired at `now_ns` (default: the wall clock), which also
catches an expiry written in seconds. `check_operator_ticket` answers what `check_ticket` would,
`None`, `"E53_TICKET_INVALIDO"` or `"E54_TICKET_VENCIDO"`, with the operator's current
`ticket_pubkey` or `None` (the whole table above; E54 judged before the signature,
`authorize.rs:275-282`); `verify_operator_ticket` is the same with a key that must be given. The
ticket's `expiry` must be the canonical decimal string the msg carries: a JSON number there is
`E03`, and the chain's `str::parse` also takes `"+1"` / `"01"`, which these checks call E53 (the SDK
never writes them). The ticket key is injected: a 32-byte seed in `NearEd25519Signer`, or any
object with `public_key` and `sign(message)`. The SDK never reads a key from a file or the
environment. `OwsNearSigner` is not a ticket signer: its `sign` accepts only 32-byte digests and
refuses the message. A payment ticket is single use in practice (the id never repeats, `E13`); a
budget ticket admits any number of deposits of its pair until `expiry`, even after `revoke_agent`:
sign short expiries. The ticket binds the `payment_id` only, not `dispute_deadline`: a payer
sending its own `ft_transfer_call` can pair EM's ticket with any `dispute_deadline` the contract
accepts (`ticket.rs:41-48`; declared as P3-4 in arras `docs/handoffs/c0-arras-v2.md`).

Execution Market's v2 flow (owner's decision of 2026-11: the owner removes `em`'s `ticket_pubkey`
first, below, and EM signs no ticket). The msg carries v1's keys only, which is also all the
facilitator accepts today, and the payer signs it against the v2 contract:

```python
import json

em_v2_network = "near:mainnet"
em_v2_contract = "v2.arras-escrow.near"  # EM's configured contract, checked by the SDK
assert near.near_escrow_contract_version(em_v2_network, em_v2_contract) == "v2"
em_v2_token = near.NEAR_ESCROW_TOKENS[em_v2_network]
em_v2_msg = near.build_escrow_msg(terms)  # served in extra.nearEscrow.msg: no v2 fields
em_v2_payment_id = near.compute_payment_id(
    {**terms, "token": em_v2_token, "payer": payer, "amount": amount}
)
em_v2_options = near.NearEscrowDelegateOptions(
    network=em_v2_network, signer=near.NearEd25519Signer(secret_key),
    sender_id=payer, escrow_contract=em_v2_contract,
    nonce=next_nonce, max_block_height=expiry_height,
)
em_v2_payment = near.sign_escrow_authorization(
    em_v2_options, token=em_v2_token, amount=amount, msg=json.loads(em_v2_msg),
)
```

Use a random `salt` per payment, never one derived from the task: the same terms in v1 and v2 give
the same `payment_id`, and each escrow's verifier would rate it.

With the ticket instead (operator `em` keeps its `ticket_pubkey`, one payment):

```python
import json
import time

from cryptography.hazmat.primitives.serialization import (
    Encoding, NoEncryption, PrivateFormat, load_pem_private_key,
)

# EM signs a ticket per payment. Its ticket key is an OpenSSL Ed25519 PEM
# (/arras/mainnet/em-ticket-v2): EM loads it and injects it; the SDK reads no key.
# Any object with public_key + sign(message) works as the signer too.
seed = load_pem_private_key(ticket_pem, password=None).private_bytes(
    Encoding.Raw, PrivateFormat.Raw, NoEncryption()
)
ticket_signer = near.NearEd25519Signer(seed)

v2_network = "near:mainnet"
v2_contract = near.NEAR_ESCROW_V2_CONTRACTS[v2_network][0]  # from EM's own config
v2_token = near.NEAR_ESCROW_TOKENS[v2_network]
v2_payment_id = near.compute_payment_id(
    {**terms, "token": v2_token, "payer": payer, "amount": amount}
)
now_ns = time.time_ns()
v2_terms = {**terms, "operator_ticket": near.sign_operator_ticket(
    ticket_signer, contract=v2_contract,
    subject={"payment": {"payment_id": v2_payment_id}},
    expiry=now_ns + 10 * 60 * 10**9,  # ten minutes: valid while the chain's now < expiry
)}
v2_msg = near.build_escrow_msg(v2_terms)  # served in extra.nearEscrow.msg

# The payer signs exactly the msg it was served, against the v2 contract.
served = json.loads(v2_msg)
assert near.build_escrow_msg(served) == v2_msg  # refuse a msg the SDK would rewrite
v2_options = near.NearEscrowDelegateOptions(
    network=v2_network, signer=near.NearEd25519Signer(secret_key),
    sender_id=payer, escrow_contract=v2_contract,
    nonce=next_nonce, max_block_height=expiry_height,
)
v2_payment = near.sign_escrow_authorization(
    v2_options, token=v2_token, amount=amount, msg=served,
)
```

Without `ticket_pubkey` on the operator (the owner can remove it, below), omit `operator_ticket`: a
ticket there is `E53`. `dispute_deadline` is omitted above, so v2 uses `reclaim_after`; setting it to
`release_deadline` (the minimum, `E67`) removes the branch where a payer who disputes from
`release_deadline` on gets the live amount back if the arbiter does not answer (`lib.rs:8-12`,
`dispute.rs:20-25`). `build_escrow_msg` rewrites a served msg that is not canonical (another `v` or
`action`, unknown or camelCase keys are dropped, numbers become strings), hence the `assert`.

`extra.nearEscrow` is the same as on v1 (`contract`, `operatorId`, `msg`, `paymentId`). The payer
pays storage as on v1 (`build_arras_storage_deposit` takes any contract of `NEAR_ESCROW_CONTRACTS`)
unless the operator's `sponsor_pool` covers it. Lifecycle on v2 is signed by `em`'s v2 capturer,
`em-capturer-v2.arras-escrow.near` (arras `docs/MAINNET.md` 8.2), not v1's.

**What the facilitator needs first** (x402-rs `665585e6`, two separate gates): its served-contract
list, `NEAR_ESCROW_CONTRACTS_MAINNET`, lists v1 only (arras `docs/MAINNET.md` 8.9), and it relays
to nothing else (`ContractNotServed`, `src/chain/near/escrow.rs:888-891`); and it parses the msg with
`deny_unknown_fields` and only the v1 keys (`src/chain/near/escrow.rs:575-590`), so it refuses a msg
with `dispute_deadline` or `operator_ticket`. With v2 added to that list, a v1-shaped msg reaches v2
through an operator without `ticket_pubkey`; with a ticket, the parser must accept the v2 fields too.

Lifecycle on v2 is the v1 wire: `release`, `refund`, `reclaim`, `withdraw`, `withdraw_for` keep
their names and arguments (`lifecycle.rs:17-91`, `withdraw.rs:19-26`; withdraw callbacks 10 + 10
TGas as v1, `lib.rs:47-50`). The gas windows (38..100 TGas for the authorization, default 100) are
the facilitator's v1 measurements (x402-rs `src/chain/near/escrow.rs:84-92`); v2 adds an Ed25519 check and
86 bytes of dispute storage per payment and was measured with 100 TGas, not at the floor. A
`Disputed` payment answers `E22` to all three until resolved (`lifecycle.rs:1-3`). v2 keeps v1's
codes and log format and adds `E47`, `E48`, `E50`-`E57` and `E60`-`E67` (`error.rs:6-34`). Its
events keep `standard: arras`, version `1.0.0` (`events.rs:1-4`): `parse_arras_events` reads the v1
events v2 emits and skips the seven v2-only ones, `dispute_resolved` included, which moves money
(`to_receiver`, `to_payer`, `fee`): reconciling a disputed payment needs another reader. Not in this
SDK yet: the `budget` msg, `authorize_from_budget`, the dispute methods and the v2 views
(`get_budget`, `compute_budget_payment_id`). Rating a v2 payment through ERC-8004 needs a verifier
for v2, which is not registered yet: `near_erc8004.near_erc8004_verifier("near:mainnet",
"v2.arras-escrow.near")` raises (`docs/near-erc8004.md`).

## Reproducible conformance

`tests/fixtures/near-escrow.json` is an unchanged byte copy of Arras `docs/vectores.json` at
arras `9f8332f` (git blob `246303c1`, sha256
`5a78be59d03ee2c57aae49aac6f64488b996f0e5868dda095c063a1372eb2f70`). Never regenerate it; re-copy
it from Arras. It pins all V-1..V-3 preimages/hashes/ids and the canonical message, and its `v2`
section the three canonical v2 msgs, VB-1 and the tickets T-1 / T-2 (message bytes and signature
with arras' published test seed `01` x 32). Payment IDs can have 32..44 characters; the decoder
checks exactly 32 bytes, including leading zeroes.

```powershell
python -m pytest -q tests/test_near_escrow.py tests/test_near_escrow_v2.py
python scripts/near_escrow_parity.py --ts-root ../uvd-x402-sdk-typescript --node node
```

`tests/test_near_escrow_v2.py` reproduces T-1 and T-2 byte for byte and signature for signature
(the TypeScript SDK must give the same bytes and signature with that key), rebuilds the message by
hand, and pins the sha256 of every v1 output of 0.112.0 (authorizations, lifecycle, storage):
v1 did not move by one byte.

The second command requires the TypeScript H-6 checkout with dependencies installed (`npm ci`).
It compiles the actual TS helper in memory and compares all three unchanged vectors and
21 signed delegates on each network (authorization and each lifecycle method), including signatures
and the full JSON payment. V-3 retains its mainnet message/preimage/ID. Signing exercises use
each network's registered contract and USDC. The harness also compares the contract registry and refusals
before signing, without RPC calls or generated expectations. It fails if TypeScript is unavailable.
That harness covers v1 only; v2 parity is through T-1 / T-2 in both SDKs' suites.

## Golden-flow helpers (0.106.0)

Execution Market (EM#659) and KarmaKadabra can use these public helpers instead of
keeping their own Arras event parser or storage builders. All are offline. No
helper contacts RPC, allocates a nonce, submits a transaction or loads credentials.

### NEP-145 storage, paid by the payer

```python
from uvd_x402_sdk import build_arras_storage_deposit, build_usdc_storage_deposit

arras_registration = build_arras_storage_deposit(
    network="near:mainnet",
    payer_id="parent.near",             # signs and pays the normal transaction
    account_id="agent.parent.near",      # storage beneficiary; defaults to payer_id
    escrow_contract="v1.arras-escrow.near",
)
usdc_registration = build_usdc_storage_deposit(
    network="near:mainnet", payer_id="parent.near", account_id="agent.parent.near",
)
```

These return `{signerId, receiverId, actions}` for a wallet's normal transaction
API. They are unsigned transactions, with one `storage_deposit` call and 30 TGas.
The payer pays both gas and the attached deposit; they are never facilitator
delegates. Arras requires a deployment in `near_escrow.NEAR_ESCROW_CONTRACTS` for
the selected network, just like escrow signing (v1 or v2: the same NEP-145 `storage_deposit`). No
account parent is inferred.
USDC uses the native token from `NEAR_ESCROW_TOKENS` for that network.

Arras defaults to 0.05 NEAR (`50000000000000000000000` yoctoNEAR) with
`registration_only=False`, so surplus stays available to fund payments. USDC
defaults to 0.00125 NEAR (`1250000000000000000000`) with
`registration_only=True`, returning surplus. These are starting amounts, not
capacity guarantees: query the target's `storage_balance_bounds` and
`storage_balance_of` before choosing a live amount. `deposit=` accepts a positive
u128 integer or canonical decimal string in yoctoNEAR.

### NEP-413 payout-account proof

```python
from uvd_x402_sdk import NearEd25519Signer, sign_nep413

signer = NearEd25519Signer(seed_from_your_key_store)  # 32-byte seed, near extra
proof = sign_nep413(
    signer, account_id="agent.parent.near", message=server_challenge,
    nonce=server_nonce_32_bytes, recipient="execution.market",
    callback_url=None,
)
```

Returns `{accountId, publicKey, signature}` with an `ed25519:` base58 public key
and base64 signature. Per [NEP-413](https://github.com/near/NEPs/blob/master/neps/nep-0413.md),
it signs SHA-256 of little-endian u32 `2**31 + 413`, then Borsh `message`, fixed
32-byte `nonce`, `recipient`, and `Option<String>` callback. Absent callback and
empty callback are distinct. `nep413_preimage` exposes those exact bytes.

Use a FullAccess key. This offline signer cannot inspect on-chain permissions;
the verifier must bind account and public key to a FullAccess access key on the
intended network and check its challenge, one-use nonce, recipient and callback.
The account id itself is not part of the NEP-413 signed preimage. A signature alone
is not account ownership or settlement proof. `intents.near` is refused, including
case and trailing-dot variants; callers still enforce their configured recipient
and message policy. The helper verifies a custom signer's Ed25519 output before
returning it. The official `hi` / nonce `[0..31]` / `myapp.com` / callback example
and independent signature checks live in `tests/test_near_kk.py`; the test seed is
public synthetic data, never a funded key.

### Exact NEAR payments

The existing `networks.near` serializers build Borsh bytes but did not sign exact
NEAR payments. `sign_exact_near_delegate` reuses the escrow NEP-366 encoder and
signature verifier; there is one implementation of delegate serialization.

```python
from uvd_x402_sdk import NearExactDelegateOptions, sign_exact_near_delegate
from uvd_x402_sdk.near_escrow import NEAR_ESCROW_TOKENS

payment = sign_exact_near_delegate(
    NearExactDelegateOptions(
        network="near:mainnet", signer=signer, sender_id="agent.parent.near",
        nonce=next_access_key_nonce, max_block_height=expiry_block_height,
    ),
    token=NEAR_ESCROW_TOKENS["near:mainnet"], pay_to="seller.near", amount="10000",
)
```

Returns the x402 payment `{x402Version: 2, scheme: "exact", network, payload:
{signedDelegateAction}}`. It signs exactly one native-USDC `ft_transfer`, with
1 yoctoNEAR and default/maximum 30 TGas. Amount is positive u128; nonce and expiry
are positive u64 with `nonce < max_block_height * 1_000_000`. Amounts are always
JSON decimal strings, including when Python callers pass integers.

Select a FullAccess payer key. Read the next nonce and current height from the
chain, serialize concurrent nonce allocation, enforce expiry freshness (KK uses
at most 600 blocks), enforce the purchase budget and recipient/code policy, and
persist the signed payment before relay. The SDK cannot check those live facts.
`intents.near` is refused. Escrow payments continue through
`sign_escrow_authorization`; exact does not replace Arras authorization.

### Arras NEP-297 logs

```python
from uvd_x402_sdk import parse_arras_events

for event in parse_arras_events(receipt_logs):
    if event["event"] == "released":
        for item in event["data"]:
            print(item["payment_id"], item["amount"])
```

Returns typed envelopes `{standard: "arras", version: "1.0.0", event, data}`,
preserving log order, batches and U128/U64 decimal strings without converting
them to floats. Supports all eleven variants in `escrow-core/src/events.rs`:
`authorized`, `released`, `refunded`, `reclaimed`, `withdrawn`, `compacted`,
`operator_registered`, `operator_updated`, `caps_set`, `paused`, `unpaused`.
Operator updates can omit unchanged fields. Extra data fields are preserved.
Unrelated logs, malformed entries, unknown standards/versions/events, invalid
field types and out-of-range integers are skipped; an invalid batch is skipped
as a whole. Each JSON document is limited to 512 KiB and 64 nesting levels.

Parsing is not proof of emitter identity or payment success. Consumers must
check the receipt executor, receipt outcomes and expected payment id. In
particular, a `withdrawn` entry with `ok=False` reports a failed withdrawal.

`tests/fixtures/near-kk.json` is a byte copy of the TypeScript golden-flow fixture.
Run `python scripts/near_kk_parity.py --ts-root <TS-checkout> --node <node20>` to
compare both real runtimes' preimages, signatures, exact delegates, storage calls
and event envelopes. Use a checkout with LF fixture bytes (`.gitattributes`), as in CI.
The cross-language CI job runs this check too; the existing ERC-8128/escrow driver remains
unchanged.
