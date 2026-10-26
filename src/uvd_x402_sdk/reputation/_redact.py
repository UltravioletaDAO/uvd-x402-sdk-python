"""Log redaction: what this module writes to a log never identifies anyone.

The operator streams live. A full wallet address, a transaction hash, a
signature, a proof's ``paymentHash`` or an RPC URL (which may carry an API key,
even in the host) must never reach a log line or an exception message. Every
log call of the module goes through :func:`log_event`, and every error message
through :func:`redact`.

What is kept is enough to correlate two lines of the same run: the first six
and last four characters of a hex value (``0x19E7...ff2A``), the scheme of a URL.
ASCII on purpose: a Windows console in cp1252 cannot encode an ellipsis, and a
log handler that fails to write is a lost line.
"""

from __future__ import annotations

import logging
import re
from typing import Any

LOGGER = logging.getLogger("uvd_x402_sdk.reputation")

# 0x + 16 or more hex digits: addresses (40), hashes and nonces (64),
# signatures (130), raw transactions. Shorter hex (a chain id, a small number
# written as hex) carries no identity and is kept.
_LONG_HEX = re.compile(r"0[xX][0-9a-fA-F]{16,}")
# Bare 64-hex values without the prefix (a hexbytes .hex() of a tx hash).
_BARE_HEX = re.compile(r"(?<![0-9a-zA-Z])[0-9a-fA-F]{40,}(?![0-9a-zA-Z])")
_URL = re.compile(r"\b([a-zA-Z][a-zA-Z0-9+.-]*)://[^\s\"'<>]+")
# httpcore at DEBUG: ``connect_tcp.started host='<host>' port=443`` and
# ``start_tls.started ... server_hostname='<host>'``.
_HOST = re.compile(r"\b((?:server_)?host(?:name)?=)['\"][^'\"]*['\"]")


def _short(match: re.Match[str]) -> str:
    text = match.group(0)
    return text[:6] + "..." + text[-4:]


def redact(value: Any) -> str:
    """``str(value)`` with long hex shortened and URLs reduced to their scheme."""
    text = str(value)
    text = _URL.sub(lambda m: m.group(1) + "://...", text)
    text = _HOST.sub(lambda m: m.group(1) + "'...'", text)
    text = _LONG_HEX.sub(_short, text)
    return _BARE_HEX.sub(_short, text)


class _RedactingFilter(logging.Filter):
    """Rewrites a record's message through :func:`redact` (never drops it)."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 - a malformed record is left as is
            return True
        record.msg, record.args = redact(message), None
        return True


_HTTP_FILTER = _RedactingFilter()
#: httpx logs ``HTTP Request: POST <full url>`` at INFO, and httpcore the host
#: at DEBUG: an RPC URL carries its API key, sometimes in the host itself.
_HTTP_LOGGERS = (
    "httpx",
    "httpcore",
    "httpcore.connection",
    "httpcore.http11",
    "httpcore.http2",
    "httpcore.proxy",
)


def redact_http_client_logs() -> None:
    """Install the redacting filter on the HTTP client's loggers (idempotent)."""
    for name in _HTTP_LOGGERS:
        target = logging.getLogger(name)
        if _HTTP_FILTER not in target.filters:
            target.addFilter(_HTTP_FILTER)


def log_event(level: int, event: str, **fields: Any) -> None:
    """One structured line: ``event k=v ...``, every value redacted."""
    if not LOGGER.isEnabledFor(level):
        return
    parts = [event] + [f"{key}={redact(value)}" for key, value in sorted(fields.items())]
    LOGGER.log(level, " ".join(parts))
