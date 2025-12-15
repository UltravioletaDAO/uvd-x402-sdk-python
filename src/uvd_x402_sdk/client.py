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

        # Get recipient for this network
        recipient = self.config.get_recipient(normalized_network)

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
    ) -> VerifyResponse:
        """
        Verify payment with the facilitator.

        This validates the signature/authorization without settling on-chain.

        Args:
            payload: Parsed payment payload
            expected_amount_usd: Expected payment amount in USD

        Returns:
            VerifyResponse from facilitator

        Raises:
            PaymentVerificationError: If verification fails
            FacilitatorError: If facilitator returns an error
            TimeoutError: If request times out
        """
        normalized_network = self.validate_network(payload.network)
        requirements = self._build_payment_requirements(payload, expected_amount_usd)

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
    ) -> SettleResponse:
        """
        Settle payment on-chain via the facilitator.

        This executes the actual on-chain transfer.

        Args:
            payload: Parsed payment payload
            expected_amount_usd: Expected payment amount in USD

        Returns:
            SettleResponse from facilitator

        Raises:
            PaymentSettlementError: If settlement fails
            FacilitatorError: If facilitator returns an error
            TimeoutError: If request times out
        """
        normalized_network = self.validate_network(payload.network)
        requirements = self._build_payment_requirements(payload, expected_amount_usd)

        settle_request = {
            "x402Version": 1,
            "paymentPayload": payload.model_dump(by_alias=True),
            "paymentRequirements": requirements.model_dump(by_alias=True, exclude_none=True),
        }

        logger.info(f"Settling payment on {payload.network} for ${expected_amount_usd}")
        logger.debug(f"Settle request: {json.dumps(settle_request, indent=2)}")

        try:
            client = self._get_http_client()
            response = client.post(
                f"{self.config.facilitator_url}/settle",
                json=settle_request,
                headers={"Content-Type": "application/json"},
                timeout=self.config.settle_timeout,
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
            raise X402TimeoutError(operation="settle", timeout_seconds=self.config.settle_timeout)
        except httpx.RequestError as e:
            raise FacilitatorError(message=f"Facilitator request failed: {e}")

    # =========================================================================
    # Main Processing Method
    # =========================================================================

    def process_payment(
        self,
        x_payment_header: str,
        expected_amount_usd: Decimal,
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
        verify_response = self.verify_payment(payload, expected_amount_usd)

        # Settle payment
        settle_response = self.settle_payment(payload, expected_amount_usd)

        # Build result
        return PaymentResult(
            success=True,
            payer_address=settle_response.payer or verify_response.payer or "",
            transaction_hash=settle_response.get_transaction_hash(),
            network=payload.network,
            amount_usd=expected_amount_usd,
        )

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
    ) -> Tuple[bool, str]:
        """
        Verify payment without settling.

        Useful for checking payment validity before committing to settlement.

        Args:
            x_payment_header: X-PAYMENT header value
            expected_amount_usd: Expected payment amount

        Returns:
            Tuple of (is_valid, payer_address)
        """
        payload = self.extract_payload(x_payment_header)
        verify_response = self.verify_payment(payload, expected_amount_usd)
        return verify_response.isValid, verify_response.payer or ""
