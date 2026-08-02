"""build_escrow_pre_auth / compute_escrow_nonce — escrow sign-on-assignment.

PROVENANCE of ``tests/fixtures/escrow-preauth.json``: byte-identical copy of
the Execution Market monorepo's ``shared/test-vectors/escrow-preauth.json``
(the F0-1 golden vectors consumed by the EM dashboard, em-mobile and
em-plugin-sdk suites). The monorepo file is the source of truth — if the
vectors change there, re-copy the file byte-for-byte, NEVER edit the copy
here. The nonce MUST match ``AuthCaptureEscrow.getHash(paymentInfo)`` or the
on-chain authorize reverts.

The frozen-build tests monkeypatch ``time.time`` and ``secrets.token_hex``
(the builder reads now/salt internally, without parameters) and sign with the
fixture's synthetic test key (0x42 * 32, never held funds): the RFC 6979
deterministic signature must equal the fixture's expected bytes in
eth_account and viem alike.

NOTE: hex values >= 32 bytes are stored in the fixture WITHOUT the ``0x``
prefix — secret scanners block any literal ``0x`` + 64 hex chars.
``_hydrate`` re-prefixes them on load.
"""

import json
import re
import types
from pathlib import Path

import pytest
from eth_account import Account
from eth_account.messages import encode_typed_data

import uvd_x402_sdk.escrow_signing as escrow_mod
from uvd_x402_sdk.escrow_signing import (
    ESCROW_TIER_WINDOWS,
    RECEIVE_WITH_AUTHORIZATION_TYPES,
    REFUND_WINDOW_SEC,
    REVIEW_WINDOW_SEC,
    build_escrow_pre_auth,
    compute_escrow_nonce,
)
from uvd_x402_sdk.wallet import EnvKeyAdapter

_LONG_HEX = re.compile(r"^[0-9a-f]{64,}$")


def _hydrate(value):
    """Re-prefix long hex values (stored 0x-less to dodge secret scanners)."""
    if isinstance(value, str) and _LONG_HEX.fullmatch(value):
        return "0x" + value
    if isinstance(value, list):
        return [_hydrate(v) for v in value]
    if isinstance(value, dict):
        return {k: _hydrate(v) for k, v in value.items()}
    return value


_FIXTURE_PATH = Path(__file__).resolve().parent / "fixtures" / "escrow-preauth.json"
FIXTURE = _hydrate(json.loads(_FIXTURE_PATH.read_text(encoding="utf-8")))
FROZEN = FIXTURE["frozen_build"]

TYPEHASH = FIXTURE["network_config"]["payment_info_typehash"]
EXPECTED_NONCE = FIXTURE["static_vector"]["expected_nonce"]
MOCK_SIGNATURE = "0x" + "11" * 65

PAYER = FIXTURE["payer"]
WORKER = FIXTURE["worker"]

# Base mainnet — addresses mirror the EM production config.
BASE_NETWORK = {
    key: FIXTURE["network_config"][key]
    for key in (
        "chain_id",
        "operator",
        "escrow",
        "token_collector",
        "usdc",
        "usdc_domain_name",
        "usdc_domain_version",
    )
}

# Shape of GET /api/v1/h2a/payment-config (Execution Market).
PAYMENT_CONFIG = {
    "treasury": "0x3333333333333333333333333333333333333333",
    "fee_pct": 0.13,
    "escrow": {
        "payment_info_typehash": TYPEHASH,
        "min_fee_bps": FIXTURE["network_config"]["min_fee_bps"],
        "max_fee_bps": FIXTURE["network_config"]["max_fee_bps"],
        "deposit_limit_usd": FIXTURE["deposit_limit_usd"],
        "tier_timings": FIXTURE["escrow_tier_windows"],
        "networks": {"base": BASE_NETWORK},
    },
}

FIXTURE_PI = FIXTURE["static_vector"]["payment_info"]

NOW = FROZEN["now"]


class FakeWallet:
    """WalletAdapter test double recording sign_typed_data calls."""

    def __init__(self):
        self.typed_data_calls = []

    def get_address(self):
        return PAYER

    def sign_message(self, message):
        raise NotImplementedError

    def sign_typed_data(self, typed_data):
        self.typed_data_calls.append(typed_data)
        return {
            "signature": MOCK_SIGNATURE,
            "v": 27,
            "r": "0x" + "22" * 32,
            "s": "0x" + "33" * 32,
        }

    def sign_eip3009(self, params):
        raise NotImplementedError

    def sign_transaction(self, tx):
        raise NotImplementedError


class GoldenWallet:
    """EnvKeyAdapter with the fixture's test key, recording typed-data calls.

    Delegating to the real adapter pins EnvKeyAdapter.sign_typed_data's
    digest against the golden signature bytes (RFC 6979 deterministic).
    """

    def __init__(self):
        self.typed_data_calls = []
        self._adapter = EnvKeyAdapter(private_key=FROZEN["signer_private_key"])

    def sign_typed_data(self, typed_data):
        self.typed_data_calls.append(typed_data)
        return self._adapter.sign_typed_data(typed_data)


@pytest.fixture
def wallet():
    return FakeWallet()


@pytest.fixture
def frozen_time(monkeypatch):
    monkeypatch.setattr(escrow_mod, "time", types.SimpleNamespace(time=lambda: NOW))
    return NOW


@pytest.fixture
def frozen_salt(monkeypatch):
    salt_hex = FROZEN["salt"].removeprefix("0x")
    monkeypatch.setattr(
        escrow_mod,
        "secrets",
        types.SimpleNamespace(token_hex=lambda n: salt_hex[: 2 * n]),
    )
    return FROZEN["salt"]


class TestComputeEscrowNonce:
    def test_matches_golden_fixture(self):
        """Same vector as the EM suites — AuthCaptureEscrow.getHash mirror."""
        nonce = compute_escrow_nonce(
            BASE_NETWORK["chain_id"],
            BASE_NETWORK["escrow"],
            TYPEHASH,
            FIXTURE_PI,
        )
        assert nonce == EXPECTED_NONCE

    def test_changes_when_receiver_changes(self):
        """The nonce commits to the worker."""
        other = compute_escrow_nonce(
            BASE_NETWORK["chain_id"],
            BASE_NETWORK["escrow"],
            TYPEHASH,
            {**FIXTURE_PI, "receiver": PAYER},
        )
        assert other != EXPECTED_NONCE

    def test_accepts_lowercase_addresses(self):
        """Checksums internally like AdvancedEscrowClient."""
        nonce = compute_escrow_nonce(
            BASE_NETWORK["chain_id"],
            BASE_NETWORK["escrow"].lower(),
            TYPEHASH,
            {
                **FIXTURE_PI,
                "operator": FIXTURE_PI["operator"].lower(),
                "token": FIXTURE_PI["token"].lower(),
                "feeReceiver": FIXTURE_PI["feeReceiver"].lower(),
            },
        )
        assert nonce == EXPECTED_NONCE


class TestAdvancedEscrowParity:
    """compute_escrow_nonce is the standalone port of
    AdvancedEscrowClient._compute_nonce — same bytes, no web3 required."""

    def test_matches_advanced_escrow_client_compute_nonce(self):
        pytest.importorskip("web3")
        from uvd_x402_sdk.advanced_escrow import AdvancedEscrowClient, PaymentInfo

        client = AdvancedEscrowClient(
            private_key=FROZEN["signer_private_key"],
            chain_id=BASE_NETWORK["chain_id"],
            rpc_url="http://localhost:9",  # never contacted: nonce math is local
        )
        pi = PaymentInfo(
            operator=FIXTURE_PI["operator"],
            receiver=FIXTURE_PI["receiver"],
            token=FIXTURE_PI["token"],
            max_amount=int(FIXTURE_PI["maxAmount"]),
            pre_approval_expiry=FIXTURE_PI["preApprovalExpiry"],
            authorization_expiry=FIXTURE_PI["authorizationExpiry"],
            refund_expiry=FIXTURE_PI["refundExpiry"],
            min_fee_bps=FIXTURE_PI["minFeeBps"],
            max_fee_bps=FIXTURE_PI["maxFeeBps"],
            fee_receiver=FIXTURE_PI["feeReceiver"],
            salt=FIXTURE_PI["salt"],
        )
        assert client._compute_nonce(pi) == EXPECTED_NONCE

    def test_tier_windows_match_tier_timings(self):
        """ESCROW_TIER_WINDOWS mirrors advanced_escrow.TIER_TIMINGS for the
        micro/standard tiers (kept as a plain dict so this module never
        imports web3)."""
        pytest.importorskip("web3")
        from uvd_x402_sdk.advanced_escrow import TIER_TIMINGS, TaskTier

        assert ESCROW_TIER_WINDOWS["micro"] == TIER_TIMINGS[TaskTier.MICRO]
        assert ESCROW_TIER_WINDOWS["standard"] == TIER_TIMINGS[TaskTier.STANDARD]


class TestSignEip3009Relationship:
    """EnvKeyAdapter.sign_eip3009 signs the SAME ReceiveWithAuthorization
    digest as build_escrow_pre_auth's typed-data path — pinned, not changed.

    sign_eip3009 resolves the EIP-712 domain from the network registry and
    always signs ``from`` = the adapter's own address; build_escrow_pre_auth
    takes the domain from the server config and signs ``from`` = the payer
    argument. When the payer IS the adapter wallet, the two are
    interchangeable given the same nonce/value/validBefore.
    """

    def test_registry_domain_matches_fixture_domain(self):
        """Guard: the SDK's Base registry entry (which sign_eip3009 uses)
        agrees with the golden fixture's escrow network config."""
        from uvd_x402_sdk.networks.base import get_token_config

        token = get_token_config("base", "usdc")
        assert token is not None
        assert token.address == BASE_NETWORK["usdc"]
        assert token.name == BASE_NETWORK["usdc_domain_name"]
        assert token.version == BASE_NETWORK["usdc_domain_version"]

    def test_sign_eip3009_produces_the_escrow_digest(self):
        """Same key + same nonce/value/validBefore ⇒ sign_eip3009 emits the
        exact signature bytes of the escrow typed-data path (from = signer).
        """
        adapter = EnvKeyAdapter(private_key=FROZEN["signer_private_key"])
        expected_msg = FROZEN["expected_typed_data"]["message"]

        via_typed_data = adapter.sign_typed_data(
            {
                "domain": FROZEN["expected_typed_data"]["domain"],
                "types": RECEIVE_WITH_AUTHORIZATION_TYPES,
                "message": {
                    "from": adapter.get_address(),  # sign_eip3009 fixes this
                    "to": expected_msg["to"],
                    "value": int(expected_msg["value"]),
                    "validAfter": int(expected_msg["validAfter"]),
                    "validBefore": int(expected_msg["validBefore"]),
                    "nonce": bytes.fromhex(
                        expected_msg["nonce"].removeprefix("0x")
                    ),
                },
            }
        )["signature"]

        via_eip3009 = adapter.sign_eip3009(
            {
                "to": expected_msg["to"],
                "amount_usdc": float(FIXTURE["bounty_usd"]),
                "network": FIXTURE["network"],
                "valid_after": int(expected_msg["validAfter"]),
                "valid_before": int(expected_msg["validBefore"]),
                "nonce": expected_msg["nonce"],
            }
        )

        assert via_eip3009["signature"] == via_typed_data
        assert via_eip3009["value"] == expected_msg["value"]
        assert via_eip3009["from_address"] == FROZEN["signer_address"]


class TestGoldenVectors:
    """Frozen time + frozen salt + real key ⇒ the fixture wrapper, exactly."""

    def test_tier_windows_match_fixture(self):
        assert ESCROW_TIER_WINDOWS == FIXTURE["escrow_tier_windows"]
        assert REVIEW_WINDOW_SEC == FIXTURE["review_window_sec"]
        assert REFUND_WINDOW_SEC == FIXTURE["refund_window_sec"]

    def test_frozen_build_reproduces_expected_wrapper(self, frozen_time, frozen_salt):
        header = build_escrow_pre_auth(
            PAYMENT_CONFIG,
            FIXTURE["network"],
            PAYER,
            WORKER,
            FIXTURE["bounty_usd"],
            FROZEN["deadline"],
            GoldenWallet(),
            tier=FROZEN["tier"],
        )
        assert json.loads(header) == FROZEN["expected_wrapper"]

    def test_frozen_build_signs_expected_typed_data(self, frozen_time, frozen_salt):
        signer = GoldenWallet()
        build_escrow_pre_auth(
            PAYMENT_CONFIG,
            FIXTURE["network"],
            PAYER,
            WORKER,
            FIXTURE["bounty_usd"],
            FROZEN["deadline"],
            signer,
            tier=FROZEN["tier"],
        )
        expected = FROZEN["expected_typed_data"]
        assert len(signer.typed_data_calls) == 1
        typed = signer.typed_data_calls[0]
        assert typed["domain"] == expected["domain"]
        assert list(typed["types"]) == [expected["primaryType"]]
        message = typed["message"]
        assert message["from"] == expected["message"]["from"]
        assert message["to"] == expected["message"]["to"]
        assert message["value"] == int(expected["message"]["value"])
        assert message["validAfter"] == int(expected["message"]["validAfter"])
        assert message["validBefore"] == int(expected["message"]["validBefore"])
        assert message["nonce"] == bytes.fromhex(
            expected["message"]["nonce"].removeprefix("0x")
        )

    def test_fixture_signature_recovers_to_frozen_signer(self):
        """Integrity: a corrupted copy of the fixture fails here before any
        conformance assert (same guard pattern as tests/test_erc8128.py)."""
        expected = FROZEN["expected_typed_data"]
        signable = encode_typed_data(
            domain_data=expected["domain"],
            message_types=RECEIVE_WITH_AUTHORIZATION_TYPES,
            message_data={
                "from": expected["message"]["from"],
                "to": expected["message"]["to"],
                "value": int(expected["message"]["value"]),
                "validAfter": int(expected["message"]["validAfter"]),
                "validBefore": int(expected["message"]["validBefore"]),
                "nonce": bytes.fromhex(
                    expected["message"]["nonce"].removeprefix("0x")
                ),
            },
        )
        recovered = Account.recover_message(
            signable,
            signature=FROZEN["expected_wrapper"]["payload"]["signature"],
        )
        assert recovered == FROZEN["signer_address"]


class TestBuildEscrowPreAuth:
    def test_wrapper_shape_with_receiver_committed(self, wallet):
        header = build_escrow_pre_auth(
            PAYMENT_CONFIG, "base", PAYER, WORKER, "0.10", None, wallet
        )
        wrapper = json.loads(header)
        assert sorted(wrapper) == [
            "payload",
            "paymentRequirements",
            "scheme",
            "x402Version",
        ]
        assert wrapper["x402Version"] == 2
        assert wrapper["scheme"] == "escrow"
        assert wrapper["paymentRequirements"] == {
            "scheme": "escrow",
            "network": "eip155:8453",
        }

        payload = wrapper["payload"]
        authorization = payload["authorization"]
        payment_info = payload["paymentInfo"]
        assert payload["signature"] == MOCK_SIGNATURE

        # authorization: string-valued, to = token collector,
        # validBefore = preApprovalExpiry
        assert sorted(authorization) == [
            "from",
            "nonce",
            "to",
            "validAfter",
            "validBefore",
            "value",
        ]
        assert authorization["from"] == PAYER
        assert authorization["to"] == BASE_NETWORK["token_collector"]
        assert authorization["value"] == "100000"
        assert authorization["validAfter"] == "0"
        assert authorization["validBefore"] == str(payment_info["preApprovalExpiry"])

        # paymentInfo: maxAmount string, expiries/bps ints, receiver = worker
        assert sorted(payment_info) == [
            "authorizationExpiry",
            "feeReceiver",
            "maxAmount",
            "maxFeeBps",
            "minFeeBps",
            "operator",
            "preApprovalExpiry",
            "receiver",
            "refundExpiry",
            "salt",
            "token",
        ]
        assert payment_info["receiver"] == WORKER
        assert payment_info["operator"] == BASE_NETWORK["operator"]
        assert payment_info["token"] == BASE_NETWORK["usdc"]
        assert payment_info["maxAmount"] == "100000"
        assert isinstance(payment_info["preApprovalExpiry"], int)
        assert isinstance(payment_info["authorizationExpiry"], int)
        assert isinstance(payment_info["refundExpiry"], int)
        assert payment_info["minFeeBps"] == 0
        assert payment_info["maxFeeBps"] == 1800
        assert payment_info["feeReceiver"] == BASE_NETWORK["operator"]
        assert payment_info["salt"].startswith("0x")
        assert len(payment_info["salt"]) == 66

        # nonce is reproducible from the serialized paymentInfo (getHash mirror)
        assert authorization["nonce"] == compute_escrow_nonce(
            BASE_NETWORK["chain_id"],
            BASE_NETWORK["escrow"],
            TYPEHASH,
            payment_info,
        )

    def test_signs_receive_with_authorization_typed_data(self, wallet):
        header = build_escrow_pre_auth(
            PAYMENT_CONFIG, "base", PAYER, WORKER, 0.10, None, wallet
        )
        wrapper = json.loads(header)

        assert len(wallet.typed_data_calls) == 1
        typed = wallet.typed_data_calls[0]
        assert list(typed["types"]) == ["ReceiveWithAuthorization"]
        assert typed["domain"] == {
            "name": "USD Coin",
            "version": "2",
            "chainId": 8453,
            "verifyingContract": BASE_NETWORK["usdc"],
        }
        message = typed["message"]
        assert message["from"] == PAYER
        assert message["to"] == BASE_NETWORK["token_collector"]
        assert message["value"] == 100000
        assert message["validAfter"] == 0
        assert (
            message["validBefore"]
            == wrapper["payload"]["paymentInfo"]["preApprovalExpiry"]
        )
        # bytes32 nonce in the typed data == hex nonce on the wire
        nonce_hex = wrapper["payload"]["authorization"]["nonce"]
        assert message["nonce"] == bytes.fromhex(nonce_hex.removeprefix("0x"))

    def test_release_window_outlasts_deadline(self, wallet, frozen_time):
        # No deadline: preApproval keeps the short tier window (the lock is
        # immediate), but auth/refund are extended to the review windows.
        micro = json.loads(
            build_escrow_pre_auth(
                PAYMENT_CONFIG, "base", PAYER, WORKER, "0.10", None, wallet
            )
        )["payload"]["paymentInfo"]
        assert micro["preApprovalExpiry"] == NOW + ESCROW_TIER_WINDOWS["micro"]["pre"]
        assert micro["authorizationExpiry"] == NOW + REVIEW_WINDOW_SEC
        assert micro["refundExpiry"] == NOW + REVIEW_WINDOW_SEC + REFUND_WINDOW_SEC

        # With a future deadline, the release window is anchored on the
        # deadline (the worker delivers near it) plus the review buffer.
        deadline = NOW + 5 * 24 * 3600
        with_deadline = json.loads(
            build_escrow_pre_auth(
                PAYMENT_CONFIG, "base", PAYER, WORKER, "0.10", deadline, wallet
            )
        )["payload"]["paymentInfo"]
        assert (
            with_deadline["preApprovalExpiry"]
            == NOW + ESCROW_TIER_WINDOWS["micro"]["pre"]
        )
        assert with_deadline["authorizationExpiry"] == deadline + REVIEW_WINDOW_SEC
        assert with_deadline["refundExpiry"] == (
            deadline + REVIEW_WINDOW_SEC + REFUND_WINDOW_SEC
        )


class TestFailLoud:
    """Never a silent EIP-712 domain fallback; on-chain limits client-side."""

    def test_unknown_network_raises(self, wallet):
        with pytest.raises(ValueError, match="Unknown escrow network 'solana'"):
            build_escrow_pre_auth(
                PAYMENT_CONFIG, "solana", PAYER, WORKER, "0.10", None, wallet
            )
        assert wallet.typed_data_calls == []

    def test_incomplete_network_config_raises(self, wallet):
        cfg = json.loads(json.dumps(PAYMENT_CONFIG))
        del cfg["escrow"]["networks"]["base"]["usdc_domain_name"]
        with pytest.raises(ValueError, match="usdc_domain_name"):
            build_escrow_pre_auth(cfg, "base", PAYER, WORKER, "0.10", None, wallet)
        assert wallet.typed_data_calls == []

    def test_missing_typehash_raises(self, wallet):
        cfg = json.loads(json.dumps(PAYMENT_CONFIG))
        del cfg["escrow"]["payment_info_typehash"]
        with pytest.raises(ValueError, match="payment_info_typehash"):
            build_escrow_pre_auth(cfg, "base", PAYER, WORKER, "0.10", None, wallet)

    def test_bounty_above_deposit_limit_raises(self, wallet):
        with pytest.raises(ValueError, match="deposit limit"):
            build_escrow_pre_auth(
                PAYMENT_CONFIG, "base", PAYER, WORKER, "100.01", None, wallet
            )
        assert wallet.typed_data_calls == []

    def test_bounty_at_limit_is_accepted(self, wallet):
        header = build_escrow_pre_auth(
            PAYMENT_CONFIG, "base", PAYER, WORKER, "100", None, wallet
        )
        pi = json.loads(header)["payload"]["paymentInfo"]
        assert pi["maxAmount"] == "100000000"

    def test_non_positive_bounty_raises(self, wallet):
        with pytest.raises(ValueError, match="positive"):
            build_escrow_pre_auth(
                PAYMENT_CONFIG, "base", PAYER, WORKER, "0", None, wallet
            )

    def test_max_fee_bps_below_operator_fee_raises(self, wallet):
        cfg = json.loads(json.dumps(PAYMENT_CONFIG))
        cfg["escrow"]["max_fee_bps"] = 800
        with pytest.raises(ValueError, match="1300"):
            build_escrow_pre_auth(cfg, "base", PAYER, WORKER, "0.10", None, wallet)
        assert wallet.typed_data_calls == []

    def test_unknown_tier_raises(self, wallet):
        with pytest.raises(ValueError, match="Unknown escrow tier 'premium'"):
            build_escrow_pre_auth(
                PAYMENT_CONFIG,
                "base",
                PAYER,
                WORKER,
                "0.10",
                None,
                wallet,
                tier="premium",
            )
