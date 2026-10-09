"""
`BazaarClient.register_resource(..., extensions=...)`: a seller declares what to
send and what comes back AT REGISTRATION, in the body x402-rs reads.

x402-rs keeps `extensions` from `POST /discovery/register` verbatim
(`RegisterResourceRequest.extensions`, `src/types_v2.rs`) and reads two things
out of `extensions.bazaar`: the listing's `hasInputSchema`
(`DiscoveryResource::has_input_schema`, `src/types_v2.rs`) and the request its
health prober sends (`declared_request`, `src/discovery_health.rs`). Until
0.95.0 the SDK had no way to send the block, so a listing registered with it
always came out `hasInputSchema: false` (the Tenjin handoff, section 2.2).

`tests/fixtures/bazaar-register-x402rs.json` is the body of x402-rs's own test of
that route (`the_seller_declares_its_schema_at_registration`, 2.49.0), which
asserts the 201, the verbatim copy, `hasInputSchema` and the probe. The two
readers are ported below from the same commit, and calibrated against that test
and against x402-rs's unit cases before they are used on what the SDK builds.

Every request goes to an `httpx.MockTransport`; nothing leaves the process.
"""

import inspect
import json
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

import httpx
import pytest
from jsonschema import Draft202012Validator

from uvd_x402_sdk.discovery import BazaarClient
from uvd_x402_sdk.response import bazaar_extension

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "bazaar-register-x402rs.json").read_text(encoding="utf-8")
)
BODY = FIXTURE["body"]
EXPECT = FIXTURE["expect"]
DECLARED = BODY["extensions"]["bazaar"]
BODY_SCHEMA = DECLARED["schema"]["properties"]["input"]["properties"]["body"]
BODY_EXAMPLE = DECLARED["info"]["input"]["body"]
OUTPUT_EXAMPLE = DECLARED["info"]["output"]["example"]


# ---------------------------------------------------------------------------
# x402-rs 2.49.0 (6b0fefea), the two readers, ported read-only.
# ---------------------------------------------------------------------------


def _get(value, key):
    """serde_json's `Value::get`: None unless `value` is an object holding `key`."""
    return value.get(key) if isinstance(value, dict) else None


def _ascii_upper(text):
    """Rust's `to_ascii_uppercase`; `str.upper()` would also fold non-ASCII."""
    return "".join(c.upper() if c.isascii() else c for c in text)


def _compact(value):
    """`serde_json::to_string`. Every body compared here has one key, so the
    map order serde_json uses does not enter."""
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


def x402rs_has_input_schema(extensions):
    """`DiscoveryResource::has_input_schema` (`src/types_v2.rs`)."""
    bazaar = _get(extensions, "bazaar")

    def declared(value):
        return isinstance(value, dict) and len(value) > 0

    return declared(_get(_get(bazaar, "info"), "input")) or declared(
        _get(_get(_get(bazaar, "schema"), "properties"), "input")
    )


_PROBE_METHODS = {
    "GET": "GET",
    "HEAD": "GET",
    "DELETE": "GET",
    "POST": "POST",
    "PUT": "PUT",
    "PATCH": "PATCH",
}


def _probe_method(raw):
    """`ProbeMethod::parse`: HEAD and DELETE probe as GET, anything else is None."""
    return _PROBE_METHODS.get(_ascii_upper(raw.strip()))


def _schema_method(schema_input):
    """`schema_method`: the SDK's plain `method`, else `properties.method`'s
    `const` or first `enum` value."""
    plain = _get(schema_input, "method")
    if isinstance(plain, str):
        return _probe_method(plain)
    method = _get(_get(schema_input, "properties"), "method")
    const = _get(method, "const")
    if isinstance(const, str):
        return _probe_method(const)
    enum = _get(method, "enum")
    if isinstance(enum, list) and enum and isinstance(enum[0], str):
        return _probe_method(enum[0])
    return None


def _declares_body(info_input, schema_input):
    """`declares_body`: a `body` or `bodyType` in either half."""

    def present(container, key):
        return _get(container, key) is not None

    properties = _get(schema_input, "properties")
    return (
        present(info_input, "body")
        or present(info_input, "bodyType")
        or present(properties, "body")
        or present(properties, "bodyType")
    )


_MAX_PROBE_BODY_BYTES = 8 * 1024


def _example_body(info_input):
    """`example_body`: the JSON example, serialized, within bounds, never `{}`."""
    body_type = _get(info_input, "bodyType")
    if body_type is not None and not (
        isinstance(body_type, str) and _ascii_upper(body_type.strip()) == "JSON"
    ):
        return None
    body = _get(info_input, "body")
    if body is None:
        return None
    serialized = _compact(body)
    if len(serialized.encode("utf-8")) > _MAX_PROBE_BODY_BYTES or serialized == "{}":
        return None
    return serialized


def x402rs_declared_request(extensions):
    """`declared_request` (`src/discovery_health.rs`): None for `Undeclared`,
    else `(method, example)`; a GET never carries an example."""
    bazaar = _get(extensions, "bazaar")
    info_input = _get(_get(bazaar, "info"), "input")
    schema_input = _get(_get(_get(bazaar, "schema"), "properties"), "input")
    method = None
    named = _get(info_input, "method")
    if isinstance(named, str):
        method = _probe_method(named)
    if method is None and schema_input is not None:
        method = _schema_method(schema_input)
    if method is None and _declares_body(info_input, schema_input):
        method = "POST"
    if method is None:
        return None
    if method == "GET":
        return ("GET", None)
    return (method, _example_body(info_input))


# ---------------------------------------------------------------------------
# A double of x402-rs's register + list routes, reduced to what is checked here.
# ---------------------------------------------------------------------------


class _Catalog:
    """`POST /discovery/register` keeps the body (`extensions` verbatim);
    `GET /discovery/resources` serves each listing with the `hasInputSchema`
    x402-rs derives, and honours the `hasInputSchema` filter. x402-rs serves a
    listing only once a probe verified it; the double serves it at once."""

    # RegisterResourceRequest fields without a serde default: missing = 422.
    REQUIRED = ("url", "type", "description", "accepts")

    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.held: dict[str, dict] = {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.method == "POST" and request.url.path == "/discovery/register":
            body = json.loads(request.content)
            missing = [k for k in self.REQUIRED if k not in body]
            if missing:
                return httpx.Response(422, text=f"missing field `{missing[0]}`")
            if not body["accepts"]:
                return httpx.Response(400, json={"error": "No payment methods specified"})
            self.held[body["url"]] = body
            return httpx.Response(
                201,
                json={
                    "success": True,
                    "message": "Resource registered successfully",
                    "url": body["url"],
                },
            )
        if request.method == "GET" and request.url.path == "/discovery/resources":
            query = dict(parse_qsl(urlsplit(str(request.url)).query))
            items = []
            for body in self.held.values():
                listing = dict(body)
                listing["hasInputSchema"] = x402rs_has_input_schema(body.get("extensions"))
                wanted = query.get("hasInputSchema")
                if wanted is not None and (wanted == "true") != listing["hasInputSchema"]:
                    continue
                items.append(listing)
            page = {"limit": 10, "offset": 0, "total": len(items)}
            return httpx.Response(200, json={"x402Version": 2, "items": items, "pagination": page})
        return httpx.Response(404)


def _bazaar(catalog: _Catalog) -> BazaarClient:
    bazaar = BazaarClient()
    bazaar._client = httpx.AsyncClient(transport=httpx.MockTransport(catalog))
    return bazaar


def _sent_body(request: httpx.Request) -> dict:
    assert request.method == "POST"
    assert request.url.path == "/discovery/register"
    return json.loads(request.content)


# ---------------------------------------------------------------------------
# The port reads what x402-rs reads.
# ---------------------------------------------------------------------------


class TestThePortedReaders:
    def test_they_read_the_fixture_as_x402rs_asserts(self):
        extensions = BODY["extensions"]
        assert x402rs_has_input_schema(extensions) is EXPECT["hasInputSchema"] is True
        assert x402rs_declared_request(extensions) == (
            EXPECT["probeMethod"],
            _compact(EXPECT["probeExampleBody"]),
        )

    @pytest.mark.parametrize("extensions", [None, {}, {"other": {}}, {"bazaar": {}}])
    def test_nothing_declared_is_nothing_read(self, extensions):
        assert x402rs_has_input_schema(extensions) is False
        assert x402rs_declared_request(extensions) is None

    # x402-rs `declared_request_reads_every_declaration_in_order`, its literal cases.
    @pytest.mark.parametrize(
        ("raw", "want"),
        [
            (" post ", ("POST", None)),
            ("put", ("PUT", None)),
            ("PATCH", ("PATCH", None)),
            ("get", ("GET", None)),
            ("HEAD", ("GET", None)),
            ("DELETE", ("GET", None)),
            ("FETCH", None),
            ("", None),
            (42, None),
            (None, None),
        ],
    )
    def test_info_input_method(self, raw, want):
        extensions = {"bazaar": {"info": {"input": {"method": raw}}}}
        assert x402rs_declared_request(extensions) == want

    @pytest.mark.parametrize(
        ("schema_input", "want"),
        [
            ({"type": "http", "method": "GET"}, ("GET", None)),
            ({"properties": {"method": {"const": "PUT"}}}, ("PUT", None)),
            ({"properties": {"method": {"enum": ["POST", "GET"]}}}, ("POST", None)),
            ({"properties": {"body": {"type": "object"}}}, ("POST", None)),
        ],
    )
    def test_the_json_schema_half(self, schema_input, want):
        extensions = {"bazaar": {"schema": {"properties": {"input": schema_input}}}}
        assert x402rs_declared_request(extensions) == want

    def test_a_body_without_a_method_is_a_post(self):
        bare_type = {"bazaar": {"info": {"input": {"bodyType": "json"}}}}
        bare_body = {"bazaar": {"info": {"input": {"body": {"q": 1}}}}}
        assert x402rs_declared_request(bare_type) == ("POST", None)
        assert x402rs_declared_request(bare_body) == ("POST", '{"q":1}')

    def test_info_input_method_wins_over_the_schema(self):
        extensions = {
            "bazaar": {
                "info": {"input": {"method": "GET"}},
                "schema": {"properties": {"input": {"properties": {"body": {}}}}},
            }
        }
        assert x402rs_declared_request(extensions) == ("GET", None)


# ---------------------------------------------------------------------------
# register_resource sends the block, in the body x402-rs accepts.
# ---------------------------------------------------------------------------


class TestRegisterSendsExtensions:
    @pytest.mark.asyncio
    async def test_the_body_is_the_one_x402rs_accepts(self):
        catalog = _Catalog()
        async with _bazaar(catalog) as bazaar:
            result = await bazaar.register_resource(
                url=BODY["url"],
                resource_type=BODY["type"],
                description=BODY["description"],
                accepts=BODY["accepts"],
                extensions=BODY["extensions"],
            )
        sent = _sent_body(catalog.requests[0])
        assert sent == BODY
        assert list(sent) == list(BODY)
        assert result == {
            "success": True,
            "message": "Resource registered successfully",
            "url": BODY["url"],
        }

    @pytest.mark.asyncio
    async def test_what_bazaar_extension_builds_goes_out_verbatim(self):
        extensions = bazaar_extension(BODY_SCHEMA, OUTPUT_EXAMPLE, method="POST", body=BODY_EXAMPLE)
        catalog = _Catalog()
        async with _bazaar(catalog) as bazaar:
            await bazaar.register_resource(
                url=BODY["url"],
                description=BODY["description"],
                accepts=BODY["accepts"],
                extensions=extensions,
            )
        sent = _sent_body(catalog.requests[0])["extensions"]
        assert sent == extensions
        # The halves x402-rs reads are the ones its own test registers.
        assert sent["bazaar"]["info"] == DECLARED["info"]
        assert sent["bazaar"]["schema"]["properties"]["input"]["properties"]["body"] == BODY_SCHEMA
        assert x402rs_has_input_schema(sent) is True
        assert x402rs_declared_request(sent) == ("POST", _compact(BODY_EXAMPLE))
        # `info` fits the schema the builder emits and the one x402-rs's test carries.
        for schema in (sent["bazaar"]["schema"], DECLARED["schema"]):
            errors = list(Draft202012Validator(schema).iter_errors(sent["bazaar"]["info"]))
            assert errors == [], [e.message for e in errors]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("build", "probe"),
        [
            pytest.param(
                lambda: bazaar_extension(
                    BODY_SCHEMA, OUTPUT_EXAMPLE, method="POST", body=BODY_EXAMPLE
                ),
                ("POST", _compact(BODY_EXAMPLE)),
                id="spec-post",
            ),
            pytest.param(
                lambda: bazaar_extension(
                    BODY_SCHEMA, OUTPUT_EXAMPLE, method="put", body=BODY_EXAMPLE
                ),
                ("PUT", _compact(BODY_EXAMPLE)),
                id="spec-put",
            ),
            pytest.param(
                lambda: bazaar_extension(method="GET", query_params={"symbol": "AAPL"}, info=True),
                ("GET", None),
                id="spec-get",
            ),
            pytest.param(
                lambda: bazaar_extension(BODY_SCHEMA, OUTPUT_EXAMPLE),
                ("POST", None),
                id="historical-body",
            ),
            pytest.param(
                lambda: bazaar_extension(
                    output_example=OUTPUT_EXAMPLE, method="GET", query_params={"symbol": "AAPL"}
                ),
                ("GET", None),
                id="historical-http",
            ),
        ],
    )
    async def test_a_listing_registered_with_any_shape_comes_back_with_its_input(
        self, build, probe
    ):
        """The handoff's acceptance: an alta made with the SDK lists as
        `hasInputSchema: true`, and the prober asks the way it was declared."""
        catalog = _Catalog()
        async with _bazaar(catalog) as bazaar:
            await bazaar.register_resource(
                url=BODY["url"],
                description=BODY["description"],
                accepts=BODY["accepts"],
                extensions=build(),
            )
            page = await bazaar.list_resources(has_input_schema=True)
        assert [item.url for item in page.items] == [BODY["url"]]
        assert page.items[0].hasInputSchema is True
        assert x402rs_declared_request(catalog.held[BODY["url"]]["extensions"]) == probe

    @pytest.mark.asyncio
    async def test_without_extensions_the_listing_has_no_input(self):
        catalog = _Catalog()
        async with _bazaar(catalog) as bazaar:
            await bazaar.register_resource(
                url=BODY["url"], description=BODY["description"], accepts=BODY["accepts"]
            )
            declared = await bazaar.list_resources(has_input_schema=True)
            undeclared = await bazaar.list_resources(has_input_schema=False)
        assert declared.items == []
        assert [item.url for item in undeclared.items] == [BODY["url"]]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("bad", [[{"bazaar": {}}], "bazaar", 1, True])
    async def test_extensions_must_be_an_object(self, bad):
        catalog = _Catalog()
        async with _bazaar(catalog) as bazaar:
            with pytest.raises(ValueError, match="extensions"):
                await bazaar.register_resource(
                    url=BODY["url"], accepts=BODY["accepts"], extensions=bad
                )
        assert catalog.requests == []


# ---------------------------------------------------------------------------
# Without extensions, the request of 0.94.0.
# ---------------------------------------------------------------------------


class TestWithoutExtensionsTheBodyIsUnchanged:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("kwargs", [{}, {"extensions": None}, {"extensions": {}}])
    async def test_the_minimal_body(self, kwargs):
        catalog = _Catalog()
        async with _bazaar(catalog) as bazaar:
            with pytest.raises(httpx.HTTPStatusError):
                # x402-rs needs `accepts`; the SDK has never filled it in.
                await bazaar.register_resource(
                    url="https://api.example.com/x", description="d", **kwargs
                )
        sent = _sent_body(catalog.requests[0])
        assert sent == {"url": "https://api.example.com/x", "type": "http", "description": "d"}
        assert list(sent) == ["url", "type", "description"]

    @pytest.mark.asyncio
    async def test_the_positional_call_of_0_94_0(self):
        metadata = {"category": "finance", "tags": ["market-data"]}
        catalog = _Catalog()
        async with _bazaar(catalog) as bazaar:
            await bazaar.register_resource(
                BODY["url"], "http", BODY["description"], BODY["accepts"], metadata
            )
        sent = _sent_body(catalog.requests[0])
        assert list(sent) == ["url", "type", "description", "accepts", "metadata"]
        assert sent["metadata"] == metadata

    def test_extensions_is_keyword_only_and_last(self):
        params = inspect.signature(BazaarClient.register_resource).parameters
        assert list(params) == [
            "self",
            "url",
            "resource_type",
            "description",
            "accepts",
            "metadata",
            "extensions",
        ]
        assert params["extensions"].kind is inspect.Parameter.KEYWORD_ONLY
        assert params["extensions"].default is None
