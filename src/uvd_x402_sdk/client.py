"""
Main x402 client for payment processing.

This module provides the X402Client class which handles:
- Parsing X-PAYMENT headers
- Verifying payments with the facilitator
- Settling payments on-chain
- Error handling with clear messages
"""

import base64
import hashlib
import json
import logging
import os
import re
import secrets
import time
from collections.abc import Mapping
from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Optional, Tuple, List, Dict, Any, Union

import httpx

from uvd_x402_sdk.config import X402Config
from uvd_x402_sdk.envelope import (
    build_settle_request_for_version,
    build_verify_request_for_version,
    resolve_envelope_version,
)
from uvd_x402_sdk.exceptions import (
    ADMITTED_AUTHORIZATION_CODES,
    AUTHORIZATION_ALREADY_SETTLED,
    AUTHORIZATION_IN_FLIGHT,
    PAYMENT_STORE_UNAVAILABLE,
    SETTLEMENT_IN_PROGRESS,
    X402Error,
    InvalidPayloadError,
    PaymentBindingError,
    PaymentVerificationError,
    PaymentSettlementError,
    UnsupportedNetworkError,
    FacilitatorError,
    TimeoutError as X402TimeoutError,
    PaymentExceedsMaxError,
    NoAcceptablePaymentError,
    PolicyRefusedError,
    MAX_RETRY_AFTER_SECONDS,
    _REF_SUFFIX,
    body_tx_hash,
    parse_facilitator_error_body,
    parse_retry_after,
    write_retry_is_safe,
)
from uvd_x402_sdk.models import (
    PaymentPayload,
    PaymentRequirements,
    PaymentResult,
    VerifyResponse,
    SettleResponse,
)
from uvd_x402_sdk.receipts import payment_response_headers
from uvd_x402_sdk.stack_key import stack_key_headers, stack_key_request_kwargs
from uvd_x402_sdk.policy import (
    AdvertisedQuote,
    Offer,
    ParsedAccepts,
    PolicyRefusal,
    PurchasePolicy,
    canonical_address,
    no_readable_offer,
    offer_valid_until,
    parse_accepts,
)
from uvd_x402_sdk.networks import (
    get_network,
    NetworkType,
    get_supported_network_names,
    normalize_network,
    is_caip2_format,
    parse_caip2_network,
)
from uvd_x402_sdk.networks.base import TokenConfig, get_token_config, to_base_units

logger = logging.getLogger(__name__)


# =============================================================================
# Settle Retry Policy (opt-in via settle_payment(..., retry=True))
# =============================================================================
#
# Ported from Execution Market's facilitator retry policy
# (mcp_server/integrations/_http_retry.py). The rules:
#
#   * Retry transient transport failures (timeout / network / protocol) and
#     5xx responses — up to SETTLE_RETRY_ATTEMPTS with exponential backoff.
#   * NEVER retry a 4xx. Those are deterministic (bad request, auth,
#     idempotency conflict); retrying only amplifies the error.
#   * NEVER retry a business-level failure inside a 2xx (success=false).
#   * NEVER retry a 5xx whose body already carries a transaction hash. The
#     facilitator can respond 5xx AFTER broadcasting the tx (e.g. a non-fatal
#     post-settle hook failed) — retrying in that state risks a DOUBLE-SETTLE.

SETTLE_RETRY_ATTEMPTS = 3
_SETTLE_RETRY_MAX_BACKOFF_SECONDS = 10.0

#: How long the timeout fallback keeps asking about a settle the facilitator
#: answers ``202 settlement_in_progress`` under this handling's own binding: the
#: settle that timed out was admitted and is still in flight. Within it the same
#: request ends in its settle; past it the 202 itself is raised (transient: a
#: paywall answers 503 + Retry-After, never 402), and only this binding's resend
#: gets the answer later.
SETTLE_IN_FLIGHT_POLL_SECONDS = 30.0
#: Ceiling on the pause between two of those asks. The facilitator's own
#: guidance (``Retry-After``, or the receipt's ``retry.afterSeconds``) wins when
#: it is shorter; without any, one second.
_IN_FLIGHT_POLL_MAX_INTERVAL_SECONDS = 5.0
_IN_FLIGHT_POLL_DEFAULT_INTERVAL_SECONDS = 1.0


# =============================================================================
# Idempotency-Key: the purchase binding (X402Config.send_idempotency_key)
# =============================================================================
#
# On by default since 0.89.0. Every payment handling carries ONE key: the same
# value on `/verify`, on `/settle`, on the settle's retries and on the timeout
# fallback's resend. The facilitator's receipt rail gives an admitted payment's
# answer back only to the binding that admitted it, so this key is what lets a
# seller recover its own lost settle answer.
#
# The key is RANDOM (`new_idempotency_key()`) unless the caller brings one:
# `idempotency_key` (a key the caller created and stored with the order, to
# resume after a restart) or `idempotency_scope` (derived with
# `derive_idempotency_key`, a binding only while the scope is a secret of the
# seller). A key derived from the X-PAYMENT alone is not a binding: whoever
# holds the payment recomputes it, and a buyer resending their own X-PAYMENT in
# a NEW request would make the seller send the same key and get the original
# settle back, the replay x402-rs 2.39.0 closes. 0.83.1 to 0.88.0 sent a key
# only with a scope; 0.83.0 sent the unscoped derived key.
#
# What the facilitator does with the header on networks WITHOUT receipts,
# measured on x402-rs 2.28.0 (`src/handlers.rs`, `post_settle`, and its
# `settle_idempotency_tests`):
#
#   * it hashes the RAW request body (sha256, no JSON re-encoding);
#   * same key + same hash -> the cached response, 200 with
#     `Idempotent-Replayed: true`, and nothing new executes;
#   * same key + different hash -> 409 `idempotency_key_conflict`;
#   * only a SUCCESSFUL settle is cached, so a failure never locks a retry out;
#   * a store it cannot read -> 503 `idempotency_store_unavailable`, and it does
#     NOT settle (fail-closed). That is no verdict: present the same credential
#     later. `send_idempotency_key=False` removes the dependency;
#   * `/verify` ignores the header.
#
# On networks WITH receipts (x402-rs 2.39.0, `docs/facilitator-receipts.md`,
# "Replays of an admitted authorization"; Arc and native Hedera today) an
# admitted authorization gets its original answer back only with the binding
# that admitted it, this key or the same `X-UVD-Purchase`:
#
#   * `/settle` with the binding -> the original status and body with
#     `Idempotent-Replayed: true`, or `202 settlement_in_progress` in flight;
#   * `/verify` with the binding -> the stored verdict and receipt;
#   * without it -> `409 authorization_already_settled` (confirmed) or
#     `409 authorization_in_flight`, and `/verify` answers `isValid: false`
#     with the same reason. Another purchase context: `409
#     receipt_request_conflict`. A rejected payment replays its rejection.
#
# Facilitators before 2.39.0 did not tie the replay to the binding. A handling
# that brought no binding of its own (a fresh key, no `X-UVD-Purchase`) refuses
# a replay that reaches it before any attempt of its own could have admitted
# the payment (see `_Binding`), whatever the facilitator's version.

#: The header the facilitator deduplicates a settle on.
IDEMPOTENCY_KEY_HEADER = "Idempotency-Key"

_IDEMPOTENCY_OPERATIONS = ("verify", "settle")

#: Leads the material of a scoped key. The unscoped material is the signed
#: block, always a JSON object; the scoped one is a JSON array opened by this
#: tag, so the two can never serialise to the same text.
_SCOPED_KEY_TAG = "x402-idempotency-scope/1"

#: Leads every derived key. The name of the operation that has always admitted
#: the payment, kept verbatim so keys derived before 0.89.0 still bind their
#: purchase.
_PAYMENT_KEY_PREFIX = "x402-settle-"

#: What the facilitator refuses as a caller key (``400
#: reserved_idempotency_key``), and the shape a header and its store can carry.
_RESERVED_KEY_PREFIX = "receipt:"
_KEY_SHAPE = re.compile(r"[\x21-\x7e]{1,255}")


def new_idempotency_key() -> str:
    """A new, unguessable ``Idempotency-Key`` for ONE payment: ``x402-<64 hex>``.

    Send it on that payment's ``/verify`` and ``/settle`` and every retry of
    either (``idempotency_key=``); after a lost response it is what earns the
    facilitator's original answer back. Random on purpose: a key derived from
    the ``X-PAYMENT`` is known to whoever holds the payment, and holding the
    payment is exactly what the facilitator refuses to accept as a binding.
    Store it with the order to resume that payment after a restart. Same shape
    as ``createIdempotencyKey()`` in the TypeScript SDK. Merchant-private:
    never hand it to the buyer.
    """
    return "x402-" + secrets.token_hex(32)


def _checked_idempotency_key(key: Any) -> str:
    """The caller's key, or TypeError / ValueError before anything is sent."""
    if not isinstance(key, str):
        raise TypeError(f"idempotency_key must be a string, got {type(key).__name__}")
    if not _KEY_SHAPE.fullmatch(key) or key.startswith(_RESERVED_KEY_PREFIX):
        raise ValueError(
            'idempotency_key must be 1-255 visible ASCII characters and must not '
            'start with "receipt:"'
        )
    return key


def derive_idempotency_key(
    payload: PaymentPayload, operation: str, scope: Optional[str] = None
) -> Optional[str]:
    """A key derived from ``payload`` and ``scope``: ``x402-settle-<sha256 hex>``.

    **Not a purchase binding unless ``scope`` is a secret of the seller** (an
    order id the seller generates and stores with the purchase, which the buyer
    does not know and cannot guess). Whoever holds the ``X-PAYMENT`` and the
    scope recomputes this key. Without a scope, or with one the buyer controls
    or guesses, a buyer resending their own ``X-PAYMENT`` in a NEW request makes
    the seller send the same key and get the original settle back: the replay
    the facilitator's receipt rail refuses to a bare payment. :class:`X402Client`
    sends it only when the caller passes ``idempotency_scope``; by default it
    sends :func:`new_idempotency_key`. Public for compatibility.

    Without ``scope``, over the signed ``payload`` block serialised with sorted
    keys and no whitespace: the 0.83.0 settle key, unchanged. With ``scope``,
    over the JSON array ``["x402-idempotency-scope/1", <that block>, <scope>]``
    serialised the same way. Non-ASCII text is hashed as its UTF-8 bytes, never
    as JSON escapes. The same authorization and the same scope give the same
    key in any process and on any run, so a seller that restarted and settles
    the same purchase again lands on the facilitator's stored settle instead of
    executing it again.

    * **The scope names the purchase, and only the seller knows it.** A key
      derived from the signed block alone does not tell two purchases of the
      same price apart: their settle requests are byte-identical. One value
      per purchase, stable across that purchase's retries, never shared by two.
      The scope is generated by the seller and stored with the purchase, never
      a value taken from the request.
    * **The two forms cannot meet.** The block is a JSON object, so an unscoped
      key hashes text that opens with ``{``; a scoped key hashes an array that
      opens with ``[`` and a fixed tag. No block, whatever its shape, derives
      the key of a scoped call. 0.83.1 hashed an object for scoped keys too, so
      a scoped key from 0.83.1 differs from the one derived now.
    * **One key per payment, not per operation** (since 0.89.0; 0.83.0 to
      0.88.0 sent ``x402-verify-...`` on ``/verify``). The facilitator's
      receipt rail requires the same key on ``/verify`` and on ``/settle``: a
      key that differs per operation binds only the settle, so the ``/verify``
      of a retried purchase answered ``isValid: false``
      (``authorization_already_settled``) for a payment that had settled.
      ``operation`` is still validated and no longer changes the value. The
      value is the settle key those releases already sent, so a purchase whose
      settle went out before an upgrade keeps its key after it. ``/verify``
      ignores the key on networks without receipts, so nothing caches a
      verify under it.
    * **The signed block and the scope, not the requirements.** A settle of the
      same authorization for the same purchase under different terms must
      reuse the key, so the facilitator refuses it (``409``) instead of running
      it.
    * **Not a secret.** The store is one namespace shared by every caller of the
      facilitator, and the key is computable by whoever holds the ``X-PAYMENT``
      and the scope. Whoever has both before the seller settles (a proxy, a
      log) can settle an authorization of THEIR OWN under this key first --
      another body, one micro-payment -- and the legitimate settle then gets
      ``409 idempotency_key_conflict`` for 24 hours, which
      :func:`spent_nonce_evidence` reads as "already settled" while the real
      authorization stays unpaid: check it on-chain before delivering. A scope
      nobody else can guess (a random order id, not a sequential one) keeps the
      key out of their reach; closing it for good belongs to the facilitator
      (scope the key by payer or ``payTo``, or bind it to the body).
    * ``None`` for an empty block: every empty payload would share one key.

    It does NOT catch a buyer who signs a NEW authorization for the same
    purchase: different block, different key, a second settlement. The scope
    does not change that, because the block still enters the key.

    Raises:
        ValueError: If ``operation`` is not ``"verify"`` or ``"settle"``, or
            ``scope`` is an empty or blank string.
        TypeError: If ``scope`` is neither ``None`` nor a string.
    """
    if operation not in _IDEMPOTENCY_OPERATIONS:
        raise ValueError(
            f"operation must be one of {_IDEMPOTENCY_OPERATIONS}, got {operation!r}"
        )
    if scope is not None:
        if not isinstance(scope, str):
            raise TypeError(f"scope must be a string, got {type(scope).__name__}")
        if not scope.strip():
            raise ValueError("scope must not be empty or blank")
    signed = payload.payload
    if not signed:
        return None
    material: Any = signed if scope is None else [_SCOPED_KEY_TAG, signed, scope]
    canonical = json.dumps(material, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return f"{_PAYMENT_KEY_PREFIX}{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"


#: The one reader of a facilitator error body, shared with the verdict
#: :class:`FacilitatorError` reaches in its own constructor. Two subtly
#: different readings of the same body is how one code path stops honouring the
#: ``retryable: false`` that the other honours.
_extract_tx_hash_from_body = body_tx_hash


def _facilitator_error_tx_hash(exc: FacilitatorError) -> Optional[str]:
    """The tx hash a FacilitatorError carries, if the facilitator sent one."""
    return exc.transaction


def _facilitator_reason(body: Optional[str]) -> Optional[str]:
    """Pull the facilitator's ``reason`` out of an error body, tolerantly.

    The 503 the EVM writer lease emits carries ``{"error": ..., "reason": ...}``.
    Parsed with the same discipline as :func:`_extract_tx_hash_from_body`: no
    model, no required fields, no exception. A ``reason`` value this SDK has
    never seen must reach the caller untouched rather than raise here — the
    facilitator is free to add one, and a parser that refuses unknown values
    turns a new diagnosis into an outage.
    """
    if not body:
        return None
    try:
        parsed = json.loads(body)
    except (ValueError, TypeError):
        return None
    if not isinstance(parsed, dict):
        return None
    reason = parsed.get("reason")
    return str(reason) if isinstance(reason, str) and reason else None


def _response_retry_after(response: Any) -> Optional[float]:
    """Read ``Retry-After`` off an httpx response, clamped. Never raises."""
    try:
        return parse_retry_after(response.headers.get("retry-after"))
    except Exception:  # noqa: BLE001 - a header read must not break error handling
        return None


def _response_text(response: Any) -> Optional[str]:
    """The body of an httpx response as text, or ``None``. Never raises."""
    try:
        text = response.text
    except Exception:  # noqa: BLE001 - a body read must not break error handling
        return None
    return text if isinstance(text, str) else None


#: The header with which the facilitator marks an answer served from a payment
#: it had already admitted (or cached) instead of executed.
IDEMPOTENT_REPLAYED_HEADER = "Idempotent-Replayed"


def _response_replayed(response: Any) -> bool:
    """Does this facilitator response carry ``Idempotent-Replayed: true``? Never raises.

    Read off the headers only: a body saying so is not the facilitator's word.
    """
    try:
        headers = response.headers
        value = headers.get(IDEMPOTENT_REPLAYED_HEADER)
        if value is None and isinstance(headers, dict):
            wanted = IDEMPOTENT_REPLAYED_HEADER.lower()
            value = next((v for k, v in headers.items() if str(k).lower() == wanted), None)
    except Exception:  # noqa: BLE001 - a header read must not break a settle
        return False
    return isinstance(value, str) and value.strip().lower() == "true"


@dataclass
class _Binding:
    """The purchase binding of ONE payment handling.

    ``key`` is what its ``/verify``, its ``/settle``, the settle's retries and
    the timeout fallback all carry, and ``receipt_context`` the buyer's
    ``X-UVD-Purchase``. ``brought``: the key came from the caller
    (``idempotency_key`` or ``idempotency_scope``), so an earlier handling may
    have admitted the payment under it. ``may_have_admitted``: an attempt of
    THIS handling ended without a verdict (timeout, transport error, 5xx) and
    may have admitted the payment.
    """

    key: Optional[str]
    receipt_context: Optional[str]
    brought: bool
    may_have_admitted: bool = False

    def headers(self) -> Dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.receipt_context is not None:
            headers["X-UVD-Purchase"] = self.receipt_context
        if self.key is not None:
            headers[IDEMPOTENCY_KEY_HEADER] = self.key
        return headers

    def refuse_foreign_replay(self, response: Any, receipt: Any, network: str) -> None:
        """Raise when ``response`` replays a payment this handling did not admit.

        A handling with no binding of its own (a fresh key or none, no
        ``X-UVD-Purchase``) cannot have admitted the payment before one of its
        attempts ended without a verdict. A replay reaching it earlier was
        earned by possession of the ``X-PAYMENT`` alone: another request's
        purchase, handed to this one by a facilitator before 2.39.0 (2.39.0
        answers it ``409``). Refused the way 2.39.0 refuses it, with the
        receipt, so no caller delivers on it. Independent of how the key was
        made. A replayed rejection is still the original rejection and is not
        this method's business.
        """
        if self.brought or self.receipt_context is not None or self.may_have_admitted:
            return
        if not _response_replayed(response):
            return
        in_flight = response.status_code == 202 or (
            receipt is not None and getattr(receipt, "status", None) in ("pending", "unknown")
        )
        raise PaymentSettlementError(
            message=(
                "The facilitator replayed a payment this request did not admit: the "
                "X-PAYMENT was already used by another request"
                + (" and is still in flight" if in_flight else "")
                + ", so it is not delivered on"
            ),
            network=network,
            reason=AUTHORIZATION_IN_FLIGHT if in_flight else AUTHORIZATION_ALREADY_SETTLED,
            receipt=receipt,
            status_code=getattr(response, "status_code", None),
            response_body=_response_text(response),
        )


def _in_flight_poll_interval(refusal: FacilitatorError) -> float:
    """Seconds to wait before asking again about a settle still in flight."""
    wait = refusal.retry_after
    if wait is None:
        retry = getattr(refusal.receipt, "retry", None)
        after = retry.get("afterSeconds") if isinstance(retry, dict) else None
        if isinstance(after, (int, float)) and not isinstance(after, bool) and after > 0:
            wait = float(after)
    if wait is None:
        wait = _IN_FLIGHT_POLL_DEFAULT_INTERVAL_SECONDS
    return min(wait, _IN_FLIGHT_POLL_MAX_INTERVAL_SECONDS)


def retry_after_seconds(exc: Exception, default: Optional[float] = None) -> Optional[float]:
    """The ``Retry-After`` the facilitator asked for, in seconds, or ``default``.

    Already clamped to ``MAX_RETRY_AFTER_SECONDS``: a server answering
    ``Retry-After: 3600`` gets to say "later", not to hold a request open for an
    hour.
    """
    value = getattr(exc, "retry_after", None)
    if value is None:
        details = getattr(exc, "details", None) or {}
        value = details.get("retryAfter")
    parsed = parse_retry_after(value)
    return parsed if parsed is not None else default


def facilitator_reason(exc: Exception) -> Optional[str]:
    """The facilitator's machine-readable ``reason`` for a failure, if it sent one."""
    value = getattr(exc, "reason", None)
    if isinstance(value, str) and value:
        return value
    details = getattr(exc, "details", None) or {}
    value = details.get("reason")
    return value if isinstance(value, str) and value else None


def _is_retryable_settle_error(exc: Exception) -> bool:
    """Return True if a failed settle attempt is safe to retry.

    See the policy block above. The anti-double-settle guard lives in
    :meth:`FacilitatorError._retryable_verdict` now, so every path that raises
    one — settle, verify, escrow, ERC-8004 — reaches the same verdict by
    construction instead of each re-deriving it.
    """
    if isinstance(exc, X402TimeoutError):
        # The facilitator is idempotent per EIP-3009 nonce, and the SDK's
        # on-chain fallback check already ran before this was raised.
        return True
    if isinstance(exc, FacilitatorError):
        if exc.status_code is None:
            # Wrapped httpx.RequestError — transient transport issue.
            return True
        if exc.status_code == 202:
            # `settlement_in_progress`: the facilitator replays the admitted
            # settle to the binding this attempt carried, and never executes a
            # second one. See FacilitatorError._retryable_verdict.
            return exc.retryable
        # A 429 is transient for a paywall deciding 402-vs-503, but this loop
        # has never re-POSTed one and re-POSTing is what costs money. Unchanged.
        if exc.status_code < 500:
            return False
        if not exc.retryable:
            logger.warning(
                "Facilitator returned %d and the body says not to re-send "
                "(tx=%s, paymentId=%s, error=%s) — not retrying to avoid "
                "double-settle. Check the chain, do not pay again.",
                exc.status_code, exc.transaction, exc.payment_id, exc.error_code,
            )
            return False
        return True
    # PaymentSettlementError and everything else: business errors, not transient.
    return False


def is_transient_error(exc: Exception, *, anti_double_settle: bool = True) -> bool:
    """Public verdict: is this payment-path failure transient (retry-later) or final?

    The question every x402 SERVER has to answer when the facilitator
    misbehaves: does this failure mean "the payment was rejected" (answer 402,
    the client must sign a new authorization) or "I could not find out"
    (answer 503, the client must retry the SAME credential — never pay twice
    for one unknown)? Getting it wrong in either direction costs someone
    money. Until now every consumer wrote its own classifier: describe.net
    carried `paywall._is_transient` for three SDK generations, and this SDK
    kept its own private `_is_retryable_settle_error` with a guard the
    consumers' copies lacked. This merges both criteria in one public place.

    The matrix:

    * ``X402TimeoutError`` -> transient. The facilitator is idempotent per
      EIP-3009 nonce; re-presenting the same credential is safe.
    * ``FacilitatorError`` with ``status_code`` None (wrapped transport
      error), 429, or >=500 -> transient — EXCEPT a 5xx whose body already
      carries a transaction hash when ``anti_double_settle`` is True (the
      default): the facilitator can fail AFTER broadcasting, and treating
      that as retryable risks a double-settle. That guard lived only in the
      private settle path until now.
    * A 5xx whose body states ``"retryable": false`` -> final, and
      ``anti_double_settle=False`` does NOT lift that. The opt-out exists to
      let a caller own the risk of an INFERENCE the SDK drew from a hash; it
      is not a licence to contradict a facilitator that said so outright.
    * ``202 settlement_in_progress`` -> transient. The receipt rail's answer to
      a resend that carries the binding that admitted the payment: present
      the same request with the same binding again (503, never 402).
    * ``authorization_in_flight`` (the receipt rail's ``409``, ``/verify``'s
      ``invalidReason``) -> transient: the outcome is not final, so 503 and
      the same request later, never a new signature.
    * ``authorization_already_settled`` and ``receipt_request_conflict`` ->
      final: the same request never gets a success back. Not a rejection
      either; :func:`admitted_authorization_code` names them, and a seller
      answers 409 (:func:`payment_conflict_response`).
    * Any other ``X402Error`` -> respects ``details["retryable"]`` when the
      raiser set it; otherwise final.
    * Non-x402 exceptions -> final (this function judges the payment path,
      not the world).
    """
    if isinstance(exc, X402TimeoutError):
        return True
    if admitted_authorization_code(exc) == AUTHORIZATION_IN_FLIGHT:
        return True
    if isinstance(exc, FacilitatorError):
        if exc.status_code is None:
            return True
        if exc.status_code == 429:
            return True
        if exc.status_code >= 500:
            if not anti_double_settle:
                # Same verdict with the hash signal disarmed: an explicit
                # ``retryable: false`` still stands.
                return FacilitatorError._retryable_verdict(
                    exc.status_code,
                    {"retryable": parse_facilitator_error_body(exc.response_body)["retryable"]},
                )
            return exc.retryable
        if exc.status_code == 202:
            return exc.retryable
        return False
    if isinstance(exc, X402Error):
        details = getattr(exc, "details", None) or {}
        return bool(details.get("retryable", False))
    return False


# =============================================================================
# Spent-nonce classification
# =============================================================================
#
# Ported from tarotof's paywall (api/main.py, `_codigo_de_nonce_gastado` and
# `_huele_a_nonce_gastado`). What it can see depends on the facilitator. On EVM
# the UVD facilitator returns a used EIP-3009 authorization as an opaque
# `400 contract_call_failed (ref: ...)` (x402-rs `handlers.rs`, ContractCall
# arm, which withholds revert reasons on purpose), so neither a code nor the
# wording names the nonce there. What does reach it: other facilitators, the
# nonce-store rejections of non-EVM chains, `409 idempotency_key_conflict`, and
# the receipt rail's answers to an admitted authorization resent without the
# binding that admitted it (`authorization_already_settled`,
# `receipt_request_conflict`; `authorization_in_flight` is transient instead.
# See `admitted_authorization_code`).

#: Codes with which a facilitator says "this authorization was already used",
#: compared NORMALISED (lowercase, separators stripped) against the values of
#: code-bearing fields, never against free text.
_SPENT_NONCE_CODES = frozenset(
    {
        "nonceused",
        "noncealreadyused",
        "noncespent",
        "nonceconsumed",
        "alreadyused",
        "alreadysettled",
        "alreadyprocessed",
        "duplicatenonce",
        "duplicatepayment",
        "authorizationused",
        "authorizationalreadyused",
        # Not a nonce code, and here on purpose: x402-rs caches only a
        # SUCCESSFUL settle under an Idempotency-Key, so a conflict means a
        # settle under that key already succeeded with another body. Under the
        # key derive_idempotency_key() sends, that is this authorization.
        "idempotencykeyconflict",
        # Same reasoning: x402-rs answers `503 idempotency_cache_corrupt` only
        # when the record under this key carries the SAME body hash -- a settle
        # of this exact request that succeeded -- and cannot be parsed back.
        "idempotencycachecorrupt",
        # The receipt rail (x402-rs 2.39.0) on an authorization it already
        # admitted, resent without the binding that admitted it: settled, or
        # admitted for another purchase. Neither is a rejection. Its third
        # answer, `authorization_in_flight`, is not here: the outcome is not
        # final, and is_transient_error() reads it (503, the same request later).
        "authorizationalreadysettled",
        "receiptrequestconflict",
    }
)
_SPENT_NONCE_CODE_FIELDS = ("code", "errorCode", "error_code", "error", "reason", "status")
_SPENT_NONCE_PHRASES = (
    "already used",
    "already settled",
    "already processed",
    # USDC's own revert for a used or cancelled EIP-3009 authorization
    # (FiatTokenV2: "authorization is used or canceled").
    "authorization is used",
)
_SPENT_NONCE_STEMS = ("used", "spent", "consumed", "duplicate", "replay")
#: Whole words that contain a spent stem without meaning it, removed before the
#: substring match. The ONLY place this reads less than tarotof, each one
#: measured: "refused" is in the facilitator's transient NonceOrMempool message,
#: "unused" says the opposite.
_NOT_SPENT_WORDS = re.compile(r"\b(?:refused|unused)\b")


def _normalised_code(value: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value).lower())


def _spent_nonce_code(data: Any) -> Optional[str]:
    """A spent-nonce code anywhere in ``data`` (nested dicts and lists), verbatim."""
    pending = [data]
    while pending:
        current = pending.pop()
        if isinstance(current, list):
            pending.extend(current)
            continue
        if not isinstance(current, dict):
            continue
        for field in _SPENT_NONCE_CODE_FIELDS:
            value = current.get(field)
            if value is not None and _normalised_code(value) in _SPENT_NONCE_CODES:
                return str(value)
        pending.extend(v for v in current.values() if isinstance(v, (dict, list)))
    return None


def _mentions_spent_nonce(text: Optional[str]) -> bool:
    """Free text saying the authorization was used.

    tarotof's SUBSTRING match, kept on purpose: it reads ``NonceAlreadyUsed``,
    ``NonceReused { .. }`` and ``nonce_used`` inside prose and structs. A
    whole-word match (this function's first cut) missed all three -- the
    expensive direction, a 402 over a payment that already moved -- so this
    must read at least everything tarotof reads. The one change is subtractive
    and named: ``_NOT_SPENT_WORDS`` go first, because "refused" contains "used"
    and the facilitator's transient "the node refused this transaction on nonce
    or mempool grounds and never queued it" read as a spent authorization.

    Known false positives, kept because they cost a lookup and not a payment: a
    node's own "nonce has already been used" (the facilitator's signer nonce),
    negations ("no nonce consumed", "the nonce was not used") and a body that
    echoes a ``nonce`` field next to ``gas_used``.
    """
    lowered = _NOT_SPENT_WORDS.sub(" ", (text or "").lower())
    if any(phrase in lowered for phrase in _SPENT_NONCE_PHRASES):
        return True
    return "nonce" in lowered and any(stem in lowered for stem in _SPENT_NONCE_STEMS)


def spent_nonce_evidence(exc: Exception) -> Optional[str]:
    """How this failure says the authorization was ALREADY USED, if it does.

    ``"structured"`` when a code-bearing field says so (``code``, ``errorCode``,
    ``error``, ``reason``... anywhere in the exception's ``details`` or in the
    facilitator's JSON body), ``"wording"`` when only the free text does, and
    ``None`` otherwise, including for any exception outside the payment path.

    The question behind it: a 402 tells the buyer "sign a new authorization",
    and said over one that already settled, that is how a buyer pays twice. So
    the text heuristic leans to the false positive ("check, you may have paid
    already" costs a lookup; "pay again" costs money), and a code wins over the
    wording, because a code is a contract and a message is prose.

    Where it sits among the other verdicts, first match wins:

    1. a transaction hash on the error (``FacilitatorError.transaction``,
       ``PaymentSettlementError.tx_hash``): the facilitator broadcast;
    2. this function: the authorization was already used, so answer "may be
       settled, check before paying again", never a 402;
    3. :func:`is_transient_error`: no verdict, retry the SAME credential (503);
    4. otherwise final: the payment was rejected (402).
    """
    if not isinstance(exc, X402Error):
        return None
    if _spent_nonce_code_of(exc) is not None:
        return "structured"
    body_text = _classified_body(exc)
    reason = getattr(exc, "reason", None)
    text = " ".join(
        part for part in (exc.message, reason, body_text) if isinstance(part, str) and part
    )
    return "wording" if _mentions_spent_nonce(text) else None


def _classified_body(exc: X402Error) -> Any:
    """The facilitator body the spent-nonce classification reads.

    Not the one a :class:`PaymentSettlementError` or
    :class:`PaymentVerificationError` carries since it has one: that is a whole
    settle or verify answer (payer, network, receipt) whose verdict already
    reached ``reason``, and scanning its free text would move a rejection that
    answered ``402`` into ``409``.
    """
    if isinstance(exc, (PaymentSettlementError, PaymentVerificationError)):
        return None
    return getattr(exc, "response_body", None)


def _spent_nonce_code_of(exc: X402Error) -> Optional[str]:
    """The spent-nonce code in ``exc``'s details or facilitator JSON body, verbatim."""
    body_text = _classified_body(exc)
    try:
        body = json.loads(body_text) if body_text else None
    except (ValueError, TypeError):
        body = None
    return _spent_nonce_code(exc.details) or _spent_nonce_code(body)


def is_spent_nonce_error(exc: Exception) -> bool:
    """Does this payment-path failure say the authorization was already used?

    See :func:`spent_nonce_evidence`, which also says how it knows.
    """
    return spent_nonce_evidence(exc) is not None


def admitted_authorization_code(exc: Exception) -> Optional[str]:
    """The receipt rail's code, when this failure says the authorization was
    already admitted for a purchase this request did not bind.

    One of :data:`~uvd_x402_sdk.exceptions.ADMITTED_AUTHORIZATION_CODES`, read
    from the facilitator's ``error`` on a ``/settle`` failure
    (:class:`FacilitatorError`, a ``409``) or from ``invalidReason`` on a
    ``/verify`` (:class:`PaymentVerificationError`), and ``None`` otherwise:

    * ``authorization_already_settled``: the payment is confirmed. The
      receipt on the exception (``exc.receipt``) proves it to whoever holds
      the payment, and ``receipt.settlement`` names the transaction.
    * ``authorization_in_flight``: admitted, outcome not final. Transient
      (:func:`is_transient_error`): the same request later learns the outcome.
      Only the binding that admitted it (the same ``Idempotency-Key`` or the
      same ``X-UVD-Purchase``) gets the original answer back; without it, a
      resend learns the final outcome, never the success.
    * ``receipt_request_conflict``: admitted for another purchase context, or
      under other terms.

    For a seller none of them is delivered on: the ``X-PAYMENT`` was already
    used by another request. And none is a 402, which tells the buyer to sign
    a new payment for one that moved or may still move.
    :func:`payment_conflict_response` builds the answer: ``409`` for the first
    and the last, ``503`` + ``Retry-After`` while in flight. The first and the
    last also read as spent for :func:`is_spent_nonce_error`.

    The client raises the same codes itself (as
    :class:`PaymentSettlementError`) when a facilitator before 2.39.0 replays
    a payment to a request that did not admit it.
    """
    if isinstance(exc, FacilitatorError):
        code: Any = exc.error_code
    elif isinstance(exc, X402Error):
        code = getattr(exc, "reason", None)
    else:
        return None
    if not isinstance(code, str):
        return None
    code = code.strip().lower()
    return code if code in ADMITTED_AUTHORIZATION_CODES else None


#: What a paywall waits before inviting a retry when the facilitator gave no
#: ``Retry-After`` of its own. Matches the facilitator's own value for the EVM
#: writer lease.
DEFAULT_TRANSIENT_RETRY_AFTER_SECONDS = 5.0


def transient_503_response(
    exc: X402Error,
    *,
    default_retry_after: float = DEFAULT_TRANSIENT_RETRY_AFTER_SECONDS,
) -> Tuple[Dict[str, Any], Dict[str, str]]:
    """Build the ``(body, headers)`` a paywall should answer with a **503**.

    Call it only when :func:`is_transient_error` said the failure was transient.
    The distinction it serves is the whole point of the pair: a 402 tells the
    buyer "your payment was REJECTED, sign a new authorization", and a buyer
    that obeys pays twice for a payment nobody ever refused. A 503 says "no
    verdict — present the SAME credential again".

    The body keeps the exception's own ``to_dict()`` shape and adds ``retryable``,
    the facilitator's ``reason`` when it sent one (``settlement_in_progress``
    for its ``202``, which names the state in ``error``), and ``safeToRetry``:
    ``False`` for ``forward_failed`` and for any unrecognised ``reason``, where
    the write may already have executed.

    ``Retry-After`` is the facilitator's value clamped to
    ``MAX_RETRY_AFTER_SECONDS`` — a misconfigured deployment answering
    ``Retry-After: 3600`` gets to say "later", not to park a buyer for an hour.

    And when the exception carries a transaction hash, ``transaction`` and
    ``paymentId`` are lifted to the TOP level and ``safeToRetry`` is forced to
    ``False`` — whatever ``reason`` says. A caller only gets here over such a
    body by passing ``anti_double_settle=False``, and owning that risk is not
    the same as being told nothing about it: the buyer reads the top level, and
    a hash is the proof the facilitator already broadcast.
    """
    retry_after = retry_after_seconds(exc, default_retry_after) or default_retry_after
    retry_after = min(float(retry_after), MAX_RETRY_AFTER_SECONDS)
    reason = facilitator_reason(exc)
    if reason is None and getattr(exc, "error_code", None) == SETTLEMENT_IN_PROGRESS:
        # The facilitator names this state in `error`, not `reason`.
        reason = SETTLEMENT_IN_PROGRESS

    body: Dict[str, Any] = dict(exc.to_dict())
    body["retryable"] = True
    body["retryAfter"] = retry_after
    if reason is not None:
        body["reason"] = reason
        body["safeToRetry"] = write_retry_is_safe(reason)

    # A hash in a FAILURE body means the facilitator got as far as
    # BROADCASTING. The 503 itself still stands -- the caller reached here by
    # opting out of the anti-double-settle guard, and that is their risk to
    # own -- but the buyer reads the TOP level, and the only proof of the
    # broadcast used to sit one level down in ``details``. Worse, a ``reason``
    # in WRITE_NOT_ATTEMPTED_REASONS made ``safeToRetry`` read True over a body
    # that carried a hash. Same rule as
    # :meth:`FacilitatorError._retryable_verdict`, one layer up: the evidence
    # beats the label, and refusing to promise safety without saying where to
    # look is the dead end this release closed everywhere else.
    details = body.get("details") or {}
    if details.get("transaction") is not None:
        body["transaction"] = details["transaction"]
        body["safeToRetry"] = False
        if details.get("paymentId") is not None:
            body["paymentId"] = details["paymentId"]
    return body, {
        "Content-Type": "application/json",
        "Retry-After": str(int(retry_after)) if retry_after == int(retry_after) else str(retry_after),
    }


def payment_conflict_response(
    exc: X402Error,
    *,
    default_retry_after: float = DEFAULT_TRANSIENT_RETRY_AFTER_SECONDS,
) -> Optional[Tuple[int, Dict[str, Any], Dict[str, str]]]:
    """Build the ``(status, body, headers)`` a paywall should answer when
    :func:`admitted_authorization_code` names a code for ``exc``, else ``None``.

    The ``X-PAYMENT`` was already admitted for another request, so nothing is
    delivered on this one, and it is never a 402, which would tell the buyer to
    sign a second payment for one that moved or may still move:

    * ``authorization_in_flight`` -> **503** + ``Retry-After``, ``retryable``
      true, ``reason`` the code: resend the SAME request later to learn the
      outcome; only the binding that admitted it gets the original answer.
      The body is :func:`transient_503_response`'s.
    * ``authorization_already_settled`` and ``receipt_request_conflict`` ->
      **409**, ``retryable`` false: this ``X-PAYMENT`` was already used.

    Same mapping as ``buildPaymentConflictResponse`` in the TypeScript SDK. The
    body is the exception's own ``to_dict()`` plus ``reason``, ``retryable``
    and ``safeToReplay``; the receipt, when the facilitator sent one, travels in
    ``PAYMENT-RESPONSE`` / ``X-PAYMENT-RESPONSE`` as a paid response carries it,
    so whoever holds the payment can see where it went.
    """
    code = admitted_authorization_code(exc)
    if code is None:
        return None
    if code == AUTHORIZATION_IN_FLIGHT:
        status = 503
        body, headers = transient_503_response(exc, default_retry_after=default_retry_after)
    else:
        status = 409
        body = dict(exc.to_dict())
        body["retryable"] = False
        headers = {"Content-Type": "application/json"}
    body["reason"] = code
    body["safeToReplay"] = False
    receipt = getattr(exc, "receipt", None)
    if receipt is not None:
        headers.update(
            payment_response_headers({"success": False, "receipt": receipt.model_dump()})
        )
    return status, body, headers


#: What a buyer is told when its payment may have moved. The body's
#: ``transaction`` / ``paymentId``, when present, are what to check.
_MAY_HAVE_SETTLED_MESSAGE = (
    "The payment may have settled: do not sign another one. "
    "Check the transaction before paying again."
)
#: The same, when the facilitator named no transaction.
_MAY_HAVE_SETTLED_NO_TRANSACTION_MESSAGE = (
    "The payment may have settled: do not sign another one. "
    "The facilitator reported no transaction; check with the seller before paying again."
)
#: What a buyer is told when its authorization was already used.
_AUTHORIZATION_ALREADY_USED_MESSAGE = (
    "This payment authorization was already used and the payment may have settled: "
    "do not sign another one. Check the payment before paying again."
)


def _with_receipt(headers: Dict[str, str], exc: X402Error) -> Dict[str, str]:
    """``headers`` plus ``PAYMENT-RESPONSE`` when ``exc`` carries a receipt."""
    receipt = getattr(exc, "receipt", None)
    if receipt is not None:
        headers.update(
            payment_response_headers({"success": False, "receipt": receipt.model_dump()})
        )
    return headers


def _broadcast_transaction(exc: X402Error) -> Optional[str]:
    """The transaction a failure says was broadcast, if it names one."""
    if isinstance(exc, FacilitatorError):
        return _facilitator_error_tx_hash(exc)
    if isinstance(exc, PaymentSettlementError):
        return exc.tx_hash
    return None


#: What x402-rs refuses on ``/settle`` before it executes anything: the request,
#: not the payment. Compared against the ``error`` token without its
#: `` (ref: …)`` suffix (``handlers.rs`` ``post_settle`` and its
#: ``IntoResponse``; ``receipts/mod.rs`` ``settle``). ``403`` is ``Address
#: blocked``. Every other ``4xx`` of a settle may come after the payment moved.
_SETTLE_REQUEST_REFUSALS = frozenset(
    {
        "Invalid request",
        "invalid_address",
        "clock_error",
        "reserved_idempotency_key",
        "invalid_receipt_context",
    }
)
_SETTLE_REQUEST_REFUSAL_PREFIXES = (
    "receipt_",
    "Failed to deserialize",
    "Failed to decode",
    "Failed to process",
    "PAYMENT-SIGNATURE header",
    "Invalid UTF-8",
    "Address blocked",
)


def _settle_refused_after_verify(exc: FacilitatorError) -> bool:
    """A ``4xx`` from ``/settle`` that is not a refusal of the request itself.

    The integrations settle only after ``/verify`` accepted the same payload and
    signature, so a settle that fails there -- the opaque ``400
    contract_call_failed (ref)`` of an EVM revert, or ``400 internal_error
    (ref)``, which is how x402-rs reports a nonce Stellar, Algorand, NEAR or Sui
    already saw -- is not a bad signature. Something changed between the two
    calls, and the likeliest change is that the authorization was used. Only a
    ``403`` and what ``_SETTLE_REQUEST_REFUSALS`` and
    ``_SETTLE_REQUEST_REFUSAL_PREFIXES`` name were refused before anything ran.
    """
    status = exc.status_code
    if exc.operation != "settle" or status is None or not 400 <= status < 500:
        return False
    if status == 403:
        return False
    token = _REF_SUFFIX.sub("", exc.error_code or "")
    if token in _SETTLE_REQUEST_REFUSALS or token.startswith(_SETTLE_REQUEST_REFUSAL_PREFIXES):
        return False
    return True


def _may_have_settled_response(
    exc: X402Error, transaction: Optional[str]
) -> Tuple[int, Dict[str, Any], Dict[str, str]]:
    """``500`` for a failure after which the payment may have moved.

    The answer the TypeScript SDK gives ``settlement_unconfirmed``: not ``402``,
    which asks for a new signature, and not ``503`` + ``Retry-After``, which
    invites a resend. The body is the exception's ``to_dict()`` plus
    ``retryable`` / ``safeToReplay`` false, the facilitator's ``reason``, and
    ``transaction`` / ``paymentId`` at the top level when it sent them.
    """
    body: Dict[str, Any] = dict(exc.to_dict())
    body["message"] = (
        _MAY_HAVE_SETTLED_MESSAGE if transaction is not None
        else _MAY_HAVE_SETTLED_NO_TRANSACTION_MESSAGE
    )
    body["retryable"] = False
    body["safeToReplay"] = False
    reason = getattr(exc, "error_code", None) or facilitator_reason(exc)
    if reason is not None:
        body["reason"] = reason
    if transaction is not None:
        body["transaction"] = transaction
    payment_id = getattr(exc, "payment_id", None)
    if payment_id is not None:
        body["paymentId"] = payment_id
    return 500, body, _with_receipt({"Content-Type": "application/json"}, exc)


def _spent_authorization_response(
    exc: X402Error, evidence: str
) -> Tuple[int, Dict[str, Any], Dict[str, str]]:
    """``409`` for an authorization the facilitator says was already used.

    The body is the exception's ``to_dict()`` plus ``retryable`` /
    ``safeToReplay`` false, ``spentNonceEvidence`` (what
    :func:`spent_nonce_evidence` returned) and, when a code said so, that code
    as ``reason``.
    """
    body: Dict[str, Any] = dict(exc.to_dict())
    body["message"] = _AUTHORIZATION_ALREADY_USED_MESSAGE
    body["retryable"] = False
    body["safeToReplay"] = False
    body["spentNonceEvidence"] = evidence
    code = _spent_nonce_code_of(exc)
    if code is not None:
        body["reason"] = code
    return 409, body, _with_receipt({"Content-Type": "application/json"}, exc)


def _binding_refusal_response(
    exc: PaymentBindingError,
) -> Tuple[int, Dict[str, Any], Dict[str, str]]:
    """What the seller's purchase binding answers instead of the facilitator.

    ``payment_store_unavailable`` is ``503`` + ``Retry-After``: present the same
    ``X-PAYMENT`` later. Not :func:`transient_503_response`, whose
    ``safeToRetry`` reads a facilitator ``reason`` this is not. The other two
    are ``409``, with the facilitator's receipt when the refusal carried one.
    """
    body: Dict[str, Any] = dict(exc.to_dict())
    body["reason"] = exc.reason
    body["safeToReplay"] = False
    if exc.reason == PAYMENT_STORE_UNAVAILABLE:
        retry_after = int(DEFAULT_TRANSIENT_RETRY_AFTER_SECONDS)
        body["retryable"] = True
        body["retryAfter"] = retry_after
        return 503, body, {"Content-Type": "application/json", "Retry-After": str(retry_after)}
    body["retryable"] = False
    return 409, body, _with_receipt({"Content-Type": "application/json"}, exc)


def _undelivered_response(exc: X402Error) -> Optional[Tuple[int, Dict[str, Any], Dict[str, str]]]:
    """What the SDK's middlewares and decorators answer for a payment that is
    neither delivered nor rejected, or ``None`` for a rejection (each keeps its
    own 402 or 400). First match wins:

    0. A :class:`~uvd_x402_sdk.exceptions.PaymentBindingError` (only with a
       :mod:`~uvd_x402_sdk.bindings` store configured): 503 when the store
       could not answer, 409 for a payment bought for another resource or
       refused after this seller had already seen it.
    1. :func:`payment_conflict_response`: 409, or 503 while in flight.
    2. A transaction on a failure that is not transient (``502
       settlement_unconfirmed``, any other ``5xx`` with a hash): 500, with
       ``transaction`` and ``paymentId``. It was broadcast and may be mined.
    3. :func:`spent_nonce_evidence`: 409. The authorization was already used,
       so the payment may have moved.
    4. :func:`is_transient_error` (a timeout, a ``202 settlement_in_progress``,
       ``503 idempotency_store_unavailable`` or ``receipt_store_unavailable``,
       another retryable ``5xx``, a 429): 503 + ``Retry-After``
       (:func:`transient_503_response`). Present the same ``X-PAYMENT`` later.
    5. Any other ``5xx`` (the facilitator said ``retryable: false``), and a
       ``4xx`` from ``/settle`` that is not a refusal of the request
       (:func:`_settle_refused_after_verify`: ``400 contract_call_failed
       (ref)``, ``400 internal_error (ref)``): 500. The TypeScript SDK answers
       every settle failure it may not retry with 500.

    None of them is a 402: each tells the buyer not to sign another payment.
    The anti-double-settle guard in :func:`is_transient_error` is unchanged:
    the SDK still does not re-send any of them. The receipt, when there is one,
    travels in ``PAYMENT-RESPONSE``.
    """
    if isinstance(exc, PaymentBindingError):
        return _binding_refusal_response(exc)
    conflict = payment_conflict_response(exc)
    if conflict is not None:
        return conflict
    transient = is_transient_error(exc)
    transaction = _broadcast_transaction(exc)
    if transaction is not None and not transient:
        return _may_have_settled_response(exc, transaction)
    evidence = spent_nonce_evidence(exc)
    if evidence is not None:
        return _spent_authorization_response(exc, evidence)
    if transient:
        body, headers = transient_503_response(exc)
        return 503, body, _with_receipt(headers, exc)
    if isinstance(exc, FacilitatorError) and (
        (exc.status_code or 0) >= 500 or _settle_refused_after_verify(exc)
    ):
        return _may_have_settled_response(exc, None)
    return None


def _validated_eip712_domain(domain: Dict[str, str]) -> Dict[str, str]:
    """Validate a caller-supplied EIP-712 domain override and normalise it.

    Fail-loud on purpose: a wrong or partial EIP-712 domain produces a
    signature the token contract rejects — fail here, not on-chain.
    """
    missing = [k for k in ("name", "version") if not domain.get(k)]
    if missing:
        raise ValueError(
            "eip712_domain requires non-empty 'name' and 'version' "
            f"(missing: {', '.join(missing)})"
        )
    return {"name": domain["name"], "version": domain["version"]}


def _signing_token(network: str, token_type: str) -> Optional[TokenConfig]:
    """The token ``create_authorization()`` signs on ``network``.

    It comes from the network registry. None where it signs nothing: an
    unknown or non-EVM network, or a token the registry does not have there.
    ``create_authorization()`` raises its own error for those, before signing.
    """
    try:
        normalized = normalize_network(network)
    except ValueError:
        return None
    config = get_network(normalized)
    if config is None or config.network_type != NetworkType.EVM:
        return None
    return get_token_config(normalized, token_type)  # type: ignore[arg-type]


def _is_token(asset: str, token: TokenConfig) -> bool:
    """Whether ``asset`` is ``token``'s address, compared as the purchase
    policy compares addresses: hex in any case, anything else exactly."""
    return canonical_address(asset) == canonical_address(token.address)


def _signs_as_offered(option: Mapping[str, Any], token_type: str) -> bool:
    """Whether ``token_type`` pays ``option`` in the token it names.

    True when the option's ``asset`` is that token's address on the option's
    own network, and when it names no asset (it is then paid in that token).
    """
    asset = option.get("asset")
    if not asset:
        return True
    token = _signing_token(option["network"], token_type)
    return token is not None and _is_token(asset, token)


def _offer_in(offer: Any, asset: str) -> Any:
    """``offer`` naming ``asset``: an :class:`Offer` or a raw ``accepts`` entry."""
    if isinstance(offer, Offer):
        return replace(offer, asset=asset)
    return {**offer, "asset": asset}


def _check_signed_as_offered(
    amount: Any,
    asset: Optional[str],
    network: str,
    price: Decimal,
    token_type: str,
    token_decimals: int,
) -> None:
    """Raise ``ValueError`` unless signing ``price`` signs the offer itself.

    ``create_authorization()`` signs the registry's token for ``token_type``,
    whatever ``asset`` the offer names, and ``fetch()`` prices the offer with
    ``token_decimals`` while ``create_authorization()`` converts that price
    back into base units with the decimals of the token it signs. Where the
    token is not the offer's, where those decimals differ, or where the offer
    has more digits than the ``Decimal`` division keeps (28), the signature
    would carry another token or amount than the one ``max_amount`` and the
    purchase policy were checked against. An offer that names no asset is paid
    in ``token_type``'s token on the offer's network (USDC by default).
    """
    token = _signing_token(network, token_type)
    if token is None:
        return
    if asset and not _is_token(asset, token):
        raise ValueError(
            f"This offer asks to be paid in {asset} on {network}, and "
            f"token_type={token_type!r} signs {token_type.upper()} at {token.address}. "
            f"Nothing was signed."
        )
    decimals = token.decimals
    try:
        signed: Optional[int] = to_base_units(price, decimals)
    except ValueError:
        signed = None
    if signed == int(amount):
        return
    would_sign = "no whole number of base units" if signed is None else f"{signed} base units"
    reason = (
        f"token_decimals={token_decimals} does not match the {decimals} decimals of "
        f"{token_type.upper()} on {network}, which is what signs"
        if token_decimals != decimals
        else f"{amount} has more digits than the price carries exactly"
    )
    raise ValueError(
        f"This offer asks {amount} base units, and signing its price "
        f"{format(price, 'f')} would sign {would_sign}: {reason}. Nothing was signed."
    )


def _merge_caller_extra(
    requirements: PaymentRequirements, extra: Optional[Mapping[str, Any]]
) -> PaymentRequirements:
    """``requirements`` with the caller's ``extra`` merged into its ``extra``.

    The entries the SDK set stay (the EIP-712 ``name`` / ``version`` on EVM,
    ``feePayer`` on Hedera): a caller key that gives one of them ANOTHER value
    raises before anything is sent, and the same value is accepted. Everything
    else is added as given, nested values included, e.g.
    ``{"8004-reputation": {"includeProof": True}}``, with which the facilitator
    returns the settlement's ``proofOfPayment``.

    Raises:
        TypeError: ``extra`` is not a mapping with string keys.
        ValueError: it is not JSON-serialisable, or it overrides an entry the
            SDK set (the EIP-712 domain goes through ``eip712_domain``).
    """
    if extra is None:
        return requirements
    if not isinstance(extra, Mapping) or not all(isinstance(k, str) for k in extra):
        raise TypeError("extra must be a mapping with string keys")
    caller = dict(extra)
    try:
        # allow_nan=False: NaN / Infinity are not JSON, and some httpx
        # versions within the supported range would send them as bare tokens.
        json.dumps(caller, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"extra must be JSON-serialisable: {exc}") from None
    merged: Dict[str, Any] = dict(requirements.extra or {})
    clashes = sorted(k for k in caller if k in merged and merged[k] != caller[k])
    if clashes:
        raise ValueError(
            f"extra gives {', '.join(clashes)} another value than the SDK set for this "
            "payment; the EIP-712 domain goes through eip712_domain"
        )
    merged.update(caller)
    requirements.extra = merged
    return requirements


class X402Client:
    """
    Client for processing x402 payments via the Ultravioleta facilitator.

    The client handles the two-step payment flow:
    1. Verify: Validate the payment signature/authorization
    2. Settle: Execute the payment on-chain

    Example:
        >>> client = X402Client(
        ...     recipient_address="0xYourWallet...",
        ...     facilitator_url="https://facilitator.ultravioletadao.xyz"
        ... )
        >>> result = client.process_payment(
        ...     x_payment_header=request.headers.get("X-PAYMENT"),
        ...     expected_amount_usd=Decimal("10.00")
        ... )
        >>> print(f"Paid by {result.payer_address}, tx: {result.transaction_hash}")
    """

    def __init__(
        self,
        recipient_address: Optional[str] = None,
        facilitator_url: str = "https://facilitator.ultravioletadao.xyz",
        config: Optional[X402Config] = None,
        *,
        verify_facilitator_support: bool = False,
        policy: Optional[PurchasePolicy] = None,
        http_client: Optional[httpx.Client] = None,
        **kwargs: Any,
    ) -> None:
        """
        Initialize the x402 client.

        Args:
            recipient_address: Default recipient for EVM chains (convenience arg)
            facilitator_url: URL of the facilitator service. Serves every network
                unless `facilitator_by_network` routes it elsewhere.
            config: Full X402Config object (overrides other args)
            verify_facilitator_support: Probe every configured facilitator's
                `GET /supported` at construction and raise if an enabled network
                is routed to a facilitator that does not settle it. Off by
                default because it performs network I/O; see `verify_routes()`.
            policy: What this buyer is allowed to sign, evaluated by `fetch()`
                against the offer in hand BEFORE anything is signed (see
                :class:`~uvd_x402_sdk.policy.PurchasePolicy`). Defaults to
                `PurchasePolicy.permissive()`, which is NOT what
                `PurchasePolicy()` gives you: a policy a caller sits down to
                WRITE gets the safe default (an asset with no declared ceiling
                is refused), while a caller that supplied none keeps exactly the
                behaviour they had before 0.82.0.
            http_client: The `httpx.Client` every facilitator call of this
                client goes through (`/verify`, `/settle` and its timeout
                fallback, `/supported`, ...). Default: one the SDK creates and
                closes. A client passed here is the caller's: `close()` leaves
                it open. It is the public way to see the facilitator's raw
                answers, through httpx's own response hooks:
                `httpx.Client(event_hooks={"response": [hook]})`, where the
                hook calls `response.read()` before reading the body. The
                requests of `fetch()`, the paid one included, go through it
                too unless `fetch()` gets its own `http_client`. The SDK sets
                its own timeout on `/verify`, `/settle` and its timeout
                fallback, `/accepts` and the `/supported` of route
                validation; `get_version()`, `get_supported()`,
                `get_blacklist()`, `health_check()`, the other GETs and
                `fetch()` use the timeout of the client passed here.
            **kwargs: Additional config parameters passed to X402Config —
                including `facilitator_by_network`, the `network -> facilitator
                URL` routing table (see X402Config).

        Raises:
            ValueError: If no recipient address is configured
            ConfigurationError: If the facilitator routing table leaves an
                enabled network unrouted, or (with verify_facilitator_support)
                routes one to a facilitator that does not settle it.

        Example:
            >>> # base settles through CDP, avalanche through UVD (CDP does not
            >>> # settle avalanche). Unrouted networks are refused, not guessed.
            >>> client = X402Client(
            ...     recipient_address="0xMerchant...",
            ...     supported_networks=["base", "avalanche"],
            ...     facilitator_by_network={
            ...         "base": "https://api.cdp.coinbase.com/platform/v2/x402",
            ...         "avalanche": "https://facilitator.ultravioletadao.xyz",
            ...     },
            ... )
        """
        if config:
            self.config = config
        else:
            # Build config from individual args
            config_kwargs = {
                "facilitator_url": facilitator_url,
                "recipient_evm": recipient_address or kwargs.get("recipient_evm", ""),
                **kwargs,
            }
            # Remove None values
            config_kwargs = {k: v for k, v in config_kwargs.items() if v is not None}
            self.config = X402Config(**config_kwargs)

        # HTTP client for facilitator requests
        self._http_client: Optional[httpx.Client] = None
        # The caller's, when one was passed: used as given, never closed here.
        self._caller_http_client = http_client

        # Client-side signer (set via connect_with_private_key)
        self._hedera_signer: Any = None
        self._signer: Any = None  # eth_account.Account when connected
        self._signer_address: Optional[str] = None
        # Normalised signing seam. BOTH connect_* methods populate this with a
        # callable (domain, types, message) -> "0x…" 65-byte signature, so
        # create_authorization has a single code path and the local and remote
        # signers cannot drift apart.
        self._sign_typed_data: Optional[Any] = None
        self._connected_chain: Optional[str] = None

        # What this buyer may sign. Permissive unless the caller wrote one:
        # nothing in the payment path ever widens it (see `policy.py`).
        self.policy: PurchasePolicy = (
            policy if policy is not None else PurchasePolicy.permissive()
        )

        if verify_facilitator_support:
            self.verify_routes()

    # =========================================================================
    # Facilitator Routing
    # =========================================================================

    def facilitator_url_for(self, network: str) -> str:
        """Resolve which facilitator settles `network`.

        Delegates to :meth:`X402Config.facilitator_url_for`. Without a
        `facilitator_by_network` table this is always `config.facilitator_url`.

        Raises:
            ConfigurationError: If a routing table is configured and the network
                is neither in it nor covered by a `"*"` fallback.
        """
        return self.config.facilitator_url_for(network)

    def verify_routes(self) -> Dict[str, list]:
        """Prove every configured facilitator settles the networks routed to it.

        Performs one `GET /supported` per DISTINCT facilitator URL and checks
        each enabled network against what that facilitator advertises. This is
        the check `verify_facilitator_support=True` runs at construction: a
        network pointed at a facilitator that cannot settle it fails at boot
        instead of on the first payment.

        Returns:
            `facilitator URL -> [networks]` for the verified routes.

        Raises:
            ConfigurationError: If a facilitator does not advertise a network
                routed to it.
            FacilitatorError: If a facilitator's `/supported` cannot be read.
                Deliberately fatal — an unverifiable route is not a verified one.
        """
        from uvd_x402_sdk.exceptions import ConfigurationError

        by_url: Dict[str, list] = {}
        for network, url in self.config.facilitator_routes().items():
            by_url.setdefault(url, []).append(network)

        for url, networks in by_url.items():
            advertised = self._fetch_supported_networks(url)
            unsupported = sorted(
                n for n in networks if self.config.network_key(n) not in advertised
            )
            if unsupported:
                raise ConfigurationError(
                    f"Facilitator {url} does not settle: {', '.join(unsupported)}. "
                    f"It advertises: {', '.join(sorted(advertised)) or '(nothing)'}. "
                    f"Route those networks to a facilitator that supports them, or "
                    f"drop them from supported_networks.",
                    config_key="facilitator_by_network",
                )

        return by_url

    def _fetch_supported_networks(self, facilitator_url: str) -> set:
        """Normalised set of network names a facilitator advertises via /supported."""
        try:
            client = self._get_http_client()
            response = client.get(
                f"{facilitator_url}/supported",
                timeout=self.config.verify_timeout,
                **self._facilitator_kwargs(facilitator_url),
            )
            response.raise_for_status()
            data = response.json()
        except httpx.HTTPStatusError as e:
            raise FacilitatorError(
                message=f"GET {facilitator_url}/supported failed: {e.response.status_code}",
                status_code=e.response.status_code,
                response_body=e.response.text,
            )
        except Exception as e:
            raise FacilitatorError(
                message=f"GET {facilitator_url}/supported failed: {e}"
            )

        networks = set()
        for kind in data.get("kinds", []) or []:
            name = kind.get("network") if isinstance(kind, dict) else None
            if name:
                networks.add(self.config.network_key(str(name)))
        return networks

    def _get_http_client(self) -> httpx.Client:
        """Get or create HTTP client."""
        if self._caller_http_client is not None:
            return self._caller_http_client
        if self._http_client is None or self._http_client.is_closed:
            self._http_client = httpx.Client(
                timeout=httpx.Timeout(
                    connect=10.0,
                    read=self.config.settle_timeout,
                    write=10.0,
                    pool=10.0,
                )
            )
        return self._http_client

    def _facilitator_headers(
        self, facilitator_url: str, headers: Optional[dict[str, str]] = None
    ) -> dict[str, str]:
        """``headers`` plus ``X-UVD-Stack-Key`` for a request to ``facilitator_url``.

        Per request, never a default header of the HTTP client: ``fetch()``
        sends its requests to sellers through that same client. And only to a
        facilitator of Ultravioleta DAO (``uvd_x402_sdk.stack_key``): a
        facilitator that ``facilitator_by_network`` routes to a third party
        never sees the key.
        """
        return {
            **(headers or {}),
            **stack_key_headers(
                self.config.stack_key, facilitator_url, self.config.stack_key_hosts
            ),
        }

    def _facilitator_kwargs(self, facilitator_url: str) -> dict[str, Any]:
        """The ``headers=`` keyword of a GET to ``facilitator_url``: empty
        without a key to send, so the call is made exactly as before."""
        return stack_key_request_kwargs(
            self.config.stack_key, facilitator_url, self.config.stack_key_hosts
        )

    def close(self) -> None:
        """Close the HTTP client."""
        if self._http_client:
            self._http_client.close()
            self._http_client = None

    def __enter__(self) -> "X402Client":
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    # =========================================================================
    # Payload Parsing
    # =========================================================================

    @staticmethod
    def _normalize_v2_envelope(data: Dict[str, Any]) -> Dict[str, Any]:
        """Flatten an x402 v2 X-PAYMENT envelope into the v1 shape.

        The v2 challenge (``create_402_response_v2``) instructs the payer to
        echo the chosen accept back as ``accepted`` with NO top-level
        ``network`` — but this client parsed only the flat v1 model, so every
        payment built exactly as the challenge asked died with
        "network Field required": the server rejected the format it had
        itself requested. Found 2026-08-12 with a real Rabby signature on
        Base (describe-net incident); the wallet was signing perfectly, and
        the first successful x402 payment of that API only happened once its
        paywall translated the envelope before handing it to this SDK. This
        is that translation, moved to where it always belonged.

        Only the wrapper changes: the EIP-3009 signature and authorization
        inside ``payload`` are copied through untouched. A dict without
        ``accepted``, or with a top-level ``network``, is already v1-shaped
        and is returned as-is, so v1 parsing stays byte-identical.

        Unlike the downstream shim this replaces, unknown networks raise a
        clear :class:`InvalidPayloadError` instead of silently reproducing
        the old cryptic failure, and CAIP-2 resolution covers every
        namespace the registry knows (eip155 by chain id, solana, near,
        stellar, ...), not just EVM.
        """
        if "accepted" not in data or "network" in data:
            return data

        accepted = data.get("accepted") or {}
        if not isinstance(accepted, dict):
            raise InvalidPayloadError(
                "v2 envelope: `accepted` must be the accept object echoed "
                f"from the 402, got {type(accepted).__name__}"
            )

        raw_network = accepted.get("network")
        if not raw_network:
            raise InvalidPayloadError(
                "v2 envelope: `accepted.network` is missing — echo the "
                "chosen accept object from the 402 unchanged"
            )
        network = str(raw_network)
        if is_caip2_format(network):
            resolved = parse_caip2_network(network)
            if resolved is None:
                raise InvalidPayloadError(
                    f"v2 envelope: unrecognized CAIP-2 network '{network}'"
                )
            network = resolved

        # pay.js (the only producer observed in production) sends the inner
        # block as top-level `payload`; the SDK's own outbound v2 envelope
        # nests it as `paymentPayload` (sometimes with `payload` inside).
        payload = data.get("payload")
        if payload is None:
            inner = data.get("paymentPayload")
            if isinstance(inner, dict):
                payload = inner.get("payload", inner)
        if not isinstance(payload, dict) or not payload:
            raise InvalidPayloadError(
                "v2 envelope: no payment `payload` found (expected "
                "`payload` or `paymentPayload` with the signature and "
                "authorization)"
            )

        return {
            "x402Version": data.get("x402Version", 2),
            "scheme": accepted.get("scheme", "exact"),
            "network": network,
            "payload": payload,
        }

    def extract_payload(self, x_payment_header: str) -> PaymentPayload:
        """
        Extract and validate payment payload from X-PAYMENT header.

        Accepts both envelope shapes: the flat v1 payload and the v2
        envelope (``accepted`` echo, no top-level ``network``) that this
        SDK's own v2 challenge instructs payers to send.

        Args:
            x_payment_header: Base64-encoded JSON payload

        Returns:
            Parsed PaymentPayload object

        Raises:
            InvalidPayloadError: If payload is invalid
        """
        if not x_payment_header:
            raise InvalidPayloadError("Missing X-PAYMENT header")

        try:
            # Decode base64
            json_bytes = base64.b64decode(x_payment_header)
            json_str = json_bytes.decode("utf-8")

            # Parse JSON
            data = json.loads(json_str)

            # v2 envelope (`accepted` echo) -> flat v1 shape; v1 passes as-is
            if isinstance(data, dict):
                data = self._normalize_v2_envelope(data)

            # Validate and parse with Pydantic
            payload = PaymentPayload(**data)

            logger.debug(f"Extracted payload for network: {payload.network}")
            return payload

        except base64.binascii.Error as e:
            raise InvalidPayloadError(f"Invalid base64 encoding: {e}")
        except json.JSONDecodeError as e:
            raise InvalidPayloadError(f"Invalid JSON in payload: {e}")
        except InvalidPayloadError:
            raise
        except Exception as e:
            raise InvalidPayloadError(f"Failed to parse payload: {e}")

    # =========================================================================
    # Per-Network Timeout
    # =========================================================================

    def _get_settle_timeout(self, network: str) -> float:
        """
        Get settle timeout for a specific network.

        Uses the network's settle_timeout_seconds if available,
        otherwise falls back to config.settle_timeout.
        Ethereum L1 uses 900s; L2s use 90s (default).
        """
        try:
            normalized = normalize_network(network)
        except ValueError:
            return self.config.settle_timeout

        network_config = get_network(normalized)
        if network_config and network_config.settle_timeout_seconds > 0:
            return network_config.settle_timeout_seconds
        return self.config.settle_timeout

    # =========================================================================
    # Network Validation
    # =========================================================================

    def validate_network(self, network: str) -> str:
        """
        Validate that a network is supported and enabled.

        Handles both v1 ("base") and v2 CAIP-2 ("eip155:8453") formats.

        Args:
            network: Network identifier (v1 or CAIP-2)

        Returns:
            Normalized network name

        Raises:
            UnsupportedNetworkError: If network is not supported
        """
        # Normalize CAIP-2 to network name
        try:
            normalized = normalize_network(network)
        except ValueError:
            raise UnsupportedNetworkError(
                network=network,
                supported_networks=get_supported_network_names(),
            )

        network_config = get_network(normalized)
        if not network_config:
            raise UnsupportedNetworkError(
                network=network,
                supported_networks=get_supported_network_names(),
            )

        if not network_config.enabled:
            raise UnsupportedNetworkError(
                network=network,
                supported_networks=[n for n in get_supported_network_names()
                                   if get_network(n) and get_network(n).enabled],
            )

        if not self.config.is_network_enabled(normalized):
            raise UnsupportedNetworkError(
                network=network,
                supported_networks=self.config.supported_networks,
            )

        return normalized

    # =========================================================================
    # Payment Requirements Building
    # =========================================================================

    def _build_payment_requirements(
        self,
        payload: PaymentPayload,
        expected_amount_usd: Decimal,
        pay_to: Optional[str] = None,
        asset: Optional[str] = None,
        eip712_domain: Optional[Dict[str, str]] = None,
        token_decimals: Optional[int] = None,
    ) -> PaymentRequirements:
        """
        Build payment requirements for facilitator request.

        Args:
            payload: Parsed payment payload
            expected_amount_usd: Expected payment amount in USD
            pay_to: Override recipient address
            asset: Override the token contract address sent as `asset`
                (defaults to the network's USDC). Required for non-USDC
                settles where the token is not in the SDK registry.
            eip712_domain: Override the EIP-712 domain params sent to the
                facilitator via `extra` ({"name": ..., "version": ...}).
                Use when the caller's token registry disagrees with the
                SDK's (e.g. USDT "USD₮0" vs "Tether USD" on Optimism).
            token_decimals: Decimals of the token named by `asset`. The default
                converts USD with the NETWORK's USDC decimals, which is wrong
                the moment `asset` points at a token that does not share them —
                USDC is 7 decimals on Stellar and 18 on BSC while AUSD is 6
                everywhere, so the same override silently mis-prices by orders
                of magnitude. Pass it whenever `asset` is passed.

        Returns:
            PaymentRequirements object
        """
        # Normalize network name (handles CAIP-2 format)
        normalized_network = payload.get_normalized_network()

        network_config = get_network(normalized_network)
        if not network_config:
            raise UnsupportedNetworkError(
                network=payload.network,
                supported_networks=get_supported_network_names(),
            )

        if network_config.network_type == NetworkType.HEDERA:
            from uvd_x402_sdk.hedera import build_hedera_requirements
            if payload.x402Version != 2:
                raise ValueError("Native Hedera supports only x402 v2")
            if asset not in (None, network_config.usdc_address) or token_decimals not in (None, 6) or eip712_domain:
                raise ValueError("Hedera payments support native USDC only; HBAR is for network fees")
            atomic = expected_amount_usd * Decimal(10**6)
            if not atomic.is_finite() or atomic != atomic.to_integral_value():
                raise ValueError("USDC price must have at most 6 decimal places")
            r = build_hedera_requirements(normalized_network, pay_to or self.config.get_recipient(normalized_network), str(int(atomic)))
            return PaymentRequirements(scheme="exact", network=r["network"], maxAmountRequired=r["amount"],
                payTo=r["payTo"], asset=r["asset"], extra=r["extra"], maxTimeoutSeconds=180,
                resource=self.config.resource_url or "https://api.example.com/payment", description=self.config.description, mimeType="application/json")

        # A price in USD only becomes base units when the settlement asset is
        # worth a dollar per whole unit. Without an explicit `asset` the network
        # default settles, and on XRPL that default is native XRP: $1.00 would
        # be charged as 1 XRP. `token_decimals` does not rescue this — it fixes
        # SCALE, and the defect is UNIT. Refuse before signing anything.
        if not network_config.usd_pegged and asset is None:
            raise ValueError(network_config.usd_conversion_error())

        if asset is not None and network_config.network_type == NetworkType.EVM:
            for token in network_config.tokens.values():
                if token.address.lower() == asset.lower() and not token.usd_pegged:
                    raise ValueError(
                        "This asset is not pegged to USD. EURC prices are euros; "
                        "use explicit atomic requirements and the envelope builders, "
                        "not expected_amount_usd. No FX conversion is performed."
                    )

        # Convert USD to token amount. With an explicit decimals the conversion
        # stays exact: float(Decimal("0.07")) is 0.070000000000000007, and
        # at 18 decimals that rounds into a different amount than the payer
        # signed, which the facilitator rejects. Digits below one base unit
        # raise on both paths instead of being truncated.
        if token_decimals is not None:
            if token_decimals < 0:
                raise ValueError(f"token_decimals must be non-negative, got {token_decimals}")
            expected_amount_wei = to_base_units(expected_amount_usd, token_decimals)
        else:
            expected_amount_wei = network_config.get_token_amount(expected_amount_usd)

        # Get recipient for this network (allow per-call override)
        recipient = pay_to or self.config.get_recipient(normalized_network)

        # Build base requirements
        # Use original network format (v1 or v2) for facilitator
        requirements = PaymentRequirements(
            scheme="exact",
            network=payload.network,  # Preserve original format
            maxAmountRequired=str(expected_amount_wei),
            resource=self.config.resource_url or f"https://api.example.com/payment",
            description=self.config.description,
            mimeType="application/json",
            payTo=recipient,
            maxTimeoutSeconds=self.config.max_timeout_seconds,
            asset=asset if asset is not None else network_config.usdc_address,
        )

        # EIP-712 domain params: caller override wins; otherwise the SDK
        # registry values for EVM chains (unchanged default behavior).
        if eip712_domain is not None:
            requirements.extra = _validated_eip712_domain(eip712_domain)
        elif network_config.network_type == NetworkType.EVM:
            requirements.extra = {
                "name": network_config.usdc_domain_name,
                "version": network_config.usdc_domain_version,
            }

        return requirements

    # =========================================================================
    # Facilitator Communication
    # =========================================================================

    def _binding(
        self,
        payload: PaymentPayload,
        idempotency_key: Optional[str] = None,
        idempotency_scope: Optional[str] = None,
        receipt_context: Optional[str] = None,
    ) -> "_Binding":
        """The purchase binding of ONE payment handling.

        ``idempotency_key`` (the caller's own, see :func:`new_idempotency_key`)
        or ``idempotency_scope`` (:func:`derive_idempotency_key`) when the
        caller brings one, else a fresh random key; none at all with
        ``config.send_idempotency_key`` off. ``TypeError`` / ``ValueError``
        before anything is sent for a key or scope that is not a string, a
        malformed key, or both at once.
        """
        if idempotency_scope is not None and not isinstance(idempotency_scope, str):
            raise TypeError(
                f"idempotency_scope must be a string, got {type(idempotency_scope).__name__}"
            )
        scope = idempotency_scope if idempotency_scope and idempotency_scope.strip() else None
        if idempotency_key is not None:
            _checked_idempotency_key(idempotency_key)
            if scope is not None:
                raise ValueError("pass idempotency_key or idempotency_scope, not both")
        if not self.config.send_idempotency_key:
            return _Binding(key=None, receipt_context=receipt_context, brought=False)
        if idempotency_key is not None:
            return _Binding(key=idempotency_key, receipt_context=receipt_context, brought=True)
        if scope is not None:
            derived = derive_idempotency_key(payload, "settle", scope=scope)
            if derived is not None:
                return _Binding(key=derived, receipt_context=receipt_context, brought=True)
        return _Binding(key=new_idempotency_key(), receipt_context=receipt_context, brought=False)

    def verify_payment(
        self,
        payload: PaymentPayload,
        expected_amount_usd: Decimal,
        pay_to: Optional[str] = None,
        *,
        asset: Optional[str] = None,
        eip712_domain: Optional[Dict[str, str]] = None,
        token_decimals: Optional[int] = None,
        idempotency_scope: Optional[str] = None,
        receipt_context: Optional[str] = None,
        idempotency_key: Optional[str] = None,
    ) -> VerifyResponse:
        """
        Verify payment with the facilitator.

        This validates the signature/authorization without settling on-chain.

        Args:
            payload: Parsed payment payload
            expected_amount_usd: Expected payment amount in USD
            pay_to: Override recipient address (must match auth.to in EIP-3009)
            asset: Override the token contract address (non-USDC settles).
                Must match what settle_payment will use.
            eip712_domain: Override the EIP-712 domain params sent via `extra`
                ({"name": ..., "version": ...})
            idempotency_key: The payment's ``Idempotency-Key``
                (:func:`new_idempotency_key`). Pass the SAME key to
                ``settle_payment``: on a network with receipts the key is the
                purchase binding. Without it (and without
                ``idempotency_scope``) the call sends a fresh key and reports
                it back as ``idempotency_key`` on the response.
            idempotency_scope: A secret order id of the seller, from which the
                key is derived (:func:`derive_idempotency_key`) instead.
            receipt_context: The buyer's validated ``X-UVD-Purchase``,
                forwarded unchanged.

        Returns:
            VerifyResponse from facilitator

        Raises:
            PaymentVerificationError: If verification fails
            FacilitatorError: If facilitator returns an error
            TimeoutError: If request times out
        """
        binding = self._binding(payload, idempotency_key, idempotency_scope, receipt_context)
        return self._verify(
            payload, expected_amount_usd, pay_to, asset=asset,
            eip712_domain=eip712_domain, token_decimals=token_decimals, binding=binding,
        )

    def _verify(
        self,
        payload: PaymentPayload,
        expected_amount_usd: Decimal,
        pay_to: Optional[str],
        *,
        asset: Optional[str],
        eip712_domain: Optional[Dict[str, str]],
        token_decimals: Optional[int],
        binding: "_Binding",
    ) -> VerifyResponse:
        """One ``/verify`` under ``binding``."""
        normalized_network = self.validate_network(payload.network)
        requirements = self._build_payment_requirements(
            payload,
            expected_amount_usd,
            pay_to=pay_to,
            asset=asset,
            eip712_domain=eip712_domain,
            token_decimals=token_decimals,
        )

        if binding.receipt_context is not None:
            context_data = json.loads(base64.b64decode(binding.receipt_context, validate=True))
            requirements.resource = context_data["url"]

        envelope_version = resolve_envelope_version(
            payload, requirements, self.config.x402_version
        )
        verify_request = build_verify_request_for_version(
            payload, requirements, envelope_version
        )

        logger.info(
            f"Verifying payment on {payload.network} for ${expected_amount_usd} "
            f"(x402 v{envelope_version} envelope)"
        )
        logger.debug(f"Verify request: {json.dumps(verify_request, indent=2)}")

        try:
            client = self._get_http_client()
            facilitator_url = self.facilitator_url_for(payload.network)
            response = client.post(
                f"{facilitator_url}/verify",
                json=verify_request,
                headers=self._facilitator_headers(facilitator_url, binding.headers()),
                timeout=self.config.verify_timeout,
            )

            if response.status_code != 200:
                raise FacilitatorError(
                    message=f"Facilitator verify failed with status {response.status_code}",
                    status_code=response.status_code,
                    response_body=response.text,
                    reason=_facilitator_reason(response.text),
                    retry_after=_response_retry_after(response),
                    operation="verify",
                )

            data = response.json()
            verify_response = VerifyResponse(**data)
            verify_response.idempotency_key = binding.key

            if not verify_response.isValid:
                raise PaymentVerificationError(
                    message=f"Payment verification failed: {verify_response.message}",
                    reason=verify_response.invalidReason,
                    errors=verify_response.errors,
                    receipt=verify_response.receipt,
                    status_code=response.status_code,
                    error_reason=verify_response.invalidReason,
                    response_body=_response_text(response),
                )

            logger.info(f"Payment verified! Payer: {verify_response.payer}")
            return verify_response

        except httpx.TimeoutException:
            raise X402TimeoutError(operation="verify", timeout_seconds=self.config.verify_timeout)
        except httpx.RequestError as e:
            raise FacilitatorError(message=f"Facilitator request failed: {e}", operation="verify")

    def settle_payment(
        self,
        payload: PaymentPayload,
        expected_amount_usd: Decimal,
        pay_to: Optional[str] = None,
        *,
        asset: Optional[str] = None,
        eip712_domain: Optional[Dict[str, str]] = None,
        token_decimals: Optional[int] = None,
        retry: bool = False,
        idempotency_scope: Optional[str] = None,
        receipt_context: Optional[str] = None,
        idempotency_key: Optional[str] = None,
        extra: Optional[Dict[str, Any]] = None,
    ) -> SettleResponse:
        """
        Settle payment on-chain via the facilitator.

        This executes the actual on-chain transfer.

        Args:
            payload: Parsed payment payload
            expected_amount_usd: Expected payment amount in USD
            pay_to: Override recipient address (must match auth.to in EIP-3009)
            asset: Override the token contract address (non-USDC settles)
            eip712_domain: Override the EIP-712 domain params sent via `extra`
                ({"name": ..., "version": ...})
            token_decimals: Decimals of the token named by `asset`. Without it
                the USD amount is converted with the network's USDC decimals,
                which mis-prices any token that does not share them (USDC is 7
                decimals on Stellar, 18 on BSC). Pass it whenever `asset` is.
            retry: Opt into the settle retry policy (default: False, single
                attempt exactly as before). When True: up to
                SETTLE_RETRY_ATTEMPTS attempts with exponential backoff on
                transient transport errors and 5xx — but NEVER on 4xx,
                business failures, or a 5xx whose body already carries a
                transaction hash (anti-double-settle guard).
            idempotency_key: The payment's ``Idempotency-Key``: the one its
                ``verify_payment`` carried (``VerifyResponse.idempotency_key``),
                or one the caller created with :func:`new_idempotency_key` and
                stored with the order to resume after a restart. Every attempt
                of this call and its timeout fallback carry it. Without it (and
                without ``idempotency_scope``) the call sends a fresh key.
            idempotency_scope: A secret order id of the seller, from which the
                key is derived (:func:`derive_idempotency_key`) instead.
            receipt_context: The buyer's validated ``X-UVD-Purchase``,
                forwarded unchanged.
            extra: Entries added to ``paymentRequirements.extra``, values of
                any JSON type. ``{"8004-reputation": {"includeProof": True}}``
                asks the facilitator for the settlement's proof, returned in
                ``proof_of_payment`` (``ERC8004_EXTENSION_ID`` names the key).
                An entry the SDK already set (the EIP-712 ``name`` /
                ``version``, Hedera's ``feePayer``) may be repeated but not
                changed: ``ValueError`` before anything is sent.

        Returns:
            SettleResponse from facilitator. ``proof_of_payment`` carries the
            facilitator's ``proofOfPayment`` when it sent one (it does for
            ``extra`` with ``8004-reputation`` on a network with ERC-8004),
            else ``None``; a proof that does not parse is ``None`` too, never
            an error over a payment that settled. ``idempotent_replayed`` is True
            when the facilitator answered from a payment it had already
            admitted (``Idempotent-Replayed: true``) under this call's own
            binding: the key or context the caller brought, or this call's
            own earlier attempt. A replay that reached a call with no binding
            of its own before any of its attempts could have admitted the
            payment is somebody else's purchase: it is raised, never returned.

        Raises:
            PaymentSettlementError: If settlement fails, or with reason
                ``authorization_already_settled`` / ``authorization_in_flight``
                when the facilitator replayed a payment this call did not
                admit (a facilitator before 2.39.0 does that to a bare resend).
            FacilitatorError: If facilitator returns an error
            TimeoutError: If request times out
        """
        binding = self._binding(payload, idempotency_key, idempotency_scope, receipt_context)
        return self._settle(
            payload, expected_amount_usd, pay_to, asset=asset, eip712_domain=eip712_domain,
            token_decimals=token_decimals, retry=retry, binding=binding, extra=extra,
        )

    def _settle(
        self,
        payload: PaymentPayload,
        expected_amount_usd: Decimal,
        pay_to: Optional[str],
        *,
        asset: Optional[str],
        eip712_domain: Optional[Dict[str, str]],
        token_decimals: Optional[int],
        retry: bool,
        binding: "_Binding",
        extra: Optional[Dict[str, Any]] = None,
    ) -> SettleResponse:
        """The settle of one handling: its attempts all carry ``binding``."""
        if not retry:
            return self._settle_once(
                payload, expected_amount_usd, pay_to=pay_to,
                asset=asset, eip712_domain=eip712_domain,
                token_decimals=token_decimals, binding=binding, extra=extra,
            )

        for attempt in range(1, SETTLE_RETRY_ATTEMPTS + 1):
            try:
                return self._settle_once(
                    payload, expected_amount_usd, pay_to=pay_to,
                    asset=asset, eip712_domain=eip712_domain,
                    token_decimals=token_decimals, binding=binding, extra=extra,
                )
            except Exception as exc:
                if attempt == SETTLE_RETRY_ATTEMPTS or not _is_retryable_settle_error(exc):
                    raise
                # The server's own Retry-After wins when it asks for LONGER
                # than the exponential schedule. The writer lease sends
                # `Retry-After: 5` while the local schedule sleeps 1s then 2s,
                # so all three attempts used to land inside the single window
                # the facilitator asked us to wait out, and all three failed.
                # Still floored by the exponential value and still capped by
                # _SETTLE_RETRY_MAX_BACKOFF_SECONDS, so a server cannot stretch
                # one settle indefinitely.
                backoff = min(float(2 ** (attempt - 1)), _SETTLE_RETRY_MAX_BACKOFF_SECONDS)
                asked = retry_after_seconds(exc)
                if asked is not None:
                    backoff = min(max(backoff, asked), _SETTLE_RETRY_MAX_BACKOFF_SECONDS)
                logger.warning(
                    "Settle attempt %d/%d failed (%s%s) — retrying in %.0fs",
                    attempt, SETTLE_RETRY_ATTEMPTS, exc,
                    f", reason={facilitator_reason(exc)}" if facilitator_reason(exc) else "",
                    backoff,
                )
                time.sleep(backoff)

        raise AssertionError("unreachable: settle retry loop returns or raises")

    def try_settle_payment(
        self,
        payload: PaymentPayload,
        expected_amount_usd: Decimal,
        pay_to: Optional[str] = None,
        *,
        asset: Optional[str] = None,
        eip712_domain: Optional[Dict[str, str]] = None,
        token_decimals: Optional[int] = None,
        retry: bool = False,
        idempotency_scope: Optional[str] = None,
        receipt_context: Optional[str] = None,
        idempotency_key: Optional[str] = None,
        extra: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Settle payment without raising on payment-flow errors.

        Same arguments and behavior as :meth:`settle_payment`, but every
        ``X402Error`` is captured and returned as a result dict instead of
        raised. Useful for callers that treat settle failures as data (job
        queues, batch dispatchers) rather than control flow.

        Non-payment exceptions (TypeError, etc. — genuine programming errors)
        still propagate.

        Returns:
            Dict with:
              - ``success`` (bool): whether settlement succeeded
              - ``tx_hash`` (Optional[str]): the on-chain transaction hash.
                May be set even when ``success`` is False — a 5xx whose body
                carries a hash means the facilitator DID broadcast the tx
                (do NOT re-settle; verify on-chain instead).
              - ``payment_id`` (Optional[str]): the facilitator's identifier
                for the payment, when it sent one.
              - ``error_code`` (Optional[str]): its machine-readable ``error``,
                e.g. ``settlement_unconfirmed``.
              - ``error`` (Optional[str]): error message when failed
              - ``proof_of_payment`` (Optional[dict]): on success, the
                facilitator's ``proofOfPayment`` as its own camelCase object
                (``SettleResponse.proof_of_payment`` dumped by alias), or
                ``None`` without one.
              - ``safe_to_retry`` (Optional[bool]): on failure,
                ``FacilitatorError.safe_to_retry``; ``None`` for any other
                error and on success.

            ``tx_hash``, ``payment_id`` and ``error_code`` are what turns a
            refusal to retry into something the caller can act on: without
            them the answer is "do not re-send" with nowhere to look, and
            whoever paid cannot find out whether their money moved. Every key
            is always present, so reading one never depends on the outcome.
        """
        try:
            response = self.settle_payment(
                payload, expected_amount_usd, pay_to=pay_to,
                asset=asset, eip712_domain=eip712_domain,
                token_decimals=token_decimals, retry=retry,
                idempotency_scope=idempotency_scope, receipt_context=receipt_context,
                idempotency_key=idempotency_key, extra=extra,
            )
        except X402Error as exc:
            tx_hash: Optional[str] = None
            payment_id: Optional[str] = None
            error_code: Optional[str] = None
            safe_to_retry: Optional[bool] = None
            if isinstance(exc, FacilitatorError):
                tx_hash = exc.transaction
                payment_id = exc.payment_id
                error_code = exc.error_code
                safe_to_retry = exc.safe_to_retry
            elif isinstance(exc, PaymentSettlementError):
                tx_hash = exc.tx_hash
            return {
                "success": False,
                "tx_hash": tx_hash,
                "payment_id": payment_id,
                "error_code": error_code,
                "error": exc.message,
                "proof_of_payment": None,
                "safe_to_retry": safe_to_retry,
            }
        proof = response.proof_of_payment
        return {
            "success": True,
            "tx_hash": response.get_transaction_hash(),
            "payment_id": None,
            "error_code": None,
            "error": None,
            "proof_of_payment": proof.model_dump(by_alias=True) if proof is not None else None,
            "safe_to_retry": None,
        }

    def _settle_once(
        self,
        payload: PaymentPayload,
        expected_amount_usd: Decimal,
        pay_to: Optional[str] = None,
        asset: Optional[str] = None,
        eip712_domain: Optional[Dict[str, str]] = None,
        token_decimals: Optional[int] = None,
        *,
        binding: "_Binding",
        extra: Optional[Dict[str, Any]] = None,
    ) -> SettleResponse:
        """Single settle attempt — the pre-retry settle_payment body, under ``binding``."""
        normalized_network = self.validate_network(payload.network)
        requirements = self._build_payment_requirements(
            payload,
            expected_amount_usd,
            pay_to=pay_to,
            asset=asset,
            eip712_domain=eip712_domain,
            token_decimals=token_decimals,
        )
        requirements = _merge_caller_extra(requirements, extra)

        if binding.receipt_context is not None:
            context_data = json.loads(base64.b64decode(binding.receipt_context, validate=True))
            requirements.resource = context_data["url"]

        envelope_version = resolve_envelope_version(
            payload, requirements, self.config.x402_version
        )
        settle_request = build_settle_request_for_version(
            payload, requirements, envelope_version
        )

        # Use per-network timeout (Ethereum L1 = 900s, L2s = 90s)
        settle_timeout = self._get_settle_timeout(payload.network)
        facilitator_url = self.facilitator_url_for(payload.network)
        headers = self._facilitator_headers(facilitator_url, binding.headers())
        logger.info(
            f"Settling payment on {payload.network} for ${expected_amount_usd} "
            f"(x402 v{envelope_version} envelope, timeout={settle_timeout}s, "
            f"facilitator={facilitator_url})"
        )
        logger.debug(f"Settle request: {json.dumps(settle_request, indent=2)}")

        try:
            client = self._get_http_client()
            response = client.post(
                f"{facilitator_url}/settle",
                json=settle_request,
                headers=headers,
                timeout=settle_timeout,
            )

            if response.status_code != 200:
                refusal = FacilitatorError(
                    message=f"Facilitator settle failed with status {response.status_code}",
                    status_code=response.status_code,
                    response_body=response.text,
                    reason=_facilitator_reason(response.text),
                    retry_after=_response_retry_after(response),
                    operation="settle",
                )
                if response.status_code == 202:
                    binding.refuse_foreign_replay(response, refusal.receipt, payload.network)
                if response.status_code >= 500:
                    binding.may_have_admitted = True
                raise refusal

            data = response.json()
            if isinstance(data, dict) and "success" not in data and data.get("isValid") is False:
                # x402-rs answers a settle whose re-validation fails in the
                # shape of a verify (`200 {"isValid": false, "invalidReason"}`):
                # a rejection, raised as one instead of failing to parse.
                rejected = VerifyResponse(**data)
                raise PaymentSettlementError(
                    message=f"Payment settlement failed: {rejected.invalidReason}",
                    network=payload.network,
                    reason=rejected.invalidReason or rejected.message,
                    receipt=rejected.receipt,
                    status_code=response.status_code,
                    error_reason=rejected.invalidReason,
                    response_body=_response_text(response),
                )
            settle_response = SettleResponse(**data)
            settle_response.idempotent_replayed = _response_replayed(response)
            settle_response.idempotency_key = binding.key

            if not settle_response.success:
                raise PaymentSettlementError(
                    message=f"Payment settlement failed: {settle_response.message}",
                    network=payload.network,
                    # A failed settle that names a transaction was mined and
                    # reverted: the hash is where the buyer looks.
                    tx_hash=_extract_tx_hash_from_body(data),
                    reason=settle_response.errorReason or settle_response.message,
                    receipt=settle_response.receipt,
                    status_code=response.status_code,
                    error_reason=settle_response.errorReason,
                    response_body=_response_text(response),
                )

            binding.refuse_foreign_replay(response, settle_response.receipt, payload.network)
            tx_hash = settle_response.get_transaction_hash()
            logger.info(
                f"Payment settled! TX: {tx_hash}, Payer: {settle_response.payer}"
                + (" (replayed by the facilitator)" if settle_response.idempotent_replayed else "")
            )
            return settle_response

        except httpx.TimeoutException:
            # ACCION 2: On-chain fallback - check if payment succeeded despite timeout
            logger.warning(
                f"Settle timed out after {settle_timeout}s on {payload.network}, "
                f"checking on-chain state..."
            )
            binding.may_have_admitted = True
            fallback = self._check_settle_fallback(
                settle_request, settle_timeout, facilitator_url, headers=headers
            )
            if fallback:
                fallback.idempotency_key = binding.key
                return fallback
            raise X402TimeoutError(operation="settle", timeout_seconds=settle_timeout)
        except httpx.RequestError as e:
            binding.may_have_admitted = True
            raise FacilitatorError(message=f"Facilitator request failed: {e}", operation="settle")

    def _check_settle_fallback(
        self,
        settle_request: Dict[str, Any],
        settle_timeout: float,
        facilitator_url: Optional[str] = None,
        headers: Optional[Dict[str, str]] = None,
    ) -> Optional[SettleResponse]:
        """
        Check on-chain state after a settle timeout.

        When the HTTP request times out, the on-chain transaction may still
        have succeeded. This queries the facilitator's /settle endpoint again
        with a short timeout to check if the transaction was confirmed.

        Args:
            facilitator_url: The facilitator the timed-out settle was sent to.
                Must be that same one — re-resolving or defaulting could ask a
                DIFFERENT facilitator about a payment it never saw.
            headers: The headers of the timed-out settle: the same purchase
                binding (``Idempotency-Key``, ``X-UVD-Purchase``). Under it a
                settle that completed is answered from the facilitator's cache
                or receipt (``200`` with ``Idempotent-Replayed: true``) instead
                of executing, and on a network with receipts it is the only
                resend that gets that answer back.

        A ``202 settlement_in_progress`` under the same binding means the
        timed-out settle was admitted and is still in flight: the fallback asks
        again (same request, same binding) for up to
        ``SETTLE_IN_FLIGHT_POLL_SECONDS``, pausing as the facilitator says.

        Returns:
            SettleResponse if payment was confirmed on-chain, None otherwise.

        Raises:
            FacilitatorError: The ``202 settlement_in_progress`` itself when the
                settle is still in flight after that budget. Transient
                (:func:`is_transient_error`): a paywall answers 503 +
                ``Retry-After``, never 402, because the payment is moving. Before
                0.89.0 this was a ``TimeoutError`` that the SDK's middlewares
                answered with 402.
            FacilitatorError: The facilitator's own answer when it says the
                authorization was already admitted for a purchase this resend
                does not bind (``409 authorization_already_settled``,
                ``authorization_in_flight``, ``receipt_request_conflict``; see
                :func:`admitted_authorization_code`). With the key on, the
                resend carries the timed-out settle's own key and does not get
                this for its own admission. With ``send_idempotency_key``
                off it is what the timed-out settle gets when it WAS admitted:
                PROBABLY this seller's own payment, but nothing proves it, so
                do not deliver blindly; reconcile the receipt's
                ``settlement.id`` with the seller's own records first. Never a
                402 either, and not a timeout: resending the same request
                without a binding never gets the success back.
        """
        url = facilitator_url or self.config.facilitator_url
        client = self._get_http_client()
        deadline = time.monotonic() + SETTLE_IN_FLIGHT_POLL_SECONDS
        in_flight: Optional[FacilitatorError] = None
        while True:
            try:
                response = client.post(
                    f"{url}/settle",
                    json=settle_request,
                    headers=headers
                    or self._facilitator_headers(url, {"Content-Type": "application/json"}),
                    timeout=30.0,  # Short timeout for fallback check
                )
                if response.status_code == 200:
                    settle_response = SettleResponse(**response.json())
                    if settle_response.success:
                        settle_response.idempotent_replayed = _response_replayed(response)
                        tx_hash = settle_response.get_transaction_hash()
                        logger.info(
                            f"Fallback confirmed payment on-chain! "
                            f"TX: {tx_hash}, Payer: {settle_response.payer}"
                        )
                        return settle_response
                    break
                refusal = FacilitatorError(
                    message=(
                        f"Facilitator settle failed with status {response.status_code} "
                        f"on the resend after a timeout"
                    ),
                    status_code=response.status_code,
                    response_body=response.text,
                    reason=_facilitator_reason(response.text),
                    retry_after=_response_retry_after(response),
                    operation="settle",
                )
            except Exception as e:
                logger.warning(f"Fallback check failed: {e}")
                break

            if admitted_authorization_code(refusal) is not None:
                logger.warning(
                    "Fallback check: the facilitator had already admitted this authorization "
                    "(%s) and this resend does not carry the binding that admitted it. Not a "
                    "success to deliver on, and not a rejection.",
                    refusal.error_code,
                )
                raise refusal
            if not (refusal.status_code == 202 and refusal.retryable):
                break
            # `202 settlement_in_progress` under this handling's own binding: the
            # settle that timed out was admitted and is still in flight. Ask
            # again, the same request under the same binding, while the budget
            # lasts; the facilitator never executes it twice.
            in_flight = refusal
            wait = _in_flight_poll_interval(refusal)
            if time.monotonic() + wait >= deadline:
                break
            time.sleep(wait)

        if in_flight is not None:
            logger.warning(
                "Fallback check: the settle is still in flight after %.0fs; the same "
                "request under the same binding gets its answer later",
                SETTLE_IN_FLIGHT_POLL_SECONDS,
            )
            raise in_flight
        logger.warning("Fallback check: payment not confirmed on-chain")
        return None

    # =========================================================================
    # Main Processing Method
    # =========================================================================

    def process_payment(
        self,
        x_payment_header: str,
        expected_amount_usd: Decimal,
        pay_to: Optional[str] = None,
        *,
        asset: Optional[str] = None,
        eip712_domain: Optional[Dict[str, str]] = None,
        token_decimals: Optional[int] = None,
        idempotency_scope: Optional[str] = None,
        receipt_context: Optional[str] = None,
        idempotency_key: Optional[str] = None,
    ) -> PaymentResult:
        """
        Process a complete x402 payment (verify + settle).

        This is the main method for handling payments. It:
        1. Extracts and validates the payment payload
        2. Verifies the payment signature with the facilitator
        3. Settles the payment on-chain
        4. Returns the payment result

        One call is one handling of the payment: its verify, its settle and
        the settle's timeout fallback carry ONE ``Idempotency-Key``.

        Args:
            x_payment_header: X-PAYMENT header value (base64-encoded JSON)
            expected_amount_usd: Expected payment amount in USD
            pay_to: Override recipient address (must match auth.to in EIP-3009)
            asset: Override the token contract address (non-USDC settles).
                Applied to BOTH the verify and the settle requirements.
            eip712_domain: Override the EIP-712 domain params sent via `extra`
                ({"name": ..., "version": ...}). Applied to both steps.
            token_decimals: Decimals of the token named by `asset`. Without it
                the USD amount is converted with the network's USDC decimals,
                which mis-prices any token that does not share them. Pass it
                whenever `asset` is passed. Applied to both steps.
            idempotency_key: The payment's key, created with
                :func:`new_idempotency_key` and stored with the order before
                this call, to resume this payment after a restart. Without it
                (and without ``idempotency_scope``) the call uses a fresh key.
            idempotency_scope: A secret order id of the seller, from which the
                key is derived (:func:`derive_idempotency_key`) instead.
            receipt_context: The buyer's validated ``X-UVD-Purchase``,
                forwarded unchanged to both steps.

        Returns:
            PaymentResult with payer address, transaction hash, etc.
            ``idempotent_replayed`` is True when the settle was the
            facilitator's replay of this purchase's own admitted payment: under
            the key or context the caller brought, or answering this call's own
            timed-out settle. ``idempotency_key`` is the key the call carried.

        Raises:
            InvalidPayloadError: If payload is invalid
            UnsupportedNetworkError: If network is not supported
            PaymentVerificationError: If verification fails, including an
                authorization the facilitator already admitted for another
                request (see :func:`admitted_authorization_code`)
            PaymentSettlementError: If settlement fails, or the facilitator
                replayed a payment this call did not admit
            FacilitatorError: If facilitator returns an error
            TimeoutError: If request times out
        """
        # Extract payload
        payload = self.extract_payload(x_payment_header)
        logger.info(f"Processing payment: network={payload.network}, amount=${expected_amount_usd}")
        binding = self._binding(payload, idempotency_key, idempotency_scope, receipt_context)
        return self._handle_payment(
            payload, expected_amount_usd, pay_to, asset=asset,
            eip712_domain=eip712_domain, token_decimals=token_decimals, binding=binding,
        )

    def _handle_payment(
        self,
        payload: PaymentPayload,
        expected_amount_usd: Decimal,
        pay_to: Optional[str],
        *,
        asset: Optional[str],
        eip712_domain: Optional[Dict[str, str]],
        token_decimals: Optional[int],
        binding: "_Binding",
    ) -> PaymentResult:
        """The verify and the settle of :meth:`process_payment`, under ``binding``.

        Also what :func:`~uvd_x402_sdk.bindings.process_payment_bound` runs, with
        the key its store persisted: a key the store minted for THIS request
        keeps the fresh key's guard against a replay nobody here admitted.
        """
        # Verify payment
        verify_response = self._verify(
            payload, expected_amount_usd, pay_to, asset=asset,
            eip712_domain=eip712_domain, token_decimals=token_decimals, binding=binding,
        )

        # Settle payment, under the key the verification carried
        settle_response = self._settle(
            payload, expected_amount_usd, pay_to, asset=asset, eip712_domain=eip712_domain,
            token_decimals=token_decimals, retry=False, binding=binding,
        )

        # Build result
        return PaymentResult(
            success=True,
            payer_address=settle_response.payer or verify_response.payer or "",
            transaction_hash=settle_response.get_transaction_hash(),
            network=payload.network,
            amount_usd=expected_amount_usd,
            receipt=settle_response.receipt,
            idempotent_replayed=settle_response.idempotent_replayed,
            idempotency_key=binding.key,
        )

    # =========================================================================
    # Accepts Negotiation (Faremeter middleware compatibility)
    # =========================================================================

    def negotiate_accepts(
        self,
        payment_requirements: list[Dict[str, Any]],
        *,
        x402_version: int = 2,
    ) -> list[Dict[str, Any]]:
        """
        Negotiate payment requirements with the facilitator via POST /accepts.

        Sends merchant payment requirements to the facilitator, which matches
        them against its supported capabilities and returns enriched requirements
        with facilitator data (feePayer, tokens, escrow configuration).

        This is used by Faremeter middleware and clients that need to discover
        what the facilitator can settle before constructing payment authorizations.

        Args:
            payment_requirements: List of payment requirement objects
            x402_version: x402 protocol version (default: 2)

        Returns:
            List of enriched payment requirements with facilitator extras

        Raises:
            FacilitatorError: If the facilitator returns an error

        Example:
            >>> requirements = [
            ...     {
            ...         "scheme": "exact",
            ...         "network": "base-mainnet",
            ...         "maxAmountRequired": "1000000",
            ...         "resource": "https://api.example.com/data",
            ...         "payTo": "0xMerchant...",
            ...     }
            ... ]
            >>> enriched = client.negotiate_accepts(requirements)
            >>> # enriched[0]["extra"]["feePayer"] is now set
        """
        facilitator_url = self._accepts_facilitator_url(payment_requirements)
        url = f"{facilitator_url}/accepts"
        payload = {
            "x402Version": x402_version,
            "accepts": payment_requirements,
        }

        try:
            client = self._get_http_client()
            response = client.post(
                url,
                json=payload,
                headers=self._facilitator_headers(
                    facilitator_url, {"Content-Type": "application/json"}
                ),
                timeout=self.config.verify_timeout,
            )
            response.raise_for_status()
            data = response.json()
            return data.get("accepts", [])
        except httpx.HTTPStatusError as e:
            raise FacilitatorError(
                message=f"Facilitator /accepts error: {e.response.status_code}",
                status_code=e.response.status_code,
                response_body=e.response.text,
            )
        except httpx.TimeoutException:
            raise X402TimeoutError(operation="accepts", timeout_seconds=self.config.verify_timeout)
        except Exception as e:
            raise FacilitatorError(message=f"Facilitator /accepts error: {e}")

    def _accepts_facilitator_url(self, payment_requirements: list) -> str:
        """Resolve the facilitator for a /accepts negotiation.

        One request cannot span two facilitators: if the requirements name
        networks that route to different ones, the caller must split the call
        rather than have the SDK pick a winner.
        """
        from uvd_x402_sdk.exceptions import ConfigurationError

        urls = {
            self.facilitator_url_for(req["network"])
            for req in payment_requirements
            if isinstance(req, dict) and req.get("network")
        }
        if not urls:
            return self.config.facilitator_url
        if len(urls) > 1:
            raise ConfigurationError(
                f"negotiate_accepts got requirements spanning {len(urls)} facilitators "
                f"({', '.join(sorted(urls))}). Split them into one call per facilitator.",
                config_key="facilitator_by_network",
            )
        return urls.pop()

    # =========================================================================
    # Facilitator Info Methods
    # =========================================================================

    def get_version(self) -> Dict[str, Any]:
        """
        Get the facilitator version info.

        Returns:
            Dict with version information (e.g., {"version": "1.37.0"})

        Raises:
            FacilitatorError: If the request fails
        """
        try:
            client = self._get_http_client()
            response = client.get(
                f"{self.config.facilitator_url}/version",
                **self._facilitator_kwargs(self.config.facilitator_url),
            )
            response.raise_for_status()
            return response.json()
        except httpx.HTTPStatusError as e:
            raise FacilitatorError(
                message=f"GET /version failed: {e.response.status_code}",
                status_code=e.response.status_code,
                response_body=e.response.text,
            )
        except Exception as e:
            raise FacilitatorError(message=f"GET /version failed: {e}")

    def get_supported(self, network: Optional[str] = None) -> Dict[str, Any]:
        """
        Get the facilitator's supported networks and payment schemes.

        Args:
            network: Ask the facilitator that settles this network. Only
                meaningful with a `facilitator_by_network` table configured;
                without one every network resolves to the same facilitator.

        Returns:
            Dict with 'kinds' array of supported network/scheme combos

        Example:
            >>> supported = client.get_supported()
            >>> for kind in supported["kinds"]:
            ...     print(f"{kind['network']} - {kind['scheme']}")

        Raises:
            FacilitatorError: If the request fails
        """
        base_url = self.facilitator_url_for(network) if network else self.config.facilitator_url
        try:
            client = self._get_http_client()
            response = client.get(
                f"{base_url}/supported", **self._facilitator_kwargs(base_url)
            )
            response.raise_for_status()
            return response.json()
        except httpx.HTTPStatusError as e:
            raise FacilitatorError(
                message=f"GET /supported failed: {e.response.status_code}",
                status_code=e.response.status_code,
                response_body=e.response.text,
            )
        except Exception as e:
            raise FacilitatorError(message=f"GET /supported failed: {e}")

    def get_stats(self) -> Dict[str, Any]:
        """Aggregated totals per network and asset (``GET /api/stats``).

        Returns:
            Dict with ``totals``, ``byNetworkAndAsset`` and the caveats the
            facilitator attaches to its own numbers.

        Note:
            **This is an index, not a ledger.** Records are written best-effort
            AFTER settlement, so an outage loses rows while payments proceed —
            verify anything that matters against the transaction hash.

            Counting starts when the operator enabled the store; earlier
            operations are UNKNOWN, not zero. And unless the operator set
            ``X402_EVENTS_PUBLISH_FAILURES=true``, operations that ERROR are not
            recorded at all, so a 100% success rate means "no failures were
            recorded".

            ``volumeAtomic`` is a STRING (u256-shaped; a float loses precision
            above 2^53) and each row carries its own ``decimals``. **Use that,
            never a constant** — USDC is 6 decimals nearly everywhere and 18 on
            BSC, so scaling by 6 there overstates volume by 10^12. ``decimals``
            is null when the facilitator does not recognise the asset; render the
            atomic value rather than guessing.

        Raises:
            FacilitatorError: If the request fails or the store is unconfigured.
        """
        return self._get_json("/api/stats")

    def get_transactions(
        self,
        limit: int = 50,
        network: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Recent recorded operations, newest first (``GET /transactions``).

        Args:
            limit: Rows to return. The facilitator CLAMPS this to 200.
            network: Canonical slug, e.g. ``base``. Matches the name
                ``/supported`` uses, which is not always the alias you may send:
                ``skale`` is accepted inbound but records say ``skale-base``.

        Note:
            There is **no pagination and no cursor**. This returns the newest N,
            walking back at most 30 days. With 10,000 rows you get the newest
            200 — not page one of fifty.

        Raises:
            FacilitatorError: If the request fails or the store is unconfigured.
        """
        params = f"?limit={limit}"
        if network:
            params += f"&network={network}"
        return self._get_json(f"/transactions{params}")

    def _get_json(self, path: str) -> Dict[str, Any]:
        """GET a facilitator endpoint and return its JSON."""
        try:
            client = self._get_http_client()
            response = client.get(
                f"{self.config.facilitator_url}{path}",
                **self._facilitator_kwargs(self.config.facilitator_url),
            )
            response.raise_for_status()
            return response.json()
        except httpx.HTTPStatusError as e:
            raise FacilitatorError(
                message=f"GET {path} failed: {e.response.status_code}",
                status_code=e.response.status_code,
                response_body=e.response.text,
            )
        except Exception as e:
            raise FacilitatorError(message=f"GET {path} failed: {e}")

    def get_blacklist(self) -> Dict[str, Any]:
        """
        Get the facilitator's blocked/sanctioned addresses.

        Returns:
            Dict with blacklist info (totalBlocked, loadedAtStartup, addresses)

        Example:
            >>> bl = client.get_blacklist()
            >>> print(f"Blocked: {bl['totalBlocked']} addresses")

        Raises:
            FacilitatorError: If the request fails
        """
        try:
            client = self._get_http_client()
            response = client.get(
                f"{self.config.facilitator_url}/blacklist",
                **self._facilitator_kwargs(self.config.facilitator_url),
            )
            response.raise_for_status()
            return response.json()
        except httpx.HTTPStatusError as e:
            raise FacilitatorError(
                message=f"GET /blacklist failed: {e.response.status_code}",
                status_code=e.response.status_code,
                response_body=e.response.text,
            )
        except Exception as e:
            raise FacilitatorError(message=f"GET /blacklist failed: {e}")

    def health_check(self, network: Optional[str] = None) -> bool:
        """
        Check facilitator health.

        Args:
            network: Check the facilitator that settles this network. Without it
                (or without a `facilitator_by_network` table) the default
                facilitator is checked — which says nothing about the others.

        Returns:
            True if the facilitator is healthy
        """
        base_url = self.facilitator_url_for(network) if network else self.config.facilitator_url
        try:
            client = self._get_http_client()
            response = client.get(
                f"{base_url}/health", **self._facilitator_kwargs(base_url)
            )
            return response.is_success
        except Exception:
            return False

    # =========================================================================
    # Convenience Methods
    # =========================================================================

    def get_payer_address(self, x_payment_header: str) -> Tuple[str, str]:
        """
        Extract payer address from payment header without processing.

        Useful for logging or pre-validation.

        Args:
            x_payment_header: X-PAYMENT header value

        Returns:
            Tuple of (payer_address, network)
        """
        payload = self.extract_payload(x_payment_header)

        # Normalize network name
        normalized_network = payload.get_normalized_network()

        # Extract payer based on network type
        network_config = get_network(normalized_network)
        if not network_config:
            raise UnsupportedNetworkError(
                network=payload.network,
                supported_networks=get_supported_network_names(),
            )

        payer = ""
        if network_config.network_type == NetworkType.EVM:
            evm_payload = payload.get_evm_payload()
            payer = evm_payload.authorization.from_address
        elif network_config.network_type == NetworkType.STELLAR:
            stellar_payload = payload.get_stellar_payload()
            payer = stellar_payload.from_address
        # For SVM/NEAR/XRPL, payer is determined during verification
        # (XRPL t54 carries only the signed tx blob; the sender is recovered
        #  by the facilitator when it decodes/submits the Payment transaction)

        return payer, normalized_network

    def verify_only(
        self,
        x_payment_header: str,
        expected_amount_usd: Decimal,
        pay_to: Optional[str] = None,
    ) -> Tuple[bool, str]:
        """
        Verify payment without settling.

        Useful for checking payment validity before committing to settlement.

        Args:
            x_payment_header: X-PAYMENT header value
            expected_amount_usd: Expected payment amount
            pay_to: Override recipient address (must match auth.to in EIP-3009)

        Returns:
            Tuple of (is_valid, payer_address)
        """
        payload = self.extract_payload(x_payment_header)
        verify_response = self.verify_payment(payload, expected_amount_usd, pay_to=pay_to)
        return verify_response.isValid, verify_response.payer or ""

    # =========================================================================
    # Client-Side Signing (Server-side signer without browser wallet)
    # =========================================================================

    def connect_with_hedera(self, account_id: str, private_key: str, *, network: str,
                            fee_payer: Optional[str] = None) -> str:
        """Connect an offline native Hedera buyer (Python 3.10+, [hedera] extra)."""
        from uvd_x402_sdk.hedera import HederaSigner
        signer = HederaSigner(account_id, private_key, network=network, fee_payer=fee_payer)
        self._hedera_signer = signer
        self._signer = None
        self._sign_typed_data = None
        self._signer_address = account_id
        self._connected_chain = network
        return account_id

    def connect_with_private_key(
        self,
        private_key: str,
        chain_name: Optional[str] = None,
    ) -> str:
        """
        Connect a wallet using a private key for server-side signing.

        Creates an EVM signer from the private key, enabling the client to
        create signed EIP-3009 TransferWithAuthorization payloads without
        a browser wallet.

        Requires: pip install uvd-x402-sdk[signer]

        Args:
            private_key: Hex-encoded private key (with or without 0x prefix)
            chain_name: Network to connect to (e.g., 'skale-base', 'base').
                        If None, must be specified when creating authorizations.

        Returns:
            The wallet address derived from the private key

        Raises:
            ImportError: If eth-account is not installed
            UnsupportedNetworkError: If chain_name is not a valid EVM network
            ValueError: If private key is invalid

        Example:
            >>> client = X402Client(recipient_address="0xMerchant...")
            >>> address = client.connect_with_private_key(
            ...     os.environ['PRIVATE_KEY'],
            ...     'skale-base'
            ... )
            >>> print(f"Connected: {address}")
        """
        try:
            from eth_account import Account
        except ImportError:
            raise ImportError(
                "eth-account is required for connect_with_private_key. "
                "Install it with: pip install uvd-x402-sdk[signer]"
            )

        # Validate chain if provided
        if chain_name:
            try:
                normalized = normalize_network(chain_name)
            except ValueError:
                raise UnsupportedNetworkError(
                    network=chain_name,
                    supported_networks=get_supported_network_names(),
                )
            network_config = get_network(normalized)
            if not network_config:
                raise UnsupportedNetworkError(
                    network=chain_name,
                    supported_networks=get_supported_network_names(),
                )
            if network_config.network_type != NetworkType.EVM:
                raise UnsupportedNetworkError(
                    network=chain_name,
                    supported_networks=[
                        n for n in get_supported_network_names()
                        if get_network(n) and get_network(n).network_type == NetworkType.EVM
                    ],
                )
            self._connected_chain = normalized
        else:
            self._connected_chain = None

        # Create account from private key
        self._signer = Account.from_key(private_key)
        self._signer_address = self._signer.address

        def _sign_local(domain_data, types, message) -> str:
            from eth_account.messages import encode_typed_data

            signable = encode_typed_data(
                domain_data=domain_data,
                message_types=types,
                message_data=message,
            )
            sig = self._signer.sign_message(signable).signature.hex()
            # hexbytes < 1.0 returns bare hex; >= 1.0 returns it 0x-prefixed. The
            # previous code did "0x" + sig unconditionally, which yields "0x0x…"
            # on the newer release. Normalising here keeps the old behaviour and
            # fixes that case.
            return sig if sig.startswith("0x") else "0x" + sig

        self._hedera_signer = None
        self._sign_typed_data = _sign_local

        logger.info(f"Connected wallet {self._signer_address}"
                     + (f" on {self._connected_chain}" if self._connected_chain else ""))

        return self._signer_address

    def connect_with_signer(
        self,
        signer: Any,
        chain_name: Optional[str] = None,
    ) -> str:
        """
        Connect an EXTERNAL signer that holds the key outside this process.

        The SDK could previously only sign with a raw private key loaded into
        memory (:meth:`connect_with_private_key`). That rules out every setup
        where the key is deliberately somewhere else — an HSM, a KMS, a
        delegated/agentic wallet, an MPC service — which is exactly the setup a
        production agent wants. This is the seam for those.

        Args:
            signer: any object exposing

                * ``address`` -> the checksummed EOA these signatures recover to
                  (a plain attribute or a property)
                * ``sign_typed_data(domain, types, message)`` -> the 65-byte
                  EIP-712 signature as a hex string (``0x``-prefixed or not)

            chain_name: network to bind to, same semantics as
                :meth:`connect_with_private_key`.

        Returns:
            The signer's address.

        Raises:
            TypeError: if the object does not implement the two members above.
                Checked up front ON PURPOSE: a missing method discovered at
                signing time fails after the caller already believes it is
                connected.

        Example:
            >>> class MyRemoteSigner:
            ...     address = "0x…"
            ...     def sign_typed_data(self, domain, types, message):
            ...         return remote_hsm.sign_eip712(domain, types, message)
            >>> client.connect_with_signer(MyRemoteSigner(), chain_name="base")
        """
        address = getattr(signer, "address", None)
        if not isinstance(address, str) or not address.startswith("0x"):
            raise TypeError(
                "signer.address must be a 0x-prefixed address string; got "
                f"{address!r}"
            )
        if not callable(getattr(signer, "sign_typed_data", None)):
            raise TypeError(
                "signer must implement sign_typed_data(domain, types, message) "
                "returning a hex signature"
            )

        if chain_name:
            try:
                normalized = normalize_network(chain_name)
            except ValueError:
                raise UnsupportedNetworkError(
                    network=chain_name,
                    supported_networks=get_supported_network_names(),
                )
            self._connected_chain = normalized
        else:
            self._connected_chain = None

        self._signer_address = address

        def _sign_remote(domain_data, types, message) -> str:
            sig = signer.sign_typed_data(domain_data, types, message)
            if isinstance(sig, (bytes, bytearray)):
                sig = sig.hex()
            if not isinstance(sig, str):
                raise TypeError(
                    f"sign_typed_data must return a hex string, got {type(sig)}"
                )
            return sig if sig.startswith("0x") else "0x" + sig

        self._hedera_signer = None
        self._sign_typed_data = _sign_remote

        logger.info(
            f"Connected external signer {address}"
            + (f" on {self._connected_chain}" if self._connected_chain else "")
        )
        return address

    @property
    def is_connected(self) -> bool:
        """Check if a signer is connected (private key OR external)."""
        return self._sign_typed_data is not None or self._hedera_signer is not None

    @property
    def address(self) -> Optional[str]:
        """Get the connected wallet address."""
        return self._signer_address

    @property
    def connected_chain(self) -> Optional[str]:
        """Get the connected chain name."""
        return self._connected_chain

    def create_authorization(
        self,
        pay_to: str,
        amount_usd: Decimal,
        *,
        chain_name: Optional[str] = None,
        valid_duration: int = 3600,
        token_type: str = "usdc",
        x402_version: int = 1,
        accepted: Optional[Dict[str, Any]] = None,
        resource: Optional[Union[str, Dict[str, Any]]] = None,
        extensions: Optional[Any] = None,
        eip712_domain: Optional[Dict[str, str]] = None,
    ) -> str:
        """
        Create a signed EIP-3009 payment authorization (X-PAYMENT header value).

        Signs a TransferWithAuthorization message and returns a base64-encoded
        payload ready to be sent as the X-PAYMENT header.

        Args:
            pay_to: Recipient address
            amount_usd: Whole units of the selected token (legacy parameter
                name): euros for EURC, dollars for USDC. No FX conversion.
            chain_name: Network name (uses connected chain if not specified)
            valid_duration: Authorization validity in seconds (default: 1 hour)
            token_type: Token to pay with (default: 'usdc')
            eip712_domain: Override the EIP-712 domain used to SIGN
                ({"name": ..., "version": ...}). The domain is part of the
                signed digest — if the server/facilitator resolves a different
                name/version than the SDK registry (they diverge on some
                chains), the signature will not verify unless the caller
                injects the domain the verifier expects.

        The amount is converted as the settle converts it
        (:func:`~uvd_x402_sdk.networks.base.to_base_units`): float noise is
        rounded to the nearest base unit (``0.3 - 0.1`` signs 200000 at 6
        decimals), so the payer signs what the seller's requirements ask for.

        Returns:
            Base64-encoded X-PAYMENT header value

        Raises:
            RuntimeError: If no signer is connected
            ImportError: If eth-account is not installed
            UnsupportedNetworkError: If chain is invalid
            ValueError: If the amount has a real digit below one base unit
                (``0.0000015`` at 6 decimals), is negative or is not finite,
                before anything is signed

        Example:
            >>> header = client.create_authorization(
            ...     pay_to="0xRecipient...",
            ...     amount_usd=Decimal("0.01"),
            ... )
            >>> response = httpx.get(
            ...     "https://api.example.com/data",
            ...     headers={"X-PAYMENT": header}
            ... )
        """
        if not self._sign_typed_data and self._hedera_signer is None:
            raise RuntimeError(
                "No signer connected. Call connect_with_private_key() or "
                "connect_with_signer() first."
            )

        if self._hedera_signer is not None:
            from uvd_x402_sdk.hedera import validate_hedera_requirements
            if x402_version != 2 or accepted is None or extensions:
                raise ValueError("Hedera requires v2, accepted requirements and no extensions")
            r = validate_hedera_requirements(accepted)
            # Native USDC is the only Hedera payment asset.
            if token_type != "usdc" or r["asset"] == "0.0.0":
                raise ValueError("Hedera payments support native USDC only; HBAR is for network fees")
            if (r["payTo"] != pay_to or Decimal(r["amount"]) != Decimal(str(amount_usd)) * 10**6
                    or (chain_name and normalize_network(chain_name) != r["network"])):
                raise ValueError("Hedera offer differs from the approved price, recipient or network")
            info = resource if isinstance(resource, dict) else {"url": resource or ""}
            return self._hedera_signer.create_payment_header(r, resource=info)

        # NOTE: eth-account is NOT imported here any more. It is only needed by the
        # local-key path, which imports it inside its own closure — so an external
        # signer (connect_with_signer) works without the [signer] extra installed.

        # Resolve chain
        chain = chain_name or self._connected_chain
        if not chain:
            raise ValueError(
                "No chain specified. Pass chain_name or connect with a chain."
            )
        try:
            normalized = normalize_network(chain)
        except ValueError:
            raise UnsupportedNetworkError(
                network=chain,
                supported_networks=get_supported_network_names(),
            )
        network_config = get_network(normalized)
        if not network_config:
            raise UnsupportedNetworkError(
                network=chain,
                supported_networks=get_supported_network_names(),
            )
        if network_config.network_type != NetworkType.EVM:
            raise UnsupportedNetworkError(
                network=chain,
                supported_networks=[
                    n for n in get_supported_network_names()
                    if get_network(n) and get_network(n).network_type == NetworkType.EVM
                ],
            )

        # Get token config
        from uvd_x402_sdk.networks.base import get_token_config
        token_config = get_token_config(normalized, token_type)
        if not token_config:
            raise ValueError(
                f"Token '{token_type}' not supported on {normalized}"
            )

        # Convert amount to base units with the settle's own conversion: float
        # noise rounds to the nearest base unit as the seller's requirements
        # round it, and a real digit below one base unit raises here, before
        # anything is signed. int() truncated: 0.3 - 0.1 signed 199999 where
        # the settle requires 200000, which the facilitator refuses.
        eurc_error = "EURC amount must be positive euros with at most 6 decimal places"
        try:
            amount_base = to_base_units(
                amount_usd, token_config.decimals, unit=f"{token_type.upper()} on {normalized}"
            )
        except ValueError as exc:
            if token_type == "eurc":
                raise ValueError(eurc_error) from exc
            raise
        if token_type == "eurc" and amount_base <= 0:
            raise ValueError(eurc_error)

        # Build EIP-3009 TransferWithAuthorization
        now = int(time.time())
        valid_after = 0
        valid_before = now + valid_duration
        nonce = "0x" + os.urandom(32).hex()

        # EIP-712 domain — registry values unless the caller injects its own
        # (name/version divergences between registries are real, and a wrong
        # domain silently produces a signature the verifier rejects).
        if eip712_domain is not None:
            domain_override = _validated_eip712_domain(eip712_domain)
            domain_name = domain_override["name"]
            domain_version = domain_override["version"]
        else:
            domain_name = token_config.name
            domain_version = token_config.version

        domain_data = {
            "name": domain_name,
            "version": domain_version,
            "chainId": network_config.chain_id,
            "verifyingContract": token_config.address,
        }

        # EIP-3009 types
        types = {
            "TransferWithAuthorization": [
                {"name": "from", "type": "address"},
                {"name": "to", "type": "address"},
                {"name": "value", "type": "uint256"},
                {"name": "validAfter", "type": "uint256"},
                {"name": "validBefore", "type": "uint256"},
                {"name": "nonce", "type": "bytes32"},
            ],
        }

        # Message
        message = {
            "from": self._signer_address,
            "to": pay_to,
            "value": amount_base,
            "validAfter": valid_after,
            "validBefore": valid_before,
            "nonce": nonce,
        }

        # Sign through the normalised seam: identical bytes whether the key is
        # local (eth_account) or remote (connect_with_signer).
        signature = self._sign_typed_data(domain_data, types, message)

        # Build x402 payload
        inner = {
            "signature": signature,
            "authorization": {
                "from": self._signer_address,
                "to": pay_to,
                "value": str(amount_base),
                "validAfter": str(valid_after),
                "validBefore": str(valid_before),
                "nonce": nonce,
            },
        }

        if int(x402_version) >= 2:
            # v2 envelope (x402 spec v2 §5.2). No top-level scheme/network: the CHOSEN
            # accept is echoed back VERBATIM as `accepted`, so the seller can match it
            # against what it advertised. Reconstructing it instead of echoing is how a
            # payment gets rejected by a server that did nothing wrong.
            if not accepted:
                raise ValueError(
                    "x402_version=2 requires `accepted`: the accept object from the 402, "
                    "echoed back verbatim. Rebuilding it makes the seller reject the payment."
                )
            payload: Dict[str, Any] = {
                "x402Version": 2,
                "accepted": dict(accepted),
                "payload": inner,
            }
            if resource:
                # `resource` is a ResourceInfo OBJECT, not the bare URL. The facilitator
                # requires url + description + mimeType; a plain string matches NO variant
                # of the VerifyRequestEnvelope and fails with the opaque
                # "data did not match any variant". A dict that already arrived well-formed
                # is passed through untouched — normalising what was already right is how a
                # working case breaks.
                payload["resource"] = resource if isinstance(resource, dict) else {
                    "url": str(resource),
                    "description": (accepted.get("description") or ""),
                    "mimeType": (accepted.get("mimeType") or "application/json"),
                }
            if extensions:
                # Spec §5: when the server declares extensions the client MUST echo at
                # least what it received. Strict servers reject a payment with a missing
                # echo by RE-SERVING the 402 with no hint — indistinguishable from "you
                # sent no payment at all", which is what makes it expensive to diagnose.
                payload["extensions"] = extensions
        else:
            payload = {
                "x402Version": 1,
                "scheme": "exact",
                "network": network_config.name,
                "payload": inner,
            }

        # Add token info for non-USDC tokens. Carries the EFFECTIVE domain
        # (override included): the eip712 block exists so the verifier resolves
        # the same domain the signature was produced with.
        if token_type != "usdc":
            payload["payload"]["token"] = {
                "address": token_config.address,
                "symbol": token_type.upper(),
                "eip712": {
                    "name": domain_name,
                    "version": domain_version,
                },
            }

        # Encode to base64
        json_bytes = json.dumps(payload).encode("utf-8")
        return base64.b64encode(json_bytes).decode("utf-8")

    # =========================================================================
    # Buyer loop (payer side): fetch a resource, pay the 402, retry
    # =========================================================================

    def _parse_402(
        self, body: Dict[str, Any]
    ) -> Tuple[int, List[Dict[str, Any]], ParsedAccepts]:
        """Normalise a 402 body into (x402_version, [payment options], parsed).

        Handles both the spec shape ``{x402Version, accepts: [...]}`` (v1 and v2)
        and the non-spec shape where a single requirement sits at the top level.
        Each option is normalised to ``{network, asset, amount, payTo,
        eip712_domain, raw, offer}`` -- ``amount`` in token base units, ``raw``
        the original accept object (echoed verbatim for v2), ``offer`` the
        :class:`~uvd_x402_sdk.policy.Offer` the policy will be evaluated
        against.

        The third element carries the offers this build could NOT read, with
        their scheme names, and the challenge's ``extensions`` (where the seller
        declares how long its offer stands). One unreadable entry no longer
        takes the list with it: a seller advertising ``exact`` beside a scheme we
        do not implement stays payable, and a challenge where NOTHING is
        readable can name what the seller actually offered.
        """
        version = int(body.get("x402Version", 1))
        parsed = parse_accepts(body)

        options: List[Dict[str, Any]] = [
            {
                "network": offer.network,
                "asset": offer.asset or None,
                "amount": str(offer.amount),
                "payTo": offer.pay_to,
                "eip712_domain": offer.extra,
                "raw": offer.raw,
                "offer": offer,
            }
            for offer in parsed.offers
        ]
        return version, options, parsed

    def _select_payment_option(
        self,
        options: List[Dict[str, Any]],
        token_decimals: int,
        token_type: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Default selector: the cheapest option, ignoring the ceiling.

        The ``max_amount`` ceiling is NOT applied here on purpose: if even the
        cheapest option is over budget, the caller wants a clear "this costs
        more than you allowed" (:class:`PaymentExceedsMaxError`), not a vague
        "no acceptable option". So this always returns the cheapest, and
        :meth:`fetch` enforces the ceiling with one explicit check afterwards.

        With ``token_type``, the cheapest is taken among the options that
        token signs as offered: those whose ``asset`` is its address on the
        option's own network, and those that name no asset. Only when there is
        none is it taken among them all, as without ``token_type``.
        """
        if not options:
            return None
        if token_type is not None:
            signable = [opt for opt in options if _signs_as_offered(opt, token_type)]
            options = signable or options
        scale = Decimal(10) ** token_decimals
        return min(options, key=lambda opt: Decimal(opt["amount"]) / scale)

    def fetch_with_receipt(self, url: str, *, context: Any, persist: Any, **kwargs: Any) -> Any:
        """Fetch or resume one purchase, retaining its facilitator receipt."""
        from uvd_x402_sdk.receipts import fetch_with_receipt
        return fetch_with_receipt(self, url, context=context, persist=persist, **kwargs)

    def fetch(
        self,
        url: str,
        *,
        method: str = "GET",
        max_amount: Optional[Union[Decimal, str, float]] = None,
        token_type: str = "usdc",
        token_decimals: int = 6,
        select: Optional[Any] = None,
        valid_duration: int = 3600,
        eip712_domain: Optional[Dict[str, str]] = None,
        http_client: Optional[httpx.Client] = None,
        policy: Optional[PurchasePolicy] = None,
        quote: Optional[AdvertisedQuote] = None,
        **request_kwargs: Any,
    ) -> httpx.Response:
        """Fetch a resource, paying the x402 ``402`` challenge if there is one.

        The BUYER side of x402, and the piece that was missing: request the
        resource; if the server answers ``402 Payment Required``, parse the
        ``accepts``, pick an option, sign an EIP-3009 authorization with the
        connected wallet, and retry the request with the ``X-PAYMENT`` header --
        the manual three-step flow from :meth:`create_authorization`, automated.

        Safe by default: ``max_amount`` is a hard ceiling. A 402 that asks for
        more raises :class:`PaymentExceedsMaxError` instead of silently signing.
        This is the seed of a spend guardrail -- the caller sets the limit, the
        loop never crosses it.

        A non-402 response (including a first-try 200, or a 4xx/5xx that is not
        402) is returned untouched -- ``fetch`` only pays when payment is asked.

        Args:
            url: Resource URL.
            method: HTTP method (default ``GET``).
            max_amount: Ceiling in token units (e.g. ``Decimal("0.10")``). None
                = no ceiling (pay whatever is asked -- discouraged for untrusted
                resources).
            token_type: Token to pay with (default ``usdc``). The offer's
                ``asset`` must be that token's address on the offer's network
                (hex compared in any case); an offer in another token raises
                ``ValueError`` before anything is signed. An offer that names
                no asset is paid in ``token_type``'s token on the offer's
                network (USDC by default), and the purchase policy judges it
                as that token too.
            token_decimals: Decimals of ``token_type`` (default 6 for USDC).
                The offer is priced with them and signed with the decimals
                the SDK's registry gives the token on the offer's network.
                The amount signed is always the offer's own atomic amount:
                where the two conversions would not give it back,
                ``ValueError`` is raised before anything is signed.
            select: Optional ``callable(options) -> option`` to override the
                default cheapest-within-ceiling selection. The default takes
                the cheapest among the offers ``token_type`` signs as offered
                (in its token, or naming no asset), and among them all only
                when there is none.
            valid_duration: Authorization validity in seconds.
            eip712_domain: Override the signing domain (see
                :meth:`create_authorization`); by default the domain from the
                chosen accept's ``extra`` is used when present.
            http_client: Reuse a specific ``httpx.Client`` (default: the SDK's).
            policy: Override the client's
                :class:`~uvd_x402_sdk.policy.PurchasePolicy` for this call. The
                policy is evaluated against the offer in hand BEFORE anything is
                signed, and is never widened to fit an offer.
            quote: What a catalog listing advertised, when one was read
                (:class:`~uvd_x402_sdk.policy.AdvertisedQuote`). Compared against
                the real offer and reported at debug level -- a divergence is
                evidence, never a refusal: a seller repricing inside a policy the
                operator already authorised is ordinary commerce, and an agent
                that halts on that is an agent nobody can leave running.
            **request_kwargs: Passed through to the probe and the paid retry
                (``headers``, ``params``, ``json``, ``timeout``, ...).

        Returns:
            The ``httpx.Response`` -- the paid one after a 402, or the original.

        Raises:
            RuntimeError: No signer connected.
            ValueError: Signing the chosen offer would not sign its own token
                or atomic amount (its ``asset`` is not ``token_type``'s token,
                ``token_decimals`` differs from the signing token's decimals,
                or the amount has more digits than a price carries exactly).
                Raised after the ceiling and the policy, whose own refusals
                come first, and before anything is signed.
            PaymentExceedsMaxError: The price exceeds ``max_amount``.
            PolicyRefusedError: The purchase policy will not pay for this offer.
                Carries one of the six contract codes in ``refusal_code``.
            NoAcceptablePaymentError: The 402 offered no option within the ceiling.

        Example:
            >>> client.connect_with_private_key(key, chain_name="base")
            >>> resp = client.fetch(
            ...     "https://api.example.com/data",
            ...     max_amount="0.05",
            ... )
            >>> resp.json()
        """
        if not self._sign_typed_data and self._hedera_signer is None:
            raise RuntimeError(
                "No signer connected. Call connect_with_private_key() or "
                "connect_with_signer() first."
            )

        ceiling = None if max_amount is None else Decimal(str(max_amount))
        client = http_client or self._get_http_client()

        resp = client.request(method, url, **request_kwargs)
        if resp.status_code != 402:
            return resp

        try:
            body = resp.json()
        except Exception as exc:  # noqa: BLE001 - a 402 must carry JSON accepts
            raise InvalidPayloadError(
                f"402 response body is not JSON: {exc}"
            ) from exc

        version, options, parsed = self._parse_402(body)
        if not options:
            # A challenge that CARRIED offers, none of which this build can read,
            # is not "no matching payment method": it is a seller asking for a
            # scheme we do not implement, and saying so names what they wanted.
            # Without this the caller sees an empty list and goes looking for a
            # bug in its own code.
            if parsed.unreadable:
                raise PolicyRefusedError(
                    no_readable_offer(parsed.unreadable), resource=url
                )
            raise NoAcceptablePaymentError(
                "402 response offered no usable payment options", resource=url
            )

        chosen = (
            select(options) if select is not None
            else self._select_payment_option(
                options,
                token_decimals,
                # Native Hedera checks the chosen offer against its own ledger.
                token_type if self._hedera_signer is None else None,
            )
        )
        if not chosen:
            raise NoAcceptablePaymentError(
                "no payment option within max_amount", resource=url
            )

        if self._hedera_signer is not None:
            from uvd_x402_sdk.hedera import HEDERA_NETWORKS
            network = self._hedera_signer.network
            asset_id = HEDERA_NETWORKS[network]["usdc"] if token_type == "usdc" else None
            if chosen["network"] != network or chosen["asset"] != asset_id:
                raise ValueError("Hedera offer differs from the connected ledger or selected asset")
            token_decimals = 6
        price = Decimal(chosen["amount"]) / (Decimal(10) ** token_decimals)
        if ceiling is not None and price > ceiling:
            raise PaymentExceedsMaxError(price, ceiling, resource=url)

        # The policy, evaluated against THIS offer, before anything is signed.
        # An offer that diverges from a listing but sits inside an authorised
        # policy proceeds; one that does not is refused with a cause, and the
        # policy is not widened to fit it. Evaluating does NOT record the spend:
        # signing can still fail and the settlement can still be refused, so
        # `policy.record_spend(...)` is the caller's separate call afterwards.
        in_force = self.policy if policy is None else policy
        offer = chosen.get("offer")
        if offer is None:
            # A custom `select` may hand back a dict it built itself.
            offer = chosen.get("raw") or chosen
        offered_asset = offer.asset if isinstance(offer, Offer) else str(offer.get("asset") or "")
        now = int(time.time())
        decision = in_force.evaluate(
            offer,
            now,
            quote=quote,
            valid_until=offer_valid_until(parsed.extensions),
            unreadable=parsed.unreadable,
        )
        if isinstance(decision, PolicyRefusal):
            raise PolicyRefusedError(decision, resource=url)
        # An offer that names no asset is paid in the token `token_type` signs
        # on its network, and was judged above with no asset at all. Judge it
        # again as that token, so that token's ceilings hold; the decision
        # above keeps its codes and its order.
        signing = None if self._hedera_signer is not None or offered_asset else (
            _signing_token(chosen["network"], token_type)
        )
        if signing is not None:
            decision = in_force.evaluate(
                _offer_in(offer, signing.address),
                now,
                quote=quote,
                valid_until=offer_valid_until(parsed.extensions),
                unreadable=parsed.unreadable,
            )
            if isinstance(decision, PolicyRefusal):
                raise PolicyRefusedError(decision, resource=url)
        logger.debug(
            "policy approved this offer: amount=%s versus_quote=%s",
            decision.amount,
            decision.versus_quote.code,
        )

        if self._hedera_signer is not None:
            if version != 2 or parsed.extensions:
                raise ValueError("Hedera supports x402 v2 without extensions")
            header = self._hedera_signer.create_payment_header(chosen["raw"], resource={"url": url})
        else:
            # The ceiling and the policy judged the offer's own token and amount,
            # each with its own refusals and in its own order. Sign exactly that,
            # or nothing.
            _check_signed_as_offered(
                chosen["amount"],
                offered_asset or None,
                chosen["network"],
                price,
                token_type,
                token_decimals,
            )
            header = self.create_authorization(
                pay_to=chosen["payTo"],
                amount_usd=price,
                chain_name=chosen["network"],
                token_type=token_type,
                x402_version=version,
                accepted=(chosen["raw"] if version >= 2 else None),
                resource=url,
                valid_duration=valid_duration,
                eip712_domain=eip712_domain or chosen.get("eip712_domain"),
            )

        paid_headers = dict(request_kwargs.pop("headers", None) or {})
        paid_headers["X-PAYMENT"] = header
        if version == 2:
            paid_headers["PAYMENT-SIGNATURE"] = header
        return client.request(method, url, headers=paid_headers, **request_kwargs)
