"""The 402 challenge travels in a header, and a body-only reader finds nothing.

Vectors are real: REAL_HEADER is base64 of the challenge shape production
serves, and PREVIEW_BODY is what Tenjin actually returns in a 402 body -- the
free preview of the paid article.
"""

from uvd_x402_sdk import payment_challenge_from

REAL_HEADER = "eyJ4NDAyVmVyc2lvbiI6IDIsICJlcnJvciI6ICJQYXltZW50IHJlcXVpcmVkIiwgImFjY2VwdHMiOiBbeyJzY2hlbWUiOiAiZXhhY3QiLCAibmV0d29yayI6ICJlaXAxNTU6ODQ1MyIsICJhbW91bnQiOiAiMTAwMDAwIiwgImFzc2V0IjogIjB4ODMzNTg5ZkNENmVEYjZFMDhmNGM3QzMyRDRmNzFiNTRiZEEwMjkxMyIsICJwYXlUbyI6ICIweGIwNTllQUM5MzMwREM1ZjIzRjUzNDZhODEzNDhBZjFFOTlmMzc5YmQiLCAibWF4VGltZW91dFNlY29uZHMiOiAzMDB9XX0="

PREVIEW_BODY = {
    "id": "01a01a4c",
    "slug": "china-macro-weekly",
    "title": "China Macro Weekly",
    "price": "100000",
}


def test_the_challenge_is_found_in_the_header():
    """Measured 2026-08-20: 36 of 36 live resources answering 402 do this."""
    got = payment_challenge_from({"payment-required": REAL_HEADER}, PREVIEW_BODY)
    assert got is not None
    assert len(got["accepts"]) == 1
    assert got["accepts"][0]["payTo"].lower() == "0xb059eac9330dc5f23f5346a81348af1e99f379bd"


def test_a_free_preview_is_not_a_challenge():
    """THE failure this exists to prevent.

    Valid JSON with no payment terms. Returning it as a challenge is how a
    body-only reader concludes it looked and found nothing wrong -- which is
    exactly what the Bazaar hijack check did on every header-transport resource.
    """
    assert payment_challenge_from({}, PREVIEW_BODY) is None


def test_the_body_transport_still_works():
    """Both transports are legal; supporting the header must not drop the body."""
    body = {"x402Version": 2, "accepts": [{"payTo": "0xAAAA"}]}
    got = payment_challenge_from({}, body)
    assert got is not None and len(got["accepts"]) == 1


def test_a_v1_top_level_pay_to_counts():
    assert payment_challenge_from({}, {"payTo": "0xBBBB"}) is not None


def test_an_unparseable_header_falls_through_to_the_body():
    """A header we cannot decode must not refuse a seller whose body is fine."""
    got = payment_challenge_from(
        {"payment-required": "!!!not base64!!!"}, {"accepts": [{"payTo": "0xCCCC"}]}
    )
    assert got is not None


def test_a_json_string_body_is_parsed():
    import json

    got = payment_challenge_from({}, json.dumps({"accepts": [{"payTo": "0xDDDD"}]}))
    assert got is not None


def test_header_casing_does_not_matter():
    for key in ("payment-required", "PAYMENT-REQUIRED", "Payment-Required"):
        assert payment_challenge_from({key: REAL_HEADER}) is not None, key


def test_nothing_anywhere_is_none_not_an_empty_dict():
    """'No terms here' must be distinguishable from 'terms with nothing in them'."""
    assert payment_challenge_from({}, None) is None
    assert payment_challenge_from({}, "not json") is None


def test_the_v1_paymentRequirements_key_counts():
    """The v1 spelling of `accepts`. KarmaKadabra's buyer matched it in production
    and the header reader missed it — a seller answering `{"paymentRequirements":
    [...]}` looked like "no terms" and its 402 was unpayable. Upstreamed 2026-08-20."""
    body = {"paymentRequirements": [{"payTo": "0xEEEE", "maxAmountRequired": "1000"}]}
    assert payment_challenge_from({}, body) is not None
    # and in the header transport too
    import base64 as _b64
    import json as _json
    hdr = _b64.b64encode(_json.dumps(body).encode()).decode()
    assert payment_challenge_from({"payment-required": hdr}) is not None
