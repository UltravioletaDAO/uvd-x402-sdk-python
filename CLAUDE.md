# CLAUDE.md - x402 Python SDK

Project guidance for Claude Code when working with this SDK.

## Overview

x402 Python SDK for backend payment verification and settlement. Used by servers to process x402 payments via the facilitator.

## Repository Structure

```
src/uvd_x402_sdk/
├── __init__.py              # Main exports
├── client.py                # X402Client - payment processing, negotiate_accepts(), facilitator info, connect_with_private_key()
├── config.py                # Configuration management
├── models.py                # Pydantic data models (PaymentPayload, SettlementAccountPayload, etc.)
├── exceptions.py            # Custom exceptions
├── response.py              # 402 response helpers
├── discovery.py             # BazaarClient - resource registration and discovery
├── erc8004.py               # ERC-8004 Trustless Agents (EVM + Solana)
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
- Supports 18 networks: 16 EVM + Solana + Solana-devnet
- `AgentId = Union[int, str]` - EVM uses int, Solana uses base58 pubkey string
- `seal_hash` parameter on `revoke_feedback()` and `append_response()` (SEAL v1)
- Solana uses QuantuLabs 8004-solana Anchor program + ATOM Engine

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

1. **X402Client.process_payment()** - Doesn't accept `token_type` parameter yet
2. **Response builder** - Hardcodes `token="USDC"` in 402 response
3. **SVM/Stellar/NEAR** - Only USDC supported

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
