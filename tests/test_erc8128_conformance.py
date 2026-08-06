"""ERC-8128 conformance — the shipped vectors, run against this build.

NO SKIPS ARE ALLOWED IN THIS FILE. Not ``importorskip``, not ``skipif``, not a
try/except around the import. The vectors are loaded through PACKAGE
resolution (``importlib.resources``), never by a path relative to a monorepo,
so a missing file is a collection error rather than a silent green — which is
exactly how ``em-plugin-sdk/tests/test_erc8128_canonical_parity.py`` ended up
with byte-equality assertions that never ran outside the monorepo.

What each block catches:

* ``TestVectorIntegrity`` — a hand-edited vector file. The hashes are pinned
  in code and compared against the bytes that actually shipped, so a drifted
  copy fails before anything is signed.
* ``TestSignConformance`` — the signer's bytes moved.
* ``TestVerifyMatrix`` — the accept/reject verdict of a posture moved. The
  matrix is DATA, so the two language suites cannot drift into disagreeing
  about what "strict" means.
* ``TestPolicyPresets`` — a preset drifted from the posture it claims to
  reproduce (R7: a caller who spreads a preset and overrides a knob changes
  posture with no type error; this is the tripwire).
* ``TestPackaging`` — the wheel would ship without the vectors.
"""

import hashlib
import json
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from uvd_x402_sdk.erc8128 import (
    CONFORMANCE_SHA256,
    POLICY_PRESETS,
    WIRE_CONTRACT_VERSION,
    load_vectors,
    normalize_authority,
    policy_from_preset,
    preset_as_data,
    run_conformance,
    sign_request,
    vector_bytes,
    verify_request,
)
from uvd_x402_sdk.erc8128.vectors import build_verifiable_request
from uvd_x402_sdk.wallet import EnvKeyAdapter

F3_1 = load_vectors("f3-1")
F3_3 = load_vectors("f3-3")
FROZEN = F3_1["frozen"]

ALL_REQUESTS = {**F3_1["requests"], **F3_3["requests"]}
ALL_VECTORS = {
    f"{family}/{name}": vector
    for doc in (F3_1, F3_3)
    for family, entries in doc["vectors"].items()
    for name, vector in entries.items()
}
SIGNABLE = [
    (vector_id, vector)
    for vector_id, vector in ALL_VECTORS.items()
    # The signer lowercases the keyid on purpose, so the checksummed family is
    # verify-only — it exists as tolerance coverage, not as an emit target.
    if not vector_id.startswith("legacy_alg_checksum_keyid/")
]


@pytest.fixture
def frozen_wallet():
    # Public synthetic key from the fixture, stored 0x-less so scanners never
    # see 0x + 64 hex. It never held funds.
    return EnvKeyAdapter(private_key="0x" + FROZEN["private_key"])


class TestVectorIntegrity:
    @pytest.mark.parametrize("generation", ["f3-1", "f3-3"])
    def test_shipped_bytes_match_the_pinned_hash(self, generation):
        assert (
            hashlib.sha256(vector_bytes(generation)).hexdigest()
            == CONFORMANCE_SHA256[generation]
        )

    def test_f3_1_is_the_byte_identical_upstream_copy(self):
        """9275 bytes, LF, md5 95ea41b7e939ba6a5b8aca136142b013 — the bytes
        execution-market, the TS SDK and this package all STORE. (The
        md5 54969151… quoted around the fleet is a CRLF working copy.)"""
        raw = vector_bytes("f3-1")
        assert hashlib.md5(raw).hexdigest() == "95ea41b7e939ba6a5b8aca136142b013"
        assert b"\r" not in raw

    def test_f3_3_is_lf_only(self):
        assert b"\r" not in vector_bytes("f3-3")

    def test_f3_3_declares_its_base_generation(self):
        assert F3_3["generation"] == "F3-3" == WIRE_CONTRACT_VERSION
        assert F3_3["extends"] == "F3-1"
        # F3-3 shares F3-1's frozen inputs and adds the two the verify matrix
        # needs, but NOT the private key: it is read from F3-1 by both SDKs,
        # and duplicating key material doubles what a scanner must catch.
        assert "private_key" not in F3_3["frozen"]
        for field, value in FROZEN.items():
            if field == "private_key":
                continue
            assert F3_3["frozen"][field] == value, field
        assert F3_3["frozen"]["authority"] == "api.execution.market"
        assert F3_3["frozen"]["label"] == "eth"

    def test_the_f3_3_artefact_is_the_one_shared_across_the_fleet(self):
        """One file, three repos. The bytes here are what the TypeScript SDK
        embeds and what execution-market/shared/test-vectors mirrors; the
        cross-language harness re-checks it against the live Node runtime."""
        assert hashlib.sha256(vector_bytes("f3-3")).hexdigest() == (
            "154ef31dc3e704c376e282ca5ca5dccde00877328cbab250001dbb20ba2e91ac"
        )
        assert list(CONFORMANCE_SHA256) == ["f3-1", "f3-3"]

    def test_the_artefact_ships_no_invisible_characters(self):
        """A vector carries a U+00A0 inside a CONFIGURED authority on purpose.
        It is stored as a ``\\u00a0`` ESCAPE, never as the raw byte pair: this
        file is compared byte for byte across three repos and pinned by sha256,
        and an invisible character in it is a hash no reviewer can see and any
        well-meaning editor can silently "clean"."""
        raw = vector_bytes("f3-3")
        assert chr(0xA0).encode("utf-8") not in raw
        # (the same literal, kept only as a comment): " ".encode("utf-8") not in raw
        # …and it is still THERE, as a value, after parsing.
        configured = [c.get("authority", "") for c in F3_3["verify_cases"]]
        assert any(chr(0xA0) in value for value in configured)
        # (the same literal, kept only as a comment): any(" " in value for value in configured)

    @pytest.mark.parametrize("vector_id", sorted(ALL_VECTORS))
    def test_signature_recovers_the_frozen_signer(self, vector_id):
        """The vectors are cryptographically sound on their own terms — a
        corrupted copy fails here, before any conformance assert."""
        import base64

        from eth_account import Account
        from eth_account.messages import encode_defunct

        vector = ALL_VECTORS[vector_id]
        field = vector["headers"]["Signature"]
        assert field.startswith("eth=:") and field.endswith(":")
        signature = base64.b64decode(field[len("eth=:") : -1])
        assert len(signature) == 65, "pinned encoding is base64(r||s||v, 65 bytes)"
        recovered = Account.recover_message(
            encode_defunct(text=vector["signature_base"]), signature=signature
        )
        assert recovered.lower() == FROZEN["address"]

    @pytest.mark.parametrize("vector_id", sorted(ALL_VECTORS))
    def test_signature_input_is_the_signature_params_line(self, vector_id):
        vector = ALL_VECTORS[vector_id]
        last = vector["signature_base"].splitlines()[-1]
        assert last.startswith('"@signature-params": ')
        assert vector["headers"]["Signature-Input"] == "eth=" + last[
            len('"@signature-params": ') :
        ]

    @pytest.mark.parametrize("vector_id", sorted(ALL_VECTORS))
    def test_base_is_lf_joined_with_no_trailing_newline(self, vector_id):
        base = ALL_VECTORS[vector_id]["signature_base"]
        assert "\r" not in base
        assert not base.endswith("\n")


class TestSignConformance:
    @pytest.mark.parametrize("vector_id,vector", SIGNABLE, ids=[v for v, _ in SIGNABLE])
    def test_headers_match_byte_for_byte(self, frozen_wallet, vector_id, vector):
        family, name = vector_id.split("/", 1)
        spec = ALL_REQUESTS[name]
        headers = sign_request(
            frozen_wallet,
            method=spec["method"],
            url=spec["url"],
            body=spec["body"],
            nonce=FROZEN["nonce"],
            chain_id=FROZEN["chain_id"],
            profile="canonical" if family == "canonical" else "legacy-no-alg",
            now=lambda: FROZEN["created"],
        )
        assert headers == vector["headers"]

    @pytest.mark.parametrize("name", sorted(ALL_REQUESTS))
    def test_every_request_signs_the_authority_the_rule_derives(self, name):
        """The `@authority` line of the pinned vector is what
        :func:`normalize_authority` produces for that URL — checked for EVERY
        request, including the six written with a port or an uppercase host.

        This used to assert `normalize_authority(netloc, scheme) == netloc`,
        i.e. that no pinned URL could see the rule at all. That was true, and
        it was the bug: it made the whole vector set blind to the authority
        rule, so reverting either SDK to the scheme-blind variant kept every
        suite green."""
        parsed = urlsplit(ALL_REQUESTS[name]["url"])
        derived = normalize_authority(parsed.netloc, parsed.scheme)
        signed = ALL_VECTORS[f"canonical/{name}"]["signature_base"].splitlines()[1]
        assert signed == f'"@authority": {derived}'

    def test_the_authority_vectors_separate_the_two_candidate_rules(self):
        """The four deployment shapes, as the vectors sign them. A scheme-blind
        "drop :443 and :80 always" rule agrees with the correct one on the
        first three rows and DISAGREES on the last two — which is the only
        reason those two exist."""
        authority_of = lambda name: (  # noqa: E731
            ALL_VECTORS[f"canonical/{name}"]["signature_base"].splitlines()[1]
        )
        host = "api.execution.market"

        # Formatting only: these collapse onto the bare host.
        assert authority_of("authority_uppercase_host") == f'"@authority": {host}'
        assert authority_of("authority_https_on_443") == f'"@authority": {host}'
        assert authority_of("authority_http_on_80") == f'"@authority": {host}'

        # A non-default port is part of the authority.
        assert authority_of("authority_https_on_8443") == f'"@authority": {host}:8443'

        # THE TWO THAT BITE: 443 is ordinary under http, 80 is ordinary under
        # https, so both survive into the signature.
        assert authority_of("authority_http_on_443") == f'"@authority": {host}:443'
        assert authority_of("authority_https_on_80") == f'"@authority": {host}:80'

        # …and each of those signs DIFFERENT bytes from its same-port twin
        # under the other scheme, which no pre-existing vector could show.
        assert authority_of("authority_http_on_443") != authority_of(
            "authority_https_on_443"
        )
        assert authority_of("authority_https_on_80") != authority_of(
            "authority_http_on_80"
        )

    def test_the_new_generation_covers_the_content_digest_split(self):
        """V4/V5/V7: the calls where the fleet has three incompatible
        predicates and F3-1 had no vector at all."""
        canonical = F3_3["vectors"]["canonical"]
        assert "Content-Digest" not in canonical["post_nobody"]["headers"]
        assert "Content-Digest" not in canonical["delete_nobody"]["headers"]
        assert "Content-Digest" in canonical["post_emptybody"]["headers"]
        assert (
            canonical["post_nobody"]["headers"]["Signature"]
            != canonical["post_emptybody"]["headers"]["Signature"]
        ), "same call, two predicates, two different signatures"


class TestVerifyMatrix:
    CASES = F3_3["verify_cases"]

    def test_the_matrix_covers_every_posture_and_both_generations(self):
        policies = {case["policy"] for case in self.CASES}
        assert policies == {"meshrelay-strict", "em-lenient", "canonical-strict"}
        assert {case["vector_id"] for case in self.CASES} <= set(ALL_VECTORS)
        assert any(case["expect"] == "reject" for case in self.CASES)

    @pytest.mark.parametrize(
        "case",
        CASES,
        ids=[
            f"{i}|{c['vector_id']}|{c['policy']}|{c['expect']}"
            for i, c in enumerate(CASES)
        ],
    )
    async def test_case(self, case):
        vector = ALL_VECTORS[case["vector_id"]]
        spec = ALL_REQUESTS[case["vector_id"].split("/", 1)[1]]
        request = build_verifiable_request(spec, vector["headers"])
        policy = policy_from_preset(
            case["policy"],
            # The CONFIGURED authority is a per-case value, not something read
            # back out of the request being checked. Deriving it from
            # `urlsplit(url).netloc` — which is what this line used to do —
            # meant a request written `https://host:443/x` configured
            # `host:443` and the case tested a rule nobody had written down.
            authority=case.get("authority", F3_3["frozen"]["authority"]),
            nonce_store=_FirstUseStore(),
            now=lambda: FROZEN["created"] + 1,
        )
        result = await verify_request(request, policy)
        assert result.ok is (case["expect"] == "accept"), result.code
        assert result.observed_profile == case["observed_profile"]
        if case["expect"] == "accept":
            assert result.wallet == case["wallet"]
        else:
            assert result.code == case["code"]
            # 401 blames the client, 503 blames the operator.
            assert result.status == case["status"]

    def test_the_matrix_pins_the_configured_authority_as_data(self):
        """R2, as rows rather than prose: a default port in the CONFIGURED
        value is 503 (the operator's typo), a non-default port is accepted
        against a matching signature, and every whitespace character the rule
        names is covered — NBSP included, because JS ``\\s`` matches it and
        Python ``\\s`` does not, so a rule spelled ``\\s`` on both sides would
        reject two different input sets."""
        configured = [c for c in self.CASES if "authority" in c]
        assert configured, "no case exercises the configured authority"

        invalid = [c for c in configured if c.get("code") == "authority_invalid"]
        assert len(invalid) == 7  # two default ports + five whitespace classes
        for case in invalid:
            assert case["status"] == 503, case
            assert case["status"] != 401, case
            # Refused before Signature-Input is ever parsed.
            assert case["observed_profile"] is None, case

        values = [c["authority"] for c in invalid]
        # chr(0xA0) rather than the literal character: an invisible byte in an
        # assertion is one an editor can silently turn into a space.
        for char in ("\n", "\r", "\v", "\f", chr(0xA0)):
            assert any(char in value for value in values), repr(char)
        # (the literal that used to live here, kept commented so nothing in
        #  this file depends on an invisible byte): (" ")
        assert "api.execution.market:443" in values
        assert "api.execution.market:80" in values

        accepted = [
            c
            for c in configured
            if c["expect"] == "accept" and c["authority"] == "api.execution.market:8443"
        ]
        assert len(accepted) == 3
        assert {c["vector_id"] for c in accepted} == {
            "canonical/authority_https_on_8443"
        }

    def test_every_reject_row_pins_its_http_status(self):
        for case in self.CASES:
            if case["expect"] != "reject":
                continue
            assert case["status"] in (401, 409, 429, 503), case

    async def test_the_split_case_is_the_reason_this_matrix_exists(self):
        """A bodyless POST passes EM and 401s MeshRelay — the one genuinely
        open contract divergence, now pinned as data instead of prose."""
        split = [
            c
            for c in self.CASES
            if c["vector_id"] == "canonical/post_nobody"
        ]
        by_policy = {c["policy"]: c for c in split}
        assert by_policy["em-lenient"]["expect"] == "accept"
        assert by_policy["meshrelay-strict"]["code"] == "content_digest_required"


class TestPolicyPresets:
    @pytest.mark.parametrize("name", sorted(POLICY_PRESETS))
    def test_runtime_preset_equals_the_pinned_block(self, name):
        assert preset_as_data(name) == F3_3["policies"][name]

    def test_meshrelay_preset_reproduces_todays_posture(self):
        preset = POLICY_PRESETS["meshrelay-strict"]
        assert preset.components == "exact-ordered"
        assert preset.content_digest == "non-idempotent-methods"
        assert preset.allowed_chain_ids == (8453,)
        assert preset.clock_skew_past_expiry_sec == 0
        assert preset.label == "eth"

    def test_em_preset_reproduces_todays_posture(self):
        preset = POLICY_PRESETS["em-lenient"]
        assert preset.components == "request-bound-subset"
        assert preset.content_digest == "body-present"
        assert preset.allowed_chain_ids is None
        assert preset.clock_skew_past_expiry_sec == 30
        assert preset.label == "any"

    def test_presets_carry_no_authority_and_no_store(self):
        for preset in POLICY_PRESETS.values():
            assert preset.authority == ""
            assert preset.nonce is None


class TestRunConformance:
    def test_full_run_is_green(self):
        report = run_conformance()
        assert report.failed == []
        assert report.ok
        assert report.passed == report.total
        assert report.wire_contract_version == "F3-3"

    @pytest.mark.parametrize("only", ["sign", "verify"])
    def test_sections_can_run_alone(self, only):
        report = run_conformance(only=only)
        assert report.failed == []
        assert report.passed > 0

    def test_a_drifted_hash_is_reported_not_raised(self, monkeypatch):
        monkeypatch.setitem(CONFORMANCE_SHA256, "f3-3", "0" * 64)
        report = run_conformance(only="sign")
        assert not report.ok
        assert report.failed[0]["section"] == "integrity"


class TestPackaging:
    def test_the_vectors_live_inside_the_package(self):
        import uvd_x402_sdk.erc8128 as pkg

        package_dir = Path(pkg.__file__).parent
        for name in ("erc8128.f3-1.json", "erc8128.f3-3.json"):
            assert (package_dir / name).is_file()

    def test_package_data_is_declared(self):
        """Autodiscovery ships only .py — without this stanza the vectors load
        from a source checkout and raise FileNotFoundError from a wheel."""
        pyproject = (
            Path(__file__).resolve().parents[1] / "pyproject.toml"
        ).read_text(encoding="utf-8")
        assert "[tool.setuptools.package-data]" in pyproject
        assert 'uvd_x402_sdk = ["**/*.json"]' in pyproject

    def test_vectors_are_readable_as_package_resources(self):
        assert json.loads(vector_bytes("f3-1").decode("utf-8"))["frozen"] == FROZEN


class _FirstUseStore:
    def __init__(self):
        self._seen = set()

    def consume(self, nonce, *, wallet, chain_id, **_):
        key = (nonce, wallet, chain_id)
        if key in self._seen:
            return "replayed"
        self._seen.add(key)
        return "ok"
