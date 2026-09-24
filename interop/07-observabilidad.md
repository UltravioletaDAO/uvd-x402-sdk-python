# Capa 7 · Observabilidad y límites

Especificación de interop del stack, **v1**. Los límites se publican en `rate_limits` y la salud en
`health` del manifiesto ([`schemas/manifest.schema.json`](schemas/manifest.schema.json)).

Hoy los límites de tasa no se ven hasta el bloqueo, y el tráfico entre apps se atribuye solo por un
`User-Agent` puesto a mano. Esta capa hace visible quién llama, cuánto aguanta cada app y si está
viva.

## R7.1 · El `User-Agent` es `<app>/<version> (+<url>)` y lo pone el SDK

`<app>` es el id de la app ([R1.10](01-descubrimiento.md)), `<version>` la desplegada y `<url>` su
sitio o el manifiesto. Ejemplo: `mercado-ejemplo/1.4.0 (+https://mercado.example)`. Hacia las apps de la
casa, una app **NO DEBE** presentarse como un navegador; solo hacia un tercero que lo exige, y solo
hacia ese tercero.

- **Por qué:** varios proveedores del stack tienen un límite compartido y lo único que atribuye el
  consumo a una app es el `User-Agent`; uno disfrazado de navegador no se puede contar.
- **Sale de:** el SDK de describe (UA con `product`) y el mostrador de Emporium (UA propio para que
  cada superficie cuente su tráfico).

## R7.2 · La traza viaja en `traceparent` (W3C Trace Context)

Toda llamada S2S lleva `traceparent`, y todo evento `uvd.event/1` lleva en `trace_id` el trace-id de
esa misma traza (32 hex en minúsculas, [R5.6](05-eventos.md)). Quien recibe una llamada con
`traceparent` continúa esa traza en lo que dispara.

- **Por qué:** una operación que cruza tres apps (tarea, escrow, anuncio) se puede seguir de punta a
  punta solo si las tres hablan del mismo id.

## R7.3 · Los límites se publican en el manifiesto y en las cabeceras

`rate_limits` del manifiesto declara cada límite (`scope`: `ip`, `wallet`, `key` o `global`;
`limit` por `window_s`; y `applies_to` si es de una sola puerta). Las respuestas llevan además las
cabeceras `RateLimit-Policy` y `RateLimit` (draft IETF *RateLimit header fields for HTTP*), y un 429
lleva `Retry-After`.

- **Por qué:** un cliente que conoce el límite antes de chocar no llega al bloqueo; hoy el límite de
  cada app se aprende por el bloqueo, y hay APIs que bloquean la IP entera, con todas las apps que
  salen por ella.

## R7.4 · El cliente del SDK respeta los límites con un token bucket leído del manifiesto

El cliente S2S del SDK arma su cubeta con el `rate_limits` del receptor y la ajusta con las cabeceras
`RateLimit`. Ante un 429 espera `Retry-After` y no reintenta antes.

- **Por qué:** es la cubeta que un cliente del stack armó a mano después de un bloqueo; escrita una vez
  en el SDK, la tiene toda app que llama.
- **Sale de:** KarmaKadabra (cubeta de tokens hacia Execution Market).

## R7.5 · La salud va partida: `live` sin I/O externo, `ready` con dependencias

`health.live` responde si el proceso está vivo, **sin tocar** base, red ni otra app. `health.ready`
responde si la app puede atender, midiendo sus dependencias. Un balanceador mira `live`; un cliente
que va a depender de la app mira `ready`.

- **Por qué:** un chequeo de salud que depende de la base saca de servicio todas las réplicas cuando la
  base anda lenta, y el servicio se cae entero por una dependencia que solo estaba lenta.

## R7.6 · Todo sondeo de conformidad contra producción corre en serie, con pausa, y se detiene a tiempo

Un runner de conformidad que lee superficies vivas: una petición a la vez, **al menos 1 s** entre
peticiones, y se detiene en el **primer 429** o en el **tercer 401**. Nunca firma lecturas públicas
(R3.1) ni llama tools que escriben o cobran.

- **Por qué:** hay APIs del stack que bloquean la IP por acumulación de 429 **y también de 401**, y
  el bloqueo alcanza a todas las apps de la casa que salen por la misma IP.
