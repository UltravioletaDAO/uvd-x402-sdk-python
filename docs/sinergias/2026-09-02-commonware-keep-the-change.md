# Sinergias con "Keep the Change" (Commonware) — 3.2 uvd-x402-sdk-python

> Depositado por c0der el 2026-09-02. Fuente: `c0der/docs/plans/commonware-clearing-que-adoptar.md`
> (análisis de los 15 proyectos x402 del stack: 66 sinergias propuestas, 40 sostenidas por un refutador
> que abrió cada `archivo:línea`; las descartadas y su motivo están en la sección 4 del documento fuente).
> Post original: <https://commonware.xyz/blogs/clearing> (Patrick O'Grady, 2026-08-19). Esta carpeta
> `docs/sinergias/` es donde c0der deja lo que otros análisis encuentren para este proyecto.

## Principios transversales que aplican a todo el stack (títulos; el detalle está en la fuente, sección 2)

- P1 · La preconfirmación es un par firmado transferible, no un booleano
- P2 · El reintento devuelve el mismo recibo, y la clave se DERIVA de la identidad del pedido
- P3 · Una escritura cara por cuenta cambiada, no por evento
- P4 · La retención de evidencia se ata a la ventana de disputa — y la ventana no existe
- P5 · La ventana de idempotencia y la de retención de evidencia son dos relojes
- P6 · Disputa de un solo tiro: el que reclama presenta el par, y un predicado lo resuelve
- P7 · Un piso es seguro para gastar; el estado que se reconcilia tarde se ajusta, nunca se sobrescribe
- P8 · El benchmark declara qué variable NO aparece
- P9 · El identificador de deduplicación lo pone quien ya lo usa, no vos *(no sale del post)*
- P10 · Cada componente declara su postura ante fallo en su propio doc-comment *(no sale del post)*
- P11 · El valor efectivo de un parámetro se publica en un endpoint legible *(regla del CLAUDE.md global, no del post)*

## Lo específico de este proyecto (sección 3.2 de la fuente, verbatim)

### 3.2 uvd-x402-sdk-python

| Idea (sección del post) | Aplicación concreta | archivo:línea | Esf. | Valor | Riesgo | Cómo se verifica |
|---|---|---|---|---|---|---|
| Reintento idempotente ("Payments as Fast as Browsing the Web") | Mandar `Idempotency-Key` derivada (nonce EIP-3009 **+ recurso/requisitos**) en el bucle de reintento del settle y en el fallback | bucle en `client.py:946`, POST en `:1063`, fallback en `:1107`, constantes en `:72-73` | **S** | alto | bajo | `tests/test_settle_hooks.py` ya mockea el settle: afirmar que los 3 intentos llevan **el mismo** header y que el 2.º vuelve cacheado sin segundo `send` |
| El par transferible ("Payments as Fast as Browsing the Web") | Retener el intento (header, nonce, url, `validBefore`) y aceptar `fetch(..., resume=intento)` que **re-presenta el mismo** `X-PAYMENT` | `client.py:2152-2166` (arma, manda y tira el header), `:1872` (`os.urandom(32)`: cada firma es un nonce nuevo) | M | alto | **medio** | `tests/test_fetch_buyer_loop.py`: cortar la respuesta paga y afirmar que el 2.º intento manda **el mismo nonce** |
| Retener lo firmado ("The Unavoidable Challenge") | (a) `process_payment` devuelve el `SettleResponse` **completo** y sabe persistirlo | `client.py:1160`; ausencia de `PAYMENT-RESPONSE` medida por grep (0 hits en `src/uvd_x402_sdk/`) | S | medio | bajo | Test: el par persistido permite recomputar `paymentId = keccak256(caip2 ‖ txHash)` (`x402-rs/src/dx402/mod.rs:82-86`) sin volver a llamar al facilitador |

**Notas.** (1) La clave **debe cubrir el recurso/requisitos** y no solo el nonce, porque el
facilitador keyea por `(clave, hash del cuerpo)` y `verify` y `settle` mandan cuerpos
distintos. (2) Este ítem y x402-rs #1 **son uno solo partido en dos repos**: solo paga
cuando aterrizan los dos lados. (3) El `resume=` devuelve un **handle opaco**, nunca un
dict con el header: es una autorización al portador viva hasta `validBefore` y el dueño
está en stream. Y `fetch()` construye el header **después** de leer el 402, así que
`resume=` tiene que saltear también la re-cotización o el precio puede haber cambiado.
(4) El "par" del post está **partido en dos entregas**: (a) devolver el `SettleResponse`
completo es barato y real; (b) el **acuse firmado** es cambio upstream de x402-rs (solo
WSL) sobre `src/dx402/receipt.rs:28-44`, porque hoy `/settle` no firma nada.
