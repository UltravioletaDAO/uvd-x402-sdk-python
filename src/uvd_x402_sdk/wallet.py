"""
WalletAdapter -- abstract interface for wallet signing operations.

Provides a unified Protocol for any wallet backend:
- EnvKeyAdapter: uses a raw private key from environment or direct param
- OWSWalletAdapter: stub for Open Wallet Standard (when available on PyPI)

The WalletAdapter protocol can be passed to X402Client or used standalone
for signing EIP-3009 ReceiveWithAuthorization messages.

Example:
    >>> from uvd_x402_sdk.wallet import EnvKeyAdapter
    >>>
    >>> wallet = EnvKeyAdapter()  # reads WALLET_PRIVATE_KEY from env
    >>> print(wallet.get_address())
    '0x...'
    >>>
    >>> auth = wallet.sign_eip3009({
    ...     "to": "0xRecipient...",
    ...     "amount_usdc": 0.10,
    ...     "network": "base",
    ... })
    >>> print(auth["signature"])

Requires: pip install uvd-x402-sdk[wallet]  (eth-account>=0.11.0)
"""

from __future__ import annotations

import os
import secrets
import time
from typing import Any, Dict, Optional, Union

# Use typing_extensions for Protocol on Python 3.9-3.11, stdlib on 3.12+
try:
    from typing import Protocol, TypedDict, runtime_checkable
except ImportError:
    from typing_extensions import Protocol, TypedDict, runtime_checkable


# =============================================================================
# Type Definitions
# =============================================================================


class EIP3009Params(TypedDict, total=False):
    """Parameters for signing an EIP-3009 ReceiveWithAuthorization."""

    to: str
    """Recipient address (required)."""

    amount_usdc: float
    """Amount in USD (e.g., 0.10 for $0.10). Required."""

    network: str
    """Network name (e.g., 'base', 'ethereum'). Required."""

    valid_before: int
    """Unix timestamp before which auth is valid. Optional (default: now + 1 hour)."""

    valid_after: int
    """Unix timestamp after which auth is valid. Optional (default: 0)."""

    usdc_contract: str
    """USDC contract address override. Optional (auto-detected from network)."""

    chain_id: int
    """Chain ID override. Optional (auto-detected from network)."""

    token_type: str
    """Token type (default: 'usdc'). Optional."""

    nonce: str
    """Hex-encoded 32-byte nonce. Optional (random generated if not provided)."""


class EIP3009Authorization(TypedDict):
    """Result of signing an EIP-3009 ReceiveWithAuthorization."""

    from_address: str
    """Signer address (using from_address because 'from' is reserved in Python)."""

    to: str
    """Recipient address."""

    value: str
    """Amount in token base units (e.g., '100000' for $0.10 USDC)."""

    valid_after: str
    """Unix timestamp string."""

    valid_before: str
    """Unix timestamp string."""

    nonce: str
    """Hex-encoded 32-byte nonce."""

    v: int
    """ECDSA recovery parameter."""

    r: str
    """ECDSA r component (hex)."""

    s: str
    """ECDSA s component (hex)."""

    signature: str
    """Full signature (hex, 0x-prefixed)."""


class SignedTypedData(TypedDict):
    """Result of signing EIP-712 typed data."""

    signature: str
    """Full signature (hex, 0x-prefixed)."""

    v: int
    """ECDSA recovery parameter."""

    r: str
    """ECDSA r component (hex)."""

    s: str
    """ECDSA s component (hex)."""


# =============================================================================
# WalletAdapter Protocol
# =============================================================================


@runtime_checkable
class WalletAdapter(Protocol):
    """
    Abstract wallet interface for signing operations.

    Any class that implements these four methods satisfies the protocol.
    Use isinstance(obj, WalletAdapter) to check at runtime.

    Implementations:
    - EnvKeyAdapter: raw private key from env var or direct param
    - OWSWalletAdapter: Open Wallet Standard (future)
    """

    def get_address(self) -> str:
        """
        Get the EVM wallet address.

        Returns:
            Checksummed EVM address (0x-prefixed, 42 chars).
        """
        ...

    def sign_message(self, message: str) -> str:
        """
        Sign a message using EIP-191 personal_sign.

        Args:
            message: UTF-8 message string to sign.

        Returns:
            Hex-encoded signature (0x-prefixed).
        """
        ...

    def sign_typed_data(self, typed_data: dict) -> SignedTypedData:
        """
        Sign EIP-712 typed data.

        Args:
            typed_data: Dict with 'domain', 'types', and 'message' keys.
                - domain: EIP-712 domain separator dict
                - types: dict of type definitions (excluding EIP712Domain)
                - message: the message data dict

        Returns:
            SignedTypedData with signature, v, r, s.
        """
        ...

    def sign_eip3009(self, params: EIP3009Params) -> EIP3009Authorization:
        """
        Sign EIP-3009 ReceiveWithAuthorization for USDC (or other stablecoins).

        Builds the EIP-712 typed data for ReceiveWithAuthorization and signs it.
        USDC contract addresses and EIP-712 domain names are auto-detected from
        the network name.

        Args:
            params: EIP3009Params with to, amount_usdc, network, and optional overrides.

        Returns:
            EIP3009Authorization with from_address, to, value, nonce, signature, etc.

        Raises:
            ValueError: If network is not recognized or params are invalid.
            ImportError: If eth-account is not installed.
        """
        ...

    def sign_transaction(self, tx: dict) -> str:
        """
        Sign a raw EVM transaction and return the signed raw transaction hex.

        The wallet receives an unsigned transaction dict (with fields like
        ``to``, ``value``, ``data``, ``nonce``, ``gas``, ``maxFeePerGas``, etc.)
        and returns the RLP-encoded signed transaction as a hex string,
        ready to be sent via ``eth_sendRawTransaction``.

        Args:
            tx: Unsigned transaction dict (web3.py format).

        Returns:
            Hex-encoded signed raw transaction (0x-prefixed).
        """
        ...


# =============================================================================
# EnvKeyAdapter
# =============================================================================


class EnvKeyAdapter:
    """
    WalletAdapter using a raw private key from environment variable or direct param.

    Reads the private key from (in order):
    1. The ``private_key`` constructor argument
    2. ``WALLET_PRIVATE_KEY`` environment variable
    3. ``PRIVATE_KEY`` environment variable

    Requires: ``pip install uvd-x402-sdk[wallet]`` (installs ``eth-account>=0.11.0``)

    Example:
        >>> wallet = EnvKeyAdapter()  # reads from env
        >>> print(wallet.get_address())
        '0x...'

        >>> wallet = EnvKeyAdapter(private_key="0xabc...")  # direct key
        >>> auth = wallet.sign_eip3009({"to": "0x...", "amount_usdc": 0.10, "network": "base"})
    """

    def __init__(self, private_key: Optional[str] = None) -> None:
        """
        Initialize with a private key.

        Args:
            private_key: Hex-encoded private key (with or without 0x prefix).
                         Falls back to WALLET_PRIVATE_KEY or PRIVATE_KEY env vars.

        Raises:
            ValueError: If no private key is found.
            ImportError: If eth-account is not installed.
        """
        try:
            from eth_account import Account
        except ImportError:
            raise ImportError(
                "eth-account is required for EnvKeyAdapter. "
                "Install it with: pip install uvd-x402-sdk[signer]"
            )

        key = (
            private_key
            or os.environ.get("WALLET_PRIVATE_KEY")
            or os.environ.get("PRIVATE_KEY")
        )
        if not key:
            raise ValueError(
                "No private key provided. Pass private_key argument or set "
                "WALLET_PRIVATE_KEY / PRIVATE_KEY environment variable."
            )

        if not key.startswith("0x"):
            key = "0x" + key

        self._account = Account.from_key(key)

    def get_address(self) -> str:
        """Get the checksummed EVM wallet address."""
        return self._account.address

    def sign_message(self, message: str) -> str:
        """
        Sign a message using EIP-191 personal_sign.

        Args:
            message: UTF-8 message string.

        Returns:
            Hex-encoded signature (0x-prefixed).
        """
        from eth_account.messages import encode_defunct

        msg = encode_defunct(text=message)
        signed = self._account.sign_message(msg)
        sig_hex = signed.signature.hex()
        return sig_hex if sig_hex.startswith("0x") else "0x" + sig_hex

    def sign_typed_data(self, typed_data: dict) -> SignedTypedData:
        """
        Sign EIP-712 typed data.

        Args:
            typed_data: Dict with 'domain', 'types', and 'message' keys.

        Returns:
            SignedTypedData with signature, v, r, s.
        """
        from eth_account.messages import encode_typed_data

        signable = encode_typed_data(
            domain_data=typed_data["domain"],
            message_types=typed_data["types"],
            message_data=typed_data["message"],
        )
        signed = self._account.sign_message(signable)
        sig_hex = signed.signature.hex()
        if not sig_hex.startswith("0x"):
            sig_hex = "0x" + sig_hex

        return SignedTypedData(
            signature=sig_hex,
            v=signed.v,
            r="0x" + signed.r.to_bytes(32, "big").hex(),
            s="0x" + signed.s.to_bytes(32, "big").hex(),
        )

    def sign_transaction(self, tx: dict) -> str:
        """
        Sign a raw EVM transaction.

        Args:
            tx: Unsigned transaction dict (web3.py format).

        Returns:
            Hex-encoded signed raw transaction (0x-prefixed).
        """
        signed = self._account.sign_transaction(tx)
        # eth-account >= 0.12 uses raw_transaction, older versions use rawTransaction
        raw = getattr(signed, "raw_transaction", None) or signed.rawTransaction
        raw_hex = raw.hex()
        return raw_hex if raw_hex.startswith("0x") else "0x" + raw_hex

    def sign_eip3009(self, params: EIP3009Params) -> EIP3009Authorization:
        """
        Sign EIP-3009 ReceiveWithAuthorization for USDC.

        Builds the EIP-712 typed data and signs it. Network-specific USDC
        contract addresses and domain names are auto-detected.

        Args:
            params: EIP3009Params dict.

        Returns:
            EIP3009Authorization.

        Raises:
            ValueError: If required params are missing or network is invalid.
        """
        from uvd_x402_sdk.networks.base import get_network, get_token_config, normalize_network

        # Validate required params
        to = params.get("to")
        amount_usdc = params.get("amount_usdc")
        network_name = params.get("network")

        if not to:
            raise ValueError("'to' address is required in EIP3009Params")
        if amount_usdc is None:
            raise ValueError("'amount_usdc' is required in EIP3009Params")
        if not network_name:
            raise ValueError("'network' is required in EIP3009Params")

        # Resolve network config
        try:
            normalized = normalize_network(network_name)
        except ValueError:
            raise ValueError(f"Unknown network: {network_name}")

        network_config = get_network(normalized)
        if network_config is None:
            raise ValueError(f"Network not found: {normalized}")

        # Get token config
        token_type = params.get("token_type", "usdc")
        token_config = get_token_config(normalized, token_type)  # type: ignore[arg-type]
        if token_config is None:
            raise ValueError(f"Token '{token_type}' not supported on {normalized}")

        # Resolve chain_id and usdc_contract
        chain_id = params.get("chain_id") or network_config.chain_id
        usdc_contract = params.get("usdc_contract") or token_config.address

        # Convert amount to base units
        from decimal import Decimal

        amount_base = int(Decimal(str(amount_usdc)) * (10 ** token_config.decimals))

        # Time parameters
        now = int(time.time())
        valid_after = params.get("valid_after", 0)
        valid_before = params.get("valid_before") or (now + 3600)

        # Nonce
        nonce_hex = params.get("nonce") or ("0x" + secrets.token_hex(32))

        # Convert nonce hex string to bytes for bytes32 encoding
        # (eth_account >= 0.10 / eth_abi >= 5.x requires bytes, not hex str)
        nonce_raw: Union[bytes, str] = nonce_hex
        if isinstance(nonce_raw, str):
            nonce_raw = bytes.fromhex(nonce_raw.removeprefix("0x"))

        # EIP-712 domain
        domain_data = {
            "name": token_config.name,
            "version": token_config.version,
            "chainId": chain_id,
            "verifyingContract": usdc_contract,
        }

        # EIP-3009 ReceiveWithAuthorization types
        types: Dict[str, Any] = {
            "ReceiveWithAuthorization": [
                {"name": "from", "type": "address"},
                {"name": "to", "type": "address"},
                {"name": "value", "type": "uint256"},
                {"name": "validAfter", "type": "uint256"},
                {"name": "validBefore", "type": "uint256"},
                {"name": "nonce", "type": "bytes32"},
            ],
        }

        # Message data
        message = {
            "from": self._account.address,
            "to": to,
            "value": amount_base,
            "validAfter": valid_after,
            "validBefore": valid_before,
            "nonce": nonce_raw,
        }

        # Sign using the proven encode_typed_data + sign_message pattern
        # (same approach as advanced_escrow.py and client.py)
        from eth_account.messages import encode_typed_data

        signable = encode_typed_data(
            domain_data=domain_data,
            message_types=types,
            message_data=message,
        )
        signed = self._account.sign_message(signable)
        sig_hex = signed.signature.hex()
        if not sig_hex.startswith("0x"):
            sig_hex = "0x" + sig_hex

        return EIP3009Authorization(
            from_address=self._account.address,
            to=to,
            value=str(amount_base),
            valid_after=str(valid_after),
            valid_before=str(valid_before),
            nonce=nonce_hex,
            v=signed.v,
            r="0x" + signed.r.to_bytes(32, "big").hex(),
            s="0x" + signed.s.to_bytes(32, "big").hex(),
            signature=sig_hex,
        )


# =============================================================================
# OWSWalletAdapter (Stub)
# =============================================================================


class OWSWalletAdapter:
    """
    WalletAdapter using Open Wallet Standard (OWS).

    OWS provides secure, local, multi-chain wallet management for AI agents.
    Private keys are encrypted locally (AES-256-GCM) and never leave the vault.

    As of April 2026, the OWS Python package is not yet on PyPI.
    Use EnvKeyAdapter as the primary adapter, or the OWS MCP Server (TypeScript)
    for signing operations.

    When the OWS Python SDK becomes available:
        pip install open-wallet-standard

    Example:
        >>> from uvd_x402_sdk.wallet import OWSWalletAdapter
        >>> wallet = OWSWalletAdapter(wallet_name="my-agent-wallet")
        >>> print(wallet.get_address())
    """

    def __init__(
        self,
        wallet_name: str,
        passphrase: Optional[str] = None,
    ) -> None:
        """
        Initialize with an OWS wallet.

        Args:
            wallet_name: Name of the wallet in the OWS vault.
            passphrase: Vault passphrase. Falls back to OWS_PASSPHRASE env var.

        Raises:
            ImportError: If the OWS Python SDK is not installed.
        """
        try:
            import ows as _ows  # type: ignore[import-not-found]

            self._ows = _ows
        except ImportError:
            raise ImportError(
                "OWS Python SDK not available. Install: pip install open-wallet-standard\n"
                "Note: As of April 2026, the package is not yet on PyPI.\n"
                "Use EnvKeyAdapter or the OWS MCP Server (TypeScript) instead."
            )
        self._wallet_name = wallet_name
        self._passphrase = passphrase or os.environ.get("OWS_PASSPHRASE")

    def get_address(self) -> str:
        """Get the EVM wallet address from OWS vault."""
        wallet = self._ows.get_wallet(self._wallet_name, passphrase=self._passphrase)
        return wallet.address

    def sign_message(self, message: str) -> str:
        """Sign a message using EIP-191 personal_sign via OWS."""
        result = self._ows.sign_message(
            wallet_name=self._wallet_name,
            message=message,
            passphrase=self._passphrase,
        )
        return result.signature

    def sign_typed_data(self, typed_data: dict) -> SignedTypedData:
        """Sign EIP-712 typed data via OWS."""
        result = self._ows.sign_typed_data(
            wallet_name=self._wallet_name,
            domain=typed_data["domain"],
            types=typed_data["types"],
            message=typed_data["message"],
            passphrase=self._passphrase,
        )
        return SignedTypedData(
            signature=result.signature,
            v=result.v,
            r=result.r,
            s=result.s,
        )

    def sign_transaction(self, tx: dict) -> str:
        """
        Sign a raw EVM transaction via OWS.

        Delegates transaction signing to the OWS vault. The private key is
        decrypted in memory, used to sign, then immediately wiped.

        Args:
            tx: Unsigned transaction dict (web3.py format).

        Returns:
            Hex-encoded signed raw transaction (0x-prefixed).
        """
        result = self._ows.sign_transaction(
            wallet_name=self._wallet_name,
            transaction=tx,
            passphrase=self._passphrase,
        )
        return result.raw_transaction

    def sign_eip3009(self, params: EIP3009Params) -> EIP3009Authorization:
        """
        Sign EIP-3009 ReceiveWithAuthorization via OWS.

        Uses the OWS MCP server's ows_sign_eip3009 capability.
        """
        from uvd_x402_sdk.networks.base import get_network, get_token_config, normalize_network

        # Validate required params
        to = params.get("to")
        amount_usdc = params.get("amount_usdc")
        network_name = params.get("network")

        if not to:
            raise ValueError("'to' address is required in EIP3009Params")
        if amount_usdc is None:
            raise ValueError("'amount_usdc' is required in EIP3009Params")
        if not network_name:
            raise ValueError("'network' is required in EIP3009Params")

        # Resolve network config for amount conversion
        try:
            normalized = normalize_network(network_name)
        except ValueError:
            raise ValueError(f"Unknown network: {network_name}")

        network_config = get_network(normalized)
        if network_config is None:
            raise ValueError(f"Network not found: {normalized}")

        token_type = params.get("token_type", "usdc")
        token_config = get_token_config(normalized, token_type)  # type: ignore[arg-type]
        if token_config is None:
            raise ValueError(f"Token '{token_type}' not supported on {normalized}")

        from decimal import Decimal

        amount_base = int(Decimal(str(amount_usdc)) * (10 ** token_config.decimals))

        now = int(time.time())
        valid_after = params.get("valid_after", 0)
        valid_before = params.get("valid_before") or (now + 3600)
        nonce_hex = params.get("nonce") or ("0x" + secrets.token_hex(32))
        chain_id = params.get("chain_id") or network_config.chain_id
        usdc_contract = params.get("usdc_contract") or token_config.address

        result = self._ows.sign_eip3009(
            wallet_name=self._wallet_name,
            to=to,
            value=str(amount_base),
            valid_after=str(valid_after),
            valid_before=str(valid_before),
            nonce=nonce_hex,
            chain_id=chain_id,
            token_address=usdc_contract,
            domain_name=token_config.name,
            domain_version=token_config.version,
            passphrase=self._passphrase,
        )

        return EIP3009Authorization(
            from_address=result.from_address,
            to=to,
            value=str(amount_base),
            valid_after=str(valid_after),
            valid_before=str(valid_before),
            nonce=nonce_hex,
            v=result.v,
            r=result.r,
            s=result.s,
            signature=result.signature,
        )
