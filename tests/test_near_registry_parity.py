"""The CI registry parity gate rejects byte drift, including formatting only."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from scripts import near_registry_parity as parity

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("suffix", [b"", b"\n", b" "])
def test_registry_fixture_requires_identical_bytes(tmp_path, suffix):
    fixture = tmp_path / "src/providers/near/vectors/near-registry.json"
    fixture.parent.mkdir(parents=True)
    fixture.write_bytes((ROOT / "tests/fixtures/near-registry.json").read_bytes() + suffix)
    if suffix:
        with pytest.raises(SystemExit, match="differs between the SDKs"):
            parity.check_parity(tmp_path)
    else:
        parity.check_parity(tmp_path)


def test_registry_parity_missing_fixture_is_an_error(tmp_path):
    with pytest.raises(FileNotFoundError):
        parity.check_parity(tmp_path)


def test_registry_parity_is_triggered_and_run_in_ci():
    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8"))
    for event in ("push", "pull_request"):
        assert "scripts/near_registry_parity.py" in workflow[True][event]["paths"]
    steps = workflow["jobs"]["cross-language"]["steps"]
    assert any(
        "python scripts/near_registry_parity.py --ts-root " in step.get("run", "") for step in steps
    )
