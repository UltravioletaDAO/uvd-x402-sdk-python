"""What a rater is asked to sign, checked before anything is signed.

``validate_preparation`` is the one gate between the facilitator's
``/feedback/evm/prepare`` answer and the rater's key. It refuses to sign
anything but the v4 ``RelayedGiveFeedback`` of THIS rater, on the chain of the
network the caller asked for, for exactly the rating the caller built, and it
rebuilds the digest here (:func:`relayed_feedback_digest`, EIP-712 written out
by hand, independent of eth-account) instead of trusting the prepare's.

Ported from the three implementations it replaces, keeping the strictest of
each: Karma Hello (``services/reputation/live.py::validate_preparation``:
every message field bound to the request; ``live_worker.py``, right after
the call: the deadline window, 15 s to 3600 s ahead), Karma Kadabra
(``agents_sdk/rating_rail.py::_mensaje_atado`` / ``firmar_rating``: exact
types, exact domain, the registry pinned per chain, the prepare's digest must
be ours) and Execution Market (``shared/relayed-rating.ts::
verifyRelayedRatingPrepare``: a chain id of 0 refused, bytes32 compared by
value, a deadline beyond the window refused, stable rejection codes).

Why each refusal exists, in one line each:

- a delegate outside the allowlist: the EIP-7702 authorization would hand the
  account to it;
- no typed data (a v3 raw digest): signing an opaque hash is a primitive a
  Safe accepts as an owner approval;
- another primary type: the same delegate accepts ``RelayedRevokeFeedback``,
  which destroys one of the rater's own ratings;
- another domain or ``verifyingContract``: a Permit of USDC is typed data too,
  and with the delegate as ``verifyingContract`` one signature would be valid
  for every account delegated to it;
- any message field other than the requested one: the facilitator rebuilds
  the calldata from the fields, so a changed agent, value, tag or URI is a
  rating about someone else, or something else, under the rater's name.
"""

from __future__ import annotations

import re
import time
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any, Optional, Union

from uvd_x402_sdk.erc8004 import PrepareRelayFeedbackResponse
from uvd_x402_sdk.reputation._chains import (
    DOMAIN_NAME,
    DOMAIN_VERSION,
    EIP712_DOMAIN_FIELDS,
    GIVE_FEEDBACK_FIELDS,
    PRIMARY_TYPE,
    RELAY_DELEGATES,
    relay_chain_id,
    reputation_registry,
)
from uvd_x402_sdk.reputation._errors import PreparationRejected

ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
BYTES32_RE = re.compile(r"^0x[0-9a-fA-F]{64}$")
_HEX64_RE = re.compile(r"^(0x)?[0-9a-fA-F]{64}$")
_UINT_RE = re.compile(r"^[0-9]{1,78}$")
_INT_RE = re.compile(r"^-?[0-9]{1,39}$")

MAX_UINT256 = 2**256 - 1
MAX_UINT64 = 2**64 - 1

#: Default window a prepare's deadline must fall in, in seconds from now. The
#: facilitator issues 900 s (``ERC8004_RELAY_DEADLINE_SECS``). Less than the
#: minimum leaves no time to submit; more than the maximum makes the signature
#: a standing permission rather than an answer (Karma Hello's 15 s / 3600 s).
DEFAULT_MIN_VALIDITY = 15
DEFAULT_MAX_VALIDITY = 3600


def checksum(address: str) -> str:
    """EIP-55 form of a 20-byte hex address (``ValueError`` otherwise)."""
    if not isinstance(address, str) or not ADDRESS_RE.fullmatch(address):
        raise ValueError("not a 20-byte hex address")
    from eth_utils import to_checksum_address

    return str(to_checksum_address(address))


def _uint(value: Any) -> Optional[int]:
    """A uint as the facilitator emits it: a JSON integer or a decimal string."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        number = value
    elif isinstance(value, str) and _UINT_RE.fullmatch(value):
        number = int(value)
    else:
        return None
    return number if 0 <= number <= MAX_UINT256 else None


def _int128(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        number = value
    elif isinstance(value, str) and _INT_RE.fullmatch(value):
        number = int(value)
    else:
        return None
    return number if -(2**127) <= number < 2**127 else None


def _bytes32(value: Any) -> Optional[str]:
    """``0x`` + 64 lowercase hex, whatever the case and with or without ``0x``."""
    if not isinstance(value, str) or not _HEX64_RE.fullmatch(value):
        return None
    return "0x" + (value[2:] if value[:2].lower() == "0x" else value).lower()


def _same_address(value: Any, expected: str) -> bool:
    return (
        isinstance(value, str)
        and bool(ADDRESS_RE.fullmatch(value))
        and (value.lower() == expected.lower())
    )


@dataclass(frozen=True)
class RatingParams:
    """The rating the caller decided: what ``prepare`` is asked for and bound to.

    ``value_decimals`` is always 0 and ``value`` is 0..100: every consumer of
    this module rates on that scale, and a foreign ``valueDecimals`` turns a
    "100" into a "1.00" under the same signature. ``feedback_hash`` is the
    keccak of the evidence document (:func:`~uvd_x402_sdk.reputation.evidence.
    evidence_document`) and is required: a rating without auditable evidence is
    not one this module writes.
    """

    agent_id: int
    value: int
    tag1: str
    tag2: str
    endpoint: str
    feedback_uri: str
    feedback_hash: str

    def __post_init__(self) -> None:
        if (
            isinstance(self.agent_id, bool)
            or not isinstance(self.agent_id, int)
            or not 0 <= self.agent_id <= MAX_UINT256
        ):
            raise ValueError("RatingParams.agent_id must be a non-negative int")
        if isinstance(self.value, bool) or not isinstance(self.value, int):
            raise ValueError("RatingParams.value must be an int")
        if not 0 <= self.value <= 100:
            raise ValueError("RatingParams.value must be within 0..100")
        for name in ("tag1", "tag2", "endpoint", "feedback_uri"):
            if not isinstance(getattr(self, name), str):
                raise ValueError(f"RatingParams.{name} must be a str")
        if not isinstance(self.feedback_hash, str) or not BYTES32_RE.fullmatch(self.feedback_hash):
            raise ValueError("RatingParams.feedback_hash must be 0x + 64 hex")
        object.__setattr__(self, "feedback_hash", "0x" + self.feedback_hash[2:].lower())

    def to_wire(self) -> dict[str, Any]:
        """Keyword arguments of ``Erc8004Client.prepare/submit_relayed_feedback``."""
        return {
            "agent_id": self.agent_id,
            "value": self.value,
            "value_decimals": 0,
            "tag1": self.tag1,
            "tag2": self.tag2,
            "endpoint": self.endpoint,
            "feedback_uri": self.feedback_uri,
            "feedback_hash": self.feedback_hash,
        }


def _encode_type(name: str, fields: tuple) -> str:
    return name + "(" + ",".join(f"{kind} {field}" for field, kind in fields) + ")"


def domain_separator(chain_id: int, rater: str) -> bytes:
    """EIP-712 domain separator of the delegate running AS ``rater`` (7702)."""
    from eth_abi import encode
    from eth_utils import keccak

    return bytes(
        keccak(
            encode(
                ["bytes32", "bytes32", "bytes32", "uint256", "address"],
                [
                    keccak(text=_encode_type("EIP712Domain", EIP712_DOMAIN_FIELDS)),
                    keccak(text=DOMAIN_NAME),
                    keccak(text=DOMAIN_VERSION),
                    chain_id,
                    checksum(rater),
                ],
            )
        )
    )


def relayed_feedback_digest(
    chain_id: int,
    rater: str,
    registry: str,
    params: RatingParams,
    deadline: int,
    nonce: str,
) -> bytes:
    """``keccak(0x1901 || domainSeparator || structHash)`` of the rating.

    Written out here rather than delegated to a typed-data library: the struct
    is fixed, and a second encoder that agrees with the contract is the check
    on the first. Pinned against the vector read from the DEPLOYED v4 delegate
    on Base (x402-rs ``the_vector_matches_the_deployed_v4_contract``).
    """
    from eth_abi import encode
    from eth_utils import keccak

    struct = keccak(
        encode(
            [
                "bytes32",
                "address",
                "uint256",
                "int128",
                "uint8",
                "bytes32",
                "bytes32",
                "bytes32",
                "bytes32",
                "bytes32",
                "uint256",
                "bytes32",
            ],
            [
                keccak(text=_encode_type(PRIMARY_TYPE, GIVE_FEEDBACK_FIELDS)),
                checksum(registry),
                params.agent_id,
                params.value,
                0,
                keccak(text=params.tag1),
                keccak(text=params.tag2),
                keccak(text=params.endpoint),
                keccak(text=params.feedback_uri),
                bytes.fromhex(params.feedback_hash[2:]),
                deadline,
                bytes.fromhex(nonce[2:]),
            ],
        )
    )
    return bytes(keccak(b"\x19\x01" + domain_separator(chain_id, rater) + struct))


def relayed_feedback_typed_data(
    chain_id: int,
    rater: str,
    registry: str,
    params: RatingParams,
    deadline: int,
    nonce: str,
) -> dict[str, Any]:
    """The typed data handed to a wallet: rebuilt here, never the prepare's.

    The SDK's :class:`~uvd_x402_sdk.wallet.WalletAdapter` convention: four keys,
    ``types`` WITHOUT ``EIP712Domain`` (eth-account derives the domain type and
    cannot find the primary type when it is listed), uints as decimal strings
    the way the facilitator emits them.
    """
    return {
        "domain": {
            "name": DOMAIN_NAME,
            "version": DOMAIN_VERSION,
            "chainId": chain_id,
            "verifyingContract": checksum(rater),
        },
        "types": {PRIMARY_TYPE: [{"name": n, "type": t} for n, t in GIVE_FEEDBACK_FIELDS]},
        "primaryType": PRIMARY_TYPE,
        "message": {
            "registry": checksum(registry),
            "agentId": str(params.agent_id),
            "value": str(params.value),
            "valueDecimals": 0,
            "tag1": params.tag1,
            "tag2": params.tag2,
            "endpoint": params.endpoint,
            "feedbackURI": params.feedback_uri,
            "feedbackHash": params.feedback_hash,
            "deadline": str(deadline),
            "nonce": nonce,
        },
    }


@dataclass(frozen=True)
class VerifiedPreparation:
    """A prepare that passed :func:`validate_preparation`. The only input the
    signing functions accept."""

    network: str
    chain_id: int
    rater: str
    delegate: str
    registry: str
    params: RatingParams
    deadline: int
    nonce: str
    delegated: bool
    account_nonce: Optional[int]
    digest: str

    @property
    def typed_data(self) -> dict[str, Any]:
        """A fresh copy of the typed data to sign (see
        :func:`relayed_feedback_typed_data`)."""
        return relayed_feedback_typed_data(
            self.chain_id, self.rater, self.registry, self.params, self.deadline, self.nonce
        )

    def recompute_digest(self) -> str:
        """The digest rebuilt from the fields, as ``0x`` hex."""
        return (
            "0x"
            + relayed_feedback_digest(
                self.chain_id, self.rater, self.registry, self.params, self.deadline, self.nonce
            ).hex()
        )


def _reject(code: str, message: str) -> PreparationRejected:
    return PreparationRejected(code, message)


def _fields_match(actual: Any, expected: tuple) -> bool:
    return (
        isinstance(actual, list)
        and len(actual) == len(expected)
        and all(
            isinstance(item, dict)
            and set(item) == {"name", "type"}
            and item["name"] == name
            and item["type"] == kind
            for item, (name, kind) in zip(actual, expected)
        )
    )


def _as_preparation(prep: Any) -> PrepareRelayFeedbackResponse:
    if isinstance(prep, PrepareRelayFeedbackResponse):
        return prep
    if isinstance(prep, Mapping):
        try:
            return PrepareRelayFeedbackResponse.model_validate(dict(prep))
        except Exception:  # noqa: BLE001 - any shape error is one refusal
            raise _reject("prepare_shape", "the prepare is not a prepare response") from None
    raise _reject("prepare_shape", "the prepare is not a prepare response")


def validate_preparation(
    prep: Union[PrepareRelayFeedbackResponse, Mapping[str, Any]],
    params: RatingParams,
    rater: str,
    *,
    network: str,
    now: Optional[float] = None,
    min_validity: int = DEFAULT_MIN_VALIDITY,
    max_validity: int = DEFAULT_MAX_VALIDITY,
) -> VerifiedPreparation:
    """Check ``prep`` against the one thing this module signs; return it verified.

    Args:
        prep: The facilitator's answer, as the SDK parses it or as raw JSON.
        params: The rating the caller asked ``prepare`` for.
        rater: The address that will sign (the signer's address).
        network: The network the caller asked for. Required: a prepare that
            names another chain is a rating somewhere else, possibly about
            someone else, and is refused.
        now: Unix seconds (tests); defaults to the wall clock.
        min_validity / max_validity: the window the deadline must fall in.

    Raises:
        PreparationRejected: with a stable ``code``. Nothing was signed.
    """
    if not isinstance(params, RatingParams):
        raise TypeError("validate_preparation: params must be a RatingParams")
    parsed = _as_preparation(prep)
    if not parsed.success:
        raise _reject("prepare_failed", "the facilitator did not prepare the rating")

    chain_id = relay_chain_id(network)
    registry = reputation_registry(network)
    if chain_id is None or registry is None:
        raise _reject("unknown_network", f"no relayed feedback on {network!r}")
    if not parsed.chain_id:
        raise _reject("no_chain", "a chain id of 0 would authorise every chain")
    if parsed.chain_id != chain_id:
        raise _reject("chain_mismatch", f"prepared for chain {parsed.chain_id}, asked {chain_id}")
    delegate = RELAY_DELEGATES.get(chain_id)
    if delegate is None or not _same_address(parsed.delegate, delegate):
        raise _reject("delegate_mismatch", f"delegate {parsed.delegate} is not allowlisted")
    if not isinstance(rater, str) or not ADDRESS_RE.fullmatch(rater):
        raise _reject("rater_mismatch", "the rater is not an address")

    td = parsed.typed_data
    if not isinstance(td, dict) or not td:
        raise _reject("typed_data_missing", "no EIP-712 typed data: a raw digest is never signed")
    if set(td) != {"domain", "types", "primaryType", "message"}:
        raise _reject("typed_data_shape", "the typed data has unexpected keys")
    if td["primaryType"] != PRIMARY_TYPE:
        raise _reject("primary_type", f"{str(td['primaryType'])[:40]!r} is not {PRIMARY_TYPE}")
    types = td["types"]
    if (
        not isinstance(types, dict)
        or set(types) != {"EIP712Domain", PRIMARY_TYPE}
        or not _fields_match(types["EIP712Domain"], EIP712_DOMAIN_FIELDS)
        or not _fields_match(types[PRIMARY_TYPE], GIVE_FEEDBACK_FIELDS)
    ):
        raise _reject("types_mismatch", "the types are not the v4 RelayedGiveFeedback")
    domain = td["domain"]
    if (
        not isinstance(domain, dict)
        or set(domain) != {"name", "version", "chainId", "verifyingContract"}
        or domain["name"] != DOMAIN_NAME
        or domain["version"] != DOMAIN_VERSION
    ):
        raise _reject("domain_mismatch", f"the domain is not {DOMAIN_NAME} v{DOMAIN_VERSION}")
    if _uint(domain["chainId"]) != chain_id:
        raise _reject("domain_chain_id", "the typed data names another chain")
    if not _same_address(domain["verifyingContract"], rater):
        raise _reject("domain_verifying_contract", "verifyingContract is not the rater's account")

    message = td["message"]
    if not isinstance(message, dict) or set(message) != {name for name, _ in GIVE_FEEDBACK_FIELDS}:
        raise _reject("message_shape", f"the message is not exactly the {PRIMARY_TYPE} fields")
    if not _same_address(message["registry"], registry):
        raise _reject("registry", "the rating is not addressed to the ERC-8004 registry")
    if _int128(message["value"]) != params.value or _uint(message["valueDecimals"]) != 0:
        raise _reject("value", "the value to sign is not the value asked for")
    deadline = _uint(parsed.deadline) if parsed.deadline is not None else None
    nonce = _bytes32(parsed.nonce) if parsed.nonce is not None else None
    if deadline is None or nonce is None:
        raise _reject("message_shape", "the prepare carries no deadline or nonce")
    bound = (
        ("agentId", _uint(message["agentId"]) == params.agent_id),
        ("tag1", message["tag1"] == params.tag1),
        ("tag2", message["tag2"] == params.tag2),
        ("endpoint", message["endpoint"] == params.endpoint),
        ("feedbackURI", message["feedbackURI"] == params.feedback_uri),
        ("feedbackHash", _bytes32(message["feedbackHash"]) == params.feedback_hash),
        ("deadline", _uint(message["deadline"]) == deadline),
        ("nonce", _bytes32(message["nonce"]) == nonce),
    )
    for field, ok in bound:
        if not ok:
            raise _reject("message_binding", f"message.{field} is not the one asked for")

    clock = time.time() if now is None else now
    if not clock + min_validity < deadline <= clock + max_validity:
        raise _reject("deadline", "the deadline is outside the signing window")

    account_nonce: Optional[int] = None
    if not parsed.delegated:
        account_nonce = _uint(parsed.account_nonce) if parsed.account_nonce is not None else None
        if account_nonce is None or account_nonce > MAX_UINT64:
            raise _reject("account_nonce", "a first rating needs a uint64 account nonce")

    verified = VerifiedPreparation(
        network=network,
        chain_id=chain_id,
        rater=checksum(rater),
        delegate=delegate,
        registry=checksum(registry),
        params=params,
        deadline=deadline,
        nonce=nonce,
        delegated=bool(parsed.delegated),
        account_nonce=account_nonce,
        digest="",
    )
    digest = verified.recompute_digest()
    if _bytes32(parsed.digest) != digest:
        raise _reject("digest_mismatch", "the prepare's digest is not the rating rebuilt here")
    return replace(verified, digest=digest)
