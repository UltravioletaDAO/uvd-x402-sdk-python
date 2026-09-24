# Perfiles legados

Especificación de interop del stack, **v1**. No normativo para apps nuevas: una app nueva habla el
contrato de las capas 1 a 9. Estos perfiles describen **lo que ya corre**, por su formato en el
cable, para que adoptar el SDK no le cambie el comportamiento a nadie y para que un receptor pueda
aceptar el perfil viejo y el nuevo a la vez mientras migra.

Cada perfil dice qué viaja (cabeceras, cuerpo, firma), quién lo habla y qué lugar tiene en el
contrato:

- **Transición**: se acepta junto al contrato hasta medir 7 días seguidos de cero uso, y después se
  retira.
- **Producto**: se queda, porque lo usan terceros que no son apps de la casa.
- **Compatible**: ya es una postura del contrato (un preset de ERC-8128).

Todos los HMAC de esta página son HMAC-SHA256 con la firma en hex, comparada en tiempo constante; el
timestamp es Unix en **segundos**.

| Perfil | Qué es | Lugar |
|---|---|---|
| [L1](#l1--sobre-emeventv1) | Sobre `em.event.v1` | Transición hacia `uvd.event/1` |
| [L2](#l2--hmac-x-webhook-signature-tv1) | HMAC `X-Webhook-Signature: t=,v1=` entre apps | Transición hacia ERC-8128 |
| [L3](#l3--hmac-x-em-signature) | HMAC `X-EM-Signature` | Transición (ya apagado por omisión) |
| [L4](#l4--hmac-x-mr-signature) | HMAC `X-MR-Signature` | Transición hacia ERC-8128 |
| [L5](#l5--webhooks-vendidos-a-terceros-hmac-por-registro) | Webhooks vendidos a terceros | Producto |
| [L6](#l6--erc-8128-em-lenient) | ERC-8128 `em-lenient` | Compatible |
| [L7](#l7--erc-8128-meshrelay-strict) | ERC-8128 `meshrelay-strict` | Compatible |
| [L8](#l8--cursor-con-llave-por-par) | Cursor con llave por par | Transición hacia ERC-8128 |
| [L9](#l9--bloqueo-de-ip-marcado-por-error-en-la-raíz) | Bloqueo de IP marcado por `error` en la raíz | Se respeta (R6.2) |
| [L10](#l10--x-idempotency-key-como-alias) | `X-Idempotency-Key` | Alias permanente de `Idempotency-Key` |

## L1 · Sobre `em.event.v1`

**Quién:** Execution Market → meshrelay, `POST` al buzón de eventos de meshrelay.
**Lugar:** transición hacia `uvd.event/1` ([R5.16](05-eventos.md), con la tabla de conversión).

Cuerpo JSON, conjunto de claves cerrado (una clave desconocida es un 400):

| Clave | Obligatoria | Formato |
|---|---|---|
| `schema` | sí | `"em.event.v1"` |
| `event_id` | sí | UUID (mayúsculas o minúsculas) |
| `event_type` | sí | `<sujeto>.<verbo>`, cada segmento `[a-z][a-z0-9_]*` |
| `occurred_at` | sí | RFC 3339 con zona: `Z` o `±hh:mm`, fracción opcional |
| `task_id` **o** `subject` | exactamente uno | `task_id`: UUID. `subject`: `{type, id}` con `type` `[a-z][a-z0-9_]{0,31}` e `id` UUID |
| `sequence` | no | Entero ≥ 0; el receptor la exige en las mutaciones del hilo de una tarea |
| `actor`, `counterparty` | no | `{role, agent_id?, executor_id?, wallet?, irc_nick?}`; `role` obligatorio, y al menos uno de `agent_id`, `executor_id` o `wallet` |
| `economic` | no | `{amount, currency, network, escrow_status, tx_hash}`, todo string; `amount` decimal con hasta 6 decimales; `tx_hash` `0x` + 64 hex, o base58 en Solana |
| `payload` | sí | Objeto |
| `trace_id` | no | UUID |

Tipos en uso: `task.*` (`created`, `published`, `assigning`, `assigned`, `assign_failed`, `approved`,
`completed`, `rejected`, `cancelled`, `mutual_cancel`, `disputed`, `expired`), `submission.*`
(`received`, `approved`, `rejected`), `channel.*` (`opened`, `closed`), `payment.*` (`settled`,
`escrow_released`, `released`), `reputation.updated`, `rating.created`, `reputation.feedback`,
`relay.*` (`leg_started`, `handoff`, `completed`) y `service.*` (`listed`, `updated`, `ordered`).

Respuestas del receptor:

| Respuesta | Cuándo |
|---|---|
| `202 {"status": "accepted", "event_id", "channels", "acl_suppressed"}` | Aceptado |
| `200 {"status": "already_processed", "event_id", "acl_suppressed"}` | Ya lo tenía (idempotente por `event_id`) |
| `422 {"status": "rejected", "reason"}` | Una regla del receptor lo rechaza; entre ellas `stale_sequence`, cuando `sequence` ≤ la última vista para esa tarea |
| `400`/`401`/`503 {"error": "..."}` | Sobre inválido, firma inválida, receptor sin configurar |

Firma: [L2](#l2--hmac-x-webhook-signature-tv1).

## L2 · HMAC `X-Webhook-Signature: t=,v1=`

**Quién:** Execution Market → meshrelay (L1); y el mismo dialecto en los webhooks a terceros (L5).
**Lugar:** entre apps de la casa, transición hacia ERC-8128 con `evento-s2s`
([R3.13](03-autenticacion.md)).

```http
POST <buzón> HTTP/1.1
Content-Type: application/json
X-Webhook-Signature: t=1790000000,v1=<64 hex>
X-Webhook-Timestamp: 1790000000
```

- Firma: `hex(HMAC-SHA256(secreto, "<t>." + <cuerpo crudo en UTF-8>))`.
- `X-Webhook-Signature` son pares `clave=valor` separados por coma; `v1` es hex de 64 caracteres
  (se compara en minúsculas). Una clave repetida es un rechazo; una clave desconocida se ignora.
- `X-Webhook-Timestamp` es obligatoria entre apps de la casa y no difiere de `t` en más de 1 s.
- Ventana: ±300 s alrededor de la hora del receptor.
- Un receptor sin secreto configurado contesta 503 y no procesa nada.

## L3 · HMAC `X-EM-Signature`

**Quién:** dialecto anterior de L2, que el receptor solo acepta si su operador lo habilita (apagado
por omisión).
**Lugar:** transición; hoy no lo emite nadie.

```http
X-EM-Signature: sha256=<64 hex>
X-EM-Timestamp: 1790000000
```

- Firma: `hex(HMAC-SHA256(secreto, "<timestamp>." + <cuerpo crudo>))`; el prefijo `sha256=` es
  opcional. Ventana de 300 s.
- Un mensaje con L2 y L3 a la vez solo se acepta con el perfil habilitado y si las dos firmas
  verifican.

## L4 · HMAC `X-MR-Signature`

**Quién:** meshrelay → Execution Market, `POST /api/v1/streams/presence` (presencia en canales de
pago, base del cobro por streaming).
**Lugar:** transición hacia ERC-8128 con `evento-s2s`.

```http
POST /api/v1/streams/presence HTTP/1.1
Content-Type: application/json
X-MR-Signature: <64 hex, sin prefijo>
X-MR-Timestamp: 1790000000
```

- Firma: `hex(HMAC-SHA256(secreto, "<timestamp>." + <cuerpo crudo>))`. Ventana de ±300 s.
- Cuerpo:

  ```json
  {
    "events": [
      {"kind": "opened", "nick": "…", "channel": "#…", "at_ms": 0, "expires_at_ms": null,
       "mr_kind": "opened", "seq": 0, "session_id": 0, "wallet": null, "network": null}
    ],
    "latest_cursor": "<seq como string>"
  }
  ```

  `events` tiene hasta 500 elementos; `kind` es `opened`, `rejoined`, `renewed`, `parted`, `expired`
  o `revoked`; `latest_cursor` viaja como **string**.
- Respuestas: `200 {"received", "matched_sessions", "recorded", "closed", "ceilings_raised",
  "latest_cursor"}`; los errores, anidados bajo `detail`: `401 {"detail": {"error":
  "presence_signature_invalid", "code", "message"}}`, `422 {"detail": {"error":
  "presence_payload_invalid", "code", "message"}}`.
- Es un push de un intento; la fuente de verdad es el cursor ([L8](#l8--cursor-con-llave-por-par)).

## L5 · Webhooks vendidos a terceros (HMAC por registro)

**Quién:** Execution Market → cualquier suscriptor que registró un webhook.
**Lugar:** producto; se queda ([R3.13](03-autenticacion.md)).

```http
POST <url del suscriptor> HTTP/1.1
Content-Type: application/json
User-Agent: ExecutionMarket-Webhook/1.0
X-Webhook-Id: <id del registro>
X-Webhook-Event: <tipo>
X-Webhook-Signature: t=<unix s>,v1=<64 hex>
X-Webhook-Timestamp: <unix s>
X-Idempotency-Key: <uuid>
```

```json
{
  "event": "<tipo>",
  "data": {},
  "metadata": {
    "event_id": "<uuid>",
    "event_type": "<tipo>",
    "timestamp": "<RFC 3339>",
    "api_version": "2026-01-25",
    "idempotency_key": "<uuid>"
  }
}
```

- Firma: la de L2, con el secreto **del registro**. El suscriptor verifica `v1` sobre `"<t>." + cuerpo`
  y la ventana de 300 s.
- `X-Idempotency-Key` es igual a `metadata.idempotency_key` (distinta de `metadata.event_id`): la
  misma en todos los reintentos. El suscriptor deduplica por ella.
- Entregado: 200, 201, 202 o 204. Todo lo demás se reintenta: hasta 6 intentos con esperas de 1, 2,
  4, 8 y 16 s más hasta un 10 % de azar, 30 s de timeout por intento. La firma se calcula una vez y
  se reusa en los reintentos.
- Registro: `POST /api/v1/webhooks` con `{url (https), events, description}`; el secreto se muestra
  una sola vez. Rotación: `POST /api/v1/webhooks/{id}/rotate-secret`, que reemplaza el secreto en el
  momento.

## L6 · ERC-8128 `em-lenient`

**Quién:** la API y el MCP de Execution Market, como verificador.
**Lugar:** compatible; es el preset `em-lenient` de `uvd_x402_sdk.erc8128` ([R3.5](03-autenticacion.md)).

- Nonce: `GET /api/v1/auth/erc8128/nonce` → `{"nonce": "<base64url>", "ttl_seconds": 300,
  "message"}`. El verificador acepta **cualquier nonce que no vio** dentro de la ventana
  (`nonce.source: "unseen"`), y lo consume **antes** de verificar la criptografía, con clave
  `erc8128:{chain_id}:{address}:{nonce}`.
- Authorities: `api.execution.market` y `mcp.execution.market`.
- Política publicada en `GET /api/v1/auth/erc8128/info`: `authorities`, `supported_chains`, `signing`
  (`covered_components`, `content_digest`, `label`, `keyid_format: "erc8128:{chain_id}:{address}"`),
  `policy` (`max_validity_sec: 300`, `clock_skew_sec: 30`, `require_request_bound`,
  `require_nonce`) y `nonce_endpoint`.
- En el manifiesto: `"preset": "em-lenient"`, `"nonce": {"source": "unseen", "endpoint": ...}`.

## L7 · ERC-8128 `meshrelay-strict`

**Quién:** la API de meshrelay, como verificador.
**Lugar:** compatible; es el preset `meshrelay-strict` ([R3.5](03-autenticacion.md)).

- Nonce: `GET /auth/erc8128/nonce` → `{"nonce": "<base64url de 32 bytes>", "expires_at":
  <unix s>, "ttl_seconds": 300}`, con `Cache-Control: no-store`. **Solo** acepta nonces que emitió
  él, una vez cada uno (`nonce.source: "issued"`), y los consume **después** de verificar la
  criptografía. `nonce_unknown` 401, `nonce_replayed` 409, `nonce_expired` 401.
- Componentes exactos y en orden: `@method @authority @path [@query] [content-digest]`;
  `content-digest` es obligatorio en toda escritura, aunque no lleve cuerpo.
- Cadenas: 8453 por omisión. Authority: la de su API.
- Política publicada en `GET /auth/erc8128/info` (la forma de L6 más `policy.authority` y
  `nonce_issuance: "server"`).
- En el manifiesto: `"preset": "meshrelay-strict"`, `"nonce": {"source": "issued", "endpoint":
  ...}`.

## L8 · Cursor con llave por par

**Quién:** el log de presencia de meshrelay, `GET /payments/presence/events`, leído por el medidor de
presencia de un cliente.
**Lugar:** transición: el cursor pasa a aceptar ERC-8128 más allowlist **junto** a la llave
([R5.11](05-eventos.md)); la llave se retira después.

```http
GET /payments/presence/events?after=184467&limit=100 HTTP/1.1
X-Presence-Api-Key: <llave del par>
```

- `after`: entero, exclusivo (`seq > after`), 0 por omisión; `limit` de 1 a 500, 100 por omisión;
  `channel` opcional.
- Respuesta: `{"events": [{"seq", "session_id", "nick", "channel", "kind", "at_ms",
  "expires_at_ms", "payer_address", "network", "amount"}], "next_cursor": <entero>,
  "latest_cursor": <entero>, "has_more": <bool>}`.
- `401 {"error": "Missing or invalid X-Presence-Api-Key"}`; 503 si el servidor no tiene llave
  configurada.

Es la forma que adopta el cursor del contrato (R5.11), con dos diferencias: el cursor del contrato
viaja como string y se autentica con ERC-8128.

## L9 · Bloqueo de IP marcado por `error` en la raíz

**Quién:** la API de Execution Market.
**Lugar:** se respeta: `uvd_error` no toca `error` ni `detail` ([R6.2](06-errores.md)).

Tres respuestas de la misma API llevan `error` en la raíz y ninguna lleva `detail`:

| Respuesta | Cuerpo |
|---|---|
| Bloqueo de IP | `403 {"error": "<texto>"}`: **solo** la clave `error` |
| Permiso denegado | `403 {"error": "forbidden", "message": "<texto>", "timestamp": "<RFC 3339>"}` |
| Límite de tasa | `429 {"error": "rate_limit_exceeded", "message": "<texto>", "retry_after": <s>}`, con `Retry-After` |

- **Regla del perfil:** una respuesta es un bloqueo de IP si es un 403 y su cuerpo tiene
  **exactamente una** clave, `error`. Un 403 que trae además `message` y `timestamp` es un permiso
  denegado, no un bloqueo.
- Los demás errores de la API usan `detail`: `{"detail": "<texto>"}` o `{"detail": {"error"?,
  "code", "retryable"?, "message"}}`; un 422, `{"detail", "errors": [{"field", "message",
  "type"}]}`.
- Como la regla mira qué claves hay en la raíz, el sobre común vive en su propia clave y no agrega
  nada a un cuerpo de bloqueo.
- La regla es de este perfil y de esta API: otras apps del stack usan `error` en la raíz para
  cualquier falla, así que un cliente no la aplica a sus respuestas.
- **Vector:** [`vectors/l9-bloqueo-de-ip.json`](vectors/l9-bloqueo-de-ip.json).

## L10 · `X-Idempotency-Key` como alias

**Quién:** la API de tareas de Execution Market (`POST /api/v1/tasks`).
**Lugar:** alias permanente de `Idempotency-Key` ([R4.7](04-pago.md)); no se retira.

- `X-Idempotency-Key: <clave>` en la creación de una tarea: si ya existe una tarea viva de ese
  agente con esa clave, la respuesta es **200** con esa tarea y la cabecera `X-Idempotent: true`.
- La misma API acepta además `Idempotency-Key` en sus `POST` autenticados: con el mismo cuerpo
  devuelve la respuesta original con `Idempotency-Replay: true`; con otro cuerpo,
  `409 {"error": "idempotency_key_conflict"}`.
