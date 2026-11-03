"""NEAR golden-flow vectors, independent signatures and fail-before-key boundaries."""

import base64
import copy
import hashlib
import json
import struct
from dataclasses import replace
from pathlib import Path

import pytest
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from tests.test_near_escrow import decode
from uvd_x402_sdk import near_events as events
from uvd_x402_sdk import near_exact as exact
from uvd_x402_sdk import near_nep413 as nep
from uvd_x402_sdk import near_storage as storage
from uvd_x402_sdk.near_escrow import (
    NEAR_ESCROW_CONTRACTS,
    NEAR_ESCROW_TOKENS,
    NearEd25519Signer,
)

FIXTURE = Path(__file__).parent / "fixtures/near-kk.json"
V = json.loads(FIXTURE.read_text(encoding="utf-8"))
NONCE = bytes(range(32))


class NoKey:
    @property
    def public_key(self):
        pytest.fail("invalid request reached the key")

    def sign(self, digest):
        pytest.fail("invalid request reached signing")


def nep_args(**changes):
    args = dict(
        account_id="payer-demo.testnet",
        message="hi",
        nonce=NONCE,
        recipient="myapp.com",
        callback_url="myapp.com/callback",
    )
    args.update(changes)
    return args


def exact_options(**changes):
    return replace(
        exact.NearExactDelegateOptions(
            network="near:testnet",
            signer=NearEd25519Signer(NONCE),
            sender_id="payer-demo.testnet",
            nonce=7,
            max_block_height=100_000_000,
        ),
        **changes,
    )


def pay(o=None, **changes):
    args = dict(
        token=NEAR_ESCROW_TOKENS["near:testnet"], pay_to="worker-demo.testnet", amount="1000000"
    )
    args.update(changes)
    return exact.sign_exact_near_delegate(exact_options() if o is None else o, **args)


@pytest.mark.parametrize("v", V["nep413"], ids=lambda v: v["id"])
def test_nep413_official_example_and_unicode_independent_verifier(v):
    args = nep_args(message=v["message"], recipient=v["recipient"], callback_url=v["callbackUrl"])
    output = nep.sign_nep413(NearEd25519Signer(NONCE), **args)
    args.pop("account_id")
    preimage = nep.nep413_preimage(**args)
    assert preimage.hex() == v["preimageHex"]

    # Independent Borsh encoder (not the production serializer).
    def text(value):
        raw = value.encode("utf-8")
        return struct.pack("<I", len(raw)) + raw

    expected = (
        struct.pack("<I", 2**31 + 413)
        + text(v["message"])
        + NONCE
        + text(v["recipient"])
        + (b"\0" if v["callbackUrl"] is None else b"\1" + text(v["callbackUrl"]))
    )
    assert preimage == expected
    digest = hashlib.sha256(expected).digest()
    assert digest.hex() == v["digestHex"]
    assert output == dict(
        accountId="payer-demo.testnet",
        publicKey="ed25519:FAe4sisG95oZ42w7buUn5qEE4TAnfTTFPiguZUHmhiF",
        signature=v["signatureBase64"],
    )
    public = NearEd25519Signer(NONCE).public_key
    Ed25519PublicKey.from_public_bytes(public).verify(base64.b64decode(output["signature"]), digest)
    with pytest.raises(InvalidSignature):
        Ed25519PublicKey.from_public_bytes(public).verify(
            base64.b64decode(output["signature"]), hashlib.sha256(expected + b"x").digest()
        )


@pytest.mark.parametrize(
    "field,bad",
    [
        ("account_id", None),
        ("account_id", "A.near"),
        ("account_id", "aa.near\n"),
        ("account_id", "ñ.near"),
        ("message", None),
        ("message", 1),
        ("message", "\ud800"),
        ("recipient", None),
        ("recipient", ""),
        ("recipient", " myapp.com"),
        ("recipient", "myapp.com\n"),
        ("recipient", "intents.near"),
        ("recipient", "INTENTS.NEAR"),
        ("recipient", "Intents.Near."),
        ("recipient", "\udfff"),
        ("nonce", None),
        ("nonce", bytes(31)),
        ("nonce", bytes(33)),
        ("nonce", bytearray(32)),
        ("callback_url", False),
        ("callback_url", "\ud800"),
    ],
)
def test_nep413_refuses_before_key(field, bad):
    with pytest.raises(ValueError):
        nep.sign_nep413(NoKey(), **nep_args(**{field: bad}))


class BrokenSigner:
    def __init__(self, public, signature):
        self.public_key = public
        self.signature = signature

    def sign(self, digest):
        return self.signature


@pytest.mark.parametrize(
    "public,signature",
    [
        (None, bytes(64)),
        (bytes(31), bytes(64)),
        (bytes(33), bytes(64)),
        (bytes(32), None),
        (bytes(32), bytes(63)),
        (bytes(32), bytes(65)),
        (NearEd25519Signer(NONCE).public_key, bytes(64)),
    ],
)
def test_nep413_checks_signer_output(public, signature):
    with pytest.raises(ValueError):
        nep.sign_nep413(BrokenSigner(public, signature), **nep_args())


@pytest.mark.parametrize("v", V["exact"])
def test_exact_vectors_independent_delegate_decoder(v):
    result = exact.sign_exact_near_delegate(
        exact_options(network=v["network"], sender_id=v["senderId"]),
        token=v["token"],
        pay_to=v["payTo"],
        amount=v["amount"],
    )
    assert result == dict(
        x402Version=2,
        scheme="exact",
        network=v["network"],
        payload=dict(signedDelegateAction=v["signedDelegateAction"]),
    )
    decoded = decode(result)  # Independently parses every byte and verifies Ed25519.
    assert decoded["sender"] == v["senderId"]
    assert decoded["receiver"] == v["token"]
    assert decoded["method"] == "ft_transfer"
    assert decoded["args"] == dict(receiver_id=v["payTo"], amount=v["amount"])
    assert (decoded["gas"], decoded["deposit"], decoded["nonce"], decoded["height"]) == (
        30_000_000_000_000,
        1,
        7,
        100_000_000,
    )


@pytest.mark.parametrize(
    "field,bad",
    [
        ("network", "near"),
        ("network", None),
        ("network", "near:testnet\n"),
        ("sender_id", "a"),
        ("sender_id", "A.near"),
        ("sender_id", "aa.near\n"),
        ("sender_id", "a" * 65),
        ("nonce", 0),
        ("nonce", -1),
        ("nonce", True),
        ("nonce", 1.5),
        ("nonce", 2**64),
        ("nonce", "07"),
        ("nonce", "7\n"),
        ("nonce", "７"),
        ("nonce", "0x7"),
        ("nonce", "0X7"),
        ("nonce", " 7"),
        ("nonce", "+7"),
        ("nonce", 100_000_000_000_000),
        ("nonce", 100_000_000_000_001),
        ("max_block_height", 0),
        ("max_block_height", -1),
        ("max_block_height", 2**64),
        ("gas", 0),
        ("gas", -1),
        ("gas", 30_000_000_000_001),
        ("gas", True),
    ],
)
def test_exact_options_refused_before_key(field, bad):
    with pytest.raises(ValueError):
        pay(exact_options(signer=NoKey(), **{field: bad}))


@pytest.mark.parametrize(
    "field,bad",
    [
        ("token", "wrong.near"),
        ("token", NEAR_ESCROW_TOKENS["near:mainnet"]),
        ("token", None),
        ("pay_to", "intents.near"),
        ("pay_to", "INTENTS.NEAR"),
        ("pay_to", "intents.near."),
        ("pay_to", "worker.near\n"),
        ("pay_to", "ñ.near"),
        ("pay_to", None),
        ("amount", 0),
        ("amount", -1),
        ("amount", True),
        ("amount", 1.1),
        ("amount", 2**128),
        ("amount", "01"),
        ("amount", "1\n"),
        ("amount", "0x1"),
        ("amount", "0X1"),
        ("amount", " 1"),
        ("amount", "１"),
    ],
)
def test_exact_payment_refused_before_key(field, bad):
    with pytest.raises(ValueError):
        pay(exact_options(signer=NoKey()), **{field: bad})


def test_exact_integer_string_boundaries():
    for gas in (1, 30_000_000_000_000):
        decoded = decode(
            pay(
                exact_options(gas=gas, nonce="99999999999999", max_block_height="100000000"),
                amount=2**128 - 1,
                pay_to="11",
            )
        )
        assert decoded["gas"] == gas
        assert decoded["nonce"] == 99_999_999_999_999
        assert decoded["args"]["amount"] == str(2**128 - 1)
    assert pay(amount=1) == pay(amount="1")
    assert (
        decode(pay(exact_options(max_block_height=2**64 - 1), pay_to="a" * 64))["height"]
        == 2**64 - 1
    )


@pytest.mark.parametrize(
    "public,signature",
    [(None, bytes(64)), (bytes(32), bytes(63)), (NearEd25519Signer(NONCE).public_key, bytes(64))],
)
def test_exact_checks_signer_output(public, signature):
    with pytest.raises(ValueError):
        pay(exact_options(signer=BrokenSigner(public, signature)))


def test_storage_payer_paid_normal_transactions():
    for network, contracts in NEAR_ESCROW_CONTRACTS.items():
        for contract in contracts:
            tx = storage.build_arras_storage_deposit(
                network=network,
                payer_id="parent.near",
                escrow_contract=contract,
                account_id="agent.near",
            )
            assert tx == dict(
                signerId="parent.near",
                receiverId=contract,
                actions=[
                    dict(
                        type="FunctionCall",
                        params=dict(
                            methodName="storage_deposit",
                            args=dict(account_id="agent.near", registration_only=False),
                            gas="30000000000000",
                            deposit="50000000000000000000000",
                        ),
                    )
                ],
            )
        tx = storage.build_usdc_storage_deposit(network=network, payer_id="parent.near")
        assert tx == dict(
            signerId="parent.near",
            receiverId=NEAR_ESCROW_TOKENS[network],
            actions=[
                dict(
                    type="FunctionCall",
                    params=dict(
                        methodName="storage_deposit",
                        args=dict(account_id="parent.near", registration_only=True),
                        gas="30000000000000",
                        deposit="1250000000000000000000",
                    ),
                )
            ],
        )
        for amount in (1, 2**128 - 1):
            tx = storage.build_usdc_storage_deposit(
                network=network, payer_id="11", deposit=amount, account_id="a" * 64
            )
            assert tx["actions"][0]["params"]["deposit"] == str(amount)
            assert tx["actions"][0]["params"]["args"]["account_id"] == "a" * 64
    assert (
        storage.build_arras_storage_deposit(
            network="near:testnet", payer_id="aa.testnet", escrow_contract="arras-v1b-1101.testnet"
        )["actions"][0]["params"]["args"]["account_id"]
        == "aa.testnet"
    )


@pytest.mark.parametrize(
    "network,contract",
    [
        ("near:testnet", "evil-sink.testnet"),
        ("near:mainnet", "evil-sink.near"),
        ("near:mainnet", "arras-v1b-1101.testnet"),
        ("near:testnet", "v1.arras-escrow.near"),
        ("near:testnet", "intents.near"),
        ("near:testnet", "ARRAS-V1B-1101.testnet"),
        ("near:testnet", "arras-v1b-1101.testnet\n"),
        ("near:testnet", None),
        ("near", "arras-v1b-1101.testnet"),
        (None, "arras-v1b-1101.testnet"),
    ],
)
def test_arras_storage_registry_before_construction(monkeypatch, network, contract):
    monkeypatch.setattr(
        storage, "_storage", lambda *args: pytest.fail("constructed unregistered call")
    )
    with pytest.raises(ValueError):
        storage.build_arras_storage_deposit(
            network=network, payer_id="aa.testnet", escrow_contract=contract
        )


@pytest.mark.parametrize(
    "builder", [storage.build_arras_storage_deposit, storage.build_usdc_storage_deposit]
)
@pytest.mark.parametrize(
    "field,bad",
    [
        ("network", "near"),
        ("network", "NEAR:TESTNET"),
        ("payer_id", None),
        ("payer_id", "AA.near"),
        ("payer_id", "aa.near\n"),
        ("account_id", ""),
        ("account_id", "ñ.near"),
        ("account_id", "aa..near"),
        ("deposit", 0),
        ("deposit", -1),
        ("deposit", True),
        ("deposit", 1.5),
        ("deposit", 2**128),
        ("deposit", "01"),
        ("deposit", "0x1"),
        ("deposit", "0X1"),
        ("deposit", "1\n"),
        ("deposit", "１"),
        ("deposit", None),
    ],
)
def test_storage_invalid_arguments(builder, field, bad):
    args = dict(network="near:testnet", payer_id="payer.testnet")
    if builder is storage.build_arras_storage_deposit:
        args["escrow_contract"] = "arras-v1b-1101.testnet"
    args[field] = bad
    with pytest.raises(ValueError):
        builder(**args)


def event_log(value):
    return "EVENT_JSON:" + json.dumps(value)


def test_events_all_rust_variants_batch_order_extra_fields_and_large_integers():
    expected = [json.loads(log[11:]) for log in V["arrasEvents"]]
    assert events.parse_arras_events(iter(["foreign", None, *V["arrasEvents"]])) == expected
    assert len(expected) == 11
    v = copy.deepcopy(expected[1])
    v["data"] *= 2
    v["data"][0]["amount"] = str(2**128 - 1)
    v["data"][0]["extension"] = "future"
    assert events.parse_arras_events([event_log(v)]) == [v]
    v["data"] = []
    assert events.parse_arras_events([event_log(v)]) == [v]


@pytest.mark.parametrize("logs", [None, 1, "EVENT_JSON:{}", b"EVENT_JSON:{}", {}, []])
def test_events_invalid_container(logs):
    assert events.parse_arras_events(logs) == []


@pytest.mark.parametrize(
    "field,bad",
    [
        ("standard", "other"),
        ("standard", "Arras"),
        ("standard", "arras "),
        ("standard", None),
        ("version", "2.0.0"),
        ("version", "1.0.0\n"),
        ("event", "unknown"),
        ("event", "Released"),
        ("event", "released\n"),
        ("event", {}),
        ("data", {}),
        ("data", None),
        ("data", [1]),
    ],
)
def test_events_bad_envelope_does_not_hide_next_log(field, bad):
    value = json.loads(V["arrasEvents"][1][11:])
    value[field] = bad
    assert events.parse_arras_events([event_log(value), V["arrasEvents"][1]]) == [
        json.loads(V["arrasEvents"][1][11:])
    ]


@pytest.mark.parametrize("log", V["arrasEvents"])
def test_events_every_required_field_is_checked(log):
    value = json.loads(log[11:])
    for field in list(value["data"][0]):
        # Optional operator fields can be absent, but never incorrectly typed.
        mutant = copy.deepcopy(value)
        mutant["data"][0][field] = None
        assert events.parse_arras_events([event_log(mutant)]) == []
        if value["event"].startswith("operator_") and field != "operator_id":
            mutant = copy.deepcopy(value)
            del mutant["data"][0][field]
            assert events.parse_arras_events([event_log(mutant)]) == [mutant]
        else:
            mutant = copy.deepcopy(value)
            del mutant["data"][0][field]
            assert events.parse_arras_events([event_log(mutant)]) == []


@pytest.mark.parametrize(
    "amount", [0, True, None, "-1", "01", "1\n", " 1", "0x1", "0X1", "１", str(2**128)]
)
def test_events_u128_canonical_and_bounded(amount):
    value = json.loads(V["arrasEvents"][1][11:])
    value["data"][0]["amount"] = amount
    assert events.parse_arras_events([event_log(value)]) == []


@pytest.mark.parametrize("fee", [True, -1, 65536, "1300", 1.5])
def test_events_u16(fee):
    value = json.loads(V["arrasEvents"][0][11:])
    value["data"][0]["fee_bps"] = fee
    assert events.parse_arras_events([event_log(value)]) == []


def test_events_other_types_limits_and_atomic_batches():
    for index, field, bad in [
        (9, "at", str(2**64)),
        (9, "paused", 1),
        (4, "ok", "false"),
        (6, "capturers", "capturer.near"),
        (6, "capturers", [1]),
    ]:
        value = json.loads(V["arrasEvents"][index][11:])
        value["data"][0][field] = bad
        assert events.parse_arras_events([event_log(value)]) == []
    good = V["arrasEvents"][1]
    v = json.loads(good[11:])
    v["data"].append(dict(payment_id="id", amount="1"))
    assert events.parse_arras_events([event_log(v)]) == []
    invalid = [
        "EVENT_JSON:{",
        "EVENT_JSON:null",
        "EVENT_JSON:[]",
        " EVENT_JSON:{}",
        "event_json:{}",
        "EVENT_JSON:" + "[" * 65 + "]" * 65,
        "EVENT_JSON:" + " " * (512 * 1024 + 1),
    ]
    assert events.parse_arras_events([*invalid, good]) == [json.loads(good[11:])]


def test_public_exports():
    import uvd_x402_sdk as sdk

    for module, names in [
        (nep, ["Nep413SignedMessage", "NEP413_PREFIX", "nep413_preimage", "sign_nep413"]),
        (exact, ["NearExactDelegateOptions", "sign_exact_near_delegate"]),
        (
            storage,
            [
                "NearStorageTransaction",
                "NEAR_ARRAS_STORAGE_DEPOSIT",
                "NEAR_USDC_STORAGE_DEPOSIT",
                "build_arras_storage_deposit",
                "build_usdc_storage_deposit",
            ],
        ),
        (
            events,
            [name for name in vars(events) if name.startswith("Arras")] + ["parse_arras_events"],
        ),
    ]:
        for name in names:
            assert name in sdk.__all__
            assert getattr(sdk, name) is getattr(module, name)


def test_events_json_limits_apply_to_valid_envelopes_and_extra_data():
    valid = json.loads(V["arrasEvents"][1][11:])
    large = copy.deepcopy(valid)
    large["data"][0]["extension"] = "x" * (512 * 1024)
    deep = copy.deepcopy(valid)
    nested = "x"
    for _ in range(65):
        nested = [nested]
    deep["data"][0]["extension"] = nested
    assert events.parse_arras_events([event_log(large), event_log(deep), event_log(valid)]) == [
        valid
    ]


def test_fixture_is_frozen():
    assert hashlib.sha256(FIXTURE.read_bytes()).hexdigest() == FIXTURE_SHA256


FIXTURE_SHA256 = "763537d4ed2026e286bc4876efcbfbd141385a2cd7968c60653ed13efcc14bca"


@pytest.mark.parametrize(
    "builder", [storage.build_arras_storage_deposit, storage.build_usdc_storage_deposit]
)
@pytest.mark.parametrize("payer", [None, "A.near", "aa.near\n", "ñ.near"])
def test_storage_valid_beneficiary_does_not_mask_invalid_payer(builder, payer):
    args = dict(network="near:testnet", payer_id=payer, account_id="beneficiary.testnet")
    if builder is storage.build_arras_storage_deposit:
        args["escrow_contract"] = "arras-v1b-1101.testnet"
    with pytest.raises(ValueError):
        builder(**args)


def test_events_prefix_is_exact_even_with_valid_json_tail():
    good = V["arrasEvents"][1]
    assert (
        events.parse_arras_events(
            [good.replace("EVENT_JSON:", "event_json:"), "garbage___:" + good[11:]]
        )
        == []
    )
