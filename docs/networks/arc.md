# Arc mainnet and testnet

Direct USDC `exact` payments are supported by the Ultravioleta facilitator in both x402 v1 and v2. This release adds the network definitions to the SDK's normal signing and payment paths.

| Setting | Mainnet | Testnet |
|---|---|---|
| SDK / v1 name | `arc` | `arc-testnet` |
| Chain ID | `5042` | `5042002` |
| v2 network | `eip155:5042` | `eip155:5042002` |
| RPC | `https://rpc.mainnet.arc.io` | `https://rpc.testnet.arc.io` |
| Explorer | `https://explorer.arc.io` | `https://explorer.testnet.arc.io` |

Both networks use USDC `0x3600000000000000000000000000000000000000` with EIP-712 domain `name: "USDC", version: "2"`. Payment amounts have **6 decimals**: `0.01 USDC` is `10000` atomic units. Native gas has 18 decimals on the same balance; the two readings must not be added. The facilitator pays gas. A payer needs USDC on the selected network.

The chain ID is part of the signature domain. An authorization signed on mainnet cannot be reused on testnet. The SDK preserves their distinct registry entries and CAIP-2 identifiers.

The facilitator URL is `https://facilitator.ultravioletadao.xyz`. Check `/supported` at runtime when using another facilitator. EURC/USYC, Gateway, contract-wallet signatures/EIP-6492, `upto`, escrow and ERC-8004 writes are outside this Arc release.

## Usage

Install `pip install "uvd-x402-sdk[signer]>=0.84.0"` for local EVM signing, or the base package for the merchant.

```python
from decimal import Decimal
from uvd_x402_sdk import X402Client, X402Config
from uvd_x402_sdk.response import create_402_response_v2

config = X402Config(
    recipient_evm="0xYourMerchantAddress",
    supported_networks=["arc", "arc-testnet"],
    x402_version=2,
)
merchant = X402Client(config=config)
challenge = create_402_response_v2(
    Decimal("0.01"), config,
    resource={"url": "https://your-service.example/paid"},
    max_timeout_seconds=300,
)
# Return challenge with HTTP 402 when there is no payment.
# After receiving the buyer's PAYMENT-SIGNATURE header:
# result = merchant.process_payment(payment_header, Decimal("0.01"))
# Deliver the paid response only after checking result.success.
```

The buyer echoes the chosen `accepted` object from the merchant's 402:

```python
import os

accepted = next(a for a in challenge["accepts"] if a["network"] == "eip155:5042002")
buyer = X402Client(recipient_address=accepted["payTo"])
buyer.connect_with_private_key(os.environ["ARC_PRIVATE_KEY"], chain_name="arc-testnet")
payment_header = buyer.create_authorization(
    accepted["payTo"], Decimal("0.01"), x402_version=2,
    accepted=accepted, resource=challenge["resource"], valid_duration=300,
)
```

Use `arc` / `eip155:5042` to select mainnet. Set `X402Config.x402_version=1` and use v1 headers when integrating with a v1 merchant. Explicit version selection is shown because Python's existing header normalization can choose a v1 envelope under `auto`; both facilitator versions are supported and tested.

## Validation and operations

Automated tests cover both registries, amount scaling, v1/v2 forms, real EIP-712 signatures and rejection of signatures under the other network's domain. The complete SDK suite passed **1192 tests**, and the existing Python/TypeScript conformance suite passed **430 checks**. Four real payments from this SDK were confirmed through the public facilitator. Each transferred one atomic USDC unit; replay did not credit the recipient again. [Full receipt evidence](../reports/2026-09-16-arc-sdk-acceptance.json).

A timeout or an error carrying a transaction hash is an uncertain payment. Reconcile that hash and the original authorization nonce before asking the payer for a new signature. Never create a fresh authorization automatically to resolve uncertainty.

Primary references: [Arc connection parameters](https://docs.arc.io/arc/references/connect-to-arc), [contract addresses](https://docs.arc.io/arc/references/contract-addresses), [facilitator Arc operations and receipts](https://github.com/UltravioletaDAO/x402-rs/blob/main/docs/networks/arc.md).

## Confirmed SDK payments (2026-09-16)

| Network | Protocol | Receipt |
|---|---|---|
| arc-testnet | v1 | [0xc59b3e674dc3ba570aa4f7c3139d3b98abd82307e91f38257d1d6be9c2e4869c](https://explorer.testnet.arc.io/tx/0xc59b3e674dc3ba570aa4f7c3139d3b98abd82307e91f38257d1d6be9c2e4869c) |
| arc-testnet | v2 | [0x4f0063a35fd080871df6bc86076977c0794eb3a6af80fbaa31d91cece3f1ee4c](https://explorer.testnet.arc.io/tx/0x4f0063a35fd080871df6bc86076977c0794eb3a6af80fbaa31d91cece3f1ee4c) |
| arc | v1 | [0xb7907dfee4cddc5a2a1cf3ffa1cbbe292eff3db46d526eafef362a03bebfeb43](https://explorer.arc.io/tx/0xb7907dfee4cddc5a2a1cf3ffa1cbbe292eff3db46d526eafef362a03bebfeb43) |
| arc | v2 | [0x08a13e447f19e61ddba6c3a05006f909a443fb947bef859c5307ca7684167f8a](https://explorer.arc.io/tx/0x08a13e447f19e61ddba6c3a05006f909a443fb947bef859c5307ca7684167f8a) |

These are controlled operator canaries through the SDK and public facilitator. They do not constitute customer sales or a browser/merchant UI acceptance test.
