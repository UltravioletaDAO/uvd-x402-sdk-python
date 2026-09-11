---
date: 2026-09-11
tags:
  - type/handoff
  - domain/x402
  - domain/sdk-python
  - priority/p3
status: active
---

# El comprador decide contra la oferta que tiene en la mano, y decide antes de firmar

**Versión:** 0.82.0 · Fase **P3** del plan de precios de Astra 6, mitad Python ·
**Esta fase toca dinero.**

El contrato no lo inventé acá: lo fijó el facilitador en `x402-rs` 2.25.0
(`crates/x402-reqwest/src/policy.rs`, PR #44) y lo escribió para los SDK en
`docs/handoffs/2026-09-10-bazar-precios-p3.md`, sección *"El contrato para los
SDK"*. Este repo lo implementa campo por campo y código por código, así que una
negativa significa lo mismo en el comprador de Rust, en este SDK y en el de
TypeScript.

## Lo que faltaba

El SDK ya tenía el lazo del comprador (`X402Client.fetch()`: pide, le contestan
402, firma, reintenta) y tenía **un** techo: `max_amount`, un número en unidades
humanas comparado contra la opción más barata. Eso no es un presupuesto:

- no dice **a quién** se le puede pagar,
- no distingue **en qué activo** está el precio — y el firmante toma el dominio
  EIP-712 del `extra` del propio vendedor, así que firma feliz por un token y una
  red que nunca vio,
- no lleva **cuánto va gastado**, así que un límite acumulado no existía,
- y no leía la **vigencia** que el vendedor declara: toda oferta era eterna.

Ahora hay `PurchasePolicy`, evaluada **contra la oferta concreta del 402**, en
`fetch()`, entre leer el desafío y producir el `X-PAYMENT`.

## Dónde corre, que es la mitad que importa

La revisión de seguridad del PR de Rust encontró exactamente esta pieza a medio
construir: la política existía, el vendedor declaraba su vigencia, y **el cable
entre las dos faltó un commit entero con todos los tests unitarios en verde**.

Por eso `tests/test_policy_real_path.py` entra por `fetch()` contra un transporte
mockeado con una llave real, y afirma sobre si llegó a producirse un header de
pago. **Probado en rojo**: borrando el bloque de decisión de `fetch()`, 7 de sus
13 tests se ponen rojos con `DID NOT RAISE` — la oferta vencida se firma y se
paga.

Los otros 60 tests (`tests/test_purchase_policy.py`) fijan el contrato pieza por
pieza, y también están probados en rojo: once mutaciones dirigidas de
`policy.py` — intercambiar el orden, hacer que `evaluate` gaste, copiar la bolsa
en profundidad, plegar base58 con `lower()`, tirar las ofertas legibles,
`now >= valid_until`, vigencia ilegible como cero, `_allow_unlisted` siempre en
`True` — cada una pone en rojo un test **nombrado**. Ninguna sobrevivió.

1041 tests pasan (968 antes, 73 agregados, ninguno perdido).

## El orden es el contrato

`no-readable-offer` → `offer-expired` → `recipient-not-permitted` →
`asset-not-budgeted` → `per-payment-limit` → `cumulative-limit`.

La **primera** que falla es la que se reporta, porque quien llama ramifica sobre
ella. Y `asset-not-budgeted` corre **antes** de los techos a propósito: los
techos son un mapa, y un mapa no tiene opinión sobre una clave que no contiene.
A quien llama hay que decirle *"presupuestá ese activo"*, no *"subí un techo que
no existe"*.

## Lo que cambia de comportamiento, dicho fuerte

Tres cosas cambian para quien ya usa `fetch()`. Ninguna le quita un pago que hoy
funciona:

1. **Un `accepts` con una entrada ilegible ya no la ignora en silencio.** Antes
   `_parse_402` descartaba lo que no podía leer; ahora lo cuenta con su nombre de
   esquema. Un desafío donde **nada** es legible contesta `no-readable-offer`
   nombrando lo que el vendedor ofreció (`["batch-settlement", "agent-pay"]`) en
   vez de una lista vacía que manda a buscar un bug en el código propio.
2. **Una oferta cuyo `scheme` este build no sabe nombrar ya no se firma como
   `exact`.** El vocabulario es cerrado (`exact`, `upto`, `escrow`, `commerce`,
   `fhe-transfer`), igual que el enum del facilitador. Antes se le armaba un
   header `exact` que el facilitador rechazaba; ahora se dice por qué.
3. **`PolicyRefusedError` es nueva, y hereda de `NoAcceptablePaymentError`**, así
   que un `except` escrito antes de 0.82.0 la sigue atrapando. `exc.code` es el
   código del SDK (`POLICY_REFUSED`); el del contrato es `exc.refusal_code`.

Y lo que **no** cambia: sin política configurada el cliente sostiene
`PurchasePolicy.permissive()`, así que un activo sin techo se sigue pagando
exactamente como antes. `max_amount` está intacto y sigue levantando
`PaymentExceedsMaxError`, y sigue corriendo **primero**. La asimetría es a
propósito: quien se sienta a **escribir** una política se merece el default
seguro.

## Para c0der

### Qué cambió en este repo

- **Nuevo**: `src/uvd_x402_sdk/policy.py` — `PurchasePolicy`, `PolicyApproval` /
  `PolicyRefusal`, `QuoteComparison`, `AdvertisedQuote`, `TokenAsset`, `Offer` /
  `UnreadableOffer` / `ParsedAccepts`, `parse_accepts()`, `offer_valid_until()`,
  `canonical_address()` (alias `canonical_recipient`), `no_readable_offer()`,
  y las constantes `OFFER_VALIDITY_EXTENSION` (`"offer-receipt/1"`),
  `KNOWN_SCHEMES` y `REFUSAL_CODES`.
- **Nuevo**: `PolicyRefusedError` en `exceptions.py`.
- **Cableado**: `client.py` — `X402Client(policy=...)`, `fetch(policy=, quote=)`,
  y `_parse_402` ahora devuelve también el `ParsedAccepts` (ofertas ilegibles +
  `extensions` del desafío). La evaluación está **antes** de
  `create_authorization`.
- **Tests**: `tests/test_purchase_policy.py` (60) y
  `tests/test_policy_real_path.py` (13).
- **Docs**: sección *"Purchase policy"* en el README con la tabla de campos, el
  orden, los códigos y las reglas numeradas como las numera el contrato, para
  que un consumidor no tenga que ir al facilitador. Changelog de 0.82.0.
- **Versión**: `pyproject.toml` 0.81.0 → **0.82.0**. El tag y la publicación a
  PyPI **no** están hechos: *publicar un SDK es un tag, no un push*.

### Qué NO trae, igual que en Rust

Lo digo explícito para que no se dé por hecho:

- **Verificación de firma de `offer-receipt`.** Está el transporte y la vigencia;
  **no** la verificación de firma ni de autoridad del firmante. Eso necesita
  decidir qué clave firma una oferta y cómo se prueba que es la del vendedor —
  diseño de identidad, no un parser.
- **Vinculación por input.** La extensión versionada es el lugar; el perfil que
  ata método, parámetros y revisión de política, no. Queda sin definir a
  propósito hasta que haya un vendedor real que lo necesite.
- **Contabilidad de `upto`.** `upto` es un esquema *legible* (no tumba la lista)
  pero este build no lo firma; la contabilidad de un monto variable no está.
- **Reconciliación de liquidaciones inciertas.** El facilitador ya tiene
  anti-doble-cobro, nonces y `PendingNonceManager`; un segundo mecanismo al lado
  sin leer ese primero sería construir dos cosas para el mismo problema.
- **Nada se persiste.** La política vive en memoria del comprador y no se
  escribe. `record_spend` es del proceso que llama.
- **`fetch` no llama `record_spend` solo**, igual que el middleware de Rust no lo
  llama. Evaluar no gasta; el que sabe que la liquidación resolvió es quien
  llama.

### Qué tienen que hacer los consumidores para adoptarla

Ninguno **tiene** que hacer nada para seguir funcionando: sin política, el
comportamiento es el de 0.81.0. Lo que sigue es para que el presupuesto exista.

El patrón es siempre el mismo — una política al construir el cliente, y un
`record_spend` donde ya saben que la liquidación resolvió:

```python
from uvd_x402_sdk import PurchasePolicy, TokenAsset, X402Client, PolicyRefusedError

USDC_BASE = TokenAsset("base", "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913")
policy = PurchasePolicy(
    per_payment={USDC_BASE: 50_000},
    cumulative={USDC_BASE: 5_000_000},
    only_pay=[PROVEEDOR],
)
client = X402Client(recipient_address=..., policy=policy)
...
policy.record_spend(USDC_BASE, pagado_en_unidades_atomicas)
```

- **Execution Market** — es quien más lo necesita: su MCP paga recursos ajenos
  con una wallet que también sostiene escrows. `only_pay` con las direcciones de
  los proveedores que ya conocen, y `cumulative` por corrida del agente. El
  `record_spend` va donde hoy leen el `X-PAYMENT-RESPONSE` del reintento pagado.
  Ojo con la bolsa compartida: si clonan el cliente por request, la política se
  copia con `copy`/`deepcopy` y **comparte** el total, que es lo que quieren.
- **402milly** — es vendedor, no comprador, así que la política no le aplica al
  camino de cobro. Lo que sí le sirve es la otra mitad: si alguna vez publica
  `extensions["offer-receipt/1"].info.validUntil` en su 402, los compradores
  dejan de tratar sus ofertas como eternas. Hoy nadie la publica.
- **KarmaKadabra** — su comprador ya matcheaba `paymentRequirements` (la grafía
  v1 de `accepts`); `parse_accepts()` la lee. Si compran de catálogo, pasen el
  `quote=AdvertisedQuote(...)` para que el repricing quede en el log en vez de
  sorprenderlos en la factura. **No** hace falta que paren a preguntarle a nadie:
  un repricing dentro de la política es comercio normal.
- **MeshRelay** — el caso de `only_pay`: paga a un conjunto chico de destinos
  conocidos. Una allowlist ahí convierte un bug de `payTo` en una negativa con
  causa en vez de un pago a un desconocido. **Si algún destino es base58**
  (Solana, XRPL), escríbanlo con la grafía del vendedor: acá se compara exacto,
  no se pliega.
- **Todos**: los montos son **enteros en unidades atómicas**. Un `Decimal("0.05")`
  no es un techo; es `50_000` con 6 decimales. Y el activo lleva la red adentro:
  `TokenAsset("base", USDC)` no cubre una oferta en `polygon` ni una en
  `eip155:8453` — declaren las grafías que su vendedor use, o la negativa será
  `asset-not-budgeted` (que es la dirección segura, pero es una negativa).

### Lo que este SDK hace y el de Rust no (y al revés)

- Una oferta **sin `asset`** sigue siendo legible acá: el lazo del comprador
  siempre las pagó con el USDC de la red y llamarlas ilegibles le quitaría un
  pago que hoy funciona. Le queda un activo vacío, que ninguna política puede
  contener, así que una política escrita la rechaza por nombre
  (`asset-not-budgeted`, *"no asset named"*) y `permissive()` la paga como antes.
  En Rust `asset` es obligatorio para deserializar, así que allá es ilegible.
- Un `scheme` **ausente** se asume `exact` acá, por la misma razón. En Rust es un
  campo requerido.
- El `validUntil` tiene que ser un **número** JSON, igual que en Rust
  (`as_u64()`): un string es una forma que el contrato no definió, y por la regla
  5 vale como ausente, nunca como cero.

### Pendiente para quien siga

- El **SDK de TypeScript** tiene que quedar con el mismo contrato. Los códigos y
  el orden son los mismos; lo que hay que vigilar es que la comparación de
  direcciones no termine en `toLowerCase()` y que `validUntil` no se lea de una
  clave sin versión.
- `response.py` sigue publicando `token="USDC"` fijo en el 402 (limitación ya
  anotada en el `CLAUDE.md` del repo). Cuando eso se arregle, un vendedor de este
  SDK podría además declarar su `validUntil` y cerrar el lazo del lado vendedor.
