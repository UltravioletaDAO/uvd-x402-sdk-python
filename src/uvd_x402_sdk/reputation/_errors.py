"""Errors of the reputation module.

Every error carries a stable ``code`` (lower_snake_case). The TypeScript twin
raises the same codes for the same inputs, and the shared vectors in
``tests/vectors/reputation`` pin them, so a consumer branches on ``code`` and
never on the message. Messages never carry a full address, hash, signature,
proof or URL: they go through :func:`uvd_x402_sdk.reputation._redact.redact`.
"""

from __future__ import annotations

from uvd_x402_sdk.reputation._redact import redact


class ReputationError(Exception):
    """Base class. ``code`` is the stable reason; the message is for humans."""

    def __init__(self, code: str, message: str = "") -> None:
        self.code = code
        text = redact(message) if message else code
        super().__init__(f"{code}: {text}" if message else code)


class PreparationRejected(ReputationError, ValueError):
    """The facilitator's prepare asks to sign something this module will not.

    Nothing was signed. A ``ValueError`` on purpose: retrying the same prepare
    does not fix it, and a consumer parks it as a permanent refusal.
    """


class SignatureRejected(ReputationError):
    """The signer returned a signature that is not the one asked for.

    High-s, wrong length, wrong ``v``, or it does not recover to the rater.
    Nothing is sent: a remote signer that misbehaves is caught here, not
    on-chain.
    """


class PaymentRejected(ReputationError, ValueError):
    """A definite refusal about the anchor payment (never retried as is)."""


class PaymentPending(ReputationError):
    """The anchor payment has no verdict yet: retry later with the SAME tx."""


class OutboxConflict(ReputationError):
    """A compare-and-set on the outbox failed: someone else moved the row."""


class EvidenceRejected(ReputationError, ValueError):
    """The public evidence would carry something it must not."""
