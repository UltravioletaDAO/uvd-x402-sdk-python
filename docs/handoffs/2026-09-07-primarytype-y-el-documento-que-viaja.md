# El documento de la orden de ciclo de vida no se podia firmar en un navegador

**Fecha:** 2026-09-07 · **Version:** 0.79.0 → **0.80.0** · **Gemelo:** `uvd-x402-sdk-typescript` 2.87.0 (sin cambios de SDK; solo el gate)

## Lo que se pidio, y lo que aparecio al medirlo

El encargo eran dos cosas: que el SDK de Python emitiera `primaryType` en el typed
data de la orden de ciclo de vida, y que el gate de conformidad cruzada comparara
ese campo. Las dos estan hechas.

Al probar la ruta de verdad —el documento que produce Python, serializado como
JSON, firmado por `viem`— aparecieron **tres** divergencias con el gemelo, no una.
Las tres cierran la misma puerta y las tres estan en este PR:

| # | Divergencia | Sintoma | Quien la habia parcheado |
|---|---|---|---|
| 1 | Falta `primaryType` | `viem` **tira** antes de mostrar nada | Execution Market, en el consumidor |
| 2 | `nonce` sale como `bytes` | `json.dumps` tira `TypeError`: el documento no se puede ni mandar | Execution Market, en el consumidor |
| 3 | Los uint salen como enteros de Python | `JSON.parse` redondea un `salt` de 32 bytes y el navegador firma **otro struct, sin error** | **nadie** |

La 1 y la 2 estaban parcheadas a mano en
`execution-market:mcp_server/integrations/x402/lifecycle_auth.py:495-515`, con el
comentario `[DIVERGENCIA DE LOS SDK, para upstream]` escrito por el worker que las
encontro. Este PR las sube al SDK y esas lineas se pueden borrar del consumidor.

La 3 no la habia visto nadie. Es la peor de las tres porque **no falla**: firma.

## La 3, medida

Con un `salt` real de 32 bytes (`0xab…ab`), antes de este cambio:

```
Python firma                    : 0x15a8587e82d062e4e1c97f343d0eea1f…572810a71b
viem, leyendo el MISMO documento: 0x4c88c56a7c4bd3f3cc4520a1ed435bee…8a2543a71c
```

Dos firmas validas sobre dos structs distintos. El facilitador rechaza la segunda
como `bad_signature` y ninguna de las dos puntas puede decir por que. El vector
fijado no lo veia porque su `salt` es `12345`, que entra en un `double`.

Despues del cambio, las dos firmas son **la misma**:

```
Python firma                    : 0x15a8587e82d062e4e1c97f343d0eea1f…572810a71b
viem, leyendo el MISMO documento: 0x15a8587e82d062e4e1c97f343d0eea1f…572810a71b
```

El gemelo TypeScript ya emitia strings (`toUintString`); Python era el que estaba
mal. Ninguno de los tres cambios toca el digest: EIP-712 hashea dominio, tipos y
mensaje, y `eth-account` decodifica un uint escrito como entero o como string
decimal al mismo valor.

## Criterio 1 — el vector compartido no se movio un byte

`src/lifecycle-auth.vectors.json`, firmado con el codigo de hoy (`a43a03f`) y con
el de este PR:

```
antes  (0.79.0, a43a03f)  0x78fe143886ee329e235cd7735e948f50ecd2ef32b20a4c88c46767cdd63b33ca
                            77553f16c73adacf754e34b2cc988137fd77b843649d990c9a1a5001b28a13a71c

despues (0.80.0)          0x78fe143886ee329e235cd7735e948f50ecd2ef32b20a4c88c46767cdd63b33ca
                            77553f16c73adacf754e34b2cc988137fd77b843649d990c9a1a5001b28a13a71c

identicas ✔   y las dos son la del `lifecycle_auth.rs` del facilitador
```

Digest antes y despues: `0x3dbd8a90a80785131a198a685f9f7400b1bf9a48d998e3aa1853abae56921918`.
Una orden firmada antes de este cambio sigue verificando despues.

Ademas, un test lo deja clavado: `test_primaryType_no_movio_la_firma_del_vector_fijado`
borra el campo del documento y comprueba que el digest es el mismo — el porque, no
solo el que.

## Criterio 2 — el gate, probado en los DOS estados

El gate vive en el repo de TypeScript (`scripts/xlang/`), asi que va en su PR hermano.

**Verde:** `CROSS-LANGUAGE CONFORMANCE PASSED — 430 checks across 8 phases` (exit 0).
Eran 414 antes de agregar el caso `release/real-salt`.

**Rojo, tres veces, quitando a proposito cada pieza:**

| Que se quito | Exit | Checks en rojo | Primer mensaje |
|---|---|---|---|
| `primaryType` del lado **Python** | 1 | 16 | `both SDKs name the root struct 'LifecycleOrder' … ts=LifecycleOrder py=null` |
| `primaryType` del lado **TypeScript** | 1 | 16 | `… ts=null py=LifecycleOrder` |
| los uint como string del lado **Python** | 1 | 5 | `both documents survive JSON with the same message … py={…"salt":12345}` |

El gate es simetrico: se pone rojo apunte quien apunte. El campo `primaryType` **no
entra al digest**, asi que ninguna comparacion de firmas podia verlo — por eso los
agentes ahora devuelven el DOCUMENTO que el SDK le entrego a la wallet, no solo los
65 bytes.

Y el mensaje viaja **por JSON de verdad**: el agente de Python lo escribe con
`json.dumps` y el gate lo lee con `JSON.parse`. La divergencia 3 la atrapa el
transporte mismo, no un assert que la imite.

## Criterio 3 — la suite de Python

`931 passed` (eran 925). Cinco tests nuevos; dos de ellos se ponen rojos si se saca
el campo:

```
FAILED tests/test_lifecycle_auth.py::test_el_typed_data_nombra_su_struct_raiz
FAILED tests/test_lifecycle_auth.py::test_el_documento_es_exactamente_lo_que_viem_recibe
```

## Criterio 4 — la ruta que motivo todo, corrida de verdad

No se aproximo: se corrio `viem` 2.56.3 contra el JSON que produce este SDK, con la
clave sintetica `0x11*32` del vector (nunca tuvo fondos).

```
con primaryType : firma 0x78fe14…3a71c  ← identica a la de Python, a la del gemelo
                                           TypeScript y a la del facilitador
sin primaryType : BaseError: Invalid primary type `undefined` must be one of
                  ["EIP712Domain","LifecycleOrder","PaymentInfo"]
```

Lo que queda sin probar: no se firmo desde un navegador real con una wallet de
extension, ni se mando una orden asi al facilitador en vivo. Lo que si esta probado
es la biblioteca que el navegador usa, contra el documento exacto que sale de este
SDK, y que los 65 bytes coinciden con el vector del `.rs`.

## Lo que cambio, archivo por archivo

**Emisores** — los cuatro lugares donde este SDK le entrega un typed data a una
wallet. El gemelo emite `primaryType` en todos; Python en ninguno:

- `escrow_signing.py` · `build_lifecycle_typed_data` → `LifecycleOrder` (el encargo),
  `nonce` en hex y los uint como string decimal
- `escrow_signing.py` · `build_escrow_pre_auth` → `ReceiveWithAuthorization`
  (`escrow-preauth.ts:454`)
- `erc7702.py` · `sign_eip3009_for_delegated` → `ReplaySafeHash`
  (`escrow-preauth.ts:511`)
- `advanced_escrow.py` · el ReceiveWithAuthorization del operador
  (`backend/index.ts:6293`)

Dejar tres de los cuatro sin arreglar era dejar la misma puerta cerrada para el
pre-auth, que es la otra mitad de la ruta del navegador.

**Lectura** — `lifecycle_auth_from_signature` rechaza un `primaryType` presente y
equivocado, y **tolera que falte**. El gemelo lo exige, pero el gemelo nunca emitio
un documento sin el; Python si: todo lo que salio de 0.78.0 y 0.79.0. Un backend que
guardo su documento antes de mandarlo a firmar tiene uno de esos en la mano, y
rechazarlo seria romper una orden buena por un campo que no entra al digest.

**Contrato** — el docstring de `WalletAdapter.sign_typed_data` ahora dice que el
dict lleva `primaryType`, que no entra al digest y que un firmante de navegador lo
necesita.

## Para c0der

**¿Cambio la firma?** No. Byte por byte, antes y despues, contra el vector
compartido y contra el `.rs` del facilitador:
`0x78fe143886ee329e235cd7735e948f50ecd2ef32b20a4c88c46767cdd63b33ca77553f16c73adacf754e34b2cc988137fd77b843649d990c9a1a5001b28a13a71c`.
Digest igual: `0x3dbd8a90…56921918`. Las ordenes ya emitidas siguen verificando.

**¿En que estados se probo el gate?** En cuatro: verde (430 checks, exit 0), y rojo
tres veces — sin `primaryType` en Python (16 checks rojos), sin `primaryType` en
TypeScript (16), y con los uint como enteros en Python (5). Los tres con exit 1.

**¿Hay que republicar los dos SDK?** **Solo Python.** El SDK de TypeScript no
cambia una linea de `src/`: su 2.87.0 ya emitia las tres cosas bien. Su PR toca
unicamente `scripts/xlang/`, que no se publica a npm. Entonces: publicar
`uvd-x402-sdk` 0.80.0 en PyPI, y mergear el PR de TypeScript sin bumpear ni
publicar.

**Un pendiente que no es de este PR:** cuando la 0.80.0 este en PyPI, las lineas
495-515 de `execution-market:mcp_server/integrations/x402/lifecycle_auth.py` sobran
—las dos que parchean `nonce` y `primaryType`— y el propio comentario del worker
dice que se sacan "el dia que los gemelos coincidan". Ese dia es este. Pero el
parche del consumidor es idempotente (escribe el mismo hex y el mismo string), asi
que no urge y no rompe nada si se queda.
