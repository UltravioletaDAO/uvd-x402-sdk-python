# Barrido de backlog — cuatro filas estaban vencidas y el backlog no existía

**Fecha:** 2026-09-05 · **Rama:** `0xultravioleta/px-backlog` · **Base:** `main` en `0577982` (0.76.0)
**Encargo:** verificar el backlog de este repo, cerrar lo vencido, hacer lo real.

---

## QUÉ / POR QUÉ / RIESGO

**QUÉ:** el repo no tenía backlog — las filas vivían dentro del cuerpo de un
handoff. Ahora hay `docs/planning/BACKLOG.md` con once filas verificadas contra
disco, git y los registros de paquetes: nueve abiertas con el comando que lo
prueba, cinco cerradas (dos por vencidas, tres arregladas). Y dos defectos de
los que estaban abiertos quedaron arreglados con test discriminante.

**POR QUÉ:** el encargo predijo tasa alta de filas vencidas y se quedó corto en
un sentido y largo en otro. De once filas barridas, **cuatro ya no eran ciertas**
— incluyendo una que decía "0.75.0 sin publicar" cuando PyPI ya tenía 0.76.0 y
npm 2.81.0, o sea que MeshRelay lleva días desbloqueado sin saberlo. Pero la
causa de fondo no era que las filas envejecieran: era que **el backlog estaba en
la §5 de un handoff**, tres niveles bajo un título, donde nadie lo barre.

**RIESGO:** los dos arreglos son opt-in o de camino no-feliz, con guarda verde en
los dos estados. El default de `create_402_response` sale byte por byte igual y
el 502 transitorio sin hash también. Suite 869 → 881, cero rojos nuevos.

---

## 1. Refutaciones — lo que el spec decía y no era

El encargo pedía explícitamente que lo refutara. Cinco cosas:

**1.1 — `docs/planning/BACKLOG.md` no existía.** El spec decía "suele ser
`docs/planning/BACKLOG.md`, pero puede llamarse distinto o vivir en otro lado".
No vivía en otro lado: **no existía en ninguna forma**. El único backlog del
repo era una tabla de cinco filas dentro de
`docs/handoffs/2026-09-04-py-hash-al-llamador.md` §5. Lo encontré buscando por
forma (`grep -rl -iE '\|\s*(P0|P1|P2|Priority)\s*\|' --include="*.md" .`), que
devolvió exactamente un archivo.

**1.2 — La fila que decía "sin publicar" era la más vencida de todas.** Los dos
handoffs de ayer cierran con "0.75.0 (sin publicar)" y "**Cero push, cero PyPI,
cero deploy**". Medido hoy:

```
$ python -c "...pypi.org/pypi/uvd-x402-sdk/json..."
latest: 0.76.0
releases: ['0.75.0', '0.76.0', ...]

$ python -c "...registry.npmjs.org/uvd-x402-sdk..."
latest: {'latest': '2.81.0'}
ultimas: ['2.76.0', '2.77.0', '2.78.0', '2.79.0', '2.80.0', '2.81.0']
```

El handoff `auto-sin-network.md` dice: *"MeshRelay puede sacar los pines de
turnstile y multibrain cuando publiques —0.75.0 del lado Python, 2.79.0 del lado
TypeScript—, no antes."* **Las dos condiciones se cumplieron.** Es trabajo
desbloqueado que nadie sabe que está desbloqueado, y es de otro repo.

**1.3 — La fila del checkout sucio no estaba vencida: estaba incompleta, y es el
bloqueador de las otras dos.** Decía "está en 0.72.0 y sucio, 14 modificados + 5
sin trackear". Los 19 archivos siguen exactos. Lo que no decía:

```
$ git -C Z:/ultravioleta/dao/uvd-x402-sdk-python log --oneline cfdd270..origin/main
0577982 fix(errors): ... (0.76.0) (#8)
7dba1e1 fix(envelope): ... (0.75.0) (#7)
9ec55b1 feat(envelope): ... (0.74.0) (#6)
6a20818 feat(dx402): modo pointer en Python ...
89e839d chore: bump to 0.72.0 ...
```

**Cinco commits detrás de `main`, y además un commit local sin pushear**
(`cfdd270`). No es una fila más de la lista: las filas P0 y P2 de abajo están
las dos adentro de ese árbol.

**1.4 — La fila P2 de `WriterUnavailableError` pedía la acción equivocada.**
Decía "código muerto en `main`; probablemente se cablea en el trabajo sin
commitear de `Z:`". Ese "probablemente" es un sí:

```
$ grep -rn 'WriterUnavailableError' Z:/.../src/
client.py:159:    cls = WriterUnavailableError if status == 503 else FacilitatorError
erc8004.py:589:    return WriterUnavailableError(
exceptions.py:368:class WriterUnavailableError(FacilitatorError)   # <- en main hereda de X402Error
__init__.py:99  y  __init__.py:456                                  # <- ya exportada alli
```

Ya se levanta, ya se exporta, **y cambió de clase padre**. Arreglarlo en `main`
no es cerrar una fila, es crear un conflicto sobre una jerarquía de excepciones.
Quedó marcada **NO TOCAR** con el motivo escrito.

Hallazgo extra de esa verificación: en `main`, `WriterUnavailableError` es la
**única de las 14 clases de `exceptions.py` que no aparece en `__init__.py`**.
Sus dos hermanas del mismo grupo (`LookupInconclusiveError`,
`RegistrationPendingError`) sí. Es un olvido, no una decisión.

**1.5 — La fila P2 de `transient_503_response` se daba por cubierta y no lo
estaba.** Decía: *"Protegido por su docstring… el llamador asumió ese riesgo por
escrito"*. Reproducido antes de tocar nada, sobre un 502 con hash:

```
body["retryable"]             = True      <- lo que el comprador lee
body["details"]["retryable"]  = False     <- el veredicto real, un nivel abajo
body["details"]["transaction"] presente = True
top safeToRetry = <<AUSENTE>>
```

Asumir el riesgo de reintentar no es lo mismo que no recibir la evidencia. Y
había un caso peor que la fila no contemplaba: con un `reason` de
`WRITE_NOT_ATTEMPTED_REASONS` (p. ej. `holder_unknown`, "el write nunca corrió,
re-presentá"), el body afirmaba **`safeToRetry: true` sobre un cuerpo que
llevaba el hash de la difusión adentro**. Arreglado.

---

## 2. Lo que arreglé, con las dos salidas

### 2.1 `transient_503_response` — commit `e9c972b`

El hash y el `paymentId` suben al top level y `safeToRetry` queda en `False`,
gane lo que gane el `reason`. Es la misma regla que
`FacilitatorError._retryable_verdict` (la evidencia dura le gana a la etiqueta),
un nivel más arriba: en el JSON que cruza la red hasta el que pagó.

**ROJO** — sin el cambio, con los tests puestos:

```
FAILED ...::TestElHashSubeAlTopLevel::test_el_hash_viaja_en_el_top_level
FAILED ...::TestElHashSubeAlTopLevel::test_safe_to_retry_es_falso_por_el_hash_aunque_no_haya_reason
FAILED ...::TestElHashSubeAlTopLevel::test_el_hash_gana_sobre_un_reason_que_autoriza_reintentar
FAILED ...::TestElHashSubeAlTopLevel::test_el_payment_id_tambien_sube
4 failed, 1 passed, 39 warnings in 1.58s
```

**VERDE** — con el cambio:

```
5 passed, 39 warnings in 1.34s
```

El 1 que pasa en rojo es la guarda del camino feliz (`test_control_el_502_sin_hash_no_cambia_en_nada`),
que tiene que pasar en los dos estados o no está guardando nada.

**Un test mío no discriminaba y lo agarré.** La primera versión de
`test_el_hash_gana_sobre_un_reason_que_dice_que_es_seguro` usaba
`reason="retry_safe"`, que **no está** en `WRITE_NOT_ATTEMPTED_REASONS` — así que
pasaba en rojo sin probar nada. Lo reescribí con `holder_unknown`, que sí
autoriza, y ahí falló como debía. Un test decorativo es peor que no tenerlo.

### 2.2 El 402 anunciaba USDC aunque se cobrara EURC — commit `da3623f`

`create_402_response` acepta `token` keyword-only, default `"USDC"`. La fila
nombraba un sitio (`response.py:131`); eran **dos**: el campo y el mensaje
generado (`f"Payment of ${amount} USDC required"`). Corregir sólo uno habría
dejado el cuerpo contradiciéndose — `token: EURC` junto a "Payment of $1 USDC
required" es peor que cualquiera de los dos errores por separado.

El literal era además redundante: `Payment402Response.token` ya trae
`default="USDC"` en `models.py:497`, así que pasarlo a mano sólo servía para que
no se pudiera cambiar.

**ROJO:** `6 failed, 1 passed` · **VERDE:** `7 passed`

Opt-in y apagado por defecto, igual que los otros dos flags de esa misma función
(`omit_unused_solana_facilitator`, `require_recipient`). `token=""` es
`ValueError`: un desafío que no nombra moneda no le da al comprador con qué
decidir qué firmar.

### 2.3 Las Known Limitations del CLAUDE.md — commit `2a92cb5`

Dos de las tres eran falsas, y el CLAUDE.md se carga en **cada sesión** de este
repo — una limitación falsa manda a un agente a arreglar lo que ya funciona.

- *"`process_payment()` … amounts still convert with the network's USDC
  decimals"* → falso. `grep -c 'token_decimals' src/uvd_x402_sdk/client.py` da
  **26 líneas**, propagado por verify / settle / process_payment, con tests para
  18 decimales, 7, el borde `0` falsy y el rechazo del negativo.
- *"SVM/Stellar/NEAR - Only USDC"* → falso para SVM. Solana trae AUSD por
  Token2022 (`solana.py:86`) con su `token_2022_program_id`.

Quedaron **tachadas y no borradas**, con el comando que las cierra, para que
nadie las vuelva a abrir.

---

## 3. Lo que NO hice, y por qué

| Fila | Por qué no |
|---|---|
| **P0** — el veredicto de reintento del writer de ERC-8004 | Vive en el árbol **sin commitear** de `Z:`. El spec dice: si el working tree está sucio hay otra sesión adentro, no pises lo que no es tuyo. `Z:` tiene 19 entradas sucias. Verificada y anotada con el bloqueo escrito |
| **P2** — `WriterUnavailableError` | Su arreglo ya existe en ese mismo árbol, con cambio de clase padre incluido. Hacerlo en `main` es fabricar un conflicto (§1.4) |
| **P1** — fase 7 de xlang, y el cable `payloadShape: v2` | Los tres archivos viven en `uvd-x402-sdk-typescript/scripts/xlang/`. `find . -name "*xlang*"` acá da vacío. El cable, además, ya está desbloqueado |
| **P1** — la divergencia `extract_payload` v2 → v1 | Decisión del dueño, no mecánica: mueve de v1 a v2 un camino con plata real (describe-net). El handoff de ayer ya la dejó medida para su criterio |
| **P2** — `asset` hardcodeado en el builder v2 | Encontrada hoy al cerrar la del v1. Es más ancha (la dirección del contrato, no una etiqueta) y toca la conversión de decimales. Anotada como fila propia en vez de metida a último momento |

---

## 4. Verificación

| Criterio | Estado |
|---|---|
| Working tree limpio antes de escribir | ✅ `git status --short` vacío al empezar. El sucio es `Z:`, otro checkout, y quedó declarado |
| Un commit por fila resuelta o cerrada, nombrándola | ✅ 5 commits, cada uno nombra su fila |
| Comando + salida en cada fila cerrada-ya-estaba | ✅ en el commit y en `docs/planning/BACKLOG.md` |
| Test que falla sin el cambio y pasa con él, corrido en los dos estados | ✅ §2.1 (4→5) y §2.2 (6→7), las dos salidas pegadas |
| Suite en verde, rojos preexistentes declarados | ✅ **869 → 881 passed, 0 skips, 0 rojos**. Base medida sobre el árbol limpio antes de tocar |
| mypy sin regresión | ✅ **110 errores preexistentes en 14 archivos, idénticos con y sin los cambios** — medido guardando y restaurando el árbol, no por inferencia |
| Escaneo de credenciales antes de pushear | ✅ `0x`+64hex / `sk-` / `AKIA` / `PRIVATE KEY` sobre cada diff: cero coincidencias. Los hashes de los tests se construyen (`"0x" + "ab" * 32`), no son literales |

**Nota de operación** (heredada y reconfirmada): la suite necesita
`PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PYTHONPATH=src python -m pytest -p pytest_asyncio.plugin`.
Sin `-p pytest_asyncio.plugin` se saltan tests en silencio; sin `PYTHONPATH=src`
se testea el paquete instalado. Quedó al pie del BACKLOG para que el próximo no
la vuelva a descubrir.

**Lección que me costó tres correcciones:** los números de línea envejecen mal.
Escribí `client.py:504`, `response.py:131` y `response.py:149` citando handoffs
y lecturas previas, y **mis propios commits los habían corrido** antes de que
terminara el barrido. Los tres los corregí midiendo de nuevo. Quedó como regla
al principio del BACKLOG: quien cite una línea, que cite también el grep que la
encuentra. Uno de esos números quedó mal en el mensaje del commit `da3623f`
(dice 442, es 453) y está anotado en la fila correspondiente.

---

## Para c0der

**Cerré 4 filas por vencidas** (las dos Known Limitations falsas del CLAUDE.md,
la publicación de 0.75.0 que ya estaba en PyPI y npm, y la mitad SVM de "only
USDC"), **arreglé 2** con test discriminante rojo→verde (`transient_503_response`
y el `token` del 402 v1), y **creé el backlog que no existía** — las filas vivían
dentro de la §5 de un handoff, que es la causa real de que nadie las barriera.

**Necesitan al dueño 5 filas:** la P0 del writer de ERC-8004 y la P2 de
`WriterUnavailableError` están las dos dentro del árbol sin commitear de
`Z:/ultravioleta/dao/uvd-x402-sdk-python` — no las toqué, y **la fila que las
desbloquea a las dos es la del checkout**, que está 5 commits detrás de `main`
con 19 archivos sucios y 1 commit sin pushear. Las dos de xlang son del repo de
TypeScript, y una de ellas (`payloadShape: v2`) **ya está desbloqueada** desde
que 0.75.0 se publicó. La quinta es la divergencia `extract_payload`, que mueve
un camino con plata real y es tu criterio, no mecánico.

**Y algo que te toca decidir a vos, fuera de las filas:** MeshRelay puede sacar
los pines de `x402_version` de turnstile y multibrain **hoy** — las dos
condiciones que el handoff de ayer ponía (Python 0.75.0, TypeScript 2.79.0) están
publicadas en PyPI y npm. Nadie lo sabe porque los handoffs quedaron escritos
antes de publicar.

**El PR queda listo para revisar y NO lo mergeé.** Suite 881 en verde, mypy sin
regresión sobre 110 preexistentes, escaneo de credenciales limpio. Hay una
observación sobre exposición en repo público que te dejé en el mensaje del
worker, fuera de este documento.
