"""Nothing the module logs identifies anyone: the operator streams live.

The whole rail runs (payment, prepare, a lost answer, reconcile, confirm) with
the module's logger at DEBUG, and no full address, hash, signature, proof or
URL may appear in any line. Plus the packaging promises: the extra exists,
and a base install of the SDK does not import this module.
"""

from __future__ import annotations

import logging
import re
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("eth_account")

from tests.reputation_doubles import SUBJECT, Env, make_entry  # noqa: E402
from uvd_x402_sdk.reputation import InMemoryRatingOutbox, PaymentRejected, redact  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
LONG_HEX = re.compile(r"[0-9a-fA-F]{40,}")


@pytest.mark.parametrize(
    "text,leak",
    [
        (
            "rater 0x19E7E376E7C213B7E7e7e46cc70A5dD086DAff2A ok",
            "19E7E376E7C213B7E7e7e46cc70A5dD086DAff2A",
        ),
        ("tx 0x" + "ab" * 32, "ab" * 32),
        ("bare " + "cd" * 32, "cd" * 32),
        ("sig 0x" + "ef" * 65, "ef" * 65),
        ("rpc https://base-mainnet.example/v2/KEY123 down", "KEY123"),
        ("rpc https://KEY456.quiknode.pro/ down", "KEY456"),
        ("upper 0X" + "AB" * 20, "AB" * 20),
    ],
)
def test_redact(text, leak):
    assert leak not in redact(text)


def test_short_values_are_kept():
    assert redact("chain 8453 status 0x1 code 429") == "chain 8453 status 0x1 code 429"


async def test_the_whole_rail_logs_nothing_identifying(caplog):
    caplog.set_level(logging.DEBUG, logger="uvd_x402_sdk.reputation")
    env = Env(InMemoryRatingOutbox())
    env.own()
    env.facilitator.submit_modes = ["lost"]
    entry = make_entry()
    env.outbox.enqueue(entry)
    row = await env.drive(entry.ref)
    assert row.status == "confirmed"
    env.facilitator.assert_consumed()
    secrets = {
        env.account.address,
        SUBJECT,
        row.rating_tx,
        row.signature,
        row.proof["transactionHash"],
        row.proof["paymentHash"],
        row.feedback_hash,
        row.nonce,
        row.ref,
        row.payment["raw"],
        "facilitator.test",
        "credits.example",
    }
    text = "\n".join(record.getMessage() for record in caplog.records)
    assert caplog.records, "the rail logged nothing: the test would prove nothing"
    for secret in secrets:
        body = secret[2:] if secret.startswith("0x") else secret
        assert body.lower() not in text.lower(), f"leaked: {secret[:10]}"
    assert not LONG_HEX.search(text)


def test_error_messages_are_redacted_too():
    error = PaymentRejected("payee_invalid", f"payee {SUBJECT} via https://rpc.example/KEY789")
    assert SUBJECT[2:] not in str(error) and "KEY789" not in str(error)
    assert error.code == "payee_invalid"


def test_the_extra_is_declared():
    text = (REPO / "pyproject.toml").read_text(encoding="utf-8")
    block = re.search(r"^reputation = \[(.*?)\]", text, re.S | re.M)
    assert block and '"eth-account>=0.13.0"' in block.group(1)


def _readme_blocks() -> list:
    text = (REPO / "src/uvd_x402_sdk/reputation/README.md").read_text(encoding="utf-8")
    return re.findall(r"```python\n(.*?)```", text, re.S)


def test_the_readme_program_is_fifteen_lines_of_real_api():
    import ast

    import uvd_x402_sdk.reputation as rep

    program = _readme_blocks()[0]
    assert len([line for line in program.splitlines() if line.strip()]) == 15
    for block in _readme_blocks():
        tree = compile(block, "README.md", "exec", flags=ast.PyCF_ONLY_AST)
        used = {
            node.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "rep"
        }
        assert used and used <= set(rep.__all__), used - set(rep.__all__)


def test_the_readme_calls_use_the_real_keyword_arguments():
    """Every ``rep.X(..., kw=...)`` of the README names parameters ``X`` has."""
    import ast
    import inspect

    import uvd_x402_sdk.reputation as rep

    checked = 0
    for block in _readme_blocks():
        tree = compile(block, "README.md", "exec", flags=ast.PyCF_ONLY_AST)
        for node in ast.walk(tree):
            func = node.func if isinstance(node, ast.Call) else None
            if (
                isinstance(func, ast.Attribute)
                and isinstance(func.value, ast.Name)
                and func.value.id == "rep"
            ):
                target = getattr(rep, func.attr)
                if callable(target):
                    names = set(inspect.signature(target).parameters)
                    used = {kw.arg for kw in node.keywords if kw.arg}
                    assert used <= names, (func.attr, used - names)
                    checked += 1
    assert checked >= 6


def test_the_installed_eth_account_is_the_one_the_extra_requires():
    """Not a skip: CI installs ``signer`` (eth-account>=0.11), and the module
    needs 0.13 (``unsafe_sign_hash``, ``sign_authorization``). If a resolver
    ever picks an older one, this is the line that says so."""
    from importlib.metadata import version

    major, minor = (int(part) for part in version("eth-account").split(".")[:2])
    assert (major, minor) >= (0, 13)


def test_a_base_import_of_the_sdk_does_not_load_the_module():
    code = "import sys, uvd_x402_sdk; print('uvd_x402_sdk.reputation' in sys.modules)"
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=120, cwd=str(REPO)
    )
    assert result.stdout.strip() == "False", result.stderr
