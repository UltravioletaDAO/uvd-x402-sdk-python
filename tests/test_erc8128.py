"""ERC-8128 (RFC 9421) request signing — unit tests + golden-vector conformance.

PROVENANCE of ``tests/fixtures/erc8128.json``: byte-identical copy of the
Execution Market monorepo's ``shared/test-vectors/erc8128.json`` (the F3-1
golden vectors that pin the fleet-wide wire format). The monorepo file is the
source of truth — if the vectors change there, re-copy the file byte-for-byte,
NEVER edit the copy here. ``TestFixtureIntegrity`` re-derives the vectors
cryptographically, so a corrupted copy fails before any conformance assert.

The fixture's signing key is a synthetic test key (0x42 * 32) that never held
funds, stored WITHOUT the 0x prefix so secret scanners never see 0x + 64 hex;
loaders re-prefix it. The other tests use a THROWAWAY key generated in-test
with ``eth_account.Account.create()`` — never a real wallet, never printed.
Determinism comes from RFC 6979: the same key + signature base always
produces the same 65 signature bytes.
"""

import base64
import json
import re
import types
from pathlib import Path

import pytest
from eth_account import Account
from eth_account.messages import encode_defunct

import uvd_x402_sdk.erc8128 as erc8128_mod
from uvd_x402_sdk.erc8128 import fetch_nonce, sign_request
from uvd_x402_sdk.wallet import EnvKeyAdapter

FIXED_NOW = 1760000000

# RFC 9530 §Section B.1 known vector: sha-256 of '{"hello": "world"}'
RFC9530_DIGEST = "sha-256=:X48E9qOokqqrvdts8nOJRJN3OWDUoyWxBf7kbu9DBPE=:"

FIXTURE_PATH = Path(__file__).resolve().parent / "fixtures" / "erc8128.json"
FIXTURE = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))

FROZEN = FIXTURE["frozen"]
VECTORS = FIXTURE["vectors"]
REQUESTS = FIXTURE["requests"]

REQUEST_NAMES = ("get_query", "post_body")
VARIANT_NAMES = ("canonical", "legacy_no_alg", "legacy_alg_checksum_keyid")


@pytest.fixture
def account():
    return Account.create()


@pytest.fixture
def wallet(account):
    key_hex = account.key.hex()
    return EnvKeyAdapter(private_key=key_hex)


@pytest.fixture
def frozen_time(monkeypatch):
    monkeypatch.setattr(
        erc8128_mod, "time", types.SimpleNamespace(time=lambda: FIXED_NOW)
    )
    return FIXED_NOW


def _decode_signature(headers):
    m = re.fullmatch(r"eth=:(?P<b64>[A-Za-z0-9+/=]+):", headers["Signature"])
    assert m, headers["Signature"]
    return base64.b64decode(m.group("b64"))


# =============================================================================
# Signer unit tests
# =============================================================================


class TestSignRequest:
    def test_content_digest_rfc9530_vector(self, wallet):
        headers = sign_request(
            wallet,
            method="POST",
            url="https://api.execution.market/api/v1/tasks",
            body='{"hello": "world"}',
            nonce="n1",
        )
        assert headers["Content-Digest"] == RFC9530_DIGEST

    def test_known_vector_signature_base(self, wallet, account, frozen_time):
        """Deterministic fixed-request vector: exact Signature-Input string,
        signature over the independently rebuilt RFC 9421 base, and ECDSA
        recovery back to the signer address."""
        body = '{"hello": "world"}'
        headers = sign_request(
            wallet,
            method="POST",
            url="https://api.execution.market/api/v1/tasks",
            body=body,
            nonce="test-nonce",
        )

        address = account.address.lower()
        expected_params = (
            '("@method" "@authority" "@path" "content-digest")'
            f";created={FIXED_NOW};expires={FIXED_NOW + 300}"
            f';nonce="test-nonce";keyid="erc8128:8453:{address}";alg="eip191"'
        )
        assert headers["Signature-Input"] == f"eth={expected_params}"

        expected_base = "\n".join(
            [
                '"@method": POST',
                '"@authority": api.execution.market',
                '"@path": /api/v1/tasks',
                f'"content-digest": {RFC9530_DIGEST}',
                f'"@signature-params": {expected_params}',
            ]
        )
        sig = _decode_signature(headers)

        # (a) deterministic re-sign of the rebuilt base matches byte-for-byte
        expected_sig = account.sign_message(encode_defunct(text=expected_base))
        assert sig == expected_sig.signature

        # (b) EIP-191 recovery of the header signature yields the signer
        recovered = Account.recover_message(
            encode_defunct(text=expected_base), signature=sig
        )
        assert recovered.lower() == address

    def test_query_component_covered(self, wallet):
        headers = sign_request(
            wallet,
            method="GET",
            url="https://api.execution.market/api/v1/tasks?status=published&limit=5",
            nonce="n1",
        )
        assert '"@query"' in headers["Signature-Input"]
        assert "Content-Digest" not in headers

    def test_no_body_no_content_digest(self, wallet):
        headers = sign_request(
            wallet,
            method="POST",
            url="https://api.execution.market/api/v1/tasks/abc/cancel",
            nonce="n1",
        )
        assert "Content-Digest" not in headers
        assert '"content-digest"' not in headers["Signature-Input"]


# =============================================================================
# @authority normalisation (RFC 9421 §2.2.3)
#
# The authority is lowercased and the scheme's DEFAULT port dropped; any other
# port is part of the authority. `urlsplit().netloc` keeps an explicit `:443`,
# so the same request written two ways used to sign two different bases — and
# only one of them is what a server sees in `Host`. The rule is IDEMPOTENT, so
# every already-normalised URL (all live traffic, every pinned vector) signs
# the exact bytes it signed before.
# =============================================================================


class TestAuthorityNormalisation:
    @pytest.mark.parametrize(
        "url,expected_authority",
        [
            ("https://api.execution.market/api/v1/tasks", "api.execution.market"),
            ("https://api.execution.market:443/api/v1/tasks", "api.execution.market"),
            ("http://api.execution.market:80/api/v1/tasks", "api.execution.market"),
            ("https://API.Execution.Market/api/v1/tasks", "api.execution.market"),
            # A port that is not THIS scheme's default stays put — including
            # 80 under https, which is not the same authority as bare host.
            (
                "https://api.execution.market:8443/api/v1/tasks",
                "api.execution.market:8443",
            ),
            ("https://api.execution.market:80/api/v1/tasks", "api.execution.market:80"),
            ("http://api.execution.market:443/api/v1/tasks", "api.execution.market:443"),
        ],
    )
    def test_signed_authority_line(self, wallet, account, url, expected_authority):
        """Pins the AUTHORITY BYTES, not just that two spellings agree: the
        base is rebuilt by hand and re-signed (RFC 6979 is deterministic)."""
        headers = sign_request(
            wallet, method="GET", url=url, nonce="n1", now=lambda: FIXED_NOW
        )
        params = headers["Signature-Input"][len("eth=") :]
        expected_base = "\n".join(
            [
                '"@method": GET',
                f'"@authority": {expected_authority}',
                '"@path": /api/v1/tasks',
                f'"@signature-params": {params}',
            ]
        )
        expected = account.sign_message(encode_defunct(text=expected_base))
        assert _decode_signature(headers) == expected.signature

    def test_the_url_rule_requires_a_scheme(self):
        """No default scheme, because there is no caller without one: the
        signer reads it off the URL, the verifier off the request it serves.
        The one value that has no scheme — the CONFIGURED policy authority —
        does not come here at all (it goes through ``policy_authority``, which
        never touches ports); a default would have let it in silently."""
        from uvd_x402_sdk.erc8128 import normalize_authority

        with pytest.raises(TypeError):
            normalize_authority("api.execution.market:443")

    def test_normalisation_is_idempotent(self):
        """Why this cannot move live traffic: every form the fleet already
        emits is a fixed point, IPv6 literals included."""
        from uvd_x402_sdk.erc8128 import normalize_authority

        for value in (
            "api.execution.market",
            "api.execution.market:8443",
            "[2001:db8::1]",
            "[2001:db8::1]:8443",
            "",
        ):
            once = normalize_authority(value, "https")
            assert once == value
            assert normalize_authority(once, "https") == once
        assert normalize_authority("[2001:db8::1]:443", "https") == "[2001:db8::1]"


# =============================================================================
# Fixture integrity — the copied vectors are cryptographically sound.
# A corrupted or hand-edited copy fails here before any conformance assert.
# =============================================================================


class TestFixtureIntegrity:
    @pytest.mark.parametrize("variant", VARIANT_NAMES)
    @pytest.mark.parametrize("request_name", REQUEST_NAMES)
    def test_signature_recovers_to_frozen_address(self, variant, request_name):
        """EIP-191 recovery over each stored base yields the frozen signer."""
        vec = VECTORS[variant][request_name]
        sig_field = vec["headers"]["Signature"]
        assert sig_field.startswith("eth=:") and sig_field.endswith(":")
        sig = base64.b64decode(sig_field[len("eth=:") : -1])
        assert len(sig) == 65, "pinned encoding is base64(r||s||v, 65 bytes)"
        recovered = Account.recover_message(
            encode_defunct(text=vec["signature_base"]), signature=sig
        )
        assert recovered.lower() == FROZEN["address"]

    @pytest.mark.parametrize("variant", VARIANT_NAMES)
    @pytest.mark.parametrize("request_name", REQUEST_NAMES)
    def test_signature_input_matches_base_params(self, variant, request_name):
        """The on-wire Signature-Input is the base's @signature-params line."""
        vec = VECTORS[variant][request_name]
        last_line = vec["signature_base"].splitlines()[-1]
        assert last_line.startswith('"@signature-params": ')
        sig_params = last_line[len('"@signature-params": ') :]
        assert vec["headers"]["Signature-Input"] == f"eth={sig_params}"


# =============================================================================
# Pinned policy — the canonical wire format this module emits, spelled out
# =============================================================================


class TestPinnedPolicy:
    @pytest.mark.parametrize("request_name", REQUEST_NAMES)
    def test_canonical_emits_alg_eip191(self, request_name):
        sig_input = VECTORS["canonical"][request_name]["headers"]["Signature-Input"]
        assert sig_input.endswith(';alg="eip191"')

    @pytest.mark.parametrize("request_name", REQUEST_NAMES)
    def test_canonical_keyid_is_lowercase(self, request_name):
        sig_input = VECTORS["canonical"][request_name]["headers"]["Signature-Input"]
        keyid = f'keyid="erc8128:{FROZEN["chain_id"]}:{FROZEN["address"]}"'
        assert keyid in sig_input
        assert FROZEN["address_checksummed"] not in sig_input, (
            "checksummed keyid caused the v9.x silent-auth incident"
        )

    @pytest.mark.parametrize("request_name", REQUEST_NAMES)
    def test_canonical_param_order(self, request_name):
        """created;expires;nonce;keyid;alg — the pinned order."""
        sig_input = VECTORS["canonical"][request_name]["headers"]["Signature-Input"]
        params_part = sig_input.split(")", 1)[1]
        keys = [p.split("=", 1)[0] for p in params_part.lstrip(";").split(";")]
        assert keys == ["created", "expires", "nonce", "keyid", "alg"]

    def test_policy_block_pins_the_decision(self):
        pol = FIXTURE["policy"]["pinned_wire_format"]
        assert pol["alg"] == "eip191"
        assert pol["alg_required"] is True
        assert pol["keyid_lowercase"] is True
        assert pol["signature_params_order"] == [
            "created",
            "expires",
            "nonce",
            "keyid",
            "alg",
        ]


# =============================================================================
# Golden-vector conformance — byte-equality against the canonical wire format.
# ANY change that moves these bytes breaks auth for every consumer at once.
# =============================================================================


class TestGoldenVectorConformance:
    @pytest.mark.parametrize("request_name", REQUEST_NAMES)
    def test_headers_match_canonical_vector(self, request_name, monkeypatch):
        # Key stored 0x-less so the secret scanner never sees 0x + 64 hex.
        wallet = EnvKeyAdapter(private_key="0x" + FROZEN["private_key"])
        monkeypatch.setattr(
            erc8128_mod,
            "time",
            types.SimpleNamespace(time=lambda: float(FROZEN["created"])),
        )
        spec = REQUESTS[request_name]
        headers = sign_request(
            wallet,
            method=spec["method"],
            url=spec["url"],
            body=spec["body"],
            nonce=FROZEN["nonce"],
            chain_id=FROZEN["chain_id"],
        )
        expected = VECTORS["canonical"][request_name]["headers"]
        assert headers == expected


# =============================================================================
# fetch_nonce — URL construction and response parsing (no network)
# =============================================================================


class _FakeNonceResponse:
    def raise_for_status(self):
        pass

    def json(self):
        return {"nonce": "nonce-xyz", "ttl_seconds": 300}


class _FakeAsyncClient:
    """Records the requested URL; returns a canned nonce payload."""

    last_url = None

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, **kwargs):
        _FakeAsyncClient.last_url = url
        return _FakeNonceResponse()


class TestFetchNonce:
    async def test_fetches_from_the_nonce_endpoint(self, monkeypatch):
        monkeypatch.setattr(
            erc8128_mod, "httpx", types.SimpleNamespace(AsyncClient=_FakeAsyncClient)
        )
        nonce = await fetch_nonce("https://api.execution.market")
        assert nonce == "nonce-xyz"
        assert (
            _FakeAsyncClient.last_url
            == "https://api.execution.market/api/v1/auth/erc8128/nonce"
        )

    async def test_trailing_slash_is_stripped(self, monkeypatch):
        monkeypatch.setattr(
            erc8128_mod, "httpx", types.SimpleNamespace(AsyncClient=_FakeAsyncClient)
        )
        await fetch_nonce("https://api.execution.market/")
        assert (
            _FakeAsyncClient.last_url
            == "https://api.execution.market/api/v1/auth/erc8128/nonce"
        )


# ── el hermano SÍNCRONO (2026-08-02) ─────────────────────────────────────────
#
# El async sigue siendo el principal. Pero un consumidor con camino de ejecución
# síncrono tendría que hacer `asyncio.run` por cada request firmado sólo para pedir un
# nonce — abre y cierra un event loop por llamada, y revienta si ya hay uno corriendo.
# Un SDK que obliga a eso empuja a que cada consumidor reimplemente el fetch, que es
# exactamente lo que había pasado.

def test_fetch_nonce_sync_devuelve_nonce_y_ttl(monkeypatch):
    import httpx as _h
    from uvd_x402_sdk import erc8128

    class _R:
        status_code = 200
        def raise_for_status(self): pass
        def json(self): return {"nonce": "abc123", "ttl": 300}

    monkeypatch.setattr(_h, "get", lambda *a, **k: _R())
    assert erc8128.fetch_nonce_sync("https://x") == ("abc123", 300)


def test_fetch_nonce_sync_sin_ttl_devuelve_None_no_un_numero_inventado(monkeypatch):
    """Un TTL adivinado hace reusar un nonce ya consumido, y ese fallo se lee como un
    problema de FIRMA, no de nonce."""
    import httpx as _h
    from uvd_x402_sdk import erc8128

    class _R:
        def raise_for_status(self): pass
        def json(self): return {"nonce": "abc123"}

    monkeypatch.setattr(_h, "get", lambda *a, **k: _R())
    assert erc8128.fetch_nonce_sync("https://x") == ("abc123", None)


def test_sin_endpoint_falla_CERRADO_por_defecto():
    from uvd_x402_sdk import erc8128
    import pytest as _p
    with _p.raises(RuntimeError, match="fallback local"):
        erc8128.fetch_nonce_sync("http://127.0.0.1:9", timeout=0.2)


def test_el_fallback_local_es_OPT_IN():
    """Encenderlo contra un servidor que exige nonce propio convierte 'el endpoint está
    caído' en 'tu firma es inválida' — un diagnóstico mucho peor."""
    from uvd_x402_sdk import erc8128
    nonce, ttl = erc8128.fetch_nonce_sync("http://127.0.0.1:9", timeout=0.2,
                                          allow_local_fallback=True)
    assert len(nonce) == 32 and ttl is None
