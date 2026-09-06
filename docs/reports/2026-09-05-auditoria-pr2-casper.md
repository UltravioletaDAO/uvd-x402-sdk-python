# Auditoría del PR #2 — Casper Network (wCSPR, CEP-18 `transfer_with_authorization`)

**Veredicto: 🛑 DO NOT SHIP (tal como está).** El código es correcto en forma pero no tiene dónde correr y, si corriera, cobraría ~350 veces menos de lo que el integrador cree; además cambia el 402 por defecto de los cuatro consumidores que mueven dinero sin que ninguno haya pedido Casper.

**El facilitador del stack NO soporta Casper hoy.** `GET https://facilitator.ultravioletadao.xyz/supported` (v2.14.0, 78 entradas) no la lista, y un `/verify` con cuerpo Casper devuelve `400 invalid_request_body` (medido en vivo, abajo). El facilitador "dedicado" que propone el PR (`x402-facilitator.cspr.cloud`) responde `401 authorization is not provided` a todo, y este SDK no tiene forma de mandar ese header. Hasta que alguno de los dos cambie, el PR es código muerto.

| | |
|---|---|
| PR | [UltravioletaDAO/uvd-x402-sdk-python#2](https://github.com/UltravioletaDAO/uvd-x402-sdk-python/pull/2), `feat/casper-network`, head `1b3f5f8`, base `bd50ba2` (v0.26.0) |
| Autor del PR | `mssteuer` — Michael Steuer, *"President & CTO of @casper-network, Co-founder & CTO of @make-software"* (perfil de GitHub, leído 2026-09-05) |
| Autor del commit | `jeanclawd99` — *"I'm @mssteuer's trusted AI Agent and COO"*, cuenta creada **2026-09-04**, 0 repos. El commit es del 2026-07-24 |
| Abierto | 2026-07-24 · **43 días** · +878/−8 · 12 archivos · `MERGEABLE` / `CLEAN` |
| Distancia a `main` | `main` (37e05c6, v0.76.0) tiene **70 commits** por encima de la base del PR. Merge textual limpio |
| Auditor | Claude Fable 5.1, effort xhigh, método `web3-security-auditor` |
| Fecha | 2026-09-05 |

**Declaración de límites.** Cero paquetes instalados (el PR no agrega dependencias; `pyproject.toml` solo suma keywords). Cero firmas, cero deploys, cero envíos a ninguna cadena ni testnet: las únicas llamadas de red fueron lecturas (`/supported`, `/version`, `query_global_state` en los RPC públicos de Casper, CoinGecko) y dos `POST /verify` al facilitador de la DAO con una firma de relleno (`0x11…`) que no puede liquidar nada. Cero push a la rama del PR. Las mediciones "después" se hicieron en un worktree descartable con `main` + PR mergeado localmente, borrado al terminar.

---

## 1. Qué hace el PR de verdad (y qué no)

El PR **no firma nada**. No hay `create_authorization` para Casper, no hay `sign_eip3009` equivalente, no hay construcción de dominio EIP-712 en el SDK. Lo que agrega es:

- Dos `NetworkConfig` (`casper`, `casper-testnet`) con `NetworkType.CASPER`, 9 decimales y el hash de paquete wCSPR como `usdc_address` — `src/uvd_x402_sdk/networks/casper.py:70-136`.
- Modelos Pydantic del payload de cable (`CasperAuthorization`, `CasperPayloadContent`) — `models.py:167-216`.
- Validadores por regex, conversores motes↔CSPR y heurísticas de nombre de cadena — `casper.py:148-413`.
- Plomería: `recipient_casper` en config, `casper`/`casper-testnet` en `supported_networks` **por defecto** (`config.py:131`), extracción del pagador en `get_payer_address` (`client.py:1571-1573`), clave `casper` en el 402 v1 (`response.py:104`), constante `CASPER_FACILITATOR_URL` (`facilitator.py:34`), mapeos CAIP-2 (`base.py:398,433`).
- 26 tests unitarios de forma (`tests/test_casper.py`).

Toda la verificación de firma, nonce, ventana de validez y liquidación queda **en el facilitador**. Por eso los seis puntos de abajo miran tanto lo que el PR escribe como a quién se lo manda.

---

## 2. Los seis puntos auditados

### 2.1 La firma

**Qué se firma.** Según el facilitador de referencia (`make-software/casper-x402`, `go/x402/mechanisms/casper/exact/facilitator/scheme.go:28-37`, leído de GitHub 2026-09-05): un `TransferWithAuthorization{from: address, to: address, value: uint256, validAfter: uint256, validBefore: uint256, nonce: bytes32}` bajo un dominio `BuildDomain(name, version, chainName, contractPackageHash)` (`scheme.go:170`). El `name`/`version` **no se leen de la cadena: llegan en `requirements.extra`** del servidor, y si faltan el verify falla con `ErrMissingTokenName` / `ErrMissingTokenVersion` (`scheme.go:157-164`).

**Lo que el SDK manda.** `client.py:795-799` solo adjunta `extra` para `NetworkType.EVM`. Para Casper el cuerpo sale **sin dominio**. Medido en el worktree mergeado:

```
CASPER requirements for Decimal('1.00'):
  {"network": "casper:casper", "maxAmountRequired": "1000000000",
   "asset": "8df5d267…c505b6", "payTo": "00cdcd…"}      -> extra present: False
BASE   requirements for Decimal('1.00'):
  {"network": "base", "maxAmountRequired": "1000000", "asset": "0x8335…", "extra": {"name": "USD Coin", "version": "2"}}
```

El `usdc_domain_name="Wrapped CSPR"` / `"1"` que el PR declara en `casper.py:80-81,116-117` **nunca sale por el cable**: es configuración muerta. El valor en sí es correcto — coincide con el named key `name = 'Wrapped CSPR'` leído del contrato en mainnet y testnet, y con `Extra: {"name": assetName, "version": "1"}` del servidor de referencia (`go/examples/server/main.go:108`).

**Quién firma vs. quién paga.** `VerifyEIP712Signature(digest, sig, publicKey)` del facilitador de referencia (`go/x402/signers/casper/facilitator.go:63-73`) verifica la firma contra la `publicKey` **que viene en el payload** y nunca comprueba que `account_hash(publicKey) == authorization.from`. El `/verify` devuelve `Payer: p.Authorization.From` (`scheme.go:214`). En el SDK, `get_payer_address` hace lo mismo: devuelve `authorization.from` sin atarlo a nada (`client.py:1573`; medido: `('00abab…', 'casper')` con una firma de relleno). Un tercero firma con **su** llave y pone la cuenta de la víctima en `from`: el verify dice válido y nombra a la víctima como pagador. Lo único que puede parar el débito es el contrato en cadena (§2.2), que este PR no controla.

> 🟠 **HIGH — H1.** `client.py:795-799` (main) + `casper.py:80-81`: el dominio EIP-712 no viaja para Casper; todo `/verify` y `/settle` construido por el SDK falla en el facilitador de referencia salvo que el llamador pase `eip712_domain={"name": "Wrapped CSPR", "version": "1"}` a mano en cada llamada. Prueba: el bloque de arriba, y `scheme.go:157-164`.
>
> 🟡 **MEDIUM — M1.** `client.py:1571-1573`: el pagador que el SDK expone no está atado al firmante. Igual que en EVM en el SDK, pero en EVM el `ecrecover` del facilitador ata `from`; aquí ningún componente antes de la cadena lo hace (`facilitator.go:63-73`). Cualquier consumidor que use el pagador de `verify` para allowlists o reputación sin esperar el `settle` es suplantable.

### 2.2 Nonces y replay

- **Generación.** Cliente de referencia: `crypto/rand` 32 bytes, `validAfter = now − 600`, `validBefore = now + maxTimeoutSeconds` (`exact/client/scheme.go:64-70`). El SDK fija `maxTimeoutSeconds=60` (`client.py:787`) → ventana de 60 s; el facilitador exige ≥ 6 s restantes (`facilitator/scheme.go:143-145`).
- **Chequeo en el facilitador.** `Verify` valida formato (32 bytes hex, `scheme.go:189-193`) y ventana, pero **no consulta si el nonce ya se usó**. No hay lectura de `authorization_state` antes de liquidar. Un mismo header reenviado pasa `/verify` dos veces; la segunda liquidación depende de que el contrato la rechace, y el gas de ese intento lo paga el facilitador.
- **En cadena.** Leído por RPC (`query_global_state`, sin firmar) el contrato vigente del paquete mainnet `8df5d267…` expone `authorization_state`, `cancel_authorization`, `transfer_with_authorization`, `receive_with_authorization` — la familia EIP-3009. [HIPÓTESIS] el nonce es por `(from, nonce)` como en EIP-3009; el WASM (`infra/local/deployer/Cep18X402.wasm`) no se puede leer aquí.
- **En el SDK.** `CasperAuthorization.nonce: str` (`models.py:190`) acepta cualquier string; `validate_casper_payload` (`casper.py:322-367`) no valida longitud del nonce ni que `validAfter`/`validBefore` sean numéricos, y **nadie la llama** desde el cliente — `get_casper_payload()` es un parse Pydantic sin reglas. Esto no crea riesgo de fondos (el facilitador re-valida) pero la función "validate" es opt-in y parcial.

> 🟢 **LOW — L1.** `casper.py:322-367`: `validate_casper_payload` no se invoca en ningún camino del cliente y no cubre nonce ni timestamps. Recomendación: o se cablea en `get_payer_address` / `_build_payment_requirements`, o se documenta como utilidad.

### 2.3 Decimales — el hallazgo que pesa

`process_payment(header, amount_usd)` convierte con `network_config.get_token_amount(float(expected_amount_usd))` (`client.py:772`) = `int(usd_amount * 10**usdc_decimals)` (`base.py:145`). Con `usdc_decimals=9` (`casper.py:79`):

```
process_payment(header, Decimal("1.00")) en casper  ->  maxAmountRequired = 1000000000 motes = 1 wCSPR
```

El parámetro se llama `amount_usd`, la firma pública del SDK lo documenta en USD, y los cuatro consumidores pasan precios en USD. **wCSPR no es un stablecoin.** Precio de CSPR leído de CoinGecko el 2026-09-05: **US$ 0.00284498**. Un endpoint que cobra "$1.00" recibe 1 wCSPR ≈ **US$ 0.0028: 351 veces menos.** El escalado de enteros es correcto (9 decimales bien aplicados); lo que está roto es la **unidad**. No es un bug de mil veces más o mil veces menos: es un bug de "esto no son dólares".

El PR lo hereda de XRPL, que tiene el mismo defecto (`xrpl.py:47`, XRP a 6 decimales por el mismo `get_token_amount`) y que tampoco lo documenta. Eso no absuelve al PR: agrega una segunda red donde `amount_usd` miente, y la agrega **en los defaults**.

Hallazgos secundarios del mismo eje:

- `cspr_to_motes(8.2)` devuelve `8199999999` (el exacto es `8200000000`): aritmética en `float` (`casper.py:171`). Un mote de diferencia hace que `authorization.value != requirements.amount` y el facilitador rechace con `ErrAmountMismatch` (`scheme.go:104`). El SDK ya resolvió esto en `Decimal` para el camino principal (`client.py:770`); el helper nuevo vuelve a `float`.
- `get_wcspr_contract_package("base")` devuelve el paquete wCSPR de **mainnet**, y `get_casper_chain_name("eip155:8453")` devuelve `"casper"` (`casper.py:301,317`): cualquier string que no contenga "test" cae a mainnet. Fail-open hacia la red con dinero real.
- El token se registra bajo la clave `"usdc"` (`casper.py:85,121`), así que `get_networks_by_token(TokenType.USDC)` **incluye a Casper** (medido: `True`). Un consumidor que enumere "redes con USDC" para armar sus `accepts` ofrece wCSPR etiquetado como USDC.

> 🟠 **HIGH — H2.** `client.py:772` + `casper.py:79`: `amount_usd` se interpreta como wCSPR. Prueba: el bloque de arriba (1e9 motes por `Decimal("1.00")`) y el precio de CoinGecko. Corrección mínima: negarse a convertir con `usdc_decimals` cuando `extra_config["settlement_asset"]` no es un stablecoin (o exigir `token_decimals` + un precio explícito en el asset), con test rojo/verde. Lo mismo aplica a XRPL, fuera de este PR — va al backlog.
>
> 🟡 **MEDIUM — M2.** `casper.py:85,121`: wCSPR bajo la clave `"usdc"` contamina `get_networks_by_token(USDC)`.
>
> 🟢 **LOW — L2.** `casper.py:171,301,317`: `float` en dinero y heurísticas que caen a mainnet.

### 2.4 Aislamiento — el PR sí cambia a las otras redes

Suites y conformidad, mismo entorno (`PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`, `-p asyncio`, `PYTHONPATH=src`):

| | `main` (37e05c6) | `main` + PR #2 |
|---|---|---|
| `pytest` | **881 passed**, 0 failed | **907 passed** (881 + 26 nuevos), 0 failed |
| Conformidad cruzada TS↔PY (`scripts/xlang/cross-language-conformance.mjs` del repo TS) | **266 checks, PASSED** | **266 checks, PASSED**, líneas `ok` idénticas (diff vacío) |
| `ruff check` sobre los archivos nuevos | — | All checks passed |

Cero regresiones en tests. Pero los tests no cubren el 402 por defecto, y ahí el PR **sí** cambia el comportamiento de las 25 redes existentes:

```
                                   main    main+PR
default supported_networks           25         27
v1 402 supportedChains (config
  por defecto, solo recipient_evm)   25         27   <- 'casper', 'casper-testnet' anunciados
v2 402 accepts                       19         19   (el v2 exige recipient; sin cambio)
```

`create_402_response` recorre `config.supported_networks` (`response.py:110`) y con `require_recipient=False` (el default histórico) anuncia todo lo que esté en la lista. **Todo consumidor que use los defaults del SDK empieza a anunciar `casper` y `casper-testnet` en su 402 v1 sin haber configurado nada**, apuntando a un facilitador que responde 400 a esa red. Además, con `verify_facilitator_support=True` el arranque pasa de fallar por un motivo a fallar por tres:

```
main    : Facilitator … does not settle: xrpl-mainnet
main+PR : Facilitator … does not settle: casper, casper-testnet, xrpl-mainnet
```

(El `xrpl-mainnet` es un defecto preexistente de `main` — el facilitador anuncia `xrpl`, el SDK pide `xrpl-mainnet` — fuera de este PR; queda en el backlog.)

Lo que **no** cambia: la unión `PayloadContent` (`models.py:214`) no se usa para parsear en ningún sitio, así que sumarle `CasperPayloadContent` es inerte; `get_network_by_chain_id` filtra por `NetworkType.EVM` (`base.py:209`), así que `chain_id=0` no colisiona; `extract_payload` de un header v2 Casper aplana `casper:casper` → `casper` como con cualquier otra red.

> 🟡 **MEDIUM — M3.** `config.py:131`: las dos redes entran en los defaults y cambian el 402 v1 de todos. El propio autor ofreció en el cuerpo del PR *"gate the networks behind `enabled=False` if you prefer a staged rollout"* — esa es la forma correcta: `enabled=False` **y** fuera de `supported_networks` por defecto.

### 2.5 Dependencias

**Ninguna.** El diff de `pyproject.toml` agrega cuatro keywords (`casper`, `cspr`, `wcspr`, `cep-18`) y nada más. `casper.py` importa `re` y `typing` de la stdlib y `networks.base` del propio SDK. Auditado el archivo completo antes de ejecutar un solo test: sin `eval`/`exec`/`subprocess`, sin llamadas de red, sin acceso a llaves. Nada se instaló en la máquina de trabajo.

### 2.6 El facilitador — no hay dónde usarlo

| Facilitador | Medición 2026-09-05 | Resultado |
|---|---|---|
| `facilitator.ultravioletadao.xyz` (x402-rs v2.14.0) | `GET /supported` → 78 entradas, `"casper"` no aparece | **No soporta Casper** |
| ídem | `POST /verify` con el cuerpo v2 que el SDK arma para `casper:casper` (firma de relleno) | `400 {"code":"invalid_request_body"}` |
| ídem | `POST /verify` con el cuerpo v1 para `casper` | `400 {"code":"invalid_request_body"}` |
| ídem, **control** | mismo cuerpo v2, cable `eip155:8453` | `200 {"isValid":false,"invalidReason":"insufficient_funds"}` — la forma es correcta; lo que no entra es la red |
| `x402-facilitator.cspr.cloud` (el del PR) | `GET /supported`, `GET /health`, `GET /` | `401 authorization is not provided` |
| Código fuente de x402-rs (`Z:\ultravioleta\dao\x402-rs`) | `grep -ril casper` en `.rs/.toml/.json` | 0 archivos de código (1 backup de recursos del bazaar) |
| SDK TypeScript (`uvd-x402-sdk-typescript` 2.76.0) | `grep -ril casper src/` · `gh pr list --search casper` | 0 menciones, 0 PRs |

Y aunque el integrador quisiera usar el facilitador de cspr.cloud:

1. El SDK **no rutea** Casper hacia él. `extra_config["facilitator_url"]` (`casper.py:100,132`) no lo lee nadie en `client.py`; `facilitator_url_for("casper")` con config por defecto devuelve el de la DAO (medido). `main` ya tiene `facilitator_by_network` (posterior a la base del PR), pero el PR no la usa y su README (`README.md:659`) recomienda cambiar `facilitator_url` de **todo el cliente**, lo que rompe las otras 25 redes.
2. El SDK **no puede autenticarse**. Los cuatro `httpx.post` al facilitador mandan solo `Content-Type` (`client.py:867,1092,1161,1311`); no existe ninguna opción de header. El servidor de referencia manda `Authorization: <apiKey>` (`go/examples/server/main.go:33-35,82-83`, `FACILITATOR_API_KEY` en `config.go:17`).
3. Aun con ruteo y API key, el `/verify` fallaría por H1 (sin `extra.name`/`version`).

> 🔴 **CRITICAL (de decisión, no de explotación) — C1.** No hay ningún facilitador alcanzable desde este SDK que hable Casper. Todo lo demás del PR es inalcanzable en producción, y sin embargo M3 sí alcanza a los consumidores. Esto no es un defecto del PR: es que **no hay dónde usarlo**.

---

## 3. Lo que el PR hace bien

- **Forma de cable exacta.** `CasperPayloadContent{signature, publicKey, authorization{from,to,value,validAfter,validBefore,nonce}}` es campo por campo `ExactCasperPayload` / `ExactCasperAuthorization` del repo de referencia (`go/x402/mechanisms/casper/types.go:37-52`). El alias `from` con `populate_by_name` calca `EVMAuthorization`.
- **Regexes idénticas** a `utils.go:11-12` (`^(00|01)[0-9a-fA-F]{64}$`, `^[0-9a-fA-F]{64}$`); la de public key distingue ed25519 (`01`+64) de secp256k1 (`02`+66) como Casper.
- **Hashes de paquete verificados en cadena** (RPC públicos, solo lectura, 2026-09-05): mainnet `8df5d267…` y testnet `3d80df21…` existen, `symbol = 'WCSPR'`, `name = 'Wrapped CSPR'`, `decimals = 9`, y el contrato vigente expone `transfer_with_authorization`. El PR no inventó direcciones.
- **Dominio EIP-712 correcto** (`Wrapped CSPR` / `1`) aunque el SDK no lo transmita (H1).
- **CAIP-2 y nombres de cadena correctos** (`casper:casper` / `casper:casper-test`, `chain_name` `casper` / `casper-test`) contra `constants.go:6-18`.
- **Cero dependencias nuevas**, ruff limpio, 26 tests que pasan, cero regresiones en 881 tests ni en los 266 checks de conformidad.
- **Sin manejo de llaves en el SDK**: al no firmar, el PR no abre superficie de exposición de secretos.
- El autor **ofreció el gate** (`enabled=False`) y es quien opera la red (CTO de Casper Network): si el stack alguna vez decide soportarla, la contraparte técnica existe.

---

## 4. Observaciones sin severidad

- ℹ️ **Procedencia.** El commit está firmado por `jeanclawd99` ("AI Agent and COO" de mssteuer), cuenta creada el 2026-09-04. No es un hallazgo de seguridad; sí es contexto para c0der sobre quién mantiene qué.
- ℹ️ **Contrato upgradeable.** Los dos paquetes wCSPR están `Unlocked` (3 versiones en mainnet, 8 en testnet) con entry point `upgrade`. Quien tenga la llave del paquete puede cambiar la semántica de `transfer_with_authorization`. USDC también es upgradeable, pero aquí [HIPÓTESIS] el dueño es MAKE / Casper Association y el stack no tiene relación de confianza formal con ellos.
- ℹ️ **Costo de gas del facilitador de referencia.** `defaultPaymentMotes = 7_000_000_000` (`scheme.go:29`) = 7 CSPR ≈ US$ 0.02 por liquidación al precio de hoy. Barato en dólares, pero exige una wallet operativa fondeada en una cadena nueva.
- ℹ️ **README.** `README.md:660` y `tests/test_casper.py:46` usan como destinatario de ejemplo `001857b5…e344`, que es el `PAYEE_ADDRESS` real del `.env.testnet` de MAKE. Los demás ejemplos del SDK usan placeholders (`0xYourWallet...`); un copy-paste manda fondos a la cuenta de un tercero.
- ℹ️ **Divergencia v1/v2 ya conocida** (CLAUDE.md, "Envelope Selection"): `extract_payload` aplana el header v2 a nombre v1, así que por el camino normal del cliente un pago Casper sale en sobre **v1** con `network: "casper"`; el facilitador de referencia solo conoce `casper:casper` y lee `payload.Accepted.Network` (`scheme.go:88-96`). Para Casper la divergencia no es cosmética: no hay nombre v1 que el facilitador de referencia acepte.

---

## 5. Tabla de hallazgos

| ID | Sev | Dónde | Qué | Prueba |
|---|---|---|---|---|
| C1 | 🔴 | facilitadores | Ningún facilitador alcanzable habla Casper | §2.6, mediciones en vivo |
| H1 | 🟠 | `client.py:795-799`, `casper.py:80-81` | Dominio EIP-712 no viaja; verify/settle fallan en el facilitador de referencia | `extra present: False`; `scheme.go:157-164` |
| H2 | 🟠 | `client.py:772`, `casper.py:79` | `amount_usd` cobra en wCSPR: $1.00 → 1 wCSPR ≈ US$ 0.0028 | `maxAmountRequired = 1000000000`; CoinGecko |
| M1 | 🟡 | `client.py:1571-1573` | Pagador no atado al firmante (tampoco en el facilitador de referencia) | `facilitator.go:63-73` |
| M2 | 🟡 | `casper.py:85,121` | wCSPR registrado como `"usdc"` | `get_networks_by_token(USDC)` incluye casper |
| M3 | 🟡 | `config.py:131` | Entra en defaults: 402 v1 de todos pasa de 25 a 27 redes | §2.4 |
| L1 | 🟢 | `casper.py:322-367` | `validate_casper_payload` no se llama y no valida nonce/timestamps | lectura |
| L2 | 🟢 | `casper.py:171,301,317` | `float` en dinero (`8.2` → off-by-one mote); heurísticas caen a mainnet | probe |

---

## Para c0der

**Veredicto: DO NOT SHIP.** No por un exploit — el PR no firma ni mueve fondos por sí mismo — sino porque (1) no hay facilitador en el stack ni alcanzable desde el SDK que liquide Casper, (2) el único efecto real de mergearlo hoy es cambiar el 402 por defecto de 402milly, karmakadabra, faro y execution-market para anunciar una red que responde 400, y (3) si algún día llegara a liquidar, cobra en wCSPR lo que el integrador escribió en dólares.

**Qué haría falta para CONDITIONAL** (todo lo de abajo, no una parte):

1. Un facilitador. O x402-rs aprende Casper (familia nueva: RPC de Casper, `TransactionV1`, firmante ed25519/secp256k1, la variante Casper de EIP-712 de `casper-ecosystem/casper-eip-712`, TxWatcher para deploys, wallet operativa fondeada en CSPR), o el SDK gana header de autenticación por facilitador y rutea Casper a cspr.cloud vía `facilitator_by_network` por defecto. La primera opción son semanas de un dev Rust con acceso a x402-rs [estimación, no medida: 2–4 semanas]; la segunda son ~1–2 días de SDK más una dependencia operativa de MAKE (su API key, su uptime, su política de gas).
2. H2 cerrado con test rojo/verde: el SDK se niega a convertir `amount_usd` con `usdc_decimals` cuando el asset de liquidación no es un stablecoin, o exige precio explícito en unidades del asset. Mismo arreglo para XRPL.
3. H1 cerrado: `extra` con dominio para toda red cuyo facilitador lo exija (no solo EVM), con el valor del registro.
4. M3 cerrado: `enabled=False` y fuera de `supported_networks` por defecto hasta que exista el punto 1.
5. M2 cerrado: clave de token propia (`wcspr`) en vez de `"usdc"`.
6. Paridad TS: hoy no hay PR gemelo en `uvd-x402-sdk-typescript`; el stack no publica una red en un SDK y no en el otro.

**Qué cuesta mantener una red que nadie del stack usa** [estimación de quien firma esto, no medida]: cada release de x402-rs y de los dos SDK suma una red más al `/supported`, a la matriz de conformidad, al chequeo de deriva de registro y al smoke de arranque; son horas por mes de quien mantenga x402-rs y los SDK (hoy, c0der y sus workers), más una wallet operativa en una cadena nueva que alguien tiene que fondear y vigilar, más la dependencia de que MAKE mantenga su facilitador con API key. Con **cero consumidores medidos** en los repos propios, ese costo no tiene contrapartida. Si Casper Network quiere estar en el SDK, lo razonable es que el PR llegue con el facilitador resuelto (punto 1) y con H2/H1/M3 cerrados; el autor ya se ofreció a ajustar y es el CTO de la red.

**Recomendación operativa:** responder al autor con los puntos 1–6 (c0der decide el canal; este reporte no le escribe), dejar el PR abierto sin mergear, y mover a backlog los dos defectos preexistentes de `main` que salieron a la luz: `amount_usd` en XRPL y `xrpl-mainnet` vs `xrpl` en el chequeo de arranque.

---

## Apéndice — comandos que reproducen las mediciones

```bash
# Suites (mismo entorno para antes y después)
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PYTHONPATH=src python -m pytest -p asyncio -q -p no:cacheprovider
# Conformidad cruzada, desde el repo TS
UVD_PYTHON=python UVD_X402_PY_ROOT=<worktree> node scripts/xlang/cross-language-conformance.mjs
# Facilitador de la DAO
curl -s https://facilitator.ultravioletadao.xyz/supported | grep -c -i casper   # 0
curl -s -o /dev/null -w "%{http_code}\n" https://x402-facilitator.cspr.cloud/supported  # 401
# Cadena (solo lectura): chain_get_state_root_hash + query_global_state sobre hash-<paquete>
# Precio: https://api.coingecko.com/api/v3/simple/price?ids=casper-network&vs_currencies=usd
```
