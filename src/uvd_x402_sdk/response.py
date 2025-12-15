"""
HTTP 402 response helpers.

This module provides utilities for creating standard 402 Payment Required
responses that are compatible with both x402 v1 and v2 protocols.

v1 Response:
- JSON body with payment requirements
- X-Accept-Payment header

v2 Response:
- PAYMENT-REQUIRED header (base64-encoded JSON)
- PAYMENT-SIGNATURE header
- accepts array with multiple payment options
"""

from decimal import Decimal
from typing import Dict, List, Optional, Any, Union, Literal

from uvd_x402_sdk.config import X402Config
from uvd_x402_sdk.models import Payment402Response, PaymentOption, PaymentRequirementsV2
from uvd_x402_sdk.networks import (
    get_network,
    list_networks,
    NetworkType,
    get_supported_network_names,
    to_caip2_network,
)


def create_402_response(
    amount_usd: Union[Decimal, float, str],
    config: X402Config,
    message: Optional[str] = None,
    resource_description: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Create a standard 402 Payment Required response body.

    This response tells the client what payment is required and provides
    all necessary information to create a payment authorization.

    Args:
        amount_usd: Required payment amount in USD
        config: X402Config with recipient addresses
        message: Optional custom message (default: generated)
        resource_description: Optional description of what's being purchased

    Returns:
        Dictionary suitable for JSON response body

    Example:
        >>> config = X402Config(recipient_evm="0x...")
        >>> response_body = create_402_response(
        ...     amount_usd=Decimal("10.00"),
        ...     config=config,
        ...     message="Payment required for API access"
        ... )
        >>> return JSONResponse(status_code=402, content=response_body)
    """
    amount = Decimal(str(amount_usd))

    # Build recipients map
    recipients: Dict[str, str] = {}
    if config.recipient_evm:
        recipients["evm"] = config.recipient_evm
    if config.recipient_solana:
        recipients["solana"] = config.recipient_solana
    if config.recipient_near:
        recipients["near"] = config.recipient_near
    if config.recipient_stellar:
        recipients["stellar"] = config.recipient_stellar

    # Get supported chain IDs and network names
    supported_chains: List[Union[int, str]] = []

    for network_name in config.supported_networks:
        network = get_network(network_name)
        if network and network.enabled and config.is_network_enabled(network_name):
            if network.network_type == NetworkType.EVM and network.chain_id > 0:
                supported_chains.append(network.chain_id)
            else:
                # Non-EVM networks: include name
                supported_chains.append(network_name)

    # Default message
    if not message:
        message = f"Payment of ${amount} USDC required"
        if resource_description:
            message += f" for {resource_description}"

    response = Payment402Response(
        error="Payment required",
        recipient=config.recipient_evm,  # Default for backward compatibility
        recipients=recipients if recipients else None,
        facilitator=config.facilitator_solana,
        amount=str(amount),
        token="USDC",
        supportedChains=supported_chains,
        message=message,
    )

    return response.model_dump(exclude_none=True, by_alias=True)


def create_402_headers(
    accept_payment: str = "x402 USDC 1.0",
) -> Dict[str, str]:
    """
    Create headers for a 402 response.

    Args:
        accept_payment: Value for X-Accept-Payment header

    Returns:
        Dictionary of headers
    """
    return {
        "Content-Type": "application/json",
        "X-Accept-Payment": accept_payment,
    }


def payment_required_response(
    amount_usd: Union[Decimal, float, str],
    config: X402Config,
    message: Optional[str] = None,
) -> tuple:
    """
    Create a complete 402 response (body, headers, status code).

    Useful for frameworks that accept tuple responses.

    Args:
        amount_usd: Required payment amount
        config: X402Config with recipient addresses
        message: Optional custom message

    Returns:
        Tuple of (body_dict, headers_dict, status_code)

    Example (Flask):
        >>> @app.route("/api/resource")
        >>> def resource():
        ...     if not has_payment:
        ...         body, headers, status = payment_required_response(
        ...             amount_usd="1.00", config=config
        ...         )
        ...         return body, status, headers
    """
    body = create_402_response(amount_usd, config, message)
    headers = create_402_headers()
    return body, headers, 402


class Payment402Builder:
    """
    Builder class for constructing 402 responses with fluent API.

    Example:
        >>> response = (
        ...     Payment402Builder(config)
        ...     .amount(Decimal("5.00"))
        ...     .message("Premium feature access required")
        ...     .networks(["base", "solana"])
        ...     .build()
        ... )
    """

    def __init__(self, config: X402Config) -> None:
        self._config = config
        self._amount: Decimal = Decimal("1.00")
        self._message: Optional[str] = None
        self._description: Optional[str] = None
        self._networks: Optional[List[str]] = None
        self._extra_data: Dict[str, Any] = {}

    def amount(self, usd: Union[Decimal, float, str]) -> "Payment402Builder":
        """Set the required payment amount in USD."""
        self._amount = Decimal(str(usd))
        return self

    def message(self, msg: str) -> "Payment402Builder":
        """Set the payment message."""
        self._message = msg
        return self

    def description(self, desc: str) -> "Payment402Builder":
        """Set the resource description."""
        self._description = desc
        return self

    def networks(self, network_names: List[str]) -> "Payment402Builder":
        """Limit to specific networks."""
        self._networks = network_names
        return self

    def extra(self, key: str, value: Any) -> "Payment402Builder":
        """Add extra data to the response."""
        self._extra_data[key] = value
        return self

    def build(self) -> Dict[str, Any]:
        """Build the response body."""
        # Create modified config if networks are limited
        if self._networks:
            limited_config = X402Config(
                facilitator_url=self._config.facilitator_url,
                recipient_evm=self._config.recipient_evm,
                recipient_solana=self._config.recipient_solana,
                recipient_near=self._config.recipient_near,
                recipient_stellar=self._config.recipient_stellar,
                facilitator_solana=self._config.facilitator_solana,
                supported_networks=self._networks,
            )
        else:
            limited_config = self._config

        response = create_402_response(
            amount_usd=self._amount,
            config=limited_config,
            message=self._message,
            resource_description=self._description,
        )

        # Add extra data
        response.update(self._extra_data)

        return response

    def build_tuple(self) -> tuple:
        """Build complete response tuple (body, headers, status)."""
        return self.build(), create_402_headers(), 402


# =============================================================================
# x402 v2 Response Helpers
# =============================================================================


def create_402_response_v2(
    amount_usd: Union[Decimal, float, str],
    config: X402Config,
    resource: str = "",
    description: str = "",
    networks: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """
    Create an x402 v2 format 402 response with accepts array.

    x402 v2 uses CAIP-2 network identifiers and allows clients to
    choose from multiple payment options.

    Args:
        amount_usd: Required payment amount in USD
        config: X402Config with recipient addresses
        resource: Resource URL being purchased
        description: Human-readable description
        networks: List of networks to offer (default: all enabled)

    Returns:
        Dictionary suitable for JSON response body

    Example:
        >>> response = create_402_response_v2(
        ...     amount_usd=Decimal("5.00"),
        ...     config=config,
        ...     resource="/api/premium",
        ...     description="Premium API access",
        ... )
    """
    amount = Decimal(str(amount_usd))

    # Determine which networks to include
    if networks:
        network_list = networks
    else:
        network_list = config.supported_networks

    # Build accepts array
    accepts: List[Dict[str, Any]] = []

    for network_name in network_list:
        network = get_network(network_name)
        if not network or not network.enabled:
            continue
        if not config.is_network_enabled(network_name):
            continue

        # Get CAIP-2 format network ID
        caip2_network = to_caip2_network(network_name)
        if not caip2_network:
            continue

        # Get recipient for this network
        recipient = config.get_recipient(network_name)
        if not recipient:
            continue

        # Calculate amount in token base units
        token_amount = network.get_token_amount(float(amount))

        option: Dict[str, Any] = {
            "network": caip2_network,
            "asset": network.usdc_address,
            "amount": str(token_amount),
            "payTo": recipient,
        }

        # Add EIP-712 domain for EVM chains
        if network.network_type == NetworkType.EVM:
            option["extra"] = {
                "name": network.usdc_domain_name,
                "version": network.usdc_domain_version,
            }

        accepts.append(option)

    return {
        "x402Version": 2,
        "scheme": "exact",
        "resource": resource or config.resource_url or "/api/resource",
        "description": description or config.description,
        "mimeType": "application/json",
        "maxTimeoutSeconds": 60,
        "accepts": accepts,
    }


def create_402_headers_v2(
    requirements: Dict[str, Any],
) -> Dict[str, str]:
    """
    Create headers for an x402 v2 402 response.

    The PAYMENT-REQUIRED header contains base64-encoded JSON of the
    payment requirements.

    Args:
        requirements: Payment requirements dictionary

    Returns:
        Dictionary of headers
    """
    import base64
    import json

    requirements_json = json.dumps(requirements, separators=(',', ':'))
    requirements_b64 = base64.b64encode(requirements_json.encode()).decode()

    return {
        "Content-Type": "application/json",
        "PAYMENT-REQUIRED": requirements_b64,
        "X-Accept-Payment": "x402 USDC 2.0",
    }


def payment_required_response_v2(
    amount_usd: Union[Decimal, float, str],
    config: X402Config,
    resource: str = "",
    description: str = "",
    networks: Optional[List[str]] = None,
) -> tuple:
    """
    Create a complete x402 v2 402 response (body, headers, status code).

    Args:
        amount_usd: Required payment amount
        config: X402Config with recipient addresses
        resource: Resource URL
        description: Description
        networks: Networks to offer

    Returns:
        Tuple of (body_dict, headers_dict, status_code)
    """
    body = create_402_response_v2(amount_usd, config, resource, description, networks)
    headers = create_402_headers_v2(body)
    return body, headers, 402


class Payment402BuilderV2:
    """
    Builder class for constructing x402 v2 402 responses.

    Example:
        >>> response = (
        ...     Payment402BuilderV2(config)
        ...     .amount(Decimal("5.00"))
        ...     .resource("/api/premium")
        ...     .description("Premium feature access")
        ...     .networks(["base", "solana", "near"])
        ...     .build()
        ... )
    """

    def __init__(self, config: X402Config) -> None:
        self._config = config
        self._amount: Decimal = Decimal("1.00")
        self._resource: str = ""
        self._description: str = ""
        self._networks: Optional[List[str]] = None

    def amount(self, usd: Union[Decimal, float, str]) -> "Payment402BuilderV2":
        """Set the required payment amount in USD."""
        self._amount = Decimal(str(usd))
        return self

    def resource(self, url: str) -> "Payment402BuilderV2":
        """Set the resource URL."""
        self._resource = url
        return self

    def description(self, desc: str) -> "Payment402BuilderV2":
        """Set the resource description."""
        self._description = desc
        return self

    def networks(self, network_names: List[str]) -> "Payment402BuilderV2":
        """Limit to specific networks."""
        self._networks = network_names
        return self

    def build(self) -> Dict[str, Any]:
        """Build the v2 response body."""
        return create_402_response_v2(
            amount_usd=self._amount,
            config=self._config,
            resource=self._resource,
            description=self._description,
            networks=self._networks,
        )

    def build_with_headers(self) -> tuple:
        """Build complete response with headers (body, headers, status)."""
        body = self.build()
        headers = create_402_headers_v2(body)
        return body, headers, 402
