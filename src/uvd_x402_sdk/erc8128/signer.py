"""ERC-8128 Signed HTTP Requests (RFC 9421) — client-side request signer.

Signs HTTP requests per ERC-8128 (Signed HTTP Requests with Ethereum) so
agents can authenticate against wallet-signed APIs — most notably Execution
Market, where API keys are rejected in production and only wallet signing is
accepted. The private key never leaves the
:class:`~uvd_x402_sdk.wallet.WalletAdapter`; only ``get_address()`` and
``sign_message()`` (EIP-191 personal_sign) are used.

Flow:
  1. Fetch a fresh single-use nonce from ``GET /api/v1/auth/erc8128/nonce``
  2. Build the RFC 9421 signature base from request components
  3. Sign with EIP-191 personal_sign via the WalletAdapter
  4. Produce ``Signature`` + ``Signature-Input`` (+ ``Content-Digest``) headers

Example:
    >>> from uvd_x402_sdk.wallet import EnvKeyAdapter
    >>> from uvd_x402_sdk.erc8128 import fetch_nonce, sign_request
    >>>
    >>> wallet = EnvKeyAdapter()
    >>> nonce = await fetch_nonce("https://api.execution.market")
    >>> headers = sign_request(
    ...     wallet,
    ...     method="POST",
    ...     url="https://api.execution.market/api/v1/tasks",
    ...     body='{"title": "test"}',
    ...     nonce=nonce,
    ... )
    >>> # headers = {"Signature": "...", "Signature-Input": "...", "Content-Digest": "..."}

The server nonce is single-use and expires after 5 minutes — fetch a fresh
one per signed request, including on retries (the server consumes the nonce
before verification, so a retried request signed with a spent nonce fails).

**Wire format — PINNED, do not change.** This is the canonical wire format of
the Execution Market fleet (pinned by EM F3-1, golden vectors shipped inside
this package as ``erc8128.f3-1.json``): ``alg="eip191"`` always emitted, keyid
ALWAYS lowercase (``erc8128:{chain_id}:{address}``), signature params in the
order ``created;expires;nonce;keyid;alg``. Byte-equality against the golden
vectors is enforced in ``tests/test_erc8128.py`` — any change that moves
those bytes breaks authentication for every consumer at once.

Reference:
  - ERC-8128: https://eip.tools/eip/8128
  - RFC 9421: https://www.rfc-editor.org/rfc/rfc9421
  - ERC-191: https://eips.ethereum.org/EIPS/eip-191
"""

from __future__ import annotations

import base64
import sys
import time
from typing import Any, Callable, Dict, Optional, Tuple, Union
from urllib.parse import urlsplit

import httpx

from uvd_x402_sdk.erc8128.core import (
    ALG,
    DEFAULT_CHAIN_ID,
    DEFAULT_LABEL,
    DEFAULT_VALIDITY_SEC,
    CanonicalMessage,
    build_signature_base,
    build_signature_params,
    canonical_keyid,
    canonical_params,
    compute_content_digest,
    normalize_authority,
    select_covered,
)
from uvd_x402_sdk.wallet import WalletAdapter

_HEADER_NAMES = {
    "title": ("Signature", "Signature-Input", "Content-Digest"),
    "lower": ("signature", "signature-input", "content-digest"),
}

# ── frozen-clock / fake-httpx compatibility ─────────────────────────────────
# ``uvd_x402_sdk.erc8128`` was a flat MODULE before this package existed, and
# suites freeze the clock (or fake httpx) by patching the attribute on THAT
# name — including em-plugin-sdk's cross-repo parity test, which asserts byte
# equality of the emitted headers against the golden vectors. Resolving both
# through the package namespace keeps that contract intact after the move.
# Patching this module's own globals keeps working too, and `now=` (below) is
# the way new code should inject a clock.
_REAL_TIME = time
_REAL_HTTPX = httpx


def _package_override(name: str, real: Any) -> Optional[Any]:
    override = getattr(sys.modules.get(__package__ or ""), name, None)
    return override if override is not None and override is not real else None


def _now_seconds() -> int:
    return int((_package_override("time", _REAL_TIME) or time).time())


def _http() -> Any:
    return _package_override("httpx", _REAL_HTTPX) or httpx


def sign_request(
    wallet: WalletAdapter,
    method: str,
    url: str,
    body: Optional[Union[str, bytes]] = None,
    nonce: Optional[str] = None,
    chain_id: int = DEFAULT_CHAIN_ID,
    label: str = DEFAULT_LABEL,
    validity_sec: int = DEFAULT_VALIDITY_SEC,
    *,
    profile: str = "canonical",
    content_digest: str = "body-present",
    header_case: str = "title",
    now: Optional[Callable[[], int]] = None,
) -> Dict[str, str]:
    """Sign an HTTP request per ERC-8128.

    Args:
        wallet: WalletAdapter that signs the RFC 9421 signature base with
            EIP-191 personal_sign. The key stays inside the adapter.
        method: HTTP method (GET, POST, etc.).
        url: Full URL of the request (authority + path + query are covered).
            The authority is normalised per RFC 9421 §2.2.3 — lowercased, with
            the scheme's default port dropped; a non-default port is kept.
        body: Request body (for POST/PUT/PATCH). ``None`` for bodyless
            requests. Must be byte-identical to what is sent on the wire —
            the ``Content-Digest`` covers it.
        nonce: Single-use nonce from the server. Required by servers that
            reject replayable signatures (fetch via :func:`fetch_nonce`).
        chain_id: EVM chain ID for the keyid (default: 8453 = Base).
        label: Signature label (default: ``"eth"``).
        validity_sec: Signature validity window in seconds (default and cap:
            300 — both live verifiers reject a wider window as stale, so a
            larger value is clamped rather than silently unauthenticated).
        profile: ``"canonical"`` (emit ``alg="eip191"``) or
            ``"legacy-no-alg"``. The legacy profile exists only so a signer
            can be migrated to the SDK in a byte-identical commit BEFORE the
            commit that flips the wire.
        content_digest: ``"body-present"`` (digest whenever ``body is not
            None``, including an empty string — the canonical rule) or
            ``"body-truthy"`` (digest only a non-empty body, the rule two
            older EM copies emit; pin it to keep their bytes unchanged).
        header_case: ``"title"`` or ``"lower"``. Callers that merge these
            into a lowercase header dict need ``"lower"``, otherwise the
            merge silently keeps BOTH spellings.
        now: Injectable clock returning epoch seconds. Exists so a test does
            not have to monkeypatch ``time`` in this module's globals.

    Returns:
        Dict with the ``Signature`` and ``Signature-Input`` headers, plus
        ``Content-Digest`` when a body is present. Merge these into the
        request headers before sending.
    """
    if header_case not in _HEADER_NAMES:
        raise ValueError(f"header_case must be 'title' or 'lower', got {header_case!r}")
    if profile not in ("canonical", "legacy-no-alg"):
        raise ValueError(f"unknown wire profile: {profile!r}")
    if content_digest not in ("body-present", "body-truthy"):
        raise ValueError(f"unknown content_digest rule: {content_digest!r}")

    sig_name, sig_input_name, digest_name = _HEADER_NAMES[header_case]

    address = wallet.get_address().lower()
    keyid = canonical_keyid(chain_id, address)

    parsed = urlsplit(url)
    # NORMALISED, per RFC 9421 §2.2.3 — never the verbatim netloc. `netloc`
    # keeps an explicit `:443`, and a verifier compares against the authority a
    # server actually sees (Host, without the default port), so the same
    # request written two ways would otherwise produce two signatures, one of
    # which nothing verifies. The rule is idempotent, so already-normalised
    # URLs — every live one — sign the same bytes as before.
    authority = normalize_authority(parsed.netloc, parsed.scheme)
    path = parsed.path or "/"
    query = f"?{parsed.query}" if parsed.query else None

    created = int(now()) if now is not None else _now_seconds()
    expires = created + min(int(validity_sec), DEFAULT_VALIDITY_SEC)

    has_digest = body is not None if content_digest == "body-present" else bool(body)
    covered = select_covered(url, has_digest)

    headers: Dict[str, str] = {}
    digest_value: Optional[str] = None
    if has_digest:
        digest_value = compute_content_digest(body if body is not None else "")
        headers[digest_name] = digest_value

    params = canonical_params(
        created=created,
        expires=expires,
        keyid=keyid,
        nonce=nonce,
        alg=ALG if profile == "canonical" else None,
    )

    sig_base = build_signature_base(
        CanonicalMessage(
            method=method,
            authority=authority,
            path=path,
            query=query,
            content_digest=digest_value,
            covered=covered,
            params=params,
        )
    )

    # EIP-191 personal_sign via the wallet adapter
    sig_hex = wallet.sign_message(sig_base)
    sig_bytes = bytes.fromhex(sig_hex[2:] if sig_hex.startswith("0x") else sig_hex)

    # RFC 8941 byte sequence
    sig_b64 = base64.b64encode(sig_bytes).decode("ascii")

    headers[sig_name] = f"{label}=:{sig_b64}:"
    headers[sig_input_name] = f"{label}={build_signature_params(covered, params)}"

    return headers


async def fetch_nonce(api_base: str, timeout: float = 10.0) -> str:
    """Fetch a fresh single-use nonce from the server.

    Args:
        api_base: Origin of the API (e.g., ``"https://api.execution.market"``)
            — the ``/api/v1`` prefix is appended here.
        timeout: Request timeout in seconds.

    Returns:
        The nonce value (single-use, 5-minute TTL).
    """
    url = f"{api_base.rstrip('/')}/api/v1/auth/erc8128/nonce"
    async with _http().AsyncClient(timeout=timeout) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        data = resp.json()
        nonce: str = data["nonce"]
        return nonce


def fetch_nonce_sync(
    api_base: str,
    timeout: float = 10.0,
    allow_local_fallback: bool = False,
) -> Tuple[str, Optional[int]]:
    """Igual que :func:`fetch_nonce` pero SÍNCRONO, y devolviendo el TTL.

    POR QUÉ EXISTE (el async sigue siendo el principal): un consumidor con un camino
    de ejecución síncrono —una herramienta que corre dentro de un bucle de agente, por
    ejemplo— tendría que hacer ``asyncio.run`` por cada request firmado sólo para pedir
    un nonce. Eso abre y cierra un event loop por llamada, y revienta directamente si
    ya hay uno corriendo. Un SDK que obliga a eso empuja a que cada consumidor
    reimplemente el fetch, que es exactamente lo que pasó.

    Devuelve ``(nonce, ttl_segundos)``. El TTL viene del servidor cuando lo informa y
    es ``None`` cuando no: sirve para no re-pedir un nonce que todavía sirve, pero
    **nunca se adivina** — un TTL inventado hace reusar un nonce ya consumido, y ese
    fallo se ve como un problema de firma, no de nonce.

    ``allow_local_fallback`` genera un nonce local si el endpoint no responde. Está
    APAGADO por defecto a propósito: sólo sirve contra servidores que aceptan nonce del
    cliente. Encenderlo contra uno que exige nonce propio convierte "el endpoint está
    caído" en "tu firma es inválida", que es un diagnóstico mucho peor.
    """
    url = f"{api_base.rstrip('/')}/api/v1/auth/erc8128/nonce"
    try:
        resp = _http().get(url, timeout=timeout)
        resp.raise_for_status()
        data = resp.json()
        nonce = data.get("nonce")
        if nonce:
            ttl = data.get("ttl") or data.get("expires_in")
            return str(nonce), (int(ttl) if isinstance(ttl, int) else None)
    except Exception:  # noqa: BLE001 - abajo se decide qué hacer con la falta
        pass
    if not allow_local_fallback:
        raise RuntimeError(
            f"no pude obtener un nonce de {url} y el fallback local está apagado. "
            f"Encendelo SOLO si este servidor acepta nonce del cliente."
        )
    import secrets as _secrets

    return _secrets.token_hex(16), None


__all__ = ["fetch_nonce", "fetch_nonce_sync", "sign_request"]
