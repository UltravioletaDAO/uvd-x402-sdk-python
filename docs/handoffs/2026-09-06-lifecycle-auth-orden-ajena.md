# El SDK de Python ya transporta una orden de ciclo de vida firmada por otro

**Fecha:** 2026-09-06 · **Rama:** `0xultravioleta/py-lifecycle-2` · **Versión:** 0.79.0 (sin publicar, **sin tag** — publicar lo decide c0der)
**Base medida:** `origin/main` en `fd094cf` (0.78.0)
**De dónde sale:** el handoff de Execution Market `docs/handoffs/2026-09-06-lifecycle-auth-firma-el-payer.md` (rama `main` de EM), que midió el hueco upstream: *"El SDK Python acepta solo `lifecycle_signer` en `release_via_facilitator` / `refund_via_facilitator`, no una orden **ya firmada**. Mientras no acepte un `lifecycle_auth=`, [EM] no puede transportar la orden de un tercero."*

---

## QUÉ / POR QUÉ / RIESGO

**QUÉ:** `release_via_facilitator()` y `refund_via_facilitator()` aceptan
`lifecycle_auth=`, el bloque de wire `{signer, deadline, nonce, signature}` **ya
firmado**, y lo cuelgan de `payload.lifecycleAuth` tal cual. Y
`lifecycle_auth_from_signature(typed_data, signature, signer)` arma ese bloque a
partir del typed data que devuelve `build_lifecycle_typed_data()` más los 65
bytes que devuelve un navegador.

**POR QUÉ:** el dueño decidió que la orden la firma el **payer**, y que quien
pide el movimiento la transporta. 0.78.0 solo tenía `lifecycle_signer=`, que
firma con una llave **dentro del proceso que llama** — exactamente lo que el
transportador no tiene y no debe tener. Sin `lifecycle_auth=` no había por dónde
entrar una orden ajena, y el lado de EM ya está mergeado esperando esto
(PR #178, flag `EM_LIFECYCLE_PAYER_SIGNS` en `off`).

**RIESGO:** que un llamador de hoy deje de funcionar. Acotado: `lifecycle_auth`
es `Optional` y por default `None`; sin ninguno de los dos argumentos el pedido
sale byte por byte igual que en 0.78.0. Lo fija
`test_sin_ninguno_de_los_dos_el_payload_sigue_saliendo_como_antes`, además del
`test_sin_firmante_el_payload_no_trae_lifecycleAuth` que ya existía.

---

## 1. La API nueva

### `lifecycle_auth=` en los dos métodos

`src/uvd_x402_sdk/advanced_escrow.py:1105` (`release_via_facilitator`) y
`:1143` (`refund_via_facilitator`), ambos delegando en
`_settle_via_facilitator` (`:974`).

```python
tx = client.release_via_facilitator(payment_info, lifecycle_auth=auth)
tx = client.refund_via_facilitator(payment_info, lifecycle_auth=auth)
```

- Se adjunta **tal cual**: `inner["lifecycleAuth"] = lifecycle_auth`
  (`advanced_escrow.py:1035`). No se re-firma, no se normaliza el hex, no se
  reordenan las claves, no se recalcula la ventana. El digest ya está cerrado
  sobre **ese** nonce y **ese** deadline; tocar un campo acá es firmar una cosa
  y enviar otra.
- **Excluyente con `lifecycle_signer=`.** Los dos juntos levantan `ValueError`
  antes del POST (`advanced_escrow.py:1002`). No hay una lectura correcta de los
  dos: uno firma una orden nueva con nonce y deadline propios, el otro trae una
  ajena; elegir en silencio manda al facilitador una orden distinta de la que el
  llamador cree haber mandado — y sobre `release` eso es plata que se mueve.

### `lifecycle_auth_from_signature(typed_data, signature, signer)`

`src/uvd_x402_sdk/escrow_signing.py:811`. Exportada desde
`uvd_x402_sdk` (`__init__.py`, import y `__all__`).

```python
typed = build_lifecycle_typed_data(          # ya era pública en 0.78.0
    action="release", payment_info=pi_wire, payer=payer_address,
    amount=1_000_000, chain_id=8453,
    deadline=int(time.time()) + 600, nonce="0x" + secrets.token_hex(32),
)
# -> al navegador como JSON; vuelve solo la firma
auth = lifecycle_auth_from_signature(
    typed_data=typed, signature=firma_del_navegador, signer=payer_address,
)
# -> {"signer", "deadline", "nonce", "signature"}
```

Tres decisiones que valen la pena por escrito:

1. **`deadline` y `nonce` no se pasan aparte** — salen del mismo `typed_data`
   que se firmó (`escrow_signing.py:896-897`). Aceptarlos por separado sería
   dejar que el wire declare una ventana distinta de la que entró al digest: un
   `bad_signature` que ninguna de las dos puntas puede nombrar.
2. **La firma se verifica contra `signer` antes de devolver nada**
   (`escrow_signing.py:888-893`). El error real del camino de navegador es que
   la página firma con la cuenta que tiene conectada y el backend cree que es
   otra; el facilitador contesta `bad_signature` y no dice cuál de los dos
   estaba mal. Recuperar acá es el único lugar donde sí se puede decir. Necesita
   `eth-account` (`pip install uvd-x402-sdk[signer]`) y lo dice por nombre si
   falta.
3. **Rechaza por nombre** una firma que no mide 65 bytes (`_sig_to_hex`,
   `escrow_signing.py:785`) y un typed data de otro dominio
   (`escrow_signing.py:869`) — pasar el EIP-712 equivocado produciría una firma
   perfectamente válida sobre otra cosa.

`build_lifecycle_typed_data()` **ya era pública y ya estaba exportada** desde
0.78.0 (`escrow_signing.py:620`, `__init__.py:357`): no se duplicó, solo se
documentó como la costura del navegador en el README.

---

## 2. Los tests, y que están en rojo sin el cambio

9 tests nuevos en `tests/test_lifecycle_auth.py` (925 pasan, 916 pasaban antes,
ninguno perdido). Los tres que pide el criterio de cierre:

| Qué prueba | Test |
|---|---|
| La orden ajena se transporta byte a byte | `test_lifecycle_auth_viaja_byte_a_byte_en_el_release` / `..._en_el_refund` |
| Los dos parámetros juntos fallan | `test_los_dos_parametros_juntos_son_un_error_explicito` |
| Paridad interna con `build_lifecycle_auth` | `test_una_orden_firmada_afuera_arma_el_mismo_bloque_que_si_la_firmaramos` |

**Rojo medido, en dos mitades separadas** (no solo el `ImportError` fácil):

```
# src entero de origin/main -> el archivo ni colecciona
ImportError: cannot import name 'lifecycle_auth_from_signature'

# escrow_signing.py NUEVO + advanced_escrow.py de origin/main
FAILED test_lifecycle_auth_viaja_byte_a_byte_en_el_release
FAILED test_lifecycle_auth_viaja_byte_a_byte_en_el_refund
FAILED test_los_dos_parametros_juntos_son_un_error_explicito
3 failed, 33 passed
```

La segunda mitad es la que importa: prueba que los tests de transporte
discriminan el cambio en `advanced_escrow.py` y no solamente la función nueva.

`ruff` y `mypy` sin deuda nueva: 497 y 120 en la rama, 497 y 120 en
`origin/main`, medidos con el mismo comando.

---

## Para c0der

**Lo que quedó hecho:** 0.79.0 en `pyproject.toml`, changelog en `README.md`,
PR abierto, CI verde, `git status` limpio. **Sin tag y sin publicar** — eso es
tuyo.

**Lo que le falta a EM para usarla.** El lado de EM ya está mergeado (PR #178,
`EM_LIFECYCLE_PAYER_SIGNS` en `off`), así que el orden es:

1. **Publicar 0.79.0 a PyPI.** Mientras no esté publicada, EM no la puede pinear
   y el flag no se puede prender. Es el único bloqueo real.
2. **EM mueve su pin** de `uvd-x402-sdk` a `>=0.79.0` y cambia su llamada a
   `lifecycle_auth=` (hoy no puede pasar nada, por eso el flag está en `off`).
3. **El facilitador sigue en `log`.** `ESCROW_LIFECYCLE_AUTH` no se mueve a
   `enforce` hasta ver órdenes con `verdict="ok"` en el tráfico real. La medición
   de 0.78.0: 2.953 release/refund en 17 días, **cero** firmadas.

**Un detalle que EM va a necesitar y no es obvio:** para armar el typed data hay
que pasar el `paymentInfo` **de wire** (camelCase). El helper que lo produce en
este SDK es privado (`AdvancedEscrowClient._payment_info_to_camel_dict`), pero no
hace falta: el digest normaliza (`int(...)` sobre `maxAmount` y los expiries,
`_salt_to_int` sobre el salt), así que el dict que EM ya arma sirve igual siempre
que traiga las 11 claves. El `payer` **no va adentro** del `paymentInfo` — viaja
como hermano, y es `client.payer`, que sí es público.

**Lo que NO se tocó:** el gemelo TypeScript sigue sin emitir `lifecycleAuth` en
ninguno de los dos caminos. El vector fijado para que lo persiga sigue siendo el
de `docs/handoffs/2026-09-05-lifecycle-auth-firma.md`, intacto; este cambio no
agrega vectores nuevos porque los dos caminos producen el mismo bloque de wire
—`test_una_orden_firmada_afuera_arma_el_mismo_bloque_que_si_la_firmaramos`— y
el vector que ya existe lo cubre.
