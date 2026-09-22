# Facilitator receipts and restart-safe purchases

Install `uvd-x402-sdk[receipts]` for offline Ed25519 verification. Hedera continues
to require the `[hedera]` extra and accepts USDC only; HBAR pays sponsor fees.
Receipts initially cover Arc mainnet/testnet USDC/EURC and native Hedera USDC v2.
Check `/supported.facilitatorReceipts` before relying on facilitator support.

```python
from uvd_x402_sdk import PurchaseContext

# client is your configured X402Client. Store contexts privately in your DB.
context = PurchaseContext.from_dict(saved_context) if saved_context else PurchaseContext()
result = client.fetch_with_receipt(
    "https://merchant.example/data",
    context=context,
    persist=save_context_to_private_database,
    jwks=trusted_facilitator_keys,
)
print(result.payment_state, result.proof_verified)
if result.receipt:
    print(result.receipt.network, result.receipt.asset, result.receipt.amount,
          result.receipt.payTo, result.receipt.requestHash,
          result.receipt.settlement, result.receipt.refusalReason)
# result.response is the original httpx response; result.error records transport loss.
```

The persistence callback must complete durably before returning. Saved context
includes a payment authorization and secret access token: never log or publish
it. Resume exactly that context after a lost response, restart or HTTP 500.
No new payment signature is created on resume. A new context is a new purchase.
Payment confirmation is independent of merchant delivery and HTTP success.

FastAPI integrations validate and forward `X-UVD-Purchase`, then return the real
facilitator result in `PAYMENT-RESPONSE` and `X-PAYMENT-RESPONSE`. Direct merchant
integrations should call `validate_purchase_context` against the actual method,
public URL and body bytes, pass `receipt_context=header` to verify/settle, and use
`payment_response_headers(result)` on their response. Configure CORS/proxies to
forward those headers. Other frameworks can use these same helpers explicitly.

`get_receipt(http_client, receipt_id, context)` privately queries the latest
receipt. `verify_receipt(receipt, trusted_keys)` validates issuer, request hash
and JWS. Obtain keys from your configured facilitator's
`/.well-known/receipt-keys.json`; retain old public keys before rotation. Parsing
alone does not verify provenance. The lookup helper does not silently trust or
fetch a key advertised by a receipt.

Amounts are atomic integer strings; `verified` is not `confirmed`. Treat
`pending`/`unknown` conservatively, keeping the authorization. No receipt from an
older merchant means `receipt=None`, never a fabricated success. A confirmed
payment may accompany an HTTP 500 response. The buyer helper is synchronous,
matching the existing `fetch`; async FastAPI merchant propagation is supported.

See the [full contract and operational limits](https://github.com/UltravioletaDAO/x402-rs/blob/main/docs/facilitator-receipts.md).
EURC real payments settled on Arc mainnet on 2026-09-22 (v1/v2), each with a signed
`confirmed` receipt; see [Arc](networks/arc.md#confirmed-eurc-payments-2026-09-22). The signed fixture vectors are offline.

This API is published in [0.88.0](https://pypi.org/project/uvd-x402-sdk/0.88.0/). The facilitator's
[release snapshot](https://github.com/UltravioletaDAO/x402-rs/blob/main/docs/reports/2026-09-17-facilitator-receipts-release.md)
documents eight confirmed USDC payments: both published SDKs on Arc and Hedera,
mainnet and testnet. Each purchase resumed after a merchant HTTP 500 with the
same authorization, receipt and transaction. The artifact includes all eight
signed receipts, independent chain checks and an audited quota-blocked attempt.
Both SDKs verified all nine exported signatures. Real EURC settlements were not
part of that snapshot and should not be inferred from offline fixtures; they came
later (Arc mainnet, 2026-09-22).

Facilitator 2.36.1 also makes private receipt lookup return HTTP 200 for any
authorized stored receipt, including `unknown` after a failed settlement call.
HTTP 200 means lookup succeeded; inspect and verify the receipt status before
treating the payment as confirmed. This fix preserves the original signature
and settlement POST result, and requires no SDK upgrade beyond this release.
