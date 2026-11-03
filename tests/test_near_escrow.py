"""Frozen Rust vectors and an independent decoder of every signed NEP-366 field."""

import base64
import builtins
import hashlib
import inspect
import json
import struct
from dataclasses import replace
from pathlib import Path
from types import MappingProxyType

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from uvd_x402_sdk import near_escrow as es
from uvd_x402_sdk.solana_signing import _b58encode

FIXTURE = Path(__file__).parent / "fixtures" / "near-escrow.json"
VECTORS = json.loads(FIXTURE.read_text(encoding="utf-8"))
V1 = VECTORS["payment_id"]["vectores"][0]
TOKEN = V1["token"]


def options(**changes):
    return replace(
        es.NearEscrowDelegateOptions(
            network="near:testnet",
            signer=es.NearEd25519Signer(bytes(range(32))),
            sender_id=V1["payer"],
            escrow_contract="arras-v1b-1101.testnet",
            nonce=7,
            max_block_height=100_000_000,
        ),
        **changes,
    )


def authorize(o=None, **changes):
    values = dict(token=TOKEN, amount=V1["amount"], msg=V1)
    values.update(changes)
    return es.sign_escrow_authorization(options() if o is None else o, **values)


def decode(payment):
    wire = base64.b64decode(payment["payload"]["signedDelegateAction"], validate=True)
    offset = 0

    def take(n):
        nonlocal offset
        result = wire[offset : offset + n]
        assert len(result) == n
        offset += n
        return result

    def integer(n):
        return int.from_bytes(take(n), "little")

    def string():
        return take(integer(4)).decode("utf-8")

    sender, receiver = string(), string()
    count = integer(4)
    assert count == 1
    assert integer(1) == 2  # FunctionCall, not Delegate
    method, args = string(), string()
    gas, deposit = integer(8), integer(16)
    nonce, height = integer(8), integer(8)
    assert integer(1) == 0  # Ed25519 key
    public_key = take(32)
    delegate = wire[:offset]
    assert integer(1) == 0  # Ed25519 signature
    signature = take(64)
    assert offset == len(wire)
    digest = hashlib.sha256(struct.pack("<I", (1 << 30) + 366) + delegate).digest()
    Ed25519PublicKey.from_public_bytes(public_key).verify(signature, digest)
    return dict(
        sender=sender,
        receiver=receiver,
        method=method,
        args=json.loads(args),
        args_text=args,
        gas=gas,
        deposit=deposit,
        nonce=nonce,
        height=height,
        public_key=public_key,
        signature=signature,
        digest=digest,
    )


def test_frozen_file_not_rewritten():
    assert (
        hashlib.sha256(FIXTURE.read_bytes()).hexdigest()
        == "fe81bfc72bd1e90a5afb86fb00eece59cbd78fde983eda397c797bf65351709b"
    )


@pytest.mark.parametrize("vector", VECTORS["payment_id"]["vectores"], ids=lambda v: v["id"])
def test_rust_vectors(vector):
    preimage = es.payment_id_preimage(vector)
    assert len(preimage) == vector["preimagen_bytes"]
    if "preimagen_hex" in vector:
        assert preimage.hex() == vector["preimagen_hex"]
    assert hashlib.sha256(preimage).hexdigest() == vector["sha256"]
    assert es.compute_payment_id(vector) == vector["payment_id"]


def test_canonical_msg_and_short_id():
    assert es.build_escrow_msg(V1) == VECTORS["msg_canonico_v1"]
    assert len(es.compute_payment_id({**V1, "salt": "0" * 28 + "0415"})) == 42
    assert (
        es.compute_payment_id({**V1, "salt": "0" * 28 + "0415"})
        == "1y1ZbrieUbaioiUBHxX7EyZMNkCis2tLKCs5Xxezgy"
    )


@pytest.mark.parametrize("vector", VECTORS["payment_id"]["vectores"], ids=lambda v: v["id"])
def test_authorization_wire(vector):
    # The mainnet vector still pins its message/id; signing uses registered testnet USDC.
    network = "near:testnet"
    o = options(sender_id=vector["payer"])
    payment = authorize(o, msg=vector, amount=vector["amount"])
    assert set(payment) == {"x402Version", "scheme", "network", "payload"}
    assert (
        payment["x402Version"] == 2
        and payment["scheme"] == "escrow"
        and payment["network"] == network
    )
    d = decode(payment)
    assert d["sender"] == vector["payer"] and d["receiver"] == TOKEN
    assert d["method"] == "ft_transfer_call"
    assert d["args"] == {
        "receiver_id": o.escrow_contract,
        "amount": vector["amount"],
        "msg": es.build_escrow_msg(vector),
    }
    assert d["gas"] == 100_000_000_000_000 and d["deposit"] == 1
    assert d["nonce"] == 7 and d["height"] == 100_000_000
    assert d["public_key"] == o.signer.public_key
    assert authorize(o, msg=vector, amount=vector["amount"]) == payment


@pytest.mark.parametrize(
    "field,value",
    [
        ("operator_id", ""),
        ("operator_id", "EM"),
        ("operator_id", " em"),
        ("operator_id", "em\n"),
        ("operator_id", "ém"),
        ("operator_id", "x" * 33),
        ("receiver", None),
        ("receiver", ""),
        ("receiver", "Worker.near"),
        ("receiver", "worker..near"),
        ("receiver", "a"),
        ("receiver", "x" * 65),
        ("receiver", ".near"),
        ("receiver", "worker-"),
        ("receiver", "wo_-rker.near"),
        ("receiver", "worker.near\n"),
        ("receiver", "ｗorker.near"),
        ("terms_hash", "A" * 64),
        ("terms_hash", "0x" + "0" * 62),
        ("terms_hash", "0X" + "0" * 62),
        ("terms_hash", "0" * 63),
        ("terms_hash", "0" * 64 + "\n"),
        ("terms_hash", None),
        ("salt", ""),
        ("salt", "a" * 31),
        ("salt", "g" * 32),
        ("release_deadline", True),
        ("release_deadline", 1.0),
        ("release_deadline", " 1"),
        ("release_deadline", "+1"),
        ("release_deadline", "01"),
        ("release_deadline", "1_000"),
        ("release_deadline", "１"),
        ("release_deadline", "1\n"),
        ("release_deadline", -1),
        ("release_deadline", 2**64),
        ("release_deadline", "1" * 10000),
        ("reclaim_after", V1["release_deadline"]),
        ("reclaim_after", 0),
        ("fee_bps_max", True),
        ("fee_bps_max", "1300"),
        ("fee_bps_max", -1),
        ("fee_bps_max", 2**16),
    ],
)
def test_msg_rejects(field, value):
    with pytest.raises(ValueError):
        es.build_escrow_msg({**V1, field: value})


def test_uint_boundaries_and_account_edges():
    p = {
        **V1,
        "release_deadline": 0,
        "reclaim_after": 2**64 - 1,
        "amount": 2**128 - 1,
        "fee_bps_max": 65535,
        "operator_id": "_" * 32,
        "receiver": "a" * 64,
    }
    assert es.compute_payment_id(p)
    assert json.loads(es.build_escrow_msg(p))["reclaim_after"] == str(2**64 - 1)
    for field in ("token", "payer"):
        with pytest.raises(ValueError):
            es.compute_payment_id({**V1, field: "a..near"})
    for bad in (-1, 2**128, True, 1.1, None, "00"):
        with pytest.raises(ValueError):
            es.compute_payment_id({**V1, "amount": bad})


@pytest.mark.parametrize(
    "change",
    [
        {"network": "near"},
        {"network": "near:MAINNET"},
        {"network": " near:testnet"},
        {"sender_id": "Bad.near"},
        {"escrow_contract": "a..near"},
        {"nonce": 0},
        {"nonce": True},
        {"nonce": 2**64},
        {"max_block_height": 0},
        {"max_block_height": 2**64},
        {"gas": 38_000_000_000_000 - 1},
        {"gas": 100_000_000_000_000 + 1},
        {"gas": 0},
    ],
)
def test_sign_rejects_options_before_key(change):
    class NoKey:
        @property
        def public_key(self):
            pytest.fail("accessed key for invalid intent")

    with pytest.raises(ValueError):
        authorize(options(signer=NoKey(), **change))


@pytest.mark.parametrize("gas", [38_000_000_000_000, 100_000_000_000_000])
def test_authorization_gas_edges(gas):
    assert decode(authorize(options(gas=gas)))["gas"] == gas


@pytest.mark.parametrize(
    "field,value", [("token", "wrong.near"), ("amount", 0), ("amount", 2**128)]
)
def test_authorization_rejects_intent(field, value):
    with pytest.raises(ValueError):
        authorize(**{field: value})


@pytest.mark.parametrize("receiver", [V1["payer"], "arras-v1b-1101.testnet"])
def test_self_receiver_rejected(receiver):
    with pytest.raises(ValueError):
        authorize(msg={**V1, "receiver": receiver})


@pytest.mark.parametrize(
    "tamper",
    [
        "deposit0",
        "deposit2",
        "two",
        "none",
        "receiver",
        "sender",
        "nonce",
        "height",
        "method",
        "args",
        "gas_low",
        "gas_high",
    ],
)
def test_delegate_tampering(tamper):
    o = options()
    d = es.build_escrow_authorization_delegate(o, token=TOKEN, amount=V1["amount"], msg=V1)
    if tamper == "two":
        d["actions"] *= 2
    elif tamper == "none":
        d["actions"] = []
    elif tamper in ("receiver", "sender"):
        d[tamper + "_id"] = "foreign.near"
    elif tamper in ("nonce", "height"):
        d["nonce" if tamper == "nonce" else "max_block_height"] = 8
    else:
        key, value = {
            "deposit0": ("deposit", 0),
            "deposit2": ("deposit", 2),
            "method": ("method_name", "ft_transfer"),
            "args": ("args", "{}"),
            "gas_low": ("gas", 37_999_999_999_999),
            "gas_high": ("gas", 100_000_000_000_001),
        }[tamper]
        d["actions"][0][key] = value
    with pytest.raises(ValueError):
        es.validate_escrow_authorization_delegate(d, o, token=TOKEN, amount=V1["amount"], msg=V1)


@pytest.mark.parametrize(
    "method,args,maximum",
    [
        ("release", {"payment_id": V1["payment_id"], "amount": None}, 30),
        ("release", {"payment_id": V1["payment_id"], "amount": "400000"}, 30),
        ("refund", {"payment_id": V1["payment_id"]}, 30),
        ("reclaim", {"payment_id": V1["payment_id"]}, 30),
        ("withdraw", {}, 50),
        ("withdraw_for", {"account": "worker.testnet"}, 50),
    ],
)
def test_lifecycle_wire(method, args, maximum):
    d = decode(es.sign_escrow_lifecycle(options(), method, args))
    assert d["method"] == method and d["args"] == args
    assert d["receiver"] == options().escrow_contract
    assert d["deposit"] == 0 and d["gas"] == maximum * 10**12
    assert d["sender"] == V1["payer"] and d["nonce"] == 7 and d["height"] == 100_000_000
    expected = es.sign_escrow_lifecycle(options(), method, args)
    helper = getattr(es, method)
    if method == "withdraw":
        assert helper(options()) == expected
    elif method == "withdraw_for":
        assert helper(options(), args["account"]) == expected
    elif method == "release":
        assert helper(options(), args["payment_id"], args["amount"]) == expected
    else:
        assert helper(options(), args["payment_id"]) == expected


@pytest.mark.parametrize("method", ["release", "refund", "reclaim", "withdraw", "withdraw_for"])
def test_lifecycle_gas_edges(method):
    args = (
        {"account": "worker.testnet"}
        if method == "withdraw_for"
        else {} if method == "withdraw" else {"payment_id": V1["payment_id"]}
    )
    minimum, maximum = (29, 50) if method.startswith("withdraw") else (3, 30)
    for gas in (minimum * 10**12, maximum * 10**12):
        assert decode(es.sign_escrow_lifecycle(options(gas=gas), method, args))["gas"] == gas
    for gas in (minimum * 10**12 - 1, maximum * 10**12 + 1):
        with pytest.raises(ValueError):
            es.sign_escrow_lifecycle(options(gas=gas), method, args)


@pytest.mark.parametrize(
    "method,args",
    [
        ("pause", {}),
        ("on_withdraw", {}),
        ("Release", {}),
        ("release ", {}),
        ("", {}),
        ("authorize_from_budget", {}),
        ("withdraw", {"token": TOKEN}),
        ("withdraw_for", {"account": "a..near"}),
        ("withdraw_for", {}),
        ("refund", {"payment_id": V1["payment_id"], "amount": "1"}),
        ("release", []),
        ("release", {"payment_id": V1["payment_id"], "amount": 0}),
        ("release", {"payment_id": V1["payment_id"], "amount": True}),
        ("release", {"payment_id": "1" * 31}),
        ("refund", {"payment_id": "1" * 33}),
        ("refund", {"payment_id": "0" * 44}),
        ("reclaim", {}),
    ],
)
def test_lifecycle_rejects_before_signing(method, args):
    class NoKey:
        @property
        def public_key(self):
            pytest.fail("key accessed")

    with pytest.raises(ValueError):
        es.sign_escrow_lifecycle(options(signer=NoKey()), method, args)


def test_short_payment_ids_are_valid():
    for payment_id in (
        "1" * 32,
        VECTORS["payment_id"]["vectores"][1]["payment_id"],
        "1y1ZbrieUbaioiUBHxX7EyZMNkCis2tLKCs5Xxezgy",
    ):
        assert decode(es.refund(options(), payment_id))["args"]["payment_id"] == payment_id


def test_local_signer_forms_and_secret_redaction():
    signer = options().signer
    secret = "ed25519:" + _b58encode(bytes(range(32)) + signer.public_key)
    assert authorize(options(signer=es.NearEd25519Signer(secret))) == authorize()
    assert secret not in repr(signer)
    for bad in (
        "ed25519:bad-secret!",
        "ed25519:" + _b58encode(bytes(64)),
        secret + "\n",
        secret[8:],
        bytes(31),
        bytes(64),
    ):
        with pytest.raises(ValueError) as raised:
            es.NearEd25519Signer(bad)
        assert str(bad) not in str(raised.value)
    assert signer.public_key == options().signer.public_key


@pytest.mark.parametrize(
    "public_key,signature",
    [(b"a", bytes(64)), (bytes(32), bytes(63)), (bytes(32), bytes(64)), (bytes(32), "a" * 64)],
)
def test_bad_signer_results_rejected(public_key, signature):
    class BadSigner:
        def sign(self, digest):
            return signature

    bad = BadSigner()
    bad.public_key = public_key
    with pytest.raises(ValueError):
        authorize(options(signer=bad))


def test_budget_boundary_is_fail_closed():
    assert "request" in inspect.signature(es.AuthorizeFromBudget.authorize_from_budget).parameters
    with pytest.raises(NotImplementedError, match="v2"):
        es.authorize_from_budget(options(signer=object()), {})


def test_public_api_names():
    assert len(es.__all__) == len(set(es.__all__))
    for name in es.__all__:
        assert hasattr(es, name)


@pytest.mark.parametrize("value", [None, {}, [], {"operator_id": "em"}])
def test_missing_msg_fields(value):
    with pytest.raises(ValueError):
        es.build_escrow_msg(value)


def test_invalid_public_key_never_signs():
    class NoSign:
        public_key = b"x"

        def sign(self, digest):
            pytest.fail("signer called with invalid public key")

    with pytest.raises(ValueError):
        authorize(options(signer=NoSign()))


def test_invalid_id_length_before_base58(monkeypatch):
    o = options()

    def no_decode(value):
        pytest.fail("decoded oversized payment id")

    monkeypatch.setattr(es, "_b58decode", no_decode)
    with pytest.raises(ValueError):
        es.refund(o, "1" * 10000)


def test_secret_shapes_before_decode(monkeypatch):
    signer = options().signer
    seed = bytes(range(32))
    for bad in ("ed25519:" + _b58encode(seed), seed + signer.public_key):
        with pytest.raises(ValueError):
            es.NearEd25519Signer(bad)

    def no_decode(value):
        pytest.fail("decoded oversized secret")

    monkeypatch.setattr(es, "_b58decode", no_decode)
    with pytest.raises(ValueError):
        es.NearEd25519Signer("ed25519:" + "1" * 10000)


@pytest.mark.parametrize("field,value", [("terms_hash", "a" * 62), ("salt", "a" * 30)])
def test_hex_even_wrong_lengths(field, value):
    with pytest.raises(ValueError):
        es.build_escrow_msg({**V1, field: value})


@pytest.mark.parametrize("network", ["near", "NEAR:MAINNET", "", None])
def test_lifecycle_invalid_network(network):
    with pytest.raises(ValueError):
        es.withdraw(options(network=network))


def test_facilitator_v2_envelopes():
    from uvd_x402_sdk.envelope_v2 import build_settle_request_v2, build_verify_request_v2

    resource = {
        "url": "https://seller.example/task",
        "description": "task",
        "mimeType": "application/json",
    }
    accepted = {
        "scheme": "escrow",
        "network": "near:testnet",
        "asset": TOKEN,
        "amount": V1["amount"],
        "payTo": options().escrow_contract,
        "maxTimeoutSeconds": 300,
        "extra": {
            "nearEscrow": {
                "contract": options().escrow_contract,
                "operatorId": V1["operator_id"],
                "msg": es.build_escrow_msg(V1),
                "paymentId": V1["payment_id"],
            }
        },
    }
    body = build_verify_request_v2(authorize()["payload"], resource, accepted)
    assert body["accepted"] == body["paymentPayload"]["accepted"] == accepted
    assert (
        decode({"payload": body["paymentPayload"]["payload"]})["args"]["msg"]
        == accepted["extra"]["nearEscrow"]["msg"]
    )
    lifecycle_payment = es.release(options(nonce=8), V1["payment_id"])
    lifecycle_accepted = {
        **accepted,
        "amount": "0",
        "extra": {
            "nearEscrow": {
                "contract": options().escrow_contract,
                "action": "release",
                "paymentId": V1["payment_id"],
            }
        },
    }
    body = build_settle_request_v2(lifecycle_payment["payload"], resource, lifecycle_accepted)
    assert body["accepted"]["amount"] == "0"
    assert body["paymentPayload"]["accepted"]["extra"]["nearEscrow"]["action"] == "release"
    decoded = decode({"payload": body["paymentPayload"]["payload"]})
    assert decoded["method"] == "release" and decoded["nonce"] == 8
    assert body["resource"] == resource


class NoKey:
    @property
    def public_key(self):
        pytest.fail("invalid intent accessed the signer public key")

    def sign(self, digest):
        pytest.fail("invalid intent called the signer")


def sign_operation(o, method):
    if method == "authorize":
        return authorize(o, token=es.NEAR_ESCROW_TOKENS[o.network])
    args = (
        {"account": "worker.testnet"}
        if method == "withdraw_for"
        else {} if method == "withdraw" else {"payment_id": V1["payment_id"]}
    )
    return es.sign_escrow_lifecycle(o, method, args)


@pytest.mark.parametrize(
    "method", ["authorize", "release", "refund", "reclaim", "withdraw", "withdraw_for"]
)
@pytest.mark.parametrize(
    "network,contract",
    [
        ("near:mainnet", "evil-sink.near"),
        ("near:mainnet", "arras-v1b-1101.testnet"),
        ("near:mainnet", "arras.near"),
        ("near:mainnet", "v1.arras.ultravioletadao.near"),
        ("near:mainnet", "V1.arras-escrow.near"),
        ("near:mainnet", " v1.arras-escrow.near"),
        ("near:mainnet", "v1.arras-escrow.near "),
        ("near:mainnet", "v1.arras-escrow.near\n"),
        ("near:mainnet", "v1.ａｒｒａｓ-escrow.near"),
        ("near:testnet", "v1.arras-escrow.near"),
        ("near:testnet", "evil-sink.testnet"),
        ("near:testnet", "arras.testnet"),
        ("near:testnet", "ARRAS-v1b-1101.testnet"),
        ("near:testnet", " arras-v1b-1101.testnet"),
        ("near:testnet", "arras-v1b-1101.testnet "),
        ("near:testnet", "arras-v1b-1101.testnet\n"),
        ("near:testnet", "ａｒｒａｓ-v1b-1101.testnet"),
        ("near:testnet", ""),
        ("near:testnet", None),
    ],
)
def test_unregistered_contract_before_key(network, contract, method):
    with pytest.raises(ValueError):
        sign_operation(options(network=network, escrow_contract=contract, signer=NoKey()), method)


@pytest.mark.parametrize(
    "method", ["authorize", "release", "refund", "reclaim", "withdraw", "withdraw_for"]
)
def test_registered_mainnet_contract_wire(method):
    o = options(network="near:mainnet", escrow_contract="v1.arras-escrow.near")
    payment = sign_operation(o, method)
    assert payment["network"] == "near:mainnet"
    d = decode(payment)
    if method == "authorize":
        assert d["receiver"] == VECTORS["payment_id"]["vectores"][2]["token"]
        assert d["method"] == "ft_transfer_call" and d["deposit"] == 1
        assert d["args"]["receiver_id"] == "v1.arras-escrow.near"
    else:
        assert d["receiver"] == "v1.arras-escrow.near"
        assert d["method"] == method and d["deposit"] == 0
    assert d["public_key"] == o.signer.public_key


def test_registries_are_immutable():
    assert isinstance(es.NEAR_ESCROW_CONTRACTS, MappingProxyType)
    assert dict(es.NEAR_ESCROW_CONTRACTS) == {
        "near:testnet": ("arras-v1b-1101.testnet",),
        "near:mainnet": ("v1.arras-escrow.near",),
    }
    assert isinstance(es.NEAR_ESCROW_TOKENS, MappingProxyType)
    assert dict(es.NEAR_ESCROW_TOKENS) == {
        "near:testnet": TOKEN,
        "near:mainnet": VECTORS["payment_id"]["vectores"][2]["token"],
    }
    with pytest.raises(TypeError):
        es.NEAR_ESCROW_CONTRACTS["near:mainnet"] = ("evil-sink.near",)
    with pytest.raises(TypeError):
        es.NEAR_ESCROW_CONTRACTS["near:testnet"][0] = "evil-sink.testnet"
    with pytest.raises(TypeError):
        es.NEAR_ESCROW_CONTRACTS["near:mainnet"][0] = "evil-sink.near"
    with pytest.raises(TypeError):
        es.NEAR_ESCROW_TOKENS["near:testnet"] = "evil-token.testnet"


@pytest.mark.parametrize(
    "method", ["authorize", "release", "refund", "reclaim", "withdraw", "withdraw_for"]
)
@pytest.mark.parametrize("as_string", [False, True])
def test_nonce_ceiling_before_key(method, as_string):
    height = 100
    ceiling = height * 1_000_000
    encode = str if as_string else int
    d = decode(
        sign_operation(options(nonce=encode(ceiling - 1), max_block_height=encode(height)), method)
    )
    assert d["nonce"] == ceiling - 1 and d["height"] == height
    for nonce in (ceiling, ceiling + 1, 2**64 - 1):
        with pytest.raises(ValueError, match="nonce"):
            sign_operation(
                options(nonce=encode(nonce), max_block_height=encode(height), signer=NoKey()),
                method,
            )
    # The product must not wrap/truncate to u64 or pass through floating point.
    d = decode(sign_operation(options(nonce=2**64 - 1, max_block_height=2**64 - 1), method))
    assert d["nonce"] == d["height"] == 2**64 - 1


def test_integer_inputs_are_decimal_strings_in_signed_bytes():
    terms = {**V1, "release_deadline": 1793577600000000000, "reclaim_after": 1793664000000000000}
    d = decode(authorize(amount=1000000, msg=terms))
    assert d["args"]["amount"] == "1000000"
    msg = json.loads(d["args"]["msg"])
    assert msg["release_deadline"] == "1793577600000000000"
    assert msg["reclaim_after"] == "1793664000000000000"
    assert d["args"]["msg"] == VECTORS["msg_canonico_v1"]
    assert decode(es.release(options(), V1["payment_id"], 400000))["args"]["amount"] == "400000"


def test_missing_crypto_extra_names_near(monkeypatch):
    original = builtins.__import__

    def without_crypto(name, *args, **kwargs):
        if name.startswith("cryptography"):
            raise ImportError("cryptography unavailable")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", without_crypto)
    with pytest.raises(ImportError, match=r"uvd-x402-sdk\[near\]") as exc:
        es.NearEd25519Signer(bytes(range(32)))
    assert "solana" not in str(exc.value)


def test_withdraw_for_can_name_the_caller():
    o = options()
    assert decode(es.withdraw_for(o, o.sender_id))["args"] == {"account": o.sender_id}


def test_guide_uses_the_capturer_key_for_release_and_refund():
    guide = (Path(__file__).resolve().parents[1] / "docs/near-escrow.md").read_text(
        encoding="utf-8"
    )
    blocks = [part.split("```", 1)[0] for part in guide.split("```python\n")[1:]]
    context = dict(
        network="near:testnet",
        secret_key=bytes(range(32)),
        payer=V1["payer"],
        contract="arras-v1b-1101.testnet",
        next_nonce=7,
        expiry_height=100_000_000,
        token=TOKEN,
        amount=1000000,
        terms=V1,
        resource={
            "url": "https://seller.example/task",
            "description": "task",
            "mimeType": "application/json",
        },
        capturer="capturer.testnet",
        capturer_secret_key=bytes(range(1, 33)),
        capturer_next_nonce=11,
        capturer_refund_nonce=12,
        capturer_expiry_height=100_000_001,
        seed_from_your_key_store=bytes(range(32)),
        server_challenge="EM payout proof: test",
        server_nonce_32_bytes=bytes(range(32)),
        next_access_key_nonce=7,
        expiry_block_height=100_000_000,
        receipt_logs=[],
    )
    for block in blocks:
        exec(compile(block, "docs/near-escrow.md", "exec"), context)
    for name, method, nonce in (("release_body", "release", 11), ("refund_body", "refund", 12)):
        body = context[name]
        d = decode({"payload": body["paymentPayload"]["payload"]})
        assert d["sender"] == "capturer.testnet"
        assert d["public_key"] == es.NearEd25519Signer(bytes(range(1, 33))).public_key
        assert d["method"] == method and d["nonce"] == nonce
        assert d["height"] == 100_000_001 and d["deposit"] == 0
        assert body["accepted"]["amount"] == "0"
        assert body["accepted"]["extra"]["nearEscrow"]["action"] == method
