"""``scripts/release_tag.py``: the tag ``v<version>`` that ``publish.yml`` creates after publishing.

Real git, no network: a bare repository stands for GitHub, and a ``--depth 1 --no-tags`` clone
of its ``main`` for what ``actions/checkout`` leaves on the runner (one commit, no tags). Every
case runs under git protocol 0 and 2, the two a server may speak. Under both, ``ls-remote``
prints the peeled line of an annotated tag only when ``<ref>^{}`` is passed as a pattern too
(GitHub answers the same), and it matches a pattern against the TAIL of a ref, so
``refs/tags/old/refs/tags/v1.2.3`` answers for ``refs/tags/v1.2.3``.

``TestGuardMutations`` removes each guard from the script's SOURCE and asserts that the case it
guards then ends otherwise: a guard that no case depends on guards nothing.
"""

from __future__ import annotations

import itertools
import os
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from types import ModuleType
from typing import Callable

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "release_tag.py"
SOURCE = SCRIPT.read_text(encoding="utf-8")
TAG = "refs/tags/v1.2.3"
TAGGER = "github-actions[bot] <41898282+github-actions[bot]@users.noreply.github.com>"


def _load(source: str = SOURCE) -> ModuleType:
    module = ModuleType("release_tag")
    module.__file__ = str(SCRIPT)
    exec(compile(source, str(SCRIPT), "exec"), module.__dict__)
    return module


def _git(cwd: Path, *args: str) -> str:
    done = subprocess.run(["git", *args], cwd=cwd, capture_output=True, check=True)
    return done.stdout.decode("utf-8").strip()


def manifest(version: str) -> str:
    return f'[project]\nname = "uvd-x402-sdk"\nversion = "{version}"\n'


class Origin:
    """A bare repository standing for GitHub, and the runner's shallow checkout of its main."""

    def __init__(self, root: Path, manifests: Sequence[str]) -> None:
        root.mkdir()
        self.bare = root / "origin.git"
        seed = root / "seed"
        _git(root, "init", "-q", "--bare", "-b", "main", str(self.bare))
        _git(root, "init", "-q", "-b", "main", str(seed))
        self.commits: list[str] = []
        for number, text in enumerate(manifests):
            (seed / "pyproject.toml").write_bytes(text.encode("utf-8"))
            (seed / "commit").write_text(str(number), encoding="utf-8")
            _git(seed, "add", "pyproject.toml", "commit")
            _git(seed, "commit", "-q", "-m", f"commit {number}")
            self.commits.append(_git(seed, "rev-parse", "HEAD"))
        _git(seed, "push", "-q", self.bare.as_posix(), "main")
        self.checkout = root / "checkout"
        _git(
            root,
            "clone", "-q", "--depth", "1", "--no-tags", "--branch", "main",
            self.bare.as_uri(), str(self.checkout),
        )  # fmt: skip
        self.older, self.head = self.commits[0], self.commits[-1]

    def tag(self, name: str, target: str, annotated: bool = True) -> None:
        """A tag someone else made on the origin."""
        made = ["tag", "-a", "-m", f"made by hand: {name}"] if annotated else ["tag"]
        _git(self.bare, *made, name, target)

    def tags(self) -> dict[str, str]:
        """Every tag ref on the origin, with the object it holds."""
        listed = _git(self.bare, "for-each-ref", "refs/tags", "--format=%(refname) %(objectname)")
        return dict(line.split(" ", 1) for line in listed.splitlines())

    def peeled(self, ref: str = TAG) -> str:
        return _git(self.bare, "rev-parse", ref + "^{}")


@pytest.fixture(params=["0", "2"], ids=["protocol-0", "protocol-2"])
def protocol(request: pytest.FixtureRequest) -> str:
    return str(request.param)


@pytest.fixture
def make_origin(
    protocol: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Callable[..., Origin]:
    # Nothing of the machine's git setup or of an enclosing git process (a hook running the
    # suite sets GIT_DIR) reaches these repositories; nothing of a CI run reaches the script.
    for name in list(os.environ):
        if name.startswith(("GIT_", "GITHUB_")):
            monkeypatch.delenv(name)
    config = tmp_path / "gitconfig"
    config.write_text("", encoding="utf-8")
    for name, value in {
        "GIT_CONFIG_GLOBAL": str(config),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_AUTHOR_NAME": "Someone",
        "GIT_AUTHOR_EMAIL": "someone@example.invalid",
        "GIT_COMMITTER_NAME": "Someone",
        "GIT_COMMITTER_EMAIL": "someone@example.invalid",
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "protocol.version",
        "GIT_CONFIG_VALUE_0": protocol,
    }.items():
        monkeypatch.setenv(name, value)
    numbers = itertools.count()

    def make(*manifests: str) -> Origin:
        origin = Origin(
            tmp_path / f"origin-{next(numbers)}",
            manifests or (manifest("1.2.3"), manifest("1.2.3")),
        )
        monkeypatch.chdir(origin.checkout)
        return origin

    return make


def run(
    capsys: pytest.CaptureFixture[str], *argv: str, module: ModuleType | None = None
) -> tuple[int, str]:
    code = (module or _load()).main(list(argv))
    return code, capsys.readouterr().out


class TestTagging:
    def test_creates_an_annotated_tag_on_the_published_commit(self, make_origin, capsys) -> None:
        origin = make_origin()
        code, out = run(capsys, "--version", "1.2.3", "--sha", origin.head)
        assert code == 0, out
        assert out.strip() == f"Created v1.2.3 -> {origin.head}"
        assert list(origin.tags()) == [TAG]
        assert _git(origin.bare, "cat-file", "-t", TAG) == "tag"
        assert origin.peeled() == origin.head
        body = _git(origin.bare, "cat-file", "-p", TAG)
        assert f"\ntype commit\ntag v1.2.3\ntagger {TAGGER} " in body
        assert body.endswith("\n\nuvd-x402-sdk 1.2.3")
        # No local ref: a stale tag in the checkout can never be what gets pushed.
        assert _git(origin.checkout, "tag", "--list") == ""

    def test_the_message_names_the_run_that_published(
        self, make_origin, capsys, monkeypatch
    ) -> None:
        origin = make_origin()
        monkeypatch.setenv("GITHUB_SERVER_URL", "https://github.com")
        monkeypatch.setenv("GITHUB_REPOSITORY", "UltravioletaDAO/uvd-x402-sdk-python")
        monkeypatch.setenv("GITHUB_RUN_ID", "123")
        assert run(capsys, "--version", "1.2.3", "--sha", origin.head)[0] == 0
        assert _git(origin.bare, "cat-file", "-p", TAG).endswith(
            "\n\nuvd-x402-sdk 1.2.3\n\nPublished to PyPI by "
            "https://github.com/UltravioletaDAO/uvd-x402-sdk-python/actions/runs/123"
        )

    def test_the_commit_defaults_to_github_sha(self, make_origin, capsys, monkeypatch) -> None:
        origin = make_origin()
        monkeypatch.setenv("GITHUB_SHA", origin.head)
        assert run(capsys, "--version", "1.2.3")[0] == 0
        assert origin.peeled() == origin.head

    def test_an_uppercase_sha_is_the_same_commit(self, make_origin, capsys) -> None:
        origin = make_origin()
        code, out = run(capsys, "--version", "1.2.3", "--sha", origin.head.upper())
        assert code == 0, out
        assert out.strip() == f"Created v1.2.3 -> {origin.head}"
        assert origin.peeled() == origin.head

    def test_a_second_run_writes_nothing(self, make_origin, capsys) -> None:
        origin = make_origin()
        assert run(capsys, "--version", "1.2.3", "--sha", origin.head)[0] == 0
        created = origin.tags()
        code, out = run(capsys, "--version", "1.2.3", "--sha", origin.head)
        assert code == 0, out
        assert out.strip() == f"Already there: v1.2.3 -> {origin.head}"
        assert origin.tags() == created

    @pytest.mark.parametrize("annotated", [False, True], ids=["lightweight", "annotated"])
    def test_a_tag_already_on_this_commit_is_success(
        self, make_origin, capsys, annotated: bool
    ) -> None:
        origin = make_origin()
        origin.tag("v1.2.3", origin.head, annotated)
        before = origin.tags()
        code, out = run(capsys, "--version", "1.2.3", "--sha", origin.head)
        assert code == 0, out
        assert out.strip() == f"Already there: v1.2.3 -> {origin.head}"
        assert origin.tags() == before

    def test_a_tag_of_a_tag_of_this_commit_is_success(self, make_origin, capsys) -> None:
        origin = make_origin()
        origin.tag("inner", origin.head)
        origin.tag("v1.2.3", "refs/tags/inner")
        before = origin.tags()
        assert run(capsys, "--version", "1.2.3", "--sha", origin.head)[0] == 0
        assert origin.tags() == before

    @pytest.mark.parametrize("annotated", [False, True], ids=["lightweight", "annotated"])
    def test_a_tag_on_another_commit_fails_and_stays(
        self, make_origin, capsys, annotated: bool
    ) -> None:
        origin = make_origin()
        origin.tag("v1.2.3", origin.older, annotated)
        before = origin.tags()
        code, out = run(capsys, "--version", "1.2.3", "--sha", origin.head)
        assert code == 1, out
        assert out.startswith(f"::error::v1.2.3 on origin names {origin.older}, not {origin.head}")
        assert origin.tags() == before

    def test_a_tag_on_this_commits_tree_fails_and_stays(self, make_origin, capsys) -> None:
        origin = make_origin()
        origin.tag("v1.2.3", origin.head + "^{tree}")
        before = origin.tags()
        code, out = run(capsys, "--version", "1.2.3", "--sha", origin.head)
        assert code == 1, out
        assert origin.tags() == before

    def test_refs_that_only_end_like_the_tag_are_not_it(self, make_origin, capsys) -> None:
        origin = make_origin()
        _git(origin.bare, "update-ref", "refs/tags/old/refs/tags/v1.2.3", origin.older)
        origin.tag("v1.2.30", origin.older, annotated=False)
        others = origin.tags()
        code, out = run(capsys, "--version", "1.2.3", "--sha", origin.head)
        assert code == 0, out
        assert origin.peeled() == origin.head
        assert {ref: oid for ref, oid in origin.tags().items() if ref != TAG} == others


class TestRefusals:
    """Exit 2, an ``::error::`` line, and the origin as it was."""

    @staticmethod
    def refused(origin: Origin, capsys, *argv: str) -> str:
        before = origin.tags()
        code, out = run(capsys, *argv)
        assert code == 2, out
        assert out.startswith("::error::")
        assert origin.tags() == before
        return out

    def test_the_manifest_declares_another_version(self, make_origin, capsys) -> None:
        origin = make_origin(manifest("1.2.3"), manifest("1.2.4"))
        out = self.refused(origin, capsys, "--version", "1.2.3", "--sha", origin.head)
        assert f"pyproject.toml at {origin.head} declares ['1.2.4'], not ['1.2.3']" in out

    def test_the_manifest_declares_a_version_twice(self, make_origin, capsys) -> None:
        # The check job's sed would read two lines there, and refuse the run too.
        twice = manifest("1.2.3") + '\n[tool.other]\nversion = "1.2.3"\n'
        origin = make_origin(twice, twice)
        out = self.refused(origin, capsys, "--version", "1.2.3", "--sha", origin.head)
        assert "declares ['1.2.3', '1.2.3']" in out

    def test_the_manifest_has_no_version_line(self, make_origin, capsys) -> None:
        bare = '[project]\nname = "uvd-x402-sdk"\nversion="1.2.3"\n'
        origin = make_origin(bare, bare)
        out = self.refused(origin, capsys, "--version", "1.2.3", "--sha", origin.head)
        assert "declares []" in out

    @pytest.mark.parametrize(
        "version",
        [
            "1.2.3+local",
            "1!1.2.3",
            "v1.2.3",
            "1.2.3-rc1",
            "1.2.3RC1",
            "1.2.3 ",
            " 1.2.3",
            "1.2.3.",
            "1..2",
            "1.2.3/x",
            "\uff11.\uff12.\uff13",  # full-width digits: Unicode digits, not ASCII ones
            "",
        ],
    )
    def test_not_a_normalized_release_version(self, make_origin, capsys, version: str) -> None:
        # The manifest declares it too: only the version guard stands in the way.
        origin = make_origin(manifest(version), manifest(version))
        out = self.refused(origin, capsys, "--version", version, "--sha", origin.head)
        assert "is not a release version (no epoch, no local part)" in out

    @pytest.mark.parametrize(
        "sha",
        [
            lambda o: "HEAD",
            lambda o: "main",
            lambda o: "origin/main",
            lambda o: o.head[:12],
            lambda o: o.head + "0",
            lambda o: "0x" + o.head[:38],
            lambda o: " " + o.head,
            lambda o: o.head + "\n",
            lambda o: "",
        ],
        ids=["HEAD", "branch", "remote-branch", "short", "41", "0x", "space", "newline", "empty"],
    )
    def test_not_a_full_commit_id(self, make_origin, capsys, sha: Callable[[Origin], str]) -> None:
        origin = make_origin()
        out = self.refused(origin, capsys, "--version", "1.2.3", "--sha", sha(origin))
        assert "is not a full commit id" in out

    def test_no_sha_at_all(self, make_origin, capsys) -> None:
        origin = make_origin()
        assert "--sha '' is not a full commit id" in self.refused(
            origin, capsys, "--version", "1.2.3"
        )

    @pytest.mark.parametrize("which", ["older", "unknown"])
    def test_a_commit_the_checkout_does_not_hold(self, make_origin, capsys, which: str) -> None:
        # The depth-1 checkout holds the commit it was made for and nothing older.
        origin = make_origin()
        sha = origin.older if which == "older" else "0" * 40
        out = self.refused(origin, capsys, "--version", "1.2.3", "--sha", sha)
        assert "git rev-parse exited" in out

    def test_a_tree_is_not_a_commit(self, make_origin, capsys) -> None:
        origin = make_origin()
        tree = _git(origin.checkout, "rev-parse", "HEAD^{tree}")
        out = self.refused(origin, capsys, "--version", "1.2.3", "--sha", tree)
        assert "git rev-parse exited" in out


def _absent_at_first(module: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    """The first read finds no tag: someone pushes one before this run's push lands."""
    real = module.remote_target
    reads: list[str] = []

    def remote_target(remote: str, ref: str) -> str | None:
        reads.append(ref)
        return None if len(reads) == 1 else real(remote, ref)

    monkeypatch.setattr(module, "remote_target", remote_target)


def _push_answers(module: ModuleType, monkeypatch: pytest.MonkeyPatch, returncode: int) -> None:
    """``git push`` answers ``returncode`` and pushes nothing."""
    real = module._git

    def _git(*args: str, stdin: str | None = None) -> subprocess.CompletedProcess[bytes]:
        if args[0] == "push":
            refused = b"denied by the test"
            return subprocess.CompletedProcess(["git", *args], returncode, b"", refused)
        return real(*args, stdin=stdin)

    monkeypatch.setattr(module, "_git", _git)


class TestPushes:
    @pytest.mark.parametrize("annotated", [False, True], ids=["lightweight", "annotated"])
    def test_a_tag_pushed_meanwhile_on_this_commit_is_success(
        self, make_origin, capsys, monkeypatch, annotated: bool
    ) -> None:
        origin = make_origin()
        origin.tag("v1.2.3", origin.head, annotated)
        before = origin.tags()
        module = _load()
        _absent_at_first(module, monkeypatch)
        code, out = run(capsys, "--version", "1.2.3", "--sha", origin.head, module=module)
        assert code == 0, out
        assert out.strip() == f"Already there: v1.2.3 -> {origin.head}"
        assert origin.tags() == before

    @pytest.mark.parametrize("annotated", [False, True], ids=["lightweight", "annotated"])
    def test_a_tag_pushed_meanwhile_on_another_commit_fails_and_is_not_overwritten(
        self, make_origin, capsys, monkeypatch, annotated: bool
    ) -> None:
        origin = make_origin()
        origin.tag("v1.2.3", origin.older, annotated)
        before = origin.tags()
        module = _load()
        _absent_at_first(module, monkeypatch)
        code, out = run(capsys, "--version", "1.2.3", "--sha", origin.head, module=module)
        assert code == 1, out
        assert origin.tags() == before

    def test_a_refused_push_with_no_tag_there_cannot_run(
        self, make_origin, capsys, monkeypatch
    ) -> None:
        origin = make_origin()
        module = _load()
        _push_answers(module, monkeypatch, 1)
        code, out = run(capsys, "--version", "1.2.3", "--sha", origin.head, module=module)
        assert code == 2, out
        assert "origin has no refs/tags/v1.2.3 after the push (exit 1): denied by the test" in out
        assert origin.tags() == {}

    def test_a_push_that_says_ok_and_left_no_tag_is_not_success(
        self, make_origin, capsys, monkeypatch
    ) -> None:
        origin = make_origin()
        module = _load()
        _push_answers(module, monkeypatch, 0)
        code, out = run(capsys, "--version", "1.2.3", "--sha", origin.head, module=module)
        assert code == 2, out
        assert "after the push (exit 0)" in out
        assert origin.tags() == {}


class TestCommandLine:
    def test_as_publish_yml_runs_it(self, make_origin) -> None:
        origin = make_origin()
        done = subprocess.run(
            [sys.executable, str(SCRIPT), "--version", "1.2.3"],
            cwd=origin.checkout,
            env=dict(os.environ, GITHUB_SHA=origin.head),
            capture_output=True,
        )
        assert done.returncode == 0, done.stdout + done.stderr
        assert origin.peeled() == origin.head

    def test_without_a_version_it_does_not_run(self, make_origin) -> None:
        origin = make_origin()
        done = subprocess.run(
            [sys.executable, str(SCRIPT)],
            cwd=origin.checkout,
            env=dict(os.environ, GITHUB_SHA=origin.head),
            capture_output=True,
        )
        assert done.returncode == 2
        assert origin.tags() == {}


def test_ci_runs_these_tests_when_the_script_changes() -> None:
    # ci.yml runs only when a file it reads changes, and scripts/ is otherwise left out.
    ci = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8"))
    triggers = ci.get("on", ci.get(True))
    for event in ("push", "pull_request"):
        assert "scripts/release_tag.py" in triggers[event]["paths"], event


# ---------------------------------------------------------------------------------------------
# Each guard removed from the source turns its case into another verdict.


def _case_local_version(make_origin, capsys, monkeypatch, module) -> int:
    origin = make_origin(manifest("1.2.3+local"), manifest("1.2.3+local"))
    return run(capsys, "--version", "1.2.3+local", "--sha", origin.head, module=module)[0]


def _case_unicode_digits(make_origin, capsys, monkeypatch, module) -> int:
    version = "\uff11.\uff12.\uff13"
    origin = make_origin(manifest(version), manifest(version))
    return run(capsys, "--version", version, "--sha", origin.head, module=module)[0]


def _case_moving_name(make_origin, capsys, monkeypatch, module) -> int:
    make_origin()
    return run(capsys, "--version", "1.2.3", "--sha", "HEAD", module=module)[0]


def _case_other_manifest(make_origin, capsys, monkeypatch, module) -> int:
    origin = make_origin(manifest("1.2.3"), manifest("1.2.4"))
    return run(capsys, "--version", "1.2.3", "--sha", origin.head, module=module)[0]


def _case_manifest_twice(make_origin, capsys, monkeypatch, module) -> int:
    twice = manifest("1.2.3") + '\n[tool.other]\nversion = "1.2.3"\n'
    origin = make_origin(twice, twice)
    return run(capsys, "--version", "1.2.3", "--sha", origin.head, module=module)[0]


def _case_annotated_here(make_origin, capsys, monkeypatch, module) -> int:
    origin = make_origin()
    origin.tag("v1.2.3", origin.head)
    return run(capsys, "--version", "1.2.3", "--sha", origin.head, module=module)[0]


def _case_tail_ref_elsewhere(make_origin, capsys, monkeypatch, module) -> int:
    origin = make_origin()
    _git(origin.bare, "update-ref", "refs/tags/old/refs/tags/v1.2.3", origin.older)
    return run(capsys, "--version", "1.2.3", "--sha", origin.head, module=module)[0]


def _case_tag_elsewhere(make_origin, capsys, monkeypatch, module) -> int:
    origin = make_origin()
    origin.tag("v1.2.3", origin.older, annotated=False)
    return run(capsys, "--version", "1.2.3", "--sha", origin.head, module=module)[0]


def _case_push_says_ok(make_origin, capsys, monkeypatch, module) -> int:
    origin = make_origin()
    _push_answers(module, monkeypatch, 0)
    return run(capsys, "--version", "1.2.3", "--sha", origin.head, module=module)[0]


def _case_raced_here(make_origin, capsys, monkeypatch, module) -> int:
    origin = make_origin()
    origin.tag("v1.2.3", origin.head)
    _absent_at_first(module, monkeypatch)
    return run(capsys, "--version", "1.2.3", "--sha", origin.head, module=module)[0]


def _case_raced_elsewhere(make_origin, capsys, monkeypatch, module) -> int:
    origin = make_origin()
    origin.tag("v1.2.3", origin.older)
    _absent_at_first(module, monkeypatch)
    return run(capsys, "--version", "1.2.3", "--sha", origin.head, module=module)[0]


PUSH = '        push = _git("push", remote, f"{tag}:{ref}")\n'
READ_BACK = "        found = remote_target(remote, ref)\n"
VERSION_PATTERN = (
    'RELEASE_VERSION = re.compile(r"[0-9]+(\\.[0-9]+)*((a|b|rc)[0-9]+)?'
    '(\\.post[0-9]+)?(\\.dev[0-9]+)?")'
)

# name: (anchor, replacement, case, exit code with the guard, exit code without it)
MUTATIONS: dict[str, tuple[str, str, Callable[..., int], int, int]] = {
    "no version guard": (
        "    if not RELEASE_VERSION.fullmatch(version):\n",
        "    if False:\n",
        _case_local_version,
        2,
        0,
    ),
    "the version pattern takes any Unicode digit": (
        VERSION_PATTERN,
        VERSION_PATTERN.replace("[0-9]", "\\d"),
        _case_unicode_digits,
        2,
        0,
    ),
    "no full-commit-id guard": (
        "    if not FULL_SHA.fullmatch(sha):\n",
        "    if False:\n",
        _case_moving_name,
        2,
        0,
    ),
    "no manifest check": (
        "    if declared != [version]:\n",
        "    if False:\n",
        _case_other_manifest,
        2,
        0,
    ),
    "any line declaring it is enough": (
        "    if declared != [version]:\n",
        "    if version not in declared:\n",
        _case_manifest_twice,
        2,
        0,
    ),
    "the tag is not peeled": (
        "    return listed.get(peeled, listed.get(ref))\n",
        "    return listed.get(ref)\n",
        _case_annotated_here,
        0,
        1,
    ),
    "the peeled line is not asked for": (
        'git("ls-remote", "--tags", remote, ref, peeled)',
        'git("ls-remote", "--tags", remote, ref)',
        _case_annotated_here,
        0,
        1,
    ),
    "any ref that ls-remote lists is the tag": (
        "        listed[name] = oid\n",
        "        listed[ref] = oid\n",
        _case_tail_ref_elsewhere,
        0,
        1,
    ),
    "a tag on another commit is success": (
        "    if found != commit:\n",
        "    if False:\n",
        _case_tag_elsewhere,
        1,
        0,
    ),
    "the push's exit code is believed": (
        PUSH + READ_BACK,
        PUSH + "        found = commit if push.returncode == 0 else remote_target(remote, ref)\n",
        _case_push_says_ok,
        2,
        0,
    ),
    "no read-back after a refused push": (
        PUSH + READ_BACK,
        PUSH + "        found = remote_target(remote, ref) if push.returncode == 0 else None\n",
        _case_raced_here,
        0,
        2,
    ),
    "the push is forced": (
        'f"{tag}:{ref}"',
        'f"+{tag}:{ref}"',
        _case_raced_elsewhere,
        1,
        0,
    ),
}


class TestGuardMutations:
    @pytest.fixture
    def protocol(self) -> str:
        # Every mutation shows under either protocol; one of them is enough here.
        return "0"

    @pytest.mark.parametrize("name", sorted(MUTATIONS))
    def test_each_guard_removed_changes_its_verdict(
        self, make_origin, capsys, monkeypatch, name: str
    ) -> None:
        old, new, case, guarded, unguarded = MUTATIONS[name]
        assert SOURCE.count(old) == 1, f"the mutation's anchor is not in the script once: {old!r}"
        assert case(make_origin, capsys, monkeypatch, _load()) == guarded
        mutated = _load(SOURCE.replace(old, new))
        assert case(make_origin, capsys, monkeypatch, mutated) == unguarded
