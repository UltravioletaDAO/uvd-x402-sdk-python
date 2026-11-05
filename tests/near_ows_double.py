"""The measured OWS 1.4.2 NEAR call contract, with an ephemeral in-memory key."""

import hashlib

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


class MemoryNearOws:
    def __init__(self):
        self.key = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
        self.calls = []
        self.transform = lambda signature: signature

    def _sign(self, operation, wallet, chain, payload, passphrase, index, vault_path_opt):
        self.calls.append((operation, wallet, chain, payload, passphrase, index, vault_path_opt))
        assert passphrase == "ows_key_" + "ab" * 32
        raw = bytes.fromhex(payload)
        digest = hashlib.sha256(raw).digest() if operation == "sign_transaction" else raw
        return {"signature": self.transform(self.key.sign(digest).hex()), "recovery_id": None}

    def sign_message(
        self,
        wallet,
        chain,
        message,
        passphrase=None,
        encoding=None,
        index=None,
        vault_path_opt=None,
    ):
        assert encoding == "hex"
        return self._sign("sign_message", wallet, chain, message, passphrase, index, vault_path_opt)

    def sign_transaction(
        self, wallet, chain, tx_hex, passphrase=None, index=None, vault_path_opt=None
    ):
        return self._sign(
            "sign_transaction", wallet, chain, tx_hex, passphrase, index, vault_path_opt
        )
