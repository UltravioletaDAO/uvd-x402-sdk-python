"""Offline reference runner of the interop conformance suite (``interop/``).

Everything comes from files; nothing goes over the network:

* the schema fixtures, ``interop/fixtures/cases.json``, compared the portable
  way ``interop/README.md`` writes down: errors from keywords that only wrap
  another are dropped, and the rest must equal the set the case lists;
* the vectors, ``interop/vectors/index.json``: the rules a JSON Schema cannot
  express, written as data and evaluated against :data:`REFERENCE`, or against
  an :class:`Implementation` the caller passes in (that is how another module
  runs them against its real code);
* an app's manifest (``/.well-known/uvd-stack.json``) saved to a file: the
  manifest schema first, then R1.10, R3.3 and R5.5.

The contract is the JSON. This module is its reference implementation: the
TypeScript SDK and the Rust crate vendor ``interop/`` and run the same files
against their own code.

``jsonschema`` (the ``interop`` extra) is imported only when a schema is
needed; the rule functions work on a base install.
"""

from __future__ import annotations

import copy
import importlib
import json
import re
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit

#: Keywords that only wrap the verdict of another. Validators differ on whether
#: they report them (Ajv reports a failed ``if``; jsonschema reports the inner
#: keyword), so the portable comparison drops them.
WRAPPERS = frozenset({"if", "then", "else", "allOf", "anyOf", "oneOf", "$ref", "propertyNames"})

#: R3.1: the methods that write. Everything else is a read.
WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

#: ERC-8128 (RFC 9421) carries a signature in these two headers.
SIGNATURE_HEADERS = frozenset({"signature", "signature-input"})

_KEYID = re.compile(r"erc8128:([0-9]+):(0x[0-9a-fA-F]{40})")


class MissingDependencyError(RuntimeError):
    """``jsonschema`` is not installed: the schemas cannot run."""


class SpecError(Exception):
    """The copy of ``interop/`` cannot be loaded (a schema or an index is broken)."""


# ---------------------------------------------------------------------------
# The reference implementation of the rules the vectors fix
# ---------------------------------------------------------------------------


def is_ip_ban(profile: str | None, status: int, headers: Mapping[str, str], body: str) -> bool:
    """Profile L9: a response of the API that speaks L9 is its IP ban when it
    is a 403 whose body is a JSON object with exactly one key, ``error``.

    ``profile`` is the legacy profile of the API that answered. Other apps of
    the stack put ``error`` at the root of any failure: for them (``None``)
    this is never a ban. The headers, Content-Type included, do not count.
    """
    if profile != "L9" or status != 403:
        return False
    try:
        parsed = json.loads(body)
    except ValueError:
        return False
    return isinstance(parsed, dict) and set(parsed) == {"error"}


def operation_of(method: str, operation: str | None = None) -> str:
    """``"read"`` or ``"write"``: the operation when the caller states it,
    otherwise the method's (POST, PUT, PATCH and DELETE write). A public MCP
    read travels by POST, so only the caller can say it reads.
    """
    if operation is not None:
        return operation
    return "write" if method in WRITE_METHODS else "read"


def must_sign(method: str, needs_identity: bool, operation: str | None = None) -> bool:
    """R3.1: every write is signed, and every read whose receiver needs to know
    who is calling. A public read is not, an MCP one by POST included.
    """
    return operation_of(method, operation) == "write" or needs_identity


def signing_conforms(
    method: str,
    headers: Mapping[str, str],
    needs_identity: bool,
    operation: str | None = None,
) -> bool:
    """R3.1 on the wire: a request that must be signed carries both signature
    headers; any other request carries neither. Header names are compared
    without case, as HTTP does.
    """
    present = SIGNATURE_HEADERS & {name.lower() for name in headers}
    if must_sign(method, needs_identity, operation):
        return present == SIGNATURE_HEADERS
    return not present


def parse_keyid(keyid: str) -> tuple[int, str]:
    """``erc8128:<chain_id>:<address>`` -> ``(chain_id, address in lower case)``."""
    match = _KEYID.fullmatch(keyid)
    if match is None:
        raise ValueError(f"not an ERC-8128 keyid: {keyid!r}")
    return int(match.group(1)), match.group(2).lower()


class EventInboxLike(Protocol):
    def deliver(
        self, event: Mapping[str, Any], keyid: str, nonce: str, signature_valid: bool
    ) -> tuple[int, dict[str, Any]]: ...


class EventInbox:
    """Reference receiver of event deliveries under the ``evento-s2s`` preset.

    The first check that applies decides: the signature (401, and the nonce is
    not consumed, so a garbage signature burns nothing), then the nonce, keyed
    by ``(chain_id, wallet, nonce)`` (409 ``nonce_replayed``; R3.6), then the
    event, deduplicated by ``(source, event_id)`` (200 ``already_processed``,
    never a 409 for a duplicate; R5.9), then it is accepted.
    """

    def __init__(self) -> None:
        self._nonces: set[tuple[int, str, str]] = set()
        self._events: set[tuple[str, str]] = set()

    def deliver(
        self, event: Mapping[str, Any], keyid: str, nonce: str, signature_valid: bool
    ) -> tuple[int, dict[str, Any]]:
        if not signature_valid:
            return 401, _uvd_error("signature_invalid", "The signature does not verify.")
        chain_id, wallet = parse_keyid(keyid)
        seen = (chain_id, wallet, nonce)
        if seen in self._nonces:
            return 409, _uvd_error("nonce_replayed", "This nonce was already used.")
        self._nonces.add(seen)
        dedupe = (event["source"], event["event_id"])
        if dedupe in self._events:
            return 200, {"status": "already_processed", "event_id": event["event_id"]}
        self._events.add(dedupe)
        return 202, {"status": "accepted", "event_id": event["event_id"]}


def _uvd_error(code: str, message: str) -> dict[str, Any]:
    # Repeating the same request cannot fix either: sign again, with a new nonce.
    return {
        "uvd_error": {
            "code": code,
            "message": message,
            "retryable": False,
            "spent": "no",
            "next_action": "authenticate",
        }
    }


def sender_nonces_conform(
    attempts: Sequence[Mapping[str, Any]], events: Mapping[str, Mapping[str, Any]]
) -> bool:
    """R3.6 and R3.7 on the sender's side: a signer (the wallet of the keyid,
    on any chain) never repeats a nonce, and a nonce is never the ``event_id``
    of the event it carries, with or without its hyphens.
    """
    used: dict[str, set[str]] = {}
    for attempt in attempts:
        if attempt["by"] != "sender":
            continue
        event_id = events[attempt["event"]]["event_id"].lower()
        nonce = attempt["nonce"]
        seen = used.setdefault(parse_keyid(attempt["keyid"])[1], set())
        if nonce in seen or nonce.lower() in (event_id, event_id.replace("-", "")):
            return False
        seen.add(nonce)
    return True


def url_authority(url: str) -> str:
    """The authority a client signs for ``url``, as a WHATWG URL parser reads
    it: the host in lower case and the port as a number, dropped when it is
    empty or 443 (the default of https).
    """
    netloc = urlsplit(url).netloc.lower()
    host, colon, port = netloc.rpartition(":")
    if not colon or not (port == "" or (port.isascii() and port.isdigit())):
        return netloc  # no port (an IPv6 literal's last colon is not one)
    if port == "" or int(port) == 443:
        return host
    return f"{host}:{int(port)}"


def date_exists(timestamp: str) -> bool:
    """R5.5: the calendar date of an RFC 3339 timestamp exists in the
    proleptic Gregorian calendar (year 0000 included). The time is not read,
    so a leap second (``23:59:60``) is valid.
    """
    year, month, day = int(timestamp[0:4]), int(timestamp[5:7]), int(timestamp[8:10])
    leap = year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)
    days = [31, 29 if leap else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
    return 1 <= month <= 12 and 1 <= day <= days[month - 1]


def manifest_rule_findings(manifest: Mapping[str, Any]) -> list[dict[str, str]]:
    """R1.10, R3.3 and R5.5 on a manifest that already validates against its
    schema. Each finding is ``{"rule", "instance_path"}``.
    """
    findings: list[dict[str, str]] = []
    app = manifest["app"]
    for index, event_type in enumerate(manifest["events"]["emits"]):
        if event_type.split(".", 1)[0] != app:
            findings.append(_finding("R1.10", ("events", "emits", index)))
    endpoints = manifest["endpoints"]
    own = {url_authority(endpoint["url"]) for endpoint in endpoints.values()}
    for name, endpoint in endpoints.items():
        policy = endpoint.get("erc8128")
        if policy is None:
            continue
        for index, authority in enumerate(policy["authorities"]):
            if authority not in own:
                findings.append(
                    _finding("R3.3", ("endpoints", name, "erc8128", "authorities", index))
                )
    generated_at = manifest.get("generated_at")
    if generated_at is not None and not date_exists(generated_at):
        findings.append(_finding("R5.5", ("generated_at",)))
    return findings


def _finding(rule: str, path: Iterable[Any]) -> dict[str, str]:
    return {"rule": rule, "instance_path": json_pointer(path)}


@dataclass(frozen=True)
class Implementation:
    """What the vectors call. :data:`REFERENCE` is this module's; replace one
    field (``dataclasses.replace``) to run the vectors against other code.
    """

    is_ip_ban: Callable[[str | None, int, Mapping[str, str], str], bool]
    must_sign: Callable[[str, bool, str | None], bool]
    signing_conforms: Callable[[str, Mapping[str, str], bool, str | None], bool]
    event_inbox: Callable[[], EventInboxLike]
    sender_nonces_conform: Callable[
        [Sequence[Mapping[str, Any]], Mapping[str, Mapping[str, Any]]], bool
    ]
    manifest_rule_findings: Callable[[Mapping[str, Any]], list[dict[str, str]]]


REFERENCE = Implementation(
    is_ip_ban=is_ip_ban,
    must_sign=must_sign,
    signing_conforms=signing_conforms,
    event_inbox=EventInbox,
    sender_nonces_conform=sender_nonces_conform,
    manifest_rule_findings=manifest_rule_findings,
)


# ---------------------------------------------------------------------------
# Schemas, with the pattern semantics JSON Schema defines
# ---------------------------------------------------------------------------


def _jsonschema() -> Any:
    try:
        return importlib.import_module("jsonschema")
    except ImportError as exc:
        raise MissingDependencyError(
            "the interop runner needs jsonschema: pip install 'uvd-x402-sdk[interop]'"
        ) from exc


def _ecma_anchors(pattern: str) -> str:
    """ECMA-262 reads ``$`` (no ``m`` flag) as the end of the input; Python's
    ``re`` also lets it match just before a final newline. Each ``$`` outside a
    character class becomes ``\\Z``, which is ECMA's ``$`` in Python.
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


def schema_validator(schema: Mapping[str, Any]) -> Any:
    """A draft 2020-12 validator whose ``pattern`` has ECMA-262 semantics, the
    ones Ajv and Rust's ``regex`` apply. Offline: its registry is empty, so a
    ``$ref`` to another document is an error, never a download (jsonschema's
    default registry fetches remote references over HTTP).
    """
    jsonschema = _jsonschema()
    referencing = importlib.import_module("referencing")

    def ecma_pattern(validator: Any, pattern: str, instance: Any, _schema: Any) -> Any:
        if validator.is_type(instance, "string") and not re.search(
            _ecma_anchors(pattern), instance
        ):
            yield jsonschema.ValidationError(f"{instance!r} does not match {pattern!r}")

    extended = jsonschema.validators.extend(
        jsonschema.Draft202012Validator, {"pattern": ecma_pattern}
    )
    return extended(schema, registry=referencing.Registry())


def json_pointer(path: Iterable[Any]) -> str:
    """JSON Pointer (RFC 6901) of an instance location."""
    return "".join("/" + str(step).replace("~", "~0").replace("/", "~1") for step in path)


def schema_errors(validator: Any, document: Any) -> list[dict[str, str]]:
    """The portable facts of each error: keyword, instance path and, for
    ``required``, the missing property. Wrapper keywords are dropped.
    """
    facts: list[dict[str, str]] = []
    for error in validator.iter_errors(document):
        if error.validator in WRAPPERS:
            continue
        fact = {"keyword": error.validator, "instance_path": json_pointer(error.absolute_path)}
        if error.validator == "required":
            # jsonschema yields ONE error per missing property, and names it.
            missing = [p for p in error.validator_value if p not in error.instance]
            fact["property"] = next(p for p in missing if repr(p) in error.message)
        facts.append(fact)
    return facts


def _fact_key(fact: Mapping[str, Any]) -> tuple[str, str, str]:
    return (fact["keyword"], fact["instance_path"], fact.get("property", ""))


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def load_validators(interop_dir: Path) -> dict[str, Any]:
    """One validator per schema, by the names ``fixtures/cases.json`` maps."""
    jsonschema = _jsonschema()
    try:
        layout = load_json(interop_dir / "fixtures" / "cases.json")["schemas"]
        validators: dict[str, Any] = {}
        for name, place in layout.items():
            schema = load_json(interop_dir / place["schema"])
            jsonschema.Draft202012Validator.check_schema(schema)
            validators[name] = schema_validator(schema)
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        raise SpecError(f"cannot load the schemas: {exc!r}") from exc
    except jsonschema.SchemaError as exc:
        raise SpecError(f"a schema is not valid draft 2020-12: {exc.message}") from exc
    return validators


def default_interop_dir() -> Path | None:
    """``interop/`` of the source checkout this module runs from, if any. An
    installed wheel does not ship ``interop/``: pass the directory instead.
    """
    candidate = Path(__file__).resolve().parents[3] / "interop"
    return candidate if (candidate / "fixtures" / "cases.json").is_file() else None


# ---------------------------------------------------------------------------
# The vectors
# ---------------------------------------------------------------------------


def merge_patch(target: Any, patch: Any) -> Any:
    """JSON Merge Patch (RFC 7396)."""
    if not isinstance(patch, dict):
        return copy.deepcopy(patch)
    result = copy.deepcopy(target) if isinstance(target, dict) else {}
    for key, value in patch.items():
        if value is None:
            result.pop(key, None)
        else:
            result[key] = merge_patch(result.get(key), value)
    return result


def _contains(actual: Any, expected: Any) -> bool:
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(
            key in actual and _contains(actual[key], value) for key, value in expected.items()
        )
    return bool(actual == expected)


def outcome_of(status: int, body: Any, outcomes: Mapping[str, Any]) -> str | None:
    """The outcome whose forms match a response: same status, and every key of
    the form's body in the response body with the same value. ``None`` when no
    outcome matches, or more than one does.
    """
    matches = [
        name
        for name, outcome in outcomes.items()
        if any(
            form["status"] == status and _contains(body, form["body"]) for form in outcome["any_of"]
        )
    ]
    return matches[0] if len(matches) == 1 else None


def _flag(value: Any, name: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{name} must be true or false, not {value!r}")
    return value


def _one_of(value: Any, allowed: Iterable[str], name: str) -> str:
    options = sorted(allowed)
    if value not in options:
        raise ValueError(f"{name} must be one of {options}, not {value!r}")
    return str(value)


def _ip_ban_case(
    case: Mapping[str, Any],
    vector: Mapping[str, Any],
    validators: Mapping[str, Any],
    impl: Implementation,
) -> list[str]:
    profile, response = case["profile"], case["response"]
    status, headers, body = response["status"], response["headers"], response["body"]
    if not (isinstance(status, int) and isinstance(headers, dict) and isinstance(body, str)):
        raise ValueError("response needs status (integer), headers (object) and body (text)")
    if not (profile is None or isinstance(profile, str)):
        raise ValueError(f"profile must be text or null, not {profile!r}")
    problems = []
    try:
        parsed = json.loads(body)
    except ValueError:
        parsed = None
    if isinstance(parsed, dict) and "uvd_error" in parsed:
        errors = schema_errors(validators["uvd-error"], parsed)
        if errors:
            problems.append(f"the uvd_error in its body does not validate: {errors}")
    want = _flag(case["expect"]["ip_ban"], "expect.ip_ban")
    got = impl.is_ip_ban(profile, status, headers, body)
    if got is not want:
        problems.append(f"expected ip_ban={want}, got {got!r}")
    return problems


def _request_signing_case(
    case: Mapping[str, Any],
    vector: Mapping[str, Any],
    validators: Mapping[str, Any],
    impl: Implementation,
) -> list[str]:
    request = case["request"]
    method, headers = request["method"], request["headers"]
    if not (isinstance(method, str) and isinstance(headers, dict)):
        raise ValueError("request needs method (text) and headers (object)")
    if not isinstance(request.get("body", ""), str):
        raise ValueError("request.body, when present, is the raw body as text")
    needs_identity = _flag(case["needs_identity"], "needs_identity")
    operation = case.get("operation")
    if operation is not None:
        operation = _one_of(operation, ("read", "write"), "operation")
    expect = case["expect"]
    problems = []
    for name, got in (
        ("must_sign", impl.must_sign(method, needs_identity, operation)),
        ("conforms", impl.signing_conforms(method, headers, needs_identity, operation)),
    ):
        want = _flag(expect[name], f"expect.{name}")
        if got is not want:
            problems.append(f"expected {name}={want}, got {got!r}")
    return problems


def _event_redelivery_case(
    scenario: Mapping[str, Any],
    vector: Mapping[str, Any],
    validators: Mapping[str, Any],
    impl: Implementation,
) -> list[str]:
    outcomes, events = vector["outcomes"], vector["events"]
    problems = []
    inbox = impl.event_inbox()
    for number, attempt in enumerate(scenario["attempts"], start=1):
        _one_of(attempt["by"], ("sender", "third_party"), "by")
        signature = _one_of(attempt["signature"], ("valid", "invalid"), "signature")
        want = _one_of(attempt["expect"], outcomes, "expect")
        status, body = inbox.deliver(
            events[attempt["event"]], attempt["keyid"], attempt["nonce"], signature == "valid"
        )
        got = outcome_of(status, body, outcomes)
        if got != want:
            seen = got if got is not None else f"{status} {json.dumps(body, sort_keys=True)}"
            problems.append(f"attempt {number}: expected {want}, got {seen}")
    want_sender = _flag(scenario["sender_conforms"], "sender_conforms")
    got_sender = impl.sender_nonces_conform(scenario["attempts"], events)
    if got_sender is not want_sender:
        problems.append(f"expected sender_conforms={want_sender}, got {got_sender!r}")
    return problems


def _event_redelivery_data(vector: Mapping[str, Any], validators: Mapping[str, Any]) -> list[str]:
    problems = []
    for name, event in vector["events"].items():
        errors = schema_errors(validators["uvd-event-1"], event)
        if errors:
            problems.append(f"event {name} does not validate against uvd-event-1: {errors}")
    return problems


def _manifest_rules_case(
    case: Mapping[str, Any],
    vector: Mapping[str, Any],
    validators: Mapping[str, Any],
    impl: Implementation,
) -> list[str]:
    manifest = merge_patch(vector["base"], case["patch"])
    errors = schema_errors(validators["manifest"], manifest)
    if errors:
        return [f"the patched manifest does not validate against its schema: {errors}"]
    got = sorted((f["rule"], f["instance_path"]) for f in impl.manifest_rule_findings(manifest))
    want = sorted((f["rule"], f["instance_path"]) for f in case["expect"]["findings"])
    return [] if got == want else [f"expected findings {want}, got {got}"]


_CaseCheck = Callable[
    [Mapping[str, Any], Mapping[str, Any], Mapping[str, Any], Implementation], list[str]
]

#: kind -> (the key that holds its cases, the check of one case, the check of
#: the data the cases share).
_KINDS: dict[str, tuple[str, _CaseCheck, Callable[..., list[str]] | None]] = {
    "ip-ban": ("cases", _ip_ban_case, None),
    "request-signing": ("cases", _request_signing_case, None),
    "event-redelivery": ("scenarios", _event_redelivery_case, _event_redelivery_data),
    "manifest-rules": ("cases", _manifest_rules_case, None),
}


def evaluate_vector(
    vector: Mapping[str, Any],
    validators: Mapping[str, Any],
    implementation: Implementation = REFERENCE,
    rules: Iterable[str] | None = None,
) -> tuple[int, list[tuple[str, str]]]:
    """Run one vector document. Returns how many cases ran and the failures,
    as ``(case id, detail)``. A malformed case, or one whose implementation
    raises, is a failure: the runner reports, it does not crash.

    ``rules`` are the rules ``index.json`` lists for this file: when given,
    every case must cite one of them and every one must be cited.
    """
    kind = vector.get("kind") if isinstance(vector, dict) else None
    if kind not in _KINDS:
        return 0, [("*", f"no implementation for kind {kind!r}")]
    key, check, shared = _KINDS[kind]
    failures: list[tuple[str, str]] = []
    # Broad on purpose, here and below: a broken vector or an implementation
    # that raises is a red suite with the case named, never a traceback.
    try:
        cases = list(vector[key])
        listed = None if rules is None else {str(rule) for rule in rules}
        if shared is not None:
            failures.extend(("*", problem) for problem in shared(vector, validators))
    except Exception as exc:
        return 0, [("*", f"malformed vector: {exc!r}")]
    if not cases:
        failures.append(("*", f"no {key}: a vector without cases guards nothing"))
    ids = Counter(repr(case.get("id")) if isinstance(case, dict) else "?" for case in cases)
    for case in cases:
        case_id = str(case.get("id")) if isinstance(case, dict) else "?"
        try:
            for name in ("id", "rule", "why"):
                if not (isinstance(case.get(name), str) and case[name].strip()):
                    raise ValueError(f"every case needs a non-empty {name}")
            if ids[repr(case["id"])] > 1:
                raise ValueError("duplicate case id")
            if listed is not None and case["rule"] not in listed:
                raise ValueError(f"rule {case['rule']} is not listed for this file in index.json")
            problems = check(case, vector, validators, implementation)
        except Exception as exc:
            problems = [f"malformed case, or the implementation raised: {exc!r}"]
        failures.extend((case_id, problem) for problem in problems)
    if listed is not None:
        cited = {case.get("rule") for case in cases if isinstance(case, dict)}
        for rule in sorted(listed - cited):
            failures.append(("*", f"index.json lists {rule} and no case fixes it"))
    return len(cases), failures


# ---------------------------------------------------------------------------
# The runner
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Failure:
    """One reason the suite is red."""

    section: str
    item: str
    detail: str

    def __str__(self) -> str:
        return f"{self.section} {self.item}: {self.detail}"


@dataclass
class Report:
    interop_dir: Path
    fixtures: int = 0
    vectors: dict[str, int] = field(default_factory=dict)
    manifests: dict[str, int] = field(default_factory=dict)
    failures: list[Failure] = field(default_factory=list)

    @property
    def green(self) -> bool:
        return not self.failures


def _fixture_problem(
    fixtures_dir: Path,
    case: Mapping[str, Any],
    validators: Mapping[str, Any],
    implementation: Implementation,
) -> str | None:
    validator = validators[case["schema"]]
    document = load_json(fixtures_dir / case["file"])
    got = sorted({_fact_key(f) for f in schema_errors(validator, document)})
    valid = _flag(case["valid"], "valid")
    if valid:
        if got:
            return f"expected valid, got {got}"
        # A valid example must conform to the whole contract, not only to what
        # its schema can see: whoever copies it copies its mistakes.
        if case["schema"] == "manifest":
            findings = implementation.manifest_rule_findings(document)
            if findings:
                return f"valid for its schema, but breaks the runner rules: {findings}"
        return None
    want = sorted({_fact_key(f) for f in case["errors"]})
    if not want:
        return "an invalid case must list its errors"
    if any(keyword in WRAPPERS for keyword, _, _ in want):
        return "lists a wrapper keyword, which the comparison drops"
    return None if got == want else f"expected {want}, got {got}"


def _run_fixtures(
    interop_dir: Path,
    validators: Mapping[str, Any],
    implementation: Implementation,
    report: Report,
) -> None:
    fixtures_dir = interop_dir / "fixtures"
    try:
        cases = list(load_json(fixtures_dir / "cases.json")["cases"])
    except (OSError, ValueError, KeyError, TypeError) as exc:
        report.failures.append(Failure("fixtures", "cases.json", f"cannot load: {exc!r}"))
        return
    listed = Counter(
        case["file"] if isinstance(case, dict) and isinstance(case.get("file"), str) else "?"
        for case in cases
    )
    on_disk = {
        path.relative_to(fixtures_dir).as_posix()
        for path in fixtures_dir.rglob("*.json")
        if path.name != "cases.json"
    }
    for unlisted in sorted(on_disk - set(listed)):
        report.failures.append(
            Failure("fixtures", unlisted, "on disk but not listed in cases.json")
        )
    for file, count in sorted(listed.items()):
        if count > 1 and file != "?":
            report.failures.append(Failure("fixtures", file, "listed more than once"))
    for case in cases:
        report.fixtures += 1
        item = str(case.get("file")) if isinstance(case, dict) else "?"
        try:
            problem = _fixture_problem(fixtures_dir, case, validators, implementation)
        except Exception as exc:  # a broken fixture is a red suite, not a traceback
            problem = f"malformed case: {exc!r}"
        if problem:
            report.failures.append(Failure("fixtures", item, problem))


def _run_vectors(
    interop_dir: Path,
    validators: Mapping[str, Any],
    implementation: Implementation,
    report: Report,
) -> None:
    vectors_dir = interop_dir / "vectors"
    try:
        entries = dict(load_json(vectors_dir / "index.json")["vectors"])
        files = {kind: str(entry["file"]) for kind, entry in entries.items()}
        rules = {kind: list(entry["rules"]) for kind, entry in entries.items()}
    except (OSError, ValueError, KeyError, TypeError) as exc:
        report.failures.append(Failure("vectors", "index.json", f"cannot load: {exc!r}"))
        return
    on_disk = {
        path.relative_to(vectors_dir).as_posix()
        for path in vectors_dir.rglob("*.json")
        if path.relative_to(vectors_dir).as_posix() != "index.json"
    }
    for name in sorted(on_disk - set(files.values())):
        report.failures.append(Failure("vectors", name, "on disk but not listed in index.json"))
    for kind, name in files.items():
        try:
            vector = load_json(vectors_dir / name)
        except (OSError, ValueError) as exc:
            report.failures.append(Failure("vectors", name, f"cannot load: {exc!r}"))
            continue
        if not isinstance(vector, dict) or vector.get("kind") != kind:
            found = vector.get("kind") if isinstance(vector, dict) else None
            report.failures.append(
                Failure("vectors", name, f"index.json lists it as {kind!r}, it says {found!r}")
            )
            continue
        count, failures = evaluate_vector(vector, validators, implementation, rules[kind])
        report.vectors[kind] = count
        report.failures.extend(
            Failure("vectors", f"{kind}/{case_id}", detail) for case_id, detail in failures
        )


def check_manifest(
    document: Any, validators: Mapping[str, Any], implementation: Implementation = REFERENCE
) -> list[str]:
    """What is wrong with one app manifest, empty when it conforms: its schema
    errors, or, when it validates, what breaks R1.10, R3.3 or R5.5.
    """
    errors = schema_errors(validators["manifest"], document)
    if errors:
        return [
            f"schema: {e['keyword']} at {e['instance_path'] or '/'}"
            + (f" (missing {e['property']!r})" if "property" in e else "")
            for e in errors
        ]
    return [
        f"{f['rule']} at {f['instance_path']}: {_value_at(document, f['instance_path'])!r} "
        f"{_RULE_TEXT.get(f['rule'], 'breaks the rule')}"
        for f in implementation.manifest_rule_findings(document)
    ]


_RULE_TEXT = {
    "R1.10": "does not start with this manifest's app id",
    "R3.3": "is not the authority of any endpoint of this manifest",
    "R5.5": "is not a date that exists",
}


def _value_at(document: Any, pointer: str) -> Any:
    value = document
    for step in pointer.split("/")[1:]:
        step = step.replace("~1", "/").replace("~0", "~")
        value = value[int(step)] if isinstance(value, list) else value[step]
    return value


def run(
    interop_dir: Path,
    manifests: Sequence[Path] = (),
    implementation: Implementation = REFERENCE,
) -> Report:
    """Run the whole suite in ``interop_dir`` (fixtures, then vectors) and check
    each manifest file. Raises :class:`MissingDependencyError` without jsonschema.
    """
    report = Report(interop_dir=interop_dir)
    try:
        validators = load_validators(interop_dir)
    except SpecError as exc:
        report.failures.append(Failure("schemas", "cases.json", str(exc)))
        return report
    _run_fixtures(interop_dir, validators, implementation, report)
    _run_vectors(interop_dir, validators, implementation, report)
    for path in manifests:
        try:
            document = load_json(path)
        except (OSError, ValueError) as exc:
            problems = [f"cannot read it as JSON: {exc!r}"]
        else:
            problems = check_manifest(document, validators, implementation)
        report.manifests[str(path)] = len(problems)
        report.failures.extend(Failure("manifest", str(path), problem) for problem in problems)
    return report
