# XRPL: cobraba en XRP lo que el integrador escribió en dólares, y el nombre de red no era el del facilitador

**Estado: arreglado en 0.77.0, PR [#11](https://github.com/UltravioletaDAO/uvd-x402-sdk-python/pull/11) abierto sin mergear, sin tag.**

| | |
|---|---|
| Origen | Auditoría del PR #2 (Casper) — `docs/reports/2026-09-05-auditoria-pr2-casper.md`, hallazgo **H2** y la recomendación de cierre. Los dos defectos son de `main`, no del PR de Casper |
| Rama | `0xultravioleta/py-xrpl`, commit `67b7a0d`, base `4e74581` (= `origin/main`) |
| Versión | `pyproject.toml` 0.76.0 → **0.77.0** (MINOR: cambia el comportamiento del cobro) |
| Fecha | 2026-09-05 |

**Declaración de límites.** Cero paquetes instalados. Cero firmas, cero deploys, cero transacciones: las únicas llamadas de red fueron lecturas (`GET /supported` del facilitador de la DAO) y el `git push` de la rama del PR. Cero push a `main`, cero tag. El checkout principal (`Z:/ultravioleta/dao/uvd-x402-sdk-python`, en otra rama con archivos sucios) no se tocó; todo se midió contra `origin/main` desde un worktree aparte.

---

## 1. El cobro — `amount_usd` se cobraba en XRP

`process_payment(header, Decimal("1.00"))` sobre XRPL producía `maxAmountRequired = 1000000` drops, o sea **1 XRP**, no un dólar. Medido antes del arreglo:

```
xrpl-mainnet    decimals=6 asset=''  $1.00 -> 1000000
base            decimals=6           $1.00 -> 1000000
stellar         decimals=7           $1.00 -> 10000000
```

El escalado estaba bien — XRP tiene 6 decimales de verdad. Lo que estaba mal era la **unidad**. `NetworkConfig.get_token_amount()` (`networks/base.py:145` en `main`) hace `int(usd_amount * 10**usdc_decimals)`, y eso solo convierte dólares en unidades base cuando **una unidad entera es un dólar**: cierto para las 23 redes de stablecoin del registro, falso para una cadena que liquida en su propio activo nativo flotante.

`token_decimals` (agregado en una versión anterior para el mismo tipo de problema) **no rescataba esto**: arregla la ESCALA y el defecto es la UNIDAD — seis decimales de XRP siguen siendo XRP.

### El arreglo

- `NetworkConfig.usd_pegged` (`networks/base.py:129`), default `True` → las 23 redes restantes quedan byte a byte iguales.
- XRPL mainnet y testnet lo llevan en `False` (`networks/xrpl.py:65,88`).
- `get_token_amount()` (`networks/base.py:156`) y el constructor de requirements del cliente (`client.py:763-769`) **se niegan** en vez de convertir.
- El mensaje nombra el activo, dice qué habría cobrado el código viejo y apunta a `GET /supported` (`networks/base.py:160-176`) — negarse sin decir dónde mirar solo mueve el callejón sin salida una capa arriba.

**El camino válido** es nombrar un `asset` con paridad al dólar más su `token_decimals`. No es hipotético: el facilitador liquida un USDC en XRPL, emisor `rGm7WCVp9gb4jZHWTEtGUr4dd74z2XuWhE`, verificado como de Circle en `x402-rs/src/network.rs:1230-1242`.

---

## 2. El nombre de red — `xrpl`, no `xrpl-mainnet`

Medido en vivo contra `https://facilitator.ultravioletadao.xyz/supported` el 2026-09-05. Las cuatro entradas XRPL que publica:

| `network` | `networkAliases` | x402Version |
|---|---|---|
| `xrpl` | `xrpl`, `xrpl:0` | 1 |
| `xrpl-testnet` | `xrpl-testnet`, `xrpl:1` | 1 |
| `xrpl:0` | `xrpl`, `xrpl:0` | 2 |
| `xrpl:1` | `xrpl-testnet`, `xrpl:1` | 2 |

**`xrpl-mainnet` no aparece nunca.** La fuente de verdad en código concuerda: `x402-rs/src/network.rs:189` imprime `xrpl`, y `:251` acepta `"xrpl" | "xrpl-mainnet"` solo en su `FromStr`, que el comentario de `:719` describe como *"right for a lookup and **wrong for a wire format**"*. El SDK ponía justamente ese alias en el cable.

**El síntoma medido, antes:**

```
ConfigurationError: Facilitator https://facilitator.ultravioletadao.xyz does not settle: xrpl-mainnet.
```

**Después:** `verify_routes()` pasa contra el facilitador real, con `xrpl` en la lista verificada.

`xrpl-mainnet` **sigue resolviendo** como alias, por la misma tabla `_NETWORK_ALIASES` que ya cargaba `skale` (`networks/base.py:199-204`). Deja de ser una entrada aparte del registro — los conteos siguen en 25 redes — y deja de ser lo que el SDK emite: `validate_network("xrpl-mainnet")` ahora devuelve `"xrpl"`. El nombre del testnet ya coincidía y no se movió.

---

## 3. Medición, antes y después

| | `origin/main` (4e74581) | rama (67b7a0d) |
|---|---|---|
| `pytest` | **881 passed** | **889 passed** (881 + 8 nuevos, 0 perdidos) |
| Conformidad cruzada TS↔PY | 266 checks PASSED | **266 checks PASSED** |
| `ruff` (los 3 archivos de `src/` tocados) | 63 | **63** |
| `mypy src/` | 120 errores / 17 archivos | **120 errores / 17 archivos** |
| CI del PR | — | **Tests ✅ · Cross-language conformance ✅** |

El test de H2, `tests/test_xrpl_pricing.py`, **en rojo antes** del arreglo:

```
FAILED tests/test_xrpl_pricing.py::test_get_token_amount_refuses_on_xrpl
FAILED tests/test_xrpl_pricing.py::test_build_requirements_refuses_usd_on_xrpl
FAILED tests/test_xrpl_pricing.py::test_build_requirements_refuses_even_with_token_decimals
FAILED tests/test_xrpl_pricing.py::test_explicit_stablecoin_asset_prices_in_dollars
FAILED tests/test_xrpl_pricing.py::test_xrpl_mainnet_canonical_name_is_xrpl
FAILED tests/test_xrpl_pricing.py::test_xrpl_mainnet_alias_still_resolves
6 failed, 2 passed
```

**en verde después:** `8 passed`. Los dos que pasaban desde el principio son los controles: que las redes con paridad sigan convirtiendo igual, y que el nombre del testnet no se mueva.

Tres tests existentes se ajustaron **sin cambiar lo que prueban**: `tests/test_client.py:267` codificaba el nombre viejo, y los dos de `tests/test_envelope_selection.py:271,279` usaban XRPL como vehículo para probar la selección de sobre — ahora nombran el USDC de XRPL para pasar el guard de precio y seguir probando el sobre.

---

## Para c0der

### Qué cambió y qué versión

**0.77.0** (bump MINOR en `pyproject.toml`, entrada de CHANGELOG en el README cuya primera línea dice que XRPL cobraba mal). Dos arreglos: el SDK se niega a convertir un precio en USD cuando el activo de liquidación no tiene paridad al dólar, y la mainnet de XRPL pasa a llamarse `xrpl` con `xrpl-mainnet` como alias. **Sin tag** — publicar es un tag y lo decidís vos.

PR [#11](https://github.com/UltravioletaDAO/uvd-x402-sdk-python/pull/11) contra `main`, CI verde, sin mergear.

### ¿TypeScript tiene el mismo defecto? Sí, los dos

SDK TypeScript **2.76.0** (`package.json`), leído hoy. No lo toqué, como pediste.

- **Defecto del cobro: SÍ.** `src/backend/index.ts:402-403` —
  `Math.floor(parseFloat(amount) * Math.pow(10, chain.usdc.decimals))`, y en XRPL
  `chain.usdc = { address: 'XRP', decimals: 6, name: 'XRP' }` (`src/chains/index.ts:952-957`).
  Misma forma exacta que Python. *Atenuante:* el parámetro se documenta como
  `"Amount in human-readable format (e.g., \"1.00\")"` (`src/backend/index.ts:215`),
  no como `amountUsd`, así que el contrato es **ambiguo** en vez de explícitamente falso —
  pero el campo del que saca los decimales se llama `usdc`, que empuja a leerlo en dólares.

- **Defecto del nombre: SÍ, y ahí es peor que en Python.** `src/chains/index.ts:937,940`
  registra la mainnet como `xrpl-mainnet`. Y medido en runtime contra el `dist` de 2.76.0:

  ```
  chainToCAIP2("xrpl-mainnet") -> xrpl-mainnet
  chainToCAIP2("xrpl-testnet") -> xrpl-testnet
  chainToCAIP2("base")         -> eip155:8453
  ```

  Ese valor entra al campo `network` de un cuerpo **v2** en `src/backend/index.ts:407`
  (`x402Version === 2 ? chainToCAIP2(chainName) : chainName`) y en
  `src/client/X402Client.ts:698`. El facilitador espera `xrpl:0` ahí. Python devuelve
  `None` y salta la red; **TS emite un nombre v1 dentro de un cuerpo v2**, que es
  exactamente el 400 que el módulo de sobres existe para prevenir.

- **De yapa, el mismo M2 que marcaste en Casper:** `src/chains/index.ts:952-957`
  registra XRP bajo la clave `usdc`, así que cualquier consumidor de TS que enumere
  "redes con USDC" ofrece XRP etiquetado como USDC.

Los dos son despacho aparte, en `uvd-x402-sdk-typescript`. **Cuidado con el orden**: la corrección del nombre en TS toca el 402 v2, que es superficie de más consumidores que el fix de Python.

### Qué consumidor del stack cobra por XRPL hoy

**Ninguno.** `grep -rn "X402_RECIPIENT_XRPL"` sobre `execution-market`, `karmakadabra`, `describe-net`, `meshrelay` y `million` (excluyendo `node_modules`, `.build`, `.git`) no devuelve **ninguna** configuración con valor: solo la definición del propio SDK. Sin `recipient_xrpl` no hay destinatario, así que el 402 v2 salta la red y el v1 anuncia un callejón sin salida. **El defecto era latente, no estaba sangrando.**

Dos cosas que sí salieron de esa búsqueda y te sirven:

1. **`karmakadabra/tests/sdk/test_traza_endpoint.py:35`** ya lista `"xrpl"` — el nombre canónico — en su set de mainnets del facilitador. O sea que karmakadabra ya estaba alineado con el facilitador y **el SDK era el desalineado**. Ese test se pone rojo cuando el facilitador agrega una red; confirma independientemente cuál de los dos nombres es el bueno.
2. **402milly lleva una copia vendorizada del SDK** en `million/402milly/backend/lambdas/purchase_pixels/uvd_x402_sdk/` (su `config.py:127` todavía dice `"xrpl-mainnet", "xrpl-testnet"`). Esa copia **no recibe este arreglo** hasta que alguien la re-vendorice. No es urgente porque 402milly tampoco configura XRPL, pero es deriva: una copia del SDK que envejece sola en un lambda de producción.

### Hallazgo que NO toqué a propósito: XRPL sí tiene CAIP-2

El facilitador publica **`xrpl:0` y `xrpl:1`** con entradas `x402Version: 2` (tabla de la §2; `x402-rs/src/network.rs:613`). El SDK afirma lo contrario en cuatro lugares:

- `src/uvd_x402_sdk/networks/xrpl.py` (docstring — corregido en este PR, es lo único que toqué de esto)
- `src/uvd_x402_sdk/envelope.py:179` — *"a network that has none (XRPL…)"*
- `CLAUDE.md`, sección "Envelope Selection" — *"Networks with no CAIP-2 form (XRPL) stay on v1 under auto and raise under an explicit pin to 2"*
- `tests/test_client.py:291` — `assert to_caip2_network("xrpl-mainnet") is None`, un test que **pinea la afirmación falsa**

Lo dejé como está a propósito, y esta es la razón: agregar el CAIP-2 haría que XRPL empiece a aparecer en el `accepts` del 402 v2 de **todo consumidor que la tenga en `supported_networks`** — y está en los defaults (`config.py:127`). Ese es el mismo perfil de cambio de default que marcaste como **M3** en el PR de Casper, y no es lo que este encargo vino a hacer. Además tiene una consecuencia que hay que resolver primero: si XRPL entra al v2, `create_402_response_v2` llega a `response.py:444` (`network.get_token_amount(...)`) y con el arreglo de este PR eso **levanta** — habría que hacer que el 402 v2 **salte** la red sin paridad en vez de reventar la respuesta entera.

Es un encargo chico y bien delimitado, pero es tuyo decidir cuándo: toca los defaults de los cuatro consumidores que cobran.

### Lo que queda abierto, sin arreglar

- `NetworkConfig.format_token_amount()` (`networks/base.py:178`) es la inversa simétrica y sigue documentando *"Returns: Amount in USD"* mientras devuelve XRP en XRPL. No está en el camino del cobro (solo formatea), por eso no lo toqué; si el CAIP-2 entra, conviene cerrarlo en el mismo viaje.
- La limitación #1 del `CLAUDE.md` está **desactualizada en la letra pero viva en el efecto**: ya no hay un `token="USDC"` hardcodeado en `response.py:131` — hoy es un parámetro con default (`response.py:39`) y una validación que exige un símbolo no vacío (`response.py:124`). Pero el default sigue siendo `"USDC"`, así que un 402 de XRPL que no pase `token=` explícitamente sigue anunciando "USDC" cuando cobra otra cosa. Vale corregir esa fila del `CLAUDE.md` cuando toque.
