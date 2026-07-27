"""
Contract tests for the Bazaar discovery models.

The fixture below is a verbatim (trimmed) page from
GET https://facilitator.ultravioletadao.xyz/discovery/resources?limit=2&health=alive
captured 2026-07-27. Regressions here mean the SDK stopped matching the live
registry, which is exactly how `firstSeen` (epoch int vs declared str) and the
missing `health` / `curation` objects went unnoticed.
"""

from datetime import datetime, timezone

import pytest

from uvd_x402_sdk.discovery import (
    HEALTH_FILTERS,
    MAX_SEARCH_LEN,
    TIER_FILTERS,
    BazaarClient,
    DiscoveryResource,
    DiscoveryResponse,
    _coerce_epoch,
)

LIVE_PAGE = {
    "x402Version": 2,
    "items": [
        {
            "url": "https://tenjin.blog/api/read/onchain-notes/stablecoin-chart",
            "type": "http",
            "x402Version": 2,
            "description": "DeFiLlama's July 27 chart row slipped to $306.23B.",
            "accepts": [
                {
                    "scheme": "exact",
                    "network": "eip155:8453",
                    "asset": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
                    "amount": "100000",
                    "payTo": "0xf4dDbE500C0caDD3e48f3ee4Bf55836dE3622938",
                    "maxTimeoutSeconds": 120,
                }
            ],
            "lastUpdated": 1785175425,
            "source": "self_registered",
            "firstSeen": 1785175425,
            "health": {
                "status": "alive",
                "lastChecked": 1785175442,
                "httpStatus": 402,
                "latencyMs": 248,
            },
            "curation": {"tier": "vip", "label": "Tenjin"},
        },
        {
            "url": "https://tenjin.blog/api/read/crypto-context/daily",
            "type": "http",
            "x402Version": 2,
            "description": "Crypto prices barely moved.",
            "accepts": [],
            "lastUpdated": 1785172479,
            "source": "self_registered",
            "firstSeen": 1785172479,
            "health": {
                "status": "alive",
                "lastChecked": 1785172502,
                "httpStatus": 402,
                "latencyMs": 324,
            },
            "curation": {"tier": "vip", "label": "Tenjin"},
        },
    ],
    "pagination": {"limit": 2, "offset": 0, "total": 1883},
}


class TestLivePageContract:
    """The exact payload the registry serves must validate as-is."""

    def test_live_page_validates(self):
        page = DiscoveryResponse.model_validate(LIVE_PAGE)
        assert len(page.items) == 2
        assert page.pagination.total == 1883

    def test_pagination_still_supports_dict_access(self):
        page = DiscoveryResponse.model_validate(LIVE_PAGE)
        assert page.pagination["total"] == 1883
        assert page.pagination.get("missing", 7) == 7

    def test_health_is_exposed(self):
        item = DiscoveryResponse.model_validate(LIVE_PAGE).items[0]
        assert item.health is not None
        assert item.health.status == "alive"
        assert item.health.http_status == 402
        assert item.health.latency_ms == 248
        assert item.health.last_checked == 1785175442
        assert item.is_alive

    def test_curation_is_exposed(self):
        item = DiscoveryResponse.model_validate(LIVE_PAGE).items[0]
        assert item.curation is not None
        assert item.curation.tier == "vip"
        assert item.curation.label == "Tenjin"
        assert item.tier == "vip"

    def test_health_and_curation_survive_model_dump(self):
        item = DiscoveryResponse.model_validate(LIVE_PAGE).items[0]
        dumped = item.model_dump(by_alias=True)
        assert dumped["health"]["status"] == "alive"
        assert dumped["curation"]["tier"] == "vip"

    def test_unmodelled_fields_are_kept(self):
        payload = dict(LIVE_PAGE["items"][0], someFutureField="keep me")
        item = DiscoveryResource.model_validate(payload)
        assert item.model_dump()["someFutureField"] == "keep me"


class TestTimestampCoercion:
    """firstSeen arrives as an epoch int; older exports use strings."""

    def test_epoch_int(self):
        item = DiscoveryResource.model_validate(
            {"url": "https://x", "type": "http", "firstSeen": 1780346591}
        )
        assert item.first_seen == 1780346591

    def test_numeric_string(self):
        item = DiscoveryResource.model_validate(
            {"url": "https://x", "type": "http", "firstSeen": "1780346591"}
        )
        assert item.first_seen == 1780346591

    def test_iso_string(self):
        item = DiscoveryResource.model_validate(
            {"url": "https://x", "type": "http", "firstSeen": "2026-06-02T09:23:11Z"}
        )
        assert item.first_seen == 1780392191

    def test_datetime_object(self):
        moment = datetime(2026, 6, 2, 9, 23, 11, tzinfo=timezone.utc)
        item = DiscoveryResource.model_validate(
            {"url": "https://x", "type": "http", "firstSeen": moment}
        )
        assert item.first_seen == int(moment.timestamp())

    def test_missing_timestamp_stays_none(self):
        item = DiscoveryResource.model_validate({"url": "https://x", "type": "http"})
        assert item.first_seen is None
        assert item.first_seen_at is None

    def test_datetime_helper(self):
        item = DiscoveryResource.model_validate(
            {"url": "https://x", "type": "http", "firstSeen": 1780346591}
        )
        assert item.first_seen_at == datetime.fromtimestamp(1780346591, tz=timezone.utc)

    def test_naive_datetime_is_treated_as_utc(self):
        assert _coerce_epoch(datetime(1970, 1, 1, 0, 0, 1)) == 1

    def test_garbage_is_rejected(self):
        with pytest.raises(ValueError):
            _coerce_epoch("not a timestamp")

    def test_bool_is_rejected(self):
        with pytest.raises(ValueError):
            _coerce_epoch(True)


class TestListResourcesValidation:
    """Client-side guards that mirror the facilitator's own rejections."""

    @pytest.mark.asyncio
    async def test_long_q_is_rejected(self):
        async with BazaarClient() as bazaar:
            with pytest.raises(ValueError, match="at most"):
                await bazaar.list_resources(q="x" * (MAX_SEARCH_LEN + 1))

    @pytest.mark.asyncio
    async def test_unknown_health_filter_is_rejected(self):
        async with BazaarClient() as bazaar:
            with pytest.raises(ValueError, match="health must be"):
                await bazaar.list_resources(health="online")

    @pytest.mark.asyncio
    async def test_unknown_tier_filter_is_rejected(self):
        async with BazaarClient() as bazaar:
            with pytest.raises(ValueError, match="tier must be"):
                await bazaar.list_resources(tier="gold")

    def test_filter_vocabularies_match_the_server(self):
        assert "alive" in HEALTH_FILTERS
        assert "quarantined" in HEALTH_FILTERS
        assert TIER_FILTERS == ("first_party", "vip", "verified", "listed")
