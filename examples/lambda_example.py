"""
AWS Lambda example for x402 payments.

Deploy as Lambda function with API Gateway trigger.

Environment variables:
    X402_RECIPIENT_EVM: Your EVM wallet address
    X402_RECIPIENT_SOLANA: Your Solana wallet address (optional)
"""

import json
import logging
from decimal import Decimal
from typing import Any, Dict

from uvd_x402_sdk.config import X402Config
from uvd_x402_sdk.integrations.lambda_integration import (
    LambdaX402,
    lambda_handler,
    LambdaEvent,
    LambdaResponse,
)
from uvd_x402_sdk.models import PaymentResult

logger = logging.getLogger()
logger.setLevel(logging.INFO)


# Method 1: Using LambdaX402 helper class directly
# Best for handlers that need fine-grained control

config = X402Config(
    recipient_evm="0xYourEVMWalletAddress",  # Replace with your wallet
    recipient_solana="YourSolanaAddress",     # Optional
)

x402 = LambdaX402(config=config)


def handler_method1(event: LambdaEvent, context: Any) -> LambdaResponse:
    """
    Lambda handler using LambdaX402 helper.

    This approach gives you full control over the payment flow.
    """
    # Parse request body
    body = json.loads(event.get("body", "{}"))

    # Calculate price based on request
    quantity = body.get("quantity", 1)
    price_usd = Decimal(str(quantity * 0.01))  # $0.01 per unit

    # Process payment or return 402
    result = x402.process_or_require(event, price_usd)

    # If result is a response dict, payment is required
    if isinstance(result, dict) and "statusCode" in result:
        return result

    # Payment verified - process the purchase
    payment: PaymentResult = result

    return {
        "statusCode": 200,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
        },
        "body": json.dumps({
            "success": True,
            "message": f"Purchased {quantity} units",
            "payer": payment.payer_address,
            "transaction": payment.transaction_hash,
            "network": payment.network,
            "amount_paid": str(payment.amount_usd),
        }),
    }


# Method 2: Using decorator with fixed amount
# Best for simple endpoints with fixed pricing

@lambda_handler(
    amount_usd=Decimal("5.00"),
    config=config,
    message="Pay $5.00 to access premium content"
)
def handler_premium(
    event: LambdaEvent,
    context: Any,
    payment_result: PaymentResult = None,  # Injected by decorator
) -> LambdaResponse:
    """
    Premium Lambda handler using decorator.

    The @lambda_handler decorator handles payment verification.
    PaymentResult is injected into the function.
    """
    return {
        "statusCode": 200,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps({
            "message": "Premium content unlocked!",
            "payer": payment_result.payer_address,
            "transaction": payment_result.transaction_hash,
        }),
    }


# Method 3: Using decorator with dynamic pricing
# Best for per-request pricing calculations

def calculate_price(event: LambdaEvent) -> Decimal:
    """Calculate price based on request."""
    body = json.loads(event.get("body", "{}"))
    pixels = body.get("pixels", 1)
    return Decimal(str(pixels * 0.01))  # $0.01 per pixel


@lambda_handler(amount_callback=calculate_price, config=config)
def handler_dynamic(
    event: LambdaEvent,
    context: Any,
    payment_result: PaymentResult = None,
) -> LambdaResponse:
    """
    Lambda handler with dynamic pricing.

    Price is calculated per-request by the amount_callback.
    """
    body = json.loads(event.get("body", "{}"))
    pixels = body.get("pixels", 1)

    return {
        "statusCode": 200,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps({
            "message": f"Purchased {pixels} pixels!",
            "payer": payment_result.payer_address,
            "transaction": payment_result.transaction_hash,
            "pixels": pixels,
            "price_per_pixel": "0.01",
        }),
    }


# Method 4: Full example matching 402milly purchase_pixels handler
# Shows how to migrate existing Lambda handlers to use the SDK

def purchase_handler(event: LambdaEvent, context: Any) -> LambdaResponse:
    """
    Complete example matching 402milly's purchase_pixels pattern.

    This shows how to:
    1. Parse request body
    2. Calculate dynamic price
    3. Return 402 if no payment
    4. Verify and settle payment
    5. Save purchase to database
    """
    # CORS preflight
    if event.get("httpMethod") == "OPTIONS":
        return {
            "statusCode": 200,
            "headers": {
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Allow-Methods": "POST, OPTIONS",
                "Access-Control-Allow-Headers": "Content-Type, X-PAYMENT",
            },
            "body": "",
        }

    try:
        # Parse request
        body = json.loads(event.get("body", "{}"))
        pixels = body.get("pixels", [])
        owner = body.get("owner")

        if not pixels or not owner:
            return {
                "statusCode": 400,
                "body": json.dumps({"error": "Missing required fields"}),
            }

        # Calculate price ($0.01 per pixel)
        total_pixels = sum(p.get("width", 1) * p.get("height", 1) for p in pixels)
        price_usd = Decimal(str(total_pixels * 0.01))

        # Process payment
        result = x402.process_or_require(event, price_usd)

        if isinstance(result, dict) and "statusCode" in result:
            return result

        payment: PaymentResult = result

        # Payment verified - save to database
        # db.save_purchase(pixels, owner, payment)

        return {
            "statusCode": 200,
            "headers": {
                "Content-Type": "application/json",
                "Access-Control-Allow-Origin": "*",
            },
            "body": json.dumps({
                "success": True,
                "purchaseId": "px_123",  # Would be generated
                "pixels": total_pixels,
                "price": str(price_usd),
                "payer": payment.payer_address,
                "transaction": payment.transaction_hash,
                "network": payment.network,
            }),
        }

    except Exception as e:
        logger.error(f"Handler error: {e}")
        return {
            "statusCode": 500,
            "body": json.dumps({"error": str(e)}),
        }


# Export handlers for Lambda
# Configure in template.yaml or serverless.yml to point to specific handler
