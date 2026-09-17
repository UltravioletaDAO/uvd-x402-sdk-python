"""Native Hedera x402 v2/exact. Amounts are atomic units, never USD prices.

Signing is offline; the facilitator adds its fee-payer signature and submits.
Install ``uvd-x402-sdk[hedera]`` on Python 3.10+ for signing. Discovery and
requirement/envelope builders also work without the optional Hiero dependency.
"""

from __future__ import annotations

import base64
import copy
import json
import re
from typing import Any, Dict, Optional

HEDERA_NETWORKS = {
    "hedera:mainnet": {"usdc": "0.0.456858", "feePayer": "0.0.10868300"},
    "hedera:testnet": {"usdc": "0.0.429274", "feePayer": "0.0.10576385"},
}
# Historical identifier retained for import compatibility, not a payment asset.
HBAR_ASSET = "0.0.0"
MAX_ATOMIC_AMOUNT = (1 << 63) - 1


def _account(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"0\.0\.[1-9][0-9]*", value):
        raise ValueError("Hedera accounts must be canonical numeric IDs: 0.0.123")
    if int(value.split(".")[2]) > MAX_ATOMIC_AMOUNT:
        raise ValueError("Hedera account number exceeds int64")
    return value


def validate_hedera_requirements(requirements: Dict[str, Any]) -> Dict[str, Any]:
    """Validate and copy the exact offer before any signing or network call."""
    r = copy.deepcopy(requirements)
    fields = {"scheme", "network", "asset", "amount", "payTo", "maxTimeoutSeconds", "extra"}
    if set(r) != fields or r.get("scheme") != "exact":
        raise ValueError("Hedera requires exact v2 requirements without extensions")
    network = r.get("network")
    if not isinstance(network, str) or network not in HEDERA_NETWORKS:
        raise ValueError("Expected hedera:mainnet or hedera:testnet")
    if r["asset"] != HEDERA_NETWORKS[network]["usdc"]:
        raise ValueError("Hedera payments support native USDC only; HBAR is for network fees")
    amount = r["amount"]
    if not isinstance(amount, str) or not re.fullmatch(r"[1-9][0-9]*", amount):
        raise ValueError("amount must be a positive canonical atomic integer string")
    if len(amount) > 19 or int(amount) > MAX_ATOMIC_AMOUNT:
        raise ValueError("amount exceeds int64")
    timeout = r["maxTimeoutSeconds"]
    if type(timeout) is not int or not 15 <= timeout <= 180:
        raise ValueError("Hedera maxTimeoutSeconds must be 15..180")
    extra = r["extra"]
    if not isinstance(extra, dict) or set(extra) != {"feePayer"}:
        raise ValueError("Hedera extra must contain only feePayer")
    _account(extra["feePayer"])
    _account(r["payTo"])
    if r["payTo"] == extra["feePayer"]:
        raise ValueError("The fee payer must be distinct from the merchant")
    return r


def build_hedera_requirements(
    network: str, pay_to: str, amount_atomic: str, *, asset: str = "usdc",
    fee_payer: Optional[str] = None, max_timeout_seconds: int = 180,
) -> Dict[str, Any]:
    """Build a native USDC offer with 6 decimals. HBAR is for network fees.

    Defaults name the Ultravioleta facilitator. For another deployment pass
    its feePayer from a trusted ``GET /supported`` response.
    """
    if network not in HEDERA_NETWORKS:
        raise ValueError("Expected hedera:mainnet or hedera:testnet")
    info = HEDERA_NETWORKS[network]
    return validate_hedera_requirements({
        "scheme": "exact", "network": network,
        "asset": info["usdc"] if asset == "usdc" else asset,
        "amount": amount_atomic, "payTo": pay_to,
        "maxTimeoutSeconds": max_timeout_seconds,
        "extra": {"feePayer": fee_payer or info["feePayer"]},
    })


def build_hedera_request(payment_payload: Dict[str, Any], requirements: Dict[str, Any]) -> Dict[str, Any]:
    """Merchant /verify and /settle body. Supply the merchant's OWN offer.

    Never derive the price/recipient from an untrusted buyer's accepted copy.
    """
    r = validate_hedera_requirements(requirements)
    p = copy.deepcopy(payment_payload)
    if (p.get("x402Version") != 2 or p.get("accepted") != r
            or set(p) - {"x402Version", "accepted", "resource", "payload"}):
        raise ValueError("Hedera requires v2 and an exact accepted echo without extensions")
    inner = p.get("payload")
    if not isinstance(inner, dict) or set(inner) != {"transaction"} or not isinstance(inner["transaction"], str):
        raise ValueError("Hedera payload must contain only transaction")
    return {"x402Version": 2, "paymentPayload": p, "paymentRequirements": r}


class HederaSigner:
    """Buyer signing with a DER Ed25519 or ECDSA key; never broadcasts.

    Bound to one ledger, numeric buyer account and trusted fee payer. Keys are
    not included in repr, errors or payment payloads. Never reuse mainnet keys
    for testnet. This class needs Python 3.10+ and the [hedera] extra.
    """

    def __init__(self, account_id: str, private_key: str, *, network: str,
                 fee_payer: Optional[str] = None):
        if network not in HEDERA_NETWORKS:
            raise ValueError("Expected hedera:mainnet or hedera:testnet")
        self.account_id = _account(account_id)
        self.network = network
        self.fee_payer = _account(fee_payer or HEDERA_NETWORKS[network]["feePayer"])
        if self.account_id == self.fee_payer:
            raise ValueError("Buyer and fee payer must be distinct")
        try:
            from hiero_sdk_python import PrivateKey
        except ImportError:
            raise ImportError("Install uvd-x402-sdk[hedera] on Python 3.10+") from None
        try:
            self._key = PrivateKey.from_string_der(private_key)
        except Exception:
            raise ValueError("Invalid Hedera DER private key (value redacted)") from None

    def create_payment_payload(self, accepted: Dict[str, Any], *,
                               resource: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        r = validate_hedera_requirements(accepted)
        if r["network"] != self.network or r["extra"]["feePayer"] != self.fee_payer:
            raise ValueError("Offer does not match the signer's ledger and trusted fee payer")
        if r["payTo"] == self.account_id:
            raise ValueError("Buyer and merchant must be distinct")
        from hiero_sdk_python import AccountId, TokenId, TransactionId, TransferTransaction
        from hiero_sdk_python.hapi.sdk.transaction_list_pb2 import TransactionList
        from hiero_sdk_python.hapi.services.transaction_pb2 import Transaction

        buyer = AccountId.from_string(self.account_id)
        merchant = AccountId.from_string(r["payTo"])
        tx_id = TransactionId.generate(AccountId.from_string(self.fee_payer))
        amount = int(r["amount"])
        variants = []
        # Explicit nodes and one immutable ID; no client/operator or network I/O.
        # Hiero Python to_bytes serializes ONE Transaction; x402 needs a List.
        for node in ("0.0.3", "0.0.4", "0.0.7" if self.network == "hedera:mainnet" else "0.0.5"):
            tx = TransferTransaction()
            token = TokenId.from_string(r["asset"])
            tx.add_token_transfer(token, buyer, -amount).add_token_transfer(token, merchant, amount)
            tx.set_transaction_id(tx_id)
            tx.set_node_account_id(AccountId.from_string(node))
            tx.set_transaction_valid_duration(r["maxTimeoutSeconds"])
            tx.transaction_fee = 100_000_000  # sponsor maximum: 1 HBAR, not a quoted fee
            tx.freeze().sign(self._key)
            variants.append(Transaction.FromString(tx.to_bytes()))
        encoded = TransactionList(transaction_list=variants).SerializeToString()
        p: Dict[str, Any] = {"x402Version": 2, "accepted": r,
                             "payload": {"transaction": base64.b64encode(encoded).decode("ascii")}}
        if resource is not None:
            p["resource"] = copy.deepcopy(resource)
        return p

    def create_payment_header(self, accepted: Dict[str, Any], *,
                              resource: Optional[Dict[str, Any]] = None) -> str:
        p = self.create_payment_payload(accepted, resource=resource)
        return base64.b64encode(json.dumps(p).encode("utf-8")).decode("ascii")
