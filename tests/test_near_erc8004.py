"""Offline ERC-8004 transaction, configuration and signing contracts."""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import struct
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from uvd_x402_sdk import near_erc8004 as erc

FIXTURE = json.loads((Path(__file__).parent / "fixtures/near-erc8004.json").read_bytes())
NETWORKS = ["near:mainnet", "near:testnet"]
NOW = 1_800_000_000_000_000_000


def config(role="identity", network="near:mainnet"):
    row = next(n for n in FIXTURE["networks"] if n["network"] == network)
    raw = next(
        c["result"]
        for c in row["calls"]
        if c["method"] == "get_config"
        and c["contract"] == erc.NEAR_ERC8004_CONTRACTS[network][role]
    )
    return erc.parse_erc8004_config(raw, network=network, role=role)


def raw_config(role="identity"):
    cfg = config(role)
    result = dict(cfg.data)
    if "limits" in result:
        result["limits"] = dict(result["limits"])
    return result


CASES = [
    (
        "register",
        "identity",
        {
            "agent_uri": "ipfs://card",
            "metadata": [{"metadata_key": "name", "metadata_value": "YWdlbnQ="}],
        },
        None,
        "132790000000000000000000",
        30,
    ),
    ("set_agent_uri", "identity", {"agent_id": "0", "new_uri": ""}, "1", "1", 30),
    (
        "set_metadata",
        "identity",
        {"agent_id": "0", "metadata_key": "name", "metadata_value": ""},
        "1",
        "1",
        30,
    ),
    (
        "set_agent_wallet",
        "identity",
        {"agent_id": "0", "new_wallet": "worker.near", "deadline": str(NOW)},
        None,
        "1",
        30,
    ),
    ("accept_agent_wallet", "identity", {"agent_id": "0"}, None, "1", 30),
    ("unset_agent_wallet", "identity", {"agent_id": "0"}, None, "1", 30),
    (
        "nft_transfer",
        "identity",
        {"receiver_id": "worker.near", "token_id": "0", "approval_id": 0, "memo": "ok"},
        None,
        "1",
        30,
    ),
    (
        "nft_transfer_call",
        "identity",
        {"receiver_id": "worker.near", "token_id": "0", "msg": "{}"},
        None,
        "1",
        60,
    ),
    (
        "nft_approve",
        "identity",
        {"token_id": "0", "account_id": "approved.near", "msg": ""},
        "1",
        "1",
        30,
    ),
    ("nft_revoke", "identity", {"token_id": "0", "account_id": "approved.near"}, None, "1", 30),
    ("nft_revoke_all", "identity", {"token_id": "0"}, None, "1", 30),
    (
        "give_feedback",
        "reputation",
        {
            "agent_id": "0",
            "value": "-32",
            "value_decimals": 1,
            "tag1": "trading yield",
            "tag2": "",
            "endpoint": "https://agent.example",
            "feedback_uri": "ipfs://feedback",
            "feedback_hash": "ab" * 32,
        },
        None,
        "5910000000000000000000",
        60,
    ),
    (
        "give_feedback_with_proof",
        "reputation",
        {
            "agent_id": "0",
            "value": "100",
            "value_decimals": 0,
            "verifier": erc.NEAR_ERC8004_CONTRACTS["near:mainnet"]["verifier"],
            "proof": "proof-id",
        },
        None,
        "14720000000000000000000",
        150,
    ),
    ("revoke_feedback", "reputation", {"agent_id": "0", "feedback_index": "1"}, None, "1", 30),
    (
        "append_response",
        "reputation",
        {
            "agent_id": "0",
            "client_address": "client.near",
            "feedback_index": "1",
            "response_uri": "data:answer",
            "response_hash": "ab" * 32,
        },
        "1",
        "1",
        30,
    ),
    (
        "verify_feedback_proof",
        "verifier",
        {"client": "client.near", "proof": "proof-id"},
        None,
        "0",
        50,
    ),
]


def build(method="register", args=None, role="identity", **options):
    return erc.build_erc8004_call(
        network="near:mainnet",
        signer_id="sponsor.near",
        config=config(role),
        method=method,
        args={} if args is None else args,
        now_ns=NOW,
        **options,
    )


@pytest.mark.parametrize("network", NETWORKS)
@pytest.mark.parametrize("method,role,args,deposit,expected_deposit,tgas", CASES)
def test_each_builder_exact_wallet_shape(
    network, method, role, args, deposit, expected_deposit, tgas
):
    args = dict(args)
    if method == "give_feedback_with_proof":
        args["verifier"] = erc.NEAR_ERC8004_CONTRACTS[network]["verifier"]
    kwargs = dict(
        network=network,
        signer_id="sponsor.near",
        config=config(role, network),
        deposit=deposit,
        now_ns=NOW,
    )
    tx = getattr(erc, "build_erc8004_" + method)(**args, **kwargs)
    assert tx == {
        "signerId": "sponsor.near",
        "receiverId": erc.NEAR_ERC8004_CONTRACTS[network][role],
        "actions": [
            {
                "type": "FunctionCall",
                "params": {
                    "methodName": method,
                    "args": args,
                    "deposit": expected_deposit,
                    "gas": str(tgas * 10**12),
                },
            }
        ],
    }
    actual = erc.erc8004_args_json(tx, network=network, config=config(role, network), now_ns=NOW)
    assert json.loads(actual) == args
    assert b": " not in actual and b", " not in actual


def test_immutable_tables_and_network_identifier():
    for network in NETWORKS:
        assert (
            erc.agent_registry(network)
            == network + ":" + erc.NEAR_ERC8004_CONTRACTS[network]["identity"]
        )
        assert set(erc.NEAR_ERC8004_CONTRACTS[network]) == {"identity", "reputation", "verifier"}
        with pytest.raises(TypeError):
            erc.NEAR_ERC8004_CONTRACTS[network]["identity"] = "other.near"
    with pytest.raises(TypeError):
        erc.ERC8004_DEPOSIT_CEILINGS["register_deposit"] = 1
    cfg = config()
    with pytest.raises(TypeError):
        cfg.data["limits"]["max_agent_uri_len"] = 0


@pytest.mark.parametrize(
    "network", ["mainnet", "NEAR:MAINNET", "near:mainnet ", "", None, [], "near:localnet"]
)
def test_network_guard(network):
    with pytest.raises(ValueError):
        erc.build_erc8004_call(
            network=network, signer_id="payer.near", config=config(), method="register", args={}
        )


@pytest.mark.parametrize(
    "account",
    [
        "A.near",
        " a.near",
        "a.near\n",
        "",
        "x",
        "a..near",
        "_a.near",
        "a-.near",
        "é.near",
        None,
        "a" * 65,
    ],
)
def test_account_guard(account):
    with pytest.raises(ValueError):
        erc.build_erc8004_register(network="near:mainnet", signer_id=account, config=config())


@pytest.mark.parametrize("role", ["identity", "reputation", "verifier"])
def test_config_required_fields_and_versions(role):
    raw = raw_config(role)
    for name in raw:
        missing = {k: v for k, v in raw.items() if k != name}
        with pytest.raises(ValueError):
            erc.parse_erc8004_config(missing, network="near:mainnet", role=role)
        malformed = {**raw, name: None}
        with pytest.raises(ValueError):
            erc.parse_erc8004_config(malformed, network="near:mainnet", role=role)
    with pytest.raises(ValueError):
        erc.parse_erc8004_config({**raw, "contract": "other"}, network="near:mainnet", role=role)


@pytest.mark.parametrize(
    "field,role",
    [
        ("register_deposit", "identity"),
        ("feedback_deposit", "reputation"),
        ("feedback_proof_deposit", "reputation"),
    ],
)
def test_config_deposit_ceiling_and_real_config_default(field, role):
    raw = raw_config(role)
    ceiling = erc.ERC8004_DEPOSIT_CEILINGS[field]
    raw[field] = str(ceiling)
    cfg = erc.parse_erc8004_config(raw, network="near:mainnet", role=role)
    case = next(
        c
        for c in CASES
        if c[0]
        == {
            "register_deposit": "register",
            "feedback_deposit": "give_feedback",
            "feedback_proof_deposit": "give_feedback_with_proof",
        }[field]
    )
    tx = erc.build_erc8004_call(
        network="near:mainnet", signer_id="payer.near", config=cfg, method=case[0], args=case[2]
    )
    assert tx["actions"][0]["params"]["deposit"] == str(ceiling)
    for bad in [str(ceiling + 1), "01", 1, True, "0", "-1", "+1", "1\n"]:
        with pytest.raises(ValueError):
            erc.parse_erc8004_config({**raw, field: bad}, network="near:mainnet", role=role)


@pytest.mark.parametrize("method,role,args,deposit,expected_deposit,tgas", CASES)
def test_deposit_and_gas_guard(method, role, args, deposit, expected_deposit, tgas):
    options = dict(method=method, role=role, args=args)
    for bad in [str(2**128 - 1), "-1", "+1", "01", "1\n", 1, True, ""]:
        with pytest.raises(ValueError):
            build(**options, deposit=bad)
    if expected_deposit != "0":
        with pytest.raises(ValueError):
            build(**options, deposit="0")
    for bad in ["0", str(300 * 10**12 + 1), "01", 30 * 10**12, True, "30000000000000\n"]:
        with pytest.raises(ValueError):
            build(**options, deposit=deposit, gas=bad)
    if method in ("set_agent_uri", "set_metadata", "nft_approve", "append_response"):
        with pytest.raises(ValueError):
            build(**options)


def test_config_binding_and_gas_changes():
    for cfg in [config("reputation"), config(network="near:testnet")]:
        with pytest.raises(ValueError):
            erc.build_erc8004_register(network="near:mainnet", signer_id="payer.near", config=cfg)
    raw = raw_config("reputation")
    raw["min_gas_give_feedback"] = str(70 * 10**12)
    cfg = erc.parse_erc8004_config(raw, network="near:mainnet", role="reputation")
    tx = erc.build_erc8004_give_feedback(
        network="near:mainnet",
        signer_id="payer.near",
        config=cfg,
        agent_id="0",
        value="0",
        value_decimals=0,
    )
    assert tx["actions"][0]["params"]["gas"] == str(70 * 10**12)
    raw["identity_registry"] = erc.NEAR_ERC8004_CONTRACTS["near:testnet"]["identity"]
    with pytest.raises(ValueError):
        erc.parse_erc8004_config(raw, network="near:mainnet", role="reputation")
    raw = raw_config("verifier")
    raw["escrow"] = erc.NEAR_ERC8004_ESCROWS["near:testnet"]
    with pytest.raises(ValueError):
        erc.parse_erc8004_config(raw, network="near:mainnet", role="verifier")


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "01",
        "+1",
        "-0",
        " 1",
        "1 ",
        "1\n",
        "1.0",
        "1e1",
        "0x1",
        "0X1",
        "١",
        "１",
        "1_0",
        str(2**64),
        1,
        True,
        None,
    ],
)
def test_canonical_agent_id_guard(bad):
    with pytest.raises(ValueError):
        build("unset_agent_wallet", {"agent_id": bad})


@pytest.mark.parametrize(
    "bad",
    ["-0", "-01", "+1", "01", str(10**38 + 1), str(-(10**38) - 1), "1\n", "١", 10, True, None, ""],
)
def test_signed_value_guard(bad):
    with pytest.raises(ValueError):
        build("give_feedback", {"agent_id": "0", "value": bad, "value_decimals": 0}, "reputation")


@pytest.mark.parametrize("value", ["0", "1", "-1", str(10**38), str(-(10**38))])
@pytest.mark.parametrize("decimals", [0, 18])
def test_signed_value_boundaries(value, decimals):
    tx = build(
        "give_feedback", {"agent_id": "0", "value": value, "value_decimals": decimals}, "reputation"
    )
    assert tx["actions"][0]["params"]["args"]["value"] == value


@pytest.mark.parametrize("bad", [-1, 19, "0", True, None, 0.0])
def test_decimal_scale_guard(bad):
    with pytest.raises(ValueError):
        build("give_feedback", {"agent_id": "0", "value": "1", "value_decimals": bad}, "reputation")


@pytest.mark.parametrize(
    "uri",
    [
        "ipfs://x",
        "https://a/%xx",
        "HTTPS://A",
        "data:application/json,{}",
        "",
        "did:near:a",
        "a+b-c.d:1",
    ],
)
def test_uri_grammar(uri):
    if "{" in uri:
        with pytest.raises(ValueError):
            build(args={"agent_uri": uri})
    else:
        assert build(args={"agent_uri": uri})["actions"][0]["params"]["args"]["agent_uri"] == uri


@pytest.mark.parametrize(
    "uri",
    [
        "CID",
        "GetPrice",
        "a:",
        ":x",
        "1a:x",
        "a_b:x",
        " a:b",
        "a:b\n",
        "https://a b",
        "https://é",
        'https://a"b',
        "https://a\\b",
        "a:<b>",
        "a:{b}",
        "a:|",
        "a:^",
        "a:`",
        "a:" + "b" * 8191,
    ],
)
def test_uri_guard(uri):
    with pytest.raises(ValueError):
        build(args={"agent_uri": uri})


@pytest.mark.parametrize(
    "tag,valid",
    [
        ("", True),
        (" ñ " * 10, True),
        ("ñ" * 32, True),
        ("ñ" * 33, False),
        ("x" * 64, True),
        ("x" * 65, False),
        ("x\x00", False),
        ("x\x7f", False),
        ("x\x85", False),
        ("x\x9f", False),
        ("\ud800", False),
    ],
)
def test_tag_utf8_guard(tag, valid):
    kwargs = dict(
        method="give_feedback",
        role="reputation",
        args={"agent_id": "0", "value": "1", "value_decimals": 0, "tag1": tag},
    )
    if valid:
        build(**kwargs)
    else:
        with pytest.raises(ValueError):
            build(**kwargs)


@pytest.mark.parametrize(
    "bad", ["AB" * 32, "0x" + "ab" * 32, "0X" + "ab" * 32, "ab" * 31, "ab" * 32 + "\n", "", 1]
)
def test_hash_guard(bad):
    with pytest.raises(ValueError):
        build(
            "give_feedback",
            {"agent_id": "0", "value": "1", "value_decimals": 0, "feedback_hash": bad},
            "reputation",
        )


@pytest.mark.parametrize(
    "deadline,valid",
    [(NOW - 1, False), (NOW, True), (NOW + 300_000_000_000, True), (NOW + 300_000_000_001, False)],
)
def test_wallet_deadline_guard(deadline, valid):
    args = {"agent_id": "0", "new_wallet": "worker.near", "deadline": str(deadline)}
    if valid:
        build("set_agent_wallet", args)
    else:
        with pytest.raises(ValueError):
            build("set_agent_wallet", args)


@pytest.mark.parametrize(
    "entry",
    [
        {"metadata_key": "agentWallet", "metadata_value": ""},
        {"metadata_key": "", "metadata_value": ""},
        {"metadata_key": "x" * 65, "metadata_value": ""},
        {"metadata_key": "ñ", "metadata_value": ""},
        {"metadata_key": "a", "metadata_value": "YQ"},
        {"metadata_key": "a", "metadata_value": "YR=="},
        {"metadata_key": "a", "metadata_value": "YQ==\n"},
        {"metadata_key": "a", "metadata_value": base64.b64encode(b"x" * 1025).decode()},
        {"metadata_key": "a", "metadata_value": "", "extra": 1},
    ],
)
def test_metadata_guard(entry):
    with pytest.raises(ValueError):
        build(args={"metadata": [entry]})


def test_other_input_guards():
    with pytest.raises(ValueError):
        build(args={"metadata": [{"metadata_key": "a", "metadata_value": ""}] * 5})
    with pytest.raises(ValueError):
        build(
            "set_metadata",
            {"agent_id": "0", "metadata_key": "agentWallet", "metadata_value": ""},
            deposit="1",
        )
    with pytest.raises(ValueError):
        build("revoke_feedback", {"agent_id": "0", "feedback_index": "0"}, "reputation")
    for proof in ["", " p", "p q", "é", "x" * 129, "x\n"]:
        with pytest.raises(ValueError):
            build(
                "give_feedback_with_proof",
                {
                    "agent_id": "0",
                    "value": "0",
                    "value_decimals": 0,
                    "verifier": erc.NEAR_ERC8004_CONTRACTS["near:mainnet"]["verifier"],
                    "proof": proof,
                },
                "reputation",
            )
    with pytest.raises(ValueError):
        build(
            "give_feedback_with_proof",
            {
                "agent_id": "0",
                "value": "0",
                "value_decimals": 0,
                "verifier": erc.NEAR_ERC8004_CONTRACTS["near:mainnet"]["reputation"],
                "proof": "p",
            },
            "reputation",
        )
    for args in [{"unknown": 1}, {"metadata": "x"}, {"metadata": {}}, {"agent_uri": 1}]:
        with pytest.raises(ValueError):
            build(args=args)


class Signer:
    def __init__(self):
        self.key = Ed25519PrivateKey.generate()

    @property
    def public_key(self):
        return self.key.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )

    def sign(self, digest):
        return self.key.sign(digest)


class NoKey:
    @property
    def public_key(self):
        pytest.fail("invalid descriptor reached a signing key")


def sign(tx, signer=None, **options):
    return erc.sign_erc8004_transaction(
        tx,
        signer or NoKey(),
        "1",
        "1" * 32,
        network="near:mainnet",
        config=config(),
        now_ns=NOW,
        **options,
    )


@pytest.mark.parametrize(
    "mutation",
    [
        "network",
        "receiver",
        "amount",
        "gas",
        "extra",
        "two_actions",
        "method",
        "type",
        "args",
        "canonical",
        "signer",
    ],
)
def test_signing_revalidates_before_key(mutation):
    tx = build()
    p = tx["actions"][0]["params"]
    if mutation == "network":
        tx["receiverId"] = erc.NEAR_ERC8004_CONTRACTS["near:testnet"]["identity"]
    if mutation == "receiver":
        tx["receiverId"] = "other.near"
    if mutation == "amount":
        p["deposit"] = str(erc.ERC8004_DEPOSIT_CEILINGS["register_deposit"] + 1)
    if mutation == "gas":
        p["gas"] = "0"
    if mutation == "extra":
        tx["extra"] = 1
    if mutation == "two_actions":
        tx["actions"].append(copy.deepcopy(tx["actions"][0]))
    if mutation == "method":
        p["methodName"] = "delete_account"
    if mutation == "type":
        tx["actions"][0]["type"] = "Transfer"
    if mutation == "args":
        p["args"] = {"unexpected": 1}
    if mutation == "canonical":
        p["deposit"] = "0" + p["deposit"]
    if mutation == "signer":
        tx["signerId"] = "INVALID"
    with pytest.raises(ValueError):
        sign(tx)


def test_expired_wallet_rechecked_before_key():
    tx = build(
        "set_agent_wallet", {"agent_id": "0", "new_wallet": "worker.near", "deadline": str(NOW)}
    )
    with pytest.raises(ValueError):
        erc.sign_erc8004_transaction(
            tx, NoKey(), "1", "1" * 32, network="near:mainnet", config=config(), now_ns=NOW + 1
        )


def test_independent_borsh_and_verified_signature():
    tx = build(args={"agent_uri": "ipfs://card"})
    signer = Signer()
    signed = base64.b64decode(sign(tx, signer))

    def string(value):
        raw = value.encode() if isinstance(value, str) else value
        return struct.pack("<I", len(raw)) + raw

    wire = (
        string("sponsor.near")
        + b"\x00"
        + signer.public_key
        + struct.pack("<Q", 1)
        + string("identity-registry.erc-8004.near")
        + bytes(32)
        + struct.pack("<I", 1)
        + b"\x02"
        + string("register")
        + string(b'{"agent_uri":"ipfs://card"}')
        + struct.pack("<Q", 30 * 10**12)
        + int("132790000000000000000000").to_bytes(16, "little")
    )
    assert signed[:-65] == wire
    assert signed[-65] == 0
    signer.key.public_key().verify(signed[-64:], hashlib.sha256(wire).digest())
    signer.sign = lambda _: bytes(64)
    with pytest.raises(ValueError, match="signature"):
        sign(tx, signer)


def test_signer_cannot_mutate_prepared_args():
    tx = build(args={"metadata": [{"metadata_key": "name", "metadata_value": "YQ=="}]})

    class MutatingSigner(Signer):
        @property
        def public_key(self):
            tx["actions"][0]["params"]["args"]["metadata"][0]["metadata_value"] = "Yg=="
            return super().public_key

    signed = base64.b64decode(sign(tx, MutatingSigner()))
    assert b"YQ==" in signed[:-65] and b"Yg==" not in signed[:-65]


@pytest.mark.parametrize("role", [None, [], "validation", "Identity", "identity "])
def test_config_role_type_guard(role):
    with pytest.raises(ValueError, match="unknown contract role"):
        erc.parse_erc8004_config({}, network="near:mainnet", role=role)


@pytest.mark.parametrize("raw", [None, [], True, 1, "config"])
def test_config_object_type_guard(raw):
    with pytest.raises(ValueError, match="config must be an object"):
        erc.parse_erc8004_config(raw, network="near:mainnet", role="identity")


def test_identity_config_limits_and_window():
    original = raw_config()
    for key, value in original["limits"].items():
        for bad in [None, True, 0, str(int(value) + 1) if isinstance(value, str) else value + 1]:
            raw = copy.deepcopy(original)
            raw["limits"][key] = bad
            with pytest.raises(ValueError):
                erc.parse_erc8004_config(raw, network="near:mainnet", role="identity")


def test_tighter_config_limits_are_enforced():
    for limit, args in [
        ("max_agent_uri_len", {"agent_uri": "a:long"}),
        ("max_register_metadata", {"metadata": [{"metadata_key": "k", "metadata_value": ""}] * 2}),
        ("max_metadata_key_len", {"metadata": [{"metadata_key": "long", "metadata_value": ""}]}),
        ("max_metadata_value_len", {"metadata": [{"metadata_key": "k", "metadata_value": "YWI="}]}),
    ]:
        raw = raw_config()
        raw["limits"][limit] = 1
        cfg = erc.parse_erc8004_config(raw, network="near:mainnet", role="identity")
        with pytest.raises(ValueError):
            erc.build_erc8004_register(
                network="near:mainnet", signer_id="payer.near", config=cfg, **args
            )


@pytest.mark.parametrize("args", [None, [], 1, "args"])
def test_args_are_an_object(args):
    with pytest.raises(ValueError, match="args must be an object"):
        erc.build_erc8004_call(
            network="near:mainnet",
            signer_id="payer.near",
            config=config(),
            method="register",
            args=args,
        )


def test_growth_without_deposit_is_explicit_error():
    with pytest.raises(ValueError, match="storage growth requires an explicit deposit"):
        build("set_agent_uri", {"agent_id": "0", "new_uri": ""})


def test_transaction_params_are_closed():
    tx = build()
    tx["actions"][0]["params"]["unexpected"] = True
    with pytest.raises(ValueError):
        sign(tx)


@pytest.mark.parametrize(
    "nonce,block,message",
    [
        ("0", "1" * 32, "nonce must be positive"),
        ("1", "1" * 31, "invalid block hash"),
        ("1", "1" * 45, "invalid block hash"),
        ("1", "1" * 33, "block hash must decode to 32 bytes"),
        ("1", None, "invalid block hash"),
    ],
)
def test_nonce_and_block_checks_before_key(nonce, block, message):
    with pytest.raises(ValueError, match=message):
        erc.sign_erc8004_transaction(
            build(), NoKey(), nonce, block, network="near:mainnet", config=config()
        )


def test_invalid_signer_public_key_and_signature_types():
    class BadKey(Signer):
        @property
        def public_key(self):
            return bytes(31)

    with pytest.raises(ValueError, match="invalid Ed25519 public key"):
        sign(build(), BadKey())
    for signature in ["x" * 64, bytes(63), None]:
        signer = Signer()
        signer.sign = lambda _: signature
        with pytest.raises(ValueError, match="invalid Ed25519 signature"):
            sign(build(), signer)


@pytest.mark.parametrize(
    "field,good,bad",
    [
        ("endpoint", "a:" + "x" * 1022, "a:" + "x" * 1023),
        ("feedback_uri", "a:" + "x" * 4094, "a:" + "x" * 4095),
        ("feedback_hash", "00" * 32, "0X" + "00" * 32),
    ],
)
def test_feedback_field_boundaries(field, good, bad):
    args = {"agent_id": "0", "value": "0", "value_decimals": 0, field: good}
    build("give_feedback", args, "reputation")
    args[field] = bad
    with pytest.raises(ValueError):
        build("give_feedback", args, "reputation")


def test_metadata_memo_response_and_message_boundaries():
    entry = {"metadata_key": "k" * 64, "metadata_value": base64.b64encode(b"x" * 1024).decode()}
    build(args={"metadata": [entry] * 4, "agent_uri": "a:" + "x" * 8190})
    txargs = {
        "token_id": str(2**64 - 1),
        "receiver_id": "w" * 64,
        "memo": "ñ" * 128,
        "approval_id": 2**64 - 1,
    }
    build("nft_transfer", txargs)
    for field, value in [("memo", "ñ" * 129), ("approval_id", True), ("approval_id", 2**64)]:
        with pytest.raises(ValueError):
            build("nft_transfer", {**txargs, field: value})
    response = {
        "agent_id": "0",
        "client_address": "client.near",
        "feedback_index": "1",
        "response_uri": "a:" + "x" * 4094,
    }
    build("append_response", response, "reputation", deposit="1")
    for bad in ["", None, "a:" + "x" * 4095]:
        with pytest.raises(ValueError):
            build("append_response", {**response, "response_uri": bad}, "reputation", deposit="1")
    with pytest.raises(ValueError):
        build("nft_transfer_call", {"receiver_id": "w.near", "token_id": "0", "msg": "x" * 65537})


def test_config_snapshot_can_be_revalidated_without_mutable_aliases():
    cfg = config()
    again = erc.parse_erc8004_config(cfg.data, network="near:mainnet", role="identity")
    assert again == cfg and again is not cfg
    raw = raw_config()
    snapshot = erc.parse_erc8004_config(raw, network="near:mainnet", role="identity")
    raw["limits"]["max_agent_uri_len"] = 0
    assert snapshot.data["limits"]["max_agent_uri_len"] == 8192


@pytest.mark.parametrize(
    "field,literal",
    [
        ("register_deposit", 139429500000000000000000),
        ("feedback_deposit", 6205500000000000000000),
        ("feedback_proof_deposit", 15456000000000000000000),
    ],
)
def test_deposit_ceilings_are_pinned_yocto_literals(field, literal):
    assert erc.ERC8004_DEPOSIT_CEILINGS[field] == literal


@pytest.mark.parametrize("network", NETWORKS)
@pytest.mark.parametrize(
    "method,role,args,literal",
    [
        ("register", "identity", {}, 139429500000000000000000),
        (
            "give_feedback",
            "reputation",
            {"agent_id": "0", "value": "1", "value_decimals": 0},
            6205500000000000000000,
        ),
        (
            "give_feedback_with_proof",
            "reputation",
            {"agent_id": "0", "value": "1", "value_decimals": 0, "proof": "proof-id"},
            15456000000000000000000,
        ),
    ],
)
def test_literal_deposit_ceiling_at_build_and_before_signing(network, method, role, args, literal):
    args = dict(args)
    if method == "give_feedback_with_proof":
        args["verifier"] = erc.NEAR_ERC8004_CONTRACTS[network]["verifier"]
    cfg = config(role, network)
    options = dict(network=network, signer_id="payer.near", config=cfg, method=method, args=args)
    tx = erc.build_erc8004_call(**options, deposit=str(literal))
    assert tx["actions"][0]["params"]["deposit"] == str(literal)
    assert erc.sign_erc8004_transaction(tx, Signer(), "1", "1" * 32, network=network, config=cfg)
    with pytest.raises(ValueError, match="deposit outside SDK bounds"):
        erc.build_erc8004_call(**options, deposit=str(literal + 1))
    tx["actions"][0]["params"]["deposit"] = str(literal + 1)
    with pytest.raises(ValueError, match="deposit outside SDK bounds"):
        erc.sign_erc8004_transaction(tx, NoKey(), "1", "1" * 32, network=network, config=cfg)


@pytest.mark.parametrize("network", NETWORKS)
def test_growth_deposit_is_capped_by_recorded_config_before_signing(network):
    cfg = config(network=network)
    maximum = int(cfg.data["register_deposit"])
    options = dict(
        network=network, signer_id="payer.near", config=cfg, agent_id="0", new_uri="ipfs://new"
    )
    tx = erc.build_erc8004_set_agent_uri(**options, deposit=str(maximum))
    assert tx["actions"][0]["params"]["deposit"] == str(maximum)
    with pytest.raises(ValueError, match="deposit outside SDK bounds"):
        erc.build_erc8004_set_agent_uri(**options, deposit=str(maximum + 1))
    tx["actions"][0]["params"]["deposit"] = str(maximum + 1)
    with pytest.raises(ValueError, match="deposit outside SDK bounds"):
        erc.sign_erc8004_transaction(tx, NoKey(), "1", "1" * 32, network=network, config=cfg)


@pytest.mark.parametrize("network", NETWORKS)
@pytest.mark.parametrize(
    "role,field,floor,method,args",
    [
        (
            "identity",
            "min_gas_nft_transfer_call",
            50000000000000,
            "nft_transfer_call",
            {"receiver_id": "worker.near", "token_id": "0", "msg": ""},
        ),
        (
            "reputation",
            "min_gas_give_feedback",
            40000000000000,
            "give_feedback",
            {"agent_id": "0", "value": "1", "value_decimals": 0},
        ),
        (
            "reputation",
            "min_gas_give_feedback_with_proof",
            120000000000000,
            "give_feedback_with_proof",
            {"agent_id": "0", "value": "1", "value_decimals": 0, "proof": "p"},
        ),
        (
            "verifier",
            "min_gas_verify",
            25000000000000,
            "verify_feedback_proof",
            {"client": "payer.near", "proof": "p"},
        ),
    ],
)
def test_config_gas_floor_at_parse_build_and_sign(network, role, field, floor, method, args):
    raw = dict(config(role, network).data)
    if "limits" in raw:
        raw["limits"] = dict(raw["limits"])
    args = dict(args)
    if method == "give_feedback_with_proof":
        args["verifier"] = erc.NEAR_ERC8004_CONTRACTS[network]["verifier"]
    raw[field] = str(floor)
    cfg = erc.parse_erc8004_config(raw, network=network, role=role)
    tx = erc.build_erc8004_call(
        network=network,
        signer_id="payer.near",
        config=cfg,
        method=method,
        args=args,
        gas=str(floor),
    )
    assert tx["actions"][0]["params"]["gas"] == str(floor)
    assert erc.sign_erc8004_transaction(tx, Signer(), "1", "1" * 32, network=network, config=cfg)
    raw[field] = str(floor - 1)
    with pytest.raises(ValueError, match=field):
        erc.parse_erc8004_config(raw, network=network, role=role)
    forged = erc.Erc8004Config(network, role, raw)
    with pytest.raises(ValueError, match=field):
        erc.build_erc8004_call(
            network=network,
            signer_id="payer.near",
            config=forged,
            method=method,
            args=args,
            gas=str(floor - 1),
        )
    tx["actions"][0]["params"]["gas"] = str(floor - 1)
    with pytest.raises(ValueError, match=field):
        erc.sign_erc8004_transaction(tx, NoKey(), "1", "1" * 32, network=network, config=forged)


@pytest.mark.parametrize("network", NETWORKS)
@pytest.mark.parametrize(
    "kind", ["other_network", "foreign", "registry", "uppercase", "space", "empty", "null"]
)
def test_proof_verifier_is_network_bound_before_key(network, kind):
    verifier = erc.NEAR_ERC8004_CONTRACTS[network]["verifier"]
    other = "near:testnet" if network == "near:mainnet" else "near:mainnet"
    bad = {
        "other_network": erc.NEAR_ERC8004_CONTRACTS[other]["verifier"],
        "foreign": "other.near",
        "registry": erc.NEAR_ERC8004_CONTRACTS[network]["reputation"],
        "uppercase": verifier.upper(),
        "space": verifier + " ",
        "empty": "",
        "null": None,
    }[kind]
    cfg = config("reputation", network)
    options = dict(
        network=network,
        signer_id="payer.near",
        config=cfg,
        agent_id="0",
        value="1",
        value_decimals=0,
        proof="p",
    )
    tx = erc.build_erc8004_give_feedback_with_proof(**options, verifier=verifier)
    assert erc.sign_erc8004_transaction(tx, Signer(), "1", "1" * 32, network=network, config=cfg)
    with pytest.raises(ValueError):
        erc.build_erc8004_give_feedback_with_proof(**options, verifier=bad)
    tx["actions"][0]["params"]["args"]["verifier"] = bad
    with pytest.raises(ValueError):
        erc.sign_erc8004_transaction(tx, NoKey(), "1", "1" * 32, network=network, config=cfg)


# --- Arras escrow -> ERC-8004 verifier: one verifier per escrow -----------------------------------

# v2's verifier, payment-proof.arras-escrow.near, is keyless on mainnet since 2026-11-13 and 0.114.0
# registers it. The tests of the data patch itself (a row filled, or None) still patch the table,
# with a fictitious account (V2_VERIFIER) or None.
PAYMENT_PROOF = "payment-proof.arras-escrow.near"
V2_VERIFIER = "v2-verifier.example.near"
V1_VERIFIER = erc.NEAR_ERC8004_CONTRACTS["near:mainnet"]["verifier"]


def feedback_with_proof(verifier, network="near:mainnet"):
    return erc.build_erc8004_give_feedback_with_proof(
        network=network,
        signer_id="payer.near",
        config=config("reputation", network),
        agent_id="0",
        value="1",
        value_decimals=0,
        proof="p",
        verifier=verifier,
    )


def patched_v2_row(monkeypatch, verifier=V2_VERIFIER):
    from types import MappingProxyType

    rows = {**erc.NEAR_ERC8004_ESCROW_VERIFIERS["near:mainnet"], "v2.arras-escrow.near": verifier}
    monkeypatch.setattr(
        erc,
        "NEAR_ERC8004_ESCROW_VERIFIERS",
        MappingProxyType(
            {**erc.NEAR_ERC8004_ESCROW_VERIFIERS, "near:mainnet": MappingProxyType(rows)}
        ),
    )


def new_feedback_log(verifier):
    data = {
        "agent_id": "0",
        "client_address": "payer.near",
        "feedback_index": "1",
        "value": "1",
        "value_decimals": 0,
        "tag1": "",
        "tag2": "",
        "endpoint": "",
        "feedback_uri": "",
        "feedback_hash": None,
        "proof": {
            "verifier": verifier,
            "proof": "p",
            "counterparty": "worker.near",
            "role": "payer",
            "context": "",
            "weight": "1",
        },
    }
    event = {"standard": "erc8004", "version": "1.0.0", "event": "new_feedback", "data": [data]}
    return "EVENT_JSON:" + json.dumps(event)


def test_every_registered_escrow_has_a_verifier_row():
    from types import MappingProxyType

    from uvd_x402_sdk import near_escrow as es

    assert erc.NEAR_ERC8004_ESCROW_VERIFIERS.keys() == erc.NEAR_ERC8004_CONTRACTS.keys()
    assert dict(erc.NEAR_ERC8004_ESCROW_VERIFIERS["near:mainnet"]) == {
        "v1.arras-escrow.near": "erc8004-verifier.arras-escrow.near",
        "v2.arras-escrow.near": PAYMENT_PROOF,
    }
    assert dict(erc.NEAR_ERC8004_ESCROW_VERIFIERS["near:testnet"]) == {
        "arras-v1b-1101.testnet": "erc8004-verifier.arras-dev.testnet",
    }
    for network, rows in erc.NEAR_ERC8004_ESCROW_VERIFIERS.items():
        assert isinstance(rows, MappingProxyType)
        # One row per escrow the SDK signs for: a new escrow cannot land without deciding it.
        assert tuple(rows) == es.NEAR_ESCROW_CONTRACTS[network]
        # The v1 constants are the v1 row of the map.
        assert (
            rows[erc.NEAR_ERC8004_ESCROWS[network]]
            == erc.NEAR_ERC8004_CONTRACTS[network]["verifier"]
        )
        # A verifier reads one escrow: no account is the verifier of two.
        assert len(set(rows.values())) == len(rows)
        for escrow in es.NEAR_ESCROW_V2_CONTRACTS[network]:
            assert rows[escrow] == PAYMENT_PROOF
    with pytest.raises(TypeError):
        erc.NEAR_ERC8004_ESCROW_VERIFIERS["near:mainnet"]["v2.arras-escrow.near"] = V2_VERIFIER


@pytest.mark.parametrize("network", NETWORKS)
def test_the_v1_escrow_is_rated_by_the_current_verifier(network):
    verifier = erc.near_erc8004_verifier(network, erc.NEAR_ERC8004_ESCROWS[network])
    assert verifier == erc.NEAR_ERC8004_CONTRACTS[network]["verifier"]
    tx = feedback_with_proof(verifier, network)
    assert tx["actions"][0]["params"]["args"]["verifier"] == verifier


def test_an_escrow_whose_row_is_none_fails_closed(monkeypatch):
    # 0.113.0's v2 row, and any escrow registered before its verifier is deployed.
    patched_v2_row(monkeypatch, None)
    with pytest.raises(ValueError, match="no ERC-8004 verifier is registered"):
        erc.near_erc8004_verifier("near:mainnet", "v2.arras-escrow.near")
    for verifier in (PAYMENT_PROOF, V2_VERIFIER):
        with pytest.raises(ValueError, match="unsupported verifier"):
            feedback_with_proof(verifier)
    # A receipt naming that verifier is not read as a rating either.
    reputation = erc.NEAR_ERC8004_CONTRACTS["near:mainnet"]["reputation"]
    v1 = erc.NEAR_ERC8004_CONTRACTS["near:mainnet"]["verifier"]
    events = erc.parse_erc8004_events(
        [new_feedback_log(V2_VERIFIER), new_feedback_log(v1)],
        network="near:mainnet",
        executor_id=reputation,
    )
    assert [e["data"][0]["proof"]["verifier"] for e in events] == [v1]


def test_the_v2_verifier_is_a_data_patch(monkeypatch):
    patched_v2_row(monkeypatch)
    assert erc.near_erc8004_verifier("near:mainnet", "v2.arras-escrow.near") == (V2_VERIFIER)
    tx = feedback_with_proof(V2_VERIFIER)
    assert tx["actions"][0]["params"]["args"]["verifier"] == V2_VERIFIER
    events = erc.parse_erc8004_events(
        [new_feedback_log(V2_VERIFIER)],
        network="near:mainnet",
        executor_id=erc.NEAR_ERC8004_CONTRACTS["near:mainnet"]["reputation"],
    )
    assert len(events) == 1
    # The v1 escrow still names its own verifier, and testnet did not move.
    assert (
        erc.near_erc8004_verifier("near:mainnet", "v1.arras-escrow.near")
        == (erc.NEAR_ERC8004_CONTRACTS["near:mainnet"]["verifier"])
    )
    with pytest.raises(ValueError):
        feedback_with_proof(V2_VERIFIER, "near:testnet")


@pytest.mark.parametrize(
    "network,escrow",
    [
        ("near:mainnet", "v1.arras-escrow.near "),
        ("near:mainnet", "V1.arras-escrow.near"),
        ("near:mainnet", "arras-v1b-1101.testnet"),
        ("near:mainnet", "erc8004-verifier.arras-escrow.near"),
        ("near:testnet", "v1.arras-escrow.near"),
        ("near:testnet", "v2.arras-escrow.near"),
        ("near:mainnet", ""),
        ("near:mainnet", None),
        ("near:mainnet", ["v1.arras-escrow.near"]),
        ("near", "v1.arras-escrow.near"),
        (None, "v1.arras-escrow.near"),
    ],
)
def test_an_unregistered_escrow_has_no_verifier(network, escrow):
    with pytest.raises(ValueError):
        erc.near_erc8004_verifier(network, escrow)


# --- the verifier config: read from the verifier of the payment, bound to its escrow ------------


def verifier_config(escrow="v1.arras-escrow.near", **changes):
    return {**raw_config("verifier"), "escrow": escrow, **changes}


def rpc_by_account(answers, calls=None):
    """get_config by account; an account without an answer does not exist (as NEAR says)."""

    def rpc(method, params):
        if calls is not None:
            calls.append(params["account_id"])
        if params["account_id"] not in answers:
            raise RuntimeError(f"account {params['account_id']} does not exist while viewing")
        return {"result": {"result": list(json.dumps(answers[params["account_id"]]).encode())}}

    return rpc


def never_rpc(method, params):
    pytest.fail("an unregistered verifier reached RPC")


VERIFY = dict(network="near:mainnet", signer_id="payer.near", client="payer.near", proof="p")


def test_with_a_none_row_no_verifier_config_rates_its_payments(monkeypatch):
    patched_v2_row(monkeypatch, None)
    client = erc.NearErc8004Client("near:mainnet", rpc=never_rpc)
    for verifier in (
        PAYMENT_PROOF,
        V2_VERIFIER,
        erc.NEAR_ERC8004_CONTRACTS["near:testnet"]["verifier"],
        erc.NEAR_ERC8004_CONTRACTS["near:mainnet"]["reputation"],
        "v2.arras-escrow.near",
        V1_VERIFIER.upper(),
        V1_VERIFIER + " ",
        "",
        1,
        [V1_VERIFIER],
    ):
        with pytest.raises(ValueError, match="unsupported verifier"):
            client.get_config("verifier", verifier=verifier)
    # A snapshot that reads v2 is refused, whoever answered it: nothing rates v2 with a None row.
    with pytest.raises(ValueError, match="escrow"):
        erc.parse_erc8004_config(
            verifier_config("v2.arras-escrow.near"), network="near:mainnet", role="verifier"
        )
    v1_says_v2 = rpc_by_account({V1_VERIFIER: verifier_config("v2.arras-escrow.near")})
    with pytest.raises(ValueError, match="escrow"):
        erc.NearErc8004Client("near:mainnet", rpc=v1_says_v2).get_config("verifier")


@pytest.mark.parametrize("role", ["identity", "reputation"])
def test_the_verifier_argument_is_only_for_the_verifier_config(role):
    client = erc.NearErc8004Client("near:mainnet", rpc=never_rpc)
    with pytest.raises(ValueError, match="only to role verifier"):
        client.get_config(role, verifier=V1_VERIFIER)
    with pytest.raises(ValueError, match="only to role verifier"):
        erc.parse_erc8004_config(
            raw_config(role), network="near:mainnet", role=role, verifier=V1_VERIFIER
        )


def test_the_default_verifier_config_is_v1s_and_bound_to_it():
    calls = []
    client = erc.NearErc8004Client(
        "near:mainnet", rpc=rpc_by_account({V1_VERIFIER: verifier_config()}, calls)
    )
    cfg = client.get_config("verifier")
    assert calls == [V1_VERIFIER] and cfg.data["escrow"] == "v1.arras-escrow.near"
    assert client.get_config("verifier", verifier=V1_VERIFIER) == cfg
    assert calls == [V1_VERIFIER, V1_VERIFIER]
    assert (
        erc.build_erc8004_verify_feedback_proof(config=cfg, **VERIFY)["receiverId"] == V1_VERIFIER
    )


def test_with_the_v2_row_filled_get_config_reads_the_verifier_asked_for(monkeypatch):
    patched_v2_row(monkeypatch)
    calls = []
    answers = {
        V1_VERIFIER: verifier_config(),
        V2_VERIFIER: verifier_config("v2.arras-escrow.near"),
    }
    client = erc.NearErc8004Client("near:mainnet", rpc=rpc_by_account(answers, calls))
    # Per task: the verifier of the escrow it was paid in, then that verifier's config.
    for escrow, account in (
        ("v1.arras-escrow.near", V1_VERIFIER),
        ("v2.arras-escrow.near", V2_VERIFIER),
    ):
        verifier = erc.near_erc8004_verifier("near:mainnet", escrow)
        assert verifier == account
        cfg = client.get_config("verifier", verifier=verifier)
        assert calls[-1] == account and cfg.data["escrow"] == escrow
        assert (
            erc.build_erc8004_verify_feedback_proof(config=cfg, **VERIFY)["receiverId"] == account
        )
    assert client.get_config("verifier").data["escrow"] == "v1.arras-escrow.near"
    assert calls[-1] == V1_VERIFIER


def test_a_verifier_config_read_from_another_verifier_is_refused(monkeypatch):
    patched_v2_row(monkeypatch)
    swapped = rpc_by_account(
        {V1_VERIFIER: verifier_config("v2.arras-escrow.near"), V2_VERIFIER: verifier_config()}
    )
    for verifier in (None, V1_VERIFIER, V2_VERIFIER):
        with pytest.raises(ValueError, match="another verifier"):
            erc.NearErc8004Client("near:mainnet", rpc=swapped).get_config(
                "verifier", verifier=verifier
            )
    with pytest.raises(ValueError, match="another verifier"):
        erc.parse_erc8004_config(
            verifier_config(), network="near:mainnet", role="verifier", verifier=V2_VERIFIER
        )
    # A caller-held snapshot (no account given) is bound by its escrow alone.
    unbound = erc.parse_erc8004_config(
        verifier_config("v2.arras-escrow.near"), network="near:mainnet", role="verifier"
    )
    assert erc.build_erc8004_verify_feedback_proof(config=unbound, **VERIFY)["receiverId"] == (
        V2_VERIFIER
    )


@pytest.mark.parametrize(
    "escrow",
    [
        "arras-v1b-1101.testnet",
        "v3.arras-escrow.near",
        "evil-sink.near",
        "V2.arras-escrow.near",
        "v2.arras-escrow.near ",
        "",
        None,
        ["v2.arras-escrow.near"],
    ],
    ids=repr,
)
def test_a_verifier_config_of_an_escrow_without_a_row_is_refused(monkeypatch, escrow):
    patched_v2_row(monkeypatch)
    with pytest.raises(ValueError, match="escrow"):
        erc.parse_erc8004_config(verifier_config(escrow), network="near:mainnet", role="verifier")
    # Another network's rows never count.
    with pytest.raises(ValueError, match="escrow"):
        erc.parse_erc8004_config(
            verifier_config("v2.arras-escrow.near"), network="near:testnet", role="verifier"
        )


def test_verify_feedback_proof_goes_to_the_verifier_of_its_config(monkeypatch):
    patched_v2_row(monkeypatch)
    v1 = config("verifier")
    v2 = erc.parse_erc8004_config(
        verifier_config("v2.arras-escrow.near", min_gas_verify=str(27 * erc.TGAS)),
        network="near:mainnet",
        role="verifier",
    )
    tx = erc.build_erc8004_verify_feedback_proof(config=v2, **VERIFY)
    assert tx["receiverId"] == V2_VERIFIER
    # Its gas floor is the one of the config it was quoted with.
    with pytest.raises(ValueError, match="gas"):
        erc.build_erc8004_verify_feedback_proof(config=v2, gas=str(26 * erc.TGAS), **VERIFY)
    low = erc.build_erc8004_verify_feedback_proof(config=v1, gas=str(26 * erc.TGAS), **VERIFY)
    assert low["receiverId"] == V1_VERIFIER
    signed = erc.sign_erc8004_transaction(
        tx, Signer(), "1", "1" * 32, network="near:mainnet", config=v2
    )
    assert isinstance(signed, str)
    # Signing rebuilds the receiver: a v2 quote never goes to v1's verifier, nor the reverse.
    for descriptor, cfg in (
        ({**tx, "receiverId": V1_VERIFIER}, v2),
        ({**low, "receiverId": V2_VERIFIER}, v1),
    ):
        with pytest.raises(ValueError, match="receiver"):
            erc.sign_erc8004_transaction(
                descriptor, NoKey(), "1", "1" * 32, network="near:mainnet", config=cfg
            )


def test_fails_closed_when_the_v2_verifier_does_not_answer(monkeypatch):
    patched_v2_row(monkeypatch)
    down = erc.NearErc8004Client(
        "near:mainnet", rpc=rpc_by_account({V1_VERIFIER: verifier_config()})
    )
    with pytest.raises(RuntimeError, match="does not exist"):
        down.get_config("verifier", verifier=V2_VERIFIER)
    assert down.get_config("verifier").data["escrow"] == "v1.arras-escrow.near"
    # A JSON-RPC error (the account does not exist, the view failed) is never a config.
    for answer in (
        {"error": {"name": "HANDLER_ERROR", "cause": {"name": "UNKNOWN_ACCOUNT"}}},
        {"result": {"error": "wasm execution failed"}},
        {"result": {}},
        None,
    ):
        client = erc.NearErc8004Client("near:mainnet", rpc=lambda m, p, a=answer: a)
        with pytest.raises(ValueError):
            client.get_config("verifier", verifier=V2_VERIFIER)


def verify_outcome(receiver):
    value = {"counterparty": "worker.near", "role": "payer", "context": "em", "weight": "1"}
    args = base64.b64encode(b'{"client":"payer.near","proof":"p"}').decode()
    return {
        "transaction": {
            "receiver_id": receiver,
            "signer_id": "payer.near",
            "actions": [{"FunctionCall": {"method_name": "verify_feedback_proof", "args": args}}],
        },
        "status": {"SuccessValue": base64.b64encode(json.dumps(value).encode()).decode()},
    }


def test_a_verify_outcome_names_a_registered_verifier(monkeypatch):
    def parse(receiver):
        return erc.parse_erc8004_outcome(
            verify_outcome(receiver), network="near:mainnet", method="verify_feedback_proof"
        )

    assert parse(V1_VERIFIER).status == parse(PAYMENT_PROOF).status == "accepted"
    refused = (V2_VERIFIER, "erc8004-verifier.arras-dev.testnet", None, [1])
    for receiver in refused:
        with pytest.raises(ValueError, match="provenance"):
            parse(receiver)
    patched_v2_row(monkeypatch)
    assert parse(V2_VERIFIER).status == parse(V1_VERIFIER).status == "accepted"
    for receiver in (PAYMENT_PROOF, *refused[1:]):
        with pytest.raises(ValueError, match="provenance"):
            parse(receiver)


@pytest.mark.parametrize("code", [1, 15, 16])
def test_a_verifier_panic_is_an_error_code_e16_included(monkeypatch, code):
    # E16 is v2's verifier only (a dispute expired in the receiver's favour): an error the
    # caller reads, never "unrecognized" (which reads as pending or malformed).
    patched_v2_row(monkeypatch)
    outcome = verify_outcome(V2_VERIFIER)
    message = f"Smart contract panicked: ERC8004_ARV_E{code:02}: refused"
    outcome["status"] = {
        "Failure": {"ActionError": {"kind": {"FunctionCallError": {"ExecutionError": message}}}}
    }
    result = erc.parse_erc8004_outcome(
        outcome, network="near:mainnet", method="verify_feedback_proof"
    )
    assert result.status == "error" and result.panic.value == f"ERC8004_ARV_E{code:02}"
    outcome["status"]["Failure"]["ActionError"]["kind"]["FunctionCallError"]["ExecutionError"] = (
        "Smart contract panicked: ERC8004_ARV_E17: refused"
    )
    with pytest.raises(ValueError, match="unrecognized"):
        erc.parse_erc8004_outcome(outcome, network="near:mainnet", method="verify_feedback_proof")


# --- 0.114.0: v2's verifier registered, and the payment's escrow checked against it -------------

V1_ESCROW, V2_ESCROW = "v1.arras-escrow.near", "v2.arras-escrow.near"
# payment-proof.arras-escrow.near's get_config, read by c0der on 2026-11-13 (private RPC, final).
MEASURED_V2_CONFIG = {
    "contract": "erc8004-arras-verifier",
    "escrow": V2_ESCROW,
    "min_gas_verify": "25000000000000",
    "gas_get_payment": "10000000000000",
    "gas_on_payment": "10000000000000",
}
# Its verify_feedback_proof of the released payment 9nySy6vT... (tx ixnTdSzC..., same day).
MEASURED_V2_ANSWER = {
    "counterparty": "worker-v2.arras-escrow.near",
    "role": "receiver",
    "context": "em",
    "weight": "50000",
}
PAIRS = [(V1_ESCROW, V1_VERIFIER), (V2_ESCROW, PAYMENT_PROOF)]
CROSSED = [(V1_ESCROW, PAYMENT_PROOF), (V2_ESCROW, V1_VERIFIER)]
CROSS = "verifier is not the ERC-8004 verifier of the payment's escrow"


def v2_config(**changes):
    return erc.parse_erc8004_config(
        {**MEASURED_V2_CONFIG, **changes}, network="near:mainnet", role="verifier"
    )


def test_v2_payments_are_rated_by_payment_proof():
    # No identity check against the package re-exports here: the mutation runner swaps this
    # module's object, so one would fail for every mutant (test_near_erc8004_api pins them).
    assert erc.near_erc8004_verifier("near:mainnet", V2_ESCROW) == PAYMENT_PROOF
    assert erc.near_erc8004_verifier("near:mainnet", V1_ESCROW) == V1_VERIFIER
    assert erc._verifiers("near:mainnet") == {V1_VERIFIER, PAYMENT_PROOF}
    assert erc._verifiers("near:testnet") == {"erc8004-verifier.arras-dev.testnet"}
    # v2 is a mainnet row only: testnet has no v2 escrow and no second verifier.
    with pytest.raises(ValueError, match="registered Arras contract"):
        erc.near_erc8004_verifier("near:testnet", V2_ESCROW)
    # The v1 constants, read by consumers of 0.113.0, are v1's pair as before.
    assert erc.NEAR_ERC8004_CONTRACTS["near:mainnet"]["verifier"] == V1_VERIFIER
    assert erc.NEAR_ERC8004_ESCROWS["near:mainnet"] == V1_ESCROW


def test_the_measured_v2_config_is_accepted_from_payment_proof():
    calls = []
    answers = {V1_VERIFIER: verifier_config(), PAYMENT_PROOF: MEASURED_V2_CONFIG}
    client = erc.NearErc8004Client("near:mainnet", rpc=rpc_by_account(answers, calls))
    by_account = client.get_config("verifier", verifier=PAYMENT_PROOF)
    by_escrow = client.get_config("verifier", escrow=V2_ESCROW)
    assert calls == [PAYMENT_PROOF, PAYMENT_PROOF]
    assert dict(by_account.data) == dict(by_escrow.data) == MEASURED_V2_CONFIG
    captured = erc.parse_erc8004_config(
        MEASURED_V2_CONFIG, network="near:mainnet", role="verifier", verifier=PAYMENT_PROOF
    )
    for cfg in (by_account, by_escrow, captured, v2_config()):
        tx = erc.build_erc8004_verify_feedback_proof(config=cfg, escrow=V2_ESCROW, **VERIFY)
        assert tx["receiverId"] == PAYMENT_PROOF
        assert tx["actions"][0]["params"]["gas"] == str(50 * erc.TGAS)
        assert tx["actions"][0]["params"]["deposit"] == "0"


@pytest.mark.parametrize(
    "escrow",
    [
        V1_ESCROW,
        "arras-v1b-1101.testnet",
        "v3.arras-escrow.near",
        "V2.arras-escrow.near",
        "v2.arras-escrow.near ",
        " v2.arras-escrow.near",
        PAYMENT_PROOF,
        "",
        None,
        [V2_ESCROW],
    ],
    ids=repr,
)
def test_a_payment_proof_config_that_reads_another_escrow_is_refused(escrow):
    answer = {**MEASURED_V2_CONFIG, "escrow": escrow}
    client = erc.NearErc8004Client("near:mainnet", rpc=rpc_by_account({PAYMENT_PROOF: answer}))
    with pytest.raises(ValueError, match="escrow|another verifier"):
        client.get_config("verifier", verifier=PAYMENT_PROOF)
    with pytest.raises(ValueError, match="escrow|another verifier"):
        client.get_config("verifier", escrow=V2_ESCROW)


def test_payment_proof_is_trusted_by_its_account_not_its_config():
    # Another account answering v2's exact config is not v2's verifier.
    impostor = rpc_by_account({"payment-proof.evil.near": MEASURED_V2_CONFIG})
    client = erc.NearErc8004Client("near:mainnet", rpc=impostor)
    with pytest.raises(ValueError, match="unsupported verifier"):
        client.get_config("verifier", verifier="payment-proof.evil.near")
    # v1's verifier answering v2's config, and payment-proof answering v1's, are refused.
    swapped = rpc_by_account(
        {V1_VERIFIER: MEASURED_V2_CONFIG, PAYMENT_PROOF: verifier_config(V1_ESCROW)}
    )
    for kwargs in (
        {},
        {"verifier": V1_VERIFIER},
        {"escrow": V1_ESCROW},
        {"verifier": PAYMENT_PROOF},
        {"escrow": V2_ESCROW},
    ):
        with pytest.raises(ValueError, match="another verifier"):
            erc.NearErc8004Client("near:mainnet", rpc=swapped).get_config("verifier", **kwargs)
    for change in ({"contract": "erc8004-payment-proof"}, {"min_gas_verify": str(24 * erc.TGAS)}):
        with pytest.raises(ValueError):
            v2_config(**change)


def feedback(network="near:mainnet", **options):
    return erc.build_erc8004_give_feedback_with_proof(
        network=network,
        signer_id="payer.near",
        config=config("reputation", network),
        agent_id="0",
        value="1",
        value_decimals=0,
        proof="9nySy6vTK95bgPUm5MkdsZtzC9XaGrSCV1EjNnRR6uyC",
        **options,
    )


@pytest.mark.parametrize("escrow,verifier", PAIRS)
def test_give_feedback_with_proof_takes_the_verifier_of_the_payments_escrow(escrow, verifier):
    plain = feedback(verifier=verifier)
    bound = feedback(verifier=verifier, escrow=escrow)
    # The escrow is checked, never sent: the descriptor is the 0.113.0 one.
    assert bound == plain
    assert bound["receiverId"] == erc.NEAR_ERC8004_CONTRACTS["near:mainnet"]["reputation"]
    assert bound["actions"][0]["params"]["args"]["verifier"] == verifier
    cfg = config("reputation")
    assert erc.erc8004_args_json(
        bound, network="near:mainnet", config=cfg, escrow=escrow
    ) == erc.erc8004_args_json(bound, network="near:mainnet", config=cfg)
    signed = erc.sign_erc8004_transaction(
        bound, Signer(), "1", "1" * 32, network="near:mainnet", config=cfg, escrow=escrow
    )
    assert isinstance(signed, str)


@pytest.mark.parametrize("escrow,verifier", CROSSED)
def test_a_crossed_verifier_is_refused_before_the_key(escrow, verifier):
    # The other verifier answers E12 (no such payment): the registry refunds the deposit and the
    # rating is lost without an error. With the payment's escrow, the SDK refuses it first.
    with pytest.raises(ValueError, match=CROSS):
        feedback(verifier=verifier, escrow=escrow)
    cfg = config("reputation")
    descriptor = feedback(verifier=verifier)
    with pytest.raises(ValueError, match=CROSS):
        erc.sign_erc8004_transaction(
            descriptor, NoKey(), "1", "1" * 32, network="near:mainnet", config=cfg, escrow=escrow
        )
    with pytest.raises(ValueError, match=CROSS):
        erc.erc8004_args_json(descriptor, network="near:mainnet", config=cfg, escrow=escrow)


@pytest.mark.parametrize(
    "escrow",
    [
        "V2.arras-escrow.near",
        "v2.arras-escrow.near ",
        "v2.arras-escrow.testnet",
        "arras-v1b-1101.testnet",
        PAYMENT_PROOF,
        "",
        False,
        0,
        1,
        True,
        [V2_ESCROW],
    ],
    ids=repr,
)
def test_an_unregistered_escrow_is_refused_on_every_proof_path(escrow):
    with pytest.raises(ValueError, match="registered Arras contract"):
        feedback(verifier=PAYMENT_PROOF, escrow=escrow)
    with pytest.raises(ValueError, match="registered Arras contract"):
        erc.build_erc8004_verify_feedback_proof(config=v2_config(), escrow=escrow, **VERIFY)
    client = erc.NearErc8004Client("near:mainnet", rpc=never_rpc)
    with pytest.raises(ValueError, match="registered Arras contract|unsupported verifier"):
        client.get_config("verifier", escrow=escrow)
    with pytest.raises(ValueError, match="registered Arras contract"):
        client.get_proof_feedback(PAYMENT_PROOF, "p", "payer.near", escrow=escrow)


def test_escrow_is_refused_where_it_proves_nothing():
    with pytest.raises(ValueError, match="escrow applies only"):
        erc.build_erc8004_give_feedback(
            network="near:mainnet",
            signer_id="payer.near",
            config=config("reputation"),
            agent_id="0",
            value="1",
            value_decimals=0,
            escrow=V2_ESCROW,
        )
    with pytest.raises(ValueError, match="escrow applies only"):
        build("register", {"agent_uri": "ipfs://card"}, escrow=V1_ESCROW)
    client = erc.NearErc8004Client("near:mainnet", rpc=never_rpc)
    for role in ("identity", "reputation"):
        with pytest.raises(ValueError, match="escrow applies only to role verifier"):
            client.get_config(role, escrow=V2_ESCROW)


def test_a_none_row_is_refused_with_the_escrow_given(monkeypatch):
    patched_v2_row(monkeypatch, None)
    with pytest.raises(ValueError, match="no ERC-8004 verifier is registered"):
        feedback(verifier=V1_VERIFIER, escrow=V2_ESCROW)
    with pytest.raises(ValueError, match="no ERC-8004 verifier is registered"):
        erc.NearErc8004Client("near:mainnet", rpc=never_rpc).get_config(
            "verifier", escrow=V2_ESCROW
        )


def test_testnet_keeps_its_one_pair():
    tx = feedback(
        "near:testnet",
        verifier="erc8004-verifier.arras-dev.testnet",
        escrow="arras-v1b-1101.testnet",
    )
    assert tx["actions"][0]["params"]["args"]["verifier"] == "erc8004-verifier.arras-dev.testnet"
    with pytest.raises(ValueError):
        feedback("near:testnet", verifier=PAYMENT_PROOF, escrow=V2_ESCROW)
    with pytest.raises(ValueError, match="registered Arras contract"):
        feedback("near:testnet", verifier="erc8004-verifier.arras-dev.testnet", escrow=V1_ESCROW)
    # get_config by escrow reads the testnet verifier, and refuses a mainnet escrow before RPC.
    calls = []
    answers = {"erc8004-verifier.arras-dev.testnet": verifier_config("arras-v1b-1101.testnet")}
    client = erc.NearErc8004Client("near:testnet", rpc=rpc_by_account(answers, calls))
    cfg = client.get_config("verifier", escrow="arras-v1b-1101.testnet")
    assert calls == ["erc8004-verifier.arras-dev.testnet"]
    assert cfg.data["escrow"] == "arras-v1b-1101.testnet"
    for escrow in (V1_ESCROW, V2_ESCROW):
        with pytest.raises(ValueError, match="registered Arras contract"):
            client.get_config("verifier", escrow=escrow)
    assert len(calls) == 1


def test_verify_feedback_proof_escrow_must_be_its_configs():
    v1, v2 = config("verifier"), v2_config()
    assert erc.build_erc8004_verify_feedback_proof(config=v1, escrow=V1_ESCROW, **VERIFY) == (
        erc.build_erc8004_verify_feedback_proof(config=v1, **VERIFY)
    )
    assert (
        erc.build_erc8004_verify_feedback_proof(config=v2, escrow=V2_ESCROW, **VERIFY)["receiverId"]
        == PAYMENT_PROOF
    )
    for cfg, escrow in ((v1, V2_ESCROW), (v2, V1_ESCROW)):
        with pytest.raises(ValueError, match=CROSS):
            erc.build_erc8004_verify_feedback_proof(config=cfg, escrow=escrow, **VERIFY)
        descriptor = erc.build_erc8004_verify_feedback_proof(config=cfg, **VERIFY)
        with pytest.raises(ValueError, match=CROSS):
            erc.sign_erc8004_transaction(
                descriptor,
                NoKey(),
                "1",
                "1" * 32,
                network="near:mainnet",
                config=cfg,
                escrow=escrow,
            )


@pytest.mark.parametrize("escrow,verifier", CROSSED)
def test_get_config_refuses_a_crossed_pair_before_rpc(escrow, verifier):
    client = erc.NearErc8004Client("near:mainnet", rpc=never_rpc)
    with pytest.raises(ValueError, match=CROSS):
        client.get_config("verifier", verifier=verifier, escrow=escrow)


@pytest.mark.parametrize("escrow,verifier", PAIRS)
def test_get_config_by_escrow_reads_its_verifier(escrow, verifier):
    calls = []
    answers = {V1_VERIFIER: verifier_config(), PAYMENT_PROOF: MEASURED_V2_CONFIG}
    client = erc.NearErc8004Client("near:mainnet", rpc=rpc_by_account(answers, calls))
    assert client.get_config("verifier", escrow=escrow).data["escrow"] == escrow
    assert client.get_config("verifier", verifier=verifier, escrow=escrow).data["escrow"] == escrow
    assert calls == [verifier, verifier]


VERIFIER_VIEWS = {
    "get_verified_summary": (
        lambda c, v, **kw: c.get_verified_summary("0", v, context="em", **kw),
        {"count": "1", "summary_value": "90", "summary_value_decimals": 0, "weight_sum": "50000"},
    ),
    "list_verified_feedback": (
        lambda c, v, **kw: c.list_verified_feedback("0", v, from_index=0, limit=25, **kw),
        [{"agent_id": "0", "client_address": "payer.near", "feedback_index": "1"}],
    ),
    "get_proof_feedback": (
        lambda c, v, **kw: c.get_proof_feedback(v, "p", "payer.near", **kw),
        {"agent_id": "0", "client_address": "payer.near", "feedback_index": "1"},
    ),
    "is_proof_pending": (lambda c, v, **kw: c.is_proof_pending(v, "p", "payer.near", **kw), False),
}


@pytest.mark.parametrize("view", VERIFIER_VIEWS)
def test_verifier_views_take_the_payments_escrow(view):
    call, answer = VERIFIER_VIEWS[view]
    # Without escrow= a view takes any account as verifier (0.113.0): with it, only the escrow's.
    foreign = [(V2_ESCROW, "evil.near"), (V1_ESCROW, "evil.near"), (V2_ESCROW, V2_VERIFIER)]
    for escrow, verifier in CROSSED + foreign:
        with pytest.raises(ValueError, match=CROSS):
            call(erc.NearErc8004Client("near:mainnet", rpc=never_rpc), verifier, escrow=escrow)
    with pytest.raises(ValueError, match="registered Arras contract"):
        call(erc.NearErc8004Client("near:testnet", rpc=never_rpc), V1_VERIFIER, escrow=V1_ESCROW)
    sent = []

    def rpc_for(network):
        def rpc(method, params):
            sent.append(json.loads(base64.b64decode(params["args_base64"])))
            assert params["account_id"] == erc.NEAR_ERC8004_CONTRACTS[network]["reputation"]
            assert params["method_name"] == view
            return {"result": {"result": list(json.dumps(answer).encode())}}

        return rpc

    testnet_pair = ("arras-v1b-1101.testnet", "erc8004-verifier.arras-dev.testnet")
    for network, pairs in (("near:mainnet", PAIRS), ("near:testnet", [testnet_pair])):
        client = erc.NearErc8004Client(network, rpc=rpc_for(network))
        for escrow, verifier in pairs:
            assert call(client, verifier, escrow=escrow) == answer
            # Without the escrow, the 0.113.0 call: the same request.
            assert call(client, verifier) == answer
            assert sent[-1] == sent[-2] and sent[-1]["verifier"] == verifier
            assert "escrow" not in sent[-1]


def v2_feedback_outcome():
    """The recorded v1 rating (mainnet), as the same rating through payment-proof would read."""
    outcome = json.loads(
        json.dumps(FIXTURE["networks"][0]["transactions"][2]["result"]).replace(
            V1_VERIFIER, PAYMENT_PROOF
        )
    )
    call = outcome["transaction"]["actions"][0]["FunctionCall"]
    args = json.loads(base64.b64decode(call["args"]))
    assert args["verifier"] == V1_VERIFIER
    call["args"] = base64.b64encode(
        json.dumps({**args, "verifier": PAYMENT_PROOF}).encode()
    ).decode()
    return outcome


def test_a_v2_rating_parses_end_to_end():
    outcome = v2_feedback_outcome()
    parsed = erc.parse_erc8004_outcome(
        outcome, network="near:mainnet", method="give_feedback_with_proof"
    )
    assert parsed.status == "accepted" and parsed.value == "1"
    events = erc.parse_erc8004_receipt_events(outcome["receipts_outcome"], "near:mainnet")
    assert [e["data"][0]["proof"]["verifier"] for e in events if e["event"] == "new_feedback"] == [
        PAYMENT_PROOF
    ]
    reputation = erc.NEAR_ERC8004_CONTRACTS["near:mainnet"]["reputation"]
    logs = [new_feedback_log(v) for v in (PAYMENT_PROOF, V2_VERIFIER, "payment-proof.evil.near")]
    events = erc.parse_erc8004_events(logs, network="near:mainnet", executor_id=reputation)
    assert [e["data"][0]["proof"]["verifier"] for e in events] == [PAYMENT_PROOF]
    # The event must name the verifier the transaction was sent with.
    call = outcome["transaction"]["actions"][0]["FunctionCall"]
    args = json.loads(base64.b64decode(call["args"]))
    call["args"] = base64.b64encode(json.dumps({**args, "verifier": V1_VERIFIER}).encode()).decode()
    with pytest.raises(ValueError, match="matching receipt event"):
        erc.parse_erc8004_outcome(
            outcome, network="near:mainnet", method="give_feedback_with_proof"
        )


def test_the_measured_v2_answer_is_an_accepted_verification():
    outcome = verify_outcome(PAYMENT_PROOF)
    outcome["status"] = {
        "SuccessValue": base64.b64encode(json.dumps(MEASURED_V2_ANSWER).encode()).decode()
    }
    parsed = erc.parse_erc8004_outcome(
        outcome, network="near:mainnet", method="verify_feedback_proof"
    )
    assert parsed.status == "accepted" and parsed.value == MEASURED_V2_ANSWER


def panicked(code, receiver=PAYMENT_PROOF):
    outcome = verify_outcome(receiver)
    message = f"Smart contract panicked: {code}: refused"
    outcome["status"] = {
        "Failure": {"ActionError": {"kind": {"FunctionCallError": {"ExecutionError": message}}}}
    }
    return outcome


@pytest.mark.parametrize("code", ["ERC8004_ARV_E16", "ERC8004_ARV_E13", "ERC8004_ARV_E12"])
def test_e16_from_payment_proof_is_a_refused_proof_not_an_unknown_failure(code):
    # With the real row, no patch: a direct verify_feedback_proof that payment-proof refuses.
    parsed = erc.parse_erc8004_outcome(
        panicked(code), network="near:mainnet", method="verify_feedback_proof"
    )
    assert parsed.status == "error" and parsed.panic == erc.Erc8004PanicCode(code)
    assert parsed.panic.name == code.removeprefix("ERC8004_")


def test_e16_through_give_feedback_with_proof_is_the_registrys_rejection():
    # The registry's callback logs E45 (verifier failed) and returns null; the verifier's own
    # failed receipt (ERC8004_ARV_E16) is not the registry's and does not change the verdict.
    outcome = v2_feedback_outcome()
    outcome["status"] = {"SuccessValue": base64.b64encode(b"null").decode()}
    reputation = erc.NEAR_ERC8004_CONTRACTS["near:mainnet"]["reputation"]
    outcome["receipts_outcome"] = [
        {
            "outcome": {
                "executor_id": PAYMENT_PROOF,
                "status": {"Failure": {"ActionError": {"kind": "ERC8004_ARV_E16"}}},
                "logs": [],
            }
        },
        {
            "outcome": {
                "executor_id": reputation,
                "status": {"SuccessValue": "bnVsbA=="},
                "logs": ["erc8004:rechazo:" + erc.Erc8004Rejection.E45.value],
            }
        },
    ]
    parsed = erc.parse_erc8004_outcome(
        outcome, network="near:mainnet", method="give_feedback_with_proof"
    )
    assert parsed.status == "rejected" and parsed.rejection == erc.Erc8004Rejection.E45


# sha256 of v1 outputs, recorded from origin/main 6488e37 (0.113.0), Ed25519 seed 01 x 32: v1 did
# not move by one byte, with or without its escrow given. Never re-record to make a change pass.
V1_DIGESTS = {
    "near:mainnet give_feedback_with_proof descriptor": (
        "7a46ad90250d5c1ccc801e3f0f828c7831911371c97174941ca41ccb69635342"
    ),
    "near:mainnet give_feedback_with_proof args": (
        "27f03b86aa956f8f446bbf8d8c8f64e78564202076f175fdf55ab25326c33147"
    ),
    "near:mainnet give_feedback_with_proof signed": (
        "0656994eb927431fe6068b97e325687a51f74c06bf9e62fb1ab9aa592081f322"
    ),
    "near:mainnet verify_feedback_proof descriptor": (
        "587b1139ead0f3173e9a544fcc04e309160b1404e42937b2fef3345aba473ab6"
    ),
    "near:mainnet verify_feedback_proof args": (
        "f080ed0c86b5cd3f239dc03d5c0fa42b6e72c8b3559989f7be2ef89f87a02a62"
    ),
    "near:mainnet verify_feedback_proof signed": (
        "2c15a96437a8576acdbe5058141cde0bed251753fefd01b63bb30f4bbefd330c"
    ),
    "near:testnet give_feedback_with_proof descriptor": (
        "dc6ed7db6f5ae29cdb34c577abfd74ba18a50ac908ab44acd9dcec68e9644875"
    ),
    "near:testnet give_feedback_with_proof args": (
        "c8bb4d6caa96f52b05d214dd546b76f860f58801b926fe19a12e121846c87bae"
    ),
    "near:testnet give_feedback_with_proof signed": (
        "bd98ec5be465499774c40b7999bfe7b9f0ecd156c39af3b7a8a19a3694b95c4a"
    ),
    "near:testnet verify_feedback_proof descriptor": (
        "d2c17068e0667298ad5c702f785059698e69b1fcfa4124db6f07de620f601b38"
    ),
    "near:testnet verify_feedback_proof args": (
        "f080ed0c86b5cd3f239dc03d5c0fa42b6e72c8b3559989f7be2ef89f87a02a62"
    ),
    "near:testnet verify_feedback_proof signed": (
        "315fd909ab63dfcfeceade65a75f35e1344ab0615dd16ce3f2bea56649f1ed42"
    ),
}


class SeedSigner(Signer):
    def __init__(self):
        self.key = Ed25519PrivateKey.from_private_bytes(bytes([1]) * 32)


@pytest.mark.parametrize("with_escrow", [False, True])
def test_v1_outputs_did_not_move(with_escrow):
    def digest(value):
        raw = value if isinstance(value, bytes) else value.encode()
        return hashlib.sha256(raw).hexdigest()

    seen = {}
    for network in NETWORKS:
        verifier = erc.NEAR_ERC8004_CONTRACTS[network]["verifier"]
        bound = {"escrow": erc.NEAR_ERC8004_ESCROWS[network]} if with_escrow else {}
        rep, ver = config("reputation", network), config("verifier", network)
        proof = "EbkEtGaM7ZwY6aHSdHSqxdFx9cJV13KPT7fYBWjxbfd9"
        rating = erc.build_erc8004_give_feedback_with_proof(
            network=network,
            signer_id="payer.near",
            config=rep,
            agent_id="7",
            value="90",
            value_decimals=0,
            tag1="starred",
            verifier=verifier,
            proof=proof,
            **bound,
        )
        check = erc.build_erc8004_verify_feedback_proof(
            network=network,
            signer_id="payer.near",
            config=ver,
            client="payer.near",
            proof=proof,
            **bound,
        )
        for name, tx, cfg in (
            ("give_feedback_with_proof", rating, rep),
            ("verify_feedback_proof", check, ver),
        ):
            key = f"{network} {name}"
            seen[f"{key} descriptor"] = digest(
                json.dumps(tx, sort_keys=True, separators=(",", ":"))
            )
            seen[f"{key} args"] = digest(
                erc.erc8004_args_json(tx, network=network, config=cfg, **bound)
            )
            seen[f"{key} signed"] = digest(
                erc.sign_erc8004_transaction(
                    tx, SeedSigner(), "7", "1" * 32, network=network, config=cfg, **bound
                )
            )
    assert seen == V1_DIGESTS
