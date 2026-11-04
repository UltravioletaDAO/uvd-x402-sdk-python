"""Receipt success filtering, aligned with the TypeScript registry parser."""

import json
from pathlib import Path

import pytest

from uvd_x402_sdk import near_registry as r

NETWORK = "near:mainnet"
REGISTRY = "registry.arras-escrow.near"
MISSING = object()
FIXTURE = json.loads(
    (Path(__file__).parents[1] / "tests/fixtures/near-registry.json").read_text(encoding="utf-8")
)
EVENTS = [row["parsed"] for row in FIXTURE["events"]]
LOGS = [row["log"] for row in FIXTURE["events"]]


def receipt(status, logs=LOGS, executor=REGISTRY):
    outcome = {"executor_id": executor, "logs": logs}
    if status is not MISSING:
        outcome["status"] = status
    return {"outcome": outcome}


@pytest.mark.parametrize(
    "status",
    [
        pytest.param({"SuccessValue": ""}, id="empty-success-value"),
        pytest.param({"SuccessValue": "bnVsbA=="}, id="success-return-data"),
        pytest.param({"SuccessReceiptId": "1" * 32}, id="success-receipt-id"),
        pytest.param({"SuccessReceiptId": ""}, id="empty-success-receipt-id"),
        pytest.param({"SuccessValue": "not base64: é"}, id="opaque-success-string"),
        pytest.param({"SuccessValue": "", "SuccessReceiptId": "receipt"}, id="two-success-keys"),
        pytest.param({"SuccessValue": "", "SuccessReceiptId": None}, id="value-with-malformed-id"),
        pytest.param(
            {"SuccessValue": 1, "SuccessReceiptId": "receipt"}, id="id-with-malformed-value"
        ),
        pytest.param({"SuccessValue": "", "extra": True}, id="additional-fields"),
        pytest.param({"SuccessValue": "", "failure": {}}, id="failure-key-is-case-sensitive"),
    ],
)
def test_receipt_success_status_still_requires_registered_executor_and_network(status):
    assert r.parse_registry_receipt_events([receipt(status)], NETWORK) == EVENTS
    assert r.parse_registry_receipt_events([receipt(status, executor="other.near")], NETWORK) == []
    assert r.parse_registry_receipt_events([receipt(status)], "near:testnet") == []


REJECTED = [
    pytest.param(
        {
            "Failure": {
                "ActionError": {
                    "index": 0,
                    "kind": {
                        "LackBalanceForState": {
                            "account_id": REGISTRY,
                            "amount": "1",
                        }
                    },
                }
            }
        },
        id="failure-lack-balance-for-state",
    ),
    pytest.param(
        {
            "Failure": {
                "ActionError": {
                    "index": 0,
                    "kind": {
                        "FunctionCallError": {
                            "ExecutionError": "Exceeded the prepaid gas.",
                        }
                    },
                }
            }
        },
        id="failure-execution-error-after-emit",
    ),
    pytest.param("Unknown", id="unknown"),
    pytest.param(MISSING, id="missing"),
    pytest.param(None, id="null"),
    pytest.param({}, id="empty-object"),
    pytest.param([], id="empty-array"),
    pytest.param([{"SuccessValue": ""}], id="array-with-success"),
    pytest.param("SuccessValue", id="success-string"),
    pytest.param(1, id="number"),
    pytest.param(True, id="boolean"),
    pytest.param({"Unknown": ""}, id="unknown-variant"),
    *[
        pytest.param({key: ""}, id=f"wrong-key-{index}")
        for index, key in enumerate(
            [
                "successValue",
                "SUCCESSVALUE",
                "SuccessValue ",
                " SuccessValue",
                "SuccessValue\n",
                "SuccessReceiptID",
                "SuccessReceiptId ",
                "ＳuccessValue",
            ]
        )
    ],
    *[
        pytest.param({key: value}, id=f"{key}-non-string-{index}")
        for key in ("SuccessValue", "SuccessReceiptId")
        for index, value in enumerate([None, 1, 0, False, [], {}, [""], {"value": ""}])
    ],
    *[
        pytest.param({"Failure": failure, key: ""}, id=f"failure-wins-{key}-{index}")
        for key in ("SuccessValue", "SuccessReceiptId")
        for index, failure in enumerate([None, False, 0, "", [], {}, {"ExecutionError": "failed"}])
    ],
    pytest.param(
        {"Failure": None, "SuccessValue": "", "SuccessReceiptId": ""},
        id="failure-wins-over-both-success-keys",
    ),
]


@pytest.mark.parametrize("status", REJECTED)
def test_receipt_unsuccessful_status_discards_entire_receipt_and_preserves_other_receipts(status):
    bad = receipt(status)
    assert r.parse_registry_receipt_events([bad], NETWORK) == []
    before = receipt({"SuccessValue": ""}, [LOGS[1], LOGS[0]])
    after = receipt({"SuccessReceiptId": "receipt"}, [LOGS[2]])
    assert r.parse_registry_receipt_events([bad, before, bad, after, bad], NETWORK) == [
        EVENTS[1],
        EVENTS[0],
        EVENTS[2],
    ]


def test_receipt_failed_status_skips_log_parser(monkeypatch):
    def should_not_parse(logs):
        pytest.fail("logs of an unsuccessful receipt must never be parsed")

    monkeypatch.setattr(r, "parse_registry_events", should_not_parse)
    assert r.parse_registry_receipt_events([receipt({"Failure": None})], NETWORK) == []
