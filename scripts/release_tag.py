"""Tag the commit ``publish.yml`` just published as ``v<version>``, once.

The ``tag`` job of ``.github/workflows/publish.yml`` runs this after ``publish``
uploaded the release to PyPI, in a checkout of the commit that run built
(``GITHUB_SHA``). Until then nobody created the tag after a ``workflow_dispatch``
release, and whatever reads the highest ``v*`` tag as the published version saw
releases as missing.

    python3 scripts/release_tag.py --version 0.99.0 [--sha <commit>]

1. ``--sha`` (default ``$GITHUB_SHA``) must be a full commit id present in the
   checkout: a name such as ``HEAD`` or ``main`` is refused, it can move.
   The remote is the checkout's ``origin``.
2. ``--version`` must be a normalized PEP 440 public version (what PyPI files a
   release under: no epoch, no local part), and ``pyproject.toml`` at that
   commit must declare exactly it, read as the ``check`` job reads it (the one
   line ``version = "..."``).
3. The remote is asked for ``refs/tags/v<version>``, peeled (``git ls-remote``).
   Already there and naming this commit, lightweight or annotated, is success
   with nothing written. Naming anything else is a failure: the tag of a
   published version is never moved, and nothing here forces a push.
4. Otherwise an annotated tag is written (``git mktag``, no local ref) and
   pushed without force, and the remote is read back whether the push went
   through or not: a tag someone created meanwhile on this same commit is
   still success, one on another commit is still a failure.

Exit 0: the tag names this commit (created now, or before). 1: it names
something else. 2: could not run (input, version mismatch, git failed). Running
it again after any of them is safe; in Actions, "Re-run failed jobs" retries the
tag job alone, without publishing again.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import time
from collections.abc import Sequence

FULL_SHA = re.compile(r"[0-9a-fA-F]{40}")
# A normalized PEP 440 public version: ASCII digits, dots and the pre / post / dev markers only
# (`\d` would also take non-ASCII digits).
RELEASE_VERSION = re.compile(r"[0-9]+(\.[0-9]+)*((a|b|rc)[0-9]+)?(\.post[0-9]+)?(\.dev[0-9]+)?")
# The line the `check` job of publish.yml reads the package version from (with sed).
MANIFEST_VERSION = re.compile(r'^version = "(.*)"$', re.MULTILINE)
TAGGER = "github-actions[bot] <41898282+github-actions[bot]@users.noreply.github.com>"


class CannotRunError(Exception):
    """Something this run needs is missing or wrong: exit 2, nothing pushed."""


def _git(*args: str, stdin: str | None = None) -> subprocess.CompletedProcess[bytes]:
    # Bytes in and out: a text-mode pipe on Windows writes "\r\n", and mktag refuses it.
    return subprocess.run(
        ["git", *args],
        input=None if stdin is None else stdin.encode("utf-8"),
        capture_output=True,
    )


def git(*args: str, stdin: str | None = None) -> str:
    done = _git(*args, stdin=stdin)
    if done.returncode != 0:
        error = done.stderr.decode("utf-8", "replace").strip()
        raise CannotRunError(f"git {args[0]} exited {done.returncode}: {error}")
    return done.stdout.decode("utf-8", "replace").strip()


def remote_target(remote: str, ref: str) -> str | None:
    """The object ``ref`` finally names on ``remote`` (an annotated tag peeled), or None."""
    peeled = ref + "^{}"
    listed = {}
    for line in git("ls-remote", "--tags", remote, ref, peeled).splitlines():
        oid, _, name = line.partition("\t")
        listed[name] = oid
    return listed.get(peeled, listed.get(ref))


def _message(version: str) -> str:
    message = f"uvd-x402-sdk {version}\n"
    run = [os.environ.get(name, "") for name in ("GITHUB_SERVER_URL", "GITHUB_REPOSITORY")]
    run_id = os.environ.get("GITHUB_RUN_ID", "")
    if all(run) and run_id:
        message += f"\nPublished to PyPI by {run[0]}/{run[1]}/actions/runs/{run_id}\n"
    return message


def tag_release(version: str, sha: str, remote: str) -> int:
    if not RELEASE_VERSION.fullmatch(version):
        raise CannotRunError(f"{version!r} is not a normalized PEP 440 public version")
    if not FULL_SHA.fullmatch(sha):
        raise CannotRunError(f"--sha {sha!r} is not a full commit id")
    commit = git("rev-parse", "--verify", "--end-of-options", sha + "^{commit}")
    declared = MANIFEST_VERSION.findall(git("show", f"{commit}:pyproject.toml"))
    if declared != [version]:
        raise CannotRunError(f"pyproject.toml at {commit} declares {declared}, not [{version!r}]")

    name = "v" + version
    ref = "refs/tags/" + name
    found = remote_target(remote, ref)
    created = False
    if found is None:
        tag = git(
            "mktag",
            stdin=f"object {commit}\ntype commit\ntag {name}\n"
            f"tagger {TAGGER} {int(time.time())} +0000\n\n{_message(version)}",
        )
        push = _git("push", remote, f"{tag}:{ref}")
        found = remote_target(remote, ref)
        if found is None:
            error = push.stderr.decode("utf-8", "replace").strip()
            raise CannotRunError(
                f"{remote} has no {ref} after the push (exit {push.returncode}): {error}"
            )
        created = push.returncode == 0

    if found != commit:
        print(
            f"::error::{name} on {remote} names {found}, not {commit}, the commit this run "
            "published. The tag of a published version is never moved here: compare the "
            "two commits and decide by hand."
        )
        return 1
    print(f"{'Created' if created else 'Already there:'} {name} -> {commit}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Tag the commit publish.yml just published.")
    parser.add_argument("--version", required=True, help="the version publish.yml published")
    parser.add_argument("--sha", default=os.environ.get("GITHUB_SHA", ""), help="its commit")
    args = parser.parse_args(argv)
    try:
        return tag_release(args.version, args.sha, "origin")
    except CannotRunError as error:
        print(f"::error::{error}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
