"""
Escrow and Refund client for x402 SDK.

This module provides escrow payment functionality with refund and dispute
resolution capabilities. Payments can be held in escrow until service delivery
is confirmed, with options for refunds and dispute arbitration.

Features:
- Create escrow payments
- Release funds to recipients
- Request and process refunds
- Open and resolve disputes

Example:
    >>> from uvd_x402_sdk.escrow import EscrowClient
    >>>
    >>> client = EscrowClient()
    >>>
    >>> # Create escrow payment
    >>> escrow = await client.create_escrow(
    ...     payment_header="...",
    ...     requirements={...},
    ...     escrow_duration=86400,  # 24 hours
    ... )
    >>>
    >>> # After service delivery, release funds
    >>> await client.release(escrow.id)
    >>>
    >>> # Or if service failed, request refund
    >>> await client.request_refund(
    ...     escrow_id=escrow.id,
    ...     reason="Service not delivered",
    ... )
"""

from enum import Enum
from typing import Any, Literal, Optional

import httpx
from pydantic import BaseModel, Field


class EscrowStatus(str, Enum):
    """Escrow payment status."""

    PENDING = "pending"  # Payment initiated, awaiting confirmation
    HELD = "held"  # Funds held in escrow
    RELEASED = "released"  # Funds released to recipient
    REFUNDED = "refunded"  # Funds returned to payer
    DISPUTED = "disputed"  # Dispute in progress
    EXPIRED = "expired"  # Escrow expired without resolution


class RefundStatus(str, Enum):
    """Refund request status."""

    PENDING = "pending"  # Refund requested, awaiting processing
    APPROVED = "approved"  # Refund approved
    REJECTED = "rejected"  # Refund rejected
    PROCESSED = "processed"  # Refund completed on-chain
    DISPUTED = "disputed"  # Under dispute review


class DisputeOutcome(str, Enum):
    """Dispute resolution outcome."""

    PENDING = "pending"  # Dispute under review
    PAYER_WINS = "payer_wins"  # Payer gets refund
    RECIPIENT_WINS = "recipient_wins"  # Recipient keeps funds
    SPLIT = "split"  # Funds split between parties


class ReleaseConditions(BaseModel):
    """Conditions for releasing escrow funds."""

    min_hold_time: Optional[int] = Field(None, alias="minHoldTime")
    confirmations: Optional[int] = None
    custom: Optional[Any] = None

    class Config:
        populate_by_name = True


class EscrowPayment(BaseModel):
    """Escrow payment record."""

    id: str
    payment_header: str = Field(..., alias="paymentHeader")
    status: EscrowStatus
    network: str
    payer: str
    recipient: str
    amount: str
    asset: str
    resource: str
    expires_at: str = Field(..., alias="expiresAt")
    release_conditions: Optional[ReleaseConditions] = Field(None, alias="releaseConditions")
    transaction_hash: Optional[str] = Field(None, alias="transactionHash")
    created_at: str = Field(..., alias="createdAt")
    updated_at: str = Field(..., alias="updatedAt")

    class Config:
        populate_by_name = True


class RefundResponse(BaseModel):
    """Refund response from recipient/facilitator."""

    status: Literal["approved", "rejected"]
    reason: Optional[str] = None
    responded_at: str = Field(..., alias="respondedAt")

    class Config:
        populate_by_name = True


class RefundRequest(BaseModel):
    """Refund request record."""

    id: str
    escrow_id: str = Field(..., alias="escrowId")
    status: RefundStatus
    reason: str
    evidence: Optional[str] = None
    amount_requested: str = Field(..., alias="amountRequested")
    amount_approved: Optional[str] = Field(None, alias="amountApproved")
    requester: str
    transaction_hash: Optional[str] = Field(None, alias="transactionHash")
    response: Optional[RefundResponse] = None
    created_at: str = Field(..., alias="createdAt")
    updated_at: str = Field(..., alias="updatedAt")

    class Config:
        populate_by_name = True


class Dispute(BaseModel):
    """Dispute record."""

    id: str
    escrow_id: str = Field(..., alias="escrowId")
    refund_request_id: Optional[str] = Field(None, alias="refundRequestId")
    outcome: DisputeOutcome
    initiator: Literal["payer", "recipient"]
    reason: str
    payer_evidence: Optional[str] = Field(None, alias="payerEvidence")
    recipient_evidence: Optional[str] = Field(None, alias="recipientEvidence")
    arbitration_notes: Optional[str] = Field(None, alias="arbitrationNotes")
    payer_amount: Optional[str] = Field(None, alias="payerAmount")
    recipient_amount: Optional[str] = Field(None, alias="recipientAmount")
    transaction_hashes: Optional[list[str]] = Field(None, alias="transactionHashes")
    created_at: str = Field(..., alias="createdAt")
    resolved_at: Optional[str] = Field(None, alias="resolvedAt")

    class Config:
        populate_by_name = True


class EscrowListResponse(BaseModel):
    """Paginated list of escrow payments."""

    escrows: list[EscrowPayment]
    total: int
    page: int
    limit: int
    has_more: bool = Field(..., alias="hasMore")

    class Config:
        populate_by_name = True


class EscrowClient:
    """
    Client for x402 Escrow & Refund operations.

    The Escrow system holds payments until service is verified,
    enabling refunds and dispute resolution.

    Example:
        >>> client = EscrowClient()
        >>>
        >>> # Create escrow payment (backend)
        >>> escrow = await client.create_escrow(
        ...     payment_header=request.headers["x-payment"],
        ...     requirements=payment_requirements,
        ...     escrow_duration=86400,  # 24 hours
        ... )
        >>>
        >>> # After service is provided, release the escrow
        >>> await client.release(escrow.id)
        >>>
        >>> # If service not provided, payer can request refund
        >>> await client.request_refund(
        ...     escrow_id=escrow.id,
        ...     reason="Service not delivered within expected timeframe",
        ... )
    """

    def __init__(
        self,
        base_url: str = "https://escrow.ultravioletadao.xyz",
        api_key: Optional[str] = None,
        timeout: float = 30.0,
    ):
        """
        Initialize the Escrow client.

        Args:
            base_url: Base URL of the Escrow API
            api_key: API key for authenticated operations
            timeout: Request timeout in seconds
        """
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self._client = httpx.AsyncClient(timeout=timeout)

    async def __aenter__(self) -> "EscrowClient":
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self._client.aclose()

    def _get_headers(self, authenticated: bool = False) -> dict[str, str]:
        """Get request headers."""
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if authenticated and self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    async def create_escrow(
        self,
        payment_header: str,
        requirements: dict[str, Any],
        *,
        escrow_duration: int = 86400,
        release_conditions: Optional[dict[str, Any]] = None,
    ) -> EscrowPayment:
        """
        Create an escrow payment.

        Holds the payment in escrow until released or refunded.

        Args:
            payment_header: Base64-encoded X-PAYMENT header
            requirements: Payment requirements dict
            escrow_duration: Escrow duration in seconds (default: 24h)
            release_conditions: Optional release conditions

        Returns:
            Created escrow payment

        Raises:
            httpx.HTTPStatusError: If the request fails
        """
        url = f"{self.base_url}/escrow"
        payload = {
            "paymentHeader": payment_header,
            "paymentRequirements": requirements,
            "escrowDuration": escrow_duration,
        }
        if release_conditions:
            payload["releaseConditions"] = release_conditions

        response = await self._client.post(
            url,
            json=payload,
            headers=self._get_headers(authenticated=True),
        )
        response.raise_for_status()
        return EscrowPayment.model_validate(response.json())

    async def get_escrow(self, escrow_id: str) -> EscrowPayment:
        """
        Get escrow payment by ID.

        Args:
            escrow_id: Escrow payment ID

        Returns:
            Escrow payment details

        Raises:
            httpx.HTTPStatusError: If the request fails
        """
        url = f"{self.base_url}/escrow/{escrow_id}"
        response = await self._client.get(url, headers=self._get_headers())
        response.raise_for_status()
        return EscrowPayment.model_validate(response.json())

    async def release(self, escrow_id: str) -> EscrowPayment:
        """
        Release escrow funds to recipient.

        Call this after service has been successfully provided.

        Args:
            escrow_id: Escrow payment ID

        Returns:
            Updated escrow payment with transaction hash

        Raises:
            httpx.HTTPStatusError: If the request fails
        """
        url = f"{self.base_url}/escrow/{escrow_id}/release"
        response = await self._client.post(
            url,
            headers=self._get_headers(authenticated=True),
        )
        response.raise_for_status()
        return EscrowPayment.model_validate(response.json())

    async def request_refund(
        self,
        escrow_id: str,
        reason: str,
        *,
        amount: Optional[str] = None,
        evidence: Optional[str] = None,
    ) -> RefundRequest:
        """
        Request a refund for an escrow payment.

        Initiates a refund request that must be approved.

        Args:
            escrow_id: Escrow payment ID
            reason: Reason for refund request
            amount: Amount to refund (full amount if not specified)
            evidence: Supporting evidence

        Returns:
            Created refund request

        Raises:
            httpx.HTTPStatusError: If the request fails
        """
        url = f"{self.base_url}/escrow/{escrow_id}/refund"
        payload: dict[str, Any] = {"reason": reason}
        if amount:
            payload["amount"] = amount
        if evidence:
            payload["evidence"] = evidence

        response = await self._client.post(
            url,
            json=payload,
            headers=self._get_headers(authenticated=True),
        )
        response.raise_for_status()
        return RefundRequest.model_validate(response.json())

    async def approve_refund(
        self,
        refund_id: str,
        amount: Optional[str] = None,
    ) -> RefundRequest:
        """
        Approve a refund request (for recipients).

        Args:
            refund_id: Refund request ID
            amount: Amount to approve (may be less than requested)

        Returns:
            Updated refund request

        Raises:
            httpx.HTTPStatusError: If the request fails
        """
        url = f"{self.base_url}/refund/{refund_id}/approve"
        payload: dict[str, Any] = {}
        if amount:
            payload["amount"] = amount

        response = await self._client.post(
            url,
            json=payload,
            headers=self._get_headers(authenticated=True),
        )
        response.raise_for_status()
        return RefundRequest.model_validate(response.json())

    async def reject_refund(self, refund_id: str, reason: str) -> RefundRequest:
        """
        Reject a refund request (for recipients).

        Args:
            refund_id: Refund request ID
            reason: Reason for rejection

        Returns:
            Updated refund request

        Raises:
            httpx.HTTPStatusError: If the request fails
        """
        url = f"{self.base_url}/refund/{refund_id}/reject"
        response = await self._client.post(
            url,
            json={"reason": reason},
            headers=self._get_headers(authenticated=True),
        )
        response.raise_for_status()
        return RefundRequest.model_validate(response.json())

    async def get_refund(self, refund_id: str) -> RefundRequest:
        """
        Get refund request by ID.

        Args:
            refund_id: Refund request ID

        Returns:
            Refund request details

        Raises:
            httpx.HTTPStatusError: If the request fails
        """
        url = f"{self.base_url}/refund/{refund_id}"
        response = await self._client.get(url, headers=self._get_headers())
        response.raise_for_status()
        return RefundRequest.model_validate(response.json())

    async def open_dispute(
        self,
        escrow_id: str,
        reason: str,
        evidence: Optional[str] = None,
    ) -> Dispute:
        """
        Open a dispute for an escrow payment.

        Initiates arbitration when payer and recipient disagree.

        Args:
            escrow_id: Escrow payment ID
            reason: Reason for dispute
            evidence: Supporting evidence

        Returns:
            Created dispute

        Raises:
            httpx.HTTPStatusError: If the request fails
        """
        url = f"{self.base_url}/escrow/{escrow_id}/dispute"
        payload: dict[str, Any] = {"reason": reason}
        if evidence:
            payload["evidence"] = evidence

        response = await self._client.post(
            url,
            json=payload,
            headers=self._get_headers(authenticated=True),
        )
        response.raise_for_status()
        return Dispute.model_validate(response.json())

    async def submit_evidence(self, dispute_id: str, evidence: str) -> Dispute:
        """
        Submit evidence to a dispute.

        Args:
            dispute_id: Dispute ID
            evidence: Evidence to submit

        Returns:
            Updated dispute

        Raises:
            httpx.HTTPStatusError: If the request fails
        """
        url = f"{self.base_url}/dispute/{dispute_id}/evidence"
        response = await self._client.post(
            url,
            json={"evidence": evidence},
            headers=self._get_headers(authenticated=True),
        )
        response.raise_for_status()
        return Dispute.model_validate(response.json())

    async def get_dispute(self, dispute_id: str) -> Dispute:
        """
        Get dispute by ID.

        Args:
            dispute_id: Dispute ID

        Returns:
            Dispute details

        Raises:
            httpx.HTTPStatusError: If the request fails
        """
        url = f"{self.base_url}/dispute/{dispute_id}"
        response = await self._client.get(url, headers=self._get_headers())
        response.raise_for_status()
        return Dispute.model_validate(response.json())

    async def list_escrows(
        self,
        *,
        status: Optional[EscrowStatus] = None,
        payer: Optional[str] = None,
        recipient: Optional[str] = None,
        page: int = 1,
        limit: int = 20,
    ) -> EscrowListResponse:
        """
        List escrow payments with filters.

        Args:
            status: Filter by status
            payer: Filter by payer address
            recipient: Filter by recipient address
            page: Page number (1-indexed)
            limit: Results per page

        Returns:
            Paginated list of escrow payments

        Raises:
            httpx.HTTPStatusError: If the request fails
        """
        params: dict[str, Any] = {"page": page, "limit": limit}
        if status:
            params["status"] = status.value
        if payer:
            params["payer"] = payer
        if recipient:
            params["recipient"] = recipient

        url = f"{self.base_url}/escrow"
        response = await self._client.get(
            url,
            params=params,
            headers=self._get_headers(authenticated=True),
        )
        response.raise_for_status()
        return EscrowListResponse.model_validate(response.json())

    async def health_check(self) -> bool:
        """
        Check Escrow API health.

        Returns:
            True if healthy
        """
        try:
            url = f"{self.base_url}/health"
            response = await self._client.get(url)
            return response.is_success
        except Exception:
            return False


# Helper functions


def can_release_escrow(escrow: EscrowPayment) -> bool:
    """
    Check if an escrow can be released.

    Args:
        escrow: Escrow payment to check

    Returns:
        True if the escrow can be released
    """
    from datetime import datetime

    if escrow.status != EscrowStatus.HELD:
        return False

    # Check expiration
    expires_at = datetime.fromisoformat(escrow.expires_at.replace("Z", "+00:00"))
    if expires_at < datetime.now(expires_at.tzinfo):
        return False

    # Check minimum hold time if specified
    if escrow.release_conditions and escrow.release_conditions.min_hold_time:
        created_at = datetime.fromisoformat(escrow.created_at.replace("Z", "+00:00"))
        min_release_time = created_at.timestamp() + escrow.release_conditions.min_hold_time
        if datetime.now(created_at.tzinfo).timestamp() < min_release_time:
            return False

    return True


def can_refund_escrow(escrow: EscrowPayment) -> bool:
    """
    Check if an escrow can be refunded.

    Args:
        escrow: Escrow payment to check

    Returns:
        True if the escrow can be refunded
    """
    return escrow.status in (EscrowStatus.HELD, EscrowStatus.PENDING)


def is_escrow_expired(escrow: EscrowPayment) -> bool:
    """
    Check if an escrow is expired.

    Args:
        escrow: Escrow payment to check

    Returns:
        True if the escrow is expired
    """
    from datetime import datetime

    expires_at = datetime.fromisoformat(escrow.expires_at.replace("Z", "+00:00"))
    return expires_at < datetime.now(expires_at.tzinfo)


def escrow_time_remaining(escrow: EscrowPayment) -> float:
    """
    Calculate time remaining until escrow expires.

    Args:
        escrow: Escrow payment to check

    Returns:
        Seconds until expiration (negative if expired)
    """
    from datetime import datetime

    expires_at = datetime.fromisoformat(escrow.expires_at.replace("Z", "+00:00"))
    return (expires_at - datetime.now(expires_at.tzinfo)).total_seconds()
