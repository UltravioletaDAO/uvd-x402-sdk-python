"""Arras v2 (arras 9f8332f, contracts/escrow-v2): operator tickets T-1/T-2, the v2
authorize msg, the v2 registry, and every v1 byte exactly as before.

The ticket key of the vectors is arras' published test seed (01 x 32); the other
keys are generated here. No network, no credentials.
"""

import builtins
import hashlib
import json
import struct
import time
from types import MappingProxyType

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from tests.test_near_escrow import TOKEN, V1, VECTORS, decode, options
from uvd_x402_sdk import near_escrow as es
from uvd_x402_sdk import near_storage as storage
from uvd_x402_sdk.solana_signing import _b58decode, _b58encode

V2 = VECTORS["v2"]
TICKETS = V2["operator_ticket"]
KEY = TICKETS["llave_de_prueba"]
SEED = bytes.fromhex(KEY["semilla_hex"])
T1, T2 = TICKETS["vectores"]
# The vectors' contract name: not a deployment, so tickets accept any account.
CONTRACT = T1["contract"]
EXPIRY = int(T1["expiry"])
# The vectors' expiry (2026-11-02) is in the past: every signature here says when "now" is.
BEFORE = EXPIRY - 1
V2_MAINNET = es.NEAR_ESCROW_V2_CONTRACTS["near:mainnet"][0]
V2_TESTNET_REHEARSAL = "v2.arras-v2e-1107.testnet"  # takes a test token: not registered
V2_ID = VECTORS["payment_id"]["vectores"][1]["payment_id"]
LIFECYCLE = [
    ("release", {"payment_id": V1["payment_id"]}),
    ("release", {"payment_id": V1["payment_id"], "amount": "400000"}),
    ("refund", {"payment_id": V1["payment_id"]}),
    ("reclaim", {"payment_id": V1["payment_id"]}),
    ("withdraw", {}),
    ("withdraw_for", {"account": "worker.testnet"}),
]


class NoKey:
    @property
    def public_key(self):
        pytest.fail("invalid input accessed the signer public key")

    def sign(self, message):
        pytest.fail("invalid input called the signer")


class ExternalSigner:
    """An injected signer (an HSM, a KMS proxy): a key generated for this test."""

    def __init__(self):
        self._key = Ed25519PrivateKey.generate()
        self.messages = []

    @property
    def public_key(self):
        return self._key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)

    def sign(self, message):
        self.messages.append(message)
        return self._key.sign(message)


def borsh_string(text):
    raw = text.encode("utf-8")
    return struct.pack("<I", len(raw)) + raw


def t1_ticket():
    return {"expiry": T1["expiry"], "sig": T1["sig"]}


def verify(**changes):
    values = dict(
        public_key=KEY["ticket_pubkey"],
        contract=CONTRACT,
        subject=T1["subject"],
        ticket=t1_ticket(),
        now_ns=EXPIRY - 1,
    )
    values.update(changes)
    return es.verify_operator_ticket(values.pop("public_key"), **values)


def sign(subject=None, expiry=EXPIRY, contract=CONTRACT, signer=None, now_ns=BEFORE):
    return es.sign_operator_ticket(
        es.NearEd25519Signer(SEED) if signer is None else signer,
        contract=contract,
        subject=T1["subject"] if subject is None else subject,
        expiry=expiry,
        now_ns=now_ns,
    )


def v2_options(network="near:mainnet", **changes):
    return options(
        network=network, escrow_contract=es.NEAR_ESCROW_V2_CONTRACTS[network][0], **changes
    )


def v2_terms(**changes):
    terms = {**V1, "dispute_deadline": V1["release_deadline"], "operator_ticket": t1_ticket()}
    terms.update(changes)
    return terms


# --- the ticket: T-1 / T-2 and the contract's own byte-by-byte test ---------------------------


def test_vector_key_is_the_published_test_key():
    signer = es.NearEd25519Signer(SEED)
    assert SEED == bytes([1]) * 32
    assert signer.public_key.hex() == KEY["pubkey_hex"]
    assert KEY["ticket_pubkey"] == "ed25519:" + _b58encode(signer.public_key)
    # T-1 is the ticket of V-1's payment: the id the payer's msg produces.
    assert T1["subject"] == {"payment": {"payment_id": es.compute_payment_id(V1)}}


@pytest.mark.parametrize("vector", TICKETS["vectores"], ids=lambda v: v["id"])
def test_ticket_vectors(vector):
    message = es.operator_ticket_message(vector["contract"], vector["subject"], vector["expiry"])
    assert message.hex() == vector["mensaje_hex"]
    ticket = sign(vector["subject"], vector["expiry"], vector["contract"])
    assert ticket == {"expiry": vector["expiry"], "sig": vector["sig"]}
    # Independent of the SDK: the vector's signature over the vector's bytes.
    Ed25519PublicKey.from_public_bytes(bytes.fromhex(KEY["pubkey_hex"])).verify(
        _b58decode(vector["sig"]), bytes.fromhex(vector["mensaje_hex"])
    )
    for public_key in (bytes.fromhex(KEY["pubkey_hex"]), KEY["ticket_pubkey"]):
        assert es.verify_operator_ticket(
            public_key,
            contract=vector["contract"],
            subject=vector["subject"],
            ticket=ticket,
            now_ns=int(vector["expiry"]) - 1,
        )


def test_ticket_expiry_as_int_signs_the_same_bytes():
    assert sign(expiry=EXPIRY) == sign(expiry=str(EXPIRY)) == t1_ticket()


def test_sign_refuses_a_ticket_already_expired():
    # E54 at now >= expiry: expiry == now is refused, now + 1 signs.
    with pytest.raises(ValueError, match="E54"):
        sign(now_ns=EXPIRY)
    with pytest.raises(ValueError, match="E54"):
        sign(signer=NoKey(), now_ns=EXPIRY + 1)
    assert sign(now_ns=EXPIRY - 1) == t1_ticket()
    # Without now_ns the wall clock decides: the vectors' expiry is past, and an expiry
    # written in seconds instead of nanoseconds is always in the past.
    with pytest.raises(ValueError, match="E54"):
        sign(signer=NoKey(), now_ns=None)
    with pytest.raises(ValueError, match="E54"):
        sign(signer=NoKey(), expiry=1_794_432_664, now_ns=None)
    soon = time.time_ns() + 60 * 10**9
    assert sign(expiry=soon, now_ns=None)["expiry"] == str(soon)
    for bad in (True, -1, 2**64, "01", 1.0):
        with pytest.raises(ValueError):
            sign(signer=NoKey(), now_ns=bad)


def test_ticket_message_byte_by_byte():
    """ticket.rs `mensaje_byte_a_byte`, built here without the SDK's Borsh writer."""
    head = borsh_string("arras:v2:operator_ticket") + borsh_string(CONTRACT)
    tail = struct.pack("<Q", EXPIRY)
    payment = head + bytes([0]) + borsh_string(V1["payment_id"]) + tail
    budget = (
        head
        + bytes([1])
        + borsh_string("em")
        + borsh_string("payer-demo.testnet")
        + borsh_string("agent-demo.testnet")
        + tail
    )
    assert es.operator_ticket_message(CONTRACT, T1["subject"], EXPIRY) == payment
    assert es.operator_ticket_message(CONTRACT, T2["subject"], EXPIRY) == budget
    assert es.OPERATOR_TICKET_DOMAIN == "arras:v2:operator_ticket"
    # Field order of the budget subject: operator_id, owner, agent (ticket.rs:34-38).
    swapped = {
        "budget": {
            **T2["subject"]["budget"],
            "owner": "agent-demo.testnet",
            "agent": "payer-demo.testnet",
        }
    }
    assert es.operator_ticket_message(CONTRACT, swapped, EXPIRY) != budget


def test_ticket_expiry_edges_are_u64():
    for expiry in (1, 2**64 - 1, str(2**64 - 1)):
        ticket = sign(expiry=expiry, now_ns=0)
        assert ticket["expiry"] == str(expiry)
        assert es.operator_ticket_message(CONTRACT, T1["subject"], expiry)[-8:] == struct.pack(
            "<Q", int(expiry)
        )


# --- what does NOT verify (ticket.rs `verifica_y_rechaza_cualquier_cambio`, `firma_mal_formada`)


def test_verify_accepts_only_before_expiry():
    assert verify()
    assert verify(now_ns=0)
    # E54: valid while now < expiry (authorize.rs:276).
    assert not verify(now_ns=EXPIRY)
    assert not verify(now_ns=EXPIRY + 1)
    assert not verify(now_ns=2**64 - 1)
    assert not verify(now_ns=str(EXPIRY))


def test_verify_rejects_another_contract_subject_expiry_or_key():
    assert not verify(contract="v2b.arras-dev.testnet")
    assert not verify(contract=V2_TESTNET_REHEARSAL)
    assert not verify(contract=V2_MAINNET)
    assert not verify(subject={"payment": {"payment_id": V2_ID}})
    assert not verify(subject=T2["subject"])
    assert not verify(
        subject={"budget": {**T2["subject"]["budget"], "agent": "agent-otro.testnet"}}
    )
    assert not verify(ticket={"expiry": str(EXPIRY + 1), "sig": T1["sig"]})
    assert not verify(ticket={"expiry": str(EXPIRY - 1), "sig": T1["sig"]}, now_ns=0)
    assert not verify(ticket={"expiry": T2["expiry"], "sig": T2["sig"]})
    other = bytearray.fromhex(KEY["pubkey_hex"])
    other[0] ^= 1
    assert not verify(public_key=bytes(other))
    assert not verify(public_key=ExternalSigner().public_key)
    # And the budget vector verifies only as itself.
    assert verify(subject=T2["subject"], ticket={"expiry": T2["expiry"], "sig": T2["sig"]})


SIG = T1["sig"]
RAW = _b58decode(SIG)
MALFORMED_SIGS = [
    "",
    "0",
    SIG + "1",
    "1" + SIG,
    "l" + SIG[1:],
    "1" * 89,
    _b58encode(bytes([7]) * 63),
    _b58encode(bytes([7]) * 65),
    _b58encode(bytes(64))[1:],
    SIG + " ",
    " " + SIG,
    SIG + "\n",
    None,
    1,
    RAW,
]
# Well formed (canonical base58 of 64 bytes) but of something else.
OTHER_SIGS = [SIG[:-1], _b58encode(bytes([0]) + RAW[1:]), _b58encode(bytes([7]) * 64)]


def test_signature_shape_matches_the_contract_decoder():
    assert len(SIG) <= 88
    assert es._ticket_signature(SIG) == RAW
    # Leading zero bytes are leading '1's: shorter, still canonical (ticket.rs:228-235).
    with_zero = _b58encode(bytes([0]) + RAW[1:])
    assert with_zero.startswith("1") and len(with_zero) < len(SIG)
    assert es._ticket_signature(with_zero) == bytes([0]) + RAW[1:]
    for sig in OTHER_SIGS:
        assert len(es._ticket_signature(sig)) == 64


@pytest.mark.parametrize("sig", MALFORMED_SIGS + OTHER_SIGS, ids=repr)
def test_verify_rejects_signatures(sig):
    assert not verify(ticket={"expiry": T1["expiry"], "sig": sig})


@pytest.mark.parametrize(
    "ticket",
    [
        None,
        [],
        "x",
        {},
        {"expiry": T1["expiry"]},
        {"sig": SIG},
        {**t1_ticket(), "x": 1},
        {"expiry": "0" + T1["expiry"], "sig": SIG},
        {"expiry": " " + T1["expiry"], "sig": SIG},
        {"expiry": "+" + T1["expiry"], "sig": SIG},
        {"expiry": True, "sig": SIG},
        {"expiry": -1, "sig": SIG},
        {"expiry": 2**64, "sig": SIG},
        {"expiry": float(EXPIRY), "sig": SIG},
        # A JSON number, as json.loads reads one: the contract answers E03 (msg.rs:324-327).
        {"expiry": EXPIRY, "sig": SIG},
    ],
    ids=repr,
)
def test_verify_rejects_malformed_tickets(ticket):
    assert not verify(ticket=ticket)


def test_check_is_the_contracts_check_ticket_table():
    """authorize.rs:271-284, row by row, with the operator's key or without one."""

    def check(ticket_pubkey=KEY["ticket_pubkey"], ticket="t1", now_ns=BEFORE, subject=None):
        return es.check_operator_ticket(
            ticket_pubkey,
            contract=CONTRACT,
            subject=T1["subject"] if subject is None else subject,
            ticket=t1_ticket() if ticket == "t1" else ticket,
            now_ns=now_ns,
        )

    assert check() is None
    assert check(ticket_pubkey=bytes.fromhex(KEY["pubkey_hex"])) is None
    assert check(ticket_pubkey=None, ticket=None) is None  # (None, None) => Ok
    assert check(ticket=None) == "E53_TICKET_INVALIDO"  # key, no ticket
    assert check(ticket_pubkey=None) == "E53_TICKET_INVALIDO"  # ticket, no key
    assert check(now_ns=EXPIRY) == "E54_TICKET_VENCIDO"
    # E54 is judged before the signature: an expired ticket of something else is E54.
    assert check(now_ns=EXPIRY, subject=T2["subject"]) == "E54_TICKET_VENCIDO"
    assert check(subject=T2["subject"]) == "E53_TICKET_INVALIDO"
    assert check(ticket={"expiry": T1["expiry"], "sig": SIG + "1"}) == "E53_TICKET_INVALIDO"
    assert check(ticket={"expiry": EXPIRY, "sig": SIG}) == "E53_TICKET_INVALIDO"
    assert check(ticket=[]) == "E53_TICKET_INVALIDO"
    other = ExternalSigner().public_key
    assert check(ticket_pubkey=other) == "E53_TICKET_INVALIDO"
    # verify is check with a key that must be given.
    with pytest.raises(ValueError):
        es.verify_operator_ticket(
            None, contract=CONTRACT, subject=T1["subject"], ticket=t1_ticket(), now_ns=BEFORE
        )
    # Without now_ns the wall clock judges: the vectors' expiry is in the past.
    assert check(now_ns=None) == "E54_TICKET_VENCIDO"
    fresh = sign(expiry=time.time_ns() + 60 * 10**9, now_ns=None)
    assert check(ticket=fresh, now_ns=None) is None


@pytest.mark.parametrize(
    "change",
    [
        {"public_key": b"x"},
        {"public_key": bytes(31)},
        {"public_key": KEY["pubkey_hex"]},
        {"public_key": KEY["ticket_pubkey"][8:]},
        {"public_key": KEY["ticket_pubkey"] + " "},
        {"public_key": "ED25519:" + KEY["ticket_pubkey"][8:]},
        {"public_key": "ed25519:" + _b58encode(bytes([5]) * 31)},
        {"public_key": "ed25519:" + "1" * 60},
        {"public_key": None},
        {"contract": "Bad.near"},
        {"contract": ""},
        {"subject": None},
        {"subject": {"payment": {"payment_id": "1" * 31}}},
        {"now_ns": True},
        {"now_ns": -1},
        {"now_ns": "01"},
        {"now_ns": 2**64},
    ],
    ids=repr,
)
def test_verify_raises_on_the_callers_own_inputs(change):
    with pytest.raises(ValueError):
        verify(**change)
    # Even when the ticket is garbage or expired: a bad config never reads as "invalid ticket".
    for ticket in (None, {"expiry": "1", "sig": SIG}):
        with pytest.raises(ValueError):
            verify(**{"ticket": ticket, **change})


def test_lengths_are_capped_before_base58_decoding(monkeypatch):
    assert len(SIG) == 88
    assert es._ticket_signature(SIG) == RAW

    def no_decode(value):
        pytest.fail("decoded an oversized value")

    monkeypatch.setattr(es, "_b58decode", no_decode)
    with pytest.raises(ValueError):
        es._ticket_signature("1" * 89)
    with pytest.raises(ValueError):
        es._ticket_public_key("ed25519:" + "1" * 45)


# --- signing: every input before the key, every output checked --------------------------------


@pytest.mark.parametrize(
    "change",
    [
        {"contract": "Bad.near"},
        {"contract": "v2.arras-dev.testnet\n"},
        {"contract": None},
        {"subject": None},
        {"subject": {}},
        {"subject": []},
        {"subject": {**T1["subject"], **T2["subject"]}},
        {"subject": {"refund": {"payment_id": V1["payment_id"]}}},
        {"subject": {"payment": [V1["payment_id"]]}},
        {"subject": {"payment": {}}},
        {"subject": {"payment": {"payment_id": V1["payment_id"], "x": 1}}},
        {"subject": {"payment": {"payment_id": "1" * 31}}},
        {"subject": {"payment": {"payment_id": "0" * 44}}},
        {"subject": {"payment": {"payment_id": V1["payment_id"] + "\n"}}},
        {"subject": {"budget": {**T2["subject"]["budget"], "operator_id": "EM"}}},
        {"subject": {"budget": {**T2["subject"]["budget"], "operator_id": ""}}},
        {"subject": {"budget": {**T2["subject"]["budget"], "operator_id": "x" * 33}}},
        {"subject": {"budget": {**T2["subject"]["budget"], "owner": "Payer.testnet"}}},
        {"subject": {"budget": {**T2["subject"]["budget"], "agent": "a..testnet"}}},
        {"subject": {"budget": {"operator_id": "em", "owner": "payer-demo.testnet"}}},
        {"subject": {"budget": {**T2["subject"]["budget"], "x": "y"}}},
        {"expiry": 0},
        {"expiry": "0"},
        {"expiry": -1},
        {"expiry": 2**64},
        {"expiry": True},
        {"expiry": 1.0},
        {"expiry": "01"},
        {"expiry": " 1"},
        {"expiry": None},
    ],
    ids=repr,
)
def test_sign_rejects_inputs_before_the_key(change):
    values = dict(contract=CONTRACT, subject=T1["subject"], expiry=EXPIRY, now_ns=0)
    values.update(change)
    with pytest.raises(ValueError):
        es.sign_operator_ticket(NoKey(), **values)


def test_an_injected_signer_signs_exactly_the_message():
    signer = ExternalSigner()
    ticket = sign(T2["subject"], expiry=7, contract=V2_MAINNET, signer=signer, now_ns=6)
    assert signer.messages == [es.operator_ticket_message(V2_MAINNET, T2["subject"], 7)]
    assert ticket["expiry"] == "7"
    assert es.verify_operator_ticket(
        signer.public_key, contract=V2_MAINNET, subject=T2["subject"], ticket=ticket, now_ns=6
    )
    assert not es.verify_operator_ticket(
        signer.public_key, contract=V2_MAINNET, subject=T2["subject"], ticket=ticket, now_ns=7
    )
    assert not verify(public_key=signer.public_key, ticket=ticket)


@pytest.mark.parametrize(
    "public_key,signature",
    [
        (None, bytes(64)),
        ("ed25519:" + _b58encode(bytes(32)), bytes(64)),
        (bytes(31), bytes(64)),
        (bytes.fromhex(KEY["pubkey_hex"]), bytes(63)),
        (bytes.fromhex(KEY["pubkey_hex"]), "a" * 64),
        (bytes.fromhex(KEY["pubkey_hex"]), bytes(64)),
        (bytes.fromhex(KEY["pubkey_hex"]), _b58decode(T2["sig"])),
        (ExternalSigner().public_key, _b58decode(T1["sig"])),
    ],
)
def test_sign_refuses_a_bad_signer_result(public_key, signature):
    class BadSigner:
        def sign(self, message):
            return signature

    bad = BadSigner()
    bad.public_key = public_key
    with pytest.raises(ValueError):
        sign(signer=bad)


def test_ticket_crypto_needs_the_near_extra(monkeypatch):
    original = builtins.__import__

    def without_crypto(name, *args, **kwargs):
        if name.startswith("cryptography"):
            raise ImportError("cryptography unavailable")
        return original(name, *args, **kwargs)

    class PureSigner:
        public_key = bytes.fromhex(KEY["pubkey_hex"])

        def sign(self, message):
            return _b58decode(T1["sig"])

    monkeypatch.setattr(builtins, "__import__", without_crypto)
    with pytest.raises(ImportError, match=r"uvd-x402-sdk\[near\]"):
        sign(signer=PureSigner())
    with pytest.raises(ImportError, match=r"uvd-x402-sdk\[near\]"):
        verify()
    # The message itself needs nothing beyond the base install.
    assert es.operator_ticket_message(CONTRACT, T1["subject"], EXPIRY).hex() == T1["mensaje_hex"]


# --- the v2 authorize msg (msg.rs) -------------------------------------------------------------


def test_v2_canonical_msgs_from_the_vectors():
    m = V2["msg_authorize"]
    assert es.build_escrow_msg(V1) == m["canonico_v1_en_v2"] == VECTORS["msg_canonico_v1"]
    with_deadline = {**V1, "dispute_deadline": V1["release_deadline"]}
    assert es.build_escrow_msg(with_deadline) == m["canonico_con_dispute_deadline"]
    ticket = sign({"payment": {"payment_id": es.compute_payment_id(V1)}}, V1["release_deadline"])
    assert (
        es.build_escrow_msg({**with_deadline, "operator_ticket": ticket})
        == (m["canonico_con_ticket_t1"])
    )
    for key in ("canonico_v1_en_v2", "canonico_con_dispute_deadline", "canonico_con_ticket_t1"):
        parsed = json.loads(m[key])
        # A served msg, parsed, rebuilds to itself: what a payer signs is what it was given.
        assert es.build_escrow_msg(parsed) == m[key]
        # dispute_deadline and operator_ticket do not enter the payment id (msg.rs:5-6).
        assert (
            es.compute_payment_id(
                {**parsed, "token": TOKEN, "payer": V1["payer"], "amount": V1["amount"]}
            )
            == m["payment_id_de_los_tres"]
        )


def test_v2_msg_key_order_and_absent_fields():
    text = es.build_escrow_msg({**V1, "operator_ticket": t1_ticket()})
    assert list(json.loads(text)) == [
        "v",
        "action",
        "operator_id",
        "receiver",
        "release_deadline",
        "reclaim_after",
        "fee_bps_max",
        "terms_hash",
        "salt",
        "operator_ticket",
    ]
    assert text.endswith(
        ',"operator_ticket":'
        + json.dumps({"expiry": str(EXPIRY), "sig": SIG}, separators=(",", ":"))
        + "}"
    )
    # Ticket keys are written expiry, sig whatever order they come in.
    reordered = {"sig": SIG, "expiry": str(EXPIRY)}
    assert es.build_escrow_msg({**V1, "operator_ticket": reordered}) == text
    # None is absent: the canonical text omits it (msg.rs:66-69, skip_serializing_if).
    assert (
        es.build_escrow_msg({**V1, "dispute_deadline": None, "operator_ticket": None})
        == VECTORS["msg_canonico_v1"]
    )
    deadline_only = es.build_escrow_msg({**V1, "dispute_deadline": int(V1["reclaim_after"])})
    assert json.loads(deadline_only)["dispute_deadline"] == V1["reclaim_after"]


def test_dispute_deadline_bounds():
    release, reclaim = int(V1["release_deadline"]), int(V1["reclaim_after"])
    for deadline in (release, release + 1, reclaim - 1, reclaim, str(reclaim)):
        assert json.loads(es.build_escrow_msg({**V1, "dispute_deadline": deadline}))[
            "dispute_deadline"
        ] == str(deadline)
    with pytest.raises(ValueError, match="E67"):
        es.build_escrow_msg({**V1, "dispute_deadline": release - 1})
    with pytest.raises(ValueError, match="E11"):
        es.build_escrow_msg({**V1, "dispute_deadline": reclaim + 1})


@pytest.mark.parametrize(
    "value", [True, 1.0, -1, 2**64, "01", " 1", "+1", "1_0", "1\n", "", [], {}]
)
def test_dispute_deadline_type(value):
    with pytest.raises(ValueError):
        es.build_escrow_msg({**V1, "dispute_deadline": value})


@pytest.mark.parametrize(
    "ticket",
    [
        "x",
        [],
        {},
        {"expiry": T1["expiry"]},
        {**t1_ticket(), "x": 1},
        {"expiry": "0" + T1["expiry"], "sig": SIG},
        {"expiry": 2**64, "sig": SIG},
        {"expiry": True, "sig": SIG},
        # A JSON number, as json.loads reads a served msg: the contract answers E03.
        {"expiry": EXPIRY, "sig": SIG},
    ]
    + [{"expiry": T1["expiry"], "sig": sig} for sig in MALFORMED_SIGS],
    ids=repr,
)
def test_msg_refuses_a_malformed_ticket(ticket):
    with pytest.raises(ValueError):
        es.build_escrow_msg({**V1, "operator_ticket": ticket})


@pytest.mark.parametrize("sig", OTHER_SIGS)
def test_msg_cannot_tell_a_wrong_but_well_formed_signature(sig):
    # Shape only: without the operator key the msg builder cannot verify (the chain says E53).
    assert json.loads(es.build_escrow_msg({**V1, "operator_ticket": {"expiry": "1", "sig": sig}}))[
        "operator_ticket"
    ] == {"expiry": "1", "sig": sig}


# --- the v2 contract in the escrow client --------------------------------------------------------


def test_v2_registry():
    assert isinstance(es.NEAR_ESCROW_V2_CONTRACTS, MappingProxyType)
    assert dict(es.NEAR_ESCROW_V2_CONTRACTS) == {
        "near:testnet": (),
        "near:mainnet": ("v2.arras-escrow.near",),
    }
    assert es.NEAR_ESCROW_V2_CONTRACTS.keys() == es.NEAR_ESCROW_TOKENS.keys()
    with pytest.raises(TypeError):
        es.NEAR_ESCROW_V2_CONTRACTS["near:mainnet"] = ("evil-sink.near",)
    with pytest.raises(TypeError):
        es.NEAR_ESCROW_V2_CONTRACTS["near:mainnet"][0] = "evil-sink.near"
    # NEAR_ESCROW_CONTRACTS is every contract the SDK signs for, v1 first; v2 is a subset.
    assert es.NEAR_ESCROW_CONTRACTS["near:mainnet"] == ("v1.arras-escrow.near", V2_MAINNET)
    for network in es.NEAR_ESCROW_TOKENS:
        v1 = es.NEAR_ESCROW_CONTRACTS[network][0]
        assert es.near_escrow_contract_version(network, v1) == "v1"
        assert set(es.NEAR_ESCROW_V2_CONTRACTS[network]) <= set(es.NEAR_ESCROW_CONTRACTS[network])
        for contract in es.NEAR_ESCROW_CONTRACTS[network]:
            expected = "v2" if contract in es.NEAR_ESCROW_V2_CONTRACTS[network] else "v1"
            assert es.near_escrow_contract_version(network, contract) == expected
    for network, contract in [
        ("near:testnet", V2_MAINNET),
        ("near:mainnet", V2_TESTNET_REHEARSAL),
        # The testnet rehearsal of v2 takes a test token, not Circle's USDC: not registered.
        ("near:testnet", V2_TESTNET_REHEARSAL),
        ("near:testnet", CONTRACT),
        ("near:testnet", "arras-v2-1103.testnet"),
        ("near:mainnet", "v3.arras-escrow.near"),
        ("near:mainnet", "V2.arras-escrow.near"),
        ("near:MAINNET", V2_MAINNET),
        ("near", V2_MAINNET),
    ]:
        with pytest.raises(ValueError):
            es.near_escrow_contract_version(network, contract)


def test_v2_authorization_wire():
    network = "near:mainnet"  # the only network with a registered v2 contract
    o = v2_options(network)
    token = es.NEAR_ESCROW_TOKENS[network]
    payment = es.sign_escrow_authorization(o, token=token, amount=V1["amount"], msg=v2_terms())
    assert payment["network"] == network and payment["scheme"] == "escrow"
    d = decode(payment)
    assert d["sender"] == V1["payer"] and d["receiver"] == token
    assert d["method"] == "ft_transfer_call" and d["deposit"] == 1
    assert d["gas"] == es.NEAR_ESCROW_MAX_GAS
    assert d["args"] == {
        "receiver_id": o.escrow_contract,
        "amount": V1["amount"],
        "msg": V2["msg_authorize"]["canonico_con_ticket_t1"],
    }
    # A v1-shaped msg is a valid v2 msg too (msg.rs:3-6).
    plain = decode(es.sign_escrow_authorization(o, token=token, amount=V1["amount"], msg=V1))
    assert plain["args"]["msg"] == VECTORS["msg_canonico_v1"]
    delegate = es.build_escrow_authorization_delegate(
        o, token=token, amount=V1["amount"], msg=v2_terms()
    )
    es.validate_escrow_authorization_delegate(
        delegate, o, token=token, amount=V1["amount"], msg=v2_terms()
    )
    with pytest.raises(ValueError):
        es.validate_escrow_authorization_delegate(
            delegate, o, token=token, amount=V1["amount"], msg=V1
        )


@pytest.mark.parametrize("network", ["near:testnet", "near:mainnet"])
@pytest.mark.parametrize(
    "extra",
    [
        {"dispute_deadline": V1["release_deadline"]},
        {"operator_ticket": {"expiry": T1["expiry"], "sig": T1["sig"]}},
        {"dispute_deadline": V1["reclaim_after"], "operator_ticket": {"expiry": "1", "sig": SIG}},
    ],
)
def test_v1_contract_refuses_v2_fields_before_the_key(network, extra):
    o = options(
        network=network, escrow_contract=es.NEAR_ESCROW_CONTRACTS[network][0], signer=NoKey()
    )
    with pytest.raises(ValueError, match="v2"):
        es.sign_escrow_authorization(
            o, token=es.NEAR_ESCROW_TOKENS[network], amount=V1["amount"], msg={**V1, **extra}
        )


@pytest.mark.parametrize("method", ["authorize"] + sorted({m for m, _ in LIFECYCLE}))
def test_the_network_is_refused_before_the_accounts(method):
    # 0.112.0's order and the TypeScript SDK's: the network, then the accounts, then the row.
    for network in ("near:MAINNET", "near", "eip155:1"):
        o = options(network=network, sender_id="", escrow_contract="", signer=NoKey())
        with pytest.raises(ValueError, match="unsupported NEAR network"):
            if method == "authorize":
                es.sign_escrow_authorization(o, token=TOKEN, amount=V1["amount"], msg=V1)
            else:
                es.sign_escrow_lifecycle(o, method, dict(LIFECYCLE)[method])
    for network in (["near:mainnet"], None, 1):
        with pytest.raises(ValueError, match="unsupported NEAR network"):
            es.near_escrow_contract_version(network, V2_MAINNET)


@pytest.mark.parametrize(
    "network,contract",
    [
        ("near:mainnet", "V2.arras-escrow.near"),
        ("near:mainnet", " v2.arras-escrow.near"),
        ("near:mainnet", "v2.arras-escrow.near "),
        ("near:mainnet", "v2.arras-escrow.near\n"),
        ("near:mainnet", "v3.arras-escrow.near"),
        ("near:mainnet", "v2.ａｒｒａｓ-escrow.near"),
        ("near:mainnet", "v2.arras-v2e-1107.testnet"),
        ("near:testnet", "v2.arras-escrow.near"),
        ("near:testnet", "v2.arras-dev.testnet"),
        ("near:testnet", "arras-v2-1103.testnet"),
        ("near:testnet", "V2.arras-v2e-1107.testnet"),
        ("near:testnet", V2_TESTNET_REHEARSAL),
    ],
)
@pytest.mark.parametrize("method", ["authorize"] + sorted({m for m, _ in LIFECYCLE}))
def test_unregistered_v2_lookalikes_before_the_key(network, contract, method):
    o = options(network=network, escrow_contract=contract, signer=NoKey())
    with pytest.raises(ValueError):
        if method == "authorize":
            es.sign_escrow_authorization(
                o, token=es.NEAR_ESCROW_TOKENS[network], amount=V1["amount"], msg=v2_terms()
            )
        else:
            es.sign_escrow_lifecycle(o, method, dict(LIFECYCLE)[method])


@pytest.mark.parametrize("method,args", LIFECYCLE)
def test_v2_lifecycle_is_the_v1_wire_on_the_v2_contract(method, args):
    network = "near:mainnet"
    o = v2_options(network)
    d = decode(es.sign_escrow_lifecycle(o, method, args))
    assert d["receiver"] == V2_MAINNET
    # release without an amount sends "amount": null, as on v1.
    assert d["method"] == method and d["args"].items() >= args.items() and d["deposit"] == 0
    assert d["gas"] == (50 if method.startswith("withdraw") else 30) * 10**12
    v1 = decode(
        es.sign_escrow_lifecycle(
            options(network=network, escrow_contract=es.NEAR_ESCROW_CONTRACTS[network][0]),
            method,
            args,
        )
    )
    assert (d["method"], d["args_text"], d["gas"], d["deposit"]) == (
        v1["method"],
        v1["args_text"],
        v1["gas"],
        v1["deposit"],
    )


def test_storage_deposit_on_the_v2_contract():
    network, contract = "near:mainnet", V2_MAINNET
    tx = storage.build_arras_storage_deposit(
        network=network, payer_id="parent.near", escrow_contract=contract, account_id="agent.near"
    )
    assert tx["receiverId"] == contract
    v1 = storage.build_arras_storage_deposit(
        network=network,
        payer_id="parent.near",
        escrow_contract=es.NEAR_ESCROW_CONTRACTS[network][0],
        account_id="agent.near",
    )
    assert tx["actions"] == v1["actions"]
    for net, other in [
        (network, "v2.arras-dev.testnet"),
        (network, "V2.arras-escrow.near"),
        (network, "arras-v2-1103.testnet"),
        ("near:testnet", V2_MAINNET),
        ("near:testnet", V2_TESTNET_REHEARSAL),
    ]:
        with pytest.raises(ValueError):
            storage.build_arras_storage_deposit(
                network=net, payer_id="parent.near", escrow_contract=other
            )


def test_v2_facilitator_envelope():
    from uvd_x402_sdk.envelope_v2 import build_verify_request_v2

    o = v2_options()
    token = es.NEAR_ESCROW_TOKENS[o.network]
    msg = es.build_escrow_msg(v2_terms())
    accepted = {
        "scheme": "escrow",
        "network": o.network,
        "asset": token,
        "amount": V1["amount"],
        "payTo": o.escrow_contract,
        "maxTimeoutSeconds": 300,
        "extra": {
            "nearEscrow": {
                "contract": o.escrow_contract,
                "operatorId": V1["operator_id"],
                "msg": msg,
                "paymentId": es.compute_payment_id({**V1, "token": token}),
            }
        },
    }
    payment = es.sign_escrow_authorization(o, token=token, amount=V1["amount"], msg=v2_terms())
    body = build_verify_request_v2(
        payment["payload"],
        {"url": "https://seller.example/task", "description": "t", "mimeType": "application/json"},
        accepted,
    )
    assert decode({"payload": body["paymentPayload"]["payload"]})["args"]["msg"] == msg
    assert body["accepted"]["extra"]["nearEscrow"]["msg"] == msg


# --- v1: not one byte moved ----------------------------------------------------------------------

# sha256 of every v1 output, recorded from origin/main 4e01180 (0.112.0) before this change.
V1_DIGESTS = {
    "near:mainnet": {
        "V-1/authorize": "e7e3338fffc1af89f48f070b19669f8c74e0b0ae83342a24c62c227c166573b5",
        "V-1/msg": "515b6855096ffdc4250d85e4a4a311eb14b569be8150f63f7851026bc08e86c7",
        "V-1/reclaim": "8cbc665927a0ee766a9ac47180828d67ba9e4d4fa9f7cf5c26c7861e9f3e05e6",
        "V-1/refund": "9c50826d23ca38afe6b22573201d1f82fd10beea7468dab862da3386962ade56",
        "V-1/release": "0182092989f134106ba00278a738c1e74fa54e1ab167dbc94ed0e02af8f75008",
        "V-1/release+amount": "b511f5d1c8c6a1182cf7a0c3b3e3e0deb4ea0738c5f12e2cfd7705f894796a2f",
        "V-1/withdraw": "9e099d73aa467746b11b556ce672f9b8e987868e652f90ccf2323e9d084e47ef",
        "V-1/withdraw_for": "993fd98186bf64295bf97f55ac4ea57e0744c900a81dc89b005c02d74618cc69",
        "V-2/authorize": "2129064a3f46a1b07ec7afe1e2314a1af2f61afdeebf85b3617637593c4e6064",
        "V-2/msg": "7f7cb19fa2366515d77ee58a8d1ae3a75bb686d591c5fb6ab99fd61307e911ae",
        "V-2/reclaim": "80c6dab771089b2a5a368b72ce3229ad8697028ea9e30bec8966d4133928c229",
        "V-2/refund": "a3fd1f2ba8e1492deb6d2b3817b3220afe28393f812a1fc8d5f368210c2e91fd",
        "V-2/release": "bdb91d968b8b92d69eab5f8471d95d06115b58d96a3ef33917e979d755ce9a95",
        "V-2/release+amount": "82f21b5198f1f80c50f57c5e471ce3c56daf65acf4917eb8f7f117e949357050",
        "V-2/withdraw": "9e099d73aa467746b11b556ce672f9b8e987868e652f90ccf2323e9d084e47ef",
        "V-2/withdraw_for": "993fd98186bf64295bf97f55ac4ea57e0744c900a81dc89b005c02d74618cc69",
        "V-3/authorize": "42a4317cf9e20ca18f2a7da109f316879680c61564db5397cdd5300930f30db0",
        "V-3/msg": "a34058aabf5780d584beeb65474040b3693fd642f1c88402b6c270a1c44a013b",
        "V-3/reclaim": "1ae53e79d5413980bf4cf7939ea52fda23cad1e17224f068718dd29f09d85e65",
        "V-3/refund": "d3602cea24f4685d3f74e49b009dad3db5cec3ed3a604230fb588fba249be5b2",
        "V-3/release": "0cf63f87ad6c778d278422bab7cf6df99d83851ee8cf613bfcf7db84ebd478f7",
        "V-3/release+amount": "cb85a456682efb89c39f4c2c9c23844425ea1ef8fdcfa6f0a16d027bd0fd5352",
        "V-3/withdraw": "dfd9d775bea3b049c4fc92a608e2c8250b9b3d3c279b7a0de58b1587758f4c09",
        "V-3/withdraw_for": "38179423719aed46af9c1dab61f3d0a56dcaaa0b44049ea8861e9b71c47c2685",
        "storage": "89615da924f3a7c3bff3fd8f638a4c3eae9f2eaca3b3cc272d6a05f99c0ee04d",
    },
    "near:testnet": {
        "V-1/authorize": "37fe2dc8141632a6aef01bc3a9b1225236a211af1ff46e6ec5f3e6cf95a02066",
        "V-1/msg": "515b6855096ffdc4250d85e4a4a311eb14b569be8150f63f7851026bc08e86c7",
        "V-1/reclaim": "c4ab984bd9510f48422efe670473daaf20fde6a78db2fe2943db80eb6d770824",
        "V-1/refund": "f1204f7eb4bc4211fb41ad42d32d10107ccd5fed8f7064e916b78a1b38dcff20",
        "V-1/release": "52cdcded5595937c4465bcf0f1cd731b52d35be16642e09ba8247c4a9a701dfa",
        "V-1/release+amount": "b186edb9b426d6ad1f8b405c387e11477379f8cf9589e3925a207b61791b59f5",
        "V-1/withdraw": "a9bf49b64cb4408b2f7e5a8babff1cec1bc309c948c02b407f286981ce1859e2",
        "V-1/withdraw_for": "15d08ed12fbd6ec6632ab3a1683cb941b17ab2a65a7d3f6e9c6590d216e68447",
        "V-2/authorize": "56d8a376f02506c6ad3fa8a9ac6da14ca7dcb5ab7fff600f3289f5db1317f81b",
        "V-2/msg": "7f7cb19fa2366515d77ee58a8d1ae3a75bb686d591c5fb6ab99fd61307e911ae",
        "V-2/reclaim": "baaa9bc055fdbeec2622b392237e815b63f40650d846a81465ea5ba94d3b664f",
        "V-2/refund": "231eab11cf1589cd947a325e58890c985570d04eac1209db388a207c7cd83c1f",
        "V-2/release": "b2aeab7d8460af98ea945a7d7aa1424ae2faba2913aaa912c007fde4d3c19b73",
        "V-2/release+amount": "57b4a23b1f5ab797a1a3cf44c137cba8b273a9ed265f2a693985017db9d48dfb",
        "V-2/withdraw": "a9bf49b64cb4408b2f7e5a8babff1cec1bc309c948c02b407f286981ce1859e2",
        "V-2/withdraw_for": "15d08ed12fbd6ec6632ab3a1683cb941b17ab2a65a7d3f6e9c6590d216e68447",
        "V-3/authorize": "29c1d0f50d29bc42005533869722487233d014a3959c4253ac677abbb93ca543",
        "V-3/msg": "a34058aabf5780d584beeb65474040b3693fd642f1c88402b6c270a1c44a013b",
        "V-3/reclaim": "5f13ded6abc24fed0b038a0bbdce1693f2d6e48e0956fcda674ebddbaa1cb95e",
        "V-3/refund": "544ff90ae07eaaa553a072a7ae1f300764a3ecd00c066243610dd686ab74bee1",
        "V-3/release": "c59fd68c1d0e4f37d921ab5afb6414428e4137d97b1e5f833829940c51efb692",
        "V-3/release+amount": "b1025bba4cda48486706636f356c1c6cd1461b9c9e51a4d0c4dc3f5870521c48",
        "V-3/withdraw": "8bfa721ebbf0b4b0ce472718c88e24bafb760863341bf9972b30421a1d5195be",
        "V-3/withdraw_for": "1169d623f4998a2e65e14f83d24cc2c3ef9ae0248840cd2d1432a977ef205e81",
        "storage": "261dd6b469176ea2f7fb21cbe5f9d019c8025077805303ecbcdc9e603829a51d",
    },
}


def _digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def test_v1_output_is_byte_for_byte_the_previous_release():
    actual: dict = {}
    for network in ("near:testnet", "near:mainnet"):
        row = actual.setdefault(network, {})
        contract = es.NEAR_ESCROW_CONTRACTS[network][0]
        token = es.NEAR_ESCROW_TOKENS[network]
        for v in VECTORS["payment_id"]["vectores"]:
            o = options(network=network, escrow_contract=contract, sender_id=v["payer"])
            row[f"{v['id']}/msg"] = hashlib.sha256(
                es.build_escrow_msg(v).encode("utf-8")
            ).hexdigest()
            row[f"{v['id']}/authorize"] = _digest(
                es.sign_escrow_authorization(o, token=token, amount=v["amount"], msg=v)
            )
            for name, method, args in (
                ("release", "release", {"payment_id": v["payment_id"]}),
                ("release+amount", "release", {"payment_id": v["payment_id"], "amount": "400000"}),
                ("refund", "refund", {"payment_id": v["payment_id"]}),
                ("reclaim", "reclaim", {"payment_id": v["payment_id"]}),
                ("withdraw", "withdraw", {}),
                ("withdraw_for", "withdraw_for", {"account": v["receiver"]}),
            ):
                row[f"{v['id']}/{name}"] = _digest(es.sign_escrow_lifecycle(o, method, args))
        row["storage"] = _digest(
            storage.build_arras_storage_deposit(
                network=network,
                payer_id="parent.near",
                escrow_contract=contract,
                account_id="agent.near",
            )
        )
    assert actual == V1_DIGESTS
