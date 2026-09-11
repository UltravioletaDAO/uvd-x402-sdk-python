# CLAUDE.md - x402 Python SDK

Project guidance for Claude Code when working with this SDK.

## Overview

x402 Python SDK for backend payment verification and settlement. Used by servers to process x402 payments via the facilitator.

## Repository Structure

```
src/uvd_x402_sdk/
├── __init__.py              # Main exports
├── client.py                # X402Client - payment processing, negotiate_accepts(), facilitator info, connect_with_private_key()
├── wallet.py                # WalletAdapter Protocol + EnvKeyAdapter + OWSWalletAdapter
├── config.py                # Configuration management
├── models.py                # Pydantic data models (PaymentPayload, SettlementAccountPayload, etc.)
├── exceptions.py            # Custom exceptions
├── envelope.py              # Envelope SELECTION — resolve_envelope_version() + v1 -> v2 conversion
├── envelope_v2.py           # The v2 /verify + /settle bodies (build_verify_request_v2, ...)
├── response.py              # 402 response helpers
├── discovery.py             # BazaarClient - resource registration and discovery
├── erc8004.py               # ERC-8004 Trustless Agents (EVM + Solana)
├── solana_signing.py        # Solana rater-authored feedback: sign the prepared tx (ed25519)
├── erc8128.py               # ERC-8128 Signed HTTP Requests (RFC 9421) — sign_request() + fetch_nonce()
├── policy.py                # PurchasePolicy - what this buyer may sign, decided BEFORE signing
├── escrow.py                # Escrow & Refund support + get_escrow_state()
├── advanced_escrow.py       # PaymentOperator on-chain escrow
├── facilitator.py           # Facilitator addresses and fee payers
├── networks/
│   ├── __init__.py          # Network registry
│   ├── base.py              # NetworkConfig, TokenType, helpers
│   ├── evm.py               # 13 EVM networks
│   ├── solana.py            # Solana, Fogo
│   ├── near.py              # NEAR Protocol
│   ├── stellar.py           # Stellar
│   ├── algorand.py          # Algorand
│   └── sui.py               # Sui
└── integrations/
    ├── fastapi_integration.py
    ├── flask_integration.py
    ├── django_integration.py
    └── lambda_integration.py
```

## Multi-Stablecoin Support

### Supported Tokens
- USDC, EURC, AUSD, PYUSD, USDT

### CRITICAL: EIP-712 Domain Names Vary by Chain

**Different chains use different domain names for the same token!**

| Token | Ethereum/Avalanche | Base |
|-------|-------------------|------|
| EURC | `"Euro Coin"` | `"EURC"` |
| USDC | `"USD Coin"` | `"USDC"` on (Celo/HyperEVM/Unichain/Monad) |

### Token Configuration Structure

```python
# src/uvd_x402_sdk/networks/base.py
@dataclass
class TokenConfig:
    address: str       # Contract address
    decimals: int      # 6 for most, 18 for GHO/crvUSD
    name: str          # EIP-712 domain name (CRITICAL!)
    version: str       # EIP-712 domain version
```

### Network-Specific Domain Names

```python
# src/uvd_x402_sdk/networks/evm.py

# Most chains use "USD Coin"
base.usdc_domain_name = "USD Coin"

# These 4 use "USDC"
celo.usdc_domain_name = "USDC"
hyperevm.usdc_domain_name = "USDC"
unichain.usdc_domain_name = "USDC"
monad.usdc_domain_name = "USDC"
```

## Payment Processing with Custom Tokens

### Backend Must Extract Token Info from Payload

When processing payments with non-USDC tokens, the backend MUST:

1. Extract `token` object from x402 payload
2. Use `token.address` as asset (NOT hardcoded USDC)
3. Pass `token.eip712` to facilitator via `extra` field

```python
# Example: Extracting token info
inner_payload = payload.get("payload", {})
token_info = inner_payload.get("token")

if token_info:
    # Custom token (EURC, AUSD, etc.)
    token_address = token_info.get("address")
    token_symbol = token_info.get("symbol")
    token_eip712 = token_info.get("eip712")
else:
    # Default USDC
    token_address = network_config.usdc_address
    token_symbol = "USDC"
```

### Sending Domain Info to Facilitator

```python
payment_requirements = {
    "asset": token_address,  # NOT hardcoded USDC
    "extra": {
        "name": token_eip712["name"],    # e.g., "EURC" for Base EURC
        "version": token_eip712["version"],
    },
}
```

## Key Features

### WalletAdapter Protocol (wallet.py)
- `WalletAdapter` - Abstract Protocol interface for any wallet backend
- `EnvKeyAdapter` - Uses raw private key from env var or direct param
- `OWSWalletAdapter` - Stub for Open Wallet Standard (not yet on PyPI)
- `EIP3009Params` / `EIP3009Authorization` / `SignedTypedData` - TypedDict types
- Requires `pip install uvd-x402-sdk[wallet]` or `uvd-x402-sdk[signer]`
- Auto-detects USDC contract addresses and EIP-712 domain names per network
- Uses same proven `encode_typed_data()` + `sign_message()` pattern as advanced_escrow.py

### Client-Side Signing (client.py)
- `X402Client.connect_with_private_key(private_key, chain_name)` - Server-side EVM signer without browser wallet
- `X402Client.create_authorization(pay_to, amount_usd)` - Create signed EIP-3009 X-PAYMENT headers
- Uses `encode_typed_data()` + `sign_message()` (proven two-step signing method)
- Requires `pip install uvd-x402-sdk[signer]` (only `eth-account`, not full `web3`)
- EVM-only: validates chain is EVM type before signing

### SKALE Base Network
- Mainnet: `skale-base` (chainId 1187947933), Testnet: `skale-base-sepolia` (chainId 324705682)
- EIP-712 domain name: `Bridged USDC (SKALE Bridge)` (NOT "USDC" or "USD Coin")
- Gasless transactions (CREDIT gas token), legacy tx only (no EIP-1559)
- No escrow support (blocked on Cancun EVM compatibility)

### ERC-8004 Trustless Agents (erc8004.py)
- Supports 20 networks: 18 EVM + Solana + Solana-devnet
- `AgentId = Union[int, str]` - EVM uses int, Solana uses base58 pubkey string
- `seal_hash` parameter on `revoke_feedback()` and `append_response()` (SEAL v1)
- Solana uses QuantuLabs 8004-solana Anchor program + ATOM Engine

### Escrow Pre-Auth Builder (escrow_signing.py)
- `build_escrow_pre_auth(payment_config, network, payer, receiver, amount_usd, deadline, wallet, tier)` - signs the ADR-002 sign-on-assignment escrow lock (`ReceiveWithAuthorization`) and returns the raw JSON `X-Payment-Auth` header value
- `compute_escrow_nonce(chain_id, escrow_address, typehash, payment_info)` - `AuthCaptureEscrow.getHash` mirror (raw keccak, payer zeroed, receiver INCLUDED - the signature commits to the worker, so signing happens AT ASSIGNMENT, never before)
- **Golden vectors are PINNED** in `tests/fixtures/escrow-preauth.json` (byte-identical copy of Execution Market's `shared/test-vectors/escrow-preauth.json` F0-1 fixture; re-copy from there, never edit here)
- Same math as `AdvancedEscrowClient._compute_nonce` but standalone (dict-based, no web3); `EnvKeyAdapter.sign_eip3009` produces the same digest when payer == adapter wallet - parity pinned in `tests/test_escrow_signing.py`
- Fail-loud: unknown network / incomplete config raises `ValueError` (silent domain fallback = wallet-draining auth)

### The typed data is a WIRE type, and the wire is JSON (escrow_signing.py, v0.80.0)
- Every typed-data dict this SDK hands a `WalletAdapter` carries **`primaryType`** — `LifecycleOrder`, `ReceiveWithAuthorization` (pre-auth and `advanced_escrow`), `ReplaySafeHash` (`erc7702`). It does NOT enter the digest (EIP-712 hashes domain + types + message) and `eth-account` derives it, but **viem refuses to sign without it**, which closed the browser route for the Python side while every byte-comparison stayed green. TS emitted it from day one
- `build_lifecycle_typed_data` returns the four keys viem reads and nothing else; its `message` is **all strings** — `nonce` as 0x-hex (`bytes` is not JSON-serializable: `json.dumps` raises) and every uint as a decimal string. A `salt` written as a JSON number is destroyed by `JSON.parse`: measured with `0xab*32`, Python signed `0x15a8587e…` and viem, reading that same document, `0x4c88c56a…` — **two valid signatures over different structs, no error anywhere**
- The digest is unchanged by all of the above; the pinned vector `0x78fe14…3a71c` is byte-identical before and after (`test_primaryType_no_movio_la_firma_del_vector_fijado`)
- `lifecycle_auth_from_signature` rejects a `primaryType` that is present and wrong, and **tolerates one that is absent** — every document emitted by 0.78.0/0.79.0 lacks it. The TS twin requires it because it never emitted one without
- The cross-language gate compares the DOCUMENT, not just the signature (`scripts/xlang/` in the TS repo): the agents return what the SDK handed the wallet, and the message crosses a real `json.dumps` → `JSON.parse` boundary. Proven red in three states, not just green

### Solana rater-authored feedback (erc8004.py + solana_signing.py, v0.81.0)
- `prepare_solana_feedback()` / `submit_solana_feedback()` drive `/feedback/solana/prepare` and `/feedback/solana/submit`, live on the deployed facilitator since **v1.74.0** (measured on **v2.16.0**, 2026-09-07) and with **no client on either SDK** until this version. Server half: `x402-rs`, `src/handlers.rs:6674,6786` + `src/erc8004/solana.rs:1036-1174`
- **`SOLANA_FEEDBACK_NETWORKS` is a SIBLING of `RELAYED_FEEDBACK_NETWORKS`, never a row in it.** That frozenset means "a `FeedbackDelegate` is deployed and verified here" and `erc8004.py` builds `/feedback/evm/prepare` out of it; a `solana` entry routes the call to the EVM URL, which answers 400. Pinned in `tests/test_solana_feedback.py`; `tests/test_relayed_feedback.py:56` is untouched
- **Solana needs no delegate**: account 0 of the program's `give_feedback` is already `[signer, writable] client (feedback author / fee payer)`, and Solana takes several signers per transaction, so the rater signs as `client` while the facilitator stays fee payer
- **`sign_solana_feedback_transaction()` never re-serialises the message.** It carries the exact wire bytes and rewrites only the signature array, because `accept_rater_signed_transaction` compares the submitted message against its own rebuild. Decoding to structs and re-encoding is how you get `400 submitted transaction does not match the one this facilitator built` with nothing on the client side to point at
- Fee payer is signature slot 0 and is left EMPTY: the facilitator fills it after verifying the rater's, so a transaction the network would reject never costs a fee
- `Ed25519Signer` (extra `solana` -> `cryptography`) takes a 32-byte seed, a 64-byte `solana-keygen` key, its int array or its base58 form, and checks the 64-byte form's public half against the derived one. `SolanaSigner` is a two-member Protocol, so a browser wallet or custodian plugs in unchanged
- **Wire pinned from a LIVE capture**: `tests/fixtures/solana-feedback-prepare.json` (facilitator v2.16.0). Re-capture with `examples/solana_feedback_smoke.py`, which runs prepare -> sign -> verify against production with an ephemeral rater and **stops before submit** (submit is an on-chain write the facilitator pays for)

### Purchase policy (policy.py, v0.82.0)
- `PurchasePolicy` implements the contract the facilitator fixed in its P3 phase (`x402-rs` 2.25.0, `crates/x402-reqwest/src/policy.rs`), field for field and code for code. Same contract in the TypeScript SDK
- **It runs inside `X402Client.fetch()`**, between reading the 402 and producing the `X-PAYMENT` header. That IS the feature: the Rust security review caught this half-built, with the policy and the seller's `validUntil` both present and no cable between them for a whole commit, all unit tests green. `tests/test_policy_real_path.py` enters through `fetch()` and asserts on whether a header was produced
- **Order is the contract** (first failing check is the one reported): `no-readable-offer` -> `offer-expired` -> `recipient-not-permitted` -> `asset-not-budgeted` -> `per-payment-limit` -> `cumulative-limit`. `asset-not-budgeted` runs BEFORE the ceilings because a map has no opinion about a key it does not hold
- **Amounts are integers in atomic units** and the asset key carries the network (`TokenAsset("base", USDC)`); `TokenAsset("base", ...)` does NOT cover an offer on `polygon` or on `eip155:8453`
- **`canonical_address` is not `lower()`**: hex folds, base58 (Solana, XRPL) compares exactly. Folding base58 refuses legitimate payees and can admit one nobody listed
- **Evaluating does not spend** (`record_spend` is a separate call, after settlement), **copies share the purse** (`copy` AND `deepcopy`), and unreadable purse state reports the CEILING, never zero
- `validUntil` comes from `extensions["offer-receipt/1"].info.validUntil`, Unix seconds, JSON number only. Unreadable = absent, NEVER zero. `validUntil == now` still stands
- **Defaults**: `X402Client` holds `PurchasePolicy.permissive()` when no policy was supplied (nothing changes for existing callers); `PurchasePolicy()` denies an asset with no ceiling. `PolicyRefusedError` subclasses `NoAcceptablePaymentError` for compatibility — `exc.code` is `POLICY_REFUSED`, the contract code is `exc.refusal_code`
- **What it does NOT bring** (same as Rust): offer-receipt signature verification, input binding, `upto` accounting, settlement reconciliation. Nothing is persisted

### ERC-8128 Signed HTTP Requests (erc8128.py)
- `sign_request(wallet, method, url, body=None, nonce=None, ...)` - RFC 9421 request signing over any `WalletAdapter` (EIP-191 personal_sign); returns `Signature` / `Signature-Input` / `Content-Digest` headers
- `fetch_nonce(api_base)` - async, gets the single-use server nonce (5-min TTL, one per signed request including retries)
- **Wire format is PINNED** — byte-equality enforced in `tests/test_erc8128.py` against `tests/fixtures/erc8128.json` (byte-identical copy of Execution Market's `shared/test-vectors/erc8128.json` F3-1 golden vectors; re-copy from there, never edit here): `alg="eip191"` emitted, keyid ALWAYS lowercase, params order `created;expires;nonce;keyid;alg`
- Importable on a base install (httpx + stdlib; no eth-account until an adapter is instantiated)

### Settle Overrides, Retry & Non-Raising Settle (client.py, v0.36.0)
- `settle_payment()` / `verify_payment()` / `process_payment()` accept `asset` (token contract address) and `eip712_domain` (`{"name", "version"}`) overrides — the caller's token registry wins over the SDK's (non-USDC settles, registry drift). Defaults unchanged: network USDC + registry domain
- `create_authorization()` accepts the same `eip712_domain` override — enters the SIGNED digest and the non-USDC `token.eip712` block. Partial domain raises `ValueError` before signing (fail-loud)
- `settle_payment(..., retry=True)` (default OFF) — up to 3 attempts, backoff 1s/2s, ported from Execution Market's `mcp_server/integrations/_http_retry.py`: retries transient transport errors + 5xx, NEVER 4xx, NEVER `success=false` in a 2xx, NEVER a 5xx whose body carries a tx hash (**anti-double-settle guard**)
- `try_settle_payment()` — non-raising settle returning `{"success", "tx_hash", "error"}`; `success=False` + `tx_hash` set = the facilitator broadcast despite the error status (verify on-chain, never re-send)

### Envelope Selection — v1 vs v2 (envelope.py, v0.74.0; v2 payloads v0.75.0)
- `verify_payment()` / `_settle_once()` used to write `"x402Version": 1` as a **literal**: the SDK could advertise v2 in a 402 and was then unable to speak it. The v2 builders existed since v0.62.0 with **no caller** and `X402Config.x402_version` was read by nothing. Same defect TypeScript fixed in 2.78.0 after it broke a real ChatGPT payment
- `resolve_envelope_version(payload, requirements, requested="auto")` — **auto keys off CAIP-2 on the wire, NEVER off `payload.x402Version`.** The facilitator's envelope enum is untagged (matches on shape, ignores the marker), so a header that only *declares* v2 with plain names is a 200 in v1 and a **400** in v2. A pin (`1` / `2`) always wins over the wire
- `build_verify_request_for_version()` / `build_settle_request_for_version()` — v1 return is byte-for-byte the pre-0.74.0 body. `to_resource_info_v2()` / `to_accepted_requirements_v2()` do the v1 → v2 conversion (`maxAmountRequired` → `amount`, network → CAIP-2, `resource` string → 3-key object, `extra` carried through — it holds the EIP-712 domain for EURC and the bridged USDCs)
- **Networks with no CAIP-2 form (XRPL) stay on v1 under auto and raise under an explicit pin to 2** — a silent downgrade would put a v1 network name inside a v2 body, which is the 400 this exists to prevent
- **v0.75.0: the network is read wherever the payload keeps it** — top level (v1) or `accepted.network` (v2), top level winning when present; a payload with neither stays on v1 instead of raising. Before that, `auto` crashed on a v2 payload (`AttributeError` / `TypeError`), which is why MeshRelay pinned the version instead of using the default. The builders take the same shapes and pass the payload through unreshaped
- **`X402Client.extract_payload` flattens a v2 header BEFORE any of this** (`client.py:504`), resolving `eip155:8453` → `base`, so a v2 header through the client comes out in the **v1** envelope. Measured. TypeScript has no such flattening and answers v2 on the same header — the one live divergence left between the two SDKs
- **Byte-identical to the TypeScript SDK** for the same wire, with one difference in TS's published 2.78.0: its v1 envelope inherits `x402Version` from the payer's header, so it can declare `2` on a v1-shaped body. Python keeps the literal `1` — the top-level field names the ENVELOPE. Both are 200 today (untagged enum), but the facilitator already picks its 400 `hint` from the declared version. TS closed this in 2.79.0 (not published yet)

### /accepts Negotiation (client.py)
- `X402Client.negotiate_accepts()` - POST /accepts to facilitator
- Faremeter middleware compatibility
- Returns enriched requirements with feePayer, tokens, escrow config

### Facilitator Info (client.py)
- `X402Client.get_version()` - GET /version
- `X402Client.get_supported()` - GET /supported (networks + schemes)
- `X402Client.get_blacklist()` - GET /blacklist (sanctioned addresses)
- `X402Client.health_check()` - GET /health

### Bazaar Discovery (discovery.py)
- `BazaarClient.list_resources()` - GET /discovery/resources (with pagination, filtering)
- `BazaarClient.register_resource()` - POST /discovery/register
- `DiscoveryResource`, `DiscoveryResponse` Pydantic models

### Escrow State Queries (escrow.py)
- `EscrowClient.get_escrow_state()` - POST /escrow/state
- Reads on-chain escrow state without settlement

### Settlement Account Payload (models.py)
- `SettlementAccountPayload` - For Crossmint/custodial wallets that sendTransaction (not signTransaction)
- Fields: `transactionSignature`, `settleSecretKey`, `settlementRentDestination`

## Known Limitations

> Verificadas contra el codigo el 2026-09-05. Dos de las tres que estaban aqui
> ya no eran ciertas y mandaban a arreglar lo que funciona; quedan escritas
> abajo con el comando que las cierra.

1. **Response builder** - Hardcodes `token="USDC"` in the 402 response
   (`response.py:131`, y el mensaje por defecto en `response.py:117`). Vigente:
   `grep -n 'token="USDC"' src/uvd_x402_sdk/response.py`. Un 402 que anuncia
   EURC sigue diciendo USDC en el campo `token`.
2. **Stellar y NEAR** - Solo USDC. Vigente: ninguno de los dos define un dict
   `tokens` (`grep -n 'tokens=' src/uvd_x402_sdk/networks/{stellar,near}.py`
   no devuelve nada), asi que no hay a donde colgar un segundo token.

### Cerradas (no volver a "arreglarlas")

- ~~`process_payment()` convierte montos con los decimales de USDC de la red~~ -
  **falso desde antes de 0.76.0**. Acepta `token_decimals` y lo propaga por
  `verify` / `settle` / `process_payment`
  (`grep -c 'token_decimals' src/uvd_x402_sdk/client.py` -> 26 lineas, de la 727
  a la 2180), con tests que cubren 18, 7, el borde `0` falsy y el negativo
  (`tests/test_multi_token_requirements.py`). Lo que sigue sin existir es el
  parametro llamado `token_type`, que es otra cosa.
- ~~SVM solo soporta USDC~~ - **falso**. Solana ya trae AUSD por Token2022
  (`grep -n '"ausd"' src/uvd_x402_sdk/networks/solana.py` -> 86), con su
  `token_2022_program_id` en `extra_config` (linea 97).

## Development Commands

```bash
pip install -e ".[dev]"
pytest                   # Run tests
ruff check .             # Lint
mypy src/                # Type check
```

## Integration with 402milly

The 402milly pixel marketplace uses this SDK pattern. Key integration file:
- `backend/lambdas/purchase_pixels/x402_facilitator.py`

### Critical Lessons from 402milly Integration

1. **Always extract token info from payload** - Don't assume USDC
2. **Always pass `extra` field to facilitator** - Required for domain resolution
3. **Store `token_symbol` in database** - For proper currency display

## Related Repositories

- **402milly Backend**: `Z:\ultravioleta\dao\million\402milly\backend`
- **TypeScript SDK**: `Z:\ultravioleta\dao\uvd-x402-sdk-typescript`
- **Facilitator**: `Z:\ultravioleta\dao\x402-rs`
