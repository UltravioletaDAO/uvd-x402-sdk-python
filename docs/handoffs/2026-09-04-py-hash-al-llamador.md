# El hash que el SDK leía para decidir, y tiraba antes de contestar

**Fecha:** 2026-09-04
**Rama:** `0xultravioleta/py-hash` (worktree `py-hash`, sobre `main` @ `7dba1e1`)
**Encargo:** `spec-py-hash.txt`, despachado por c0der. Diagnóstico ya hecho; el
gemelo de TypeScript ya publicado como 2.80.0.
**Estado:** 2 commits + este handoff. **869 tests, 0 fallos.** Ruff sin errores
nuevos. **Cero push, cero PyPI, cero deploy.**

---

## 1. El veredicto en cinco líneas

1. **El encargo estaba bien planteado y el arreglo es el que decía.** El hash se
   leía para el veredicto, se logueaba y se **descartaba**. §2.1
2. **El segundo sitio de decisión existe en Python, pero no donde el handoff de
   TypeScript lo ubicó.** `erc8004.py:678 _write_verdict` **no está en `main`** —
   vive en trabajo sin commitear del checkout `Z:`. El sitio real es
   `FacilitatorError.__init__`, y arreglarlo ahí cubre TODOS los caminos de una
   vez. §3
3. **Python queda mejor que TypeScript en la forma del arreglo**: TS necesita un
   parser compartido entre dos clases; acá el veredicto vive en el constructor
   de la única excepción que todos los caminos levantan, así que ningún camino
   futuro puede olvidarse de él. §3.2
4. **Un test no alcanzaba, y los dos rojos lo prueban con números distintos:** el
   cable al llamador y la decisión de reintento caen por separado (12/5 y 8/9). §4
5. **Encontré una asimetría preexistente que casi rompo** y que ahora está
   escrita: el bucle de settle **nunca** re-POSTeó un `429`, mientras que
   `is_transient_error` sí lo llama transitorio. Son verdades distintas y las dos
   correctas. §2.4

---

## 2. Qué se hizo

| Commit | Qué |
|---|---|
| `f1bd894` | El cambio entero: los tres campos al llamador, el segundo sitio de decisión, un solo parser, 17 tests |
| `407aedd` | `pyproject.toml` → **0.76.0** + changelog + README |
| *este* | Handoff |

### 2.1 El defecto, con archivo:línea

`src/uvd_x402_sdk/client.py:96` (antes del cambio), `_extract_tx_hash_from_body`:
privada. Su único consumidor, `_facilitator_error_tx_hash`, alimentaba dos
veredictos booleanos y un `logger.warning`. **Nada salía.**

El llamador recibía un `FacilitatorError` con `message`, `status_code` y
`response_body` — el cuerpo crudo, sin parsear, que cada consumidor tendría que
volver a leer con su propio criterio. O sea: el SDK ya sabía el hash y no lo
decía. Negarse a reintentar sin entregar dónde mirar reconstruye el mismo
callejón sin salida una capa más arriba.

### 2.2 La forma, pineada del facilitador

```json
502 {"error":"settlement_unconfirmed","transaction":"0x…",
     "paymentId":"0x…","retryable":false}
```

Sin `Retry-After`. El otro `502` de `/settle`, el único que existía hasta ahora,
es `{"error":"upstream_rpc_unavailable (ref: <uuid>)"}` **con** `Retry-After: 30`
y donde nada se difundió. **Los dos son `502`. El status no los distingue** —
por eso el cuerpo tiene que entrar en la decisión.

### 2.3 Los tres campos

`FacilitatorError` carga `transaction`, `payment_id` y `error_code`, y los repite
en `to_dict()["details"]` como `transaction` / `paymentId` / `errorCode`.

Eso último no es cosmético: `transient_503_response()` construye su body con
`dict(exc.to_dict())`, así que **los tres campos viajan solos hasta el comprador
sin que ningún paywall tenga que saber de ellos**. Hay un test que lo pinea.

`try_settle_payment()` los devuelve como datos. Las claves están **siempre
presentes** (`None` en el camino feliz) para que leer `result["payment_id"]` no
dependa de si el settle funcionó.

**El hash va verbatim.** Algorand imprime base32, Solana base58. Normalizarlo lo
vuelve impegable en un explorador, y pegarlo es el remedio entero que ofrecemos.
Test con un hash base32 real de Algorand.

### 2.4 Lo que quedó intacto, y una trampa que casi me como

- El `502` transitorio: mismas 3 vueltas, misma espera clampeada.
- Un `4xx` sigue **sin** clave `retryable` en su `to_dict()`.
- `anti_double_settle=False` sigue desarmando la **inferencia** del hash.

Y la trampa: al reescribir `_is_retryable_settle_error` para que delegue en
`exc.retryable`, iba a introducir un cambio que nadie pidió. **El bucle de settle
nunca re-POSTeó un `429`** (`if exc.status_code < 500: return False`), mientras
que `exc.retryable` — y `is_transient_error` — lo llaman transitorio. Las dos
lecturas son correctas y distintas: para un paywall que decide 402-vs-503 un 429
es "reintentá luego"; para un bucle que re-POSTea un cobro, re-POSTear es lo que
cuesta plata. La asimetría está preservada y ahora escrita en el código.

---

## 3. El veredicto sobre el segundo sitio de decisión

**Sí existe, y no está donde el handoff de TypeScript lo ubicó.**

### 3.1 Por qué el handoff de TS apuntaba a otro lado

Ese worker leyó `Z:/ultravioleta/dao/uvd-x402-sdk-python`, que — como él mismo
avisó — está en **0.72.0 y con cambios sin commitear**. Medido:

```
Z:  erc8004.py  2200 líneas, _write_verdict en :656   ← trabajo SIN COMMITEAR
main erc8004.py 1981 líneas, _write_verdict NO EXISTE
```

Los `client.py:96 / :137 / :194 / :222` del encargo tampoco casan con `main` por
la misma razón (`facilitator_http_error` no existe acá: el cuerpo se adjunta
directo en cada `raise FacilitatorError(...)`). **`_write_verdict` y
`FacilitatorWriteVerdict` no están en `main` ni en esta rama.** No los arreglé
porque no hay qué arreglar todavía — pero el hueco vuelve en cuanto ese trabajo
se commitee: ver §5, sigue siendo P0 para quien lo mergee.

### 3.2 El sitio que sí existe, y es mejor

`src/uvd_x402_sdk/exceptions.py`, `FacilitatorError.__init__`:

```python
retryable = (status_code is None or status_code == 429 or status_code >= 500)
```

**Idéntico en forma al `_write_verdict` de TS y al de Z:**, y con más alcance:
ese flag es público (`exc.retryable`), viaja en `details["retryable"]`, y salía
`True` para el `502 settlement_unconfirmed` — **contradiciendo a
`is_transient_error`, que para el mismo error decía `False`**. Un consumidor que
leyera el atributo en vez de llamar a la función se comía el doble gasto.

Ahora el status es el **techo** y el cuerpo solo puede **bajarlo**. Y como el
veredicto vive en el constructor de la excepción que **todos** los caminos
levantan — settle, verify, escrow (`client.py`), `events.py`, `erc8004.py:928` —
se arregla en un lugar en vez de en N, y **un camino futuro no puede olvidarse
de aplicarlo**. TypeScript necesitó un parser compartido entre dos clases
distintas; acá la forma del código lo garantiza.

**Solo BAJA, nunca sube.** Un cuerpo con `retryable: true` sobre un `400` no hace
que el SDK reenvíe una credencial que el facilitador rechazó de verdad. Hay test.

Y de paso entra la señal que el handoff de TS listaba como hueco #3: **nadie
honraba el `"retryable": false` explícito.** Python lo infería solo de la
presencia del hash — funciona con la forma de hoy, ignora un contrato que el
facilitador está declarando. Ahora es la señal de mayor autoridad, y
`anti_double_settle=False` **no** la levanta: ese opt-out existe para que el
llamador se coma el riesgo de una *inferencia* del SDK, no para contradecir al
facilitador cuando habló claro.

### 3.3 Los otros candidatos, revisados y descartados

| Sitio | Veredicto |
|---|---|
| `LookupInconclusiveError` (`retryable=True` fijo) | Es un **GET** de lectura de identidad. No mueve plata. Sin riesgo |
| `WriterUnavailableError` (`retryable=True` fijo) | Definida pero **nunca levantada** en esta rama. Nada que arreglar hoy |
| `erc8004.register_agent` | No tiene flag de reintento. Un `4xx` vuelve como `RegisterAgentResponse(success=False)`; un `502` no valida contra ese modelo (le falta `network`) y vuelve como error string. **Ningún reintento automático** |
| `X402Client.fetch()` (buyer loop) | Solo firma y reintenta ante un **402**. Cualquier otro status vuelve intacto. Sin riesgo |
| `erc8128/errors.py` | Tabla estática por código del protocolo, no por status HTTP. Ajeno |

---

## 4. Verificación

Corrido con la nota de operación del encargo:
`PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PYTHONPATH=src python -m pytest -p pytest_asyncio.plugin`

### 4.1 Rojo A — sin el arreglo entero

Suite `tests/test_facilitator_failure_fields.py` contra el código de `main`:

```
FAILED …TestElHashLlegaAlLlamador::test_la_excepcion_carga_los_tres_campos
FAILED …TestElHashLlegaAlLlamador::test_to_dict_los_repite_para_quien_serializa
FAILED …TestElHashLlegaAlLlamador::test_settle_payment_se_los_entrega_al_que_pago
FAILED …TestElHashLlegaAlLlamador::test_try_settle_payment_los_devuelve_como_datos
FAILED …TestElHashLlegaAlLlamador::test_el_hash_va_verbatim
FAILED …TestElHashLlegaAlLlamador::test_un_cuerpo_sin_los_campos_no_inventa_ninguno
FAILED …TestElHashLlegaAlLlamador::test_un_cuerpo_ilegible_no_revienta
FAILED …TestElCuerpoSoloBaja::test_el_flag_explicito_se_honra_aunque_no_venga_hash
FAILED …TestElCuerpoSoloBaja::test_cualquier_5xx_con_hash_para_se_llame_como_se_llame
FAILED …TestElCuerpoSoloBaja::test_el_opt_out_desarma_la_inferencia_no_el_contrato
FAILED …TestPorConstruccion::test_verify_tambien_los_entrega
FAILED …TestPorConstruccion::test_el_503_de_un_paywall_repite_los_campos
======================== 12 failed, 5 passed in 0.82s =========================
```

Y los dos mensajes que importan. El cable, que no existe:

```
AttributeError: 'FacilitatorError' object has no attribute 'transaction'
KeyError: 'transaction'
```

Y el segundo sitio de decisión midiendo mal, medido:

```
AssertionError: assert True is False
 +  where True = FacilitatorError('settle failed').retryable
```

**Los 5 verdes son los controles**, incluido `test_el_502_transitorio_se_sigue_reintentando`
(3 POSTs), `test_el_no_confirmado_no_se_reintenta_jamas` (1 POST) y
`test_el_cuerpo_nunca_sube_un_4xx_a_reintentable`. El camino feliz ya estaba
bien y este rojo lo confirma antes de tocar nada.

### 4.2 Rojo B — sacando SOLO el cable al llamador

Quitando los tres campos de `FacilitatorError` y **dejando el veredicto intacto**:

```
FAILED …TestElHashLlegaAlLlamador::test_la_excepcion_carga_los_tres_campos
FAILED …TestElHashLlegaAlLlamador::test_to_dict_los_repite_para_quien_serializa
FAILED …TestElHashLlegaAlLlamador::test_settle_payment_se_los_entrega_al_que_pago
FAILED …TestElHashLlegaAlLlamador::test_try_settle_payment_los_devuelve_como_datos
FAILED …TestElHashLlegaAlLlamador::test_el_hash_va_verbatim
FAILED …TestElHashLlegaAlLlamador::test_un_cuerpo_sin_los_campos_no_inventa_ninguno
FAILED …TestPorConstruccion::test_verify_tambien_los_entrega
FAILED …TestPorConstruccion::test_el_503_de_un_paywall_repite_los_campos
========================= 8 failed, 9 passed in 0.83s =========================
```

**La mitad del reintento queda entera en verde**: el bloque `TestElCuerpoSoloBaja`
completo (4/4, incluido el opt-out) y `TestElTransitorioSigueIgual` completo
(3/3). Un solo test no habría visto esto: el SDK puede dejar de reintentar y
**aun así** dejar al llamador sin nada que buscar, que es exactamente la forma
que este cambio existe para sacar.

### 4.3 El control, escrito aparte

`test_el_502_transitorio_se_sigue_reintentando` cuenta **3 POSTs** contra un
`502` con `Retry-After: 5` y sin hash, y `test_el_no_confirmado_no_se_reintenta_jamas`
cuenta **1** contra el no-confirmado. El mismo status, el mismo método, dos
conteos: eso es el defecto y su ausencia, medidos con el mismo instrumento.

### 4.4 Suite

```
869 passed in 4.32s      (852 antes del cambio + 17 nuevos)
ruff check               62 errores, TODOS preexistentes (verificado contra HEAD:
                         60 en los mismos archivos; los +2 son `Dict` vs `dict`
                         del estilo py39 del propio archivo)
mypy                     no instalado en este entorno — no corrido, no reportado
```

**Un test existente cambió a propósito:** `test_settle_hooks.py::test_success_shape`
pineaba la forma exacta del dict de éxito. `payment_id` y `error_code` ahora están
presentes y en `None` ahí — a propósito, para que un llamador que lea
`result["payment_id"]` no reciba un `KeyError` según si el settle funcionó.

---

## 5. Backlog

| Date | Item | Context | Priority | Status |
|---|---|---|---|---|
| 2026-09-04 | `_write_verdict` vuelve con el trabajo sin commitear de `Z:` | Cuando se commitee, trae `retryable = status is None or status == 429 or status >= 500` **sin** guard. Un `POST /register` con `settlement_unconfirmed` sale reintentable → mint duplicado. El arreglo es una línea: delegar en `FacilitatorError._retryable_verdict`, que ya existe | **P0** | Nuevo |
| 2026-09-04 | El checkout `Z:/ultravioleta/dao/uvd-x402-sdk-python` está en 0.72.0 y sucio | 14 archivos modificados + 5 sin trackear. Es lo que leyó el worker de TS, y por eso su handoff apunta a líneas que no existen en `main`. Rompe la fase 6 de xlang para cualquiera que la corra desde ahí | P1 | Preexistente |
| 2026-09-04 | xlang no tiene fase para lectura de errores del facilitador | Los dos SDK ya divergen en algo medible y el conformance no lo ve. Fase 7 natural ahora que Python tiene su lado | P1 | Heredado del handoff TS |
| 2026-09-04 | `transient_503_response` hardcodea `body["retryable"] = True` | Protegido por su docstring ("llamalo solo cuando `is_transient_error` dijo transitorio") y ahora también porque el opt-out ya no levanta el contrato explícito. Queda el caso `anti_double_settle=False` + 5xx con hash sin flag: el llamador asumió ese riesgo por escrito | P2 | Nuevo |
| 2026-09-04 | `WriterUnavailableError` está definida y nunca se levanta | Código muerto en `main`; probablemente se cablea en el trabajo sin commitear de `Z:` | P2 | Nuevo |

---

## 6. Para c0der

**Qué hice.** Dos commits en `0xultravioleta/py-hash` más este handoff.
`f1bd894` es el cambio entero — `transaction` / `payment_id` / `error_code` en
`FacilitatorError` y en su `to_dict()`, en `try_settle_payment()`, el segundo
sitio de decisión, un solo parser y 17 tests. `407aedd` es **0.76.0** con
changelog y README. **869/869 en verde. Cero push, cero PyPI, cero deploy.**

**Qué encontré que el encargo no decía.** Tres cosas.

La primera, y la que cambia el mapa: **el `erc8004.py:678 _write_verdict` que el
handoff de TypeScript señala NO está en `main`.** Vive en el trabajo sin
commitear del checkout `Z:` (0.72.0, 14 archivos modificados) — el mismo checkout
que ese worker avisó que estaba sucio. Sus números de línea de `client.py`
tampoco casan con `main` por lo mismo. **Sigue siendo P0, pero para quien mergee
ese trabajo, no para esta rama.** Está en el backlog con el arreglo de una línea.

La segunda: **el segundo sitio de decisión real es `FacilitatorError.__init__`**,
y era peor de lo que parecía. `exc.retryable` salía `True` para el `502`
`settlement_unconfirmed` — **contradiciendo a `is_transient_error`, que para ese
mismo error decía `False`**. Dos verdades opuestas sobre el mismo objeto, y la
equivocada era la barata de leer. Arreglado en el constructor, que es donde
cubre a los cinco caminos que levantan esa excepción de una sola vez; Python
queda con mejor forma que TypeScript acá, que necesita un parser compartido
entre dos clases y depende de que nadie agregue una tercera.

La tercera: **casi introduzco una regresión de scope.** Al delegar
`_is_retryable_settle_error` en `exc.retryable` iba a hacer que el bucle de
settle empezara a re-POSTear los `429`, que nunca re-POSTeó. Lo agarré antes de
commitear y la asimetría quedó escrita en el código con su porqué.

**Sobre el upstream-first.** No hay nada que subir a TypeScript desde acá: el
guard genérico que TS adoptó de Python en `35ef33a` ya era nuestro, y las tres
señales quedaron con la misma autoridad en los dos lados. La única diferencia de
forma es la de §3.2, y favorece a Python — si querés simetría real, la dirección
es TS adoptando el patrón "el veredicto vive en el constructor", no al revés.

**Sobre el trailer de los commits.** El encargo pide
`Co-Authored-By: Claude <noreply@anthropic.com>`, y el harness de esta sesión
instruye `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>` + una línea
`Claude-Session:`, diciendo que reemplaza cualquier guía previa. **Puse los dos**
para no romper ninguno de los dos criterios. Si querés solo el del historial, es
un `--msg-filter` antes de pushear — decidilo vos, no reescribo commits sin OK.

**Antes de publicar.** `mypy` no está instalado en este entorno, así que el
type check **no corrió** — no lo cuento como verde. Si el pipeline de publicación
lo exige, corrélo vos; los cambios son anotaciones `Optional[str]` y un `Dict[str,
Any]`, nada exótico. Y `0.76.0` es MINOR sobre la publicada `0.75.0`: nada
existente cambia de forma salvo el `retryable` de un `5xx` cuyo cuerpo pide que
no se re-envíe, que es la corrección.
