"""The payer signs the amount the settle requires.

0.91.0 made the seller convert a price with ``to_base_units``: float noise
rounds to the nearest base unit, and a real digit below one base unit raises
``ValueError``. The paths that SIGN kept ``int()``, which truncates. With the
same float price on both sides:

* ``Decimal(str(0.3 - 0.1))`` is ``0.19999999999999998``: the payer signed
  199999 base units and the settle required 200000. The facilitator refuses a
  payment below the amount required, and nothing told the payer why.
* ``0.0000015`` at 6 decimals: the payer signed 1 base unit, the settle raised.
* A price without noise, or with noise above the cent, came out the same.

Every path that turns an amount in whole tokens into base units to sign now
converts with the settle's function, tolerance included:
``X402Client.create_authorization()`` (local key or external signer),
``EnvKeyAdapter.sign_eip3009()``, ``OWSWalletAdapter.sign_eip3009()`` and
``build_escrow_pre_auth()``. Noise rounds the same way on both sides, and a
real sub-unit digit raises before anything is signed.

``TestTheMeasuredRows`` runs those rows end to end with ephemeral keys: the
header the payer builds goes through ``settle_payment()`` with the same price,
and the request the facilitator receives carries a signed value equal to its
``maxAmountRequired``. ``TestEveryCentPrice`` runs every cent price from $0.01
to $99.99, computed five ways with floats (products, sums, differences),
through each signing path against the settle's requirements, and a real
sub-unit digit on every cent. ``TestMutations`` puts each old conversion back
and checks it turns red.
"""
from __future__ import annotations

import base64
import json
import sys
import types
from decimal import Decimal
from fractions import Fraction
from functools import cache
from pathlib import Path
from typing import Any, Callable

import httpx
import pytest
from eth_account import Account
from eth_account.messages import encode_typed_data

import uvd_x402_sdk.escrow_signing as escrow_module
from tests.test_escrow_signing import PAYMENT_CONFIG
from uvd_x402_sdk import X402Client, X402Config
from uvd_x402_sdk import client as client_module
from uvd_x402_sdk.escrow_signing import build_escrow_pre_auth
from uvd_x402_sdk.hedera import build_hedera_requirements
from uvd_x402_sdk.models import PaymentPayload
from uvd_x402_sdk.networks import base as base_module
from uvd_x402_sdk.networks import get_network
from uvd_x402_sdk.wallet import EnvKeyAdapter, OWSWalletAdapter

NETWORK = "base"
RECIPIENT = "0x2222222222222222222222222222222222222222"
PAYER = "0x1111111111111111111111111111111111111111"
FACILITATOR = "http://facilitator.invalid"
SIGNATURE = "0x" + "11" * 65
SETTLED = json.loads(
    (
        Path(__file__).parent
        / "fixtures"
        / "facilitator-settle-2.40.0"
        / "settle_success_without_proof.json"
    ).read_text(encoding="utf-8")
)["body"]

TRANSFER = [
    {"name": "from", "type": "address"},
    {"name": "to", "type": "address"},
    {"name": "value", "type": "uint256"},
    {"name": "validAfter", "type": "uint256"},
    {"name": "validBefore", "type": "uint256"},
    {"name": "nonce", "type": "bytes32"},
]

# A signing path takes the price and returns the base units it signed, or None
# when it raised ValueError without signing anything.
Signer = Callable[[Any], "str | None"]


def usdc_domain() -> dict[str, Any]:
    network = get_network(NETWORK)
    assert network is not None
    return {
        "name": network.usdc_domain_name,
        "version": network.usdc_domain_version,
        "chainId": network.chain_id,
        "verifyingContract": network.usdc_address,
    }


# ── the prices ───────────────────────────────────────────────────────────────

# Five ways a payer and a seller compute a cent price with floats: a product,
# differences and sums. `c` is the price in cents.
FLOAT_FORMS: dict[str, Callable[[int], float]] = {
    "c * 0.01": lambda c: c * 0.01,
    "(c + 10) * 0.01 - 0.1": lambda c: (c + 10) * 0.01 - 0.1,
    "c * 0.01 + 0.1 - 0.1": lambda c: c * 0.01 + 0.1 - 0.1,
    "0.1 * (c // 10) + 0.01 * (c % 10)": lambda c: 0.1 * (c // 10) + 0.01 * (c % 10),
    "c * 0.07 - c * 0.06": lambda c: c * 0.07 - c * 0.06,
}
CENTS = range(1, 10_000)  # $0.01 .. $99.99

# float price -> the base units meant (6 decimals)
PRICES: dict[float, int] = {
    form(c): c * 10**4 for form in FLOAT_FORMS.values() for c in CENTS
}
# Read as the SDK reads a float (its decimal form), a hair below or above the cent.
BELOW = sorted(p for p, units in PRICES.items() if Decimal(str(p)) * 10**6 < units)
ABOVE = sorted(p for p, units in PRICES.items() if Decimal(str(p)) * 10**6 > units)
BELOW_SET = set(BELOW)
NOISY = BELOW_SET | set(ABOVE)


def one_per_cent(c: int) -> float:
    """The form of this cent that lands below it if one does, else above it."""
    prices = {form(c) for form in FLOAT_FORMS.values()}
    return min(prices, key=lambda p: (p not in BELOW_SET, p not in NOISY, p))


# Every cent once, for the signing paths that cost more per call. It holds
# every price of BELOW: no cent has two forms below it.
EVERY_CENT = [one_per_cent(c) for c in CENTS]

# A real digit below one base unit on every cent: exact (0.0100005) and as a
# float sum (c * 0.01 + 0.0000015); one of the two per cent for the costly paths.
SUB_UNIT_EXACT: list[Any] = [Decimal(c).scaleb(-2) + Decimal("0.0000005") for c in CENTS]
SUB_UNIT_FLOAT: list[Any] = [c * 0.01 + 0.0000015 for c in CENTS]
SUB_UNIT = SUB_UNIT_EXACT + SUB_UNIT_FLOAT
SUB_UNIT_PER_CENT = [
    SUB_UNIT_EXACT[c - 1] if c % 2 else SUB_UNIT_FLOAT[c - 1] for c in CENTS
]


# ── the settle ───────────────────────────────────────────────────────────────


def v1_payload() -> PaymentPayload:
    return PaymentPayload(
        x402Version=1,
        scheme="exact",
        network=NETWORK,
        payload={"signature": SIGNATURE, "authorization": {}},
    )


def settle_requires(price: Any) -> str | None:
    """``maxAmountRequired`` of the settle for this price, None if it raised."""
    client = X402Client(recipient_address=RECIPIENT)
    try:
        return client._build_payment_requirements(v1_payload(), price).maxAmountRequired
    except ValueError:
        return None


@cache
def settle_table() -> dict[Any, str | None]:
    """What the settle requires for every price of the sweep, computed once
    with the code as it is (the mutations patch the conversion afterwards)."""
    client = X402Client(recipient_address=RECIPIENT)
    payload = v1_payload()
    table: dict[Any, str | None] = {}
    for price in [*PRICES, *SUB_UNIT]:
        try:
            table[price] = client._build_payment_requirements(payload, price).maxAmountRequired
        except ValueError:
            table[price] = None
    return table


class Facilitator:
    """Answers every settle with a recorded success and keeps what it got."""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.sent.append(json.loads(request.content))
        return httpx.Response(200, headers={"content-type": "application/json"}, content=SETTLED)


def seller(facilitator: Facilitator) -> X402Client:
    return X402Client(
        recipient_address=RECIPIENT,
        facilitator_url=FACILITATOR,
        http_client=httpx.Client(transport=httpx.MockTransport(facilitator.handler)),
    )


# ── the signing paths ────────────────────────────────────────────────────────


class Recorder:
    """An external signer (``connect_with_signer``) that keeps each message."""

    address = PAYER

    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []

    def sign_typed_data(
        self, domain: dict[str, Any], types_: dict[str, Any], message: dict[str, Any]
    ) -> str:
        self.messages.append(message)
        return SIGNATURE


class RecordingAccount:
    """Stands in for ``EnvKeyAdapter``'s eth_account key: counts signatures."""

    def __init__(self, address: str) -> None:
        self.address = address
        self.signed = 0

    def sign_message(self, signable: Any) -> Any:
        self.signed += 1
        return types.SimpleNamespace(signature=bytes.fromhex("11" * 65), v=27, r=1, s=1)


class FakeOws:
    """The ``ows`` module's ``sign_eip3009``, keeping what it was asked to sign."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def sign_eip3009(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return types.SimpleNamespace(
            from_address=PAYER, v=27, r="0x" + "22" * 32, s="0x" + "33" * 32, signature=SIGNATURE
        )


class RecordingWallet:
    """A ``WalletAdapter`` for the escrow pre-auth that keeps each typed data."""

    def __init__(self) -> None:
        self.typed: list[dict[str, Any]] = []

    def get_address(self) -> str:
        return PAYER

    def sign_message(self, message: Any) -> Any:
        raise NotImplementedError

    def sign_typed_data(self, typed_data: dict[str, Any]) -> dict[str, Any]:
        self.typed.append(typed_data)
        return {"signature": SIGNATURE, "v": 27, "r": "0x" + "22" * 32, "s": "0x" + "33" * 32}


def via_create_authorization() -> Signer:
    recorder = Recorder()
    client = X402Client(recipient_address=RECIPIENT)
    client.connect_with_signer(recorder, chain_name=NETWORK)

    def sign(price: Any) -> str | None:
        recorder.messages.clear()
        try:
            header = client.create_authorization(RECIPIENT, price)
        except ValueError:
            assert recorder.messages == [], "raised after signing"
            return None
        value = json.loads(base64.b64decode(header))["payload"]["authorization"]["value"]
        # The header says what was signed.
        assert [m["value"] for m in recorder.messages] == [int(value)]
        return str(value)

    return sign


def via_env_key_adapter() -> Signer:
    adapter = EnvKeyAdapter(private_key=Account.create().key.hex())
    account = RecordingAccount(adapter.get_address())
    adapter._account = account

    def sign(price: Any) -> str | None:
        account.signed = 0
        try:
            auth = adapter.sign_eip3009({"to": RECIPIENT, "amount_usdc": price, "network": NETWORK})
        except ValueError:
            assert account.signed == 0, "raised after signing"
            return None
        assert account.signed == 1
        return str(auth["value"])

    return sign


def via_ows_adapter(monkeypatch: pytest.MonkeyPatch) -> Signer:
    ows = FakeOws()
    monkeypatch.setitem(sys.modules, "ows", ows)
    adapter = OWSWalletAdapter(wallet_name="test-wallet")

    def sign(price: Any) -> str | None:
        ows.calls.clear()
        try:
            auth = adapter.sign_eip3009({"to": RECIPIENT, "amount_usdc": price, "network": NETWORK})
        except ValueError:
            assert ows.calls == [], "raised after signing"
            return None
        assert [call["value"] for call in ows.calls] == [auth["value"]]
        return str(auth["value"])

    return sign


def via_escrow_pre_auth() -> Signer:
    wallet = RecordingWallet()

    def sign(price: Any) -> str | None:
        wallet.typed.clear()
        try:
            header = build_escrow_pre_auth(
                PAYMENT_CONFIG, NETWORK, PAYER, RECIPIENT, price, None, wallet
            )
        except ValueError:
            assert wallet.typed == [], "raised after signing"
            return None
        body = json.loads(header)["payload"]
        value = body["authorization"]["value"]
        assert [t["message"]["value"] for t in wallet.typed] == [int(value)]
        assert body["paymentInfo"]["maxAmount"] == value
        return str(value)

    return sign


PATHS = ("create_authorization", "EnvKeyAdapter", "OWSWalletAdapter", "build_escrow_pre_auth")


def signing_path(name: str, monkeypatch: pytest.MonkeyPatch) -> Signer:
    if name == "create_authorization":
        return via_create_authorization()
    if name == "EnvKeyAdapter":
        return via_env_key_adapter()
    if name == "OWSWalletAdapter":
        return via_ows_adapter(monkeypatch)
    return via_escrow_pre_auth()


def float_prices(path: str) -> list[Any]:
    """Every float price for create_authorization, every cent for the others."""
    return list(PRICES) if path == "create_authorization" else EVERY_CENT


def sub_unit_prices(path: str) -> list[Any]:
    return SUB_UNIT if path == "create_authorization" else SUB_UNIT_PER_CENT


def mismatches(sign: Signer, prices: list[Any]) -> dict[Any, tuple[str | None, str | None]]:
    """Every price whose signed amount is not what the settle requires:
    price -> (signed, required), None meaning ValueError."""
    table = settle_table()
    found = {}
    for price in prices:
        signed = sign(price)
        if signed != table[price]:
            found[price] = (signed, table[price])
    return found


# ── the measured rows, end to end ────────────────────────────────────────────

# (id, the price the payer and the seller both compute, base units or None: refused)
ROWS = (
    ("noise-below", 0.3 - 0.1, "200000"),
    ("real-digit", 0.0000015, None),
    ("tier", Decimal("0.20"), "200000"),
    ("noise-above", 35 * 0.01, "350000"),
)


class TestTheMeasuredRows:
    def test_the_rows_carry_what_they_say(self) -> None:
        assert Decimal(str(ROWS[0][1])) == Decimal("0.19999999999999998")
        assert Decimal(str(ROWS[3][1])) == Decimal("0.35000000000000003")

    @pytest.mark.parametrize("row", ROWS, ids=[r[0] for r in ROWS])
    def test_create_authorization_signs_what_settle_payment_requires(
        self, row: tuple[str, Any, str | None]
    ) -> None:
        _, price, expected = row
        key = Account.create()
        buyer = X402Client(recipient_address=RECIPIENT)
        buyer.connect_with_private_key(key.key.hex(), chain_name=NETWORK)
        facilitator = Facilitator()

        if expected is None:
            with pytest.raises(ValueError, match="not a whole number of base units"):
                buyer.create_authorization(RECIPIENT, price)
            with pytest.raises(ValueError, match="not a whole number of base units"):
                seller(facilitator).settle_payment(v1_payload(), price)
            assert facilitator.sent == []
            return

        header = buyer.create_authorization(RECIPIENT, price)
        merchant = seller(facilitator)
        merchant.settle_payment(merchant.extract_payload(header), price)
        (sent,) = facilitator.sent
        authorization = sent["paymentPayload"]["payload"]["authorization"]
        # The facilitator compares these two numbers.
        assert authorization["value"] == sent["paymentRequirements"]["maxAmountRequired"]
        assert authorization["value"] == expected
        # And the signature covers that value.
        message = encode_typed_data(
            usdc_domain(), {"TransferWithAuthorization": TRANSFER}, authorization
        )
        signature = sent["paymentPayload"]["payload"]["signature"]
        assert Account.recover_message(message, signature=signature) == key.address

    @pytest.mark.parametrize("row", ROWS, ids=[r[0] for r in ROWS])
    def test_env_key_adapter_signs_what_the_settle_requires(
        self, row: tuple[str, Any, str | None]
    ) -> None:
        _, price, expected = row
        key = Account.create()
        adapter = EnvKeyAdapter(private_key=key.key.hex())
        params = {"to": RECIPIENT, "amount_usdc": price, "network": NETWORK}
        assert settle_requires(price) == expected
        if expected is None:
            with pytest.raises(ValueError, match="not a whole number of base units"):
                adapter.sign_eip3009(params)
            return
        auth = adapter.sign_eip3009(params)
        assert auth["value"] == expected
        receive = [dict(field) for field in TRANSFER]
        message = encode_typed_data(
            usdc_domain(),
            {"ReceiveWithAuthorization": receive},
            {
                "from": key.address,
                "to": RECIPIENT,
                "value": int(auth["value"]),
                "validAfter": int(auth["valid_after"]),
                "validBefore": int(auth["valid_before"]),
                "nonce": bytes.fromhex(auth["nonce"].removeprefix("0x")),
            },
        )
        assert Account.recover_message(message, signature=auth["signature"]) == key.address

    @pytest.mark.parametrize("row", ROWS, ids=[r[0] for r in ROWS])
    @pytest.mark.parametrize("path", PATHS[1:])
    def test_the_other_signing_paths_sign_what_the_settle_requires(
        self, path: str, row: tuple[str, Any, str | None], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _, price, expected = row
        assert signing_path(path, monkeypatch)(price) == expected == settle_requires(price)


# ── every cent price ─────────────────────────────────────────────────────────


class TestEveryCentPrice:
    def test_the_sweep_has_noise_below_and_above_the_cent(self) -> None:
        """The float forms put the price a hair below the cent (what int()
        truncated) and a hair above it (what int() already got right)."""
        assert len(PRICES) == 24_645
        assert len(BELOW) == 923
        assert len(ABOVE) == 14_177
        assert 0.3 - 0.1 in BELOW_SET and 35 * 0.01 in NOISY
        # One per cent: all 9,999 cents, every price below its cent included.
        assert [PRICES[p] for p in EVERY_CENT] == [c * 10**4 for c in CENTS]
        assert BELOW_SET <= set(EVERY_CENT)
        assert len(NOISY.intersection(EVERY_CENT)) == 923 + 8_843

    def test_the_settle_requires_the_price_meant_for_every_float_price(self) -> None:
        table = settle_table()
        assert {p: table[p] for p, units in PRICES.items() if table[p] != str(units)} == {}

    @pytest.mark.parametrize("path", PATHS)
    def test_every_float_price_is_signed_as_the_settle_requires(
        self, path: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        assert mismatches(signing_path(path, monkeypatch), float_prices(path)) == {}

    @pytest.mark.parametrize("path", PATHS)
    def test_a_real_sub_unit_digit_raises_before_signing(
        self, path: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        table = settle_table()
        assert all(table[price] is None for price in SUB_UNIT)
        assert mismatches(signing_path(path, monkeypatch), sub_unit_prices(path)) == {}


# ── the other routes ─────────────────────────────────────────────────────────


class TestOtherRoutes:
    def test_eurc_rounds_noise_like_the_settle_and_keeps_its_refusals(self) -> None:
        key = Account.create()
        buyer = X402Client(recipient_address=RECIPIENT)
        buyer.connect_with_private_key(key.key.hex(), chain_name="arc")
        header = buyer.create_authorization(RECIPIENT, 0.3 - 0.1, token_type="eurc")
        signed = json.loads(base64.b64decode(header))["payload"]["authorization"]["value"]
        assert signed == "200000"
        for refused in (0.0000015, 0, Decimal("0.0000001"), Decimal("-1"), Decimal("NaN")):
            with pytest.raises(ValueError, match="positive euros"):
                buyer.create_authorization(RECIPIENT, refused, token_type="eurc")

    def test_native_hedera_keeps_its_own_check_on_both_sides(self) -> None:
        """Native Hedera signs the offer's atomic amount, never a converted
        price, and its seller refuses float noise. The payer's check of the
        offer refuses the same price, so both sides still agree."""
        hiero = pytest.importorskip("hiero_sdk_python")
        offer = build_hedera_requirements("hedera:testnet", "0.0.222", "200000")
        buyer = X402Client(recipient_address=RECIPIENT)
        buyer.connect_with_hedera(
            "0.0.111", hiero.PrivateKey.generate_ed25519().to_string_der(), network="hedera:testnet"
        )
        with pytest.raises(ValueError, match="differs from the approved price"):
            buyer.create_authorization("0.0.222", 0.3 - 0.1, x402_version=2, accepted=offer)
        header = buyer.create_authorization(
            "0.0.222", Decimal("0.20"), x402_version=2, accepted=offer
        )
        merchant = X402Client(
            config=X402Config(recipient_hedera="0.0.222", supported_networks=["hedera:testnet"])
        )
        payment = merchant.extract_payload(header)
        required = merchant._build_payment_requirements(payment, Decimal("0.20"))
        assert required.maxAmountRequired == "200000"
        with pytest.raises(ValueError, match="at most 6 decimal places"):
            merchant._build_payment_requirements(payment, Decimal(str(0.3 - 0.1)))


# ── the mutations: each old conversion put back turns its test red ──────────


def truncating(amount: Any, decimals: int, **_: Any) -> int:
    """The signing conversion before this fix: ``int(atomic)``."""
    return int(Decimal(str(amount)) * 10**decimals)


def rounding_everything(amount: Any, decimals: int, **_: Any) -> int:
    """A conversion that rounds with no refusal: the real digit gets signed."""
    return round(Fraction(Decimal(str(amount))) * 10**decimals)


class TestMutations:
    @pytest.mark.parametrize(
        "path,module",
        [
            ("create_authorization", client_module),
            ("EnvKeyAdapter", base_module),
            ("OWSWalletAdapter", base_module),
            ("build_escrow_pre_auth", escrow_module),
        ],
    )
    def test_int_back_in_a_signing_path_signs_below_the_settle(
        self, path: str, module: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        settle_table()  # computed with the real conversion first
        sign = signing_path(path, monkeypatch)
        monkeypatch.setattr(module, "to_base_units", truncating)
        wrong = mismatches(sign, float_prices(path))
        # Exactly the prices a hair below the cent, one base unit short.
        assert sorted(wrong) == BELOW
        short = {int(str(required)) - int(str(signed)) for signed, required in wrong.values()}
        assert short == {1}
        assert wrong[0.3 - 0.1] == ("199999", "200000")
        # And every real sub-unit digit is signed where the settle raises.
        refused = mismatches(sign, sub_unit_prices(path))
        assert set(refused) == set(sub_unit_prices(path))
        assert all(required is None and signed is not None for signed, required in refused.values())

    @pytest.mark.parametrize("path", PATHS)
    def test_a_signer_without_the_tolerance_refuses_what_the_settle_rounds(
        self, path: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        settle_table()
        sign = signing_path(path, monkeypatch)
        monkeypatch.setattr(base_module, "_FLOAT_NOISE_BASE_UNITS", Fraction(0))
        monkeypatch.setattr(base_module, "_FLOAT_NOISE_RELATIVE", Fraction(0))
        wrong = mismatches(sign, float_prices(path))
        assert set(wrong) == NOISY.intersection(float_prices(path))
        assert all(signed is None for signed, _ in wrong.values())

    def test_a_signer_that_rounds_everything_signs_the_real_digit(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        settle_table()
        sign = signing_path("create_authorization", monkeypatch)
        monkeypatch.setattr(client_module, "to_base_units", rounding_everything)
        assert mismatches(sign, list(PRICES)) == {}
        refused = mismatches(sign, SUB_UNIT)
        assert set(refused) == set(SUB_UNIT)
