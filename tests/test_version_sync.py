"""Guards against `__version__` drifting from the real package version.

v0.39.0 and v0.40.0 both shipped with `__version__` still reading "0.38.0",
because the release bumped `pyproject.toml` and nothing updated the constant in
`__init__.py`. Execution Market hit it and reported it as cosmetic; it is not.
Anything gating a feature on the version -- `__version__ >= "0.40.0"` to decide
whether the facilitator's `score` field is supported -- silently gets the wrong
answer and skips the feature.

The fix reads the version from installed package metadata, so the two cannot
disagree. These tests keep it that way.
"""

from __future__ import annotations

import re
from pathlib import Path

import uvd_x402_sdk

_INIT = Path(uvd_x402_sdk.__file__)


def test_version_is_not_hardcoded() -> None:
    """A literal assignment is exactly how the drift happened; keep it out."""
    source = _INIT.read_text(encoding="utf-8")

    hardcoded = re.search(
        r'^__version__\s*=\s*[\'"]\d+\.\d+', source, flags=re.MULTILINE
    )
    assert hardcoded is None, (
        "__version__ is assigned a version literal again. Derive it from package "
        "metadata instead, or it will drift from pyproject.toml on the next release."
    )


def test_version_is_resolvable_and_well_formed() -> None:
    resolved = uvd_x402_sdk.__version__

    assert isinstance(resolved, str) and resolved
    assert re.match(r"^\d+\.\d+", resolved), f"unexpected version: {resolved!r}"


def test_version_is_exported() -> None:
    assert "__version__" in uvd_x402_sdk.__all__
