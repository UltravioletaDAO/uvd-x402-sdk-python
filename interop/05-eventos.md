# Capa 5 · Eventos entre apps

Especificación de interop del stack, **v1**. Esquema: [`schemas/uvd-event-1.schema.json`](schemas/uvd-event-1.schema.json).
Fixtures: [`fixtures/uvd-event-1/`](fixtures/uvd-event-1/).

Hoy el único contrato de eventos del stack es de una app a otra y exige una tarea: un evento que no
es de una tarea no cabe. Esta capa da un sobre genérico, una entrega con garantías, un cursor que es
la fuente de verdad, y dos perfiles para lo que no pasa por HTTP.

## El sobre `uvd.event/1`

### R5.1 · Los campos

| Campo | Obligatorio | Qué es |
|---|---|---|
| `schema` | sí | `"uvd.event/1"` |
| `event_id` | sí | UUID en minúsculas. Con `source`, la clave de deduplicación (R5.9) |
| `source` | sí | Id de la app que emite ([R1.10](01-descubrimiento.md)) |
| `type` | sí | `<app>.<sujeto>.<verbo>` (R5.2) |
| `subject` | sí | `{kind, id}`: sobre qué es el evento |
| `sequence` | sí | Entero ≥ 0 que crece por sujeto (R5.3) |
| `occurred_at` | sí | Cuándo pasó (R5.5) |
| `actor` | sí | Quién lo causó: `{kind: wallet \| app \| system, id, role?}` |
| `economic` | no | La plata que involucra: `{amount, currency, network, tx_hash?}` (R5.4) |
| `payload` | sí | Objeto con el cuerpo propio del tipo de evento |
| `trace_id` | sí | El trace-id W3C de la traza (R5.6) |

El sobre es cerrado: lo propio de la app va en `payload`, no en la raíz.

- **Por qué:** con `subject` genérico entra cualquier cosa que una app necesite contar (una tarea, un
  canal, un feedback, una orden), y con el sobre cerrado un campo mal escrito es un documento
  inválido y no un dato que se pierde.
- **Sale de:** `em.event.v1` de Execution Market (`event_id`, `sequence`, `trace_id`, montos como
  string), generalizado a cualquier sujeto.

### R5.2 · `type` es `<app>.<sujeto>.<verbo>`, y empieza por `source`

El primer segmento de `type` **DEBE** ser igual a `source`. El segundo **DEBERÍA** ser igual a
`subject.kind`. Los segmentos van en minúsculas; sujeto y verbo admiten `_`
(`execution-market.task.assign_failed`).

- **Por qué:** con el emisor dentro del tipo, dos apps pueden tener un `task.created` sin chocar, y un
  consumidor filtra por prefijo.

### R5.3 · `sequence` crece por sujeto

Para un mismo `(source, subject.kind, subject.id)`, cada evento nuevo lleva una `sequence` mayor que
la anterior. Puede haber huecos. Es un entero seguro en JavaScript (≤ 2⁵³ − 1).

- **Por qué:** el receptor ordena por sujeto y descarta lo que llega tarde (R5.10); con huecos
  permitidos, el emisor puede usar un contador por sujeto o un reloj monótono.

### R5.4 · Los montos van como string decimal

`economic.amount` es un string (`"0.100000"`), nunca un número JSON; `currency` es el símbolo en
mayúsculas (`USDC`); `network` es CAIP-2 (`eip155:8453`); `tx_hash`, si ya lo hay, es el sello
(R5.15).

- **Por qué:** un float pierde precisión en el camino, y un monto que llega distinto de como salió es
  un error de plata.

### R5.5 · `occurred_at` es RFC 3339 en UTC, con `Z`

- **Por qué:** comparar fechas con offsets distintos obliga a normalizar en cada consumidor; con una
  sola forma se comparan como texto.

### R5.6 · `trace_id` es el trace-id W3C

32 hex en minúsculas, distinto de todo ceros: el mismo valor que el trace-id del `traceparent` de la
petición que produjo el evento ([R7.2](07-observabilidad.md)).

- **Por qué:** así un evento y las llamadas que lo causaron se siguen con el mismo id.

## Entrega

### R5.7 · El emisor escribe en su outbox en la misma transacción que el cambio

El evento se guarda en un outbox **en la misma transacción** que el cambio de estado que lo produce.
Un despachador lo lee del outbox y lo entrega con un POST al `event_inbox` del receptor (la URL sale
del manifiesto del receptor), firmado con ERC-8128 por la wallet de servicio del emisor, con el preset
`evento-s2s` ([R3.6](03-autenticacion.md)) y la cabecera `traceparent`. El outbox es un protocolo con
adaptadores: puede ser una tabla con lease en la base o cualquier otro almacén.

- **Por qué:** un evento que se publica fuera de la transacción se pierde si el proceso muere entre
  el commit y el envío, o se anuncia algo que nunca se guardó.

### R5.8 · El despachador reintenta con backoff y, al agotar, manda a dead-letter

| Qué contesta el receptor | Qué hace el despachador |
|---|---|
| 200, 201, 202 o 204 | Entregado. Fin |
| Una respuesta con `uvd_error.next_action` en `fix_request` o `stop` | Dead-letter ya: repetirla no la cambia |
| Cualquier otra cosa (5xx, 429, 408, red, timeout, otros 4xx) | Reintenta |

Calendario: hasta **6 intentos**, con esperas de 1, 2, 4, 8 y 16 s más hasta un 10 % de azar, y
**30 s** de timeout por intento; si la respuesta trae `Retry-After` mayor, espera eso. **Cada intento
se firma de nuevo, con un nonce nuevo** ([R3.7](03-autenticacion.md)). Agotados los intentos, el
evento va a un dead-letter **persistido**, desde el que se puede reentregar.

- **Por qué:** una entrega de un solo intento pierde el evento ante el primer corte; y un dead-letter
  que no se guarda es una pérdida que además no avisa.
- **Sale de:** Execution Market (el emisor de su producto de webhooks: reintentos con backoff y
  dead-letter) y meshrelay (su bandeja de mensajes: reintento durable y dead-letter).

### R5.9 · El receptor es idempotente por `(source, event_id)`

Un evento que ya procesó se contesta **200 `{"status": "already_processed"}`**, nunca 409.

- **Por qué:** un reintento tras una respuesta perdida es el caso normal de toda entrega con
  reintentos; si se contestara con un error, el despachador lo mandaría a dead-letter o lo daría por
  entregado sin saber si se procesó.
- **Sale de:** meshrelay (idempotente por `event_id`, 200 `already_processed`).

### R5.10 · El receptor ordena por `sequence`

Un evento cuya `sequence` es menor o igual a la última que el receptor aplicó para ese sujeto se
contesta **200 `{"status": "stale_sequence"}`** y no se aplica.

| Respuesta del receptor | Cuándo |
|---|---|
| `200 {"status": "processed"}` o `202 {"status": "accepted"}` | Lo aplicó o lo encoló |
| `200 {"status": "already_processed"}` | Ya lo tenía (R5.9) |
| `200 {"status": "stale_sequence"}` | Llegó después de uno más nuevo del mismo sujeto |
| `4xx`/`5xx` con `uvd_error` ([capa 6](06-errores.md)) | Todo lo demás |

- **Por qué:** con la `sequence` por sujeto, un evento viejo que llega tarde no pisa un estado más
  nuevo; y como reintentarlo no lo vuelve nuevo, se contesta como éxito para que el despachador pare.
- **Sale de:** meshrelay (descarta `sequence` ≤ la última vista por tarea).

### R5.11 · Cada emisor con ingreso HTTP sirve un cursor, y el cursor es el contrato

`GET <event_cursor>?after=<cursor>&limit=<n>` devuelve los eventos en el orden en que se escribieron
en el outbox:

```json
{
  "events": [ { "schema": "uvd.event/1", "...": "..." } ],
  "next_cursor": "184467",
  "latest_cursor": "184502",
  "has_more": true
}
```

- `after` es **exclusivo** y opaco (un string que el emisor devolvió como `next_cursor`); sin `after`,
  desde el principio de lo que el emisor retiene.
- `limit` va de 1 a 500; por omisión, 100.
- `next_cursor` es desde dónde seguir; `latest_cursor`, la cabeza del log; `has_more`, si quedan
  eventos después de esta página. Los cursores viajan como string.
- El cursor se autentica igual que el resto: ERC-8128 más allowlist ([R3.1](03-autenticacion.md): es
  una lectura que necesita identidad).

**El cursor es el contrato; el push es una pista.** Un consumidor que perdió un push, o que no tiene
ingreso HTTP, lee por el cursor y no pierde nada.

- **Por qué:** un push puede perderse aunque tenga reintentos; un log con posición no. `sequence` es
  por sujeto y no sirve de posición del log entero: por eso la posición es el cursor.
- **Sale de:** meshrelay (el cursor de su log de presencia: `after` exclusivo, `limit` hasta 500,
  `next_cursor`, `latest_cursor`, `has_more`, y la regla «el contrato autoritativo es el cursor»).

### R5.12 · Una suscripción es una fila declarada en el emisor

El emisor tiene, en un archivo revisado, la lista de quién recibe qué tipos. Esa lista se contrasta con
`events.consumes` del manifiesto de cada receptor, y los tipos que el emisor manda salen de su
`events.emits`. `events.envelopes` dice qué sobres habla cada app (`uvd.event/1` y, durante la
migración, `em.event.v1`).

- **Por qué:** una suscripción que se da de alta sola por API es una puerta para que cualquiera reciba
  eventos; revisada como una allowlist, no.

### R5.13 · IRC es el canal para personas y agentes, no el bus entre máquinas

Lo que una app necesita procesar llega por `uvd.event/1` o por el cursor. IRC sigue siendo el canal
legible donde se conversa y se anuncia.

- **Por qué:** texto libre sin sobre, sin orden y sin reintento no es algo sobre lo que un programa
  pueda decidir.

## Perfiles para lo que no pasa por HTTP

### R5.14 · Perfil «instantánea publicada»

Un emisor sin ingreso HTTP (o un dato que muchos leen y pocos escriben) publica un documento JSON en
almacenamiento y CDN:

- El documento lleva en su raíz `schema` (`<app>.<nombre>/<mayor>`), `generated_at` (RFC 3339, UTC,
  con `Z`) y `source` (el id de la app), y después sus datos.
- Se escribe con **escritura condicional**: `If-Match` con el ETag leído, o `If-None-Match: *` si
  todavía no existe; ante un conflicto, se relee y se vuelve a intentar.
- Se declara en `snapshots` del manifiesto con su `name`, su `url` y su `schema`.
- Quien lee usa el ETag (`If-None-Match`) y mira `generated_at` para saber qué tan viejo es.

- **Por qué:** es la ruta de lectura entre apps que más se usa en el stack, y la única que sirve a un
  emisor que no puede recibir un POST. La escritura condicional evita que dos escritores se pisen.
- **Sale de:** KarmaKadabra (su feed de trades: cada escritura dispara, por un evento del
  almacenamiento, la reescritura condicional del documento publicado).

### R5.15 · Perfil «la cadena es el cursor»

Para eventos on-chain y para el stream en vivo del facilitador (`GET /events`, SSE): el aviso es una
pista, **la verdad es la cadena**, y el sello es el `tx_hash`. Un consumidor nunca toma la ausencia de
avisos como evidencia de que no pasó nada, y reconcilia contra la cadena.

- **Por qué:** el stream del facilitador es con pérdida por diseño: nunca frena un pago para mantener
  al día a un observador.
- **Sale de:** el facilitador y `uvd_x402_sdk.events` (que lo documenta así).

## Migración

### R5.16 · `em.event.v1` es un perfil, y el receptor acepta los dos sobres mientras dure la migración

`em.event.v1` ([L1](10-perfiles-legados.md#l1--sobre-emeventv1)) es el perfil de `uvd.event/1` con
`subject.kind = "task"`. Un receptor que hoy recibe `em.event.v1` acepta además `uvd.event/1`, y cuenta
cuál llega. Nada se retira hasta medir 7 días seguidos de cero uso del sobre viejo.

Cómo se lleva un `em.event.v1` a `uvd.event/1`:

| `em.event.v1` | `uvd.event/1` |
|---|---|
| `schema: "em.event.v1"` | `schema: "uvd.event/1"` |
| `event_id` (UUID) | `event_id`, en minúsculas |
| `event_type` (`<sujeto>.<verbo>`) | `type`: `execution-market.<sujeto>.<verbo>` |
| `task_id` | `subject: {"kind": "task", "id": <task_id>}` |
| `subject: {type, id}` | `subject: {kind: <type>, id}` |
| `sequence` | `sequence` (un evento sin `sequence` no se lleva hasta que el emisor la ponga) |
| `occurred_at` (con `Z` o con offset) | `occurred_at` en UTC, con `Z` |
| `actor` con `wallet` | `actor: {kind: "wallet", id: <wallet en minúsculas>, role}` |
| sin `actor`, o sin `wallet` | `actor: {kind: "app", id: "execution-market"}`; lo demás del actor, a `payload` |
| `counterparty` | `payload.counterparty` |
| `economic: {amount, currency, network, escrow_status, tx_hash}` | `economic: {amount, currency, network: <CAIP-2>, tx_hash}`; `escrow_status` a `payload` |
| `trace_id` (UUID) | `trace_id`: los mismos 32 hex, sin guiones |
| `payload` | `payload` |

- **Por qué:** primero se agrega y después se retira, midiendo: un receptor que deja de aceptar el
  sobre viejo el mismo día que llega el nuevo corta a quien todavía no migró.

## Fuera del contrato, con lo que los traería

| Transporte | Por qué queda afuera hoy | Qué lo trae |
|---|---|---|
| IRC entre agentes (el canal de mercado) | Es texto libre para personas y agentes, no un bus entre máquinas | Una API de publicación de meshrelay, o un mensaje tipado que ate una conversación a una tarea |
| WebSocket de una UI | Es push hacia una interfaz, con un solo productor | Que una segunda app necesite push hacia una UI |
| Proxy de CDN hacia la API de otra app | Concentra en una IP el tráfico hacia una API que limita por IP | Medirlo antes de poner otra app detrás de un proxy así |
| Bus de eventos central | Con cursor más push firmado, pocos emisores no lo justifican | Más de ~5 emisores, o que los cursores por par se vuelvan N×M |
