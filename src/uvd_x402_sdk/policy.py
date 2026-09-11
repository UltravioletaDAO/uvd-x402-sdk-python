"""What this buyer is allowed to sign, decided before it signs.

Python half of the contract the facilitator fixed in its P3 phase (``x402-rs``
2.25.0, ``crates/x402-reqwest/src/policy.rs``). The TypeScript SDK implements
the same one, so a refusal code means the same thing in all three.

# The rule this module exists to enforce

A catalog listing is a claim somebody else made about their own price. The
``402`` that comes back from the actual request is the offer. They can differ,
legitimately: a seller may have repriced, and the listing may be a copy of a
copy. So the buying decision cannot be made against the listing. It has to be
made against **the offer in hand**, every time, before anything is signed.

Two things follow, and both are load-bearing:

* **A divergence from the listing is not, by itself, a refusal.** If the offer
  costs more than the catalog said but still sits inside a policy the operator
  already authorised, the payment proceeds. Stopping to ask would turn every
  ordinary reprice into a halt, and an agent that halts on ordinary commerce is
  an agent nobody can leave running. There is no human-confirmation hook on
  this path.
* **A policy is never widened to fit an offer.** Not by a byte, not once, not
  "because the seller says so". If the offer exceeds what was authorised the
  answer is a refusal with a concrete cause, and the caller decides whether to
  authorise more. There is deliberately no method on :class:`PurchasePolicy`
  that raises a limit: the ceilings are fixed at construction and the mappings
  handed out are read-only views.

# Order of evaluation

Fixed, and part of the contract, because the FIRST failing check is the one
reported and a caller branches on it:

1. Was any offer readable at all? -> ``no-readable-offer``
2. Has it expired? -> ``offer-expired``
3. Is the recipient one we are willing to pay? -> ``recipient-not-permitted``
4. Is its asset budgeted at all? -> ``asset-not-budgeted``
5. Does it exceed the per-payment limit for that asset? -> ``per-payment-limit``
6. Does it exceed what remains of the cumulative limit? -> ``cumulative-limit``

The comparison against an advertised listing is made LAST and decides nothing:
it is evidence for the caller (:class:`QuoteComparison`).

# Three decisions worth saying out loud

* **Evaluating does not spend.** Signing can still fail and a settlement can
  still be refused; a limit that counted attempts would lock a caller out of
  money it never spent. :meth:`PurchasePolicy.record_spend` is a separate call,
  made once the settlement resolved.
* **A clone spends from the same purse.** A client is copied per request; if
  each copy carried its own total, a cumulative limit would mean nothing.
* **Corrupt state reports the ceiling, never zero.** If the running total
  cannot be read, :meth:`PurchasePolicy.spent` answers with the limit: for
  money, the safe direction is to refuse, never to permit.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import (
    Any,
    Dict,
    Iterable,
    Mapping,
    Optional,
    Sequence,
    Tuple,
    Union,
)

from uvd_x402_sdk.networks import normalize_network

__all__ = [
    "OFFER_VALIDITY_EXTENSION",
    "KNOWN_SCHEMES",
    "REFUSAL_CODES",
    "QUOTE_COMPARISON_CODES",
    "AdvertisedQuote",
    "Offer",
    "ParsedAccepts",
    "PolicyApproval",
    "PolicyRefusal",
    "PurchasePolicy",
    "QuoteComparison",
    "TokenAsset",
    "UnreadableOffer",
    "canonical_address",
    "canonical_recipient",
    "no_readable_offer",
    "offer_valid_until",
    "parse_accepts",
]

#: Extension key under which a seller declares how long its offer stands.
#:
#: The version is IN the key. The offer-and-receipt transport may still change,
#: and a value read from an unversioned key could not be compared against
#: anything later. A key we do not recognise is ignored, which means "no
#: declared validity" -- a different thing from "expired".
#:
#: Defined identically in ``x402-rs`` (``src/types.rs``,
#: ``OFFER_VALIDITY_EXTENSION``); a seller and a buyer naming different keys are
#: a seller and a buyer with nothing to say to each other.
OFFER_VALIDITY_EXTENSION = "offer-receipt/1"

#: The scheme vocabulary this build can name, mirroring the facilitator's closed
#: ``Scheme`` enum. An offer naming anything else is unreadable -- kept, counted
#: and named, never allowed to make the rest of the list unpayable (rule 7).
KNOWN_SCHEMES = frozenset({"exact", "upto", "escrow", "commerce", "fhe-transfer"})

#: Longest scheme name kept from an unreadable offer. It is somebody else's
#: string and it ends up in an error message.
MAX_UNREADABLE_SCHEME_LEN = 64

#: The closed refusal vocabulary, in evaluation order.
REFUSAL_CODES: Tuple[str, ...] = (
    "no-readable-offer",
    "offer-expired",
    "recipient-not-permitted",
    "asset-not-budgeted",
    "per-payment-limit",
    "cumulative-limit",
)

#: The closed vocabulary for "how does the offer compare to the listing".
QUOTE_COMPARISON_CODES: Tuple[str, ...] = (
    "not-compared",
    "matches",
    "amount-differs",
    "different-asset",
)

#: How long :meth:`PurchasePolicy.spent` waits for the shared purse before it
#: decides the state is unreadable and answers with the ceiling.
_PURSE_LOCK_TIMEOUT_SECONDS = 2.0


# =============================================================================
# Addresses
# =============================================================================


def canonical_address(address: str) -> str:
    """Canonical form of an address, for comparison.

    **This is not ``str.lower()``**, and that distinction is the whole point.
    EVM addresses are hex and arrive both checksummed and lowercase, so folding
    case is right and necessary. **Base58 is case-sensitive** -- Solana and XRPL
    addresses use both cases as distinct symbols -- so lowercasing one does not
    produce the same address spelled differently, it produces a string that is
    not an address at all.

    An allowlist written in a seller's own spelling would then never match, and
    every payment to a legitimate Solana payee would be refused with
    ``recipient-not-permitted``. Worse in the other direction: two distinct
    base58 addresses can fold to the same lowercase string, so an allowlist
    could admit an address nobody put on it.

    So: hex is folded, everything else is compared exactly.
    """
    trimmed = str(address).strip()
    rest = ""
    if trimmed[:2] in ("0x", "0X"):
        rest = trimmed[2:]
    is_hex = bool(rest) and all(c in "0123456789abcdefABCDEF" for c in rest)
    return trimmed.lower() if is_hex else trimmed


#: The name this function carries in ``x402-rs``. Same function; both spellings
#: are exported so a reader coming from the Rust side finds what they expect.
canonical_recipient = canonical_address


def _canonical_network(network: Any) -> str:
    """Canonical form of a network identifier — ONE name per chain.

    ``base`` and ``eip155:8453`` are the same chain under two dialects: v1 uses
    the name, v2 uses the CAIP-2 id, and the same seller can answer either. Both
    resolve here to the SDK's canonical name (via
    :func:`~uvd_x402_sdk.networks.normalize_network`, which also folds aliases
    like ``skale`` -> ``skale-base``), so a budget written in one dialect covers
    an offer priced in the other.

    Without this a policy written as ``base`` refuses a v2 challenge with
    ``asset-not-budgeted`` — a refusal with a cause that is not true, and one
    the Rust and TypeScript buyers do not produce. Portability of the DECISION
    is the point: the same policy must decide the same way in all three.

    An identifier the registry cannot resolve (an unknown CAIP-2 id, a chain
    this build does not carry) falls back to the lowercased literal rather than
    raising. Two unresolvable dialects of one chain then stay distinct keys, so
    the answer is ``asset-not-budgeted`` — a refusal, which is the safe
    direction for money; normalisation failing must never abort an evaluation.
    """
    text = str(network).strip()
    if not text:
        return ""
    try:
        return normalize_network(text)
    except Exception:  # noqa: BLE001 - see the fallback note above
        return text.lower()


# =============================================================================
# Assets and offers
# =============================================================================


@dataclass(frozen=True)
class TokenAsset:
    """A token on a chain: the key a budget is declared against.

    The network is part of the key on purpose. USDC on Base and USDC on Polygon
    are different money as far as a spending limit is concerned, and a key that
    was only the contract address would let a budget for one pay for the other.
    """

    network: str
    address: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "network", _canonical_network(self.network))
        object.__setattr__(self, "address", canonical_address(self.address))

    @classmethod
    def parse(cls, value: "AssetLike") -> "TokenAsset":
        """Accept a :class:`TokenAsset`, a ``(network, address)`` pair, or a
        mapping with ``network`` and ``asset``/``address`` keys."""
        if isinstance(value, TokenAsset):
            return value
        if isinstance(value, Mapping):
            network = value.get("network")
            address = value.get("asset", value.get("address"))
            if network is None or address is None:
                raise ValueError(
                    "an asset mapping needs 'network' and 'asset' (or 'address')"
                )
            return cls(str(network), str(address))
        if isinstance(value, (tuple, list)) and len(value) == 2:
            return cls(str(value[0]), str(value[1]))
        raise ValueError(
            f"cannot read {value!r} as an asset: pass a TokenAsset, a "
            "(network, address) pair, or a mapping"
        )

    def to_dict(self) -> Dict[str, str]:
        return {"network": self.network, "address": self.address}

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"{self.address or '<no asset named>'} on {self.network}"


AssetLike = Union[TokenAsset, Tuple[str, str], Sequence[str], Mapping[str, Any]]


@dataclass(frozen=True)
class Offer:
    """One readable entry of a 402's ``accepts``, in atomic units.

    ``amount`` is the integer the seller asked for, in the asset's own base
    units. It is never converted to a human amount here: a ceiling compared in
    decimals is a ceiling that depends on a decimals field the seller supplied.
    """

    scheme: str
    network: str
    asset: str
    amount: int
    pay_to: str
    extra: Optional[Dict[str, Any]] = None
    raw: Optional[Dict[str, Any]] = None

    @property
    def token_asset(self) -> TokenAsset:
        return TokenAsset(self.network, self.asset)

    @classmethod
    def from_accept(cls, entry: Mapping[str, Any]) -> "Offer":
        """Read one ``accepts`` entry, or raise :class:`ValueError`.

        Callers normally want :func:`parse_accepts`, which catches the raise and
        records the entry as unreadable instead of losing the whole list.
        """
        if not isinstance(entry, Mapping):
            raise ValueError("offer is not an object")

        # A payment we cannot NAME is a payment we cannot make, so the scheme is
        # required and its vocabulary is closed — same as the facilitator's
        # `Scheme` enum, where an entry without one fails to deserialize. This
        # SDK assumed `exact` until 0.82.0; measured against the one real
        # capture in this repo (`tests/test_x402_transport.py`, 36 of 36 live
        # resources answering 402 on 2026-08-20) every seller names it, so the
        # assumption was covering nobody and was a silent way to sign an `exact`
        # authorization for an offer that asked for something else.
        scheme = entry.get("scheme")
        if not isinstance(scheme, str) or not scheme.strip():
            raise ValueError("offer names no scheme")
        scheme = scheme.strip()
        if scheme not in KNOWN_SCHEMES:
            raise ValueError(f"unknown scheme {scheme!r}")

        network = entry.get("network")
        pay_to = entry.get("payTo")
        # v1 spells it `maxAmountRequired`; a v2 PaymentOption spells it `amount`.
        amount = entry.get("amount")
        if amount is None:
            amount = entry.get("maxAmountRequired")
        if not network or not pay_to or amount is None:
            raise ValueError("offer is missing network, payTo or amount")

        try:
            parsed_amount = _atomic_amount(amount)
        except ValueError as exc:
            raise ValueError(f"offer amount is not atomic: {exc}") from exc

        # An offer that names no asset is still readable: this build defaults to
        # the network's USDC and has always paid those, so calling it unreadable
        # would refuse a seller that works today. It gets an EMPTY asset, which
        # no budget can hold -- so a written policy refuses it by name
        # (`asset-not-budgeted`, "no asset named"), which is the honest answer:
        # a caller cannot budget what the seller never said.
        asset = entry.get("asset") or ""

        extra = entry.get("extra")
        return cls(
            scheme=scheme,
            network=str(network),
            asset=str(asset),
            amount=parsed_amount,
            pay_to=str(pay_to),
            extra=dict(extra) if isinstance(extra, Mapping) else None,
            raw=dict(entry),
        )


@dataclass(frozen=True)
class UnreadableOffer:
    """An offer in a 402 this build cannot interpret.

    The scheme name is the one thing worth keeping: a caller that learns the
    seller offered ``batch-settlement`` knows to look for a facilitator that
    implements it, where "could not parse the response" would have sent it
    looking for a bug in its own code.
    """

    scheme: Optional[str] = None
    reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {"scheme": self.scheme, "reason": self.reason}


@dataclass(frozen=True)
class ParsedAccepts:
    """A 402's offers, sorted into the ones this build can read and the rest."""

    offers: Tuple[Offer, ...] = ()
    unreadable: Tuple[UnreadableOffer, ...] = ()
    extensions: Mapping[str, Any] = field(default_factory=dict)

    @property
    def offered_schemes(self) -> Tuple[str, ...]:
        """Scheme names of the unreadable offers, for the refusal message."""
        return tuple(o.scheme for o in self.unreadable if o.scheme)

    @property
    def valid_until(self) -> Optional[int]:
        return offer_valid_until(self.extensions)


def _atomic_amount(value: Any) -> int:
    """Read an atomic (integer, base-unit) amount, or raise.

    A fractional atomic amount is not an atomic amount, and ``bool`` is not a
    number here even though Python says it is an ``int``.
    """
    if isinstance(value, bool):
        raise ValueError("a boolean is not an amount")
    if isinstance(value, int):
        parsed = value
    else:
        text = str(value).strip()
        if not text:
            raise ValueError("empty amount")
        try:
            parsed = int(text)
        except ValueError:
            raise ValueError(f"{value!r} is not an integer of base units") from None
    if parsed < 0:
        raise ValueError("a negative amount is not an amount")
    return parsed


def parse_accepts(
    challenge: Union[Mapping[str, Any], Sequence[Any], None],
) -> ParsedAccepts:
    """Read a 402 challenge, keeping the offers this build understands.

    **Why this is tolerant (rule 7).** The scheme vocabulary is closed, because
    a payment we cannot name is a payment we cannot make. But ``accepts`` is a
    LIST, and one unreadable entry used to take the whole list with it -- so a
    seller advertising ``exact`` alongside anything this build does not
    implement was unpayable, and the buyer never learned that a perfectly
    payable offer had been sitting beside it.

    The offers we can read are kept; the ones we cannot are counted, with their
    scheme name, so a refusal can say what the seller actually offered.

    Accepts the whole challenge body (``{x402Version, accepts, extensions}``,
    v1 or v2), a bare list of offers, or the non-spec shape where a single
    requirement sits at the top level.
    """
    extensions: Mapping[str, Any] = {}
    if challenge is None:
        entries: Sequence[Any] = []
    elif isinstance(challenge, Mapping):
        raw_extensions = challenge.get("extensions")
        extensions = raw_extensions if isinstance(raw_extensions, Mapping) else {}
        raw_accepts = challenge.get("accepts")
        if raw_accepts is None:
            # v1 spelling used by some sellers, then the top-level single offer.
            raw_accepts = challenge.get("paymentRequirements")
        if raw_accepts is None:
            raw_accepts = [challenge] if challenge.get("payTo") else []
        entries = raw_accepts if isinstance(raw_accepts, (list, tuple)) else []
    elif isinstance(challenge, (list, tuple)):
        entries = challenge
    else:
        entries = []

    offers = []
    unreadable = []
    for entry in entries:
        try:
            offers.append(Offer.from_accept(entry))
        except (ValueError, TypeError, AttributeError) as exc:
            scheme = None
            if isinstance(entry, Mapping):
                raw_scheme = entry.get("scheme")
                if isinstance(raw_scheme, str):
                    scheme = raw_scheme[:MAX_UNREADABLE_SCHEME_LEN]
            unreadable.append(UnreadableOffer(scheme=scheme, reason=str(exc)))

    return ParsedAccepts(
        offers=tuple(offers),
        unreadable=tuple(unreadable),
        extensions=extensions,
    )


def offer_valid_until(extensions: Optional[Mapping[str, Any]]) -> Optional[int]:
    """Read ``validUntil`` (Unix seconds) from a challenge's extensions.

    Shape: ``extensions["offer-receipt/1"].info.validUntil``. The
    ``{info, schema}`` envelope is the one every merged extension uses; reading
    the number from anywhere else would be reading a field nobody agreed to
    publish.

    Anything unreadable is ``None``, **never zero** (rule 5): "the seller said
    something we could not read" must not become "this offer expired in 1970",
    which would refuse every payment to that seller. A JSON number is required,
    exactly as the Rust side requires one -- a string is a shape the contract
    does not define.
    """
    if not isinstance(extensions, Mapping):
        return None
    extension = extensions.get(OFFER_VALIDITY_EXTENSION)
    if not isinstance(extension, Mapping):
        return None
    info = extension.get("info")
    if not isinstance(info, Mapping):
        return None
    value = info.get("validUntil")
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if value < 0:
        return None
    return value


# =============================================================================
# What the caller gets back
# =============================================================================


@dataclass(frozen=True)
class AdvertisedQuote:
    """What a catalog listing advertised, for comparison against the real offer.

    Optional throughout. A buyer that never read a listing simply evaluates the
    offer against its policy, which is the same decision with one fewer input.
    """

    asset: TokenAsset
    amount: int

    @classmethod
    def of(cls, asset: AssetLike, amount: Any) -> "AdvertisedQuote":
        return cls(asset=TokenAsset.parse(asset), amount=_atomic_amount(amount))


@dataclass(frozen=True)
class QuoteComparison:
    """How the offer in hand compares to what the catalog advertised.

    **Never decides anything.** It is reported so a caller can log it or surface
    it; a seller repricing inside a policy the operator already authorised is
    ordinary commerce.
    """

    code: str
    advertised_amount: Optional[int] = None
    offered_amount: Optional[int] = None
    advertised_asset: Optional[TokenAsset] = None
    offered_asset: Optional[TokenAsset] = None

    @classmethod
    def not_compared(cls) -> "QuoteComparison":
        return cls(code="not-compared")

    @classmethod
    def matches(cls) -> "QuoteComparison":
        return cls(code="matches")

    @classmethod
    def amount_differs(cls, advertised: int, offered: int) -> "QuoteComparison":
        return cls(
            code="amount-differs",
            advertised_amount=advertised,
            offered_amount=offered,
        )

    @classmethod
    def different_asset(
        cls, advertised: TokenAsset, offered: TokenAsset
    ) -> "QuoteComparison":
        return cls(
            code="different-asset",
            advertised_asset=advertised,
            offered_asset=offered,
        )

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"code": self.code}
        if self.advertised_amount is not None:
            out["advertised"] = str(self.advertised_amount)
        if self.offered_amount is not None:
            out["offered"] = str(self.offered_amount)
        if self.advertised_asset is not None:
            out["advertisedAsset"] = self.advertised_asset.to_dict()
        if self.offered_asset is not None:
            out["offeredAsset"] = self.offered_asset.to_dict()
        return out


class PolicyDecision:
    """Base of the two outcomes, so a caller can branch on ``.approved``."""

    approved: bool = False


@dataclass(frozen=True)
class PolicyApproval(PolicyDecision):
    """A payment this policy permits, and what it noticed on the way."""

    asset: TokenAsset
    amount: int
    versus_quote: QuoteComparison = field(default_factory=QuoteComparison.not_compared)

    approved = True

    def to_dict(self) -> Dict[str, Any]:
        return {
            "approved": True,
            "asset": self.asset.to_dict(),
            "amount": str(self.amount),
            "versusQuote": self.versus_quote.to_dict(),
        }


@dataclass(frozen=True)
class PolicyRefusal(PolicyDecision):
    """Why a payment was not signed.

    ``code`` is drawn from :data:`REFUSAL_CODES` and nothing else: there is no
    ``other``, because a refusal a caller cannot interpret is a refusal it will
    paper over. Every variant carries the numbers that caused it.
    """

    code: str
    message: str
    asset: Optional[TokenAsset] = None
    pay_to: Optional[str] = None
    requested: Optional[int] = None
    allowed: Optional[int] = None
    spent: Optional[int] = None
    would_total: Optional[int] = None
    valid_until: Optional[int] = None
    now: Optional[int] = None
    offered: Tuple[str, ...] = ()
    versus_quote: QuoteComparison = field(default_factory=QuoteComparison.not_compared)

    approved = False

    def __str__(self) -> str:
        return self.message

    def to_dict(self) -> Dict[str, Any]:
        """The refusal as the numbers that caused it, for logs and telemetry.

        Keys are camelCase because that is the vocabulary shared with the
        TypeScript SDK and the facilitator.
        """
        out: Dict[str, Any] = {"approved": False, "code": self.code, "message": self.message}
        if self.asset is not None:
            out["asset"] = self.asset.to_dict()
        if self.pay_to is not None:
            out["payTo"] = self.pay_to
        for key, value in (
            ("requested", self.requested),
            ("allowed", self.allowed),
            ("spent", self.spent),
            ("wouldTotal", self.would_total),
        ):
            if value is not None:
                out[key] = str(value)
        for key, value in (("validUntil", self.valid_until), ("now", self.now)):
            if value is not None:
                out[key] = value
        if self.offered:
            out["offered"] = list(self.offered)
        return out


def no_readable_offer(unreadable: Iterable[UnreadableOffer]) -> PolicyRefusal:
    """Turn a challenge with nothing payable in it into a refusal that says so.

    The point is the message: a caller that learns the seller offered
    ``batch-settlement`` knows to look for a facilitator that implements it.
    """
    unreadable = list(unreadable)
    offered = tuple(o.scheme for o in unreadable if o.scheme)
    message = (
        "no offer in this challenge is one this build can pay; offered: "
        f"{list(offered)}"
    )
    # `offered[]` carries the NAMED schemes and nothing else — it is the wire
    # vocabulary the contract fixed. An offer that named no scheme has nothing
    # to put there, so it is counted in the prose instead: "offered: []" alone
    # is the message that sends a caller hunting a bug in its own code.
    unnamed = len(unreadable) - len(offered)
    if unnamed:
        message += f" ({unnamed} offer(s) named no scheme)"
    return PolicyRefusal(
        code="no-readable-offer",
        message=message,
        offered=offered,
    )


# =============================================================================
# The policy
# =============================================================================


class _Purse:
    """The running total, shared by every copy of a policy."""

    __slots__ = ("lock", "totals", "corrupt")

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.totals: Dict[TokenAsset, int] = {}
        self.corrupt = False


class PurchasePolicy:
    """The spending rules a caller authorised in advance.

    Copying shares the running total: two copies of one policy spend from the
    same purse, which is what makes a cumulative limit mean anything when a
    client is copied per request.

    The ceilings are fixed at construction. There is no setter, no builder that
    raises a limit and no ``allow`` toggle -- widening a policy means writing a
    new one, which is a visible act in the caller's code (rule 2).

    Example::

        from uvd_x402_sdk import PurchasePolicy, TokenAsset

        USDC_BASE = TokenAsset("base", "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913")
        policy = PurchasePolicy(
            per_payment={USDC_BASE: 50_000},       # 0.05 USDC per call
            cumulative={USDC_BASE: 5_000_000},     # 5 USDC while this lives
            only_pay=["0xe4dc963c56979E0260fc146b87eE24F18220e545"],
        )
        client = X402Client(recipient_address=..., policy=policy)
        resp = client.fetch("https://api.example.com/data")
        policy.record_spend(USDC_BASE, 50_000)     # after the settlement resolved
    """

    __slots__ = ("_per_payment", "_cumulative", "_recipients", "_allow_unlisted", "_purse")

    def __init__(
        self,
        per_payment: Optional[Mapping[AssetLike, Any]] = None,
        cumulative: Optional[Mapping[AssetLike, Any]] = None,
        *,
        only_pay: Optional[Iterable[str]] = None,
        allow_unlisted_assets: bool = False,
    ) -> None:
        """Build a policy.

        Args:
            per_payment: Most this policy will pay in ONE payment of an asset,
                in atomic units.
            cumulative: Most this policy will pay in an asset in total, across
                every payment it approves for as long as it lives.
            only_pay: Restrict payment to these recipients. Canonicalised **by
                family** (see :func:`canonical_address`), never by lowercasing.
                ``None`` means any recipient.
            allow_unlisted_assets: Whether an asset with no declared ceiling may
                be paid at all. **False by default, and deliberately so**: the
                limits are a map, and a map answers "no entry" for every asset
                nobody thought of, so permitting on a missing entry means a
                budget in USDC is no budget at all for any other token. A seller
                pricing the same resource in something unlisted would walk
                straight past the ceiling, and the wallet would sign it --
                the EVM signer takes its EIP-712 domain from the seller's own
                ``extra`` and will happily sign for a token it has never heard
                of. Ask for the permissive mode by name
                (:meth:`PurchasePolicy.permissive`) if you want it.
        """
        self._per_payment = MappingProxyType(_asset_limits(per_payment))
        self._cumulative = MappingProxyType(_asset_limits(cumulative))
        self._recipients: Optional[frozenset] = (
            None
            if only_pay is None
            else frozenset(canonical_address(r) for r in only_pay)
        )
        self._allow_unlisted = bool(allow_unlisted_assets)
        self._purse = _Purse()

    # -- construction ---------------------------------------------------------

    @classmethod
    def permissive(cls) -> "PurchasePolicy":
        """A policy that permits an asset it was never told about.

        This is what :class:`~uvd_x402_sdk.client.X402Client` holds when the
        caller supplied no policy, and it exists for exactly one reason: the
        buyer loop had no budget before 0.82.0, and turning one on silently
        would refuse payments that callers are making today. Named rather than
        defaulted, so choosing it is visible.

        The asymmetry is on purpose: whoever sits down to WRITE a policy gets
        the safe default.
        """
        return cls(allow_unlisted_assets=True)

    def __copy__(self) -> "PurchasePolicy":
        return self._sharing_purse()

    def __deepcopy__(self, memo: Dict[int, Any]) -> "PurchasePolicy":
        # Deliberately NOT a deep copy of the purse. A deep copy that duplicated
        # the running total would hand a fresh budget to every copy, which is
        # precisely the failure a cumulative limit exists to prevent.
        return self._sharing_purse()

    def _sharing_purse(self) -> "PurchasePolicy":
        twin = PurchasePolicy.__new__(PurchasePolicy)
        twin._per_payment = self._per_payment
        twin._cumulative = self._cumulative
        twin._recipients = self._recipients
        twin._allow_unlisted = self._allow_unlisted
        twin._purse = self._purse
        return twin

    # -- what was authorised --------------------------------------------------

    @property
    def per_payment(self) -> Mapping[TokenAsset, int]:
        """Read-only view of the per-payment ceilings."""
        return self._per_payment

    @property
    def cumulative(self) -> Mapping[TokenAsset, int]:
        """Read-only view of the cumulative ceilings."""
        return self._cumulative

    @property
    def only_pay(self) -> Optional[frozenset]:
        """Canonicalised recipients, or ``None`` when any recipient is allowed."""
        return self._recipients

    @property
    def allows_unlisted_assets(self) -> bool:
        return self._allow_unlisted

    def __repr__(self) -> str:  # pragma: no cover - diagnostics
        return (
            f"PurchasePolicy(per_payment={dict(self._per_payment)!r}, "
            f"cumulative={dict(self._cumulative)!r}, "
            f"only_pay={None if self._recipients is None else sorted(self._recipients)!r}, "
            f"allow_unlisted_assets={self._allow_unlisted!r})"
        )

    # -- the running total ----------------------------------------------------

    def spent(self, asset: AssetLike) -> int:
        """Total recorded so far for ``asset``.

        If the shared purse cannot be read -- a held lock, a value that is not a
        non-negative integer -- this reports the CEILING, not zero. Reporting
        zero would silently restore a caller's whole budget; for money the safe
        direction is to refuse, never to permit.
        """
        key = TokenAsset.parse(asset)
        ceiling = self._cumulative.get(key, 0)
        if self._purse.corrupt:
            return ceiling
        if not self._purse.lock.acquire(timeout=_PURSE_LOCK_TIMEOUT_SECONDS):
            self._purse.corrupt = True
            return ceiling
        try:
            value = self._purse.totals.get(key, 0)
        finally:
            self._purse.lock.release()
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            self._purse.corrupt = True
            return ceiling
        return value

    def record_spend(self, asset: AssetLike, amount: Any) -> None:
        """Record that a payment actually happened.

        Separate from :meth:`evaluate` on purpose (rule 1): approving does not
        spend, because signing can still fail and a settlement can still be
        refused. Call this once the settlement resolved.

        Raises:
            ValueError: ``amount`` is not a non-negative integer of base units.
                A negative spend would hand budget back.
        """
        key = TokenAsset.parse(asset)
        delta = _atomic_amount(amount)
        if not self._purse.lock.acquire(timeout=_PURSE_LOCK_TIMEOUT_SECONDS):
            # The spend is lost, so the total now understates reality. Mark the
            # purse unreadable: from here on `spent` answers with the ceiling
            # and the cumulative check refuses, which is the safe direction.
            self._purse.corrupt = True
            return
        try:
            current = self._purse.totals.get(key, 0)
            if isinstance(current, bool) or not isinstance(current, int) or current < 0:
                self._purse.corrupt = True
                return
            self._purse.totals[key] = current + delta
        finally:
            self._purse.lock.release()

    # -- the decision ---------------------------------------------------------

    def evaluate(
        self,
        offer: Union[Offer, Mapping[str, Any], None],
        now: Optional[int] = None,
        *,
        quote: Optional[AdvertisedQuote] = None,
        valid_until: Optional[int] = None,
        unreadable: Iterable[UnreadableOffer] = (),
    ) -> Union[PolicyApproval, PolicyRefusal]:
        """Decide whether this offer may be signed.

        **Approving does not record the spend** -- see :meth:`record_spend`.

        Args:
            offer: The offer in hand. An :class:`Offer`, a raw ``accepts``
                entry, or ``None`` when nothing readable was on offer.
            now: Unix seconds. Passed rather than read so the decision is
                testable at an exact instant; money decisions that depend on a
                hidden clock cannot be pinned. Defaults to the wall clock.
            quote: What a listing advertised, when one was read. Reported, never
                decisive.
            valid_until: Unix seconds the offer stands until, from
                :func:`offer_valid_until`. ``None`` means no declared validity,
                which is not the same as expired.
            unreadable: The offers this build could not read, for the
                ``no-readable-offer`` message.

        Returns:
            :class:`PolicyApproval` or :class:`PolicyRefusal`; both answer
            ``.approved``.
        """
        now = int(time.time()) if now is None else int(now)

        # 1. Was any offer readable at all?
        if offer is None:
            return no_readable_offer(unreadable)
        if not isinstance(offer, Offer):
            try:
                offer = Offer.from_accept(offer)
            except (ValueError, TypeError, AttributeError) as exc:
                scheme = None
                if isinstance(offer, Mapping) and isinstance(offer.get("scheme"), str):
                    scheme = str(offer["scheme"])[:MAX_UNREADABLE_SCHEME_LEN]
                return no_readable_offer(
                    list(unreadable) + [UnreadableOffer(scheme=scheme, reason=str(exc))]
                )

        # 2. Expiry. Before anything about money: terms that have lapsed are not
        #    terms, whatever they say. `now == valid_until` still stands (rule
        #    6) -- it is the last instant the offer is in force.
        if valid_until is not None and now > valid_until:
            return PolicyRefusal(
                code="offer-expired",
                message=(
                    f"this offer expired at {valid_until} (now {now}); "
                    "ask the seller for new terms"
                ),
                valid_until=valid_until,
                now=now,
                asset=offer.token_asset,
                requested=offer.amount,
                pay_to=offer.pay_to,
            )

        asset = offer.token_asset
        requested = offer.amount

        # 3. Recipient, canonicalised by family (rule 4c).
        if self._recipients is not None:
            if canonical_address(offer.pay_to) not in self._recipients:
                return PolicyRefusal(
                    code="recipient-not-permitted",
                    message=f"this policy does not pay {offer.pay_to}",
                    pay_to=offer.pay_to,
                    asset=asset,
                    requested=requested,
                    now=now,
                )

        # 4. An asset nobody budgeted for. Checked BEFORE the ceilings, because
        #    the ceilings are a map and a map has no opinion about a key it does
        #    not hold -- which is precisely how an unlisted token would sail past
        #    a budget that looks complete. The caller needs to hear "budget that
        #    asset", not "raise a ceiling that does not exist".
        if (
            not self._allow_unlisted
            and asset not in self._per_payment
            and asset not in self._cumulative
        ):
            return PolicyRefusal(
                code="asset-not-budgeted",
                message=(
                    f"this policy has no budget for {asset}; it pays only what "
                    "it was told it may pay"
                ),
                asset=asset,
                requested=requested,
                pay_to=offer.pay_to,
                now=now,
            )

        # 5. Per-payment ceiling.
        allowed = self._per_payment.get(asset)
        if allowed is not None and requested > allowed:
            return PolicyRefusal(
                code="per-payment-limit",
                message=(
                    f"offer of {requested} exceeds the per-payment limit of "
                    f"{allowed} for {asset}"
                ),
                asset=asset,
                requested=requested,
                allowed=allowed,
                pay_to=offer.pay_to,
                now=now,
            )

        # 6. Cumulative ceiling.
        allowed = self._cumulative.get(asset)
        if allowed is not None:
            spent = self.spent(asset)
            would_total = spent + requested
            if would_total > allowed:
                return PolicyRefusal(
                    code="cumulative-limit",
                    message=(
                        f"offer of {requested} would take spend to {would_total}, "
                        f"past the cumulative limit of {allowed} for {asset} "
                        f"(already spent {spent})"
                    ),
                    asset=asset,
                    requested=requested,
                    spent=spent,
                    would_total=would_total,
                    allowed=allowed,
                    pay_to=offer.pay_to,
                    now=now,
                )

        # The comparison against the listing is made LAST and refuses nothing.
        # It is evidence for the caller, not a gate.
        return PolicyApproval(
            asset=asset,
            amount=requested,
            versus_quote=_compare_quote(quote, asset, requested),
        )


def _compare_quote(
    quote: Optional[AdvertisedQuote], asset: TokenAsset, requested: int
) -> QuoteComparison:
    if quote is None:
        return QuoteComparison.not_compared()
    if quote.asset != asset:
        # The same number in another currency is not the same price (rule 4), so
        # there is nothing to compare.
        return QuoteComparison.different_asset(quote.asset, asset)
    if quote.amount != requested:
        return QuoteComparison.amount_differs(quote.amount, requested)
    return QuoteComparison.matches()


def _asset_limits(limits: Optional[Mapping[AssetLike, Any]]) -> Dict[TokenAsset, int]:
    if not limits:
        return {}
    out: Dict[TokenAsset, int] = {}
    for asset, amount in limits.items():
        out[TokenAsset.parse(asset)] = _atomic_amount(amount)
    return out
