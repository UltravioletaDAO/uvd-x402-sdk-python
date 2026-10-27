"""validate_preparation: only the rating asked for, on the chain asked for.

The exhaustive refusal table is the shared vector file
(``tests/vectors/reputation/prepare-rejections.json``); this file covers the
inputs a vector cannot express and the tables the gate depends on.
"""

from __future__ import annotations

import pytest

pytest.importorskip("eth_account")

from eth_utils import to_checksum_address  # noqa: E402

from tests.reputation_doubles import Clock, FakeChain, FakeFacilitator, fixed_account  # noqa: E402
from uvd_x402_sdk.erc8004 import (  # noqa: E402
    ERC8004_CONTRACTS,
    RELAYED_FEEDBACK_NETWORKS,
    PrepareRelayFeedbackResponse,
)
from uvd_x402_sdk.networks import get_network  # noqa: E402
from uvd_x402_sdk.reputation import (  # noqa: E402
    RELAY_CHAIN_IDS,
    RELAY_DELEGATES,
    PreparationRejected,
    RatingParams,
    relay_chain_id,
    validate_preparation,
)

NOW = 1_790_000_000
# Karma Hello's fixture (tests/reputation/test_live_worker.py::params).
KH = RatingParams(
    agent_id=123,
    value=72,
    tag1="requester_rating:stream_qa",
    tag2="kk|0xultravioleta",
    endpoint="https://www.twitch.tv/0xultravioleta",
    feedback_uri="https://example.com/feedback.json",
    feedback_hash="0x" + "22" * 32,
)


def prepare(params: RatingParams = KH, *, network: str = "base", rater=None) -> dict:
    clock = Clock(NOW)
    rater = rater or fixed_account().address
    feedback = {
        "agentId": params.agent_id,
        "value": params.value,
        "valueDecimals": 0,
        "tag1": params.tag1,
        "tag2": params.tag2,
        "endpoint": params.endpoint,
        "feedbackUri": params.feedback_uri,
        "feedbackHash": params.feedback_hash,
        "rater": rater,
    }
    return (
        FakeFacilitator(FakeChain(clock), clock)
        ._prepare({"network": network, "feedback": feedback})
        .json()
    )


@pytest.mark.parametrize(
    "key,value",
    [
        ("registry", "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"),
        ("agentId", "124"),
        ("value", "99"),
        ("tag1", "attacker"),
        ("tag2", "false issuer"),
        ("feedbackURI", "https://attacker.test"),
        ("endpoint", "https://attacker.test"),
        ("feedbackHash", "0x" + "99" * 32),
        ("agentId", "123\n"),
        ("agentId", " 123"),
        ("agentId", "0x7b"),
        ("value", "72.0"),
        ("value", True),
    ],
)
def test_never_sign_changed_feedback(key, value):
    """Karma Hello's test, plus the encodings a lax parser would read as the same number."""
    answer = prepare()
    validate_preparation(answer, KH, fixed_account().address, network="base", now=NOW)
    answer["typedData"]["message"][key] = value
    with pytest.raises(PreparationRejected):
        validate_preparation(answer, KH, fixed_account().address, network="base", now=NOW)


def test_the_parsed_model_and_raw_json_are_the_same_input():
    answer = prepare()
    model = PrepareRelayFeedbackResponse.model_validate(answer)
    one = validate_preparation(answer, KH, fixed_account().address, network="base", now=NOW)
    two = validate_preparation(model, KH, fixed_account().address, network="base", now=NOW)
    assert one == two


def test_the_network_is_required_and_the_deprecated_alias_still_routes():
    answer = prepare()
    with pytest.raises(TypeError):
        validate_preparation(answer, KH, fixed_account().address, now=NOW)  # type: ignore[call-arg]
    assert (
        validate_preparation(
            answer, KH, fixed_account().address, network="base-mainnet", now=NOW
        ).chain_id
        == 8453
    )


def test_a_prepare_for_arc_is_not_a_rating_on_base():
    answer = prepare(network="arc")
    with pytest.raises(PreparationRejected) as caught:
        validate_preparation(answer, KH, fixed_account().address, network="base", now=NOW)
    assert caught.value.code == "chain_mismatch"
    assert (
        validate_preparation(answer, KH, fixed_account().address, network="arc", now=NOW).chain_id
        == 5042
    )


@pytest.mark.parametrize("garbage", [None, 5, "x", [], {"success": "maybe", "network": 3}])
def test_garbage_is_a_refusal_not_a_crash(garbage):
    with pytest.raises((PreparationRejected, TypeError)) as caught:
        validate_preparation(garbage, KH, fixed_account().address, network="base", now=NOW)
    if isinstance(caught.value, PreparationRejected):
        assert caught.value.code == "prepare_shape"


@pytest.mark.parametrize(
    "bad",
    [
        dict(agent_id=-1),
        dict(agent_id=True),
        dict(agent_id="1"),
        dict(value=101),
        dict(value=-1),
        dict(value=True),
        dict(feedback_hash="0x1234"),
        dict(feedback_hash="22" * 32),
        dict(tag1=None),
    ],
)
def test_rating_params_are_what_the_facilitator_can_encode(bad):
    args = dict(
        agent_id=1,
        value=90,
        tag1="",
        tag2="",
        endpoint="",
        feedback_uri="",
        feedback_hash="0x" + "00" * 32,
    )
    args.update(bad)
    with pytest.raises(ValueError):
        RatingParams(**args)


def test_the_relay_tables_are_bound_to_the_sdks():
    assert set(RELAY_CHAIN_IDS) == set(RELAYED_FEEDBACK_NETWORKS)
    assert set(RELAY_DELEGATES) == set(RELAY_CHAIN_IDS.values())
    for chain_id, delegate in RELAY_DELEGATES.items():
        assert delegate == to_checksum_address(delegate), chain_id
    for network, chain_id in RELAY_CHAIN_IDS.items():
        assert relay_chain_id(network) == chain_id
        assert ERC8004_CONTRACTS[network].reputation_registry
        registered = get_network(network)
        if registered is not None:  # bsc and base-sepolia are not in the payment registry
            assert registered.chain_id == chain_id, network


def test_the_delegates_are_the_facilitators():
    """x402-rs ``relay.rs::delegate_address`` at main 09c3e3719 (2026-10-25)."""
    assert RELAY_DELEGATES[8453] == "0x260D3D0258680aA458D0EBB8BcAE8A2f68bf6163"
    assert RELAY_DELEGATES[5042] == "0x955Cc9fB9aB95FC0821ae74197D273dde5dA84f1"
    assert RELAY_DELEGATES[84532] == "0x9551263b9B83b1A737D55fd5e67Fb6D60e4eF787"
    assert RELAY_DELEGATES[10] == RELAY_DELEGATES[143]  # the same CREATE address on two chains
    assert relay_chain_id("avalanche") is None and relay_chain_id("arc-testnet") is None
