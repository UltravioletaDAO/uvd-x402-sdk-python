"""Interop v1: the JSON Schemas in ``interop/schemas`` against ``interop/fixtures``.

NO SKIPS. ``jsonschema`` is a ``dev`` dependency: without it this module is a
collection error, never a silent green.

The spec is language-independent (Markdown + JSON Schema + fixtures) and gets
vendored by the TypeScript SDK and a Rust crate, so the expectations live in
DATA (``interop/fixtures/cases.json``), not in this file, and the comparison
is the portable one the spec's README writes down: errors from keywords that
only wrap another (``if``, ``then``, ``else``, ``allOf``, ``anyOf``,
``oneOf``, ``$ref``, ``propertyNames``) are dropped, and what remains must be
exactly the set the case lists. ``pattern`` runs with ECMA-262 semantics, the
ones JSON Schema defines and Ajv and Rust's ``regex`` apply. What each block
catches:

* ``TestSchemas`` -- a schema that is not valid draft 2020-12, or whose
  ``$id`` moved. Vendors pin the ``$id``; moving it is a breaking change.
* ``TestPortablePatterns`` -- a regular expression outside the subset every
  engine reads the same way.
* ``TestCaseIndex`` -- a fixture on disk that no case lists (it would never
  run), a case whose file is gone, or a schema with no valid or no invalid
  fixture.
* ``TestFixtures`` -- a valid fixture that stopped validating; an invalid one
  that validates; or an invalid one that fails for a reason OTHER than the rule
  it claims. Each invalid case names the exact errors it must produce (keyword,
  instance path and, for ``required``, the missing property), so a fixture that
  breaks two rules cannot hide a third. Also: stock Python ``jsonschema`` (whose
  ``$`` accepts a final newline) still rejects every invalid fixture.
* ``TestMutations`` -- a constraint that no fixture guards. Every ``required``
  entry, ``pattern``, ``const``, ``enum``, ``not``, ``then``/``else`` and the
  rest is removed from a copy of the schema, one at a time, and the fixture
  suite must turn red. "Delete a ``required`` and a test goes red" is checked
  here for every ``required`` in the three schemas, not once by hand.
"""

from __future__ import annotations

import copy
import json
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator, ValidationError, validators

INTEROP = Path(__file__).resolve().parents[1] / "interop"
SCHEMAS = INTEROP / "schemas"
FIXTURES = INTEROP / "fixtures"

#: The stable identifiers. A vendored copy (TypeScript, Rust) resolves these.
SCHEMA_IDS = {
    "manifest": "https://ultravioletadao.xyz/interop/v1/manifest.schema.json",
    "uvd-event-1": "https://ultravioletadao.xyz/interop/v1/uvd-event-1.schema.json",
    "uvd-error": "https://ultravioletadao.xyz/interop/v1/uvd-error.schema.json",
}

DRAFT_2020_12 = "https://json-schema.org/draft/2020-12/schema"

#: Keywords that only wrap the verdict of another. Validators differ on whether
#: they report them (Ajv reports a failed ``if``; jsonschema reports the inner
#: keyword), so the portable comparison drops them.
WRAPPERS = frozenset({"if", "then", "else", "allOf", "anyOf", "oneOf", "$ref", "propertyNames"})

#: Keywords whose removal must be caught by some fixture. ``type``, ``$ref``,
#: ``properties``, ``items``, ``allOf`` and ``if`` are structure: their
#: content is mutated keyword by keyword instead. The list is wider than what
#: the schemas use today on purpose: a constraint added later without a
#: fixture turns this suite red. ``format`` is here too: in draft 2020-12 it is
#: an annotation by default, so a schema that relies on it enforces nothing,
#: and its mutation survives.
MUTABLE_KEYWORDS = (
    "required",
    "dependentRequired",
    "additionalProperties",
    "patternProperties",
    "propertyNames",
    "pattern",
    "format",
    "const",
    "enum",
    "not",
    "then",
    "else",
    "contains",
    "minContains",
    "maxContains",
    "minimum",
    "maximum",
    "exclusiveMinimum",
    "exclusiveMaximum",
    "multipleOf",
    "minItems",
    "maxItems",
    "minLength",
    "maxLength",
    "minProperties",
    "maxProperties",
    "uniqueItems",
)

#: Annotation-only keywords, never constraints.
ANNOTATIONS = {"title", "description", "$comment", "examples", "$schema", "$id"}

#: "## R6.3 · ..." / "### R5.1 · ..." -- where a rule is written.
RULE_HEADING = re.compile(r"^#{2,3} (R[0-9]+[.][0-9]+) ", re.MULTILINE)
#: "R6.3" anywhere in a schema's descriptions and comments.
RULE_CITATION = re.compile(r"\bR[0-9]+[.][0-9]+\b")


def _ecma_anchors(pattern: str) -> str:
    """ECMA-262 reads ``$`` (no ``m`` flag) as the end of the input; Python's
    ``re`` also lets it match just before a final newline, so
    ``"0x" + 40 hex + "\\n"`` passes ``^0x[0-9a-f]{40}$`` in stock
    ``jsonschema`` and fails in Ajv. Each ``$`` anchor outside a character
    class becomes ``\\Z``, which is ECMA's ``$`` in Python.
    """
    out: list[str] = []
    in_class = escaped = False
    for char in pattern:
        if escaped:
            escaped = False
        elif char == "\\":
            escaped = True
        elif in_class:
            in_class = char != "]"
        elif char == "[":
            in_class = True
        elif char == "$":
            out.append(r"\Z")
            continue
        out.append(char)
    return "".join(out)


def _ecma_pattern(
    validator: Any, pattern: str, instance: Any, schema: Any
) -> Iterator[ValidationError]:
    if validator.is_type(instance, "string") and not re.search(_ecma_anchors(pattern), instance):
        yield ValidationError(f"{instance!r} does not match {pattern!r}")


#: Draft 2020-12 with the ``pattern`` keyword as JSON Schema defines it.
ECMA_VALIDATOR: Any = validators.extend(Draft202012Validator, {"pattern": _ecma_pattern})


def _load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _schema(name: str) -> dict[str, Any]:
    loaded: dict[str, Any] = _load(SCHEMAS / f"{name}.schema.json")
    return loaded


INDEX: dict[str, Any] = _load(FIXTURES / "cases.json")
CASES: list[dict[str, Any]] = INDEX["cases"]
#: Schema name -> {"schema": path under interop/, "fixtures": folder}.
LAYOUT: dict[str, dict[str, str]] = INDEX["schemas"]


def _pointer(path: Any) -> str:
    """JSON Pointer (RFC 6901) of an instance location."""
    parts = [str(p).replace("~", "~0").replace("/", "~1") for p in path]
    return "".join("/" + p for p in parts)


def _describe(error: Any) -> dict[str, Any]:
    """The portable facts of one validation error: keyword, where, and for
    ``required`` which property is missing.
    """
    found: dict[str, Any] = {
        "keyword": error.validator,
        "instance_path": _pointer(error.absolute_path),
    }
    if error.validator == "required":
        missing = [p for p in error.validator_value if p not in error.instance]
        # jsonschema yields ONE error per missing property, and names it.
        found["property"] = next(p for p in missing if repr(p) in error.message)
    return found


def _key(item: dict[str, Any]) -> tuple[str, str, str]:
    return (item["keyword"], item["instance_path"], item.get("property", ""))


def _suite_failures(schema_name: str, schema: dict[str, Any]) -> list[str]:
    """Run every case of one schema against ``schema``; return what went wrong."""
    validator = ECMA_VALIDATOR(schema)
    failures: list[str] = []
    for case in CASES:
        if case["schema"] != schema_name:
            continue
        document = _load(FIXTURES / case["file"])
        errors = [
            _describe(e) for e in validator.iter_errors(document) if e.validator not in WRAPPERS
        ]
        if case["valid"]:
            if errors:
                failures.append(f"{case['file']}: expected valid, got {errors}")
            continue
        got = sorted({_key(e) for e in errors})
        want = sorted({_key(e) for e in case["errors"]})
        if got != want:
            failures.append(f"{case['file']}: expected {want}, got {got}")
    return failures


def _locations(node: Any, path: tuple[Any, ...] = ()) -> Iterator[tuple[Any, ...]]:
    """Every (path to a schema object, keyword) worth mutating."""
    if isinstance(node, dict):
        for key, value in node.items():
            if key in ANNOTATIONS:
                continue
            if key in MUTABLE_KEYWORDS:
                if key == "required":
                    for index in range(len(value)):
                        yield path + (key, index)
                else:
                    yield path + (key,)
            yield from _locations(value, path + (key,))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from _locations(value, path + (index,))


def _mutate(schema: dict[str, Any], location: tuple[Any, ...]) -> dict[str, Any]:
    mutated = copy.deepcopy(schema)
    if location[-2:-1] == ("required",) and isinstance(location[-1], int):
        parent = mutated
        for step in location[:-2]:
            parent = parent[step]
        required = parent["required"]
        del required[location[-1]]
        if not required:
            del parent["required"]
        return mutated
    parent = mutated
    for step in location[:-1]:
        parent = parent[step]
    del parent[location[-1]]
    return mutated


def _label(location: tuple[Any, ...]) -> str:
    return "/".join(str(step) for step in location)


def _walk(node: Any) -> Iterator[dict[str, Any]]:
    if isinstance(node, dict):
        yield node
        for key, value in node.items():
            if key not in ANNOTATIONS:
                yield from _walk(value)
    elif isinstance(node, list):
        for value in node:
            yield from _walk(value)


def _regexes(node: Any) -> Iterator[str]:
    """Every regular expression a schema carries: ``pattern`` values and
    ``patternProperties`` keys.
    """
    for obj in _walk(node):
        if isinstance(obj.get("pattern"), str):
            yield obj["pattern"]
        if isinstance(obj.get("patternProperties"), dict):
            yield from obj["patternProperties"]


class TestSchemas:
    @pytest.mark.parametrize("name", sorted(SCHEMA_IDS))
    def test_is_valid_draft_2020_12(self, name: str) -> None:
        schema = _schema(name)
        assert schema["$schema"] == DRAFT_2020_12
        Draft202012Validator.check_schema(schema)

    @pytest.mark.parametrize("name", sorted(SCHEMA_IDS))
    def test_id_is_stable(self, name: str) -> None:
        assert _schema(name)["$id"] == SCHEMA_IDS[name]

    def test_no_other_schema_files(self) -> None:
        on_disk = sorted(p.name for p in SCHEMAS.glob("*.json"))
        assert on_disk == sorted(f"{n}.schema.json" for n in SCHEMA_IDS)


class TestPortablePatterns:
    """The README promises patterns that ECMA-262, Python ``re`` and Rust
    ``regex`` read the same way. This holds the promise to the schemas.
    """

    @pytest.mark.parametrize("name", sorted(SCHEMA_IDS))
    def test_patterns_stay_in_the_portable_subset(self, name: str) -> None:
        found = list(_regexes(_schema(name)))
        assert found
        for regex in found:
            # No lookaround, no inline flags, no non-capturing groups: Rust's
            # regex has no lookaround at all.
            assert "(?" not in regex, regex
            # The only escapes are \n and \r: no \d, \w, \s, \b, \. -- their
            # meaning differs between engines (\d is Unicode in Python).
            assert "\\" not in re.sub(r"\\[nr]", "", regex), regex
            # A $ inside a character class would be a literal in one reading and
            # an anchor in a careless rewrite; keep it out.
            in_class = False
            for char in regex:
                if char == "[":
                    in_class = True
                elif char == "]":
                    in_class = False
                elif char == "$":
                    assert not in_class, regex
            re.compile(regex)

    @pytest.mark.parametrize("name", sorted(SCHEMA_IDS))
    def test_pattern_property_keys_have_no_end_anchor(self, name: str) -> None:
        # jsonschema matches patternProperties with its own re.search, not with
        # the ``pattern`` keyword this suite overrides; without a $ the two
        # readings cannot differ.
        for obj in _walk(_schema(name)):
            for regex in obj.get("patternProperties", {}):
                assert "$" not in regex, regex


class TestCaseIndex:
    def test_layout_names_every_schema_and_its_folder(self) -> None:
        # Vendors read this map instead of guessing folder names. The manifest's
        # folder is NOT "manifest/": a Python .gitignore usually ignores
        # MANIFEST, and on a case-insensitive filesystem that silently swallows
        # a manifest/ folder -- green here, missing from every clean checkout.
        assert sorted(LAYOUT) == sorted(SCHEMA_IDS)
        for name, place in LAYOUT.items():
            assert place["schema"] == f"schemas/{name}.schema.json"
            assert (INTEROP / place["schema"]).is_file()
            assert (FIXTURES / place["fixtures"]).is_dir()
            assert place["fixtures"].lower() != "manifest"

    def test_every_fixture_file_is_listed(self) -> None:
        listed = sorted(case["file"] for case in CASES)
        on_disk = sorted(
            p.relative_to(FIXTURES).as_posix()
            for p in FIXTURES.rglob("*.json")
            if p.name != "cases.json"
        )
        assert listed == on_disk

    def test_no_duplicate_cases(self) -> None:
        files = [case["file"] for case in CASES]
        assert len(files) == len(set(files))

    @pytest.mark.parametrize("name", sorted(SCHEMA_IDS))
    def test_each_schema_has_valid_and_invalid_fixtures(self, name: str) -> None:
        kinds = {case["valid"] for case in CASES if case["schema"] == name}
        assert kinds == {True, False}

    def test_every_cited_rule_is_written(self) -> None:
        # A case or a schema that cites a rule the spec never wrote is a
        # pointer to nothing: the reviewer cannot check the reason.
        written = set()
        for doc in INTEROP.glob("*.md"):
            written.update(RULE_HEADING.findall(doc.read_text(encoding="utf-8")))
        cited = {case["rule"] for case in CASES}
        for name in SCHEMA_IDS:
            text = (SCHEMAS / f"{name}.schema.json").read_text(encoding="utf-8")
            cited.update(RULE_CITATION.findall(text))
        assert cited and written
        assert sorted(cited - written) == []

    def test_every_case_names_its_rule_and_reason(self) -> None:
        for case in CASES:
            assert case["schema"] in SCHEMA_IDS, case["file"]
            assert case["rule"].startswith("R"), case["file"]
            assert case["why"].strip(), case["file"]
            folder = "valid" if case["valid"] else "invalid"
            prefix = f"{LAYOUT[case['schema']]['fixtures']}/{folder}/"
            assert case["file"].startswith(prefix), case["file"]
            if not case["valid"]:
                assert case["errors"], case["file"]

    def test_no_case_expects_a_wrapper_keyword(self) -> None:
        # The comparison drops wrapper errors, so a case that listed one could
        # never pass in any language.
        for case in CASES:
            for error in case.get("errors", []):
                assert error["keyword"] not in WRAPPERS, case["file"]


class TestFixtures:
    @pytest.mark.parametrize("case", CASES, ids=[case["file"] for case in CASES])
    def test_case(self, case: dict[str, Any]) -> None:
        schema = _schema(case["schema"])
        failures = _suite_failures(case["schema"], schema)
        mine = [f for f in failures if f.startswith(case["file"] + ":")]
        assert mine == []

    @pytest.mark.parametrize("case", CASES, ids=[case["file"] for case in CASES])
    def test_stock_python_regex_gives_the_same_verdict(self, case: dict[str, Any]) -> None:
        # A consumer on stock jsonschema (Python's $ accepts a final newline)
        # must still accept every valid fixture and reject every invalid one:
        # that is what the una_linea definition in each schema is for.
        errors = list(
            Draft202012Validator(_schema(case["schema"])).iter_errors(
                _load(FIXTURES / case["file"])
            )
        )
        assert (errors == []) == case["valid"], case["file"]


def _mutations() -> list[tuple[str, tuple[Any, ...]]]:
    return [
        (name, location) for name in sorted(SCHEMA_IDS) for location in _locations(_schema(name))
    ]


class TestMutations:
    def test_the_unmutated_suite_is_green(self) -> None:
        for name in SCHEMA_IDS:
            assert _suite_failures(name, _schema(name)) == []

    def test_every_required_is_mutated(self) -> None:
        # The tripwire of the tripwire: the walker must see every ``required``.
        for name in SCHEMA_IDS:
            text = (SCHEMAS / f"{name}.schema.json").read_text(encoding="utf-8")
            seen = sum(1 for loc in _locations(_schema(name)) if "required" in loc[-2:-1])
            entries = sum(
                len(node["required"]) for node in _walk(_schema(name)) if "required" in node
            )
            assert seen == entries > 0, name
            assert text.count('"required"') == sum(
                1 for node in _walk(_schema(name)) if "required" in node
            ), name

    @pytest.mark.parametrize(
        "name,location",
        _mutations(),
        ids=[f"{n}:{_label(loc)}" for n, loc in _mutations()],
    )
    def test_removing_a_constraint_turns_the_suite_red(
        self, name: str, location: tuple[Any, ...]
    ) -> None:
        mutated = _mutate(_schema(name), location)
        assert _suite_failures(
            name, mutated
        ), f"no fixture guards {name}:{_label(location)}; add an invalid fixture for it"
