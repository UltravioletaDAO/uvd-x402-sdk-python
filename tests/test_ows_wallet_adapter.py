"""``OWSWalletAdapter`` against the real ``ows`` (open-wallet-standard 1.4.2).

Until this module the adapter was written against an API nobody had measured:
``get_wallet(name, passphrase=...)``, ``sign_message(wallet_name=...)``,
``sign_typed_data(wallet_name=..., domain=..., types=..., message=...)``,
``sign_transaction(wallet_name=..., transaction=...)`` and a ``sign_eip3009``
the library does not have. Against 1.4.2 each call raised ``TypeError`` (the
last ``AttributeError``), and the suite was green because its double had the
same invented signatures. What 1.4.2 is, measured:

* ``get_wallet(name_or_id, vault_path_opt=None)`` -> dict, the EVM address in
  ``accounts[chain_id="eip155:*"]``. No passphrase: the address is not secret.
* ``sign_message(wallet, chain, message, passphrase=None, encoding=None,
  index=None, vault_path_opt=None)``, ``sign_typed_data(wallet, chain,
  typed_data_json, ...)``, ``sign_transaction(wallet, chain, tx_hex, ...)`` ->
  ``{"signature": <hex, no 0x>, "recovery_id": int}``.
* ``sign_typed_data`` refuses a document without ``EIP712Domain`` in ``types``
  or without ``primaryType``; an integer beyond 128 bits must be hex (even
  length), a negative one beyond 128 bits two's complement. It signs a value
  out of its type's range without a word (``2**256`` as ``uint256``, ``300``
  as ``uint8``), and refuses a ``string`` that is not a string.
* ``sign_transaction`` signs the UNSIGNED transaction bytes and returns only
  the signature (``recovery_id`` 0/1), not the signed transaction.
* ECDSA is deterministic on both sides: the same key signs the same bytes
  that eth-account does.

The four groups:

* ``TestTheDoubleIsTheLibrary``: ``tests/ows_double.FakeOws`` has the
  library's signatures (``inspect.signature``) and the shape of its results.
  Needs ``ows``; skipped with the reason when it is not installed.
* ``TestTheAdapterCallsWhatExists``: every ``self._ows.<fn>(...)`` in the
  adapter's source binds to the double's signature. Runs without ``ows``, so
  an invented keyword is red even where the library cannot be installed.
* ``TestEndToEnd``: a wallet created by ows in a temporary vault (no funds, no
  network), signed through the adapter, recovered with eth-account.
* ``TestSameBytesAsEnvKeyAdapter``: an ephemeral key imported into the vault
  signs byte for byte what ``EnvKeyAdapter`` signs with it, on every method,
  the lifecycle order of ``escrow_signing`` (its uint256 ``salt`` travels as a
  decimal string) and a transaction that carries ``from`` included.
* ``TestThroughTheEscrowClient``: ``AdvancedEscrowClient`` over the adapter,
  web3's own ``build_transaction`` (which keeps ``from``) and an in-process
  chain: the raw transaction sent is the raw-key path's.
* ``TestWhatTheAdapterSends``: through the double, without ows: what is sent,
  and what is refused before anything is signed (a value out of its type's
  range, a ``from`` that is not the wallet, unsigned bytes that do not hash as
  eth-account hashes them) or after (a signature over another digest).
"""

from __future__ import annotations

import ast
import importlib.metadata
import importlib.util
import inspect
import json
import textwrap
from pathlib import Path
from typing import Any

import pytest

from tests.ows_double import FUNCTIONS, OWS_VERSION, FakeOws
from uvd_x402_sdk.wallet import EnvKeyAdapter, OWSWalletAdapter

HAS_OWS = importlib.util.find_spec("ows") is not None
HAS_ETH_ACCOUNT = importlib.util.find_spec("eth_account") is not None

requires_ows = pytest.mark.skipif(
    not HAS_OWS,
    reason=(
        "open-wallet-standard is not installed (the `dev` extra has it where its wheels "
        "exist: CPython 3.9-3.13 on manylinux x86_64/aarch64 and macOS): the double is "
        "not compared with the library and the adapter is not run against a real vault"
    ),
)
requires_eth_account = pytest.mark.skipif(
    not HAS_ETH_ACCOUNT, reason="eth-account is not installed (extra `signer`)"
)
requires_web3 = pytest.mark.skipif(
    importlib.util.find_spec("web3") is None, reason="web3 is not installed (extra `escrow`)"
)

RECIPIENT = "0x000000000000000000000000000000000000dEaD"
PASSPHRASE = "not-a-secret-test-passphrase"

TX_1559 = {
    "type": 2,
    "chainId": 5042,
    "nonce": 0,
    "to": RECIPIENT,
    "value": 0,
    "gas": 50000,
    "maxFeePerGas": 10**9,
    "maxPriorityFeePerGas": 1,
    "data": "0xa9059cbb",
}
TX_LEGACY = {
    "chainId": 1187947933,
    "nonce": 1,
    "to": RECIPIENT,
    "value": 0,
    "gas": 50000,
    "gasPrice": 10**8,
    "data": "0x",
}


def _typed(kind: str, value: Any, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    """A one-field document: ``M { <kind> v }``."""
    return {
        "types": {"M": [{"name": "v", "type": kind}]},
        "primaryType": "M",
        "domain": {"name": "x", "chainId": 8453, **(extra or {})},
        "message": {"v": value},
    }


def _lifecycle_payment_info(salt: str, receiver: str) -> dict[str, Any]:
    """A paymentInfo as ``build_lifecycle_auth`` takes it (wire form, salt in hex)."""
    return {
        "operator": "0x271f9fa7f8907aCf178CCFB470076D9129D8F0Eb",
        "receiver": receiver,
        "token": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
        "maxAmount": "1000000",
        "preApprovalExpiry": 1_757_003_600,
        "authorizationExpiry": 1_757_007_200,
        "refundExpiry": 1_759_592_000,
        "minFeeBps": 0,
        "maxFeeBps": 1300,
        "feeReceiver": "0xaE07cEB6b395BC685a776a0b4c489E8d9cE9A6ad",
        "salt": salt,
    }


def _installed_version() -> str:
    try:
        return importlib.metadata.version("open-wallet-standard")
    except importlib.metadata.PackageNotFoundError:
        return "unknown"


def _shape(signature: inspect.Signature) -> list[tuple[str, Any, Any]]:
    """Name, kind and default of each parameter: what a call binds against.

    Annotations are left out: the library's functions are native and carry
    none, and they change nothing about which call raises ``TypeError``.
    """
    return [(p.name, p.kind, p.default) for p in signature.parameters.values()]


# ── the double is the library ─────────────────────────────────────────────


@requires_ows
class TestTheDoubleIsTheLibrary:
    @pytest.mark.parametrize("name", FUNCTIONS)
    def test_same_signature_as_the_library(self, name: str) -> None:
        import ows

        real = inspect.signature(getattr(ows, name))
        double = inspect.signature(getattr(FakeOws(), name))
        assert _shape(double) == _shape(real), (
            f"tests/ows_double.FakeOws.{name}{double} differs from "
            f"ows.{name}{real} (installed open-wallet-standard {_installed_version()}, "
            f"the double copies {OWS_VERSION})"
        )

    def test_the_double_has_nothing_the_library_lacks(self) -> None:
        import ows

        own = {n for n in vars(FakeOws) if not n.startswith("_") and callable(vars(FakeOws)[n])}
        own -= {"signed"}  # the double's reader of its calls, not an ows function
        assert own == set(FUNCTIONS)
        assert [n for n in FUNCTIONS if not callable(getattr(ows, n, None))] == []

    def test_the_double_returns_the_library_shape(self, tmp_path: Path) -> None:
        import ows

        vault = str(tmp_path)
        ows.create_wallet("shape", passphrase=PASSPHRASE, vault_path_opt=vault)
        fake = FakeOws()
        real_wallet = ows.get_wallet("shape", vault_path_opt=vault)
        fake_wallet = fake.get_wallet("shape")
        assert set(fake_wallet) == set(real_wallet)
        assert set(fake_wallet["accounts"][0]) == set(real_wallet["accounts"][0])

        adapter = OWSWalletAdapter(wallet_name="shape", passphrase=PASSPHRASE, vault_path=vault)
        document = json.dumps(
            {
                "types": {
                    "EIP712Domain": [{"name": "name", "type": "string"}],
                    "M": [{"name": "a", "type": "uint256"}],
                },
                "primaryType": "M",
                "domain": {"name": "x"},
                "message": {"a": "1"},
            }
        )
        unsigned = (
            "02" + "c9" + "01" + "80" * 7 + "c0"
        )  # 0x02 || rlp([1, 0, 0, 0, 0, "", 0, "", []])
        real_results = {
            "sign_message": ows.sign_message(
                "shape", "eip155:8453", "m", passphrase=PASSPHRASE, vault_path_opt=vault
            ),
            "sign_typed_data": ows.sign_typed_data(
                "shape", "eip155:8453", document, passphrase=PASSPHRASE, vault_path_opt=vault
            ),
            "sign_transaction": ows.sign_transaction(
                "shape", "eip155:1", unsigned, passphrase=PASSPHRASE, vault_path_opt=vault
            ),
        }
        fake_results = {
            "sign_message": fake.sign_message("shape", "eip155:8453", "m"),
            "sign_typed_data": fake.sign_typed_data("shape", "eip155:8453", document),
            "sign_transaction": fake.sign_transaction("shape", "eip155:1", unsigned),
        }
        for name, real in real_results.items():
            fake_result = fake_results[name]
            assert set(fake_result) == set(real), name
            # hex without 0x, 65 bytes, the recovery byte in the same range
            assert not real["signature"].startswith("0x"), name
            assert (
                len(bytes.fromhex(real["signature"]))
                == len(bytes.fromhex(fake_result["signature"]))
                == 65
            ), name
            assert (real["recovery_id"] >= 27) == (fake_result["recovery_id"] >= 27), name
        assert adapter.get_address() == real_wallet["accounts"][0]["address"]


# ── the adapter calls what exists ─────────────────────────────────────────


def _ows_calls() -> list[tuple[str, ast.Call]]:
    """Every ``self._ows.<function>(...)`` in ``OWSWalletAdapter``'s source."""
    tree = ast.parse(textwrap.dedent(inspect.getsource(OWSWalletAdapter)))
    found = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Attribute)
            and node.func.value.attr == "_ows"
            and isinstance(node.func.value.value, ast.Name)
            and node.func.value.value.id == "self"
        ):
            found.append((node.func.attr, node))
    return found


class TestTheAdapterCallsWhatExists:
    def test_the_adapter_calls_the_library(self) -> None:
        assert sorted({name for name, _ in _ows_calls()}) == sorted(FUNCTIONS)

    @pytest.mark.parametrize("name,call", _ows_calls(), ids=[n for n, _ in _ows_calls()])
    def test_each_call_binds_to_the_signature(self, name: str, call: ast.Call) -> None:
        assert all(k.arg is not None for k in call.keywords), "**kwargs cannot be checked"
        assert not any(isinstance(a, ast.Starred) for a in call.args), "*args cannot be checked"
        signature = inspect.signature(getattr(FakeOws(), name))
        # TypeError here is the TypeError the real library raises.
        signature.bind(*call.args, **{k.arg: k.value for k in call.keywords if k.arg})


# ── end to end, a real vault ──────────────────────────────────────────────


def _evm_address(wallet: dict[str, Any]) -> str:
    return next(a["address"] for a in wallet["accounts"] if a["chain_id"].startswith("eip155:"))


@pytest.fixture
def vault(tmp_path: Path) -> str:
    return str(tmp_path / "vault")


@requires_ows
@requires_eth_account
class TestEndToEnd:
    """A wallet ows creates, signed through the adapter, recovered with eth-account."""

    @pytest.fixture
    def created(self, vault: str) -> tuple[OWSWalletAdapter, str]:
        import ows

        wallet = ows.create_wallet("e2e", passphrase=PASSPHRASE, vault_path_opt=vault)
        adapter = OWSWalletAdapter(wallet_name="e2e", passphrase=PASSPHRASE, vault_path=vault)
        return adapter, _evm_address(wallet)

    def test_the_address_is_the_wallet_s(self, created: tuple[OWSWalletAdapter, str]) -> None:
        adapter, address = created
        assert adapter.get_address() == address

    def test_the_wallet_is_found_by_id_too(self, vault: str) -> None:
        import ows

        wallet = ows.create_wallet("by-id", passphrase=PASSPHRASE, vault_path_opt=vault)
        adapter = OWSWalletAdapter(
            wallet_name=wallet["id"], passphrase=PASSPHRASE, vault_path=vault
        )
        assert adapter.get_address() == _evm_address(wallet)

    def test_a_message_recovers_to_the_wallet(self, created: tuple[OWSWalletAdapter, str]) -> None:
        from eth_account import Account
        from eth_account.messages import encode_defunct

        adapter, address = created
        signature = adapter.sign_message("x402 over an OWS vault")
        assert signature.startswith("0x") and len(signature) == 132
        recovered = Account.recover_message(
            encode_defunct(text="x402 over an OWS vault"), signature=signature
        )
        assert recovered == address

    def test_typed_data_recovers_to_the_wallet(self, created: tuple[OWSWalletAdapter, str]) -> None:
        from eth_account import Account
        from eth_account.messages import encode_typed_data

        adapter, address = created
        # The shapes this SDK's producers hand a wallet: no EIP712Domain, bytes
        # and ints in the message, a uint256 above 2**128.
        typed_data = {
            "types": {
                "Order": [
                    {"name": "payer", "type": "address"},
                    {"name": "amount", "type": "uint256"},
                    {"name": "salt", "type": "uint256"},
                    {"name": "nonce", "type": "bytes32"},
                    {"name": "final", "type": "bool"},
                ],
            },
            "primaryType": "Order",
            "domain": {
                "name": "USD Coin",
                "version": "2",
                "chainId": 8453,
                "verifyingContract": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
            },
            "message": {
                "payer": address,
                "amount": 250000,
                "salt": 2**255 + 12345,
                "nonce": b"\xab" * 32,
                "final": True,
            },
        }
        signed = adapter.sign_typed_data(typed_data)
        signable = encode_typed_data(
            domain_data=typed_data["domain"],
            message_types=typed_data["types"],
            message_data=typed_data["message"],
        )
        assert Account.recover_message(signable, signature=signed["signature"]) == address
        assert (
            signed["signature"] == "0x" + signed["r"][2:] + signed["s"][2:] + f"{signed['v']:02x}"
        )
        assert signed["v"] in (27, 28)

    def test_eip3009_recovers_to_the_wallet(self, created: tuple[OWSWalletAdapter, str]) -> None:
        from eth_account import Account
        from eth_account.messages import encode_typed_data

        from uvd_x402_sdk.networks.base import get_token_config

        adapter, address = created
        auth = adapter.sign_eip3009(
            {"to": RECIPIENT, "amount_usdc": "0.25", "network": "arc", "valid_before": 1900000000}
        )
        token = get_token_config("arc", "usdc")
        assert token is not None
        signable = encode_typed_data(
            domain_data={
                "name": token.name,
                "version": token.version,
                "chainId": 5042,
                "verifyingContract": token.address,
            },
            message_types={
                "ReceiveWithAuthorization": [
                    {"name": "from", "type": "address"},
                    {"name": "to", "type": "address"},
                    {"name": "value", "type": "uint256"},
                    {"name": "validAfter", "type": "uint256"},
                    {"name": "validBefore", "type": "uint256"},
                    {"name": "nonce", "type": "bytes32"},
                ]
            },
            message_data={
                "from": address,
                "to": RECIPIENT,
                "value": 250000,
                "validAfter": 0,
                "validBefore": 1900000000,
                "nonce": bytes.fromhex(auth["nonce"][2:]),
            },
        )
        assert auth["from_address"] == address
        assert auth["value"] == "250000"
        assert Account.recover_message(signable, signature=auth["signature"]) == address

    @pytest.mark.parametrize(
        "tx",
        [
            {
                "type": 2,
                "chainId": 8453,
                "nonce": 3,
                "to": RECIPIENT,
                "value": 1,
                "gas": 21000,
                "maxFeePerGas": 2 * 10**9,
                "maxPriorityFeePerGas": 10**9,
                "data": b"",
            },
            # SKALE Base: legacy transactions only, a chain id above 2**30.
            {
                "chainId": 1187947933,
                "nonce": 3,
                "to": RECIPIENT,
                "value": 0,
                "gas": 100000,
                "gasPrice": 10**8,
                "data": "0x1234",
            },
        ],
        ids=["eip1559-base", "legacy-skale-base"],
    )
    def test_a_transaction_recovers_to_the_wallet(
        self, created: tuple[OWSWalletAdapter, str], tx: dict[str, Any]
    ) -> None:
        from eth_account import Account

        adapter, address = created
        raw = adapter.sign_transaction(tx)
        assert Account.recover_transaction(raw) == address

    def test_the_wrong_passphrase_signs_nothing(
        self, created: tuple[OWSWalletAdapter, str], vault: str
    ) -> None:
        wrong = OWSWalletAdapter(wallet_name="e2e", passphrase="wrong", vault_path=vault)
        with pytest.raises(RuntimeError, match="decryption failed"):
            wrong.sign_message("m")

    def test_a_network_that_is_not_evm_is_refused(self, vault: str) -> None:
        with pytest.raises(ValueError, match="EVM"):
            OWSWalletAdapter(wallet_name="e2e", network="solana", vault_path=vault)

    def test_a_domain_type_in_another_order_is_not_returned(
        self, created: tuple[OWSWalletAdapter, str]
    ) -> None:
        """ows signs the domain in the order the document declares; eth-account
        (EnvKeyAdapter) always in EIP-712's. Two digests: nothing is returned."""
        adapter, _ = created
        typed = _typed("uint256", 1)
        typed["types"]["EIP712Domain"] = [
            {"name": "chainId", "type": "uint256"},
            {"name": "name", "type": "string"},
        ]
        with pytest.raises(ValueError, match="another digest"):
            adapter.sign_typed_data(typed)

    def test_a_domain_eth_account_cannot_encode_is_not_returned(
        self, created: tuple[OWSWalletAdapter, str]
    ) -> None:
        """ows signs a domain field EIP-712 does not define; eth-account refuses it."""
        adapter, _ = created
        typed = _typed("uint256", 1, {"extra": "field"})
        typed["types"]["EIP712Domain"] = [
            {"name": "name", "type": "string"},
            {"name": "chainId", "type": "uint256"},
            {"name": "extra", "type": "string"},
        ]
        with pytest.raises(ValueError, match="cannot encode"):
            adapter.sign_typed_data(typed)

    @pytest.mark.parametrize(
        "kind,value",
        [("string", 5), ("bool", 1), ("address", 0xDEAD)],
        ids=["string", "bool", "address"],
    )
    def test_a_value_not_of_its_type_is_refused(
        self, created: tuple[OWSWalletAdapter, str], kind: str, value: Any
    ) -> None:
        """eth-account signs the int 5 as the string "\\x05"; ows refuses it."""
        adapter, _ = created
        with pytest.raises(ValueError, match=kind):
            adapter.sign_typed_data(_typed(kind, value))

    def test_a_vault_path_that_does_not_exist_is_created_by_ows(self, tmp_path: Path) -> None:
        missing = tmp_path / "not" / "yet"
        adapter = OWSWalletAdapter(wallet_name="nobody", vault_path=str(missing))
        with pytest.raises(RuntimeError, match="wallet not found"):
            adapter.get_address()
        assert (missing / "wallets").is_dir()


@requires_ows
@requires_eth_account
class TestSameBytesAsEnvKeyAdapter:
    """An ephemeral key in both adapters: every method signs the same bytes."""

    @pytest.fixture
    def pair(self, vault: str) -> tuple[OWSWalletAdapter, EnvKeyAdapter]:
        import ows
        from eth_account import Account

        key = Account.create().key.hex().removeprefix("0x")
        ows.import_wallet_private_key("k", key, passphrase=PASSPHRASE, vault_path_opt=vault)
        return (
            OWSWalletAdapter(wallet_name="k", passphrase=PASSPHRASE, vault_path=vault),
            EnvKeyAdapter(private_key=key),
        )

    def test_message(self, pair: tuple[OWSWalletAdapter, EnvKeyAdapter]) -> None:
        ows_adapter, env = pair
        assert ows_adapter.get_address() == env.get_address()
        assert ows_adapter.sign_message("hola") == env.sign_message("hola")

    @pytest.mark.parametrize("network", ["base", "arc", "arc-testnet", "skale-base", "celo"])
    def test_eip3009(self, pair: tuple[OWSWalletAdapter, EnvKeyAdapter], network: str) -> None:
        ows_adapter, env = pair
        params = {
            "to": RECIPIENT,
            "amount_usdc": "1.10",
            "network": network,
            "nonce": "0x" + "cd" * 32,
            "valid_before": 1900000000,
        }
        assert ows_adapter.sign_eip3009(params) == env.sign_eip3009(params)  # type: ignore[arg-type]

    @pytest.mark.parametrize(
        "tx",
        [
            {
                "type": 2,
                "chainId": 5042,
                "nonce": 0,
                "to": RECIPIENT,
                "value": 0,
                "gas": 50000,
                "maxFeePerGas": 10**9,
                "maxPriorityFeePerGas": 1,
                "data": "0xa9059cbb",
            },
            {
                "type": 1,
                "chainId": 1,
                "nonce": 7,
                "to": RECIPIENT,
                "value": 0,
                "gas": 50000,
                "gasPrice": 10**9,
                "data": "0x",
                "accessList": [{"address": RECIPIENT, "storageKeys": ["0x" + "00" * 31 + "01"]}],
            },
            {
                "chainId": 1187947933,
                "nonce": 1,
                "to": RECIPIENT,
                "value": 0,
                "gas": 50000,
                "gasPrice": 10**8,
                "data": "0x",
            },
            {
                "nonce": 0,
                "to": RECIPIENT,
                "value": 0,
                "gas": 21000,
                "gasPrice": 10**9,
                "data": "0x",
            },
        ],
        ids=["eip1559-arc", "eip2930", "legacy-eip155-skale", "legacy-no-chain-id"],
    )
    def test_transaction(
        self, pair: tuple[OWSWalletAdapter, EnvKeyAdapter], tx: dict[str, Any]
    ) -> None:
        ows_adapter, env = pair
        assert ows_adapter.sign_transaction(dict(tx)) == env.sign_transaction(dict(tx))

    @pytest.mark.parametrize("tx", [TX_1559, TX_LEGACY], ids=["eip1559", "legacy"])
    def test_transaction_with_from(
        self, pair: tuple[OWSWalletAdapter, EnvKeyAdapter], tx: dict[str, Any]
    ) -> None:
        """web3's build_transaction keeps ``from``; eth-account takes it out."""
        ows_adapter, env = pair
        without = env.sign_transaction(dict(tx))
        with_from = {**tx, "from": env.get_address()}
        assert ows_adapter.sign_transaction(dict(with_from)) == env.sign_transaction(with_from)
        assert ows_adapter.sign_transaction(dict(with_from)) == without
        # Compared without case (eth-account compares the checksummed form exactly).
        lower = {**tx, "from": env.get_address().lower()}
        assert ows_adapter.sign_transaction(lower) == without

    @pytest.mark.parametrize(
        "salt",
        ["0x" + "ab" * 32, "0x" + "ff" * 32, "0x" + "00" * 15 + "01" + "00" * 16, "random"],
        ids=["salt-ab", "salt-max", "salt-2**128", "salt-random"],
    )
    def test_lifecycle_order(self, pair: tuple[OWSWalletAdapter, EnvKeyAdapter], salt: str) -> None:
        """``build_lifecycle_typed_data`` sends the uint256 ``salt`` as a DECIMAL
        STRING. Normalised by value, a string went through as it came and ows
        refused every salt above 2**128 (all but one in 2**128 of random ones)."""
        import secrets

        from uvd_x402_sdk.escrow_signing import build_lifecycle_auth

        ows_adapter, env = pair
        if salt == "random":
            salt = "0x" + secrets.token_hex(32)
        signed = [
            build_lifecycle_auth(
                action="release",
                payment_info=_lifecycle_payment_info(salt, RECIPIENT),
                payer=env.get_address(),
                amount=1_000_000,
                chain_id=8453,
                wallet=adapter,
                deadline=1_757_000_600,
                nonce="0x" + "cd" * 32,
                now=1_757_000_000,
            )
            for adapter in (ows_adapter, env)
        ]
        assert signed[0] == signed[1]

    @pytest.mark.parametrize(
        "kind,value",
        [
            ("uint256", 2**128 - 1),
            ("uint256", 2**128),
            ("uint256", str(2**128)),
            ("uint256", "0x01" + "00" * 16),
            ("uint256", 2**256 - 1),
            ("int256", 2**255 - 1),
            ("int256", -(2**127)),
            ("int256", -(2**127) - 1),
            ("int256", -(2**255)),
            ("uint120", 2**120 - 1),
        ],
    )
    def test_integers_at_the_edges(
        self, pair: tuple[OWSWalletAdapter, EnvKeyAdapter], kind: str, value: Any
    ) -> None:
        ows_adapter, env = pair
        assert ows_adapter.sign_typed_data(_typed(kind, value)) == env.sign_typed_data(
            _typed(kind, value)
        )


def _in_process_chain(chain_id: int) -> Any:
    """A web3 provider answering the few RPCs ``_send_tx`` makes; no network.

    Built in a function because web3 is an optional extra."""
    from eth_utils import keccak
    from web3.providers.base import BaseProvider

    class Chain(BaseProvider):
        def __init__(self) -> None:
            super().__init__()
            self.sent: list[bytes] = []
            self.methods: list[str] = []

        def make_request(self, method: Any, params: Any) -> Any:
            self.methods.append(method)
            if method == "eth_chainId":
                result: Any = hex(chain_id)
            elif method == "eth_gasPrice":
                result = hex(10**9)
            elif method == "eth_getTransactionCount":
                result = hex(7)
            elif method == "eth_sendRawTransaction":
                raw = bytes.fromhex(params[0][2:])
                self.sent.append(raw)
                result = "0x" + keccak(raw).hex()
            elif method == "eth_getTransactionReceipt":
                result = {
                    "transactionHash": params[0],
                    "transactionIndex": "0x0",
                    "blockHash": "0x" + "11" * 32,
                    "blockNumber": "0x1",
                    "from": "0x" + "00" * 20,
                    "to": "0x" + "00" * 20,
                    "cumulativeGasUsed": "0x5208",
                    "gasUsed": "0x5208",
                    "contractAddress": None,
                    "logs": [],
                    "logsBloom": "0x" + "00" * 256,
                    "status": "0x1",
                    "effectiveGasPrice": "0x1",
                    "type": "0x2",
                }
            else:
                raise AssertionError(f"unexpected RPC {method}")
            return {"jsonrpc": "2.0", "id": len(self.methods), "result": result}

    return Chain()


@requires_ows
@requires_eth_account
@requires_web3
class TestThroughTheEscrowClient:
    def test_release_sends_what_the_raw_key_path_sends(self, vault: str) -> None:
        """``_send_tx`` hands the adapter web3's ``build_transaction`` output,
        ``from`` included. Before the adapter took ``from`` out, eth-account
        refused it ("unrecognized fields" / "Unknown kwargs") and ``release``
        answered ``success=False`` with nothing sent."""
        import ows
        from eth_account import Account
        from web3 import Web3

        import uvd_x402_sdk.advanced_escrow as ae

        key = Account.create().key.hex().removeprefix("0x")
        ows.import_wallet_private_key("k", key, vault_path_opt=vault)
        payment_info = ae.PaymentInfo(
            operator="0x0258472A1410Ac3Ad720f1BC83f22B3c0af1Fd9D",
            receiver=RECIPIENT,
            token="0x3600000000000000000000000000000000000000",
            max_amount=1_000_000,
            pre_approval_expiry=1_900_000_000,
            authorization_expiry=1_900_000_000,
            refund_expiry=1_900_000_000,
            salt="0x" + "ab" * 32,
        )
        sent = {}
        for path, signer in (
            ("ows", {"wallet": OWSWalletAdapter(wallet_name="k", network="arc", vault_path=vault)}),
            ("raw key", {"private_key": "0x" + key}),
        ):
            client = ae.AdvancedEscrowClient(chain_id=5042, **signer)  # type: ignore[arg-type]
            chain = _in_process_chain(5042)
            client.w3 = Web3(chain)
            client.operator_contract = client.w3.eth.contract(
                address=client.operator_contract.address, abi=client.operator_contract.abi
            )
            result = client.release(payment_info, 500_000)
            assert result.success, result.error
            sent[path] = chain.sent
        assert len(sent["ows"]) == 1
        assert sent["ows"] == sent["raw key"]
        assert Account.recover_transaction(sent["ows"][0]) == Account.from_key(key).address


# ── the adapter's side of the wire, through the double ────────────────────


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeOws:
    import sys

    double = FakeOws()
    monkeypatch.setitem(sys.modules, "ows", double)
    return double


class TestWhatTheAdapterSends:
    """Runs without ows: the arguments, named as the library names them."""

    def test_the_chain_is_the_adapter_network(self, fake: FakeOws) -> None:
        OWSWalletAdapter(wallet_name="w", passphrase="p", network="arc").sign_message("m")
        [sent] = fake.signed("sign_message")
        assert sent["wallet"] == "w"
        assert sent["chain"] == "eip155:5042"
        assert sent["passphrase"] == "p"

    def test_typed_data_is_signed_on_its_own_chain(self, fake: FakeOws) -> None:
        adapter = OWSWalletAdapter(wallet_name="w", network="base")
        adapter.sign_typed_data(
            {
                "types": {"M": [{"name": "a", "type": "uint256"}]},
                "domain": {"name": "x", "chainId": 1187947933},
                "message": {"a": 1},
            }
        )
        [sent] = fake.signed("sign_typed_data")
        assert sent["chain"] == "eip155:1187947933"
        document = json.loads(sent["typed_data_json"])
        assert document["primaryType"] == "M"
        assert document["types"]["EIP712Domain"] == [
            {"name": "name", "type": "string"},
            {"name": "chainId", "type": "uint256"},
        ]
        assert document["message"] == {"a": "1"}

    def test_the_passphrase_falls_back_to_the_environment(
        self, fake: FakeOws, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OWS_PASSPHRASE", "from-env")
        OWSWalletAdapter(wallet_name="w").sign_message("m")
        assert fake.signed("sign_message")[0]["passphrase"] == "from-env"

    def test_an_explicit_passphrase_wins_over_the_environment(
        self, fake: FakeOws, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OWS_PASSPHRASE", "from-env")
        OWSWalletAdapter(wallet_name="w", passphrase="explicit").sign_message("m")
        assert fake.signed("sign_message")[0]["passphrase"] == "explicit"

    def test_the_address_is_the_evm_account_wherever_it_is(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import sys

        solana = {"chain_id": "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp", "address": "6tEs"}
        evm = {"chain_id": "eip155:1", "address": "0x" + "ab" * 20}
        monkeypatch.setitem(sys.modules, "ows", FakeOws(accounts=[solana, evm]))
        assert OWSWalletAdapter(wallet_name="w").get_address() == evm["address"]
        monkeypatch.setitem(sys.modules, "ows", FakeOws(accounts=[solana]))
        with pytest.raises(ValueError, match="no EVM account"):
            OWSWalletAdapter(wallet_name="w").get_address()

    @pytest.mark.parametrize(
        "kind,value",
        [
            ("uint256", 2**256),
            ("uint256", str(2**256)),
            ("uint256", "0x01" + "00" * 32),
            ("uint256", -1),
            ("uint8", 256),
            ("uint120", 2**120),
            ("int8", 128),
            ("int8", -129),
            ("int256", 2**255),
        ],
    )
    def test_a_value_out_of_range_is_refused_before_signing(
        self, fake: FakeOws, kind: str, value: Any
    ) -> None:
        """ows 1.4.2 signs ``2**256`` as a ``uint256`` (and ``300`` as a ``uint8``)."""
        with pytest.raises(ValueError, match="out of range"):
            OWSWalletAdapter(wallet_name="w").sign_typed_data(_typed(kind, value))
        assert fake.signed("sign_typed_data") == []

    @pytest.mark.parametrize(
        "kind,value",
        [
            ("uint256", "12abc"),
            ("uint256", "0X10"),
            ("uint256", 1.5),
            ("uint256", True),
            ("string", 5),
            ("bool", 1),
            ("address", 0xDEAD),
        ],
    )
    def test_a_value_not_of_its_type_is_refused_before_signing(
        self, fake: FakeOws, kind: str, value: Any
    ) -> None:
        with pytest.raises(ValueError):
            OWSWalletAdapter(wallet_name="w").sign_typed_data(_typed(kind, value))
        assert fake.signed("sign_typed_data") == []

    def test_an_eip3009_amount_beyond_uint256_is_refused_before_signing(
        self, fake: FakeOws
    ) -> None:
        """10**78 USDC is 10**84 base units: it was signed as 10**84 mod 2**256."""
        with pytest.raises(ValueError, match="out of range"):
            OWSWalletAdapter(wallet_name="w").sign_eip3009(
                {"to": RECIPIENT, "amount_usdc": "1" + "0" * 72, "network": "base"}
            )
        assert fake.signed("sign_typed_data") == []

    def test_a_signature_over_another_digest_is_not_returned(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import sys

        class ReadsItOtherwise(FakeOws):
            def sign_typed_data(
                self,
                wallet: Any,
                chain: Any,
                typed_data_json: Any,
                passphrase: Any = None,
                index: Any = None,
                vault_path_opt: Any = None,
            ) -> Any:
                document = json.loads(typed_data_json)
                document["domain"]["name"] += " as ows read it"
                return super().sign_typed_data(wallet, chain, json.dumps(document))

        monkeypatch.setitem(sys.modules, "ows", ReadsItOtherwise())
        with pytest.raises(ValueError, match="another digest"):
            OWSWalletAdapter(wallet_name="w").sign_typed_data(_typed("uint256", 1))

    def test_a_foreign_from_is_refused_and_nothing_is_signed(self, fake: FakeOws) -> None:
        with pytest.raises(TypeError, match="from field must match"):
            OWSWalletAdapter(wallet_name="w").sign_transaction({**TX_1559, "from": RECIPIENT})
        assert fake.signed("sign_transaction") == []

    @pytest.mark.parametrize("tx", [TX_1559, TX_LEGACY], ids=["eip1559", "legacy"])
    def test_the_wallet_s_from_is_taken_out(self, fake: FakeOws, tx: dict[str, Any]) -> None:
        from eth_account import Account

        raw = OWSWalletAdapter(wallet_name="w").sign_transaction({**tx, "from": fake.address})
        assert Account.recover_transaction(raw) == fake.address

    def test_unsigned_bytes_that_do_not_hash_as_eth_account_are_not_signed(
        self, fake: FakeOws, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The adapter serialises the unsigned transaction itself (eth-account
        has no public call for it) and checks it against eth-account's hash."""
        import eth_utils

        monkeypatch.setattr(eth_utils, "keccak", lambda *args, **kwargs: b"\0" * 32)
        with pytest.raises(RuntimeError, match="nothing was signed"):
            OWSWalletAdapter(wallet_name="w").sign_transaction(dict(TX_1559))
        assert fake.signed("sign_transaction") == []

    def test_a_typed_document_without_a_single_root_is_refused_before_signing(
        self, fake: FakeOws
    ) -> None:
        with pytest.raises(ValueError, match="primaryType"):
            OWSWalletAdapter(wallet_name="w").sign_typed_data(
                {
                    "types": {
                        "A": [{"name": "a", "type": "uint8"}],
                        "B": [{"name": "b", "type": "uint8"}],
                    },
                    "domain": {"name": "x"},
                    "message": {"a": 1},
                }
            )
        assert fake.calls == []
