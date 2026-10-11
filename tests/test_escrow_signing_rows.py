"""``build_escrow_pre_auth`` signs only what this SDK's tables can check.

An escrow pre-auth commits to a chain id, an escrow, a token collector, a USDC
and its EIP-712 domain, an operator and a fee ceiling. Each one is checked
against a row of the SDK's tables (``tests/test_escrow_signing_registry.py``).
Since 0.97.0 a chain that lacks a row in any of them (``ESCROW_CONTRACTS``,
``ESCROW_OPERATORS``, ``ESCROW_MAX_FEE_BPS``, ``VERIFIED_USDC_DOMAINS``) is
refused before anything is signed, and so is a network name the SDK does not
register, whose chain id could not be checked. Until 0.96.0 both were signed
with a logged warning.

Execution Market's ten escrow networks (``tests/fixtures/em-escrow-networks.json``,
recorded by ``scripts/em_escrow_networks.py`` from EM's own registry, never by
hand) all sign, with the escrow block EM serves.

The windows of a tier come from the payment config as well. Each is an integer
of seconds within ``ESCROW_MAX_TIER_WINDOWS``, and the signed
``authorizationExpiry`` is at most ``ESCROW_MAX_AUTHORIZATION_SEC`` after
signing. The same release checks ``min_fee_bps`` against the operator's fee,
takes ``deadline`` only as whole seconds, and signs the domain it checked.
``TestGuardMutations`` removes each new guard from a copy of the module's
source and checks that the case it refuses is then signed.
"""

from __future__ import annotations

import json
import math
import time
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest import mock

import pytest

import uvd_x402_sdk.escrow_signing as es
from tests.test_escrow_signing_registry import (
    EM_ESCROW_NETWORKS,
    EXECUTION_MARKET_ESCROW,
    EXECUTION_MARKET_NETWORKS,
    PAYER,
    WORKER,
    _Wallet,
    config,
    table_block,
)
from tests.test_escrow_vectors import mutant
from uvd_x402_sdk.advanced_escrow import TIER_TIMINGS
from uvd_x402_sdk.escrow_contracts import ESCROW_CONTRACTS, ESCROW_MAX_FEE_BPS, ESCROW_OPERATORS
from uvd_x402_sdk.escrow_signing import (
    ESCROW_MAX_AUTHORIZATION_SEC,
    ESCROW_MAX_TIER_WINDOWS,
    ESCROW_TIER_WINDOWS,
    OPERATOR_FEE_BPS,
    REFUND_WINDOW_SEC,
    REVIEW_WINDOW_SEC,
    VERIFIED_USDC_DOMAINS,
)
from uvd_x402_sdk.networks import get_network

NOW = 1_790_000_000
# Inputs and the verdict both SDKs give them; run here and through the published
# TypeScript twin (the PR that added it lists the run).
CASES = json.loads(
    (Path(__file__).resolve().parent / "fixtures" / "escrow-preauth-input-cases.json").read_text(
        encoding="utf-8"
    )
)
HOUR = 3600
DAY = 24 * HOUR
# The longest task deadline Execution Market accepts: deadline_hours le=720.
EM_MAX_DEADLINE_SEC = 720 * HOUR

# Networks of the SDK's registry that have no escrow deployment it knows.
WITHOUT_ROWS = {"hyperevm": 999, "unichain": 130, "scroll": 534352, "robinhood": 4663}
# Rows in ESCROW_CONTRACTS only: no operator, no fee ceiling, no verified domain.
TESTNETS = {"base-sepolia": 84532, "ethereum-sepolia": 11155111}
TABLES = ("ESCROW_CONTRACTS", "ESCROW_OPERATORS", "ESCROW_MAX_FEE_BPS", "VERIFIED_USDC_DOMAINS")


def _table(module: Any, name: str) -> dict[int, Any]:
    """The table as ``module`` reads it (a mutant has its own VERIFIED_USDC_DOMAINS)."""
    return getattr(module, name)


def build(
    network: str,
    block: dict[str, Any],
    module: Any = es,
    *,
    deadline: int | None = None,
    tier: str = "micro",
    **escrow: Any,
) -> tuple[dict[str, Any], _Wallet]:
    wallet = _Wallet()
    with mock.patch.object(time, "time", return_value=NOW):
        header = module.build_escrow_pre_auth(
            config(network, block, **escrow), network, PAYER, WORKER, "0.25", deadline, wallet,
            tier=tier,
        )
    return json.loads(header), wallet


def refused(
    network: str,
    block: dict[str, Any],
    match: str,
    module: Any = es,
    *,
    deadline: int | None = None,
    tier: str = "micro",
    **escrow: Any,
) -> None:
    wallet = _Wallet()
    with pytest.raises(ValueError, match=match), mock.patch.object(time, "time", return_value=NOW):
        module.build_escrow_pre_auth(
            config(network, block, **escrow), network, PAYER, WORKER, "0.25", deadline, wallet,
            tier=tier,
        )
    assert wallet.signed == [], "nothing may be signed before the refusal"


def foreign_block(network: str, addresses: str) -> dict[str, Any]:
    """A complete config for a chain without rows: its own USDC, and some contracts.

    ``addresses`` is ``"synthetic"`` or ``"base"``: Base's escrow, collector and
    operator, as if the same deployment existed on that chain.
    """
    net = get_network(network)
    if addresses == "base":
        base = table_block(8453)
        contracts = {k: base[k] for k in ("operator", "escrow", "token_collector")}
    else:
        contracts = {
            "operator": "0x" + "0e" * 20,
            "escrow": "0x" + "0c" * 20,
            "token_collector": "0x" + "0d" * 20,
        }
    return {
        "chain_id": net.chain_id,
        **contracts,
        "usdc": net.usdc_address,
        "usdc_domain_name": net.usdc_domain_name,
        "usdc_domain_version": net.usdc_domain_version,
    }


# ── chains without a row ─────────────────────────────────────────────────────


@pytest.mark.parametrize("network", sorted(WITHOUT_ROWS))
def test_the_chain_is_registered_and_has_no_row_in_any_table(network):
    """The refusal below is about the rows, not about an unknown name."""
    assert get_network(network).chain_id == WITHOUT_ROWS[network]
    for name in TABLES:
        assert WITHOUT_ROWS[network] not in _table(es, name), name


@pytest.mark.parametrize("addresses", ["synthetic", "base"])
@pytest.mark.parametrize("network", sorted(WITHOUT_ROWS))
def test_a_registered_chain_without_rows_is_refused(network, addresses):
    refused(network, foreign_block(network, addresses), "no escrow row for chain")


@pytest.mark.parametrize("spelling", ["eip155:{}", "EIP155:{}", " eip155:{} "])
@pytest.mark.parametrize("network", sorted(WITHOUT_ROWS))
def test_the_same_chain_named_by_its_chain_id_is_refused(network, spelling):
    block = foreign_block(network, "synthetic")
    refused(spelling.format(WITHOUT_ROWS[network]), block, "no escrow row for chain")


@pytest.mark.parametrize("network", sorted(WITHOUT_ROWS))
def test_the_same_chain_under_a_name_the_sdk_does_not_register_is_refused(network):
    refused("red-nueva", foreign_block(network, "synthetic"), "not a network this SDK registers")


@pytest.mark.parametrize("network", sorted(WITHOUT_ROWS))
def test_the_refusal_names_every_missing_table(network):
    with pytest.raises(ValueError) as refusal:
        build(network, foreign_block(network, "synthetic"))
    for name in TABLES:
        assert name in str(refusal.value)


@pytest.mark.parametrize("network", sorted(TESTNETS))
def test_a_testnet_with_only_its_escrow_contracts_is_refused(network):
    chain_id = TESTNETS[network]
    assert chain_id in ESCROW_CONTRACTS
    block = table_block(chain_id)
    refused(f"eip155:{chain_id}", block, "no escrow row for chain .*ESCROW_OPERATORS")
    # Its name is not one of the SDK's network registry.
    assert get_network(network) is None
    refused(network, block, "not a network this SDK registers")


@pytest.mark.parametrize("name", TABLES)
def test_a_chain_missing_one_row_is_refused_naming_that_table(name):
    """Base with every row but one: still refused, nothing signed."""
    block = table_block(8453)
    with mock.patch.dict(_table(es, name)):
        del _table(es, name)[8453]
        refused("base", block, f"no escrow row for chain 8453 .*{name}")


def test_every_chain_with_an_operator_row_has_every_row():
    """So every chain the tables were written for is signed; the rest are refused."""
    chains = set(ESCROW_OPERATORS)
    assert chains == set(ESCROW_MAX_FEE_BPS) == set(VERIFIED_USDC_DOMAINS)
    assert chains <= set(ESCROW_CONTRACTS)
    assert set(ESCROW_CONTRACTS) - chains == set(TESTNETS.values())


# ── a network name the SDK does not register ─────────────────────────────────


@pytest.mark.parametrize(
    "network",
    ["red-nueva", "base-mainnet-2", "Base Sepolia", "", "  ", "8453", "base:8453"],
    ids=["new", "suffix", "spaced", "empty", "blank", "bare-id", "other-prefix"],
)
def test_a_name_the_sdk_does_not_register_is_refused_even_with_the_table(network):
    """Base's own rows, under a name whose chain id the SDK cannot check: refused."""
    refused(network, table_block(8453), "not a network this SDK registers")


# ── Execution Market's escrow networks sign ──────────────────────────────────


def test_the_fixture_is_execution_markets_ten_escrow_networks():
    assert EM_ESCROW_NETWORKS["source"]["repo"] == "UltravioletaDAO/execution-market"
    assert len(EM_ESCROW_NETWORKS["source"]["commit"]) == 40
    assert sorted(EXECUTION_MARKET_NETWORKS) == [
        "arbitrum", "arc", "avalanche", "base", "celo",
        "ethereum", "monad", "optimism", "polygon", "skale",
    ]


@pytest.mark.parametrize("deadline", [None, EM_MAX_DEADLINE_SEC], ids=["no-deadline", "720h"])
@pytest.mark.parametrize("tier", sorted(EXECUTION_MARKET_ESCROW["tier_timings"]))
@pytest.mark.parametrize("network", sorted(EXECUTION_MARKET_NETWORKS))
def test_execution_markets_networks_sign(network, tier, deadline):
    block = EXECUTION_MARKET_NETWORKS[network]
    escrow = dict(EXECUTION_MARKET_ESCROW)
    wrapper, wallet = build(
        network, dict(block), tier=tier, deadline=None if deadline is None else NOW + deadline,
        **escrow,
    )
    assert len(wallet.signed) == 1
    payment_info = wrapper["payload"]["paymentInfo"]
    assert payment_info["operator"] == block["operator"]
    assert wrapper["paymentRequirements"]["network"] == f"eip155:{block['chain_id']}"
    assert payment_info["authorizationExpiry"] - NOW <= ESCROW_MAX_AUTHORIZATION_SEC


# ── the windows of a tier ────────────────────────────────────────────────────


def test_the_window_ceilings_are_the_longest_tier_of_the_sdk():
    for key, ceiling in ESCROW_MAX_TIER_WINDOWS.items():
        assert ceiling == max(int(t[key]) for t in TIER_TIMINGS.values()), key
    assert set(ESCROW_MAX_TIER_WINDOWS) == {"pre", "auth", "refund"}
    # The config cannot push authorizationExpiry past the review floor.
    assert ESCROW_MAX_TIER_WINDOWS["auth"] == REVIEW_WINDOW_SEC


def test_the_authorization_ceiling_is_the_longest_deadline_plus_the_review_window():
    assert ESCROW_MAX_AUTHORIZATION_SEC == EM_MAX_DEADLINE_SEC + REVIEW_WINDOW_SEC
    # The TypeScript twin's ESCROW_MAX_AUTHORIZATION_SEC (uvd-x402-sdk 2.104.0).
    assert ESCROW_MAX_AUTHORIZATION_SEC == 3_196_800


def test_the_sdk_and_execution_market_windows_are_within_the_ceilings():
    served = EXECUTION_MARKET_ESCROW["tier_timings"].values()
    for windows in (*ESCROW_TIER_WINDOWS.values(), *served):
        for key, ceiling in ESCROW_MAX_TIER_WINDOWS.items():
            assert 0 <= windows[key] <= ceiling


def _tier(**override: Any) -> dict[str, Any]:
    windows: dict[str, Any] = dict(EXECUTION_MARKET_ESCROW["tier_timings"]["micro"])
    windows.update(override)
    return {"tier_timings": {"micro": windows}}


@pytest.mark.parametrize("key", sorted(ESCROW_MAX_TIER_WINDOWS))
def test_a_window_at_its_ceiling_is_signed(key):
    ceiling = ESCROW_MAX_TIER_WINDOWS[key]
    wrapper, wallet = build("base", table_block(8453), **_tier(**{key: ceiling}))
    assert len(wallet.signed) == 1
    payment_info = wrapper["payload"]["paymentInfo"]
    if key == "pre":
        assert payment_info["preApprovalExpiry"] == NOW + ceiling
        assert wrapper["payload"]["authorization"]["validBefore"] == str(NOW + ceiling)


@pytest.mark.parametrize("key", sorted(ESCROW_MAX_TIER_WINDOWS))
def test_a_window_above_its_ceiling_is_refused(key):
    above = ESCROW_MAX_TIER_WINDOWS[key] + 1
    refused(
        "base", table_block(8453), f"'{key}' window of {above} s, above this SDK's ceiling",
        **_tier(**{key: above}),
    )


@pytest.mark.parametrize(
    "value",
    [-1, True, "-1", " 3600", "3600.0", "+3600", None, 3600.5, math.inf, math.nan, [3600], 2**256],
    ids=[
        "negative", "bool", "signed-string", "padded-string", "fraction-string", "plus-string",
        "none", "fraction", "inf", "nan", "list", "huge",
    ],
)
@pytest.mark.parametrize("key", sorted(ESCROW_MAX_TIER_WINDOWS))
def test_a_window_that_is_not_a_whole_number_of_seconds_in_range_is_refused(key, value):
    refused("base", table_block(8453), f"tier 'micro' .*'{key}'", **_tier(**{key: value}))


@pytest.mark.parametrize("key", sorted(ESCROW_MAX_TIER_WINDOWS))
def test_a_window_written_in_ascii_digits_is_the_integer(key):
    """As the twin reads it (``configInteger``): "86400" is 86400."""
    ceiling = ESCROW_MAX_TIER_WINDOWS[key]
    wrapper, wallet = build("base", table_block(8453), **_tier(**{key: str(ceiling)}))
    assert len(wallet.signed) == 1
    refused("base", table_block(8453), f"'{key}' window of {ceiling + 1} s",
            **_tier(**{key: str(ceiling + 1)}))


@pytest.mark.parametrize("key", sorted(ESCROW_MAX_TIER_WINDOWS))
def test_a_missing_window_is_refused(key):
    windows = dict(EXECUTION_MARKET_ESCROW["tier_timings"]["micro"])
    del windows[key]
    refused(
        "base", table_block(8453), f"tier 'micro' .*'{key}'", tier_timings={"micro": windows}
    )


def test_a_window_written_as_a_whole_float_is_the_integer():
    """What JSON.parse gives the TypeScript twin for 3600.0: the integer 3600."""
    wrapper, _ = build("base", table_block(8453), **_tier(pre=3600.0))
    assert wrapper["payload"]["paymentInfo"]["preApprovalExpiry"] == NOW + 3600
    assert isinstance(wrapper["payload"]["paymentInfo"]["preApprovalExpiry"], int)


def test_a_tier_that_is_not_an_object_is_refused():
    refused("base", table_block(8453), "tier 'micro'", tier_timings={"micro": [3600, 7200, 86400]})


def test_the_sdk_fallback_windows_are_checked_too():
    """A tier only the SDK defines goes through the same checks."""
    with mock.patch.dict(ESCROW_TIER_WINDOWS, {"long": {"pre": 1, "auth": 1, "refund": 10**9}}):
        refused("base", table_block(8453), "'refund' window", tier="long")


# ── authorizationExpiry ──────────────────────────────────────────────────────


def test_the_longest_deadline_execution_market_accepts_is_signed():
    wrapper, wallet = build("base", table_block(8453), deadline=NOW + EM_MAX_DEADLINE_SEC)
    assert len(wallet.signed) == 1
    payment_info = wrapper["payload"]["paymentInfo"]
    assert payment_info["authorizationExpiry"] == NOW + EM_MAX_DEADLINE_SEC + REVIEW_WINDOW_SEC
    assert payment_info["authorizationExpiry"] == NOW + ESCROW_MAX_AUTHORIZATION_SEC
    assert payment_info["refundExpiry"] == (
        NOW + EM_MAX_DEADLINE_SEC + REVIEW_WINDOW_SEC + REFUND_WINDOW_SEC
    )


@pytest.mark.parametrize(
    "beyond",
    [1, 60, HOUR - 1, HOUR, DAY, 365 * DAY, 2**47],
    ids=["1s", "1min", "59m59s", "1h", "1d", "1y", "2**47"],
)
def test_a_deadline_that_puts_authorization_expiry_past_the_ceiling_is_refused(beyond):
    """From 720 h + 1 s on: the twin refuses the same seconds (no clock slack in either)."""
    refused(
        "base", table_block(8453), "authorizationExpiry .* above this SDK's ceiling",
        deadline=NOW + EM_MAX_DEADLINE_SEC + beyond,
    )


def test_a_deadline_written_as_a_whole_float_is_the_integer():
    wrapper, _ = build("base", table_block(8453), deadline=float(NOW + DAY))
    expiry = wrapper["payload"]["paymentInfo"]["authorizationExpiry"]
    assert expiry == NOW + DAY + REVIEW_WINDOW_SEC
    assert isinstance(expiry, int)


@pytest.mark.parametrize(
    "deadline",
    [
        NOW + DAY + 0.5, Decimal(NOW + DAY), str(NOW + DAY), True, math.inf, math.nan,
        2**53, -(2**53), float(2**53),
    ],
    ids=[
        "fraction", "decimal", "string", "bool", "inf", "nan",
        "past-safe-integer", "below-safe-integer", "float-past-safe-integer",
    ],
)
def test_a_deadline_that_is_not_whole_epoch_seconds_is_refused(deadline):
    """A fractional expiry would go on the wire while the nonce hashes it truncated."""
    refused("base", table_block(8453), "is not whole epoch seconds", deadline=deadline)


# ── minFeeBps ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("min_fee_bps", [0, 1, OPERATOR_FEE_BPS, str(OPERATOR_FEE_BPS)])
def test_a_fee_floor_up_to_the_operators_fee_is_signed(min_fee_bps):
    wrapper, wallet = build("base", table_block(8453), min_fee_bps=min_fee_bps)
    assert len(wallet.signed) == 1
    assert wrapper["payload"]["paymentInfo"]["minFeeBps"] == int(min_fee_bps)


@pytest.mark.parametrize("min_fee_bps", [OPERATOR_FEE_BPS + 1, 1800, 65_535])
def test_a_fee_floor_above_the_operators_fee_is_refused(min_fee_bps):
    """The capture takes 1300 bps; a floor above it makes every capture revert."""
    refused(
        "base", table_block(8453), f"minFeeBps={min_fee_bps} must be between 0 and",
        min_fee_bps=min_fee_bps,
    )


@pytest.mark.parametrize("key", ["min_fee_bps", "max_fee_bps"])
@pytest.mark.parametrize(
    "value",
    [-1, 65_536, 0.5, 1800.5, "1800.5", " 1800", "+1800", "1_800", "0x708", True, [1800], {}],
    ids=[
        "negative", "above-uint16", "half", "fraction", "fraction-string", "padded", "plus",
        "underscore", "hex", "bool", "list", "dict",
    ],
)
def test_a_fee_bound_that_is_not_a_uint16_integer_is_refused(key, value):
    """ValueError, never a TypeError or a truncated fraction (the twin's feeBps)."""
    refused("base", table_block(8453), f"Invalid {key} ", **{key: value})


@pytest.mark.parametrize("key,default", [("min_fee_bps", 0), ("max_fee_bps", 1800)])
def test_a_null_fee_bound_is_its_default(key, default):
    wrapper, _ = build("base", table_block(8453), **{key: None})
    camel = {"min_fee_bps": "minFeeBps", "max_fee_bps": "maxFeeBps"}[key]
    assert wrapper["payload"]["paymentInfo"][camel] == default


# ── chain_id: an integer, as the twin reads it ───────────────────────────────


@pytest.mark.parametrize(
    "chain_id",
    [" 8453", "8453 ", "+8453", "8_453", "\uff18\uff14\uff15\uff13", 8453.5, 8453.999, "8453.0",
     "0x2105", -8453, 0.5, 2**53, math.inf, math.nan, [8453], {"id": 8453}],
    ids=[
        "lead-space", "trail-space", "plus", "underscore", "fullwidth", "fraction", "fraction-999",
        "fraction-string", "hex", "negative", "half", "past-safe-integer", "inf", "nan", "list",
        "dict",
    ],
)
def test_a_chain_id_that_is_not_an_integer_is_refused(chain_id):
    """``int()`` took the first nine (8453); the twin refuses them, and so does this."""
    block = table_block(8453)
    block["chain_id"] = chain_id
    refused("base", block, "Invalid chain_id")


@pytest.mark.parametrize("chain_id", [8453, "8453", "08453", 8453.0])
def test_a_chain_id_written_as_digits_or_a_whole_float_is_the_integer(chain_id):
    block = table_block(8453)
    block["chain_id"] = chain_id
    wrapper, wallet = build("base", block)
    assert wallet.signed[0]["domain"]["chainId"] == 8453
    assert wrapper["paymentRequirements"]["network"] == "eip155:8453"


# ── the case table shared with the TypeScript twin ───────────────────────────


def _case_config(case: dict[str, Any]) -> tuple[dict[str, Any], int | None]:
    """The case's input, in the Python shape (fees and tier_timings in the escrow block)."""
    base = CASES["base_config"]
    block = {k: v for k, v in base.items() if k != "collector"}
    block["token_collector"] = base["collector"]
    tier_timings = json.loads(json.dumps(CASES["tier_timings"]))
    escrow: dict[str, Any] = {"tier_timings": tier_timings, "min_fee_bps": 0, "max_fee_bps": 1800}
    field, value, deadline = case["field"], case["value"], None
    if field == "chain_id":
        block["chain_id"] = value
    elif field in ("min_fee_bps", "max_fee_bps"):
        escrow[field] = value
    elif field.startswith("window."):
        tier_timings["micro"][field[len("window."):]] = value
    elif field == "tier":
        tier_timings["micro"] = value
    elif field == "deadline":
        deadline = value
    return {"block": block, "escrow": escrow}, deadline


@pytest.mark.parametrize("case", CASES["cases"], ids=[c["id"] for c in CASES["cases"]])
def test_the_case_table_shared_with_the_typescript_twin(case):
    built, deadline = _case_config(case)
    wallet = _Wallet()
    payment_config = config(CASES["base_network"], built["block"], **built["escrow"])
    with mock.patch.object(time, "time", return_value=CASES["now"]):
        try:
            header = es.build_escrow_pre_auth(
                payment_config, CASES["base_network"], PAYER, WORKER, "0.25", deadline, wallet
            )
        except ValueError:
            assert case["expect"] == "refuse", case
            assert wallet.signed == []
            return
    assert case["expect"] == "sign", case
    if "signed" in case:
        payment_info = json.loads(header)["payload"]["paymentInfo"]
        signed = {
            "chain_id": wallet.signed[0]["domain"]["chainId"],
            "min_fee_bps": payment_info["minFeeBps"],
            "max_fee_bps": payment_info["maxFeeBps"],
            "window.pre": payment_info["preApprovalExpiry"] - CASES["now"],
        }[case["field"]]
        assert signed == case["signed"]


# ── the domain signed is the domain checked ──────────────────────────────────


def test_a_domain_version_written_as_a_number_is_signed_as_the_checked_string():
    """Checked as "2"; signed as 2 it would be another digest."""
    block = table_block(8453)
    block["usdc_domain_version"] = 2
    _, wallet = build("base", block)
    [typed] = wallet.signed
    assert typed["domain"]["name"] == "USD Coin"
    assert typed["domain"]["version"] == "2"


def test_the_domain_name_signed_is_the_string_the_check_compared():
    """Any value whose str() is the verified name is signed as that str, not as itself."""

    class Named:
        def __str__(self) -> str:
            return "USD Coin"

    block = table_block(8453)
    block["usdc_domain_name"] = Named()
    _, wallet = build("base", block)
    name = wallet.signed[0]["domain"]["name"]
    assert type(name) is str and name == "USD Coin"


def test_a_domain_version_written_as_a_float_is_another_version():
    block = table_block(8453)
    block["usdc_domain_version"] = 2.0
    refused("base", block, "EIP-712 domain mismatch .*'2.0'")


def test_a_deadline_in_the_past_signs_from_now():
    wrapper, _ = build("base", table_block(8453), deadline=NOW - DAY)
    assert wrapper["payload"]["paymentInfo"]["authorizationExpiry"] == NOW + REVIEW_WINDOW_SEC


# ── each new guard removed: its refusal becomes a signature ─────────────────


def _with_chain_id(chain_id: Any) -> dict[str, Any]:
    block = table_block(8453)
    block["chain_id"] = chain_id
    return block


def _rows_case(name: str) -> Any:
    """Drop Base's row from one table (as the module reads it), sign Base."""
    def run(module: Any) -> Any:
        block = table_block(8453)
        with mock.patch.dict(_table(module, name)):
            del _table(module, name)[8453]
            return build("base", block, module)
    return run


GUARD_MUTATIONS: dict[str, tuple[str, str, Any]] = {
    "unregistered-network": (
        "    if registered is None:\n",
        "    if False:\n",
        lambda module: build("red-nueva", table_block(8453), module),
    ),
    "no-row-at-all": (
        "    if absent:\n",
        "    if False:\n",
        lambda module: build("hyperevm", foreign_block("hyperevm", "synthetic"), module),
    ),
    **{
        f"row:{name}": (f'    "{name}",  #', "    #", _rows_case(name))
        for name in TABLES
    },
    "window-ceiling": (
        "        if value > ceiling:\n",
        "        if False:\n",
        lambda module: build("base", table_block(8453), module, **_tier(pre=10**6)),
    ),
    "window-type": (
        "        if value is None or value < 0:\n",
        "        if False:\n",
        lambda module: build("base", table_block(8453), module, **_tier(pre=-1)),
    ),
    "authorization-ceiling": (
        "    if auth_expiry - now > ESCROW_MAX_AUTHORIZATION_SEC:\n",
        "    if False:\n",
        lambda module: build(
            "base", table_block(8453), module,
            deadline=NOW + EM_MAX_DEADLINE_SEC + 1,
        ),
    ),
    "min-fee-floor": (
        "    if not 0 <= min_fee_bps <= OPERATOR_FEE_BPS:\n",
        "    if False:\n",
        lambda module: build("base", table_block(8453), module, min_fee_bps=OPERATOR_FEE_BPS + 1),
    ),
    "ascii-digits-only": (
        "    if isinstance(value, str) and _ASCII_DIGITS.fullmatch(value):\n",
        "    if isinstance(value, str):\n",
        lambda module: build("base", _with_chain_id(" 8453"), module),
    ),
    "no-truncated-fraction": (
        "        return int(value) if value.is_integer() else None\n",
        "        return int(value)\n",
        lambda module: build("base", _with_chain_id(8453.5), module),
    ),
    "config-tier-not-replaced": (
        "    if timings is None:\n        timings = ESCROW_TIER_WINDOWS.get(tier)\n",
        "    if not timings:\n        timings = ESCROW_TIER_WINDOWS.get(tier)\n",
        lambda module: build("base", table_block(8453), module, tier_timings={"micro": {}}),
    ),
    "deadline-type": (
        "    if deadline is not None and not whole:\n",
        "    if False:\n",
        lambda module: build("base", table_block(8453), module, deadline=True),
    ),
}


class TestGuardMutations:
    @pytest.mark.parametrize("guard", sorted(GUARD_MUTATIONS))
    def test_the_unmutated_source_refuses_the_case(self, guard):
        _, _, case = GUARD_MUTATIONS[guard]
        with pytest.raises(ValueError):
            case(es)

    @pytest.mark.parametrize("guard", sorted(GUARD_MUTATIONS))
    def test_without_the_guard_the_case_is_signed(self, guard):
        old, new, case = GUARD_MUTATIONS[guard]
        _, wallet = case(mutant(old, new))
        assert len(wallet.signed) == 1

    def test_without_the_window_ceiling_a_long_auth_window_is_still_refused(self):
        """The ``auth`` window has a second guard: the authorizationExpiry ceiling."""
        module = mutant("        if value > ceiling:\n", "        if False:\n")
        refused(
            "base", table_block(8453), "authorizationExpiry .* above this SDK's ceiling", module,
            **_tier(auth=10**9),
        )

    def test_signing_the_raw_config_version_signs_another_value_than_the_checked_one(self):
        module = mutant(
            '            "version": _afirmado[1],\n',
            '            "version": net["usdc_domain_version"],\n',
        )
        block = table_block(8453)
        block["usdc_domain_version"] = 2
        _, wallet = build("base", block, module)
        assert wallet.signed[0]["domain"]["version"] == 2  # what 0.96.0 signed

    @pytest.mark.parametrize("network", sorted(WITHOUT_ROWS))
    def test_without_the_row_guard_the_four_chains_sign_as_in_0_96_0(self, network):
        module = mutant("    if absent:\n", "    if False:\n")
        _, wallet = build(network, foreign_block(network, "synthetic"), module)
        assert len(wallet.signed) == 1
