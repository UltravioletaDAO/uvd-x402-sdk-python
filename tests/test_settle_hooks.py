"""Settle hooks: asset / EIP-712 domain overrides, opt-in retry, try_settle_payment.

Why these exist: marketplace backends (Execution Market F6-15) settle non-USDC
stablecoins through this SDK, and their token registry is the source of truth —
not the SDK's. ``settle_payment`` used to hardcode the network's USDC address and
domain, so a non-USDC settle was impossible without hand-building the facilitator
request outside the SDK. Registries also drift: ``get_token_config("skale", ...)``
returns None today because the alias is not normalised, and domain names have
diverged across snapshots before ("USD₮0" vs "Tether USD"). The caller MUST be
able to inject both the asset and the domain.

The retry policy is ported from Execution Market's facilitator policy
(``mcp_server/integrations/_http_retry.py``). Its one non-negotiable rule is the
anti-double-settle guard: a 5xx whose body already carries a tx hash means the
facilitator BROADCAST the transaction — retrying would move the money twice.

Everything here is opt-in. The default path must stay byte-identical: existing
callers (KK swarm, public agents) never asked for any of this.
"""
from __future__ import annotations

import json
import types
from decimal import Decimal

import httpx
import pytest

import uvd_x402_sdk.client as client_mod
from uvd_x402_sdk import X402Client
from uvd_x402_sdk.client import (
    SETTLE_RETRY_ATTEMPTS,
    _extract_tx_hash_from_body,
)
from uvd_x402_sdk.exceptions import (
    FacilitatorError,
    PaymentSettlementError,
)
from uvd_x402_sdk.exceptions import (
    TimeoutError as X402TimeoutError,
)
from uvd_x402_sdk.models import PaymentPayload

RECIPIENT = "0x1234567890123456789012345678901234567890"
BASE_USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
# A token address the SDK registry does NOT know — the whole point of the hook.
CUSTOM_ASSET = "0x01bFF41798a0BcF287b996046Ca68b395DbC1071"
CUSTOM_DOMAIN = {"name": "USD₮0", "version": "1"}

TEST_KEY = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"


def _evm_payload(network: str = "base") -> PaymentPayload:
    return PaymentPayload(
        x402Version=1,
        scheme="exact",
        network=network,
        payload={
            "signature": "0xsig",
            "authorization": {
                "from": "0xSender",
                "to": RECIPIENT,
                "value": "10000",
                "validAfter": "0",
                "validBefore": "9999999999",
                "nonce": "0x01",
            },
        },
    )


class _FakeResponse:
    def __init__(self, status_code: int = 200, body: dict | None = None):
        self.status_code = status_code
        self._body = body if body is not None else {}
        self.text = json.dumps(self._body)

    def json(self) -> dict:
        return self._body


class _FakeHttpClient:
    """Stands in for httpx.Client: records every POST, replays a script.

    Script items are either a _FakeResponse to return or an Exception to raise
    — exactly the two things the real transport can do to settle_payment.
    """

    def __init__(self, script):
        self.script = list(script)
        self.calls: list[dict] = []

    def post(self, url, json=None, headers=None, timeout=None):
        self.calls.append({"url": url, "json": json, "timeout": timeout})
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _ok_settle_body(tx: str = "0xf00d") -> dict:
    return {"success": True, "transaction": tx, "payer": "0xSender"}


@pytest.fixture
def client():
    return X402Client(recipient_address=RECIPIENT)


def _wire(client, monkeypatch, script) -> _FakeHttpClient:
    fake = _FakeHttpClient(script)
    monkeypatch.setattr(client, "_get_http_client", lambda: fake)
    return fake


def _record_sleeps(monkeypatch) -> list:
    sleeps: list = []
    monkeypatch.setattr(client_mod.time, "sleep", sleeps.append)
    return sleeps


# ── Requirement overrides (asset / eip712_domain) ────────────────────────────


class TestRequirementOverrides:
    """_build_payment_requirements: overrides win, defaults are untouched."""

    def test_defaults_are_unchanged(self, client):
        """The no-override path is what every existing caller runs. Pin it."""
        req = client._build_payment_requirements(_evm_payload(), Decimal("0.01"))
        assert req.asset == BASE_USDC
        assert req.extra == {"name": "USD Coin", "version": "2"}

    def test_asset_override_lands_in_requirements(self, client):
        """A non-USDC settle sends the caller's token address as `asset`."""
        req = client._build_payment_requirements(
            _evm_payload(), Decimal("0.01"), asset=CUSTOM_ASSET
        )
        assert req.asset == CUSTOM_ASSET
        # The domain was NOT overridden, so the registry default still applies.
        assert req.extra == {"name": "USD Coin", "version": "2"}

    def test_domain_override_replaces_extra(self, client):
        """The caller's registry is the source of truth for the EIP-712 domain."""
        req = client._build_payment_requirements(
            _evm_payload(), Decimal("0.01"), eip712_domain=CUSTOM_DOMAIN
        )
        assert req.extra == CUSTOM_DOMAIN

    def test_domain_override_is_filtered_to_name_and_version(self, client):
        """Extra keys must not leak onto the wire — `extra` is name/version only."""
        req = client._build_payment_requirements(
            _evm_payload(),
            Decimal("0.01"),
            eip712_domain={"name": "USDC", "version": "2", "chainId": "8453"},
        )
        assert req.extra == {"name": "USDC", "version": "2"}

    @pytest.mark.parametrize(
        "bad",
        [{}, {"name": "USDC"}, {"version": "2"}, {"name": "", "version": "2"}],
        ids=["empty", "no-version", "no-name", "blank-name"],
    )
    def test_partial_domain_fails_loud(self, client, bad):
        """A partial domain would sign/verify against garbage. Fail here, not on-chain."""
        with pytest.raises(ValueError, match="eip712_domain"):
            client._build_payment_requirements(
                _evm_payload(), Decimal("0.01"), eip712_domain=bad
            )

    def test_non_evm_still_gets_no_extra_by_default(self, client):
        """Non-EVM chains never carried EIP-712 params; that must not change."""
        payload = PaymentPayload(
            x402Version=1,
            scheme="exact",
            network="solana",
            payload={"transaction": "SGVsbG8="},
        )
        req = client._build_payment_requirements(payload, Decimal("0.01"))
        assert req.extra is None


class TestOverridesReachTheWire:
    """The overrides must survive into the JSON actually POSTed to the facilitator."""

    def test_settle_posts_overridden_asset_and_domain(self, client, monkeypatch):
        fake = _wire(client, monkeypatch, [_FakeResponse(200, _ok_settle_body())])
        client.settle_payment(
            _evm_payload(), Decimal("0.01"),
            asset=CUSTOM_ASSET, eip712_domain=CUSTOM_DOMAIN,
        )
        sent = fake.calls[0]["json"]["paymentRequirements"]
        assert sent["asset"] == CUSTOM_ASSET
        assert sent["extra"] == CUSTOM_DOMAIN

    def test_verify_posts_overridden_asset_and_domain(self, client, monkeypatch):
        """verify and settle share the requirements builder — a verify that used
        the default USDC asset would reject the very payment settle accepts."""
        fake = _wire(
            client, monkeypatch,
            [_FakeResponse(200, {"isValid": True, "payer": "0xSender"})],
        )
        client.verify_payment(
            _evm_payload(), Decimal("0.01"),
            asset=CUSTOM_ASSET, eip712_domain=CUSTOM_DOMAIN,
        )
        sent = fake.calls[0]["json"]["paymentRequirements"]
        assert sent["asset"] == CUSTOM_ASSET
        assert sent["extra"] == CUSTOM_DOMAIN

    def test_default_wire_shape_is_byte_identical_to_before(self, client, monkeypatch):
        """No overrides -> the exact request every deployed integration sends today."""
        fake = _wire(client, monkeypatch, [_FakeResponse(200, _ok_settle_body())])
        client.settle_payment(_evm_payload(), Decimal("0.01"))
        sent = fake.calls[0]["json"]["paymentRequirements"]
        assert sent["asset"] == BASE_USDC
        assert sent["extra"] == {"name": "USD Coin", "version": "2"}


# ── Opt-in settle retry ──────────────────────────────────────────────────────


class TestSettleRetry:
    """Ported EM policy: retry transient failures, never anything deterministic,
    and NEVER a 5xx that already carries a tx hash."""

    def test_retry_is_off_by_default(self, client, monkeypatch):
        """Exactly one POST, exactly the old exception. Existing callers see
        zero behavior change unless they opt in."""
        fake = _wire(client, monkeypatch, [_FakeResponse(500, {"error": "boom"})])
        with pytest.raises(FacilitatorError):
            client.settle_payment(_evm_payload(), Decimal("0.01"))
        assert len(fake.calls) == 1

    def test_transient_5xx_is_retried_until_it_recovers(self, client, monkeypatch):
        sleeps = _record_sleeps(monkeypatch)
        fake = _wire(client, monkeypatch, [
            _FakeResponse(502, {"error": "bad gateway"}),
            _FakeResponse(503, {"error": "unavailable"}),
            _FakeResponse(200, _ok_settle_body("0xrecovered")),
        ])
        resp = client.settle_payment(_evm_payload(), Decimal("0.01"), retry=True)
        assert resp.get_transaction_hash() == "0xrecovered"
        assert len(fake.calls) == 3
        assert sleeps == [1.0, 2.0]  # exponential: 1s, 2s (EM's tenacity schedule)

    def test_4xx_is_never_retried(self, client, monkeypatch):
        """4xx is deterministic (bad request, auth, idempotency conflict).
        Retrying only amplifies the error."""
        _record_sleeps(monkeypatch)
        fake = _wire(client, monkeypatch, [_FakeResponse(422, {"error": "bad payload"})])
        with pytest.raises(FacilitatorError):
            client.settle_payment(_evm_payload(), Decimal("0.01"), retry=True)
        assert len(fake.calls) == 1

    def test_5xx_with_tx_hash_is_not_retried(self, client, monkeypatch):
        """THE guard. The facilitator can 5xx AFTER broadcasting (a non-fatal
        post-settle hook failed). The body carries the hash; a retry would
        broadcast a second transfer of real money."""
        _record_sleeps(monkeypatch)
        fake = _wire(client, monkeypatch, [
            _FakeResponse(500, {"success": False, "transaction": {"hash": "0xbroadcast"}}),
        ])
        with pytest.raises(FacilitatorError):
            client.settle_payment(_evm_payload(), Decimal("0.01"), retry=True)
        assert len(fake.calls) == 1

    def test_business_failure_in_a_200_is_not_retried(self, client, monkeypatch):
        """success=false inside a 200 is a business error, not a transient one."""
        _record_sleeps(monkeypatch)
        fake = _wire(client, monkeypatch, [
            _FakeResponse(200, {"success": False, "message": "insufficient balance"}),
        ])
        with pytest.raises(PaymentSettlementError):
            client.settle_payment(_evm_payload(), Decimal("0.01"), retry=True)
        assert len(fake.calls) == 1

    def test_exhausted_attempts_reraise_the_last_error(self, client, monkeypatch):
        _record_sleeps(monkeypatch)
        fake = _wire(client, monkeypatch, [
            _FakeResponse(500, {"error": "boom"})
            for _ in range(SETTLE_RETRY_ATTEMPTS)
        ])
        with pytest.raises(FacilitatorError):
            client.settle_payment(_evm_payload(), Decimal("0.01"), retry=True)
        assert len(fake.calls) == SETTLE_RETRY_ATTEMPTS

    def test_network_error_is_retried(self, client, monkeypatch):
        """DNS/connection-reset style failures never reached the facilitator —
        the safest possible retry."""
        _record_sleeps(monkeypatch)
        fake = _wire(client, monkeypatch, [
            httpx.ConnectError("connection refused"),
            _FakeResponse(200, _ok_settle_body()),
        ])
        resp = client.settle_payment(_evm_payload(), Decimal("0.01"), retry=True)
        assert resp.success is True
        assert len(fake.calls) == 2

    def test_timeout_is_retried_after_the_fallback_check(self, client, monkeypatch):
        """A timeout first runs the existing on-chain fallback check (its own
        POST). Only when that also fails does the retry policy kick in —
        facilitator idempotency per EIP-3009 nonce makes the re-send safe."""
        _record_sleeps(monkeypatch)
        fake = _wire(client, monkeypatch, [
            httpx.ReadTimeout("settle timed out"),   # attempt 1: settle POST
            httpx.ReadTimeout("fallback timed out"),  # attempt 1: fallback POST
            _FakeResponse(200, _ok_settle_body()),    # attempt 2: settle POST
        ])
        resp = client.settle_payment(_evm_payload(), Decimal("0.01"), retry=True)
        assert resp.success is True
        assert len(fake.calls) == 3

    def test_retry_off_timeout_behavior_is_unchanged(self, client, monkeypatch):
        """Without the flag, a timeout still raises after the fallback check —
        the pre-existing contract."""
        fake = _wire(client, monkeypatch, [
            httpx.ReadTimeout("settle timed out"),
            httpx.ReadTimeout("fallback timed out"),
        ])
        with pytest.raises(X402TimeoutError):
            client.settle_payment(_evm_payload(), Decimal("0.01"))
        assert len(fake.calls) == 2


class TestTxHashDetection:
    """Every shape the facilitator uses to report a hash must trip the guard.
    Missing one means that shape gets retried — and double-settled."""

    @pytest.mark.parametrize(
        "body",
        [
            {"transaction": "0xabc"},
            {"transaction": {"hash": "0xabc"}},
            {"txHash": "0xabc"},
            {"tx_hash": "0xabc"},
            {"transaction_hash": "0xabc"},
        ],
        ids=["tx-str", "tx-obj", "txHash", "tx_hash", "transaction_hash"],
    )
    def test_detects_every_known_shape(self, body):
        assert _extract_tx_hash_from_body(body) == "0xabc"

    @pytest.mark.parametrize(
        "body",
        [{}, {"transaction": None}, {"transaction": ""}, {"transaction": {}},
         {"error": "boom"}, "not a dict", None, [1, 2]],
        ids=["empty", "null-tx", "blank-tx", "empty-obj", "no-tx", "str", "none", "list"],
    )
    def test_no_hash_means_none(self, body):
        assert _extract_tx_hash_from_body(body) is None


# ── Non-raising settle (try_settle_payment) ──────────────────────────────────


class TestTrySettlePayment:
    """Result-dict mode for callers that treat settle failures as data."""

    def test_success_shape(self, client, monkeypatch):
        _wire(client, monkeypatch, [_FakeResponse(200, _ok_settle_body("0xf00d"))])
        result = client.try_settle_payment(_evm_payload(), Decimal("0.01"))
        assert result == {"success": True, "tx_hash": "0xf00d", "error": None}

    def test_failure_does_not_raise(self, client, monkeypatch):
        _wire(client, monkeypatch, [_FakeResponse(422, {"error": "bad payload"})])
        result = client.try_settle_payment(_evm_payload(), Decimal("0.01"))
        assert result["success"] is False
        assert result["tx_hash"] is None
        assert "422" in result["error"]

    def test_5xx_with_hash_surfaces_the_broadcast_tx(self, client, monkeypatch):
        """success=False AND tx_hash set is the double-settle warning shape:
        the money may have moved. The caller must verify on-chain, not re-send."""
        _wire(client, monkeypatch, [
            _FakeResponse(500, {"success": False, "transaction": {"hash": "0xbroadcast"}}),
        ])
        result = client.try_settle_payment(_evm_payload(), Decimal("0.01"))
        assert result["success"] is False
        assert result["tx_hash"] == "0xbroadcast"

    def test_business_failure_carries_the_message(self, client, monkeypatch):
        _wire(client, monkeypatch, [
            _FakeResponse(200, {"success": False, "message": "insufficient balance"}),
        ])
        result = client.try_settle_payment(_evm_payload(), Decimal("0.01"))
        assert result["success"] is False
        assert "insufficient balance" in result["error"]

    def test_retry_flag_passes_through(self, client, monkeypatch):
        _record_sleeps(monkeypatch)
        fake = _wire(client, monkeypatch, [
            _FakeResponse(500, {"error": "boom"}),
            _FakeResponse(200, _ok_settle_body()),
        ])
        result = client.try_settle_payment(_evm_payload(), Decimal("0.01"), retry=True)
        assert result["success"] is True
        assert len(fake.calls) == 2


# ── create_authorization: the domain enters the signature ────────────────────


class _RecordingSigner:
    """Captures the EIP-712 domain the client asks it to sign — the only way to
    observe what would be hashed without re-implementing eth-account."""

    address = "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266"

    def __init__(self):
        self.domains: list = []

    def sign_typed_data(self, domain, types, message) -> str:
        self.domains.append(domain)
        return "0x" + "ab" * 65


class TestCreateAuthorizationDomainOverride:
    def _connected(self):
        c = X402Client(recipient_address=RECIPIENT)
        signer = _RecordingSigner()
        c.connect_with_signer(signer, chain_name="base")
        return c, signer

    def test_default_domain_comes_from_the_registry(self):
        c, signer = self._connected()
        c.create_authorization(pay_to=RECIPIENT, amount_usd=Decimal("0.01"))
        domain = signer.domains[0]
        assert domain["name"] == "USD Coin"
        assert domain["version"] == "2"
        assert domain["verifyingContract"] == BASE_USDC

    def test_override_changes_what_gets_signed(self):
        """The domain is part of the digest: injecting the verifier's domain is
        the ONLY way to produce a signature it accepts when registries drift."""
        c, signer = self._connected()
        c.create_authorization(
            pay_to=RECIPIENT, amount_usd=Decimal("0.01"),
            eip712_domain={"name": "USDC", "version": "2"},
        )
        domain = signer.domains[0]
        assert domain["name"] == "USDC"
        assert domain["version"] == "2"
        # The contract address is NOT part of the override — registry still rules.
        assert domain["verifyingContract"] == BASE_USDC
        assert domain["chainId"] == 8453

    def test_partial_override_fails_before_signing(self):
        """An authorization is bearer money once transmitted — refuse to sign
        against a half-specified domain."""
        c, signer = self._connected()
        with pytest.raises(ValueError, match="eip712_domain"):
            c.create_authorization(
                pay_to=RECIPIENT, amount_usd=Decimal("0.01"),
                eip712_domain={"name": "USDC"},
            )
        assert signer.domains == []  # never reached the signer

    def test_non_usdc_token_block_carries_the_effective_domain(self):
        """The eip712 block in the payload exists so the verifier resolves the
        SAME domain the signature used. Emitting the registry values under an
        override would re-create the exact mismatch the override fixes."""
        import base64

        c, _ = self._connected()
        header = c.create_authorization(
            pay_to=RECIPIENT, amount_usd=Decimal("0.01"),
            token_type="eurc",
            eip712_domain={"name": "Euro Coin", "version": "2"},
        )
        payload = json.loads(base64.b64decode(header))
        assert payload["payload"]["token"]["eip712"] == {
            "name": "Euro Coin", "version": "2",
        }

    def test_override_actually_changes_the_signature_bytes(self, monkeypatch):
        """With time and nonce frozen, the only free variable is the domain. If
        the two signatures matched, the override never entered the digest."""
        pytest.importorskip("eth_account")
        monkeypatch.setattr(
            client_mod, "os", types.SimpleNamespace(urandom=lambda n: b"\x11" * n)
        )
        monkeypatch.setattr(
            client_mod, "time",
            types.SimpleNamespace(time=lambda: 1_754_000_000, sleep=lambda s: None),
        )
        import base64

        c = X402Client(recipient_address=RECIPIENT)
        c.connect_with_private_key(TEST_KEY, chain_name="base")

        def _sig(**kwargs) -> str:
            header = c.create_authorization(
                pay_to=RECIPIENT, amount_usd=Decimal("0.01"), **kwargs
            )
            return json.loads(base64.b64decode(header))["payload"]["signature"]

        default_sig = _sig()
        overridden_sig = _sig(eip712_domain={"name": "USDC", "version": "2"})
        assert default_sig != overridden_sig
        # Determinism check: same inputs, same signature.
        assert _sig() == default_sig
