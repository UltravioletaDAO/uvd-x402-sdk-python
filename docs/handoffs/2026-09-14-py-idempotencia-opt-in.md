---
date: 2026-09-14
tags:
  - type/handoff
  - domain/x402
  - domain/sdk-python
  - priority/p1
status: active
---

# La `Idempotency-Key` pasa a opt-in y se ata a la compra que nombra el llamador (0.83.1)

**Versión:** 0.83.0 → **0.83.1**. **Toca dinero.** Pide refutador aparte antes del tag.
El tag `v0.83.1` y la publicación en PyPI no son parte de este PR.

## Qué cambió

| Archivo | Qué |
|---|---|
| `src/uvd_x402_sdk/config.py` | `send_idempotency_key: bool = False`, con el porqué en el comentario del campo y en el docstring de `X402Config` |
| `src/uvd_x402_sdk/client.py`, `derive_idempotency_key(payload, op, scope=None)` | Sin `scope`, la llave de 0.83.0 byte a byte (su vector fijado no se movió). Con `scope`, sha256 sobre `{"payload": <bloque firmado>, "scope": <scope>}` con claves ordenadas y sin espacios. `scope` vacío o en blanco levanta `ValueError`; un `scope` que no es str levanta `TypeError` |
| `client.py`, `_facilitator_headers(payload, op, idempotency_scope)` y `_warn_missing_idempotency_scope()` | La llave sale solo con la config prendida **y** un scope. Prendida sin scope (o con uno en blanco): sin header y un `logger.warning` por proceso |
| `client.py`, `verify_payment` / `settle_payment` / `try_settle_payment` / `process_payment` / `_settle_once` | kwarg `idempotency_scope=None`, propagado a los dos pasos. El fallback del timeout reusa los headers del settle, así que pregunta con la misma llave |
| `pyproject.toml` | 0.83.1. `__version__` sale de la metadata del paquete (`tests/test_version_sync.py`); verificado `0.83.1` en un venv nuevo |
| `tests/test_idempotency_opt_in.py` (8, nuevo) | Los cinco puntos del encargo contra un facilitador local con la semántica de `post_settle` de x402-rs |
| `tests/test_idempotency_key.py` (+12) | Segundo vector fijado (con scope), derivación con scope, header con scope, scope en blanco y scope no-str |
| `tests/test_idempotency_local_facilitator.py` | Los vendedores que quieren la llave la piden (`send_idempotency_key=True`) y nombran la compra |
| `README.md`, `CLAUDE.md`, `docs/planning/BACKLOG.md` | Sección de la llave reescrita, changelog 0.83.1, fila 40 actualizada y tres filas nuevas |

**1159 passed, 1 skipped** (base 787add2f: 1139 passed, 1 skipped; 20 agregados, ninguno perdido).

## Por qué, en términos del SDK

Una llave derivada solo del cuerpo no distingue dos compras del mismo precio: el cuerpo del
settle (`_build_payment_requirements`) no lleva nada que identifique la compra, y quién sabe qué
compra es cada pago es el llamador. De ahí las dos decisiones: apagada por defecto, y prendida
solo con un identificador de compra que pasa el llamador en cada llamada.

El bloque firmado se queda dentro de la llave junto al scope. Así, dos vendedores que usan el
mismo id de orden (`"order-1"`) no chocan en el store del facilitador, que es un único namespace.
El costo: una autorización NUEVA para la misma compra sigue trayendo otra llave (fila 40).

El segundo vector (`x402-settle-1243a777…309fe8`) se calculó con `hashlib` sobre el texto
canónico literal, fuera del SDK, igual que el primero.

## Rojo contra 787add2f, verde en la rama

Medido con `git archive 787add2f` en un directorio aparte, con el archivo de tests copiado ahí:
`PYTHONPATH=<base>/src python -m pytest tests/test_idempotency_opt_in.py`. Antes de correrlo se
verificó con `uvd_x402_sdk.client.__file__` que importaba la base y no la rama.

| Test (`tests/test_idempotency_opt_in.py`) | Punto | 787add2f | Rama |
|---|---|---|---|
| `test_by_default_neither_verify_nor_settle_carries_the_key` | (a) | ROJO por aserción: `assert True is False` (default encendido) | verde |
| `test_by_default_a_second_purchase_is_not_answered_from_the_first_ones_cache` | (a) | ROJO por aserción: `DID NOT RAISE FacilitatorError` | verde |
| `test_with_the_key_on_two_scopes_do_not_share_a_cached_settle` | (b) | ROJO por `TypeError`: `process_payment()` no conoce `idempotency_scope` | verde |
| `test_with_the_key_on_the_same_scope_replays_the_settle_that_completed` | (c) | ROJO por `TypeError` (idem) | verde |
| `test_with_the_key_on_and_no_scope_no_key_goes_out_and_it_warns_once` | (d) | ROJO por aserción: sale `x402-verify-…` donde se espera `None` | verde |
| `test_a_conflict_under_the_scoped_key_is_evidence_of_a_settle_not_a_transient` | (e) e2e | ROJO por `TypeError`: `settle_payment()` no conoce `idempotency_scope` | verde |
| `test_an_unreadable_store_is_transient_and_the_same_credential_settles_later` | (e) e2e | ROJO por `TypeError` (idem) | verde |
| `test_a_409_that_is_not_an_idempotency_conflict_is_no_evidence_of_a_settle` | (e) negativo | **verde: pin de regresión** | verde |

**Sobre (e):** la clasificación ya existía en 0.83.0. `idempotencykeyconflict` está en
`_SPENT_NONCE_CODES`, y `tests/test_spent_nonce.py` fija que un 409 `idempotency_key_conflict`
da `"structured"` y que un 503 `idempotency_store_unavailable` es transitorio. Contra la base no
puede haber rojo de clasificación. Los dos e2e fallan ahí solo porque el parámetro no existe, y el
negativo pasa en los dos lados.

## Mutaciones: mueren las 5

Cada una sobre una copia de `src/`, corriendo los tres archivos de tests de la llave.

| Mutación | Tests que se ponen rojos |
|---|---|
| El scope no entra a la llave | `test_the_pinned_scoped_vector`, `test_another_scope_gives_another_key`, (b) |
| Sin scope se manda la llave sin scope | `test_a_blank_scope_is_no_scope[empty]`, `[blank]`, (d) |
| Default encendido | (a) `…_neither_verify_nor_settle_carries_the_key` |
| Un scope en blanco cuenta como scope | `test_a_blank_scope_is_no_scope[empty]`, `[blank]` |
| El aviso sale en cada llamada | (d) |

## Pre-CI

`ci.yml` no filtra por `paths` y hoy está `disabled_manually` en el repo, así que este PR no
dispara nada en GitHub. Igual se corrieron a mano sus dos jobs; la tabla con los comandos
exactos está en el cuerpo del PR.

- `test`: venv nuevo, `pip install -e '.[signer,dev,dx402,escrow]'` y `python -m pytest -q` dan
  1159 passed, 1 skipped.
- `cross-language`: TypeScript `main` 919d580, `npm ci && npm run build`,
  `node scripts/derive-erc8128-f3-3.mjs --check` (al día) y
  `node scripts/xlang/cross-language-conformance.mjs`: 430 checks, PASSED.
- `ruff check` sobre los archivos tocados: mismos hallazgos que la base (client 52, config 17,
  tests 0) y 0 en el test nuevo.

## Verificación de cierre

```text
$ git grep -n "send_idempotency_key: bool" src/
src/uvd_x402_sdk/config.py:152:    send_idempotency_key: bool = False
```

## Para c0der

### Cómo migra un vendedor que quiera prender la llave

1. `X402Client(..., send_idempotency_key=True)`, o el mismo campo en `X402Config`.
2. Pasa `idempotency_scope=<id de la compra>` a `verify_payment()`, a `settle_payment()` o
   `try_settle_payment()`, y a `process_payment()`. Tiene que ser el mismo valor en los dos pasos
   y en cada reintento de esa compra.
3. **Qué scope:** el identificador de la compra u orden que el vendedor crea ANTES del settle y
   guarda con la compra, para que sobreviva un reinicio. Por ejemplo, un UUID v4 por orden.
   - Nunca un valor que compartan dos compras: la URL del recurso, el precio, el pagador, la red,
     una constante o `resource_url`/`description` de la config.
   - Mejor aleatorio que secuencial: la llave es tan privada como el scope.
   - Un scope generado por request HTTP, y no por compra, es seguro pero no desduplica el
     reintento.
4. **Antes de liberar la compra, en este orden:**
   1. Hash de transacción en el error: se transmitió; verificar en cadena.
   2. `spent_nonce_evidence(exc)`: un `409 idempotency_key_conflict` da `"structured"`. No
      liberar, no pedir firma nueva, verificar en cadena.
   3. `is_transient_error(exc)`: un `503 idempotency_store_unavailable` es transitorio. Contestar
      503 al comprador para que presente la misma credencial.
   4. Recién ahí, 402.
5. Quien no quiera la llave no tiene que hacer nada: 0.83.1 no la manda. Un
   `send_idempotency_key=False` puesto a mano ahora es el default; puede quedarse o quitarse.

### La clasificación ya estaba desde 0.83.0

El 409 y el 503 de idempotencia ya se clasificaban bien en 0.83.0 (`_SPENT_NONCE_CODES` en
`client.py`; `tests/test_spent_nonce.py`). Lo pendiente es de los vendedores: llamar a
`spent_nonce_evidence()` e `is_transient_error()` antes de decidir entre 402 y liberar la
compra. Este PR no cambia esa clasificación: la fija de punta a punta por el camino con scope.

### Qué falta

- Refutación, tag `v0.83.1` y publicación.
- El SDK de TypeScript no manda `Idempotency-Key` (`grep -rni idempotency src/` en `main`
  919d580 no devuelve nada). Si la agrega, que sea opt-in con scope y con los dos vectores
  fijados en `tests/test_idempotency_key.py`.
- Las filas de abajo.

### Backlog

| Date | Item | Context | Priority | Status |
|---|---|---|---|---|
| 2026-09-13 | Llave de idempotencia por COMPRA, no solo por autorización (fila 40) | Ya existe `idempotency_scope`, mezclado con el bloque firmado. Sigue abierta la mitad que da nombre a la fila: una autorización nueva para la misma compra trae otra llave. Cerrarla pide una llave sin el bloque, acotada por `payTo` | P1 | PARCIAL 2026-09-14 |
| 2026-09-14 | Las integraciones no pasan `idempotency_scope` | `decorators.py` y las integraciones de Flask, Django y Lambda llaman `process_payment()` sin scope: con la llave prendida no la mandan y loguean el aviso | P2 | ABIERTA |
| 2026-09-14 | `transient_503_response()` no marca `safeToRetry` en `503 idempotency_store_unavailable` | El cuerpo trae `error` y no `reason`, y `safeToRetry` solo se escribe cuando hay `reason`; ese 503 no ejecutó nada | P2 | ABIERTA |
| 2026-09-14 | `verify_only()` no acepta `idempotency_scope` | Con la llave prendida loguea el aviso; `/verify` ignora el header hoy | P3 | ABIERTA |
