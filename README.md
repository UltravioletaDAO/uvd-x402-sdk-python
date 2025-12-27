# uvd-x402-sdk

Python SDK for integrating **x402 cryptocurrency payments** via the Ultravioleta DAO facilitator.

Accept **gasless stablecoin payments** across **16 blockchain networks** with a single integration. The SDK handles signature verification, on-chain settlement, and all the complexity of multi-chain payments.

**New in v0.5.0+**: The SDK now includes embedded facilitator addresses - no manual configuration needed!

## Features

- **16 Networks**: EVM chains (Base, Ethereum, Polygon, etc.), SVM chains (Solana, Fogo), NEAR, Stellar, and Algorand
- **5 Stablecoins**: USDC, EURC, AUSD, PYUSD, USDT (EVM chains)
- **x402 v1 & v2**: Full support for both protocol versions with auto-detection
- **Framework Integrations**: Flask, FastAPI, Django, AWS Lambda
- **Gasless Payments**: Users sign EIP-712/EIP-3009 authorizations, facilitator pays all network fees
- **Simple API**: Decorators and middleware for quick integration
- **Type Safety**: Full Pydantic models and type hints
- **Extensible**: Register custom networks and tokens easily

## Quick Start (5 Lines)

```python
from decimal import Decimal
from uvd_x402_sdk import X402Client

client = X402Client(recipient_address="0xYourWallet...")
result = client.process_payment(request.headers["X-PAYMENT"], Decimal("10.00"))
print(f"Paid by {result.payer_address}, tx: {result.transaction_hash}")
```

## Supported Networks

| Network | Type | Chain ID | CAIP-2 | Status |
|---------|------|----------|--------|--------|
| Base | EVM | 8453 | `eip155:8453` | Active |
| Ethereum | EVM | 1 | `eip155:1` | Active |
| Polygon | EVM | 137 | `eip155:137` | Active |
| Arbitrum | EVM | 42161 | `eip155:42161` | Active |
| Optimism | EVM | 10 | `eip155:10` | Active |
| Avalanche | EVM | 43114 | `eip155:43114` | Active |
| Celo | EVM | 42220 | `eip155:42220` | Active |
| HyperEVM | EVM | 999 | `eip155:999` | Active |
| Unichain | EVM | 130 | `eip155:130` | Active |
| Monad | EVM | 143 | `eip155:143` | Active |
| Solana | SVM | - | `solana:5eykt...` | Active |
| Fogo | SVM | - | `solana:fogo` | Active |
| NEAR | NEAR | - | `near:mainnet` | Active |
| Stellar | Stellar | - | `stellar:pubnet` | Active |
| Algorand | Algorand | - | `algorand:mainnet` | Active |
| Algorand Testnet | Algorand | - | `algorand:testnet` | Active |

### Supported Tokens (EVM Chains)

| Token | Networks | Decimals |
|-------|----------|----------|
| USDC | All EVM chains | 6 |
| EURC | Ethereum, Base, Avalanche | 6 |
| AUSD | Ethereum, Arbitrum, Avalanche, Polygon, Monad | 6 |
| PYUSD | Ethereum | 6 |
| USDT | Ethereum, Arbitrum, Optimism, Avalanche, Polygon | 6 |

## Installation

```bash
# Core SDK (minimal dependencies)
pip install uvd-x402-sdk

# With framework support
pip install uvd-x402-sdk[flask]      # Flask integration
pip install uvd-x402-sdk[fastapi]    # FastAPI/Starlette integration
pip install uvd-x402-sdk[django]     # Django integration
pip install uvd-x402-sdk[aws]        # AWS Lambda helpers
pip install uvd-x402-sdk[algorand]   # Algorand atomic group helpers

# All integrations
pip install uvd-x402-sdk[all]
```

---

## Framework Examples

### Flask

```python
from decimal import Decimal
from flask import Flask, g, jsonify
from uvd_x402_sdk.integrations import FlaskX402

app = Flask(__name__)
x402 = FlaskX402(
    app,
    recipient_address="0xYourEVMWallet...",
    recipient_solana="YourSolanaAddress...",
    recipient_near="your-account.near",
    recipient_stellar="G...YourStellarAddress",
)

@app.route("/api/premium")
@x402.require_payment(amount_usd=Decimal("5.00"))
def premium():
    return jsonify({
        "message": "Premium content!",
        "payer": g.payment_result.payer_address,
        "tx": g.payment_result.transaction_hash,
        "network": g.payment_result.network,
    })

@app.route("/api/basic")
@x402.require_payment(amount_usd=Decimal("0.10"))
def basic():
    return jsonify({"data": "Basic tier data"})

if __name__ == "__main__":
    app.run(debug=True)
```

### FastAPI

```python
from decimal import Decimal
from fastapi import FastAPI, Depends
from uvd_x402_sdk.config import X402Config
from uvd_x402_sdk.models import PaymentResult
from uvd_x402_sdk.integrations import FastAPIX402

app = FastAPI()
x402 = FastAPIX402(
    app,
    recipient_address="0xYourEVMWallet...",
    recipient_solana="YourSolanaAddress...",
    recipient_near="your-account.near",
    recipient_stellar="G...YourStellarAddress",
)

@app.get("/api/premium")
async def premium(
    payment: PaymentResult = Depends(x402.require_payment(amount_usd="5.00"))
):
    return {
        "message": "Premium content!",
        "payer": payment.payer_address,
        "network": payment.network,
    }

@app.post("/api/generate")
async def generate(
    body: dict,
    payment: PaymentResult = Depends(x402.require_payment(amount_usd="1.00"))
):
    # Dynamic processing based on request
    return {"result": "generated", "payer": payment.payer_address}
```

### Django

```python
# settings.py
X402_FACILITATOR_URL = "https://facilitator.ultravioletadao.xyz"
X402_RECIPIENT_EVM = "0xYourEVMWallet..."
X402_RECIPIENT_SOLANA = "YourSolanaAddress..."
X402_RECIPIENT_NEAR = "your-account.near"
X402_RECIPIENT_STELLAR = "G...YourStellarAddress"
X402_PROTECTED_PATHS = {
    "/api/premium/": "5.00",
    "/api/basic/": "1.00",
}

MIDDLEWARE = [
    # ...other middleware...
    "uvd_x402_sdk.integrations.django_integration.DjangoX402Middleware",
]

# views.py
from django.http import JsonResponse
from uvd_x402_sdk.integrations import django_x402_required

@django_x402_required(amount_usd="5.00")
def premium_view(request):
    payment = request.payment_result
    return JsonResponse({
        "message": "Premium content!",
        "payer": payment.payer_address,
    })
```

### AWS Lambda

```python
import json
from decimal import Decimal
from uvd_x402_sdk.config import X402Config
from uvd_x402_sdk.integrations import LambdaX402

config = X402Config(
    recipient_evm="0xYourEVMWallet...",
    recipient_solana="YourSolanaAddress...",
    recipient_near="your-account.near",
    recipient_stellar="G...YourStellarAddress",
)
x402 = LambdaX402(config=config)

def handler(event, context):
    # Calculate price based on request
    body = json.loads(event.get("body", "{}"))
    quantity = body.get("quantity", 1)
    price = Decimal(str(quantity * 0.01))

    # Process payment or return 402
    result = x402.process_or_require(event, price)

    # If 402 response, return it
    if isinstance(result, dict) and "statusCode" in result:
        return result

    # Payment verified!
    return {
        "statusCode": 200,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps({
            "success": True,
            "payer": result.payer_address,
            "tx": result.transaction_hash,
            "network": result.network,
            "quantity": quantity,
        })
    }
```

---

## Network-Specific Examples

### EVM Chains (Base, Ethereum, Polygon, etc.)

EVM chains use ERC-3009 `TransferWithAuthorization` with EIP-712 signatures.

```python
from uvd_x402_sdk import X402Client, X402Config

# Accept payments on Base and Ethereum only
config = X402Config(
    recipient_evm="0xYourEVMWallet...",
    supported_networks=["base", "ethereum"],
)

client = X402Client(config=config)
result = client.process_payment(x_payment_header, Decimal("10.00"))

# The payload contains EIP-712 signature + authorization
payload = client.extract_payload(x_payment_header)
evm_data = payload.get_evm_payload()
print(f"From: {evm_data.authorization.from_address}")
print(f"To: {evm_data.authorization.to}")
print(f"Value: {evm_data.authorization.value}")
```

### Solana & Fogo (SVM Chains)

SVM chains use partially-signed VersionedTransactions with SPL token transfers.

```python
from uvd_x402_sdk import X402Client, X402Config

# Accept payments on Solana and Fogo
config = X402Config(
    recipient_solana="YourSolanaAddress...",
    supported_networks=["solana", "fogo"],
)

client = X402Client(config=config)
result = client.process_payment(x_payment_header, Decimal("5.00"))

# The payload contains a base64-encoded VersionedTransaction
payload = client.extract_payload(x_payment_header)
svm_data = payload.get_svm_payload()
print(f"Transaction: {svm_data.transaction[:50]}...")

# Fogo has ultra-fast finality (~400ms)
if result.network == "fogo":
    print("Payment confirmed in ~400ms!")
```

### Stellar

Stellar uses Soroban Authorization Entries with fee-bump transactions.

```python
from uvd_x402_sdk import X402Client, X402Config

config = X402Config(
    recipient_stellar="G...YourStellarAddress",
    supported_networks=["stellar"],
)

client = X402Client(config=config)
result = client.process_payment(x_payment_header, Decimal("1.00"))

# Stellar uses 7 decimals (stroops)
payload = client.extract_payload(x_payment_header)
stellar_data = payload.get_stellar_payload()
print(f"From: {stellar_data.from_address}")
print(f"Amount (stroops): {stellar_data.amount}")
print(f"Token Contract: {stellar_data.tokenContract}")
```

### NEAR Protocol

NEAR uses NEP-366 meta-transactions with Borsh serialization.

```python
from uvd_x402_sdk import X402Client, X402Config

config = X402Config(
    recipient_near="your-recipient.near",
    supported_networks=["near"],
)

client = X402Client(config=config)
result = client.process_payment(x_payment_header, Decimal("2.00"))

# NEAR payload contains a SignedDelegateAction
payload = client.extract_payload(x_payment_header)
near_data = payload.get_near_payload()
print(f"SignedDelegateAction: {near_data.signedDelegateAction[:50]}...")

# Validate NEAR payload structure
from uvd_x402_sdk.networks.near import validate_near_payload
validate_near_payload(payload.payload)  # Raises ValueError if invalid
```

### Algorand

Algorand uses atomic groups with ASA (Algorand Standard Assets) transfers.

```python
from uvd_x402_sdk import X402Client, X402Config

config = X402Config(
    recipient_algorand="NCDSNUQ2QLXDMJXRALAW4CRUSSKG4IS37MVOFDQQPC45SE4EBZO42U6ZX4",
    supported_networks=["algorand"],
)

client = X402Client(config=config)
result = client.process_payment(x_payment_header, Decimal("1.00"))

# Algorand uses atomic groups: [fee_tx, payment_tx]
from uvd_x402_sdk.networks.algorand import (
    validate_algorand_payload,
    get_algorand_fee_payer,
    build_atomic_group,
)

# Get the facilitator fee payer address
fee_payer = get_algorand_fee_payer("algorand")
print(f"Fee payer: {fee_payer}")  # KIMS5H6Q...

# Validate payload structure
payload = client.extract_payload(x_payment_header)
validate_algorand_payload(payload.payload)  # Raises ValueError if invalid
```

#### Building Algorand Payments (requires `pip install uvd-x402-sdk[algorand]`)

```python
from uvd_x402_sdk.networks.algorand import (
    build_atomic_group,
    build_x402_payment_request,
    get_algorand_fee_payer,
)
from algosdk.v2client import algod

# Connect to Algorand node
client = algod.AlgodClient("", "https://mainnet-api.algonode.cloud")

# Build atomic group
payload = build_atomic_group(
    sender_address="YOUR_ADDRESS...",
    recipient_address="MERCHANT_ADDRESS...",
    amount=1000000,  # 1 USDC (6 decimals)
    asset_id=31566704,  # USDC ASA ID on mainnet
    facilitator_address=get_algorand_fee_payer("algorand"),
    sign_transaction=lambda txn: txn.sign(private_key),
    algod_client=client,
)

# Build x402 payment request
request = build_x402_payment_request(payload, network="algorand")
```

---

## x402 v1 vs v2

The SDK supports both x402 protocol versions with automatic detection.

### Version Differences

| Aspect | v1 | v2 |
|--------|----|----|
| Network ID | String (`"base"`) | CAIP-2 (`"eip155:8453"`) |
| Payment delivery | JSON body | `PAYMENT-REQUIRED` header |
| Multiple options | Limited | `accepts` array |
| Discovery | Implicit | Optional extension |

### Auto-Detection

```python
from uvd_x402_sdk import PaymentPayload

# The SDK auto-detects based on network format
payload = PaymentPayload(
    x402Version=1,
    scheme="exact",
    network="base",  # v1 format
    payload={"signature": "...", "authorization": {...}}
)

print(payload.is_v2())  # False

payload_v2 = PaymentPayload(
    x402Version=2,
    scheme="exact",
    network="eip155:8453",  # v2 CAIP-2 format
    payload={"signature": "...", "authorization": {...}}
)

print(payload_v2.is_v2())  # True

# Both work the same way
print(payload.get_normalized_network())  # "base"
print(payload_v2.get_normalized_network())  # "base"
```

### Creating v2 Responses

```python
from uvd_x402_sdk import X402Config, create_402_response_v2, Payment402BuilderV2

config = X402Config(
    recipient_evm="0xYourEVM...",
    recipient_solana="YourSolana...",
    recipient_near="your.near",
    recipient_stellar="G...Stellar",
)

# Simple v2 response
response = create_402_response_v2(
    amount_usd=Decimal("5.00"),
    config=config,
    resource="/api/premium",
    description="Premium API access",
)
# Returns:
# {
#     "x402Version": 2,
#     "scheme": "exact",
#     "resource": "/api/premium",
#     "accepts": [
#         {"network": "eip155:8453", "asset": "0x833...", "amount": "5000000", "payTo": "0xYour..."},
#         {"network": "solana:5eykt...", "asset": "EPjF...", "amount": "5000000", "payTo": "Your..."},
#         {"network": "near:mainnet", "asset": "1720...", "amount": "5000000", "payTo": "your.near"},
#         ...
#     ]
# }

# Builder pattern for more control
response = (
    Payment402BuilderV2(config)
    .amount(Decimal("10.00"))
    .resource("/api/generate")
    .description("AI generation credits")
    .networks(["base", "solana", "near"])  # Limit to specific networks
    .build()
)
```

---

## Payload Validation

Each network type has specific payload validation:

### EVM Validation

```python
from uvd_x402_sdk.models import EVMPayloadContent, EVMAuthorization

# Parse and validate EVM payload
payload = client.extract_payload(x_payment_header)
evm_data = payload.get_evm_payload()

# Validate authorization fields
auth = evm_data.authorization
assert auth.from_address.startswith("0x")
assert auth.to.startswith("0x")
assert int(auth.value) > 0
assert int(auth.validBefore) > int(auth.validAfter)
```

### SVM Validation

```python
from uvd_x402_sdk.networks.solana import validate_svm_payload, is_valid_solana_address

# Validate SVM payload
payload = client.extract_payload(x_payment_header)
validate_svm_payload(payload.payload)  # Raises ValueError if invalid

# Validate Solana addresses
assert is_valid_solana_address("YourSolanaAddress...")
```

### NEAR Validation

```python
from uvd_x402_sdk.networks.near import (
    validate_near_payload,
    is_valid_near_account_id,
    BorshSerializer,
)

# Validate NEAR payload
payload = client.extract_payload(x_payment_header)
validate_near_payload(payload.payload)  # Raises ValueError if invalid

# Validate NEAR account IDs
assert is_valid_near_account_id("your-account.near")
assert is_valid_near_account_id("0xultravioleta.near")
```

### Stellar Validation

```python
from uvd_x402_sdk.networks.stellar import (
    is_valid_stellar_address,
    is_valid_contract_address,
    stroops_to_usd,
)

# Validate Stellar addresses
assert is_valid_stellar_address("G...YourStellarAddress")  # G...
assert is_valid_contract_address("C...USDCContract")  # C...

# Convert stroops to USD (7 decimals)
usd = stroops_to_usd(50000000)  # Returns 5.0
```

---

## Configuration

### Environment Variables

```bash
# Core configuration
X402_FACILITATOR_URL=https://facilitator.ultravioletadao.xyz
X402_VERIFY_TIMEOUT=30
X402_SETTLE_TIMEOUT=55

# Recipient addresses (at least one required)
X402_RECIPIENT_EVM=0xYourEVMWallet
X402_RECIPIENT_SOLANA=YourSolanaAddress
X402_RECIPIENT_NEAR=your-account.near
X402_RECIPIENT_STELLAR=G...YourStellarAddress

# Optional
X402_FACILITATOR_SOLANA=F742C4VfFLQ9zRQyithoj5229ZgtX2WqKCSFKgH2EThq
X402_RESOURCE_URL=https://api.example.com
X402_DESCRIPTION=API access payment
```

### Programmatic Configuration

```python
from uvd_x402_sdk import X402Config, MultiPaymentConfig

# Full configuration
config = X402Config(
    facilitator_url="https://facilitator.ultravioletadao.xyz",

    # Recipients
    recipient_evm="0xYourEVMWallet",
    recipient_solana="YourSolanaAddress",
    recipient_near="your-account.near",
    recipient_stellar="G...YourStellarAddress",

    # Timeouts
    verify_timeout=30.0,
    settle_timeout=55.0,

    # Limit to specific networks
    supported_networks=["base", "solana", "near", "stellar"],

    # Metadata
    resource_url="https://api.example.com/premium",
    description="Premium API access",

    # Protocol version (1, 2, or "auto")
    x402_version="auto",
)

# From environment
config = X402Config.from_env()
```

---

## Facilitator Addresses

The SDK includes all facilitator addresses as embedded constants. You don't need to configure them manually.

### Fee Payer Addresses (Non-EVM)

Non-EVM chains require a fee payer address for gasless transactions:

```python
from uvd_x402_sdk import (
    # Algorand
    ALGORAND_FEE_PAYER_MAINNET,  # KIMS5H6QLCUDL65L5UBTOXDPWLMTS7N3AAC3I6B2NCONEI5QIVK7LH2C2I
    ALGORAND_FEE_PAYER_TESTNET,  # 5DPPDQNYUPCTXRZWRYSF3WPYU6RKAUR25F3YG4EKXQRHV5AUAI62H5GXL4

    # Solana
    SOLANA_FEE_PAYER_MAINNET,    # F742C4VfFLQ9zRQyithoj5229ZgtX2WqKCSFKgH2EThq
    SOLANA_FEE_PAYER_DEVNET,     # 6xNPewUdKRbEZDReQdpyfNUdgNg8QRc8Mt263T5GZSRv

    # Fogo
    FOGO_FEE_PAYER_MAINNET,      # F742C4VfFLQ9zRQyithoj5229ZgtX2WqKCSFKgH2EThq
    FOGO_FEE_PAYER_TESTNET,      # 6xNPewUdKRbEZDReQdpyfNUdgNg8QRc8Mt263T5GZSRv

    # NEAR
    NEAR_FEE_PAYER_MAINNET,      # uvd-facilitator.near
    NEAR_FEE_PAYER_TESTNET,      # uvd-facilitator.testnet

    # Stellar
    STELLAR_FEE_PAYER_MAINNET,   # GCHPGXJT2WFFRFCA5TV4G4E3PMMXLNIDUH27PKDYA4QJ2XGYZWGFZNHB
    STELLAR_FEE_PAYER_TESTNET,   # GBBFZMLUJEZVI32EN4XA2KPP445XIBTMTRBLYWFIL556RDTHS2OWFQ2Z

    # Helper function
    get_fee_payer,               # Get fee payer for any network
)

# Get fee payer for any network
fee_payer = get_fee_payer("algorand")  # Returns KIMS5H6Q...
fee_payer = get_fee_payer("solana")    # Returns F742C4VfF...
fee_payer = get_fee_payer("base")      # Returns None (EVM doesn't need fee payer)
```

### EVM Facilitator Addresses

EVM chains use EIP-3009 transferWithAuthorization (gasless by design), but the facilitator wallet addresses are available for reference:

```python
from uvd_x402_sdk import (
    EVM_FACILITATOR_MAINNET,  # 0x103040545AC5031A11E8C03dd11324C7333a13C7
    EVM_FACILITATOR_TESTNET,  # 0x34033041a5944B8F10f8E4D8496Bfb84f1A293A8
)
```

### Helper Functions

```python
from uvd_x402_sdk import (
    get_fee_payer,           # Get fee payer address for a network
    requires_fee_payer,      # Check if network needs fee payer
    get_all_fee_payers,      # Get all registered fee payers
    build_payment_info,      # Build payment info with auto feePayer
    DEFAULT_FACILITATOR_URL, # https://facilitator.ultravioletadao.xyz
)

# Check if network needs fee payer
requires_fee_payer("algorand")  # True
requires_fee_payer("base")      # False

# Build payment info with automatic fee payer
info = build_payment_info(
    network="algorand",
    pay_to="MERCHANT_ADDRESS...",
    max_amount_required="1000000",
    description="API access"
)
# info = {
#     'network': 'algorand',
#     'payTo': 'MERCHANT_ADDRESS...',
#     'maxAmountRequired': '1000000',
#     'description': 'API access',
#     'asset': '31566704',
#     'extra': {
#         'token': 'usdc',
#         'feePayer': 'KIMS5H6QLCUDL65L5UBTOXDPWLMTS7N3AAC3I6B2NCONEI5QIVK7LH2C2I'
#     }
# }
```

---

## Registering Custom Networks

```python
from uvd_x402_sdk.networks import NetworkConfig, NetworkType, register_network

# Register a custom EVM network
custom_chain = NetworkConfig(
    name="mychain",
    display_name="My Custom Chain",
    network_type=NetworkType.EVM,
    chain_id=12345,
    usdc_address="0xUSDCContractAddress",
    usdc_decimals=6,
    usdc_domain_name="USD Coin",  # Check actual EIP-712 domain!
    usdc_domain_version="2",
    rpc_url="https://rpc.mychain.com",
    enabled=True,
)

register_network(custom_chain)

# Now you can use it
config = X402Config(
    recipient_evm="0xYourWallet...",
    supported_networks=["base", "mychain"],
)
```

---

## Multi-Token Support

The SDK supports 6 stablecoins on EVM chains. Use the token helper functions to query and work with different tokens.

### Querying Token Support

```python
from uvd_x402_sdk import (
    TokenType,
    get_token_config,
    get_supported_tokens,
    is_token_supported,
    get_networks_by_token,
)

# Check which tokens a network supports
tokens = get_supported_tokens("ethereum")
print(tokens)  # ['usdc', 'eurc', 'ausd', 'pyusd']

tokens = get_supported_tokens("base")
print(tokens)  # ['usdc', 'eurc']

# Check if a specific token is supported
if is_token_supported("ethereum", "eurc"):
    print("EURC is available on Ethereum!")

# Get token configuration
config = get_token_config("ethereum", "eurc")
if config:
    print(f"EURC address: {config.address}")
    print(f"Decimals: {config.decimals}")
    print(f"EIP-712 name: {config.name}")
    print(f"EIP-712 version: {config.version}")

# Find all networks that support a token
networks = get_networks_by_token("eurc")
for network in networks:
    print(f"EURC available on: {network.display_name}")
# Output: EURC available on: Ethereum, Base, Avalanche C-Chain
```

### Token Configuration

Each token has specific EIP-712 domain parameters required for signing:

```python
from uvd_x402_sdk import TokenConfig, get_token_config

# TokenConfig structure
# - address: Contract address
# - decimals: Token decimals (6 for all supported stablecoins)
# - name: EIP-712 domain name (e.g., "USD Coin", "EURC", "Gho Token")
# - version: EIP-712 domain version

# Example: Get EURC config on Base
eurc = get_token_config("base", "eurc")
# TokenConfig(
#     address="0x60a3E35Cc302bFA44Cb288Bc5a4F316Fdb1adb42",
#     decimals=6,
#     name="EURC",
#     version="2"
# )

# Example: Get PYUSD config on Ethereum
pyusd = get_token_config("ethereum", "pyusd")
# TokenConfig(
#     address="0x6c3ea9036406852006290770BEdFcAbA0e23A0e8",
#     decimals=6,
#     name="PayPal USD",
#     version="1"
# )
```

### Available Tokens

| Token | Description | Decimals | Issuer |
|-------|-------------|----------|--------|
| `usdc` | USD Coin | 6 | Circle |
| `eurc` | Euro Coin | 6 | Circle |
| `ausd` | Agora USD | 6 | Agora Finance |
| `pyusd` | PayPal USD | 6 | PayPal/Paxos |

### Critical Implementation Notes

#### EIP-712 Domain Names Vary by Chain

The same token may use **different EIP-712 domain names on different chains**. This affects signature verification.

| Token | Ethereum | Base | Avalanche |
|-------|----------|------|-----------|
| EURC | `"Euro Coin"` | `"EURC"` | `"Euro Coin"` |
| USDC | `"USD Coin"` | `"USD Coin"` | `"USD Coin"` |
| AUSD | `"Agora Dollar"` | N/A | `"Agora Dollar"` |
| PYUSD | `"PayPal USD"` | N/A | N/A |

**Important:** Always use `get_token_config()` to get the correct domain name. Never hardcode domain names.

```python
# CORRECT: Use get_token_config for each chain
eurc_base = get_token_config("base", "eurc")
# TokenConfig(name="EURC", version="2", ...)

eurc_ethereum = get_token_config("ethereum", "eurc")
# TokenConfig(name="Euro Coin", version="2", ...)
```

#### PYUSD Signature Format (PayPal USD)

PYUSD uses the Paxos implementation which only supports the **v,r,s signature variant** of `transferWithAuthorization`. This is different from Circle's USDC/EURC which support both compact bytes and v,r,s variants.

**Backend implications:**
- The x402 facilitator (v1.9.0+) automatically handles this by detecting PYUSD and using `transferWithAuthorization_1(v,r,s)` instead of `transferWithAuthorization_0(bytes signature)`
- If using a custom facilitator, ensure it supports the v,r,s variant for PYUSD

#### Token Info Must Be Passed to Facilitator

When using non-USDC tokens, your backend **must** pass the token info (including EIP-712 domain) to the facilitator. This is done via the `extra` field in `paymentRequirements`:

```python
# When building payment requirements for the facilitator
payment_requirements = {
    "asset": token_address,  # Use actual token address, NOT hardcoded USDC
    "extra": {
        "name": token_config.name,     # EIP-712 domain name
        "version": token_config.version,  # EIP-712 domain version
    }
}
```

Without this, the facilitator will use wrong EIP-712 domain and signature verification will fail with "invalid signature" error.

---

## Error Handling

```python
from uvd_x402_sdk.exceptions import (
    X402Error,
    PaymentRequiredError,
    PaymentVerificationError,
    PaymentSettlementError,
    UnsupportedNetworkError,
    InvalidPayloadError,
    FacilitatorError,
    X402TimeoutError,
)

try:
    result = client.process_payment(header, amount)
except PaymentVerificationError as e:
    # Signature invalid, amount mismatch, expired, etc.
    print(f"Verification failed: {e.reason}")
    print(f"Errors: {e.errors}")
except PaymentSettlementError as e:
    # On-chain settlement failed (insufficient balance, nonce used, etc.)
    print(f"Settlement failed on {e.network}: {e.message}")
except UnsupportedNetworkError as e:
    # Network not recognized or disabled
    print(f"Network {e.network} not supported")
    print(f"Supported: {e.supported_networks}")
except InvalidPayloadError as e:
    # Malformed X-PAYMENT header
    print(f"Invalid payload: {e.message}")
except FacilitatorError as e:
    # Facilitator returned error
    print(f"Facilitator error: {e.status_code} - {e.response_body}")
except X402TimeoutError as e:
    # Request timed out
    print(f"{e.operation} timed out after {e.timeout_seconds}s")
except X402Error as e:
    # Catch-all for x402 errors
    print(f"Payment error: {e.message}")
```

---

## How x402 Works

The x402 protocol enables gasless stablecoin payments (USDC, EURC, AUSD, PYUSD):

```
1. User Request     -->  Client sends request without payment
2. 402 Response     <--  Server returns payment requirements
3. User Signs       -->  Wallet signs authorization (NO GAS!)
4. Frontend Sends   -->  X-PAYMENT header with signed payload
5. SDK Verifies     -->  Validates signature with facilitator
6. SDK Settles      -->  Facilitator executes on-chain transfer
7. Success          <--  Payment confirmed, request processed
```

The facilitator (https://facilitator.ultravioletadao.xyz) handles all on-chain interactions and pays gas fees on behalf of users.

### Payment Flow by Network Type

| Network Type | User Signs | Facilitator Does |
|--------------|-----------|------------------|
| EVM | EIP-712 message | Calls `transferWithAuthorization()` |
| SVM | Partial transaction | Co-signs + submits transaction |
| NEAR | DelegateAction (Borsh) | Wraps in `Action::Delegate` |
| Stellar | Auth entry (XDR) | Wraps in fee-bump transaction |
| Algorand | ASA transfer tx | Signs fee tx + submits atomic group |

---

## Error Codes

| Exception | Description |
|-----------|-------------|
| `PaymentRequiredError` | No payment header provided |
| `PaymentVerificationError` | Signature invalid, amount mismatch, expired |
| `PaymentSettlementError` | On-chain settlement failed |
| `UnsupportedNetworkError` | Network not recognized or disabled |
| `InvalidPayloadError` | Malformed X-PAYMENT header |
| `FacilitatorError` | Facilitator service error |
| `ConfigurationError` | Invalid SDK configuration |
| `X402TimeoutError` | Request timed out |

---

## Security

- Users **NEVER** pay gas or submit transactions directly
- **EVM**: Users sign EIP-712 structured messages for any supported stablecoin (USDC, EURC, AUSD, PYUSD)
- **Solana/Fogo**: Users sign partial transactions (facilitator co-signs and submits)
- **Stellar**: Users sign Soroban authorization entries only
- **NEAR**: Users sign NEP-366 meta-transactions (DelegateAction)
- The facilitator submits and pays for all on-chain transactions
- All signatures include expiration timestamps (`validBefore`) for replay protection
- Nonces prevent double-spending of authorizations
- Each token has verified contract addresses and EIP-712 domain parameters

---

## Troubleshooting

### Common Issues

**"Unsupported network"**
- Check that the network is in `supported_networks`
- Verify the network is enabled
- For v2, ensure CAIP-2 format is correct

**"Payment verification failed"**
- Amount mismatch between expected and signed
- Recipient address mismatch
- Authorization expired (`validBefore` in the past)
- Nonce already used (replay attack protection)

**"Settlement timed out"**
- Network congestion - increase `settle_timeout`
- Facilitator under load - retry after delay

**"Invalid payload"**
- Check base64 encoding of X-PAYMENT header
- Verify JSON structure matches expected format
- Ensure `x402Version` is 1 or 2

### Debug Logging

```python
import logging
logging.basicConfig(level=logging.DEBUG)
logging.getLogger("uvd_x402_sdk").setLevel(logging.DEBUG)
```

---

## Development

```bash
# Clone and install
git clone https://github.com/UltravioletaDAO/uvd-x402-sdk-python
cd uvd-x402-sdk-python
pip install -e ".[dev]"

# Run tests
pytest

# Format code
black src tests
ruff check src tests

# Type checking
mypy src
```

---

## Links

- [x402 Protocol](https://x402.org)
- [Ultravioleta DAO](https://ultravioletadao.xyz)
- [402milly](https://402milly.xyz)
- [GitHub](https://github.com/UltravioletaDAO/uvd-x402-sdk-python)
- [PyPI](https://pypi.org/project/uvd-x402-sdk/)
- [TypeScript SDK](https://github.com/UltravioletaDAO/uvd-x402-sdk-typescript)

---

## License

MIT License - see LICENSE file.

---

## Changelog

### v0.5.2 (2025-12-26)

- Added EVM facilitator addresses for reference
  - `EVM_FACILITATOR_MAINNET`: 0x103040545AC5031A11E8C03dd11324C7333a13C7
  - `EVM_FACILITATOR_TESTNET`: 0x34033041a5944B8F10f8E4D8496Bfb84f1A293A8

### v0.5.1 (2025-12-26)

- Changed default Algorand mainnet network name from `algorand-mainnet` to `algorand`
- Aligns with facilitator v1.9.5+ which now uses `algorand` as the primary network identifier

### v0.5.0 (2025-12-26)

- **Facilitator Module**: Added `facilitator.py` with all fee payer addresses embedded as constants
- SDK users no longer need to configure facilitator addresses manually
- Added constants: `ALGORAND_FEE_PAYER_MAINNET`, `SOLANA_FEE_PAYER_MAINNET`, `NEAR_FEE_PAYER_MAINNET`, `STELLAR_FEE_PAYER_MAINNET`, etc.
- Added helper functions: `get_fee_payer()`, `requires_fee_payer()`, `build_payment_info()`
- Network-specific helpers: `get_algorand_fee_payer()`, `get_svm_fee_payer()`, `get_near_fee_payer()`, `get_stellar_fee_payer()`

### v0.4.2 (2025-12-26)

- **Algorand Atomic Group Fix**: Rewrote Algorand payload format to use GoPlausible x402-avm atomic group spec
- New `AlgorandPaymentPayload` dataclass with `paymentIndex` and `paymentGroup` fields
- Added `build_atomic_group()` helper for constructing two-transaction atomic groups
- Added `validate_algorand_payload()` for payload validation
- Added `build_x402_payment_request()` for building complete x402 requests

### v0.4.1 (2025-12-26)

- Added AUSD (Agora USD) support on Solana using Token2022 program
- Added `TOKEN_2022_PROGRAM_ID` constant
- Added `get_token_program_id()` and `is_token_2022()` helpers

### v0.4.0 (2025-12-26)

- **Algorand Support**: Added Algorand mainnet and testnet networks
- Added `ALGORAND` NetworkType
- Added `algorand` optional dependency (`py-algorand-sdk>=2.0.0`)
- SDK now supports 16 blockchain networks

### v0.3.4 (2025-12-22)

- Added USDT support (USDT0 omnichain via LayerZero) on Ethereum, Arbitrum, Optimism, Avalanche, Polygon
- SDK now supports 5 stablecoins: USDC, EURC, AUSD, PYUSD, USDT

### v0.3.3 (2025-12-22)

- Fixed EIP-712 domain names: AUSD uses "Agora Dollar" (not "Agora USD")
- Fixed EURC domain name on Ethereum/Avalanche: "Euro Coin" (not "EURC")

### v0.3.2 (2025-12-21)

- Added critical implementation notes for multi-token support:
  - EIP-712 domain names vary by chain (e.g., EURC is "Euro Coin" on Ethereum but "EURC" on Base)
  - PYUSD uses v,r,s signature variant (Paxos implementation)
  - Token info must be passed to facilitator via `extra` field

### v0.3.1 (2025-12-21)

- Removed GHO and crvUSD token support (not EIP-3009 compatible)
- SDK now supports 4 stablecoins: USDC, EURC, AUSD, PYUSD

### v0.3.0 (2025-12-20)

- **Multi-Stablecoin Support**: Added support for 4 stablecoins on EVM chains
  - USDC (all EVM chains)
  - EURC (Ethereum, Base, Avalanche)
  - AUSD (Ethereum, Arbitrum, Avalanche, Polygon, Monad)
  - PYUSD (Ethereum)
- Added `TokenType` literal type and `TokenConfig` dataclass
- Added token helper functions: `get_token_config()`, `get_supported_tokens()`, `is_token_supported()`, `get_networks_by_token()`
- Added `tokens` field to `NetworkConfig` for multi-token configurations
- Updated EVM network configurations with token contract addresses and EIP-712 domain parameters

### v0.2.2 (2025-12-16)

- Added Security section to documentation
- Added Error Codes table
- Updated links to new GitHub repository
- Synced documentation with TypeScript SDK

### v0.2.1 (2025-12-16)

- Removed BSC network (doesn't support ERC-3009)
- Added GitHub Actions workflow for PyPI publishing
- Updated to 14 supported networks

### v0.2.0 (2025-12-15)

- Added **NEAR Protocol** support with NEP-366 meta-transactions
- Added **Fogo** SVM chain support
- Added **x402 v2** protocol support with CAIP-2 network identifiers
- Added `accepts` array for multi-network payment options
- Refactored Solana to generic SVM type (supports Solana, Fogo, future SVM chains)
- Added CAIP-2 parsing utilities (`parse_caip2_network`, `to_caip2_network`)
- Added `MultiPaymentConfig` for multi-network recipient configuration
- Added `Payment402BuilderV2` for v2 response construction

### v0.1.0 (2025-12-01)

- Initial release
- EVM, Solana, Stellar network support
- Flask, FastAPI, Django, Lambda integrations
- Full Pydantic models
