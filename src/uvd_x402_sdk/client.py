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
from typing import Optional, Tuple, Dict, Any, Union

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

    def extract_payload(self, x_payment_header: str) -> PaymentPayload:
        """
        Extract and validate payment payload from X-PAYMENT header.

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

            # Validate and parse with Pydantic
            payload = PaymentPayload(**data)

            logger.debug(f"Extracted payload for network: {payload.network}")
            return payload

        except base64.binascii.Error as e:
            raise InvalidPayloadError(f"Invalid base64 encoding: {e}")
        except json.JSONDecodeError as e:
            raise InvalidPayloadError(f"Invalid JSON in payload: {e}")
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
                backoff = min(float(2 ** (attempt - 1)), _SETTLE_RETRY_MAX_BACKOFF_SECONDS)
                logger.warning(
                    "Settle attempt %d/%d failed (%s) — retrying in %.0fs",
                    attempt, SETTLE_RETRY_ATTEMPTS, exc, backoff,
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
