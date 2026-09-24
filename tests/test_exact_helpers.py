"""The public conversion helpers of Sui, XRPL and Stellar convert like the settle.

``format_sui_amount()``, ``xrp_to_drops()`` and ``usd_to_stroops()`` scaled a
binary float and truncated: ``int(2.01 * 10**6)`` is 2009999. A payer signing
with them signed one base unit less than the settle requires on 151 of the
9,999 cent prices at 6 decimals (Sui, XRPL) and 636 at 7 (Stellar). They now
convert with ``to_base_units()``: float noise rounds to the nearest base unit,
and a real digit below one base unit raises ``ValueError``.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any, Callable

import pytest

from uvd_x402_sdk.networks import stellar as stellar_module
from uvd_x402_sdk.networks import sui as sui_module
from uvd_x402_sdk.networks import xrpl as xrpl_module
from uvd_x402_sdk.networks.base import to_base_units

CENTS = range(1, 10_000)  # $0.01 .. $99.99

# name -> (helper, decimals, module, number of cents int(x * 10**d) gets wrong)
HELPERS: dict[str, tuple[Callable[[Any], int], int, Any, int]] = {
    "format_sui_amount": (sui_module.format_sui_amount, 6, sui_module, 151),
    "xrp_to_drops": (xrpl_module.xrp_to_drops, 6, xrpl_module, 151),
    "usd_to_stroops": (stellar_module.usd_to_stroops, 7, stellar_module, 636),
}


def short_cents(helper: Callable[[Any], int], decimals: int) -> list[int]:
    """Every cent literal (``c / 100``) the helper does not convert to the cent."""
    return [c for c in CENTS if helper(c / 100) != c * 10 ** (decimals - 2)]


def truncating(amount: Any, decimals: int, **_: Any) -> int:
    """The helpers before this change: ``int(x * 10**d)`` over the float."""
    return int(amount * (10**decimals))


@pytest.mark.parametrize("name", HELPERS)
class TestHelpers:
    def test_every_cent_converts_to_the_cent(self, name: str) -> None:
        helper, decimals, _, _ = HELPERS[name]
        assert short_cents(helper, decimals) == []

    def test_float_noise_rounds_as_the_settle(self, name: str) -> None:
        helper, decimals, _, _ = HELPERS[name]
        for price in (0.3 - 0.1, 35 * 0.01, 2.01, Decimal("2.010"), "2.01"):
            assert helper(price) == to_base_units(price, decimals)

    def test_a_real_sub_unit_digit_raises(self, name: str) -> None:
        helper, decimals, _, _ = HELPERS[name]
        sub_unit = Decimal(1) + Decimal(5).scaleb(-(decimals + 1))  # 1.0000005 at 6
        for value in (sub_unit, float(sub_unit), str(sub_unit), -1, float("nan")):
            with pytest.raises(ValueError):
                helper(value)

    def test_int_back_leaves_cents_short(self, name: str, monkeypatch: pytest.MonkeyPatch) -> None:
        helper, decimals, module, wrong = HELPERS[name]
        monkeypatch.setattr(module, "to_base_units", truncating)
        short = short_cents(helper, decimals)
        assert len(short) == wrong
        assert all(helper(c / 100) == c * 10 ** (decimals - 2) - 1 for c in short)


def test_the_documented_xrp_example() -> None:
    assert xrpl_module.xrp_to_drops(1.5) == 1_500_000
    assert xrpl_module.drops_to_xrp(1_000_000) == 1.0
    with pytest.raises(ValueError, match="XRP"):
        xrpl_module.xrp_to_drops(1.0000005)
