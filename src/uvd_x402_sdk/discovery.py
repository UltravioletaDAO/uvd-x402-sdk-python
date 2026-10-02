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
    ...     # Newer filters are sent only when passed (see list_resources)
    ...     cheap_posts = await bazaar.list_resources(max_price_usd="0.05", method="POST")
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
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional, Union

import httpx
from pydantic import BaseModel, Field, field_validator

from uvd_x402_sdk.stack_key import (
    refuse_redirect_hook,
    stack_key_request_kwargs,
    usable_stack_key,
)

#: Default client-side cap on the free-text `q` filter, in characters (code
#: points, the facilitator's `q.chars().count()`). The facilitator is moving
#: `q` to natural-language relevance search of about 400 characters; x402-rs
#: 2.46.1 and earlier still answer a 400 above 128 characters. The cap is per
#: client: `BazaarClient(max_search_len=...)`, `None` leaves it to the server.
MAX_SEARCH_LEN = 400

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

#: Values accepted by the `method` filter: the HTTP methods of the x402
#: `bazaar` extension (`info.input.method`). Matched case-insensitively and
#: sent upper-case.
METHOD_FILTERS = ("GET", "HEAD", "DELETE", "POST", "PUT", "PATCH")


def _price_param(value: Union[Decimal, int, float, str]) -> str:
    """`max_price_usd` as a plain non-negative decimal string.

    `str(Decimal("1E+2"))` is `"1E+2"`, which is not a price a query string
    should carry; `format(..., "f")` writes `"100"`.
    """
    if isinstance(value, bool) or not isinstance(value, (Decimal, int, float, str)):
        raise ValueError("max_price_usd must be a number or a decimal string")
    try:
        price = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError(f"max_price_usd is not a decimal: {value!r}") from exc
    if not price.is_finite() or price < 0:
        raise ValueError(f"max_price_usd must be a finite amount >= 0, got {value!r}")
    if price == 0:
        return "0"
    return format(price, "f")


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
        *,
        stack_key: Optional[str] = None,
        stack_key_hosts: Optional[list[str]] = None,
        max_search_len: Optional[int] = MAX_SEARCH_LEN,
    ):
        """``stack_key``: the ``X-UVD-Stack-Key`` of a service of Ultravioleta
        DAO, sent on every request when ``base_url`` is a facilitator of
        Ultravioleta DAO (``stack_key_hosts`` adds hosts); see
        ``uvd_x402_sdk.stack_key``. Not for third parties.

        ``max_search_len``: longest ``q`` that ``list_resources`` sends, in
        characters (default ``MAX_SEARCH_LEN``, 400). A longer one raises
        ``ValueError`` before any request. ``None`` sends any length and lets
        the facilitator decide; ``128`` matches x402-rs 2.46.1 and earlier."""
        if max_search_len is not None and (
            isinstance(max_search_len, bool)
            or not isinstance(max_search_len, int)
            or max_search_len < 1
        ):
            raise ValueError("max_search_len must be a positive int or None")
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.max_search_len = max_search_len
        self._stack_key = usable_stack_key(stack_key)
        self._stack_key_hosts = stack_key_hosts
        self._client = httpx.AsyncClient(
            timeout=timeout, event_hooks={"response": [refuse_redirect_hook]}
        )

    def _stack_key_kwargs(self) -> dict[str, Any]:
        return stack_key_request_kwargs(self._stack_key, self.base_url, self._stack_key_hosts)

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
        max_price_usd: Optional[Union[Decimal, int, float, str]] = None,
        method: Optional[str] = None,
        has_input_schema: Optional[bool] = None,
        kind: Optional[str] = None,
        exclude_host: Optional[str] = None,
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
                tags, at most `max_search_len` characters (see `__init__`)
            max_price_usd: Only resources priced at or under this many USD
                (`maxPriceUsd`); a number or a decimal string, sent as a plain
                decimal
            method: Only resources called with this HTTP method (`method`),
                one of `METHOD_FILTERS`, case-insensitive
            has_input_schema: Only resources that do (True) or do not (False)
                declare an input schema (`hasInputSchema`)
            kind: Only resources of this kind (`kind`), sent as given
            exclude_host: Leave out the resources of this host
                (`excludeHost`), sent as given

        The last five filters need a facilitator that knows them: newer than
        x402-rs 2.46.1, which answers a 400 (`httpx.HTTPStatusError` here) to
        any query parameter it does not know rather than ignoring it. Each one
        is sent only when passed, so a call that passes none of them is the
        same request as before.

        Returns:
            Paginated list of discovery resources
        """
        if q is not None and self.max_search_len is not None and len(q) > self.max_search_len:
            raise ValueError(f"q must be at most {self.max_search_len} characters")
        if health is not None and health not in HEALTH_FILTERS:
            raise ValueError(f"health must be one of {', '.join(HEALTH_FILTERS)}")
        if tier is not None and tier not in TIER_FILTERS:
            raise ValueError(f"tier must be one of {', '.join(TIER_FILTERS)}")
        if method is not None:
            if not isinstance(method, str) or method.upper() not in METHOD_FILTERS:
                raise ValueError(f"method must be one of {', '.join(METHOD_FILTERS)}")
            method = method.upper()
        if has_input_schema is not None and not isinstance(has_input_schema, bool):
            raise ValueError("has_input_schema must be True, False or None")

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
            "maxPriceUsd": None if max_price_usd is None else _price_param(max_price_usd),
            "method": method,
            "hasInputSchema": None if has_input_schema is None else str(has_input_schema).lower(),
            "kind": kind,
            "excludeHost": exclude_host,
        }
        params.update({k: v for k, v in optional.items() if v is not None})

        url = f"{self.base_url}/discovery/resources"
        response = await self._client.get(url, params=params, **self._stack_key_kwargs())
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
        response = await self._client.post(endpoint, json=payload, **self._stack_key_kwargs())
        response.raise_for_status()
        return response.json()
