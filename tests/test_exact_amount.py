"""The seller never asks for an amount a payer cannot sign as written.

A payer signs a whole number of base units, and a strict one refuses, before
signing, a decimal it cannot turn into one exactly. Two seller paths produced
such amounts:

* The v1 402 body wrote its ``amount`` with ``str()``, which puts a ``Decimal``
  in exponent form: ``Decimal("10.00").normalize()`` went out as ``"1E+1"`` and
  ``Decimal("0.0000005")`` as ``"5E-7"``.
* ``NetworkConfig.get_token_amount()`` (the v2 402, and the requirements of
  ``verify_payment()`` / ``settle_payment()``) truncated digits below one base
  unit: ``0.0000015`` required 1 base unit at 6 decimals, ``5E-7`` a price of 0.
  So did the ``token_decimals`` path of the requirements and
  ``build_erc8004_payment_requirements()``.

Now every path ends one of two ways: the amount goes out as a plain decimal that
is a whole number of base units of each chain listed (a base-unit integer where
the wire carries one), or ``ValueError`` before anything is emitted or sent.

``FORMS`` are eight shapes a price takes on its way in: six a payer could not
sign as written, and two controls (trailing zeros are valid). Each is checked
with a 6-decimal token (USDC on Base) and a 7-decimal one (USDC on Stellar): 16
cases, 11 of which the v1 body used to send in a form a strict payer refuses.
The mutations at the end put each old behavior back and check it turns red.
"""
from __future__ import annotations

import json
import re
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable

import httpx
import pytest

from uvd_x402_sdk import X402Client
from uvd_x402_sdk import client as client_module
from uvd_x402_sdk import erc8004 as erc8004_module
from uvd_x402_sdk import response as response_module
from uvd_x402_sdk.config import X402Config
from uvd_x402_sdk.erc8004 import build_erc8004_payment_requirements
from uvd_x402_sdk.models import PaymentPayload
from uvd_x402_sdk.networks import base as base_module
from uvd_x402_sdk.networks import get_network
from uvd_x402_sdk.response import create_402_response, create_402_response_v2

EVM_RECIPIENT = "0x2222222222222222222222222222222222222222"
STELLAR_RECIPIENT = "GBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB"
FACILITATOR = "http://facilitator.invalid"
SETTLED = json.loads(
    (
        Path(__file__).parent
        / "fixtures"
        / "facilitator-settle-2.40.0"
        / "settle_success_without_proof.json"
    ).read_text(encoding="utf-8")
)["body"]

# decimals -> the network whose USDC has them, and the config that lists only it
NETWORKS: dict[int, str] = {6: "base", 7: "stellar"}
CONFIGS: dict[int, X402Config] = {
    6: X402Config(recipient_evm=EVM_RECIPIENT, supported_networks=["base"]),
    7: X402Config(recipient_stellar=STELLAR_RECIPIENT, supported_networks=["stellar"]),
}

# (id, the price as the seller passes it, {decimals: what the v1 body must say})
# None: ValueError, the price cannot be charged exactly with those decimals.
FORMS: tuple[tuple[str, Decimal, dict[int, str | None]], ...] = (
    ("normalize-10.00", Decimal("10.00").normalize(), {6: "10", 7: "10"}),
    ("1E+3", Decimal("1E+3"), {6: "1000", 7: "1000"}),
    ("float-3x0.10", Decimal(str(3 * 0.10)), {6: None, 7: None}),
    ("binary-2.01", Decimal(2.01), {6: None, 7: None}),
    ("5E-7", Decimal("5E-7"), {6: None, 7: "0.0000005"}),
    ("0.0000015", Decimal("0.0000015"), {6: None, 7: "0.0000015"}),
    ("2.010", Decimal("2.010"), {6: "2.010", 7: "2.010"}),
    ("10.00", Decimal("10.00"), {6: "10.00", 7: "10.00"}),
)
CASES = [(form_id, value, decimals) for form_id, value, _ in FORMS for decimals in (6, 7)]
EXPECTED_V1 = {(form_id, d): expected[d] for form_id, _, expected in FORMS for d in (6, 7)}
REFUSED = {case for case, expected in EXPECTED_V1.items() if expected is None}

PLAIN_DECIMAL = re.compile(r"[0-9]+(\.[0-9]+)?")


def payer_can_sign(amount: str, decimals: int) -> bool:
    """What a strict payer checks before signing a decimal amount: a plain
    decimal (no exponent, no sign) whose digits past ``decimals`` are all zero."""
    if not PLAIN_DECIMAL.fullmatch(amount):
        return False
    return amount.partition(".")[2][decimals:].strip("0") == ""


def base_units(plain: str | None, decimals: int) -> str | None:
    """The base-unit integer of an expected plain amount, as the wire writes it."""
    return None if plain is None else str(int(Decimal(plain).scaleb(decimals)))


def outcome(produce: Callable[[], Any]) -> str | None:
    """What a seller path produced for one price: its string, or None on ValueError."""
    try:
        return str(produce())
    except ValueError:
        return None


# ── what each seller path produces ──────────────────────────────────────────


def v1_amount(value: Decimal, decimals: int) -> str | None:
    return outcome(lambda: create_402_response(value, CONFIGS[decimals])["amount"])


def v2_amount(value: Decimal, decimals: int) -> str | None:
    return outcome(lambda: create_402_response_v2(value, CONFIGS[decimals])["accepts"][0]["amount"])


def token_amount(value: Decimal, decimals: int) -> str | None:
    network = get_network(NETWORKS[decimals])
    assert network is not None
    return outcome(lambda: network.get_token_amount(value))


class Facilitator:
    """Answers every settle with a recorded success and keeps what it got."""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.sent.append(json.loads(request.content))
        return httpx.Response(200, headers={"content-type": "application/json"}, content=SETTLED)


def evm_payload() -> PaymentPayload:
    return PaymentPayload(
        x402Version=1,
        scheme="exact",
        network="base",
        payload={
            "signature": "0x" + "11" * 65,
            "authorization": {
                "from": "0x774A351d1AA8cd221a1B87da639EFcc5A56cd9ce",
                "to": EVM_RECIPIENT,
                "value": "10000",
                "validAfter": "0",
                "validBefore": "9999999999",
                "nonce": "0x" + "42" * 32,
            },
        },
    )


def settled_amount(value: Decimal, token_decimals: int | None) -> str | None:
    """``maxAmountRequired`` of the settle, or None when it raised ValueError
    without sending anything."""
    facilitator = Facilitator()
    client = X402Client(
        recipient_address=EVM_RECIPIENT,
        facilitator_url=FACILITATOR,
        http_client=httpx.Client(transport=httpx.MockTransport(facilitator.handler)),
    )
    try:
        client.settle_payment(evm_payload(), value, token_decimals=token_decimals)
    except ValueError:
        assert facilitator.sent == []
        return None
    return str(facilitator.sent[-1]["paymentRequirements"]["maxAmountRequired"])


def erc8004_amount(value: Decimal) -> str | None:
    return outcome(
        lambda: build_erc8004_payment_requirements(str(value), EVM_RECIPIENT, FACILITATOR)[
            "maxAmountRequired"
        ]
    )


# ── the contract, as the set of cases that break it ─────────────────────────


def v1_mismatches() -> dict[tuple[str, int], str | None]:
    """Every case whose v1 body is not the expected one, with what it said."""
    got = {(form_id, d): v1_amount(value, d) for form_id, value, d in CASES}
    return {case: amount for case, amount in got.items() if amount != EXPECTED_V1[case]}


def base_unit_mismatches(
    produce: Callable[[Decimal, int], str | None], decimals: tuple[int, ...] = (6, 7)
) -> dict[tuple[str, int], str | None]:
    """Every case whose base-unit amount is not the exact one (or not refused)."""
    got = {(form_id, d): produce(value, d) for form_id, value, d in CASES if d in decimals}
    return {
        case: amount
        for case, amount in got.items()
        if amount != base_units(EXPECTED_V1[case], case[1])
    }


class TestTheSixteenForms:
    def test_the_v1_body_is_plain_and_exact_or_refused(self) -> None:
        assert v1_mismatches() == {}
        for (form_id, d), expected in EXPECTED_V1.items():
            if expected is not None:
                assert payer_can_sign(expected, d), (form_id, d)

    def test_the_v2_body_asks_the_exact_base_units_or_refuses(self) -> None:
        assert base_unit_mismatches(v2_amount) == {}

    def test_get_token_amount_is_exact_or_refuses(self) -> None:
        assert base_unit_mismatches(token_amount) == {}

    def test_the_settle_requires_the_exact_base_units_or_sends_nothing(self) -> None:
        assert base_unit_mismatches(lambda v, d: settled_amount(v, None), (6,)) == {}

    def test_the_settle_with_token_decimals_too(self) -> None:
        assert base_unit_mismatches(settled_amount) == {}

    def test_the_erc8004_requirements_helper_too(self) -> None:
        assert base_unit_mismatches(lambda v, d: erc8004_amount(v), (6,)) == {}

    def test_the_default_message_says_the_same_plain_amount(self) -> None:
        body = create_402_response(Decimal("1E+1"), CONFIGS[6])
        assert body["message"] == "Payment of $10 USDC required"

    def test_the_refusal_names_the_chain_and_its_decimals(self) -> None:
        with pytest.raises(ValueError, match=r"0\.0000015 .*USDC on base \(6 decimals\)"):
            create_402_response(Decimal("0.0000015"), CONFIGS[6])

    def test_a_v1_body_listing_two_chains_needs_the_price_exact_on_both(self) -> None:
        both = X402Config(
            recipient_evm=EVM_RECIPIENT,
            recipient_stellar=STELLAR_RECIPIENT,
            supported_networks=["stellar", "base"],
        )
        with pytest.raises(ValueError, match="on base"):
            create_402_response(Decimal("0.0000015"), both)
        assert create_402_response(Decimal("0.000001"), both)["amount"] == "0.000001"

    @pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity"])
    def test_a_price_that_is_not_a_number_is_refused(self, value: str) -> None:
        for decimals in (6, 7):
            assert v1_amount(Decimal(value), decimals) is None
            assert v2_amount(Decimal(value), decimals) is None

    def test_exact_beyond_the_decimal_context(self) -> None:
        """A ``Decimal`` product rounds to 28 significant digits, which turned
        this sub-unit digit into a whole number of base units."""
        base = get_network("base")
        assert base is not None
        with pytest.raises(ValueError):
            base.get_token_amount(Decimal("1.0000000000000000000000000000001"))
        huge = Decimal("123456789012345678901234567890.123456")
        assert base.get_token_amount(huge) == 123456789012345678901234567890123456


# ── every cent price still goes out exactly as written ──────────────────────

CENT_PRICES = range(1, 10_000)  # $0.01 .. $99.99


class TestEveryCentPrice:
    def test_the_v1_body(self) -> None:
        for decimals, config in CONFIGS.items():
            for to_input in (lambda c: Decimal(c) / 100, lambda c: c / 100):
                wrong = []
                for c in CENT_PRICES:
                    amount = create_402_response(to_input(c), config)["amount"]
                    if Decimal(amount) != Decimal(c) / 100 or not payer_can_sign(amount, decimals):
                        wrong.append((c, amount))
                assert wrong == [], decimals

    def test_the_base_units_with_seven_decimals(self) -> None:
        stellar = get_network("stellar")
        assert stellar is not None
        wrong = [c for c in CENT_PRICES if stellar.get_token_amount(Decimal(c) / 100) != c * 10**5]
        assert wrong == []


# ── the mutations: each old behavior put back turns its test red ────────────


def truncating(amount: Any, decimals: int, **_: Any) -> int:
    """The conversion before this fix: digits below one base unit dropped."""
    value = amount if isinstance(amount, Decimal) else Decimal(str(amount))
    return int(value * (Decimal(10) ** decimals))


# The cases a strict payer refused when the v1 body wrote str(amount).
EXPONENT_FORM = {
    ("normalize-10.00", 6), ("normalize-10.00", 7), ("1E+3", 6), ("1E+3", 7), ("5E-7", 7),
}


class TestMutations:
    def test_the_v1_body_written_with_str_puts_exponents_on_the_wire(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A module global named `format` shadows the builtin in response.py only:
        # the body goes back to str(amount).
        monkeypatch.setattr(
            response_module, "format", lambda value, spec: str(value), raising=False
        )
        mismatches = v1_mismatches()
        assert set(mismatches) == EXPONENT_FORM
        assert not any(payer_can_sign(mismatches[case] or "", case[1]) for case in EXPONENT_FORM)

    def test_the_v1_body_without_its_exactness_check_asks_what_nobody_can_sign(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(response_module, "to_base_units", lambda *a, **k: 0)
        mismatches = v1_mismatches()
        assert set(mismatches) == REFUSED
        assert not any(payer_can_sign(mismatches[case] or "", case[1]) for case in REFUSED)

    def test_get_token_amount_truncating_asks_less_than_written(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(base_module, "to_base_units", truncating)
        assert set(base_unit_mismatches(token_amount)) == REFUSED
        assert set(base_unit_mismatches(v2_amount)) == REFUSED
        # 5E-7 with 6 decimals: a price of 0.
        assert base_unit_mismatches(token_amount)[("5E-7", 6)] == "0"

    def test_the_settle_truncating_requires_less_than_written(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(base_module, "to_base_units", truncating)
        monkeypatch.setattr(client_module, "to_base_units", truncating)
        assert set(base_unit_mismatches(settled_amount)) == REFUSED
        refused_at_6 = {case for case in REFUSED if case[1] == 6}
        assert set(base_unit_mismatches(lambda v, d: settled_amount(v, None), (6,))) == refused_at_6

    def test_the_erc8004_helper_truncating_requires_less_than_written(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(erc8004_module, "to_base_units", truncating)
        refused_at_6 = {case for case in REFUSED if case[1] == 6}
        assert set(base_unit_mismatches(lambda v, d: erc8004_amount(v), (6,))) == refused_at_6
