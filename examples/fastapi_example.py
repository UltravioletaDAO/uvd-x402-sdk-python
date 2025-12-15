"""
FastAPI example for x402 payments.

Run with:
    pip install uvd-x402-sdk[fastapi] uvicorn
    uvicorn examples.fastapi_example:app --reload

Test:
    # Request without payment (get 402)
    curl http://localhost:8000/api/premium
"""

from decimal import Decimal

from fastapi import FastAPI, Request, Depends
from fastapi.responses import JSONResponse

from uvd_x402_sdk.config import X402Config
from uvd_x402_sdk.models import PaymentResult
from uvd_x402_sdk.integrations.fastapi_integration import (
    FastAPIX402,
    X402Depends,
    X402Middleware,
)

app = FastAPI(title="x402 FastAPI Example")

# Configure x402
config = X402Config(
    recipient_evm="0xYourEVMWalletAddress",  # Replace with your wallet
    recipient_solana="YourSolanaAddress",     # Optional
)

x402 = FastAPIX402(app, config=config)


# Method 1: Using the extension's dependency factory
@app.get("/api/premium")
async def premium_endpoint(
    payment: PaymentResult = Depends(x402.require_payment(amount_usd="5.00"))
):
    """
    Premium endpoint - requires $5.00 USDC payment.

    The payment is processed via FastAPI's dependency injection.
    If no payment or invalid payment, returns 402 automatically.
    """
    return {
        "message": "Premium content unlocked!",
        "payer": payment.payer_address,
        "transaction": payment.transaction_hash,
        "network": payment.network,
    }


# Method 2: Using X402Depends class directly
basic_payment = X402Depends(
    config=config,
    amount_usd=Decimal("1.00"),
    message="Pay $1.00 to access basic content"
)


@app.get("/api/basic")
async def basic_endpoint(payment: PaymentResult = Depends(basic_payment)):
    """Basic endpoint - requires $1.00 USDC payment."""
    return {
        "message": "Basic content unlocked!",
        "payer": payment.payer_address,
    }


# Method 3: Using middleware for path-based protection
# Uncomment to use middleware approach instead of per-route dependencies
#
# app.add_middleware(
#     X402Middleware,
#     config=config,
#     protected_paths={
#         "/api/premium": Decimal("5.00"),
#         "/api/basic": Decimal("1.00"),
#     }
# )


@app.get("/")
async def index():
    """Free endpoint - no payment required."""
    return {"message": "Welcome to the x402 FastAPI API"}


@app.get("/api/free")
async def free_endpoint():
    """Another free endpoint."""
    return {"message": "This content is free!"}


@app.post("/api/dynamic")
async def dynamic_pricing(
    request: Request,
    payment: PaymentResult = Depends(x402.require_payment(amount_usd="2.50"))
):
    """
    Endpoint with dynamic pricing.

    In a real app, you might calculate the amount based on request body.
    """
    body = await request.json()
    return {
        "message": "Dynamic content unlocked!",
        "payer": payment.payer_address,
        "request_data": body,
    }


if __name__ == "__main__":
    import uvicorn
    print("Starting FastAPI x402 example server...")
    print("Test endpoints:")
    print("  GET  http://localhost:8000/            (free)")
    print("  GET  http://localhost:8000/api/premium ($5.00)")
    print("  GET  http://localhost:8000/api/basic   ($1.00)")
    print("  POST http://localhost:8000/api/dynamic ($2.50)")
    uvicorn.run(app, host="0.0.0.0", port=8000)
