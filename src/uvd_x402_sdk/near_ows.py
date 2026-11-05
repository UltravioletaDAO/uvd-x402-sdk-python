"""Token-only NEAR signing through Open Wallet Standard (optional ``ows`` extra)."""

from __future__ import annotations

import hashlib
import os
import re


class OwsNearSigner:
    """A NearLocalSigner backed by an already provisioned OWS wallet.

    ``public_key`` is the trusted, raw 32-byte access key selected by the caller.
    No owner passphrase, secret key, environment credential or default vault is
    read here. Provision the wallet and API token outside the agent runtime.
    OWS names its credential argument ``passphrase`` even in API-token mode.
    Every returned signature is verified locally against the selected key.
    """

    __slots__ = ("_wallet", "_network", "_token", "_public_key", "_vault", "_index", "_ows")

    def __init__(
        self,
        wallet: str,
        *,
        network: str,
        token: str,
        public_key: bytes,
        vault_path: str | os.PathLike[str],
        index: int = 0,
    ) -> None:
        # Never normalise credentials: prefix changes could select owner mode.
        if not isinstance(token, str) or re.fullmatch(r"ows_key_[0-9a-f]{64}", token) is None:
            raise ValueError("an OWS API token is required; owner passphrases are forbidden")
        if not isinstance(wallet, str) or not wallet or wallet.strip() != wallet:
            raise ValueError("wallet must be a nonempty OWS name or id")
        if network not in ("near:mainnet", "near:testnet"):
            raise ValueError("unsupported NEAR network")
        if not isinstance(public_key, bytes) or len(public_key) != 32:
            raise ValueError("expected 32-byte Ed25519 public key")
        if type(index) is not int or not 0 <= index < 2**32:
            raise ValueError("index must be a uint32")
        try:
            vault = os.fspath(vault_path)
        except TypeError:
            raise ValueError("an explicit vault path is required") from None
        if not isinstance(vault, str) or not vault or "\x00" in vault:
            raise ValueError("an explicit vault path is required")
        try:
            import ows
        except ImportError:
            raise ImportError("OwsNearSigner requires pip install 'uvd-x402-sdk[ows]'") from None
        self._wallet = wallet
        self._network = network
        self._token = token
        self._public_key = public_key
        self._vault = vault
        self._index = index
        self._ows = ows

    @property
    def public_key(self) -> bytes:
        return self._public_key

    @property
    def network(self) -> str:
        return self._network

    def _signature(self, operation: str, payload: bytes, digest: bytes) -> bytes:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

        # Check again at the boundary: a changed credential must never use owner mode.
        if re.fullmatch(r"ows_key_[0-9a-f]{64}", self._token) is None:
            raise ValueError("an OWS API token is required")
        kwargs = dict(passphrase=self._token, index=self._index, vault_path_opt=self._vault)
        if operation == "sign_message":
            kwargs["encoding"] = "hex"
        try:
            result = getattr(self._ows, operation)(
                self._wallet, self._network, payload.hex(), **kwargs
            )
        except Exception:
            # A backend error can contain its credential or a private vault path.
            raise ValueError("OWS NEAR signing failed") from None
        if (
            not isinstance(result, dict)
            or not isinstance(result.get("signature"), str)
            or re.fullmatch(r"[0-9a-fA-F]{128}", result["signature"]) is None
            or result.get("recovery_id") is not None
        ):
            raise ValueError("invalid OWS Ed25519 signature")
        signature = bytes.fromhex(result["signature"])
        try:
            Ed25519PublicKey.from_public_bytes(self._public_key).verify(signature, digest)
        except InvalidSignature:
            raise ValueError("invalid OWS Ed25519 signature") from None
        return signature

    def sign(self, digest: bytes) -> bytes:
        """Sign a NEP-413/NEP-366 digest through OWS raw hex message signing."""
        if not isinstance(digest, bytes) or len(digest) != 32:
            raise ValueError("expected a 32-byte NEAR digest")
        return self._signature("sign_message", digest, digest)

    def sign_transaction(self, transaction: bytes) -> bytes:
        """Return a signature over SHA-256(Borsh tx), giving OWS the ENTIRE tx."""
        if not isinstance(transaction, bytes) or not transaction:
            raise ValueError("expected nonempty Borsh transaction bytes")
        return self._signature(
            "sign_transaction", transaction, hashlib.sha256(transaction).digest()
        )
