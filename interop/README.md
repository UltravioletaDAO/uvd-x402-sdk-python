# Especificación de interop del stack · v1

Cómo se hablan las apps del stack de Ultravioleta DAO (Execution Market, meshrelay, describe-net,
KarmaKadabra, Emporium, el facilitador y las que vengan): un contrato escrito una vez, con esquemas y
fixtures, que implementan los SDK de Python y de TypeScript y un crate de Rust.

**Versión de la especificación: v1** (2026-09-24).

## El patrón, en una línea

Toda app del stack **publica un manifiesto**; **firma con su propia wallet de servicio** (ERC-8128,
verificada contra una lista de direcciones públicas, sin secretos compartidos) cada escritura, cada
evento y cada lectura cuyo receptor necesita saber quién llama, y **nunca una lectura pública**;
**mueve plata solo por x402 y el facilitador** con `Idempotency-Key`; y habla con **un sobre común de
eventos y de errores**. Entre las apps de la casa no hay un servicio en el medio: se hablan directo.

## Cómo se lee

- La especificación está en Markdown, JSON Schema (draft 2020-12) y fixtures JSON. No depende de
  ningún lenguaje.
- Cada regla tiene un id, **R<capa>.<n>**, una línea con su **por qué** y, cuando sale de una app que
  ya la resolvió, el nombre de esa app (**sale de**). Los fixtures y los vectores citan el id.
- **DEBE / NO DEBE** es obligatorio; **DEBERÍA** es lo esperado salvo una razón escrita; **PUEDE** es
  opcional (como MUST, MUST NOT, SHOULD y MAY del RFC 2119).
- Las claves de los documentos del contrato van en inglés, como x402 y MCP: los leen agentes de
  afuera. La prosa, en español.
- La especificación describe **contratos**. Lo que una app todavía no cumple vive en el backlog de esa
  app, no acá.

## Índice

| Archivo | Capa | Qué fija |
|---|---|---|
| [01-descubrimiento.md](01-descubrimiento.md) | 1 | El manifiesto `/.well-known/uvd-stack.json`, el directorio del stack en el SDK y el registro derivado |
| [02-identidad.md](02-identidad.md) | 2 | La wallet de servicio, `agentId` ERC-8004 por red y los hosts de reputación, declarados en un lugar |
| [03-autenticacion.md](03-autenticacion.md) | 3 | Qué se firma (toda escritura y toda lectura que necesita identidad; nunca una lectura pública), la authority propia, los presets y `evento-s2s` |
| [04-pago.md](04-pago.md) | 4 | x402 por el SDK, la `Idempotency-Key` persistida antes de llamar y el veredicto de tres estados |
| [05-eventos.md](05-eventos.md) | 5 | El sobre `uvd.event/1`, la entrega con outbox y dead-letter, el cursor, y los perfiles «instantánea publicada» y «la cadena es el cursor» |
| [06-errores.md](06-errores.md) | 6 | El sobre aditivo `uvd_error`: qué se puede repetir y si la plata se movió |
| [07-observabilidad.md](07-observabilidad.md) | 7 | `User-Agent`, `traceparent`, límites publicados, salud partida y la disciplina de los sondeos |
| [08-configuracion.md](08-configuracion.md) | 8 | URLs que no son secretos, un secreto de interop por app y ninguno compartido |
| [09-mcp.md](09-mcp.md) | 9 | La cara MCP: fachada sobre el REST, el mismo auth, `_meta["uvd/clase"]` y `outputSchema` |
| [10-perfiles-legados.md](10-perfiles-legados.md) | — | Lo que ya corre, descrito por su formato en el cable, para migrar sin cortar a nadie |
| [11-alta-de-una-app.md](11-alta-de-una-app.md) | — | El recorrido de una app nueva, paso por paso |
| [`schemas/`](schemas/) | — | JSON Schema del manifiesto, de `uvd.event/1` y de `uvd_error` |
| [`fixtures/`](fixtures/) | — | Documentos válidos e inválidos por esquema, y [`cases.json`](fixtures/cases.json) con lo que se espera de cada uno |

## Esquemas

| Esquema | `$id` | Valida |
|---|---|---|
| [`manifest.schema.json`](schemas/manifest.schema.json) | `https://ultravioletadao.xyz/interop/v1/manifest.schema.json` | El manifiesto de una app (`schema: "uvd.stack/1"`) |
| [`uvd-event-1.schema.json`](schemas/uvd-event-1.schema.json) | `https://ultravioletadao.xyz/interop/v1/uvd-event-1.schema.json` | Un evento `uvd.event/1` |
| [`uvd-error.schema.json`](schemas/uvd-error.schema.json) | `https://ultravioletadao.xyz/interop/v1/uvd-error.schema.json` | Un cuerpo de error que lleva `uvd_error` (el objeto está en `$defs/uvd_error`) |

- El `$id` es un identificador estable, no una URL de descarga: los esquemas se vendorean
  (ver [Vendoreo](#vendoreo)). Moverlo es un cambio incompatible.
- **Estrictos con quien emite, tolerantes quien lee.** Los esquemas cierran los objetos
  (`additionalProperties: false`) para que un typo del emisor falle en su prueba. Quien **lee** un
  documento ignora las claves que no conoce y trata un valor desconocido de un vocabulario cerrado
  como el más conservador (un `next_action` desconocido es `stop`; un modo de `auth` desconocido no se
  usa).
- **Patrones portables.** `pattern` significa lo que dice JSON Schema: una expresión ECMA-262, la
  que aplican Ajv y el crate `regex` de Rust. Las de estos esquemas usan el subconjunto que además
  `re` de Python lee igual: `[0-9]` en lugar de `\d`, `[.]` en lugar de `\.`, sin lookaround, y las
  únicas barras son `\n` y `\r`. Una diferencia no se puede evitar con el subconjunto: el `$` de
  Python también acepta un salto de línea **final**, así que en Python `"0x" + 40 hex + "\n"`
  pasaría `^0x[0-9a-f]{40}$`. Por eso cada esquema tiene `$defs/una_linea` (ningún `\n` ni `\r`), y
  todo string con patrón anclado la referencia: el documento se rechaza en cualquier motor.
- **Fechas.** Los patrones de fecha acotan mes, día (01 a 31), hora, minuto y segundo; que la fecha
  exista (un 30 de febrero) no se puede decir con una expresión regular y lo verifica el runner.
- Ninguna regla depende de `format`, que en draft 2020-12 es solo una anotación.

## Fixtures

Cada esquema tiene fixtures **válidos** (ejemplos completos que se pueden copiar) e **inválidos**, uno
por cada regla que el esquema hace cumplir. [`fixtures/cases.json`](fixtures/cases.json) dice en
`schemas` dónde está cada esquema y en qué carpeta están sus fixtures, y en `cases` lista todos:

```json
"schemas": {
  "manifest": {"schema": "schemas/manifest.schema.json", "fixtures": "uvd-stack"},
  "uvd-event-1": {"schema": "schemas/uvd-event-1.schema.json", "fixtures": "uvd-event-1"},
  "uvd-error": {"schema": "schemas/uvd-error.schema.json", "fixtures": "uvd-error"}
}
```

Los fixtures del manifiesto están en `uvd-stack/` (el nombre del documento) y no en `manifest/`: el
`.gitignore` de un repo de Python suele ignorar `MANIFEST`, y en un sistema de archivos que no
distingue mayúsculas eso se traga una carpeta `manifest/` sin avisar. Un repo que vendorea lee el mapa;
no adivina carpetas.

```json
{
  "file": "uvd-stack/invalid/authority-con-puerto-443.json",
  "schema": "manifest",
  "valid": false,
  "rule": "R3.3",
  "why": "Un puerto por omisión en la authority configurada no coincide con lo que firma el cliente.",
  "errors": [{"keyword": "not", "instance_path": "/endpoints/api/erc8128/authorities/0"}]
}
```

**Cómo se compara, en cualquier lenguaje.** Se valida con todos los errores (en Ajv,
`allErrors: true`). Cada error se reduce a su palabra clave de JSON Schema, el JSON Pointer de la
instancia y, para `required`, la propiedad que falta. Se **descartan** los errores de las palabras
que solo envuelven el veredicto de otra: `if`, `then`, `else`, `allOf`, `anyOf`, `oneOf`, `$ref` y
`propertyNames` (un validador los informa y otro no: Ajv informa el `if` que falló, `jsonschema`
informa solo la palabra de adentro). Lo que queda tiene que ser **igual al conjunto** que lista el
caso: ni uno más, ni uno menos. Así un fixture no puede romper dos reglas y esconder una tercera.
Ningún caso lista una palabra envoltorio.

En el repo de origen (`uvd-x402-sdk-python`) los corre `tests/test_interop_schemas.py`, que además:

- **borra cada restricción de cada esquema, una por vez, y exige que algún fixture se ponga rojo**:
  una restricción sin fixture que la cuide no entra;
- corre `pattern` con la semántica de ECMA-262 y, aparte, comprueba que `jsonschema` de fábrica (con
  el `$` de Python) da el mismo veredicto válido o inválido en todos los casos.

```bash
pip install -e ".[dev]"
python -m pytest -q tests/test_interop_schemas.py
```

## Reglas que los esquemas no pueden expresar

JSON Schema no compara un campo con otro documento, ni con una cabecera, ni con el pasado. Estas
reglas las verifica el runner de conformidad del SDK, no el esquema:

| Regla | Qué verifica |
|---|---|
| [R1.3](01-descubrimiento.md) | El manifiesto se sirve como `application/json` y su cuerpo es distinto del de `/` |
| [R1.10](01-descubrimiento.md) | El mismo id de app en el manifiesto, en `source` y en el prefijo de `type` |
| [R3.1](03-autenticacion.md) | Ningún GET a una ruta pública sale firmado |
| [R3.3](03-autenticacion.md) | Cada authority de una puerta es el host de alguna puerta del mismo manifiesto |
| [R5.2](05-eventos.md) | El primer segmento de `type` es `source` |
| [R5.3](05-eventos.md) | `sequence` crece por `(source, subject.kind, subject.id)` |
| [R5.5](05-eventos.md) | La fecha de `occurred_at` (y de `generated_at`) existe: no hay 30 de febrero |
| [R5.9](05-eventos.md) | Un reintento de un evento ya procesado recibe 200 `already_processed`, no 409 |
| [R3.7](03-autenticacion.md) | Un reintento de entrega lleva un nonce nuevo y no es rechazado como repetido |
| [R6.6](06-errores.md) | `retry_after_s` es igual a la cabecera `Retry-After` cuando las dos están |
| [R7.6](07-observabilidad.md) | Un sondeo contra producción corre en serie, con ≥ 1 s de pausa, y corta en el primer 429 o en el tercer 401 |

## Vendoreo

El SDK de TypeScript y el crate de Rust copian `interop/` **desde un commit de `main`** de este repo,
nunca desde la punta de una rama, y dejan al lado un `ORIGEN.json`:

```json
{
  "repo": "UltravioletaDAO/uvd-x402-sdk-python",
  "commit": "<sha de main>",
  "path": "interop",
  "files": {"schemas/manifest.schema.json": "<sha256>", "...": "..."}
}
```

Un test de drift en el repo que vendorea compara los sha256 contra los archivos copiados y falla si
alguien los editó a mano. Para actualizar, se vuelve a copiar desde un commit nuevo de `main`.

## Versionado

- **v1.x** agrega sin romper: claves opcionales nuevas, valores nuevos en un vocabulario cerrado,
  reglas nuevas que no invalidan documentos que antes eran válidos. Los esquemas conservan su `$id`.
- **v2** es un cambio incompatible: nombres nuevos (`uvd.stack/2`, `uvd.event/2`), esquemas nuevos
  con `/v2/` en el `$id`, y los de v1 se quedan mientras alguien los hable.

## Migración

Lo que ya corre se describe como perfil ([capa 10](10-perfiles-legados.md)) y convive con el
contrato: un receptor acepta el dialecto viejo y el nuevo, y cuenta cuál llega. **Primero se agrega,
después se retira, y se retira midiendo**: nada se retira hasta 7 días seguidos de cero uso del camino
viejo.
