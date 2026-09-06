"""
XRPL settles in XRP, and XRP is not a dollar.

Two defects of `main` that the Casper PR audit surfaced
(`docs/reports/2026-09-05-auditoria-pr2-casper.md`, H2 and the closing
"Recomendación operativa"), both measured against the live facilitator on
2026-09-05:

1. `amount_usd` was converted with the network's decimals, which assumes one
   token unit is worth one dollar. True for every USDC/EURC/AUSD/PYUSD/USDT
   network in the registry; false for XRPL, whose settlement asset is native
   XRP. `Decimal("1.00")` produced 1_000_000 drops = 1 XRP — whatever XRP is
   worth today, it is not the dollar the integrator wrote.
2. The facilitator publishes XRPL mainnet as `xrpl`; the SDK asked for
   `xrpl-mainnet`, a name `/supported` never advertises, so the boot check
   (`verify_routes()`) rejected a network the facilitator settles fine.

The escape hatch in test 4 is not hypothetical: the facilitator settles a
dollar-pegged USDC on XRPL (Circle issuer, verified in
`x402-rs/src/network.rs:1230-1242`), so pricing an XRPL call in dollars IS
possible — it just has to name that token instead of riding on XRP.
"""

from decimal import Decimal

import pytest

from uvd_x402_sdk import X402Client, X402Config
from uvd_x402_sdk.models import PaymentPayload
from uvd_x402_sdk.networks import get_network, get_supported_network_names

# USDC on XRPL as the facilitator advertises it in GET /supported (mainnet),
# byte-identical to x402-rs/src/network.rs:1235. currency=USDC, issuer=Circle.
XRPL_USDC = "5553444300000000000000000000000000000000.rGm7WCVp9gb4jZHWTEtGUr4dd74z2XuWhE"


def _xrpl_payload(network: str = "xrpl") -> PaymentPayload:
    return PaymentPayload.model_validate(
        {
            "x402Version": 1,
            "scheme": "exact",
            "network": network,
            "payload": {"signedTxBlob": "ABC123"},
        }
    )


def _client() -> X402Client:
    return X402Client(
        X402Config(
            recipient_evm="0x" + "11" * 20,
            recipient_xrpl="rfADKkVXBNqK3z72tVSS3LVzAR3psYkonp",
        )
    )


# ---------------------------------------------------------------------------
# 1. The conversion itself refuses to pretend XRP is a dollar
# ---------------------------------------------------------------------------


def test_get_token_amount_refuses_on_xrpl():
    """`$1.00` must not silently become 1 XRP."""
    for name in ("xrpl", "xrpl-testnet"):
        with pytest.raises(ValueError) as exc:
            get_network(name).get_token_amount(1.0)
        # The error has to say what to do, not just that it refused.
        assert "XRP" in str(exc.value)
        assert "asset" in str(exc.value)


def test_get_token_amount_unchanged_on_pegged_networks():
    """Every dollar-pegged network keeps converting exactly as before."""
    assert get_network("base").get_token_amount(1.0) == 1_000_000
    assert get_network("stellar").get_token_amount(1.0) == 10_000_000  # 7 decimals
    assert get_network("ethereum").get_token_amount(10.5) == 10_500_000


# ---------------------------------------------------------------------------
# 2. The real payment path refuses too (this is what charges money)
# ---------------------------------------------------------------------------


def test_build_requirements_refuses_usd_on_xrpl():
    """`_build_payment_requirements` is the path that sets maxAmountRequired."""
    client = _client()
    with pytest.raises(ValueError) as exc:
        client._build_payment_requirements(_xrpl_payload(), Decimal("1.00"))
    assert "XRP" in str(exc.value)


def test_build_requirements_refuses_even_with_token_decimals():
    """`token_decimals` fixes SCALE, not UNIT: 6 decimals of XRP are still XRP."""
    client = _client()
    with pytest.raises(ValueError):
        client._build_payment_requirements(
            _xrpl_payload(), Decimal("1.00"), token_decimals=6
        )


# ---------------------------------------------------------------------------
# 3. Naming an explicit dollar-pegged asset is the way through
# ---------------------------------------------------------------------------


def test_explicit_stablecoin_asset_prices_in_dollars():
    """With XRPL USDC named explicitly, $1.00 is 1_000_000 units of USDC."""
    client = _client()
    reqs = client._build_payment_requirements(
        _xrpl_payload(),
        Decimal("1.00"),
        asset=XRPL_USDC,
        token_decimals=6,
    )
    assert reqs.maxAmountRequired == "1000000"
    assert reqs.asset == XRPL_USDC


# ---------------------------------------------------------------------------
# 4. The network name matches what the facilitator advertises
# ---------------------------------------------------------------------------


def test_xrpl_mainnet_canonical_name_is_xrpl():
    """`/supported` advertises `xrpl`; x402-rs prints `xrpl` (network.rs:189)."""
    mainnet = get_network("xrpl")
    assert mainnet is not None
    assert mainnet.name == "xrpl"
    assert "xrpl" in get_supported_network_names()
    assert "xrpl-mainnet" not in get_supported_network_names()


def test_xrpl_mainnet_alias_still_resolves():
    """`xrpl-mainnet` keeps resolving — x402-rs takes it too (network.rs:251)."""
    assert get_network("xrpl-mainnet") is get_network("xrpl")


def test_xrpl_testnet_name_unchanged():
    """The testnet name already matched the facilitator; it must not move."""
    assert get_network("xrpl-testnet").name == "xrpl-testnet"
