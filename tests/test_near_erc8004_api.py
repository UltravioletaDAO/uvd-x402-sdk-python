"""Public imports and executable integration guide, using recorded RPC responses."""

from __future__ import annotations

import json
import re
from pathlib import Path

import uvd_x402_sdk as sdk
from tests.test_near_erc8004 import FIXTURE, Signer
from uvd_x402_sdk import near_erc8004 as erc


def test_package_exports():
    for name in erc.__all__:
        assert name in sdk.__all__
        assert getattr(sdk, name) is getattr(erc, name)


def test_guide_flow_executes_offline(monkeypatch):
    cases = FIXTURE["networks"][0]["calls"]

    def rpc(self, method, params):
        assert method == "query"
        result = next(
            c["result"]
            for c in cases
            if c["method"] == params["method_name"] and c["contract"] == params["account_id"]
        )
        return {"result": {"result": list(json.dumps(result).encode())}}

    monkeypatch.setattr(erc.NearErc8004Client, "_http_rpc", rpc)
    scope = {}
    guide = (Path(__file__).parents[1] / "docs/near-erc8004.md").read_text(encoding="utf-8")
    for snippet in re.findall(r"```python\n(.*?)```", guide, re.S):
        exec(compile(snippet, "near-erc8004.md", "exec"), scope)
    registration = FIXTURE["networks"][0]["transactions"][0]["result"]
    tx = scope["transfer_registered_worker"](registration, "worker.near")
    assert tx["actions"][0]["params"]["args"] == {"receiver_id": "worker.near", "token_id": "0"}
    feedback = scope["rate_worker"]("0", "payment-id", "payer.near")
    assert feedback["actions"][0]["params"]["methodName"] == "give_feedback_with_proof"
    assert scope["sign_registration"](Signer(), "1", "1" * 32)


def test_the_unmutated_source_passes_through_the_mutation_child():
    # scripts/near_erc8004_mutations.py counts a mutant KILLED when its child sees a failure: a
    # test that fails with the source unmutated (the child swaps the module object) kills them all.
    # This file is not in the child's run, so it can start one.
    import os
    import subprocess
    import sys

    root = Path(__file__).parents[1]
    source = (root / "src/uvd_x402_sdk/near_erc8004.py").read_text(encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(root / "scripts/near_erc8004_mutations.py"), "--child"],
        input=json.dumps(["near_erc8004", source]),
        text=True,
        encoding="utf-8",
        capture_output=True,
        cwd=root,
        env={
            **os.environ,
            "HTTPS_PROXY": "http://127.0.0.1:9",
            "HTTP_PROXY": "http://127.0.0.1:9",
            "NO_PROXY": "127.0.0.1,localhost",
        },
        timeout=120,
    )
    assert result.returncode == 0, result.stdout[-2000:]
    assert " passed" in result.stdout and " failed" not in result.stdout
