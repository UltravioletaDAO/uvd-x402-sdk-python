"""Interop v1: the conformance vectors (``interop/vectors``) and the offline
runner that evaluates them (``uvd_x402_sdk.interop``).

NO SKIPS. ``jsonschema`` is a ``dev`` dependency (and the ``interop`` extra):
without it this module is a collection error, never a silent green.

What each block catches:

* ``TestVectorIndex`` -- a vector file nobody runs, a case without its rule or
  its reason, a rule cited that the spec never wrote.
* ``TestGreen`` -- the reference implementation disagreeing with the vectors;
  a valid fixture that breaks a rule only the runner checks (the schema cannot
  see it); a case that is listed but never evaluated.
* ``TestRed`` -- the other state of the closure: a broken fixture, a broken
  vector, an unlisted file, a broken schema or a broken manifest turns the
  runner red, with the item named, and never into a traceback. So do a valid
  fixture that breaks a runner rule, an empty vector, and a case whose rule
  its index entry does not list (or an index rule no case fixes).
* ``TestOffline`` -- a ``$ref`` to another document is an error in the
  runner, never a download (stock jsonschema would fetch it).
* ``TestEveryCaseIsLive`` -- a case whose expectation can flip without the
  runner noticing: every expectation of every case is flipped, one at a time.
* ``TestWrongImplementations`` -- a vector set a plausible wrong
  implementation would pass. Each named mistake must turn its vector red.
* ``TestCommandLine`` / ``TestPublishGate`` -- the exit codes, ``python -m``,
  the console script, and ``publish.yml`` running this folder before it
  builds, on tags only.
"""

from __future__ import annotations

import copy
import datetime
import json
import re
import shutil
import subprocess
import sys
import urllib.request
from collections.abc import Callable, Iterator, Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest
from jsonschema import Draft202012Validator

from uvd_x402_sdk.interop import __main__ as interop_main
from uvd_x402_sdk.interop import conformance
from uvd_x402_sdk.interop.__main__ import main
from uvd_x402_sdk.interop.conformance import (
    REFERENCE,
    SIGNATURE_HEADERS,
    EventInbox,
    Implementation,
    check_manifest,
    evaluate_vector,
    load_validators,
    run,
    schema_errors,
)

ROOT = Path(__file__).resolve().parents[2]
INTEROP = ROOT / "interop"
VECTORS = INTEROP / "vectors"
FIXTURES = INTEROP / "fixtures"

INDEX: dict[str, dict[str, Any]] = json.loads((VECTORS / "index.json").read_text(encoding="utf-8"))[
    "vectors"
]
VALIDATORS = load_validators(INTEROP)

#: "## R6.3 · ..." / "### R5.1 · ..." and "## L9 · ..." -- where a rule or a
#: legacy profile is written.
WRITTEN = re.compile(r"^#{2,3} ((?:R[0-9]+[.][0-9]+)|(?:L[0-9]+)) ", re.MULTILINE)


def _vector(kind: str) -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads((VECTORS / INDEX[kind]["file"]).read_text(encoding="utf-8"))
    return loaded


def _cases_key(kind: str) -> str:
    return "scenarios" if kind == "event-redelivery" else "cases"


def _failures(kind: str, implementation: Implementation = REFERENCE) -> list[tuple[str, str]]:
    return evaluate_vector(_vector(kind), VALIDATORS, implementation)[1]


def _copy_interop(tmp_path: Path) -> Path:
    target = tmp_path / "interop"
    shutil.copytree(INTEROP, target)
    return target


def _edit_json(path: Path, edit: Callable[[Any], None]) -> None:
    document = json.loads(path.read_text(encoding="utf-8"))
    edit(document)
    path.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")


def _items(report: conformance.Report) -> list[str]:
    return [failure.item for failure in report.failures]


class TestVectorIndex:
    def test_every_file_is_listed_and_every_listed_file_exists(self) -> None:
        on_disk = sorted(p.relative_to(VECTORS).as_posix() for p in VECTORS.rglob("*.json"))
        listed = sorted(entry["file"] for entry in INDEX.values())
        assert on_disk == sorted(listed + ["index.json"])

    @pytest.mark.parametrize("kind", sorted(INDEX))
    def test_each_file_says_its_kind(self, kind: str) -> None:
        vector = _vector(kind)
        assert vector["spec"] == "uvd-interop/1"
        assert vector["kind"] == kind
        assert vector["about"].strip()

    @pytest.mark.parametrize("kind", sorted(INDEX))
    def test_every_case_has_an_id_a_rule_of_its_file_and_a_reason(self, kind: str) -> None:
        cases = _vector(kind)[_cases_key(kind)]
        assert cases
        ids = [case["id"] for case in cases]
        assert len(ids) == len(set(ids))
        for case in cases:
            assert case["rule"] in INDEX[kind]["rules"], case["id"]
            assert case["why"].strip(), case["id"]

    def test_every_cited_rule_is_written(self) -> None:
        written = set()
        for doc in INTEROP.glob("*.md"):
            written.update(WRITTEN.findall(doc.read_text(encoding="utf-8")))
        cited = {rule for entry in INDEX.values() for rule in entry["rules"]}
        assert cited and written
        assert sorted(cited - written) == []

    def test_every_index_rule_is_fixed_by_some_case(self) -> None:
        for kind, entry in INDEX.items():
            used = {case["rule"] for case in _vector(kind)[_cases_key(kind)]}
            assert sorted(set(entry["rules"]) - used) == [], kind


class TestGreen:
    def test_the_whole_suite_is_green_and_every_case_ran(self) -> None:
        report = run(INTEROP)
        assert [str(f) for f in report.failures] == []
        cases = json.loads((FIXTURES / "cases.json").read_text(encoding="utf-8"))["cases"]
        assert report.fixtures == len(cases)
        assert report.vectors == {kind: len(_vector(kind)[_cases_key(kind)]) for kind in INDEX}

    @pytest.mark.parametrize(
        "fixture",
        sorted(p.name for p in (FIXTURES / "uvd-stack" / "valid").glob("*.json")),
    )
    def test_a_valid_manifest_fixture_passes_the_runner_rules(self, fixture: str) -> None:
        # The schema cannot see R1.10, R3.3 or R5.5: a "valid" example that
        # breaks one of them teaches the wrong thing to whoever copies it.
        document = json.loads(
            (FIXTURES / "uvd-stack" / "valid" / fixture).read_text(encoding="utf-8")
        )
        assert check_manifest(document, VALIDATORS) == []

    def test_the_reference_inbox_answers_with_valid_envelopes(self) -> None:
        inbox = EventInbox()
        event = _vector("event-redelivery")["events"]["pedido-pagado"]
        keyid = "erc8128:8453:0x5eed00000000000000000000000000000000cafe"
        answers = [
            inbox.deliver(event, keyid, "C1CZEy1O14Hm_AU4IgcpLg", False),
            inbox.deliver(event, keyid, "C1CZEy1O14Hm_AU4IgcpLg", True),
            inbox.deliver(event, keyid, "C1CZEy1O14Hm_AU4IgcpLg", True),
        ]
        assert [status for status, _ in answers] == [401, 202, 409]
        for status, body in answers:
            if status >= 400:
                assert schema_errors(VALIDATORS["uvd-error"], body) == []

    def test_every_event_of_the_vectors_is_typed_by_its_source(self) -> None:
        # R5.2 is a runner rule for events; the vectors' own events keep it.
        for event in _vector("event-redelivery")["events"].values():
            assert event["type"].split(".", 1)[0] == event["source"]


class TestRed:
    def test_a_broken_fixture(self, tmp_path: Path) -> None:
        interop = _copy_interop(tmp_path)
        _edit_json(interop / "fixtures/uvd-stack/valid/minimo.json", lambda d: d.pop("app"))
        report = run(interop)
        assert not report.green
        assert _items(report) == ["uvd-stack/valid/minimo.json"]

    def test_a_broken_vector(self, tmp_path: Path) -> None:
        interop = _copy_interop(tmp_path)

        def flip(vector: Any) -> None:
            case = next(c for c in vector["cases"] if c["id"] == "permiso-denegado")
            case["expect"]["ip_ban"] = True

        _edit_json(interop / "vectors" / INDEX["ip-ban"]["file"], flip)
        report = run(interop)
        assert _items(report) == ["ip-ban/permiso-denegado"]

    def test_a_malformed_case_is_red_not_a_traceback(self, tmp_path: Path) -> None:
        interop = _copy_interop(tmp_path)
        _edit_json(
            interop / "vectors" / INDEX["request-signing"]["file"],
            lambda v: v["cases"][0].pop("expect"),
        )
        report = run(interop)
        assert _items(report) == ["request-signing/sondeo-de-salud-sin-firma"]
        assert "malformed" in report.failures[0].detail

    def test_a_vector_file_nobody_lists(self, tmp_path: Path) -> None:
        interop = _copy_interop(tmp_path)
        shutil.copy(interop / "vectors" / INDEX["ip-ban"]["file"], interop / "vectors" / "x.json")
        assert _items(run(interop)) == ["x.json"]

    def test_a_kind_the_runner_does_not_know(self, tmp_path: Path) -> None:
        interop = _copy_interop(tmp_path)
        (interop / "vectors" / "nuevo.json").write_text(
            json.dumps({"spec": "uvd-interop/1", "kind": "nuevo", "cases": []}),
            encoding="utf-8",
        )
        _edit_json(
            interop / "vectors" / "index.json",
            lambda i: i["vectors"].update({"nuevo": {"file": "nuevo.json", "rules": []}}),
        )
        report = run(interop)
        assert _items(report) == ["nuevo/*"]
        assert "no implementation" in report.failures[0].detail

    def test_a_vector_listed_under_another_kind(self, tmp_path: Path) -> None:
        interop = _copy_interop(tmp_path)
        _edit_json(interop / "vectors" / INDEX["ip-ban"]["file"], lambda v: v.update(kind="x"))
        assert _items(run(interop)) == [INDEX["ip-ban"]["file"]]

    def test_an_event_of_a_vector_that_does_not_validate(self, tmp_path: Path) -> None:
        interop = _copy_interop(tmp_path)
        _edit_json(
            interop / "vectors" / INDEX["event-redelivery"]["file"],
            lambda v: v["events"]["pedido-pagado"].update(sequence=-1),
        )
        assert _items(run(interop)) == ["event-redelivery/*"]

    def test_a_broken_schema_is_red_not_a_traceback(self, tmp_path: Path) -> None:
        interop = _copy_interop(tmp_path)
        _edit_json(interop / "schemas/manifest.schema.json", lambda s: s.update(type=5))
        report = run(interop)
        assert not report.green
        assert report.failures[0].section == "schemas"

    def test_a_manifest_that_breaks_a_runner_rule(self, tmp_path: Path) -> None:
        manifest = json.loads((FIXTURES / "uvd-stack/valid/completo.json").read_text("utf-8"))
        good = tmp_path / "good.json"
        good.write_text(json.dumps(manifest), encoding="utf-8")
        manifest["endpoints"]["api"]["erc8128"]["authorities"] = ["api.otra-app.example"]
        bad = tmp_path / "bad.json"
        bad.write_text(json.dumps(manifest), encoding="utf-8")
        assert run(INTEROP, [good]).green
        report = run(INTEROP, [bad])
        assert _items(report) == [str(bad)]
        assert report.failures[0].detail.startswith(
            "R3.3 at /endpoints/api/erc8128/authorities/0: 'api.otra-app.example'"
        )

    def test_a_manifest_that_breaks_its_schema(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.json"
        bad.write_text(json.dumps({"schema": "uvd.stack/1"}), encoding="utf-8")
        report = run(INTEROP, [bad])
        assert not report.green
        assert all(f.detail.startswith("schema: required at / (missing ") for f in report.failures)

    def test_a_manifest_that_is_not_json(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.json"
        bad.write_text("<html></html>", encoding="utf-8")
        assert _items(run(INTEROP, [bad])) == [str(bad)]

    def test_an_envelope_in_an_ip_ban_body_that_does_not_validate(self, tmp_path: Path) -> None:
        # The case keeps its verdict (not a ban); what breaks is its example
        # envelope, and a vector must not carry an invalid uvd_error.
        interop = _copy_interop(tmp_path)

        def break_envelope(vector: Any) -> None:
            case = next(c for c in vector["cases"] if c["id"] == "bloqueo-con-uvd-error-al-lado")
            body = json.loads(case["response"]["body"])
            body["uvd_error"]["spent"] = "sometimes"
            case["response"]["body"] = json.dumps(body)

        _edit_json(interop / "vectors" / INDEX["ip-ban"]["file"], break_envelope)
        report = run(interop)
        assert _items(report) == ["ip-ban/bloqueo-con-uvd-error-al-lado"]
        assert "uvd_error" in report.failures[0].detail

    def test_a_valid_manifest_fixture_that_breaks_a_runner_rule(self, tmp_path: Path) -> None:
        # What the schema suite let through: en-migracion.json without its mcp
        # endpoint accepted an authority that no endpoint of it serves.
        interop = _copy_interop(tmp_path)
        _edit_json(
            interop / "fixtures/uvd-stack/valid/en-migracion.json",
            lambda d: d["endpoints"].pop("mcp"),
        )
        report = run(interop)
        assert _items(report) == ["uvd-stack/valid/en-migracion.json"]
        assert "R3.3" in report.failures[0].detail

    def test_a_case_citing_a_rule_its_index_entry_does_not_list(self, tmp_path: Path) -> None:
        interop = _copy_interop(tmp_path)
        _edit_json(
            interop / "vectors" / INDEX["ip-ban"]["file"],
            lambda v: v["cases"][0].update(rule="R3.1"),
        )
        assert _items(run(interop)) == ["ip-ban/bloqueo"]

    def test_an_index_rule_no_case_fixes(self, tmp_path: Path) -> None:
        interop = _copy_interop(tmp_path)
        _edit_json(
            interop / "vectors" / "index.json",
            lambda i: i["vectors"]["ip-ban"]["rules"].append("R6.2"),
        )
        report = run(interop)
        assert _items(report) == ["ip-ban/*"]
        assert "R6.2" in report.failures[0].detail

    def test_a_vector_without_cases(self, tmp_path: Path) -> None:
        interop = _copy_interop(tmp_path)
        _edit_json(interop / "vectors" / INDEX["ip-ban"]["file"], lambda v: v.update(cases=[]))
        report = run(interop)
        assert set(_items(report)) == {"ip-ban/*"}
        assert any("without cases" in f.detail for f in report.failures)

    def test_ids_that_cannot_be_keys_are_red_not_a_traceback(self, tmp_path: Path) -> None:
        interop = _copy_interop(tmp_path)
        _edit_json(
            interop / "vectors" / INDEX["ip-ban"]["file"],
            lambda v: v["cases"][0].update(id=["x"]),
        )
        _edit_json(interop / "fixtures/cases.json", lambda i: i["cases"][0].update(file=["x"]))
        report = run(interop)
        assert sorted(_items(report)) == [
            "['x']",
            "ip-ban/['x']",
            "uvd-stack/valid/completo.json",
        ]


class TestOffline:
    #: A schema whose only content is a reference to another document.
    REMOTE = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$ref": "https://example.invalid/remote.schema.json",
    }

    @pytest.fixture
    def fetches(self, monkeypatch: pytest.MonkeyPatch) -> list[Any]:
        calls: list[Any] = []

        def urlopen(*args: Any, **kwargs: Any) -> Any:
            calls.append(args)
            raise OSError("no network in these tests")

        monkeypatch.setattr(urllib.request, "urlopen", urlopen)
        return calls

    def test_stock_jsonschema_would_fetch_a_remote_ref(self, fetches: list[Any]) -> None:
        # The other state: without the runner's empty registry, the reference
        # goes out over HTTP (here to the stub).
        with pytest.raises(Exception):
            list(Draft202012Validator(self.REMOTE).iter_errors({}))
        assert fetches

    def test_the_runner_never_fetches_one(self, fetches: list[Any]) -> None:
        with pytest.raises(Exception):
            list(conformance.schema_validator(self.REMOTE).iter_errors({}))
        assert fetches == []


def _set(case: dict[str, Any], path: list[Any], value: Any) -> dict[str, Any]:
    changed = copy.deepcopy(case)
    target = changed
    for step in path[:-1]:
        target = target[step]
    target[path[-1]] = value
    return changed


def _flips(kind: str, case: dict[str, Any]) -> Iterator[tuple[str, dict[str, Any]]]:
    """Every expectation of one case, flipped one at a time."""
    if kind == "ip-ban":
        yield "ip_ban", _set(case, ["expect", "ip_ban"], not case["expect"]["ip_ban"])
    elif kind == "request-signing":
        for name in ("must_sign", "conforms"):
            yield name, _set(case, ["expect", name], not case["expect"][name])
    elif kind == "event-redelivery":
        for number, attempt in enumerate(case["attempts"]):
            other = "already_processed" if attempt["expect"] == "accepted" else "accepted"
            yield f"attempt-{number + 1}", _set(case, ["attempts", number, "expect"], other)
        yield "sender_conforms", _set(case, ["sender_conforms"], not case["sender_conforms"])
    elif kind == "manifest-rules":
        findings = case["expect"]["findings"]
        other = findings[1:] if findings else [{"rule": "R3.3", "instance_path": "/app"}]
        yield "findings", _set(case, ["expect", "findings"], other)
    else:  # pragma: no cover - a new kind needs its flips here
        raise AssertionError(f"no flips for {kind}")


def _all_flips() -> list[tuple[str, str, str]]:
    return [
        (kind, case["id"], label)
        for kind in sorted(INDEX)
        for case in _vector(kind)[_cases_key(kind)]
        for label, _ in _flips(kind, case)
    ]


class TestEveryCaseIsLive:
    @pytest.mark.parametrize(
        "kind,case_id,label", _all_flips(), ids=[":".join(f) for f in _all_flips()]
    )
    def test_flipping_an_expectation_turns_the_runner_red(
        self, kind: str, case_id: str, label: str
    ) -> None:
        vector = _vector(kind)
        cases = vector[_cases_key(kind)]
        index = next(i for i, case in enumerate(cases) if case["id"] == case_id)
        cases[index] = dict(_flips(kind, cases[index]))[label]
        _, failures = evaluate_vector(vector, VALIDATORS)
        assert [case for case, _ in failures] == [case_id]


# --- plausible wrong implementations -------------------------------------------


Headers = Mapping[str, str]


def _json_object(body: str) -> dict[str, Any] | None:
    try:
        parsed = json.loads(body)
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _ban_any_403_with_error(profile: Any, status: int, headers: Headers, body: str) -> bool:
    parsed = _json_object(body)
    return profile == "L9" and status == 403 and parsed is not None and "error" in parsed


def _ban_only_error_any_status(profile: Any, status: int, headers: Headers, body: str) -> bool:
    parsed = _json_object(body)
    return profile == "L9" and parsed is not None and set(parsed) == {"error"}


def _ban_error_anywhere(profile: Any, status: int, headers: Headers, body: str) -> bool:
    return profile == "L9" and status == 403 and '"error"' in body


def _ban_parses_blindly(profile: Any, status: int, headers: Headers, body: str) -> bool:
    return profile == "L9" and status == 403 and set(json.loads(body)) == {"error"}


def _ban_any_json_with_only_error(profile: Any, status: int, headers: Headers, body: str) -> bool:
    # No check that the body is an object: set(["error"]) is {"error"} too.
    try:
        return profile == "L9" and status == 403 and set(json.loads(body)) == {"error"}
    except (ValueError, TypeError):
        return False


def _ban_on_403_or_429(profile: Any, status: int, headers: Headers, body: str) -> bool:
    parsed = _json_object(body)
    return (
        profile == "L9" and status in (403, 429) and parsed is not None and set(parsed) == {"error"}
    )


def _ban_ignores_the_profile(profile: Any, status: int, headers: Headers, body: str) -> bool:
    return conformance.is_ip_ban("L9", status, headers, body)


def _ban_error_must_be_text(profile: Any, status: int, headers: Headers, body: str) -> bool:
    if not conformance.is_ip_ban(profile, status, headers, body):
        return False
    parsed = _json_object(body)
    return parsed is not None and isinstance(parsed["error"], str)


def _ban_needs_a_json_content_type(profile: Any, status: int, headers: Headers, body: str) -> bool:
    kind = next((v for k, v in headers.items() if k.lower() == "content-type"), "")
    return conformance.is_ip_ban(profile, status, headers, body) and kind.startswith(
        "application/json"
    )


def _conforms_with(
    *,
    names: Callable[[Mapping[str, str]], set],
    complete: Callable[[set], bool],
    decide: Callable[..., bool] = conformance.must_sign,
) -> Callable[..., bool]:
    def conforms(
        method: str, headers: Mapping[str, str], needs_identity: bool, operation: Any = None
    ) -> bool:
        present = SIGNATURE_HEADERS & names(headers)
        if decide(method, needs_identity, operation):
            return complete(present)
        return not present

    return conforms


def _by_method(method: str, needs_identity: bool, operation: Any = None) -> bool:
    # Ignores the stated operation: every POST is a write, an MCP read too.
    return method in conformance.WRITE_METHODS or needs_identity


def _by(writes: Callable[[str], bool]) -> Callable[..., bool]:
    """A decision that follows a stated operation and gets the method wrong."""

    def decide(method: str, needs_identity: bool, operation: Any = None) -> bool:
        return (operation == "write" if operation else writes(method)) or needs_identity

    return decide


def _lower(headers: Mapping[str, str]) -> set:
    return {name.lower() for name in headers}


class _Inbox:
    """The reference inbox with one knob turned."""

    def __init__(
        self,
        *,
        consume_before_verify: bool = False,
        duplicate: tuple[int, str] = (200, "already_processed"),
        dedupe_by_source: bool = True,
        nonce_key_has_wallet: bool = True,
        nonce_key_has_chain: bool = True,
        dedupe_before_nonce: bool = False,
        sequence_before_dedupe: bool = False,
    ) -> None:
        self.consume_before_verify = consume_before_verify
        self.duplicate = duplicate
        self.dedupe_by_source = dedupe_by_source
        self.nonce_key_has_wallet = nonce_key_has_wallet
        self.nonce_key_has_chain = nonce_key_has_chain
        self.dedupe_before_nonce = dedupe_before_nonce
        self.sequence_before_dedupe = sequence_before_dedupe
        self.nonces: set = set()
        self.events: set = set()
        self.last_sequence: dict[Any, int] = {}

    def _duplicate(self) -> tuple[int, dict[str, Any]]:
        status, word = self.duplicate
        return status, ({"uvd_error": {"code": word}} if status >= 400 else {"status": word})

    def deliver(
        self, event: Mapping[str, Any], keyid: str, nonce: str, signature_valid: bool
    ) -> tuple[int, dict[str, Any]]:
        _, chain_id, wallet = keyid.split(":")
        seen = (
            (chain_id if self.nonce_key_has_chain else None),
            (wallet.lower() if self.nonce_key_has_wallet else None),
            nonce,
        )
        dedupe = (
            (event["source"], event["event_id"]) if self.dedupe_by_source else event["event_id"]
        )
        subject = (event["source"], event["subject"]["kind"], event["subject"]["id"])
        if self.consume_before_verify:
            if seen in self.nonces:
                return 409, {"uvd_error": {"code": "nonce_replayed"}}
            self.nonces.add(seen)
        if not signature_valid:
            return 401, {"uvd_error": {"code": "signature_invalid"}}
        if self.dedupe_before_nonce and dedupe in self.events:
            return self._duplicate()
        if not self.consume_before_verify:
            if seen in self.nonces:
                return 409, {"uvd_error": {"code": "nonce_replayed"}}
            self.nonces.add(seen)
        if self.sequence_before_dedupe and event["sequence"] <= self.last_sequence.get(subject, -1):
            return 200, {"status": "stale_sequence"}
        if dedupe in self.events:
            return self._duplicate()
        self.events.add(dedupe)
        self.last_sequence[subject] = event["sequence"]
        return 202, {"status": "accepted"}


def _sender_with(
    *,
    repeats: bool = True,
    equality: bool = True,
    hyphenless: bool = True,
    only_sender: bool = True,
    per_signer: bool = True,
    lower_case: bool = True,
) -> Any:
    def conforms(attempts: Any, events: Any) -> bool:
        used: dict[Any, set] = {}
        for attempt in attempts:
            if only_sender and attempt["by"] != "sender":
                continue
            event = events[attempt["event"]]
            nonce = attempt["nonce"].lower() if lower_case else attempt["nonce"]
            key = (
                attempt["keyid"].split(":")[2].lower()
                if per_signer
                else (event["source"], event["event_id"])
            )
            seen = used.setdefault(key, set())
            if repeats and attempt["nonce"] in seen:
                return False
            if equality and nonce == event["event_id"]:
                return False
            if hyphenless and nonce == event["event_id"].replace("-", ""):
                return False
            seen.add(attempt["nonce"])
        return True

    return conforms


def _calendar_of_the_stdlib(timestamp: str) -> bool:
    # datetime.date starts at year 1: it refuses 0000, which RFC 3339 admits.
    try:
        datetime.date(int(timestamp[0:4]), int(timestamp[5:7]), int(timestamp[8:10]))
    except ValueError:
        return False
    return True


def _every_fourth_year(timestamp: str) -> bool:
    year, month, day = int(timestamp[0:4]), int(timestamp[5:7]), int(timestamp[8:10])
    days = [31, 29 if year % 4 == 0 else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
    return day <= days[month - 1]


def _full_datetime(timestamp: str) -> bool:
    try:
        datetime.datetime.strptime(timestamp[:19], "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return False
    return True


def _manifest_with(
    *,
    authority_of: Callable[[str], str] = conformance.url_authority,
    first_segment: Callable[[str, str], bool] = lambda t, app: t.split(".", 1)[0] == app,
    date_ok: Callable[[str], bool] = conformance.date_exists,
    own_endpoint_only: bool = False,
) -> Callable[[Mapping[str, Any]], list[dict[str, str]]]:
    def findings(manifest: Mapping[str, Any]) -> list[dict[str, str]]:
        out = []
        for index, event_type in enumerate(manifest["events"]["emits"]):
            if not first_segment(event_type, manifest["app"]):
                out.append({"rule": "R1.10", "instance_path": f"/events/emits/{index}"})
        endpoints = manifest["endpoints"]
        every = {authority_of(e["url"]) for e in endpoints.values()}
        for name, endpoint in endpoints.items():
            allowed = {authority_of(endpoint["url"])} if own_endpoint_only else every
            for index, authority in enumerate(endpoint.get("erc8128", {}).get("authorities", [])):
                if authority not in allowed:
                    out.append(
                        {
                            "rule": "R3.3",
                            "instance_path": f"/endpoints/{name}/erc8128/authorities/{index}",
                        }
                    )
        stamp = manifest.get("generated_at")
        if stamp is not None and not date_ok(stamp):
            out.append({"rule": "R5.5", "instance_path": "/generated_at"})
        return out

    return findings


def _title_case_only(headers: Mapping[str, str]) -> set:
    # ``"Signature" in headers`` on a plain dict: blind to ``signature``.
    return {name.lower() for name in headers if name in ("Signature", "Signature-Input")}


def _wrong(kind: str, mistake: str, **fields: Any) -> tuple[str, str, Implementation]:
    return kind, mistake, replace(REFERENCE, **fields)


def _wrong_manifest(mistake: str, **knobs: Any) -> tuple[str, str, Implementation]:
    return _wrong("manifest-rules", mistake, manifest_rule_findings=_manifest_with(**knobs))


#: (kind, the mistake, the implementation that makes it).
WRONG: list[tuple[str, str, Implementation]] = [
    _wrong("ip-ban", "any-403-with-error-at-the-root", is_ip_ban=_ban_any_403_with_error),
    _wrong("ip-ban", "only-error-whatever-the-status", is_ip_ban=_ban_only_error_any_status),
    _wrong("ip-ban", "error-anywhere-in-the-text", is_ip_ban=_ban_error_anywhere),
    _wrong("ip-ban", "raises-on-a-body-that-is-not-json", is_ip_ban=_ban_parses_blindly),
    _wrong("ip-ban", "every-api-speaks-l9", is_ip_ban=_ban_ignores_the_profile),
    _wrong("ip-ban", "error-must-be-text", is_ip_ban=_ban_error_must_be_text),
    _wrong("ip-ban", "needs-a-json-content-type", is_ip_ban=_ban_needs_a_json_content_type),
    _wrong("ip-ban", "ban-en-403-o-429", is_ip_ban=_ban_on_403_or_429),
    _wrong("ip-ban", "any-json-with-only-error", is_ip_ban=_ban_any_json_with_only_error),
    _wrong("request-signing", "never-sign", must_sign=lambda m, n, op=None: False),
    _wrong("request-signing", "always-sign", must_sign=lambda m, n, op=None: True),
    _wrong("request-signing", "writes-are-only-post", must_sign=_by(lambda m: m == "POST")),
    _wrong("request-signing", "a-read-is-only-get", must_sign=_by(lambda m: m != "GET")),
    _wrong(
        "request-signing",
        "reads-are-never-signed",
        must_sign=lambda m, n, op=None: conformance.operation_of(m, op) == "write",
    ),
    _wrong(
        "request-signing",
        "operation-ignored-decides-by-method",
        must_sign=_by_method,
        signing_conforms=_conforms_with(
            names=_lower, complete=lambda p: p == SIGNATURE_HEADERS, decide=_by_method
        ),
    ),
    _wrong(
        "request-signing",
        "a-stated-operation-is-a-read",
        must_sign=lambda m, n, op=None: (op is None and m in conformance.WRITE_METHODS) or n,
    ),
    _wrong(
        "request-signing",
        "signature-input-basta",
        signing_conforms=_conforms_with(names=_lower, complete=lambda p: "signature-input" in p),
    ),
    _wrong(
        "request-signing",
        "header-names-with-case",
        signing_conforms=_conforms_with(
            names=_title_case_only, complete=lambda p: p == SIGNATURE_HEADERS
        ),
    ),
    _wrong(
        "request-signing",
        "one-header-is-a-full-signature",
        signing_conforms=_conforms_with(names=_lower, complete=bool),
    ),
    _wrong(
        "event-redelivery",
        "nonce-consumed-before-the-signature",
        event_inbox=lambda: _Inbox(consume_before_verify=True),
    ),
    _wrong(
        "event-redelivery",
        "a-duplicate-is-answered-409",
        event_inbox=lambda: _Inbox(duplicate=(409, "already_processed")),
    ),
    _wrong(
        "event-redelivery",
        "dedupe-by-event-id-alone",
        event_inbox=lambda: _Inbox(dedupe_by_source=False),
    ),
    _wrong(
        "event-redelivery",
        "nonce-keyed-without-the-wallet",
        event_inbox=lambda: _Inbox(nonce_key_has_wallet=False),
    ),
    _wrong(
        "event-redelivery",
        "nonce-keyed-without-the-chain",
        event_inbox=lambda: _Inbox(nonce_key_has_chain=False),
    ),
    _wrong(
        "event-redelivery",
        "dedupe-before-the-nonce",
        event_inbox=lambda: _Inbox(dedupe_before_nonce=True),
    ),
    _wrong(
        "event-redelivery",
        "stale-sequence-before-dedupe",
        event_inbox=lambda: _Inbox(sequence_before_dedupe=True),
    ),
    _wrong(
        "event-redelivery",
        "sender-may-repeat-a-nonce",
        sender_nonces_conform=_sender_with(repeats=False),
    ),
    _wrong(
        "event-redelivery",
        "sender-may-use-the-event-id",
        sender_nonces_conform=_sender_with(equality=False),
    ),
    _wrong(
        "event-redelivery",
        "sender-may-use-the-event-id-without-hyphens",
        sender_nonces_conform=_sender_with(hyphenless=False),
    ),
    _wrong(
        "event-redelivery",
        "third-party-counted-as-sender",
        sender_nonces_conform=_sender_with(only_sender=False),
    ),
    _wrong(
        "event-redelivery",
        "sender-checked-per-event",
        sender_nonces_conform=_sender_with(per_signer=False),
    ),
    _wrong(
        "event-redelivery",
        "sender-compares-with-case",
        sender_nonces_conform=_sender_with(lower_case=False),
    ),
    _wrong_manifest("authority-only-of-its-own-endpoint", own_endpoint_only=True),
    _wrong_manifest("port-443-kept", authority_of=lambda u: u.split("/")[2].lower()),
    _wrong_manifest(
        "host-compared-with-case",
        authority_of=lambda u: urlsplit(u).netloc.removesuffix(":443"),
    ),
    _wrong_manifest(
        "port-read-as-text",
        authority_of=lambda u: urlsplit(u).netloc.lower().removesuffix(":443"),
    ),
    _wrong_manifest("app-as-a-text-prefix", first_segment=lambda t, app: t.startswith(app)),
    _wrong_manifest("no-date-check", date_ok=lambda s: True),
    _wrong_manifest("leap-year-every-fourth-year", date_ok=_every_fourth_year),
    _wrong_manifest("year-zero-refused", date_ok=_calendar_of_the_stdlib),
    _wrong_manifest("time-parsed-too", date_ok=_full_datetime),
]


class TestWrongImplementations:
    def test_the_harness_with_no_knob_turned_is_green(self) -> None:
        # So that red below means the mistake, not the harness.
        plain = replace(
            REFERENCE,
            signing_conforms=_conforms_with(
                names=_lower, complete=lambda p: p == SIGNATURE_HEADERS
            ),
            event_inbox=_Inbox,
            sender_nonces_conform=_sender_with(),
            manifest_rule_findings=_manifest_with(),
        )
        for kind in INDEX:
            assert _failures(kind, plain) == [], kind

    @pytest.mark.parametrize(
        "kind,mistake,implementation", WRONG, ids=[f"{k}:{m}" for k, m, _ in WRONG]
    )
    def test_the_mistake_turns_its_vector_red(
        self, kind: str, mistake: str, implementation: Implementation
    ) -> None:
        assert _failures(kind, implementation), f"{kind} does not catch {mistake}"


class TestCommandLine:
    def test_green(self, capsys: pytest.CaptureFixture[str]) -> None:
        manifest = str(FIXTURES / "uvd-stack/valid/completo.json")
        assert main(["check", "--interop", str(INTEROP), manifest]) == 0
        out = capsys.readouterr().out
        assert out.rstrip().endswith("GREEN")
        assert f"manifest {manifest}: ok" in out

    def test_red(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        manifest = json.loads((FIXTURES / "uvd-stack/valid/completo.json").read_text("utf-8"))
        manifest["generated_at"] = "2026-02-30T00:00:00Z"
        bad = tmp_path / "uvd-stack.json"
        bad.write_text(json.dumps(manifest), encoding="utf-8")
        assert main(["check", "--interop", str(INTEROP), str(bad)]) == 1
        out = capsys.readouterr().out
        assert "R5.5 at /generated_at: '2026-02-30T00:00:00Z' is not a date that exists" in out
        assert out.rstrip().endswith("RED: 1 failures")

    def test_the_default_interop_dir_is_the_source_checkout(self) -> None:
        assert conformance.default_interop_dir() == INTEROP
        assert main(["check"]) == 0

    def test_no_interop_dir_cannot_run(self, tmp_path: Path) -> None:
        assert main(["check", "--interop", str(tmp_path / "nope")]) == 2

    def test_a_directory_that_is_not_interop_cannot_run(self) -> None:
        # The repo root exists but holds no fixtures/cases.json: 2, not red.
        assert main(["check", "--interop", str(ROOT)]) == 2

    def test_a_missing_manifest_cannot_run(self, tmp_path: Path) -> None:
        assert main(["check", "--interop", str(INTEROP), str(tmp_path / "nope.json")]) == 2

    def test_without_jsonschema_it_cannot_run(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # None in sys.modules makes ``import jsonschema`` raise ImportError.
        monkeypatch.setitem(sys.modules, "jsonschema", None)
        assert main(["check", "--interop", str(INTEROP)]) == 2
        assert "pip install 'uvd-x402-sdk[interop]'" in capsys.readouterr().err

    def test_a_failure_of_the_runner_is_not_a_red_verdict(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        def broken(*args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("a bug of the runner")

        monkeypatch.setattr(interop_main, "run", broken)
        assert main(["check", "--interop", str(INTEROP)]) == 2
        assert "the runner failed" in capsys.readouterr().err

    def test_python_dash_m_green_then_red(self, tmp_path: Path) -> None:
        command = [sys.executable, "-m", "uvd_x402_sdk.interop", "check", "--interop"]
        green = subprocess.run(command + [str(INTEROP)], capture_output=True, text=True)
        assert green.returncode == 0, green.stdout + green.stderr
        interop = _copy_interop(tmp_path)
        _edit_json(interop / "fixtures/uvd-stack/valid/minimo.json", lambda d: d.pop("app"))
        red = subprocess.run(command + [str(interop)], capture_output=True, text=True)
        assert red.returncode == 1, red.stdout + red.stderr
        assert "FAIL fixtures uvd-stack/valid/minimo.json" in red.stdout

    def test_the_console_script_points_at_main(self) -> None:
        # The installed wheel was checked by hand (see the PR); here, that the
        # declaration names a callable that exists.
        pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        assert re.search(
            r'^uvd-interop = "uvd_x402_sdk\.interop\.__main__:main"$', pyproject, re.MULTILINE
        )
        assert callable(interop_main.main)


def _jobs(workflow: str) -> dict[str, str]:
    """The text of each job of a workflow, by its name (two-space indent)."""
    body = workflow.split("\njobs:\n", 1)[1]
    parts = re.split(r"^  ([a-z][a-z0-9_-]*):\n", body, flags=re.MULTILINE)
    return dict(zip(parts[1::2], parts[2::2]))


class TestPublishGate:
    WORKFLOW = (ROOT / ".github" / "workflows" / "publish.yml").read_text(encoding="utf-8")

    def test_the_interop_tests_gate_the_build(self) -> None:
        jobs = _jobs(self.WORKFLOW)
        assert "run: python -m pytest -q tests/interop" in jobs["interop"]
        assert "needs: interop" in jobs["publish"]
        assert "run: python -m build" in jobs["publish"]
        assert "pytest" not in jobs["publish"]

    def test_the_tests_never_share_a_job_with_the_upload(self) -> None:
        # Unpinned test dependencies run with read access only, away from the
        # token and the id-token permission of the upload.
        interop = _jobs(self.WORKFLOW)["interop"]
        assert "id-token" not in interop
        assert "secrets." not in interop
        assert re.search(r"permissions:\n\s+contents: read\n\s+steps:", interop)

    def test_it_runs_on_tags_and_by_hand_only(self) -> None:
        # A branch push must not run it: publishing is by tag (and the Actions
        # budget is spent only where a deploy comes from GitHub).
        triggers = self.WORKFLOW.split("\non:\n", 1)[1].split("\njobs:\n", 1)[0]
        lines = [line.strip() for line in triggers.splitlines() if line.strip()]
        assert lines == ["workflow_dispatch:", "push:", "tags:", "- 'v*'"]

    def test_the_folder_it_runs_holds_the_schema_suite_too(self) -> None:
        here = Path(__file__).resolve().parent
        assert (here / "test_schemas.py").is_file()
        assert (here / "test_conformance.py").is_file()

    def test_the_interop_extra_brings_jsonschema(self) -> None:
        pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        extra = re.search(r"^interop = \[(.*?)\]", pyproject, re.MULTILINE | re.DOTALL)
        assert extra is not None
        assert '"jsonschema>=4.18.0"' in extra.group(1)
