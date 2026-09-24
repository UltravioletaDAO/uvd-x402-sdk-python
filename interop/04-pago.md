# Capa 4 · Pago

Especificación de interop del stack, **v1**.

x402 es el idioma de pago de todas las apps y el facilitador es el único riel: no autentica a quien
llama, la autorización es la firma del pagador. Lo que cada app resolvió distinto es **la
idempotencia y el veredicto**, que es donde se cobra dos veces. Esta capa no inventa nada: fija lo
que este SDK ya hace y lo que describe-net y meshrelay resolvieron primero.

## R4.1 · Toda plata pasa por el SDK, como vendedor o como pagador

Vender: `X402Client.process_payment()` o las integraciones (FastAPI, Flask, Django, Lambda).
Pagar: `X402Client.create_authorization()` / `fetch()`. Nadie arma `/verify` ni `/settle` a mano.
(Los nombres son de Python; el SDK de TypeScript y el crate de Rust exponen su par.)

- **Por qué:** las invariantes de la plata (no reintentar una autorización enviada, una clave por
  pago, el veredicto de tres estados, nunca 402 sobre un pago que pudo moverse) se escriben y se
  prueban una vez; cada copia a mano pierde alguna cuando el riel cambia.

## R4.2 · La `Idempotency-Key` se acuña y se persiste antes de la primera llamada, y es la misma en todo el manejo del pago

El vendedor acuña la clave (`x402-<64 hex>`; `new_idempotency_key()` en Python, su par en el SDK de
TypeScript y en el crate), **la guarda** junto al pago y
recién entonces llama al facilitador. La misma clave va en `/verify`, en `/settle`, en cada reintento
y en el reenvío tras un timeout.

- **Por qué:** si el proceso muere entre el settle y la respuesta, el reintento con la misma clave
  recupera el veredicto original del facilitador en vez de liquidar otra vez; una clave que solo
  vivía en memoria se pierde con el proceso.
- **Sale de:** describe-net (vínculo persistido antes del settle) y meshrelay (la misma clave en
  `/verify` y `/settle`).

## R4.3 · La clave no se deriva solo del pago

Una `Idempotency-Key` **NO DEBE** calcularse solo a partir del contenido de `X-PAYMENT`.

- **Por qué:** quien tiene el pago puede recalcular esa clave; un comprador que reenvía su propio
  `X-PAYMENT` en una compra nueva recibiría la respuesta de la compra original. La posesión del pago
  no es un vínculo de compra.

## R4.4 · Una autorización enviada no se reintenta ni cambia de riel sin prueba de que el primero no movió nada

Una vez transmitida la autorización (`sent`), quien paga no la reintenta por otro camino ni firma
otra para el mismo cobro, salvo que esté **probado** que la primera no movió plata. La decisión es
por lista blanca: lo que no se reconoce como seguro, no se repite.

- **Por qué:** una autorización EIP-3009 transmitida es plata al portador; reintentar por el segundo
  riel después de que el primero la liquidó paga dos veces.
- **Sale de:** KarmaKadabra (comprador) y `uvd_x402_sdk.money_safety`.

## R4.5 · El veredicto de un pago es de tres estados

| Estado | Significa | Qué se hace |
|---|---|---|
| `settled` | Hay sello: transacción minada o recibo del facilitador | Se entrega |
| `refused` | Rechazado antes de mover nada | No se entrega; el comprador puede firmar otro pago |
| `unresolved` | No se sabe (timeout, difundida sin recibo, en curso) | No se entrega ni se cobra otra vez; se reconcilia contra la cadena o el recibo |

- **Por qué:** «no sé» no es «no»; tratar un timeout como un rechazo invita a firmar otro pago por
  algo que ya se cobró.
- **Sale de:** meshrelay (veredicto `settled` / `refused` / `unresolved` sobre los recibos durables
  del facilitador).

## R4.6 · Un vendedor nunca responde 402 sobre un pago que pudo moverse

Un 402 le dice al comprador «firmá otro pago». Un pago `unresolved` se responde con 503 +
`Retry-After` (sin veredicto todavía: presentá el **mismo** pago más tarde) o con 500 que nombra la
transacción (difundida: revisala antes de nada), y lleva `uvd_error` con `spent: "maybe"`
([R6.4](06-errores.md)).

- **Por qué:** un 402 sobre un pago que se liquidó hace que el comprador pague dos veces.

## R4.7 · La cabecera se llama `Idempotency-Key`

El nombre es `Idempotency-Key`. Una API que ya acepta otro nombre lo sigue aceptando como alias y no
retira nada ([L10](10-perfiles-legados.md#l10--x-idempotency-key-como-alias)).

- **Por qué:** es el nombre que usa el facilitador; dos nombres para lo mismo obligan a cada cliente a
  saber cuál habla cada API.

## R4.8 · Si el SDK no se puede importar en el camino del pago, el pago falla cerrado y ruidoso

Un camino de pago que pasa a usar el SDK **NO DEBE** volver en silencio a un pago armado a mano si el
import falla: no paga, y alerta.

- **Por qué:** un respaldo silencioso esconde que el entorno perdió la librería, y el camino a mano es
  justamente el que no tiene las invariantes de R4.2 a R4.6.
