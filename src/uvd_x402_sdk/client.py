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
from typing import Optional, Tuple, Dict, Any

import httpx

from uvd_x402_sdk.config import X402Config
from uvd_x402_sdk.exceptions import (
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
        **kwargs: Any,
    ) -> None:
        """
        Initialize the x402 client.

        Args:
            recipient_address: Default recipient for EVM chains (convenience arg)
            facilitator_url: URL of the facilitator service
            config: Full X402Config object (overrides other args)
            **kwargs: Additional config parameters passed to X402Config

        Raises:
            ValueError: If no recipient address is configured
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
        self._connected_chain: Optional[str] = None

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
    ) -> PaymentRequirements:
        """
        Build payment requirements for facilitator request.

        Args:
            payload: Parsed payment payload
            expected_amount_usd: Expected payment amount in USD

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

        # Convert USD to token amount
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
            asset=network_config.usdc_address,
        )

        # Add EIP-712 domain params for EVM chains
        if network_config.network_type == NetworkType.EVM:
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
    ) -> VerifyResponse:
        """
        Verify payment with the facilitator.

        This validates the signature/authorization without settling on-chain.

        Args:
            payload: Parsed payment payload
            expected_amount_usd: Expected payment amount in USD
            pay_to: Override recipient address (must match auth.to in EIP-3009)

        Returns:
            VerifyResponse from facilitator

        Raises:
            PaymentVerificationError: If verification fails
            FacilitatorError: If facilitator returns an error
            TimeoutError: If request times out
        """
        normalized_network = self.validate_network(payload.network)
        requirements = self._build_payment_requirements(payload, expected_amount_usd, pay_to=pay_to)

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
                f"{self.config.facilitator_url}/verify",
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
    ) -> SettleResponse:
        """
        Settle payment on-chain via the facilitator.

        This executes the actual on-chain transfer.

        Args:
            payload: Parsed payment payload
            expected_amount_usd: Expected payment amount in USD
            pay_to: Override recipient address (must match auth.to in EIP-3009)

        Returns:
            SettleResponse from facilitator

        Raises:
            PaymentSettlementError: If settlement fails
            FacilitatorError: If facilitator returns an error
            TimeoutError: If request times out
        """
        normalized_network = self.validate_network(payload.network)
        requirements = self._build_payment_requirements(payload, expected_amount_usd, pay_to=pay_to)

        settle_request = {
            "x402Version": 1,
            "paymentPayload": payload.model_dump(by_alias=True),
            "paymentRequirements": requirements.model_dump(by_alias=True, exclude_none=True),
        }

        # Use per-network timeout (Ethereum L1 = 900s, L2s = 90s)
        settle_timeout = self._get_settle_timeout(payload.network)
        logger.info(
            f"Settling payment on {payload.network} for ${expected_amount_usd} "
            f"(timeout={settle_timeout}s)"
        )
        logger.debug(f"Settle request: {json.dumps(settle_request, indent=2)}")

        try:
            client = self._get_http_client()
            response = client.post(
                f"{self.config.facilitator_url}/settle",
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
            fallback = self._check_settle_fallback(settle_request, settle_timeout)
            if fallback:
                return fallback
            raise X402TimeoutError(operation="settle", timeout_seconds=settle_timeout)
        except httpx.RequestError as e:
            raise FacilitatorError(message=f"Facilitator request failed: {e}")

    def _check_settle_fallback(
        self,
        settle_request: Dict[str, Any],
        settle_timeout: float,
    ) -> Optional[SettleResponse]:
        """
        Check on-chain state after a settle timeout.

        When the HTTP request times out, the on-chain transaction may still
        have succeeded. This queries the facilitator's /settle endpoint again
        with a short timeout to check if the transaction was confirmed.

        Returns:
            SettleResponse if payment was confirmed on-chain, None otherwise.
        """
        try:
            client = self._get_http_client()
            response = client.post(
                f"{self.config.facilitator_url}/settle",
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
        verify_response = self.verify_payment(payload, expected_amount_usd, pay_to=pay_to)

        # Settle payment
        settle_response = self.settle_payment(payload, expected_amount_usd, pay_to=pay_to)

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
        url = f"{self.config.facilitator_url}/accepts"
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

    def get_supported(self) -> Dict[str, Any]:
        """
        Get the facilitator's supported networks and payment schemes.

        Returns:
            Dict with 'kinds' array of supported network/scheme combos

        Example:
            >>> supported = client.get_supported()
            >>> for kind in supported["kinds"]:
            ...     print(f"{kind['network']} - {kind['scheme']}")

        Raises:
            FacilitatorError: If the request fails
        """
        try:
            client = self._get_http_client()
            response = client.get(f"{self.config.facilitator_url}/supported")
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

    def health_check(self) -> bool:
        """
        Check facilitator health.

        Returns:
            True if the facilitator is healthy
        """
        try:
            client = self._get_http_client()
            response = client.get(f"{self.config.facilitator_url}/health")
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
        # For SVM/NEAR, payer is determined during verification

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

        logger.info(f"Connected wallet {self._signer_address}"
                     + (f" on {self._connected_chain}" if self._connected_chain else ""))

        return self._signer_address

    @property
    def is_connected(self) -> bool:
        """Check if a signer is connected."""
        return self._signer is not None

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
        if not self._signer:
            raise RuntimeError(
                "No signer connected. Call connect_with_private_key() first."
            )

        try:
            from eth_account.messages import encode_typed_data
        except ImportError:
            raise ImportError(
                "eth-account is required for create_authorization. "
                "Install it with: pip install uvd-x402-sdk[signer]"
            )

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

        # EIP-712 domain
        domain_data = {
            "name": token_config.name,
            "version": token_config.version,
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

        # Sign: encode_typed_data + sign_message (proven method from x402r SDK)
        signable = encode_typed_data(
            domain_data=domain_data,
            message_types=types,
            message_data=message,
        )
        signed = self._signer.sign_message(signable)
        signature = signed.signature.hex()

        # Build x402 payload
        payload = {
            "x402Version": 1,
            "scheme": "exact",
            "network": network_config.name,
            "payload": {
                "signature": "0x" + signature,
                "authorization": {
                    "from": self._signer_address,
                    "to": pay_to,
                    "value": str(amount_base),
                    "validAfter": str(valid_after),
                    "validBefore": str(valid_before),
                    "nonce": nonce,
                },
            },
        }

        # Add token info for non-USDC tokens
        if token_type != "usdc":
            payload["payload"]["token"] = {
                "address": token_config.address,
                "symbol": token_type.upper(),
                "eip712": {
                    "name": token_config.name,
                    "version": token_config.version,
                },
            }

        # Encode to base64
        json_bytes = json.dumps(payload).encode("utf-8")
        return base64.b64encode(json_bytes).decode("utf-8")
