"""On-chain reputation, one implementation for every product: ERC-8004 ratings
signed by the rater, anchored by a payment, relayed by the facilitator.

Extracted from Karma Hello's per-comment rating worker (the one running in
production on Base), with the signing guards of Karma Kadabra's
``rating_rail`` and Execution Market's ``relayed-rating``, so a new program
installs reputation instead of writing it a fifth time. The TypeScript SDK
carries the same contract (``uvd-x402-sdk/reputation``); the shared vectors in
``tests/vectors/reputation`` pin both to the same digests, authorizations,
documents and refs.

Install: ``pip install "uvd-x402-sdk[reputation]"`` (eth-account >= 0.13).

The pieces, each usable alone (see ``README.md`` next to this file):

- :func:`message_ref` - the deterministic idempotency key of a rated thing;
- :class:`RatingParams`, :func:`validate_preparation` - what may be signed;
- :func:`sign_relayed_feedback`, :func:`build_relay_authorization` - the two
  signatures (low-s, digests rebuilt here, recovered before they leave);
- :func:`pay_and_prove` - the anchor payment and its chain-built proof;
- :class:`RatingOutbox` + :class:`InMemoryRatingOutbox` /
  :class:`SQLiteRatingOutbox` - the persisted state machine;
- :class:`Governor`, :class:`Caps`, :class:`CircuitBreaker` - may a new
  rating start;
- :func:`evidence_document` - the public evidence, without the chat text or
  its author;
- :func:`submit_with_reconcile` - submit, and never prepare twice;
- :class:`ReputationWorker` - all of the above in one ``tick()``.

This module never reads a key from the environment, never logs one, and
redacts addresses, hashes, signatures, proofs and URLs in every log line.
"""

from uvd_x402_sdk.erc8004 import Erc8004Client
from uvd_x402_sdk.reputation._chains import (
    GIVE_FEEDBACK_TYPEHASH,
    RELAY_CHAIN_IDS,
    RELAY_DELEGATES,
    relay_chain_id,
)
from uvd_x402_sdk.reputation._errors import (
    EvidenceRejected,
    OutboxConflict,
    PaymentPending,
    PaymentRejected,
    PreparationRejected,
    ReputationError,
    SignatureRejected,
)
from uvd_x402_sdk.reputation._redact import redact
from uvd_x402_sdk.reputation.evidence import (
    EVIDENCE_SCHEMA,
    EvidenceDocument,
    canonical_json,
    commitment,
    content_hash,
    evidence_document,
    new_salt,
)
from uvd_x402_sdk.reputation.governor import (
    Caps,
    CircuitBreaker,
    Decision,
    Governor,
    HealthReport,
    shared_addresses,
)
from uvd_x402_sdk.reputation.outbox import (
    ForgetReport,
    InMemoryRatingOutbox,
    OutboxEntry,
    RatingOutbox,
    SQLiteRatingOutbox,
    Usage,
)
from uvd_x402_sdk.reputation.payment import (
    GasPolicy,
    HttpJsonRpc,
    JsonRpc,
    RpcError,
    SignedPayment,
    pay_and_prove,
    payment_hash,
    prove_payment,
    recompute_payment_hash,
    sign_payment,
)
from uvd_x402_sdk.reputation.preparation import (
    RatingParams,
    VerifiedPreparation,
    relayed_feedback_digest,
    relayed_feedback_typed_data,
    validate_preparation,
)
from uvd_x402_sdk.reputation.ref import message_ref
from uvd_x402_sdk.reputation.signing import (
    AccountSigner,
    RaterSigner,
    authorization_digest,
    build_relay_authorization,
    sign_relayed_feedback,
)
from uvd_x402_sdk.reputation.submit import RelayClient, matching_feedback, submit_with_reconcile
from uvd_x402_sdk.reputation.worker import ReputationWorker

__all__ = [
    "EVIDENCE_SCHEMA",
    "GIVE_FEEDBACK_TYPEHASH",
    "RELAY_CHAIN_IDS",
    "RELAY_DELEGATES",
    "AccountSigner",
    "Caps",
    "CircuitBreaker",
    "Decision",
    "Erc8004Client",
    "EvidenceDocument",
    "EvidenceRejected",
    "ForgetReport",
    "GasPolicy",
    "Governor",
    "HealthReport",
    "HttpJsonRpc",
    "InMemoryRatingOutbox",
    "JsonRpc",
    "OutboxConflict",
    "OutboxEntry",
    "PaymentPending",
    "PaymentRejected",
    "PreparationRejected",
    "RaterSigner",
    "RatingOutbox",
    "RatingParams",
    "RelayClient",
    "ReputationError",
    "ReputationWorker",
    "RpcError",
    "SQLiteRatingOutbox",
    "SignatureRejected",
    "SignedPayment",
    "Usage",
    "VerifiedPreparation",
    "authorization_digest",
    "build_relay_authorization",
    "canonical_json",
    "commitment",
    "content_hash",
    "evidence_document",
    "matching_feedback",
    "message_ref",
    "new_salt",
    "pay_and_prove",
    "payment_hash",
    "prove_payment",
    "recompute_payment_hash",
    "redact",
    "relay_chain_id",
    "relayed_feedback_digest",
    "relayed_feedback_typed_data",
    "shared_addresses",
    "sign_payment",
    "sign_relayed_feedback",
    "submit_with_reconcile",
    "validate_preparation",
]
