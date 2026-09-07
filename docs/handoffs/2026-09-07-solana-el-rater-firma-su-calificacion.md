# Solana: el rater firma su propia calificación (SDK de Python, 0.81.0)

**Fecha:** 2026-09-07 · **Rama:** `0xultravioleta/sdk-feedback-solana-py` · **ws-5 del PLAN v2 «Solana a fondo»**

## En una línea

El facilitador desplegado sirve `/feedback/solana/prepare` y `/feedback/solana/submit`
desde **v1.74.0** y **ningún SDK los llamaba**. Este cambio pone el cliente en Python:
`prepare_solana_feedback()` → `sign_solana_feedback_transaction()` →
`submit_solana_feedback()`, con la firma ed25519 del rater y **sin tocar
`RELAYED_FEEDBACK_NETWORKS`**.

## Lo medido, antes de escribir una línea

| Sonda | Resultado | Hora (UTC) |
|---|---|---|
| `GET facilitator.ultravioletadao.xyz/api-docs/openapi.json` | **v2.16.0**, lista `/feedback/solana/prepare` y `/feedback/solana/submit` | 2026-09-07 23:2xZ |
| `POST /feedback/solana/prepare` (agente real, rater de ejemplo) | **200**, tx base64 de 584 bytes, 2 slots de firma **vacíos**, 11 cuentas | ídem |
| Decodificada | cuenta 0 = `F742C4Vf…EThq` (**el facilitador, fee payer**), cuenta 1 = **el rater** | ídem |
| `x402-rs` `git show 8c7fa66e:VERSION` / `8c7fa66e~1:VERSION` | `1.73.0` en los dos → el primer bump posterior (`2c811c02`) es **1.74.0**, la release que sirvió las rutas | ídem |

Esa respuesta viva quedó **fijada** en `tests/fixtures/solana-feedback-prepare.json`.
No es un ejemplo escrito a mano: es lo que contestó producción.

## Para c0der

1. **PR listo para tanda.** Rama `0xultravioleta/sdk-feedback-solana-py`, un solo commit,
   968 tests verdes (931 antes, 37 nuevos, ninguno perdido). No mergeé ni tagueé nada.
2. **Publicar es un tag, y el tag va DESPUÉS del merge a `main`.** Taguear desde la rama
   publicaría a PyPI código que no está en `main`. Cuando mergees:
   ```bash
   git checkout main && git pull
   git tag v0.81.0 && git push origin v0.81.0   # dispara .github/workflows/publish.yml
   ```
3. **Lo que NO hice y es de otro:** la prueba de punta a punta con escritura on-chain
   (`--submit`). El humo llega hasta la firma verificada y se detiene ahí a propósito:
   `submit` es una escritura que **paga el facilitador**. Correrla es decisión del dueño.
4. **Un arreglo colateral, declarado:** el changelog del README **saltaba de 0.81.0 a
   0.79.0** — 0.80.0 nunca tuvo entrada. La escribí desde el commit `cbbfd66` y el
   `CLAUDE.md` del repo. Si preferís que salga del PR, se quita en un commit.

## Paridad con el gemelo de TypeScript — **ya entró** (PR #19, 2.88.0)

c0der confirmó que el SDK de TS ya está adentro con los mismos nombres. La paridad es
un hecho medido, no una propuesta; su handoff es
`docs/handoffs/2026-09-07-feedback-solana-el-rater-firma.md` en ese repo.

| Superficie | Python (`uvd_x402_sdk`, 0.81.0) | TypeScript (2.88.0) |
|---|---|---|
| Set de redes | `SOLANA_FEEDBACK_NETWORKS` | `SOLANA_FEEDBACK_NETWORKS` |
| Ruteo | `supports_solana_feedback(network) -> bool` | `supportsSolanaFeedback(network)` |
| Paso 1 | `Erc8004Client.prepare_solana_feedback(network, agent_id, rater, value, *, value_decimals, tag1, tag2, endpoint, feedback_uri, feedback_hash, score, proof, x402_version)` | `Erc8004Client.prepareSolanaFeedback()` |
| Respuesta | `PrepareSolanaFeedbackResponse` — `success`, `transaction`, `rater`, `fee_payer`/`feePayer`, `blockhash`, `last_valid_block_height`/`lastValidBlockHeight`, `error`, `network` | ídem, en camelCase |
| Paso 2 | `sign_solana_feedback_transaction(transaction, rater, signer) -> str` | la firma del rater (su `Keypair` / wallet) |
| Firmante | `SolanaSigner` (Protocol: `pubkey: str`, `sign_message(bytes) -> bytes`) | el `Keypair` de `@solana/web3.js` ya cumple la forma |
| Firmante concreto | `Ed25519Signer(secret_key)` — semilla 32 B, clave 64 B, array de ints o base58 | — |
| Paso 3 | `Erc8004Client.submit_solana_feedback(network, agent_id, rater, value, *, transaction, ...)` | `Erc8004Client.submitSolanaFeedback()` |
| Extra de instalación | `pip install 'uvd-x402-sdk[solana]'` (solo `cryptography`) | — |

**Lo único que Python tiene de más es el paso 2 con nombre propio**
(`sign_solana_feedback_transaction` + `Ed25519Signer`), porque en Python no hay un
`Keypair` estándar de Solana en el árbol de dependencias del SDK. Es aditivo: quien
traiga su propio firmante solo necesita cumplir `SolanaSigner`.

### Las cuatro mediciones del TS, contrastadas contra las mías

| Lo que midió TS | Acá | Veredicto |
|---|---|---|
| La tx es **legacy**, primer byte `2` = dos firmas (facilitador fee payer + rater) | Idéntico: `nsig=2`, header `2/0/7`, mensaje legacy (no v0) | **Coincide** |
| **592 bytes** | **584 bytes** en mi captura | **No es contradicción**: el largo depende de los datos. Mi captura usa `tag1="quality"`, `tag2="api"`, `endpoint=""`, `feedbackUri=""`; ocho bytes de tags/endpoint distintos mueven el total. Lo que sí es invariante —y está fijado— son los 2 slots, las 11 cuentas y el orden fee payer → rater |
| `lastValidBlockHeight` es la ventana; vencida ⇒ **prepare nuevo**, retryable, `safe_to_replay=false` | Estaba a medias (decía "preparar cuando el rater esté listo"). **Agregado** al docstring del campo y al README: vencida se vuelve a llamar `prepare`, nunca se reenvía, porque el blockhash va DENTRO del mensaje firmado | **Incorporado** |
| Sin `score` el ATOM Engine ignora el feedback (`had_impact=false`) | Ya estaba en el docstring de `prepare_solana_feedback` y en el README | **Coincide** |

> Ojo con una frase que se lee al revés: el rater es la **cuenta 0 de la instrucción
> `give_feedback`** (`[signer, writable] client`), pero en el **mensaje de la
> transacción** es la cuenta **1** — la 0 es el fee payer, que es el facilitador. Las dos
> cosas son ciertas a la vez y hablan de tablas distintas. El slot de firma del rater es
> el 1, medido y fijado.

**`solana` NO entra en `RELAYED_FEEDBACK_NETWORKS` ni en el TS.** El test gemelo
`src/backend/relayed-feedback.test.ts:75` (*"is exactly the set with a verified
delegate"*) tiene que seguir verde **sin tocarlo**, igual que
`tests/test_relayed_feedback.py:56` acá.

**Vector cruzado para el gate xlang:** `tests/fixtures/solana-feedback-prepare.json`
trae `signing_vector` con `seed_phrase`, `rater`, `unsigned_transaction`,
`signed_transaction` y `rater_signature_hex`. La semilla es
`sha256("uvd-x402-sdk solana feedback rater vector v1")`, así que el TS puede derivar
la misma clave y **tiene que producir el mismo `signed_transaction` byte por byte**
(ed25519 es determinista). Si el gate xlang lo va a cubrir, copiá ese JSON al repo de
TS como se hizo con `erc8128.json`.

## Para Execution Market (ws-4)

- **`reputation_target.py:43-57 `_has_delegate`` no se toca.** Lo que cambia es el
  camino de Solana, no el de los delegates.
- El import es:
  ```python
  from uvd_x402_sdk import (
      Ed25519Signer,
      sign_solana_feedback_transaction,
      supports_solana_feedback,
  )
  ```
  y el cliente ya trae los dos métodos. Pin: `uvd-x402-sdk>=0.81.0`.
- **El rater tiene que ser el agente**, no EM: si EM firma, la cadena vuelve a anotar a
  EM. El `pubkey` que va en `rater` es el que después aparece como
  `NewFeedback.client` y el que describe.net cuenta en `distinct_raters`.
- La decisión Q3 del dueño (opción C) deja el CHECK de 8004 aceptando `solana` **ahora**
  con el facilitador como client, y este cliente es la mitad que habilita la segunda
  migración que endurece el CHECK. Nada de este PR fuerza esa migración.

## Las trampas, y por qué cada una está cerrada acá

1. **`solana` en `RELAYED_FEEDBACK_NETWORKS` rutea a la URL de EVM.** `erc8004.py`
   construye `/feedback/evm/prepare` a partir de ese frozenset, que significa "hay un
   `FeedbackDelegate` desplegado y verificado". Solana no necesita delegate: la cuenta 0
   de `give_feedback` ya está declarada `[signer] client`. Es un riel **hermano**, no una
   fila que faltaba. Fijado por test.
2. **Re-serializar el mensaje es un 400 sin pista.** `accept_rater_signed_transaction`
   compara el mensaje recibido contra el que él mismo reconstruye. Por eso
   `sign_solana_feedback_transaction()` **arrastra los bytes originales** y reescribe
   solo el arreglo de firmas. El round trip decode → encode está aserido byte-idéntico.
3. **El slot del fee payer se deja vacío.** Es del facilitador, y lo llena **después** de
   verificar la firma del rater — así una tx que la red rechazaría nunca cuesta un fee.
4. **Un mensaje versionado (v0) correría todos los offsets un byte** y la firma
   terminaría en el slot de otro: se rechaza por nombre, no se adivina.
5. **Una clave de 64 bytes pegada corta sigue parseando** y firma como otra persona: se
   compara la mitad pública contra la derivada.
6. **Firmar con la clave equivocada** da un 400 indistinguible de un blob corrupto: se
   compara `signer.pubkey` con `rater` antes de tocar la red.
7. **Sin `score` el ATOM Engine registra pero no puntúa**, y la reputación no se mueve.
   Está dicho en el docstring y en el README.

## Verificación viva, reproducible

```
$ python examples/solana_feedback_smoke.py
facilitator : https://facilitator.ultravioletadao.xyz
network     : solana
rater       : 83idx2s9r6jGqphckzTyGvkmTjWVXSpwVKndtDtp6aAN  (ephemeral, holds nothing)

fee payer   : F742C4VfFLQ9zRQyithoj5229ZgtX2WqKCSFKgH2EThq  (account 0, the facilitator)
blockhash   : BAukWMABj6PKvMPMvPBgTxQRPNy6CfAM5zWAD2vEo6y6  (valid to height 423236235)
accounts    : 11
signers     : 2
rater slot  : 1
unsigned    : 2 empty slots

[OK] message unchanged, rater slot signed, fee payer slot left empty
[OK] signature verifies over the message, as the facilitator will check

stopping before submit (on-chain write, facilitator pays the fee).
```

El rater es efímero y no tiene fondos; `prepare` no escribe nada on-chain.

## Lo que queda abierto (no es de este PR)

- **(a) del ws-5**: un `prepare → firma → submit` real con un asset nuestro. Es una
  llamada, la escribe el dueño. Cierra el criterio 2 y 3 del plan (`NewFeedback.client`
  = pubkey del agente, y `distinct_raters` de `solana` de 33 a 34).
- **(c) del ws-5**: el rail ed25519 en KarmaKadabra (`agents_sdk/rating_rail.py` es
  EVM-only a propósito). Depende de esto, ya no de nada más.
- El gemelo de TypeScript **ya está** (PR #19, 2.88.0). Lo que falta del lado cruzado es
  el vector compartido: copiar `tests/fixtures/solana-feedback-prepare.json` al repo de
  TS y hacer que el gate `scripts/xlang/` compare el `signed_transaction` byte por byte,
  como se hizo con `erc8128.json`. Ed25519 es determinista y la semilla sale de una
  frase, así que los dos runtimes tienen que producir el mismo blob.
