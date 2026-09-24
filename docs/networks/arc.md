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

The facilitator URL is `https://facilitator.ultravioletadao.xyz`. Check `/supported` at runtime when using another facilitator. USYC, Gateway, contract-wallet signatures/EIP-6492 and `upto` are outside this Arc release. ERC-8004 identity and reputation are covered since 0.90.0: see [ERC-8004 on Arc](#erc-8004-on-arc-0900). The escrow client is covered since 0.91.0: see [Escrow on Arc](#escrow-on-arc-0910).

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

## ERC-8004 on Arc (0.90.0)

`arc` and `arc-testnet` are `Erc8004Network`s with the canonical registries, and
`arc` is in `RELAYED_FEEDBACK_NETWORKS`, where the registry records the rating
under the rater's own address. `arc-testnet` has reads only: the facilitator
serves identity and reputation there, but no `FeedbackDelegate` is deployed, so
`supports_relayed_feedback("arc-testnet")` is `False`.

| | `arc` (5042) | `arc-testnet` (5042002) |
|---|---|---|
| Identity Registry | `0x8004A169FB4a3325136EB29fA0ceB6D2e539a432` | `0x8004A818BFB912233c491871b3d84c89A494BD9e` |
| Reputation Registry | `0x8004BAa17C55a88189AE136b182e5fdA19dE9b63` | `0x8004B663056A597Dffe9eCcC1965A193B7388713` |
| Validation Registry | `0x8004Cc8439f36fd5F9F049D9fF86523Df6dAAB58` | `0x8004Cb1BF31DAf7788923b405b754f57acEB4272` |
| Relayed feedback | yes: v4 delegate `0x955Cc9fB9aB95FC0821ae74197D273dde5dA84f1` | no (`prepare` answers 400) |

These are the addresses the facilitator names in `ARC_MAINNET_CONTRACTS` and
`ARC_TESTNET_CONTRACTS`, and the ones `uvd-x402-sdk` 2.98.0 (TypeScript) ships.
Measured on 2026-09-23:

- Every registry has code on the RPC this SDK uses for its network
  (`rpc.mainnet.arc.io`, `rpc.testnet.arc.io`): a 130-byte EIP-1967 proxy whose
  implementation slot holds the same address on both networks, with
  `getVersion()` = `2.0.0`.
- The delegate has code on mainnet (5857 bytes), `VERSION()` = 4 and
  `REPUTATION_REGISTRY()` = the mainnet registry. The address has no code on
  testnet.
- The facilitator (2.39.1) lists both networks in `GET /feedback` ->
  `supportedNetworks` (23 networks). `POST /feedback/evm/prepare` answers 200
  on `arc` and offers that delegate and chain 5042. On `arc-testnet` it answers
  400 `relayed feedback is not available on arc-testnet: no FeedbackDelegate is
  deployed there yet`.
- The first relayed rating on Arc is
  [0x0f8c7f7548382885d7674b5773823d560dd242596725acea4c8b4af92674bb2d](https://explorer.arc.io/tx/0x0f8c7f7548382885d7674b5773823d560dd242596725acea4c8b4af92674bb2d)
  (type 4, block 22313930), sent by the facilitator
  (`0x103040545AC5031A11E8C03dd11324C7333a13C7`, which paid the gas). Its
  `NewFeedback` names the rater, not the facilitator, as the client.

```python
from uvd_x402_sdk.erc8004 import Erc8004Client, supports_relayed_feedback

async with Erc8004Client() as client:
    identity = await client.get_identity("arc", 1)
    reputation = await client.get_reputation("arc", 1)

supports_relayed_feedback("arc")          # True  -> prepare_relayed_feedback / submit_relayed_feedback
supports_relayed_feedback("arc-testnet")  # False -> no delegate on testnet
```

Gas on Arc is USDC. The facilitator pays it for the relayed rating, as it does
for payments.

## Escrow on Arc (0.91.0)

`ESCROW_CONTRACTS` registers both networks with the canonical x402r deployment.
The addresses are the same on both:

| Registry key | Contract | Address |
|---|---|---|
| `escrow` | AuthCaptureEscrow (commerce-payments v1.0.0) | `0xBdEA0D1bcC5966192B070Fdf62aB4EF5b4420cff` |
| `operator_factory` | PaymentOperatorFactory v1.0.2 | `0xc24153B7ED8DC03e551F29DDEeA5CadFe57e2716` |
| `token_collector` | ERC3009PaymentCollector | `0x0E3dF9510de65469C4518D7843919c0b8C7A7757` |
| `protocol_fee_config` | ProtocolFeeConfig | `0xBe2d24614F339a1eB103A399F93AA2a39Ca815Bc` |
| `refund_request` | RefundRequestFactory v1.0.1 | `0xe971C674fD5c3462023f3F891dF6289DFbC9CEFC` |
| `usdc` | USDC | `0x3600000000000000000000000000000000000000` |

`AdvancedEscrowClient` defaults the operator to
`0x0258472A1410Ac3Ad720f1BC83f22B3c0af1Fd9D` on both networks. Pass
`operator_address=` to use another one.

These chains are escrow generation `"v3"` (`get_escrow_generation()`). Their
operator has `capture`, `void` and `refund`, not `release` / `refundInEscrow`,
and the client maps its methods onto them:

| Client method | v3 operator call |
|---|---|
| `release(pi, amount)` | `capture(pi, amount, b"")` |
| `refund_in_escrow(pi, amount)` | `void(pi, b"")`, only when `amount` is the whole `capturableAmount` |
| `refund_post_escrow(...)` | `refund(pi, amount, tokenCollector, collectorData)` |
| `charge(...)` | not available: raises `ValueError` before signing |

`void()` takes no amount: it returns everything still capturable. Before
sending it, the client reads `capturableAmount` from the escrow (`getHash` +
`paymentState`). Any other `amount` raises `ValueError`, and a capturable
amount of 0 raises `EscrowNothingToVoidError`. If that read fails, it raises
`EscrowStateUnavailableError`, which is retryable. None of them sends a
transaction. To
pay part and return the rest, call `release(pi, part)` and then
`refund_in_escrow(pi, rest)`.

`build_escrow_pre_auth` accepts both networks with the verified USDC domain
(`USDC` / `2`). The facilitator-proxied calls (`authorize()`,
`release_via_facilitator()`, `refund_via_facilitator()`,
`query_escrow_state()`) are unchanged and need a facilitator that supports
escrow on Arc.

Measured on 2026-09-24 (`scripts/arc_escrow_record.py`, reads only, recorded in
`tests/fixtures/arc-escrow-d.json`):

- Every registered address has code on both RPCs, with the same code hash on
  mainnet and testnet.
- `computeAddress` on the factory returns the default operator on both
  networks. The operator had no code yet on either.
- The factory's code holds every selector of `OPERATOR_ABI_V3` and none of
  `release`, `refundInEscrow`, `refundPostEscrow` or the 4-argument `charge`.
- `AuthCaptureEscrow.getHash` on each network equals the nonce this SDK
  computes for the same `PaymentInfo`.
