"""Install extras: what an extra promises, its dependencies have to deliver.

The suite itself cannot see a missing dependency in an extra: CI installs
``signer``, so ``eth-account`` is always present. This pins the declaration; the
end-to-end proof in a fresh venv is ``scripts/smoke_fastapi_extra.py``.
"""
import re
from pathlib import Path

import pytest

tomllib = pytest.importorskip("tomllib")

PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"


def _extra(name: str) -> set:
    extras = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"][
        "optional-dependencies"
    ]
    return {re.split(r"[<>=!~\[; ]", req, maxsplit=1)[0].lower() for req in extras[name]}


def test_the_fastapi_extra_brings_what_the_erc8128_verifier_imports():
    """``verify_request`` recovers the signer with ``eth_account``, imported lazily
    (``erc8128/verifier.py``, ``_recover``). Without it in the extra, a FastAPI
    server installed with ``[fastapi]`` imports fine and raises
    ``ModuleNotFoundError`` on the first signed request."""
    assert "eth-account" in _extra("fastapi")
