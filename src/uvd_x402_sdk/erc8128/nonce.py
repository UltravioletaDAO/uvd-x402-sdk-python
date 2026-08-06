"""Nonce protocol for the ERC-8128 verifier.

The SDK NEVER issues nonces and ships NO default store. A first-use-wins
in-process store as "the convenient default" gives ZERO replay protection
across ECS tasks or container restarts — exactly the property MeshRelay's
SQLite+WAL store and its restart test exist to guarantee. A convenient wrong
default is worse than no default.

``consume`` receives the RAW nonce plus context and the store derives its own
key. That is deliberate: MeshRelay indexes by the bare nonce, EM by
``erc8128:{chain}:{addr}:{nonce}``. If the SDK derived the key, every nonce
issued by the previous process would answer ``unknown`` for the whole TTL
after a deploy (a self-inflicted 401 storm), and in EM's first-use-wins store
a mis-derived key would fail OPEN — every nonce looks new, replay protection
silently gone, nothing raises.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

#: What a store reports back.
#:   ok          — first use, accepted
#:   unknown     — issuer-bound stores only: this server never issued it
#:   replayed    — already consumed (409, not retryable)
#:   expired     — issued but past its TTL
#:   unavailable — infra failure; the caller answers 503 + Retry-After
NonceOutcome = str


@runtime_checkable
class NonceStore(Protocol):
    """Duck-typed store. ``consume`` may be sync or async — the verifier awaits
    the result only when it is awaitable, so a synchronous SQLite store needs
    no wrapper.
    """

    def consume(
        self,
        nonce: str,
        *,
        wallet: str,
        chain_id: int,
        ttl_seconds: int,
        created: int,
        expires: int,
    ) -> Any:  # NonceOutcome | Awaitable[NonceOutcome]
        ...


@runtime_checkable
class IssuingNonceStore(NonceStore, Protocol):
    """Issuer-bound stores also mint nonces. Rate limiting and capacity caps
    live HERE, not in the SDK — the SDK never issues.
    """

    def issue(self, ttl_seconds: int) -> Any:  # (nonce, ttl) | Awaitable[...]
        ...


@dataclass(frozen=True)
class NoncePolicy:
    """How the verifier uses the store.

    ``consume`` order is a real product decision, not a default to converge:
    EM consumes BEFORE the crypto (closes a concurrency race; a bad signature
    burns the nonce), MeshRelay AFTER (an unauthenticated attacker cannot burn
    a nonce, and its ``UPDATE … WHERE used_at IS NULL`` is the serialisation
    point). Both are defensible; neither is interchangeable.
    """

    store: NonceStore
    mode: str = "required"  # "required" | "optional"
    consume: str = "before-verify"  # "before-verify" | "after-verify"


def _nonce_ttl(created: int, expires: int, skew_seconds: int) -> int:
    """TTL handed to the store: the signature's own window plus the grace the
    policy already tolerates past expiry.
    """
    return int(expires) - int(created) + int(skew_seconds)


__all__ = ["IssuingNonceStore", "NonceOutcome", "NoncePolicy", "NonceStore"]
