"""Deterministic references: the idempotency key of a rating.

``message_ref(namespace, *parts)`` is ``sha256("|".join([namespace, *parts]))``
in lowercase hex, the form Karma Hello uses for its ``operation_id``
(``services/reputation/live.py::initial_feedback``:
``sha256("base|<wallet>|<message_ref>")``) and Credits Roll for its
``moment_ref`` (``sha256("credits-roll:v1|<streamId>|<messageId>")``). The
outbox has a UNIQUE index on it: the same moment enqueued twice, or after a
restart, is one row and at most one rating.

The join is only injective if no part can contain the separator, so a ``|``
anywhere is refused rather than hashed: ``("a|b", "c")`` and ``("a", "b|c")``
would otherwise be the same moment, and the second would be silently dropped
as a duplicate. An empty part is refused too: an absent message id would make
every moment of a stream collide.
"""

from __future__ import annotations

import hashlib
from typing import Union

REF_SEPARATOR = "|"


def _part(value: Union[str, int], where: str) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ValueError(f"message_ref: {where} must be a str or an int")
    text = str(value)
    if not text.strip():
        raise ValueError(f"message_ref: {where} is empty")
    if REF_SEPARATOR in text:
        raise ValueError(f"message_ref: {where} contains the separator {REF_SEPARATOR!r}")
    return text


def message_ref(namespace: str, *parts: Union[str, int]) -> str:
    """The deterministic reference of one rated thing: 64 lowercase hex digits.

    ``namespace`` names the product and the version of the scheme
    (``"credits-roll:v1"``); ``parts`` identify the thing inside it. Ints are
    written in decimal. Raises ``ValueError`` on an empty part, a part with a
    ``|``, or no parts at all.
    """
    if not isinstance(namespace, str):
        raise ValueError("message_ref: namespace must be a str")
    items = [_part(namespace, "namespace")]
    if not parts:
        raise ValueError("message_ref: at least one part is required")
    items.extend(_part(part, f"part {index}") for index, part in enumerate(parts))
    return hashlib.sha256(REF_SEPARATOR.join(items).encode("utf-8")).hexdigest()
