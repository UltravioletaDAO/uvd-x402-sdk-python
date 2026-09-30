"""``publish.yml`` publishes to PyPI by trusted publishing (OIDC), by hand, from main.

The owner's decision 127 (2026-09-29): the SDKs publish by trusted publishing,
with no token, from the ``pypi`` environment. Until then this workflow uploaded
with twine and a long-lived API token kept as a repository secret, ran on every
``v*`` tag, and said in a comment that it already used OIDC. Decision 136
(2026-09-30) removed the environment's required reviewer: no human approves the
upload, and ``pypi`` keeps ``main`` as its only branch. Both are settings of the
repository, not of this file, so no rule below can see them.

Each rule below returns what it finds wrong in a workflow's text. The real
file must break none (``TestTheWorkflow``), and ``TestMutations`` edits the
real text back into each mistake and asserts the exact set of rules that
catch it: a rule that stops catching its mutation guards nothing.

* ``no_secrets`` -- a ``secrets`` context anywhere in the text (comments
  included), in any form (``secrets.X``, ``secrets['X']``, ``toJSON(secrets)``),
  or a ``secrets:`` key.
* ``manual_only`` -- a trigger other than ``workflow_dispatch``, or no
  required ``version`` input.
* ``id_token_only_where_it_publishes`` -- ``id-token`` at the workflow level,
  in any job that does not publish, through a shorthand (``write-all``), or
  missing where the upload needs it.
* ``pypi_environment`` -- the job that publishes without ``environment:
  pypi``, the environment PyPI trusts and that deploys from ``main`` only.
* ``main_only`` -- no ``github.ref == 'refs/heads/main'`` gate among the jobs
  the upload needs, or a job of that chain that runs when the gate skipped.
* ``one_publisher`` -- other than one job that uploads, an upload from a
  ``run:`` step (twine and the like), or a password handed to the action.
* ``pinned`` -- an action not pinned to a full commit SHA with its tag.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = (ROOT / ".github" / "workflows" / "publish.yml").read_text(encoding="utf-8")

PUBLISH_ACTION = "pypa/gh-action-pypi-publish@"
MAIN_ONLY = "github.ref == 'refs/heads/main'"
# A job that has one of these in its `if` runs even when a job it needs was skipped.
RUNS_AFTER_A_SKIP = re.compile(r"\b(always|failure|cancelled)\s*\(")
# Uploading to a package index from a `run:` step, token in hand.
UPLOADERS = re.compile(r"\b(twine\s+upload|(uv|poetry|flit|hatch|pdm)\s+publish)\b")
PINNED = re.compile(r"^\s*(-\s+)?uses:\s*[\w.-]+/[\w./-]+@[0-9a-f]{40} # v\d+(\.\d+)*\s*$")


def _parse(text: str) -> dict[Any, Any]:
    workflow = yaml.safe_load(text)
    assert isinstance(workflow, dict), "the workflow is not a YAML mapping"
    return workflow


def _jobs(workflow: dict[Any, Any]) -> dict[str, dict[str, Any]]:
    jobs = workflow.get("jobs")
    return jobs if isinstance(jobs, dict) else {}


def _steps(job: dict[str, Any]) -> list[dict[str, Any]]:
    steps = job.get("steps")
    return [step for step in steps if isinstance(step, dict)] if isinstance(steps, list) else []


def _uploads(step: dict[str, Any]) -> bool:
    return str(step.get("uses", "")).startswith(PUBLISH_ACTION) or bool(
        UPLOADERS.search(str(step.get("run", "")))
    )


def _publishers(workflow: dict[Any, Any]) -> list[str]:
    """The jobs that upload to PyPI, whatever they upload with."""
    return [name for name, job in _jobs(workflow).items() if any(map(_uploads, _steps(job)))]


def _condition(job: dict[str, Any]) -> str:
    """A job's `if`, without the optional ``${{ }}`` and with its spaces folded."""
    text = str(job.get("if", "")).strip()
    bare = re.fullmatch(r"\$\{\{(.*)\}\}", text, re.DOTALL)
    return " ".join((bare.group(1) if bare else text).split())


def _chain(workflow: dict[Any, Any], name: str) -> list[str]:
    """``name`` and every job it needs, transitively."""
    jobs, seen, todo = _jobs(workflow), [], [name]
    while todo:
        current = todo.pop()
        if current in seen or current not in jobs:
            continue
        seen.append(current)
        needs = jobs[current].get("needs", [])
        todo.extend([needs] if isinstance(needs, str) else needs)
    return seen


def no_secrets(text: str) -> list[str]:
    problems = [
        f"line {number}: {line.strip()}"
        for number, line in enumerate(text.splitlines(), 1)
        if re.search(r"\bsecrets\s*[.\[]", line)
    ]
    problems += [
        f"expression reads the secrets context: {expression.strip()}"
        for expression in re.findall(r"\$\{\{(.*?)\}\}", text, re.DOTALL)
        if re.search(r"\bsecrets\b", expression)
    ]

    def keys(node: Any) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "secrets":
                    problems.append("a `secrets:` key")
                keys(value)
        elif isinstance(node, list):
            for value in node:
                keys(value)

    keys(_parse(text))
    return problems


def manual_only(text: str) -> list[str]:
    workflow = _parse(text)
    # YAML 1.1, which PyYAML reads, takes the bare key `on` for the boolean true.
    triggers = workflow.get("on", workflow.get(True))
    if isinstance(triggers, (str, list)):
        triggers = dict.fromkeys([triggers] if isinstance(triggers, str) else triggers)
    if not isinstance(triggers, dict):
        return [f"no readable `on:` block: {triggers!r}"]
    problems = [f"trigger `{name}`" for name in triggers if name != "workflow_dispatch"]
    dispatch = triggers.get("workflow_dispatch")
    inputs = dispatch.get("inputs") if isinstance(dispatch, dict) else None
    version = inputs.get("version") if isinstance(inputs, dict) else None
    if not (
        isinstance(version, dict)
        and version.get("required") is True
        and version.get("type") == "string"
    ):
        problems.append("workflow_dispatch has no required string input `version`")
    return problems


def id_token_only_where_it_publishes(text: str) -> list[str]:
    workflow = _parse(text)
    publishers = _publishers(workflow)
    problems = []
    if workflow.get("permissions") != {"contents": "read"}:
        problems.append(
            f"workflow permissions {workflow.get('permissions')!r}, not contents: read alone"
        )
    for name, job in _jobs(workflow).items():
        permissions = job.get("permissions")
        if permissions is not None and not isinstance(permissions, dict):
            problems.append(f"job `{name}` permissions `{permissions}`: spell each one out")
        elif isinstance(permissions, dict) and "id-token" in permissions:
            if name not in publishers:
                problems.append(f"job `{name}` gets id-token and does not publish")
        elif name in publishers:
            problems.append(f"job `{name}` publishes without `id-token: write`")
    return problems


def pypi_environment(text: str) -> list[str]:
    workflow = _parse(text)
    problems = []
    for name in _publishers(workflow):
        environment = _jobs(workflow)[name].get("environment")
        named = environment.get("name") if isinstance(environment, dict) else environment
        if named != "pypi":
            problems.append(f"job `{name}` publishes in environment {environment!r}, not pypi")
    return problems


def main_only(text: str) -> list[str]:
    workflow = _parse(text)
    jobs = _jobs(workflow)
    problems = []
    for name in _publishers(workflow):
        chain = _chain(workflow, name)
        if not any(_condition(jobs[job]) == MAIN_ONLY for job in chain):
            problems.append(f"nothing `{name}` needs is gated by `if: {MAIN_ONLY}`")
        problems += [
            f"job `{job}` runs when the main gate skipped: if: {_condition(jobs[job])}"
            for job in chain
            if RUNS_AFTER_A_SKIP.search(_condition(jobs[job]))
        ]
    return problems


def one_publisher(text: str) -> list[str]:
    workflow = _parse(text)
    publishers = _publishers(workflow)
    problems = []
    if len(publishers) != 1:
        problems.append(f"{len(publishers)} jobs upload to PyPI {publishers}, not one")
    for name, job in _jobs(workflow).items():
        for step in _steps(job):
            if UPLOADERS.search(str(step.get("run", ""))):
                problems.append(f"job `{name}` uploads from a run: step")
            given = step.get("with") if isinstance(step.get("with"), dict) else {}
            if _uploads(step) and {"password", "user"} & set(given):
                problems.append(f"job `{name}` hands the action a password or user")
    return problems


def pinned(text: str) -> list[str]:
    return [
        f"line {number}: {line.strip()}"
        for number, line in enumerate(text.splitlines(), 1)
        if re.match(r"^\s*(-\s+)?uses:", line) and not PINNED.match(line)
    ]


RULES: dict[str, Callable[[str], list[str]]] = {
    rule.__name__: rule
    for rule in (
        no_secrets,
        manual_only,
        id_token_only_where_it_publishes,
        pypi_environment,
        main_only,
        one_publisher,
        pinned,
    )
}


def _broken(text: str) -> set[str]:
    return {name for name, rule in RULES.items() if rule(text)}


class TestTheWorkflow:
    @pytest.mark.parametrize("rule", sorted(RULES))
    def test_breaks_no_rule(self, rule: str) -> None:
        assert RULES[rule](WORKFLOW) == []

    def test_the_one_job_that_publishes_is_publish(self) -> None:
        assert _publishers(_parse(WORKFLOW)) == ["publish"]
        assert _chain(_parse(WORKFLOW), "publish") == ["publish", "build", "check"]

    def test_the_gate_checks_the_input_against_the_package_version(self) -> None:
        check = _jobs(_parse(WORKFLOW))["check"]
        assert _condition(check) == MAIN_ONLY
        version = [s for s in _steps(check) if "inputs.version" in str(s.get("env", ""))]
        assert len(version) == 1
        assert "pyproject.toml" in version[0]["run"] and "exit 1" in version[0]["run"]
        # Through env, never spliced into the script: that would be a template injection.
        assert "inputs." not in version[0]["run"]


def _replace(text: str, old: str, new: str) -> str:
    assert text.count(old) == 1, f"the mutation's anchor is not in publish.yml once: {old!r}"
    return text.replace(old, new)


PUBLISH_WITH = "        with:\n          packages-dir: dist/\n"
ON = "on:\n  workflow_dispatch:\n"
TOP_PERMISSIONS = "permissions:\n  contents: read # the job that publishes adds id-token itself\n"
PUBLISH_PERMISSIONS = (
    "      id-token: write # trusted publishing (OIDC); the only job that gets it\n"
)
GATE = "    if: github.ref == 'refs/heads/main'\n"
BUILD_NEEDS = "    name: Build the sdist and the wheel\n    needs: check\n"
VERSION_ENV = "          INPUT_VERSION: ${{ inputs.version }}\n        run: |\n          pkg="

MUTATIONS: dict[str, tuple[Callable[[str], str], set[str]]] = {
    # The three the owner's decision names.
    "the token is back": (
        lambda t: _replace(
            t, PUBLISH_WITH, PUBLISH_WITH + "          password: ${{ secrets.PYPI_TOKEN }}\n"
        ),
        {"no_secrets", "one_publisher"},
    ),
    "push: tags is back": (
        lambda t: _replace(t, ON, "on:\n  push:\n    tags:\n      - 'v*'\n  workflow_dispatch:\n"),
        {"manual_only"},
    ),
    "the environment is gone": (
        lambda t: _replace(t, "    environment: pypi\n", ""),
        {"pypi_environment"},
    ),
    # The old way back whole: twine, the token in env, no environment.
    "twine upload in build": (
        lambda t: _replace(
            t,
            '          test "$(ls dist/ | wc -l)" -eq 2\n',
            '          test "$(ls dist/ | wc -l)" -eq 2\n'
            "          TWINE_PASSWORD=${{ secrets.PYPI_TOKEN }} python -m twine upload dist/*\n",
        ),
        {"no_secrets", "one_publisher", "id_token_only_where_it_publishes", "pypi_environment"},
    ),
    "secrets by index": (
        lambda t: _replace(
            t, VERSION_ENV, "          TOKEN: ${{ secrets['PYPI_TOKEN'] }}\n" + VERSION_ENV
        ),
        {"no_secrets"},
    ),
    "the whole secrets context": (
        lambda t: _replace(t, VERSION_ENV, "          ALL: ${{ toJSON(secrets) }}\n" + VERSION_ENV),
        {"no_secrets"},
    ),
    "a release trigger": (
        lambda t: _replace(
            t, ON, "on:\n  release:\n    types: [published]\n  workflow_dispatch:\n"
        ),
        {"manual_only"},
    ),
    "a push to main": (
        lambda t: _replace(t, ON, "on:\n  push:\n    branches: [main]\n  workflow_dispatch:\n"),
        {"manual_only"},
    ),
    "no version input": (
        lambda t: _replace(t, "        required: true\n", "        required: false\n"),
        {"manual_only"},
    ),
    "another environment": (
        lambda t: _replace(t, "    environment: pypi\n", "    environment: release\n"),
        {"pypi_environment"},
    ),
    "id-token for every job": (
        lambda t: _replace(t, TOP_PERMISSIONS, TOP_PERMISSIONS + "  id-token: write\n"),
        {"id_token_only_where_it_publishes"},
    ),
    "write-all": (
        lambda t: _replace(t, TOP_PERMISSIONS, "permissions: write-all\n"),
        {"id_token_only_where_it_publishes"},
    ),
    "id-token in build": (
        lambda t: _replace(
            t,
            BUILD_NEEDS,
            BUILD_NEEDS + "    permissions:\n      contents: read\n      id-token: write\n",
        ),
        {"id_token_only_where_it_publishes"},
    ),
    "id-token in check, beside the unpinned test dependencies": (
        lambda t: _replace(t, GATE, GATE + "    permissions:\n      id-token: write\n"),
        {"id_token_only_where_it_publishes"},
    ),
    "no id-token where it publishes": (
        lambda t: _replace(t, PUBLISH_PERMISSIONS, "      contents: read\n"),
        {"id_token_only_where_it_publishes"},
    ),
    "the main gate is gone": (
        lambda t: _replace(t, GATE, ""),
        {"main_only"},
    ),
    "the main gate is widened": (
        lambda t: _replace(t, GATE, GATE[:-1] + " || github.event_name == 'workflow_dispatch'\n"),
        {"main_only"},
    ),
    "build no longer needs the gate": (
        lambda t: _replace(t, BUILD_NEEDS, "    name: Build the sdist and the wheel\n"),
        {"main_only"},
    ),
    "build runs after a skipped gate": (
        lambda t: _replace(t, BUILD_NEEDS, BUILD_NEEDS + "    if: ${{ always() }}\n"),
        {"main_only"},
    ),
    "an action by tag": (
        lambda t: _replace(
            t,
            "actions/download-artifact@3e5f45b2cfb9172054b4087a40e8e0b5a5461e7c # v8.0.1",
            "actions/download-artifact@v8",
        ),
        {"pinned"},
    ),
    # Controls: equivalent spellings the rules must not refuse.
    "control: the environment as a mapping": (
        lambda t: _replace(
            t,
            "    environment: pypi\n",
            "    environment:\n      name: pypi\n      url: https://pypi.org/p/uvd-x402-sdk\n",
        ),
        set(),
    ),
    "control: the gate inside ${{ }}": (
        lambda t: _replace(t, GATE, "    if: ${{ github.ref == 'refs/heads/main' }}\n"),
        set(),
    ),
}


class TestMutations:
    @pytest.mark.parametrize("name", sorted(MUTATIONS))
    def test_each_mutation_breaks_exactly_its_rules(self, name: str) -> None:
        mutate, expected = MUTATIONS[name]
        mutated = mutate(WORKFLOW)
        assert mutated != WORKFLOW
        assert _broken(mutated) == expected
