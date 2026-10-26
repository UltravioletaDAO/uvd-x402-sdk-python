"""Where a relayed rating may be signed, and what exactly is signed there.

The delegate allowlist is the most sensitive table of the module: signing an
EIP-7702 authorization hands the named contract the power to act AS the
rater's account. So it comes from the facilitator's VERIFIED source, never
from a prepare: x402-rs ``src/erc8004/relay.rs::delegate_address`` at
``main`` 09c3e3719 (2026-10-25), the same rows Karma Kadabra's
``agents_sdk/rating_rail.py`` and Execution Market's generated
``feedback-delegates`` carry. Keyed by CHAIN ID: with CREATE the same address
holds different versions on different chains, so an address alone authorises
nothing. A new row lands here only with a release, after reading that source.

The typed data is the v4 ``FeedbackDelegate``'s ``RelayedGiveFeedback``, field
for field as x402-rs ``relay_v4.rs::give_feedback_typed_data`` emits it. Its
typehash is pinned (:data:`GIVE_FEEDBACK_TYPEHASH`) and so is the digest of
the vector read from the DEPLOYED contract on Base
(``tests/test_reputation_signing.py``).
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Optional

from uvd_x402_sdk.erc8004 import ERC8004_CONTRACTS, RELAYED_FEEDBACK_NETWORKS, _wire

#: Network name (as the facilitator spells it) -> chain id, for every network
#: of :data:`uvd_x402_sdk.erc8004.RELAYED_FEEDBACK_NETWORKS`. A test binds the
#: two sets: a relay network without a row here cannot be signed for.
RELAY_CHAIN_IDS: Mapping[str, int] = MappingProxyType(
    {
        "base": 8453,
        "ethereum": 1,
        "polygon": 137,
        "arbitrum": 42161,
        "optimism": 10,
        "celo": 42220,
        "bsc": 56,
        "monad": 143,
        "arc": 5042,
        "base-sepolia": 84532,
    }
)

#: Chain id -> the v4 ``FeedbackDelegate`` of that chain. The ONLY EIP-7702
#: targets this module signs, and the only ``delegate`` a prepare may name.
RELAY_DELEGATES: Mapping[int, str] = MappingProxyType(
    {
        8453: "0x260D3D0258680aA458D0EBB8BcAE8A2f68bf6163",
        1: "0x9577F05D10D2052C75D115e3a0A8f9Dc0cBb7A7b",
        137: "0x1dfc1A578bf321A42c441B4bd60fD5d7bb4BFaD5",
        42161: "0xf670C69BCbb2453FaE5Ec009c2b6dd934BE46A7f",
        10: "0x794C907FdfC71BFaF0b86D0e463BBD6E949A31bA",
        42220: "0x0Dff14dFF648769cB8C3D5F5a150f32Ca2BB9511",
        56: "0xe25cF9B9F5A3B5faa7628c751466df0166d96B59",
        143: "0x794C907FdfC71BFaF0b86D0e463BBD6E949A31bA",
        5042: "0x955Cc9fB9aB95FC0821ae74197D273dde5dA84f1",
        84532: "0x9551263b9B83b1A737D55fd5e67Fb6D60e4eF787",
    }
)

DOMAIN_NAME = "FeedbackDelegate"
DOMAIN_VERSION = "1"
PRIMARY_TYPE = "RelayedGiveFeedback"

#: ``(name, type)`` of the EIP-712 domain, in order.
EIP712_DOMAIN_FIELDS = (
    ("name", "string"),
    ("version", "string"),
    ("chainId", "uint256"),
    ("verifyingContract", "address"),
)

#: ``(name, type)`` of ``RelayedGiveFeedback``, in order: the order is part of
#: the typehash, one field moved is a different struct.
GIVE_FEEDBACK_FIELDS = (
    ("registry", "address"),
    ("agentId", "uint256"),
    ("value", "int128"),
    ("valueDecimals", "uint8"),
    ("tag1", "string"),
    ("tag2", "string"),
    ("endpoint", "string"),
    ("feedbackURI", "string"),
    ("feedbackHash", "bytes32"),
    ("deadline", "uint256"),
    ("nonce", "bytes32"),
)

#: keccak of the encoded ``RelayedGiveFeedback`` type, as x402-rs pins it
#: (``relay_v4.rs::the_typehashes_are_the_confirmed_ones``).
GIVE_FEEDBACK_TYPEHASH = "0x1303f838650b9f1619400e61b9bda2d6b484ea75b3af6a9ae58337d57a0585c0"


def relay_chain_id(network: str) -> Optional[int]:
    """Chain id of a relay network, or ``None`` when relaying is not served there."""
    wire = _wire(network)
    if wire not in RELAYED_FEEDBACK_NETWORKS:
        return None
    return RELAY_CHAIN_IDS.get(wire)


def reputation_registry(network: str) -> Optional[str]:
    """The ERC-8004 Reputation Registry of ``network`` (the SDK's one table)."""
    contracts = ERC8004_CONTRACTS.get(_wire(network))
    return contracts.reputation_registry if contracts else None


def identity_registry(network: str) -> Optional[str]:
    """The ERC-8004 Identity Registry of ``network`` (the SDK's one table)."""
    contracts = ERC8004_CONTRACTS.get(_wire(network))
    return contracts.identity_registry if contracts else None
