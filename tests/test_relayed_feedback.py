"""The rater-authored feedback rail: who the chain records as the author.

The ERC-8004 Reputation Registry stores ``msg.sender``. When the facilitator
relays a rating the ordinary way -- ``POST /feedback`` -- that is the
FACILITATOR, which is how 87,2% of the reputation on Base came to be attributed
to one wallet that could also revoke it. The EIP-7702 rail sends the
transaction TO THE RATER'S ADDRESS instead, so the registry sees the rater.

What is pinned here:

1. The network list matches the delegates the facilitator actually serves
   (``delegate_address()`` in ``src/erc8004/relay.rs``). Avalanche must stay
   out: its C-Chain rejects the transaction type itself, so there is nothing to
   deploy against and never will be until a C-Chain upgrade ships EIP-7702.
2. ``rater`` reaches the wire. Without it the facilitator has no author to put
   on the transaction, and the whole rail silently degrades to the thing it
   replaces.
3. ``deadline``, ``nonce`` and ``signature`` are echoed back byte for byte on
   submit. The facilitator rebuilds the calldata from the declared parameters
   and refuses to relay anything the signature does not cover; a client that
   paraphrases them gets a refusal, not a relay.
4. An HTTP failure comes back as ``success=False``, not as an exception. Same
   discipline as ``submit_feedback``.
"""

import json

import httpx
import pytest

from uvd_x402_sdk.erc8004 import (
    RELAYED_FEEDBACK_NETWORKS,
    Erc8004Client,
    PrepareRelayFeedbackResponse,
    RelayAuthorizationParams,
    supports_relayed_feedback,
)

# The eight mainnets Execution Market deployed a FeedbackDelegate on, each read
# off its own chain on two independent RPCs before it was written down
# (2026-08-23), plus the testnet the rail was first proven against.
DELEGATE_NETWORKS = {
    "base",
    "ethereum",
    "polygon",
    "arbitrum",
    "optimism",
    "celo",
    "bsc",
    "monad",
    "base-sepolia",
}


def test_the_relay_networks_are_the_ones_with_a_delegate():
    assert set(RELAYED_FEEDBACK_NETWORKS) == DELEGATE_NETWORKS


def test_avalanche_is_out_and_stays_out():
    """Not a "not yet".

    The C-Chain answers ``-32000 transaction type not supported`` -- an explicit
    refusal from the node, not an absence of traffic. Reputation for tasks paid
    on Avalanche is anchored on another chain; the payment stays on Avalanche.
    """
    assert not supports_relayed_feedback("avalanche")
    assert not supports_relayed_feedback("avalanche-fuji")


def test_scroll_and_skale_have_no_delegate_either():
    # Both serve ERC-8004; neither has a delegate deployed, and SKALE's EVM
    # predates Shanghai so 7702 cannot land there at all.
    assert not supports_relayed_feedback("scroll")
    assert not supports_relayed_feedback("skale-base")


def test_the_deprecated_base_alias_still_routes():
    assert supports_relayed_feedback("base-mainnet")


def _client(handler) -> Erc8004Client:
    transport = httpx.MockTransport(handler)
    client = Erc8004Client(base_url="https://facilitator.example")
    client._client = httpx.AsyncClient(transport=transport)
    return client


def test_the_signing_payload_is_the_digest_without_the_envelope():
    """The two fields differ by exactly the EIP-191 envelope, and both are served.

    `digest` is what the signature must recover against and ALREADY carries the
    envelope; `signing_payload` is the same hash before it. A raw key signs the
    first as a prehash; a wallet's ``personal_sign`` signs the second, because
    ``personal_sign`` applies the envelope itself.

    Signing ``digest`` through a wallet wraps it TWICE and recovers a stranger.
    That is not hypothetical: it is what every wallet surface did, and what THIS
    SDK's own documentation prescribed until 2026-08-25, which is why the rail
    ran for days without a single successful signed rating.
    """
    from eth_hash.auto import keccak

    payload = bytes.fromhex("16f16acc" + "11" * 30)
    digest = keccak(b"\x19Ethereum Signed Message:\n32" + payload)

    parsed = PrepareRelayFeedbackResponse.model_validate({
        "success": True,
        "digest": "0x" + digest.hex(),
        "signingPayload": "0x" + payload.hex(),
        "delegated": True,
        "chainId": 8453,
        "network": "base",
    })
    assert parsed.signing_payload == "0x" + payload.hex()
    assert parsed.digest == "0x" + digest.hex()
    # The relationship a client can check instead of rebuilding the preimage.
    assert (
        keccak(b"\x19Ethereum Signed Message:\n32"
               + bytes.fromhex(parsed.signing_payload[2:])).hex()
        == parsed.digest[2:]
    )


def test_a_v4_delegate_returns_typed_data_and_no_signing_payload():
    """Two versions on the wire, and they are mutually exclusive by design.

    A v4 chain sends ``typedData`` and no ``signingPayload``: ``signTypedData``
    has no envelope to apply twice, so the field that exists only to anticipate
    that envelope has nothing to do. A client that keeps reaching for
    ``signing_payload`` on a v4 chain gets ``None`` and must fall through to the
    typed data, not to ``digest``.
    """
    parsed = PrepareRelayFeedbackResponse.model_validate({
        "success": True,
        "digest": "0x" + "cd" * 32,
        "typedData": {
            "primaryType": "RelayedGiveFeedback",
            "domain": {"name": "FeedbackDelegate", "version": "1", "chainId": 8453,
                       "verifyingContract": "0x09C32b8FC0a94A1EeD424499A42180e29667bEeE"},
            "types": {"RelayedGiveFeedback": [{"name": "registry", "type": "address"}]},
            "message": {"agentId": "2106"},
        },
        "delegated": True,
        "chainId": 8453,
        "network": "base",
    })
    assert parsed.typed_data is not None
    assert parsed.typed_data["primaryType"] == "RelayedGiveFeedback"
    # The domain names the RATER, not the delegate: with the delegate as
    # verifyingContract every account pointed at it would share a domain and the
    # signature would replay across them.
    assert parsed.typed_data["domain"]["verifyingContract"] == (
        "0x09C32b8FC0a94A1EeD424499A42180e29667bEeE"
    )
    assert parsed.signing_payload is None


def test_a_v3_delegate_returns_no_typed_data():
    parsed = PrepareRelayFeedbackResponse.model_validate({
        "success": True,
        "digest": "0x" + "cd" * 32,
        "signingPayload": "0x" + "ab" * 32,
        "delegated": True,
        "chainId": 84532,
        "network": "base-sepolia",
    })
    assert parsed.typed_data is None
    assert parsed.signing_payload == "0x" + "ab" * 32


def test_an_older_facilitator_omits_the_signing_payload():
    """It must parse as absent, not crash -- and never silently equal `digest`.

    A client that needs it should fail loudly. Defaulting it to `digest` would
    hand a wallet the value that cannot be signed.
    """
    parsed = PrepareRelayFeedbackResponse.model_validate({
        "success": True,
        "digest": "0x" + "ab" * 32,
        "delegated": True,
        "chainId": 8453,
        "network": "base",
    })
    assert parsed.signing_payload is None


@pytest.mark.asyncio
async def test_prepare_puts_the_rater_on_the_wire():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "success": True,
                "delegate": "0x754206C4247317768bD86459E829a174d9C68BA4",
                "data": "0xdeadbeef",
                "digest": "0x" + "11" * 32,
                "deadline": 1_800_000_000,
                "nonce": "0x" + "22" * 32,
                "delegated": False,
                "accountNonce": 7,
                "chainId": 8453,
                "network": "base",
            },
        )

    result = await _client(handler).prepare_relayed_feedback(
        network="base",
        agent_id=18896,
        rater="0x0000000000000000000000000000000000000001",
        value=95,
        tag1="quality",
    )

    assert seen["url"].endswith("/feedback/evm/prepare")
    assert seen["body"]["feedback"]["rater"] == (
        "0x0000000000000000000000000000000000000001"
    )
    assert seen["body"]["network"] == "base"
    assert result.success
    assert result.delegated is False
    # delegated=False is the signal that an EIP-7702 authorization is still
    # required; the account nonce is what goes in it.
    assert result.account_nonce == 7
    assert result.chain_id == 8453


@pytest.mark.asyncio
async def test_the_base_alias_is_normalised_before_it_reaches_the_wire():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200, json={"success": True, "delegated": True, "chainId": 8453, "network": "base"}
        )

    await _client(handler).prepare_relayed_feedback(
        network="base-mainnet",
        agent_id=1,
        rater="0x0000000000000000000000000000000000000001",
        value=1,
    )
    # The facilitator answers 400 "Invalid network: base-mainnet".
    assert seen["body"]["network"] == "base"


@pytest.mark.asyncio
async def test_submit_echoes_back_exactly_what_prepare_returned():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "success": True,
                "transaction": "0x" + "ab" * 32,
                "feedbackIndex": 3,
                "network": "base",
            },
        )

    deadline = 1_800_000_000
    nonce = "0x" + "22" * 32
    signature = "0x" + "cd" * 65

    result = await _client(handler).submit_relayed_feedback(
        network="base",
        agent_id=18896,
        rater="0x0000000000000000000000000000000000000001",
        value=95,
        tag1="quality",
        deadline=deadline,
        nonce=nonce,
        signature=signature,
        authorization=RelayAuthorizationParams(
            chainId=8453,
            address="0x754206C4247317768bD86459E829a174d9C68BA4",
            nonce=7,
            yParity=1,
            r="0x" + "01" * 32,
            s="0x" + "02" * 32,
        ),
    )

    body = seen["body"]
    assert seen["url"].endswith("/feedback/evm/submit")
    # Not redundant with prepare: the facilitator rebuilds the calldata from
    # these and refuses to relay what the signature does not cover.
    assert body["deadline"] == deadline
    assert body["nonce"] == nonce
    assert body["signature"] == signature
    assert body["feedback"]["rater"] == "0x0000000000000000000000000000000000000001"
    # chainId 0 is EIP-7702's wildcard, valid on every chain. Pinning the chain
    # is the narrower grant, so it must survive serialisation as sent.
    assert body["authorization"]["chainId"] == 8453
    assert body["authorization"]["yParity"] == 1
    assert result.success
    assert result.feedback_index == 3


@pytest.mark.asyncio
async def test_a_delegated_account_sends_no_authorization():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"success": True, "network": "base"})

    await _client(handler).submit_relayed_feedback(
        network="base",
        agent_id=1,
        rater="0x0000000000000000000000000000000000000001",
        value=1,
        deadline=1_800_000_000,
        nonce="0x" + "22" * 32,
        signature="0x" + "cd" * 65,
    )
    assert "authorization" not in seen["body"]


@pytest.mark.asyncio
async def test_a_refusal_is_a_result_not_an_exception():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400, json={"success": False, "error": "no FeedbackDelegate is deployed there yet"}
        )

    prepared = await _client(handler).prepare_relayed_feedback(
        network="celo",
        agent_id=1,
        rater="0x0000000000000000000000000000000000000001",
        value=1,
    )
    assert prepared.success is False
    assert "400" in (prepared.error or "")

    submitted = await _client(handler).submit_relayed_feedback(
        network="celo",
        agent_id=1,
        rater="0x0000000000000000000000000000000000000001",
        value=1,
        deadline=1_800_000_000,
        nonce="0x" + "22" * 32,
        signature="0x" + "cd" * 65,
    )
    assert submitted.success is False
    assert "400" in (submitted.error or "")
