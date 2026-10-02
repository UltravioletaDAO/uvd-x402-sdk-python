"""
`bazaar_extension`: the spec shape (`info` + `schema`) and the legacy pins.

Spec: coinbase/x402 `specs/extensions/bazaar.md` at dd927a2 (2026-04-21),
`response.BAZAAR_SPEC`. `info.input` says how to call the endpoint (`type`,
`method`, and for POST / PUT / PATCH `bodyType` + `body`), `info.output` what
it answers, and `schema` is the JSON Schema (draft 2020-12) the facilitator
validates `info` against before cataloging.

Before this, the helper could not say POST with a body (POST + input_schema
raised) and never emitted `info.input.method`, so no seller of the house could
tell the facilitator its endpoint is POST.

Every call that built a dict before builds the same bytes now: the first class
pins those, key order included (json.dumps without sort_keys).
"""

import json

import pytest
from jsonschema import Draft202012Validator

from uvd_x402_sdk.config import X402Config
from uvd_x402_sdk.response import BAZAAR_SPEC, bazaar_extension, create_402_response_v2


def _wire(obj) -> str:
    return json.dumps(obj, separators=(",", ":"))


def _assert_info_fits_its_schema(bloque):
    """What the facilitator does before cataloging (spec, "Facilitator Behavior" 1)."""
    Draft202012Validator.check_schema(bloque["schema"])
    errors = list(Draft202012Validator(bloque["schema"]).iter_errors(bloque["info"]))
    assert errors == [], [e.message for e in errors]


# ---------------------------------------------------------------------------
# Legacy: the bytes of every call that worked before.
# ---------------------------------------------------------------------------


class TestLegacyBytes:
    def test_body_shape(self):
        out = bazaar_extension(
            {"type": "object", "properties": {"x": {"type": "integer"}}}, {"ok": True}
        )
        assert _wire(out) == (
            '{"bazaar":{"schema":{"properties":{"input":{"properties":{"body":'
            '{"type":"object","properties":{"x":{"type":"integer"}}}}},'
            '"output":{"properties":{"example":{"ok":true}}}}}}}'
        )

    def test_body_shape_discoverable(self):
        out = bazaar_extension({"type": "object"}, {"ok": 1}, discoverable=False)
        assert _wire(out) == (
            '{"bazaar":{"schema":{"properties":{"input":{"properties":{"body":{"type":"object"}}},'
            '"output":{"properties":{"example":{"ok":1}}}}},"discoverable":false}}'
        )

    def test_get_shape(self):
        out = bazaar_extension(
            output_example={"score": 81.86},
            method="GET",
            query_params={"wallet": {"type": "string"}},
            discoverable=True,
        )
        assert _wire(out) == (
            '{"bazaar":{"schema":{"properties":{"input":{"type":"http","method":"GET",'
            '"queryParams":{"wallet":{"type":"string"}}},'
            '"output":{"properties":{"example":{"score":81.86}}}}},"discoverable":true}}'
        )

    @pytest.mark.parametrize("method", ["POST", "post", "get", "OPTIONS"])
    def test_method_without_a_body_keeps_the_http_shape_verbatim(self, method):
        # A POST without input_schema was buildable before (the method went
        # out as given): it stays so. The spec shape is one info=True away.
        out = bazaar_extension(output_example={"ok": 1}, method=method)
        assert _wire(out) == (
            '{"bazaar":{"schema":{"properties":{"input":{"type":"http","method":"' + method + '"},'
            '"output":{"properties":{"example":{"ok":1}}}}}}}'
        )

    @pytest.mark.parametrize("method", ["GET", "get", "HEAD", "DELETE", "OPTIONS"])
    def test_a_body_on_a_method_without_one_still_raises(self, method):
        with pytest.raises(ValueError, match="carries none"):
            bazaar_extension({"type": "object"}, {"ok": 1}, method=method)

    def test_incomplete_calls_still_raise(self):
        with pytest.raises(ValueError, match="neither shape"):
            bazaar_extension()
        with pytest.raises(ValueError, match="output_example is required"):
            bazaar_extension(method="GET")
        with pytest.raises(ValueError, match="output_example is required"):
            bazaar_extension({"type": "object"})


# ---------------------------------------------------------------------------
# Spec shape.
# ---------------------------------------------------------------------------


class TestSpecExamples:
    def test_the_spec_post_example(self):
        """Spec, "Example: POST Endpoint": info byte for byte; schema the
        spec's minus the optional headers / queryParams it does not use."""
        out = bazaar_extension(
            {"type": "object"},
            {"results": []},
            method="POST",
            body={"query": "example"},
        )
        assert out == {
            "bazaar": {
                "info": {
                    "input": {
                        "type": "http",
                        "method": "POST",
                        "bodyType": "json",
                        "body": {"query": "example"},
                    },
                    "output": {"type": "json", "example": {"results": []}},
                },
                "schema": {
                    "$schema": "https://json-schema.org/draft/2020-12/schema",
                    "type": "object",
                    "properties": {
                        "input": {
                            "type": "object",
                            "properties": {
                                "type": {"type": "string", "const": "http"},
                                "method": {"type": "string", "enum": ["POST", "PUT", "PATCH"]},
                                "bodyType": {
                                    "type": "string",
                                    "enum": ["json", "form-data", "text"],
                                },
                                "body": {"type": "object"},
                            },
                            "required": ["type", "method", "bodyType", "body"],
                            "additionalProperties": False,
                        },
                        "output": {
                            "type": "object",
                            "properties": {
                                "type": {"type": "string"},
                                "example": {"type": "object"},
                            },
                            "required": ["type"],
                        },
                    },
                    "required": ["input"],
                },
            }
        }
        _assert_info_fits_its_schema(out["bazaar"])

    def test_the_spec_get_example(self):
        """Spec, "Example: GET Endpoint": the info, byte for byte."""
        out = bazaar_extension(
            output_example={"city": "San Francisco", "weather": "foggy", "temperature": 60},
            method="GET",
            query_params={"city": "San Francisco"},
            info=True,
        )
        assert _wire(out["bazaar"]["info"]) == (
            '{"input":{"type":"http","method":"GET","queryParams":{"city":"San Francisco"}},'
            '"output":{"type":"json","example":'
            '{"city":"San Francisco","weather":"foggy","temperature":60}}}'
        )
        entrada = out["bazaar"]["schema"]["properties"]["input"]
        assert entrada["properties"]["method"] == {
            "type": "string",
            "enum": ["GET", "HEAD", "DELETE"],
        }
        assert entrada["required"] == ["type", "method"]
        assert entrada["additionalProperties"] is False
        _assert_info_fits_its_schema(out["bazaar"])

    def test_the_spec_is_cited_by_commit(self):
        assert BAZAAR_SPEC.startswith("https://github.com/coinbase/x402/blob/dd927a26")
        assert BAZAAR_SPEC.endswith("/specs/extensions/bazaar.md")


class TestSpecShape:
    BODY = {"type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"]}

    @pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "post", "Put"])
    def test_a_body_method_with_input_schema_is_now_buildable(self, method):
        out = bazaar_extension(self.BODY, {"ok": True}, method=method, body={"q": "lima"})
        entrada = out["bazaar"]["info"]["input"]
        assert entrada["method"] == method.upper()
        assert entrada["body"] == {"q": "lima"}
        # Same path the body shape always used: legacy readers keep finding it.
        assert out["bazaar"]["schema"]["properties"]["input"]["properties"]["body"] is self.BODY
        _assert_info_fits_its_schema(out["bazaar"])

    @pytest.mark.parametrize("method", ["post", "Put", "PATCH"])
    def test_the_body_method_alone_selects_the_spec_shape_in_any_case(self, method):
        # No body / body_type / info: only "a body method with input_schema"
        # can pick the spec shape here, and the legacy shape would raise.
        out = bazaar_extension({"type": "object"}, {"ok": 1}, method=method)
        assert out["bazaar"]["info"]["input"] == {
            "type": "http",
            "method": method.upper(),
            "bodyType": "json",
            "body": {},
        }
        _assert_info_fits_its_schema(out["bazaar"])

    @pytest.mark.parametrize(
        "body_type,body", [("json", {"a": 1}), ("form-data", {"f": "v"}), ("text", "hola")]
    )
    def test_body_type(self, body_type, body):
        out = bazaar_extension(
            output_example={"ok": 1}, method="POST", body=body, body_type=body_type
        )
        assert out["bazaar"]["info"]["input"]["bodyType"] == body_type
        assert out["bazaar"]["info"]["input"]["body"] == body
        _assert_info_fits_its_schema(out["bazaar"])

    @pytest.mark.parametrize("trigger", [{"body": {"a": 1}}, {"body_type": "json"}, {"info": True}])
    def test_each_trigger_selects_the_spec_shape_on_its_own(self, trigger):
        out = bazaar_extension(output_example={"ok": 1}, method="POST", **trigger)
        assert set(out["bazaar"]) == {"info", "schema"}
        assert out["bazaar"]["info"]["input"]["method"] == "POST"

    def test_info_false_does_not_turn_it_off(self):
        out = bazaar_extension(self.BODY, {"ok": 1}, method="POST", body={"q": "x"}, info=False)
        assert "info" in out["bazaar"]

    def test_info_and_schema_are_separate_and_info_carries_input_and_output(self):
        out = bazaar_extension(output_example=[1, 2], method="DELETE", info=True)
        assert set(out["bazaar"]) == {"info", "schema"}
        assert out["bazaar"]["info"] == {
            "input": {"type": "http", "method": "DELETE"},
            "output": {"type": "json", "example": [1, 2]},
        }
        # A non-object example is "any" in the spec: {} so the info still fits.
        assert out["bazaar"]["schema"]["properties"]["output"]["properties"]["example"] == {}
        _assert_info_fits_its_schema(out["bazaar"])

    def test_output_is_optional_in_the_spec_shape(self):
        out = bazaar_extension(method="HEAD", info=True)
        assert out["bazaar"]["info"] == {"input": {"type": "http", "method": "HEAD"}}
        assert "output" not in out["bazaar"]["schema"]["properties"]
        _assert_info_fits_its_schema(out["bazaar"])

    def test_query_params_on_a_body_method(self):
        out = bazaar_extension(
            output_example={"ok": 1}, method="PUT", body={"a": 1}, query_params={"dry": "true"}
        )
        assert out["bazaar"]["info"]["input"]["queryParams"] == {"dry": "true"}
        _assert_info_fits_its_schema(out["bazaar"])

    def test_discoverable_sits_beside_info_and_schema(self):
        out = bazaar_extension(method="GET", info=True, discoverable=True)
        assert out["bazaar"]["discoverable"] is True
        assert set(out["bazaar"]) == {"info", "schema", "discoverable"}

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"method": "POST", "info": True},
            {"method": "PATCH", "body_type": "text", "body": "x", "output_example": "ok"},
            {"method": "POST", "input_schema": {"type": "object", "properties": {"a": {}}}},
            {"method": "GET", "query_params": {"a": "1"}, "output_example": {"a": 1}, "info": True},
            {"method": "DELETE", "query_params": {}, "info": True},
        ],
    )
    def test_every_info_fits_its_own_schema(self, kwargs):
        _assert_info_fits_its_schema(bazaar_extension(**kwargs)["bazaar"])

    def test_the_default_body_is_the_empty_object_when_it_fits(self):
        out = bazaar_extension(
            {"type": "object", "properties": {"a": {}}}, {"ok": 1}, method="POST"
        )
        assert out["bazaar"]["info"]["input"]["body"] == {}
        out = bazaar_extension(output_example={"ok": 1}, method="POST", info=True)
        assert out["bazaar"]["info"]["input"]["body"] == {}
        assert out["bazaar"]["schema"]["properties"]["input"]["properties"]["body"] == {
            "properties": {}
        }

    @pytest.mark.parametrize(
        "schema",
        [
            {"type": "object", "required": ["q"]},
            {"required": ["q"]},
            {"type": "array"},
            {"type": "string"},
        ],
    )
    def test_a_default_body_that_would_fail_its_schema_is_refused(self, schema):
        with pytest.raises(ValueError, match="pass body="):
            bazaar_extension(schema, {"ok": 1}, method="POST")


class TestSpecShapeRefusals:
    @pytest.mark.parametrize("method", ["OPTIONS", " POST", "POST ", "", "CONNECT", 1])
    def test_a_method_outside_the_six_is_refused(self, method):
        with pytest.raises(ValueError, match="must be one of"):
            bazaar_extension(output_example={"ok": 1}, method=method, info=True)

    def test_the_spec_shape_needs_a_method(self):
        with pytest.raises(ValueError, match="got None"):
            bazaar_extension({"type": "object"}, {"ok": 1}, info=True)
        with pytest.raises(ValueError, match="got None"):
            bazaar_extension(output_example={"ok": 1}, body={"a": 1})

    @pytest.mark.parametrize("method", ["GET", "head", "DELETE"])
    @pytest.mark.parametrize(
        "kwargs",
        [
            {"body": {"a": 1}},
            {"body_type": "json"},
            {"input_schema": {"type": "object"}, "info": True},
        ],
    )
    def test_a_body_on_a_query_method_is_refused(self, method, kwargs):
        with pytest.raises(ValueError, match="carries no request body"):
            bazaar_extension(output_example={"ok": 1}, method=method, **kwargs)

    @pytest.mark.parametrize("body_type", ["JSON", "form", "xml", ""])
    def test_body_type_is_the_spec_enum_exactly(self, body_type):
        with pytest.raises(ValueError, match="body_type must be one of"):
            bazaar_extension(
                output_example={"ok": 1}, method="POST", body={"a": 1}, body_type=body_type
            )

    def test_non_object_query_params_and_input_schema_are_refused(self):
        with pytest.raises(ValueError, match="query_params"):
            bazaar_extension(method="GET", query_params=["a"], info=True)
        with pytest.raises(ValueError, match="input_schema"):
            bazaar_extension(["not", "a", "schema"], {"ok": 1}, method="POST")


def test_the_extension_travels_inside_the_v2_challenge():
    config = X402Config(recipient_evm="0x" + "11" * 20, supported_networks=["base"])
    body = create_402_response_v2(
        "0.01",
        config,
        resource={
            "url": "https://api.example.com/search",
            "description": "Search",
            "mimeType": "application/json",
        },
        extensions=bazaar_extension(
            {"type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"]},
            {"results": []},
            method="POST",
            body={"q": "example"},
        ),
    )
    entrada = body["extensions"]["bazaar"]["info"]["input"]
    assert (entrada["method"], entrada["bodyType"], entrada["body"]) == (
        "POST",
        "json",
        {"q": "example"},
    )
    _assert_info_fits_its_schema(body["extensions"]["bazaar"])
