"""Arras v1 NEP-297 envelopes, typed from escrow-core/src/events.rs.

Pass flattened receipt logs. Parsing is not proof of the emitter or settlement:
check the receipt executor and outcome separately. Invalid batches are skipped
atomically; foreign logs and unknown standards, versions and events are ignored.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Callable, Literal, TypedDict, Union

from .bounded_json import loads_bounded
from .near_escrow import _uint


class ArrasAuthorizedData(TypedDict):
    payment_id: str
    operator_id: str
    token: str
    payer: str
    receiver: str
    amount: str
    fee_bps: int
    fee_recipient: str
    terms_hash: str


class ArrasReleasedData(TypedDict):
    payment_id: str
    amount: str
    net: str
    fee: str


class ArrasPaymentAmountData(TypedDict):
    payment_id: str
    amount: str


class ArrasWithdrawnData(TypedDict):
    account: str
    token: str
    amount: str
    ok: bool


class ArrasCompactedData(TypedDict):
    payment_id: str


class _OperatorRequired(TypedDict):
    operator_id: str


class ArrasOperatorData(_OperatorRequired, total=False):
    owner: str
    capturers: list[str]
    fee_bps: int
    fee_recipient: str
    min_amount: str
    tope_tvl_operador: str
    tope_tvl_por_payer: str
    sponsor_max_por_payer: str


class ArrasCapsSetData(TypedDict):
    by: str
    tope_por_pago: str
    tope_tvl_global: str


class ArrasPauseData(TypedDict):
    by: str
    paused: bool
    at: str


ArrasEventData = Union[
    ArrasAuthorizedData,
    ArrasReleasedData,
    ArrasPaymentAmountData,
    ArrasWithdrawnData,
    ArrasCompactedData,
    ArrasOperatorData,
    ArrasCapsSetData,
    ArrasPauseData,
]


class ArrasEvent(TypedDict):
    standard: Literal["arras"]
    version: Literal["1.0.0"]
    event: Literal[
        "authorized",
        "released",
        "refunded",
        "reclaimed",
        "withdrawn",
        "compacted",
        "operator_registered",
        "operator_updated",
        "caps_set",
        "paused",
        "unpaused",
    ]
    data: list[ArrasEventData]


Check = Callable[[object], bool]


def _string(value: object) -> bool:
    return isinstance(value, str)


def _boolean(value: object) -> bool:
    return type(value) is bool


def _integer(bits: int) -> Check:
    def check(value: object) -> bool:
        if type(value) is not str:
            return False
        try:
            _uint(value, bits, "event integer")
            return True
        except ValueError:
            return False

    return check


def _fee(value: object) -> bool:
    return type(value) is int and 0 <= value <= 65535


def _strings(value: object) -> bool:
    return isinstance(value, list) and all(_string(item) for item in value)


def _record(required: dict[str, Check], optional: dict[str, Check] | None = None) -> Check:
    def check(value: object) -> bool:
        return (
            isinstance(value, dict)
            and all(key in value and validate(value[key]) for key, validate in required.items())
            and all(
                key not in value or validate(value[key])
                for key, validate in (optional or {}).items()
            )
        )

    return check


_u128, _u64 = _integer(128), _integer(64)
_operator = _record(
    {"operator_id": _string},
    {
        "owner": _string,
        "capturers": _strings,
        "fee_bps": _fee,
        "fee_recipient": _string,
        "min_amount": _u128,
        "tope_tvl_operador": _u128,
        "tope_tvl_por_payer": _u128,
        "sponsor_max_por_payer": _u128,
    },
)
_payment_amount = _record({"payment_id": _string, "amount": _u128})
_pause = _record({"by": _string, "paused": _boolean, "at": _u64})
_CHECKS: dict[str, Check] = {
    "authorized": _record(
        {
            "payment_id": _string,
            "operator_id": _string,
            "token": _string,
            "payer": _string,
            "receiver": _string,
            "amount": _u128,
            "fee_bps": _fee,
            "fee_recipient": _string,
            "terms_hash": _string,
        }
    ),
    "released": _record({"payment_id": _string, "amount": _u128, "net": _u128, "fee": _u128}),
    "refunded": _payment_amount,
    "reclaimed": _payment_amount,
    "withdrawn": _record({"account": _string, "token": _string, "amount": _u128, "ok": _boolean}),
    "compacted": _record({"payment_id": _string}),
    "operator_registered": _operator,
    "operator_updated": _operator,
    "caps_set": _record({"by": _string, "tope_por_pago": _u128, "tope_tvl_global": _u128}),
    "paused": _pause,
    "unpaused": _pause,
}


def parse_arras_events(logs: Iterable[object]) -> list[ArrasEvent]:
    """Return validated envelopes, preserving batches, fields and integer strings.

    Each JSON document is bounded to 512 KiB and 64 levels before parsing.
    An unknown or malformed entry cannot hide a later valid receipt log.
    """
    result: list[ArrasEvent] = []
    if not isinstance(logs, Iterable) or isinstance(logs, (str, bytes, dict)):
        return result
    for log in logs:
        if not isinstance(log, str) or not log.startswith("EVENT_JSON:"):
            continue
        try:
            value = loads_bounded(log[11:])
        except ValueError:
            continue
        if (
            not isinstance(value, dict)
            or value.get("standard") != "arras"
            or value.get("version") != "1.0.0"
            or not isinstance(value.get("event"), str)
            or value["event"] not in _CHECKS
            or not isinstance(value.get("data"), list)
        ):
            continue
        if not all(_CHECKS[value["event"]](item) for item in value["data"]):
            continue
        result.append(
            {
                "standard": "arras",
                "version": "1.0.0",
                "event": value["event"],
                "data": value["data"],
            }
        )
    return result
