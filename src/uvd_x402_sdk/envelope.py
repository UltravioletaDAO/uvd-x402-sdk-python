"""Choosing between the x402 **v1** and **v2** request envelopes.

:mod:`uvd_x402_sdk.envelope_v2` knows how to *build* a v2 body. This module
decides **when** to build one, and converts the v1-shaped
:class:`~uvd_x402_sdk.models.PaymentRequirements` the SDK already assembles into
the ``{resource, accepted}`` pair v2 wants. Without it the v2 builders had no
caller: :class:`~uvd_x402_sdk.X402Client` hardcoded ``"x402Version": 1``, so the
SDK could *advertise* v2 in a 402 and then was structurally unable to speak it —
a payer that believed our own 402 got a 400 back.

# The rule ``resolve_envelope_version`` applies, and why

Auto keys off **CAIP-2 on the wire**, never off ``payload.x402Version``. That is
measured, not stylistic: the facilitator's envelope enum is *untagged*, so it
matches on SHAPE and ignores the version marker. A header that merely declares
version 2 while carrying plain network names is served correctly today, and
upgrading it on the strength of the marker would change a call that works.

Measured against ``https://facilitator.ultravioletadao.xyz`` (2.10.0) on
2026-09-04, ``POST /verify`` with a fabricated signature. A 200 means the
facilitator *understood* the body (the verdict is always ``isValid: false``); a
400 means it could not deserialize it:

=========================================================  ======
body                                                       result
=========================================================  ======
v1 envelope, ``base`` / ``base``                           200
v1 envelope, ``base`` / ``base``, header marker ``2``      200
v1 envelope, ``eip155:8453`` / ``base``                    200
v1 envelope, ``base`` / ``eip155:8453``                    200
v1 envelope, ``eip155:8453`` / ``eip155:8453``             200
v2 envelope, ``eip155:8453``                               200
v2 envelope, ``base`` (a plain name)                       **400**
=========================================================  ======

The last two rows are why auto must not upgrade a plain-name pair: v2 has no way
to carry one. And the second row is why it must not key off the marker.

**This differs from what the TypeScript SDK measured one day earlier.** On
2026-09-03 the CAIP-2 rows were a hard 400 in the v1 envelope
(``unknown variant `eip155:8453```), so the argument there was "auto can only
turn a failure into a payment". The facilitator has since taught the v1 envelope
to read CAIP-2, so today both columns answer 200 and the rule needs a different
justification — see :func:`resolve_envelope_version`.
"""

from typing import Any, Dict, Union

from uvd_x402_sdk.envelope_v2 import (
    AcceptedRequirementsV2,
    ResourceInfoV2,
    build_settle_request_v2,
    build_verify_request_v2,
)
from uvd_x402_sdk.models import PaymentPayload, PaymentRequirements
from uvd_x402_sdk.networks.base import is_caip2_format, to_caip2_network

#: What ``resolve_envelope_version`` accepts as an explicit request.
EnvelopeVersion = Union[int, str]


def to_resource_info_v2(requirements: PaymentRequirements) -> ResourceInfoV2:
    """Derive the v2 ``resource`` object from v1-shaped requirements.

    v2 moved ``resource`` / ``description`` / ``mimeType`` out of the
    requirements and into an object of their own, and the facilitator requires
    **all three** keys: measured, a ``resource`` carrying only ``url`` is a 400
    that names no field.
    """
    return ResourceInfoV2(
        url=requirements.resource,
        description=requirements.description,
        mimeType=requirements.mimeType,
    )


def to_accepted_requirements_v2(
    requirements: PaymentRequirements,
) -> AcceptedRequirementsV2:
    """Derive the v2 ``accepted`` requirements from v1-shaped requirements.

    Two renames do the damage, and the facilitator reports neither by name:
    ``maxAmountRequired`` is spelled ``amount`` in v2, and ``network`` must be
    CAIP-2 — a plain name inside a v2 body is a 400.

    ``extra`` is carried through when present. That is where the EIP-712 domain
    ``name`` / ``version`` live for tokens the facilitator does not know by
    address (EURC, the bridged USDCs), so dropping it would make them unpayable.

    Raises:
        ValueError: If ``requirements.network`` has no CAIP-2 form. Only
            reachable by pinning version 2 on a network that has none (XRPL:
            its v1 string *is* its identifier), and it must be loud — a silent
            fallback would send a v1 network name inside a v2 body, which is
            the exact 400 this module exists to prevent.
    """
    network = requirements.network
    if not is_caip2_format(network):
        caip2 = to_caip2_network(network)
        if caip2 is None:
            raise ValueError(
                f"Network {network!r} has no CAIP-2 form, so it cannot travel in "
                "the x402 v2 envelope. Use x402_version=1 for this network."
            )
        network = caip2

    return AcceptedRequirementsV2(
        scheme=requirements.scheme,
        network=network,
        asset=requirements.asset,
        amount=requirements.maxAmountRequired,
        payTo=requirements.payTo,
        maxTimeoutSeconds=requirements.maxTimeoutSeconds,
        extra=requirements.extra,
    )


def resolve_envelope_version(
    payload: PaymentPayload,
    requirements: PaymentRequirements,
    requested: EnvelopeVersion = "auto",
) -> int:
    """Decide which envelope this (payload, requirements) pair travels in.

    ``requested`` wins when it names a version — a pin is respected even when it
    contradicts the wire, because choosing the version is the point of the
    option. ``"auto"`` (the default) reads the wire.

    **Auto upgrades on CAIP-2, not on the version marker.** Both envelopes are
    accepted for a CAIP-2 pair by the facilitator running today, so this is not
    "turning a failure into a payment" any more; three things still make v2 the
    right answer there:

    1. A CAIP-2 network on the wire means the 402 that produced it advertised
       v2. Answering v2 is speaking the protocol the seller announced.
    2. v2-with-CAIP-2 is the only shape accepted by *both* generations of the
       facilitator. v1-with-CAIP-2 is a 400 on any build older than 2026-09-04,
       so choosing v1 there is the option that breaks against a self-hosted or
       pinned facilitator.
    3. The TypeScript SDK (2.78.0) resolves the identical rule, so the same wire
       produces the same body in both SDKs.

    And plain-name pairs — including a header that merely *declares* version 2 —
    stay on v1, where they are a 200 and where v2 would be a 400.

    Args:
        payload: The parsed payment payload from the ``X-PAYMENT`` header.
        requirements: The requirements this SDK built for the facilitator.
        requested: ``1``, ``2``, or ``"auto"``. Anything else raises.

    Returns:
        ``1`` or ``2``.

    Raises:
        ValueError: If ``requested`` is neither 1, 2 nor ``"auto"``.
    """
    if requested != "auto":
        if requested not in (1, 2):
            raise ValueError(
                f"x402_version must be 1, 2 or 'auto', got {requested!r}"
            )
        return int(requested)

    if is_caip2_format(payload.network) or is_caip2_format(requirements.network):
        return 2
    return 1


def _build_v1(
    payload: PaymentPayload, requirements: PaymentRequirements
) -> Dict[str, Any]:
    """The v1 envelope, byte-for-byte what the client emitted before this module.

    ``x402Version`` is the literal ``1`` rather than ``payload.x402Version`` on
    purpose: it names the ENVELOPE, not the payer's header, and the facilitator
    reads the envelope by shape. Echoing a ``2`` from the header here would
    declare a v2 body while sending a v1 one.
    """
    return {
        "x402Version": 1,
        "paymentPayload": payload.model_dump(by_alias=True),
        "paymentRequirements": requirements.model_dump(
            by_alias=True, exclude_none=True
        ),
    }


def build_verify_request_for_version(
    payload: PaymentPayload,
    requirements: PaymentRequirements,
    version: int,
) -> Dict[str, Any]:
    """Build a ``POST /verify`` body in whichever envelope ``version`` names.

    The v1 return is byte-for-byte what the client sent before envelope
    selection existed, so pinning ``1`` is exactly today's behaviour.

    Example:
        >>> version = resolve_envelope_version(payload, requirements)
        >>> body = build_verify_request_for_version(payload, requirements, version)
    """
    if version == 2:
        return build_verify_request_v2(
            payload=payload.payload,
            resource=to_resource_info_v2(requirements),
            accepted=to_accepted_requirements_v2(requirements),
        )
    return _build_v1(payload, requirements)


def build_settle_request_for_version(
    payload: PaymentPayload,
    requirements: PaymentRequirements,
    version: int,
) -> Dict[str, Any]:
    """Build a ``POST /settle`` body in whichever envelope ``version`` names.

    See :func:`build_verify_request_for_version` — ``/settle`` takes the same
    body as ``/verify`` in both versions.
    """
    if version == 2:
        return build_settle_request_v2(
            payload=payload.payload,
            resource=to_resource_info_v2(requirements),
            accepted=to_accepted_requirements_v2(requirements),
        )
    return _build_v1(payload, requirements)


__all__ = [
    "EnvelopeVersion",
    "build_settle_request_for_version",
    "build_verify_request_for_version",
    "resolve_envelope_version",
    "to_accepted_requirements_v2",
    "to_resource_info_v2",
]
