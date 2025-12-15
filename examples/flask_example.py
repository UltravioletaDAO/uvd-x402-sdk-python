"""
Flask example for x402 payments.

Run with:
    pip install uvd-x402-sdk[flask]
    python examples/flask_example.py

Test:
    # Request without payment (get 402)
    curl http://localhost:5000/api/premium

    # (Frontend would then create payment with wallet and include X-PAYMENT header)
"""

from decimal import Decimal

from flask import Flask, g, jsonify

from uvd_x402_sdk.integrations.flask_integration import FlaskX402

app = Flask(__name__)

# Configure x402 with your recipient address
x402 = FlaskX402(
    app,
    recipient_address="0xYourEVMWalletAddress",  # Replace with your wallet
    # Optional: Add other recipients
    # recipient_solana="YourSolanaAddress",
    # recipient_stellar="YourStellarAddress",
)


@app.route("/")
def index():
    """Free endpoint - no payment required."""
    return jsonify({"message": "Welcome to the API"})


@app.route("/api/premium")
@x402.require_payment(amount_usd=Decimal("5.00"))
def premium_endpoint():
    """
    Premium endpoint - requires $5.00 USDC payment.

    After successful payment, g.payment_result contains:
    - payer_address: Wallet that paid
    - transaction_hash: On-chain transaction
    - network: Network used (base, solana, etc.)
    - amount_usd: Amount paid
    """
    return jsonify({
        "message": "Premium content unlocked!",
        "payer": g.payment_result.payer_address,
        "transaction": g.payment_result.transaction_hash,
        "network": g.payment_result.network,
    })


@app.route("/api/basic")
@x402.require_payment(amount_usd=Decimal("1.00"))
def basic_endpoint():
    """Basic endpoint - requires $1.00 USDC payment."""
    return jsonify({
        "message": "Basic content unlocked!",
        "payer": g.payment_result.payer_address,
    })


@app.route("/api/dynamic", methods=["POST"])
@x402.require_payment(
    amount_callback=lambda: Decimal("2.50"),  # Could read from request.json
    message="Pay $2.50 to access dynamic pricing example"
)
def dynamic_pricing():
    """Endpoint with dynamic pricing."""
    return jsonify({
        "message": "Dynamic content unlocked!",
        "payer": g.payment_result.payer_address,
    })


if __name__ == "__main__":
    print("Starting Flask x402 example server...")
    print("Test endpoints:")
    print("  GET  http://localhost:5000/            (free)")
    print("  GET  http://localhost:5000/api/premium ($5.00)")
    print("  GET  http://localhost:5000/api/basic   ($1.00)")
    print("  POST http://localhost:5000/api/dynamic ($2.50)")
    app.run(debug=True, port=5000)
