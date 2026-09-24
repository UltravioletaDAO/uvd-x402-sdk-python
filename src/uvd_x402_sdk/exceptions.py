"""
Custom exceptions for the x402 SDK.

These exceptions provide clear, actionable error messages for different
failure scenarios in the payment flow.
"""

import json
import re
from decimal import Decimal
from typing import Optional, List, Dict, Any


#: How much of the facilitator's response body a :class:`PaymentSettlementError`
#: or :class:`PaymentVerificationError` keeps, in bytes of UTF-8. Enough for every
#: body x402-rs sends on these paths (a settle or verify answer, its receipt), and
#: a ceiling on what an exception carries into a log line.
MAX_ERROR_BODY_BYTES = 4096


def _bounded_body(body: Optional[str]) -> Optional[str]:
    """``body`` cut to :data:`MAX_ERROR_BODY_BYTES`, never mid-character."""
    if body is None:
        return None
    encoded = body.encode("utf-8", errors="replace")
    if len(encoded) <= MAX_ERROR_BODY_BYTES:
        return body
    return encoded[:MAX_ERROR_BODY_BYTES].decode("utf-8", errors="ignore")


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

    When the client raised it from a facilitator answer it also carries that
    answer: ``status_code`` (the HTTP status), ``error_reason`` (the
    facilitator's own ``invalidReason``, verbatim, ``None`` when it sent none)
    and ``response_body`` (the body, cut to :data:`MAX_ERROR_BODY_BYTES`). All
    three are ``None`` otherwise, and none of them enters ``to_dict()``.
    """

    def __init__(
        self,
        message: str,
        reason: Optional[str] = None,
        errors: Optional[List[str]] = None,
        receipt: Optional[Any] = None,
        *,
        status_code: Optional[int] = None,
        error_reason: Optional[str] = None,
        response_body: Optional[str] = None,
    ) -> None:
        self.receipt = receipt
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
        self.status_code = status_code
        self.error_reason = error_reason
        self.response_body = _bounded_body(response_body)


class PaymentSettlementError(X402Error):
    """
    Raised when payment settlement fails on-chain.

    Common causes:
    - Insufficient USDC balance
    - Nonce already used
    - Authorization expired
    - Network congestion/timeout

    When the client raised it from a facilitator answer it also carries that
    answer: ``status_code`` (the HTTP status: ``200`` for a ``success: false``
    or a failed re-validation), ``error_reason`` (the facilitator's own
    ``errorReason``, or ``invalidReason`` for a re-validation, verbatim, ``None``
    when it sent none; ``reason`` stays what it was, that value or the
    ``message``) and ``response_body`` (the body, cut to
    :data:`MAX_ERROR_BODY_BYTES`). All three are ``None`` otherwise, and none of
    them enters ``to_dict()``.
    """

    def __init__(
        self,
        message: str,
        network: Optional[str] = None,
        tx_hash: Optional[str] = None,
        reason: Optional[str] = None,
        receipt: Optional[Any] = None,
        *,
        status_code: Optional[int] = None,
        error_reason: Optional[str] = None,
        response_body: Optional[str] = None,
    ) -> None:
        self.receipt = receipt
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
        self.status_code = status_code
        self.error_reason = error_reason
        self.response_body = _bounded_body(response_body)


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

#: Values of ``reason`` for which the write is AMBIGUOUS: the holder may have
#: processed the write and only the response was lost. Treat exactly like a
#: timeout.
#:
#: * ``forward_unconfirmed`` (x402-rs 2.39.6+): the holder received the
#:   forwarded write and its answer was lost. ``502``, ``retryable: false``.
#: * ``forward_failed``: from 2.39.6 on, a hop that never reached the holder
#:   (``docs/settle-errors.md``, "Nothing was sent"). Up to 2.39.5 the same
#:   ``503`` + ``Retry-After: 5``, with the same body, also answered a hop the
#:   holder received and whose answer was lost. Nothing in the answer tells the
#:   two versions apart, so it stays here.
#:
#: On ``POST /register`` this must never be resolved by re-POSTing the mint.
#: Resolve it with ``GET /identity/{network}/owner/{recipient}`` first, honouring
#: that endpoint's 404-vs-503 distinction; re-POSTing an ambiguous mint is what
#: produced five duplicate agents.
WRITE_AMBIGUOUS_REASONS = frozenset({"forward_failed", "forward_unconfirmed"})

#: What the facilitator's receipt rail answers (x402-rs 2.39.0,
#: ``docs/facilitator-receipts.md``, "Replays of an admitted authorization") when
#: an authorization it already admitted comes back WITHOUT the purchase binding
#: that admitted it -- the same ``Idempotency-Key`` or the same
#: ``X-UVD-Purchase`` capability. ``/settle`` answers ``409`` with the code in
#: ``error``; ``/verify`` answers ``isValid: false`` with it in
#: ``invalidReason``. The original answer goes back only to the binding.
#:
#: * ``authorization_already_settled``: the payment is confirmed.
#: * ``authorization_in_flight``: admitted, outcome not final yet. A resend
#:   WITH the binding gets the original answer (``202 settlement_in_progress``,
#:   then the settle); without it, only the final outcome, by resending later.
#:   Transient: 503 + ``Retry-After``.
#: * ``receipt_request_conflict``: admitted for another purchase context, or
#:   under other terms.
#:
#: For a seller none of them is delivered on: the ``X-PAYMENT`` was already
#: used by another request. And none is a 402 -- a 402 tells the buyer to sign
#: again, and the first payment moved or may still move. 409 for the settled
#: and the conflicting one, 503 while in flight.
AUTHORIZATION_ALREADY_SETTLED = "authorization_already_settled"
AUTHORIZATION_IN_FLIGHT = "authorization_in_flight"
RECEIPT_REQUEST_CONFLICT = "receipt_request_conflict"
ADMITTED_AUTHORIZATION_CODES = frozenset(
    {AUTHORIZATION_ALREADY_SETTLED, AUTHORIZATION_IN_FLIGHT, RECEIPT_REQUEST_CONFLICT}
)

#: The ``error`` of the ``202`` the receipt rail answers a resend that carries
#: the admitting binding while the payment is still in flight. Not a verdict:
#: present the same request, with the same binding, again.
SETTLEMENT_IN_PROGRESS = "settlement_in_progress"

#: What a seller's own purchase binding (:mod:`uvd_x402_sdk.bindings`) decides
#: when the facilitator's answer cannot, carried as
#: :attr:`PaymentBindingError.reason`. None is a 402, which asks the buyer to
#: sign a new payment:
#:
#: * ``payment_store_unavailable``: the binding could not be read or minted, so
#:   the facilitator was not called. 503 + ``Retry-After``: present the SAME
#:   ``X-PAYMENT`` later (an earlier presentation may have charged it).
#: * ``payment_already_used``: this ``X-PAYMENT`` was first presented for
#:   another resource, and one payment buys one resource. 409, and the
#:   facilitator is not called.
#: * ``payment_presented_before``: the facilitator refused a payment this seller
#:   had already seen, so an earlier attempt may have moved it. 409.
PAYMENT_STORE_UNAVAILABLE = "payment_store_unavailable"
PAYMENT_ALREADY_USED = "payment_already_used"
PAYMENT_PRESENTED_BEFORE = "payment_presented_before"

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
    lease holder. ``False`` for :data:`WRITE_AMBIGUOUS_REASONS` and for anything
    unknown — a ``reason`` this SDK has never heard of is ambiguous by
    construction, and guessing optimistically about an unknown is how a
    duplicate mint happens.
    """
    return reason in WRITE_NOT_ATTEMPTED_REASONS


#: ``error`` tokens, without their `` (ref: …)`` suffix, with which x402-rs says
#: a settle may have left it (``docs/settle-errors.md``, "May be on chain").
#: From 2.39.6 on the first four also carry ``retryable: false``; up to 2.39.5
#: ``broadcast_uncertain`` and ``receipt_pending`` did not, which is why they are
#: named here. ``idempotency_cache_corrupt`` (``503``) is answered only when a
#: settle of this same request under this key already SUCCEEDED and its cached
#: answer cannot be read back.
SETTLE_MAY_HAVE_SENT_ERRORS = frozenset(
    {
        "settlement_unconfirmed",
        "broadcast_uncertain",
        "receipt_pending",
        "receipt_response_unreadable",
        "idempotency_cache_corrupt",
    }
)

#: The `` (ref: <uuid>)`` x402-rs appends to an opaque ``error`` token.
_REF_SUFFIX = re.compile(r"\s*\(ref: [^)]*\)\s*$")


#: The spellings the facilitator uses for a transaction hash, across endpoints
#: and error paths. The SUCCESS path of ``settle()`` already read three of
#: these; the ERROR path read one — the same defect with the consequences
#: inverted, since it is on the error path that a missed hash costs money.
_TX_HASH_KEYS = ("txHash", "tx_hash", "transaction_hash")


def body_tx_hash(body: Any) -> Optional[str]:
    """Return the transaction hash carried in a facilitator response body, if any.

    The facilitator reports the hash under several shapes depending on the
    endpoint and error path: ``{"transaction": "0x…"}``,
    ``{"transaction": {"hash": "0x…"}}``, ``{"txHash": …}``, ``{"tx_hash": …}``,
    ``{"transaction_hash": …}``.

    Returned **verbatim**. Algorand prints base32 and Solana base58;
    normalising the value makes it unpastable into an explorer, and pasting it
    is the entire remedy this SDK offers a caller it just refused to retry.
    """
    if not isinstance(body, dict):
        return None
    tx = body.get("transaction")
    if isinstance(tx, dict) and tx.get("hash"):
        return str(tx["hash"])
    if isinstance(tx, str) and tx:
        return tx
    for key in _TX_HASH_KEYS:
        if body.get(key):
            return str(body[key])
    return None


def parse_facilitator_error_body(response_body: Optional[str]) -> Dict[str, Any]:
    """Read a facilitator error body once, for every caller that needs it.

    ONE parser on purpose. Two subtly different readings of the same body is
    how one code path stops honouring the ``retryable: false`` that the other
    honours — and the two paths here decide whether money moves twice.

    Never raises and never requires a field: an unreadable or unexpected body
    yields all-``None``, because a parser that refuses an unknown shape turns a
    new facilitator diagnosis into an outage.

    Returns ``transaction``, ``payment_id``, ``error_code``, ``reason``,
    ``retryable`` and ``safe_to_retry`` (the body's ``safeToRetry``); each is
    ``None`` when the facilitator did not state it.
    """
    empty: Dict[str, Any] = {
        "transaction": None,
        "payment_id": None,
        "error_code": None,
        "reason": None,
        "retryable": None,
        "safe_to_retry": None,
    }
    if not response_body:
        return empty
    try:
        parsed = json.loads(response_body)
    except (ValueError, TypeError):
        return empty
    if not isinstance(parsed, dict):
        return empty

    payment_id = parsed.get("paymentId") or parsed.get("payment_id")
    error_code = parsed.get("error")
    reason = parsed.get("reason")
    retryable = parsed.get("retryable")
    safe_to_retry = parsed.get("safeToRetry")
    return {
        "transaction": body_tx_hash(parsed),
        "payment_id": str(payment_id) if isinstance(payment_id, str) and payment_id else None,
        "error_code": str(error_code) if isinstance(error_code, str) and error_code else None,
        "reason": reason if isinstance(reason, str) and reason else None,
        "retryable": retryable if isinstance(retryable, bool) else None,
        "safe_to_retry": safe_to_retry if isinstance(safe_to_retry, bool) else None,
    }


class FacilitatorError(X402Error):
    """
    Raised when the facilitator returns an error.

    Contains the raw error response from the facilitator for debugging.

    Carries the two fields that decide what a server should answer its buyer:

    * ``reason`` — the facilitator's own machine-readable diagnosis. On a 503
      from the EVM writer lease this is one of ``holder_unknown``,
      ``forwarding_disabled``, ``forwarded_but_not_writer``, ``body_unreadable``
      (the write never ran, retry is safe) or ``forward_failed`` (ambiguous,
      like a timeout); on a 502, ``forward_unconfirmed`` (the holder received
      the write and its answer was lost). Use :func:`write_retry_is_safe` rather
      than comparing strings, and treat an unknown value as ambiguous.
    * ``retry_after`` — the server's ``Retry-After``, in seconds, already
      clamped to :data:`MAX_RETRY_AFTER_SECONDS`.

    ``retryable`` mirrors the transient/final verdict so this class stops being
    the only one in the hierarchy without the attribute that
    ``LookupInconclusiveError`` and ``RegistrationPendingError`` already carry.
    A 5xx is transient; a 4xx other than 429 is final — **except** that the
    status is only the CEILING of the verdict and the body can lower it. See
    :meth:`_retryable_verdict`.

    And when the facilitator refuses a retry it says where to look instead:

    * ``transaction`` — the hash it broadcast before failing, verbatim.
    * ``payment_id`` — the facilitator's own identifier for the payment.
    * ``error_code`` — its machine-readable ``error``, e.g.
      ``settlement_unconfirmed``.

    All three are ``None`` when the facilitator sent none. Refusing to retry
    without handing these over rebuilds the same dead end one layer up: the
    caller is told "do not re-send" and given nothing to check, so they cannot
    find out whether their money moved.

    ``operation`` names the facilitator call that failed, ``"verify"`` or
    ``"settle"``, when the client raised it from one of them, and is ``None``
    otherwise. Not in ``to_dict()``.

    ``safe_to_retry`` says whether the facilitator stated that NOTHING WAS SENT,
    the question x402-rs answers per failure in ``docs/settle-errors.md``
    ("Nothing was sent" / "May be on chain"). Not in ``to_dict()``. Three values:

    * ``True``: the facilitator said so outright, on a ``5xx``: ``safeToRetry:
      true`` in the body (the receipt rail, x402-rs 2.39.3+), or a ``reason`` in
      :data:`WRITE_NOT_ATTEMPTED_REASONS` (the writer lease, before the hop).
      Resend the SAME request, with the same ``Idempotency-Key``. It is never a
      reason to sign a new authorization.
    * ``False``: the payment may be on chain. ``retryable: false``, a
      ``transaction`` in the body, a receipt ``pending`` or ``unknown``, an
      ``error`` in :data:`SETTLE_MAY_HAVE_SENT_ERRORS`, a ``reason`` in
      :data:`WRITE_AMBIGUOUS_REASONS`, or the receipt rail's admitted
      authorization (``202 settlement_in_progress``, the three codes of
      :data:`ADMITTED_AUTHORIZATION_CODES`). Look the transaction up; do not
      sign again. This wins over any ``True`` signal in the same body.
      ``forward_failed`` is here although x402-rs 2.39.6+ lists it under
      "Nothing was sent": up to 2.39.5 the same ``503`` + ``Retry-After: 5``,
      with the same body, also answered a hop the lease holder received.
    * ``None``: neither was stated. A refusal (fix the request), a transport
      failure with no answer, and the other rows of the "Nothing was sent"
      table: ``upstream_rpc_unavailable``, ``upstream_nonce_or_mempool``,
      ``upstream_rate_limited`` and ``facilitator_signer_unfunded``. Up to
      x402-rs 2.39.5 those same answers, with the same status and
      ``Retry-After``, also covered a transaction whose send answer was lost,
      and nothing in the answer says which version sent it. ``retryable``
      still reads them as transient, and resending the SAME authorization
      cannot move the money twice: the token's own nonce stops it.
    """

    @staticmethod
    def _safe_to_retry_verdict(
        status_code: Optional[int],
        fields: Dict[str, Any],
        reason: Optional[str],
        receipt: Any,
    ) -> Optional[bool]:
        """``True`` / ``False`` / ``None``, as the class docstring states them.

        "May be on chain" is read first and wins: a body that says both is
        treated as the one that can cost a second payment.
        """
        token = _REF_SUFFIX.sub("", fields.get("error_code") or "")
        reason = reason if reason is not None else fields.get("reason")
        if (
            fields.get("retryable") is False
            or fields.get("transaction") is not None
            or getattr(receipt, "status", None) in ("pending", "unknown")
            or token in SETTLE_MAY_HAVE_SENT_ERRORS
            or token in ADMITTED_AUTHORIZATION_CODES
            or token == SETTLEMENT_IN_PROGRESS
            or reason in WRITE_AMBIGUOUS_REASONS
        ):
            return False
        if status_code is None or status_code < 500:
            return None
        if fields.get("safe_to_retry") is True or reason in WRITE_NOT_ATTEMPTED_REASONS:
            return True
        return None

    @staticmethod
    def _retryable_verdict(
        status_code: Optional[int], fields: Dict[str, Any]
    ) -> bool:
        """The status is the CEILING; the body can only LOWER it, never raise it.

        A body claiming ``retryable: true`` on a 400 must not make this SDK
        re-send a credential the facilitator genuinely rejected. Three signals
        lower a transient verdict to final, in order of authority:

        1. an explicit ``retryable: false`` — a contract the facilitator is
           stating, not an inference;
        2. any 5xx carrying a transaction hash, **whatever the error is
           called**. A hash in a FAILURE body means the facilitator got as far
           as BROADCASTING, so re-sending risks a double-settle. This is the
           general form of the rule and covers codes that do not exist yet;
        3. nothing else. An unreadable body leaves the status verdict standing.

        Two answers below 500 are transient by name: ``202
        settlement_in_progress``, the receipt rail's answer to a resend that
        carries the binding that admitted the payment (re-sending it with that
        binding replays the admitted settle and never executes another one,
        the recovery the facilitator documents for a pending payment), and
        ``409 authorization_in_flight``, the same state for a resend without
        the binding (the same request later learns the outcome).
        """
        if status_code == 202 and fields.get("error_code") == SETTLEMENT_IN_PROGRESS:
            return fields.get("retryable") is not False
        if status_code == 409 and fields.get("error_code") == AUTHORIZATION_IN_FLIGHT:
            # Named, not read from the body: its `retryable: false` means "this
            # request never gets the success back", not "no verdict will come".
            # The same request later learns the outcome (the TypeScript SDK
            # reads it the same way).
            return True
        by_status = status_code is None or status_code == 429 or status_code >= 500
        if not by_status:
            return False
        if fields.get("retryable") is False:
            return False
        if fields.get("transaction") is not None:
            return False
        return True

    def __init__(
        self,
        message: str,
        status_code: Optional[int] = None,
        response_body: Optional[str] = None,
        *,
        reason: Optional[str] = None,
        retry_after: Optional[float] = None,
        operation: Optional[str] = None,
    ) -> None:
        fields = parse_facilitator_error_body(response_body)
        retryable = self._retryable_verdict(status_code, fields)
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
        if status_code is None or status_code == 429 or status_code >= 500 or retryable:
            # The set that used to always read True. It now reads the real
            # verdict — which is the correction — and a 4xx keeps carrying no
            # key at all, exactly as before. ``or retryable`` adds the one
            # transient 2xx (``202 settlement_in_progress``).
            details["retryable"] = retryable
        for key, detail_key in (
            ("transaction", "transaction"),
            ("payment_id", "paymentId"),
            ("error_code", "errorCode"),
        ):
            if fields[key] is not None:
                details[detail_key] = fields[key]
        super().__init__(
            message=message,
            code="FACILITATOR_ERROR",
            details=details,
        )
        self.status_code = status_code
        self.receipt = None
        try:
            from uvd_x402_sdk.receipts import parse_receipt
            self.receipt = parse_receipt(json.loads(response_body or "{}").get("receipt"))
        except (ValueError, TypeError, AttributeError):
            pass
        self.response_body = response_body
        self.reason = reason
        self.retry_after = retry_after
        self.operation = operation
        self.retryable = retryable
        self.transaction = fields["transaction"]
        self.payment_id = fields["payment_id"]
        self.error_code = fields["error_code"]
        self.safe_to_retry = self._safe_to_retry_verdict(
            status_code, fields, reason, self.receipt
        )


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


class WriterUnavailableError(X402Error):
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
            message=message,
            code="WRITER_UNAVAILABLE",
            details={
                "statusCode": status_code,
                "response": response_body,
                "reason": reason,
                "retryAfter": retry_after,
                "retryable": True,
                "safeToRetry": write_retry_is_safe(reason),
            },
        )
        self.status_code = status_code
        self.response_body = response_body
        self.reason = reason
        self.retry_after = retry_after
        self.retryable = True
        self.safe_to_retry = write_retry_is_safe(reason)


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


class PaymentBindingError(X402Error):
    """
    Raised by :func:`~uvd_x402_sdk.bindings.process_payment_bound` when the
    seller's purchase binding decides the request instead of the facilitator.

    :attr:`reason` is one of :data:`PAYMENT_STORE_UNAVAILABLE`,
    :data:`PAYMENT_ALREADY_USED` or :data:`PAYMENT_PRESENTED_BEFORE`. For the
    last one, :attr:`cause` is the facilitator's refusal (also in
    ``details["cause"]``) and :attr:`receipt` its receipt, if it sent one.
    ``details["retryable"]`` is true only for the store that could not answer,
    so :func:`~uvd_x402_sdk.client.is_transient_error` calls that one transient.
    The integrations answer all three through the same mapping as every other
    undelivered payment: 503 for the first, 409 for the other two, never 402.
    """

    def __init__(
        self,
        reason: str,
        message: str,
        *,
        cause: Optional[X402Error] = None,
    ) -> None:
        details: Dict[str, Any] = {
            "reason": reason,
            "retryable": reason == PAYMENT_STORE_UNAVAILABLE,
        }
        if cause is not None:
            details["cause"] = cause.to_dict()
        super().__init__(message=message, code=reason.upper(), details=details)
        self.reason = reason
        self.cause = cause
        self.receipt = getattr(cause, "receipt", None)


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


class PolicyRefusedError(NoAcceptablePaymentError):
    """
    Raised by :meth:`X402Client.fetch` when the caller's own
    :class:`~uvd_x402_sdk.policy.PurchasePolicy` will not pay for the 402 it was
    handed — **before anything is signed**.

    Carries the concrete cause. Branch on :attr:`refusal_code`, which is one of
    the six kebab codes of the contract (``no-readable-offer``,
    ``offer-expired``, ``recipient-not-permitted``, ``asset-not-budgeted``,
    ``per-payment-limit``, ``cumulative-limit``) — a closed vocabulary, so a
    caller can branch without parsing English. The numbers that caused it are in
    :attr:`details` and on the :attr:`refusal` itself.

    It subclasses :class:`NoAcceptablePaymentError` so that code written before
    0.82.0 — which caught that when a 402 offered nothing payable — keeps
    catching this. ``exc.code`` is the SDK's own error code (``POLICY_REFUSED``)
    and is NOT the contract code; that one is ``exc.refusal_code``.
    """

    def __init__(
        self,
        refusal: Any,
        *,
        resource: Optional[str] = None,
    ) -> None:
        details = dict(refusal.to_dict())
        if resource:
            details["resource"] = resource
        # Skips NoAcceptablePaymentError.__init__ on purpose: this carries its
        # own code and the refusal's numbers, not a bare message.
        X402Error.__init__(
            self,
            message=f"payment refused by policy: {refusal.message}",
            code="POLICY_REFUSED",
            details=details,
        )
        self.refusal = refusal
        self.resource = resource

    @property
    def refusal_code(self) -> str:
        """The contract's kebab code for this refusal."""
        return self.refusal.code
