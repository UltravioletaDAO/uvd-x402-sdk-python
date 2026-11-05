"""Recorded RPC replay, strict views, receipt provenance and result semantics."""

from __future__ import annotations

import base64
import copy
import json

import pytest

from tests.test_near_erc8004 import FIXTURE, NETWORKS
from uvd_x402_sdk import near_erc8004 as erc


def envelope(value):
    return {"result": {"result": list(json.dumps(value, ensure_ascii=False).encode())}}


@pytest.mark.parametrize("row", FIXTURE["networks"], ids=NETWORKS)
def test_recorded_views_and_six_transaction_results(row):
    network = row["network"]
    for case in row["calls"]:

        def rpc(method, params):
            assert method == "query"
            assert params == {
                "request_type": "call_function",
                "finality": "final",
                "account_id": case["contract"],
                "method_name": case["method"],
                "args_base64": base64.b64encode(case["args_json"].encode()).decode(),
            }
            assert case["args_json"].encode().hex() == case["args_hex"]
            return envelope(case["result"])

        client = erc.NearErc8004Client(network, rpc=rpc)
        role = next(
            k for k, v in erc.NEAR_ERC8004_CONTRACTS[network].items() if v == case["contract"]
        )
        if case["method"] == "get_config":
            assert client.get_config(role).data["contract"] == case["result"]["contract"]
        else:
            assert getattr(client, case["method"])(**case["args"]) == case["result"]
    for case, expected in zip(row["transactions"], ["0", None, "1"]):
        outcome = erc.parse_erc8004_outcome(case["result"], network=network, method=case["method"])
        assert outcome.status == "accepted" and outcome.value == expected
        events = erc.parse_erc8004_receipt_events(case["result"]["receipts_outcome"], network)
        assert [e["event"] for e in events] == {
            "register": ["nft_mint", "registered", "metadata_set"],
            "nft_transfer": ["metadata_set", "nft_transfer"],
            "give_feedback_with_proof": ["new_feedback"],
        }[case["method"]]


VIEWS = [
    ("identity", "nft_tokens", {"from_index": "0", "limit": 1}, []),
    ("identity", "nft_supply_for_owner", {"account_id": "worker.near"}, "0"),
    (
        "identity",
        "nft_tokens_for_owner",
        {"account_id": "worker.near", "from_index": "0", "limit": 1},
        [],
    ),
    ("identity", "nft_total_supply", {}, "1"),
    (
        "identity",
        "nft_metadata",
        {},
        {
            "spec": "nft-1.0.0",
            "name": "Agents",
            "symbol": "AGENT",
            "icon": None,
            "base_uri": None,
            "reference": None,
            "reference_hash": None,
        },
    ),
    (
        "identity",
        "nft_is_approved",
        {"token_id": "0", "approved_account_id": "worker.near", "approval_id": 0},
        False,
    ),
    ("identity", "token_uri", {"agent_id": "0"}, ""),
    (
        "identity",
        "get_metadata",
        {"agent_id": "0", "metadata_key": "agentWallet"},
        "d29ya2VyLm5lYXI=",
    ),
    ("identity", "get_agent_wallet", {"agent_id": "0"}, "worker.near"),
    (
        "identity",
        "get_pending_agent_wallet",
        {"agent_id": "0"},
        {"wallet": "worker.near", "deadline": "1800000000000000000"},
    ),
    ("identity", "is_authorized_or_owner", {"spender": "worker.near", "agent_id": "0"}, True),
    (
        "identity",
        "check_agent",
        {"agent_id": "0", "account_id": "worker.near"},
        {"owner_id": "owner.near", "agent_wallet": None, "authorized": False},
    ),
    ("identity", "get_version", {}, "2.0.0"),
    (
        "reputation",
        "get_summary",
        {"agent_id": "0", "client_addresses": ["payer.near"], "tag1": "quality"},
        {
            "count": "3",
            "summary_value": "333333333333333333333333333333333333334",
            "summary_value_decimals": 1,
        },
    ),
    (
        "reputation",
        "get_response_count",
        {"agent_id": "0", "client_address": "payer.near", "feedback_index": "0", "responders": []},
        "1",
    ),
    ("reputation", "get_clients", {"agent_id": "0", "from_index": 0, "limit": 100}, ["payer.near"]),
    ("reputation", "get_last_index", {"agent_id": "0", "client_address": "payer.near"}, "1"),
    ("reputation", "get_identity_registry", {}, "identity-registry.erc-8004.near"),
    ("reputation", "get_version", {}, "2.0.0"),
    (
        "reputation",
        "is_proof_pending",
        {"verifier": "verifier.near", "proof": "id", "client_address": "payer.near"},
        False,
    ),
]


@pytest.mark.parametrize("role,method,args,result", VIEWS)
def test_every_other_view_named_method(role, method, args, result):
    def rpc(rpc_method, params):
        assert rpc_method == "query"
        assert params["account_id"] == erc.NEAR_ERC8004_CONTRACTS["near:mainnet"][role]
        assert params["method_name"] == method
        assert json.loads(base64.b64decode(params["args_base64"])) == args
        return envelope(result)

    client = erc.NearErc8004Client(rpc=rpc)
    if method == "get_version":
        assert client.get_version(role) == result
    else:
        assert getattr(client, method)(**args) == result


@pytest.mark.parametrize("role,method,args,result", VIEWS)
def test_view_shapes_reject_foreign_types(role, method, args, result):
    for bad in [42, 1.5, {"unexpected": "field"}]:
        client = erc.NearErc8004Client(rpc=lambda m, p: envelope(bad))
        with pytest.raises(ValueError):
            client.view(role, method, **args)


@pytest.mark.parametrize("bad", ["01", "+1", "1\n", "-0", 1, True, str(10**56)])
def test_summary_exact_decimal_guard(bad):
    result = {"count": "1", "summary_value": bad, "summary_value_decimals": 0}
    client = erc.NearErc8004Client(rpc=lambda m, p: envelope(result))
    with pytest.raises(ValueError):
        client.get_summary("0", ["payer.near"])


@pytest.mark.parametrize(
    "case", [c for n in FIXTURE["networks"][:1] for c in n["calls"] if c["method"] != "get_config"]
)
def test_recorded_view_required_fields(case):
    result = case["result"]
    role = "identity" if case["method"] == "nft_token" else "reputation"
    candidates = []
    if isinstance(result, dict):
        for key in result:
            candidates.append({k: v for k, v in result.items() if k != key})
            candidates.append({**result, key: None})
    else:
        candidates.append([{}])
    for bad in candidates:
        client = erc.NearErc8004Client(rpc=lambda m, p: envelope(bad))
        with pytest.raises(ValueError):
            client.view(role, case["method"], **case["args"])


def test_parallel_feedback_arrays_and_result_binding():
    case = next(c for c in FIXTURE["networks"][0]["calls"] if c["method"] == "read_all_feedback")
    result = copy.deepcopy(case["result"])
    result["values"].append("1")
    client = erc.NearErc8004Client(rpc=lambda m, p: envelope(result))
    with pytest.raises(ValueError):
        client.read_all_feedback("0")
    result = "identity-registry.erc-8004.testnet"
    with pytest.raises(ValueError):
        client.get_identity_registry()
    case = next(c for c in FIXTURE["networks"][0]["calls"] if c["method"] == "nft_token")
    result = {**case["result"], "token_id": "1"}
    with pytest.raises(ValueError):
        client.nft_token("0")


def test_invalid_view_args_never_call_transport():
    def rpc(*args):
        pytest.fail("invalid view reached RPC")

    client = erc.NearErc8004Client(rpc=rpc)
    calls = [
        lambda: client.get_summary("0", []),
        lambda: client.get_agent("01"),
        lambda: client.nft_tokens(from_index=1),
        lambda: client.get_clients("0", from_index="0"),
        lambda: client.nft_tokens(limit=101),
        lambda: client.nft_tokens(limit=True),
        lambda: client.get_verified_summary("0", "verifier.near", context="EM"),
        lambda: client.view("verifier", "verify_feedback_proof", client="payer.near", proof="p"),
        lambda: client.view("validation", "get_summary", agent_id="0"),
        lambda: client.view("identity", "set_agent_uri", agent_id="0", new_uri=""),
        lambda: client.read_feedback("0", "payer.near", "01"),
    ]
    for call in calls:
        with pytest.raises(ValueError):
            call()


@pytest.mark.parametrize(
    "response",
    [
        None,
        {},
        {"error": None},
        {"result": None},
        {"result": {"error": "failed"}},
        {"result": {"result": "bytes"}},
        {"result": {"result": [True]}},
        {"result": {"result": [-1]}},
        {"result": {"result": [256]}},
        {"result": {"result": [123]}},
        {"result": {"result": list(b"[" * 65 + b"]" * 65)}},
    ],
)
def test_rpc_decode_bounds(response):
    client = erc.NearErc8004Client(rpc=lambda m, p: response)
    with pytest.raises(ValueError):
        client.nft_total_supply()


def test_large_rpc_result_rejected():
    client = erc.NearErc8004Client(
        rpc=lambda m, p: {"result": {"result": [32] * (erc.MAX_VIEW_BYTES + 1)}}
    )
    with pytest.raises(ValueError):
        client.nft_total_supply()


def event(name, data, standard="erc8004"):
    return {"standard": standard, "version": "1.0.0", "event": name, "data": [data]}


EVENTS = [
    ("identity", event("nft_mint", {"owner_id": "payer.near", "token_ids": ["0"]}, "nep171")),
    (
        "identity",
        event(
            "nft_transfer",
            {
                "old_owner_id": "payer.near",
                "new_owner_id": "worker.near",
                "token_ids": ["0"],
                "memo": "transfer",
            },
            "nep171",
        ),
    ),
    (
        "identity",
        event("registered", {"agent_id": "0", "agent_uri": "ipfs://card", "owner": "payer.near"}),
    ),
    (
        "identity",
        event(
            "metadata_set", {"agent_id": "0", "metadata_key": "agentWallet", "metadata_value": ""}
        ),
    ),
    (
        "identity",
        event("uri_updated", {"agent_id": "0", "new_uri": "", "updated_by": "payer.near"}),
    ),
    (
        "identity",
        event(
            "agent_wallet_pending",
            {"agent_id": "0", "wallet": "worker.near", "deadline": "1800000000000000000"},
        ),
    ),
    ("identity", event("agent_wallet_pending", {"agent_id": "0", "wallet": None, "deadline": "0"})),
    (
        "reputation",
        event(
            "new_feedback",
            {
                "agent_id": "0",
                "client_address": "payer.near",
                "feedback_index": "1",
                "value": "-32",
                "value_decimals": 1,
                "tag1": "ñ",
                "tag2": "",
                "endpoint": "",
                "feedback_uri": "",
                "feedback_hash": None,
                "proof": None,
            },
        ),
    ),
    (
        "reputation",
        event(
            "feedback_revoked",
            {"agent_id": "0", "client_address": "payer.near", "feedback_index": "1"},
        ),
    ),
    (
        "reputation",
        event(
            "response_appended",
            {
                "agent_id": "0",
                "client_address": "payer.near",
                "feedback_index": "1",
                "responder": "worker.near",
                "response_uri": "ipfs://response",
                "response_hash": "ab" * 32,
            },
        ),
    ),
]


def receipt(role, evt, network="near:mainnet", status=None):
    return {
        "outcome": {
            "executor_id": erc.NEAR_ERC8004_CONTRACTS[network][role],
            "status": {"SuccessValue": ""} if status is None else status,
            "logs": ["EVENT_JSON:" + json.dumps(evt, ensure_ascii=False, separators=(",", ":"))],
        }
    }


@pytest.mark.parametrize("network", NETWORKS)
@pytest.mark.parametrize("role,evt", EVENTS)
def test_every_event_and_provenance_guard(network, role, evt):
    item = receipt(role, evt, network)
    assert erc.parse_erc8004_receipt_events([item], network) == [evt]
    foreign = copy.deepcopy(item)
    for emitter in [
        "foreign.near",
        "identity-registry.erc-8004.near.evil",
        erc.NEAR_ERC8004_CONTRACTS["near:testnet" if network == "near:mainnet" else "near:mainnet"][
            role
        ],
        erc.NEAR_ERC8004_CONTRACTS[network]["reputation" if role == "identity" else "identity"],
    ]:
        foreign["outcome"]["executor_id"] = emitter
        assert erc.parse_erc8004_receipt_events([foreign], network) == []
    for field in evt["data"][0]:
        invalid = copy.deepcopy(evt)
        invalid["data"][0][field] = {}
        assert erc.parse_erc8004_receipt_events([receipt(role, invalid, network)], network) == []


@pytest.mark.parametrize("role,evt", EVENTS)
@pytest.mark.parametrize(
    "status",
    [
        {},
        {"Failure": {}},
        {"Failure": None},
        {"Failure": False},
        {"SuccessValue": "", "Failure": None},
        {"SuccessReceiptId": "id", "Failure": False},
        {"SuccessValue": None},
        {"SuccessValue": True},
        {"SuccessReceiptId": 1},
        "SuccessValue",
        "Unknown",
    ],
)
def test_receipt_success_guard(role, evt, status):
    assert (
        erc.parse_erc8004_receipt_events([receipt(role, evt, status=status)], "near:mainnet") == []
    )


@pytest.mark.parametrize("role,evt", EVENTS)
def test_event_envelope_strictness(role, evt):
    mutations = [
        {"standard": "foreign"},
        {"version": "1.0.1"},
        {"event": "validation_request"},
        {"data": []},
        {"data": evt["data"] * 2},
        {"data": [None]},
    ]
    for mutation in mutations:
        assert (
            erc.parse_erc8004_receipt_events([receipt(role, {**evt, **mutation})], "near:mainnet")
            == []
        )


def test_event_integer_hash_and_wallet_content():
    evt = copy.deepcopy(EVENTS[2][1])
    evt["data"][0]["agent_id"] = "01"
    assert erc.parse_erc8004_receipt_events([receipt("identity", evt)], "near:mainnet") == []
    evt = copy.deepcopy(EVENTS[-1][1])
    evt["data"][0]["response_hash"] = "AB" * 32
    assert erc.parse_erc8004_receipt_events([receipt("reputation", evt)], "near:mainnet") == []
    evt = copy.deepcopy(EVENTS[3][1])
    evt["data"][0]["metadata_value"] = base64.b64encode(b"INVALID").decode()
    assert erc.parse_erc8004_receipt_events([receipt("identity", evt)], "near:mainnet") == []
    evt = copy.deepcopy(EVENTS[5][1])
    evt["data"][0]["wallet"] = None
    assert erc.parse_erc8004_receipt_events([receipt("identity", evt)], "near:mainnet") == []
    assert erc.parse_erc8004_receipt_events([None, {}, {"outcome": {}}], "near:mainnet") == []
    with pytest.raises(ValueError):
        erc.parse_erc8004_receipt_events([], "near:invalid")


def feedback_outcome():
    return copy.deepcopy(FIXTURE["networks"][0]["transactions"][2]["result"])


def parse(outcome, method="give_feedback_with_proof", network="near:mainnet"):
    return erc.parse_erc8004_outcome(outcome, network=network, method=method)


@pytest.mark.parametrize("code", list(erc.Erc8004Rejection))
def test_callback_rejections_only_from_successful_reputation_receipts(code):
    outcome = feedback_outcome()
    outcome["status"] = {"SuccessValue": base64.b64encode(b"null").decode()}
    item = {
        "outcome": {
            "executor_id": erc.NEAR_ERC8004_CONTRACTS["near:mainnet"]["reputation"],
            "status": {"SuccessValue": "bnVsbA=="},
            "logs": ["erc8004:rechazo:" + code.value],
        }
    }
    outcome["receipts_outcome"] = [item]
    assert parse(outcome).rejection == code
    assert parse(outcome).status == "rejected"
    for bad in [
        "foreign.near",
        erc.NEAR_ERC8004_CONTRACTS["near:mainnet"]["identity"],
        erc.NEAR_ERC8004_CONTRACTS["near:testnet"]["reputation"],
    ]:
        item["outcome"]["executor_id"] = bad
        with pytest.raises(ValueError):
            parse(outcome)
    outcome["logs"] = item["outcome"]["logs"]
    outcome["receipts_outcome"] = []
    with pytest.raises(ValueError):
        parse(outcome)


@pytest.mark.parametrize("value", ["01", 1, True, "0", "-1", "1\n", {}, None])
def test_feedback_result_integer_guard(value):
    outcome = feedback_outcome()
    outcome["status"] = {"SuccessValue": base64.b64encode(json.dumps(value).encode()).decode()}
    with pytest.raises(ValueError):
        parse(outcome)


@pytest.mark.parametrize(
    "bad",
    [
        {},
        {"SuccessReceiptId": "id"},
        {"SuccessValue": None},
        {"SuccessValue": "IjEi", "Failure": None},
        {"SuccessValue": "***"},
        {"SuccessValue": ""},
        {"SuccessValue": "IjEifQ=="},
        {"SuccessValue": "IjEi\n"},
        {"SuccessValue": "A" * (erc.MAX_VIEW_BYTES + 1)},
    ],
)
def test_final_outcome_shape_guard(bad):
    outcome = feedback_outcome()
    outcome["status"] = bad
    with pytest.raises(ValueError):
        parse(outcome)


def test_outcome_is_bound_to_network_method_and_matching_event():
    outcome = feedback_outcome()
    with pytest.raises(ValueError):
        parse(outcome, network="near:testnet")
    with pytest.raises(ValueError):
        parse(outcome, method="register")
    outcome["receipts_outcome"] = []
    with pytest.raises(ValueError):
        parse(outcome)
    for mutation in ["identifier", "signer", "emitter", "failure"]:
        outcome = feedback_outcome()
        for r in outcome["receipts_outcome"]:
            o = r["outcome"]
            if o["executor_id"] == erc.NEAR_ERC8004_CONTRACTS["near:mainnet"]["reputation"]:
                if mutation == "identifier":
                    o["logs"] = [
                        s.replace('"feedback_index":"1"', '"feedback_index":"2"') for s in o["logs"]
                    ]
                if mutation == "emitter":
                    o["executor_id"] = "foreign.near"
                if mutation == "failure":
                    o["status"] = {"Failure": None}
        if mutation == "signer":
            outcome["transaction"]["signer_id"] = "other.near"
        with pytest.raises(ValueError):
            parse(outcome)


@pytest.mark.parametrize("code", list(erc.Erc8004PanicCode))
def test_known_panics_are_errors_not_successes(code):
    outcome = feedback_outcome()
    outcome["status"] = {
        "Failure": {
            "ActionError": {
                "kind": {"FunctionCallError": {"ExecutionError": code.value + ": failed"}}
            }
        }
    }
    assert parse(outcome).panic == code and parse(outcome).status == "error"


def test_feedback_result_agent_and_proof_match_transaction():
    for key, bad in [("agent_id", "99"), ("verifier", "other.near"), ("proof", "other-proof")]:
        outcome = feedback_outcome()
        for r in outcome["receipts_outcome"]:
            o = r["outcome"]
            for i, log in enumerate(o["logs"]):
                if log.startswith("EVENT_JSON:"):
                    ev = json.loads(log[11:])
                    if ev["event"] == "new_feedback":
                        data = ev["data"][0]
                        if key == "agent_id":
                            data[key] = bad
                        else:
                            data["proof"][key] = bad
                        o["logs"][i] = "EVENT_JSON:" + json.dumps(ev)
        with pytest.raises(ValueError):
            parse(outcome)


def test_http_transport_bounds_before_json_and_no_other_methods(monkeypatch):
    import httpx

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def raise_for_status(self):
            pass

        def iter_bytes(self):
            for _ in range(5):
                yield b" " * erc.MAX_VIEW_BYTES
            pytest.fail("unbounded HTTP body consumption")

    monkeypatch.setattr(httpx, "stream", lambda *a, **k: Response())
    with pytest.raises(ValueError, match="too large"):
        erc.NearErc8004Client().nft_total_supply()


@pytest.mark.parametrize("role", ["validation", None, []])
def test_config_role_never_reaches_rpc(role):
    def rpc(*args):
        pytest.fail("invalid role reached RPC")

    with pytest.raises(ValueError, match="unknown contract role"):
        erc.NearErc8004Client(rpc=rpc).get_config(role)


def test_proof_ref_and_list_ref_binding():
    result = {"agent_id": "0", "client_address": "other.near", "feedback_index": "1"}
    client = erc.NearErc8004Client(rpc=lambda m, p: envelope(result))
    with pytest.raises(ValueError):
        client.get_proof_feedback("verifier.near", "proof", "payer.near")
    result = [{"agent_id": "1", "client_address": "payer.near", "feedback_index": "1"}]
    with pytest.raises(ValueError):
        client.list_verified_feedback("0", "verifier.near")


@pytest.mark.parametrize(
    "outcome",
    [
        None,
        [],
        1,
        {"result": None},
        {"result": []},
        {"result": {}, "error": None},
        {"status": None},
    ],
)
def test_invalid_outcome_types(outcome):
    with pytest.raises(ValueError):
        parse(outcome)


def test_invalid_outcome_method_and_error_envelope():
    outcome = feedback_outcome()
    for method in [None, [], "validation_request"]:
        with pytest.raises(ValueError):
            parse(outcome, method=method)
    with pytest.raises(ValueError):
        parse({"error": None, "result": outcome})
    outcome["status"] = None
    with pytest.raises(ValueError):
        parse(outcome)
    outcome["status"] = {"Failure": {"ExecutionError": "unknown panic"}}
    with pytest.raises(ValueError):
        parse(outcome)


def rejected_outcome():
    outcome = feedback_outcome()
    outcome["status"] = {"SuccessValue": "bnVsbA=="}
    outcome["receipts_outcome"] = [
        {
            "outcome": {
                "executor_id": erc.NEAR_ERC8004_CONTRACTS["near:mainnet"]["reputation"],
                "status": {"SuccessValue": "bnVsbA=="},
                "logs": ["erc8004:rechazo:" + erc.Erc8004Rejection.E23.value],
            }
        }
    ]
    return outcome


def test_null_requires_canonical_base64_and_actual_null_bytes():
    for bad in ["bnVsbB==", ""]:
        outcome = rejected_outcome()
        outcome["status"]["SuccessValue"] = bad
        with pytest.raises(ValueError):
            parse(outcome)


def test_transaction_args_decode_and_size_guard():
    outcome = feedback_outcome()
    call = outcome["transaction"]["actions"][0]["FunctionCall"]
    original = base64.b64decode(call["args"])
    for bad in [
        None,
        [],
        "!",
        base64.b64encode(b" " * 400000 + original).decode(),
        base64.b64encode(b"{").decode(),
    ]:
        call["args"] = bad
        with pytest.raises(ValueError):
            parse(outcome)


@pytest.mark.parametrize(
    "method,good,bad_values,role",
    [
        ("set_agent_wallet", False, [0, "false", None], "identity"),
        ("nft_transfer_call", True, [1, "true", None], "identity"),
        (
            "verify_feedback_proof",
            {"counterparty": "worker.near", "role": "receiver", "context": "em", "weight": "1"},
            [
                {},
                {
                    "counterparty": "worker.near",
                    "role": "receiver",
                    "context": "em",
                    "weight": "01",
                },
            ],
            "verifier",
        ),
        ("nft_transfer", None, [True, "0"], "identity"),
    ],
)
def test_other_method_results(method, good, bad_values, role):
    outcome = feedback_outcome()
    outcome["transaction"]["receiver_id"] = erc.NEAR_ERC8004_CONTRACTS["near:mainnet"][role]
    outcome["transaction"]["actions"][0]["FunctionCall"]["method_name"] = method
    for value in [good, *bad_values]:
        outcome["status"] = {"SuccessValue": base64.b64encode(json.dumps(value).encode()).decode()}
        if value is good:
            assert parse(outcome, method).value == good
        else:
            with pytest.raises(ValueError):
                parse(outcome, method)


def test_transaction_receiver_and_method_are_checked_independently():
    outcome = feedback_outcome()
    outcome["transaction"]["receiver_id"] = "foreign.near"
    with pytest.raises(ValueError):
        parse(outcome)
    outcome = feedback_outcome()
    outcome["transaction"]["actions"][0]["FunctionCall"]["method_name"] = "give_feedback"
    with pytest.raises(ValueError):
        parse(outcome)


def test_large_but_valid_success_json_is_rejected_before_decoding():
    outcome = feedback_outcome()
    outcome["status"]["SuccessValue"] = base64.b64encode(b" " * 400000 + b'"1"').decode()
    with pytest.raises(ValueError, match="outcome too large"):
        parse(outcome)


def test_identifier_error_is_distinct_from_missing_event():
    outcome = feedback_outcome()
    outcome["status"]["SuccessValue"] = base64.b64encode(b'"01"').decode()
    with pytest.raises(ValueError, match="invalid result identifier"):
        parse(outcome)


def test_agent_wallet_metadata_is_a_near_account():
    for value in [
        base64.b64encode(b"INVALID").decode(),
        base64.b64encode(bytes([255])).decode(),
        "",
    ]:
        client = erc.NearErc8004Client(rpc=lambda m, p: envelope(value))
        with pytest.raises(ValueError):
            client.get_metadata("0", "agentWallet")


@pytest.mark.parametrize("row", FIXTURE["networks"], ids=NETWORKS)
@pytest.mark.parametrize("foreign", ["other.near", "other_network"])
def test_foreign_proof_verifier_cannot_be_a_verified_event_or_outcome(row, foreign):
    network = row["network"]
    if foreign == "other_network":
        other = "near:testnet" if network == "near:mainnet" else "near:mainnet"
        foreign = erc.NEAR_ERC8004_CONTRACTS[other]["verifier"]
    outcome = copy.deepcopy(row["transactions"][2]["result"])
    assert parse(outcome, network=network).status == "accepted"
    call = outcome["transaction"]["actions"][0]["FunctionCall"]
    args = json.loads(base64.b64decode(call["args"]))
    args["verifier"] = foreign
    call["args"] = base64.b64encode(json.dumps(args).encode()).decode()
    for item in outcome["receipts_outcome"]:
        logs = item["outcome"]["logs"]
        for i, log in enumerate(logs):
            if log.startswith("EVENT_JSON:"):
                evt = json.loads(log[11:])
                if evt["event"] == "new_feedback":
                    evt["data"][0]["proof"]["verifier"] = foreign
                    logs[i] = "EVENT_JSON:" + json.dumps(evt)
    assert erc.parse_erc8004_receipt_events(outcome["receipts_outcome"], network) == []
    # The transaction and event agree with EACH OTHER; the network must still refuse them.
    with pytest.raises(ValueError, match="matching receipt event"):
        parse(outcome, network=network)


@pytest.mark.parametrize(
    "network,url",
    [
        ("near:mainnet", "https://rpc.mainnet.near.org"),
        ("near:testnet", "https://rpc.testnet.near.org"),
    ],
)
def test_http_rpc_uses_selected_network(monkeypatch, network, url):
    import httpx

    seen = []

    def stream(method, actual_url, **kwargs):
        seen.append((method, actual_url, kwargs))
        return httpx.Response(200, json=envelope("1"), request=httpx.Request(method, actual_url))

    # httpx.Response is not a context manager; expose the same context API as stream().
    from contextlib import contextmanager

    @contextmanager
    def response(*args, **kwargs):
        yield stream(*args, **kwargs)

    monkeypatch.setattr(httpx, "stream", response)
    assert erc.NearErc8004Client(network).nft_total_supply() == "1"
    assert len(seen) == 1
    method, actual_url, options = seen[0]
    assert (method, actual_url) == ("POST", url)
    assert options["json"]["method"] == "query"
    assert (
        options["json"]["params"]["account_id"] == erc.NEAR_ERC8004_CONTRACTS[network]["identity"]
    )


@pytest.mark.parametrize("split_receipts", [False, True])
def test_null_feedback_rejects_distinct_rejection_codes(split_receipts):
    outcome = rejected_outcome()
    first = outcome["receipts_outcome"][0]
    second_log = "erc8004:rechazo:" + erc.Erc8004Rejection.E45.value
    # Duplicating one recognized code is unambiguous and remains accepted as a rejection.
    first["outcome"]["logs"] *= 2
    assert parse(outcome).rejection == erc.Erc8004Rejection.E23
    if split_receipts:
        second = copy.deepcopy(first)
        second["outcome"]["logs"] = [second_log]
        outcome["receipts_outcome"].append(second)
    else:
        first["outcome"]["logs"].append(second_log)
    with pytest.raises(ValueError, match="one recognized rejection"):
        parse(outcome)


@pytest.mark.parametrize("method", ["list_verified_feedback", "get_proof_feedback"])
def test_view_feedback_reference_index_must_be_positive(method):
    ref = {"agent_id": "0", "client_address": "payer.near", "feedback_index": "1"}

    def rpc(*unused):
        return envelope([ref] if method == "list_verified_feedback" else ref)

    client = erc.NearErc8004Client(rpc=rpc)
    args = (
        ("0", "verifier.near")
        if method == "list_verified_feedback"
        else ("verifier.near", "proof", "payer.near")
    )
    assert getattr(client, method)(*args) == ([ref] if method == "list_verified_feedback" else ref)
    ref["feedback_index"] = "0"
    with pytest.raises(ValueError):
        getattr(client, method)(*args)


@pytest.mark.parametrize("name", ["new_feedback", "feedback_revoked", "response_appended"])
@pytest.mark.parametrize("network", NETWORKS)
def test_event_feedback_reference_index_must_be_positive(name, network):
    evt = copy.deepcopy(next(e for role, e in EVENTS if e["event"] == name))
    assert erc.parse_erc8004_receipt_events([receipt("reputation", evt, network)], network) == [evt]
    evt["data"][0]["feedback_index"] = "0"
    assert erc.parse_erc8004_receipt_events([receipt("reputation", evt, network)], network) == []
