"""
`BazaarClient.list_resources`: the `q` cap and the five newer filters.

x402-rs 2.47.0 takes `q` up to 400 characters (natural-language relevance
search) and adds `maxPriceUsd`, `method`, `hasInputSchema`, `kind` and
`excludeHost`. Two things are pinned here:

* the cap is the client's, configurable, 400 by default, counted in code
  points as the facilitator counts (`q.chars().count()`, x402-rs
  `src/handlers.rs`), and refused BEFORE any request;
* a new filter goes on the wire only when the caller passes it: x402-rs 2.46.1
  answers 400 to any parameter outside `DISCOVERY_QUERY_PARAMS`, so a call
  that passes none of them must be the very request it was before.

Every request goes to an `httpx.MockTransport`; nothing leaves the process.
"""

from decimal import Decimal
from urllib.parse import parse_qsl, urlsplit

import httpx
import pytest

from uvd_x402_sdk import METHOD_FILTERS as TOP_LEVEL_METHOD_FILTERS
from uvd_x402_sdk.discovery import MAX_SEARCH_LEN, METHOD_FILTERS, BazaarClient

EMPTY_PAGE = {
    "x402Version": 2,
    "items": [],
    "pagination": {"limit": 10, "offset": 0, "total": 0},
}

# x402-rs 2.46.1, `DISCOVERY_QUERY_PARAMS` (src/handlers.rs): anything else is a 400.
FACILITATOR_2_46_1_PARAMS = {
    "limit",
    "offset",
    "category",
    "network",
    "provider",
    "tag",
    "source",
    "sourceFacilitator",
    "health",
    "tier",
    "q",
}


def _client(requests: list[httpx.Request], **kwargs) -> BazaarClient:
    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=EMPTY_PAGE)

    bazaar = BazaarClient(**kwargs)
    bazaar._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return bazaar


def _query(request: httpx.Request) -> list[tuple]:
    return parse_qsl(urlsplit(str(request.url)).query, keep_blank_values=True)


class TestSearchCap:
    def test_default_is_400(self):
        assert MAX_SEARCH_LEN == 400
        assert BazaarClient().max_search_len == 400

    @pytest.mark.asyncio
    async def test_400_characters_are_sent(self):
        sent: list[httpx.Request] = []
        async with _client(sent) as bazaar:
            await bazaar.list_resources(q="x" * 400)
        assert dict(_query(sent[0]))["q"] == "x" * 400

    @pytest.mark.asyncio
    async def test_401_characters_are_refused_before_any_request(self):
        sent: list[httpx.Request] = []
        async with _client(sent) as bazaar:
            with pytest.raises(ValueError, match="at most 400 characters"):
                await bazaar.list_resources(q="x" * 401)
        assert sent == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize("char", ["ñ", "€", "😀"])
    async def test_the_cap_counts_code_points_not_bytes(self, char):
        # 400 code points are 800-1600 UTF-8 bytes: still 400 for the
        # facilitator's chars().count(), so they must go out.
        sent: list[httpx.Request] = []
        async with _client(sent) as bazaar:
            await bazaar.list_resources(q=char * 400)
            with pytest.raises(ValueError):
                await bazaar.list_resources(q=char * 401)
        assert len(sent) == 1
        assert dict(_query(sent[0]))["q"] == char * 400

    @pytest.mark.asyncio
    async def test_the_cap_is_configurable(self):
        sent: list[httpx.Request] = []
        async with _client(sent, max_search_len=128) as bazaar:
            await bazaar.list_resources(q="x" * 128)
            with pytest.raises(ValueError, match="at most 128 characters"):
                await bazaar.list_resources(q="x" * 129)
        assert len(sent) == 1

    @pytest.mark.asyncio
    async def test_none_leaves_the_length_to_the_server(self):
        sent: list[httpx.Request] = []
        async with _client(sent, max_search_len=None) as bazaar:
            await bazaar.list_resources(q="x" * 5000)
        assert len(dict(_query(sent[0]))["q"]) == 5000

    @pytest.mark.parametrize("bad", [0, -1, True, False, "400", 1.5])
    def test_a_cap_that_is_not_a_positive_int_is_refused(self, bad):
        with pytest.raises(ValueError, match="max_search_len"):
            BazaarClient(max_search_len=bad)


class TestNewFiltersOnTheWire:
    @pytest.mark.asyncio
    async def test_no_new_filter_is_the_same_request_as_before(self):
        sent: list[httpx.Request] = []
        async with _client(sent) as bazaar:
            await bazaar.list_resources()
            await bazaar.list_resources(q="logs", health="alive", tier="vip")
        assert _query(sent[0]) == [("limit", "10"), ("offset", "0")]
        assert _query(sent[1]) == [
            ("limit", "10"),
            ("offset", "0"),
            ("health", "alive"),
            ("tier", "vip"),
            ("q", "logs"),
        ]

    @pytest.mark.asyncio
    async def test_each_filter_has_its_wire_name(self):
        sent: list[httpx.Request] = []
        async with _client(sent) as bazaar:
            await bazaar.list_resources(
                max_price_usd="0.05",
                method="POST",
                has_input_schema=True,
                kind="http",
                exclude_host="spam.example",
            )
        assert _query(sent[0]) == [
            ("limit", "10"),
            ("offset", "0"),
            ("maxPriceUsd", "0.05"),
            ("method", "POST"),
            ("hasInputSchema", "true"),
            ("kind", "http"),
            ("excludeHost", "spam.example"),
        ]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("value", "wire"),
        [
            (Decimal("1E+2"), "100"),
            (Decimal("0.10"), "0.10"),
            ("0.10", "0.10"),
            (" 0.5 ", "0.5"),
            (0.05, "0.05"),
            (1e-7, "0.0000001"),
            (2, "2"),
            (0, "0"),
            (Decimal("-0"), "0"),
        ],
    )
    async def test_max_price_is_a_plain_decimal(self, value, wire):
        sent: list[httpx.Request] = []
        async with _client(sent) as bazaar:
            await bazaar.list_resources(max_price_usd=value)
        assert dict(_query(sent[0]))["maxPriceUsd"] == wire

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "bad",
        [-1, "-0.01", "abc", "1,5", "", float("nan"), "Infinity", Decimal("NaN"), True, [1]],
    )
    async def test_a_price_that_is_not_a_finite_non_negative_amount_is_refused(self, bad):
        sent: list[httpx.Request] = []
        async with _client(sent) as bazaar:
            with pytest.raises(ValueError, match="max_price_usd"):
                await bazaar.list_resources(max_price_usd=bad)
        assert sent == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("given", "wire"), [("post", "POST"), ("Get", "GET"), ("PATCH", "PATCH")]
    )
    async def test_method_is_sent_upper_case(self, given, wire):
        sent: list[httpx.Request] = []
        async with _client(sent) as bazaar:
            await bazaar.list_resources(method=given)
        assert dict(_query(sent[0]))["method"] == wire

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "bad",
        ["OPTIONS", " GET", "GET ", "", "CONNECT", 1, "HEAD", "head", "DELETE", "Delete"],
    )
    async def test_a_method_the_facilitator_does_not_filter_on_is_refused(self, bad):
        sent: list[httpx.Request] = []
        async with _client(sent) as bazaar:
            with pytest.raises(ValueError, match="method must be one of"):
                await bazaar.list_resources(method=bad)
        assert sent == []

    def test_method_vocabulary_is_the_facilitators(self):
        # x402-rs `METHODS` (src/discovery_search.rs): a declared HEAD or DELETE
        # is read as GET, so `method=HEAD` / `method=DELETE` are a 400 there.
        assert METHOD_FILTERS == ("GET", "POST", "PUT", "PATCH")
        assert TOP_LEVEL_METHOD_FILTERS is METHOD_FILTERS

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("value", "wire"),
        [
            ("1" * 32, "1" * 32),
            ("0." + "0" * 29 + "1", "0." + "0" * 29 + "1"),
            (1e-30, "0." + "0" * 29 + "1"),
        ],
    )
    async def test_a_price_of_32_characters_is_sent(self, value, wire):
        sent: list[httpx.Request] = []
        async with _client(sent) as bazaar:
            await bazaar.list_resources(max_price_usd=value)
        assert len(wire) == 32
        assert dict(_query(sent[0]))["maxPriceUsd"] == wire

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "bad", ["1" * 33, "0." + "0" * 30 + "1", 1e-40, Decimal("1E+40"), 1e40]
    )
    async def test_a_price_longer_than_32_characters_written_out_is_refused(self, bad):
        # x402-rs `MAX_PRICE_CHARS` (src/discovery_search.rs): longer is a 400.
        sent: list[httpx.Request] = []
        async with _client(sent) as bazaar:
            with pytest.raises(ValueError, match="at most 32"):
                await bazaar.list_resources(max_price_usd=bad)
        assert sent == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("given", "wire"),
        [
            ("a.example", "a.example"),
            ("a.example,b.example", "a.example,b.example"),
            (["a.example", "b.example"], "a.example,b.example"),
            (("a.example",), "a.example"),
        ],
    )
    async def test_exclude_host_is_one_comma_separated_value(self, given, wire):
        sent: list[httpx.Request] = []
        async with _client(sent) as bazaar:
            await bazaar.list_resources(exclude_host=given)
        # One `excludeHost`, never `excludeHost=a&excludeHost=b`: x402-rs
        # splits the one value on commas (`parse_exclude_hosts`).
        assert [kv for kv in _query(sent[0]) if kv[0] == "excludeHost"] == [("excludeHost", wire)]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("bad", [[], (), ["a.example", 1], {"a.example"}, 1])
    async def test_exclude_host_that_is_not_host_names_is_refused(self, bad):
        sent: list[httpx.Request] = []
        async with _client(sent) as bazaar:
            with pytest.raises(ValueError, match="exclude_host"):
                await bazaar.list_resources(exclude_host=bad)
        assert sent == []

    @pytest.mark.asyncio
    async def test_has_input_schema_false_is_sent_not_dropped(self):
        sent: list[httpx.Request] = []
        async with _client(sent) as bazaar:
            await bazaar.list_resources(has_input_schema=False)
        assert dict(_query(sent[0]))["hasInputSchema"] == "false"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("bad", ["true", 1, 0])
    async def test_has_input_schema_takes_only_a_bool(self, bad):
        sent: list[httpx.Request] = []
        async with _client(sent) as bazaar:
            with pytest.raises(ValueError, match="has_input_schema"):
                await bazaar.list_resources(has_input_schema=bad)
        assert sent == []


class TestAgainstAFacilitatorThatDoesNotKnowThem:
    """x402-rs 2.46.1 rejects unknown parameters with a 400 instead of ignoring them."""

    @staticmethod
    def _old_facilitator(request: httpx.Request) -> httpx.Response:
        unknown = sorted({k for k, _ in _query(request)} - FACILITATOR_2_46_1_PARAMS)
        if unknown:
            body = {"error": "unknown query parameters", "unknown": unknown}
            return httpx.Response(400, json=body)
        return httpx.Response(200, json=EMPTY_PAGE)

    @pytest.mark.asyncio
    async def test_a_call_without_the_new_filters_still_works(self):
        bazaar = BazaarClient()
        bazaar._client = httpx.AsyncClient(transport=httpx.MockTransport(self._old_facilitator))
        async with bazaar:
            page = await bazaar.list_resources(q="logs", health="alive")
        assert page.items == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "kwargs",
        [
            {"max_price_usd": 1},
            {"method": "GET"},
            {"has_input_schema": False},
            {"kind": "http"},
            {"exclude_host": "a.example"},
        ],
    )
    async def test_a_new_filter_fails_loudly_instead_of_matching_everything(self, kwargs):
        bazaar = BazaarClient()
        bazaar._client = httpx.AsyncClient(transport=httpx.MockTransport(self._old_facilitator))
        async with bazaar:
            with pytest.raises(httpx.HTTPStatusError) as caught:
                await bazaar.list_resources(**kwargs)
        assert caught.value.response.status_code == 400
