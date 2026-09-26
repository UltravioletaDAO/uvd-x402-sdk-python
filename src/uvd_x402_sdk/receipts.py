"""Portable facilitator receipts and resumable buyer context.

The context contains a reusable payment authorization and a secret lookup
capability. Persist it in private application storage, never in logs. A receipt
reports payment state; merchant delivery is the separate HTTP response.
"""
from __future__ import annotations

import base64
import hashlib
import json
import secrets
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Dict, Mapping, Optional

import httpx
from pydantic import BaseModel, ConfigDict

from uvd_x402_sdk.stack_key import no_redirect_kwargs, refuse_redirect, stack_key_headers

ISSUER = "https://facilitator.ultravioletadao.xyz"
STATES = {"verified", "pending", "confirmed", "rejected", "unknown"}


def canonical(value: Any) -> str:
    def validate(v: Any) -> None:
        if isinstance(v, dict):
            for key, item in v.items():
                if not isinstance(key, str) or not key.isascii():
                    raise ValueError("non-ASCII canonical key")
                validate(item)
        elif isinstance(v, list):
            for item in v:
                validate(item)
        elif isinstance(v, bool) or v is None or isinstance(v, str):
            pass
        elif isinstance(v, int) and 0 <= v <= 9007199254740991:
            pass
        else:
            raise ValueError("unsupported canonical value")
    validate(value)
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def commitment(domain: str, value: Any) -> str:
    return hashlib.sha256((domain + "\n" + canonical(value)).encode()).hexdigest()


class FacilitatorReceipt(BaseModel):
    model_config = ConfigDict(extra="allow")
    schemaVersion: int
    receiptId: str
    revision: int
    issuer: str
    issuedAt: int
    operation: str
    purchaseId: Optional[str] = None
    network: str
    scheme: str
    x402Version: int
    asset: str
    amount: str
    decimals: Optional[int] = None
    payTo: str
    payer: Optional[str] = None
    requestHash: str
    requestHashVersion: str
    request: Dict[str, Any]
    paymentRequestHash: str
    authorizationId: str
    status: str
    settlement: Optional[Dict[str, Any]] = None
    refusalReason: Optional[str] = None
    diagnosticCode: Optional[str] = None
    retry: Dict[str, Any]
    proof: Optional[Dict[str, Any]] = None


def parse_receipt(value: Any) -> Optional[FacilitatorReceipt]:
    if value is None:
        return None
    receipt = FacilitatorReceipt.model_validate(value)
    if receipt.schemaVersion != 1 or receipt.status not in STATES:
        raise ValueError("unsupported receipt version or state")
    if receipt.requestHashVersion != "uvd-x402-request-v1" or receipt.requestHash != commitment(receipt.requestHashVersion, receipt.request):
        raise ValueError("receipt request hash mismatch")
    for key in ("network", "scheme", "asset", "amount", "payTo", "purchaseId"):
        if getattr(receipt, key) != receipt.request.get(key):
            raise ValueError("receipt terms mismatch")
    if not receipt.amount.isascii() or not receipt.amount.isdigit():
        raise ValueError("receipt amount must be an atomic integer string")
    if receipt.status == "confirmed" and (receipt.operation != "settle" or not (receipt.settlement or {}).get("id")):
        raise ValueError("confirmation has no settlement")
    return receipt


def _unb64url(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def verify_receipt(receipt: FacilitatorReceipt, jwks: Mapping[str, Any], *, issuer: str = ISSUER) -> bool:
    """Verify against keys obtained from a trusted issuer (never from the receipt).

    Retain old trusted public keys to verify receipts issued before rotation.
    No network request or chain transaction is performed by this function.
    """
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    try:
        parse_receipt(receipt.model_dump())
        if receipt.issuer != issuer or not receipt.proof or receipt.proof.get("type") != "jws":
            return False
        protected, payload, signature = receipt.proof["jws"].split(".")
        header = json.loads(_unb64url(protected))
        if header.get("alg") != "EdDSA" or header.get("typ") != "uvd-facilitator-receipt+jws":
            return False
        keys = [key for key in jwks.get("keys", []) if key.get("kid") == header.get("kid")
                and key.get("kty") == "OKP" and key.get("crv") == "Ed25519"]
        if len(keys) != 1:
            return False
        unsigned = receipt.model_dump()
        unsigned["proof"] = None
        if _unb64url(payload) != canonical(unsigned).encode():
            return False
        key = Ed25519PublicKey.from_public_bytes(_unb64url(keys[0]["x"]))
        key.verify(_unb64url(signature), (protected + "." + payload).encode())
        return True
    except (ValueError, KeyError, TypeError, AttributeError, InvalidSignature):
        return False


@dataclass
class PurchaseContext:
    purchase_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    access_token: str = field(default_factory=lambda: secrets.token_hex(32), repr=False)
    method: Optional[str] = None
    url: Optional[str] = None
    body_sha256: Optional[str] = None
    payment_headers: Dict[str, str] = field(default_factory=dict, repr=False)
    receipt: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "PurchaseContext":
        return cls(**dict(value))

    def bind(self, request: httpx.Request) -> None:
        actual = (request.method, str(request.url), hashlib.sha256(request.content).hexdigest())
        previous = (self.method, self.url, self.body_sha256)
        if self.method is not None and actual != previous:
            raise ValueError("purchase context cannot be reused for another HTTP request")
        self.method, self.url, self.body_sha256 = actual

    def header(self) -> str:
        return base64.b64encode(canonical({"purchaseId": self.purchase_id, "accessToken": self.access_token,
            "method": self.method, "url": self.url, "bodySha256": self.body_sha256}).encode()).decode()


@dataclass
class FetchReceiptResult:
    response: Optional[httpx.Response]
    receipt: Optional[FacilitatorReceipt]
    payment_state: str
    context: PurchaseContext
    proof_verified: bool = False
    error: Optional[Exception] = None


def response_receipt(response: httpx.Response) -> Optional[FacilitatorReceipt]:
    headers = [response.headers.get_list(name) for name in ("payment-response", "x-payment-response")]
    if any(len(values) > 1 for values in headers):
        raise ValueError("duplicate payment response header")
    present = [values[0] for values in headers if values]
    if not present:
        return None
    if len(set(present)) != 1 or len(present[0]) > 32768:
        raise ValueError("ambiguous or oversized payment response header")
    value = json.loads(base64.b64decode(present[0], validate=True))
    return parse_receipt(value.get("receipt"))


def payment_response_headers(result: Any) -> Dict[str, str]:
    """Merchant helper: propagate the facilitator result without fabricating it."""
    data = result.model_dump(mode="json") if hasattr(result, "model_dump") else dict(result)
    encoded = base64.b64encode(json.dumps(data, separators=(",", ":")).encode()).decode()
    if len(encoded) > 32768:
        raise ValueError("payment response too large")
    return {"PAYMENT-RESPONSE": encoded, "X-PAYMENT-RESPONSE": encoded,
            "Access-Control-Expose-Headers": "PAYMENT-RESPONSE, X-PAYMENT-RESPONSE", "Cache-Control": "no-store"}


def validate_purchase_context(header: Optional[str], method: str, url: str, body: bytes) -> Optional[str]:
    """Validate against actual merchant HTTP bytes, not buyer-supplied metadata."""
    if header is None:
        return None
    if len(header) > 8192:
        raise ValueError("invalid purchase context")
    value = json.loads(base64.b64decode(header, validate=True))
    token = value.get("accessToken")
    if (not isinstance(token, str) or len(token) != 64 or any(c not in "0123456789abcdef" for c in token)
        or not isinstance(value.get("purchaseId"), str) or not 1 <= len(value["purchaseId"]) <= 128
        or value.get("method") != method.upper() or value.get("url") != str(httpx.URL(url))
        or value.get("bodySha256") != hashlib.sha256(body).hexdigest()):
        raise ValueError("purchase context does not match the HTTP request")
    return header


def fetch_with_receipt(client: Any, url: str, *, context: PurchaseContext,
                       persist: Callable[[Dict[str, Any]], None], jwks: Optional[Mapping[str, Any]] = None,
                       issuer: str = ISSUER, **kwargs: Any) -> FetchReceiptResult:
    """Persist BEFORE sending a signed request. Resume with the same context.

    A transport error is returned with ``payment_state='unknown'`` and the
    original authorization remains in context. No fresh signature is generated
    for a resumed purchase, even when the merchant answers 402 or 500.
    """
    http = kwargs.pop("http_client", None) or client._get_http_client()
    method = kwargs.get("method", "GET")
    request_keys = {"headers", "params", "content", "data", "files", "json", "cookies", "timeout", "extensions"}
    initial = http.build_request(method, url, **{k: v for k, v in kwargs.items() if k in request_keys})
    context.bind(initial)
    frozen_headers = dict(initial.headers)
    transport_options = {key: kwargs[key] for key in ("timeout", "extensions", "auth", "follow_redirects") if key in kwargs}
    for key in request_keys:
        kwargs.pop(key, None)
    kwargs.update(headers=frozen_headers, content=initial.content, **transport_options)
    receipt = parse_receipt(context.receipt)

    class Transport:
        def request(self, request_method: str, request_url: str, **options: Any) -> httpx.Response:
            headers = dict(options.get("headers") or {})
            payment = {k: v for k, v in headers.items() if k.lower() in ("x-payment", "payment-signature")}
            if payment:
                if context.payment_headers and payment != context.payment_headers:
                    raise ValueError("refusing a new authorization for an existing purchase")
                context.payment_headers = payment
                headers["X-UVD-Purchase"] = context.header()
                options["headers"] = headers
                persist(context.to_dict())
            return http.request(request_method, request_url, **options)

    try:
        if context.payment_headers:
            headers = {**frozen_headers, **context.payment_headers, "X-UVD-Purchase": context.header()}
            persist(context.to_dict())
            response = http.request(method, str(initial.url), headers=headers, content=initial.content, **transport_options)
        else:
            response = client.fetch(str(initial.url), http_client=Transport(), **kwargs)
        incoming = response_receipt(response)
        if incoming:
            if incoming.issuer != issuer or any(incoming.request.get(k) != v for k, v in {
                "purchaseId": context.purchase_id, "method": context.method, "url": context.url,
                "bodySha256": context.body_sha256}.items()):
                raise ValueError("receipt does not bind this purchase")
            if receipt and receipt.operation == "settle" and (incoming.receiptId != receipt.receiptId or incoming.revision < receipt.revision):
                raise ValueError("receipt revision conflict")
            if jwks and not verify_receipt(incoming, jwks, issuer=issuer):
                raise ValueError("receipt signature verification failed")
            receipt = incoming
            context.receipt = receipt.model_dump()
            persist(context.to_dict())
        verified = bool(receipt and jwks and verify_receipt(receipt, jwks, issuer=issuer))
        if receipt and jwks and not verified:
            raise ValueError("receipt signature verification failed")
        return FetchReceiptResult(response, receipt, receipt.status if receipt else ("unknown" if context.payment_headers else "not_required"), context, verified)
    except httpx.RequestError as error:
        return FetchReceiptResult(None, receipt, "unknown", context, error=error)


def get_receipt(
    http: httpx.Client,
    receipt_id: str,
    context: PurchaseContext,
    *,
    issuer: str = ISSUER,
    stack_key: str | None = None,
    stack_key_hosts: list[str] | None = None,
) -> FacilitatorReceipt:
    # Receipt IDs are paths, not attacker-controlled URLs.
    uuid.UUID(receipt_id)
    url = issuer.rstrip("/") + "/receipts/" + receipt_id
    # `stack_key`: the X-UVD-Stack-Key of a service of Ultravioleta DAO, only
    # when `issuer` is one of its facilitators (see uvd_x402_sdk.stack_key).
    headers = {"Authorization": "Bearer " + context.access_token}
    headers.update(stack_key_headers(stack_key, url, stack_key_hosts))
    response = http.get(url, headers=headers, **no_redirect_kwargs(headers, http))
    refuse_redirect(response, "receipt")
    response.raise_for_status()
    receipt = parse_receipt(response.json().get("receipt"))
    if receipt is None or receipt.issuer != issuer or receipt.purchaseId != context.purchase_id:
        raise ValueError("receipt lookup mismatch")
    return receipt
