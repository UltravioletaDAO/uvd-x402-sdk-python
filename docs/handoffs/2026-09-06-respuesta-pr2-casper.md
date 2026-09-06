# Respuesta al autor del PR #2 (Casper) — publicada, PR abierto

**Fecha:** 2026-09-06 · **Rama:** `0xultravioleta/py-casper-reply` · **Cambios de código: ninguno**
**Comentario:** https://github.com/UltravioletaDAO/uvd-x402-sdk-python/pull/2#issuecomment-5556855313
**Fuente del juicio técnico:** `docs/reports/2026-09-05-auditoria-pr2-casper.md` (veredicto DO NOT SHIP)
**Estado del PR después de comentar:** `OPEN`, sin labels, 1 comentario (el nuestro), sin respuesta del autor todavía

---

## QUÉ / POR QUÉ / RIESGO

**QUÉ:** un solo comentario de revisión en inglés en el PR #2, con los seis puntos
que lo separan de mergeable, cada uno con `archivo:línea` del PR y el cambio que
lo cierra. El PR queda abierto: ni merge, ni cierre.

**POR QUÉ:** el PR llevaba 43 días abierto con cero comentarios. La auditoría ya
había decidido el fondo — ningún facilitador alcanzable desde este SDK liquida
Casper — y la decisión del dueño fue responder y dejar abierto: el bloqueo está
upstream (facilitador), no en el diff del autor.

**RIESGO:** es una superficie pública dirigida al CTO de Casper Network. El riesgo
es decir algo falso o algo que suene a rechazo del proyecto. Mitigado: las líneas
citadas se re-verificaron contra el head del PR (`1b3f5f8`) —no contra `main`, que
tiene 70 commits por encima— y el comentario abre y cierra reconociendo el trabajo
y ofreciendo los dos caminos.

---

## 1. Los seis puntos, tal como salieron publicados

| # | Punto | Dónde (head `1b3f5f8`) | Qué lo cierra |
|---|---|---|---|
| 1 | Ningún facilitador habla Casper | `facilitator.py:34`, `networks/casper.py:100,132`, `README.md:655` | O `x402-rs` aprende Casper, o el SDK gana header de auth por facilitador + ruteo por `facilitator_by_network` |
| 2 | `amount_usd` se cobra en wCSPR | `client.py:277` + `networks/casper.py:79` | Negarse a convertir USD con `usdc_decimals` si el asset no es stablecoin; test rojo/verde. (XRPL tiene el mismo defecto en `main`, ya en backlog) |
| 3 | El dominio EIP-712 no viaja | `client.py:296-301` + `networks/casper.py:80-81` | Poblar `extra` para toda red cuyo facilitador lo exija, no solo EVM |
| 4 | Las dos redes entran en los defaults | `config.py:110-111`, `response.py:75-76` | `enabled=False` **y** fuera de `supported_networks` por defecto |
| 5 | wCSPR bajo la clave `"usdc"` | `networks/casper.py:85,121` | Clave propia `wcspr` + `TokenType` propio |
| 6 | Sin paridad TypeScript | — | PR gemelo en `uvd-x402-sdk-typescript` que aterrice junto con este |

Notas menores que también van en el comentario (no bloqueantes): la dirección real
de pago usada como ejemplo (`README.md:656`, `tests/test_casper.py:46`), el `float`
en `cspr_to_motes` (`networks/casper.py:171`), las heurísticas que caen a mainnet
(`networks/casper.py:301,317`), `validate_casper_payload` sin llamador
(`networks/casper.py:322-367`), el pagador no atado al firmante antes del settle
(`client.py:765-767`), y el aplanado v2 → v1 de `extract_payload`, que para Casper
no es cosmético porque el facilitador de referencia solo conoce `casper:casper`.

## 2. Diferencia con las líneas de la auditoría

La auditoría cita `client.py:772` y `client.py:795-799` (posición en `main`, v0.76.0)
y `config.py:131`. En el head del PR ese mismo código está en `client.py:277`,
`client.py:296-301` y `config.py:110-111`. El comentario usa **las del head**, que es
lo que ve el autor al abrir el diff. Las de `networks/casper.py` y `models.py`
coinciden en ambos (archivos nuevos).

## 3. Lo que NO se hizo

- **No se cerró ni se mergeó el PR.** Sigue `OPEN`.
- **No se etiquetó.** El repo solo tiene las 9 labels por defecto de GitHub
  (`bug`, `documentation`, `duplicate`, `enhancement`, `good first issue`,
  `help wanted`, `invalid`, `question`, `wontfix`). No existe `blocked`,
  `needs-facilitator` ni equivalente, y la instrucción era no inventar labels.
  Si c0der quiere señal de bloqueo en el PR, hace falta crear la label primero.
- **No se tocó código.** Este handoff es el único archivo del PR de la rama.

## 4. Pendiente de seguimiento

- Ver si `mssteuer` responde, y por cuál de los dos caminos del punto 1.
- Los dos defectos preexistentes de `main` que la auditoría dejó al descubierto
  (`amount_usd` en XRPL, `xrpl-mainnet` vs `xrpl` en el chequeo de arranque) siguen
  siendo backlog nuestro, independientes de este PR.
