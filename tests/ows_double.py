"""The ``ows`` module (open-wallet-standard 1.4.2) in process: no vault, no network.

``FakeOws`` has the functions ``OWSWalletAdapter`` calls, each with THE SAME
SIGNATURE as the library's: parameter names, order and defaults. The first
double of this module had one of its own invention (``wallet_name=``,
``domain=``/``types=``/``message=``, a ``sign_eip3009`` the library does not
have), so every test built on it was green while each call the adapter made
raised ``TypeError`` against the real package.
``tests/test_ows_wallet_adapter.py`` compares these signatures with
``inspect.signature`` against the installed library, and the shape of what
they return against a real call, so they cannot drift apart again.

What it returns has the library's shape, measured on 1.4.2: dicts, a signature
as hex WITHOUT ``0x`` with the recovery byte at the end (27/28 for messages and
typed data, 0/1 for transactions), ``recovery_id`` repeating it. The
signatures are REAL, made with a throwaway key (``Account.create()``) the way
ows makes them: EIP-191 over the message, EIP-712 over the JSON document it is
handed (read by eth-account), secp256k1 over ``keccak(tx bytes)``. The adapter
recovers every typed-data signature before returning it, so a double that
signed nothing would be refused.

``accounts`` replaces the wallet's accounts (``get_wallet``); by default one
``eip155:1`` account with the throwaway key's address, as ows lists it.
``real_signatures=False`` returns one fixed 65-byte signature instead, for a
test that counts WHAT is signed thousands of times (``tests/test_signed_amount.py``,
which then also skips the adapter's recovery): pure-Python ECDSA costs
milliseconds per signature.

Every call is kept in ``calls`` as ``(function, arguments)``, with the
arguments bound to the signature, so a test reads them by the library's names.
"""

from __future__ import annotations

import inspect
import json
from typing import Any


class FakeOws:
    def __init__(
        self, accounts: list[dict[str, Any]] | None = None, real_signatures: bool = True
    ) -> None:
        from eth_account import Account

        self.real_signatures = real_signatures

        self._account = Account.create()
        self.address = self._account.address
        self.accounts = (
            accounts
            if accounts is not None
            else [{"chain_id": "eip155:1", "address": self.address, "derivation_path": ""}]
        )
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def _keep(self, name: str, arguments: dict[str, Any]) -> None:
        self.calls.append((name, arguments))

    def signed(self, name: str) -> list[dict[str, Any]]:
        """The arguments of every call to ``name``, in order."""
        return [arguments for called, arguments in self.calls if called == name]

    def get_wallet(self, name_or_id: Any, vault_path_opt: Any = None) -> Any:
        self._keep("get_wallet", {"name_or_id": name_or_id, "vault_path_opt": vault_path_opt})
        return {
            "id": "00000000-0000-0000-0000-000000000000",
            "name": name_or_id,
            "accounts": list(self.accounts),
            "created_at": "2026-09-28T00:00:00Z",
        }

    def sign_message(
        self,
        wallet: Any,
        chain: Any,
        message: Any,
        passphrase: Any = None,
        encoding: Any = None,
        index: Any = None,
        vault_path_opt: Any = None,
    ) -> Any:
        from eth_account.messages import encode_defunct

        self._keep("sign_message", _bound(self.sign_message, locals()))
        return _result(self._account.sign_message(encode_defunct(text=message)).signature)

    def sign_typed_data(
        self,
        wallet: Any,
        chain: Any,
        typed_data_json: Any,
        passphrase: Any = None,
        index: Any = None,
        vault_path_opt: Any = None,
    ) -> Any:
        from eth_account.messages import encode_typed_data

        self._keep("sign_typed_data", _bound(self.sign_typed_data, locals()))
        if not self.real_signatures:
            return _result(FIXED_SIGNATURE)
        signable = encode_typed_data(full_message=json.loads(typed_data_json))
        return _result(self._account.sign_message(signable).signature)

    def sign_transaction(
        self,
        wallet: Any,
        chain: Any,
        tx_hex: Any,
        passphrase: Any = None,
        index: Any = None,
        vault_path_opt: Any = None,
    ) -> Any:
        from eth_keys import keys
        from eth_utils import keccak

        self._keep("sign_transaction", _bound(self.sign_transaction, locals()))
        key = keys.PrivateKey(bytes(self._account.key))
        return _result(key.sign_msg_hash(keccak(bytes.fromhex(tx_hex))).to_bytes())


FIXED_SIGNATURE = bytes.fromhex("22" * 32 + "33" * 32 + "1b")


def _bound(function: Any, scope: dict[str, Any]) -> dict[str, Any]:
    return {name: scope[name] for name in inspect.signature(function).parameters}


def _result(signature: bytes) -> dict[str, Any]:
    raw = bytes(signature)
    return {"signature": raw.hex(), "recovery_id": raw[64]}


# The functions of the double, which the parity test walks.
FUNCTIONS: tuple[str, ...] = ("get_wallet", "sign_message", "sign_typed_data", "sign_transaction")

# The library version the double copies; the parity test says which one is installed.
OWS_VERSION = "1.4.2"
