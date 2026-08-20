"""Run the DX402 examples.

A fenced code block in a README rots in silence: nobody compiles it, nobody
imports it, and it keeps promising a function that was renamed three releases
ago. These are files, and CI runs them.

Not hypothetical. The seller half of this SDK was not importable from the
package root for several releases, and the only thing that surfaced it was
installing the published package into an empty venv.
"""

import pathlib
import runpy
import sys

import pytest

EXAMPLES = sorted((pathlib.Path(__file__).parent.parent / "examples" / "dx402").glob("*.py"))


def test_there_are_examples_at_all():
    assert EXAMPLES, "the DX402 examples directory is empty"


@pytest.mark.parametrize("path", EXAMPLES, ids=lambda p: p.stem)
def test_the_example_runs(path, capsys, monkeypatch):
    # `choose_storage` reaches a real facilitator on purpose. Offline it must
    # still exit cleanly -- discovery returns [] rather than raising, and the
    # example has to demonstrate that rather than crash.
    monkeypatch.setitem(sys.modules, "__main_example__", None)
    runpy.run_path(str(path), run_name="__main__")
    out = capsys.readouterr().out
    assert out.strip(), f"{path.name} produced no output"
    assert "BUG:" not in out, f"{path.name} reported a broken invariant:\n{out}"
