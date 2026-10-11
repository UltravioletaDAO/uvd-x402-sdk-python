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
  receipt the facilitator reports ``pending`` / ``unknown`` /
  ``rejected``, or an error
  response (4xx / 5xx): no anchor and no header at all.

Sealing and anchoring are :func:`uvd_x402_sdk.dx402.anchor_evidence`, unchanged.
The payer's public key, the sink (the HTTP client of the anchor) and the
settlement proof come from configuration: the SDK reads no key and holds no
secret here.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable

from uvd_x402_sdk.dx402 import (
    EVIDENCE_HEADER,
    anchor_evidence,
    evidence_header,
    payment_id,
)
from uvd_x402_sdk.models import PaymentResult
from uvd_x402_sdk.networks.base import is_caip2_format, to_caip2_network

__all__ = [
    "DEFAULT_MAX_BODY_BYTES",
    "DurableConfig",
    "EVIDENCE_HEADER",
    "durable_evidence",
]

logger = logging.getLogger(__name__)

#: Largest body sealed by default: 32 MiB, the Rust middleware's
#: ``DEFAULT_MAX_BODY_BYTES``. A larger body is delivered with ``too_large``.
DEFAULT_MAX_BODY_BYTES = 32 * 1024 * 1024

_UNSETTLED_RECEIPTS = ("pending", "unknown", "rejected")


@dataclass
class DurableConfig:
    """What the post-hook needs that the payment does not carry.

    - ``payer_key(result, x_payment_header) -> bytes | None``: the payer's
      PUBLIC encryption key (33-byte secp256k1 or 32-byte X25519, e.g. from
      ``payer_key_from_evm_signature`` / ``payer_key_from_solana_address``).
      Missing, ``None`` or raising = ``no_payer_key``, delivered anyway.
    - ``proof_of_payment(result) -> dict | None``: the settlement proof in the
      facilitator's ``ProofOfPayment`` shape; the only thing that reaches
      ``verified: true`` on the anchor.
    - ``payee``: the address that was paid; defaults to the integration's
      configured recipient for the network.
    - ``client``: the HTTP client the anchor is posted with (anything with
      ``post(url, json=...)``); ``None`` = ``httpx.post``.
    - ``anchor``: the anchoring function, :func:`anchor_evidence` by default.
    - ``max_body_bytes``: bodies above it are delivered with ``too_large``.

    The rest is passed to :func:`anchor_evidence` as is.
    """

    payer_key: Callable[[PaymentResult, str], bytes | None] | None = None
    proof_of_payment: Callable[[PaymentResult], dict | None] | None = None
    payee: str | None = None
    facilitator: str = "https://facilitator.ultravioletadao.xyz"
    seller_encryption_key: bytes | None = None
    signer: Callable[[bytes], str] | None = None
    storage: str | None = None
    pointer: str | None = None
    retention: str = "90d"
    timeout: float = 15.0
    client: Any = None
    stack_key: str | None = None
    stack_key_hosts: list[str] | None = None
    max_body_bytes: int = DEFAULT_MAX_BODY_BYTES
    anchor: Callable[..., dict] = field(default=anchor_evidence)


def _skipped(reason: str) -> str:
    return evidence_header({"v": 1, "skipped": reason})


def _settled(result: Any) -> bool:
    if not isinstance(result, PaymentResult) or result.success is not True:
        return False
    if not result.transaction_hash:
        return False
    receipt = result.receipt
    return not (receipt is not None and receipt.status in _UNSETTLED_RECEIPTS)


def durable_evidence(
    config: DurableConfig | None,
    result: Any,
    x_payment_header: str,
    body: bytes,
    status_code: int,
    payee: str | None = None,
    skipped: str | None = None,
) -> str | None:
    """The ``X-Durable-Evidence`` value for one delivered response, or ``None``.

    ``None`` means "no header": durable off, a payment without a confirmed
    settlement, or an error response. Otherwise the anchor's result or a skip
    notice, encoded with :func:`evidence_header`. ``skipped`` is a reason the
    caller already has (the body could not be captured): it is the notice, on
    the same conditions. Never raises.
    """
    if config is None:
        return None
    if not 200 <= status_code < 400 or not _settled(result):
        return None
    if skipped is not None:
        return _skipped(skipped)
    try:
        if len(body) > config.max_body_bytes:
            return _skipped("too_large")
        try:
            key = config.payer_key(result, x_payment_header) if config.payer_key else None
        except Exception:  # noqa: BLE001 - no key is a skip, never a failed sale
            key = None
        if not key:
            return _skipped("no_payer_key")
        proof = config.proof_of_payment(result) if config.proof_of_payment else None
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
            pointer=config.pointer,
            retention=config.retention,
            facilitator=config.facilitator,
            timeout=config.timeout,
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
