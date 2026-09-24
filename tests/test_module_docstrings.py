"""Every module of the package starts with its docstring.

``uvd_x402_sdk.erc8004`` had its imports above its docstring, which turns the
docstring into an ordinary expression: ``__doc__`` was None and ``help()``
showed nothing for the module.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

import uvd_x402_sdk.erc8004 as erc8004_module

PACKAGE = Path(__file__).resolve().parent.parent / "src" / "uvd_x402_sdk"
MODULES = sorted(PACKAGE.rglob("*.py"))


def test_the_erc8004_module_has_its_docstring() -> None:
    doc = erc8004_module.__doc__
    assert doc is not None
    assert doc.strip().startswith("ERC-8004 Trustless Agents client for x402 SDK.")


def test_the_package_has_modules() -> None:
    assert PACKAGE / "erc8004.py" in MODULES


@pytest.mark.parametrize("path", MODULES, ids=[p.relative_to(PACKAGE).as_posix() for p in MODULES])
def test_every_module_starts_with_its_docstring(path: Path) -> None:
    # Parsed, not imported: some modules need an optional extra to import.
    assert ast.get_docstring(ast.parse(path.read_text(encoding="utf-8")))
