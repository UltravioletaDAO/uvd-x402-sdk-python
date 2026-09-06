# Backlog — uvd-x402-sdk-python

**Ultimo barrido: 2026-09-05.** Cada fila se verifico contra disco, git o el
registro de paquetes ese dia. La columna `Status` dice el comando y lo que
devolvio, no una opinion.

> **Por que existe este archivo.** Hasta hoy el backlog de este repo vivia en la
> §5 de `docs/handoffs/2026-09-04-py-hash-al-llamador.md` — dentro del cuerpo de
> un handoff, tres niveles abajo de un titulo. Un backlog que hay que saber
> donde esta no lo lee nadie, y de las once filas barridas hoy **cuatro ya
> estaban resueltas**. Este es el unico indice; los handoffs siguen siendo la
> narrativa de cada trabajo.

## Regla de la casa

Una fila afirma un estado. Antes de trabajarla, **comproba que siga siendo
cierta con un comando** y escribi el resultado en `Status`. Cerrar una fila
vencida es entregable: una fila muerta manda a otro a trabajar de gratis.

**Los numeros de linea envejecen mal** — tres de los que escribi hoy quedaron
corridos por mis propios commits antes de terminar el barrido. Cuando cites uno,
cita tambien el `grep` que lo encuentra: el grep sobrevive al refactor, el numero
no.

---

## Abiertas

| Date | Item | Context | Priority | Status |
|---|---|---|---|---|
| 2026-09-04 | El veredicto de reintento del writer de ERC-8004 no delega en el guard comun | Detalle y el arreglo de una linea en `docs/handoffs/2026-09-04-py-hash-al-llamador.md` §5. **No esta en `main` ni en ninguna rama**: vive en el arbol sin commitear de `Z:/ultravioleta/dao/uvd-x402-sdk-python`. Le toca a quien mergee ese trabajo, no a una rama nueva | **P0** | **VIGENTE 2026-09-05.** `grep -rn "_write_verdict" src/` en `main` da vacio; en el arbol de `Z:` da `erc8004.py:656` mas 9 usos. `git show cfdd270:src/uvd_x402_sdk/erc8004.py \| grep -c _write_verdict` da `0`, o sea ni siquiera esta commiteado alli. **Bloqueada por la fila de abajo** |
| 2026-09-04 | El checkout `Z:/ultravioleta/dao/uvd-x402-sdk-python` esta atrasado y sucio | Es el checkout que otras sesiones leen, y por eso handoffs previos apuntaron a lineas que no existen en `main`. Bloquea las filas P0 y P2 de esta tabla, que ya tienen su arreglo escrito adentro | **P1** | **VIGENTE Y PEOR 2026-09-05.** `git -C Z: status --porcelain \| wc -l` da `19` (14 modificados + 5 sin trackear, los mismos numeros de ayer). Ademas: rama `feat/x402client-fetch-buyer-loop` en `0.72.0`, **5 commits detras de `main`** (`git log --oneline cfdd270..origin/main` lista 0.74.0, 0.75.0, 0.76.0, dx402 pointer y el bump a 0.72.0) **y 1 commit local sin pushear** (`cfdd270`). Ayer la fila decia solo "0.72.0 y sucio" |
| 2026-09-04 | `WriterUnavailableError` esta definida y nunca se levanta | `exceptions.py:490`. Su docstring dice para que existe: evitar que un paywall reporte un 503 del writer como 402 y haga firmar una segunda autorizacion por un pago que nadie rechazo | **P2 — NO TOCAR EN `main`** | **VIGENTE, PERO EL ARREGLO YA ESTA ESCRITO** en el arbol sucio de `Z:`: alli se levanta (`client.py:159`, `erc8004.py:589`), se exporta (`__init__.py:99` y `456`) **y cambio de padre a `FacilitatorError`** (en `main` hereda de `X402Error`). Arreglarla aca colisiona con ese trabajo. Se cierra sola cuando se resuelva la fila del checkout. **Hallazgo extra:** en `main` es la unica de las 14 clases de `exceptions.py` que no se exporta |
| 2026-09-04 | `scripts/xlang` no tiene fase para la lectura de errores del facilitador | Los dos SDK divergen en algo medible y el conformance cruzado no lo ve. Seria la fase 7 | P1 | **VIGENTE, PERO NO ES DE ESTE REPO.** `find . -name "*xlang*"` aca da vacio. Los tres archivos viven en `uvd-x402-sdk-typescript/scripts/xlang/` (`agent.mjs`, `agent.py`, `cross-language-conformance.mjs`) |
| 2026-09-04 | Falta el cable `payloadShape: v2` en `ENVELOPE_CASES` de xlang | El worker de TS lo dejo afuera para no entregar rojo mientras Python no soportara el payload v2. Al agregarlo, `agent.py` tiene que mandar el dict v2 crudo, no un `PaymentPayload(...)` con `network` | P1 | **DESBLOQUEADA 2026-09-05, NO ES DE ESTE REPO.** La condicion que lo frenaba (`resolve_envelope_version` sobre un payload v2) esta en `main` desde 0.75.0 y ya publicada. Ver `docs/handoffs/2026-09-04-auto-sin-network.md` §Para c0der |
| 2026-09-04 | Un header v2 sale en sobre **v1** por `X402Client.extract_payload` | `_normalize_v2_envelope`, hoy en `client.py:525` (el handoff de ayer la cita como 504; la corrio el commit `e9c972b`), aplana el sobre y resuelve `eip155:8453` a `base` antes de que la seleccion vea nada. TypeScript, con el mismo header, contesta v2. Las dos son 200 hoy y reducen al mismo pago | P1 | **VIGENTE — DECISION DEL DUENO, NO MECANICA.** Cambiarlo mueve de v1 a v2 un camino con plata real (describe-net). Medido y documentado en `docs/handoffs/2026-09-04-auto-sin-network.md` §5 |
| 2026-09-05 | `create_402_response_v2` hardcodea la DIRECCION de USDC como `asset` | Encontrado al cerrar la fila del `token` en el builder v1. El v2 escribe `"asset": network.usdc_address` sin importar que token se cobre, y calcula el monto con `network.get_token_amount()`, que usa los decimales de USDC. Mas ancho que el defecto v1: alli era una etiqueta, aca es la direccion del contrato con la que se firma | P2 | **VIGENTE 2026-09-05.** `grep -n "network.usdc_address" src/uvd_x402_sdk/response.py` da `453:            "asset": network.usdc_address,`. **Nota:** el mensaje del commit `da3623f` cita esta linea como 442; era la posicion antes de que ese mismo commit corriera el archivo. La buena es 453 |
| 2026-09-05 | `create_402_headers` hardcodea `x402 USDC 1.0` en `X-Accept-Payment` | El tercer sitio del mismo literal. Es un default de parametro, no un literal enterrado, asi que un llamador ya puede corregirlo — por eso es la mas baja de las tres | P2 | **VIGENTE 2026-09-05.** `grep -n "x402 USDC" src/uvd_x402_sdk/response.py` da las lineas `151` y `514` |
| 2026-09-05 | Stellar y NEAR solo soportan USDC | Ninguna de las dos define un dict `tokens`, asi que no hay donde colgar un segundo token | P2 | **VIGENTE 2026-09-05.** `grep -n "tokens=" src/uvd_x402_sdk/networks/stellar.py src/uvd_x402_sdk/networks/near.py` no devuelve coincidencias (exit 1) |
| 2026-09-05 | `amount_usd` se cobra en unidades del asset nativo en XRPL (y lo heredaría Casper) | Auditoría del PR #2 (`docs/reports/2026-09-05-auditoria-pr2-casper.md`, H2). `client.py:772` -> `get_token_amount` escala con `usdc_decimals` sin mirar si el asset es stablecoin; `process_payment(h, Decimal("1.00"))` en `xrpl-mainnet` pide 1 XRP, no US$ 1 | P1 | **VIGENTE.** `grep -n 'get_token_amount(float' src/uvd_x402_sdk/client.py` -> 772; ningún test cubre un asset no-stable. Arreglo: negarse a convertir cuando `extra_config` no declara stablecoin, o exigir precio en unidades del asset; test rojo/verde |
| 2026-09-05 | `verify_facilitator_support=True` con la config por defecto falla en `main` por `xrpl-mainnet` | Auditoría del PR #2, §2.4. El facilitador (v2.14.0) anuncia `xrpl` / `xrpl-testnet` / `xrpl:0` / `xrpl:1`; el SDK registra `xrpl-mainnet` y `network_key()` no lo traduce | P2 | **VIGENTE.** `PYTHONPATH=src python -c "from uvd_x402_sdk import *; X402Client(config=X402Config(recipient_evm='0x'+'11'*20), verify_facilitator_support=True)"` -> `ConfigurationError: ... does not settle: xrpl-mainnet` |

---

## Cerradas en el barrido del 2026-09-05

| Date | Item | Priority | Status |
|---|---|---|---|
| 2026-09-04 | `transient_503_response` hardcodea `body["retryable"] = True` | P2 | **ARREGLADA — commit `e9c972b`.** La fila la daba por cubierta ("el llamador asumio ese riesgo por escrito"); medido, el defecto era otro y peor. El body que salia al comprador decia `retryable: true` arriba y escondia en `details` la unica prueba de que el facilitador ya habia difundido, sin ninguna senal de peligro en el top level; y con un `reason` de `WRITE_NOT_ATTEMPTED_REASONS` afirmaba ademas `safeToRetry: true` sobre un cuerpo que llevaba el hash adentro. Ahora `transaction` y `paymentId` suben al top level y `safeToRetry` queda en `False`. 5 tests nuevos: **rojo 4 failed / 1 passed, verde 5 passed** |
| 2026-09-04 | Publicar 0.75.0 para que MeshRelay saque los pines de `x402_version` | P1 | **VENCIDA — YA ESTABA PUBLICADA.** Los dos handoffs decian "0.75.0 (sin publicar)" y "cero PyPI". Medido: `pypi.org/pypi/uvd-x402-sdk/json` da `latest: 0.76.0`, con `0.75.0` y `0.76.0` en releases; `registry.npmjs.org/uvd-x402-sdk` da `latest: 2.81.0`, con `2.79.0` presente. **Las dos condiciones se cumplieron: turnstile y multibrain de MeshRelay pueden sacar los pines hoy** |
| 2026-09-05 | "`process_payment()` convierte montos con los decimales de USDC de la red" | — | **VENCIDA — commit `2a92cb5`.** `grep -c "token_decimals" src/uvd_x402_sdk/client.py` da `26` lineas (de la 727 a la 2180), propagado por verify, settle y process_payment, con tests para 18 decimales, 7, el borde `0` falsy y el rechazo del negativo en `tests/test_multi_token_requirements.py` |
| 2026-09-05 | El builder del 402 v1 hardcodea `token="USDC"` | P2 | **ARREGLADA — commit `da3623f`.** La fila nombraba un sitio; medido, eran dos: el campo `token` (entonces en `response.py:131`, hoy `142`) y el mensaje generado (entonces `117`, hoy `128`), `f"Payment of ${amount} USDC required"`. Corregir solo uno habria dejado el cuerpo contradiciendose. `create_402_response` acepta ahora `token` keyword-only con default `"USDC"`, opt-in como los otros dos flags de esa funcion, y `token=""` es `ValueError`. 7 tests: **rojo 6 failed / 1 passed, verde 7 passed** |
| 2026-09-05 | "SVM/Stellar/NEAR - Only USDC supported" (la mitad SVM) | — | **VENCIDA — commit `2a92cb5`.** `grep -n '"ausd"' src/uvd_x402_sdk/networks/solana.py` da `86`. Solana trae AUSD por Token2022 con su `token_2022_program_id` (linea 97). La mitad Stellar/NEAR sigue vigente y quedo arriba como fila propia |

---

## Nota de operacion (heredada, confirmada el 2026-09-05)

Este entorno tiene plugins de pytest globales que revientan la coleccion, y una
version vieja del paquete instalada en `site-packages`. La suite corre con:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PYTHONPATH=src python -m pytest -p pytest_asyncio.plugin
```

Sin `-p pytest_asyncio.plugin` se saltan tests **en silencio**. Sin
`PYTHONPATH=src` se testea el paquete instalado, no el del arbol.

Linea base al 2026-09-05, sobre `main` limpio: **869 passed, 0 skips**.
Con los tests de este barrido: **881 passed**.
`mypy src/` tiene **110 errores preexistentes en 14 archivos**, medidos con y
sin los cambios de hoy — declararlos antes de culpar a un cambio nuevo.
