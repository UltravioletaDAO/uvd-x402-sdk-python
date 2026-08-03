"""EIP-7702: make a DELEGATED EOA's EIP-3009 signature settle again.

THE PROBLEM. A gasless-wallet provider's first sponsored operation on a chain may
delegate the user's EOA (EIP-7702) to a smart-account implementation — Alchemy's
``SemiModularAccount7702`` (``0x69007702…``) is the one seen in the wild. From then on
the address HAS code, so Circle's USDC (and any ``SignatureChecker`` consumer) verifies
signatures via **ERC-1271 only**. A raw ECDSA authorization, however perfect, is
rejected: ``0x151d90fe``. The account is not broken and the signature is not wrong —
they simply no longer speak the same dialect.

Consequence for anything x402: a delegated payer's ``transferWithAuthorization`` /
``receiveWithAuthorization`` becomes **unsettleable on-chain**, so direct x402 payments
AND marketplace escrow locks both fail. Measured in production 2026-07-31: 14 of 14
delegated agents failed their escrow lock; the one non-delegated agent locked fine. The
failure is silent in the worst way — the sellers looked broken and they were correct.

THE FIX (verified on-chain; the wallet provider needs to change nothing). That account's
``isValidSignature`` DOES accept the EOA's own ECDSA — it just wants it in the account's
envelope. Two steps, both provable against the verified source
(``SemiModularAccountBase._exec1271Validation`` + ``SparseCalldataSegmentLib``):

  1. The account does not check ``hash`` directly; it checks a REPLAY-SAFE hash =
     EIP-712 over the account's OWN domain ``EIP712Domain(uint256 chainId, address
     verifyingContract=account)`` with struct ``ReplaySafeHash(bytes32 hash)``. Sign
     THAT, not the transfer digest.
  2. Wrap the 65-byte signature with the account's fallback-validation locator:
     ``0x00 00000000`` (validation type 0, entity id 0 = FALLBACK_VALIDATION_ID) ``FF``
     (RESERVED_VALIDATION_DATA_INDEX, the final segment) ``00`` (SignatureType.EOA).

Because step 1 is still an ordinary typed-data signature, a REMOTE signer (a delegated
agentic wallet) can produce it: the private key is never needed locally.

DETECTION IS INJECTABLE, ON PURPOSE
-----------------------------------
Knowing whether an address is delegated needs one ``eth_getCode`` — a chain read, and
this SDK does not own an RPC policy. So detection is a **callable you pass in**
(:class:`DelegationResolver`). :func:`rpc_delegation_resolver` ships a default built on
``httpx`` (already a core dependency, so **no new dependency**), and a caller with its
own endpoints, proxy or rotation passes its own.

THE THIRD STATE IS LOAD-BEARING
-------------------------------
A resolver returns ``True`` / ``False`` / **``None`` = could not tell**. ``None`` must
never collapse to "not delegated": that is exactly how this bug survived eight days —
the resolver failed, returned ``None``, and the caller's ``if delegated:`` read it as
falsy and signed raw. **An unreadable answer is not a negative answer.**
"""

from __future__ import annotations

from typing import Any, Callable, Optional, Union

__all__ = [
    "DELEGATE_PREFIX",
    "DelegationResolver",
    "delegate_target",
    "eip712_digest",
    "is_delegated",
    "replay_safe_typed_data",
    "rpc_delegation_resolver",
    "sign_eip3009_for_delegated",
    "wrap_signature",
]

DELEGATE_PREFIX = "ef0100"          # EIP-7702 delegation designator

# Account fallback-validation locator + final-segment marker + EOA signature type.
#   0x00 00000000 : validation type 0, entity id 0 (FALLBACK_VALIDATION_LOOKUP_KEY)
#   0xFF          : RESERVED_VALIDATION_DATA_INDEX  (getFinalSegment)
#   0x00          : SignatureType.EOA
_WRAP_PREFIX = bytes([0x00, 0, 0, 0, 0, 0xFF, 0x00])

_REPLAY_SAFE_TYPES = {"ReplaySafeHash": [{"name": "hash", "type": "bytes32"}]}

#: ``(address, network) -> True | False | None``. ``None`` means UNKNOWN, never "no".
DelegationResolver = Callable[[str, str], Optional[bool]]


def delegate_target(code: Union[bytes, bytearray, str, None]) -> Optional[str]:
    """The address this EOA's EIP-7702 code delegates to, or ``None``.

    ``None`` for a plain EOA, for non-7702 code, and for anything unparseable — the
    caller confirms the implementation before applying the wrap.
    """
    if code is None:
        return None
    h = code.hex() if isinstance(code, (bytes, bytearray)) else str(code)
    h = h[2:] if h.startswith("0x") else h
    h = h.lower()
    if not h.startswith(DELEGATE_PREFIX) or len(h) < 46:
        return None
    return "0x" + h[6:46]


def wrap_signature(inner_signature: str) -> str:
    """Wrap a 65-byte ECDSA signature in the account's fallback-EOA envelope."""
    s = inner_signature[2:] if inner_signature.startswith("0x") else inner_signature
    return "0x" + (_WRAP_PREFIX + bytes.fromhex(s)).hex()


def replay_safe_typed_data(inner_digest: bytes, chain_id: int, account: str) -> tuple:
    """``(domain, types, message)`` whose signature the delegated account accepts.

    The domain is the ACCOUNT's own — ``chainId`` + ``verifyingContract=account``, the
    two fields its typehash declares — and the struct is ``ReplaySafeHash(bytes32 hash)``
    over the inner EIP-3009 digest. Signing THIS, not the digest, is what validates.
    """
    return (
        {"chainId": int(chain_id), "verifyingContract": account},
        _REPLAY_SAFE_TYPES,
        {"hash": inner_digest},
    )


def eip712_digest(domain: dict, types: dict, message: dict) -> bytes:
    """The 32-byte EIP-712 digest of a typed message. No key involved.

    Requires the ``signer`` extra (``eth-account``).
    """
    try:
        from eth_account.messages import _hash_eip191_message, encode_typed_data
    except ImportError as e:  # pragma: no cover - depends on the install extra
        raise ImportError(
            "eip712_digest needs eth-account: pip install 'uvd-x402-sdk[signer]'"
        ) from e
    return _hash_eip191_message(
        encode_typed_data(domain_data=domain, message_types=types, message_data=message)
    )


def sign_eip3009_for_delegated(
    *, wallet: Any, inner_digest: bytes, chain_id: int, account: str
) -> str:
    """The ERC-1271-wrapped signature a delegated account needs.

    ``wallet`` is any :class:`~uvd_x402_sdk.wallet.WalletAdapter`. Since this is an
    ordinary typed-data signature, a REMOTE signer can produce it — the private key
    never has to exist in this process.
    """
    domain, types, message = replay_safe_typed_data(inner_digest, chain_id, account)
    signed = wallet.sign_typed_data(
        {"domain": domain, "types": types, "message": message}
    )
    sig = (
        signed.get("signature")
        if isinstance(signed, dict)
        else getattr(signed, "signature", None)
    )
    if not sig:
        raise ValueError(
            "the wallet returned no signature for the replay-safe wrap — refusing to "
            "build an unsettleable authorization"
        )
    return wrap_signature(sig)


def rpc_delegation_resolver(
    urls: Union[str, "list[str]"], timeout: float = 6.0
) -> DelegationResolver:
    """A default resolver over plain JSON-RPC ``eth_getCode``.

    Uses ``httpx`` (a core dependency — this adds nothing to your install). Rotates
    through ``urls`` and returns ``None`` when **every** endpoint failed: an unreadable
    chain is not a "not delegated" verdict.

    A caller with its own RPC policy (a signed proxy, paid endpoints, per-chain routing)
    should pass its own resolver instead; that is the whole point of the injection.
    """
    endpoints = [urls] if isinstance(urls, str) else list(urls)

    def _resolver(address: str, network: str) -> Optional[bool]:
        addr = (address or "").strip()
        if not addr or not endpoints:
            return None
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "eth_getCode",
            "params": [addr.lower(), "latest"],
        }
        try:
            import httpx
        except ImportError:  # pragma: no cover - httpx is a core dependency
            return None
        for url in endpoints:
            try:
                r = httpx.post(url, json=payload, timeout=timeout)
                r.raise_for_status()
                code = r.json().get("result")
            except Exception:  # noqa: BLE001 - rotate to the next endpoint
                continue
            if isinstance(code, str) and code.startswith("0x"):
                return delegate_target(code) is not None
        return None

    return _resolver


def is_delegated(
    address: str,
    network: str,
    resolver: Optional[DelegationResolver] = None,
) -> Optional[bool]:
    """``True`` / ``False`` / ``None`` (**unknown**) for one address on one network.

    Without a resolver the answer is ``None``, never ``False``: "nobody asked" and "the
    address is a plain EOA" are different facts and only one of them is safe to sign on.
    """
    if resolver is None:
        return None
    try:
        v = resolver(address, network)
    except Exception:  # noqa: BLE001 - a broken resolver is UNKNOWN, not a negative
        return None
    return v if isinstance(v, bool) else None
