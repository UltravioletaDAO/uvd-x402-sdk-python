# El SDK de Python ahora elige el sobre; antes lo imponía

**Fecha:** 2026-09-04 · **Rama:** `0xultravioleta/py-sobre-v2` · **Versión:** 0.74.0 (sin publicar)
**Facilitador medido:** `https://facilitator.ultravioletadao.xyz` — versión **2.10.0**
**Antecedente:** el mismo arreglo en TypeScript, `0xultravioleta/sdk-sobre-v2`,
`docs/handoffs/2026-09-03-sobre-v2.md` (§6 es este encargo)

---

## QUÉ / POR QUÉ / RIESGO

**QUÉ:** `verify_payment()` y `_settle_once()` eligen entre el sobre v1 y el v2
en vez de escribir `"x402Version": 1` como literal. Se agregó
`uvd_x402_sdk.envelope` con la regla de decisión y la conversión desde el
`PaymentRequirements` v1 que el SDK ya arma hacia el par `{resource, accepted}`.

**POR QUÉ:** el SDK podía anunciar v2 en un 402 y después era estructuralmente
incapaz de hablarlo. Y `X402Config.x402_version` ya estaba declarado,
documentado como `"1, 2 o auto"`, con default `"auto"` — y **no lo leía nadie**.

**RIESGO:** que una llamada que hoy funciona cambie de forma. Acotado midiendo
primero: el camino v1 sale byte por byte igual, y lo único que cambia de sobre
es el cable CAIP-2, que es justo el que el 402 del propio SDK anuncia. Escape:
`x402_version=1`.

**Recall ping (~10s):** ¿por qué `auto` no se dispara con
`payload.x402Version == 2`? Si la respuesta no menciona la palabra *untagged*,
vale la pena leer §2.3.

---

## 1. Lo que estaba mal, y una cosa que el despacho no sabía

El despacho tenía razón en todo lo que decía. Faltaba una pieza que hace el
defecto peor de lo que se veía:

| qué | archivo:línea (antes) | estado |
|---|---|---|
| `verify_payment` arma el sobre v1 con `1` literal | `client.py:823-827` | ❌ |
| `_settle_once` idem | `client.py:1019-1023` | ❌ |
| `build_verify_request_v2` / `build_settle_request_v2` | `envelope_v2.py:131,156` | ✅ existían, forma correcta |
| exportados en `__init__.py`, sin un solo llamador | `__init__.py:259-264` | ❌ |
| **`X402Config.x402_version: Literal[1, 2, "auto"] = "auto"`** | **`config.py:138`** | ❌ **letra muerta** |

La última fila es la que agrava el diagnóstico. No es que faltara la opción: la
opción **ya existía, con su docstring** (`x402_version: Protocol version to use
(1, 2, or "auto")`) y su default en `"auto"`, y cero lectores. El SDK prometía
elegir la versión en su propia configuración y después mandaba `1` fijo.

Y el README lo decía al revés de lo que el SDK hacía falta que hiciera:

> `X402Client.verify_payment` and `settle_payment` emit the v1 one and **cannot
> express v2**

Eso era cierto. Por eso el arreglo tiene que tocar los docs: la sección arrancaba
describiendo el defecto como si fuera el diseño.

**Ningún test del repo fijaba el defecto.** A diferencia de TypeScript —donde
`index.test.ts:300` asserteaba el cuerpo roto como correcto— acá la suite
simplemente no miraba el sobre que salía. 842/842 en verde antes y después.

---

## 2. Las pruebas vivas (pegadas)

### 2.1 El facilitador cambió entre ayer y hoy — y eso corrige la premisa de TS

Esto es lo primero que medí y lo más importante del handoff. `POST /verify`
contra producción, 2026-09-04, firma fabricada (`0x11…22…1b`), así que el
veredicto siempre es `isValid:false`; lo que discrimina es si el facilitador
**entendió** el cuerpo (200) o **no pudo leerlo** (400):

```
### P1  v1 sobre v1, nombres planos (lo que el SDK PY emitía hoy)
HTTP 200  {"isValid":false,"invalidReason":null,"payer":"0x0000…0001"}

### P2  v1 con marcador x402Version:2 en el header, nombres planos
HTTP 200  {"isValid":false,"invalidReason":null,"payer":"0x0000…0001"}

### P3  v1 con network CAIP-2 en el payload
HTTP 200

### P4  v1 con network CAIP-2 en requirements
HTTP 200

### P5  v1 con CAIP-2 de los dos lados (el caso ChatGPT)
HTTP 200

### P6  v2 CAIP-2, duplicación interna (lo que build_verify_request_v2 emite)
HTTP 200

### P7  v2 CAIP-2, SIN duplicación interna
HTTP 200

### P8  v2 con nombre plano en vez de CAIP-2
HTTP 400  {"error":"Failed to deserialize VerifyRequest: data did not match any
           variant of untagged enum VerifyRequestEnvelope",
           "code":"invalid_request_body",
           "hint":"This body declares `x402Version: 2`. x402 v2 is a JSON object wi…"}

### P9  v2 con extra {name,version} en accepted        HTTP 200
### P10 v2 sin maxTimeoutSeconds                       HTTP 400
### S1  /settle v1 con CAIP-2 de los dos lados         HTTP 200
### S2  /settle v2 CAIP-2, duplicación interna         HTTP 200
```

**P3, P4 y P5 eran 400 duro ayer.** El handoff de TypeScript midió, el
2026-09-03, que el sobre v1 con CAIP-2 fallaba con
`unknown variant `eip155:8453`, expected one of `base-sepolia`, `base`, …`. Hoy
el mismo cuerpo da 200: el facilitador le enseñó CAIP-2 al enum del sobre v1, y
lo documentó en `/skill.md`:

> `network` may be written either way, in both objects: `"base"` or
> `"eip155:8453"`. That is what lets an offer taken straight out of
> `/discovery/resources` — which is CAIP-2 — be paid without rewriting it.

**Eso invalida el argumento que TypeScript usó para justificar `auto`.** Allá el
razonamiento era: "toda combinación con CAIP-2 ya es 400 duro, así que pasarlas
a v2 no puede regresionar a nadie: solo convierte una falla en un pago". Hoy esa
frase es falsa. La regla sigue siendo la correcta, pero **por otras tres razones**
— ver §3.

También cambió la duplicación interna. `/skill.md`:

> Older facilitator builds *also* required `resource` and `accepted` to be
> repeated **inside** `paymentPayload` … That is fixed: the inner copy is
> optional. If you already send it, keep sending it — the duplicated envelope is
> still accepted, byte for byte.

Medido: P6 (con) y P7 (sin) dan los dos 200. **`envelope_v2.py` no se toca**:
seguir mandando la copia interna es lo que hace que un solo cuerpo funcione
contra las dos generaciones del facilitador. Sí actualicé su comentario, que
afirmaba que omitirla fallaba.

### 2.2 v2 no es peor que v1 en ninguna familia de cadena

`auto` sube por CAIP-2, y CAIP-2 no es solo EVM. Antes de dejarlo subir a
`solana:…` o `stellar:pubnet` había que saber si el sobre v2 parsea para ellos:

```
### A1 v2 solana CAIP-2                     200  invalidReason="io error: unexpected end of file"
### A2 v1 solana CAIP-2 (control)           200  invalidReason="io error: unexpected end of file"
### A3 v2 scheme=escrow, base               400  "Escrow verification error: … missing field `paymentInfo`"
### A4 v1 scheme=escrow, base (control)     400  "Escrow verification error: … missing field `paymentInfo`"
### A5 v2 con extra EIP-712 (lo que el SDK manda siempre en EVM)   200
### A7 v2 avalanche CAIP-2                                        200
### B3 stellar v2  400 invalid_request_body   ← pero B1/B2 stellar v1 TAMBIÉN 400
### B4 near v2     400 invalid_request_body   ← pero B5 near v1    TAMBIÉN 400
```

Las cuatro filas de control son el punto: donde v2 falla, v1 falla igual y por
la misma razón (mi payload interno de Stellar/NEAR es inventado, no es el sobre).
**No encontré ninguna forma que v1 acepte y v2 rechace**, salvo la que ya sé —
el nombre plano de red, que la conversión resuelve.

### 2.3 Por qué `auto` no se dispara con el marcador

`P2` arriba: un header que lleva `x402Version: 2` con nombres planos **da 200 en
el sobre v1**. El enum de sobres del facilitador es *untagged*: matchea por
**forma** e ignora el marcador. Y el mismo par de nombres planos en el sobre v2
es **400** (`P8`).

Así que subir por el marcador rompería una llamada que funciona y la mandaría a
la única forma que no puede llevarla. `auto` lee la **red**, no la versión
declarada.

### 2.4 End-to-end: el SDK armando el cuerpo contra el `/verify` vivo

No son cuerpos escritos a mano — es `X402Client.verify_payment()` posteando a
producción. `PaymentVerificationError` = el facilitador entendió el cuerpo;
`FacilitatorError 400 invalid_request_body` = no pudo:

```
   [sdk] Verifying payment on base for $0.01 (x402 v1 envelope)
   [sdk] Verifying payment on eip155:8453 for $0.01 (x402 v2 envelope)
   [sdk] Verifying payment on eip155:43114 for $0.01 (x402 v2 envelope)
   [sdk] Verifying payment on base for $0.01 (x402 v1 envelope)
   [sdk] Verifying payment on eip155:8453 for $0.01 (x402 v1 envelope)
   [sdk] Verifying payment on base for $0.01 (x402 v2 envelope)

### v1 sin cambios: nombre plano `base`
   el facilitador entendió el cuerpo: SI   reason=None

### EL CASO CHATGPT: el pagador repite el CAIP-2 que anunció el 402
   el facilitador entendió el cuerpo: SI   reason=None

### CAIP-2 en otra EVM (avalanche)
   el facilitador entendió el cuerpo: SI   reason=None

### marcador x402Version:2 con nombre plano (tiene que seguir en v1)
   el facilitador entendió el cuerpo: SI   reason=None

### CONTROL: pin a 1 sobre un cable CAIP-2
   el facilitador entendió el cuerpo: SI   reason=None

### pin a 2 sobre un cable plano
   el facilitador entendió el cuerpo: SI   reason=None
```

Los logs del SDK (arriba) muestran que cada caso eligió el sobre que
corresponde. **Y el control negativo**, sin el cual "los seis entendieron" no
prueba nada — la misma forma v2, rota a propósito, por el mismo camino:

```
### CONTROL NEGATIVO: v2 con el nombre de red SIN convertir a CAIP-2
   el facilitador entendió el cuerpo: NO   HTTP 400

### CONTROL NEGATIVO: v2 con `resource` como string suelto (el error clásico)
   el facilitador entendió el cuerpo: NO   HTTP 400

### CONTROL NEGATIVO: v2 con `amount` todavía llamado maxAmountRequired
   el facilitador entendió el cuerpo: NO   HTTP 400
```

El arnés discrimina por los dos lados: seis SÍ y tres NO.

---

## 3. La decisión de diseño, con el argumento actualizado

`auto` sube a v2 en cuanto ve CAIP-2 en el cable. Con el facilitador de hoy eso
**ya no es** "convertir una falla en un pago" — las dos formas dan 200. Las tres
razones que lo sostienen igual:

1. **Es hablar el protocolo que el vendedor anunció.** Una red CAIP-2 en el cable
   significa que el 402 que la produjo anunciaba v2.
2. **Es la única forma que aceptan las dos generaciones del facilitador.**
   v2-con-CAIP-2 funciona en la build de ayer y en la de hoy; v1-con-CAIP-2 es un
   400 duro en cualquier build anterior al 2026-09-04. Contra un facilitador
   self-hosted o pineado, elegir v1 ahí es la opción que rompe.
3. **Paridad con TypeScript 2.78.0**: el mismo cable produce el mismo cuerpo en
   los dos SDK (§5).

Y las dos filas que hoy dan 200 con nombres planos —incluida la del marcador
`2`— no se tocan.

### Qué se agregó

`src/uvd_x402_sdk/envelope.py` (nuevo):

- `resolve_envelope_version(payload, requirements, requested="auto")` — la regla
  de §2.3. Un pin gana sobre el cable: elegir la versión es el punto de la opción.
- `build_verify_request_for_version` / `build_settle_request_for_version` — el
  retorno v1 es byte por byte lo que el cliente mandaba antes.
- `to_resource_info_v2` / `to_accepted_requirements_v2` — la conversión.
  **Esto es lo que significa "el consumidor no escribe código"**: sigue pasando
  el mismo `PaymentPayload` que ya tenía.

`envelope_v2.py` queda **intacto en su código**; solo se corrigieron dos
comentarios que habían quedado falsos (el que decía que el cliente no puede
expresar v2, y el que decía que omitir la copia interna falla).

**Los tres renombres que hacen el daño** y que el facilitador no nombra nunca:
`maxAmountRequired` → `amount`; `network` en CAIP-2; y
`resource`/`description`/`mimeType` salen a un **objeto** `resource`. `extra`
viaja igual — ahí vive el dominio EIP-712 de los tokens que el facilitador no
conoce por dirección (EURC, los USDC puenteados); tirarlo los haría impagables.

XRPL se queda en v1 y está bien: `xrpl-mainnet` no tiene forma CAIP-2, su string
v1 **es** su identificador. Con `auto` se queda en v1; con un pin explícito a 2
**revienta** en vez de mandar un nombre de red v1 adentro de un cuerpo v2.

---

## 4. La prueba discriminante

Contra el `client.py` **revertido al literal `1`** (o sea: los builders y los
helpers puestos, y nadie llamándolos — el defecto exacto), con los 20 tests
nuevos:

```
>       assert body["x402Version"] == 2
E       assert 1 == 2
tests\test_envelope_selection.py:117: AssertionError

>       assert body["resource"] == {
E       KeyError: 'resource'
tests\test_envelope_selection.py:141: KeyError

>       assert fake.bodies[0]["accepted"]["extra"] == {"name": "EURC", "version": "2"}
E       KeyError: 'accepted'
tests\test_envelope_selection.py:182: KeyError

>       with pytest.raises(ValueError, match="no CAIP-2 form"):
E       Failed: DID NOT RAISE <class 'ValueError'>
tests\test_envelope_selection.py:265: Failed

FAILED …::TestUpgradesToV2::test_verify_sends_the_v2_envelope_when_the_network_is_caip2
FAILED …::TestUpgradesToV2::test_settle_sends_the_v2_envelope_on_the_same_trigger
FAILED …::TestUpgradesToV2::test_the_v2_body_has_the_exact_shape_the_facilitator_accepts
FAILED …::TestUpgradesToV2::test_the_inner_resource_accepted_pair_is_repeated
FAILED …::TestUpgradesToV2::test_the_eip712_domain_survives_the_conversion
FAILED …::TestExplicitPin::test_pinning_2_forces_v2_on_a_plain_wire
FAILED …::TestExplicitPin::test_pinning_2_on_a_network_with_no_caip2_form_fails_loudly
FAILED …::TestSettleFallbackReplaysTheSameEnvelope::test_the_fallback_resends_the_v2_body_not_a_rebuilt_v1
8 failed, 12 passed
```

Los ocho se ponen rojos con **falla de assert real**, no con "no existe la
función". Y los cuatro que son guardas de no-regresión están **verdes en los dos
estados** — que es exactamente para lo que existen:

- `test_a_plain_network_still_gets_the_v1_envelope_byte_for_byte`
- `test_a_header_that_only_DECLARES_v2_is_not_upgraded`
- `test_the_v1_envelope_version_is_the_envelope_not_the_header`
- `test_pinning_1_keeps_v1_on_a_caip2_wire` · `test_auto_leaves_xrpl_on_v1`

Después del cambio: **20/20** en ese archivo, **842/842** en la suite.

---

## 5. Paridad con TypeScript — y la divergencia que encontré

### 5.1 El test de conformidad cruzada pasa, y **no cubre el sobre**

Corrí el del CI de TypeScript ("Cross-language conformance (TS ↔ PY)") con este
worktree como `UVD_X402_PY_ROOT`:

```
CROSS-LANGUAGE CONFORMANCE PASSED — 266 checks across 5 phases.
  26 signatures produced live by TypeScript and verified live by Python,
  26 produced live by Python and verified live by TypeScript,
  72 matrix verdicts compared verifier to verifier.
```

Verde con mi cambio. **Pero sus cinco fases son ERC-8128, presets y la matriz de
verificación de firmas. Cero cobertura del sobre:**

```
$ grep -c -i "envelope|paymentRequirements|buildVerifyRequest" \
    scripts/xlang/cross-language-conformance.mjs scripts/xlang/agent.mjs scripts/xlang/agent.py
0
0
0
```

Ese es el hueco por donde los dos SDK se separaron en el sobre, y es
literalmente el problema que el propio script dice existir para resolver: *"both
SDK suites were green while the two implementations diverged"*. **Extenderlo es
trabajo del lado TypeScript** (los tres archivos viven allá) y no lo hice, para
no pisar ese worktree. Lo que hace falta es una fase 6 con un op nuevo en
`agent.mjs` / `agent.py` — `build_envelope`, entrada `{network, marker, pin}`,
salida `{version, verify, settle}` — y comparar los tres campos. Es la forma
exacta de la comparación que corrí a mano acá abajo.

### 5.2 La comparación a mano: 5 de 6 byte-idénticos

TS compilado (`dist/`, rama `sdk-sobre-v2`) contra este Python, mismo cable:

```
ok   base plano               v1 (TS v1)  verify==  settle==
ok   CAIP-2 base              v2 (TS v2)  verify==  settle==
ok   CAIP-2 avalanche         v2 (TS v2)  verify==  settle==
FAIL plano con marcador 2     v1 (TS v1)  verify=DIFF  settle=DIFF
ok   pin 1 sobre CAIP-2       v1 (TS v1)  verify==  settle==
ok   pin 2 sobre plano        v2 (TS v2)  verify==  settle==

5 idénticos, 1 distintos, sobre 6 cables.
```

La versión elegida coincide en **los seis**. La única divergencia de cuerpo:

| | top-level `x402Version` | forma |
|---|---|---|
| TypeScript | **2** (heredado del header del pagador) | v1 (`paymentRequirements`) |
| Python | **1** | v1 |

Medido, los dos son 200 hoy:

```
### TS: top-level x402Version=2 con FORMA v1     HTTP 200  {"isValid":false,…}
### PY: top-level x402Version=1 con forma v1     HTTP 200  {"isValid":false,…}
```

### 5.3 Acá conviene distinto, y ya estaba distinto

**No lo cambié a paridad, a propósito.** El `x402Version` del top level nombra el
**sobre**, no el header del pagador: declarar `2` mandando `paymentRequirements`
es una contradicción interna del cuerpo. Y ya no es inocua — el facilitador
elige el `hint` de su 400 según la versión declarada:

> `"hint":"This body declares \`x402Version: 2\`. x402 v2 is a JSON object with
> \`paymentPayload\`, \`resource\` and \`accepted\`…"`

O sea que si ese cuerpo falla por cualquier otro motivo, el mensaje manda al
integrador a documentar la forma equivocada — el mismo tipo de trampa que ya
costó un día. Hoy funciona porque el enum es untagged; el día que el facilitador
mire el marcador, TypeScript rompe y Python no.

Además, en Python el literal `1` **es** el comportamiento anterior: cambiarlo
sería una regresión gratuita en el único camino que prometí no mover. Está
pineado en `test_the_v1_envelope_version_is_the_envelope_not_the_header`.

**Para el lado TypeScript** (no es mío, va como reporte): `buildVerifyRequest` /
`buildSettleRequest` heredan `paymentHeader.x402Version` en el top level del
sobre v1. Es preexistente, no lo introdujo 2.78.0, y no rompe nada hoy — pero es
la única diferencia de cuerpo que queda entre los dos SDK y el argumento está
del lado de Python.

---

## 6. Versión

**0.74.0**, MINOR sobre la publicada 0.73.0. **NO publicado** — el tag es del
dueño.

No es patch: el default cambia de comportamiento en un caso (un cable CAIP-2
pasa de sobre v1 a v2). No es MAJOR: la API pública no rompe, ninguna firma
cambia, el camino v1 sale byte por byte igual, y el caso que cambia es
exactamente el que el propio 402 del SDK definía como roto. `x402_version=1`
restaura el cuerpo anterior.

El changelog vive en `README.md` (§ Changelog); no hay `CHANGELOG.md` en este
repo y no inventé uno.

---

## 7. Estado de cierre

| Criterio | Estado |
|---|---|
| Emite v2 y v1 sigue igual, con tests discriminantes | ✅ §4 |
| Forma verificada contra el `/verify` vivo (no `invalid_request_body`) | ✅ §2.1, §2.4 |
| Bump y CHANGELOG listos, sin publicar | ✅ 0.74.0, §6 |
| Test de conformidad cruzada TS/PY revisado | ✅ §5.1 — pasa (266 checks) y **no cubre el sobre**; la extensión es del lado TS |
| Suite en verde | ✅ **842/842**, 0 skips. `envelope.py` limpio en mypy y en ruff |
| Handoff con las pruebas pegadas | ✅ este archivo |
| `git status` limpio, cero push | ✅ 6 commits locales |

**Lo que dejé fuera y por qué:** la fase 6 del test de conformidad cruzada
(§5.1) — sus tres archivos viven en el repo de TypeScript y el encargo era el
SDK de Python; dejo la especificación exacta del op que hace falta. Y el
`x402Version` heredado de TypeScript (§5.3), que es un reporte para ese repo, no
un cambio acá.

**Nota de operación:** este entorno tiene plugins de pytest instalados
globalmente (`anchorpy`, `web3.tools`) que revientan la colección. La suite
completa corre con
`PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PYTHONPATH=src python -m pytest -p pytest_asyncio.plugin`.
Sin `-p pytest_asyncio.plugin` pasan 656 y se **saltan 186** en silencio.

---

## Para c0der

Hice que el cliente elija el sobre en el punto donde faltaba de verdad —
`verify_payment` y `_settle_once`—, y el defecto era peor de lo que decía el
despacho: `X402Config.x402_version` ya existía, con su docstring y su default en
`"auto"`, y no lo leía nadie; el SDK prometía elegir la versión en su propia
configuración y mandaba `1` fijo. **Medí antes de decidir y la medición me
corrigió el heredado de TypeScript**: entre ayer y hoy el facilitador le enseñó
CAIP-2 al enum del sobre v1, así que el argumento de allá ("todo CAIP-2 ya es
400, no puede regresionar a nadie") hoy es falso — la regla sigue siendo la
correcta pero por tres razones nuevas, y la principal es que v2-con-CAIP-2 es la
única forma que aceptan las dos generaciones del facilitador.

Queda 0.74.0 con changelog en el README, 842/842 en verde, la forma verificada
end-to-end con el SDK compilado contra el `/verify` vivo y cerrada por el lado
negativo con tres formas rotas a propósito, y los dos SDK emitiendo cuerpos
byte-idénticos en 5 de 6 cables. **Sin publicar ni pushear** — el tag es tuyo.

**Dos cosas que no son mías.** (1) El test de conformidad cruzada TS↔PY pasa con
este worktree (266 checks) pero **no cubre el sobre en absoluto**: cero menciones
en sus tres archivos. Es el hueco por donde los dos SDK se separaron, y la
extensión hay que hacerla en el repo de TypeScript — dejé en §5.1 el op exacto
que hay que agregar. (2) TypeScript hereda el `x402Version` del pagador en el
top level del sobre v1, así que puede declarar `2` en un cuerpo con forma v1; hoy
da 200 porque el enum es untagged, pero el facilitador ya elige el `hint` de su
400 según ese marcador, o sea que manda al integrador a la forma equivocada. El
argumento está del lado de Python; va como reporte para ese repo, no lo cambié.

**Rama:** `0xultravioleta/py-sobre-v2`, 6 commits, worktree limpio.
