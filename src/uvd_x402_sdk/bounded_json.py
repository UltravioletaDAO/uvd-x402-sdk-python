"""Parse JSON that somebody else wrote, with a ceiling on its size and nesting.

A 402 challenge is written by the seller, and so is anything else a buyer
reads before it has decided to pay. CPython's JSON parser recurses once per
level of nesting and the SDK runs with a raised recursion limit (``py_ecc``,
which this SDK imports, raises it), so an unbounded nesting is not reliably a
``ValueError``. Parsing of third-party documents is bounded here.

:func:`loads_bounded` is ``json.loads`` with two checks made first, on the
SAME text ``json.loads`` then parses:

* **bytes**: the input's size, before anything is decoded;
* **nesting**: the deepest ``[`` / ``{`` outside JSON strings, counted by one
  iterative pass (no recursion of its own), after bytes are decoded exactly
  as ``json.loads`` decodes them (``json.detect_encoding``: UTF-8 with or
  without BOM, UTF-16, UTF-32). A ``charset`` in a ``Content-Type`` header is
  not authoritative for JSON and is never consulted: measuring one decoding
  and parsing another could read an escaped quote as a delimiter.

``max_depth`` itself is capped (:data:`_MAX_DEPTH_CEILING`): a caller cannot
lift the nesting bound past what the parser handles.

Public API (stable): ``MAX_CHALLENGE_BYTES``, ``MAX_JSON_DEPTH``,
``JSONBoundsError``, ``json_depth_within``, ``loads_bounded``. Every
parameter is keyword-only after the data, so new ones can be added without
breaking a caller.
"""

from __future__ import annotations

import json
import re
from typing import Any, Optional, Union

__all__ = [
    "MAX_CHALLENGE_BYTES",
    "MAX_JSON_DEPTH",
    "JSONBoundsError",
    "json_depth_within",
    "loads_bounded",
]

#: Default ceiling, in bytes, on a JSON document read from a third party: a
#: 402 body, a ``PAYMENT-REQUIRED`` header (before base64 decoding), a
#: string body. Real challenges are a few KiB; 512 KiB leaves room for a
#: 402 body that also carries a free preview.
MAX_CHALLENGE_BYTES = 512 * 1024

#: Default ceiling on nesting (levels of ``[`` / ``{``). A challenge with a
#: bazaar schema nests about a dozen levels.
MAX_JSON_DEPTH = 64

# Highest ``max_depth`` a caller may ask for. Well under what CPython's parser
# handles on any supported platform; far above any real challenge.
_MAX_DEPTH_CEILING = 1000

BytesLike = Union[bytes, bytearray, memoryview]

_STRUCTURAL = re.compile(r'[\[\]{}"\\]')


class JSONBoundsError(ValueError):
    """The document is larger or deeper than the caller allowed.

    A :class:`ValueError`, so a caller that already treats "not JSON" as
    ``except ValueError`` treats this the same way. ``reason`` is
    ``"too-large"`` or ``"too-deep"``.
    """

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


def _check_limit(
    name: str, value: Any, *, optional: bool, ceiling: Optional[int] = None
) -> None:
    if value is None and optional:
        return
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an int{' or None' if optional else ''}")
    if value < 0:
        raise ValueError(f"{name} must not be negative")
    if ceiling is not None and value > ceiling:
        raise ValueError(f"{name} must be at most {ceiling}")


def _check_limits(max_bytes: Any, max_depth: Any) -> None:
    """Validate a ``max_bytes`` / ``max_depth`` pair (``TypeError`` / ``ValueError``)."""
    _check_limit("max_bytes", max_bytes, optional=True)
    _check_limit("max_depth", max_depth, optional=False, ceiling=_MAX_DEPTH_CEILING)


def _decode(data: Any) -> str:
    """The text ``json.loads`` would parse for ``data``."""
    if isinstance(data, str):
        return data
    if isinstance(data, (bytes, bytearray, memoryview)):
        raw = bytes(data)
        return raw.decode(json.detect_encoding(raw), "surrogatepass")
    raise TypeError(
        f"the JSON object must be str, bytes or bytearray, not {type(data).__name__}"
    )


def _size_in_bytes(data: Any, ceiling: int) -> int:
    if isinstance(data, str):
        # Every code point is at least one byte: a longer string is over the
        # ceiling without encoding it.
        if len(data) > ceiling:
            return len(data)
        return len(data.encode("utf-8", "surrogatepass"))
    if isinstance(data, memoryview):
        return data.nbytes
    return len(data)


def _depth_exceeds(text: str, limit: int) -> bool:
    """Whether ``text`` opens more than ``limit`` containers at once.

    Counts ``[`` / ``{`` minus ``]`` / ``}`` outside JSON strings; a backslash
    inside a string escapes the next character. The count never goes below
    zero, so stray closers cannot hide openers that follow them.
    """
    depth = 0
    in_string = False
    escaped = -1
    for match in _STRUCTURAL.finditer(text):
        pos = match.start()
        char = text[pos]
        if in_string:
            if pos == escaped:
                continue
            if char == "\\":
                escaped = pos + 1
            elif char == '"':
                in_string = False
        elif char == '"':
            in_string = True
        elif char in "[{":
            depth += 1
            if depth > limit:
                return True
        elif char in "]}" and depth:
            depth -= 1
    return False


def json_depth_within(data: Union[str, BytesLike], max_depth: int = MAX_JSON_DEPTH) -> bool:
    """Whether ``data`` nests at most ``max_depth`` levels of ``[`` / ``{``.

    Bytes are decoded as ``json.loads`` decodes them (a
    :class:`UnicodeDecodeError`, a :class:`ValueError`, when it could not).
    Never recurses.
    """
    _check_limit("max_depth", max_depth, optional=False, ceiling=_MAX_DEPTH_CEILING)
    return not _depth_exceeds(_decode(data), max_depth)


def loads_bounded(
    data: Union[str, BytesLike],
    *,
    max_bytes: Optional[int] = MAX_CHALLENGE_BYTES,
    max_depth: int = MAX_JSON_DEPTH,
) -> Any:
    """``json.loads(data)``, refusing a document over ``max_bytes`` or ``max_depth``.

    Raises :class:`JSONBoundsError` (``reason`` ``"too-large"`` /
    ``"too-deep"``) before parsing, and whatever ``json.loads`` raises for
    invalid JSON or undecodable bytes; all of them are :class:`ValueError`.
    A :class:`RecursionError` from a caller that is already deep in its own
    stack is reported as ``"too-deep"`` too. ``max_bytes=None`` means no size
    ceiling; there is always a depth ceiling, and ``max_depth`` above
    1000 is a :class:`ValueError`.
    """
    _check_limits(max_bytes, max_depth)
    if not isinstance(data, (str, bytes, bytearray, memoryview)):
        raise TypeError(
            f"the JSON object must be str, bytes or bytearray, not {type(data).__name__}"
        )
    if max_bytes is not None:
        size = _size_in_bytes(data, max_bytes)
        if size > max_bytes:
            raise JSONBoundsError(
                "too-large", f"JSON document of {size} bytes exceeds {max_bytes}"
            )
    text = _decode(data)
    if _depth_exceeds(text, max_depth):
        raise JSONBoundsError(
            "too-deep", f"JSON document nests deeper than {max_depth} levels"
        )
    try:
        return json.loads(text)
    except RecursionError:
        raise JSONBoundsError(
            "too-deep", "JSON document nests deeper than the stack allows"
        ) from None
