"""The rater's two signatures: the rating, and (once per chain) the delegation.

The key never enters this module. A signer is any object with
``get_address()`` (or an ``address`` attribute) and ``sign_hash(digest)``;
``sign_typed_data(typed_data)`` is used for the rating when present, so a
wallet or a custodian shows the rater named fields instead of a hash.
:class:`AccountSigner` wraps an ``eth_account`` account for the common case;
the CALLER loads the key (from its secret store, never from this module), and
the adapter never prints it.

Whatever produced a signature, it is checked before it leaves:

- 65 bytes, ``v`` in {0, 1, 27, 28} (normalised to 27/28 for the rating);
- **low-s** (``s <= n/2``): a high-s signature is malleable, and the remedy
  (flipping ``s``) also flips ``v``, so it is refused rather than fixed
  (Karma Kadabra ``rating_rail._chequear_low_s``);
- it recovers to the rater **over the digest rebuilt here**, never over a
  digest taken from the network. A delegated signer (a vault, a KMS) that
  signs the wrong bytes is caught here, not on-chain.

The EIP-7702 authorization digest is written out by hand,
``keccak(0x05 || rlp([chain_id, delegate, nonce]))``, from the ALLOWLIST row
of the chain, never from a hash of the prepare: a prepare with the legitimate
delegate and ``digest = keccak(0x05 || rlp([0, EVIL, 0]))`` would otherwise
sign a universal (chain id 0) delegation to EVIL. Byte-identical to
``eth_account``'s ``sign_authorization`` (pinned in the tests).
"""

from __future__ import annotations

from typing import Any, Optional

try:
    from typing import Protocol, runtime_checkable
except ImportError:  # pragma: no cover - Python < 3.8
    from typing_extensions import Protocol, runtime_checkable  # type: ignore[assignment]

from uvd_x402_sdk.erc8004 import RelayAuthorizationParams
from uvd_x402_sdk.reputation._chains import RELAY_DELEGATES
from uvd_x402_sdk.reputation._errors import PreparationRejected, SignatureRejected
from uvd_x402_sdk.reputation._redact import redact
from uvd_x402_sdk.reputation.preparation import (
    MAX_UINT64,
    VerifiedPreparation,
    checksum,
)

SECP256K1_N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
_HALF_N = SECP256K1_N // 2


@runtime_checkable
class RaterSigner(Protocol):
    """What the module needs from a wallet. The key stays inside it."""

    def get_address(self) -> str:
        """The rater's checksummed address."""
        ...

    def sign_hash(self, digest: bytes) -> Any:
        """Sign a 32-byte digest as a prehash (no envelope). 65-byte hex or bytes."""
        ...


class AccountSigner:
    """A :class:`RaterSigner` over an ``eth_account`` ``LocalAccount``.

    Built from an account the caller already holds, or with :meth:`from_key`
    from a key the caller read from its own secret store. This class never reads
    the environment, and its ``repr`` shows the redacted address only.
    """

    def __init__(self, account: Any) -> None:
        if not hasattr(account, "unsafe_sign_hash") or not hasattr(account, "address"):
            raise TypeError("AccountSigner needs an eth_account LocalAccount (eth-account>=0.13)")
        self.__account = account

    @classmethod
    def from_key(cls, private_key: Any) -> AccountSigner:
        from eth_account import Account

        return cls(Account.from_key(private_key))

    def __repr__(self) -> str:
        return f"<AccountSigner {redact(self.__account.address)}>"

    def get_address(self) -> str:
        return str(self.__account.address)

    def sign_hash(self, digest: bytes) -> str:
        signed = self.__account.unsafe_sign_hash(bytes(digest))
        return "0x" + bytes(signed.signature).hex()

    def sign_typed_data(self, typed_data: dict) -> str:
        from eth_account.messages import encode_typed_data

        signable = encode_typed_data(
            domain_data=typed_data["domain"],
            message_types=typed_data["types"],
            message_data=typed_data["message"],
        )
        return "0x" + bytes(self.__account.sign_message(signable).signature).hex()

    def sign_transaction(self, tx: dict) -> str:
        signed = self.__account.sign_transaction(tx)
        raw = getattr(signed, "raw_transaction", None) or signed.rawTransaction
        return "0x" + bytes(raw).hex()


def signer_address(signer: Any) -> str:
    """The signer's address: ``get_address()`` or an ``address`` attribute."""
    getter = getattr(signer, "get_address", None)
    address = getter() if callable(getter) else getattr(signer, "address", None)
    try:
        return checksum(str(address))
    except ValueError:
        raise SignatureRejected("signer_address", "the signer has no valid address") from None


def _signature_bytes(raw: Any) -> bytes:
    """65 bytes out of whatever a signer returns (hex, bytes, a dict with it)."""
    if isinstance(raw, dict):
        raw = raw.get("signature")
    if hasattr(raw, "signature") and not isinstance(raw, (str, bytes, bytearray)):
        raw = raw.signature
    if isinstance(raw, (bytes, bytearray)):
        data = bytes(raw)
    elif isinstance(raw, str):
        text = raw[2:] if raw[:2].lower() == "0x" else raw
        try:
            data = bytes.fromhex(text)
        except ValueError:
            raise SignatureRejected("signature_shape", "the signature is not hex") from None
    else:
        raise SignatureRejected("signature_shape", "the signer returned no signature")
    if len(data) != 65:
        raise SignatureRejected("signature_shape", f"the signature is {len(data)} bytes, not 65")
    return data


def check_signature(raw: Any, digest: bytes, expected: str) -> tuple[int, int, int]:
    """``(r, s, y_parity)`` of a signature that is canonical and recovers to
    ``expected`` over ``digest``; :class:`SignatureRejected` otherwise."""
    data = _signature_bytes(raw)
    r = int.from_bytes(data[:32], "big")
    s = int.from_bytes(data[32:64], "big")
    v = data[64]
    if v in (27, 28):
        y_parity = v - 27
    elif v in (0, 1):
        y_parity = v
    else:
        raise SignatureRejected("signature_v", f"v={v} is not 0, 1, 27 or 28")
    if not 0 < r < SECP256K1_N or not 0 < s < SECP256K1_N:
        raise SignatureRejected("signature_range", "r or s out of range")
    if s > _HALF_N:
        raise SignatureRejected("signature_high_s", "high-s signature: not canonical, refused")
    from eth_keys import keys

    try:
        recovered = (
            keys.Signature(vrs=(y_parity, r, s))
            .recover_public_key_from_msg_hash(bytes(digest))
            .to_checksum_address()
        )
    except Exception:  # noqa: BLE001 - an unrecoverable signature is a bad one
        raise SignatureRejected(
            "signature_unrecoverable", "the signature recovers nobody"
        ) from None
    if recovered.lower() != expected.lower():
        raise SignatureRejected(
            "signature_wrong_signer", f"recovers {recovered}, the rater is {expected}"
        )
    return r, s, y_parity


def _check_verified(prep: Any) -> VerifiedPreparation:
    """Refuse anything that did not come out of ``validate_preparation`` and
    re-check what a hand-built :class:`VerifiedPreparation` could fake."""
    if not isinstance(prep, VerifiedPreparation):
        raise TypeError("sign only what validate_preparation returned")
    allowed = RELAY_DELEGATES.get(prep.chain_id)
    if not prep.chain_id or allowed is None or allowed.lower() != prep.delegate.lower():
        raise PreparationRejected("delegate_mismatch", "the delegate is not allowlisted")
    if prep.recompute_digest() != prep.digest:
        raise PreparationRejected("digest_mismatch", "the verified preparation was altered")
    return prep


def sign_relayed_feedback(prep: VerifiedPreparation, signer: Any) -> str:
    """The rater's signature over the rating, ``0x`` + 65 bytes (``v`` 27/28).

    ``prep`` must come from :func:`~uvd_x402_sdk.reputation.preparation.
    validate_preparation`, and ``signer`` must be the rater it was validated
    for. Uses ``signer.sign_typed_data`` with the typed data REBUILT from the
    validated fields when the signer has it, ``signer.sign_hash`` of the
    rebuilt digest otherwise; either way the result must recover to the rater
    over that digest.
    """
    verified = _check_verified(prep)
    address = signer_address(signer)
    if address.lower() != verified.rater.lower():
        raise PreparationRejected(
            "rater_mismatch", "the signer is not the rater it was prepared for"
        )
    digest = bytes.fromhex(verified.digest[2:])
    sign_typed = getattr(signer, "sign_typed_data", None)
    if callable(sign_typed):
        raw = sign_typed(verified.typed_data)
    else:
        raw = signer.sign_hash(digest)
    r, s, y_parity = check_signature(raw, digest, verified.rater)
    return (
        "0x"
        + r.to_bytes(32, "big").hex()
        + s.to_bytes(32, "big").hex()
        + format(27 + y_parity, "02x")
    )


def _rlp_bytes(data: bytes) -> bytes:
    if len(data) == 1 and data[0] < 0x80:
        return data
    if len(data) >= 56:  # never for [uint, address, uint64]: kept explicit
        raise ValueError("rlp item too long")
    return bytes([0x80 + len(data)]) + data


def _rlp_uint(value: int) -> bytes:
    return _rlp_bytes(b"" if value == 0 else value.to_bytes((value.bit_length() + 7) // 8, "big"))


def authorization_digest(chain_id: int, delegate: str, nonce: int) -> bytes:
    """``keccak(0x05 || rlp([chain_id, delegate, nonce]))``: EIP-7702's preimage."""
    from eth_utils import keccak

    if isinstance(chain_id, bool) or not isinstance(chain_id, int) or chain_id <= 0:
        raise ValueError("authorization chain id must be a positive int (0 = every chain)")
    if isinstance(nonce, bool) or not isinstance(nonce, int) or not 0 <= nonce <= MAX_UINT64:
        raise ValueError("authorization nonce must be a uint64")
    body = (
        _rlp_uint(chain_id) + _rlp_bytes(bytes.fromhex(checksum(delegate)[2:])) + _rlp_uint(nonce)
    )
    if len(body) >= 56:
        raise ValueError("rlp list too long")
    return bytes(keccak(b"\x05" + bytes([0xC0 + len(body)]) + body))


def build_relay_authorization(
    prep: VerifiedPreparation, signer: Any
) -> Optional[RelayAuthorizationParams]:
    """The EIP-7702 authorization of a first rating on a chain; ``None`` when the
    rater is already delegated (the submission then carries none).

    Signed over the ALLOWLIST delegate of the chain with ``signer.sign_hash``
    (a signer without it cannot delegate: :class:`SignatureRejected`), checked
    low-s and recovered against the rater. An EIP-7702 authorization has NO
    deadline: it stays executable for as long as the account's nonce equals the
    one it names. So the nonce must be the chain's (``submit_with_reconcile``
    reads ``eth_getTransactionCount(rater, "pending")`` and the rater's code
    before calling this), and it is signed right before the submit.
    """
    verified = _check_verified(prep)
    if verified.delegated:
        return None
    if verified.account_nonce is None:
        raise PreparationRejected("account_nonce", "a first rating needs an account nonce")
    address = signer_address(signer)
    if address.lower() != verified.rater.lower():
        raise PreparationRejected(
            "rater_mismatch", "the signer is not the rater it was prepared for"
        )
    sign_hash = getattr(signer, "sign_hash", None)
    if not callable(sign_hash):
        raise SignatureRejected("authorization_unavailable", "the signer cannot sign a hash")
    delegate = RELAY_DELEGATES[verified.chain_id]
    digest = authorization_digest(verified.chain_id, delegate, verified.account_nonce)
    r, s, y_parity = check_signature(sign_hash(digest), digest, verified.rater)
    return RelayAuthorizationParams(
        chain_id=verified.chain_id,
        address=delegate,
        nonce=verified.account_nonce,
        y_parity=y_parity,
        r="0x" + r.to_bytes(32, "big").hex(),
        s="0x" + s.to_bytes(32, "big").hex(),
    )
