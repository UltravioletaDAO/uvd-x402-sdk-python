"""
Custom exceptions for the x402 SDK.

These exceptions provide clear, actionable error messages for different
failure scenarios in the payment flow.
"""

from decimal import Decimal
from typing import Optional, List, Dict, Any


class X402Error(Exception):
    """Base exception for all x402 SDK errors."""

    def __init__(
        self,
        message: str,
        code: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.message = message
        self.code = code or "X402_ERROR"
        self.details = details or {}
        super().__init__(self.message)

    def to_dict(self) -> Dict[str, Any]:
        """Convert exception to dictionary for API responses."""
        return {
            "error": self.code,
            "message": self.message,
            "details": self.details,
        }


class PaymentRequiredError(X402Error):
    """
    Raised when a payment is required but not provided.

    This should trigger a 402 Payment Required response.
    """

    def __init__(
        self,
        message: str = "Payment required",
        amount_usd: Optional[str] = None,
        recipient: Optional[str] = None,
        supported_networks: Optional[List[str]] = None,
    ) -> None:
        details = {}
        if amount_usd:
            details["amount"] = amount_usd
        if recipient:
            details["recipient"] = recipient
        if supported_networks:
            details["supportedNetworks"] = supported_networks

        super().__init__(
            message=message,
            code="PAYMENT_REQUIRED",
            details=details,
        )
        self.amount_usd = amount_usd
        self.recipient = recipient
        self.supported_networks = supported_networks


class PaymentVerificationError(X402Error):
    """
    Raised when payment verification fails.

    Common causes:
    - Invalid signature
    - Amount mismatch
    - Wrong recipient
    - Expired payment authorization
    """

    def __init__(
        self,
        message: str,
        reason: Optional[str] = None,
        errors: Optional[List[str]] = None,
    ) -> None:
        details = {}
        if reason:
            details["reason"] = reason
        if errors:
            details["errors"] = errors

        super().__init__(
            message=message,
            code="PAYMENT_VERIFICATION_FAILED",
            details=details,
        )
        self.reason = reason
        self.errors = errors or []


class PaymentSettlementError(X402Error):
    """
    Raised when payment settlement fails on-chain.

    Common causes:
    - Insufficient USDC balance
    - Nonce already used
    - Authorization expired
    - Network congestion/timeout
    """

    def __init__(
        self,
        message: str,
        network: Optional[str] = None,
        tx_hash: Optional[str] = None,
        reason: Optional[str] = None,
    ) -> None:
        details = {}
        if network:
            details["network"] = network
        if tx_hash:
            details["transactionHash"] = tx_hash
        if reason:
            details["reason"] = reason

        super().__init__(
            message=message,
            code="PAYMENT_SETTLEMENT_FAILED",
            details=details,
        )
        self.network = network
        self.tx_hash = tx_hash
        self.reason = reason


class UnsupportedNetworkError(X402Error):
    """
    Raised when an unsupported network is specified.

    Use `register_network()` to add custom network support.
    """

    def __init__(
        self,
        network: str,
        supported_networks: Optional[List[str]] = None,
    ) -> None:
        super().__init__(
            message=f"Unsupported network: {network}",
            code="UNSUPPORTED_NETWORK",
            details={
                "requestedNetwork": network,
                "supportedNetworks": supported_networks or [],
            },
        )
        self.network = network
        self.supported_networks = supported_networks or []


class InvalidPayloadError(X402Error):
    """
    Raised when the X-PAYMENT header payload is invalid.

    Common causes:
    - Invalid base64 encoding
    - Invalid JSON format
    - Missing required fields
    - Invalid x402 version
    """

    def __init__(
        self,
        message: str,
        field: Optional[str] = None,
        expected: Optional[str] = None,
        received: Optional[str] = None,
    ) -> None:
        details = {}
        if field:
            details["field"] = field
        if expected:
            details["expected"] = expected
        if received:
            details["received"] = received

        super().__init__(
            message=message,
            code="INVALID_PAYLOAD",
            details=details,
        )
        self.field = field
        self.expected = expected
        self.received = received


class ConfigurationError(X402Error):
    """
    Raised when SDK configuration is invalid or missing.
    """

    def __init__(self, message: str, config_key: Optional[str] = None) -> None:
        super().__init__(
            message=message,
            code="CONFIGURATION_ERROR",
            details={"configKey": config_key} if config_key else {},
        )
        self.config_key = config_key


#: Values of the facilitator's ``reason`` field on a 503 from the EVM writer
#: lease, for which the write provably NEVER RAN. The facilitator returns these
#: BEFORE it touches the lease holder, so re-presenting the same request is
#: safe — including a mint, which is the one request that must never be
#: repeated blind.
WRITE_NOT_ATTEMPTED_REASONS = frozenset(
    {
        "holder_unknown",
        "forwarding_disabled",
        "forwarded_but_not_writer",
        "body_unreadable",
    }
)

#: Values of ``reason`` for which the write is AMBIGUOUS: the forward to the
#: lease holder failed AFTER the attempt, so the holder may have processed the
#: write and only the response was lost. Treat exactly like a timeout.
#:
#: On ``POST /register`` this must never be resolved by re-POSTing the mint.
#: Resolve it with ``GET /identity/{network}/owner/{recipient}`` first, honouring
#: that endpoint's 404-vs-503 distinction; re-POSTing an ambiguous mint is what
#: produced five duplicate agents.
WRITE_AMBIGUOUS_REASONS = frozenset({"forward_failed"})

#: Hard ceiling, in seconds, on any ``Retry-After`` the SDK will honour by
#: sleeping or by echoing to a caller. A misconfigured facilitator answering
#: ``Retry-After: 3600`` must not be able to hang a request for an hour; the
#: header is advice, not an instruction.
MAX_RETRY_AFTER_SECONDS = 30.0


def parse_retry_after(value: Any) -> Optional[float]:
    """Parse a ``Retry-After`` header into seconds, clamped to a sane ceiling.

    Returns ``None`` for a missing, non-numeric or non-positive value — the
    HTTP-date form included, which the facilitator never sends and which is not
    worth a dependency. Anything above :data:`MAX_RETRY_AFTER_SECONDS` is
    clamped rather than rejected: a server asking for longer still means
    "later", and dropping the header entirely would retry immediately.
    """
    if value is None:
        return None
    try:
        seconds = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    if seconds <= 0:
        return None
    return min(seconds, MAX_RETRY_AFTER_SECONDS)


def write_retry_is_safe(reason: Optional[str]) -> bool:
    """Is a write carrying this facilitator ``reason`` safe to re-send verbatim?

    ``True`` only for the reasons the facilitator emits BEFORE reaching the
    lease holder. ``False`` for ``forward_failed`` and for anything unknown —
    a ``reason`` this SDK has never heard of is ambiguous by construction, and
    guessing optimistically about an unknown is how a duplicate mint happens.
    """
    return reason in WRITE_NOT_ATTEMPTED_REASONS


class FacilitatorError(X402Error):
    """
    Raised when the facilitator returns an error.

    Contains the raw error response from the facilitator for debugging.

    Carries the two fields that decide what a server should answer its buyer:

    * ``reason`` — the facilitator's own machine-readable diagnosis. On a 503
      from the EVM writer lease this is one of ``holder_unknown``,
      ``forwarding_disabled``, ``forwarded_but_not_writer``, ``body_unreadable``
      (the write never ran, retry is safe) or ``forward_failed`` (ambiguous,
      like a timeout). Use :func:`write_retry_is_safe` rather than comparing
      strings, and treat an unknown value as ambiguous.
    * ``retry_after`` — the server's ``Retry-After``, in seconds, already
      clamped to :data:`MAX_RETRY_AFTER_SECONDS`.

    ``retryable`` mirrors the transient/final verdict so this class stops being
    the only one in the hierarchy without the attribute that
    ``LookupInconclusiveError`` and ``RegistrationPendingError`` already carry.
    A 5xx is transient; a 4xx other than 429 is final.
    """

    def __init__(
        self,
        message: str,
        status_code: Optional[int] = None,
        response_body: Optional[str] = None,
        *,
        reason: Optional[str] = None,
        retry_after: Optional[float] = None,
    ) -> None:
        retryable = (
            status_code is None or status_code == 429 or status_code >= 500
        )
        # Clamped HERE rather than only at the parse site, so a value handed in
        # by a caller (or by a future code path that reads the header itself)
        # cannot smuggle `Retry-After: 3600` past the ceiling.
        retry_after = parse_retry_after(retry_after)
        details: Dict[str, Any] = {
            "statusCode": status_code,
            "response": response_body,
        }
        # Added only when present, so the ``to_dict()`` of every error raised
        # before this release keeps exactly the shape it had.
        if reason is not None:
            details["reason"] = reason
        if retry_after is not None:
            details["retryAfter"] = retry_after
        if retryable:
            details["retryable"] = True
        super().__init__(
            message=message,
            code="FACILITATOR_ERROR",
            details=details,
        )
        self.status_code = status_code
        self.response_body = response_body
        self.reason = reason
        self.retry_after = retry_after
        self.retryable = retryable


class LookupInconclusiveError(X402Error):
    """
    Raised when a lookup could not reach a verdict and should be retried.

    Distinct from "not found" on purpose. The facilitator answers 404 for "this
    address owns no agent" and 503 for "I could not find out" - usually an RPC
    failure behind it. Collapsing the two is how a transient failure becomes a
    permanent wrong answer: a caller that persists "not registered" stops asking,
    and on a registration path mints a second agent for someone who already has
    one, burning gas and leaving an orphan.

    Catch this separately from the 404 and retry; never treat it as absence.
    """

    def __init__(
        self,
        message: str,
        status_code: Optional[int] = None,
        response_body: Optional[str] = None,
    ) -> None:
        super().__init__(
            message=message,
            code="LOOKUP_INCONCLUSIVE",
            details={
                "statusCode": status_code,
                "response": response_body,
                "retryable": True,
            },
        )
        self.status_code = status_code
        self.response_body = response_body
        self.retryable = True


class WriterUnavailableError(FacilitatorError):
    """
    Raised when the facilitator could not reach a verdict because no instance
    of it held the EVM writer lease.

    **This is not a rejection.** The facilitator answers 503 with
    ``Retry-After`` and a ``reason``; the credential presented is still valid
    and the correct recovery is to re-present the SAME request. A server that
    reports this to its buyer as a 402 makes them sign a second authorization
    for a payment that was never refused.

    ``safe_to_retry`` is ``False`` for ``forward_failed`` and for any ``reason``
    this SDK does not recognise: the holder may have executed the write and
    only the reply was lost. On a mint, resolve that with
    ``GET /identity/{network}/owner/{recipient}`` before re-sending anything.

    It subclasses :class:`FacilitatorError` deliberately. Every consumer written
    before this class existed catches ``FacilitatorError`` and reads
    ``status_code``; making the 503 a sibling instead of a child would have
    turned "the SDK now names this failure" into "the SDK now escapes your
    handler", which is a worse bug than the one being fixed. Existing handlers
    keep working unchanged; new ones can be specific.
    """

    def __init__(
        self,
        message: str,
        status_code: Optional[int] = None,
        response_body: Optional[str] = None,
        *,
        reason: Optional[str] = None,
        retry_after: Optional[float] = None,
    ) -> None:
        super().__init__(
            message,
            status_code=status_code,
            response_body=response_body,
            reason=reason,
            retry_after=retry_after,
        )
        self.code = "WRITER_UNAVAILABLE"
        self.safe_to_retry = write_retry_is_safe(reason)
        # A no-verdict answer is transient by definition, whatever the status
        # code turned out to be.
        self.retryable = True
        self.details["retryable"] = True
        self.details["safeToRetry"] = self.safe_to_retry
        # Always present on this class, even as None, so a consumer can read
        # them without a membership test.
        self.details["reason"] = reason
        self.details["retryAfter"] = self.retry_after


class RegistrationPendingError(X402Error):
    """
    Raised when a registration is still running after the wait elapsed.

    This is emphatically **not** a failure. The mint may still land. The job id
    is carried as an attribute rather than only in the message, because the
    correct recovery is to keep polling ``get_register_status(job_id)`` — and a
    caller that cannot reach the id without parsing a string will re-register
    instead, minting a duplicate agent. That is the exact sequence that once
    produced five duplicate mints.

    Never map this to "registration failed".
    """

    def __init__(
        self,
        job_id: str,
        last_status: str,
        timeout_seconds: float,
    ) -> None:
        super().__init__(
            message=(
                f"Registration job {job_id} still '{last_status}' after "
                f"{timeout_seconds:.0f}s. It may still complete: poll "
                f"get_register_status('{job_id}') rather than registering again."
            ),
            code="REGISTRATION_PENDING",
            details={
                "jobId": job_id,
                "lastStatus": last_status,
                "timeoutSeconds": timeout_seconds,
                "retryable": True,
            },
        )
        self.job_id = job_id
        self.last_status = last_status
        self.timeout_seconds = timeout_seconds
        self.retryable = True


class TimeoutError(X402Error):
    """
    Raised when a facilitator request times out.

    Per-network timeouts: Ethereum L1 = 900s, L2s = 90s (default).
    The SDK attempts an on-chain fallback check before raising this error.
    """

    def __init__(
        self,
        operation: str,
        timeout_seconds: float,
    ) -> None:
        super().__init__(
            message=f"{operation} timed out after {timeout_seconds}s",
            code="TIMEOUT",
            details={
                "operation": operation,
                "timeoutSeconds": timeout_seconds,
            },
        )
        self.operation = operation
        self.timeout_seconds = timeout_seconds


class PaymentExceedsMaxError(X402Error):
    """
    Raised by :meth:`X402Client.fetch` when a resource asks for more than the
    caller authorised via ``max_amount``.

    The buyer loop never pays more than the caller allowed: a 402 whose price
    exceeds ``max_amount`` raises this instead of silently signing an
    authorization. It is the seed of a spend guardrail — the caller sets the
    ceiling, the loop refuses anything above it.
    """

    def __init__(
        self,
        required: "Decimal",
        max_amount: "Decimal",
        *,
        resource: Optional[str] = None,
    ) -> None:
        msg = (
            f"Resource requires {required} but max_amount is {max_amount}"
            + (f" ({resource})" if resource else "")
        )
        super().__init__(
            message=msg,
            code="PAYMENT_EXCEEDS_MAX",
            details={
                "required": str(required),
                "maxAmount": str(max_amount),
                **({"resource": resource} if resource else {}),
            },
        )
        self.required = required
        self.max_amount = max_amount
        self.resource = resource


class NoAcceptablePaymentError(X402Error):
    """
    Raised by :meth:`X402Client.fetch` when a 402 offers no payment option the
    client can satisfy (no option under ``max_amount``, or the selector returned
    nothing).
    """

    def __init__(self, message: str, *, resource: Optional[str] = None) -> None:
        super().__init__(
            message=message,
            code="NO_ACCEPTABLE_PAYMENT",
            details={"resource": resource} if resource else {},
        )
        self.resource = resource
