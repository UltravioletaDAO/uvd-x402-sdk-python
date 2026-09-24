# Capa 1 · Descubrimiento y registro

Especificación de interop del stack, **v1**. Esquema: [`schemas/manifest.schema.json`](schemas/manifest.schema.json).
Fixtures: [`fixtures/uvd-stack/`](fixtures/uvd-stack/).

Hoy una app nueva se da de alta a mano en varios registros que no se derivan uno de otro, y cada
cliente guarda la URL, la política de firma y los límites de las demás como constantes. Esta capa
pone todo eso en **un documento por app**, que la app genera y publica, y del que se derivan los
registros.

## R1.1 · Cada app sirve `/.well-known/uvd-stack.json` en el host de su API

El manifiesto vive en el host de la API (`https://api.<app>/.well-known/uvd-stack.json`), no en el
sitio. El sitio lo enlaza.

- **Por qué:** en varias apps la API y el sitio son hosts distintos, y el sitio es una SPA que contesta
  200 con su `index.html` en cualquier ruta: un manifiesto en el sitio sería indistinguible de una
  página que no existe.

## R1.2 · Se genera desde el código, de la misma fuente que las demás superficies

El manifiesto no se escribe a mano: sale de la misma definición que alimenta el 402, `/pricing`,
`/.well-known/x402`, los documentos de OAuth y el `tools/list` del MCP. Lo enlazan el `api-catalog`
(RFC 9727) y el `llms.txt` de la app, y él enlaza a ellos en `links`.

- **Por qué:** una superficie escrita a mano contradice a la que cobra el día que una de las dos
  cambia; generadas de una fuente, no pueden contradecirse.
- **Sale de:** describe-net (una sola fuente alimenta el 402, `/pricing`, `/.well-known/x402` y el
  bazar) y Execution Market (sus documentos de auth salen de una sola definición).

## R1.3 · Se sirve como `application/json`, y un lector exige ese tipo

La respuesta lleva `Content-Type: application/json`. Un lector **DEBE** exigir ese tipo **y** un
cuerpo distinto del que el mismo host devuelve en `/`; si no, no hay manifiesto.

- **Por qué:** un HTML con 200 no es un manifiesto. Sin esta regla, la página de error de una SPA se
  lee como un manifiesto vacío.

## R1.4 · Qué lleva

| Clave | Obligatoria | Qué es | Regla |
|---|---|---|---|
| `schema` | sí | `"uvd.stack/1"` | R1.4 |
| `app` | sí | Id de la app | R1.10 |
| `name` | no | Nombre para personas | R1.4 |
| `version` | sí | Versión desplegada | R1.4 |
| `git_sha` | sí | Commit desplegado, hex en minúsculas | R1.4 |
| `generated_at` | no | Cuándo se generó (RFC 3339, UTC, con `Z`) | R1.2 |
| `endpoints` | sí | Las puertas: `api` (obligatoria), `mcp`, `event_inbox`, `event_cursor` | R1.5, R1.6 |
| `snapshots` | no | Instantáneas publicadas | [R5.14](05-eventos.md) |
| `identity` | sí | Wallet de servicio, `agentId` por red, hosts de reputación | [R2.2](02-identidad.md) |
| `payments` | sí | Si cobra, y dónde está su `/.well-known/x402` | R1.7 |
| `rate_limits` | sí | Los límites que aplica (puede ser `[]`) | [R7.3](07-observabilidad.md) |
| `events` | sí | Tipos que emite y consume, y los sobres que habla | [R5.12](05-eventos.md) |
| `health` | sí | URLs de `live` y `ready` | [R7.5](07-observabilidad.md) |
| `links` | no | `llms_txt`, `api_catalog`, `agent_card`, `openapi`... | R1.2 |

Cada puerta de `endpoints` es `{url, auth, erc8128?}`: la URL, los modos de autenticación que acepta
y, si uno de ellos es `erc8128`, la política de su verificador ([R3.4](03-autenticacion.md)).

Una clave desconocida en el manifiesto lo invalida (el esquema es estricto con el emisor). Quien lo
**lee** ignora las claves que no conoce: una versión menor de esta especificación puede agregar
claves opcionales.

- **Por qué:** es lo mínimo para que otra app sepa, de una vez, quién es, dónde atiende, qué acepta,
  qué cobra, cuánto aguanta, qué eventos emite y si está viva.

## R1.5 · Solo puertas públicas, en `https`, y nada que salga de un secreto

Toda URL del manifiesto es `https://`, sin credenciales en la URL, y **nunca** una ruta interna (el
esquema rechaza un segmento `/internal`, en mayúsculas o minúsculas). El manifiesto no publica nada
que se derive de un secreto.

- **Por qué:** el manifiesto es público y lo leen agentes de afuera; una ruta interna publicada es una
  invitación, y un valor derivado de un secreto ayuda a adivinarlo.

## R1.6 · Cada puerta declara sus modos de autenticación, en vocabulario cerrado

`auth` es una lista no vacía, sin repetidos, de:

| Modo | Qué significa |
|---|---|
| `none` | Se puede llamar sin credencial |
| `erc8128` | Petición firmada con ERC-8128 ([capa 3](03-autenticacion.md)) |
| `oauth2.1` | Token OAuth 2.1 (clientes MCP genéricos, [R3.11](03-autenticacion.md)) |
| `x402` | El pago es la credencial |
| `api_key` | Llave en una cabecera (perfil legado; una app nueva no lo ofrece entre servicios) |
| `hmac` | Firma HMAC de un perfil legado ([capa 10](10-perfiles-legados.md)) |

- **Por qué:** quien llama tiene que saber cómo entrar sin leer el código del otro; con un vocabulario
  abierto, cada app inventaría su nombre para lo mismo.

## R1.7 · Quien cobra apunta a su `/.well-known/x402`; quien no, lo declara

`payments.charges: true` exige `payments.x402` con la URL de su documento x402. `charges: false`
prohíbe `x402`: una app que no cobra lo dice, y no apunta a nada.

- **Por qué:** «no aplica» declarado se distingue de «se olvidó de declararlo».
- **Sale de:** Emporium (declara «no aplica» en las superficies de pago que no tiene).

## R1.8 · El SDK trae un directorio del stack

El SDK tiene **una** tabla de URLs por omisión de las apps del stack, un override por app con **una**
variable (`UVD_<APP>_URL`, [R8.1](08-configuracion.md)), y lee el manifiesto de cada app al arrancar,
con caché y **la última copia buena** si la lectura falla. El lector aplica R1.3.

- **Por qué:** es el patrón con el que una app ya lee las capacidades del facilitador al arrancar (una
  vez, con caché, conservando la copia anterior si falla), llevado a todo el stack; un cliente que no
  arranca porque otra app está caída es un acoplamiento que nadie pidió.
- **Sale de:** meshrelay (lee `/supported` del facilitador al arrancar, con caché y última copia
  buena).

## R1.9 · El registro se deriva, no se copia

Los registros de la casa (el mapa del ecosistema, el catálogo de Emporium, las allowlists) se
**validan contra** los manifiestos. Cada fila sigue siendo una decisión de una persona; lo que deja
de existir es la copia a mano del dato.

- **Por qué:** seis registros escritos a mano que no se derivan uno de otro divergen, y el que diverge
  no avisa.

## R1.10 · El id de la app es uno solo

`app` es `^[a-z][a-z0-9-]{1,62}$` (por ejemplo `execution-market`, `describe-net`, `meshrelay`,
`karmakadabra`). Es el mismo valor en el manifiesto, en `source` y en el prefijo de `type` de sus
eventos ([R5.2](05-eventos.md)), en su `User-Agent` ([R7.1](07-observabilidad.md)), en el nombre de
su secreto ([R8.2](08-configuracion.md)) y, en mayúsculas, en `UVD_<APP>_URL`.

- **Por qué:** el mismo nombre en todas partes es lo que permite cruzar un evento, una llamada y un
  secreto sin una tabla de equivalencias.
