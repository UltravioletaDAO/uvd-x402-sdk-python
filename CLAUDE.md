# CLAUDE.md - x402 Python SDK

Project guidance for Claude Code when working with this SDK.

## Overview

x402 Python SDK for backend payment verification and settlement. Used by servers to process x402 payments via the facilitator.

## Repository Structure

```
src/uvd_x402_sdk/
├── __init__.py              # Main exports
├── client.py                # X402Client - payment processing
├── config.py                # Configuration management
├── models.py                # Pydantic data models
├── exceptions.py            # Custom exceptions
├── response.py              # 402 response helpers
├── networks/
│   ├── __init__.py          # Network registry
│   ├── base.py              # NetworkConfig, TokenType, helpers
│   ├── evm.py               # 10 EVM networks
│   ├── solana.py            # Solana, Fogo
│   ├── near.py              # NEAR Protocol
│   └── stellar.py           # Stellar
└── integrations/
    ├── fastapi_integration.py
    ├── flask_integration.py
    ├── django_integration.py
    └── lambda_integration.py
```

## Multi-Stablecoin Support

### Supported Tokens
- USDC, EURC, AUSD, PYUSD, GHO, crvUSD

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
