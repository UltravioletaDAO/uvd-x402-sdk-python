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

## A payment the facilitator already admitted (0.89.0)

Facilitator 2.39.0 returns an admitted payment's original answer only to the
binding that admitted it: the same `X-UVD-Purchase` capability or the same
`Idempotency-Key`. Holding the signed payment is not a binding. Since 0.89.0 each
payment handling (a `process_payment()` call, or a `settle_payment()` call with its
retries and timeout fallback) sends ONE key on `/verify`, `/settle` and the
fallback's resend. The key is random (`new_idempotency_key()`) unless the call brings
`idempotency_key` or a secret `idempotency_scope`. A key derived from the
`X-PAYMENT` alone would be recomputed by whoever holds the payment, so the SDK
never sends one.

| Facilitator answer | `admitted_authorization_code` | Merchant answer |
| --- | --- | --- |
| `409 authorization_already_settled` / `/verify` `isValid: false` | `authorization_already_settled` | `409`, not delivered |
| `409 receipt_request_conflict` | `receipt_request_conflict` | `409`, not delivered |
| `409 authorization_in_flight` / `/verify` `isValid: false` | `authorization_in_flight` | `503` + `Retry-After`, not delivered |
| `202 settlement_in_progress` under the handling's binding | none (transient) | retry the same request |

`payment_conflict_response(exc)` builds those answers, and every SDK middleware
and decorator uses it; none of them is a `402`. They also answer every failure
without a verdict (a timeout, a store the facilitator could not read, a settle
still in flight) with `503` + `Retry-After`, a payment that may have moved (`502
settlement_unconfirmed`, any failure naming a `transaction`, another `5xx` with
`retryable: false`, a `/settle` `4xx` after a valid `/verify` that is not a
refusal of the request) with `500`, and an authorization already used with `409`
(0.90.1). When a settle times out while the
payment is in flight, the fallback's resend under the same key gets `202
settlement_in_progress` and asks again for up to `SETTLE_IN_FLIGHT_POLL_SECONDS`,
so the same request usually ends in its settle. Facilitators before 2.39.0 did
not tie the replay to the binding. A handling with no binding of its own (a
fresh key, no `X-UVD-Purchase`) that receives such a replay before any of its own
attempts could have admitted the payment raises `PaymentSettlementError` with the
same codes and the receipt. The replay a handling's own fallback or retry receives
is returned, with `idempotent_replayed=True`. With `send_idempotency_key=False`, a
timed-out settle that was admitted gets `409 authorization_already_settled` on its
fallback: probably the merchant's own payment, but unproven. Reconcile the
receipt's `settlement` before delivering anything.

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
