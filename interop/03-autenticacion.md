# Capa 3 · Autenticación entre servicios (S2S)

Especificación de interop del stack, **v1**. La política de cada verificador se publica en el
manifiesto ([`schemas/manifest.schema.json`](schemas/manifest.schema.json), `endpoints.<puerta>.erc8128`).

La base ya existe y es de este SDK: ERC-8128 (RFC 9421 firmado con EIP-191) en Python y en
TypeScript, con los mismos vectores (`uvd_x402_sdk.erc8128`), y una allowlist de direcciones
públicas en el receptor. Con eso nadie custodia secretos ajenos. Esta capa fija **qué** se firma,
**con qué** y **cómo** lo verifica el receptor.

## R3.1 · Se firma toda escritura y toda lectura cuyo receptor necesita la identidad; una lectura pública no se firma

Se firma: toda escritura entre apps, que es todo POST/PUT/PATCH/DELETE salvo las lecturas MCP de
abajo (un webhook de evento incluido, ver R3.13), y toda lectura cuyo receptor decide algo por la
identidad de quien llama (una exención de pago por allowlist, un cursor autenticado). **No** se firma
un GET a una ruta pública: ni un heartbeat, ni un sondeo, ni una lectura de catálogo.

Una llamada MCP que lista tools (`tools/list`) o llama una tool de clase `lectura` sobre datos
públicos ([R9.1](09-mcp.md), [R9.7](09-mcp.md)) es una lectura pública aunque viaje por POST: no se
firma. Lo que decide es la operación, no el método.

- **Por qué:** firmar cada lectura cuesta una firma de wallet por cada sondeo, y contra una API que
  pide nonce al servidor multiplica los pedidos de nonce y las respuestas 401, que en algunas APIs
  también cuentan para los bloqueos de IP. Lo que no necesita identidad no la manda.
- **Sale de:** KarmaKadabra, regla del dueño desde 2026-07-23 («SIGN ONLY WRITES, NEVER A READ»), y
  el partner gate de describe-net (la lectura que se firma es la que decide si se cobra).
- **Vector:** [`vectors/r3-1-que-se-firma.json`](vectors/r3-1-que-se-firma.json).

## R3.2 · Firma la wallet de servicio de quien llama, con la authority del receptor

Quien llama firma con **su** wallet de servicio ([R2.1](02-identidad.md)) y cubre `@authority` con el
host del receptor. El receptor verifica con el SDK (`uvd_x402_sdk.erc8128.verify_request` en Python;
el SDK de TypeScript y el crate de Rust exponen su par) **o con un verificador que pase los mismos
vectores**, y admite la dirección solo si está en su allowlist de direcciones públicas.

- **Por qué:** una llave sirve para todos los receptores sin que una firma valga en otro, porque la
  firma ata la petición a la authority del receptor. Y una allowlist de direcciones públicas no es
  un secreto: se revisa en un commit y nadie tiene que custodiar nada ajeno.
- **Sale de:** describe-net (partner gate), Execution Market y meshrelay (ERC-8128 por petición).

## R3.3 · Un receptor acepta solo sus propias authorities

Un verificador S2S **NO DEBE** aceptar una authority que no sea suya (los hosts de sus propias
puertas). La lista que acepta es la que publica en `erc8128.authorities` de cada puerta.

- **Por qué:** si dos receptores aceptaran la misma authority, una firma capturada en uno se podría
  repetir en el otro.

## R3.4 · Cada puerta que acepta ERC-8128 publica su política en el manifiesto

`endpoints.<puerta>.erc8128` lleva `authorities`, `chain_ids` (o `null` = cualquiera), `preset` y la
semántica del nonce (`nonce.source` y, si el nonce lo emite el servidor, `nonce.endpoint`). Quien
llama la lee de ahí. Una puerta sin `erc8128` en `auth` no publica política.

- **Por qué:** una política copiada a mano en el cliente (con un comentario que cita el commit del
  otro lado) deja de coincidir el día que el receptor agrega una authority, y nada falla hasta que
  la firma se rechaza en producción.

## R3.5 · Las posturas son datos: los presets

Un preset reproduce, perilla por perilla, la postura de un verificador; adoptar el SDK no cambia el
comportamiento de nadie. Los valores de `meshrelay-strict`, `em-lenient` y `canonical-strict` están
fijados en los vectores de conformidad (`erc8128.f3-*.json`) y, en Python, en
`uvd_x402_sdk/erc8128/presets.py`; el SDK de TypeScript y el crate de Rust exponen su par.

| Preset | `accept` | `components` | `content_digest` | Cadenas | Validez máx. | Tolerancia futura / pasada | Consumo del nonce |
|---|---|---|---|---|---|---|---|
| `meshrelay-strict` | ambos perfiles | exactos y en orden | métodos no idempotentes | 8453 | 300 s | 30 s / 0 s | después de la cripto |
| `em-lenient` | ambos perfiles | subconjunto ligado a la petición | si hay cuerpo | cualquiera | 300 s | 30 s / 30 s | antes de la cripto |
| `canonical-strict` | canónico (`alg` y keyid en minúsculas) | exactos y en orden | si hay cuerpo | 8453 | 300 s | 30 s / 0 s | después de la cripto |
| `evento-s2s` | canónico | exactos y en orden | si hay cuerpo | 8453 | 300 s | 30 s / 0 s | después de la cripto |

El valor de `chain_ids` que publica el manifiesto manda sobre la columna «Cadenas».

## R3.6 · El preset `evento-s2s`: nonce al azar por intento, almacén de «no visto», consumo después de verificar

Para las entregas de eventos entre apps (POST de un evento al `event_inbox` del receptor):

- **El firmante genera el nonce**: al menos 128 bits de azar criptográfico, **uno nuevo por cada
  intento** de entrega (base64url sin relleno o hex).
- **El receptor guarda los nonces vistos** con clave `(chain_id, wallet, nonce)` durante la ventana
  de la firma más la tolerancia, y rechaza un nonce ya visto con `nonce_replayed` (409).
- **El nonce se consume después de verificar la criptografía** (`after-verify`).
- La ventana es de 300 s; la postura, la de `canonical-strict`.

Así no hace falta pedirle un nonce al receptor por cada evento. En el manifiesto:
`"preset": "evento-s2s"` con `"nonce": {"source": "unseen"}`.

- **Por qué:** pedir un nonce al servidor por cada evento duplica los viajes; y consumir antes de
  verificar dejaría que cualquiera que conozca un nonce en vuelo lo queme con una firma basura.

## R3.7 · El nonce nunca se ata al evento

El nonce **NO DEBE** ser el `event_id` ni derivarse de él. La deduplicación del evento es de la
aplicación y va por `event_id` ([R5.9](05-eventos.md)).

- **Por qué:** con el nonce igual al `event_id`, un reintento tras un 5xx o una respuesta perdida
  recibe 409 `nonce_replayed`, que no se reintenta: el evento terminaría en dead-letter o se daría
  por entregado sin haberse procesado. Con consumo antes de verificar, además, quien conozca un
  `event_id` quemaría el nonce de un reintento con una firma basura.
- **Vector:** [`vectors/r5-9-reintento-de-entrega.json`](vectors/r5-9-reintento-de-entrega.json),
  con R3.6 y R5.9.

## R3.8 · Un buzón de eventos que acepta ERC-8128 lo hace con `evento-s2s`

Si `endpoints.event_inbox.auth` incluye `erc8128`, su `erc8128.preset` es `evento-s2s`. El esquema
lo exige.

- **Por qué:** es la única postura en la que un reintento del despachador con un nonce nuevo pasa, y
  una firma repetida no.

## R3.9 · La semántica del nonce, en tres valores

| `nonce.source` | Qué acepta el verificador | Qué hace quien firma |
|---|---|---|
| `issued` | Solo nonces que emitió él, una vez cada uno | Pide un nonce con `GET nonce.endpoint` antes de firmar |
| `unseen` | Cualquier nonce que no vio dentro de la ventana | Genera uno al azar por petición |
| `none` | Firmas sin nonce, repetibles dentro de su ventana | Nada; **solo admisible en lecturas** |

Con `issued`, `nonce.endpoint` es obligatorio. Con `evento-s2s`, el valor es `unseen`. Los dos los
exige el esquema.

- **Por qué:** quien llama tiene que saber, sin leer el código del otro, si antes de firmar le toca
  pedir un nonce o generarlo.

## R3.10 · La allowlist falla cerrada

Una allowlist ausente, vacía o ilegible no exime a nadie: el receptor responde como si la firma no
estuviera (cobra, o pide autenticación). Una firma válida de una dirección que no está en la lista
tampoco exime. Un error del verificador tampoco.

- **Por qué:** ante la duda, cobrar o negar es recuperable; eximir por error no lo es.
- **Sale de:** describe-net (partner gate: «ante cualquier duda, se cobra»).

## R3.11 · OAuth 2.1 es para clientes MCP genéricos, no para S2S

OAuth 2.1 (PKCE S256) queda para clientes MCP genéricos, agentes o personas, **con la wallet como
identidad**. Entre servicios de la casa se usa ERC-8128 (R3.2). Un servidor de autorización
compartido se decide cuando una tercera app lo necesite.

- **Por qué:** entre dos servicios con wallet propia no hay nadie que haga un login interactivo, y un
  token al portador es otro secreto que custodiar.
- **Sale de:** Execution Market y meshrelay (OAuth para clientes MCP genéricos).

## R3.12 · Un proxy nunca vuelve a firmar como si fuera él lo que pidió otro

Una app que reenvía una petición de un tercero reenvía **la firma de quien llamó** (las cabeceras
`Signature`, `Signature-Input` y `Content-Digest` intactas), o se niega y nombra la puerta directa.
Solo firma con su propia wallet lo que pide en nombre propio.

- **Por qué:** si el proxy firma como sí mismo, el receptor ve al proxy y no a quien llama, y la
  allowlist del receptor autoriza a cualquiera que pase por el proxy.
- **Sale de:** meshrelay (reenvía la firma del llamante hacia Execution Market) y las reglas de
  Emporium («rutea, no custodia»).

## R3.13 · Los webhooks entre apps de la casa también se firman con ERC-8128; los que se venden a terceros siguen con HMAC

Un POST de evento es una escritura: entre apps de la casa se firma con ERC-8128 (`evento-s2s`), y
**los secretos compartidos entre apps de la casa pasan a cero** ([R8.3](08-configuracion.md)).
Los webhooks que una app vende a terceros siguen con HMAC por registro: son un producto público, sus
suscriptores no se pueden pasar a una allowlist, y esta especificación los describe como perfil
legado ([L5](10-perfiles-legados.md#l5--webhooks-vendidos-a-terceros-hmac-por-registro)).

- **Por qué:** un secreto HMAC entre dos apps vive duplicado (una copia por lado) o lo lee una app
  desde la cuenta de la otra; rotarlo exige tocar los dos lados en el mismo minuto. Una dirección
  pública en una allowlist no se rota ni se filtra.

## Transición

Mientras un receptor migra, acepta la firma ERC-8128 **y** el dialecto HMAC que ya recibía, y cuenta
cuál llegó (ver [R5.16](05-eventos.md) y la regla de migración del [README](README.md#migración)).
