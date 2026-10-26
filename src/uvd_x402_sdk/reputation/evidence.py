"""The public evidence of a rating: what the ``feedbackURI`` serves.

The registry stores ``feedbackHash``; the facilitator fetches ``feedbackURI``
and checks that keccak of the served bytes is that hash, so the document must
be served byte for byte as :func:`evidence_document` returns it (``body``).

**What it never carries**: the chat text, the author, the login, the user id.
Karma Hello's ``evidence.py`` publishes the comment and its author on public
IPFS; the Twitch Developer Agreement forbids retaining chat to build public
databases, and a rating cannot be revoked. So what identifies the person
locally enters only as a **commitment**: ``HMAC-SHA256(salt, label | value)``
with a salt per viewer, kept locally. Destroying the salt (``!forgetme``)
leaves the commitments unlinkable to anyone; what stays public is what the
chain already shows (the subject's wallet, the rating, the payment).

Two guards make that structural rather than a convention:

- no key of the document, at any depth, may be one of :data:`PRIVATE_KEYS`
  (``message``, ``author``, ``login``...): a caller cannot slip the text in
  through ``context``;
- every private value long enough to be recognisable
  (:data:`LEAK_CHECK_MIN_LENGTH`) is searched for in the serialised body, and
  its presence anywhere (a tag, the endpoint, a context value) is a refusal.

The document is canonical JSON (sorted keys, no whitespace, UTF-8, no floats)
so the TypeScript twin produces the same bytes from the same inputs; the
shared vectors pin it.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

from uvd_x402_sdk.erc8004 import ProofOfPayment, _wire
from uvd_x402_sdk.reputation._chains import identity_registry, relay_chain_id
from uvd_x402_sdk.reputation._errors import EvidenceRejected
from uvd_x402_sdk.reputation.preparation import checksum

EVIDENCE_SCHEMA = "uvd.reputation.evidence/v1"
WALLET_BINDINGS = frozenset({"self_declared_unverified", "eip191_verified"})
#: Keys that never appear in public evidence (compared lowercased, without
#: ``_`` / ``-``): the content and the identity of the person, in every
#: spelling a caller is likely to use.
PRIVATE_KEYS = frozenset(
    {
        "message",
        "messages",
        "text",
        "body",
        "content",
        "quote",
        "comment",
        "transcript",
        "chat",
        "author",
        "username",
        "login",
        "user",
        "userid",
        "viewerid",
        "viewer",
        "displayname",
        "nick",
        "nickname",
        "email",
    }
)
#: A key is also private when any of its words is one of these (``chatText``,
#: ``author_name``, ``viewer-login``, ``userAgent``: refused, fail closed).
PRIVATE_TOKENS = frozenset(
    {
        "message",
        "messages",
        "msg",
        "text",
        "quote",
        "comment",
        "transcript",
        "chat",
        "author",
        "username",
        "login",
        "user",
        "viewer",
        "nick",
        "nickname",
        "email",
        "handle",
        # The same in Spanish: the teams that use this module write in it.
        "mensaje",
        "mensajes",
        "texto",
        "cita",
        "comentario",
        "comentarios",
        "transcripcion",
        "autor",
        "autora",
        "usuario",
        "usuaria",
        "apodo",
        "correo",
    }
)
#: Private values at least this long are searched for (case-insensitive) in
#: every caller-written public string; shorter ones (down to
#: :data:`LEAK_CHECK_MIN_WORD`) as whole words in ``issuer`` and ``context``
#: (a 4-letter login would match half the tags as a substring).
LEAK_CHECK_MIN_LENGTH = 8
LEAK_CHECK_MIN_WORD = 4
_LABEL_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_CAMEL_RE = re.compile(r"([a-z0-9])([A-Z])")


def is_private_key(key: str) -> bool:
    """Whether ``key`` names something that never goes into public evidence."""
    if key.lower().replace("_", "").replace("-", "") in PRIVATE_KEYS:
        return True
    words = re.split(r"[^a-z0-9]+", _CAMEL_RE.sub(r"\1 \2", key).lower())
    return any(word in PRIVATE_TOKENS for word in words)


def new_salt() -> bytes:
    """32 random bytes: one per viewer, stored locally, destroyed on forget."""
    return secrets.token_bytes(32)


def commitment(salt: bytes, label: str, value: str) -> str:
    """``0x`` + hex of ``HMAC-SHA256(salt, label + "|" + value)``."""
    if not isinstance(salt, (bytes, bytearray)) or len(salt) < 16:
        raise EvidenceRejected("salt", "the salt must be at least 16 bytes")
    if not isinstance(label, str) or not _LABEL_RE.fullmatch(label):
        raise EvidenceRejected("commitment_label", "labels are lower_snake_case")
    if not isinstance(value, str):
        raise EvidenceRejected("commitment_value", "committed values are strings")
    digest = hmac.new(bytes(salt), (label + "|" + value).encode("utf-8"), hashlib.sha256)
    return "0x" + digest.hexdigest()


def canonical_json(value: Any) -> bytes:
    """Sorted keys, no whitespace, UTF-8 (Karma Hello's ``canonical_json``)."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )


def content_hash(body: bytes) -> str:
    """keccak256 of the served bytes, ``0x`` hex: the ``feedbackHash``."""
    from eth_utils import keccak

    return "0x" + bytes(keccak(bytes(body))).hex()


def _check_public(value: Any, path: str) -> None:
    if isinstance(value, bool) or value is None or isinstance(value, (int, str)):
        return
    if isinstance(value, float):
        raise EvidenceRejected("float_in_evidence", f"{path} is a float (not canonical)")
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise EvidenceRejected("evidence_key", f"{path} has a non-string key")
            if is_private_key(key):
                raise EvidenceRejected("private_field", f"{path}.{key} cannot be public evidence")
            _check_public(item, f"{path}.{key}")
        return
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _check_public(item, f"{path}[{index}]")
        return
    raise EvidenceRejected("evidence_type", f"{path} is a {type(value).__name__}")


def _iso(timestamp: int) -> str:
    if isinstance(timestamp, bool) or not isinstance(timestamp, int) or timestamp < 0:
        raise EvidenceRejected("created_at", "created_at is whole unix seconds")
    return datetime.fromtimestamp(timestamp, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class EvidenceDocument:
    """``document`` (the dict), ``body`` (the bytes to serve) and their hash."""

    document: dict
    body: bytes
    content_hash: str


def evidence_document(
    *,
    network: str,
    agent_id: int,
    rater: str,
    subject: str,
    value: int,
    tag1: str,
    tag2: str,
    endpoint: str,
    created_at: int,
    salt: bytes,
    private: Mapping[str, str],
    wallet_binding: str = "self_declared_unverified",
    proof: Optional[ProofOfPayment] = None,
    issuer: Optional[Mapping[str, str]] = None,
    context: Optional[Mapping[str, Any]] = None,
) -> EvidenceDocument:
    """Build the public evidence of one rating.

    Args:
        network: Where the rating is written (``base``).
        agent_id: The subject's ERC-8004 agent id.
        rater: The rater's address (the ``clientAddress`` on-chain).
        subject: The subject's wallet (owner of ``agent_id``).
        value / tag1 / tag2 / endpoint: exactly what the rating carries.
        created_at: Unix seconds of the rated moment.
        salt: The viewer's salt (:func:`new_salt`).
        private: ``label -> value`` of what identifies the moment and the
            person locally (``{"moment_ref": ..., "user_id": ..., "login": ...,
            "message": <the chat text>}``). Only their commitments are public,
            and none of these values may appear anywhere else in the body.
        wallet_binding: ``self_declared_unverified`` (a directory entry) or
            ``eip191_verified`` (the person signed a claim).
        proof: The anchor payment's proof, published as is.
        issuer: Who rates (``{"name": ..., "product": ...}``), public strings.
        context: Extra PUBLIC fields; checked like the rest.
    """
    chain_id = relay_chain_id(network)
    identity = identity_registry(network)
    if chain_id is None or identity is None:
        raise EvidenceRejected("unknown_network", f"no relayed feedback on {network!r}")
    if wallet_binding not in WALLET_BINDINGS:
        raise EvidenceRejected("wallet_binding", f"{wallet_binding!r} is not a binding")
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 100:
        raise EvidenceRejected("value", "value is an int within 0..100")
    if isinstance(agent_id, bool) or not isinstance(agent_id, int) or agent_id < 0:
        raise EvidenceRejected("agent_id", "agent_id is a non-negative int")
    if not isinstance(private, Mapping) or not private:
        raise EvidenceRejected("private", "at least the moment's reference is committed")
    commitments = {label: commitment(salt, label, item) for label, item in private.items()}
    try:
        rater_address, subject_address = checksum(rater), checksum(subject)
    except ValueError:
        raise EvidenceRejected("address", "rater and subject are addresses") from None
    document: dict[str, Any] = {
        "schema": EVIDENCE_SCHEMA,
        "network": _wire(network),
        "chainId": chain_id,
        "agentRegistry": f"eip155:{chain_id}:{checksum(identity)}",
        "agentId": str(agent_id),
        "clientAddress": rater_address,
        "subject": {"wallet": subject_address, "walletBinding": wallet_binding},
        "value": value,
        "valueDecimals": 0,
        "tag1": tag1,
        "tag2": tag2,
        "endpoint": endpoint,
        "createdAt": _iso(created_at),
        "commitmentScheme": "hmac-sha256(salt, label|value); salt per viewer, kept off-chain",
    }
    if proof is not None:
        document["proofOfPayment"] = proof.model_dump(by_alias=True)
    if issuer is not None:
        document["issuer"] = dict(issuer)
    if context is not None:
        document["context"] = dict(context)
    # Checked BEFORE the commitments go in: their keys are the private labels
    # ("message", "login"...), and their values are HMACs built right here.
    _check_public(document, "evidence")
    # The caller-written strings are where a private value could ride along.
    free_text = canonical_json([tag1, tag2, endpoint, issuer, context]).decode("utf-8").lower()
    per_row = canonical_json([issuer, context]).decode("utf-8").lower()
    for label, item in private.items():
        value = item.lower()
        escaped = json.dumps(value, ensure_ascii=False)[1:-1]
        if len(value) >= LEAK_CHECK_MIN_LENGTH and (value in free_text or escaped in free_text):
            raise EvidenceRejected(
                "private_value_leaked", f"the {label} appears in public evidence"
            )
        if LEAK_CHECK_MIN_WORD <= len(value) < LEAK_CHECK_MIN_LENGTH and re.search(
            r"(?<![0-9a-z])" + re.escape(escaped) + r"(?![0-9a-z])", per_row
        ):
            raise EvidenceRejected(
                "private_value_leaked", f"the {label} appears in public evidence"
            )
    document["commitments"] = commitments
    body = canonical_json(document)
    return EvidenceDocument(document=document, body=body, content_hash=content_hash(body))
