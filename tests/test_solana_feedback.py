"""The Solana rater-authored feedback rail: who the chain records as the author.

The ERC-8004 program on Solana declares account 0 of ``give_feedback`` as
``[signer, writable] client (feedback author / fee payer)``. ``POST /feedback``
puts the FACILITATOR's keypair there, so the chain records the facilitator as
the author of the rating -- the same defect the EIP-7702 rail fixes on EVM,
reached here without a delegate because Solana takes several signers per
transaction natively: the rater signs as ``client``, the facilitator stays the
fee payer.

What is pinned here:

1. **The two rails stay apart.** ``solana`` must never enter
   ``RELAYED_FEEDBACK_NETWORKS``: that set means "a ``FeedbackDelegate`` is
   deployed and verified on this chain", and ``prepare_relayed_feedback``
   builds ``/feedback/evm/prepare`` out of it. A ``solana`` entry there sends
   the call to the EVM route, which answers 400.
2. **The wire, against a real answer from the deployed facilitator.**
   ``tests/fixtures/solana-feedback-prepare.json`` is a live capture from
   ``https://facilitator.ultravioletadao.xyz`` (v2.16.0): two signature slots,
   both empty, fee payer at account 0, rater at account 1.
3. **Signing does not touch the message.** The facilitator rebuilds the
   transaction from the declared parameters and refuses anything that is not
   byte-for-byte what it would have offered, so the one thing this SDK must
   never do is re-serialise the message. The acceptance test here mirrors
   ``accept_rater_signed_transaction`` from ``x402-rs`` line for line.
4. **Every way to get it wrong fails here, not one round trip later.** A
   signer holding another key, a rater that is not a signer, a v0 message, a
   truncated blob: all ``ValueError`` before anything reaches the network,
   because from a facilitator 400 they are indistinguishable.
"""

import base64
import hashlib
import json
from pathlib import Path

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from uvd_x402_sdk.erc8004 import (
    RELAYED_FEEDBACK_NETWORKS,
    SOLANA_FEEDBACK_NETWORKS,
    Erc8004Client,
    PrepareSolanaFeedbackResponse,
    supports_relayed_feedback,
    supports_solana_feedback,
)
from uvd_x402_sdk.solana_signing import (
    PUBKEY_LEN,
    SIGNATURE_LEN,
    Ed25519Signer,
    SolanaFeedbackTransaction,
    _b58decode,
    _b58encode,
    decode_solana_transaction,
    sign_solana_feedback_transaction,
)

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "solana-feedback-prepare.json").read_text(
        encoding="utf-8"
    )
)
LIVE = FIXTURE["response"]
DECODED = FIXTURE["decoded"]
VECTOR = FIXTURE["signing_vector"]

EMPTY_SLOT = b"\x00" * SIGNATURE_LEN


def _test_signer() -> Ed25519Signer:
    """The vector's rater. Derived from a sentence, holds nothing, signs tests."""
    return Ed25519Signer(hashlib.sha256(VECTOR["seed_phrase"].encode()).digest())


# ---------------------------------------------------------------------------
# 1. The two rails are siblings, not one list
# ---------------------------------------------------------------------------


def test_the_solana_rail_is_its_own_set():
    assert set(SOLANA_FEEDBACK_NETWORKS) == {"solana", "solana-devnet"}
    assert supports_solana_feedback("solana")
    assert supports_solana_feedback("solana-devnet")


def test_solana_is_not_in_the_delegate_set_and_must_never_be():
    """Merging the two lists routes Solana to the EVM URL, which answers 400.

    ``RELAYED_FEEDBACK_NETWORKS`` means "Execution Market deployed a
    ``FeedbackDelegate`` here and the facilitator verified it on-chain", and
    ``prepare_relayed_feedback`` builds ``/feedback/evm/prepare`` from it.
    Solana needs no delegate at all -- the program already declares account 0
    as ``[signer] client`` -- so it is a sibling rail, not a missing row.
    """
    assert not (SOLANA_FEEDBACK_NETWORKS & RELAYED_FEEDBACK_NETWORKS)
    assert "solana" not in RELAYED_FEEDBACK_NETWORKS
    assert not supports_relayed_feedback("solana")
    assert not supports_relayed_feedback("solana-devnet")


def test_the_evm_networks_do_not_serve_the_solana_rail():
    for network in ("base", "ethereum", "polygon", "base-sepolia", "base-mainnet"):
        assert not supports_solana_feedback(network), network


# ---------------------------------------------------------------------------
# 2. The wire, as the deployed facilitator actually answers it
# ---------------------------------------------------------------------------


def test_the_capture_is_from_the_deployed_facilitator():
    assert FIXTURE["facilitator"] == "https://facilitator.ultravioletadao.xyz"
    assert LIVE["success"] is True
    assert LIVE["network"] == "solana"
    assert LIVE["rater"] == FIXTURE["request"]["feedback"]["rater"]


def test_the_response_parses_into_the_model_camelcase_and_all():
    parsed = PrepareSolanaFeedbackResponse.model_validate(LIVE)
    assert parsed.success is True
    assert parsed.transaction == LIVE["transaction"]
    assert parsed.fee_payer == LIVE["feePayer"]
    assert parsed.last_valid_block_height == LIVE["lastValidBlockHeight"]
    assert parsed.blockhash == LIVE["blockhash"]
    assert parsed.error is None


def test_the_rater_is_a_signer_and_is_not_the_fee_payer():
    """The entire point of the rail, stated as an assertion.

    If these two were the same account the endpoint would be an expensive way
    to reproduce ``POST /feedback``.
    """
    tx = decode_solana_transaction(LIVE["transaction"])
    assert tx.account_keys[0] == LIVE["feePayer"]
    assert tx.signer_index(LIVE["rater"]) == DECODED["rater_index"] == 1
    assert LIVE["rater"] != LIVE["feePayer"]


def test_the_transaction_arrives_unsigned_with_two_slots():
    tx = decode_solana_transaction(LIVE["transaction"])
    assert tx.num_required_signatures == 2
    assert len(tx.signatures) == 2
    assert all(slot == EMPTY_SLOT for slot in tx.signatures)
    assert len(tx.account_keys) == DECODED["num_accounts"] == 11
    assert hashlib.sha256(tx.message).hexdigest() == DECODED["message_sha256"]


def test_the_blockhash_in_the_message_is_the_one_the_response_declares():
    """A client that trusted the field and ignored the message would submit a
    transaction pinned to a different block than it thinks."""
    tx = decode_solana_transaction(LIVE["transaction"])
    _, key_offset = 0, 4  # header (3) + a single-byte compact-u16 for 11 keys
    blockhash_offset = key_offset + PUBKEY_LEN * len(tx.account_keys)
    in_message = tx.message[blockhash_offset : blockhash_offset + 32]
    assert _b58encode(in_message) == LIVE["blockhash"]


def test_decode_then_encode_is_byte_identical():
    """The round trip has to be the identity, or every submission is a 400.

    ``accept_rater_signed_transaction`` compares the submitted message with
    the one it rebuilds. Anything this SDK adds, drops or reorders shows up
    there as ``submitted transaction does not match the one this facilitator
    built``, with nothing on the client side to point at.
    """
    tx = decode_solana_transaction(LIVE["transaction"])
    assert tx.encode() == LIVE["transaction"]


def test_base58_round_trips_every_account_in_the_capture():
    for key in DECODED["account_keys"]:
        raw = _b58decode(key)
        assert len(raw) == PUBKEY_LEN
        assert _b58encode(raw) == key
    # The system program is 32 zero bytes, i.e. the leading-'1' case.
    assert _b58encode(b"\x00" * 32) == "1" * 32
    assert _b58decode("1" * 32) == b"\x00" * 32


# ---------------------------------------------------------------------------
# 3. Signing puts one signature in and changes nothing else
# ---------------------------------------------------------------------------


def test_signing_leaves_the_message_byte_for_byte():
    signer = _test_signer()
    assert signer.pubkey == VECTOR["rater"]

    before = decode_solana_transaction(VECTOR["unsigned_transaction"])
    signed = decode_solana_transaction(
        sign_solana_feedback_transaction(
            VECTOR["unsigned_transaction"], signer.pubkey, signer
        )
    )
    assert signed.message == before.message
    assert signed.account_keys == before.account_keys
    assert signed.num_required_signatures == before.num_required_signatures


def test_the_fee_payer_slot_is_left_empty_for_the_facilitator():
    signer = _test_signer()
    signed = decode_solana_transaction(
        sign_solana_feedback_transaction(
            VECTOR["unsigned_transaction"], signer.pubkey, signer
        )
    )
    assert signed.signatures[0] == EMPTY_SLOT
    assert signed.signatures[1] != EMPTY_SLOT


def test_the_signature_is_the_pinned_one():
    """ed25519 is deterministic, so the vector pins the bytes themselves.

    This is what goes red if what gets signed ever stops being the raw message
    -- a length prefix, a domain tag, a re-serialisation. All of those still
    produce a well-formed 64-byte signature.
    """
    signer = _test_signer()
    signed = decode_solana_transaction(
        sign_solana_feedback_transaction(
            VECTOR["unsigned_transaction"], signer.pubkey, signer
        )
    )
    assert signed.signatures[1].hex() == VECTOR["rater_signature_hex"]
    assert (
        sign_solana_feedback_transaction(
            VECTOR["unsigned_transaction"], signer.pubkey, signer
        )
        == VECTOR["signed_transaction"]
    )


def test_the_facilitator_would_accept_it():
    """The three checks of ``accept_rater_signed_transaction``, run here.

    1. the submitted message equals the one the facilitator would rebuild,
    2. the rater is inside the signer prefix of the account table,
    3. the signature in the rater's slot verifies over ``message.serialize()``.
    """
    signer = _test_signer()
    expected = decode_solana_transaction(VECTOR["unsigned_transaction"])
    submitted = decode_solana_transaction(
        sign_solana_feedback_transaction(
            VECTOR["unsigned_transaction"], signer.pubkey, signer
        )
    )

    assert submitted.message == expected.message

    index = submitted.account_keys.index(signer.pubkey)
    assert index < submitted.num_required_signatures

    signature = submitted.signatures[index]
    assert signature != EMPTY_SLOT
    Ed25519PublicKey.from_public_bytes(_b58decode(signer.pubkey)).verify(
        signature, submitted.message
    )


def test_signing_twice_is_the_same_transaction():
    signer = _test_signer()
    once = sign_solana_feedback_transaction(
        VECTOR["unsigned_transaction"], signer.pubkey, signer
    )
    twice = sign_solana_feedback_transaction(once, signer.pubkey, signer)
    assert once == twice


# ---------------------------------------------------------------------------
# 4. Every way to get it wrong fails here, not one round trip later
# ---------------------------------------------------------------------------


def test_a_signer_holding_another_key_is_refused_before_the_network():
    """Otherwise the facilitator answers ``the rater's signature is missing or
    invalid`` and the caller cannot tell that from a corrupted blob."""
    other = Ed25519Signer(hashlib.sha256(b"a different rater entirely").digest())
    with pytest.raises(ValueError, match="the signer holds"):
        sign_solana_feedback_transaction(
            VECTOR["unsigned_transaction"], VECTOR["rater"], other
        )


def test_an_account_that_is_not_a_signer_is_refused():
    """The system program is account 4 of the real capture: present, not a signer."""
    tx = decode_solana_transaction(LIVE["transaction"])
    system_program = tx.account_keys[4]
    assert system_program == "1" * 32
    with pytest.raises(ValueError, match="only the first 2 are signers"):
        tx.signer_index(system_program)


def test_a_rater_that_is_not_in_the_transaction_is_refused():
    tx = decode_solana_transaction(LIVE["transaction"])
    stranger = _b58encode(hashlib.sha256(b"not in this transaction").digest())
    with pytest.raises(ValueError, match="is not an account of this transaction"):
        tx.signer_index(stranger)


def test_a_versioned_message_is_refused_rather_than_mis_parsed():
    """A v0 header would shift every offset by one byte, so the signature would
    land in a slot that belongs to somebody else."""
    raw = base64.b64decode(LIVE["transaction"])
    v0 = bytearray(raw)
    v0[1 + SIGNATURE_LEN * 2] |= 0x80
    with pytest.raises(ValueError, match="versioned"):
        decode_solana_transaction(base64.b64encode(bytes(v0)).decode())


def test_a_truncated_transaction_is_refused():
    raw = base64.b64decode(LIVE["transaction"])
    with pytest.raises(ValueError, match="truncated"):
        decode_solana_transaction(base64.b64encode(raw[:100]).decode())


def test_a_header_that_disagrees_with_the_signature_array_is_refused():
    """Three slots, a header asking for two: one of the two is a lie, and
    guessing which would put the signature in the wrong place."""
    raw = bytearray(base64.b64decode(LIVE["transaction"]))
    raw[0] = 3
    raw[1:1] = EMPTY_SLOT
    with pytest.raises(ValueError, match="requires 2 signatures but the array carries 3"):
        decode_solana_transaction(base64.b64encode(bytes(raw)).decode())


def test_a_blob_that_is_not_base64_is_refused():
    with pytest.raises(ValueError, match="not valid base64"):
        decode_solana_transaction("this is not a transaction")


def test_no_transaction_at_all_is_refused():
    """``prepare`` returning ``success=False`` leaves ``transaction`` at None."""
    with pytest.raises(ValueError, match="no transaction to sign"):
        decode_solana_transaction(None)
    with pytest.raises(ValueError, match="no transaction to sign"):
        decode_solana_transaction("   ")


def test_a_signer_that_returns_the_wrong_length_is_refused():
    class Stub:
        pubkey = VECTOR["rater"]

        def sign_message(self, message):
            return b"\x01" * 32

    with pytest.raises(ValueError, match="an ed25519 signature is 64"):
        sign_solana_feedback_transaction(
            VECTOR["unsigned_transaction"], VECTOR["rater"], Stub()
        )


def test_something_that_is_not_a_signer_at_all_is_refused():
    with pytest.raises(TypeError, match="pubkey"):
        sign_solana_feedback_transaction(
            VECTOR["unsigned_transaction"], VECTOR["rater"], object()
        )


def test_with_signature_refuses_a_signature_of_the_wrong_size():
    tx = decode_solana_transaction(VECTOR["unsigned_transaction"])
    with pytest.raises(ValueError, match="an ed25519 signature is 64 bytes"):
        tx.with_signature(VECTOR["rater"], b"\x02" * 63)


def test_the_transaction_object_is_immutable():
    tx = decode_solana_transaction(VECTOR["unsigned_transaction"])
    with pytest.raises(Exception):
        tx.message = b"other"
    assert isinstance(tx, SolanaFeedbackTransaction)


# ---------------------------------------------------------------------------
# 5. The signer itself
# ---------------------------------------------------------------------------


def test_the_three_key_shapes_give_the_same_account():
    seed = hashlib.sha256(VECTOR["seed_phrase"].encode()).digest()
    from_seed = Ed25519Signer(seed)
    expanded = seed + _b58decode(from_seed.pubkey)
    assert Ed25519Signer(expanded).pubkey == from_seed.pubkey
    assert Ed25519Signer(list(expanded)).pubkey == from_seed.pubkey
    assert Ed25519Signer(_b58encode(expanded)).pubkey == from_seed.pubkey


def test_a_corrupted_64_byte_key_is_caught_instead_of_signing_as_a_stranger():
    seed = hashlib.sha256(VECTOR["seed_phrase"].encode()).digest()
    wrong_half = seed + hashlib.sha256(b"somebody else").digest()
    with pytest.raises(ValueError, match="does not match"):
        Ed25519Signer(wrong_half)


def test_a_key_of_the_wrong_length_says_only_the_length():
    with pytest.raises(ValueError) as excinfo:
        Ed25519Signer(b"\x07" * 48)
    assert "32 or 64 bytes, got 48" in str(excinfo.value)


def test_the_key_never_reaches_repr():
    signer = _test_signer()
    text = repr(signer)
    assert signer.pubkey in text
    assert not hasattr(signer, "__dict__")  # __slots__, so no accidental exposure


# ---------------------------------------------------------------------------
# 6. The client: what goes on the wire
# ---------------------------------------------------------------------------


def _client(handler) -> Erc8004Client:
    transport = httpx.MockTransport(handler)
    client = Erc8004Client(base_url="https://facilitator.example")
    client._client = httpx.AsyncClient(transport=transport)
    return client


async def test_prepare_posts_the_solana_route_with_the_rater_on_it():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=LIVE)

    client = _client(handler)
    result = await client.prepare_solana_feedback(
        network="solana",
        agent_id=FIXTURE["request"]["feedback"]["agentId"],
        rater=LIVE["rater"],
        value=87,
        score=95,
        tag1="quality",
        tag2="api",
    )

    assert seen["url"] == "https://facilitator.example/feedback/solana/prepare"
    assert seen["body"]["network"] == "solana"
    assert seen["body"]["feedback"]["rater"] == LIVE["rater"]
    assert seen["body"]["feedback"]["score"] == 95
    assert result.success is True
    assert result.fee_payer == LIVE["feePayer"]


async def test_submit_carries_the_transaction_at_the_top_level():
    """Not inside ``feedback``. The facilitator reads
    ``SubmitFeedbackRequest.transaction`` next to ``feedback``, and a nested one
    is simply absent -- which deserialises as a missing field, i.e. a 400 that
    says nothing about where the value went."""
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={"success": True, "transaction": "5x" + "a" * 30, "network": "solana"},
        )

    client = _client(handler)
    result = await client.submit_solana_feedback(
        network="solana",
        agent_id=FIXTURE["request"]["feedback"]["agentId"],
        rater=VECTOR["rater"],
        value=87,
        score=95,
        tag1="quality",
        tag2="api",
        transaction=VECTOR["signed_transaction"],
    )

    assert seen["url"] == "https://facilitator.example/feedback/solana/submit"
    assert seen["body"]["transaction"] == VECTOR["signed_transaction"]
    assert "transaction" not in seen["body"]["feedback"]
    assert seen["body"]["feedback"]["rater"] == VECTOR["rater"]
    assert result.success is True


async def test_the_parameters_are_echoed_back_exactly():
    """The facilitator rebuilds the transaction from these and refuses a
    mismatch, so prepare and submit have to describe the same rating."""
    bodies = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        if request.url.path.endswith("/prepare"):
            return httpx.Response(200, json=LIVE)
        return httpx.Response(200, json={"success": True, "network": "solana"})

    client = _client(handler)
    common = dict(
        network="solana",
        agent_id=FIXTURE["request"]["feedback"]["agentId"],
        rater=VECTOR["rater"],
        value=87,
        value_decimals=0,
        score=95,
        tag1="quality",
        tag2="api",
        endpoint="/api/rate",
        feedback_uri="ipfs://cid",
    )
    await client.prepare_solana_feedback(**common)
    await client.submit_solana_feedback(
        transaction=VECTOR["signed_transaction"], **common
    )

    assert bodies[0]["feedback"] == bodies[1]["feedback"]


async def test_an_http_failure_comes_back_as_success_false():
    """Same discipline as the rest of this module: a 400 is a result, not an
    exception, so a caller can log the facilitator's own words."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={
                "success": False,
                "error": "submitted transaction does not match the one this "
                "facilitator built",
            },
        )

    client = _client(handler)
    prepared = await client.prepare_solana_feedback(
        network="solana", agent_id="x" * 32, rater=VECTOR["rater"], value=1
    )
    assert prepared.success is False
    assert "400" in prepared.error

    submitted = await client.submit_solana_feedback(
        network="solana",
        agent_id="x" * 32,
        rater=VECTOR["rater"],
        value=1,
        transaction=VECTOR["signed_transaction"],
    )
    assert submitted.success is False
    assert "does not match" in submitted.error


async def test_submit_without_a_transaction_never_reaches_the_network():
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("should not have been called")

    client = _client(handler)
    with pytest.raises(ValueError, match="transaction is required"):
        await client.submit_solana_feedback(
            network="solana",
            agent_id="x" * 32,
            rater=VECTOR["rater"],
            value=1,
            transaction="",
        )


async def test_a_score_outside_the_range_is_refused_on_both_calls():
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("should not have been called")

    client = _client(handler)
    common = dict(network="solana", agent_id="x" * 32, rater=VECTOR["rater"], value=1)

    with pytest.raises(ValueError, match="score must be between 0 and 100"):
        await client.prepare_solana_feedback(score=101, **common)
    with pytest.raises(ValueError, match="score must be between 0 and 100"):
        await client.submit_solana_feedback(
            score=101, transaction=VECTOR["signed_transaction"], **common
        )
