---
date: 2026-09-23
tags:
  - type/handoff
  - domain/x402
  - domain/sdk-python
  - priority/p0
status: active
---

# Una liquidación emitida sin veredicto, o un nonce ya gastado, nunca contesta 402 (0.90.1)

**Versión:** 0.90.0 → **0.90.1** (patch: cambia sólo lo que las integraciones le contestan al
comprador). **Toca dinero**: un 402 le dice al comprador "firmá otro pago", y dicho sobre un pago
que pudo moverse es un cobro doble. El tag `v0.90.1` y la publicación en PyPI **no** son parte de
esta rama.

## 0. El hallazgo, medido sobre 0.90.0 (`76fc63e`)

`_undelivered_response()` (`client.py`), por donde pasan los doce puntos de entrada (FastAPI ×4,
Flask ×2, Django ×3, Lambda ×2, `require_payment`), devolvía `None` para todo lo que no fuera un
conflicto del riel de recibos ni transitorio, y la integración contestaba su propio 402 (400 en
`require_payment`). `is_transient_error()` da `False` a propósito para un 5xx con `transaction`
(guarda anti-double-settle) y para `retryable: false`. x402-rs contesta exactamente eso cuando
emitió la transacción y no llegó el recibo: `502 {"error": "settlement_unconfirmed",
"transaction", "paymentId", "retryable": false}` (`src/handlers.rs`, brazo
`SettlementUnconfirmed`; `src/types.rs`, `SettlementUnconfirmedResponse`). Un nonce gastado
legible (`is_spent_nonce_error`) terminaba igual.

Matriz medida con un facilitador sobre socket real (`tests/test_integrations_undelivered.py`,
`Scripted`), las doce columnas en el orden de `SITES`:

| Caso | 0.90.0 | 0.90.1 |
|---|---|---|
| `502 settlement_unconfirmed` + `transaction` + `paymentId` | 402 ×11, 400 | **500** ×12 |
| `500` con `transaction` | 402 ×11, 400 | **500** ×12 |
| `503` con `transaction` y `retryable: true` | 402 ×11, 400 | **500** ×12 |
| `502` con `retryable: false`, sin `transaction` | 402 ×11, 400 | **500** ×12 |
| `400 "Nonce 5 already used for address GABC"` (redacción) | 402 ×11, 400 | **409** ×12 |
| `200 success:false errorReason: nonce_already_used` | 402 ×11, 400 | **409** ×12 |
| `/verify` `invalidReason: …_authorization_nonce_used` | 402 ×11, 400 | **409** ×12 |
| `409 idempotency_key_conflict` | 402 ×11, 400 | **409** ×12 |
| `503 idempotency_cache_corrupt` (transitorio **y** gastado) | 503 ×12 | **409** ×12 |
| firma inválida (`/verify`) | 402 ×11, 400 | 402 ×11, 400 |
| fondos insuficientes (`/verify` y `/settle`) | 402 ×11, 400 | 402 ×11, 400 |
| `400 contract_call_failed (ref)` opaco | 402 ×11, 400 | 402 ×11, 400 |

## 1. Qué entró

| Pieza | Qué |
|---|---|
| `client.py`, `_undelivered_response()` | Orden, primera coincidencia gana: (1) `payment_conflict_response` (409, o 503 en vuelo), sin cambios; (2) una `transaction` en un fallo **no transitorio** → **500**; (3) `spent_nonce_evidence` → **409**; (4) `is_transient_error` → 503 + `Retry-After`, sin cambios; (5) cualquier otro 5xx de `FacilitatorError` → **500**; (6) `None`, el 402/400 de cada integración |
| `client.py`, `_may_have_settled_response()` | El 500: `to_dict()` + `message` ("The payment may have settled: do not sign another one. Check the transaction before paying again.", o la variante sin transacción), `retryable: false`, `safeToReplay: false`, `reason` (el `error` del facilitador), `transaction` y `paymentId` arriba cuando vienen. Sin `Retry-After`. Recibo en `PAYMENT-RESPONSE` si hay |
| `client.py`, `_spent_authorization_response()` | El 409: `to_dict()` + `message`, `retryable: false`, `safeToReplay: false`, `spentNonceEvidence` (`"structured"` / `"wording"`) y el código como `reason` cuando lo dijo un código. Recibo en `PAYMENT-RESPONSE` si hay |
| `client.py`, `_spent_nonce_code_of()` | El lector de código que ya usaba `spent_nonce_evidence`, extraído para que el 409 nombre el mismo código. `spent_nonce_evidence` contesta lo mismo (su tabla contra tarotof sigue verde) |
| Integraciones | Sin cambios de código: todas pasan por `_undelivered_response`. Docstrings y comentarios actualizados |
| `tests/test_integrations_undelivered.py` | 217 tests, los doce puntos de entrada reusados de `test_integrations_replay.py` (§3) |
| Docs | CHANGELOG 0.90.1; README (tabla de respuestas y el ejemplo "check in this order", que ahora es el orden exacto de las integraciones); `docs/facilitator-receipts.md`; CLAUDE.md |

**No cambió:** `is_transient_error()`, `FacilitatorError._retryable_verdict`,
`_is_retryable_settle_error` y la política de reintento. El SDK sigue sin reenviar ninguno de
estos casos (`rail.calls == ["/verify", "/settle"]` en cada test del 500).

### Por qué 500 y no 409 para `settlement_unconfirmed`

Paridad con el SDK de TypeScript 2.98.0: `settlement_unconfirmed` cae a propósito en su rama 500
con `settlementFailureBody` (`src/backend/index.ts:2360-2371` Express, `:2594-2596` Hono;
`src/backend/facilitator-error.ts:44-66` explica por qué nunca 503). Mismo facilitador, misma
respuesta en los dos SDKs. El 409 queda para "esta autorización ya se usó", que es lo que dice
`payment_conflict_response` y el `buildPaymentConflictResponse` de TS.

### Una decisión que conviene mirar: gastado antes que transitorio

El orden sigue la doctrina que ya estaba escrita en `spent_nonce_evidence` (1 hash, 2 gastado,
3 transitorio, 4 final) y en el README. La consecuencia: un fallo transitorio **y** gastado deja
de ser 503 y pasa a 409. El caso real es `503 idempotency_cache_corrupt`: x402-rs sólo lo contesta
cuando hay un settle exitoso de este mismo request que no puede releer. Con 503, el comprador
reenvía el mismo `X-PAYMENT` en un manejo nuevo, con llave nueva (0.89.0); en EVM sin recibos eso
llega a `/verify`, que simula `transferWithAuthorization`, revierte, y vuelve como el `400
contract_call_failed` opaco, o sea **402**. Con 409 el comprador no firma otro. El costo del otro
lado son los falsos positivos baratos de la tabla de tarotof (`cheap-*`: un 5xx que menciona el
nonce de la EOA del facilitador), que pasan de 503 a 409: se pierde una venta, no se cobra dos
veces. Si c0der prefiere el orden viejo, es la mutación M4 de §3 (13 tests la fijan).

Un `202 settlement_in_progress` que nombre una transacción **sigue** siendo 503: la rama del hash
exige un fallo no transitorio, porque reenviar ese 202 bajo su vínculo es la recuperación que
documenta el facilitador (M6).

## 2. Lo que sigue llegando a 402 (límite medido, no de este repo)

El `400 contract_call_failed (ref: <uuid>)` de EVM. El brazo `ContractCall` de x402-rs oculta la
razón del revert a propósito, y la usan tanto una autorización usada como una firma inválida:
nada en el cable las separa, y el encargo pide que la firma inválida siga en 402. Lo fija
`opaque-contract-call-failed` aquí y `test_legacy_a_bare_resend_keeps_the_answer_it_had` en
`test_integrations_replay.py`. Se cierra en el facilitador con un código estable (fila
2026-09-13 de `docs/planning/BACKLOG.md`, "La autorizacion EIP-3009 usada vuelve OPACA"). En redes
con recibos el facilitador ya lo nombra (`authorization_already_settled`, 409).

## 3. Tests y mutaciones

`tests/test_integrations_undelivered.py`, por cada uno de los 12 puntos de entrada:

| Test | Casos | Qué fija |
|---|---|---|
| `test_a_payment_that_may_have_moved_is_500_with_what_to_check` | 4 × 12 | 500, `transaction` / `paymentId` / `reason` / `message`, `retryable` y `safeToReplay` false, sin `Retry-After`, nada entregado, ningún reenvío |
| `test_the_receipt_of_a_payment_that_may_have_moved_travels_in_payment_response` | 12 | el recibo del 502 llega en `PAYMENT-RESPONSE` |
| `test_an_authorization_already_used_is_409_with_the_evidence` | 5 × 12 | 409, `spentNonceEvidence`, `reason` cuando es código, sin `Retry-After` |
| `test_a_rejection_keeps_the_answer_it_had` | 4 × 12 | firma inválida, fondos insuficientes (verify y settle), el opaco: la respuesta de 0.90.0 |
| `test_a_payment_is_still_delivered` | 12 | el camino feliz |
| tres `test_without_…` (monkeypatch) | 3 × 12 | sin cada rama, su caso vuelve a lo de antes (corren en el CI) |
| `test_the_mapping_itself_first_match_wins` | 1 | el orden sin framework, incluido `PaymentSettlementError.tx_hash` |

Mutaciones de fuente (quitar cada rama de `_undelivered_response` y correr el archivo nuevo):

| Mutación | Resultado |
|---|---|
| M1 sin la rama del hash (500 con `transaction`) | **37 rojos** |
| M2 sin la rama del nonce gastado (409) | **61 rojos** |
| M3 sin la rama del 5xx final (500 sin hash) | **25 rojos** |
| M4 orden viejo: transitorio antes que gastado | **13 rojos** |
| M5 el 500 sin el recibo en `PAYMENT-RESPONSE` | **12 rojos** |
| M6 el hash sin mirar si es transitorio (el 202 en vuelo pasa a 500) | **1 rojo** |

Y el archivo nuevo contra el código de `origin/main` (0.90.0): **145 rojos, 72 verdes** (los
verdes son los rechazos, el camino feliz y las mutaciones que esperan 402).

## 4. El SDK de TypeScript 2.98.0 ante el nonce gastado (medido, sólo lectura)

**Sí contesta 402**, en la verificación, en los dos middlewares:

- Express `createPaymentMiddleware`: `src/backend/index.ts:2340` (`res.status(402)`) para todo
  `verify` inválido no reintentable que `buildPaymentConflictResponse` no reconozca.
- Hono `createHonoMiddleware`: `src/backend/index.ts:2579` (`c.json(..., 402)`), mismo criterio.
- `buildPaymentConflictResponse` (`index.ts:2086-2105`) sólo reconoce
  `authorization_already_settled` / `receipt_request_conflict`
  (`isAuthorizationAlreadyUsed`, `facilitator-error.ts:427-432`) o un status 409
  (`index.ts:2094`). TS no tiene un clasificador de nonce gastado (no hay puerto de
  `is_spent_nonce_error`).
- En EVM la autorización usada llega en `/verify` (x402-rs simula `transferWithAuthorization`)
  como `400 contract_call_failed`: TS contesta 402, y Python también (§2, no se puede distinguir).
  Un gastado **legible** en `/verify` (`invalidReason` con `nonce_used`, un 400 "Nonce … already
  used") es 402 en TS y ahora 409 en Python: esa es la divergencia.
- En el settle (`settlementStrategy: 'before-handler'`) TS nunca contesta 402: todo fallo no
  reintentable es 500 (`index.ts:2371` Express, `:2596` Hono), gastado incluido.

Si c0der despacha el TS: portar `spent_nonce_evidence` (con su tabla contra tarotof) y consultarlo
antes del 402 de `:2340` y `:2579`.

## 5. Verificación

- **Suite, línea del CI, venv limpio (Python 3.11.14):** `pip install -e
  '.[signer,dev,dx402,escrow,hedera,fastapi,flask,django]'` + `python -m pytest -q` →
  **1741 passed** (0.90.0: 1524; +217 del archivo nuevo).
- **Cross-language** (el job del CI, contra `uvd-x402-sdk-typescript` `origin/main` `ef9397e`
  exportado a un directorio temporal, venv limpio con `.[signer]`): `derive-erc8128-f3-3.mjs
  --check` exit 0; `cross-language-conformance.mjs` **PASSED, 430 checks en 8 fases**.
- **pre-CI de c0der** (`preci.py --base origin/main`): dispara sólo `ci.yml` (jobs `test` y
  `cross-language`), los dos corridos arriba.
- **ruff / mypy** (el CI no los corre; el repo arrastra 818 / 151 hallazgos en `origin/main`):
  mypy, ninguno nuevo. ruff, el archivo de test nuevo limpio; en `client.py` diez `UP006`
  (`Dict`/`Tuple`), el estilo de las funciones vecinas (`payment_conflict_response`,
  `transient_503_response`).
- **PyPI:** nada del CI resuelve el SDK por nombre. Los dos jobs instalan el checkout
  (`pip install -e .`), y el harness cross-language importa ese checkout. El único
  `uvd-x402-sdk[...]` por nombre es el extra `all` de `pyproject.toml`, que el CI no instala.
- Nada contra el facilitador vivo.

## 6. Lo que queda

1. **x402-rs**: un código estable para la autorización usada en EVM (§2). Es lo único que sigue
   llegando a 402 sobre un pago que pudo moverse.
2. **TypeScript**: el nonce gastado legible en `/verify` (§4).
3. **Paywalls propios**: `_undelivered_response` es privada. Un vendedor que arma su paywall con
   `is_transient_error` + 402 tiene el mismo hueco con `settlement_unconfirmed`. El README trae el orden exacto; exportar la
   función sería un minor (0.91.0), no este patch.
4. Tag `v0.90.1` y publicación: fuera de esta rama.
