"""A 402 written by a third party is bounded before it is parsed.

``json.loads("[" * 100_000)`` is not a ValueError once ``py_ecc`` has raised
the recursion limit to 100000 (importing this SDK does): the C stack runs out
first and the interpreter segfaults. Measured on the base before this file
existed: ``payment_challenge_from`` with that payload killed the process with
exit 139, base64 header, raw header and body alike.

The cases come from the reader that carried this guard downstream (the
header, BOM / UTF-16 / UTF-32 and misleading-charset matrix, the depth
limit, literal brackets, invalid bytes, a base64 decoder that returns junk),
plus fuzzing against ``json.loads`` itself.
"""

from __future__ import annotations

import base64
import json
import random
import subprocess
import sys
import textwrap

import httpx
import pytest

import uvd_x402_sdk
from uvd_x402_sdk import (
    MAX_CHALLENGE_BYTES,
    MAX_JSON_DEPTH,
    JSONBoundsError,
    X402Client,
    json_depth_within,
    loads_bounded,
    payment_challenge_from,
)
from uvd_x402_sdk.exceptions import InvalidPayloadError

OFFER = {
    "scheme": "exact",
    "network": "eip155:8453",
    "amount": "10000",
    "asset": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
    "payTo": "0x1111111111111111111111111111111111111111",
    "maxTimeoutSeconds": 300,
}
ENCODINGS = [
    "utf-8", "utf-8-sig", "utf-16", "utf-16-le", "utf-16-be",
    "utf-32", "utf-32-le", "utf-32-be",
]


def challenge(**extra):
    return {"x402Version": 2, "accepts": [{**OFFER, **extra}]}


def depth_of(value) -> int:
    """True nesting of a parsed JSON value, walked without recursion."""
    deepest, stack = 0, [(value, 1)]
    while stack:
        node, level = stack.pop()
        if isinstance(node, dict):
            children = list(node.values())
        elif isinstance(node, list):
            children = node
        else:
            continue
        deepest = max(deepest, level)
        stack.extend((child, level + 1) for child in children)
    return deepest


def nested(levels: int):
    value: object = []
    for _ in range(levels - 1):
        value = [value]
    return value


@pytest.fixture
def parsed_texts(monkeypatch):
    """Every text json.loads is handed, decoded the way json.loads decodes it."""
    seen = []
    original = json.loads

    def spy(raw, *a, **kw):
        if isinstance(raw, (bytes, bytearray)):
            seen.append(bytes(raw).decode(json.detect_encoding(raw), "surrogatepass"))
        else:
            seen.append(raw)
        return original(raw, *a, **kw)

    monkeypatch.setattr(json, "loads", spy)
    return seen


# =============================================================================
# loads_bounded / json_depth_within
# =============================================================================


def test_the_public_surface_is_small_and_exported():
    for name in (
        "MAX_CHALLENGE_BYTES", "MAX_JSON_DEPTH", "JSONBoundsError",
        "json_depth_within", "loads_bounded",
    ):
        assert name in uvd_x402_sdk.__all__
    assert MAX_CHALLENGE_BYTES == 512 * 1024
    assert MAX_JSON_DEPTH == 64
    assert issubclass(JSONBoundsError, ValueError)


def test_depth_at_the_limit_parses_and_one_more_is_refused(parsed_texts):
    assert loads_bounded(json.dumps(nested(3)), max_depth=3) == nested(3)
    with pytest.raises(JSONBoundsError) as err:
        loads_bounded(json.dumps(nested(4)), max_depth=3)
    assert err.value.reason == "too-deep"
    assert parsed_texts == [json.dumps(nested(3))]


def test_objects_count_like_arrays():
    doc = {"a": {"b": {"c": 1}}}
    assert loads_bounded(json.dumps(doc), max_depth=3) == doc
    with pytest.raises(JSONBoundsError):
        loads_bounded(json.dumps(doc), max_depth=2)


@pytest.mark.parametrize(
    "text",
    [
        '{"text": "[literal brackets] {and braces} [[[[[[[["}',
        '{"q": "\\"[[[[[[[["}',
        '{"q": "\\\\", "r": "[[[[[[[["}',
        '{"u": "\\u0022[[[[[[[["}',
    ],
)
def test_brackets_inside_strings_do_not_count(text):
    assert loads_bounded(text, max_depth=1) == json.loads(text)


def test_an_escaped_backslash_closes_nothing_but_the_escape():
    """``"\\\\"`` ends the string: the brackets after it are structure."""
    text = '["\\\\", [[[[]]]]]'
    assert json_depth_within(text, 5)
    assert not json_depth_within(text, 4)


def test_stray_closers_cannot_hide_openers():
    assert not json_depth_within("]]]]]]]]]]" + "[" * 5, 3)


def test_size_ceiling_is_exact_and_in_bytes():
    doc = '"' + "a" * 8 + '"'
    assert loads_bounded(doc, max_bytes=len(doc)) == "a" * 8
    with pytest.raises(JSONBoundsError) as err:
        loads_bounded(doc, max_bytes=len(doc) - 1)
    assert err.value.reason == "too-large"
    # str is measured as the UTF-8 bytes it would take, not as characters.
    wide = '"' + "é" * 8 + '"'
    with pytest.raises(JSONBoundsError):
        loads_bounded(wide, max_bytes=len(wide))
    assert loads_bounded(wide, max_bytes=len(wide.encode())) == "é" * 8
    with pytest.raises(JSONBoundsError):
        loads_bounded(doc.encode(), max_bytes=len(doc) - 1)


def test_size_is_checked_before_depth_and_before_decoding(monkeypatch):
    monkeypatch.setattr(json, "detect_encoding", lambda b: pytest.fail("decoded"))
    with pytest.raises(JSONBoundsError) as err:
        loads_bounded(b"[" * 100, max_bytes=99)
    assert err.value.reason == "too-large"


def test_no_size_ceiling_still_has_a_depth_ceiling():
    # 1_000_000 levels run in a subprocess below; here, few enough that a
    # missing check is a clean failure, not a crash of the test runner.
    with pytest.raises(JSONBoundsError) as err:
        loads_bounded("[" * (MAX_JSON_DEPTH + 1), max_bytes=None)
    assert err.value.reason == "too-deep"
    assert loads_bounded(json.dumps(nested(MAX_JSON_DEPTH)), max_bytes=None)


@pytest.mark.parametrize("kind", [bytes, bytearray, memoryview])
def test_bytes_like_inputs(kind):
    raw = json.dumps({"a": [1]}).encode("utf-16")
    assert loads_bounded(kind(raw)) == {"a": [1]}
    with pytest.raises(JSONBoundsError):
        loads_bounded(kind(raw), max_bytes=len(raw) - 1)


@pytest.mark.parametrize("raw", [b"\xff", b"\xff\xfe\x00"])
def test_undecodable_bytes_are_a_value_error(raw):
    with pytest.raises(ValueError):
        loads_bounded(raw)


@pytest.mark.parametrize("data", ["", b"", "   "])
def test_empty_input_is_invalid_json(data):
    with pytest.raises(ValueError):
        loads_bounded(data)


@pytest.mark.parametrize("data", [None, 1, {"a": 1}, ["["]])
def test_non_text_input_is_a_type_error(data):
    with pytest.raises(TypeError):
        loads_bounded(data)


@pytest.mark.parametrize(
    "kwargs",
    [{"max_bytes": -1}, {"max_depth": -1}, {"max_depth": None}, {"max_bytes": True},
     {"max_depth": 2.5}, {"max_bytes": "10"}],
)
def test_limits_are_validated(kwargs):
    with pytest.raises((TypeError, ValueError)) as err:
        loads_bounded("1", **kwargs)
    assert not isinstance(err.value, JSONBoundsError)


def test_a_recursion_error_is_reported_as_too_deep(monkeypatch):
    def boom(*a, **kw):
        raise RecursionError

    monkeypatch.setattr(json, "loads", boom)
    with pytest.raises(JSONBoundsError) as err:
        loads_bounded("[]")
    assert err.value.reason == "too-deep"


# =============================================================================
# payment_challenge_from: header (base64 and raw), body (str and bytes)
# =============================================================================


@pytest.mark.parametrize("encoding", ENCODINGS)
@pytest.mark.parametrize("transport", ["header", "header-raw", "body", "body-str",
                                       "body-misleading-charset"])
@pytest.mark.parametrize("deep", [False, True])
def test_measures_the_same_decoding_it_parses(parsed_texts, encoding, transport, deep):
    doc = challenge(description='"', extra=nested(5) if deep else {"name": "café"})
    raw = json.dumps(doc, ensure_ascii=False).encode(encoding)
    headers, body = {}, raw
    if transport == "header":
        headers = {"PaYmEnT-ReQuIrEd": " " + base64.b64encode(raw).decode() + " "}
        body = b"{}"
    elif transport == "header-raw":
        if not encoding.startswith("utf-8"):
            pytest.skip("a raw header is text")
        headers = {"payment-required": raw.decode(encoding)}
        body = None
    elif transport == "body-str":
        if encoding != "utf-8":
            pytest.skip("a str body has no encoding")
        body = raw.decode()
    elif transport == "body-misleading-charset":
        headers = {"content-type": "application/json; charset=utf-16"}
    got = payment_challenge_from(headers, body, max_depth=5)
    assert all(json_depth_within(t, 5) for t in parsed_texts)
    assert got == (None if deep else doc)


@pytest.mark.parametrize("name", ["payment-required", "PAYMENT-REQUIRED", "Payment-Required",
                                  "x-payment-required", "X-PAYMENT-REQUIRED",
                                  "pAyMeNt-rEqUiReD", " payment-required "])
def test_header_names_in_any_case(name):
    header = base64.b64encode(json.dumps(challenge()).encode()).decode()
    assert payment_challenge_from({name: header}) == challenge()
    deep = base64.b64encode(json.dumps(challenge(extra=nested(10))).encode()).decode()
    assert payment_challenge_from({name: deep}, max_depth=5) is None


def test_httpx_headers_are_read_too():
    header = base64.b64encode(json.dumps(challenge()).encode()).decode()
    assert payment_challenge_from(httpx.Headers({"Payment-Required": header})) == challenge()


def test_unpadded_base64_header_is_measured_after_decoding(parsed_texts):
    deep = json.dumps(challenge(extra=nested(10)))
    header = base64.b64encode(deep.encode()).decode().rstrip("=")
    assert payment_challenge_from({"payment-required": header}, max_depth=5) is None
    # Only the raw header (base64 text, not JSON) reached the parser.
    assert parsed_texts == [header]


def test_bytes_header_values():
    raw = json.dumps(challenge()).encode()
    assert payment_challenge_from({"payment-required": base64.b64encode(raw)}) == challenge()
    assert payment_challenge_from({"payment-required": raw}) == challenge()


def test_deep_header_falls_back_to_a_sane_body():
    deep = base64.b64encode(json.dumps(challenge(extra=nested(10))).encode()).decode()
    body = challenge(description="from body")
    got = payment_challenge_from({"payment-required": deep}, json.dumps(body), max_depth=5)
    assert got == body


def test_deep_body_is_no_terms():
    body = json.dumps(challenge(extra=nested(10)))
    assert payment_challenge_from({}, body, max_depth=5) is None
    assert payment_challenge_from({}, body.encode(), max_depth=5) is None


def test_json_at_the_depth_limit_is_read():
    doc = challenge(extra={"info": {"text": "[literal brackets]"}})
    assert depth_of(doc) == 5
    assert payment_challenge_from({}, json.dumps(doc), max_depth=5) == doc
    assert payment_challenge_from({}, json.dumps(doc), max_depth=4) is None


def test_oversized_header_is_not_even_decoded(monkeypatch):
    header = base64.b64encode(json.dumps(challenge(description="x" * 200)).encode()).decode()
    monkeypatch.setattr(base64, "b64decode", lambda *a, **kw: pytest.fail("decoded"))
    body = challenge(description="from body")
    got = payment_challenge_from({"payment-required": header}, body, max_bytes=len(header) - 1)
    assert got == body


def test_oversized_body_is_no_terms():
    body = json.dumps(challenge(description="x" * 200))
    assert payment_challenge_from({}, body, max_bytes=len(body) - 1) is None
    assert payment_challenge_from({}, body.encode(), max_bytes=len(body) - 1) is None
    assert payment_challenge_from({}, body, max_bytes=len(body)) == json.loads(body)
    assert payment_challenge_from({}, body, max_bytes=None) == json.loads(body)


def test_default_ceilings_apply_to_both_transports():
    big = json.dumps(challenge(description="x" * MAX_CHALLENGE_BYTES))
    assert payment_challenge_from({}, big) is None
    assert payment_challenge_from({}, big.encode()) is None
    assert payment_challenge_from({"payment-required": big}) is None
    deep = json.dumps(challenge(extra=nested(MAX_JSON_DEPTH)))
    assert payment_challenge_from({}, deep) is None
    fits = json.dumps(challenge(extra=nested(MAX_JSON_DEPTH - 3)))
    assert payment_challenge_from({}, fits) == json.loads(fits)


def test_a_bytearray_body_is_read_and_bounded():
    raw = json.dumps(challenge()).encode("utf-16")
    assert payment_challenge_from({}, bytearray(raw)) == challenge()
    deep = json.dumps(challenge(extra=nested(10))).encode()
    assert payment_challenge_from({}, bytearray(deep), max_depth=5) is None


def test_a_parsed_dict_body_is_taken_as_given():
    doc = challenge(extra=nested(10))
    assert payment_challenge_from({}, doc, max_depth=1) == doc


def test_bad_base64_decoding_still_allows_raw_json_header(monkeypatch):
    monkeypatch.setattr(base64, "b64decode", lambda *a, **kw: b"\xff")
    assert payment_challenge_from({"payment-required": json.dumps(challenge())}) == challenge()


@pytest.mark.parametrize("raw", [b"\xff", b"\xff\xfe\x00", b"", "", "   "])
def test_invalid_or_empty_input_is_not_a_challenge(raw):
    assert payment_challenge_from({}, raw) is None
    assert payment_challenge_from({"payment-required": raw}) is None


@pytest.mark.parametrize("headers", [None, {}, {"payment-required": None}, {1: "x"}])
def test_missing_headers(headers):
    assert payment_challenge_from(headers, challenge()) == challenge()


# =============================================================================
# The process survives (each case in a fresh interpreter: a segfault is a
# return code, not a test failure inside this process)
# =============================================================================


SURVIVES = textwrap.dedent(
    """
    import base64, json, sys
    import httpx
    import uvd_x402_sdk  # imports py_ecc, which raises the recursion limit
    from uvd_x402_sdk import X402Client, loads_bounded, payment_challenge_from
    from uvd_x402_sdk.exceptions import InvalidPayloadError

    assert sys.getrecursionlimit() >= 100000, sys.getrecursionlimit()
    case = sys.argv[1]
    deep = "[" * 100_000
    if case == "header":
        got = payment_challenge_from({"payment-required": base64.b64encode(deep.encode()).decode()})
    elif case == "header-raw":
        got = payment_challenge_from({"payment-required": deep})
    elif case == "body-bytes":
        got = payment_challenge_from({}, deep.encode())
    elif case == "body-str":
        got = payment_challenge_from({}, deep)
    elif case == "unbounded-size":
        try:
            loads_bounded("[" * 1_000_000, max_bytes=None)
        except ValueError as exc:
            got = type(exc).__name__
    elif case == "fetch":
        client = X402Client(
            recipient_evm="0x" + "22" * 20, verify_facilitator_support=False
        )
        client.connect_with_private_key("0x" + "11" * 32, chain_name="base")
        seller = lambda req: httpx.Response(402, content=deep.encode())
        with httpx.Client(transport=httpx.MockTransport(seller)) as http:
            try:
                client.fetch("https://seller.example/x", http_client=http)
            except InvalidPayloadError as exc:
                got = "InvalidPayloadError"
    print("survived", got)
    """
)


@pytest.mark.parametrize("case", ["header", "header-raw", "body-bytes", "body-str",
                                  "unbounded-size", "fetch"])
def test_hundred_thousand_brackets_do_not_kill_the_process(case):
    proc = subprocess.run(
        [sys.executable, "-c", SURVIVES, case],
        capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, (proc.returncode, proc.stderr[-2000:])
    expected = {"unbounded-size": "JSONBoundsError", "fetch": "InvalidPayloadError"}
    assert proc.stdout.strip() == f"survived {expected.get(case, 'None')}"


# =============================================================================
# X402Client.fetch: the real buyer path never parses a deep or huge 402
# =============================================================================


def _client():
    client = X402Client(recipient_evm="0x" + "22" * 20, verify_facilitator_support=False)
    client.connect_with_private_key("0x" + "11" * 32, chain_name="base")
    return client


def _fetch(client, body: bytes, headers=None):
    calls = []

    def seller(request):
        calls.append(request)
        return httpx.Response(402, content=body, headers=headers or {})

    with httpx.Client(transport=httpx.MockTransport(seller)) as http:
        try:
            client.fetch("https://seller.example/x", http_client=http)
        finally:
            assert len(calls) == 1, "nothing may be paid for an unreadable 402"


def test_fetch_refuses_a_deep_402_before_parsing(parsed_texts):
    body = json.dumps({"x402Version": 1, "accepts": [], "extra": nested(MAX_JSON_DEPTH + 1)})
    with pytest.raises(InvalidPayloadError, match="refused before parsing"):
        _fetch(_client(), body.encode())
    assert parsed_texts == []


def test_fetch_refuses_an_oversized_402_before_parsing(parsed_texts):
    body = json.dumps({"x402Version": 1, "accepts": [], "pad": "x" * MAX_CHALLENGE_BYTES})
    with pytest.raises(InvalidPayloadError, match="refused before parsing"):
        _fetch(_client(), body.encode())
    assert parsed_texts == []


def test_fetch_reads_a_utf16_402_whatever_the_charset_says():
    body = json.dumps({"x402Version": 1, "accepts": []}).encode("utf-16")
    with pytest.raises(Exception) as err:
        _fetch(_client(), body, {"content-type": "application/json; charset=iso-8859-1"})
    assert "not JSON" not in str(err.value)
    assert "refused" not in str(err.value)


def test_fetch_still_says_not_json():
    with pytest.raises(InvalidPayloadError, match="not JSON"):
        _fetch(_client(), b"<html>pay me</html>")


# =============================================================================
# Fuzz: the depth count agrees with json.loads on what it parses
# =============================================================================

_ALPHABET = '[]{}"\\,:ab 1\u00e9'


@pytest.mark.parametrize("seed", range(20))
def test_fuzz_random_text_agrees_with_the_parser(seed):
    rng = random.Random(seed)
    for _ in range(500):
        text = "".join(rng.choice(_ALPHABET) for _ in range(rng.randint(0, 40)))
        try:
            value = json.loads(text)
        except ValueError:
            continue
        true = depth_of(value)
        for limit in (max(true - 1, 0), true, true + 1):
            assert json_depth_within(text, limit) == (true <= limit), (text, limit)


def _random_doc(rng, budget):
    if budget <= 0 or rng.random() < 0.3:
        return rng.choice([
            "plain", '"', "\\", "[", "]", "{", "}", '\\"[', '"\\"', "\u00e9\u2028",
            "\U0001f600", 1, 2.5, True, None,
        ])
    if rng.random() < 0.5:
        return [_random_doc(rng, budget - 1) for _ in range(rng.randint(0, 3))]
    return {
        rng.choice(["k", "[", '"', "}", "\\"]) + str(i): _random_doc(rng, budget - 1)
        for i in range(rng.randint(0, 3))
    }


@pytest.mark.parametrize("seed", range(20))
def test_fuzz_documents_in_every_encoding(seed):
    rng = random.Random(1000 + seed)
    for _ in range(50):
        doc = _random_doc(rng, rng.randint(0, 12))
        true = depth_of(doc)
        encoding = rng.choice(ENCODINGS)
        raw = json.dumps(doc, ensure_ascii=rng.random() < 0.5).encode(encoding, "surrogatepass")
        assert loads_bounded(raw, max_depth=true) == doc
        if true:
            with pytest.raises(JSONBoundsError):
                loads_bounded(raw, max_depth=true - 1)


@pytest.mark.parametrize("seed", range(10))
def test_fuzz_challenges_through_both_transports(seed):
    rng = random.Random(2000 + seed)
    for _ in range(30):
        doc = challenge(extra=_random_doc(rng, rng.randint(0, 10)))
        true = depth_of(doc)
        limit = rng.randint(max(true - 2, 1), true + 2)
        raw = json.dumps(doc, ensure_ascii=False).encode(rng.choice(ENCODINGS))
        expected = doc if true <= limit else None
        header = {"Payment-Required": base64.b64encode(raw).decode()}
        assert payment_challenge_from(header, max_depth=limit) == expected
        assert payment_challenge_from({}, raw, max_depth=limit) == expected
