---
date: 2026-09-13
tags:
  - type/handoff
  - domain/x402
  - domain/sdk-python
  - priority/p1
status: active
---

# Idempotency-Key en verify/settle, el nonce gastado y el extra `[fastapi]` (0.83.0)

**Versión:** 0.82.0 → **0.83.0** (minor: dos funciones públicas nuevas y un campo de
config). **Toca dinero.** Pide refutador aparte antes del merge.

Tres filas del triage del 2026-09-13 sobre el mismo paquete: 88 (mitad Python), 63 y
98. Las tres quedan **cerradas**. En dos de ellas la evidencia del triage estaba
corrida o era inexacta, y además medí dos cosas que el spec no traía y que cambian
lo que cada arreglo puede prometer. Todo está abajo, con archivo:línea.

## Lo que cambió

| Archivo | Qué |
|---|---|
| `src/uvd_x402_sdk/client.py:95-160` | `IDEMPOTENCY_KEY_HEADER` y `derive_idempotency_key(payload, op)` |
| `src/uvd_x402_sdk/client.py:315-432` | `spent_nonce_evidence()` / `is_spent_nonce_error()` (port de tarotof, con dos correcciones) |
| `src/uvd_x402_sdk/client.py:1029` | `X402Client._facilitator_headers()`; lo usan `verify_payment`, `_settle_once` y `_check_settle_fallback` |
| `src/uvd_x402_sdk/config.py:142-147` | `X402Config.send_idempotency_key: bool = True` (interruptor) |
| `src/uvd_x402_sdk/__init__.py:75-80, 477-480` | los cuatro exports nuevos |
| `pyproject.toml` | versión 0.83.0; `eth-account>=0.11.0` en el extra `fastapi` |
| `scripts/smoke_fastapi_extra.py` | venv nuevo + solo `[fastapi]` + vectores de verificación ERC-8128 |
| `tests/test_idempotency_key.py` (12) | derivación, vector fijado para TS, header en el cable, interruptor, fallback |
| `tests/test_idempotency_local_facilitator.py` (4) | el contrato del facilitador sobre un socket real |
| `tests/test_spent_nonce.py` (17) | los casos de tarotof + lo medido contra x402-rs |
| `tests/test_extras.py` (1) | el extra declara lo que el verificador importa |
| `README.md`, `CLAUDE.md`, `docs/planning/BACKLOG.md` | sección nueva, changelog 0.83.0, cinco filas de backlog |

**1086 passed, 1 skipped** (base 0.82.0: 1052 passed, 1 skipped; 34 agregados, ninguno
perdido).

## Fila 88 — Idempotency-Key en verify y settle (mitad Python): CERRADA

### Lo que hace el facilitador, re-medido en x402-rs `origin/main` 331d31d4 (WSL, solo lectura)

- **La evidencia del triage estaba corrida.** Decía `handlers.rs:2929-2933`; en 331d31d4
  el header se lee en `src/handlers.rs:4295-4299` (`post_settle`, que empieza en `:4269`).
- **El facilitador NO deriva ninguna llave: acepta la del llamador.** Si no llega el
  header, no hay deduplicación. La "derivación en el facilitador" que nombra la fila es
  trabajo de x402-rs, no algo que ya exista.
- Hashea el cuerpo CRUDO: `sha256(body_str)` (`:4368-4374`, `idempotency_store.rs:171`,
  sin re-encodear JSON).
- Misma llave + mismo hash → la respuesta cacheada, `200` con `Idempotent-Replayed: true`,
  y no ejecuta nada (`:4392-4424`).
- Misma llave + otro hash → `409 {"error": "idempotency_key_conflict"}` (`:4427-4440`).
- Solo cachea un settle **exitoso** (`:5146-5152`); TTL 24 h (`idempotency_store.rs:109`).
- Store ilegible → `503 idempotency_store_unavailable` y **no liquida** (`:4447-4461`,
  fail-closed a propósito). Producción usa DynamoDB: `IDEMPOTENCY_TABLE_NAME` está en
  `terraform/environments/production/main.tf:953`.
- `/verify` ignora el header: `post_verify` (`:3512`) no tiene ninguna línea de
  idempotencia.

### Lo que hace el SDK

`verify_payment()` y `settle_payment()` mandan
`Idempotency-Key: x402-<verify|settle>-<sha256>`. El sha256 va sobre el bloque firmado
`payload.payload`, serializado con claves ordenadas y sin espacios. Tres decisiones, cada
una fijada por una mutación que pone rojo un test con nombre:

1. **Estable por autorización** (lo que pedía el spec): el mismo `X-PAYMENT` da la misma
   llave en cualquier proceso, aunque el vendedor haya reiniciado.
2. **Separada por operación.** El store es un solo namespace y verify y settle cargan el
   mismo payload. Una sola llave para los dos dejaría que una futura cache de verify le
   contestara a un settle.
3. **Solo el bloque firmado, no los requirements.** La misma autorización con otros
   términos cae en la misma llave, y el facilitador contesta `409` en vez de ejecutarla.

Además, el fallback del timeout vuelve a preguntar con **la misma llave**: si el settle
terminó mientras el cliente esperaba, la respuesta sale de la cache.

**Por qué no es adivinable:** el MCP del propio facilitador pide una llave inadivinable
porque el store es de todos los llamadores. Esta solo se puede calcular desde el
`X-PAYMENT`, y quien lo tenga podría liquidar ese pago de todos modos.

### Dos cosas medidas que el spec no traía

- **Por qué importa en EVM.** Nuestro facilitador devuelve una autorización EIP-3009 ya
  usada como `400 {"error": "contract_call_failed (ref: <uuid>)"}`, opaco a propósito
  (brazo `ContractCall` de `impl IntoResponse for FacilitatorLocalError`, en
  `handlers.rs`). Sin la llave, el reintento de un pago que ya se movió se lee como
  "rechazado" y un paywall contesta 402: firmá otra vez.
- **`process_payment()` no llega a la cache con una credencial ya liquidada.** En EVM
  `/verify` simula `transferWithAuthorization` con `.call()` (`src/chain/evm.rs:1271-1406`)
  y no tiene cache, así que la autorización usada revierte en verify, antes del settle.
  El replay les sirve a quienes llaman `settle_payment` directo, a `retry=True` y al
  fallback del timeout. Cerrarlo es trabajo del facilitador (fila de backlog abajo).

### Cómo se prueba (y qué NO es)

`tests/test_idempotency_local_facilitator.py` levanta un servidor HTTP real con el
contrato de arriba y le pega con el cliente HTTP real del SDK, así que los bytes que se
hashean son los que el SDK puso en el cable. **No es el binario de x402-rs.** Correrlo
local necesita RPC y un signer con fondos, y su propio handoff
(`docs/handoffs/2026-09-02-mcp-listo.md`) registra que `POST /settle` se cuelga en local
sin credenciales de AWS. El lado del facilitador lo cubren sus propios
`settle_idempotency_tests` (`handlers.rs:13111`), y el stub copia sus aserciones.

**Rojo en 0.82.0** (el archivo solo importa API que ya existía):

```
test_a_repeated_settle_of_the_same_authorization_executes_once
  AssertionError: the retry reached the chain: {'success': False, ...
  'error_code': 'contract_call_failed (ref: local)', ...}   assert 2 == 1
test_the_same_authorization_under_other_terms_is_refused_not_executed   assert 2 == 1
test_verify_and_settle_carry_different_keys_for_the_same_authorization  assert (None)
test_with_the_key_switched_off_the_retry_reaches_the_chain
  TypeError: X402Config.__init__() got an unexpected keyword argument 'send_idempotency_key'
```

**Verde en 0.83.0.** Comando `cierra:` de la fila, corrido:

```
$ git grep -n "Idempotency-Key" -- src/
src/uvd_x402_sdk/client.py:95:# Idempotency-Key (sent on /verify and /settle unless the config turns it off)
src/uvd_x402_sdk/client.py:112:IDEMPOTENCY_KEY_HEADER = "Idempotency-Key"
src/uvd_x402_sdk/client.py:118:    """The ``Idempotency-Key`` this SDK sends for ``operation`` on ``payload``.
src/uvd_x402_sdk/client.py:340:        # SUCCESSFUL settle under an Idempotency-Key, so a conflict means a
src/uvd_x402_sdk/client.py:1030:        """JSON content type, plus the ``Idempotency-Key`` for ``operation``.
src/uvd_x402_sdk/client.py:1389:                the same ``Idempotency-Key`` a settle that completed is
src/uvd_x402_sdk/config.py:87:        send_idempotency_key: Send an ``Idempotency-Key`` derived from the
src/uvd_x402_sdk/config.py:142:    # Send an Idempotency-Key on /verify and /settle, derived from the signed
```

La mitad "versión publicada por tag" queda para c0der: el tag no lo pongo yo.

**Vector para el SDK de TypeScript** (`TestDerive::test_the_pinned_vector`): el bloque
`{"authorization":{"from":"0xSender","nonce":"0x01","to":"0x1234567890123456789012345678901234567890","validAfter":"0","validBefore":"9999999999","value":"10000"},"signature":"0xsig"}`
da `x402-settle-6b62041bcbd7d49d05741f8bcd646f73ab239a76582b119c49e9679dc1cf6c1d`. El
digest se calculó sobre el texto literal, no con este SDK.

## Fila 63 — el clasificador de nonce gastado sube de tarotof: CERRADA (con dos correcciones)

La evidencia se sostiene: `tarotof/api/main.py:166` (`_codigo_de_nonce_gastado`) y `:188`
(`_huele_a_nonce_gastado`), con sus tests en `api/test_api.py:563` y `:650`. Portado como
`spent_nonce_evidence(exc) -> "structured" | "wording" | None` e
`is_spent_nonce_error(exc) -> bool`, junto a `is_transient_error`. Primero el código
(normalizado, en diccionarios y listas anidados), después el texto. Se portaron los dos
tests pedidos y el resto de los casos de tarotof.

**Corrección 1, medida: la heurística de texto de tarotof busca SUBCADENAS, y "refused"
contiene "used".** El mensaje transitorio del propio facilitador para
`Reason::NonceOrMempool` (`src/chain/failure.rs`, `client_message()`), *"The node refused
this transaction on nonce or mempool grounds and never queued it. Retry later."*, sale
como nonce gastado. Le diría al comprador "no reintentes, quizá ya pagaste" sobre algo que
nunca se encoló. El port compara por palabra completa (`_` y `-` cuentan como separador,
así que `nonce_used` sigue siendo dos palabras) y lo fija con ese mensaje y con
`"the nonce is unused"`. En la mutación "subcadena como tarotof" esos dos tests se ponen
rojos.

**Corrección 2: `idempotency_key_conflict` entra al conjunto de códigos.** El facilitador
solo cachea éxitos, así que con la llave derivada un 409 de conflicto significa "esta
autorización ya se liquidó". Leído como un 4xx cualquiera, un paywall lo vuelve 402 y el
comprador paga dos veces. Sin esto, la fila 88 abría ese camino.

**Límite medido:** en EVM, contra nuestro facilitador, la autorización usada vuelve opaca
(`contract_call_failed`), así que ni el código ni el texto la ven. El clasificador sirve
para otros facilitadores, para los rechazos del nonce store de las cadenas no EVM (p. ej.
Stellar, `NonceReused`: *"Nonce {nonce} already used for address {from}"*,
`src/chain/stellar.rs:75`; el render en el cable no lo medí) y para el 409 de conflicto.

## Fila 98 — el extra `[fastapi]` y el verificador ERC-8128: CERRADA (premisa corregida)

- **Lo inexacto:** "el extra `[fastapi]` expone el verificador". No lo expone: la
  integración FastAPI no toca ERC-8128 (`grep -rn "erc8128\|verify_request\|eth_account"
  src/uvd_x402_sdk/integrations/` da vacío).
- **Lo cierto, y lo que importa:** `verify_request` se importa en cualquier instalación, y
  hace `from eth_account import Account` adentro de `_recover` (`erc8128/verifier.py:340`).
  Un servidor FastAPI instalado solo con `[fastapi]` importa bien y revienta en el primer
  request firmado. En runtime, no al importar.

Comando `cierra:`, cada corrida en un venv nuevo con solo ese extra:

```
$ python scripts/smoke_fastapi_extra.py <checkout 0.82.0>
  File ".../uvd_x402_sdk/erc8128/verifier.py", line 395, in verify_request
  File ".../uvd_x402_sdk/erc8128/verifier.py", line 486, in _verify
  File ".../uvd_x402_sdk/erc8128/verifier.py", line 340, in _recover
ModuleNotFoundError: No module named 'eth_account'
exit=1

$ python scripts/smoke_fastapi_extra.py                      # este checkout
verify vectors: 77 passed, 0 failed
exit=0

$ python scripts/smoke_fastapi_extra.py dist/uvd_x402_sdk-0.83.0-py3-none-any.whl
verify vectors: 77 passed, 0 failed
exit=0
```

El script no entró como job del CI para no sumar minutos de Actions en cada PR. Lo que sí
corre en el CI es `tests/test_extras.py`, que en 0.82.0 da rojo:
`assert 'eth-account' in {'fastapi', 'starlette'}`.

## Pre-CI

| Paso | Comando | Resultado |
|---|---|---|
| `ci.yml:25` | `pip install -e '.[signer,dev,dx402,escrow]'` en venv nuevo, Python 3.11 | ok |
| `ci.yml:28` | `python -m pytest -q` | **1086 passed, 1 skipped** (base: 1052 passed, 1 skipped) |
| `ci.yml:36-83` | cross-language: clon limpio de `uvd-x402-sdk-typescript` (`919d580`), `npm ci` + `npm run build`, `derive-erc8128-f3-3.mjs --check` y `xlang/cross-language-conformance.mjs` con `UVD_X402_PY_ROOT` apuntando a este checkout | vectores `up to date`, exit 0; **CROSS-LANGUAGE CONFORMANCE PASSED — 430 checks across 8 phases**, exit 0 |
| lint (no está en el CI) | `ruff check` sobre los archivos nuevos | limpio. En `client.py` 50 → 52: dos `UP006` (`Dict`) en firmas nuevas, con el mismo estilo que los otros 50 |
| build | `pip wheel --no-deps .` | `uvd_x402_sdk-0.83.0-py3-none-any.whl` |
| humo del extra | `scripts/smoke_fastapi_extra.py` (0.82.0 / checkout / wheel) | exit 1 / 77 passed / 77 passed |
| mutaciones | 8 dirigidas sobre una copia de `src/` | **0 sobreviven** |

Las 8 mutaciones, cada una con el test que la mata: la heurística por subcadena
(`..._transaction_nonce_is_not_the_payers_authorization`, `test_unused_is_not_used`),
`idempotency_key_conflict` fuera del conjunto, el código reportado como texto (8 tests),
sin separación por operación, la llave sobre el sobre entero y no sobre el bloque
firmado, settle sin header (5 tests), el fallback sin la llave y el interruptor ignorado.

## Cómo verificarlo en producción (después del tag)

```bash
pip install "uvd-x402-sdk[fastapi]==0.83.0"
python -c "from uvd_x402_sdk import derive_idempotency_key, is_spent_nonce_error, IDEMPOTENCY_KEY_HEADER; print('ok')"
python -c "from uvd_x402_sdk.erc8128 import run_conformance; r = run_conformance(only='verify'); print(r.passed, len(r.failed))"   # 77 0
```

En el facilitador: después de que un consumidor adopte 0.83.0, sus logs de `/settle`
empiezan a traer el campo `idempotency_key` (`Idempotency cache miss` en el primer
settle, `Serving cached /settle response` en un replay). Si aparecen
`idempotency_store_unavailable`, el store de DynamoDB falló y esos settles no se
liquidaron; el consumidor puede apagar el header con `send_idempotency_key=False`.

## Cómo se despliega

Publicar es un tag, no un push. `.github/workflows/publish.yml:5-7` corre con un tag
`v*`; hace `python -m build` (`:30`) y `twine upload` (`:32-36`). Después del merge, c0der
tagea `v0.83.0`. Yo no tageo ni publico.

## Para c0der

### Lo que NO hice, y por qué

- **Tag y publicación a PyPI:** te tocan a vos, según el spec.
- **La mitad TypeScript de la fila 88 y el port del clasificador a TS:** es una task
  aparte con `--deps` sobre esta. Tiene que reproducir el vector fijado de arriba, la
  separación por operación, la comparación por palabra completa y
  `idempotency_key_conflict` en el conjunto de códigos.
- **Adoptarlo en tarotof y 402milly:** fuera del encargo. Para tarotof: reemplazar
  `_codigo_de_nonce_gastado` / `_huele_a_nonce_gastado` por `spent_nonce_evidence` (su
  `deteccion` pasa a `"structured"`/`"wording"`) y heredar la corrección de "refused".
  Ojo: en tarotof hoy un `409 idempotency_key_conflict` cae en su rama 4xx y sale como
  **402** `pago_rechazado` (`api/main.py:285-289`). Con 0.83.0 ese 409 empieza a existir,
  así que conviene adoptarlo junto con el bump. Para 402milly: comparar su clasificación
  (`handler.py:23`, `:955`) contra esta antes de borrar nada (upstream-first).
- **La prueba contra el binario de x402-rs:** ver "Cómo se prueba".
- **Una llave por compra, que la pase el llamador:** no entró. No es solo un kwarg, porque
  el store es compartido y `"order-1"` de dos vendedores choca. Quedó como fila.

### Filas de backlog nuevas

En este repo (`docs/planning/BACKLOG.md`):

| Date | Item | Priority |
|---|---|---|
| 2026-09-13 | Llave de idempotencia por COMPRA (el brazo 409 del facilitador contra una autorización re-firmada), acotada para que no choque entre vendedores | P1 |
| 2026-09-13 | `process_payment()` no llega a la cache del settle con una credencial ya liquidada (verify sin cache) — **x402-rs** | P1 |
| 2026-09-13 | La autorización EIP-3009 usada vuelve opaca (`contract_call_failed`); un token estable sin la razón cruda — **x402-rs** | P1 |
| 2026-09-13 | `Idempotent-Replayed` no se expone en `SettleResponse` / `PaymentResult` | P2 |
| 2026-09-13 | Los `/settle` de `advanced_escrow.py` (`:907`, `:1058`) no mandan la llave | P2 |

Para otros repos (no escritas allá):

| Repo | Item | Priority |
|---|---|---|
| uvd-x402-sdk-typescript | Mitad TS de la fila 88 + port del clasificador, con el vector fijado | P1 |
| tarotof | Adoptar `spent_nonce_evidence` al subir a 0.83.0 (el 409 de conflicto hoy sale como 402) | P1 |
| 402milly | Comparar `handler.py:23`/`:955` contra `spent_nonce_evidence` antes de adoptar | P2 |
| uvd-x402-sdk-python | `publish.yml:17-19` dice "OIDC trusted publishing, no token to steal", pero `:35` sube con `secrets.PYPI_TOKEN`: el comentario o el paso miente | P2 |
