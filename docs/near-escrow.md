# Arras NEAR escrow (Python 0.105.0)

Install `pip install 'uvd-x402-sdk[near]'`. Import from `uvd_x402_sdk.near_escrow`.
Builders need only the base package; signing uses cryptography. All helpers are offline.
They allocate no nonces and send no transactions.

Use contract and operator IDs from trusted `GET /supported` configuration for the selected
`scheme: escrow`, `near:mainnet` or `near:testnet` entry (`extra.nearEscrow.contracts` and
`operatorIds`). `NEAR_ESCROW_CONTRACTS` is an immutable registry of contracts this SDK will sign for:
`near:testnet` contains only `arras-v1b-1101.testnet`; `near:mainnet` contains only
`v1.arras-escrow.near`. Authorization and every lifecycle method reject accounts outside
the selected network row before accessing the signer. `NEAR_ESCROW_TOKENS` is immutable too.
Check advertised support before submitting; callers still select the contract explicitly.

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

Message fields: `operator_id`, `receiver`, `terms_hash`, `salt` are strings;
`release_deadline` and `reclaim_after` are `Unsigned`; `fee_bps_max` is an integer u16.
The hashes are exactly 32 and 16 bytes of lowercase hex, without `0x`. Only these fields enter
the canonical JSON; other fields of a shared vector/input mapping are ignored.
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

## Reproducible conformance

`tests/fixtures/near-escrow.json` is an unchanged byte copy of Arras `docs/vectores.json`
(sha256 `fe81bfc72bd1e90a5afb86fb00eece59cbd78fde983eda397c797bf65351709b`). Never regenerate it.
It pins all V-1..V-3 preimages/hashes/ids and the canonical message. Payment IDs can have
32..44 characters; the decoder checks exactly 32 bytes, including leading zeroes.

```powershell
python -m pytest -q tests/test_near_escrow.py
python scripts/near_escrow_parity.py --ts-root ../uvd-x402-sdk-typescript --node node
```

The second command requires the TypeScript H-6 checkout with dependencies installed (`npm ci`).
It compiles the actual TS helper in memory and compares all three unchanged vectors and
21 signed delegates on each network (authorization and each lifecycle method), including signatures
and the full JSON payment. V-3 retains its mainnet message/preimage/ID. Signing exercises use
each network's registered contract and USDC. The harness also compares the contract registry and refusals
before signing, without RPC calls or generated expectations. It fails if TypeScript is unavailable.
The v2 budget format is not defined here; no budget vectors or signing are invented.

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
the selected network, just like escrow signing. No account parent is inferred.
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
