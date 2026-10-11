"""DX402 ``durable-evidence`` for the seller's middleware: the post-hook.

The facilitator never sees the response body, so evidence can only be made in
the seller's own response path, after the handler produced the body and after
the payment settled. This module is that step for the integrations, with the
contract of the Rust middleware (x402-rs ``crates/x402-axum/src/durable.rs``):

* **Off unless configured.** Without a :class:`DurableConfig` nothing here runs:
  no seal, no anchor, no header.
* **It cannot break a sale.** Every failure (body over the limit, no payer key,
  an anchor that failed) is a skip notice in ``X-Durable-Evidence``; the body,
  the status and the settlement are delivered unchanged.
* **Only a settled, delivered payment gets evidence.** No transaction hash, a
  receipt the facilitator reports ``pending`` / ``unknown`` / ``rejected``, an
  idempotent replay (an earlier request already anchored that payment), a
  status outside 2xx, a ``HEAD`` request, or a response that already carries
  its own ``X-Durable-Evidence``: no anchor and no header added.

Sealing and anchoring are :func:`uvd_x402_sdk.dx402.anchor_evidence`, unchanged.
The payer's public key, the sink (the HTTP client of the anchor) and the
settlement proof come from configuration: the SDK reads no key and holds no
secret here.
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from uvd_x402_sdk.dx402 import (
    ANCHOR_MAX_REQUEST_BYTES,
    EVIDENCE_HEADER,
    anchor_evidence,
    evidence_header,
    payment_id,
)
from uvd_x402_sdk.models import PaymentResult
from uvd_x402_sdk.networks.base import is_caip2_format, to_caip2_network

__all__ = [
    "DEFAULT_MAX_BODY_BYTES",
    "DEFAULT_TIMEOUT",
    "DurableConfig",
    "EVIDENCE_HEADER",
    "durable_evidence",
    "skip_notice",
    "wants_evidence",
]

logger = logging.getLogger(__name__)

#: Largest body captured by default: the anchor's own request limit
#: (``ANCHOR_MAX_REQUEST_BYTES``, 64 KiB), as the TypeScript SDK. The sealed
#: body travels inline, so a larger one could never be anchored. A larger body
#: is delivered untouched with ``too_large``.
DEFAULT_MAX_BODY_BYTES = ANCHOR_MAX_REQUEST_BYTES

#: Budget in seconds from the route's response to the anchor's answer, capture
#: included (the TypeScript SDK's 5 s). Past it: ``timeout``, and no anchor.
DEFAULT_TIMEOUT = 5.0

_UNSETTLED_RECEIPTS = ("pending", "unknown", "rejected")


@dataclass
class DurableConfig:
    """What the post-hook needs that the payment does not carry.

    - ``payer_key(result, x_payment_header) -> bytes | None`` (required): the
      payer's PUBLIC encryption key (33-byte secp256k1 or 32-byte X25519, e.g.
      from ``payer_key_from_evm_signature`` / ``payer_key_from_solana_address``).
      ``None``, empty or raising = ``no_payer_key``, delivered anyway.
    - ``proof_of_payment(result) -> dict | None``: the settlement proof in the
      facilitator's ``ProofOfPayment`` shape; the only thing that reaches
      ``verified: true`` on the anchor.
    - ``payee``: the address that was paid; defaults to the integration's
      configured recipient for the network.
    - ``client``: the HTTP client the anchor is posted with (anything with
      ``post(url, json=...)``); ``None`` = ``httpx.post``.
    - ``anchor``: the anchoring function, :func:`anchor_evidence` by default.
    - ``max_body_bytes``: bodies above it are delivered untouched with
      ``too_large``.
    - ``timeout``: the whole budget, capture and anchor, in seconds. The
      anchor's HTTP timeout is what is left of it (a custom ``client`` brings
      its own); after it the response goes out with ``timeout`` and no anchor
      is started.

    The facilitator hosts the sealed blob (the anchor sends it inline): there is
    no fixed ``pointer`` here, since one pointer for every sale would name a blob
    that does not hold this response. The rest is passed to
    :func:`anchor_evidence` as is. Checked when built: a bad configuration is
    the integrator's to fix, not something to find on a paid request.
    """

    payer_key: Callable[[PaymentResult, str], bytes | None]
    proof_of_payment: Callable[[PaymentResult], dict | None] | None = None
    payee: str | None = None
    facilitator: str = "https://facilitator.ultravioletadao.xyz"
    seller_encryption_key: bytes | None = None
    signer: Callable[[bytes], str] | None = None
    storage: str | None = None
    retention: str = "90d"
    timeout: float = DEFAULT_TIMEOUT
    client: Any = None
    stack_key: str | None = None
    stack_key_hosts: list[str] | None = None
    max_body_bytes: int = DEFAULT_MAX_BODY_BYTES
    anchor: Callable[..., dict] = field(default=anchor_evidence)

    def __post_init__(self) -> None:
        if not callable(self.payer_key):
            raise TypeError("DurableConfig.payer_key must be callable")
        if not callable(self.anchor):
            raise TypeError("DurableConfig.anchor must be callable")
        size = self.max_body_bytes
        if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
            raise TypeError("DurableConfig.max_body_bytes must be a positive integer")
        budget = self.timeout
        if (
            isinstance(budget, bool)
            or not isinstance(budget, (int, float))
            or not math.isfinite(budget)
            or budget <= 0
        ):
            raise TypeError("DurableConfig.timeout must be a positive number of seconds")


def skip_notice(reason: str) -> str:
    """The ``X-Durable-Evidence`` value of a skip: ``{"v": 1, "skipped": reason}``."""
    return evidence_header({"v": 1, "skipped": reason})


_skipped = skip_notice


def _settled(result: Any) -> bool:
    if not isinstance(result, PaymentResult) or result.success is not True:
        return False
    if not result.transaction_hash or result.idempotent_replayed is True:
        return False
    receipt = result.receipt
    return not (receipt is not None and receipt.status in _UNSETTLED_RECEIPTS)


def _delivered(status_code: int) -> bool:
    return 200 <= status_code < 300


def wants_evidence(
    config: DurableConfig | None,
    result: Any,
    status_code: int,
    method: str = "GET",
    has_header: bool = False,
) -> bool:
    """Whether this response gets an ``X-Durable-Evidence`` at all.

    Durable on, a settled payment that is not a replay, a 2xx answer, a request
    that has a body (not ``HEAD``) and no evidence of the route's own: a route
    that anchored its response itself is not anchored a second time.
    """
    return (
        config is not None
        and _delivered(status_code)
        and _settled(result)
        and (method or "").upper() != "HEAD"
        and not has_header
    )


def durable_evidence(
    config: DurableConfig | None,
    result: Any,
    x_payment_header: str,
    body: bytes,
    status_code: int,
    payee: str | None = None,
    skipped: str | None = None,
    deadline: float | None = None,
) -> str | None:
    """The ``X-Durable-Evidence`` value for one delivered response, or ``None``.

    ``None`` means "no header": durable off, a payment without a confirmed
    settlement, or an error response. Otherwise the anchor's result or a skip
    notice, encoded with :func:`evidence_header`. ``skipped`` is a reason the
    caller already has (the body could not be captured): it is the notice, on
    the same conditions. ``deadline`` (``time.monotonic()``) ends the budget:
    past it the answer is ``timeout`` and the anchor is not started. Never
    raises.
    """
    if config is None:
        return None
    if not _delivered(status_code) or not _settled(result):
        return None
    if skipped is not None:
        return _skipped(skipped)
    try:
        if len(body) > config.max_body_bytes:
            return _skipped("too_large")
        try:
            key = config.payer_key(result, x_payment_header)
        except Exception:  # noqa: BLE001 - no key is a skip, never a failed sale
            key = None
        if not key:
            return _skipped("no_payer_key")
        proof = config.proof_of_payment(result) if config.proof_of_payment else None
        left = config.timeout if deadline is None else deadline - time.monotonic()
        if left <= 0:
            return _skipped("timeout")
        network = result.network
        caip2 = network if is_caip2_format(network) else to_caip2_network(network) or network
        tx_hash = result.transaction_hash
        evidence = config.anchor(
            body,
            payment_id_value=payment_id(caip2, tx_hash),
            network=network,
            tx_hash=tx_hash,
            payer=result.payer_address,
            payee=config.payee or payee or "",
            payer_key=key,
            seller_encryption_key=config.seller_encryption_key,
            signer=config.signer,
            proof_of_payment=proof,
            storage=config.storage,
            retention=config.retention,
            facilitator=config.facilitator,
            timeout=min(config.timeout, left),
            client=config.client,
            stack_key=config.stack_key,
            stack_key_hosts=config.stack_key_hosts,
        )
        if not isinstance(evidence, dict):
            return _skipped("anchor_failed")
        return evidence_header(evidence)
    except Exception:  # noqa: BLE001 - evidence is never a gate on the sale
        logger.warning("DX402 evidence failed; delivered without it", exc_info=True)
        return _skipped("anchor_failed")
