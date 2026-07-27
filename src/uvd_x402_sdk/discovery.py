"""
Bazaar Discovery client for x402 SDK.

Register and discover paid resources on the x402 Bazaar network.

Example:
    >>> from uvd_x402_sdk.discovery import BazaarClient
    >>>
    >>> async with BazaarClient() as bazaar:
    ...     # List resources that are actually reachable right now
    ...     page = await bazaar.list_resources(limit=20, health="alive")
    ...     for r in page.items:
    ...         print(r.url, r.health.status, r.curation.tier)
    ...
    ...     # Free-text search runs server-side over the whole catalog
    ...     hits = await bazaar.list_resources(q="logs")
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

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import httpx
from pydantic import BaseModel, Field, field_validator

#: Maximum length of the free-text `q` filter. Mirrors the facilitator's
#: `MAX_SEARCH_LEN`; longer needles are rejected server-side with a 400.
MAX_SEARCH_LEN = 128

#: Values accepted by the `health` filter of GET /discovery/resources.
HEALTH_FILTERS = (
    "alive",
    "degraded",
    "auth_gated",
    "quarantined",
    "unknown",
    "unprobeable",
    "any",
)

#: Values accepted by the `tier` filter of GET /discovery/resources.
TIER_FILTERS = ("first_party", "vip", "verified", "listed")


def _coerce_epoch(value: Any) -> Optional[int]:
    """
    Normalize a timestamp to Unix epoch seconds.

    The registry serializes timestamps as epoch integers, but the same fields
    show up as numeric strings or ISO-8601 strings in exports, fixtures and
    other facilitators. Accept all of them rather than failing validation on
    the whole page because one field arrived in a different shape.
    """
    if value is None:
        return None
    # bool is an int subclass; a boolean timestamp is always a bug upstream.
    if isinstance(value, bool):
        raise ValueError("timestamp must be a number or string, got bool")
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, datetime):
        dt = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return None
        try:
            return int(raw)
        except ValueError:
            pass
        try:
            return int(float(raw))
        except ValueError:
            pass
        # ISO-8601, with or without the trailing Z.
        iso = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
        try:
            dt = datetime.fromisoformat(iso)
        except ValueError as exc:
            raise ValueError(f"could not parse timestamp {value!r}") from exc
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    raise ValueError(f"could not parse timestamp {value!r}")


def _epoch_to_datetime(value: Optional[int]) -> Optional[datetime]:
    """Render an epoch-seconds field as a timezone-aware UTC datetime."""
    if value is None:
        return None
    return datetime.fromtimestamp(value, tz=timezone.utc)


class DiscoveryHealth(BaseModel):
    """
    Liveness of a registered resource, as measured by the facilitator's prober.

    `status` is one of alive, degraded, auth_gated, quarantined, unknown or
    unprobeable. Resources that stopped answering are quarantined rather than
    deleted, so filter on this before paying anyone.
    """

    status: Optional[str] = None
    last_checked: Optional[int] = Field(None, alias="lastChecked")
    http_status: Optional[int] = Field(None, alias="httpStatus")
    latency_ms: Optional[int] = Field(None, alias="latencyMs")

    @field_validator("last_checked", mode="before")
    @classmethod
    def _parse_last_checked(cls, value: Any) -> Optional[int]:
        return _coerce_epoch(value)

    @property
    def is_alive(self) -> bool:
        """True when the last probe reached the resource."""
        return self.status == "alive"

    @property
    def last_checked_at(self) -> Optional[datetime]:
        """`last_checked` as a timezone-aware UTC datetime."""
        return _epoch_to_datetime(self.last_checked)

    class Config:
        populate_by_name = True
        extra = "allow"


class DiscoveryCuration(BaseModel):
    """
    Curation tier assigned to a resource.

    `tier` is one of first_party, vip, verified or listed, in descending order
    of trust. `label` is the human-readable name of the curated set.
    """

    tier: Optional[str] = None
    label: Optional[str] = None

    class Config:
        populate_by_name = True
        extra = "allow"


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
    first_seen: Optional[int] = Field(None, alias="firstSeen")
    last_seen: Optional[int] = Field(None, alias="lastSeen")
    last_updated: Optional[int] = Field(None, alias="lastUpdated")
    health: Optional[DiscoveryHealth] = None
    curation: Optional[DiscoveryCuration] = None

    @field_validator("first_seen", "last_seen", "last_updated", mode="before")
    @classmethod
    def _parse_timestamps(cls, value: Any) -> Optional[int]:
        return _coerce_epoch(value)

    @property
    def first_seen_at(self) -> Optional[datetime]:
        """`first_seen` as a timezone-aware UTC datetime."""
        return _epoch_to_datetime(self.first_seen)

    @property
    def last_seen_at(self) -> Optional[datetime]:
        """`last_seen` as a timezone-aware UTC datetime."""
        return _epoch_to_datetime(self.last_seen)

    @property
    def last_updated_at(self) -> Optional[datetime]:
        """`last_updated` as a timezone-aware UTC datetime."""
        return _epoch_to_datetime(self.last_updated)

    @property
    def is_alive(self) -> bool:
        """True when the last health probe reached this resource."""
        return self.health is not None and self.health.is_alive

    @property
    def tier(self) -> Optional[str]:
        """Curated tier, or None when the resource is uncurated."""
        return self.curation.tier if self.curation else None

    class Config:
        populate_by_name = True
        # Keep fields the server adds later instead of dropping them on the
        # floor: an unmodelled field is invisible, and invisible is how
        # `health` and `curation` went missing for so long.
        extra = "allow"


class DiscoveryPagination(BaseModel):
    """Pagination envelope of GET /discovery/resources."""

    limit: int = 0
    offset: int = 0
    total: int = 0

    def __getitem__(self, key: str) -> Any:
        """Dict-style access, so `pagination["total"]` keeps working."""
        try:
            return getattr(self, key)
        except AttributeError as exc:
            raise KeyError(key) from exc

    def get(self, key: str, default: Any = None) -> Any:
        """Dict-style access with a fallback."""
        return getattr(self, key, default)

    class Config:
        populate_by_name = True
        extra = "allow"


class DiscoveryResponse(BaseModel):
    """Paginated response from GET /discovery/resources."""

    x402_version: int = Field(2, alias="x402Version")
    items: List[DiscoveryResource] = Field(default_factory=list)
    pagination: DiscoveryPagination = Field(default_factory=DiscoveryPagination)

    class Config:
        populate_by_name = True
        extra = "allow"


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
        provider: Optional[str] = None,
        tag: Optional[str] = None,
        source: Optional[str] = None,
        source_facilitator: Optional[str] = None,
        health: Optional[str] = None,
        tier: Optional[str] = None,
        q: Optional[str] = None,
    ) -> DiscoveryResponse:
        """
        List registered resources from the Bazaar discovery registry.

        Every filter is applied server-side over the whole catalog, so
        `pagination.total` reflects the filtered set. Filtering a page after
        the fact is not the same thing and will silently under-report.

        Args:
            limit: Maximum number of resources to return (default: 10, max: 100)
            offset: Number of resources to skip (for pagination)
            category: Filter by category (e.g., "finance", "ai")
            network: Filter by network (e.g., "base-mainnet", "eip155:8453")
            provider: Filter by provider name
            tag: Filter by tag
            source: Filter by discovery source (self_registered, settlement,
                crawled, aggregated)
            source_facilitator: Filter by originating facilitator
            health: Filter by liveness, one of `HEALTH_FILTERS`
            tier: Filter by curated tier, one of `TIER_FILTERS`
            q: Free-text search over url / description / provider / category /
                tags, at most `MAX_SEARCH_LEN` characters

        Returns:
            Paginated list of discovery resources
        """
        if q is not None and len(q) > MAX_SEARCH_LEN:
            raise ValueError(f"q must be at most {MAX_SEARCH_LEN} characters")
        if health is not None and health not in HEALTH_FILTERS:
            raise ValueError(f"health must be one of {', '.join(HEALTH_FILTERS)}")
        if tier is not None and tier not in TIER_FILTERS:
            raise ValueError(f"tier must be one of {', '.join(TIER_FILTERS)}")

        params: Dict[str, Any] = {"limit": limit, "offset": offset}
        optional = {
            "category": category,
            "network": network,
            "provider": provider,
            "tag": tag,
            "source": source,
            "sourceFacilitator": source_facilitator,
            "health": health,
            "tier": tier,
            "q": q,
        }
        params.update({k: v for k, v in optional.items() if v is not None})

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
