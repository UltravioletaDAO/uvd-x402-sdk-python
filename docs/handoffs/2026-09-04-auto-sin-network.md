# El default `auto` era el que nadie podía usar — arreglado del lado Python

**Fecha:** 2026-09-04 · **Rama:** `0xultravioleta/py-auto` · **Versión:** 0.75.0 (sin publicar)
**Encargo:** el tercero de los tres reportes que el worker de MeshRelay midió en runtime
(`resolve(network=None)` → `TypeError`). Los otros dos ya se cerraron en
`uvd-x402-sdk-typescript` 2.79.0, rama `0xultravioleta/ts-huecos`
**Antecedente en este repo:** `docs/handoffs/2026-09-04-sobre-v2-python.md` (0.74.0, el trabajo
que introdujo `auto`)
**Gemelo del otro lado:** `docs/handoffs/2026-09-04-sobre-v2-huecos.md` §5 (TS), commit `6efeb26`

---

## QUÉ / POR QUÉ / RIESGO

**QUÉ:** `resolve_envelope_version` —y por lo tanto el default `"auto"`— dejó de
reventar sobre un payload x402 **v2**, que es exactamente la forma que la
función existe para rutear. La red ahora se lee donde el payload la tenga: el
top level (v1) o `accepted.network` (v2). Y los dos builders del sobre leen el
payload igual, porque resolver bien y explotar una línea después es medio
arreglo.

**POR QUÉ:** un payload v2 **no tiene `network` de primer nivel en absoluto** —
v2 movió el chain id adentro de `accepted`—, y la función leía solo
`payload.network` y se lo pasaba crudo a `is_caip2_format`, que es `":" in
network`. La consecuencia ya está viva en producción: turnstile y multibrain de
MeshRelay pinnean `x402_version` 1 o 2 explícito para esquivarlo, o sea que el
default de nuestro propio SDK era la única opción que ningún consumidor podía
usar. 0.74.0 se publicó anoche con esto adentro.

**RIESGO:** ningún cable que resolvía antes cambia de versión —lo que resolvía,
resuelve igual— y el cuerpo v1 desde un `PaymentPayload` sale byte por byte
idéntico, fijado por dos guardas verdes en los dos estados. Lo que cambia es que
tres entradas que reventaban ahora contestan: un payload v2 (→ 2 si su
`accepted` es CAIP-2), un payload sin red (→ 1) y un requirements sin red (→ 1).
Escape si algo de eso molesta: `x402_version=1`.

**Recall ping (~10s):** un header v2 que entra por `X402Client.extract_payload`
sigue saliendo en sobre **v1**, incluso con este arreglo. ¿Por qué? Si la
respuesta no menciona que el CAIP-2 se pierde **antes** de la selección, vale
leer el §5.

---

## 1. Lo primero: reproducido en runtime, contra el código de 0.74.0

Sin tocar nada, con `PYTHONPATH=src` sobre el worktree en 0.74.0:

```
v1 (network presente)                 -> 2
v2 dict (sin network) LANZA:
  File "src/uvd_x402_sdk/envelope.py", line 164, in resolve_envelope_version
    if is_caip2_format(payload.network) or is_caip2_format(requirements.network):
AttributeError: 'dict' object has no attribute 'network'
model_construct(network=None) LANZA:
  File "src/uvd_x402_sdk/envelope.py", line 164, in resolve_envelope_version
    if is_caip2_format(payload.network) or is_caip2_format(requirements.network):
  File "src/uvd_x402_sdk/networks/base.py", line 519, in is_caip2_format
    return ":" in network
TypeError: argument of type 'NoneType' is not iterable
requirements.network=None             -> 2   (cortocircuito: el payload ya era CAIP-2)
```

La tercera línea es **el reporte de MeshRelay verbatim**, hasta el texto del
`TypeError`. La segunda es la misma causa con la forma real: un payload v2 no es
que traiga `network=None`, es que **no trae ese campo**, así que en Python ni
siquiera llega a `is_caip2_format` — muere en el atributo.

Nada que corregirle al reporte. Lo único que agrega esta medición es que en
Python el defecto tiene **dos caras** (`AttributeError` sobre el sobre v2 crudo,
`TypeError` sobre un `network` nulo) donde TypeScript tenía una sola.

---

## 2. El arreglo, con la regla escrita en el código

`envelope.py`, tres piezas chicas y nombradas:

- **`_network_of_payload(payload)`** — la regla: *la red se lee donde el payload
  la tenga.* Top level si está (y **gana** cuando está, así que un payload v1
  sigue leyendo el suyo), si no `accepted.network`. Un payload que no aporta
  ninguna devuelve `None`, que significa "no hay evidencia CAIP-2", no una
  excepción adentro de la selección de versión.
- **`_is_caip2(network)`** — `is_caip2_format` con la ausencia contestando
  `False`. `is_caip2_format` es `":" in network` y es público: no le cambié la
  firma, el que tolera la ausencia es el llamador, que es donde una red ausente
  es dato y no error.
- **`_read(source, field)`** — lee un campo de un modelo o de un dict. En Python
  el payload v2 llega como dict (no hay `PaymentPayloadV2`) y el v1 como modelo
  Pydantic; una sola función evita dos caminos que se desincronizan.

El tipo público pasa a `PayloadLike = Union[PaymentPayload, Mapping[str, Any]]`,
que es lo que la función de verdad recibe.

**Paridad con TypeScript:** misma regla, mismo orden de preferencia, misma
decisión de tratar la ausencia como "sin evidencia" (`networkOfPayload` en
`src/backend/index.ts`, 2.79.0). El mismo cable produce la misma versión y el
mismo cuerpo en los dos SDK.

Después del arreglo, las cuatro entradas de §1:

```
v1 (network presente)                 -> 2
v2 dict (sin network)                 -> 2
model_construct(network=None)         -> 1
requirements.network=None             -> 2
```

La segunda línea es la que importa y no se traga nada: en esa corrida los
**requirements dicen `base`**, así que el 2 solo pudo salir de
`accepted.network`. Es lo que separa "arreglé la lectura" de "me tragué el campo
que falta".

---

## 3. La prueba discriminante (salida real del rojo)

Con el arreglo sacado —`is_caip2_format(payload.network) or
is_caip2_format(requirements.network)` de vuelta— y los tests nuevos puestos:

```
FFFFF.                                                                   [100%]
================================== FAILURES ===================================
src\uvd_x402_sdk\envelope.py:228: AttributeError: 'dict' object has no attribute 'network'
src\uvd_x402_sdk\envelope.py:228: AttributeError: 'dict' object has no attribute 'network'
src\uvd_x402_sdk\networks\base.py:519: TypeError: argument of type 'NoneType' is not iterable
src\uvd_x402_sdk\envelope.py:228: AttributeError: 'dict' object has no attribute 'network'
src\uvd_x402_sdk\envelope.py:228: AttributeError: 'dict' object has no attribute 'network'
=========================== short test summary info ===========================
FAILED ...::TestAutoSurvivesAV2Payload::test_resolves_instead_of_raising
FAILED ...::TestAutoSurvivesAV2Payload::test_reads_the_chain_id_out_of_accepted_where_v2_keeps_it
FAILED ...::TestAutoSurvivesAV2Payload::test_a_payload_whose_network_is_None_does_not_raise
FAILED ...::TestAutoSurvivesAV2Payload::test_does_not_raise_when_the_requirements_have_no_network_either
FAILED ...::TestAutoSurvivesAV2Payload::test_still_refuses_to_upgrade_on_the_marker_alone
5 failed, 1 passed, 20 deselected in 0.93s
```

Los cinco caen con la traza del defecto real, no con "la función no existe". El
tercero reproduce el `TypeError` del reporte, línea por línea.

**Y el sexto queda verde en los DOS estados**, que es para lo que existe:
`test_a_v1_payload_still_reads_its_own_top_level_network` — el arreglo no puede
empezar a preferir `accepted` sobre un `network` de primer nivel que sí está.
Junto con `test_still_refuses_to_upgrade_on_the_marker_alone` (que en rojo cae
por el crash, y en verde exige que un `accepted` con nombre plano se quede en
v1) cierran los dos lados: tolerar la ausencia no relajó la regla medida.

Commit `a6d23a8`.

---

## 4. El medio arreglo que no entregué: los builders

Con la resolución arreglada medí la línea siguiente, sobre el mismo objeto:

```
resolve(v2 dict) -> 2
build_verify_request_for_version(v2 dict, 2) LANZA:
  File "src/uvd_x402_sdk/envelope.py", line 271, in build_verify_request_for_version
    payload=payload.payload,
AttributeError: 'dict' object has no attribute 'payload'
build_verify(v2 dict red plana, 1) LANZA:
  File "src/uvd_x402_sdk/envelope.py", line 248, in _build_v1
    "paymentPayload": payload.model_dump(by_alias=True),
AttributeError: 'dict' object has no attribute 'model_dump'
```

O sea: `auto` resolvía el payload v2 y el consumidor seguía sin poder construir
el cuerpo — el mismo defecto, dos funciones más allá. Los tres puntos leían el
payload asumiendo el modelo v1 plano.

Ahora el material firmado se lee con `_inner_payload` (bajo `payload`, que es
donde lo guardan las **dos** versiones) y el volcado del sobre v1 con
`_payload_wire`. Dos decisiones:

1. **El payload viaja sin reformar.** Este módulo elige el sobre; no traduce una
   forma de payload en la otra. Quien aplana un header v2 es
   `X402Client.extract_payload` (§5), y meterle esa traducción acá sería tener
   dos traductores que se desincronizan. Es también lo que hace TypeScript.
2. **Un payload sin material firmado se niega nombrando lo que falta.** El
   facilitador contesta `data did not match any variant of untagged enum` sin
   nombrar un campo; ese error ya costó un día una vez.

Camino completo después:

```
resolve(v2 dict) -> 2
build_verify_request_for_version(v2 dict, 2) -> x402Version=2 accepted=True payload={'signature': '0xdead', ...}
build_settle_request_for_version(v2 dict, 2) -> x402Version=2 accepted=True payload={'signature': '0xdead', ...}
build_verify(v2 dict red plana, 1) -> claves=['paymentPayload', 'paymentRequirements', 'x402Version']
```

**Prueba discriminante** (builders vueltos a `payload.payload` /
`payload.model_dump`):

```
FFF.                                                                     [100%]
src\uvd_x402_sdk\envelope.py:308: AttributeError: 'dict' object has no attribute 'payload'
src\uvd_x402_sdk\envelope.py:281: AttributeError: 'dict' object has no attribute 'model_dump'
src\uvd_x402_sdk\envelope.py:308: AttributeError: 'dict' object has no attribute 'payload'
FAILED ...::TestTheBuildersTakeTheSameShapesAsTheResolver::test_builds_the_v2_body_end_to_end_from_a_v2_payload
FAILED ...::TestTheBuildersTakeTheSameShapesAsTheResolver::test_the_v1_envelope_carries_a_v2_payload_unreshaped
FAILED ...::TestTheBuildersTakeTheSameShapesAsTheResolver::test_a_payload_with_no_signed_material_refuses_by_name
3 failed, 1 passed, 26 deselected in 0.73s
```

El que queda verde en los dos estados es
`test_a_PaymentPayload_still_dumps_exactly_as_before`, y mira **los dos sobres**:
el camino del modelo no se movió ni en v1 ni en v2.

Commit `7c81d00`.

---

## 5. Lo que medí y NO cambié: por el cliente, un header v2 sigue saliendo en v1

Verifiqué el camino completo del cliente, como hizo TypeScript. Acá **no da lo
mismo**, y es un hallazgo, no un olvido:

```
extract_payload(header v2).network      -> 'base'
                          .x402Version  -> 2
verify_payment -> sobre x402Version     -> 1 | claves: ['paymentPayload', 'paymentRequirements', 'x402Version']
```

La causa está aguas arriba de todo esto: `X402Client._normalize_v2_envelope`
(`src/uvd_x402_sdk/client.py:504`) aplana el sobre v2 a la forma v1 **y resuelve
el CAIP-2 a nombre plano** (`eip155:8453` → `base`) antes de que la selección de
versión vea nada. Existe por una razón buena y medida —el incidente de
describe-net del 2026-08-12: el propio 402 v2 del SDK pedía un sobre que el
cliente después no sabía parsear— pero su efecto secundario es que **borra la
evidencia CAIP-2** que `auto` usa para decidir.

Resultado: con el MISMO header v2, TypeScript manda sobre v2 y Python manda
sobre v1. Las dos son 200 contra el facilitador 2.10.0 y reducen al mismo pago,
así que hoy nadie se rompe; pero es la única divergencia viva que queda entre
los dos SDK sobre el sobre, y toca el camino más transitado (el header que
produce pay.js).

**No lo toqué a propósito.** Cambiarlo mueve de v1 a v2 el sobre de un camino
que hoy mueve plata (describe-net, firmas de Rabby reales), no es lo que este
encargo pidió, y la decisión —si el aplanado debe preservar el CAIP-2, o si la
selección debe mirar el header crudo antes de aplanar— es de criterio, no
mecánica. Va en *Para c0der* con la medición.

---

## 6. Versión

**0.75.0**, MINOR sobre la publicada 0.74.0. **No publicado, no tageado** — eso
es de c0der.

En comportamiento es un patch: todo cable que resolvía antes resuelve a la misma
versión, y el cuerpo v1 desde un `PaymentPayload` sale byte por byte igual. El
número sube a MINOR porque `resolve_envelope_version` y los dos builders aceptan
una forma de payload que antes reventaba: eso es superficie pública nueva
(`PayloadLike`), no un arreglo interno.

Changelog en `README.md` (§ Changelog); este repo no tiene `CHANGELOG.md` y no
inventé uno. `CLAUDE.md` quedó con la regla nueva y con dos datos vencidos
corregidos: TypeScript cerró el marcador heredado en 2.79.0 (sin publicar), y el
aplanado del §5, medido.

---

## 7. Estado de cierre

| Criterio | Estado |
|---|---|
| Reproducido en runtime antes de tocar, con la traza pegada | ✅ §1 — el `TypeError` del reporte, verbatim, más el `AttributeError` que en Python es la forma real |
| `auto` resuelve las DOS formas, con la regla escrita en el código | ✅ §2 — `_network_of_payload`, top level gana, ausencia = sin evidencia |
| Test discriminante por forma, probado en ROJO | ✅ §3 (5 rojos, 1 guarda verde en los dos estados) y §4 (3 rojos, 1 guarda) |
| README / docstrings corregidos en el mismo commit | ✅ § del sobre en el README con el ejemplo v2, docstring del módulo y de la función, en `a6d23a8` |
| Suite entera en verde | ✅ **852/852**, 0 skips (842 previos + 10 nuevos). `envelope.py` limpio en mypy |
| 0.75.0 con changelog, en su propio commit al final | ✅ `5af8dbe` |
| `git status` limpio, cero push, cero publish | ✅ 4 commits locales |

**Nota de operación** (heredada y confirmada): este entorno tiene plugins de
pytest instalados globalmente que revientan la colección. La suite corre con
`PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PYTHONPATH=src python -m pytest -p pytest_asyncio.plugin`.
Sin `-p pytest_asyncio.plugin` se saltan 186 tests en silencio. Y `PYTHONPATH=src`
no es opcional: este entorno tiene la 0.73.0 instalada en site-packages, así que
sin eso se testea el paquete viejo (`uvd_x402_sdk.__version__` sigue leyendo
`0.73.0` de la metadata instalada aun con el código del worktree cargado — el
módulo sí sale de `src`, verificado por la traza).

**Ruff:** las dos anotaciones nuevas usan `Dict[str, Any]` como todo el archivo,
así que ruff repite ahí su `UP006` preexistente (3 → 5 en el archivo, mismo tipo,
mismo motivo). Convertir el archivo entero a `dict[...]` es una limpieza aparte,
no de este encargo.

---

## Para c0der

**El reporte era cierto y lo reproduje antes de tocar nada.** En Python el
defecto tiene dos caras: `TypeError: argument of type 'NoneType' is not
iterable` con un `network` nulo (tu línea, verbatim) y `AttributeError: 'dict'
object has no attribute 'network'` con un sobre v2 de verdad, que es la forma
que un pagador manda. La causa es la misma que del lado TypeScript y peor que
"no chequea nulos": **un payload v2 no tiene `network` de primer nivel en
absoluto**, así que la función que existe para elegir entre v1 y v2 se caía con
v2. Ya resuelve las dos formas, con la regla escrita en `_network_of_payload` y
la misma que aplica TypeScript 2.79.0.

**Encontré medio arreglo y lo cerré:** con la resolución arreglada, los dos
builders del sobre seguían reventando sobre el mismo objeto (§4). Entregarte una
`auto` que resuelve y un `build_*` que explota una línea después habría sido el
mismo pecado que este encargo denuncia.

**MeshRelay puede sacar los pines de turnstile y multibrain cuando publiques**
—0.75.0 del lado Python, 2.79.0 del lado TypeScript—, no antes.

**El cable que dejó listo el worker de TypeScript ya se puede agregar.** Su
handoff §Para c0der dice que no metió el cable `payloadShape: 'v2'` en
`ENVELOPE_CASES` de `scripts/xlang/cross-language-conformance.mjs` para no
entregarte rojo mientras Python no estuviera. Python ya está. **Ojo con una cosa
al agregarlo:** `scripts/xlang/agent.py` arma hoy un `PaymentPayload(...)` con
`network` siempre; para ese cable el agente Python tiene que pasar el **dict v2
crudo** (`{x402Version, resource, accepted, payload}`, sin `network` de primer
nivel), que es lo que `resolve_envelope_version` acepta desde 0.75.0. Ese cambio
es del repo de TypeScript, donde viven los tres archivos; no lo toqué.

**Lo que medí y te dejo para tu criterio, sin tocarlo** (§5): por
`X402Client.extract_payload`, un header v2 real sale en sobre **v1**, porque
`_normalize_v2_envelope` (`client.py:504`) aplana el sobre y resuelve
`eip155:8453` → `base` antes de que la selección vea nada. TypeScript, con el
mismo header, manda v2. Las dos son 200 hoy y reducen al mismo pago, así que no
hay nadie roto — pero es la única divergencia viva que queda entre los dos SDK
sobre el sobre, y está en el camino que más se usa. Cambiarlo mueve de v1 a v2
un camino con plata real (describe-net) y es decisión tuya, no mecánica. Las dos
salidas posibles: que el aplanado preserve el CAIP-2, o que la selección mire el
header crudo antes de aplanar.

**Lo que no hice:** ni push, ni publish, ni tag, ni deploy, ni subagentes. 4
commits locales en `0xultravioleta/py-auto`, worktree limpio. 0.75.0 con
changelog, lista para que le pongas el tag.
