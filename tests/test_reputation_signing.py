"""The rater's two signatures: what is signed, by whom, and what is refused.

The digest is pinned against the DEPLOYED v4 delegate (x402-rs
``the_vector_matches_the_deployed_v4_contract``), the authorization against
eth-account's ``sign_authorization``. Every signer shape a consumer brings is
covered (hash-only like Karma Kadabra's Paybox, typed-data like a wallet, the
SDK's own ``EnvKeyAdapter``), and so are the ways a remote signer can lie.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

pytest.importorskip("eth_account")

from eth_account import Account  # noqa: E402
from eth_utils import keccak  # noqa: E402

from tests.reputation_doubles import (
    Clock,
    FakeChain,
    FakeFacilitator,
    fixed_account,
    fixed_key,
)  # noqa: E402
from uvd_x402_sdk.reputation import (  # noqa: E402
    GIVE_FEEDBACK_TYPEHASH,
    AccountSigner,
    PreparationRejected,
    RatingParams,
    SignatureRejected,
    authorization_digest,
    build_relay_authorization,
    relayed_feedback_digest,
    sign_relayed_feedback,
    validate_preparation,
)
from uvd_x402_sdk.reputation._chains import (
    GIVE_FEEDBACK_FIELDS,
    PRIMARY_TYPE,
    RELAY_DELEGATES,
)  # noqa: E402
from uvd_x402_sdk.reputation.preparation import domain_separator  # noqa: E402
from uvd_x402_sdk.reputation.signing import SECP256K1_N  # noqa: E402
from uvd_x402_sdk.wallet import EnvKeyAdapter  # noqa: E402

REGISTRY = "0x8004BAa17C55a88189AE136b182e5fdA19dE9b63"
NOW = 1_790_000_000
PARAMS = RatingParams(
    agent_id=18896,
    value=90,
    tag1="credits_roll:idea",
    tag2="credits-roll|moment",
    endpoint="https://www.twitch.tv/somechannel",
    feedback_uri="https://credits.example/evidence/x.json",
    feedback_hash="0x" + "5a" * 32,
)


def prepared(
    rater: str, *, delegated: bool = False, account_nonce=0, params: RatingParams = PARAMS
):
    clock = Clock(NOW)
    facilitator = FakeFacilitator(FakeChain(clock), clock)
    if delegated:
        facilitator.delegated.add(rater.lower())
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
    response = facilitator._prepare({"network": "base", "feedback": feedback})
    answer = response.json()
    if not delegated:
        answer["accountNonce"] = account_nonce
    return answer


def verified_for(signer_address: str, **kwargs):
    return validate_preparation(
        prepared(signer_address, **kwargs), PARAMS, signer_address, network="base", now=NOW
    )


class HashOnlySigner:
    """Karma Kadabra's shape: an address and ``sign_hash`` (Paybox ``op: hash``)."""

    def __init__(self, account) -> None:
        self.address = account.address
        self._account = account
        self.hashes: list[bytes] = []

    def sign_hash(self, digest: bytes) -> str:
        self.hashes.append(digest)
        return "0x" + bytes(self._account.unsafe_sign_hash(digest).signature).hex()


class Lying(HashOnlySigner):
    """Returns whatever ``transform`` makes of a correct signature."""

    def __init__(self, account, transform) -> None:
        super().__init__(account)
        self.transform = transform

    def sign_hash(self, digest: bytes) -> str:
        return self.transform(bytes.fromhex(super().sign_hash(digest)[2:]))


def high_s(signature: bytes) -> str:
    r, s, v = signature[:32], int.from_bytes(signature[32:64], "big"), signature[64]
    flipped_v = 28 if v in (27, 0) else 27
    return "0x" + (r + (SECP256K1_N - s).to_bytes(32, "big") + bytes([flipped_v])).hex()


def test_the_typehash_is_the_confirmed_one():
    encoded = PRIMARY_TYPE + "(" + ",".join(f"{t} {n}" for n, t in GIVE_FEEDBACK_FIELDS) + ")"
    assert "0x" + keccak(text=encoded).hex() == GIVE_FEEDBACK_TYPEHASH


def test_the_digest_matches_the_deployed_contract():
    account = "0x260D3D0258680aA458D0EBB8BcAE8A2f68bf6163"
    assert domain_separator(8453, account).hex() == (
        "ae792427891b13b5663e321e5c8802c304437aad75338e22a4b8e1a47243d09d"
    )
    params = RatingParams(
        agent_id=2106,
        value=95,
        tag1="quality",
        tag2="api",
        endpoint="https://agent.example",
        feedback_uri="https://execution.market/feedback/abc",
        feedback_hash="0x" + "00" * 32,
    )
    digest = relayed_feedback_digest(
        8453, account, REGISTRY, params, 1_787_662_172, "0x" + "42" * 32
    )
    assert digest.hex() == "a7a2a62dd18c44c79f7c252d628f32b3c46234b20d237866154dbe118ebaf6e1"


@pytest.mark.parametrize("shape", ["account", "hash_only", "env_key_adapter"])
def test_every_signer_shape_produces_the_same_signature(shape):
    account = fixed_account()
    signer = {
        "account": AccountSigner(account),
        "hash_only": HashOnlySigner(account),
        "env_key_adapter": EnvKeyAdapter(private_key=fixed_key()),
    }[shape]
    verified = verified_for(account.address)
    expected = (
        "0x" + bytes(account.unsafe_sign_hash(bytes.fromhex(verified.digest[2:])).signature).hex()
    )
    assert sign_relayed_feedback(verified, signer) == expected


def test_a_hash_signer_is_handed_the_digest_rebuilt_here():
    account = fixed_account()
    signer = HashOnlySigner(account)
    verified = verified_for(account.address)
    sign_relayed_feedback(verified, signer)
    assert signer.hashes == [bytes.fromhex(verified.digest[2:])]


def test_a_high_s_signature_is_refused():
    account = fixed_account()
    with pytest.raises(SignatureRejected) as caught:
        sign_relayed_feedback(verified_for(account.address), Lying(account, high_s))
    assert caught.value.code == "signature_high_s"


@pytest.mark.parametrize(
    "transform,code",
    [
        (lambda sig: "0x" + sig[:64].hex(), "signature_shape"),
        (lambda sig: "0x" + (sig[:64] + bytes([29])).hex(), "signature_v"),
        (lambda sig: "not hex", "signature_shape"),
        (lambda sig: "0x" + (bytes(32) + sig[32:]).hex(), "signature_range"),
    ],
)
def test_a_malformed_signature_is_refused(transform, code):
    account = fixed_account()
    with pytest.raises(SignatureRejected) as caught:
        sign_relayed_feedback(verified_for(account.address), Lying(account, transform))
    assert caught.value.code == code


def test_a_signature_by_another_key_is_refused():
    account, stranger = fixed_account(), fixed_account("22")

    def strangers(sig: bytes) -> str:
        return "0x" + bytes(stranger.unsafe_sign_hash(keccak(b"x")).signature).hex()

    with pytest.raises(SignatureRejected) as caught:
        sign_relayed_feedback(verified_for(account.address), Lying(account, strangers))
    assert caught.value.code == "signature_wrong_signer"


def test_a_signer_that_is_not_the_rater_signs_nothing():
    account, stranger = fixed_account(), HashOnlySigner(fixed_account("22"))
    with pytest.raises(PreparationRejected) as caught:
        sign_relayed_feedback(verified_for(account.address), stranger)
    assert caught.value.code == "rater_mismatch" and stranger.hashes == []


def test_only_a_verified_preparation_is_signed():
    account = fixed_account()
    with pytest.raises(TypeError):
        sign_relayed_feedback(prepared(account.address), AccountSigner(account))


@pytest.mark.parametrize(
    "field,value,code",
    [
        ("delegate", "0x" + "de" * 20, "delegate_mismatch"),
        ("chain_id", 1, "delegate_mismatch"),
        ("digest", "0x" + "01" * 32, "digest_mismatch"),
        ("params", replace(PARAMS, value=91), "digest_mismatch"),
    ],
)
def test_a_hand_altered_verified_preparation_is_refused(field, value, code):
    account = fixed_account()
    signer = HashOnlySigner(account)
    altered = replace(verified_for(account.address), **{field: value})
    with pytest.raises(PreparationRejected) as caught:
        sign_relayed_feedback(altered, signer)
    assert caught.value.code == code and signer.hashes == []
    with pytest.raises(PreparationRejected):
        build_relay_authorization(altered, signer)


class TestAuthorization:
    @pytest.mark.parametrize("nonce", [0, 1, 127, 128, 2**64 - 1])
    def test_equals_eth_account(self, nonce):
        account = fixed_account()
        reference = account.sign_authorization(
            {"chainId": 8453, "address": RELAY_DELEGATES[8453], "nonce": nonce}
        )
        assert authorization_digest(8453, RELAY_DELEGATES[8453], nonce) == bytes(
            reference.authorization_hash
        )
        built = build_relay_authorization(
            verified_for(account.address, account_nonce=nonce), HashOnlySigner(account)
        )
        assert (built.chain_id, built.address, built.nonce) == (8453, RELAY_DELEGATES[8453], nonce)
        assert (built.y_parity, int(built.r, 16), int(built.s, 16)) == (
            reference.y_parity,
            reference.r,
            reference.s,
        )

    def test_a_delegated_rater_signs_no_authorization(self):
        account = fixed_account()
        signer = HashOnlySigner(account)
        assert (
            build_relay_authorization(verified_for(account.address, delegated=True), signer) is None
        )
        assert signer.hashes == []

    def test_it_is_signed_over_the_allowlist_row_not_a_hash_from_the_network(self):
        account = fixed_account()
        signer = HashOnlySigner(account)
        build_relay_authorization(verified_for(account.address, account_nonce=3), signer)
        assert signer.hashes == [authorization_digest(8453, RELAY_DELEGATES[8453], 3)]

    def test_chain_zero_is_never_signed(self):
        with pytest.raises(ValueError):
            authorization_digest(0, RELAY_DELEGATES[8453], 0)

    def test_a_signer_without_sign_hash_cannot_delegate(self):
        class TypedOnly:
            def __init__(self, account) -> None:
                self.address = account.address

            def sign_typed_data(self, typed):  # pragma: no cover - not reached
                raise AssertionError

        account = fixed_account()
        with pytest.raises(SignatureRejected) as caught:
            build_relay_authorization(verified_for(account.address), TypedOnly(account))
        assert caught.value.code == "authorization_unavailable"

    def test_a_high_s_authorization_is_refused(self):
        account = fixed_account()
        with pytest.raises(SignatureRejected) as caught:
            build_relay_authorization(verified_for(account.address), Lying(account, high_s))
        assert caught.value.code == "signature_high_s"


class TestAccountSigner:
    def test_it_never_shows_the_key_or_the_full_address(self):
        signer = AccountSigner.from_key(fixed_key())
        text = repr(signer) + str(signer)
        assert fixed_key()[2:] not in text.lower()
        assert fixed_account().address not in text

    def test_it_never_reads_the_environment(self, monkeypatch):
        monkeypatch.setenv("PRIVATE_KEY", fixed_key("22"))
        monkeypatch.setenv("WALLET_PRIVATE_KEY", fixed_key("22"))
        assert AccountSigner(fixed_account()).get_address() == fixed_account().address
        with pytest.raises(TypeError):
            AccountSigner()  # type: ignore[call-arg]

    def test_it_refuses_what_is_not_an_account(self):
        with pytest.raises(TypeError):
            AccountSigner(object())
        assert Account  # eth_account is what it wraps
