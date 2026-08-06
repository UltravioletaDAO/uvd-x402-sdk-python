"""ERC-8128 verifier — unit and adversarial tests.

The signed headers in this file are built by a DELIBERATE SECOND
IMPLEMENTATION (``_sign`` below): the covered list, the parameter string and
the signature base are assembled with plain string formatting and signed with
``eth_account`` directly, never with the SDK's own builders. That is the
point — a verifier tested only against its own signer proves nothing about
the wire, and it is the only way to exercise both wire generations, a
checksummed keyid and an unknown RFC 9421 parameter with code that is not the
code under test.

The signing key is the PUBLIC synthetic test key from the conformance
fixture (``0x42 * 32``, documented there as a key that never held funds); it
is read from the shipped vectors, never written here.
"""

import asyncio
import base64
import json

import pytest
from eth_account import Account
from eth_account.messages import encode_defunct

from uvd_x402_sdk.erc8128 import (
    ERC8128_ERROR_RETRYABLE,
    ERC8128_ERROR_STATUS,
    POLICY_PRESETS,
    VerifiableRequest,
    VerifyPolicy,
    compute_content_digest,
    eip191_byte_length,
    extract_keyid_wallet,
    load_vectors,
    parse_signature_header,
    parse_signature_input,
    policy_from_preset,
    sign_request,
    verify_request,
)
from uvd_x402_sdk.erc8128.errors import Erc8128Error
from uvd_x402_sdk.erc8128.verifier import policy_authority
from uvd_x402_sdk.wallet import EnvKeyAdapter

FROZEN = load_vectors("f3-1")["frozen"]
ACCOUNT = Account.from_key("0x" + FROZEN["private_key"])
WALLET = FROZEN["address"]
CHAIN = FROZEN["chain_id"]
CREATED = FROZEN["created"]
EXPIRES = FROZEN["expires"]
NONCE = FROZEN["nonce"]
NOW = CREATED + 1
AUTHORITY = "api.execution.market"
PATH = "/api/v1/tasks"


# ---------------------------------------------------------------------------
# The adversary signer — independent of the SDK's builders
# ---------------------------------------------------------------------------


def _sign(
    method="GET",
    path=PATH,
    query=None,
    body=None,
    covered=None,
    params=None,
    label="eth",
    authority=AUTHORITY,
    keyid=None,
    alg='alg="eip191"',
    nonce=NONCE,
    created=CREATED,
    expires=EXPIRES,
    extra_lines=None,
    account=ACCOUNT,
):
    """Build signed headers with plain string formatting."""
    keyid = keyid or f"erc8128:{CHAIN}:{WALLET}"
    digest = compute_content_digest(body) if body is not None else None
    if covered is None:
        covered = ["@method", "@authority", "@path"]
        if query:
            covered.append("@query")
        if digest is not None:
            covered.append("content-digest")
    if params is None:
        parts = [f"created={created}", f"expires={expires}"]
        if nonce:
            parts.append(f'nonce="{nonce}"')
        parts.append(f'keyid="{keyid}"')
        if alg:
            parts.append(alg)
        params = ";".join(parts)

    values = {
        "@method": method.upper(),
        "@authority": authority,
        "@path": path,
        "@query": query or "?",
        "content-digest": digest or "",
    }
    values.update(extra_lines or {})
    sig_params = "(" + " ".join(f'"{c}"' for c in covered) + ");" + params
    lines = [f'"{c}": {values.get(c, "")}' for c in covered]
    lines.append(f'"@signature-params": {sig_params}')
    base = "\n".join(lines)

    signature = account.sign_message(encode_defunct(text=base)).signature
    headers = {
        "Signature": f"{label}=:{base64.b64encode(signature).decode()}:",
        "Signature-Input": f"{label}={sig_params}",
    }
    if digest is not None:
        headers["Content-Digest"] = digest
    return headers, base


def _request(headers, method="GET", path=PATH, query=None, body=None, **overrides):
    url = f"https://{AUTHORITY}{path}" + (query or "")
    wire = dict(headers)
    if body is not None:
        wire["Content-Length"] = str(len(body.encode("utf-8")))
    kwargs = dict(
        method=method,
        url=url,
        headers=wire,
        raw_body=body.encode("utf-8") if body is not None else None,
    )
    kwargs.update(overrides)
    return VerifiableRequest(**kwargs)


class _Store:
    """First-use-wins, optionally issuer-bound or broken."""

    def __init__(self, outcome=None, raises=False, known=None):
        self.seen = set()
        self.outcome = outcome
        self.raises = raises
        self.known = known
        self.calls = 0

    def consume(self, nonce, *, wallet, chain_id, ttl_seconds, created, expires):
        self.calls += 1
        if self.raises:
            raise RuntimeError("DynamoDB blip")
        if self.outcome:
            return self.outcome
        if self.known is not None and nonce not in self.known:
            return "unknown"
        key = (nonce, wallet, chain_id)
        if key in self.seen:
            return "replayed"
        self.seen.add(key)
        return "ok"


def _policy(preset="em-lenient", store=None, **overrides):
    overrides.setdefault("now", lambda: NOW)
    return policy_from_preset(
        preset,
        authority=AUTHORITY,
        nonce_store=store if store is not None else _Store(),
        **overrides,
    )


# ---------------------------------------------------------------------------
# Baseline + the one byte path
# ---------------------------------------------------------------------------


class TestAcceptance:
    async def test_canonical_verifies(self):
        headers, base = _sign()
        result = await verify_request(_request(headers), _policy())
        assert result.ok, result.code
        assert result.wallet == WALLET
        assert result.chain_id == CHAIN
        assert result.via == "eoa"
        assert result.observed_profile == "canonical"
        assert result.signature_base == base
        assert result.nonce == NONCE

    async def test_legacy_no_alg_verifies_with_no_flag(self):
        headers, _ = _sign(alg=None)
        result = await verify_request(_request(headers), _policy())
        assert result.ok, result.code
        assert result.observed_profile == "legacy_no_alg"

    async def test_checksummed_keyid_verifies(self):
        headers, _ = _sign(keyid=f"erc8128:{CHAIN}:{FROZEN['address_checksummed']}")
        result = await verify_request(_request(headers), _policy())
        assert result.ok, result.code
        assert result.wallet == WALLET
        assert result.observed_profile == "legacy_alg_checksum_keyid"

    async def test_unknown_future_parameter_verifies(self):
        """THE property the verbatim rebuild buys: a parameter this SDK has
        never heard of round-trips because the params substring is never
        re-serialised. An anchored regex over the known parameters — the bug
        this module deletes — answers signature_input_invalid here."""
        params = (
            f'created={CREATED};expires={EXPIRES};nonce="{NONCE}";'
            f'keyid="erc8128:{CHAIN}:{WALLET}";alg="eip191";tag="future-rfc-9421"'
        )
        headers, _ = _sign(params=params)
        result = await verify_request(_request(headers), _policy())
        assert result.ok, result.code

    async def test_reordered_parameters_verify(self):
        params = (
            f'keyid="erc8128:{CHAIN}:{WALLET}";alg="eip191";created={CREATED};'
            f'expires={EXPIRES};nonce="{NONCE}"'
        )
        headers, _ = _sign(params=params)
        result = await verify_request(_request(headers), _policy())
        assert result.ok, result.code

    async def test_superset_covered_list_under_em_posture(self):
        headers, _ = _sign(
            covered=["@method", "@authority", "@path", "date"],
            extra_lines={"date": "Mon, 01 Aug 2026 00:00:00 GMT"},
        )
        request = _request(headers)
        request = VerifiableRequest(
            method=request.method,
            url=request.url,
            headers={**request.headers, "Date": "Mon, 01 Aug 2026 00:00:00 GMT"},
        )
        result = await verify_request(request, _policy("em-lenient"))
        assert result.ok, result.code
        strict = await verify_request(request, _policy("meshrelay-strict"))
        assert not strict.ok
        assert strict.code == "components_invalid"

    async def test_non_ascii_base_uses_utf8_byte_length(self):
        path = "/api/v1/tareas/ñandú"
        headers, base = _sign(path=path)
        result = await verify_request(_request(headers, path=path), _policy())
        assert result.ok, result.code
        assert eip191_byte_length(base) > len(base), "the latent len()-vs-bytes bug"


# ---------------------------------------------------------------------------
# Content-Digest — EM's body-presence rule is canonical (owner decision)
# ---------------------------------------------------------------------------


class TestContentDigest:
    async def test_bodyless_post_is_signable_under_body_presence(self):
        headers, _ = _sign(method="POST", path="/api/v1/tasks/abc/cancel")
        result = await verify_request(
            _request(headers, method="POST", path="/api/v1/tasks/abc/cancel"),
            _policy("em-lenient"),
        )
        assert result.ok, result.code

    async def test_bodyless_post_is_401_under_the_by_method_rule(self):
        headers, _ = _sign(method="POST", path="/api/v1/tasks/abc/cancel")
        result = await verify_request(
            _request(headers, method="POST", path="/api/v1/tasks/abc/cancel"),
            _policy("meshrelay-strict"),
        )
        assert not result.ok
        assert result.code == "content_digest_required"
        assert result.status == 401

    @pytest.mark.parametrize("preset", ["em-lenient", "meshrelay-strict"])
    async def test_body_injection_into_a_bodyless_signature_is_rejected(self, preset):
        """The hole CRY-001 closes, and the case EM's own suite never pinned:
        a signature made WITHOUT covering content-digest must not become usable
        by attaching a body. Body presence is re-derived from the wire, so the
        request flips to 'bodied' and the uncovered digest is fatal."""
        headers, _ = _sign(method="POST", path="/api/v1/tasks")
        injected = _request(headers, method="POST", body='{"bounty_usd":9999}')
        result = await verify_request(injected, _policy(preset))
        assert not result.ok
        assert result.code == "content_digest_required"

    async def test_body_without_framing_headers_still_counts_as_a_body(self):
        """EM decides body presence from content-length/transfer-encoding
        alone, which is only safe under HTTP/1.1 framing — an ENVIRONMENTAL
        invariant a shared SDK cannot inherit. Non-empty raw bytes count."""
        headers, _ = _sign(method="POST", path="/api/v1/tasks")
        injected = VerifiableRequest(
            method="POST",
            url=f"https://{AUTHORITY}{PATH}",
            headers=dict(headers),  # no Content-Length, no Transfer-Encoding
            raw_body=b'{"bounty_usd":9999}',
        )
        result = await verify_request(injected, _policy("em-lenient"))
        assert not result.ok
        assert result.code == "content_digest_required"

    async def test_tampered_body_is_rejected(self):
        body = '{"title":"F3-1 conformance","bounty_usd":0.1}'
        headers, _ = _sign(method="POST", body=body)
        request = _request(headers, method="POST", body='{"title":"tampered"}')
        result = await verify_request(request, _policy())
        assert not result.ok
        assert result.code == "content_digest_mismatch"

    async def test_unsigned_content_digest_header_never_satisfies_the_rule(self):
        """The requirement is on the SIGNED list, not on header presence."""
        headers, _ = _sign(method="POST", path="/api/v1/tasks")
        body = '{"bounty_usd":9999}'
        headers = {**headers, "Content-Digest": compute_content_digest(body)}
        result = await verify_request(
            _request(headers, method="POST", body=body), _policy()
        )
        assert not result.ok
        assert result.code == "content_digest_required"

    async def test_covered_digest_with_missing_header(self):
        """ABSENT-when-required is `content_digest_required`, never
        `content_digest_invalid`. The partition both languages implement:
        absent ⇒ required, present-but-unparseable ⇒ invalid, present-but-not-
        the-body ⇒ mismatch. `code` is the published contract both products
        switch on, so the two SDKs must not answer it differently."""
        headers, _ = _sign(method="POST", body="")
        headers.pop("Content-Digest")
        result = await verify_request(_request(headers, method="POST"), _policy())
        assert not result.ok
        assert result.code == "content_digest_required"

    async def test_the_three_digest_codes_partition_the_failures(self):
        """One test, one place, so the split cannot drift: the same POST fails
        with a different code for each of the three distinguishable states."""
        body = '{"bounty_usd":1}'
        headers, _ = _sign(method="POST", body=body)

        absent = {k: v for k, v in headers.items() if k != "Content-Digest"}
        unparseable = {**headers, "Content-Digest": "sha-512=:AAAA:"}

        codes = {}
        for name, wire, sent in (
            ("absent", absent, body),
            ("unparseable", unparseable, body),
            ("not-the-body", headers, '{"bounty_usd":9999}'),
        ):
            result = await verify_request(
                _request(wire, method="POST", body=sent), _policy()
            )
            assert not result.ok, name
            codes[name] = result.code
        assert codes == {
            "absent": "content_digest_required",
            "unparseable": "content_digest_invalid",
            "not-the-body": "content_digest_mismatch",
        }

    async def test_unsupported_digest_algorithm(self):
        headers, _ = _sign(method="POST", body="{}")
        headers["Content-Digest"] = "sha-512=:AAAA:"
        result = await verify_request(
            _request(headers, method="POST", body="{}"), _policy()
        )
        assert not result.ok
        assert result.code == "content_digest_invalid"

    async def test_empty_body_signs_and_verifies_under_both_rules(self):
        headers, _ = _sign(method="POST", path="/api/v1/tasks/abc/cancel", body="")
        request = _request(
            headers, method="POST", path="/api/v1/tasks/abc/cancel", body=""
        )
        for preset in ("em-lenient", "meshrelay-strict", "canonical-strict"):
            result = await verify_request(request, _policy(preset))
            assert result.ok, (preset, result.code)


# ---------------------------------------------------------------------------
# Components
# ---------------------------------------------------------------------------


class TestComponents:
    async def test_class_bound_is_rejected(self):
        headers, _ = _sign(covered=["@method"])
        result = await verify_request(_request(headers), _policy("em-lenient"))
        assert not result.ok
        assert result.code == "class_bound_rejected"

    async def test_class_bound_under_exact_ordered_is_components_invalid(self):
        headers, _ = _sign(covered=["@method"])
        result = await verify_request(_request(headers), _policy("meshrelay-strict"))
        assert not result.ok
        assert result.code == "components_invalid"

    async def test_query_present_but_not_covered(self):
        query = "?status=published"
        headers, _ = _sign(covered=["@method", "@authority", "@path"], query=query)
        result = await verify_request(
            _request(headers, query=query), _policy("em-lenient")
        )
        assert not result.ok
        assert result.code == "class_bound_rejected"

    async def test_reordered_components_fail_exact_ordered_only(self):
        covered = ["@authority", "@method", "@path"]
        headers, _ = _sign(covered=covered)
        request = _request(headers)
        strict = await verify_request(request, _policy("meshrelay-strict"))
        assert strict.code == "components_invalid"
        lenient = await verify_request(request, _policy("em-lenient"))
        assert lenient.ok, lenient.code


# ---------------------------------------------------------------------------
# Freshness, chain, label, authority
# ---------------------------------------------------------------------------


class TestPolicyGates:
    async def test_expired_signature(self):
        headers, _ = _sign()
        # Past EM's 30s grace — EXPIRES + 1 is still inside it (see below).
        result = await verify_request(_request(headers), _policy(now=lambda: EXPIRES + 31))
        assert result.code == "signature_stale"

    async def test_expired_within_em_grace_is_accepted(self):
        headers, _ = _sign()
        result = await verify_request(
            _request(headers), _policy("em-lenient", now=lambda: EXPIRES + 10)
        )
        assert result.ok, result.code

    async def test_meshrelay_has_no_grace_past_expiry(self):
        headers, _ = _sign()
        result = await verify_request(
            _request(headers), _policy("meshrelay-strict", now=lambda: EXPIRES + 10)
        )
        assert result.code == "signature_stale"

    async def test_created_in_the_future(self):
        headers, _ = _sign()
        result = await verify_request(
            _request(headers), _policy(now=lambda: CREATED - 3600)
        )
        assert result.code == "signature_stale"

    async def test_validity_window_too_wide(self):
        headers, _ = _sign(expires=CREATED + 4000)
        result = await verify_request(_request(headers), _policy())
        assert result.code == "signature_stale"

    async def test_chain_allowlist(self):
        headers, _ = _sign(keyid=f"erc8128:1:{WALLET}")
        assert (await verify_request(_request(headers), _policy("em-lenient"))).ok
        strict = await verify_request(_request(headers), _policy("meshrelay-strict"))
        assert strict.code == "chain_not_allowed"

    async def test_label_policy(self):
        headers, _ = _sign(label="sig1")
        assert (await verify_request(_request(headers), _policy("em-lenient"))).ok
        strict = await verify_request(_request(headers), _policy("meshrelay-strict"))
        assert strict.code == "signature_input_invalid"

    async def test_unsupported_alg_is_rejected_in_both_postures(self):
        headers, _ = _sign(alg='alg="es256"')
        for preset in ("em-lenient", "meshrelay-strict"):
            result = await verify_request(_request(headers), _policy(preset))
            assert result.code == "alg_unsupported", preset

    async def test_canonical_strict_rejects_the_legacy_generations(self):
        no_alg, _ = _sign(alg=None)
        result = await verify_request(_request(no_alg), _policy("canonical-strict"))
        assert result.code == "alg_missing"
        checksum, _ = _sign(keyid=f"erc8128:{CHAIN}:{FROZEN['address_checksummed']}")
        result = await verify_request(_request(checksum), _policy("canonical-strict"))
        assert result.code == "keyid_not_lowercase"

    async def test_authority_must_be_a_safe_value(self):
        headers, _ = _sign()
        result = await verify_request(
            _request(headers),
            VerifyPolicy(authority="https://api.execution.market/x", now=lambda: NOW),
        )
        assert result.code == "authority_invalid"
        assert result.status == 503

    @pytest.mark.parametrize(
        "configured",
        [
            AUTHORITY.upper(),
            "API.Execution.Market",
            f"  {AUTHORITY}  ",
            f"\t{AUTHORITY}\n",
        ],
    )
    async def test_configured_authority_is_lowercased_and_trimmed(self, configured):
        """Case and surrounding whitespace are formatting, and are fixed
        silently — the trim runs BEFORE the whitespace blacklist, so a config
        read out of a YAML file with a trailing newline still works.

        The PORT is not formatting: it is not touched here at all. See
        :class:`TestConfiguredAuthority`."""
        headers, _ = _sign()
        policy = policy_from_preset(
            "em-lenient", authority=configured, nonce_store=_Store(), now=lambda: NOW
        )
        result = await verify_request(_request(headers), policy)
        assert result.ok, (configured, result.code)

    async def test_configured_non_default_port_is_part_of_the_authority(self):
        """Only the DEFAULT port is dropped. `host:8443` is a different
        authority and must keep failing against a signature over `host`."""
        def with_port():  # a fresh store per call — em-lenient spends the nonce
            return policy_from_preset(
                "em-lenient",
                authority=f"{AUTHORITY}:8443",
                nonce_store=_Store(),
                now=lambda: NOW,
            )

        signed_with_port, _ = _sign(authority=f"{AUTHORITY}:8443")
        assert (await verify_request(_request(signed_with_port), with_port())).ok

        bare, _ = _sign()
        mismatched = await verify_request(_request(bare), with_port())
        assert mismatched.code == "wallet_mismatch"

    async def test_a_default_port_url_signed_by_the_sdk_verifies(self):
        """End to end through the SDK's own signer: the one spelling D1 changes
        is exactly the one that used to sign an authority nothing reproduced."""
        wallet = EnvKeyAdapter(private_key="0x" + FROZEN["private_key"])
        url = f"https://{AUTHORITY}:443{PATH}"
        headers = sign_request(
            wallet,
            method="GET",
            url=url,
            nonce=NONCE,
            chain_id=CHAIN,
            now=lambda: CREATED,
        )
        result = await verify_request(
            VerifiableRequest(method="GET", url=url, headers=headers), _policy()
        )
        assert result.ok, result.code
        assert '"@authority": api.execution.market\n' in result.signature_base

    async def test_canonical_strict_pins_the_production_chain(self):
        """A preset named "strict" that accepts any chain is not strict."""
        assert POLICY_PRESETS["canonical-strict"].allowed_chain_ids == (8453,)
        headers, _ = _sign(keyid=f"erc8128:1:{WALLET}")
        result = await verify_request(_request(headers), _policy("canonical-strict"))
        assert result.code == "chain_not_allowed"

    async def test_wrong_authority_does_not_recover_the_signer(self):
        headers, _ = _sign()
        policy = policy_from_preset(
            "em-lenient", authority="evil.example", nonce_store=_Store(), now=lambda: NOW
        )
        result = await verify_request(_request(headers), policy)
        assert result.code == "wallet_mismatch"

    async def test_tampered_path_does_not_recover_the_signer(self):
        headers, _ = _sign(path="/api/v1/tasks")
        result = await verify_request(
            _request(headers, path="/api/v1/admin"), _policy()
        )
        assert result.code == "wallet_mismatch"

    async def test_truncated_signature(self):
        headers, _ = _sign()
        raw = base64.b64decode(headers["Signature"][len("eth=:") : -1])
        headers["Signature"] = "eth=:" + base64.b64encode(raw[:64]).decode() + ":"
        result = await verify_request(_request(headers), _policy())
        assert result.code == "signature_invalid"

    async def test_flipped_signature_byte(self):
        headers, _ = _sign()
        raw = bytearray(base64.b64decode(headers["Signature"][len("eth=:") : -1]))
        raw[0] ^= 0xFF
        headers["Signature"] = "eth=:" + base64.b64encode(bytes(raw)).decode() + ":"
        result = await verify_request(_request(headers), _policy())
        assert not result.ok
        assert result.code in ("signature_invalid", "wallet_mismatch")

    async def test_missing_headers(self):
        headers, _ = _sign()
        no_input = await verify_request(
            _request({"Signature": headers["Signature"]}), _policy()
        )
        assert no_input.code == "signature_input_invalid"
        no_sig = await verify_request(
            _request({"Signature-Input": headers["Signature-Input"]}), _policy()
        )
        assert no_sig.code == "signature_invalid"


# ---------------------------------------------------------------------------
# The CONFIGURED authority
#
# Two operations that used to be one. The URL-derived `@authority` drops the
# port when it is THAT SCHEME's default; the configured policy value carries no
# scheme, so it is never re-ported at all — it is the EXPECTED RESULT of the
# first rule and must already be written in its output form.
#
# Why not just pick a scheme for the config: over the four deployment shapes,
# each guess breaks two of them.
#
#     deploy          signed @authority   assume https   drop either port
#     https on :443   host                OK             OK
#     http  on :80    host                BROKEN         OK
#     https on :80    host:80             OK             BROKEN
#     http  on :443   host:443            BROKEN         BROKEN
#
# The rows are asserted below, signed form first (against the adversary signer,
# never the SDK's own builders) and then configured.
# ---------------------------------------------------------------------------

#: ``(url, the @authority that deployment signs, is that value configurable?)``
DEPLOYMENT_SHAPES = [
    (f"https://{AUTHORITY}:443{PATH}", AUTHORITY, True),
    (f"http://{AUTHORITY}:80{PATH}", AUTHORITY, True),
    (f"https://{AUTHORITY}:80{PATH}", f"{AUTHORITY}:80", False),
    (f"http://{AUTHORITY}:443{PATH}", f"{AUTHORITY}:443", False),
]
SHAPE_IDS = ["https-on-443", "http-on-80", "https-on-80", "http-on-443"]


def _sdk_sign(url):
    """The SDK's own signer over a deployment URL, on the frozen clock."""
    return sign_request(
        EnvKeyAdapter(private_key="0x" + FROZEN["private_key"]),
        method="GET",
        url=url,
        nonce=NONCE,
        chain_id=CHAIN,
        now=lambda: CREATED,
    )


def _authority_policy(configured):
    return policy_from_preset(
        "em-lenient",
        authority=configured,
        nonce_store=_Store(),  # fresh per call — em-lenient spends the nonce
        now=lambda: NOW,
    )


class TestConfiguredAuthority:
    @pytest.mark.parametrize(
        "url,signed,configurable", DEPLOYMENT_SHAPES, ids=SHAPE_IDS
    )
    def test_each_deployment_shape_signs_what_the_table_says(
        self, url, signed, configurable
    ):
        """The signed bytes, pinned: the SDK signer's signature must equal the
        adversary signer's over a base carrying exactly that authority. Only
        the scheme's OWN default port is dropped, so `https` on `:80` and
        `http` on `:443` really do sign the port."""
        expected, _ = _sign(authority=signed)
        assert _sdk_sign(url)["Signature"] == expected["Signature"]

    @pytest.mark.parametrize(
        "url,signed,configurable", DEPLOYMENT_SHAPES, ids=SHAPE_IDS
    )
    async def test_configuring_exactly_what_was_signed(self, url, signed, configurable):
        """Configure the authority the deployment signs and verify end to end.

        Rows 3 and 4 cannot be configured at all: their signed authority
        carries a port that is some scheme's default, and R2 answers 503 with
        the dedicated message instead of silently stripping it (stripping is
        what turned https-on-`:80` into an unexplainable wallet_mismatch)."""
        headers = _sdk_sign(url)
        result = await verify_request(
            VerifiableRequest(method="GET", url=url, headers=headers),
            _authority_policy(signed),
        )
        if configurable:
            assert result.ok, result.code
            assert f'"@authority": {signed}\n' in result.signature_base
        else:
            assert result.code == "authority_invalid"
            assert result.status == 503  # never 401 — the operator's typo
            assert "default port" in result.message

    async def test_a_non_default_port_survives_both_sides_of_the_wire(self):
        """The other half of R3: `https://host:8443/` signs `host:8443`, and a
        policy configured `host:8443` must ACCEPT — the port is preserved
        verbatim on both sides, and it is load-bearing."""
        url = f"https://{AUTHORITY}:8443{PATH}"
        headers = _sdk_sign(url)
        adversary, _ = _sign(authority=f"{AUTHORITY}:8443")
        assert headers["Signature"] == adversary["Signature"]

        accepted = await verify_request(
            VerifiableRequest(method="GET", url=url, headers=headers),
            _authority_policy(f"{AUTHORITY}:8443"),
        )
        assert accepted.ok, accepted.code
        assert f'"@authority": {AUTHORITY}:8443\n' in accepted.signature_base

        portless = await verify_request(
            VerifiableRequest(method="GET", url=url, headers=headers),
            _authority_policy(AUTHORITY),
        )
        assert portless.code == "wallet_mismatch"

    def test_the_configured_value_is_never_re_ported(self):
        """Lowercase and trim, nothing else. An IPv6 literal's own colons are
        not a port, and a non-default port is kept whatever it is."""
        assert policy_authority(f"  {AUTHORITY.upper()}:8443  ") == f"{AUTHORITY}:8443"
        assert policy_authority("[2001:db8::1]:8443") == "[2001:db8::1]:8443"
        assert policy_authority("[2001:DB8::1]") == "[2001:db8::1]"

    REJECTED = [
        ("empty", ""),
        ("blank", "   "),
        ("whitespace-only", "\t\r\n"),
        ("nbsp-only", "\u00a0"),
        ("too-long", "a" * 254),
        ("inner-space", "api.execution .market"),
        ("inner-tab", "api.execution\t.market"),
        ("inner-lf", "api.execution\n.market"),
        ("inner-cr", "api.execution\r.market"),
        ("inner-vt", "api.execution\v.market"),
        ("inner-ff", "api.execution\f.market"),
        ("inner-nbsp", "api.execution\u00a0.market"),
        ("slash", "api.execution.market/"),
        ("at", "user@api.execution.market"),
        ("question", "api.execution.market?x=1"),
        ("hash", "api.execution.market#frag"),
        ("scheme-prefix", "https://api.execution.market"),
        ("path-component", "api.execution.market/api/v1"),
        ("full-url", "https://api.execution.market/api/v1/tasks?x=1"),
    ]

    @pytest.mark.parametrize(
        "configured", [value for _, value in REJECTED], ids=[name for name, _ in REJECTED]
    )
    async def test_misconfiguration_answers_503_never_401(self, configured):
        """Every rejected class is the OPERATOR's mistake, so it is 503 with
        the generic message. Answering 401 blames the client for a typo it
        cannot see or fix — and the whitespace run matters beyond hygiene: a
        CR, LF, VT, FF or NBSP that slipped through would be embedded straight
        into the rebuilt signature base."""
        headers, _ = _sign()
        result = await verify_request(_request(headers), _authority_policy(configured))
        assert result.code == "authority_invalid"
        assert result.status == 503
        assert result.status != 401
        assert result.message == "ERC-8128 authority is not configured safely"

    @pytest.mark.parametrize(
        "configured,port,corrected",
        [
            (f"{AUTHORITY}:443", "443", AUTHORITY),
            (f"{AUTHORITY}:80", "80", AUTHORITY),
            ("API.Execution.Market:443", "443", AUTHORITY),
            ("[2001:db8::1]:443", "443", "[2001:db8::1]"),
        ],
    )
    async def test_a_default_port_in_the_config_gets_its_own_message(
        self, configured, port, corrected
    ):
        """Its own message because the fix is specific and unguessable from
        the generic one: write the authority the way it will be signed."""
        headers, _ = _sign()
        result = await verify_request(_request(headers), _authority_policy(configured))
        assert result.code == "authority_invalid"
        assert result.status == 503
        assert result.status != 401
        assert f"':{port}'" in result.message
        assert f"configure '{corrected}'" in result.message
        assert result.message != "ERC-8128 authority is not configured safely"


# ---------------------------------------------------------------------------
# Nonce
# ---------------------------------------------------------------------------


class TestNonce:
    async def test_replay_is_rejected_with_409(self):
        headers, _ = _sign()
        store = _Store()
        policy = _policy(store=store)
        assert (await verify_request(_request(headers), policy)).ok
        replay = await verify_request(_request(headers), policy)
        assert replay.code == "nonce_replayed"
        assert replay.status == 409
        assert replay.retryable is False

    async def test_issuer_bound_store_answers_unknown(self):
        headers, _ = _sign()
        result = await verify_request(
            _request(headers), _policy(store=_Store(known={"issued-by-me"}))
        )
        assert result.code == "nonce_unknown"

    async def test_store_failure_is_503_and_retryable(self):
        headers, _ = _sign()
        result = await verify_request(_request(headers), _policy(store=_Store(raises=True)))
        assert result.code == "nonce_store_unavailable"
        assert result.status == 503
        assert result.retryable is True

    async def test_missing_nonce_when_required(self):
        headers, _ = _sign(nonce=None)
        result = await verify_request(_request(headers), _policy())
        assert result.code == "nonce_required"

    async def test_consume_order_is_a_value_not_a_branch(self):
        """EM burns the nonce before the crypto (closes a race); MeshRelay
        after (an unauthenticated caller cannot burn one). Both are live
        product decisions — the SDK must reproduce each exactly."""
        headers, _ = _sign(path="/api/v1/tasks")
        bad = _request(headers, path="/api/v1/admin")  # recovery will fail

        before = _Store()
        result = await verify_request(bad, _policy("em-lenient", store=before))
        assert result.code == "wallet_mismatch"
        assert before.calls == 1, "EM's order burns the nonce on a bad signature"

        after = _Store()
        result = await verify_request(bad, _policy("meshrelay-strict", store=after))
        assert after.calls == 0, "MeshRelay's order never burns it"

    async def test_async_store_is_supported(self):
        class AsyncStore:
            def __init__(self):
                self.calls = 0

            async def consume(self, nonce, **ctx):
                self.calls += 1
                return "ok"

        store = AsyncStore()
        headers, _ = _sign()
        result = await verify_request(_request(headers), _policy(store=store))
        assert result.ok, result.code
        assert store.calls == 1

    async def test_store_receives_the_raw_nonce_and_context(self):
        seen = {}

        class Recorder:
            def consume(self, nonce, **ctx):
                seen["nonce"] = nonce
                seen.update(ctx)
                return "ok"

        headers, _ = _sign()
        await verify_request(_request(headers), _policy(store=Recorder()))
        assert seen["nonce"] == NONCE, "the store derives its OWN key"
        assert seen["wallet"] == WALLET
        assert seen["chain_id"] == CHAIN
        assert seen["created"] == CREATED and seen["expires"] == EXPIRES


# ---------------------------------------------------------------------------
# ERC-1271, census, taxonomy
# ---------------------------------------------------------------------------


class TestContractVerifier:
    async def _contract_headers(self):
        # A signature by a DIFFERENT key, claiming the frozen address: what a
        # smart-contract wallet looks like to ecrecover.
        other = Account.from_key("0x" + "11" * 32)
        return _sign(account=other)

    async def test_erc1271_accepts(self):
        headers, base = await self._contract_headers()
        captured = {}

        async def verifier(*, address, chain_id, message_hash, signature):
            captured.update(
                address=address, chain_id=chain_id, message_hash=message_hash
            )
            return True

        result = await verify_request(
            _request(headers), _policy(contract_verifier=verifier)
        )
        assert result.ok, result.code
        assert result.via == "erc1271"
        assert captured["address"] == WALLET
        assert len(captured["message_hash"]) == 32

    async def test_erc1271_rejects(self):
        headers, _ = await self._contract_headers()

        async def verifier(**_):
            return False

        result = await verify_request(
            _request(headers), _policy(contract_verifier=verifier)
        )
        assert result.code == "wallet_mismatch"

    async def test_erc1271_timeout(self):
        headers, _ = await self._contract_headers()

        async def verifier(**_):
            await asyncio.sleep(1)
            return True

        result = await verify_request(
            _request(headers),
            _policy(contract_verifier=verifier, contract_verifier_timeout=0.01),
        )
        assert result.code == "wallet_mismatch"
        assert "timed out" in result.message


class TestCensus:
    async def test_fires_on_success_and_on_failure(self):
        seen = []
        headers, _ = _sign(alg=None)
        policy = _policy(on_observed_profile=lambda p, ctx: seen.append((p, ctx)))
        assert (await verify_request(_request(headers), policy)).ok
        assert seen[-1][0] == "legacy_no_alg"
        assert seen[-1][1]["outcome"] == "ok"

        stale = _policy(
            now=lambda: EXPIRES + 1000,
            on_observed_profile=lambda p, ctx: seen.append((p, ctx)),
        )
        await verify_request(_request(headers), stale)
        assert seen[-1] == (
            "legacy_no_alg",
            {
                "wallet": WALLET,
                "chain_id": CHAIN,
                "keyid": f"erc8128:{CHAIN}:{WALLET}",
                "outcome": "signature_stale",
            },
        ), "a legacy emitter failing for another reason must still be counted"

    async def test_a_raising_hook_does_not_break_auth(self):
        def boom(*_args, **_kwargs):
            raise RuntimeError("metrics down")

        headers, _ = _sign()
        result = await verify_request(
            _request(headers), _policy(on_observed_profile=boom)
        )
        assert result.ok


class TestTaxonomy:
    def test_every_code_has_a_status_and_a_retryable_flag(self):
        from uvd_x402_sdk.erc8128 import ERC8128_CODES

        assert set(ERC8128_ERROR_STATUS) == set(ERC8128_CODES)
        assert set(ERC8128_ERROR_RETRYABLE) == set(ERC8128_CODES)
        assert set(ERC8128_ERROR_STATUS.values()) <= {401, 409, 429, 503}

    def test_only_infrastructure_failures_are_retryable(self):
        retryable = {c for c, v in ERC8128_ERROR_RETRYABLE.items() if v}
        assert retryable == {
            "nonce_store_unavailable",
            "nonce_rate_limited",
            "nonce_capacity",
        }

    def test_meshrelay_public_contract_codes_are_all_present(self):
        # The exact strings meshrelay/api/src/lib/erc8128-auth.ts answers with.
        for code, status in {
            "signature_input_invalid": 401,
            "alg_unsupported": 401,
            "nonce_invalid": 401,
            "signature_invalid": 401,
            "signature_stale": 401,
            "chain_not_allowed": 401,
            "wallet_invalid": 401,
            "wallet_mismatch": 401,
            "components_invalid": 401,
            "content_digest_invalid": 401,
            "content_digest_mismatch": 401,
            "content_digest_required": 401,
            "nonce_unknown": 401,
            "nonce_expired": 401,
            "nonce_replayed": 409,
            "nonce_rate_limited": 429,
            "nonce_capacity": 429,
            "nonce_limits_invalid": 503,
            "nonce_ttl_invalid": 503,
            "authority_invalid": 503,
        }.items():
            assert ERC8128_ERROR_STATUS[code] == status, code


# ---------------------------------------------------------------------------
# Pure parsers
# ---------------------------------------------------------------------------


class TestParsers:
    def test_params_raw_is_verbatim(self):
        params = (
            f'keyid="erc8128:{CHAIN}:{WALLET}";created={CREATED};expires={EXPIRES};'
            f'nonce="{NONCE}";alg="eip191";tag="x"'
        )
        headers, _ = _sign(params=params)
        parsed = parse_signature_input(headers["Signature-Input"])
        assert parsed.params_raw == headers["Signature-Input"][len("eth=") :]
        assert parsed.alg == "eip191"
        assert parsed.covered == ("@method", "@authority", "@path")

    def test_extract_keyid_wallet_without_crypto(self):
        headers, _ = _sign()
        assert extract_keyid_wallet(headers["Signature-Input"]) == WALLET
        assert extract_keyid_wallet("garbage") is None

    def test_signature_must_be_65_bytes(self):
        with pytest.raises(Erc8128Error) as err:
            parse_signature_header("eth=:" + base64.b64encode(b"short").decode() + ":")
        assert err.value.code == "signature_invalid"

    def test_unquoted_component_is_rejected(self):
        bad = (
            f'eth=(@method "@path");created={CREATED};expires={EXPIRES};'
            f'keyid="erc8128:{CHAIN}:{WALLET}"'
        )
        with pytest.raises(Erc8128Error) as err:
            parse_signature_input(bad)
        assert err.value.code == "signature_input_invalid"

    def test_missing_keyid_is_rejected(self):
        with pytest.raises(Erc8128Error):
            parse_signature_input(f'eth=("@method");created={CREATED};expires={EXPIRES}')


# ---------------------------------------------------------------------------
# Round trip through the SDK's own signer
# ---------------------------------------------------------------------------


class TestRoundTrip:
    @pytest.fixture
    def wallet(self):
        return EnvKeyAdapter(private_key="0x" + FROZEN["private_key"])

    @pytest.mark.parametrize("profile", ["canonical", "legacy-no-alg"])
    @pytest.mark.parametrize("header_case", ["title", "lower"])
    async def test_sign_then_verify(self, wallet, profile, header_case):
        body = json.dumps({"title": "round trip"})
        url = f"https://{AUTHORITY}{PATH}?status=published"
        headers = sign_request(
            wallet,
            method="POST",
            url=url,
            body=body,
            nonce=NONCE,
            chain_id=CHAIN,
            profile=profile,
            header_case=header_case,
            now=lambda: CREATED,
        )
        request = VerifiableRequest(
            method="POST",
            url=url,
            headers={**headers, "content-length": str(len(body))},
            raw_body=body.encode(),
        )
        for preset in ("em-lenient", "meshrelay-strict"):
            result = await verify_request(request, _policy(preset))
            assert result.ok, (preset, result.code)
            assert result.wallet == WALLET

    async def test_body_truthy_rule_drops_the_digest_for_an_empty_body(self, wallet):
        kwargs = dict(
            method="POST",
            url=f"https://{AUTHORITY}/api/v1/tasks/abc/cancel",
            body="",
            nonce=NONCE,
            chain_id=CHAIN,
            now=lambda: CREATED,
        )
        present = sign_request(wallet, **kwargs)
        truthy = sign_request(wallet, content_digest="body-truthy", **kwargs)
        assert "Content-Digest" in present
        assert "Content-Digest" not in truthy
        vectors = load_vectors("f3-3")["vectors"]["canonical"]
        assert present["Signature"] == vectors["post_emptybody"]["headers"]["Signature"]
        assert truthy["Signature"] == vectors["post_nobody"]["headers"]["Signature"]

    def test_validity_window_is_clamped(self, wallet):
        headers = sign_request(
            wallet,
            method="GET",
            url=f"https://{AUTHORITY}{PATH}",
            nonce=NONCE,
            validity_sec=3600,
            now=lambda: CREATED,
        )
        assert f"expires={CREATED + 300}" in headers["Signature-Input"]

    def test_lower_header_case_emits_no_title_case_twin(self, wallet):
        headers = sign_request(
            wallet,
            method="POST",
            url=f"https://{AUTHORITY}{PATH}",
            body="{}",
            nonce=NONCE,
            header_case="lower",
        )
        assert set(headers) == {"signature", "signature-input", "content-digest"}
