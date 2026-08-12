"""Per-network facilitator routing: one client, several facilitators.

Why this exists: NomiCheck (Execution Market's first credited external
integrator) settles `base` through Coinbase's CDP facilitator and `avalanche`
through the Ultravioleta one, because CDP does not settle avalanche. The SDK
only knew a single `facilitator_url`, and `FACILITATOR_URLS` mapped ENVIRONMENT
("production"), not network — so they kept a routing layer of their own on top
of the SDK. This is that routing table, inside the SDK.

Three properties are load-bearing, and each has tests below:

1. **Routing is explicit.** `facilitator_by_network` maps network -> facilitator.
   Not configured = the old single-facilitator behavior, byte-identical.
2. **Translation refuses to guess.** A network that is not in the table and has
   no `"*"` fallback raises. It is NEVER routed to `facilitator_url` silently —
   settling on the wrong facilitator is a money bug, not a config nit.
3. **Boot fails early.** An enabled network with no route raises in the
   constructor. With `verify_facilitator_support=True` the client also proves,
   against each facilitator's `/supported`, that it really settles what is
   routed to it — at boot, not on the first payment.
"""
from __future__ import annotations

import json
from decimal import Decimal

import pytest

from uvd_x402_sdk import X402Client, X402Config
from uvd_x402_sdk.config import FACILITATOR_FALLBACK_KEY
from uvd_x402_sdk.exceptions import ConfigurationError, FacilitatorError
from uvd_x402_sdk.models import PaymentPayload

RECIPIENT = "0x1234567890123456789012345678901234567890"
CDP = "https://api.cdp.coinbase.com/platform/v2/x402"
UVD = "https://facilitator.ultravioletadao.xyz"

# The NomiCheck configuration, verbatim.
NOMICHECK_ROUTES = {"base": CDP, "avalanche": UVD}
NOMICHECK_NETWORKS = ["base", "avalanche"]


def _payload(network: str) -> PaymentPayload:
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

    def raise_for_status(self) -> None:
        return None

    @property
    def is_success(self) -> bool:
        return 200 <= self.status_code < 300


class _FakeHttpClient:
    """Records every request URL and replays a scripted response per URL path."""

    def __init__(self, script=None, by_url=None):
        self.script = list(script or [])
        self.by_url = dict(by_url or {})
        self.calls: list[dict] = []

    def _respond(self, url):
        if url in self.by_url:
            item = self.by_url[url]
        elif self.script:
            item = self.script.pop(0)
        else:
            item = _FakeResponse(200, {})
        if isinstance(item, Exception):
            raise item
        return item

    def post(self, url, json=None, headers=None, timeout=None):
        self.calls.append({"url": url, "json": json, "timeout": timeout})
        return self._respond(url)

    def get(self, url, timeout=None):
        self.calls.append({"url": url, "timeout": timeout})
        return self._respond(url)

    @property
    def urls(self) -> list:
        return [c["url"] for c in self.calls]


def _wire(client, monkeypatch, **kwargs) -> _FakeHttpClient:
    fake = _FakeHttpClient(**kwargs)
    monkeypatch.setattr(client, "_get_http_client", lambda: fake)
    return fake


def _ok_settle_body(tx: str = "0xf00d") -> dict:
    return {"success": True, "transaction": tx, "payer": "0xSender"}


def _ok_verify_body() -> dict:
    return {"isValid": True, "payer": "0xSender"}


def _nomicheck_client(**kwargs) -> X402Client:
    return X402Client(
        recipient_address=RECIPIENT,
        supported_networks=list(NOMICHECK_NETWORKS),
        facilitator_by_network=dict(NOMICHECK_ROUTES),
        **kwargs,
    )


# ── Property 1: the routing table is expressible ─────────────────────────────


class TestRoutingTable:
    def test_nomicheck_case_is_expressible(self):
        """base -> CDP, avalanche -> UVD. The whole point of the feature."""
        config = X402Config(
            recipient_evm=RECIPIENT,
            supported_networks=list(NOMICHECK_NETWORKS),
            facilitator_by_network=dict(NOMICHECK_ROUTES),
        )
        assert config.facilitator_url_for("base") == CDP
        assert config.facilitator_url_for("avalanche") == UVD

    def test_caip2_and_aliases_resolve_to_the_same_route(self):
        """`eip155:8453` and `base` are the same network; they must route alike."""
        config = X402Config(
            recipient_evm=RECIPIENT,
            supported_networks=list(NOMICHECK_NETWORKS),
            facilitator_by_network=dict(NOMICHECK_ROUTES),
        )
        assert config.facilitator_url_for("eip155:8453") == CDP
        assert config.facilitator_url_for("BASE") == CDP

    def test_caip2_key_routes_a_v1_network_name(self):
        """The table may be written in CAIP-2; lookups by v1 name still hit it."""
        config = X402Config(
            recipient_evm=RECIPIENT,
            supported_networks=["base"],
            facilitator_by_network={"eip155:8453": CDP},
        )
        assert config.facilitator_url_for("base") == CDP

    def test_trailing_slash_is_stripped(self):
        """Otherwise the request URL becomes `.../x402//settle`."""
        config = X402Config(
            recipient_evm=RECIPIENT,
            supported_networks=["base"],
            facilitator_by_network={"base": CDP + "/"},
        )
        assert config.facilitator_url_for("base") == CDP

    def test_fallback_key_covers_everything_unnamed(self):
        config = X402Config(
            recipient_evm=RECIPIENT,
            facilitator_by_network={"base": CDP, FACILITATOR_FALLBACK_KEY: UVD},
        )
        assert config.facilitator_url_for("base") == CDP
        assert config.facilitator_url_for("polygon") == UVD
        assert config.facilitator_url_for("solana") == UVD

    def test_routes_are_reported_for_every_enabled_network(self):
        config = X402Config(
            recipient_evm=RECIPIENT,
            supported_networks=list(NOMICHECK_NETWORKS),
            facilitator_by_network=dict(NOMICHECK_ROUTES),
        )
        assert config.facilitator_routes() == {"base": CDP, "avalanche": UVD}

    def test_to_dict_carries_the_table(self):
        config = X402Config(
            recipient_evm=RECIPIENT,
            supported_networks=list(NOMICHECK_NETWORKS),
            facilitator_by_network=dict(NOMICHECK_ROUTES),
        )
        assert config.to_dict()["facilitator_by_network"] == NOMICHECK_ROUTES


# ── Property 2: translation refuses to guess ─────────────────────────────────


class TestRefusesToGuess:
    def test_unrouted_network_raises_instead_of_falling_back(self):
        """The bug this prevents: settling polygon on CDP because it was the default."""
        config = X402Config(
            recipient_evm=RECIPIENT,
            supported_networks=list(NOMICHECK_NETWORKS),
            facilitator_by_network=dict(NOMICHECK_ROUTES),
        )
        with pytest.raises(ConfigurationError) as exc:
            config.facilitator_url_for("polygon")
        assert "polygon" in str(exc.value)
        # The message must name the escape hatches, not just complain.
        assert "avalanche" in str(exc.value) and "base" in str(exc.value)

    def test_unrouted_network_does_not_leak_the_default_facilitator(self):
        config = X402Config(
            recipient_evm=RECIPIENT,
            facilitator_url=UVD,
            supported_networks=["base"],
            facilitator_by_network={"base": CDP},
        )
        with pytest.raises(ConfigurationError):
            config.facilitator_url_for("ethereum")

    def test_settle_on_an_unrouted_network_never_reaches_the_wire(self, monkeypatch):
        """Fail before the POST, not after money moved on the wrong facilitator."""
        client = X402Client(
            recipient_address=RECIPIENT,
            supported_networks=["base", "polygon"],
            facilitator_by_network={"base": CDP, "polygon": UVD},
        )
        fake = _wire(client, monkeypatch, script=[_FakeResponse(200, _ok_settle_body())])
        # ethereum is not in supported_networks: validate_network rejects it first.
        with pytest.raises(Exception):
            client.settle_payment(_payload("ethereum"), Decimal("0.01"))
        assert fake.calls == []


# ── Property 3: boot fails early ─────────────────────────────────────────────


class TestFailFastAtBoot:
    def test_enabled_network_without_a_route_raises_in_the_constructor(self):
        """The default supported_networks is 25 chains — routing two of them and
        leaving 23 unrouted is a config error, and it surfaces at boot."""
        with pytest.raises(ConfigurationError) as exc:
            X402Config(recipient_evm=RECIPIENT, facilitator_by_network=dict(NOMICHECK_ROUTES))
        message = str(exc.value)
        assert "no facilitator" in message
        assert "polygon" in message  # names the offenders
        assert FACILITATOR_FALLBACK_KEY in message  # names the escape hatch

    def test_narrowing_supported_networks_satisfies_the_check(self):
        X402Config(
            recipient_evm=RECIPIENT,
            supported_networks=list(NOMICHECK_NETWORKS),
            facilitator_by_network=dict(NOMICHECK_ROUTES),
        )

    def test_fallback_key_satisfies_the_check(self):
        X402Config(
            recipient_evm=RECIPIENT,
            facilitator_by_network={"base": CDP, FACILITATOR_FALLBACK_KEY: UVD},
        )

    def test_disabled_network_does_not_need_a_route(self):
        from uvd_x402_sdk.config import NetworkRecipientConfig

        X402Config(
            recipient_evm=RECIPIENT,
            supported_networks=["base", "polygon"],
            network_configs={"polygon": NetworkRecipientConfig(recipient=RECIPIENT, enabled=False)},
            facilitator_by_network={"base": CDP},
        )

    def test_unknown_network_key_raises(self):
        with pytest.raises(ConfigurationError, match="unknown network key"):
            X402Config(
                recipient_evm=RECIPIENT,
                supported_networks=["base"],
                facilitator_by_network={"base": CDP, "bitcoin": UVD},
            )

    @pytest.mark.parametrize(
        "bad", ["", "facilitator.example.com", "ftp://facilitator", None, 42],
        ids=["empty", "no-scheme", "wrong-scheme", "none", "int"],
    )
    def test_non_http_url_raises(self, bad):
        with pytest.raises(ConfigurationError, match="http"):
            X402Config(
                recipient_evm=RECIPIENT,
                supported_networks=["base"],
                facilitator_by_network={"base": bad},
            )

    def test_two_spellings_of_one_network_disagreeing_raises(self):
        """`base` -> CDP and `eip155:8453` -> UVD is ambiguous. Refuse it."""
        with pytest.raises(ConfigurationError, match="two different"):
            X402Config(
                recipient_evm=RECIPIENT,
                supported_networks=["base"],
                facilitator_by_network={"base": CDP, "eip155:8453": UVD},
            )

    def test_two_spellings_agreeing_is_fine(self):
        config = X402Config(
            recipient_evm=RECIPIENT,
            supported_networks=["base"],
            facilitator_by_network={"base": CDP, "eip155:8453": CDP},
        )
        assert config.facilitator_url_for("base") == CDP


class TestVerifyRoutesAgainstSupported:
    """`verify_facilitator_support=True`: prove each facilitator settles its networks."""

    def _supported(self, *networks) -> _FakeResponse:
        return _FakeResponse(
            200, {"kinds": [{"network": n, "scheme": "exact"} for n in networks]}
        )

    def test_boot_passes_when_each_facilitator_advertises_its_networks(self, monkeypatch):
        client = _nomicheck_client()
        _wire(
            client, monkeypatch,
            by_url={
                f"{CDP}/supported": self._supported("base", "ethereum"),
                f"{UVD}/supported": self._supported("avalanche", "base", "polygon"),
            },
        )
        assert client.verify_routes() == {CDP: ["base"], UVD: ["avalanche"]}

    def test_boot_fails_when_a_facilitator_does_not_settle_its_network(self, monkeypatch):
        """The exact NomiCheck failure: avalanche routed to CDP, which cannot settle it."""
        client = X402Client(
            recipient_address=RECIPIENT,
            supported_networks=list(NOMICHECK_NETWORKS),
            facilitator_by_network={"base": CDP, "avalanche": CDP},
        )
        _wire(client, monkeypatch, by_url={f"{CDP}/supported": self._supported("base")})
        with pytest.raises(ConfigurationError) as exc:
            client.verify_routes()
        assert "avalanche" in str(exc.value)

    def test_caip2_in_the_supported_response_still_matches(self, monkeypatch):
        client = _nomicheck_client()
        _wire(
            client, monkeypatch,
            by_url={
                f"{CDP}/supported": self._supported("eip155:8453"),
                f"{UVD}/supported": self._supported("eip155:43114"),
            },
        )
        client.verify_routes()

    def test_unreadable_supported_is_fatal_not_ignored(self, monkeypatch):
        """An unverifiable route is not a verified one — never pass by default."""
        client = _nomicheck_client()
        _wire(
            client, monkeypatch,
            by_url={
                f"{CDP}/supported": RuntimeError("connection refused"),
                f"{UVD}/supported": self._supported("avalanche"),
            },
        )
        with pytest.raises(FacilitatorError, match="supported"):
            client.verify_routes()

    def test_constructor_flag_runs_the_check(self, monkeypatch):
        """The flag must fail the CONSTRUCTOR, not the first payment."""
        import httpx

        def _boom(*args, **kwargs):
            raise httpx.ConnectError("no route to host")

        monkeypatch.setattr(httpx.Client, "get", _boom)
        with pytest.raises(FacilitatorError):
            _nomicheck_client(verify_facilitator_support=True)


# ── The wire: requests land on the right facilitator ─────────────────────────


class TestRoutingReachesTheWire:
    def test_settle_posts_to_the_network_facilitator(self, monkeypatch):
        client = _nomicheck_client()
        fake = _wire(
            client, monkeypatch,
            by_url={
                f"{CDP}/settle": _FakeResponse(200, _ok_settle_body()),
                f"{UVD}/settle": _FakeResponse(200, _ok_settle_body()),
            },
        )
        client.settle_payment(_payload("base"), Decimal("0.01"))
        client.settle_payment(_payload("avalanche"), Decimal("0.01"))
        assert fake.urls == [f"{CDP}/settle", f"{UVD}/settle"]

    def test_verify_posts_to_the_network_facilitator(self, monkeypatch):
        client = _nomicheck_client()
        fake = _wire(
            client, monkeypatch,
            by_url={
                f"{CDP}/verify": _FakeResponse(200, _ok_verify_body()),
                f"{UVD}/verify": _FakeResponse(200, _ok_verify_body()),
            },
        )
        client.verify_payment(_payload("avalanche"), Decimal("0.01"))
        client.verify_payment(_payload("base"), Decimal("0.01"))
        assert fake.urls == [f"{UVD}/verify", f"{CDP}/verify"]

    def test_settle_timeout_fallback_asks_the_same_facilitator(self, monkeypatch):
        """Asking a DIFFERENT facilitator about a timed-out settle gets a
        confident 'never saw it' about a payment that may well have landed."""
        import httpx

        client = _nomicheck_client()
        fake = _wire(
            client, monkeypatch,
            script=[
                httpx.TimeoutException("timed out"),
                _FakeResponse(200, _ok_settle_body()),
            ],
        )
        client.settle_payment(_payload("base"), Decimal("0.01"))
        assert fake.urls == [f"{CDP}/settle", f"{CDP}/settle"]

    def test_get_supported_can_target_one_network(self, monkeypatch):
        client = _nomicheck_client()
        fake = _wire(client, monkeypatch, by_url={f"{UVD}/supported": _FakeResponse(200, {})})
        client.get_supported(network="avalanche")
        assert fake.urls == [f"{UVD}/supported"]

    def test_health_check_can_target_one_network(self, monkeypatch):
        client = _nomicheck_client()
        fake = _wire(client, monkeypatch, by_url={f"{CDP}/health": _FakeResponse(200, {})})
        assert client.health_check(network="base") is True
        assert fake.urls == [f"{CDP}/health"]

    def test_accepts_spanning_two_facilitators_is_refused(self, monkeypatch):
        """One /accepts request cannot be two requests. Make the caller split it."""
        client = _nomicheck_client()
        fake = _wire(client, monkeypatch)
        with pytest.raises(ConfigurationError, match="spanning"):
            client.negotiate_accepts(
                [{"network": "base", "payTo": RECIPIENT},
                 {"network": "avalanche", "payTo": RECIPIENT}]
            )
        assert fake.calls == []

    def test_accepts_for_one_facilitator_routes_there(self, monkeypatch):
        client = _nomicheck_client()
        fake = _wire(
            client, monkeypatch,
            by_url={f"{CDP}/accepts": _FakeResponse(200, {"accepts": []})},
        )
        client.negotiate_accepts([{"network": "base", "payTo": RECIPIENT}])
        assert fake.urls == [f"{CDP}/accepts"]


# ── Backward compatibility: nothing changes without the new parameter ────────


class TestBackwardCompatible:
    def test_default_config_routes_everything_to_facilitator_url(self):
        config = X402Config(recipient_evm=RECIPIENT)
        for network in ("base", "avalanche", "solana", "stellar", "xrpl-mainnet"):
            assert config.facilitator_url_for(network) == config.facilitator_url

    def test_unknown_network_still_does_not_raise_without_a_table(self):
        """Rejecting unknown networks is validate_network's job, not routing's.
        Raising here would turn a clear UnsupportedNetworkError into a config one."""
        config = X402Config(recipient_evm=RECIPIENT)
        assert config.facilitator_url_for("not-a-chain") == config.facilitator_url

    def test_custom_facilitator_url_still_wins_everywhere(self):
        config = X402Config(recipient_evm=RECIPIENT, facilitator_url="https://my.facilitator")
        assert config.facilitator_url_for("base") == "https://my.facilitator"

    def test_settle_wire_is_unchanged_without_a_table(self, monkeypatch):
        client = X402Client(recipient_address=RECIPIENT, facilitator_url=UVD)
        fake = _wire(client, monkeypatch, script=[_FakeResponse(200, _ok_settle_body())])
        client.settle_payment(_payload("base"), Decimal("0.01"))
        assert fake.urls == [f"{UVD}/settle"]

    def test_network_less_endpoints_use_the_default_facilitator(self, monkeypatch):
        """/version and /blacklist are not network-scoped; a routing table must
        not make them unanswerable."""
        client = _nomicheck_client()
        fake = _wire(
            client, monkeypatch,
            by_url={
                f"{UVD}/version": _FakeResponse(200, {"version": "1.0.0"}),
                f"{UVD}/blacklist": _FakeResponse(200, {"totalBlocked": 0}),
            },
        )
        client.get_version()
        client.get_blacklist()
        assert fake.urls == [f"{UVD}/version", f"{UVD}/blacklist"]


class TestFromEnv:
    def test_table_is_read_from_the_environment(self, monkeypatch):
        monkeypatch.setenv("X402_RECIPIENT_EVM", RECIPIENT)
        monkeypatch.setenv(
            "X402_FACILITATOR_BY_NETWORK",
            json.dumps({"base": CDP, FACILITATOR_FALLBACK_KEY: UVD}),
        )
        config = X402Config.from_env()
        assert config.facilitator_url_for("base") == CDP
        assert config.facilitator_url_for("polygon") == UVD

    def test_unset_env_keeps_single_facilitator_behavior(self, monkeypatch):
        monkeypatch.setenv("X402_RECIPIENT_EVM", RECIPIENT)
        monkeypatch.delenv("X402_FACILITATOR_BY_NETWORK", raising=False)
        config = X402Config.from_env()
        assert config.facilitator_by_network == {}
        assert config.facilitator_url_for("base") == config.facilitator_url

    @pytest.mark.parametrize("bad", ["{not json", '"base"', "[]"], ids=["broken", "string", "list"])
    def test_malformed_env_fails_loudly(self, monkeypatch, bad):
        monkeypatch.setenv("X402_RECIPIENT_EVM", RECIPIENT)
        monkeypatch.setenv("X402_FACILITATOR_BY_NETWORK", bad)
        with pytest.raises(ConfigurationError):
            X402Config.from_env()
