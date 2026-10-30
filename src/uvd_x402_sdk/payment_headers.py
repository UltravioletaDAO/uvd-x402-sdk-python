"""Select one payment credential before verification, settlement or binding."""

from collections.abc import Iterable
from typing import Any, Optional

from uvd_x402_sdk.exceptions import InvalidPayloadError, X402Error


def select_payment_header(
    headers: Iterable[tuple[str, Any]],
    *,
    header_name: str = "X-PAYMENT",
) -> Optional[str]:
    """Accept either standard header, or equivalent values in both.

    Names are case insensitive. Only surrounding HTTP optional whitespace
    (SP/HTAB) is ignored when comparing values. Preserve the original
    credential from the preferred alias (header_name): existing purchase bindings
    hash those exact bytes. Binding, verification and settlement receive the
    same credential. Empty or
    non-string values are malformed, not absent credentials. A configured
    custom header is an additional alias with the same conflict rule.
    """
    names = {"x-payment", "payment-signature", header_name.lower()}
    selected: Optional[str] = None
    canonical: Optional[str] = None
    selected_name: Optional[str] = None
    preferred = header_name.lower()
    for name, value in headers:
        if name.lower() not in names:
            continue
        if not isinstance(value, str):
            raise InvalidPayloadError("Payment header must be a non-empty string")
        comparable = value.strip(" \t")
        if not comparable:
            raise InvalidPayloadError("Payment header must be a non-empty string")
        if selected is not None and comparable != canonical:
            raise InvalidPayloadError("Conflicting payment headers: provide a single payment")
        if selected is None or (name.lower() == preferred and selected_name != preferred):
            selected = value
            canonical = comparable
            selected_name = name.lower()
    return selected


def event_payment_header(event: dict[str, Any], *, header_name: str = "X-PAYMENT") -> Optional[str]:
    """Read API Gateway v1/v2, including REST API multi-value headers."""
    headers = list((event.get("headers") or {}).items())
    for name, values in (event.get("multiValueHeaders") or {}).items():
        for value in values or [""]:
            headers.append((name, value))
    return select_payment_header(headers, header_name=header_name)


def payment_input_error_response(
    error: X402Error,
) -> Optional[tuple[int, dict[str, Any], dict[str, str]]]:
    """Malformed credentials are HTTP 400 in every server integration."""
    if isinstance(error, InvalidPayloadError):
        return 400, error.to_dict(), {"Content-Type": "application/json"}
    return None
