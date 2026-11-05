"""Offline token-mode OWS parity, altered signatures and pre-key refusals."""

import base64
import copy
import hashlib
import importlib.util
import inspect
import json
import socket
import sys
import tempfile
from importlib.metadata import version
from pathlib import Path

import pytest

from tests.near_ows_double import MemoryNearOws
from tests.test_near_erc8004 import config
from tests.test_near_escrow import authorize, options
from tests.test_near_kk import exact_options, pay
from uvd_x402_sdk import near_erc8004 as erc
from uvd_x402_sdk.near_escrow import NearEd25519Signer
from uvd_x402_sdk.near_nep413 import sign_nep413
from uvd_x402_sdk.near_ows import OwsNearSigner
from uvd_x402_sdk.near_storage import (
    build_arras_storage_deposit,
    build_usdc_storage_deposit,
    sign_storage_transaction,
)
from uvd_x402_sdk.solana_signing import _b58encode

SEED = bytes(range(32))
LOCAL = NearEd25519Signer(SEED)
BLOCK = _b58encode(bytes(range(32)))
HAS_OWS = importlib.util.find_spec("ows") is not None
VECTORS = Path(__file__).parent / "fixtures/near-ows.json"


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*args, **kwargs):
        pytest.fail("OWS signing must never open a network connection")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)


@pytest.fixture
def memory(monkeypatch):
    backend = MemoryNearOws()
    monkeypatch.setitem(sys.modules, "ows", backend)
    return backend


def signer(**changes):
    options = dict(
        wallet="test",
        network="near:testnet",
        token="ows_key_" + "ab" * 32,
        public_key=LOCAL.public_key,
        vault_path=Path("temporary-test-vault"),
    )
    options.update(changes)
    return OwsNearSigner(**options)


@pytest.fixture(params=["memory", "real"])
def backend_signer(request, tmp_path, monkeypatch):
    if request.param == "memory":
        backend = MemoryNearOws()
        monkeypatch.setitem(sys.modules, "ows", backend)
        yield signer()
    else:
        if not HAS_OWS:
            pytest.skip("OWS 1.4.2 wheels require Linux glibc or macOS, CPython 3.9-3.13")
        import ows

        assert version("open-wallet-standard") == "1.4.2"
        for name in ("sign_message", "sign_transaction"):
            # The in-memory backend has exactly the binding signature (except self).
            assert inspect.signature(getattr(MemoryNearOws(), name)) == inspect.signature(
                getattr(ows, name)
            )
        # Linux uses tmpfs: no test wallet private key is persisted on disk.
        parent = Path("/dev/shm") if Path("/dev/shm").is_dir() else tmp_path
        with tempfile.TemporaryDirectory(dir=parent) as vault:
            wallet = ows.import_wallet_private_key(
                "test",
                SEED.hex(),
                chain="near:testnet",
                passphrase="test-only",
                vault_path_opt=vault,
            )
            token = ows.create_api_key(
                "test", [wallet["id"]], [], "test-only", vault_path_opt=vault
            )["token"]
            yield signer(token=token, vault_path=vault)


def transactions():
    return [
        (
            "arras",
            build_arras_storage_deposit(
                network="near:testnet",
                payer_id="payer.testnet",
                escrow_contract="arras-v1b-1101.testnet",
            ),
        ),
        ("usdc", build_usdc_storage_deposit(network="near:testnet", payer_id="payer.testnet")),
        (
            "register",
            erc.build_erc8004_register(
                network="near:testnet",
                signer_id="payer.testnet",
                config=config(network="near:testnet"),
                agent_uri="ipfs://card",
            ),
        ),
    ]


def signed_tx(name, descriptor, key):
    if name == "register":
        return erc.sign_erc8004_transaction(
            descriptor, key, 7, BLOCK, network="near:testnet", config=config(network="near:testnet")
        )
    return sign_storage_transaction(descriptor, key, 7, BLOCK, network="near:testnet")


@pytest.mark.parametrize("name,descriptor", transactions())
def test_shared_transaction_vectors(backend_signer, name, descriptor):
    actual = signed_tx(name, descriptor, backend_signer)
    assert actual == signed_tx(name, descriptor, LOCAL)
    vector = next(v for v in json.loads(VECTORS.read_bytes())["transactions"] if v["id"] == name)
    assert descriptor == vector["descriptor"]
    assert actual == vector["signedTransactionBase64"]
    assert base64.b64decode(actual)[:-65].hex() == vector["unsignedHex"]


@pytest.mark.parametrize("callback", [None, "", "https://example.com/callback"])
def test_nep413_and_delegates(backend_signer, callback):
    args = dict(
        account_id="payer.testnet",
        message="Hola 🌎",
        nonce=SEED,
        recipient="example.com",
        callback_url=callback,
    )
    actual = sign_nep413(backend_signer, **args)
    assert actual == sign_nep413(LOCAL, **args)
    vectors = json.loads(VECTORS.read_bytes())["nep413"]
    assert actual == next(v["signed"] for v in vectors if v["callbackUrl"] == callback)
    assert pay(exact_options(signer=backend_signer)) == pay(exact_options(signer=LOCAL))
    assert authorize(options(signer=backend_signer)) == authorize(options(signer=LOCAL))


def test_transactions_use_whole_wire_not_digest_or_message(memory):
    for name, descriptor in transactions():
        raw = base64.b64decode(signed_tx(name, descriptor, signer()))
        call = memory.calls[-1]
        assert call[0] == "sign_transaction"
        assert call[2] == "near:testnet"
        assert bytes.fromhex(call[3]) == raw[:-65]
        assert len(bytes.fromhex(call[3])) > 32
    digest = hashlib.sha256(b"nep413").digest()
    assert signer().sign(digest) == LOCAL.sign(digest)
    assert memory.calls[-1][0] == "sign_message"
    assert memory.calls[-1][3] == digest.hex()


@pytest.mark.parametrize(
    "token",
    [
        None,
        "",
        "owner-passphrase",
        "ows_key_",
        "ows_key_" + "a" * 63,
        "OWS_KEY_" + "ab" * 32,
        "ows_key_" + "AB" * 32,
        " ows_key_" + "ab" * 32,
        "ows_key_" + "ab" * 32 + "\n",
        "ows_key_0x" + "ab" * 32,
        "ows_key_" + "ａ" * 64,
    ],
)
def test_owner_credentials_and_malformed_tokens_never_reach_ows(memory, token):
    with pytest.raises(ValueError, match="API token"):
        signer(token=token)
    assert not memory.calls


@pytest.mark.parametrize(
    "changes",
    [
        dict(wallet=""),
        dict(wallet=" test"),
        dict(wallet=None),
        dict(network="NEAR:TESTNET"),
        dict(network="near:testnet "),
        dict(network=None),
        dict(public_key=b"x"),
        dict(public_key=None),
        dict(index=True),
        dict(index=-1),
        dict(index=2**32),
        dict(vault_path=""),
        dict(vault_path=None),
        dict(vault_path=b"path"),
        dict(vault_path="bad\x00path"),
    ],
)
def test_constructor_guards(memory, changes):
    with pytest.raises(ValueError):
        signer(**changes)
    assert not memory.calls


def test_no_passphrase_argument_and_no_credential_in_repr(memory):
    with pytest.raises(TypeError):
        signer(passphrase="owner-passphrase")
    s = signer()
    assert s._token not in repr(s)
    s._token = "owner-passphrase"
    with pytest.raises(ValueError, match="API token"):
        s.sign(SEED)
    assert not memory.calls


@pytest.mark.parametrize("value", [None, "", b"", b"x" * 31, b"x" * 33, bytearray(32)])
def test_digest_shape(memory, value):
    with pytest.raises(ValueError):
        signer().sign(value)
    assert not memory.calls


@pytest.mark.parametrize("value", [None, "", b"", bytearray(100)])
def test_transaction_shape(memory, value):
    with pytest.raises(ValueError):
        signer().sign_transaction(value)
    assert not memory.calls


@pytest.mark.parametrize("operation", ["sign", "sign_transaction"])
def test_signature_for_another_digest_is_refused(memory, operation):
    memory.transform = lambda _: LOCAL.sign(bytes(32)).hex()
    with pytest.raises(ValueError, match="signature"):
        getattr(signer(), operation)(SEED)


@pytest.mark.parametrize(
    "result",
    [
        None,
        {},
        {"signature": None},
        {"signature": "ab" * 63},
        {"signature": "0x" + "ab" * 64},
        {"signature": "0X" + "ab" * 64},
        {"signature": "ab" * 64 + "\n"},
        {"signature": "zz" * 64},
        {"signature": "ab" * 64, "recovery_id": 0},
    ],
)
def test_bad_signature_encoding(memory, result):
    memory.sign_message = lambda *a, **k: result
    with pytest.raises(ValueError, match="signature"):
        signer().sign(SEED)


def test_backend_errors_do_not_echo_credentials(memory):
    def fail(*args, **kwargs):
        raise RuntimeError(kwargs["passphrase"])

    memory.sign_message = fail
    with pytest.raises(ValueError, match="OWS NEAR signing failed") as exc:
        signer().sign(SEED)
    assert "ows_key_" not in str(exc.value)
    assert exc.value.__suppress_context__


def test_wrong_key_and_network_are_refused(memory):
    other = NearEd25519Signer(bytes(reversed(SEED)))
    with pytest.raises(ValueError, match="signature"):
        signer(public_key=other.public_key).sign(SEED)
    memory.calls.clear()
    for name, tx in transactions():
        with pytest.raises(ValueError, match="network mismatch"):
            signed_tx(name, tx, signer(network="near:mainnet"))
    with pytest.raises(ValueError, match="network mismatch"):
        pay(exact_options(signer=signer(network="near:mainnet")))
    assert not memory.calls


@pytest.mark.parametrize(
    "change", ["target", "gas", "deposit", "method", "args", "bool", "extra", "action"]
)
def test_storage_revalidates_before_signing(memory, change):
    tx = transactions()[1][1]
    params = tx["actions"][0]["params"]
    if change == "target":
        tx["receiverId"] = "other.testnet"
    elif change == "gas":
        params["gas"] = "1"
    elif change == "deposit":
        params["deposit"] = "0"
    elif change == "method":
        params["methodName"] = "ft_transfer"
    elif change == "args":
        params["args"]["extra"] = "bad"
    elif change == "bool":
        params["args"]["registration_only"] = 1
    elif change == "extra":
        tx["extra"] = "bad"
    elif change == "action":
        tx["actions"].append(copy.deepcopy(tx["actions"][0]))
    with pytest.raises(ValueError):
        signed_tx("usdc", tx, signer())
    assert not memory.calls


@pytest.mark.parametrize(
    "nonce,block",
    [
        (0, BLOCK),
        (True, BLOCK),
        (2**64, BLOCK),
        (7, None),
        (7, "x"),
        (7, "0" * 32),
        (7, "1" * 33),
        (7, BLOCK + "\n"),
    ],
)
def test_nonce_and_block_guards(memory, nonce, block):
    with pytest.raises(ValueError):
        sign_storage_transaction(
            transactions()[1][1], signer(), nonce, block, network="near:testnet"
        )
    assert not memory.calls


def test_optional_dependency_does_not_change_base_imports():
    import subprocess

    code = """
import builtins
real_import = builtins.__import__
def blocked(name, *args, **kwargs):
    if name == 'ows' or name.startswith('cryptography'):
        raise ImportError('intentionally absent')
    return real_import(name, *args, **kwargs)
builtins.__import__ = blocked
from uvd_x402_sdk import OwsNearSigner, send_near_transaction, sign_storage_transaction
from uvd_x402_sdk.near_storage import build_usdc_storage_deposit
assert build_usdc_storage_deposit(network='near:testnet', payer_id='payer.testnet')
try:
    OwsNearSigner('test', network='near:testnet', token='ows_key_' + 'ab' * 32,
                  public_key=bytes(32), vault_path='temporary-test-vault')
except ImportError as error:
    assert 'uvd-x402-sdk[ows]' in str(error)
else:
    raise AssertionError('missing optional dependency must fail at construction')
"""
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=20
    )
    assert result.returncode == 0, result.stderr


def test_published_extra_pin_and_vector_generator():
    import subprocess

    root = Path(__file__).resolve().parents[1]
    manifest = (root / "pyproject.toml").read_text(encoding="utf-8")
    assert 'ows = ["cryptography>=42.0.0", "open-wallet-standard==1.4.2"]' in manifest
    result = subprocess.run(
        [sys.executable, str(root / "scripts/near_ows_vectors.py"), "--check"],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("path", ["vault/test", "vault\\test"])
def test_vault_path_is_passed_without_platform_reinterpretation(memory, path):
    signer(vault_path=path).sign(SEED)
    assert memory.calls[-1][-1] == path


def test_uppercase_signature_hex_still_verifies(memory):
    memory.transform = str.upper
    assert signer().sign(SEED) == LOCAL.sign(SEED)


def test_previous_erc8004_mutation_sites_still_exist_after_signing_extraction():
    from scripts.near_erc8004_mutations import SOURCE, mutations

    assert list(mutations(SOURCE.read_text(encoding="utf-8")))
    from uvd_x402_sdk import near_transactions

    source = inspect.getsource(near_transactions)
    for site in (
        "Ed25519PublicKey.from_public_bytes(public_key).verify(signature, digest)",
        ".write_u8(2)",
    ):
        assert source.count(site) == 1


def test_all_new_mutation_sites_are_live():
    from scripts.near_ows_mutations import MODULES, ROOT, mutants

    for name in MODULES:
        source = (ROOT / f"src/uvd_x402_sdk/{name}.py").read_text(encoding="utf-8")
        assert list(mutants(name, source))
