"""Registry ABI boundaries, wire bytes, RPC doubles and strict outcome parsing."""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import struct
from pathlib import Path

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from uvd_x402_sdk import near_registry as r
from uvd_x402_sdk.near_escrow import NearEd25519Signer
from uvd_x402_sdk.solana_signing import _b58encode

NETWORK = "near:mainnet"
ESCROW = "v1.arras-escrow.near"
RECEIVER = "registry.arras-escrow.near"
COMMON = {"network": NETWORK, "signer_id": "payer.near"}
PAYMENT = "1" * 32
BLOCK = _b58encode(bytes([2]) * 32)
FIXTURE = Path(__file__).parent / "fixtures/near-registry.json"


def register(**kw):
    return r.build_registry_register(
        **(COMMON | {"agent_uri": "https://agente.example/card.json"} | kw)
    )


def update(**kw):
    return r.build_registry_update(**(COMMON | {"agent_uri": "ipfs://bafy"} | kw))


def feedback(**kw):
    args = COMMON | {"escrow": ESCROW, "payment_id": PAYMENT, "score": 90}
    return r.build_give_feedback(**(args | kw))


def revoke(**kw):
    return r.build_revoke_feedback(**(COMMON | {"escrow": ESCROW, "payment_id": PAYMENT} | kw))


def params(tx):
    return tx["actions"][0]["params"]


BAD_URIS = [
    None,
    1,
    "",
    " https://x",
    "https://x ",
    "https://a b",
    "https://x\t",
    "https://x\n",
    "https://\x7f",
    "https://agénte",
    "sin-esquema",
    ":solo",
    "1http://x",
    "ht_tp://x",
    "https:",
    "a:",
    "a:" + "x" * 511,
    "a:😀",
    "a:\ud800",
]


@pytest.mark.parametrize("bad", BAD_URIS)
@pytest.mark.parametrize("builder", [register, update])
def test_agent_uri_rejections(builder, bad):
    with pytest.raises(r.RegistryError, match="ARRAS_REG_E01"):
        builder(agent_uri=bad)


@pytest.mark.parametrize("bad", [v for v in BAD_URIS if v is not None])
def test_feedback_uri_rejections(bad):
    with pytest.raises(r.RegistryError, match="ARRAS_REG_E14"):
        feedback(feedback_uri=bad)


@pytest.mark.parametrize(
    "uri",
    [
        "HTTPS://AGENTE.EXAMPLE",
        "ipfs://bafy",
        "ar://abc",
        "data:x",
        "did:near:a",
        "javascript:x",
        "a:b",
        "x+y-z.w:1",
        "a:" + "x" * 510,
    ],
)
def test_uri_accepted_without_normalization(uri):
    assert params(register(agent_uri=uri))["args"]["agent_uri"] == uri
    assert params(feedback(feedback_uri=uri))["args"]["feedback_uri"] == uri


@pytest.mark.parametrize("builder", [register, update])
@pytest.mark.parametrize(
    "bad",
    ["", "a\nb", "a\tb", "\0", "\x1f", "\x7f", "x" * 1025, "ñ" * 512 + "x", "\ud800", 1, {}, False],
)
def test_metadata_rejections(builder, bad):
    with pytest.raises(r.RegistryError, match="ARRAS_REG_E02"):
        builder(metadata=bad)


@pytest.mark.parametrize(
    "metadata", [None, "m" * 1024, "ñ" * 512, '{"nombre":"ñandú"}', "\x80", "a\u202eb"]
)
def test_metadata_utf8_and_ascii_controls_only(metadata):
    args = params(register(metadata=metadata))["args"]
    assert args.get("metadata") == metadata
    if metadata is None:
        assert "metadata" not in args


@pytest.mark.parametrize("field", ["tag1", "tag2"])
@pytest.mark.parametrize(
    "bad",
    ["", " quality", "quality ", "cali dad", "ñ", "a#b", "t" * 65, "x\n", "a+b", 0, False, []],
)
def test_tags_rejected(field, bad):
    with pytest.raises(r.RegistryError, match="ARRAS_REG_E13"):
        feedback(**{field: bad})


@pytest.mark.parametrize("field", ["tag1", "tag2"])
@pytest.mark.parametrize(
    "tag", [None, "quality", "successRate", "em:task/123", "a.b_c-d", "t" * 64]
)
def test_tags_exact(field, tag):
    assert params(feedback(**{field: tag}))["args"].get(field) == tag


@pytest.mark.parametrize("builder", [feedback, revoke])
@pytest.mark.parametrize("bad", [None, "", " x", "x ", "x y", "x\n", "x\x7f", "pagó", "p" * 65, 1])
def test_payment_id_rejections(builder, bad):
    with pytest.raises(r.RegistryError, match="ARRAS_REG_E11"):
        builder(payment_id=bad)


@pytest.mark.parametrize("value", ["x", "p" * 64, "!~", PAYMENT])
def test_payment_id_not_a_base58_validator(value):
    assert params(feedback(payment_id=value))["args"]["payment_id"] == value


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "0" * 63,
        "0" * 65,
        "0x" + "0" * 62,
        "0X" + "0" * 62,
        "F" * 64,
        "0" * 63 + "g",
        "0" * 63 + " ",
        "0" * 63 + "\n",
        "0" * 63 + "/",
        "0" * 63 + ":",
        "0" * 63 + "`",
        1,
    ],
)
def test_hash_rejections(bad):
    with pytest.raises(r.RegistryError, match="ARRAS_REG_E15"):
        feedback(feedback_hash=bad)


@pytest.mark.parametrize("value", [None, "ab" * 32, "f" * 64, "0" * 64])
def test_hash_accepted(value):
    assert params(feedback(feedback_hash=value))["args"].get("feedback_hash") == value


@pytest.mark.parametrize("bad", [-1, 101, 0.0, 90.5, True, False, "90", None])
def test_score_integer_range(bad):
    with pytest.raises(r.RegistryError, match="ARRAS_REG_E12"):
        feedback(score=bad)


@pytest.mark.parametrize("score", [0, 100])
def test_score_boundaries(score):
    assert params(feedback(score=score))["args"]["score"] == score


@pytest.mark.parametrize(
    "builder,default,code",
    [(register, r.REGISTER_DEPOSIT, "E05"), (feedback, r.FEEDBACK_DEPOSIT, "E16")],
)
def test_deposit_exact_floor(builder, default, code):
    assert params(builder(deposit=default))["deposit"] == default
    assert params(builder(deposit=int(default) + 1))["deposit"] == str(int(default) + 1)
    with pytest.raises(r.RegistryError, match=code):
        builder(deposit=int(default) - 1)


@pytest.mark.parametrize("builder,code", [(register, "E05"), (update, "E06"), (feedback, "E16")])
@pytest.mark.parametrize("deposit", [-1, True, 1.0, "01", " 1", "1\n", "１", "0x10", 2**128])
def test_deposit_types(builder, code, deposit):
    with pytest.raises(r.RegistryError, match=code):
        builder(deposit=deposit)


def test_update_and_revoke_deposits():
    assert params(update(deposit=1))["deposit"] == "1"
    assert params(update())["deposit"] == r.REGISTER_DEPOSIT
    with pytest.raises(r.RegistryError, match="E06"):
        update(deposit=0)
    assert params(revoke())["deposit"] == "1"
    for amount in [0, 2, True, "01"]:
        with pytest.raises(ValueError):
            revoke(deposit=amount)


def test_feedback_gas_boundary():
    assert params(feedback())["gas"] == "100000000000000"
    assert params(feedback(gas=r.MIN_FEEDBACK_GAS))["gas"] == str(r.MIN_FEEDBACK_GAS)
    with pytest.raises(r.RegistryError, match="E17"):
        feedback(gas=r.MIN_FEEDBACK_GAS - 1)
    for builder in (register, update, revoke):
        assert params(builder())["gas"] == "30000000000000"


@pytest.mark.parametrize("gas", [0, -1, True, "01", "1\n", 2**64, 300_000_000_000_001])
def test_invalid_gas(gas):
    with pytest.raises(ValueError):
        register(gas=gas)


@pytest.mark.parametrize("builder", [register, update, feedback, revoke])
@pytest.mark.parametrize("network", ["near", "near:testnet", "NEAR:mainnet", " near:mainnet", ""])
def test_network_isolation(builder, network):
    with pytest.raises(ValueError):
        builder(network=network)


@pytest.mark.parametrize("builder", [register, update, feedback, revoke])
@pytest.mark.parametrize(
    "receiver", ["registry.other.near", "REGISTRY.arras-escrow.near", "", ESCROW]
)
def test_receiver_isolation(builder, receiver):
    with pytest.raises(ValueError, match="receiver_id"):
        builder(receiver_id=receiver)


@pytest.mark.parametrize("builder", [feedback, revoke])
@pytest.mark.parametrize(
    "escrow", ["v2.arras-escrow.near", "arras-v1b-1101.testnet", "", " " + ESCROW]
)
def test_escrow_isolation(builder, escrow):
    with pytest.raises(r.RegistryError, match="E10"):
        builder(escrow=escrow)


@pytest.mark.parametrize("account", ["A.near", "a", "a..near", "a" * 65, "a.near\n", None])
def test_signer_account(account):
    with pytest.raises(ValueError):
        register(signer_id=account)


def test_tables_immutable():
    for table, expected in [
        (r.NEAR_REGISTRY_CONTRACTS, RECEIVER),
        (r.NEAR_REGISTRY_ESCROWS, ESCROW),
    ]:
        assert table[NETWORK] == (expected,)
        assert table["near:testnet"] == ()
        with pytest.raises(TypeError):
            table[NETWORK] = ("other.near",)


def test_descriptor_format_and_json():
    tx = register(metadata="ñandú")
    assert set(tx) == {"signerId", "receiverId", "actions"}
    assert tx["receiverId"] == RECEIVER
    assert tx["actions"][0]["type"] == "FunctionCall"
    assert (
        r.registry_args_json(tx)
        == '{"agent_uri":"https://agente.example/card.json","metadata":"ñandú"}'.encode()
    )
    assert r.registry_args_json(update(metadata=None)) == b'{"agent_uri":"ipfs://bafy"}'
    assert (
        r.registry_args_json(feedback())
        == ('{"escrow":"v1.arras-escrow.near","payment_id":"' + PAYMENT + '","score":90}').encode()
    )


class UntouchedSigner:
    @property
    def public_key(self):
        pytest.fail("invalid input reached the signing key")


def test_revalidate_descriptor_before_signing():
    cases = []
    for key, value in [
        ("receiverId", "other.near"),
        ("signerId", "A.near"),
        ("actions", []),
        ("actions", [register()["actions"][0]] * 2),
    ]:
        tx = register()
        tx[key] = value
        cases.append(tx)
    for key, value in [
        ("methodName", "delete_account"),
        ("args", {}),
        ("args", []),
        ("deposit", "0"),
        ("gas", "0"),
    ]:
        tx = register()
        params(tx)[key] = value
        cases.append(tx)
    for modify in [
        lambda t: t.update(extra=1),
        lambda t: t["actions"][0].update(type="Transfer"),
        lambda t: t["actions"][0].update(extra=1),
        lambda t: params(t).update(extra=1),
        lambda t: params(t)["args"].update(extra=1),
    ]:
        tx = register()
        modify(tx)
        cases.append(tx)
    for tx in [None, {}, *cases]:
        with pytest.raises(ValueError):
            r.sign_registry_transaction(tx, UntouchedSigner(), 1, BLOCK)
    for field, value in [
        ("escrow", "v2.arras-escrow.near"),
        ("score", 101),
        ("tag1", ""),
        ("feedback_hash", "f"),
        ("payment_id", ""),
        ("feedback_uri", "x"),
    ]:
        tx = feedback()
        params(tx)["args"][field] = value
        with pytest.raises(r.RegistryError):
            r.sign_registry_transaction(tx, UntouchedSigner(), 1, BLOCK)


@pytest.mark.parametrize("nonce", [0, -1, True, 2**64, "01"])
def test_nonce_checked_before_key(nonce):
    with pytest.raises(ValueError):
        r.sign_registry_transaction(register(), UntouchedSigner(), nonce, BLOCK)


@pytest.mark.parametrize("block", [None, "", "0" * 32, "1" * 31, "1" * 33, "z" * 44, BLOCK + "\n"])
def test_block_hash_exact_32_bytes(block):
    with pytest.raises(ValueError):
        r.sign_registry_transaction(register(), UntouchedSigner(), 1, block)


def test_signer_output_verified_and_input_snapshotted():
    real = NearEd25519Signer(bytes([1]) * 32)
    for key in [b"x", "x", bytes(33)]:
        bad = type("Bad", (), {"public_key": key})()
        with pytest.raises(ValueError, match="public key"):
            r.sign_registry_transaction(register(), bad, 1, BLOCK)
    for sig in [b"x", "x" * 64, bytes(64)]:
        bad = type("Bad", (), {"public_key": real.public_key, "sign": lambda self, d: sig})()
        with pytest.raises(ValueError, match="signature"):
            r.sign_registry_transaction(register(), bad, 1, BLOCK)
    tx = register()
    expected = r.sign_registry_transaction(tx, real, 1, BLOCK)

    class MutatingSigner:
        @property
        def public_key(self):
            tx["receiverId"] = "other.near"
            return real.public_key

        def sign(self, digest):
            params(tx)["args"]["agent_uri"] = "evil:x"
            return real.sign(digest)

    assert r.sign_registry_transaction(tx, MutatingSigner(), 1, BLOCK) == expected


def independent_wire(tx, public_key):
    def blob(b):
        return struct.pack("<I", len(b)) + b

    p = params(tx)
    return (
        blob(tx["signerId"].encode())
        + bytes([0])
        + public_key
        + struct.pack("<Q", 1)
        + blob(tx["receiverId"].encode())
        + bytes([2]) * 32
        + struct.pack("<I", 1)
        + bytes([2])
        + blob(p["methodName"].encode())
        + blob(json.dumps(p["args"], separators=(",", ":"), ensure_ascii=False).encode())
        + struct.pack("<Q", int(p["gas"]))
        + int(p["deposit"]).to_bytes(16, "little")
    )


def test_parity_vectors_and_independent_borsh():
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    signer = NearEd25519Signer(bytes([1]) * 32)
    assert fixture["inputs"]["public_key"] == "ed25519:" + _b58encode(signer.public_key)
    assert fixture["inputs"]["block_hash"] == BLOCK
    transactions = {
        "register": register(),
        "give_feedback": feedback(
            tag1="quality",
            tag2="successRate",
            feedback_uri="https://agente.example/f/1.json",
            feedback_hash="ab" * 32,
        ),
        "revoke_feedback": revoke(),
    }
    for method, tx in transactions.items():
        vector = fixture["vectors"][method]
        assert r.registry_args_json(tx).hex() == vector["args_hex"]
        if method != "revoke_feedback":
            signed = r.sign_registry_transaction(tx, signer, 1, BLOCK)
            assert signed == vector["signed_transaction_base64"]
            wire = base64.b64decode(signed)
            assert wire[:-65] == independent_wire(tx, signer.public_key)
            assert wire[-65] == 0
            Ed25519PublicKey.from_public_bytes(signer.public_key).verify(
                wire[-64:], hashlib.sha256(wire[:-65]).digest()
            )
    assert r.parse_registry_events(fixture["event_logs"]) == fixture["events"]


def agent_view():
    return {
        "account_id": "agente.near",
        "agent_uri": "https://agente.example/card.json",
        "metadata": None,
        "registered_at": "5",
        "updated_at": "6",
    }


def feedback_view(**kw):
    return {
        "feedback_id": 0,
        "rater": "payer.near",
        "ratee": "worker.near",
        "ratee_role": "receiver",
        "escrow": ESCROW,
        "payment_id": PAYMENT,
        "operator_id": "em",
        "released": "1000000",
        "score": 90,
        "tag1": "quality",
        "tag2": None,
        "feedback_uri": None,
        "feedback_hash": None,
        "created_at": "7",
        "revoked": False,
        "revoked_at": None,
    } | kw


def summary_view():
    role = {"count": 0, "revoked": 0, "score_sum": "0", "average_x100": None}
    return {
        **role,
        "account_id": "worker.near",
        "as_payer": role.copy(),
        "as_receiver": role.copy(),
        "last_feedback_id": None,
        "last_at": None,
    }


def config_view():
    return {
        "version": "arras-registry-v1",
        "escrows": [ESCROW],
        "agents": 0,
        "feedbacks": 0,
        "register_deposit": r.REGISTER_DEPOSIT,
        "feedback_deposit": r.FEEDBACK_DEPOSIT,
        "min_gas_give_feedback": str(r.MIN_FEEDBACK_GAS),
        "limits": {
            "max_agent_uri_len": 512,
            "max_metadata_len": 1024,
            "max_feedback_uri_len": 512,
            "max_tag_len": 64,
            "max_payment_id_len": 64,
            "max_score": 100,
            "max_list_limit": 50,
            "pending_ttl_blocks": "1000",
        },
    }


class FakeRPC:
    def __init__(self, value):
        self.value = value
        self.calls = []

    def __call__(self, method, params):
        self.calls.append((method, params))
        return {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {"result": list(json.dumps(self.value).encode())},
        }


def test_all_views_wire_and_nulls():
    cases = [
        ("get_config", (), {}, {}, config_view()),
        ("get_agent", ("agente.near",), {}, {"account_id": "agente.near"}, agent_view()),
        ("get_agent", ("agente.near",), {}, {"account_id": "agente.near"}, None),
        ("list_agents", (), {}, {}, [agent_view()]),
        ("list_agents", (), {"from_index": 0, "limit": 51}, {"from_index": 0, "limit": 51}, []),
        ("get_feedback", (0,), {}, {"feedback_id": 0}, feedback_view()),
        ("get_feedback", (2**32 - 1,), {}, {"feedback_id": 2**32 - 1}, None),
        (
            "get_feedback_for_payment",
            (ESCROW, PAYMENT, "payer.near"),
            {},
            {"escrow": ESCROW, "payment_id": PAYMENT, "rater": "payer.near"},
            None,
        ),
        (
            "list_feedback",
            ("worker.near",),
            {"from_index": 0, "limit": 50},
            {"account_id": "worker.near", "from_index": 0, "limit": 50},
            [feedback_view()],
        ),
        ("get_summary", ("worker.near",), {}, {"account_id": "worker.near"}, summary_view()),
        (
            "get_summary_by_operator",
            ("worker.near", "em"),
            {},
            {"account_id": "worker.near", "operator_id": "em"},
            summary_view() | {"operator_id": "em"},
        ),
        (
            "is_feedback_pending",
            (ESCROW, PAYMENT, "payer.near"),
            {},
            {"escrow": ESCROW, "payment_id": PAYMENT, "rater": "payer.near"},
            False,
        ),
    ]
    for method, args, kwargs, wire_args, value in cases:
        rpc = FakeRPC(value)
        assert getattr(r.NearRegistryClient(rpc=rpc), method)(*args, **kwargs) == value
        assert rpc.calls == [
            (
                "query",
                {
                    "request_type": "call_function",
                    "finality": "final",
                    "account_id": RECEIVER,
                    "method_name": method,
                    "args_base64": base64.b64encode(
                        json.dumps(wire_args, separators=(",", ":")).encode()
                    ).decode(),
                },
            )
        ]


@pytest.mark.parametrize(
    "method,args,valid",
    [
        ("get_agent", ("agente.near",), agent_view()),
        ("get_feedback", (0,), feedback_view()),
        ("get_summary", ("worker.near",), summary_view()),
        ("get_config", (), config_view()),
    ],
)
def test_every_view_field_required_and_typed(method, args, valid):
    for key in valid:
        for replacement in ["MISSING", []]:
            bad = copy.deepcopy(valid)
            if replacement == "MISSING":
                bad.pop(key)
            else:
                bad[key] = replacement
            # escrows is an array, and an empty array is structurally valid.
            if key == "escrows" and replacement == []:
                bad[key] = [1]
            with pytest.raises(ValueError, match="shape"):
                getattr(r.NearRegistryClient(rpc=FakeRPC(bad)), method)(*args)


@pytest.mark.parametrize("value", [0, -1, True, "-1", "1.5", " 1", "1\n", "１", "01", str(2**128)])
def test_view_u128_strings(value):
    with pytest.raises(ValueError, match="shape"):
        r.NearRegistryClient(rpc=FakeRPC(feedback_view(released=value))).get_feedback(0)


def test_nested_shapes_and_optionals():
    cases = [
        ("get_agent", ("a.near",), agent_view() | {"registered_at": str(2**64)}),
        ("get_feedback", (0,), feedback_view(revoked_at=0)),
        ("get_feedback", (0,), feedback_view(feedback_id=True)),
        ("get_feedback", (0,), feedback_view(score=True)),
        ("get_feedback", (0,), feedback_view(score=101)),
        ("get_feedback", (0,), feedback_view(ratee_role="third_party")),
        ("get_feedback", (0,), feedback_view(feedback_hash="AB" * 32)),
        ("list_agents", (), [agent_view(), {}]),
        ("list_feedback", ("a.near",), [feedback_view(), {}]),
        ("is_feedback_pending", (ESCROW, PAYMENT, "a.near"), 1),
        ("get_summary", ("a.near",), summary_view() | {"as_payer": {}}),
        ("get_summary", ("a.near",), summary_view() | {"operator_id": "em"}),
        ("get_summary", ("a.near",), summary_view() | {"last_at": 7}),
        ("get_summary", ("a.near",), summary_view() | {"average_x100": 10001}),
        ("get_summary_by_operator", ("a.near", "em"), summary_view()),
        ("get_summary_by_operator", ("a.near", "em"), summary_view() | {"operator_id": "other"}),
        ("get_config", (), config_view() | {"limits": {}}),
        ("get_config", (), config_view() | {"version": "other"}),
    ]
    for method, args, value in cases:
        with pytest.raises(ValueError, match="shape"):
            getattr(r.NearRegistryClient(rpc=FakeRPC(value)), method)(*args)
    maximum = feedback_view(
        released=str(2**128 - 1),
        created_at=str(2**64 - 1),
        feedback_id=2**32 - 1,
        revoked=True,
        revoked_at="0",
        feedback_hash="ff" * 32,
    )
    assert r.NearRegistryClient(rpc=FakeRPC(maximum)).get_feedback(0) == maximum


def test_bad_view_arguments_never_call_rpc():
    rpc = FakeRPC(None)
    client = r.NearRegistryClient(rpc=rpc)
    for method, args, kw in [
        ("get_agent", ("A.near",), {}),
        ("get_summary", ("a",), {}),
        ("get_summary_by_operator", ("a.near", 1), {}),
        ("get_feedback", (True,), {}),
        ("get_feedback", (-1,), {}),
        ("get_feedback", (2**32,), {}),
        ("list_agents", (), {"limit": "1"}),
        ("list_agents", (), {"limit": 2**32}),
        ("list_feedback", ("a.near",), {"from_index": -1}),
        ("get_feedback_for_payment", ("other.near", PAYMENT, "payer.near"), {}),
        ("is_feedback_pending", (ESCROW, "", "payer.near"), {}),
        ("is_feedback_pending", (ESCROW, PAYMENT, "A.near"), {}),
    ]:
        with pytest.raises(ValueError):
            getattr(client, method)(*args, **kw)
    assert not rpc.calls
    client.receiver_id = "other.near"
    with pytest.raises(ValueError, match="receiver_id"):
        client.get_config()
    assert not rpc.calls


@pytest.mark.parametrize(
    "response",
    [
        None,
        [],
        {"error": {}},
        {},
        {"result": []},
        {"result": {"error": "boom"}},
        {"result": {}},
        {"result": {"result": "null"}},
        {"result": {"result": [True]}},
        {"result": {"result": [-1]}},
        {"result": {"result": [256]}},
        {"result": {"result": [255]}},
        {"result": {"result": list(b"{broken")}},
        {"result": {"result": [32] * (512 * 1024 + 1)}},
    ],
)
def test_rpc_malformed_response(response):
    with pytest.raises(ValueError):
        r.NearRegistryClient(rpc=lambda *args: response).get_config()


def test_httpx_default_transport_uses_network_rpc(monkeypatch):
    calls = []

    def post(url, **kw):
        calls.append((url, kw))
        body = {"result": {"result": list(json.dumps(config_view()).encode())}}
        return httpx.Response(200, json=body, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", post)
    assert r.NearRegistryClient().get_config() == config_view()
    assert calls[0][0] == r.NEAR.rpc_url
    assert calls[0][1]["timeout"] == 30
    assert calls[0][1]["json"]["method"] == "query"

    def failed(*args, **kw):
        return httpx.Response(500, request=httpx.Request("POST", r.NEAR.rpc_url))

    monkeypatch.setattr(httpx, "post", failed)
    with pytest.raises(httpx.HTTPStatusError):
        r.NearRegistryClient().get_config()


def test_events_exact_and_malformed_batches():
    fixture = json.loads(FIXTURE.read_text())
    logs = fixture["event_logs"]
    assert len(logs) == 4
    assert r.parse_registry_events(logs) == fixture["events"]
    for value in [None, "EVENT_JSON:{}", b"bytes", {}]:
        assert r.parse_registry_events(value) == []
    malformed = [
        None,
        3,
        "x",
        "EVENT_JSON:{",
        "EVENT_JSON:[]",
        "EVENT_JSON:" + "[" * 65 + "0" + "]" * 65,
    ]
    for event in fixture["events"]:
        for key, value in [
            ("standard", "arras"),
            ("version", "2.0.0"),
            ("event", "unknown"),
            ("event", []),
            ("data", {}),
        ]:
            malformed.append("EVENT_JSON:" + json.dumps(event | {key: value}))
        for field in event["data"][0]:
            row = dict(event["data"][0])
            row.pop(field)
            malformed.append("EVENT_JSON:" + json.dumps(event | {"data": [event["data"][0], row]}))
            row[field] = []
            malformed.append("EVENT_JSON:" + json.dumps(event | {"data": [row]}))
    assert r.parse_registry_events(malformed + logs) == fixture["events"]
    event = fixture["events"][0]
    event["data"][0]["extra"] = "retained"
    assert r.parse_registry_events(["EVENT_JSON:" + json.dumps(event)]) == [event]
    assert len(r.parse_registry_events(["EVENT_JSON:" + json.dumps(event | {"data": []})])) == 1


def outcome(value, logs=None):
    return {
        "status": {"SuccessValue": base64.b64encode(json.dumps(value).encode()).decode()},
        "receipts_outcome": [{"outcome": {"logs": logs or []}}],
    }


@pytest.mark.parametrize("value", [0, 1, 2**32 - 1])
def test_accepted_outcome(value):
    assert r.parse_give_feedback_outcome(outcome(value)) == r.FeedbackOutcome(
        "accepted", feedback_id=value
    )
    assert r.parse_give_feedback_outcome({"result": outcome(value)}).feedback_id == value


@pytest.mark.parametrize("code", ["E18", "E20", "E21", "E22", "E23", "E24", "E25", "E26"])
def test_callback_rejections(code):
    enum = r.RegistryErrorCode[code]
    parsed = r.parse_give_feedback_outcome(
        outcome(None, ["noise", "arras-registry:rechazo:" + enum.value])
    )
    assert parsed == r.FeedbackOutcome("rejected", code=enum)


@pytest.mark.parametrize("code", list(r.RegistryErrorCode))
def test_all_panic_codes(code):
    value = {
        "status": {
            "Failure": {
                "ActionError": {
                    "kind": {
                        "FunctionCallError": {"ExecutionError": f"ARRAS_REG_{code.name}: message"}
                    }
                }
            }
        }
    }
    assert r.parse_give_feedback_outcome(value) == r.FeedbackOutcome("error", code=code)


@pytest.mark.parametrize("value", [True, False, -1, 2**32, 1.5, "1", {}, []])
def test_outcome_not_integer_id(value):
    with pytest.raises(ValueError):
        r.parse_give_feedback_outcome(outcome(value))


def test_unrecognized_outcomes_never_succeed():
    cases = [
        None,
        {},
        [],
        {"result": None},
        {"status": "Started"},
        {"status": {"SuccessReceiptId": "a"}},
        {"status": {"Failure": "unrelated"}},
        {"status": {"Failure": "ARRAS_REG_E99: nope"}},
        {"status": {"SuccessValue": "!"}},
        {"status": {"SuccessValue": 1}},
        {"status": {"SuccessValue": " " * (512 * 1024 + 1)}},
        outcome(None),
        outcome(None, ["arras-registry:rechazo:E19_EN_VUELO"]),
        outcome(None, ["arras-registry:rechazo:E22_PAGO_INEXISTENTE extra"]),
        outcome(
            None,
            [
                "arras-registry:rechazo:E22_PAGO_INEXISTENTE",
                "arras-registry:rechazo:E24_NO_ES_PARTE",
            ],
        ),
    ]
    for case in cases:
        with pytest.raises(ValueError):
            r.parse_give_feedback_outcome(case)


def test_summarize_exact_filter_revoke_floor_and_large_values():
    rows = [
        feedback_view(score=1, released="1"),
        feedback_view(score=100, released="2"),
        feedback_view(score=10, released="4", rater="other.near"),
        feedback_view(score=100, revoked=True, revoked_at="8"),
        feedback_view(escrow="other.near"),
        feedback_view(operator_id="other"),
    ]
    expected = {
        "count": 3,
        "distinct_raters": 2,
        "revoked": 1,
        "released_total": "7",
        "average_x100": 3700,
        "weighted_average_x100": 3442,
    }
    assert r.summarize_feedback(rows, ESCROW, "em") == expected
    assert r.summarize_feedback(reversed(rows), ESCROW, "em") == expected
    for empty in [[], [feedback_view(revoked=True, revoked_at="0")]]:
        summary = r.summarize_feedback(empty, ESCROW, "em")
        assert summary["average_x100"] is None
        assert summary["weighted_average_x100"] is None
        assert summary["count"] == summary["distinct_raters"] == 0
        assert summary["revoked"] == len(empty)
        assert summary["released_total"] == "0"
    assert (
        r.summarize_feedback([feedback_view(released="0")], ESCROW, "em")["weighted_average_x100"]
        is None
    )
    big = [feedback_view(released=str(2**128 - 1), score=99)] * 2
    assert r.summarize_feedback(big, ESCROW, "em")["released_total"] == str((2**128 - 1) * 2)
    with pytest.raises(ValueError):
        r.summarize_feedback([{}], ESCROW, "em")


@pytest.mark.parametrize("uri", ["https://x", "HTTPS://X", "ipfs://bafy", "ar:abc"])
def test_safe_links(uri):
    assert r.safe_link(uri) == uri


@pytest.mark.parametrize(
    "uri",
    [
        None,
        "javascript:alert(1)",
        "data:x",
        "http://x",
        "file:///c:/x",
        "C:/x",
        "\\\\server\\x",
        "/x",
        "https:",
        "https://a b",
        "https://x\n",
        " https://x",
    ],
)
def test_unsafe_links(uri):
    assert r.safe_link(uri) is None


def test_display_text_controls_bidi_and_zero_width():
    assert r.display_text("ñandú 👩 café") == "ñandú 👩 café"
    invisible = (
        "\0\t\n\x1f\x7f\x85\u200b\u200c\u200d\u202a"
        "\u202e\u2066\u2069\ufeff\u034f\ufe0f\U000e0100\u3164\ud800"
    )
    assert r.display_text("a" + invisible + "b") == "ab"


def test_feedback_zero_gas_is_e17():
    with pytest.raises(r.RegistryError, match="E17"):
        feedback(gas=0)


@pytest.mark.parametrize(
    "change",
    [
        {"logs": None},
        {"receipts_outcome": None},
        {"receipts_outcome": [{"outcome": {"logs": None}}]},
    ],
)
def test_malformed_outcome_log_containers(change):
    with pytest.raises(ValueError, match="logs"):
        r.parse_give_feedback_outcome(outcome(None) | change)


@pytest.mark.parametrize("method", ["delete_account", [], None])
def test_unsupported_method_has_its_own_diagnostic(method):
    tx = register()
    params(tx)["methodName"] = method
    with pytest.raises(ValueError, match="unsupported registry method"):
        r.sign_registry_transaction(tx, UntouchedSigner(), 1, BLOCK)


def test_non_object_args_have_their_own_diagnostic():
    tx = register()
    params(tx)["args"] = []
    with pytest.raises(ValueError, match="registry args must be an object"):
        r.sign_registry_transaction(tx, UntouchedSigner(), 1, BLOCK)


def test_block_hash_bound_applies_before_base58_decode(monkeypatch):
    def never_decode(value):
        pytest.fail("oversized block hash reached the base58 decoder")

    monkeypatch.setattr(r, "_b58decode", never_decode)
    with pytest.raises(ValueError, match="block_hash must be base58 of 32 bytes"):
        r.sign_registry_transaction(register(), UntouchedSigner(), 1, "1" * 45)


@pytest.mark.parametrize(
    "method,args",
    [
        ("list_feedback", ("Invalid.near",)),
        ("get_summary_by_operator", ("Invalid.near", "em")),
    ],
)
def test_view_account_validation_precedes_transport(method, args):
    def never_query(*args):
        pytest.fail("invalid account reached RPC")

    with pytest.raises(ValueError, match="invalid NEAR account"):
        getattr(r.NearRegistryClient(rpc=never_query), method)(*args)


def test_outcome_bound_applies_before_base64_decode(monkeypatch):
    def never_decode(*args, **kwargs):
        pytest.fail("oversized outcome reached the base64 decoder")

    monkeypatch.setattr(r.base64, "b64decode", never_decode)
    with pytest.raises(ValueError, match="too large"):
        r.parse_give_feedback_outcome({"status": {"SuccessValue": "A" * (512 * 1024 + 1)}})


@pytest.mark.parametrize("value", [True, -1, "0", {}, 2**32])
def test_rejection_log_cannot_explain_a_non_null_result(value):
    with pytest.raises(ValueError, match="expected u32 feedback_id or null"):
        r.parse_give_feedback_outcome(
            outcome(value, ["arras-registry:rechazo:E22_PAGO_INEXISTENTE"])
        )


def test_simple_average_uses_floor():
    assert (
        r.summarize_feedback([feedback_view(score=s) for s in (0, 0, 1)], ESCROW, "em")[
            "average_x100"
        ]
        == 33
    )
