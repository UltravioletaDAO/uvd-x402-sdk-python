"""
Main x402 client for payment processing.

This module provides the X402Client class which handles:
- Parsing X-PAYMENT headers
- Verifying payments with the facilitator
- Settling payments on-chain
- Error handling with clear messages
"""

import base64
import json
import logging
import os
import time
from decimal import Decimal
from typing import Optional, Tuple, List, Dict, Any, Union

import httpx

from uvd_x402_sdk.config import X402Config
from uvd_x402_sdk.exceptions import (
    X402Error,
    InvalidPayloadError,
    PaymentVerificationError,
    PaymentSettlementError,
    UnsupportedNetworkError,
    FacilitatorError,
    TimeoutError as X402TimeoutError,
    PaymentExceedsMaxError,
    NoAcceptablePaymentError,
    MAX_RETRY_AFTER_SECONDS,
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
from uvd_x402_sdk.networks import (
    get_network,
    NetworkType,
    get_supported_network_names,
    normalize_network,
    is_caip2_format,
    parse_caip2_network,
)

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


def _extract_tx_hash_from_body(body: Any) -> Optional[str]:
    """Return the transaction hash carried in a facilitator response body, if any.

    The facilitator reports the hash under several shapes depending on the
    endpoint and error path: {"transaction": "0x…"}, {"transaction": {"hash":
    "0x…"}}, {"txHash": …}, {"tx_hash": …}, {"transaction_hash": …}.
    """
    if not isinstance(body, dict):
        return None
    tx = body.get("transaction")
    if isinstance(tx, dict) and tx.get("hash"):
        return str(tx["hash"])
    if isinstance(tx, str) and tx:
        return tx
    for key in ("txHash", "tx_hash", "transaction_hash"):
        if body.get(key):
            return str(body[key])
    return None


def _facilitator_error_tx_hash(exc: FacilitatorError) -> Optional[str]:
    """Extract a tx hash from a FacilitatorError's raw response body, if present."""
    if not exc.response_body:
        return None
    try:
        body = json.loads(exc.response_body)
    except (ValueError, TypeError):
        return None
    return _extract_tx_hash_from_body(body)


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

    See the policy block above. The anti-double-settle guard lives here: a
    5xx whose body already contains a transaction hash is NOT retryable.
    """
    if isinstance(exc, X402TimeoutError):
        # The facilitator is idempotent per EIP-3009 nonce, and the SDK's
        # on-chain fallback check already ran before this was raised.
        return True
    if isinstance(exc, FacilitatorError):
        if exc.status_code is None:
            # Wrapped httpx.RequestError — transient transport issue.
            return True
        if exc.status_code < 500:
            return False
        if _facilitator_error_tx_hash(exc) is not None:
            logger.warning(
                "Facilitator returned %d but body contains a tx hash — "
                "not retrying to avoid double-settle.",
                exc.status_code,
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
    * Any other ``X402Error`` -> respects ``details["retryable"]`` when the
      raiser set it; otherwise final.
    * Non-x402 exceptions -> final (this function judges the payment path,
      not the world).
    """
    if isinstance(exc, X402TimeoutError):
        return True
    if isinstance(exc, FacilitatorError):
        if exc.status_code is None:
            return True
        if exc.status_code == 429:
            return True
        if exc.status_code >= 500:
            if anti_double_settle and _facilitator_error_tx_hash(exc) is not None:
                return False
            return True
        return False
    if isinstance(exc, X402Error):
        details = getattr(exc, "details", None) or {}
        return bool(details.get("retryable", False))
    return False


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
    the facilitator's ``reason`` when it sent one, and ``safeToRetry``:
    ``False`` for ``forward_failed`` and for any unrecognised ``reason``, where
    the write may already have executed.

    ``Retry-After`` is the facilitator's value clamped to
    ``MAX_RETRY_AFTER_SECONDS`` — a misconfigured deployment answering
    ``Retry-After: 3600`` gets to say "later", not to park a buyer for an hour.
    """
    retry_after = retry_after_seconds(exc, default_retry_after) or default_retry_after
    retry_after = min(float(retry_after), MAX_RETRY_AFTER_SECONDS)
    reason = facilitator_reason(exc)

    body: Dict[str, Any] = dict(exc.to_dict())
    body["retryable"] = True
    body["retryAfter"] = retry_after
    if reason is not None:
        body["reason"] = reason
        body["safeToRetry"] = write_retry_is_safe(reason)
    return body, {
        "Content-Type": "application/json",
        "Retry-After": str(int(retry_after)) if retry_after == int(retry_after) else str(retry_after),
    }


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

        # Client-side signer (set via connect_with_private_key)
        self._signer: Any = None  # eth_account.Account when connected
        self._signer_address: Optional[str] = None
        # Normalised signing seam. BOTH connect_* methods populate this with a
        # callable (domain, types, message) -> "0x…" 65-byte signature, so
        # create_authorization has a single code path and the local and remote
        # signers cannot drift apart.
        self._sign_typed_data: Optional[Any] = None
        self._connected_chain: Optional[str] = None

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
                f"{facilitator_url}/supported", timeout=self.config.verify_timeout
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

        # Convert USD to token amount. With an explicit decimals the conversion
        # stays in Decimal: float(Decimal("0.07")) is 0.070000000000000007, and
        # at 18 decimals that rounds into a different amount than the payer
        # signed, which the facilitator rejects.
        if token_decimals is not None:
            if token_decimals < 0:
                raise ValueError(f"token_decimals must be non-negative, got {token_decimals}")
            expected_amount_wei = int(expected_amount_usd * (Decimal(10) ** token_decimals))
        else:
            expected_amount_wei = network_config.get_token_amount(float(expected_amount_usd))

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
            maxTimeoutSeconds=60,
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

    def verify_payment(
        self,
        payload: PaymentPayload,
        expected_amount_usd: Decimal,
        pay_to: Optional[str] = None,
        *,
        asset: Optional[str] = None,
        eip712_domain: Optional[Dict[str, str]] = None,
        token_decimals: Optional[int] = None,
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

        Returns:
            VerifyResponse from facilitator

        Raises:
            PaymentVerificationError: If verification fails
            FacilitatorError: If facilitator returns an error
            TimeoutError: If request times out
        """
        normalized_network = self.validate_network(payload.network)
        requirements = self._build_payment_requirements(
            payload,
            expected_amount_usd,
            pay_to=pay_to,
            asset=asset,
            eip712_domain=eip712_domain,
            token_decimals=token_decimals,
        )

        verify_request = {
            "x402Version": 1,
            "paymentPayload": payload.model_dump(by_alias=True),
            "paymentRequirements": requirements.model_dump(by_alias=True, exclude_none=True),
        }

        logger.info(f"Verifying payment on {payload.network} for ${expected_amount_usd}")
        logger.debug(f"Verify request: {json.dumps(verify_request, indent=2)}")

        try:
            client = self._get_http_client()
            response = client.post(
                f"{self.facilitator_url_for(payload.network)}/verify",
                json=verify_request,
                headers={"Content-Type": "application/json"},
                timeout=self.config.verify_timeout,
            )

            if response.status_code != 200:
                raise FacilitatorError(
                    message=f"Facilitator verify failed with status {response.status_code}",
                    status_code=response.status_code,
                    response_body=response.text,
                    reason=_facilitator_reason(response.text),
                    retry_after=_response_retry_after(response),
                )

            data = response.json()
            verify_response = VerifyResponse(**data)

            if not verify_response.isValid:
                raise PaymentVerificationError(
                    message=f"Payment verification failed: {verify_response.message}",
                    reason=verify_response.invalidReason,
                    errors=verify_response.errors,
                )

            logger.info(f"Payment verified! Payer: {verify_response.payer}")
            return verify_response

        except httpx.TimeoutException:
            raise X402TimeoutError(operation="verify", timeout_seconds=self.config.verify_timeout)
        except httpx.RequestError as e:
            raise FacilitatorError(message=f"Facilitator request failed: {e}")

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

        Returns:
            SettleResponse from facilitator

        Raises:
            PaymentSettlementError: If settlement fails
            FacilitatorError: If facilitator returns an error
            TimeoutError: If request times out
        """
        if not retry:
            return self._settle_once(
                payload, expected_amount_usd, pay_to=pay_to,
                asset=asset, eip712_domain=eip712_domain,
                token_decimals=token_decimals,
            )

        for attempt in range(1, SETTLE_RETRY_ATTEMPTS + 1):
            try:
                return self._settle_once(
                    payload, expected_amount_usd, pay_to=pay_to,
                    asset=asset, eip712_domain=eip712_domain,
                    token_decimals=token_decimals,
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
              - ``error`` (Optional[str]): error message when failed
        """
        try:
            response = self.settle_payment(
                payload, expected_amount_usd, pay_to=pay_to,
                asset=asset, eip712_domain=eip712_domain,
                token_decimals=token_decimals, retry=retry,
            )
        except X402Error as exc:
            tx_hash: Optional[str] = None
            if isinstance(exc, FacilitatorError):
                tx_hash = _facilitator_error_tx_hash(exc)
            elif isinstance(exc, PaymentSettlementError):
                tx_hash = exc.tx_hash
            return {"success": False, "tx_hash": tx_hash, "error": exc.message}
        return {
            "success": True,
            "tx_hash": response.get_transaction_hash(),
            "error": None,
        }

    def _settle_once(
        self,
        payload: PaymentPayload,
        expected_amount_usd: Decimal,
        pay_to: Optional[str] = None,
        asset: Optional[str] = None,
        eip712_domain: Optional[Dict[str, str]] = None,
        token_decimals: Optional[int] = None,
    ) -> SettleResponse:
        """Single settle attempt — the pre-retry settle_payment body, unchanged."""
        normalized_network = self.validate_network(payload.network)
        requirements = self._build_payment_requirements(
            payload,
            expected_amount_usd,
            pay_to=pay_to,
            asset=asset,
            eip712_domain=eip712_domain,
            token_decimals=token_decimals,
        )

        settle_request = {
            "x402Version": 1,
            "paymentPayload": payload.model_dump(by_alias=True),
            "paymentRequirements": requirements.model_dump(by_alias=True, exclude_none=True),
        }

        # Use per-network timeout (Ethereum L1 = 900s, L2s = 90s)
        settle_timeout = self._get_settle_timeout(payload.network)
        facilitator_url = self.facilitator_url_for(payload.network)
        logger.info(
            f"Settling payment on {payload.network} for ${expected_amount_usd} "
            f"(timeout={settle_timeout}s, facilitator={facilitator_url})"
        )
        logger.debug(f"Settle request: {json.dumps(settle_request, indent=2)}")

        try:
            client = self._get_http_client()
            response = client.post(
                f"{facilitator_url}/settle",
                json=settle_request,
                headers={"Content-Type": "application/json"},
                timeout=settle_timeout,
            )

            if response.status_code != 200:
                raise FacilitatorError(
                    message=f"Facilitator settle failed with status {response.status_code}",
                    status_code=response.status_code,
                    response_body=response.text,
                    reason=_facilitator_reason(response.text),
                    retry_after=_response_retry_after(response),
                )

            data = response.json()
            settle_response = SettleResponse(**data)

            if not settle_response.success:
                raise PaymentSettlementError(
                    message=f"Payment settlement failed: {settle_response.message}",
                    network=payload.network,
                    reason=settle_response.message,
                )

            tx_hash = settle_response.get_transaction_hash()
            logger.info(f"Payment settled! TX: {tx_hash}, Payer: {settle_response.payer}")
            return settle_response

        except httpx.TimeoutException:
            # ACCION 2: On-chain fallback - check if payment succeeded despite timeout
            logger.warning(
                f"Settle timed out after {settle_timeout}s on {payload.network}, "
                f"checking on-chain state..."
            )
            fallback = self._check_settle_fallback(
                settle_request, settle_timeout, facilitator_url
            )
            if fallback:
                return fallback
            raise X402TimeoutError(operation="settle", timeout_seconds=settle_timeout)
        except httpx.RequestError as e:
            raise FacilitatorError(message=f"Facilitator request failed: {e}")

    def _check_settle_fallback(
        self,
        settle_request: Dict[str, Any],
        settle_timeout: float,
        facilitator_url: Optional[str] = None,
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

        Returns:
            SettleResponse if payment was confirmed on-chain, None otherwise.
        """
        url = facilitator_url or self.config.facilitator_url
        try:
            client = self._get_http_client()
            response = client.post(
                f"{url}/settle",
                json=settle_request,
                headers={"Content-Type": "application/json"},
                timeout=30.0,  # Short timeout for fallback check
            )

            if response.status_code == 200:
                data = response.json()
                settle_response = SettleResponse(**data)
                if settle_response.success:
                    tx_hash = settle_response.get_transaction_hash()
                    logger.info(
                        f"Fallback confirmed payment on-chain! "
                        f"TX: {tx_hash}, Payer: {settle_response.payer}"
                    )
                    return settle_response

            logger.warning("Fallback check: payment not confirmed on-chain")
            return None

        except Exception as e:
            logger.warning(f"Fallback check failed: {e}")
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
    ) -> PaymentResult:
        """
        Process a complete x402 payment (verify + settle).

        This is the main method for handling payments. It:
        1. Extracts and validates the payment payload
        2. Verifies the payment signature with the facilitator
        3. Settles the payment on-chain
        4. Returns the payment result

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

        Returns:
            PaymentResult with payer address, transaction hash, etc.

        Raises:
            InvalidPayloadError: If payload is invalid
            UnsupportedNetworkError: If network is not supported
            PaymentVerificationError: If verification fails
            PaymentSettlementError: If settlement fails
            FacilitatorError: If facilitator returns an error
            TimeoutError: If request times out
        """
        # Extract payload
        payload = self.extract_payload(x_payment_header)
        logger.info(f"Processing payment: network={payload.network}, amount=${expected_amount_usd}")

        # Verify payment
        verify_response = self.verify_payment(
            payload, expected_amount_usd, pay_to=pay_to,
            asset=asset, eip712_domain=eip712_domain,
            token_decimals=token_decimals,
        )

        # Settle payment
        settle_response = self.settle_payment(
            payload, expected_amount_usd, pay_to=pay_to,
            asset=asset, eip712_domain=eip712_domain,
            token_decimals=token_decimals,
        )

        # Build result
        return PaymentResult(
            success=True,
            payer_address=settle_response.payer or verify_response.payer or "",
            transaction_hash=settle_response.get_transaction_hash(),
            network=payload.network,
            amount_usd=expected_amount_usd,
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
        url = f"{self._accepts_facilitator_url(payment_requirements)}/accepts"
        payload = {
            "x402Version": x402_version,
            "accepts": payment_requirements,
        }

        try:
            client = self._get_http_client()
            response = client.post(
                url,
                json=payload,
                headers={"Content-Type": "application/json"},
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
            response = client.get(f"{self.config.facilitator_url}/version")
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
            response = client.get(f"{base_url}/supported")
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
            response = client.get(f"{self.config.facilitator_url}{path}")
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
            response = client.get(f"{self.config.facilitator_url}/blacklist")
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
            response = client.get(f"{base_url}/health")
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

        self._sign_typed_data = _sign_remote

        logger.info(
            f"Connected external signer {address}"
            + (f" on {self._connected_chain}" if self._connected_chain else "")
        )
        return address

    @property
    def is_connected(self) -> bool:
        """Check if a signer is connected (private key OR external)."""
        return self._sign_typed_data is not None

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
            amount_usd: Payment amount in USD
            chain_name: Network name (uses connected chain if not specified)
            valid_duration: Authorization validity in seconds (default: 1 hour)
            token_type: Token to pay with (default: 'usdc')
            eip712_domain: Override the EIP-712 domain used to SIGN
                ({"name": ..., "version": ...}). The domain is part of the
                signed digest — if the server/facilitator resolves a different
                name/version than the SDK registry (they diverge on some
                chains), the signature will not verify unless the caller
                injects the domain the verifier expects.

        Returns:
            Base64-encoded X-PAYMENT header value

        Raises:
            RuntimeError: If no signer is connected
            ImportError: If eth-account is not installed
            UnsupportedNetworkError: If chain is invalid

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
        if not self._sign_typed_data:
            raise RuntimeError(
                "No signer connected. Call connect_with_private_key() or "
                "connect_with_signer() first."
            )

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

        # Convert amount to base units
        amount_base = int(Decimal(str(amount_usd)) * (10 ** token_config.decimals))

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

    def _parse_402(self, body: Dict[str, Any]) -> Tuple[int, List[Dict[str, Any]]]:
        """Normalise a 402 body into (x402_version, [payment options]).

        Handles both the spec shape ``{x402Version, accepts: [...]}`` (v1 and v2)
        and the non-spec shape where a single requirement sits at the top level.
        Each option is normalised to ``{network, asset, amount, payTo,
        eip712_domain, raw}`` -- ``amount`` in token base units, ``raw`` the
        original accept object (echoed verbatim for v2).
        """
        version = int(body.get("x402Version", 1))
        accepts = body.get("accepts")
        if accepts is None:
            accepts = [body] if body.get("payTo") else []

        options: List[Dict[str, Any]] = []
        for entry in accepts:
            if not isinstance(entry, dict):
                continue
            # v1 uses `maxAmountRequired`; v2 PaymentOption uses `amount`.
            amount = entry.get("amount")
            if amount is None:
                amount = entry.get("maxAmountRequired")
            pay_to = entry.get("payTo")
            network = entry.get("network")
            if amount is None or not pay_to or not network:
                continue
            options.append(
                {
                    "network": network,
                    "asset": entry.get("asset"),
                    "amount": str(amount),
                    "payTo": pay_to,
                    "eip712_domain": entry.get("extra"),
                    "raw": entry,
                }
            )
        return version, options

    def _select_payment_option(
        self,
        options: List[Dict[str, Any]],
        token_decimals: int,
    ) -> Optional[Dict[str, Any]]:
        """Default selector: the cheapest option, ignoring the ceiling.

        The ``max_amount`` ceiling is NOT applied here on purpose: if even the
        cheapest option is over budget, the caller wants a clear "this costs
        more than you allowed" (:class:`PaymentExceedsMaxError`), not a vague
        "no acceptable option". So this always returns the cheapest, and
        :meth:`fetch` enforces the ceiling with one explicit check afterwards.
        """
        if not options:
            return None
        scale = Decimal(10) ** token_decimals
        return min(options, key=lambda opt: Decimal(opt["amount"]) / scale)

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
            token_type: Token to pay with (default ``usdc``).
            token_decimals: Decimals of ``token_type`` (default 6 for USDC).
            select: Optional ``callable(options) -> option`` to override the
                default cheapest-within-ceiling selection.
            valid_duration: Authorization validity in seconds.
            eip712_domain: Override the signing domain (see
                :meth:`create_authorization`); by default the domain from the
                chosen accept's ``extra`` is used when present.
            http_client: Reuse a specific ``httpx.Client`` (default: the SDK's).
            **request_kwargs: Passed through to the probe and the paid retry
                (``headers``, ``params``, ``json``, ``timeout``, ...).

        Returns:
            The ``httpx.Response`` -- the paid one after a 402, or the original.

        Raises:
            RuntimeError: No signer connected.
            PaymentExceedsMaxError: The price exceeds ``max_amount``.
            NoAcceptablePaymentError: The 402 offered no option within the ceiling.

        Example:
            >>> client.connect_with_private_key(key, chain_name="base")
            >>> resp = client.fetch(
            ...     "https://api.example.com/data",
            ...     max_amount="0.05",
            ... )
            >>> resp.json()
        """
        if not self._sign_typed_data:
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

        version, options = self._parse_402(body)
        if not options:
            raise NoAcceptablePaymentError(
                "402 response offered no usable payment options", resource=url
            )

        chosen = (
            select(options) if select is not None
            else self._select_payment_option(options, token_decimals)
        )
        if not chosen:
            raise NoAcceptablePaymentError(
                "no payment option within max_amount", resource=url
            )

        price = Decimal(chosen["amount"]) / (Decimal(10) ** token_decimals)
        if ceiling is not None and price > ceiling:
            raise PaymentExceedsMaxError(price, ceiling, resource=url)

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
        return client.request(method, url, headers=paid_headers, **request_kwargs)
