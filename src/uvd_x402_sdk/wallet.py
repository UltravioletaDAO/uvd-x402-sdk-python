"""
WalletAdapter -- abstract interface for wallet signing operations.

Provides a unified Protocol for any wallet backend:
- EnvKeyAdapter: uses a raw private key from environment or direct param
- OWSWalletAdapter: signs inside an Open Wallet Standard vault (open-wallet-standard)

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

import json
import os
import re
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
    - OWSWalletAdapter: Open Wallet Standard vault
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
            typed_data: Dict with 'domain', 'types', 'primaryType' and
                'message' keys.
                - domain: EIP-712 domain separator dict
                - types: dict of type definitions (excluding EIP712Domain)
                - primaryType: name of the root struct in ``types``. It does
                  NOT enter the digest (EIP-712 hashes domain + types +
                  message), and ethers/eth-account derive it, but **viem
                  refuses to sign without it** — so a browser-backed adapter
                  needs it and every producer in this SDK sends it.
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
            typed_data: Dict with 'domain', 'types', 'primaryType' and
                'message' keys. ``primaryType`` is read by browser signers
                (viem) and ignored here: ``eth-account`` derives the root
                struct from ``types``, so the digest is the same with or
                without it.

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
            ValueError: If required params are missing or network is invalid,
                or if ``amount_usdc`` has a real digit below one base unit
                (float noise is rounded, as the settle rounds it).
        """
        from uvd_x402_sdk.networks.base import (
            get_network,
            get_token_config,
            normalize_network,
            to_base_units,
        )

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

        # Convert amount to base units the way the settle does: float noise
        # rounds, a real digit below one base unit raises before signing.
        amount_base = to_base_units(
            amount_usdc, token_config.decimals, unit=f"{token_type.upper()} on {normalized}"
        )

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
# OWSWalletAdapter
# =============================================================================

# The EIP-712 domain fields in the order EIP-712 lists them, which is the order
# eth-account derives ``EIP712Domain`` in. The digest depends on it.
_EIP712_DOMAIN_FIELDS = (
    ("name", "string"),
    ("version", "string"),
    ("chainId", "uint256"),
    ("verifyingContract", "address"),
    ("salt", "bytes32"),
)


_INTEGER_TYPE = re.compile(r"(u?)int(\d*)")


def _integer(value: Any, where: str) -> int:
    """An integer given as int, decimal string or 0x-hex string (lowercase
    ``0x``: eth-account refuses ``0X``)."""
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, str):
        try:
            return int(value, 16) if value.startswith("0x") else int(value, 10)
        except ValueError:
            pass
    raise ValueError(f"{where}: {value!r} is not an integer")


def _integer_wire(number: int) -> str:
    """An integer the way ows 1.4.2 reads it (measured).

    A JSON number above 2**64 is refused, a decimal string beyond 128 bits is
    refused ("use hex encoding"), and so is odd-length hex. So: decimal from
    ``-2**127`` to ``2**128 - 1``, even-length hex above, and a 256-bit two's
    complement below (only an ``int`` wider than 128 bits gets there).
    """
    if -(2**127) <= number < 2**128:
        return str(number)
    if number > 0:
        digits = format(number, "x")
        return "0x" + "0" * (len(digits) % 2) + digits
    return "0x" + format(number % 2**256, "064x")


def _ows_value(kind: str, value: Any, types: Dict[str, Any], where: str) -> Any:
    """``value`` of the EIP-712 type ``kind`` in the form ows reads from JSON.

    Normalised by the DECLARED type, not by the Python type of the value: a
    uint256 comes as an int, a decimal string or 0x-hex (the lifecycle order
    sends its ``salt`` as a decimal string above 2**128). Every integer is
    checked against its type's range here, before anything is signed: ows
    1.4.2 signs ``2**256`` as a ``uint256`` and ``300`` as a ``uint8`` without
    a word, where eth-account refuses both. A ``string`` or an ``address``
    that is not a str and a ``bool`` that is not a bool are refused too: ows
    refuses them, and eth-account signs the int ``5`` as the string ``"\x05"``.
    Fields not in the type are left out; they are not part of the digest.
    """
    if kind.endswith("]"):
        inner, _, size = kind[:-1].rpartition("[")
        if not isinstance(value, (list, tuple)):
            raise ValueError(f"{where}: {kind} needs a list, got {value!r}")
        if size and len(value) != int(size):
            raise ValueError(f"{where}: {kind} needs {size} items, got {len(value)}")
        return [_ows_value(inner, item, types, f"{where}[{i}]") for i, item in enumerate(value)]
    if kind in types:
        if not isinstance(value, dict):
            raise ValueError(f"{where}: {kind} needs an object, got {value!r}")
        missing = [field["name"] for field in types[kind] if field["name"] not in value]
        if missing:
            raise ValueError(f"{where}: {kind} lacks {missing}")
        return {
            field["name"]: _ows_value(
                field["type"], value[field["name"]], types, f"{where}.{field['name']}"
            )
            for field in types[kind]
        }
    integer = _INTEGER_TYPE.fullmatch(kind)
    if integer:
        bits = int(integer.group(2) or 256)
        number = _integer(value, where)
        if integer.group(1):
            low, high = 0, 2**bits
        else:
            low, high = -(2 ** (bits - 1)), 2 ** (bits - 1)
        if not low <= number < high:
            raise ValueError(f"{where}: {value!r} is out of range for {kind}")
        return _integer_wire(number)
    if kind in ("string", "address") and not isinstance(value, str):
        raise ValueError(f"{where}: {kind} needs a str, got {value!r}")
    if kind == "bool" and not isinstance(value, bool):
        raise ValueError(f"{where}: bool needs True or False, got {value!r}")
    if isinstance(value, (bytes, bytearray)):
        return "0x" + bytes(value).hex()
    return value


def _primary_type(types: Dict[str, Any]) -> str:
    """The struct no other struct references: the root eth-account picks."""
    structs = [name for name in types if name != "EIP712Domain"]
    referenced = {
        field["type"].split("[", 1)[0]
        for name in structs
        for field in types[name]
        if field["type"].split("[", 1)[0] != name
    }
    roots = [name for name in structs if name not in referenced]
    if len(roots) != 1:
        raise ValueError(f"typed data has no single root struct ({roots}); pass 'primaryType'")
    return roots[0]


def _ows_typed_data_json(typed_data: dict) -> str:
    """The typed data as the JSON document ``ows.sign_typed_data`` takes.

    ows refuses a document without ``EIP712Domain`` in ``types`` or without
    ``primaryType``. eth-account derives both and this SDK's producers leave
    ``EIP712Domain`` out, so both are added here the way eth-account derives
    them. Domain and message are normalised by type (``_ows_value``).
    """
    domain = typed_data["domain"]
    types = dict(typed_data["types"])
    if "EIP712Domain" not in types:
        types["EIP712Domain"] = [
            {"name": name, "type": kind} for name, kind in _EIP712_DOMAIN_FIELDS if name in domain
        ]
    primary = typed_data.get("primaryType") or _primary_type(types)
    if primary not in types:
        raise ValueError(f"primaryType {primary!r} is not in types")
    document = {
        "types": types,
        "primaryType": primary,
        "domain": _ows_value("EIP712Domain", domain, types, "domain"),
        "message": _ows_value(primary, typed_data["message"], types, "message"),
    }
    return json.dumps(document)


def _check_typed_signature(typed_data: dict, signature: bytes, address: str) -> None:
    """Raise unless ``signature`` is ``address``'s over eth-account's digest.

    The digest is recomputed from the caller's typed data as ``EnvKeyAdapter``
    computes it (eth-account derives ``EIP712Domain`` and the root struct
    itself, so the adapter's additions are left out). A document ows reads
    differently from eth-account then fails here instead of being returned.
    Skipped when eth-account is not installed.
    """
    try:
        from eth_account import Account
        from eth_account.messages import encode_typed_data
    except ImportError:
        return
    types = {name: fields for name, fields in typed_data["types"].items() if name != "EIP712Domain"}
    try:
        signable = encode_typed_data(
            domain_data=typed_data["domain"],
            message_types=types,
            message_data=typed_data["message"],
        )
        signer = Account.recover_message(signable, signature=signature)
    except Exception as exc:
        raise ValueError(
            f"eth-account cannot encode this typed data ({exc}); the ows signature is not returned"
        ) from exc
    if signer.lower() != address.lower():
        raise ValueError(
            "ows signed another digest than eth-account computes for this typed data; "
            "the signature is not returned"
        )


def _ows_signature(result: Dict[str, Any]) -> bytes:
    """The 65 bytes ``r || s || v`` of an ows signing result, ``v`` as 27/28.

    ows returns a dict, ``{"signature": <hex without 0x>, "recovery_id": int}``,
    with the recovery byte already at the end of ``signature``: 27/28 for
    messages and typed data, 0/1 for transactions (measured on 1.4.2).
    """
    signature = bytes.fromhex(str(result["signature"]).removeprefix("0x"))
    if len(signature) != 65:
        raise ValueError(f"ows returned a {len(signature)}-byte signature, not 65")
    if signature[64] < 27:
        signature = signature[:64] + bytes([signature[64] + 27])
    return signature


def _eip155_chain(chain_id: Any) -> Optional[str]:
    """``eip155:<id>`` for a chain id given as int, decimal or hex string."""
    if isinstance(chain_id, bool) or chain_id is None:
        return None
    try:
        number = int(chain_id, 0) if isinstance(chain_id, str) else int(chain_id)
    except (TypeError, ValueError):
        return None
    return f"eip155:{number}"


class OWSWalletAdapter:
    """
    WalletAdapter over an Open Wallet Standard (OWS) vault.

    OWS keeps the keys encrypted in a local vault and signs inside it; the key
    never reaches this process. Written against ``open-wallet-standard`` 1.4.2
    (module ``ows``), whose functions take the wallet by name or id and the
    chain explicitly, and return dicts.

    Requires: ``pip install open-wallet-standard`` (and ``eth-account``, the
    ``signer`` extra, for ``sign_transaction`` only).

    Example:
        >>> from uvd_x402_sdk.wallet import OWSWalletAdapter
        >>> wallet = OWSWalletAdapter(wallet_name="my-agent-wallet", network="base")
        >>> print(wallet.get_address())
    """

    def __init__(
        self,
        wallet_name: str,
        passphrase: Optional[str] = None,
        network: str = "base",
        vault_path: Optional[str] = None,
    ) -> None:
        """
        Initialize with an OWS wallet.

        Args:
            wallet_name: Name or id of the wallet in the OWS vault.
            passphrase: Vault passphrase (or OWS API key). Falls back to the
                OWS_PASSPHRASE env var.
            network: EVM network this adapter signs for (SDK name or CAIP-2,
                e.g. ``"base"`` or ``"eip155:8453"``). It is the ``chain`` ows
                is given, which is what an OWS policy decides on. Typed data
                and transactions that carry their own chain id are signed on
                that chain instead.
            vault_path: OWS vault directory. None = the ows default.

        Raises:
            ImportError: If the OWS Python SDK is not installed.
            ValueError: If ``network`` is not a known EVM network.
        """
        try:
            import ows as _ows  # type: ignore[import-not-found,unused-ignore]

            self._ows = _ows
        except ImportError:
            raise ImportError(
                "OWS Python SDK not available. Install: pip install open-wallet-standard\n"
                "Or use EnvKeyAdapter instead."
            )
        from uvd_x402_sdk.networks.base import NetworkType, get_network, normalize_network

        try:
            network_config = get_network(normalize_network(network))
        except ValueError:
            network_config = None
        if network_config is None or network_config.network_type != NetworkType.EVM:
            raise ValueError(f"OWSWalletAdapter signs for an EVM network; got {network!r}")

        self._wallet_name = wallet_name
        self._passphrase = passphrase or os.environ.get("OWS_PASSPHRASE")
        self._chain = f"eip155:{network_config.chain_id}"
        self._vault_path = vault_path

    def get_address(self) -> str:
        """Get the EVM wallet address from OWS vault."""
        wallet = self._ows.get_wallet(name_or_id=self._wallet_name, vault_path_opt=self._vault_path)
        for account in wallet["accounts"]:
            if str(account["chain_id"]).startswith("eip155:"):
                return str(account["address"])
        raise ValueError(f"OWS wallet {self._wallet_name!r} has no EVM account")

    def sign_message(self, message: str) -> str:
        """Sign a message using EIP-191 personal_sign via OWS."""
        result = self._ows.sign_message(
            wallet=self._wallet_name,
            chain=self._chain,
            message=message,
            passphrase=self._passphrase,
            vault_path_opt=self._vault_path,
        )
        return "0x" + _ows_signature(result).hex()

    def sign_typed_data(self, typed_data: dict) -> SignedTypedData:
        """
        Sign EIP-712 typed data via OWS.

        Raises:
            ValueError: A value out of its type's range or not readable as it
                (before anything is signed), or, with eth-account installed, a
                signature that does not recover to this wallet over the digest
                eth-account computes (it is not returned).
        """
        document = _ows_typed_data_json(typed_data)
        return self._sign_typed_data(typed_data, document, self.get_address())

    def _sign_typed_data(self, typed_data: dict, document: str, address: str) -> SignedTypedData:
        chain = _eip155_chain(typed_data["domain"].get("chainId")) or self._chain
        result = self._ows.sign_typed_data(
            wallet=self._wallet_name,
            chain=chain,
            typed_data_json=document,
            passphrase=self._passphrase,
            vault_path_opt=self._vault_path,
        )
        signature = _ows_signature(result)
        _check_typed_signature(typed_data, signature, address)
        return SignedTypedData(
            signature="0x" + signature.hex(),
            v=signature[64],
            r="0x" + signature[:32].hex(),
            s="0x" + signature[32:64].hex(),
        )

    def sign_transaction(self, tx: dict) -> str:
        """
        Sign a raw EVM transaction via OWS.

        ows signs the unsigned transaction's bytes and returns only the
        signature, so the transaction is serialised and re-assembled here with
        eth-account, the way ``Account.sign_transaction`` does it.

        Args:
            tx: Unsigned transaction dict (web3.py format).

        Returns:
            Hex-encoded signed raw transaction (0x-prefixed).

        Raises:
            ImportError: If eth-account is not installed.
            TypeError: ``from`` is not this wallet (as eth-account raises it);
                nothing is signed.
        """
        try:
            import rlp  # type: ignore[import-untyped,unused-ignore]
            from eth_account._utils.signing import (
                encode_transaction,
                serializable_unsigned_transaction_from_dict,
                to_eth_v,
            )
            from eth_utils import keccak
        except ImportError:
            raise ImportError(
                "eth-account is required for OWSWalletAdapter.sign_transaction. "
                "Install it with: pip install uvd-x402-sdk[signer]"
            )

        tx = dict(tx)
        # web3's build_transaction keeps "from"; eth-account takes it out when it
        # is the signer and refuses the transaction otherwise. So does this.
        if "from" in tx:
            sender = tx.pop("from")
            if isinstance(sender, (bytes, bytearray)):
                sender = "0x" + bytes(sender).hex() if len(sender) == 20 else bytes(sender).decode()
            address = self.get_address()
            if str(sender).lower() != address.lower():
                raise TypeError(
                    f"from field must match the wallet's {address}, but it was {sender}"
                )

        unsigned = serializable_unsigned_transaction_from_dict(tx)
        legacy = isinstance(unsigned, rlp.Serializable)
        if legacy:
            preimage = rlp.encode(unsigned)
        else:
            # EIP-2718: type || rlp([fields]). The signed form minus v, r, s.
            shape = encode_transaction(unsigned, vrs=(0, 1, 1))
            preimage = shape[:1] + rlp.encode(rlp.decode(shape[1:])[:-3])
        if keccak(preimage) != unsigned.hash():
            raise RuntimeError(
                "Could not serialise the unsigned transaction as eth-account hashes it; "
                "nothing was signed"
            )

        result = self._ows.sign_transaction(
            wallet=self._wallet_name,
            chain=_eip155_chain(tx.get("chainId")) or self._chain,
            tx_hex=preimage.hex(),
            passphrase=self._passphrase,
            vault_path_opt=self._vault_path,
        )
        signature = _ows_signature(result)
        y_parity = signature[64] - 27
        # Legacy: EIP-155 v when the transaction names its chain (eth-account
        # carries it in ``v`` of the unsigned form), 27/28 when it does not.
        v = to_eth_v(y_parity, getattr(unsigned, "v", None)) if legacy else y_parity
        raw = encode_transaction(
            unsigned,
            vrs=(
                v,
                int.from_bytes(signature[:32], "big"),
                int.from_bytes(signature[32:64], "big"),
            ),
        )
        return "0x" + bytes(raw).hex()

    def sign_eip3009(self, params: EIP3009Params) -> EIP3009Authorization:
        """
        Sign EIP-3009 ReceiveWithAuthorization via OWS.

        Builds the same typed data as ``EnvKeyAdapter.sign_eip3009`` and signs
        it with ``sign_typed_data`` (ows has no EIP-3009 call of its own).
        """
        from uvd_x402_sdk.networks.base import (
            get_network,
            get_token_config,
            normalize_network,
            to_base_units,
        )

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

        # Same conversion as the settle (see EnvKeyAdapter.sign_eip3009).
        amount_base = to_base_units(
            amount_usdc, token_config.decimals, unit=f"{token_type.upper()} on {normalized}"
        )

        now = int(time.time())
        valid_after = params.get("valid_after", 0)
        valid_before = params.get("valid_before") or (now + 3600)
        nonce_hex = params.get("nonce") or ("0x" + secrets.token_hex(32))
        chain_id = params.get("chain_id") or network_config.chain_id
        usdc_contract = params.get("usdc_contract") or token_config.address

        from_address = self.get_address()
        typed_data: Dict[str, Any] = {
            "types": {
                "ReceiveWithAuthorization": [
                    {"name": "from", "type": "address"},
                    {"name": "to", "type": "address"},
                    {"name": "value", "type": "uint256"},
                    {"name": "validAfter", "type": "uint256"},
                    {"name": "validBefore", "type": "uint256"},
                    {"name": "nonce", "type": "bytes32"},
                ],
            },
            "primaryType": "ReceiveWithAuthorization",
            "domain": {
                "name": token_config.name,
                "version": token_config.version,
                "chainId": chain_id,
                "verifyingContract": usdc_contract,
            },
            "message": {
                "from": from_address,
                "to": to,
                "value": amount_base,
                "validAfter": valid_after,
                "validBefore": valid_before,
                "nonce": "0x" + nonce_hex.removeprefix("0x"),
            },
        }
        signed = self._sign_typed_data(typed_data, _ows_typed_data_json(typed_data), from_address)

        return EIP3009Authorization(
            from_address=from_address,
            to=to,
            value=str(amount_base),
            valid_after=str(valid_after),
            valid_before=str(valid_before),
            nonce=nonce_hex,
            v=signed["v"],
            r=signed["r"],
            s=signed["s"],
            signature=signed["signature"],
        )
