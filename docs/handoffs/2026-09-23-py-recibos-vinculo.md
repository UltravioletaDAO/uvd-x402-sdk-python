---
date: 2026-09-23
tags:
  - type/handoff
  - domain/x402
  - domain/sdk-python
  - domain/receipts
  - priority/p0
status: active
---

# Un vínculo de compra por manejo de pago, y los 409 de una autorización ya admitida (0.89.0)

**Versión:** 0.88.0 → **0.89.0** (minor: cambia lo que el vendedor manda y contesta).
**Toca dinero**: decide cuándo un vendedor entrega sobre un pago. El tag `v0.89.0` y la
publicación en PyPI **no** son parte de esta rama.

Contrato del facilitador: x402-rs 2.39.0 (PR #97, merge `91d12f94`),
`docs/facilitator-receipts.md`, "Replays of an admitted authorization". Una autorización ya
admitida devuelve su respuesta original **sólo** a quien trae el vínculo con que se admitió
(el mismo `Idempotency-Key` o la misma capacidad `X-UVD-Purchase`). Tener el pago firmado no
es un vínculo.

**Versión en producción:** `2.39.0` desde el 2026-09-23 07:06:29Z; antes, `2.38.0`. Los
facilitadores anteriores a 2.39.0 no ataban la respuesta al vínculo; el SDK se defiende igual,
con cualquier versión del facilitador.

---

## 1. Qué entró

| Pieza | Qué |
|---|---|
| `client.py`, `new_idempotency_key()` | `x402-<64 hex>` aleatoria (`secrets.token_hex(32)`), la misma forma que `createIdempotencyKey()` del SDK de TypeScript |
| `client.py`, `_Binding` | UNA llave por manejo (una llamada a `process_payment()`, o a `settle_payment()` con sus reintentos y el fallback), la misma en `/verify`, `/settle` y el reenvío del fallback. Aleatoria salvo que la llamada traiga `idempotency_key` (nuevo, en los cuatro métodos) o `idempotency_scope`. Las dos a la vez: `ValueError`. Llave mal formada (fuera de 1-255 ASCII visible, o que empieza con `receipt:`): error antes de mandar nada |
| `client.py`, `_Binding.refuse_foreign_replay` | **La guardia.** Un manejo sin vínculo propio (llave fresca o ninguna, sin `X-UVD-Purchase`) que recibe `Idempotent-Replayed: true` (200 o 202) ANTES de que alguno de sus intentos haya terminado sin veredicto (timeout, error de transporte, 5xx) lanza `PaymentSettlementError` con `reason=authorization_already_settled` (o `authorization_in_flight` si es un 202) y el recibo. No depende de cómo se generó la llave. Los middlewares la heredan por `process_payment` |
| `client.py`, `derive_idempotency_key` | Pública por compatibilidad. Devuelve `x402-settle-…` para las dos operaciones: es la llave que 0.83.0-0.88.0 mandaban en `/settle`, así que una compra liquidada antes de actualizar conserva su llave. Docstring: **no es un vínculo salvo que el scope sea un secreto del vendedor**. El SDK sólo la usa cuando se le pasa `idempotency_scope` |
| `client.py`, `admitted_authorization_code()`, `payment_conflict_response()` | Nombran y contestan los tres rechazos del riel: `authorization_already_settled` y `receipt_request_conflict` → 409 (gastados para `is_spent_nonce_error`, finales); `authorization_in_flight` → 503 + `Retry-After`, `retryable: true` (transitorio para `is_transient_error`). Mismo mapeo que `buildPaymentConflictResponse` del SDK de TypeScript |
| `exceptions.py` | Las tres constantes + `ADMITTED_AUTHORIZATION_CODES`. `FacilitatorError.retryable` es `True` por nombre para `202 settlement_in_progress` y para `409 authorization_in_flight` |
| `client.py`, fallback por timeout | Reenvía con la misma llave del manejo (ya reusaba los headers: lo que faltaba era una llave por defecto). Si recibe uno de esos 409, lanza la respuesta del facilitador en lugar de un `TimeoutError`, que invitaba a reenviar la misma credencial sin vínculo, algo que nunca devuelve el éxito. Si recibe un `202 settlement_in_progress` vinculado (su pago, en vuelo), vuelve a preguntar hasta `SETTLE_IN_FLIGHT_POLL_SECONDS` (30 s; pausa según `Retry-After` o `retry.afterSeconds` del recibo, tope 5 s) y, pasado el presupuesto, lanza ese 202 (ronda 2) |
| `models.py` | `idempotent_replayed` en `SettleResponse` y `PaymentResult` (del header, nunca del cuerpo); `idempotency_key` en `VerifyResponse`, `SettleResponse` y `PaymentResult`, con `exclude=True` para que no llegue al comprador en `PAYMENT-RESPONSE` |
| `config.py` | `send_idempotency_key` pasa a `True` por defecto (ver §2) |
| Integraciones | FastAPI (`FastAPIX402`, `X402Depends`, `fastapi_require_payment`, `X402Middleware`), Flask (`FlaskX402`, `flask_require_payment`), Django (`DjangoX402Middleware`, `django_require_payment`, `X402PaymentView`), Lambda (`LambdaX402`, `lambda_handler`) y `require_payment`, todas por `_undelivered_response()`: 409, o 503 + `Retry-After` en vuelo, donde antes contestaban 402 (o 400). Nunca entregan sobre esos rechazos. Desde la ronda 2, todo fallo sin veredicto (timeout, 202 en vuelo, `503 idempotency_store_unavailable` / `receipt_store_unavailable`, 5xx reintentable, 429) sale 503 + `Retry-After` con el `reason`. Los rechazos contestan lo mismo que antes |
| `.github/workflows/ci.yml` | Instala `fastapi,flask,django`. Sin eso, cada test de middleware se saltaba, y el test de recibos de FastAPI que entró en 0.88.0 **fallaba** en la resolución del CI (medido en venv limpio sobre `origin/main`: 1230 passed, 1 failed) |
| `tests/receipt_rail.py` | Doble de facilitador sobre socket real en tres formas: `receipts` (2.39.0), `receipts-2.38` (2.36.0-2.38.0) y `legacy` (sin recibos, contrato 2.28.0) |
| Tests nuevos | `tests/test_receipt_rail_binding.py` (41), `tests/test_integrations_replay.py` (108: los 12 puntos de entrada por escenario); 16 más en `test_idempotency_key.py` y 2 en `test_idempotency_opt_in.py` |

## Ronda 2 (refutador: MERGEABLE CON RONDA, sin P0)

| Punto | Qué cambió | Test que lo fija | Mutación |
|---|---|---|---|
| **P1**: el `202` vinculado del fallback salía `TimeoutError` y todas las integraciones contestaban 402 sobre un pago que se movía | El fallback vuelve a preguntar dentro del presupuesto y el mismo request termina en su settle (una entrega). Pasado el presupuesto lanza el 202 (transitorio, con el recibo pendiente), y las integraciones contestan 503 + `Retry-After` con `reason: settlement_in_progress` | `test_a_settle_that_outlives_its_timeout_is_awaited_and_delivered_once` (12 puntos de entrada), `test_a_settle_still_in_flight_past_the_budget_is_503_with_retry_after` (12), `test_a_settle_still_in_flight_resumed_with_x_uvd_purchase_is_delivered_once` (FastAPI ×4: 503 y después, con la misma `X-UVD-Purchase`, una entrega), más dos en `test_receipt_rail_binding.py` | M1, el 202 vuelve a leerse como "no confirmado": **26 rojos** |
| **P2-a**: `503 idempotency_store_unavailable` / `receipt_store_unavailable` salían 402/400 (Flask, Django, Lambda, decorador) y 503 sin `Retry-After` (FastAPI) | `_undelivered_response()`: todo lo que `is_transient_error` llama transitorio sale 503 + `Retry-After` en las cinco integraciones | `test_a_store_the_facilitator_cannot_read_is_503_and_the_same_payment_is_delivered_later` (12 × con/sin recibos: 503, y el mismo `X-PAYMENT` después, una entrega) | M2, sin el 503 transitorio: **36 rojos** |
| **P2-c**: redacción pública | "producción corre 2.38.0" pasa a la versión con su hora (2.39.0 desde 2026-09-23 07:06:29Z), y la descripción de cómo respondían 2.36-2.38 pasa a "no ataban la respuesta al vínculo" en el handoff, CLAUDE.md, README, CHANGELOG, la guía de recibos, el comentario de `client.py` y los tests | — | — |
| **P3**: el 202 vinculado en FastAPI salía sin `Retry-After` ni `reason` | El mismo camino que P1 | Los tests de P1 recorren FastAPI | M1 / M2 |

Una consecuencia que vale la pena ver: el timeout que no se resuelve en el fallback (sin 202, sin
respuesta) también es "sin veredicto", y ahora sale 503 + `Retry-After` en todas las
integraciones. En 0.88.0 salía 402 en todas: por el `TimeoutError` sin `retryable` que FastAPI leía
y por el 402 fijo de las demás.

## 2. Decisiones que tomé

1. **`send_idempotency_key` por defecto `True`** (como TypeScript, que la manda siempre). Sin
   llave, un vendedor cuyo settle expiró después de ser admitido no recupera su propia
   respuesta en el riel de recibos: el fallback recibe 409 y no hay nada que entregar. Costo,
   en redes sin recibos: con la llave puesta, un store del facilitador que no se puede leer
   devuelve `503 idempotency_store_unavailable` SIN liquidar (sin llave, liquidaba). El SDK lo
   trata como sin veredicto (`is_transient_error`: 503 + `Retry-After`, misma credencial). Hay
   un test que lo fija por el camino por defecto. `send_idempotency_key=False` deja el cable
   como en 0.88.0 por defecto. Con la llave apagada, el 409 del fallback es "probablemente fue
   el mío, no entregues a ciegas": el SDK lanza el `FacilitatorError` con el recibo y el
   docstring del fallback dice que hay que conciliar `receipt.settlement` antes de entregar.
   Es la decisión más fácil de revertir: es un default.
2. **La guardia vive en el manejo del settle, no repetida en cada middleware.** Todos los
   middlewares y decoradores entran por `process_payment`, así que la heredan. Si estuviera
   duplicada, la mutación "sacar la guardia" no pondría nada en rojo. También protege a quien
   llama `process_payment()` directo. `refuse_foreign_replay` distingue "primer intento" de
   "reintento propio" por `may_have_admitted`: sólo un intento que terminó sin veredicto pudo
   haber admitido el pago.
3. **`authorization_in_flight` es transitorio (503), no gastado.** Así lo mapea TypeScript. Deja
   de leerse como `is_spent_nonce_error`.
4. **`202 settlement_in_progress` es transitorio por nombre**, sólo en ese código: un 202 de
   `/register` sigue siendo lo que era. `retry=True` lo reintenta con la misma llave.

## 3. Verificación

| Qué | Resultado |
|---|---|
| Suite en venv NUEVO, Python 3.11, `pip install -e '.[signer,dev,dx402,escrow,hedera,fastapi,flask,django]'` (la línea nueva del CI) | **1457 passed**, 0 failed (ronda 1: 1404; la ronda 2 suma 53). Resolución: fastapi 0.141.1, starlette 1.6.0 (1.7.0 en la corrida de la ronda 2, también verde), Flask 3.1.3, Django 5.2.17, pydantic 2.13.5, httpx 0.28.1, pytest 9.1.1 |
| Base `origin/main` (0.88.0), venv nuevo con la línea VIEJA del CI | 1230 passed, **1 failed** (`test_receipts.py::test_fastapi_propagates_receipt_and_validates_context`, falta `fastapi`), 1 skipped |
| Job `cross-language` replicado: TypeScript `main` (`b079792`, 2.97.0) clonado, `npm ci && npm run build`, vectores `--check` y `cross-language-conformance.mjs` contra este árbol con `.[signer]` en venv nuevo | vectores al día; **CROSS-LANGUAGE CONFORMANCE PASSED — 430 checks across 8 phases** |
| Pre-CI de c0der (`scripts/preci.py --base origin/main`) | dispara `ci.yml` (jobs `test` y `cross-language`); `publish.yml` no escucha PRs. Los dos jobs se corrieron a mano, arriba |
| `ruff check` sobre los archivos tocados | sin errores nuevos salvo UP006/UP045 (`Dict`/`Optional`), que es el estilo de todo el paquete (el repo trae cientos, el CI no corre ruff) |
| `mypy src/` con el mismo venv, base vs rama | 150 = 150; ninguno nuevo |
| Mutación 1: guardia anulada (`refuse_foreign_replay` → `return`) | 12 rojos: la fila 2.38 de `test_the_same_x_payment_in_a_new_request_is_not_delivered_again`, uno por punto de entrada. La fila 2.39 sigue verde: el facilitador rechaza por su cuenta (dos defensas independientes). También queda como test (`test_without_the_guard_the_replayed_settle_would_be_delivered`, 12 casos) |
| Ronda 2, M1 (el 202 vinculado del fallback vuelve a ser "no confirmado") y M2 (sin 503 para lo que no tiene veredicto) | **26** y **36** rojos |
| Mutación 2: llave por defecto derivada del `X-PAYMENT` (el error del encargo) | 28 rojos, entre ellos "dos manejos del mismo `X-PAYMENT` mandan llaves distintas" en los tres niveles. Los de no entrega siguen verdes: la guardia no depende de la llave |
| Contra el facilitador vivo | **Nada que liquide.** Sólo `GET /version` y `GET /supported` (lectura) |

## 4. Para c0der

**Tag y publicación.** El tag dispara `.github/workflows/publish.yml` (build + `twine upload`
con `PYPI_TOKEN`):

```sh
git fetch origin && git checkout main && git pull   # después del merge del PR
grep -n '^version' pyproject.toml                  # version = "0.89.0"
git tag -a v0.89.0 -m "0.89.0" && git push origin v0.89.0
gh run list --workflow publish.yml --limit 1        # esperar "completed success"
```

**Cómo verificar en PyPI**, en un venv nuevo (no el del worktree):

```sh
curl -s https://pypi.org/pypi/uvd-x402-sdk/0.89.0/json | python3 -c "import json,sys; d=json.load(sys.stdin); print(d['info']['version'], [u['filename'] for u in d['urls']])"
python3.11 -m venv /tmp/uvd-089 && /tmp/uvd-089/bin/pip install -q 'uvd-x402-sdk==0.89.0'
/tmp/uvd-089/bin/python - <<'PY'
import re, uvd_x402_sdk as u
assert u.__version__ == "0.89.0", u.__version__
assert u.X402Config(recipient_evm="0x" + "22" * 20).send_idempotency_key is True
assert re.fullmatch(r"x402-[0-9a-f]{64}", u.new_idempotency_key())
assert u.AUTHORIZATION_IN_FLIGHT in u.ADMITTED_AUTHORIZATION_CODES
exc = u.FacilitatorError("x", status_code=409, response_body='{"error":"authorization_in_flight"}')
assert u.payment_conflict_response(exc)[0] == 503 and u.is_transient_error(exc)
print("0.89.0 OK")
PY
```

**Qué queda fuera (no lo hice):**

- Flask, Django, Lambda y `require_payment` no reenvían `X-UVD-Purchase` (nunca lo hicieron;
  sólo FastAPI). Una compra reanudada por ellos no está ligada y recibe 409. Cablearlo es
  otra tarea.
- Contra un facilitador anterior a 2.39.0, el fallback de un manejo sin vínculo cuyo primer
  intento expiró acepta el replay como propio: es el "reintento propio" que pide la ronda 1,
  y no puede probar que el intento llegó. Con 2.39.0 la llave aleatoria del manejo lo decide.
- El CI de GitHub no reporta en los PR de este repo desde el 2026-09-11 (`gh pr checks 30`
  y `33`: "no checks reported"). Por eso los dos jobs se corrieron a mano.
- Una autorización NUEVA para la misma compra sigue siendo otro pago (fila del backlog del
  2026-09-13, sin cambio).
