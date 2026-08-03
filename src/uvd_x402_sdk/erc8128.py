"""
ERC-8128 Signed HTTP Requests (RFC 9421) — client-side request signer.

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
the Execution Market fleet (pinned by EM F3-1, golden vectors in
``tests/fixtures/erc8128.json``): ``alg="eip191"`` always emitted, keyid
ALWAYS lowercase (``erc8128:{chain_id}:{address}``), signature params in the
order ``created;expires;nonce;keyid;alg``. Byte-equality against the golden
vectors is enforced in ``tests/test_erc8128.py`` — any change that moves
those bytes breaks authentication for every consumer at once.

Reference:
  - ERC-8128: https://eip.tools/eip/8128
  - RFC 9421: https://www.rfc-editor.org/rfc/rfc9421
  - ERC-191: https://eips.ethereum.org/EIPS/eip-191
"""

import base64
import hashlib
import time
from typing import Dict, List, Optional
from urllib.parse import urlparse

import httpx

from uvd_x402_sdk.wallet import WalletAdapter

#: Default label for ERC-8128 signatures
DEFAULT_LABEL = "eth"

#: Signature algorithm parameter — pinned wire format (always emitted)
ALG = "eip191"

#: Default validity window (seconds) — server policy caps at 300
DEFAULT_VALIDITY_SEC = 300

#: keyid chain binding (Base mainnet, the production auth chain)
DEFAULT_CHAIN_ID = 8453


def sign_request(
    wallet: WalletAdapter,
    method: str,
    url: str,
    body: Optional[str] = None,
    nonce: Optional[str] = None,
    chain_id: int = DEFAULT_CHAIN_ID,
    label: str = DEFAULT_LABEL,
    validity_sec: int = DEFAULT_VALIDITY_SEC,
) -> Dict[str, str]:
    """Sign an HTTP request per ERC-8128.

    Args:
        wallet: WalletAdapter that signs the RFC 9421 signature base with
            EIP-191 personal_sign. The key stays inside the adapter.
        method: HTTP method (GET, POST, etc.).
        url: Full URL of the request (authority + path + query are covered).
        body: Request body (for POST/PUT/PATCH). ``None`` for bodyless
            requests. Must be byte-identical to what is sent on the wire —
            the ``Content-Digest`` covers it.
        nonce: Single-use nonce from the server. Required by servers that
            reject replayable signatures (fetch via :func:`fetch_nonce`).
        chain_id: EVM chain ID for the keyid (default: 8453 = Base).
        label: Signature label (default: ``"eth"``).
        validity_sec: Signature validity window in seconds (default: 300).

    Returns:
        Dict with keys ``"Signature"``, ``"Signature-Input"``, and (when a
        body is present) ``"Content-Digest"``. Merge these into the request
        headers before sending.
    """
    address = wallet.get_address().lower()

    parsed = urlparse(url)
    authority = parsed.netloc
    path = parsed.path or "/"
    query = f"?{parsed.query}" if parsed.query else None

    now = int(time.time())
    created = now
    expires = now + validity_sec

    keyid = f"erc8128:{chain_id}:{address}"

    # Determine covered components
    covered = ["@method", "@authority", "@path"]
    if query:
        covered.append("@query")

    extra_headers = {}

    if body is not None:
        digest = _compute_content_digest(body)
        extra_headers["Content-Digest"] = digest
        covered.append("content-digest")

    # Build signature base
    sig_base = _build_signature_base(
        method=method,
        authority=authority,
        path=path,
        query=query,
        content_digest=extra_headers.get("Content-Digest"),
        label=label,
        covered=covered,
        created=created,
        expires=expires,
        nonce=nonce,
        keyid=keyid,
    )

    # EIP-191 personal_sign via the wallet adapter
    sig_hex = wallet.sign_message(sig_base)
    sig_bytes = bytes.fromhex(sig_hex.removeprefix("0x"))

    # Encode signature as base64 (RFC 8941 byte sequence)
    sig_b64 = base64.b64encode(sig_bytes).decode("ascii")

    # Build headers
    sig_params = _build_signature_params(
        covered=covered,
        created=created,
        expires=expires,
        nonce=nonce,
        keyid=keyid,
    )

    extra_headers["Signature"] = f"{label}=:{sig_b64}:"
    extra_headers["Signature-Input"] = f"{label}={sig_params}"

    return extra_headers


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
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        data = resp.json()
        return data["nonce"]


def fetch_nonce_sync(
    api_base: str,
    timeout: float = 10.0,
    allow_local_fallback: bool = False,
) -> "tuple[str, Optional[int]]":
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
        resp = httpx.get(url, timeout=timeout)
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


# =============================================================================
# Internal helpers
# =============================================================================


def _compute_content_digest(body: str) -> str:
    """Compute Content-Digest header value (SHA-256, RFC 9530)."""
    digest = hashlib.sha256(body.encode("utf-8")).digest()
    b64 = base64.b64encode(digest).decode("ascii")
    return f"sha-256=:{b64}:"


def _build_signature_base(
    method: str,
    authority: str,
    path: str,
    query: Optional[str],
    content_digest: Optional[str],
    label: str,
    covered: List[str],
    created: int,
    expires: int,
    nonce: Optional[str],
    keyid: str,
) -> str:
    """Build the RFC 9421 signature base string."""
    lines = []

    for component in covered:
        if component == "@method":
            lines.append(f'"@method": {method.upper()}')
        elif component == "@authority":
            lines.append(f'"@authority": {authority}')
        elif component == "@path":
            lines.append(f'"@path": {path}')
        elif component == "@query":
            lines.append(f'"@query": {query or "?"}')
        elif component == "content-digest":
            lines.append(f'"content-digest": {content_digest or ""}')

    sig_params = _build_signature_params(
        covered=covered,
        created=created,
        expires=expires,
        nonce=nonce,
        keyid=keyid,
    )
    lines.append(f'"@signature-params": {sig_params}')

    return "\n".join(lines)


def _build_signature_params(
    covered: List[str],
    created: int,
    expires: int,
    nonce: Optional[str],
    keyid: str,
) -> str:
    """Build the @signature-params value per RFC 9421."""
    comp_str = " ".join(f'"{c}"' for c in covered)
    parts = [f"({comp_str})"]
    parts.append(f"created={created}")
    parts.append(f"expires={expires}")
    if nonce:
        parts.append(f'nonce="{nonce}"')
    parts.append(f'keyid="{keyid}"')
    parts.append(f'alg="{ALG}"')
    return ";".join(parts)
