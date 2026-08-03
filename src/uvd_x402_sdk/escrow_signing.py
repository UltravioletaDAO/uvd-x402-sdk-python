"""EIP-3009 escrow pre-auth builder — sign-on-assignment (ADR-002).

Standalone, dict-based port of the escrow lock signing flow: the same
paymentInfo tuple encoding, the same ``ReceiveWithAuthorization`` typed data
and the same X-Payment-Auth wrapper the Facilitator ``POST /settle`` expects.
Ported from Execution Market's ``em_plugin_sdk.escrow_signing`` (itself derived
from :class:`~uvd_x402_sdk.advanced_escrow.AdvancedEscrowClient`); the three
mirrored implementations (this module, the EM dashboard's
``buildEscrowPreAuth`` and em-mobile) must agree byte-for-byte — pinned by the
golden vectors in ``tests/fixtures/escrow-preauth.json``.

Relationship to the rest of the SDK:

* :func:`compute_escrow_nonce` is the same math as
  ``AdvancedEscrowClient._compute_nonce`` (``AuthCaptureEscrow.getHash``),
  but takes the wire-format paymentInfo dict directly and needs no ``web3`` —
  only ``eth-abi`` / ``eth-utils`` (pulled in by ``eth-account``).
* :meth:`~uvd_x402_sdk.wallet.EnvKeyAdapter.sign_eip3009` builds the exact
  same ``ReceiveWithAuthorization`` EIP-712 digest (same domain from the
  network registry, same types, same message shape) — the one difference is
  that it always signs ``from`` = the adapter's own address, while
  :func:`build_escrow_pre_auth` signs ``from`` = the ``payer`` argument. When
  the payer IS the adapter wallet (the normal case) the two produce identical
  signature bytes given the same nonce/value/validBefore.

>>> PROTOCOL CONSTRAINT (ADR-002, verbatim) <<<
The EIP-3009 nonce is ``AuthCaptureEscrow.getHash(paymentInfo)`` which
**includes the receiver** — the escrow signature can only be created AT
ASSIGNMENT, when the worker is known. Never design flows that sign an escrow
auth before the worker is chosen ("stored pre-auth with late receiver fill"
is on-chain unsound).

Fail-loud policy: an unknown network / incomplete network config raises
``ValueError`` instead of falling back — a silent fallback would sign an
EIP-3009 authorization with the WRONG EIP-712 domain (chainId +
verifyingContract): a mismatched, wallet-draining auth.

On-chain limits enforced client-side:
  - bounty <= $100 (AuthCaptureEscrow deposit condition)
  - signed ``maxFeeBps`` must cover the operator's 1300 bps static fee
    (canonical 13% flat)

Usage::

    import httpx
    from uvd_x402_sdk import EnvKeyAdapter, build_escrow_pre_auth

    # Full response of GET /api/v1/h2a/payment-config (Execution Market)
    config = httpx.get(
        "https://api.execution.market/api/v1/h2a/payment-config"
    ).json()

    payment_auth = build_escrow_pre_auth(
        payment_config=config,
        network="base",
        payer="0xPublisher...",
        receiver="0xWorker...",          # committed by the nonce
        amount_usd=0.10,
        deadline=task_deadline_epoch,    # release window outlasts it
        wallet=EnvKeyAdapter(),
    )
    # Send as the X-Payment-Auth header on the assignment request.

Requires ``pip install uvd-x402-sdk[wallet]`` (eth-account pulls in
``eth_abi`` / ``eth_utils``). The module itself imports lazily — a base
install can import it; the eth libs are only required when signing.
"""

from __future__ import annotations

import json
import secrets
import time
from decimal import Decimal
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from uvd_x402_sdk.wallet import WalletAdapter

from . import erc7702 as _erc7702

ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"

USDC_DECIMALS = 6

# AuthCaptureEscrow deposit condition: $100 max per deposit.
ESCROW_DEPOSIT_LIMIT_USD = Decimal("100")

# StaticFeeCalculator: canonical 13% flat fee. The signed maxFeeBps MUST
# cover it or the on-chain release reverts.
OPERATOR_FEE_BPS = 1300

# Canonical fee bounds served by GET /h2a/payment-config (mirrored here as
# defaults for optional keys only — never for the EIP-712 domain).
DEFAULT_MIN_FEE_BPS = 0
DEFAULT_MAX_FEE_BPS = 1800

# EIP-712 type for the escrow flow. The token collector pulls the funds, so
# the scheme is ReceiveWithAuthorization (not TransferWithAuthorization).
RECEIVE_WITH_AUTHORIZATION_TYPES = {
    "ReceiveWithAuthorization": [
        {"name": "from", "type": "address"},
        {"name": "to", "type": "address"},
        {"name": "value", "type": "uint256"},
        {"name": "validAfter", "type": "uint256"},
        {"name": "validBefore", "type": "uint256"},
        {"name": "nonce", "type": "bytes32"},
    ],
}

# ABI type of the paymentInfo tuple — mirrors AdvancedEscrowClient.
_PAYMENT_INFO_ABI = (
    "(address,address,address,address,uint120,uint48,uint48,uint48,"
    "uint16,uint16,address,uint256)"
)

# Canonical fallback — same values as advanced_escrow.TIER_TIMINGS for the
# micro/standard tiers (kept as a plain dict so importing this module never
# pulls web3; the equality is pinned in tests).
ESCROW_TIER_WINDOWS: dict[str, dict[str, int]] = {
    "micro": {"pre": 3600, "auth": 7200, "refund": 86400},
    "standard": {"pre": 7200, "auth": 86400, "refund": 604800},
}

# Human review windows. Agents approve in seconds, so the short SDK tier
# windows (micro auth = 2h) work for them. A HUMAN publisher reviews on
# their own schedule — if authorizationExpiry passes before approval, the
# on-chain release REVERTS (AfterAuthorizationExpiry). So release/refund
# windows are extended to comfortably outlast the deadline + review buffer.
REVIEW_WINDOW_SEC = 7 * 24 * 3600  # >=7 days to approve after the deadline
REFUND_WINDOW_SEC = 7 * 24 * 3600  # +7 days to refund after that

_REQUIRED_NETWORK_KEYS = (
    "chain_id",
    "operator",
    "escrow",
    "token_collector",
    "usdc",
    "usdc_domain_name",
    "usdc_domain_version",
)


def _require_eth_libs() -> tuple[Any, Any, Any]:
    try:
        from eth_abi import encode
        from eth_utils import keccak, to_checksum_address
    except ImportError as exc:  # pragma: no cover
        raise ImportError(
            "eth-abi / eth-utils are required for escrow signing. "
            "Install them with: pip install uvd-x402-sdk[wallet]"
        ) from exc
    return encode, keccak, to_checksum_address


def compute_escrow_nonce(
    chain_id: int,
    escrow_address: str,
    payment_info_typehash: str,
    payment_info: dict[str, Any],
) -> str:
    """Compute the escrow EIP-3009 nonce = ``AuthCaptureEscrow.getHash``.

    ``keccak(chainId, escrow, keccak(PAYMENT_INFO_TYPEHASH, paymentInfo
    tuple with payer=0))`` — same math as
    ``AdvancedEscrowClient._compute_nonce`` but standalone: it takes the
    wire-format dict and needs no web3 (the payer slot is zeroed for the
    payer-agnostic hash; the RECEIVER is part of the hash, which is why the
    signature commits to the worker).

    Args:
        chain_id: EVM chain id.
        escrow_address: AuthCaptureEscrow contract on that chain.
        payment_info_typehash: 32-byte hex typehash from the server config.
        payment_info: paymentInfo dict exactly as serialized on the wire
            (camelCase keys: operator, receiver, token, maxAmount,
            preApprovalExpiry, authorizationExpiry, refundExpiry, minFeeBps,
            maxFeeBps, feeReceiver, salt).

    Returns:
        32-byte hex nonce (0x-prefixed).
    """
    encode, keccak, to_checksum_address = _require_eth_libs()

    pi_tuple = (
        to_checksum_address(payment_info["operator"]),
        ZERO_ADDRESS,  # payer = 0 for the payer-agnostic hash
        to_checksum_address(payment_info["receiver"]),
        to_checksum_address(payment_info["token"]),
        int(payment_info["maxAmount"]),  # uint120
        int(payment_info["preApprovalExpiry"]),  # uint48
        int(payment_info["authorizationExpiry"]),  # uint48
        int(payment_info["refundExpiry"]),  # uint48
        int(payment_info["minFeeBps"]),  # uint16
        int(payment_info["maxFeeBps"]),  # uint16
        to_checksum_address(payment_info["feeReceiver"]),
        int(str(payment_info["salt"]), 16),  # uint256
    )

    typehash_bytes = bytes.fromhex(payment_info_typehash.removeprefix("0x"))
    pi_hash = keccak(encode(["bytes32", _PAYMENT_INFO_ABI], [typehash_bytes, pi_tuple]))
    raw = keccak(
        encode(
            ["uint256", "address", "bytes32"],
            [chain_id, to_checksum_address(escrow_address), pi_hash],
        )
    )
    return "0x" + raw.hex()


def build_escrow_pre_auth(
    payment_config: Any,
    network: str,
    payer: str,
    receiver: str,
    amount_usd: float | str | Decimal,
    deadline: int | None,
    wallet: WalletAdapter,
    tier: str = "micro",
    delegation_resolver: "_erc7702.DelegationResolver | None" = None,
) -> str:
    """Build + sign the escrow lock authorization AT ASSIGNMENT time.

    Returns the raw JSON string for the ``X-Payment-Auth`` header — the
    marketplace backend relays it verbatim to the Facilitator ``/settle``
    after validating payer/amount/receiver. Byte-mirror of Execution
    Market's ``em_plugin_sdk.escrow_signing.build_escrow_pre_auth`` and the
    dashboard's ``buildEscrowPreAuth``.

    >>> PROTOCOL CONSTRAINT (ADR-002, verbatim) <<<
    The EIP-3009 nonce is ``AuthCaptureEscrow.getHash(paymentInfo)`` which
    **includes the receiver** — the escrow signature can only be created AT
    ASSIGNMENT, when the worker is known. Never sign an escrow auth before
    the worker is chosen.

    Args:
        payment_config: Full response of ``GET /api/v1/h2a/payment-config``
            as a dict (must contain the ``escrow`` block with ``networks``,
            ``payment_info_typehash``, fee bounds and tier timings). A
            Pydantic model exposing ``model_dump()`` is also accepted.
        network: Escrow-capable payment network key (e.g. ``"base"``).
            Unknown network -> ``ValueError`` (never a silent domain
            fallback).
        payer: Publisher wallet (must match the signing wallet's address).
        receiver: Chosen worker's wallet — committed by the nonce.
        amount_usd: Bounty in USD (6-decimal USDC). Must be <= the on-chain
            deposit limit ($100).
        deadline: Task deadline (epoch seconds) or ``None``. The
            release/refund windows are extended to outlast it + a review
            buffer so a human publisher can still approve after the worker
            delivers near the deadline.
        wallet: WalletAdapter that signs the ``ReceiveWithAuthorization``
            EIP-712 typed data.
        tier: Expiry tier (``"micro"`` or ``"standard"``); windows come from
            the server config when present.

    Raises:
        ValueError: Unknown/incomplete network config, unknown tier, bounty
            outside (0, $100], or a ``maxFeeBps`` that cannot cover the
            operator's 1300 bps static fee.
    """
    _, _, to_checksum_address = _require_eth_libs()

    # Accept a typed Pydantic payment-config model as well as the raw dict.
    if hasattr(payment_config, "model_dump"):
        payment_config = payment_config.model_dump()

    escrow_cfg = payment_config.get("escrow")
    if not isinstance(escrow_cfg, dict):
        raise ValueError(
            "payment_config has no 'escrow' block — expected the full "
            "GET /api/v1/h2a/payment-config response"
        )

    networks = escrow_cfg.get("networks") or {}
    net = networks.get(network)
    if net is None:
        raise ValueError(
            f"Unknown escrow network '{network}' — refusing to sign an "
            f"EIP-3009 authorization with a mismatched domain. "
            f"Escrow-capable networks: {sorted(networks)}"
        )
    missing = [k for k in _REQUIRED_NETWORK_KEYS if not net.get(k)]
    if missing:
        raise ValueError(
            f"Incomplete escrow config for '{network}' (missing {missing}) — "
            "refusing to sign an EIP-3009 authorization with a mismatched domain."
        )

    typehash = escrow_cfg.get("payment_info_typehash")
    if not typehash:
        raise ValueError(
            "payment_config.escrow.payment_info_typehash is missing — cannot "
            "compute the AuthCaptureEscrow.getHash nonce."
        )

    amount = Decimal(str(amount_usd))
    if amount <= 0:
        raise ValueError(f"Bounty must be positive, got {amount}")
    deposit_limit = Decimal(
        str(escrow_cfg.get("deposit_limit_usd", ESCROW_DEPOSIT_LIMIT_USD))
    )
    if amount > deposit_limit:
        raise ValueError(
            f"Bounty ${amount} exceeds the on-chain escrow deposit limit "
            f"(${deposit_limit})."
        )

    min_fee_bps = int(escrow_cfg.get("min_fee_bps", DEFAULT_MIN_FEE_BPS))
    max_fee_bps = int(escrow_cfg.get("max_fee_bps", DEFAULT_MAX_FEE_BPS))
    if max_fee_bps < OPERATOR_FEE_BPS:
        raise ValueError(
            f"maxFeeBps={max_fee_bps} cannot cover the operator's "
            f"{OPERATOR_FEE_BPS} bps static fee — the on-chain release would "
            "revert."
        )

    timings = (escrow_cfg.get("tier_timings") or {}).get(
        tier
    ) or ESCROW_TIER_WINDOWS.get(tier)
    if timings is None:
        raise ValueError(
            f"Unknown escrow tier '{tier}' — known tiers: "
            f"{sorted(set(escrow_cfg.get('tier_timings') or {}) | set(ESCROW_TIER_WINDOWS))}"
        )

    now = int(time.time())
    atomic = int(amount * (10**USDC_DECIMALS))

    # The release window must outlast the human review. Base it on the task
    # deadline (the worker delivers near it) plus a generous buffer, and
    # never shorter than the SDK tier window. preApprovalExpiry stays short —
    # the lock executes immediately at assignment.
    review_base = max(now, deadline or now)
    auth_expiry = max(now + timings["auth"], review_base + REVIEW_WINDOW_SEC)
    refund_expiry = max(now + timings["refund"], auth_expiry + REFUND_WINDOW_SEC)

    payment_info: dict[str, Any] = {
        "operator": to_checksum_address(net["operator"]),
        "receiver": to_checksum_address(receiver),
        "token": to_checksum_address(net["usdc"]),
        "maxAmount": str(atomic),
        "preApprovalExpiry": now + timings["pre"],
        "authorizationExpiry": auth_expiry,
        "refundExpiry": refund_expiry,
        "minFeeBps": min_fee_bps,
        "maxFeeBps": max_fee_bps,
        "feeReceiver": to_checksum_address(net["operator"]),
        "salt": "0x" + secrets.token_hex(32),
    }

    nonce = compute_escrow_nonce(
        int(net["chain_id"]), net["escrow"], typehash, payment_info
    )

    typed = {
        "domain": {
            "name": net["usdc_domain_name"],
            "version": net["usdc_domain_version"],
            "chainId": int(net["chain_id"]),
            "verifyingContract": to_checksum_address(net["usdc"]),
        },
        "types": RECEIVE_WITH_AUTHORIZATION_TYPES,
        "message": {
            "from": to_checksum_address(payer),
            "to": to_checksum_address(net["token_collector"]),
            "value": atomic,
            "validAfter": 0,
            "validBefore": payment_info["preApprovalExpiry"],
            "nonce": bytes.fromhex(nonce.removeprefix("0x")),
        },
    }

    # ── EIP-7702: a DELEGATED payer cannot settle a raw ECDSA authorization ──
    # Once a gasless-wallet provider delegates the payer's EOA, the address has code
    # and USDC validates via ERC-1271 only — raw ECDSA reverts (0x151d90fe) and the
    # lock fails on-chain. Measured in production: 14 of 14 delegated payers failed;
    # the one plain EOA locked fine. See uvd_x402_sdk.erc7702.
    #
    # THREE STATES, and the third is the one that matters: an UNKNOWN delegation is
    # NOT "not delegated". Collapsing None to False is exactly how this survived eight
    # days — signing raw for an account that can never settle it, silently.
    delegated = _erc7702.is_delegated(payer, network, delegation_resolver)
    if delegated is None and delegation_resolver is not None:
        raise ValueError(
            f"could not determine whether {payer} is EIP-7702-delegated on "
            f"{network} (the resolver gave no verdict) — refusing to sign an escrow "
            f"authorization blindly: the wrong dialect is unsettleable on-chain and "
            f"the failure only shows up at lock time. Retry when the chain is readable."
        )
    if delegated:
        inner = _erc7702.eip712_digest(typed["domain"], typed["types"], typed["message"])
        signature = _erc7702.sign_eip3009_for_delegated(
            wallet=wallet, inner_digest=inner,
            chain_id=int(net["chain_id"]), account=to_checksum_address(payer),
        )
        signed = {"signature": signature}
    else:
        signed = wallet.sign_typed_data(typed)

    # Raw JSON (NOT base64): the backend relays this verbatim to the
    # Facilitator /settle after validating payer/amount/receiver.
    return json.dumps(
        {
            "x402Version": 2,
            "scheme": "escrow",
            "payload": {
                "authorization": {
                    "from": to_checksum_address(payer),
                    "to": to_checksum_address(net["token_collector"]),
                    "value": str(atomic),
                    "validAfter": "0",
                    "validBefore": str(payment_info["preApprovalExpiry"]),
                    "nonce": nonce,
                },
                "signature": signed["signature"],
                "paymentInfo": payment_info,
            },
            "paymentRequirements": {
                "scheme": "escrow",
                "network": f"eip155:{int(net['chain_id'])}",
            },
        },
        separators=(",", ":"),
    )
