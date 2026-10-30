"""One payment for verify and settle across every server entry point, offline."""

import asyncio
import base64
import json
from types import SimpleNamespace

import httpx
import pytest

import tests.test_bindings_integrations as bound
from tests.receipt_rail import PAYER, x_payment
from tests.test_integrations_replay import FASTAPI, SITES
from uvd_x402_sdk import X402Client
from uvd_x402_sdk.bindings import InMemoryBindingStore
from uvd_x402_sdk.exceptions import InvalidPayloadError
from uvd_x402_sdk.payment_headers import event_payment_header, select_payment_header


def encoded(data):
    return base64.b64encode(json.dumps(data).encode()).decode()


def envelope(version):
    data = json.loads(base64.b64decode(x_payment()))
    if version == 2:
        data["x402Version"] = 2
        data["accepted"] = {"network": "eip155:5042", "scheme": data.pop("scheme")}
        del data["network"]
    return data


@pytest.fixture
def facilitator(monkeypatch):
    calls = []

    def handle(request):
        body = json.loads(request.content)
        calls.append((request.url.path, body))
        if request.url.path == "/verify":
            return httpx.Response(200, json={"isValid": True, "payer": PAYER})
        assert request.url.path == "/settle"
        return httpx.Response(
            200,
            json={
                "success": True,
                "transaction": "0x" + "ab" * 32,
                "network": "arc",
                "payer": PAYER,
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        monkeypatch.setattr(X402Client, "_get_http_client", lambda self: client)
        yield SimpleNamespace(url="https://facilitator.invalid", calls=calls)


@pytest.mark.parametrize("mount", list(SITES), ids=lambda mount: mount.__name__)
@pytest.mark.parametrize("version", [1, 2])
@pytest.mark.parametrize(
    "case", ["x-only", "signature-only", "equal", "different", "no-network", "ows"]
)
def test_the_five_payment_header_cases(mount, facilitator, version, case):
    data = envelope(version)
    if case == "no-network":
        (data if version == 1 else data["accepted"]).pop("network")
    header = encoded(data)
    if case == "x-only":
        headers = {"X-PAYMENT": header}
    elif case in ("signature-only", "no-network"):
        headers = {"pAyMeNt-SiGnAtUrE": header}
    elif case == "ows":
        headers = {"X-PAYMENT": " \t" + header + "\t ", "PAYMENT-SIGNATURE": header}
    elif case == "equal":
        headers = {"x-payment": header, "PAYMENT-SIGNATURE": header}
    else:
        other = envelope(version)
        other["payload"]["authorization"]["nonce"] = "0x02"
        headers = {"X-PAYMENT": header, "PAYMENT-SIGNATURE": encoded(other)}
    site = mount(facilitator)
    status, body, _ = site._call(headers)
    if case in ("different", "no-network"):
        assert status == 400, body
        assert site.delivered == 0
        assert facilitator.calls == []
        if case == "different":
            assert "Conflicting payment headers" in json.dumps(body)
    else:
        assert (status, site.delivered) == (200, 1), body
        assert [path for path, _ in facilitator.calls] == ["/verify", "/settle"]
        verify, settle = [body["paymentPayload"] for _, body in facilitator.calls]
        assert verify == settle
        assert verify["payload"] == data["payload"]
        assert verify["network"] == "arc"


@pytest.mark.parametrize("mount", list(SITES), ids=lambda mount: mount.__name__)
@pytest.mark.parametrize(
    "value",
    [None, "", " ", [], {}, 123, " arc ", "\tarc", "arc\n", "\u2003", "\ufeffarc", "arc\ufeff"],
)
@pytest.mark.parametrize("version", [1, 2])
def test_missing_or_invalid_network_is_400_before_facilitator(mount, facilitator, value, version):
    data = envelope(version)
    (data if version == 1 else data["accepted"])["network"] = value
    site = mount(facilitator)
    status, body, _ = site._call({"X-PAYMENT": encoded(data)})
    assert status == 400, body
    assert site.delivered == 0
    assert facilitator.calls == []


@pytest.mark.parametrize("name", ["X-PAYMENT", "x-payment", "pAyMeNt-SiGnAtUrE"])
def test_selector_compares_only_http_ows_and_preserves_original_bytes(name):
    assert select_payment_header([(name, " \tcredential\t ")]) == " \tcredential\t "
    assert (
        select_payment_header([(name, " \tcredential\t "), (name, "credential")])
        == " \tcredential\t "
    )
    with pytest.raises(InvalidPayloadError):
        select_payment_header([(name, "\u00a0credential\u00a0"), (name, "credential")])
    assert select_payment_header([(name, "\u00a0credential\u00a0")]) == "\u00a0credential\u00a0"
    assert select_payment_header([("Authorization", "ignored")]) is None


@pytest.mark.parametrize("value", [None, "", " \t", 0, [], {}])
def test_explicit_empty_or_non_string_payment_header_is_invalid(value):
    with pytest.raises(InvalidPayloadError, match="non-empty string"):
        select_payment_header([("X-PAYMENT", value)])


@pytest.mark.parametrize("other", ["different", "Credential", "cre dential"])
def test_values_are_compared_exactly(other):
    with pytest.raises(InvalidPayloadError, match="Conflicting"):
        select_payment_header([("X-PAYMENT", "credential"), ("PAYMENT-SIGNATURE", other)])


def test_case_duplicate_and_custom_header_follow_the_same_rule():
    assert (
        select_payment_header([("X-CUSTOM", "credential")], header_name="x-custom") == "credential"
    )
    assert select_payment_header([("X-PAYMENT", "a"), ("x-payment", "a")]) == "a"
    with pytest.raises(InvalidPayloadError, match="Conflicting"):
        select_payment_header([("X-PAYMENT", "a"), ("x-payment", "b")])
    with pytest.raises(InvalidPayloadError, match="Conflicting"):
        select_payment_header([("X-CUSTOM", "a"), ("X-PAYMENT", "b")], header_name="X-CUSTOM")


def test_lambda_multivalue_headers_cannot_hide_another_payment():
    assert event_payment_header({"headers": None}) is None
    assert event_payment_header({"multiValueHeaders": {"payment-signature": ["a", "a"]}}) == "a"
    with pytest.raises(InvalidPayloadError, match="Conflicting"):
        event_payment_header(
            {"headers": {"X-PAYMENT": "a"}, "multiValueHeaders": {"PAYMENT-SIGNATURE": ["b"]}}
        )
    with pytest.raises(InvalidPayloadError, match="Conflicting"):
        event_payment_header({"multiValueHeaders": {"X-PAYMENT": ["a", "b"]}})


@pytest.mark.parametrize("mount", list(SITES), ids=lambda mount: mount.__name__)
def test_no_payment_header_still_requests_payment(mount, facilitator):
    site = mount(facilitator)
    status, body, _ = site._call({})
    assert status == 402, body
    assert site.delivered == 0
    assert facilitator.calls == []


@pytest.mark.parametrize("mount", list(SITES), ids=lambda mount: mount.__name__)
def test_empty_header_is_400_even_beside_a_valid_header(mount, facilitator):
    site = mount(facilitator)
    status, body, _ = site._call({"X-PAYMENT": "", "PAYMENT-SIGNATURE": x_payment()})
    assert status == 400, body
    assert site.delivered == 0
    assert facilitator.calls == []


def test_generic_decorator_custom_header_and_lambda_event():
    from uvd_x402_sdk.decorators import _get_header

    assert _get_header({"headers": {"pAyMeNt-SiGnAtUrE": "a"}}, "X-PAYMENT") == "a"
    assert _get_header({"headers": {"x-custom": "a"}}, "X-CUSTOM") == "a"
    with pytest.raises(InvalidPayloadError, match="Conflicting"):
        _get_header({"headers": {"X-CUSTOM": "a", "X-PAYMENT": "b"}}, "X-CUSTOM")
    with pytest.raises(InvalidPayloadError, match="Conflicting"):
        _get_header({"multiValueHeaders": {"X-PAYMENT": ["a", "b"]}}, "X-PAYMENT")


class CountStore:
    def __init__(self):
        self.inner = InMemoryBindingStore()
        self.calls = []

    def bind(self, *args):
        self.calls.append(args)
        return self.inner.bind(*args)


@pytest.mark.parametrize("mount", bound.ENTRY_POINTS, ids=lambda m: m.__name__)
@pytest.mark.parametrize("version", [1, 2])
@pytest.mark.parametrize(
    "case", ["x-only", "signature-only", "equal", "different", "no-network", "ows"]
)
def test_binding_receives_only_valid_unambiguous_payment(
    mount, facilitator, monkeypatch, version, case
):
    data = envelope(version)
    if case == "no-network":
        (data if version == 1 else data["accepted"]).pop("network")
    header = encoded(data)
    if case == "x-only":
        headers = {"X-PAYMENT": header}
    elif case in ("signature-only", "no-network"):
        headers = {"PAYMENT-SIGNATURE": header}
    elif case == "ows":
        headers = {"X-PAYMENT": " \t" + header + "\t ", "PAYMENT-SIGNATURE": header}
    elif case == "equal":
        headers = {"X-PAYMENT": header, "PAYMENT-SIGNATURE": header}
    else:
        second = envelope(version)
        second["payload"]["authorization"]["nonce"] = "0x02"
        headers = {"X-PAYMENT": header, "PAYMENT-SIGNATURE": encoded(second)}

    def asgi(app):
        def call(*args):
            async def send():
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://testserver"
                ) as client:
                    return await client.get("/paid", headers=headers)

            response = asyncio.run(send())
            return response.status_code, response.json(), dict(response.headers)

        return call

    old_event = bound._event

    def event(*args):
        value = old_event(*args)
        value["headers"] = headers
        return value

    monkeypatch.setattr(bound, "_asgi", asgi)
    monkeypatch.setattr(bound, "_event", event)
    store = CountStore()
    site = mount(facilitator, store)
    status, body, _ = site.get(header)
    if case in ("different", "no-network"):
        assert status == 400, body
        assert site.delivered == 0
        assert store.calls == []
        assert facilitator.calls == []
    else:
        assert (status, site.delivered) == (200, 1), body
        assert len(store.calls) == 1
        expected = (
            header
            if case != "ows" or mount.__name__.startswith("fastapi")
            else " \t" + header + "\t "
        )
        assert store.calls[0][0] == bound.payment_key(expected)
        assert [path for path, _ in facilitator.calls] == ["/verify", "/settle"]
        assert (
            facilitator.calls[0][1]["paymentPayload"] == facilitator.calls[1][1]["paymentPayload"]
        )


@pytest.mark.parametrize("mount", FASTAPI, ids=lambda m: m.__name__)
@pytest.mark.parametrize("case", ["same", "different", "mixed"])
def test_asgi_duplicate_fields(mount, facilitator, case):
    first = encoded(envelope(1))
    other = envelope(1)
    other["payload"]["authorization"]["nonce"] = "0x02"
    second = encoded(other)
    headers = [("X-PAYMENT", first), ("x-payment", first if case == "same" else second)]
    if case == "mixed":
        headers.append(("Payment-Signature", first))
    site = mount(facilitator)
    status, body, _ = site._call(headers)
    if case == "same":
        assert (status, site.delivered) == (200, 1), body
    else:
        assert status == 400, body
        assert site.delivered == 0
        assert facilitator.calls == []


@pytest.mark.parametrize(
    "value",
    [None, "", " ", [], {}, 123, " arc ", "\tarc", "arc\n", "\u2003", "\ufeffarc", "arc\ufeff"],
)
def test_v2_normalizer_rejects_invalid_network_directly(value):
    data = envelope(2)
    data["accepted"]["network"] = value
    with pytest.raises(InvalidPayloadError):
        X402Client._normalize_v2_envelope(data)


def test_ows_resend_recovers_a_payment_bound_before_the_upgrade():
    from decimal import Decimal

    from tests.receipt_rail import RECIPIENT, Facilitator
    from uvd_x402_sdk import X402Config
    from uvd_x402_sdk.bindings import process_payment_bound, purchase_resource
    from uvd_x402_sdk.integrations.lambda_integration import LambdaX402
    from uvd_x402_sdk.models import PaymentResult

    rail = Facilitator("receipts")
    try:
        config = X402Config(facilitator_url=rail.url, recipient_evm=RECIPIENT)
        store = InMemoryBindingStore()
        raw = " \t" + x_payment() + "\t "
        amount = Decimal("0.01")
        resource = purchase_resource("GET", "/paid", "", b"")
        # Existing releases persisted the original header bytes. Simulate the
        # process dying after settlement, before its response was delivered.
        with X402Client(config=config) as client:
            first = process_payment_bound(client, store, raw, amount, resource)
        assert first.success
        event = {
            "version": "2.0",
            "rawPath": "/paid",
            "rawQueryString": "",
            "headers": {"X-PAYMENT": raw},
            "requestContext": {"http": {"method": "GET", "path": "/paid"}},
        }
        second = LambdaX402(config=config, binding_store=store).process_or_require(event, amount)
        assert isinstance(second, PaymentResult), second
        assert second.success
        assert second.idempotent_replayed
        assert rail.executed == 1
    finally:
        rail.close()


@pytest.mark.parametrize("preferred", ["X-PAYMENT", "PAYMENT-SIGNATURE", "X-CUSTOM"])
@pytest.mark.parametrize("reverse", [False, True])
def test_equivalent_aliases_preserve_the_preferred_original_bytes(preferred, reverse):
    other = "PAYMENT-SIGNATURE" if preferred != "PAYMENT-SIGNATURE" else "X-PAYMENT"
    headers = [(preferred, " \tcredential\t "), (other, "credential")]
    if reverse:
        headers.reverse()
    assert select_payment_header(headers, header_name=preferred) == " \tcredential\t "


@pytest.mark.parametrize("value", [[], {}, 123])
def test_invalid_network_identifies_the_field_for_callers(value):
    data = envelope(1)
    data["network"] = value
    with X402Client(recipient_address=PAYER) as client, pytest.raises(InvalidPayloadError) as error:
        client.extract_payload(encoded(data))
    assert error.value.details["field"] == "network"
