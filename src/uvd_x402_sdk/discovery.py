"""
Bazaar Discovery client for x402 SDK.

Register and discover paid resources on the x402 Bazaar network.

Example:
    >>> from uvd_x402_sdk.discovery import BazaarClient
    >>>
    >>> async with BazaarClient() as bazaar:
    ...     # List available resources
    ...     resources = await bazaar.list_resources(category="finance")
    ...     for r in resources["items"]:
    ...         print(f"{r['url']} - {r['description']}")
    ...
    ...     # Register your own resource
    ...     await bazaar.register_resource(
    ...         url="https://api.example.com/data",
    ...         resource_type="http",
    ...         description="Premium data API",
    ...         accepts=[{
    ...             "scheme": "exact",
    ...             "network": "base-mainnet",
    ...             "maxAmountRequired": "10000",
    ...             "payTo": "0xYourWallet...",
    ...         }],
    ...     )
"""

from typing import Any, Dict, List, Optional

import httpx
from pydantic import BaseModel, Field


class DiscoveryResource(BaseModel):
    """A discoverable paid resource on the Bazaar."""

    url: str
    resource_type: str = Field(..., alias="type")
    x402_version: int = Field(2, alias="x402Version")
    description: str = ""
    accepts: List[Dict[str, Any]] = Field(default_factory=list)
    metadata: Optional[Dict[str, Any]] = None
    source: Optional[str] = None
    source_facilitator: Optional[str] = Field(None, alias="sourceFacilitator")
    first_seen: Optional[str] = Field(None, alias="firstSeen")
    last_seen: Optional[str] = Field(None, alias="lastSeen")

    class Config:
        populate_by_name = True


class DiscoveryResponse(BaseModel):
    """Paginated response from GET /discovery/resources."""

    x402_version: int = Field(2, alias="x402Version")
    items: List[DiscoveryResource] = Field(default_factory=list)
    pagination: Dict[str, int] = Field(default_factory=dict)

    class Config:
        populate_by_name = True


class BazaarClient:
    """
    Client for the x402 Bazaar Discovery API.

    Enables registering paid resources and discovering available services
    across the x402 network.
    """

    def __init__(
        self,
        base_url: str = "https://facilitator.ultravioletadao.xyz",
        timeout: float = 30.0,
    ):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._client = httpx.AsyncClient(timeout=timeout)

    async def __aenter__(self) -> "BazaarClient":
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self._client.aclose()

    async def list_resources(
        self,
        *,
        limit: int = 10,
        offset: int = 0,
        category: Optional[str] = None,
        network: Optional[str] = None,
    ) -> DiscoveryResponse:
        """
        List registered resources from the Bazaar discovery registry.

        Args:
            limit: Maximum number of resources to return (default: 10, max: 100)
            offset: Number of resources to skip (for pagination)
            category: Filter by category (e.g., "finance", "ai")
            network: Filter by network (e.g., "base-mainnet", "eip155:8453")

        Returns:
            Paginated list of discovery resources
        """
        params: Dict[str, Any] = {"limit": limit, "offset": offset}
        if category:
            params["category"] = category
        if network:
            params["network"] = network

        url = f"{self.base_url}/discovery/resources"
        response = await self._client.get(url, params=params)
        response.raise_for_status()
        return DiscoveryResponse.model_validate(response.json())

    async def register_resource(
        self,
        url: str,
        resource_type: str = "http",
        description: str = "",
        accepts: Optional[List[Dict[str, Any]]] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Register a paid resource in the Bazaar discovery registry.

        Args:
            url: The URL of the paid resource
            resource_type: Type of resource ("http", "mcp", "a2a")
            description: Human-readable description
            accepts: Payment requirements the resource accepts
            metadata: Additional metadata (category, provider, tags)

        Returns:
            Registration result with success status

        Example:
            >>> await bazaar.register_resource(
            ...     url="https://api.example.com/premium-data",
            ...     resource_type="http",
            ...     description="Premium market data API",
            ...     accepts=[{
            ...         "scheme": "exact",
            ...         "network": "eip155:8453",
            ...         "asset": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
            ...         "amount": "10000",
            ...         "payTo": "0xRecipient...",
            ...         "maxTimeoutSeconds": 60,
            ...     }],
            ...     metadata={"category": "finance", "tags": ["market-data"]},
            ... )
        """
        payload: Dict[str, Any] = {
            "url": url,
            "type": resource_type,
            "description": description,
        }
        if accepts:
            payload["accepts"] = accepts
        if metadata:
            payload["metadata"] = metadata

        endpoint = f"{self.base_url}/discovery/register"
        response = await self._client.post(endpoint, json=payload)
        response.raise_for_status()
        return response.json()
