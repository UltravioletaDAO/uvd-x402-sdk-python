# El SDK de Python ya firma `release` y `refundInEscrow`

**Fecha:** 2026-09-05 · **Rama:** `0xultravioleta/py-lifecycle` · **Versión:** 0.78.0 (sin publicar, sin tag) — 0.77.0 se la llevó `py-xrpl`, que mergeó primero
**Facilitador medido:** `https://facilitator.ultravioletadao.xyz` — versión **2.14.0**, `escrowLifecycleAuth: "log"`
**Fuente de verdad del formato:** `x402-rs`, `src/payment_operator/lifecycle_auth.rs` (PR #21), solo lectura

---

## QUÉ / POR QUÉ / RIESGO

**QUÉ:** una función pública, `build_lifecycle_auth()`, produce la orden EIP-712
que el facilitador verifica para `release` y `refundInEscrow`; y
`release_via_facilitator()` / `refund_via_facilitator()` aceptan un
`lifecycle_signer` opcional que la cuelga de `payload.lifecycleAuth`.

**POR QUÉ:** las dos acciones mueven plata que **ya** está depositada, así que
ninguna lleva firma ERC-3009 — no queda transferencia que autorizar. Eso dejaba
sin responder *quién tiene derecho a pedir el movimiento*, y la respuesta de
hecho era "el que llame". El 2026-08-30 un tercero lo sondeó: cinco llamadas con
`paymentInfo` fabricado, dos minadas, gas gastado.

**RIESGO:** que un llamador que hoy funciona deje de funcionar. Acotado por
construcción: `lifecycle_signer` es opcional en las tres capas y por default es
`None`; sin él el pedido sale byte por byte igual que antes. Hay un test que lo
fija (`test_sin_firmante_el_payload_no_trae_lifecycleAuth`) y un mutante que
confirma que ese test discrimina.

---

## 1. Lo que se agregó

`src/uvd_x402_sdk/escrow_signing.py` (+295 líneas) — el módulo standalone,
dict-based y sin `web3` donde ya vivía `build_escrow_pre_auth`:

| símbolo | qué es |
|---|---|
| `build_lifecycle_auth(action, payment_info, payer, amount, chain_id, wallet, deadline=None, nonce=None, now=None)` | firma y devuelve `{"signer","deadline","nonce","signature"}` |
| `build_lifecycle_typed_data(...)` | el documento EIP-712 en crudo — la costura que el gemelo TS espeja |
| `LIFECYCLE_ORDER_TYPES` | los dos structs, en el orden exacto de `lifecycle_auth.rs:73-99` |
| `LIFECYCLE_DOMAIN_NAME` / `_VERSION` | `"x402 escrow lifecycle"` / `"1"` |
| `LIFECYCLE_MAX_DEADLINE_SECS` = 900 | el techo del facilitador (`lifecycle_auth.rs:64`) |
| `LIFECYCLE_DEFAULT_DEADLINE_SECS` = 600 | lo que firmamos por default |
| `LIFECYCLE_ACTIONS` | `("release", "refundInEscrow")` |

`src/uvd_x402_sdk/advanced_escrow.py` — `_settle_via_facilitator()`,
`release_via_facilitator()` y `refund_via_facilitator()` toman
`lifecycle_signer: Optional[WalletAdapter] = None`.

**El firmante se inyecta.** El SDK no sale a buscar una clave al entorno por su
cuenta: recibe cualquier `WalletAdapter` (`EnvKeyAdapter`, un KMS, una wallet de
navegador).

---

## 2. La política, que es la del facilitador

| acción | firmantes aceptados |
|---|---|
| `release` | el payer; el dueño del operador (`FEE_RECIPIENT()`, leído on-chain) |
| `refundInEscrow` | el receiver; el dueño del operador; el payer, pero solo pasado `authorizationExpiry` |

El receiver **nunca** puede hacer `release` (auto-pagarse es justo lo que el
escrow existe para impedir) y el payer **nunca** puede refundear antes del
vencimiento (eso es el chargeback).

Tres trampas, cada una un rechazo que el facilitador nombra y el llamador no
puede ver:

1. **`paymentInfo.salt` es `bytes32` en el wire y `uint256` en la firma.** El
   facilitador lo convierte con `U256::from_be_bytes` (`types.rs:288`).
   Firmarlo como string da otro digest y un `bad_signature` cuyo único síntoma
   es que ninguna orden verifica nunca. El SDK lo convierte.
2. **El `amount` firmado es el enviado.** Firmar `max_amount` y mandar un
   parcial es una orden que no verifica — y el parcial es el caso normal de un
   stream, que emite una orden y un nonce por delta.
3. **El `deadline` tiene techo de 900 s.** El default firma `now + 600`: firmar
   los 900 exactos hace que un reloj del facilitador cinco segundos atrasado
   decida el veredicto (`deadline_too_far`).

---

## 3. Verificación contra el facilitador VIVO, en `log`

`GET /settle` → `{"endpoint":"/settle",...,"escrowLifecycleAuth":"log"}`

**Sin fondos y sin gas, por construcción.** Red de prueba (base-sepolia) y
`tokenCollector` deliberadamente inválido: en `execute_release_flow`
(`operator.rs:455-480`) el orden es `for_network → get_evm_provider →
lifecycle_auth::gate → execute_release`, y lo primero que hace `execute_release`
es `validate_addresses` (`operator.rs:756`), que revienta con ese collector. El
gate corre y registra su veredicto; la request muere antes de que se arme una
sola transacción. Las tres devolvieron
`token_collector mismatch: client=0x…dead`, ninguna produjo tx.

Log de `/ecs/facilitator-production`, verbatim:

```
2026-09-06T01:27:49.199084Z  INFO … lifecycle_auth: escrow lifecycle order accepted
  action="release" network=base-sepolia mode="log" verdict="ok"
  signer=Some(0x0ce2c9e3e2b183574cf8c1e7e38c245380a2331e)
  operator=0xfa8c4cb156053b867ae7489220a29b5939e3df70
  payer=0x0ce2c9e3e2b183574cf8c1e7e38c245380a2331e
  receiver=0xd67a16a4bdcc5bb818046bdbb8eb3615cc68fe43

2026-09-06T01:29:45.099379Z  WARN … lifecycle_auth: escrow lifecycle order NOT authorized
  action="release" network=base-sepolia mode="log" verdict="unauthorized_role"
  signer=Some(0x48a61595ac02e6ad51267009b0612ecf760a214f)
  operator=0x7d092ec506b3d43eb87846f9c9739303785d7b2f
  detail=0x48A61595Ac02E6aD51267009B0612ECf760a214f is neither a party to this
         escrow nor the operator owner
```

Ese `verdict="ok"` es **el primero**: la medición de c0der fue 2.953
release/refund en 17 días, 22 pagadores, 38 receptores, 9 redes, **cero con
firma**.

Una tercera, con un operator que no existe en sepolia, dio
`verdict="owner_unverifiable"` (`01:27:49.457797Z`) — el camino retryable, que
también quedó ejercitado.

---

## 4. Los tests, probados en rojo

`tests/test_lifecycle_auth.py`, 26 tests. Suite completa: **907 passed**.

Rojo previo: contra `origin/main` el módulo no colecciona —
`ImportError: cannot import name 'LIFECYCLE_MAX_DEADLINE_SECS'`.

Ese rojo prueba que la API no existía, no que cada assert discrimine. Así que
además se corrieron **10 mutantes**, uno por vez, cada uno contra el test que lo
cubre. Los 10 murieron:

| se rompió a propósito | test que se puso rojo |
|---|---|
| campos de `PaymentInfo` reordenados (payer ↔ receiver) | `test_el_digest_es_el_del_facilitador` |
| nombre del dominio EIP-712 cambiado | `test_el_digest_es_el_del_facilitador` |
| `salt` firmado como hex string | `test_el_salt_entra_como_entero_no_como_hex` |
| nonce por default fijo | `test_cada_orden_trae_un_nonce_distinto` |
| sin techo de deadline | `test_una_deadline_mas_alla_del_techo_tampoco` |
| sin chequeo de deadline vencida | `test_una_deadline_vencida_no_se_llega_a_firmar` |
| default pegado al techo (900 s) | `test_el_default_deja_colchon_contra_el_techo` |
| `paymentInfo` incompleto completado por default | `test_un_paymentInfo_incompleto_no_se_firma_por_default` |
| se firma `max_amount` y no el monto enviado | `test_el_monto_firmado_es_el_monto_enviado` |
| `lifecycleAuth` mandado siempre | `test_sin_firmante_el_payload_no_trae_lifecycleAuth` |

El pin duro es `test_el_digest_es_el_del_facilitador`: el type string EIP-712
está **tecleado a mano** desde el orden de campos del `.rs`, no derivado de
`LIFECYCLE_ORDER_TYPES`. Si viniera de ahí, el test solo probaría que el SDK
coincide consigo mismo.

**Conformidad cruzada TS/PY:** `node scripts/xlang/cross-language-conformance.mjs`
con `UVD_X402_PY_ROOT` apuntando a este worktree →
`CROSS-LANGUAGE CONFORMANCE PASSED — 266 checks across 5 phases`.

---

## Para c0der

### La API nueva

```python
from uvd_x402_sdk import build_lifecycle_auth, EnvKeyAdapter

auth = build_lifecycle_auth(
    action="release",              # o "refundInEscrow"
    payment_info=pi_wire,          # el dict camelCase que se ENVÍA
    payer=payer_address,           # payload.payer, NO va adentro de payment_info
    amount=1_000_000,              # el MISMO que payload.amount
    chain_id=8453,
    wallet=EnvKeyAdapter(clave),   # inyectado; el SDK no lee el entorno
)
# -> payload["lifecycleAuth"] = auth
```

o, sin armar nada:

```python
client.release_via_facilitator(payment_info, lifecycle_signer=adapter)
client.refund_via_facilitator(payment_info, lifecycle_signer=adapter)
```

### Qué necesita Execution Market para firmar — y con cuál llave

EM ya llama `release`/`refundInEscrow` contra `/settle`. Le falta **una sola
cosa**: pasar un `lifecycle_signer`. No tiene que reimplementar nada.

**La pregunta abierta (`WALLET_PRIVATE_KEY` → `FEE_RECIPIENT()`) tiene ahora un
dato duro.** El operador real de base-sepolia
(`0x7D092ec506B3D43EB87846F9c9739303785D7B2f`, `addresses.rs:324`) responde:

```
FEE_RECIPIENT()  (selector 0xebd09054)  ->  0x34033041a5944b8f10f8e4d8496bfb84f1a293a8
```

O sea: **la llave con la que EM firme tiene que ser el payer del escrow, o la
dueña de ese `FEE_RECIPIENT()`.** Cualquier otra da `unauthorized_role`, que es
justo el veredicto que quedó registrado arriba. Antes de despachar a EM hay que
resolver, para cada red donde EM opera:

1. leer `FEE_RECIPIENT()` del operator que EM usa en esa red (el `eth_call` es
   gratis, el selector está arriba);
2. decidir si el `WALLET_PRIVATE_KEY` de EM **es** esa dirección. Si lo es, EM
   firma como `operator_owner` en las dos acciones y no depende del payer. Si no
   lo es, EM solo puede firmar los `release` donde él mismo sea el payer, y
   ningún `refundInEscrow` salvo que sea el receiver;
3. si no coincide y hace falta que coincida, eso es una wallet dedicada — skill
   `uvd-wallet-generar`, no un generador propio de EM.

**Esto es lo que hay que cerrar antes de mover el facilitador a `enforce`**, y
es la razón de que el orden del plan sea SDK → EM → ventana en `log` → enforce.

### Qué le falta al gemelo TypeScript

El SDK de TypeScript ya tipa `PaymentInfo` (2.84.0) pero **no emite
`lifecycleAuth`**. La conformidad cruzada de 266 checks no lo cubre: es de
ERC-8128, no de escrow. Lo que tiene que espejar, exacto:

**Dominio** — sin `verifyingContract`, porque `paymentInfo.operator` ya viaja
dentro del struct firmado:

```json
{ "name": "x402 escrow lifecycle", "version": "1", "chainId": 8453 }
```

**Tipos** — el orden de los campos es parte del type hash; reordenarlo invalida
toda orden emitida:

```json
{
  "LifecycleOrder": [
    { "name": "action",      "type": "string" },
    { "name": "amount",      "type": "uint256" },
    { "name": "deadline",    "type": "uint256" },
    { "name": "nonce",       "type": "bytes32" },
    { "name": "paymentInfo", "type": "PaymentInfo" }
  ],
  "PaymentInfo": [
    { "name": "operator",            "type": "address" },
    { "name": "payer",               "type": "address" },
    { "name": "receiver",            "type": "address" },
    { "name": "token",               "type": "address" },
    { "name": "maxAmount",           "type": "uint120" },
    { "name": "preApprovalExpiry",   "type": "uint48" },
    { "name": "authorizationExpiry", "type": "uint48" },
    { "name": "refundExpiry",        "type": "uint48" },
    { "name": "minFeeBps",           "type": "uint16" },
    { "name": "maxFeeBps",           "type": "uint16" },
    { "name": "feeReceiver",         "type": "address" },
    { "name": "salt",                "type": "uint256" }
  ]
}
```

**El bloque de wire**, que va en `payload.lifecycleAuth`:

```json
{
  "signer":    "0x…",
  "deadline":  1757000600,
  "nonce":     "0x0101…01",
  "signature": "0x…"
}
```

**Vector determinista para comparar byte a byte.** Clave de prueba
`0x1111…11` (32 bytes de `0x11`), `payer` = `signer` =
`0x19E7E376E7C213B7E7e7e46cc70A5dD086DAff2A`, `chainId` 8453,
`action` `"release"`, `amount` `1000000`, `deadline` `1757000600`,
`nonce` `0x0101…01`, y este `paymentInfo`:

```json
{
  "operator": "0x271f9fa7f8907aCf178CCFB470076D9129D8F0Eb",
  "receiver": "0x2222222222222222222222222222222222222222",
  "token": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
  "maxAmount": "1000000",
  "preApprovalExpiry": 1757003600,
  "authorizationExpiry": 1757007200,
  "refundExpiry": 1759592000,
  "minFeeBps": 0,
  "maxFeeBps": 1300,
  "feeReceiver": "0xaE07cEB6b395BC685a776a0b4c489E8d9cE9A6ad",
  "salt": "0x0000000000000000000000000000000000000000000000000000000000003039"
}
```

produce, **byte por byte**:

```
digest      0x3dbd8a90a80785131a198a685f9f7400b1bf9a48d998e3aa1853abae56921918
signature   0x78fe143886ee329e235cd7735e948f50ecd2ef32b20a4c88c46767cdd63b33ca
              77553f16c73adacf754e34b2cc988137fd77b843649d990c9a1a5001b28a13a71c
```

(el `salt` entra al digest como el entero `12345`, no como el string hex.)

Si el TS produce otra firma para ese vector, están divergiendo, y la que manda
es el `.rs`. Del lado Python el vector está **fijado en un test**
(`test_vector_fijado_para_el_gemelo_typescript`), así que no puede derivar en
silencio mientras el TS lo persigue.

### Lo que NO se hizo, a propósito

- **Sin tag y sin publicar.** El bump a 0.78.0 está en `pyproject.toml` y el
  changelog en el README; publicar es un tag y lo decide c0der.
- **No se tocó `x402-rs`.** Solo lectura, como pedía el encargo.
- **No se tocó el gemelo TS.** Se corrió su harness de conformidad contra este
  worktree y se dejó el repo intacto (`git status` idéntico antes y después).
- **Fondos: cero.** Ninguna de las tres requests vivas pudo producir una
  transacción, y está argumentado arriba por qué no.
