"""
x402 **v2** request envelopes (`/verify` and `/settle`).

If the 402 you received advertises CAIP-2 networks (``eip155:8453``), you are
speaking v2 and must send the v2 envelope. :class:`~uvd_x402_sdk.X402Client`'s
``verify_payment`` / ``settle_payment`` emit the **v1** envelope
``{x402Version, paymentPayload, paymentRequirements}`` and cannot express v2.

Example:
    >>> from uvd_x402_sdk.envelope_v2 import (
    ...     AcceptedRequirementsV2, ResourceInfoV2, build_verify_request_v2,
    ... )
    >>> body = build_verify_request_v2(
    ...     payload={"signature": "0x...", "authorization": {...}},
    ...     resource=ResourceInfoV2(
    ...         url="https://api.example.com/thing",
    ...         description="Thing",
    ...         mime_type="application/json",
    ...     ),
    ...     accepted=AcceptedRequirementsV2(
    ...         scheme="exact",
    ...         network="eip155:8453",
    ...         asset="0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
    ...         amount="100000",
    ...         pay_to="0xabc...",
    ...         max_timeout_seconds=300,
    ...     ),
    ... )
    >>> httpx.post(f"{facilitator}/verify", json=body)

# v1 versus v2, and why mixing them fails so badly

============  =====================================================  =========================================
              v1 envelope                                            v2 envelope
============  =====================================================  =========================================
top level     ``{x402Version, paymentPayload, paymentRequirements}``  ``{x402Version, paymentPayload, resource, accepted}``
network       plain name — ``base``                                   CAIP-2 — ``eip155:8453``
amount field  ``maxAmountRequired``                                   ``amount``
``resource``  URL **string**                                          **object** ``{url, description, mimeType}``
============  =====================================================  =========================================

Each version demands its own network format *and* its own envelope. A v2 payload
inside a v1 envelope, or a plain network name inside a v2 request, matches no
variant at the facilitator and fails with::

    data did not match any variant of untagged enum VerifyRequestEnvelope

That error names no field. If you see it, check the **envelope shape** first, not
the fields inside it — a whole day was lost to exactly that inversion, testing
six ``accepted`` shapes while the mismatch was in the wrapper around them.
"""

from typing import Any, Dict, Optional, Union

from pydantic import BaseModel, Field


class ResourceInfoV2(BaseModel):
    """The protected resource, as x402 v2 expects it.

    Note this is an **object** in v2. Passing a bare URL string is the single
    most common v2 mistake and it surfaces as an unhelpful "no variant matched"
    error that names no field.
    """

    url: str = Field(..., description="URL of the protected resource")
    description: str = Field(..., description="Human-readable description")
    mime_type: str = Field(..., alias="mimeType", description="MIME type of the resource")

    class Config:
        populate_by_name = True


class AcceptedRequirementsV2(BaseModel):
    """Payment requirements in x402 v2 form.

    Differences from v1 that actually bite: ``network`` is CAIP-2 rather than a
    plain name, ``maxAmountRequired`` is renamed to ``amount``, and the resource
    fields moved out to :class:`ResourceInfoV2`.
    """

    scheme: str = Field(..., description='Payment scheme, e.g. "exact"')
    network: str = Field(..., description='CAIP-2 chain id, e.g. "eip155:8453"')
    asset: str = Field(..., description="Token contract address or account")
    amount: str = Field(..., description="Amount in token base units (NOT maxAmountRequired)")
    pay_to: str = Field(..., alias="payTo", description="Recipient address")
    max_timeout_seconds: int = Field(
        ..., alias="maxTimeoutSeconds", description="Seconds before the payment expires"
    )
    extra: Optional[Any] = Field(default=None, description="Chain- or app-specific data")

    class Config:
        populate_by_name = True


#: Anything the builders accept for the resource / requirements arguments.
ResourceLike = Union[ResourceInfoV2, Dict[str, Any]]
RequirementsLike = Union[AcceptedRequirementsV2, Dict[str, Any]]


def _as_wire(value: Union[BaseModel, Dict[str, Any]]) -> Dict[str, Any]:
    """Normalise a model or a plain dict to its JSON (camelCase) form."""
    if isinstance(value, BaseModel):
        return value.model_dump(by_alias=True, exclude_none=True)
    return dict(value)


def _build_envelope(
    payload: Dict[str, Any],
    resource: ResourceLike,
    accepted: RequirementsLike,
) -> Dict[str, Any]:
    resource_wire = _as_wire(resource)
    accepted_wire = _as_wire(accepted)
    return {
        "x402Version": 2,
        # The facilitator reads the payload from here. `resource` and `accepted`
        # are repeated at the top level because the v2 envelope declares both;
        # omitting either fails deserialization.
        "paymentPayload": {
            "x402Version": 2,
            "resource": resource_wire,
            "accepted": accepted_wire,
            "payload": payload,
        },
        "resource": resource_wire,
        "accepted": accepted_wire,
    }


def build_verify_request_v2(
    payload: Dict[str, Any],
    resource: ResourceLike,
    accepted: RequirementsLike,
) -> Dict[str, Any]:
    """Build a **v2** request body for ``POST /verify``.

    Args:
        payload: The chain-specific authorization, e.g.
            ``{"signature": "0x...", "authorization": {...}}``. Passed through
            verbatim — this is the client's signed material, not ours to reshape.
        resource: The protected resource. An **object**, not a URL string.
        accepted: The chosen payment requirements, with a CAIP-2 ``network`` and
            an ``amount`` field.

    Returns:
        A dict ready to POST as JSON. Deliberately a plain dict rather than a
        model: it is what goes on the wire, so there is no second step to forget.

    Note:
        There is no ``paymentRequirements`` key — that is the v1 envelope.
    """
    return _build_envelope(payload, resource, accepted)


def build_settle_request_v2(
    payload: Dict[str, Any],
    resource: ResourceLike,
    accepted: RequirementsLike,
) -> Dict[str, Any]:
    """Build a **v2** request body for ``POST /settle``.

    The envelope is identical to :func:`build_verify_request_v2`; only the
    endpoint differs. Both are provided so calling code reads as what it does.
    """
    return _build_envelope(payload, resource, accepted)
