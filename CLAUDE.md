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
├── batch_settlement.py      # x402 batch-settlement BUYER side (EVM): BatchSettlementClient signs deposit + cumulative vouchers via a WalletAdapter
├── bounded_json.py          # loads_bounded() - size and nesting ceiling BEFORE json.loads of a third party's JSON
├── bindings.py              # BindingStore - a seller's per-payment key, persisted BEFORE the facilitator is called
├── escrow.py                # Escrow & Refund support + get_escrow_state()
├── advanced_escrow.py       # PaymentOperator on-chain escrow
├── escrow_contracts.py      # ESCROW_CONTRACTS + ESCROW_OPERATORS + ESCROW_MAX_FEE_BPS (no imports; advanced_escrow re-exports the registry)
├── facilitator.py           # Facilitator addresses and fee payers
├── reputation/              # ERC-8004 rating rail, ONE implementation (extra `reputation`): prepare gate, signing, anchor payment, outbox, governor, evidence, worker; solana_identity.py (the rated party's 8004-solana identity, no extra)
├── interop/                 # Reference runner of the interop spec (interop/ at the repo root)
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
| USDC | `"USD Coin"` | `"USDC"` on (Celo/HyperEVM/Unichain/Monad/Arc mainnet + testnet) |

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
arc_testnet.usdc_domain_name = "USDC"   # name() medido on-chain
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
- `OWSWalletAdapter` - signs in an Open Wallet Standard vault; written against `open-wallet-standard` **1.4.2** (module `ows`: wallet by name or id, `chain` explicit as `eip155:<id>`, typed data as a JSON string, results are dicts). `tests/ows_double.py` has the library's signatures, pinned by `inspect.signature` in `tests/test_ows_wallet_adapter.py` (which also signs in a temporary vault and recovers with eth-account); a new ows release is a new API to measure, bump the pin in `dev` with it. Typed data is normalised by DECLARED type (`_ows_value`: ints of any width as int/decimal/0x-hex, range-checked BEFORE signing because ows signs `2**256` as `uint256`; decimal within 128 bits, even hex beyond) and every typed-data signature is recovered against eth-account's digest before it is returned (`_check_typed_signature`). `sign_transaction` takes `from` out when it is the wallet (web3's `build_transaction` keeps it), `TypeError` otherwise. CI fails if `ows` is not importable
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

### Arc mainnet and testnet (evm.py, v0.84.0)
- `arc` / `eip155:5042` and `arc-testnet` / `eip155:5042002` are independently verified networks. See `docs/networks/arc.md`.
- Both use USDC `0x3600000000000000000000000000000000000000`, 6 payment decimals and EIP-712 `USDC` / `2`. Native gas uses 18 decimals on the same balance; never use that precision for a signed payment.
- `tests/test_arc_testnet.py` preserves the decimal regression; `tests/test_arc_networks.py` checks real signatures and cross-network domain isolation.
- Support covers direct `exact` EOA USDC and EURC payments (EURC since v0.86.0: `0xbEf5…21c1` mainnet, `0x89B5…D72a` testnet, domain `EURC` / `2`, `usd_pegged=False` — prices are euros; `tests/test_arc_eurc.py`). Gateway, `upto` and EIP-6492 are not enabled for Arc. ERC-8004 is (v0.90.0): registries on both networks, relayed feedback on `arc` only. Escrow is (v0.91.0): see "Escrow generations" below.

### Escrow generations (advanced_escrow.py, v0.91.0)
- `ESCROW_GENERATIONS` / `get_escrow_generation()`: `"v1"` legacy operators, `"v2"` CREATE3 (SKALE Base), `"v3"` canonical x402r (`arc`, `arc-testnet`). Outside the table = `"v1"`. `CREATE3_CHAIN_IDS` is DERIVED from it (the `"v2"` chains); `get_operator_abi()` picks `OPERATOR_ABI` / `_V2` / `_V3`. The generation lives in its own table, not inside `ESCROW_CONTRACTS[cid]`: that dict is addresses only, and the snapshot pins it
- `ESCROW_CONTRACTS` lives in `escrow_contracts.py` (no imports at all, so `escrow_signing` reads it without web3); `advanced_escrow.ESCROW_CONTRACTS` is the same object. Its operator column is `ESCROW_OPERATORS` (own table, same reason as the generation): Execution Market's PaymentOperator on the 11 chains of `VERIFIED_USDC_DOMAINS` (the 7 mainnets beyond Base/SKALE/Arc from EM's `network_registry.py` at EM `f75d4171`). On Base `0x271f…F0Eb`, SKALE Base `0x43E4…D183`, Arc and Arc Testnet `0x0258…Fd9D` (`advanced_escrow._DEFAULT_OPERATOR_CHAINS`) it is also `AdvancedEscrowClient`'s default operator, read from the table (no copy in the client; `BASE_MAINNET_CONTRACTS["operator"]` is pinned equal to the Base row); on the other 7 the client still requires `operator_address`: the rows did NOT give the client new defaults, because `build_payment_info` defaults `max_fee_bps` to 800 < EM's 1300 bps fee (a silent EM default there = a lock whose release reverts; pinned in `test_the_client_fee_default_is_why_the_other_rows_are_not_client_defaults`). Arc Testnet is not in EM's registry: its row is the `computeAddress`. `EXECUTION_MARKET_NETWORKS` in that test file is EM's config of the 10 escrow networks of its registry (the 9 its payment config serves, plus `skale`: retired in EM, ADR-009, not served by `h2a.py`; its row stays because it is still the operator of the escrows in flight there and the client's SKALE default), loaded from `tests/fixtures/em-escrow-networks.json`: GENERATED from EM's `network_registry.py` by `python scripts/em_escrow_networks.py --em <EM checkout> [--ref origin/main] [--check]` (reads the table with `ast.literal_eval`, executes nothing of EM's; `--check` exits 1 when a value drifted, 0 with a note when only EM's commit moved, 2 when EM's sources no longer read as ported; the fixture writes `token_collector` as `collector` so secret scanners stay quiet, `_served()` maps it back), never edited by hand: each signs with NO warning. `ESCROW_MAX_FEE_BPS` = 1800 on every chain with an operator row (owner decision 215: EM's current value, the 1300 bps fee plus room for a protocol fee of up to 500; SDK policy, not an on-chain limit). The testnet rows (84532, 11155111) have neither
- Arc: escrow `0xBdEA…0cff`, factory v1.0.2 `0xc241…2716`, ERC3009 collector `0x0E3d…7757`, fee config `0xBe2d…15Bc`, RefundRequestFactory `0xe971…CEFC`; default operator `0x0258…Fd9D` on both (= `computeAddress` on the factory)
- v3 has no `release` / `refundInEscrow`: `release()` -> `capture(pi, amt, b"")`; `refund_in_escrow()` -> `void(pi, b"")` ONLY when `amt == capturableAmount` (read via escrow `getHash` + `paymentState`), else `ValueError`; capturable 0 -> `EscrowNothingToVoidError` (NOT a `ValueError`); state read failed (429, JSON-RPC error, connection; web3 6 raises a plain `ValueError` there) -> `EscrowStateUnavailableError` (retryable, neither of the other two, cause in `__cause__`); nothing sent in any. `refund_post_escrow()` -> `refund()`; `charge()` raises before signing (v3 charge is 6-arg)
- `AdvancedEscrowClient` for a chain outside `ESCROW_CONTRACTS` without `contracts=` raises `ValueError` (it used to fall back to Base's addresses); 8453 unchanged
- **`tests/fixtures/escrow-calldata-snapshot.json` is NEVER edited or re-recorded**: calldata of `release`/`refund_in_escrow`, `/settle` body of `authorize` and the registry tables of the 11 chains of 0.90.1. `tests/fixtures/arc-escrow-d.json` is a chain recording (`scripts/arc_escrow_record.py`, reads only, `--force` to re-measure): factory code (selectors), `computeAddress`, `getHash` / `paymentState`

### ERC-8004 Trustless Agents (erc8004.py)
- Supports 23 networks: 21 EVM + Solana + Solana-devnet (the 23 of the facilitator's `GET /feedback` -> `supportedNetworks`; `base-mainnet` is only a deprecated alias)
- Arc since v0.90.0: `arc` / `arc-testnet` carry the canonical mainnet / testnet registries (identity, reputation, validation). **Only `arc` is in `RELAYED_FEEDBACK_NETWORKS`** (v4 delegate `0x955Cc9fB…84f1`); `arc-testnet` has no delegate and `prepare` answers 400 there. `tests/test_erc8004_arc.py` pins that nothing else moved since 0.89.0
- **Parity with the TypeScript SDK is PINNED**: `tests/test_erc8004_ts_parity.py` compares the four lists and the answers of `supports_*` / `_wire` against `tests/fixtures/erc8004-ts.json`, generated from the PUBLISHED npm package (never edit by hand): `node scripts/erc8004_ts_snapshot.mjs --version <ts-release>` rewrites it, `--check` exits 1 when it drifted. When TS changes a list, regenerate against its release and bump `TS_RELEASE` in the test
- `AgentId = Union[int, str]` - EVM uses int, Solana uses base58 pubkey string
- Writes answer an HTTP error as `success=False` + `error="Facilitator error: <status> - <body>"` (**byte-identical, consumers regex it**) + `status_code` / `retry_after` (seconds or HTTP-date, NOT clamped), read by the one helper `_http_error_fields`; no HTTP answer = both `None`. `tests/test_erc8004_retry_after.py` fails if a write catching `httpx.HTTPStatusError` lacks a row there
- `seal_hash` parameter on `revoke_feedback()` and `append_response()` (SEAL v1)
- Solana uses QuantuLabs 8004-solana Anchor program + ATOM Engine

### On-chain reputation (reputation/, v0.101.0)
- The rater-signed, payment-anchored ERC-8004 rating rail as ONE module, extracted from Karma Hello's `services/reputation/live_worker.py` (per-comment ratings on Base in production) with the guards of KK's `agents_sdk/rating_rail.py` and EM's `shared/relayed-rating.ts`. Not imported by `uvd_x402_sdk` (a base install is unaffected); extra `reputation` = `eth-account>=0.13.0` (`unsafe_sign_hash`, `sign_authorization`). Module README: `src/uvd_x402_sdk/reputation/README.md` (the 15-line program is compiled and its calls checked against the real signatures in `tests/test_reputation_redaction.py`)
- **`validate_preparation(prep, params, rater, *, network)` is the gate before any key.** `network` is required (a prepare naming another chain is a rating about someone else). Order of checks = order of the refusal codes in `tests/vectors/reputation/prepare-rejections.json` (the FIRST failing check is reported; TS must keep the order). Delegate allowlist `RELAY_DELEGATES` keyed by CHAIN ID (x402-rs `relay.rs::delegate_address` at main 09c3e3719; base-sepolia is `0x9551…f787`, NOT the old v3 `0x1AaE…5b45`); `RELAY_CHAIN_IDS` is bound to `RELAYED_FEEDBACK_NETWORKS` by a test. The digest is rebuilt by hand (`relayed_feedback_digest`, pinned to the DEPLOYED v4 vector `0xa7a2…f6e1`) and must equal the prepare's. Integers parse as JSON ints or decimal strings with `fullmatch` (`"18896\n"`, `" 18896"`, `"0x49d0"` are refused: Python's `$` accepts a trailing newline, JS's does not)
- **Signing**: `sign_relayed_feedback` / `build_relay_authorization` accept only a `VerifiedPreparation` and re-check the allowlist and the digest (a hand-built one is refused). The wallet gets the REBUILT typed data in the SDK's `WalletAdapter` form: `types` WITHOUT `EIP712Domain` (eth-account's `encode_typed_data(message_types=...)` raises "Unable to determine primary type" when it is listed). Low-s, `v` in {0,1,27,28}, recovered against the rater. The 7702 digest is hand RLP, byte-identical to `Account.sign_authorization`; chain id 0 is never signed
- **`paymentHash`** = keccak(tx32 ‖ u64be(block) ‖ utf8(EIP-55 payer) ‖ utf8(EIP-55 payee) ‖ u256be(amount)) (x402-rs `types.rs::compute_payment_hash`; alloy displays checksummed), pinned to `tests/fixtures/facilitator-settle-2.40.0`. `pay_and_prove` persists the signed transfer (`on_signed`, REQUIRED for a new transfer: `TypeError` before signing without it) BEFORE `eth_sendRawTransaction`, re-sends the same bytes (`signed=`), treats "already known" / "nonce too low" on a re-send as "decide from the chain", refuses `payment_nonce_consumed` when the latest nonce passed it without our receipt, never pays a contract (code other than empty or a 7702 designator; a code answer that is not a hex string, a `null` included, is `PaymentPending(rpc_unreadable)`, never "no code"), decodes what the signer returned and compares every field (`TypedTransaction.from_bytes` needs `HexBytes`, not `bytes`; one test and one mutant per field in `test_reputation_round3.py` / M22)
- **Outbox**: every write is a CAS on the status (`transition(ref, expect, to, require_unset=...)`), and in SQLite the `UPDATE ... WHERE status = ? AND <field> IS NULL` is the guard (tested with a stale reader, not only the Python pre-check); `attempts` resets on every status change; SQLite updates only the columns that change; uint256 (`agent_id`, `amount`) stored as text; the schema version lives in `reputation_outbox_meta`, NEVER `PRAGMA user_version` (the consumer's file). `repr` of a row / `SignedPayment` names the row only. `forget` deletes rows that never started, tombstones the started ones (the caps keep counting what is on-chain), fails a `paid` one, flags the in-flight ones
- **A row is moved only by its rater**: `OutboxEntry.rater` is written when the worker starts the row (or, for a row submitted without the worker, with `submitting`), and `find_event` / `relay_absent` / `confirm` / `send` read and send at `entry.rater`, never at the signer. `submit.row_rater` (called by `ReputationWorker.tick` for paying/paid/submitting/submitted and by `submit_with_reconcile`) raises `RaterMismatch` (`rater_mismatch`, or `rater_unknown` for a started row without one) before anything is read, signed or written: after a key rotation the new account has no delegation and nonce 0 and "proved" a landed rating absent (third review, P1). The RPC's `eth_chainId` is read when a row starts (`_start`, before the owner check) and before a rating is signed (`prepare_and_submit`); a mismatch trips the breaker and signs nothing
- **Never prepare twice** (`submit.py`): the log scan always starts at the block read before the submit (no cursor moved by an empty answer), and a signature is declared expired only when the `safe` block (`expiry_block_tag`) is past `deadline + expiry_margin` AND on STATE read AT that block by its HASH (EIP-1898 `{"blockHash", "requireCanonical": true}`: a node without it errors instead of answering from its head; a `null` is no answer): rater code = our designator and `nonceUsed(nonce)` (32-byte ABI bool) = false via `eth_call` on the RATER's address, selector `0x61a4422b`; or no code AND this row carried the authorization AND the account nonce at that block is not past the authorization's (an applied authorization, or an undelegation, bumps it). Anything else: `relay_state_unknown` / `relay_landed_unindexed`, and past `deadline + unknown_after` (24 h) `failed` for a human, never re-prepared. A failing `eth_getLogs` does not block the state decision. Proven expired: `failed(relay_expired_unrelayed)` for a human by default (`DEFAULT_MAX_RELAY_ROUNDS = 1`, Karma Hello's `signed_feedback_requires_reconciliation`); `submitting -> paid` (a second prepare) only with `max_relay_rounds > 1`, the consumer's decision, and the check that bounds it is in `reconcile`, not in the prepare. Tests of the re-prepare path opt in with `env.worker.submit_options["max_relay_rounds"] = 3`. Both reviews' P1s (a lagging logs node, a null code read, a lagging state node, the emergency undelegation of a first rating) are pinned in `tests/test_reputation_flow.py`, `test_reputation_hardening.py`, `test_reputation_round2.py`
- **Before signing**, the rater's code is ALWAYS read (`delegated=true` with no designator, or `false` with one: `delegation_state_changed`, nothing signed; the emergency undelegation is the operator's and starts by stopping the worker, since a facilitator reporting the rater as not delegated would get a new authorization on the next rating), and for a first rating the `pending` account nonce must be the authorization's (`account_nonce_moved`); an authorization never expires. Every failed submit feeds the breaker; a prepare refused with a definite 4xx `max_prepare_refusals` times and a row past `max_relay_rounds` (1 by default) expired signatures are `failed` (a poison row must not hold the semaphore for ever)
- **Every outbox write is a compare-and-set on `version`** (`transition(..., expect_version=)`, `version` bumped by every write, `forget` and `tombstone` included; SQLite `WHERE status = ? AND version = ?`): the status alone is open to ABA (`submitting(n1) -> paid -> submitting(n2)`), the refuter's R2-P1-C. `submit.py::_Run.move` and `worker.py::_move` are the only writers
- **Payment**: `payment_nonce_consumed` needs `NONCE_CONSUMED_READS` (5) consecutive reads with the nonce used `max(confirmations, 3)` blocks deep and the payment absent from receipt AND `getTransactionByHash` (a receipt from a lagging node is not a verdict)
- **Governor**: `Caps` has NO defaults (every product brings its own; KH's lifetime 74,000 is its config). Health that raises / `None` / `False`, an unreadable outbox, an unreadable exclusion, an open breaker: no new rating. `Governor.ready()` (breaker + health) is also asked before signing the payment of a started row and (breaker) before preparing a `paid` one. Subjects are compared lowercased everywhere (`excluded` predicates get the lowercased wallet); tests use `MIXED_SUBJECT`, an address with letters, because `0x2222...` hides a missing `.lower()`. Worker semaphore: no new row while one is `paying` or `submitting`; no transfer signed while a rating is `submitting`, no first authorization signed while a transfer is `paying` (each would take the account nonce the other names). One external proof (`amount == 0`) rates one agent once (`anchor_tx`, `outbox.anchor_rows`: `proof_reused`). `[all]` does NOT include `reputation` (eth-account>=0.13 would force web3 7 on `[all]` users)
- **Evidence**: never the chat text, author, login or ids: they go in `private` and come out as HMAC-SHA256(salt, label|value). Keys are refused by exact name AND by word, English and Spanish (`chatText`, `userAgent`, `mensaje`, `autor`, `usuario`); private values are searched case-insensitively, 8+ chars as substrings of tags/endpoint/issuer/context, 4-7 chars as whole words of issuer/context. The `feedbackURI` (`https://`, `http://` or `ipfs://`, Karma Hello's `ipfs_store.py`: x402-rs relays any scheme and fetches only http(s) to audit, `proof.rs::fetch_and_hash_document`, unreachable is not a refusal), tags and endpoint (on-chain for ever) may not contain 16 consecutive hex of the ref from ANY offset, or a viewer id of 6+ chars: `ReputationWorker._identifies` (refuter's P1-2; the prefix-only check was the third review's P3). No floats (canonical JSON for the TS twin). A `!forgetme` on a `paying` row with no signed transfer yet fails it before `governor.ready()` and before signing; a paid one never reaches `publish_evidence`
- **Logs**: `HttpJsonRpc` and `ReputationWorker` install `_redact.redact_http_client_logs()` on `httpx`/`httpcore` (httpx logs the full URL at INFO, httpcore the `host=` / `server_hostname=` at DEBUG). `HttpJsonRpc` parses with `bounded_json.loads_bounded` (`MAX_RPC_RESPONSE_BYTES` = 16 MiB, `MAX_JSON_DEPTH`)
- **Shared vectors** `tests/vectors/reputation/*.json`: written only by `python scripts/reputation_vectors.py` (`--check` exits 1 on drift, 2 when a cross-check against eth-account / the facilitator's proof / the deployed contract fails); sha256 pinned in `tests/test_reputation_vectors.py::VECTOR_SHA256`. Never edited by hand. Test key = `"0x" + "11" * 32`, built at runtime
- **Doubles** (`tests/reputation_doubles.py`): `FakeChain` (virtual blocks from the clock, mining, `NewFeedback` logs, 7702 delegation, authority nonce bumps and `nonceUsed` per block, reads AT a block by number or EIP-1898 hash that error when the node lacks it, `safe` = head - 5, a lagging logs / receipts / state node, null code reads, moved block hashes, receipt logs flagged `removed`, a `safe` header from an orphaned fork whose state only `requireCanonical: true` refuses, a relay tx hash that includes the nonce so two relays never collapse into one) and `FakeFacilitator` (builds typed data like `relay_v4.rs`, verifies signatures with eth-account's encoder and authorizations with `rlp` + eth-keys; scripted modes left unused fail the test at teardown). `tests/test_reputation_mutations.py` (repo idiom `mutant(old, new)`): M1-M15 and their b/c variants, plus M16-M29 for the third review (the rater guard, the rater's state, the payee's code, `on_signed`, both `!forgetme` checks, `ipfs://`, each signed-transaction field, the removed log, `requireCanonical`, the rounds check, the bounded RPC JSON, both chain id checks, the ref windows), each guard removed and its case shown to go through; their scenarios are functions of `tests/test_reputation_round3.py`, so the mutant runs the same case. `tests/test_reputation_hardening.py`, `tests/test_reputation_round2.py` and `tests/test_reputation_round3.py` pin every finding of the three adversarial reviews by its id. A mutation site must appear exactly once in the source: reformatting a guarded line moves the site and the test says so

### The rated party's Solana identity (reputation/solana_identity.py, v0.102.0)
- `ensure_solana_identity(recipient, *, client, agent_uri, budget, network="solana", daily_cap=25, ...)`: the ONE client of the facilitator's Solana `POST /register` (KK `agents_sdk/rep_solana.py::identidad_del_calificado` and EM `reputation_target.py::_ensure_identity` + `identity.py::register_worker_gasless` are to adopt it). Base install (httpx only), though it lives in `reputation`; every eth import of that package is lazy, so `import uvd_x402_sdk.reputation` works without the extra
- **The identity is read on Solana**: `check_solana_identity` = `GET /identity/<network>/owner/<address>` (x402-rs `get_identity_by_owner_solana`, a `getProgramAccounts` scan). EM's guard read it with an EVM `eth_call` and therefore never minted (the bug this replaces). Only the owner lookup's OWN 404 is "owns nothing": its body carries the STRING `balance: "0"`; the router's fallback 404 (`code: not_found`) has none, and reading any 404 as absent would mint behind a wrong base URL. Not the number 0 either (JSON.parse cannot tell 0 from 0.0, so TS could not draw that line). 200 must name THIS owner and network and a 32-byte agent id; anything else is `unknown` and nothing is spent
- **Spend decisions, one line each** (each has a mutant in `tests/test_solana_identity_mutations.py`, M1-M50): `MintBudget.reserve` before the POST (an unresolved reservation older than the hold becomes kept-and-expired; then `in_flight` -> `held` -> `address_exhausted` -> `quota_exceeded` -> `granted`, in that order); `SOLANA_IDENTITY_DAILY_MINT_CAP` = 25 per UTC day AND network (owner decision 2026-10-27; `daily_cap` 0..25, more is `ValueError`: raising it is a release; the window is the UTC CALENDAR day, `_utc_day`, pinned by `test_the_daily_cap_is_the_utc_day_not_an_hour_nor_24_hours` and the vector's `the-daily-cap-is-the-utc-day`: the refuter's MR8, `now // 3600`, passed the whole suite before them), `MAX_MINT_ATTEMPTS_PER_ADDRESS_PER_DAY` = 3 in a ROLLING 24 h and `MINT_HOLD_SECONDS` = 1800 (EM's `_MINT_MAX_ATTEMPTS_PER_DAY` with its last-day window / `_MINT_PENDING_TTL_S`). A slot is RELEASED only when the answer proves nothing reached the chain: 400/403/408/409/429/503 with a JSON object body, `success` not true and `mint.status` absent or `not_minted`, and for 408/429/503 (`_MARKED_STATUSES`: a gateway answers those too, maybe after forwarding) the facilitator's mark (`_marked`: a lower_snake `errorCode` / `mint.errorCode` / `code` / `reason`, or `mint.status: not_minted`; else `uncertain` / `unmarked_answer`, KEPT; not on 400/403/409, since two 400s of x402-rs carry no code) (x402-rs answers those only before sending: `refuse_registration`, the run-time refusals, the daily cap, the per-IP limiter, the admission layer of `rate_policy.rs` with `overloaded` / `too_many_concurrent_requests` / `request_timeout`, the writer lease with `delivered=false`), or `ConnectError`/`ConnectTimeout`/`PoolTimeout`. Everything else is KEPT and holds the address: a 500 `not_minted` included (`send_and_confirm` errs on a confirmation timeout while the transaction can still land), a 502 `forward_unconfirmed`, a 504, any 2xx but a 200, a read timeout, a `ProxyError`, an answer that is not JSON
- The POST is repeated in the same call only for pre-chain refusals that pass (`_TRANSIENT_REFUSALS`, no connection), `register_attempts` at most, `Retry-After` honoured only up to `max_backoff` (longer ends the call with `retry_after`; `max_backoff` itself is 0..`MINT_HOLD_SECONDS / 3`, `ValueError` above, so a mint another caller sent during the wait still holds the address), and EVERY reservation, a retry's too, acts on an owner lookup read just before it (the lookup is the first thing of each turn of the loop in `_ensure_once`): the refuter's P2-1 of c0der's round 1 (`max_backoff=3600` + a 3600 s `Retry-After` slept past another process's hold and minted a second identity on the first lookup; a process frozen during the backoff does the same with any `max_backoff`). `fee_payer_insufficient_balance` and the facilitator's 429 `erc8004_daily_write_limit` (-> `quota_exceeded`) are not repeated. The 409 `held_identity_not_resumable` is `refused` (a 7th status beyond the six of the order: retrying gets the same answer). A facilitator code enters `reason` / `error_code` / `repr` / logs only if it is lower_snake (`_code`), else `facilitator_error`: the codes are the facilitator's text and base58 is not redacted by `redact`
- **`budget` is REQUIRED (no default)**, the refuter's P2 of round 1: an implicit per-process `InMemoryMintBudget` made the "hard" 25 into 25 per worker and lost the hold on a restart (a mint that landed with its answer lost, a lagging lookup, a restart within the lag = a second identity, reproduced). `InMemoryMintBudget` is for ONE long-lived process. The in-flight join (module `_IN_FLIGHT`, keyed by `(loop, key)`: a call of another loop is NOT joined nor turned away, it goes through the lookup and the budget, so a loop stopped mid-call blocks the address for the hold and not for ever, the refuter's P2 of round 2) is per process; a shared budget keys the address exactly (base58 is case-sensitive: EM's claim lowercases the wallet, which on base58 would merge addresses). A joiner gets the first call's result whatever its own arguments. `request_sent` is false when no POST connected (read from the exception branch, never from `reason`, which can carry a facilitator code). `MintBudget.keep` / `release` name their request by `reserved_at` (the `now` of its `reserve`): a caller back after its request expired never undoes a newer one. Timeouts are a TOTAL deadline (`asyncio.wait_for`, not httpx's per-phase ones) capped at `MINT_HOLD_SECONDS / 3`, so a request in flight never outlives its reservation. `_code("")` is `None` (an empty code hides nothing). `_resolve(step, key, **kw)` wraps the CALL of `keep` / `release`, not only its await (a synchronous budget raises before there is anything to await), and a `reserve` answer that is not one of the five strings (a list, a dict: unhashable) is `budget_unavailable`; neither makes the call raise
- `agent_uri` must be the same for the same recipient on every call (`https://` or `ipfs://`, no whitespace): the facilitator recognises a retry of a half-minted identity by it (`decide_mint`), never by the recipient alone (another URI for a held identity = a fresh mint, paid again; the same URI for another recipient = 409, both pinned in `tests/test_solana_identity.py`). A complete mint is NOT recognised: two concurrent POSTs mint two identities, which is why the join and the budget exist
- Accepted on purpose (documented in the module): the lookup runs BEFORE the reservation (an address that owns one never touches the budget nor reads `quota_exceeded`; a call stalled longer than the hold between the two acts on that lookup), and the hold runs on the caller's wall `clock` (a shared store compares it across processes), and an owner lookup that keeps answering "owns nothing" after a mint of ours landed mints again after each hold, at most 3 per address in 24 h (the caps bound it)
- Verdicts are pure (`lookup_verdict`, `register_verdict`, `_transport_verdict`, `_RESERVE_REFUSALS`) and pinned for the TS twin in `tests/vectors/reputation/solana-identity.json` (sha in `VECTOR_SHA256`; transport failures by MEANING, not httpx class; the `budget` section replays `InMemoryMintBudget` step by step, so a TS budget must match its rules too). The TS repo keeps its own copies and pins exactly the files it has (`src/reputation/vectors.test.ts`), so this file reaches it only when the TS port copies it. Double: `tests/solana_identity_doubles.FakeRegistry` (keeps on-chain state apart from what the lookup shows; scripted answers can land a mint and still answer a timeout / 500; resumes a held identity only as `decide_mint` does, same `agentUri` and recipient). A mutation scenario must still isolate its guard after a new one lands: the round-1 re-read and the mark masked M15-M17 until their scenarios got a lagging lookup and a marked body

### Escrow Pre-Auth Builder (escrow_signing.py)
- `build_escrow_pre_auth(payment_config, network, payer, receiver, amount_usd, deadline, wallet, tier)` - signs the ADR-002 sign-on-assignment escrow lock (`ReceiveWithAuthorization`) and returns the raw JSON `X-Payment-Auth` header value
- `compute_escrow_nonce(chain_id, escrow_address, typehash, payment_info)` - `AuthCaptureEscrow.getHash` mirror (raw keccak, payer zeroed, receiver INCLUDED - the signature commits to the worker, so signing happens AT ASSIGNMENT, never before)
- **Golden vectors are PINNED** in `tests/fixtures/escrow-preauth.json` (byte-identical copy of Execution Market's `shared/test-vectors/escrow-preauth.json` F0-1 fixture; re-copy from there, never edit here)
- Same math as `AdvancedEscrowClient._compute_nonce` but standalone (dict-based, no web3); `EnvKeyAdapter.sign_eip3009` produces the same digest when payer == adapter wallet - parity pinned in `tests/test_escrow_signing.py`
- Fail-loud: unknown network / incomplete config raises `ValueError` (silent domain fallback = wallet-draining auth)
- **ONE implementation (Unreleased).** EM and KK kept copies; they are to import this one (each drops its copy when it adopts the release that carries this). `escrow_signing.__all__` is the stable API (module docstring, "Public API (stable)"); `tests/test_escrow_signing_api.py` pins `__all__`, the top-level re-exports and every parameter. Never rename or move a parameter there: it breaks their import, not ours
- **The pre-auth's chain id and addresses are checked against the SDK's tables, before signing** (`_check_chain_id`, `_check_registered_addresses`). `chain_id` against the `network` name (network registry, aliases, `eip155:<id>`; a non-EVM name is chain 0 and never matches); `escrow`, `token_collector`, `usdc` against `ESCROW_CONTRACTS[chain_id]`; `operator` (= `feeReceiver`) against `ESCROW_OPERATORS[chain_id]`; compared checksummed, as signed. Same policy as the domain: row present and a different value = `ValueError`. `max_fee_bps` above `ESCROW_MAX_FEE_BPS[chain_id]` (`_check_fee_ceiling`) is refused the same way. Operator and fee rows exist on the 11 chains of `VERIFIED_USDC_DOMAINS`; the testnet rows (84532, 11155111) have only `ESCROW_CONTRACTS` and are refused (below). A bool `chain_id` is refused. `tests/test_escrow_signing_registry.py`: one test per address per chain of `VERIFIED_USDC_DOMAINS` (table signed, one hex digit changed refused, nothing signed; the two testnet rows are refused whatever they carry), the fee ceiling signed and +1 bps refused on the 11 chains, both refusals also on the EIP-7702 paths and `TestGuardMutations` (each guard removed from the source turns its refusal into a signature). A NEW operator on a chain that has a row (an operator rotation) is refused until the row changes in a release
- The SDK's and KK's vectors carry SYNTHETIC operators (the chain id as an address): their replays register the vector's operator in `ESCROW_OPERATORS` (`operator_registered` in `tests/test_escrow_vectors.py`; `scripts/escrow_preauth_vectors.py` does the same, `--check` still exits 0). Unregistered, exactly the vectors on chains with an operator row are refused (`TestOperatorRows`). EM's vectors carry the real operators and run against the table as it is. A test config with filler addresses on a chain with a row now fails at the guard
- **No row, no signature (v0.97.0, owner decision 224).** `_check_rows`: a chain without a row in EACH of `ESCROW_CONTRACTS`, `ESCROW_OPERATORS`, `ESCROW_MAX_FEE_BPS`, `VERIFIED_USDC_DOMAINS` (`_REQUIRED_ROWS`) raises `ValueError` naming the missing tables, before signing; `_check_chain_id`: a `network` the registry does not resolve (not a registry name / alias, not `eip155:<id>`) raises too, since its chain id cannot be checked. Until 0.96.0 both signed with a logged warning; the module no longer logs. The 11 chains with every row sign; HyperEVM 999, Unichain 130, Scroll 534352, Robinhood 4663 (registry, no escrow rows) and the two testnets are refused. A new chain signs only when a release adds ALL its rows (`test_every_chain_with_an_operator_row_has_every_row`). The `is not None` in `_check_registered_addresses` / `_check_fee_ceiling` is on purpose: each guard stays independent, and `tests/test_escrow_signing_rows.py::TestGuardMutations` removes each one (one table at a time, the whole row guard, the unregistered-name guard) and asserts its case is then SIGNED. Parity with the TypeScript twin (uvd-x402-sdk 2.104.0, SDKTS-ESC-03): same constants (`ESCROW_MAX_TIER_WINDOWS` = TS `ESCROW_TIER_WINDOW_MAX`, `ESCROW_MAX_AUTHORIZATION_SEC` 3196800) and the same verdict on every case of `tests/fixtures/escrow-preauth-input-cases.json` but one: `min_fee_bps` 1301, refused here and signed by TS 2.104.0, which has no `min_fee_bps` ceiling. The table is the place to add a case both SDKs must agree on; run it through the published TS package when it changes
- **Tier windows and `authorizationExpiry` are bounded (v0.97.0).** `_tier_windows`: each of `pre` / `auth` / `refund` of the tier (config's `tier_timings`, else `ESCROW_TIER_WINDOWS`) must be a `_config_integer` (an int, a float with no fraction, what JSON `3600.0` is in TS, or ASCII digits: TS `configInteger`) from 0 to `ESCROW_MAX_TIER_WINDOWS` = 86400 / 604800 / 2592000 (the enterprise tier of `advanced_escrow.TIER_TIMINGS`; `auth` == `REVIEW_WINDOW_SEC`, so the config cannot move `authorizationExpiry` past `max(now, deadline) + REVIEW_WINDOW_SEC`). `ESCROW_MAX_AUTHORIZATION_SEC` = 720 h (EM's longest `deadline_hours`) + `REVIEW_WINDOW_SEC` = 3196800 s, TS's value, no clock slack in either: a later `authorizationExpiry` is refused. A config tier is used unless absent or `null` (TS `??`): `{}` / `[]` / `0` / `""` are refused, never replaced by the SDK's. `chain_id` (1..2**53-1) and `min_fee_bps` / `max_fee_bps` (0..65535; `null` = default) go through `_config_integer` too: `" 8453"`, `"+8453"`, `"8_453"`, full-width digits, `8453.5`, bools = `ValueError`, never `TypeError` / `OverflowError` / a truncated value. Refused, never clamped. EM serves pre 3600/7200, auth 604800, refund 1209600: all sign. Same release: `deadline` must be whole epoch seconds (int or float without fraction, |d| <= 2**53-1 as TS `isSafeInteger`; a fractional one put a float on the wire while the nonce hashed it truncated); `min_fee_bps` within 0..`OPERATOR_FEE_BPS` (a floor above the operator's 1300 bps makes every capture revert); the EIP-712 domain signed is the `str` pair the domain check compared (`_afirmado`), never the raw config value (a version written as the number 2 was checked as "2" and signed as 2)
- `payment_info_typehash` other than `ESCROW_PAYMENT_INFO_TYPEHASH` (= keccak(`PAYMENT_INFO_TYPE`) = `advanced_escrow.PAYMENT_INFO_TYPEHASH`) is refused before signing (from KK's copy). A test config with a filler typehash now fails there, before whatever it meant to test
- **Three vector sets in `tests/test_escrow_vectors.py`, none edited by hand**: EM's (above; its `additional_networks` = Arc), KK's ten chains (`tests/fixtures/escrow-golden-vectors-kk.json`, byte-identical copy of KK's `tests/sdk/fixtures/escrow_golden_vectors.json`, re-copy from there; key = repeat-0x11 built at runtime), and the SDK's own (`tests/fixtures/escrow-preauth-networks.json`: one per chain of `VERIFIED_USDC_DOMAINS` + Base variants, header sha256 / nonce / digest / signature; `python scripts/escrow_preauth_vectors.py --check` exits 1 on drift). A new chain in `VERIFIED_USDC_DOMAINS` without a vector turns `test_one_vector_per_chain_with_a_verified_domain` red: re-run the generator, and the diff of the fixture must be that one vector and nothing else. `TestMutations` mutates the module SOURCE (domain, field order, chain id) and asserts the exact set of vectors that fall

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
- **Amounts are integers in atomic units** and the asset key carries the network (`TokenAsset("base", USDC)`). **One chain, one key**: `base` and `eip155:8453` resolve to the same canonical name via `normalize_network`, so a policy written in either dialect covers a challenge priced in the other (v1 and v2 answer the same). Different CHAINS stay different keys — `polygon` / `eip155:137` is not covered — and a dialect the registry cannot resolve falls back to the literal, which refuses rather than raising
- **`scheme` is required and its vocabulary is closed** (`exact`, `upto`, `escrow`, `commerce`, `fhe-transfer`), same as the facilitator's enum. Until 0.82.0 an entry without one was assumed `exact`; measured against the repo's one real capture (`tests/test_x402_transport.py`, 36 of 36 live 402s on 2026-08-20) every seller names it, so the assumption covered nobody and was a silent way to sign `exact` for an offer that asked for something else
- **`canonical_address` is not `lower()`**: hex folds, base58 (Solana, XRPL) compares exactly. Folding base58 refuses legitimate payees and can admit one nobody listed
- **Evaluating does not spend** (`record_spend` is a separate call, after settlement), **copies share the purse** (`copy` AND `deepcopy`), and unreadable purse state reports the CEILING, never zero
- `validUntil` comes from `extensions["offer-receipt/1"].info.validUntil`, Unix seconds, JSON number only. Unreadable = absent, NEVER zero. `validUntil == now` still stands
- **Defaults**: `X402Client` holds `PurchasePolicy.permissive()` when no policy was supplied (nothing changes for existing callers); `PurchasePolicy()` denies an asset with no ceiling. `PolicyRefusedError` subclasses `NoAcceptablePaymentError` for compatibility — `exc.code` is `POLICY_REFUSED`, the contract code is `exc.refusal_code`
- **What it does NOT bring** (same as Rust): offer-receipt signature verification, input binding, `upto` accounting, settlement reconciliation. Nothing is persisted

### A third party's JSON is bounded before it is parsed (bounded_json.py, v0.98.0)
- **Why**: CPython's JSON parser recurses per nesting level and the SDK runs with a raised recursion limit (`py_ecc`), so unbounded nesting is not reliably a `ValueError`. Every `json.loads` of bytes a seller (or a buyer, on the merchant side) wrote goes through `loads_bounded`: `payment_challenge_from`, `X402Client.fetch`, `dx402.parse_evidence_header`, `receipts.response_receipt`, `receipts.validate_purchase_context`, `receipts.verify_receipt`. A new reader of third-party JSON does the same
- **Measure what is parsed**: size on the input as given (a base64 header before decoding), depth on the text `json.loads` will parse (base64-decoded header; bytes decoded with `json.detect_encoding`, so BOM / UTF-16 / UTF-32 count; never the HTTP `charset`). Measuring another decoding can read an escaped quote as a delimiter
- **No recursion**: `_depth_exceeds` is one regex-driven pass that tracks strings and backslash escapes; stray closers never push the count below zero. `max_depth` is capped (`_MAX_DEPTH_CEILING`) so a caller cannot lift the bound past what the parser handles. `tests/test_bounded_json.py` runs the deep cases in a subprocess (a dead interpreter is a return code) and fuzzes the count against `json.loads`
- **Public API, stable**: `MAX_CHALLENGE_BYTES`, `MAX_JSON_DEPTH`, `JSONBoundsError` (`ValueError`, `reason`), `json_depth_within`, `loads_bounded`. New parameters go keyword-only

### ERC-8128 Signed HTTP Requests (erc8128.py)
- `sign_request(wallet, method, url, body=None, nonce=None, ...)` - RFC 9421 request signing over any `WalletAdapter` (EIP-191 personal_sign); returns `Signature` / `Signature-Input` / `Content-Digest` headers
- `fetch_nonce(api_base)` - async, gets the single-use server nonce (5-min TTL, one per signed request including retries)
- **Wire format is PINNED** — byte-equality enforced in `tests/test_erc8128.py` against `tests/fixtures/erc8128.json` (byte-identical copy of Execution Market's `shared/test-vectors/erc8128.json` F3-1 golden vectors; re-copy from there, never edit here): `alg="eip191"` emitted, keyid ALWAYS lowercase, params order `created;expires;nonce;keyid;alg`
- Importable on a base install (httpx + stdlib; no eth-account until an adapter is instantiated)

### Interop contract (interop/ + uvd_x402_sdk.interop)
- `interop/` at the repo root IS the contract of the stack (Markdown + JSON Schema + `fixtures/` + `vectors/`), language-independent: the TS SDK and a Rust crate vendor it from a commit of `main`. `uvd_x402_sdk.interop.conformance` is only its reference implementation
- Runner: `python -m uvd_x402_sdk.interop check [--interop DIR] [MANIFEST ...]` (or `uvd-interop`), offline. Exit 0 green, 1 red, 2 could not run. Needs `jsonschema` (extra `interop`). **The wheel does not ship `interop/`**: without `--interop` it only finds the source checkout
- Every vector `kind` has a case check in `_KINDS`; `tests/interop/test_conformance.py` flips every expectation of every case (`TestEveryCaseIsLive`) and runs a list of plausible wrong implementations that must each go red (`WRONG`). A new kind or case needs both, or it guards nothing
- A VALID fixture must also pass the runner rules the schema cannot see: `en-migracion.json` declared an authority no endpoint served (R3.3) and was green in the schema suite for a whole PR
- `publish.yml` runs `python -m pytest -q tests/interop` (schemas + runner + vectors), then the whole suite offline, in its `check` job (read-only, no id-token) that `build` and `publish` need; it runs by hand only (see "Publishing to PyPI")
- R3.1 decides by OPERATION, not by method: an MCP `tools/list` or public read tool travels by POST and is a public read (never signed). The `request-signing` vector carries `operation`; a StackClient that signs every POST fails it
- An event inbox decides in this order: signature (401), nonce (409 `nonce_replayed`), duplicate (200 `already_processed`), `stale_sequence`, accept. R5.9's "never 409" is about duplicates; the vector pins the order

### Settle Overrides, Retry & Non-Raising Settle (client.py, v0.36.0)
- `settle_payment()` / `verify_payment()` / `process_payment()` accept `asset` (token contract address) and `eip712_domain` (`{"name", "version"}`) overrides — the caller's token registry wins over the SDK's (non-USDC settles, registry drift). Defaults unchanged: network USDC + registry domain
- `create_authorization()` accepts the same `eip712_domain` override — enters the SIGNED digest and the non-USDC `token.eip712` block. Partial domain raises `ValueError` before signing (fail-loud)
- `settle_payment(..., retry=True)` (default OFF) — up to 3 attempts, backoff 1s/2s, ported from Execution Market's `mcp_server/integrations/_http_retry.py`: retries transient transport errors + 5xx, NEVER 4xx, NEVER `success=false` in a 2xx, NEVER a 5xx whose body carries a tx hash (**anti-double-settle guard**)
- `try_settle_payment()` — non-raising settle returning `{"success", "tx_hash", "error"}`; `success=False` + `tx_hash` set = the facilitator broadcast despite the error status (verify on-chain, never re-send)

### Idempotency-Key & spent nonce (client.py, v0.83.0; one binding per handling since v0.89.0)
- **A key derived from the X-PAYMENT alone is NOT a binding.** Whoever holds the payment recomputes it; a buyer resending their own X-PAYMENT in a NEW request would make the seller send the same key and get the original settle back, the replay x402-rs 2.39.0 closes (`docs/facilitator-receipts.md`: "Possession of the signed payment alone is not a purchase binding"). The TypeScript SDK (2.97.0) does the same
- **On by default since 0.89.0** (opt-in 0.83.1-0.88.0). `_Binding` = ONE key per handling (a `process_payment()` call, or a `settle_payment()` call with its retries and timeout fallback), the same on `/verify`, `/settle` and the fallback resend: `new_idempotency_key()` = `x402-<64 hex>` (random, TS `createIdempotencyKey()` shape) unless the caller brings `idempotency_key` (validated: 1-255 visible ASCII, no `receipt:` prefix) or `idempotency_scope`; both at once = `ValueError`. `send_idempotency_key=False` = no key at all. The key is reported in `idempotency_key` on the responses, `exclude=True` so it never reaches `PAYMENT-RESPONSE`
- **The guard (`_Binding.refuse_foreign_replay`)**: a handling with no binding of its own (fresh key or none, no `X-UVD-Purchase`) that gets `Idempotent-Replayed: true` (200 success or 202) BEFORE any of its attempts ended without a verdict (timeout, transport error, 5xx set `may_have_admitted`) raises `PaymentSettlementError(reason=authorization_already_settled | authorization_in_flight)` with the receipt. Independent of how the key was made; every middleware inherits it through `process_payment`. Mutation (guard removed) turns the 2.38 row of `tests/test_integrations_replay.py` red for all 12 entry points
- `derive_idempotency_key(payload, op, scope)` stays public: `x402-settle-<sha256>` for BOTH ops (0.83.0-0.88.0 sent `x402-verify-` on verify, which the receipt rail does not bind). Without scope over the signed `payload` block (sorted keys, no whitespace; the 0.83.0 vector, unchanged), with scope over the array `["x402-idempotency-scope/1", block, scope]`. Pinned vectors: unscoped, scoped ASCII, scoped non-ASCII. Used only when the caller passes a scope, and a binding only while the scope is a seller secret
- Receipt rail (2.39.0): bound resend = original answer + `Idempotent-Replayed` (or `202 settlement_in_progress`, transient, retried by `retry=True`); unbound = `409 authorization_already_settled` / `authorization_in_flight`, `/verify` `isValid: false` with the reason; other context/terms = `409 receipt_request_conflict`. `admitted_authorization_code()` names them; `payment_conflict_response()` = 409 (settled, conflict) or 503 + Retry-After (in flight, TS parity); middlewares never 402 them. Facilitators before 2.39.0 (production until 2026-09-23 07:06:29Z) did not tie the replay to the binding: hence the guard. Doubles of 2.39/2.38/legacy in `tests/receipt_rail.py`
- **No verdict is never 402.** `_undelivered_response()` (private, used by every integration): `payment_conflict_response()` first, then anything `is_transient_error()` calls transient (timeout, `202 settlement_in_progress`, `503 idempotency_store_unavailable` / `receipt_store_unavailable`, retryable 5xx, 429) = 503 + `Retry-After` via `transient_503_response()` (reason `settlement_in_progress` for the 202) + the receipt in `PAYMENT-RESPONSE`. The timeout fallback polls a bound `202` for up to `SETTLE_IN_FLIGHT_POLL_SECONDS` (30s, pause `_in_flight_poll_interval`: Retry-After, else receipt `retry.afterSeconds`, capped at 5s) and then RAISES the 202, never a TimeoutError: the first cut of 0.89.0 answered 402 over a payment that moved
- Facilitator contract, measured on x402-rs 2.28.0 `post_settle`: sha256 of the RAW body; same key + same body = cached 200 + `Idempotent-Replayed: true`; same key + other body = `409 idempotency_key_conflict`; only SUCCESS is cached; unreadable store = `503 idempotency_store_unavailable` and no settle (fail-closed, prod has `IDEMPOTENCY_TABLE_NAME`); `/verify` ignores the header
- Pinned against real-socket stubs of that contract in `tests/test_idempotency_local_facilitator.py` and `tests/test_idempotency_opt_in.py` (random key by default, key off, two scopes, same scope, 409/503 through the scoped and default paths), not the binary. With the key on by default, a store the facilitator cannot read refuses the settle (503, nothing moves) where a keyless call settled: transient, same credential later. It does not catch a NEW authorization for the same purchase (the block still enters the key), and `process_payment()` verifies first (verify has no cache)
- `is_spent_nonce_error()` / `spent_nonce_evidence()` ported from tarotof (`api/main.py`, commit `534d133d`): codes first (normalised, nested), wording second. **It must stay a SUPERSET of tarotof** (upstream-first): same substring match, so `NonceAlreadyUsed`, `NonceReused { .. }` and `nonce_used` inside prose and structs are caught; the only subtraction is `_NOT_SPENT_WORDS` (`refused` — the facilitator's transient `NonceOrMempool` message — and `unused`). A whole-word match was tried in the first cut of 0.83.0 and missed four measured spent inputs: the expensive direction. Pinned by the fixed table in `tests/test_spent_nonce.py`. Added over tarotof: `idempotency_key_conflict`, `idempotency_cache_corrupt`, `authorization_already_settled`, `receipt_request_conflict` (NOT `authorization_in_flight`: transient), the USDC revert "authorization is used". Known cheap false positives (EOA "nonce has already been used", negations, `gas_used` echoes) are kept on purpose. On EVM the UVD facilitator answers a used authorization with an opaque `400 contract_call_failed (ref)`: the classifier cannot see it there
- `[fastapi]` extra carries `eth-account` (ERC-8128 `verify_request` imports it lazily); `scripts/smoke_fastapi_extra.py` proves it in a fresh venv

### BindingStore: the key outlives the process (bindings.py, Unreleased)
- Ported from describe.net's paywall (upstream-first; describe.net adopts it back from the published SDK). Closes the Lambda that dies between the settle and the delivery: with a fresh key per request the resend gets `409 authorization_already_settled` from the receipt rail; with the stored key it gets the original settle back
- `BindingStore.bind(payment_key, resource, ttl_seconds) -> Binding | None`, never raises. `InMemoryBindingStore`, `PostgresBindingStore(pool)` (psycopg 3 pool, `POSTGRES_BINDINGS_SCHEMA`), `DynamoDBBindingStore(client)` (`DYNAMODB_BINDINGS_KEY_SCHEMA`). Four conditions, each pinned in `tests/test_bindings.py`: (a) conditional insert + re-read, one key and one `new` under concurrency; (b) random key, never derived from the header, header never stored; (c) `None` = fail closed (503, facilitator NOT called; Postgres `statement_timeout` 1000 ms); (d) one row per payment with its owner resource. Expiry renews the key, never the owner; a resend never extends it. No TTL / sweep on the rows: they are also the owner and the "presented before" proof
- DynamoDB differs in ONE thing: no server clock, so expiry uses the process clock. moto is not atomic across threads (`update_item` reads, checks, writes without a lock): `tests/binding_doubles.py` serialises its requests to model DynamoDB's atomic conditional writes. `FakePostgres` evaluates the SQL (raises on SQL it does not know) and serialises transactions; the READ COMMITTED subtlety of (a) and the hung lock are checked only against a real Postgres (`UVD_TEST_POSTGRES_DSN`)
- `process_payment_bound()`: parse (no garbage reaches the store) -> bind -> `payment_store_unavailable` (503) / `payment_already_used` (409, facilitator not called) -> `X402Client._handle_payment` (process_payment's verify + settle) under the stored key -> a would-be 402 on a payment already seen (`Binding.new` false) becomes `payment_presented_before` (409). Mapped first in `_undelivered_response`
- **`_Binding.brought = not binding.new`**: a first presentation keeps the fresh key's guard against a foreign replay; only a resend owns a replay. The row keys on the header BYTES, so a re-encoded header is a new row: without this, a store switched the guard off and 2.36-2.38 replayed the first purchase to another resource (reproduced before merge, pinned in `test_the_same_authorization_re_encoded_for_another_resource_is_not_delivered`)
- FastAPI (4 entry points) and Lambda (2) take `binding_store=`; without it the call is exactly 0.91.0's (`test_without_a_store_process_payment_is_called_exactly_as_in_0_91_0`). Resource = method + percent-encoded FULL path (mount included: `_full_request_path`, the same path `_requested_paths` reads first; two apps at `/a` and `/b` sharing a store sell `/a/x` and `/b/x`) + query + sha256(body) (`purchase_resource`). FastAPI reads the ASGI scope, NEVER `request.url` (decoded path re-parsed: `%23` cut it; some Starlette releases take the `Host` header). Lambda reads API Gateway 1.0 and 2.0; v1 query sorted by KEY only. A store with `send_idempotency_key=False` raises at construction; `X402Middleware` with a store raises on Starlette < 0.28 (measured: 0.27.0 hangs the route after the settle)
- Within the window the handler runs again (`idempotent_replayed`); headers (Authorization, cookies) are not part of the resource. Documented, not solved
- The closing test is `tests/test_bindings_integrations.py::test_the_lambda_dies_between_the_settle_and_the_put_and_the_resend_recovers_the_verdict` (6 entry points x 3 stores x receipts/legacy); its contrasts (no store, a store that does not persist) assert that the same death loses the payment, which is what proves the recovery comes from the stored key

### What the integrations answer an undelivered payment (client.py `_undelivered_response`, v0.90.1)
- Every middleware and decorator goes through it. First match wins: receipt-rail conflict (`409`, `503` in flight) -> a transaction on a failure that is not transient (`500`) -> `is_spent_nonce_error` (`409`, with `spentNonceEvidence`) -> `is_transient_error` (`503` + `Retry-After`) -> any other `5xx`, and a `4xx` from `/settle` that is not a refusal of the request (`500`) -> a receipt `pending` / `unknown` on anything else (`503` + `Retry-After`, Unreleased; FastAPI already answered it so) -> `None`, the integration's own `402` (`400` in `require_payment`)
- A settle runs only after `/verify` accepted the same payload, so its opaque `400 contract_call_failed (ref)` (EVM revert) or `400 internal_error (ref)` (the nonce store of Stellar/Algorand/NEAR/Sui) is not a bad signature: `500`, as in TS. `FacilitatorError.operation` (`"verify"` / `"settle"`) says which call failed; `_SETTLE_REQUEST_REFUSALS` lists the settle `4xx`s that ran nothing and stay `402`. A settle `200 success:false` keeps its `transaction` (mined and reverted) and answers `500`; a settle `200 {"isValid": false}` (x402-rs re-validating) is a `PaymentSettlementError`, a rejection, not a pydantic `ValidationError`
- **Never `402` over a payment that may have moved.** `502 settlement_unconfirmed` (broadcast, no receipt, may be mined) is `500` with `transaction` / `paymentId`, never `503` either: TS answers it the same way (`settlementFailureBody`). Before 0.90.1 it was a `402`
- The anti-double-settle guard in `is_transient_error` did NOT change: the SDK re-sends none of these; only the answer to the buyer changed
- Still `402`: the opaque `400` tokens on **`/verify`** (`contract_call_failed (ref)` on EVM, `internal_error (ref)` on Stellar/Algorand/NEAR/Sui), which a used authorization shares with an invalid signature; the facilitator's verdicts (`isValid: false`, `success: false` without a transaction); the settle-request refusals. Pinned in `tests/test_integrations_undelivered.py` (with mutations) and `tests/test_integrations_replay.py`

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
- `BazaarClient.list_resources()` - GET /discovery/resources (with pagination, filtering). `q` cap per client (`max_search_len`, default `MAX_SEARCH_LEN` = 400, code points, `None` = server decides; x402-rs 2.47.0+ takes 400, 2.46.1 and earlier 400 above 128). `max_price_usd` / `method` / `has_input_schema` / `kind` / `exclude_host` go on the wire ONLY when passed: x402-rs before 2.47.0 answers 400 to an unknown parameter (`DISCOVERY_QUERY_PARAMS`), so a call without them must stay the old request (`tests/test_discovery_filters.py`). Each filter matches what x402-rs parses, measured in its `src/discovery_search.rs`, not guessed: `METHOD_FILTERS` = its `METHODS` (GET/POST/PUT/PATCH; HEAD and DELETE are a 400 there), `maxPriceUsd` plain digits within `MAX_PRICE_CHARS` (32), `excludeHost` ONE comma-separated value (a list is joined)
- `bazaar_extension()` (response.py) - the two historical shapes are byte-pinned; the spec shape (`info` + `schema`, coinbase/x402 `specs/extensions/bazaar.md` @ `dd927a2`, `BAZAAR_SPEC`) is chosen by POST/PUT/PATCH + `input_schema`, `body`, `body_type` or `info=True`. Every emitted `info` must validate against its own `schema` (the spec says facilitators must check it before cataloging; x402-rs does NOT, it stores the block as sent): `tests/test_bazaar_extension_spec.py`
- `BazaarClient.register_resource(..., extensions=)` - POST /discovery/register. `extensions` (keyword-only; usually `bazaar_extension(...)`) goes in the body verbatim, which is how a listing gets `hasInputSchema: true` and a probe with its declared method; without it the body is the old one byte for byte. Pinned against the body of x402-rs's own route test (`tests/fixtures/bazaar-register-x402rs.json`, transcribed from `tests/bazaar_listing_data.rs`, never edited here) and x402-rs's two readers ported in `tests/test_discovery_register_extensions.py`. When x402-rs changes how it reads `extensions.bazaar`, re-port them from its source and re-run
- `X402Config(discoverable=None|True|False)` + `with_discoverable(accept)` (Unreleased; same rules as TS `discoverable` / `withDiscoverable`). The facilitator's settlement auto-registration reads `paymentRequirements.extra.discoverable` (JSON bool, x402-rs `src/handlers.rs` "Phase 2: Settlement Tracking"), NOT `extensions.bazaar.discoverable`. `None` = not set (nothing written); `True`/`False` = written as is. `X402Config.with_discoverable(extra)` is the one place the config adds it: v2 402 options and `_build_payment_requirements` (verify + settle). Native Hedera is left as it comes there; the per-accept `with_discoverable` raises on it (`_is_native_hedera`, case-insensitive). Gotcha: `create_402_response_v2` drops a non-canonical network spelling (`HEDERA:TESTNET`, `hedera-testnet`) from its offers because `X402Config.is_network_enabled` is exact. Non-bool = `ValueError` before I/O. `tests/test_discoverable_extra.py`
- `DiscoveryResource`, `DiscoveryResponse` Pydantic models

### Escrow State Queries (escrow.py)
- `EscrowClient.get_escrow_state()` - POST /escrow/state
- Reads on-chain escrow state without settlement

### Settlement Account Payload (models.py)
- `SettlementAccountPayload` - For Crossmint/custodial wallets that sendTransaction (not signTransaction)
- Fields: `transactionSignature`, `settleSecretKey`, `settlementRentDestination`

### DX402 durable evidence in the seller middleware (durable.py, v0.99.0)
- `durable=DurableConfig(...)` on `fastapi_require_payment` and `X402Middleware` ONLY: the two FastAPI points that hold the route's response after the settle. Off by default (no call, no header). `FastAPIX402.require_payment` / `X402Depends` are dependencies that run before the route and never see its body: they do not take it, and neither do Flask, Django or Lambda (not wired yet)
- Contract of x402-rs `crates/x402-axum/src/durable.rs` and the TS `src/backend/durable.ts`: never a gate on the sale (too_large / no_payer_key / anchor_failed / not_capturable / timeout are skip notices, body/status/headers/background/settlement unchanged); `_with_durable_evidence` NEVER raises (the payment is settled by then: a hook failure is an `anchor_failed` notice, not a 500). No anchor and no header added (`wants_evidence`) without a confirmed settlement (no tx hash, receipt pending/unknown/rejected, `idempotent_replayed`), outside 2xx, on `HEAD`, or when the route already set `X-Durable-Evidence` (kept as is). Sealing is `dx402.anchor_evidence`, never a second implementation; the payer PUBLIC key (`payer_key`, required, checked in `__post_init__` with `max_body_bytes` / `timeout`), the proof and the anchor's HTTP client come from the config. No fixed `pointer`: one pointer for every sale names a blob that is not this response; the sealed blob goes inline, so the default limit is `ANCHOR_MAX_REQUEST_BYTES` (64 KiB)
- `_buffered` walks the route's stream from ONE task (`pump`, feeding a one-slot queue the capture reads with the deadline), as Starlette does without the hook: a contextvar set/reset or an anyio cancel scope across a `yield` must exit in the task that entered it (one task per `__anext__` cut the paid stream). A cancelled capture cancels that task. In the decorator, a return that is not a `Response` (with a receipt, or with durable) is built by `_route_response` with FastAPI's own steps for that route (`request.scope["route"]`: `response_model` + `response_model_*` via `serialize_response`, `response_class`, `status_code`, the status/headers of an injected `Response` parameter); never `JSONResponse(...)` of the raw value, which turned 201 into 200, leaked fields a `response_model` filters out and made a `PlainTextResponse` route JSON. The anchor seals those bytes. A `payer_key` result that is not bytes is `no_payer_key`; a route stream that fails partway is `not_capturable` (as TS)
- Capture (`_buffered`) mirrors x402-rs `buffer_body`: no `body` and no `body_iterator` (the decorator's own `FileResponse`) or `text/event-stream` is not read (`not_capturable`; behind `BaseHTTPMiddleware` a file arrives as a stream and is captured); chunks are `bytes(chunk)` for bytes/bytearray/memoryview and only `str` is encoded (`str(memoryview)` is its repr); announced `Content-Length` over the limit is not read; over the limit or past the deadline while read, or a stream that fails, streams the bytes already read ahead of the rest (`too_large` / `timeout` / `anchor_failed`, the failure raised again after them). One `timeout` (5 s) covers capture and anchor; the anchor runs in the executor and is not awaited past the deadline (a thread left behind sees the deadline gone and starts no anchor; a custom `client` keeps its own HTTP timeout). Gotcha: `BaseHTTPMiddleware.call_next` ends the stream WITHOUT an error when the route raises mid-stream (and raises later), so `X402Middleware` wraps the app in `_RecordsRouteFailure`, which marks the scope; without it a truncated body would be anchored as whole
- `tests/test_fastapi_durable.py` (offline: `Scripted` facilitator + `httpx.MockTransport` anchor)

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

## Publishing to PyPI (publish.yml)

Trusted publishing (OIDC), no token (owner's decision 127, 2026-09-29). The flow:

1. The release is merged to `main` with its version in `pyproject.toml`.
2. c0der runs `publish.yml` on `main` with that version: `gh workflow run publish.yml --ref main -f version=X.Y.Z`.
   `check` (only on `refs/heads/main`) fails if the input is not the `pyproject.toml` version, then runs
   `tests/interop` and the whole suite offline; `build` makes `dist/` without id-token and uploads it as an artifact.
3. `publish` (the only job with `id-token: write`) uploads `dist/` with `pypa/gh-action-pypi-publish` by OIDC, with
   no human approval: the `pypi` environment deploys from `main` only and has no required reviewer (removed
   2026-09-30, owner's decision 136).
4. `tag` (needs `publish`, so a failed or skipped upload skips it; the only job with `contents: write`) runs
   `scripts/release_tag.py`: the annotated tag `vX.Y.Z` on `GITHUB_SHA`, the commit the run built. c0der's sweep
   reads the highest `v*` tag as the published version, and a release left untagged read as missing (0.94.0,
   0.95.0 and 0.97.0 were tagged by hand after the fact; 0.98.0 and 0.99.0 had no tag when this job was added, and
   it only tags the releases it publishes). A hand tag is still needed when `publish` fails after the upload
   landed (a re-run hits PyPI's "file exists", so `tag` stays skipped) or `tag` keeps failing (e.g. a ruleset on
   `v*`: on 2026-10-12 the repository's only ruleset targets the branch `main`).

What guards a release is `main` only, `workflow_dispatch` only, OIDC with no token, and the road to `main`
(review, green CI).

`release_tag.py` refuses (exit 2, nothing pushed) a `--sha` that is not a full commit id in the checkout, a version
outside this SDK's release format (ASCII digits, optional pre/post/dev; no epoch, no local part), and a
`pyproject.toml` at that commit that does not declare exactly that version (one `version = "..."` line, as `check`
reads it). It asks `origin` for the tag with `git ls-remote`, PEELED (the `<ref>^{}` pattern is required: under
protocol 0 and 2, GitHub included, the peeled line only comes back when asked for; and ls-remote matches the TAIL
of a ref, so the reply is filtered by exact name). Already on this commit (lightweight,
annotated, nested) = exit 0, nothing written; on any other object = exit 1, never moved. Otherwise `git mktag` (no
local ref) + `git push` without force, and the remote is read back whatever the push answered: a tag pushed
meanwhile on this commit is still 0, on another commit still 1. If only `tag` fails, "Re-run failed jobs" retries
it alone. `tests/test_release_tag.py` runs it against a bare repository and a `--depth 1` clone (as the runner's
checkout), under protocol 0 and 2, and `TestGuardMutations` removes each guard from the source.

The tag triggers nothing: `publish.yml` has no `push`, tag or `release` trigger, and a tag pushed with
`GITHUB_TOKEN` starts no workflow. `tests/test_publish_workflow.py` fails on any `secrets` reference in the file,
another trigger, `id-token` outside the job that publishes, a publish job without `environment: pypi`, no
`refs/heads/main` gate in the chain; on a tag job that does not need the upload or has an `if` other than none or
`success()`; on `continue-on-error` anywhere in the chain it needs, or `skip-existing` on the upload (either lets a
release that uploaded nothing reach the tag); on a write permission other than `contents: write` there or in any
other job; and on anything in the tag job beyond its exact shape (`ubuntu-latest`, a checkout with only
`persist-credentials: true`, then exactly `python3 scripts/release_tag.py --version "$INPUT_VERSION"` with the
version in env). Every action is pinned by commit SHA (checked against its tag with `git ls-remote`). `ci.yml`
lists `scripts/release_tag.py` in its `paths` (the other `scripts/` are left out).

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
