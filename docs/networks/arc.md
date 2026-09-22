# Arc mainnet and testnet

Direct USDC and EURC `exact` payments are supported by the Ultravioleta facilitator in both x402 v1 and v2. Both have funded payment receipts on Arc mainnet (EURC since 2026-09-22, x402 v1 and v2); EURC funded acceptance on Arc testnet is pending.

| Setting | Mainnet | Testnet |
|---|---|---|
| SDK / v1 name | `arc` | `arc-testnet` |
| Chain ID | `5042` | `5042002` |
| v2 network | `eip155:5042` | `eip155:5042002` |
| RPC | `https://rpc.mainnet.arc.io` | `https://rpc.testnet.arc.io` |
| Explorer | `https://explorer.arc.io` | `https://explorer.testnet.arc.io` |

Both networks use USDC `0x3600000000000000000000000000000000000000` with EIP-712 domain `name: "USDC", version: "2"`. Payment amounts have **6 decimals**: `0.01 USDC` is `10000` atomic units. Native gas has 18 decimals on the same balance; the two readings must not be added. The facilitator pays gas. A payer needs USDC on the selected network.

The chain ID is part of the signature domain. An authorization signed on mainnet cannot be reused on testnet. The SDK preserves their distinct registry entries and CAIP-2 identifiers.

The facilitator URL is `https://facilitator.ultravioletadao.xyz`. Check `/supported` at runtime when using another facilitator. USYC, Gateway, contract-wallet signatures/EIP-6492, `upto`, escrow and ERC-8004 writes are outside this Arc release.

## EURC: prices in euros

EURC is registered for direct EOA `exact` payments in x402 v1/v2. Circle publishes
different contracts for each network:

| Network | EURC contract | Payment decimals | EIP-712 name / version |
|---|---|---|---|
| Arc mainnet | `0xbEf5f6d51CB62b58e6A8f77868681825C6fe21c1` | 6 | `EURC` / `2` |
| Arc testnet | `0x89B50855Aa3bE2F677cD6303Cec089B5F319D72a` | 6 | `EURC` / `2` |

**0.01 EURC is 10000 atomic units and is a euro price.** No USD/EUR exchange rate
is applied. EURC has its own balance; the facilitator still pays gas in **USDC**.
Select the EURC address explicitly and keep USDC as the default dollar asset.
Do not pass a dollar quote into the EURC signing path.

Contract metadata and EIP-712 domain separators were checked through both live
RPCs on 2026-09-17. Offline signatures and network/token isolation are tested.
**Funded EURC payments settled on Arc mainnet on 2026-09-22** through the public
facilitator (2.36.1), signed by this SDK as published (0.88.0) from a fresh EOA,
10000 atomic units (0.01 EURC) each — see [Confirmed EURC payments](#confirmed-eurc-payments-2026-09-22).
**Arc testnet funded EURC settlement is still pending** (Circle's faucet needs a
human); unfunded testnet signatures reach the facilitator's balance check
(`insufficient_funds`). The USDC receipts further below keep their original scope.
[Assessment](../reports/2026-09-17-arc-eurc-assessment.json).
[Official Circle contract list](https://developers.circle.com/stablecoins/eurc-contract-addresses).

### EURC payer and merchant (Python)

Install `pip install "uvd-x402-sdk[signer]>=0.86.0"`. A merchant publishes explicit
atomic requirements; the USD-price convenience helpers are for dollar assets.

```python
from decimal import Decimal
from uvd_x402_sdk.networks import get_network
from uvd_x402_sdk.envelope_v2 import build_verify_request_v2, build_settle_request_v2

network = get_network("arc-testnet")  # "arc" for mainnet
eurc = network.tokens["eurc"]
accepted = {
    "scheme": "exact", "network": f"eip155:{network.chain_id}",
    "asset": eurc.address, "amount": "10000",  # 0.01 EURC
    "payTo": merchant_address, "maxTimeoutSeconds": 300,
    "extra": {"name": eurc.name, "version": eurc.version},
}
resource = {
    "url": "https://your-service.example/paid",
    "description": "Resource priced in EURC", "mimeType": "application/json",
}
# buyer is an X402Client connected to this chain's EVM signer.
header = buyer.create_authorization(
    accepted["payTo"], Decimal("0.01"), token_type="eurc",
    x402_version=2, accepted=accepted, resource=resource, valid_duration=300,
)
# The positional amount means EURC units; amount_usd is a legacy parameter name.
# Merchant: decode the header, but use YOUR stored accepted/resource, never a
# buyer-supplied price. inner is the decoded header's ["payload"].
verify_body = build_verify_request_v2(inner, resource, accepted)
settle_body = build_settle_request_v2(inner, resource, accepted)
# POST verify_body to /verify; require isValid. Then POST settle_body once to
# /settle and require success before delivery. Preserve uncertain transaction IDs.
```

`verify_payment` / `settle_payment` / `process_payment` take
`expected_amount_usd` and reject known EURC assets: use the atomic envelope
builders above. For v1 use the same token/amount/domain in `PaymentRequirements`
and `build_verify_request_for_version` / `build_settle_request_for_version`.


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

## EURC release acceptance (2026-09-17)

Version **0.86.0** is published and was installed into a clean environment.
The full suite passed **1222 tests** and the existing cross-language suite
passed **430 checks**. Four offline signatures from the installed package cover
both Arc networks and both protocol versions; recovered signers and domains
match the independently measured contracts. Package integrity was checked against
the registry. [Release evidence](../reports/2026-09-17-arc-eurc-release-acceptance.json).

At that release funded EURC payments were deferred by operator instruction; these
signature and installation checks are not settlement receipts. The funded receipts
came on 2026-09-22 and are listed below.

## Confirmed EURC payments (2026-09-22)

| Network | Protocol | Receipt | Block |
|---|---|---|---|
| arc | v2 | [0xd9de3864e11698cf730664147ac383acb763279056ac091bab57cfd3bf536128](https://explorer.arc.io/tx/0xd9de3864e11698cf730664147ac383acb763279056ac091bab57cfd3bf536128) | 22114558 |
| arc | v1 | [0x3f966c6e634380b8c741019d66c2395b670ce5755105721d7cbe1e74e3f76d1d](https://explorer.arc.io/tx/0x3f966c6e634380b8c741019d66c2395b670ce5755105721d7cbe1e74e3f76d1d) | 22114670 |

Payer `0x649E4BAf56230ae09EE62Fe47bd98C3e50772869` (a fresh EOA holding only EURC, no
USDC), payee `0x103040545AC5031A11E8C03dd11324C7333a13C7`. Each receipt holds one EURC
`Transfer` of exactly 10000 units, gas was paid by the facilitator in USDC, and the
settle response carried a signed facilitator receipt with status `confirmed`. The
merchant path in this guide was used as written: explicit `accepted` with
`amount: "10000"`, `create_authorization(..., Decimal("0.01"), token_type="eurc")`,
`build_verify_request_v2` / `build_settle_request_v2` (v1: `PaymentRequirements` +
`build_*_request_for_version`). The same run confirmed that `verify_payment` with a
dollar price and `asset=EURC` raises before any request. Replaying each settle body
did not debit the payer again (measured by payer balance); in v1 the replay
returned the original transaction hash. Arc testnet remains pending.
