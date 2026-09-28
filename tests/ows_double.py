"""The ``ows`` module (open-wallet-standard 1.4.2) in process: no vault, no key.

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
typed data, 0/1 for transactions), ``recovery_id`` repeating it. The signature
bytes are fixed, not a real signature: a test that needs one to recover signs
through the real library in a temporary vault.

Every call is kept in ``calls`` as ``(function, arguments)``, with the
arguments bound to the signature, so a test reads them by the library's names.
"""

from __future__ import annotations

import inspect
from typing import Any

ADDRESS = "0x1111111111111111111111111111111111111111"
SIGNATURE = "22" * 32 + "33" * 32 + "1b"


class FakeOws:
    def __init__(self, address: str = ADDRESS) -> None:
        self.address = address
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
            "accounts": [
                {"chain_id": "eip155:1", "address": self.address, "derivation_path": ""},
            ],
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
        self._keep("sign_message", _bound(self.sign_message, locals()))
        return {"signature": SIGNATURE, "recovery_id": 27}

    def sign_typed_data(
        self,
        wallet: Any,
        chain: Any,
        typed_data_json: Any,
        passphrase: Any = None,
        index: Any = None,
        vault_path_opt: Any = None,
    ) -> Any:
        self._keep("sign_typed_data", _bound(self.sign_typed_data, locals()))
        return {"signature": SIGNATURE, "recovery_id": 27}

    def sign_transaction(
        self,
        wallet: Any,
        chain: Any,
        tx_hex: Any,
        passphrase: Any = None,
        index: Any = None,
        vault_path_opt: Any = None,
    ) -> Any:
        self._keep("sign_transaction", _bound(self.sign_transaction, locals()))
        return {"signature": SIGNATURE[:-2] + "00", "recovery_id": 0}


def _bound(function: Any, scope: dict[str, Any]) -> dict[str, Any]:
    return {name: scope[name] for name in inspect.signature(function).parameters}


# The functions of the double, which the parity test walks.
FUNCTIONS: tuple[str, ...] = ("get_wallet", "sign_message", "sign_typed_data", "sign_transaction")

# The library version the double copies; the parity test says which one is installed.
OWS_VERSION = "1.4.2"
