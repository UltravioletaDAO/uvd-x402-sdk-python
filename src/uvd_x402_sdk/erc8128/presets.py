"""The live verifier postures, as DATA.

``meshrelay-strict`` and ``em-lenient`` reproduce what each product does today
knob for knob, so adopting the SDK changes no behaviour. ``canonical-strict``
is pre-written and unused until the hardening stage.

These exact values are pinned in the ``policies`` block of the conformance
vectors and asserted field by field by the suite — a preset that drifts from
the vectors fails before it can silently change a product's posture. A caller
that spreads a preset and then overrides a knob changes posture with no type
error, so the assertion is the only tripwire.
"""

from __future__ import annotations

import dataclasses
from typing import Any, Dict, Mapping, Optional

from uvd_x402_sdk.erc8128.nonce import NoncePolicy, NonceStore
from uvd_x402_sdk.erc8128.verifier import VerifyPolicy

#: Preset → the nonce-consumption order that belongs to it. Kept beside the
#: presets (rather than inside them) because the store itself is the caller's,
#: and ``policy_from_preset`` is what stitches the two together.
PRESET_NONCE_CONSUME: Mapping[str, str] = {
    "meshrelay-strict": "after-verify",
    "em-lenient": "before-verify",
    "canonical-strict": "after-verify",
}

POLICY_PRESETS: Mapping[str, VerifyPolicy] = {
    # MeshRelay: pinned authority, exact-ordered components, digest by method,
    # chain allowlist, no grace past expiry, nonce consumed AFTER the crypto.
    "meshrelay-strict": VerifyPolicy(
        authority="",
        accept="accept-both",
        components="exact-ordered",
        content_digest="non-idempotent-methods",
        allowed_chain_ids=(8453,),
        max_validity_sec=300,
        clock_skew_future_sec=30,
        clock_skew_past_expiry_sec=0,
        label="eth",
    ),
    # Execution Market: subset components, digest driven by body presence, any
    # chain, ±30s skew, any label, nonce consumed BEFORE the crypto.
    "em-lenient": VerifyPolicy(
        authority="",
        accept="accept-both",
        components="request-bound-subset",
        content_digest="body-present",
        allowed_chain_ids=None,
        max_validity_sec=300,
        clock_skew_future_sec=30,
        clock_skew_past_expiry_sec=30,
        label="any",
    ),
    # Post-hardening: canonical wire only (alg required, lowercase keyid).
    # Content-Digest follows the owner decision that EM's body-presence rule is
    # canonical. Chain allowlist and skew are the conservative choice — a
    # verifier adopting this preset should override them with the values it
    # already ran.
    "canonical-strict": VerifyPolicy(
        authority="",
        accept="canonical",
        components="exact-ordered",
        content_digest="body-present",
        allowed_chain_ids=(8453,),
        max_validity_sec=300,
        clock_skew_future_sec=30,
        clock_skew_past_expiry_sec=0,
        label="eth",
    ),
}


def policy_from_preset(
    name: str,
    *,
    authority: str,
    nonce_store: Optional[NonceStore] = None,
    nonce_mode: str = "required",
    **overrides: Any,
) -> VerifyPolicy:
    """Specialise a preset with the two things it cannot carry: the authority
    VALUE and the caller's nonce store. The consume order comes from
    :data:`PRESET_NONCE_CONSUME`, so adopting ``meshrelay-strict`` cannot
    accidentally flip MeshRelay to EM's ordering.
    """
    if name not in POLICY_PRESETS:
        raise ValueError(f"unknown ERC-8128 policy preset: {name!r}")
    policy = POLICY_PRESETS[name]
    nonce = (
        NoncePolicy(
            store=nonce_store, mode=nonce_mode, consume=PRESET_NONCE_CONSUME[name]
        )
        if nonce_store is not None
        else None
    )
    return dataclasses.replace(policy, authority=authority, nonce=nonce, **overrides)


def preset_as_data(name: str) -> Dict[str, Any]:
    """The JSON-shaped view of a preset — what the vectors' ``policies`` block
    pins and what the conformance suite compares against.
    """
    policy = POLICY_PRESETS[name]
    return {
        "accept": policy.accept,
        "components": policy.components,
        "content_digest": policy.content_digest,
        "allowed_chain_ids": (
            list(policy.allowed_chain_ids)
            if policy.allowed_chain_ids is not None
            else None
        ),
        "max_validity_sec": policy.max_validity_sec,
        "clock_skew_future_sec": policy.clock_skew_future_sec,
        "clock_skew_past_expiry_sec": policy.clock_skew_past_expiry_sec,
        "label": policy.label,
        "nonce_mode": "required",
        "nonce_consume": PRESET_NONCE_CONSUME[name],
    }


__all__ = [
    "POLICY_PRESETS",
    "PRESET_NONCE_CONSUME",
    "policy_from_preset",
    "preset_as_data",
]
