# Capa 6 · Errores, reintentos e idempotencia

Especificación de interop del stack, **v1**. Esquema: [`schemas/uvd-error.schema.json`](schemas/uvd-error.schema.json).
Fixtures: [`fixtures/uvd-error/`](fixtures/uvd-error/).

Cada app del stack tiene hoy su propio vocabulario de error y su propia política de reintento. Lo
que se aprende a golpes es siempre lo mismo: **qué se puede repetir, y si la plata se movió**. Esta
capa pone esas dos respuestas en un sobre común que cualquier cliente lee igual.

## R6.1 · El sobre es aditivo: una clave nueva, `uvd_error`, en la raíz del cuerpo JSON

Una respuesta de error lleva `uvd_error` **además** de lo que la app ya devolvía (`error`, `detail`,
`code`, `reason`, `retryable`, `retryAfter`, `safeToRetry`...). Nada de lo existente se renombra ni
se quita.

- **Por qué:** hay clientes que ya leen esas claves; reemplazarlas rompe a todos a la vez, y agregar
  una clave nueva no rompe a ninguno.
- **Sale de:** medición de las claves de error de las apps del stack (ninguna usa `uvd_error`).

## R6.2 · `uvd_error` nunca usa ni pisa `error` ni `detail`

El sobre vive solo bajo su clave. Una app no mueve `error` ni `detail` para hacerle lugar. Y una
respuesta cuya **forma** es la marca de un perfil legado no lleva el sobre: el cuerpo de un bloqueo
de IP del perfil [L9](10-perfiles-legados.md#l9--bloqueo-de-ip-marcado-por-error-en-la-raíz) se
queda con su única clave, `error`.

- **Por qué:** en al menos una API del stack, un 403 cuyo cuerpo tiene solo `error` es la marca de un
  bloqueo de IP (L9); agregarle `uvd_error` le cambiaría la forma y el bloqueo dejaría de
  reconocerse. Otras apps usan `error` en la raíz para cualquier falla. La regla no prohíbe `error`
  en el stack: prohíbe que el sobre lo toque.

## R6.3 · Los campos

| Campo | Tipo | Obligatorio | Qué es |
|---|---|---|---|
| `code` | string `^[a-z][a-z0-9_]{0,63}$` | sí | Código estable que un programa compara. |
| `message` | string no vacío | sí | Texto para una persona. Ningún programa decide por él. |
| `retryable` | boolean | sí | Ver R6.5. |
| `retry_after_s` | entero ≥ 0 | según R6.6 | Segundos a esperar. |
| `spent` | `no` \| `maybe` \| `yes` | sí | Ver R6.4. |
| `next_action` | vocabulario cerrado | sí | Ver R6.7. |
| `docs` | URL `https://` | no | Página que explica el código. |
| `details` | objeto | no | Datos propios de la app (la transacción, el recibo). |

Dentro de `uvd_error` no hay más claves (`additionalProperties: false`): lo propio de la app va en
`details`.

- **Por qué:** con el objeto cerrado, un `retry_after` o un `retryAfter` escrito por error es un
  documento inválido en la prueba del emisor, y no un campo que el cliente ignora en silencio.

## R6.4 · `spent` dice si esta petición movió plata

- `no`: está probado que esta petición no movió nada (fue rechazada antes de aceptarse).
- `maybe`: pudo moverla (se transmitió una autorización, hubo un timeout después del envío, la
  transacción se difundió sin recibo).
- `yes`: la movió.

- **Por qué:** es el bit que distingue «no gastaste» de «puede que hayas gastado»; confundirlos cuesta
  un cobro doble. Una autorización EIP-3009 transmitida es plata al portador.
- **Sale de:** describe-net (distinción explícita entre «no se pagó» y «reenviá el MISMO
  pago») y la lista blanca de `money_safety` de este SDK.

## R6.5 · Solo se reintenta con `retryable` verdadero, y el esquema hace imposible contradecirlo

`retryable` describe el **reintento ciego**: `retryable: true` significa que repetir **la misma
petición**, más tarde, sin mirar nada más, es seguro y puede funcionar. El esquema exige que
`retryable: true` vaya con `next_action: "retry"`, y que `next_action: "retry"` vaya con
`retryable: true` y `spent: "no"`.

Un cliente **DEBE** reintentar a ciegas solo si `retryable` es verdadero **y** `spent` es `no`. Con
el esquema cumplido, basta con mirar `retryable`.

La excepción es explícita y tiene nombre: `next_action: "resend_same_payment"` (con `retryable:
false` y `spent: "maybe"`) pide volver a presentar **el mismo** pago, no firmar otro. No es un
reintento ciego: es una instrucción que el cliente sigue solo si la entiende, y que no puede cobrar
dos veces porque la autorización es la misma (el caso «pago en curso» de la tabla de abajo).

- **Por qué:** un cliente que solo mira `retryable` no puede cobrar dos veces, porque un emisor no
  puede escribir `retryable: true` junto a `spent: "maybe"`.
- **Sale de:** KarmaKadabra («solo se reintenta lo que el servidor declara previo a la
  aceptación») y la política de reintento de `/settle` de este SDK.

## R6.6 · `retry_after_s` dice cuánto esperar, y coincide con `Retry-After`

- Con `next_action: "retry"`, `retry_after_s` es **obligatorio** (0 = ya).
- Con `next_action: "resend_same_payment"` es opcional: cuándo volver a presentar el mismo pago.
- Con cualquier otra acción, **no va**.
- Cuando la respuesta lleva la cabecera `Retry-After`, `retry_after_s` **DEBE** valer lo mismo (en
  segundos). *El esquema no puede comparar el cuerpo con la cabecera: lo verifica el runner de
  conformidad (ver [README](README.md#reglas-que-los-esquemas-no-pueden-expresar)).*

- **Por qué:** un reintento sin plazo lo adivina cada cliente, y los que adivinan corto terminan
  bloqueados por el límite de tasa.

## R6.7 · `next_action`, en vocabulario cerrado

| Valor | Qué hace quien llama |
|---|---|
| `retry` | Repite la misma petición después de `retry_after_s`. |
| `resend_same_payment` | Vuelve a presentar **el mismo** pago (la misma cabecera `X-PAYMENT`); nunca firma otro. |
| `check_transaction` | Revisa la transacción o el recibo en `details` antes de cualquier otra cosa. |
| `pay` | Paga: la respuesta trae el desafío x402. |
| `authenticate` | Firma la petición (ERC-8128) o consigue una credencial. |
| `fix_request` | La petición está mal; repetirla igual no sirve. |
| `stop` | Terminal: no hay nada que repetir (por ejemplo, ya se procesó). |
| `contact_operator` | Nada que el cliente pueda hacer. |

- **Por qué:** un texto libre en `next_action` lo tiene que interpretar un modelo o una persona; un
  valor cerrado lo ejecuta un programa. Un valor nuevo es un cambio de versión menor de la
  especificación.

## R6.8 · Cada escritura documenta su sello

La documentación de cada operación que escribe nombra **el campo que prueba el efecto**: `tx_hash`,
el id del recibo, `authored_by`. Un cliente da por hecha la escritura cuando tiene el sello, no
cuando recibe un 200.

- **Por qué:** «el 200 no es el sello»: hay respuestas exitosas cuyo efecto todavía no ocurrió o
  ocurrió en otra parte.
- **Sale de:** KarmaKadabra (el éxito se mide por el artefacto: hash de la transacción,
  `authored_by`).

## R6.9 · Dónde viaja el sobre en MCP

- Resultado de una tool que falló: `isError: true` y el sobre en `structuredContent.uvd_error`.
- Error del protocolo JSON-RPC: el sobre en `error.data.uvd_error`.

- **Por qué:** una fachada MCP sobre el REST ([R9.1](09-mcp.md)) reenvía el mismo sobre sin
  traducirlo, y el cliente lo lee en un lugar fijo según el tipo de falla.

## Casos frecuentes (no normativo)

| Situación | HTTP | `code` (ejemplo) | `retryable` | `spent` | `next_action` | `retry_after_s` |
|---|---|---|---|---|---|---|
| Dependencia caída antes de aceptar nada | 503 | `nonce_store_unavailable` | `true` | `no` | `retry` | sí |
| Límite de tasa | 429 | `rate_limited` | `true` | `no` | `retry` | sí |
| Pago en curso con este mismo vínculo | 503 | `settlement_in_progress` | `false` | `maybe` | `resend_same_payment` | opcional |
| Difundida sin recibo | 500 | `settlement_unconfirmed` | `false` | `maybe` | `check_transaction` | no |
| La autorización ya se liquidó | 409 | `authorization_already_settled` | `false` | `yes` | `stop` | no |
| Hace falta pagar | 402 | `payment_required` | `false` | `no` | `pay` | no |
| Firma ausente o inválida | 401 | `signature_invalid` | `false` | `no` | `authenticate` | no |
| Petición mal formada | 400 | `invalid_request` | `false` | `no` | `fix_request` | no |

Los códigos de ERC-8128 (`signature_invalid`, `nonce_replayed`...) y los del facilitador
(`authorization_already_settled`, `settlement_in_progress`...) conservan su ortografía: el sobre los
transporta, no los renombra.
