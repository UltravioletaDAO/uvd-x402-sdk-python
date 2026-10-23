# Handoff para Master 5 (c0der): `batch-settlement`, lado comprador, en `uvd-x402-sdk` 0.100.0 (2026-10-23)

QUÉ: módulo `uvd_x402_sdk.batch_settlement` con `BatchSettlementClient`: firma el depósito (ERC-3009 al colector
canónico + primer voucher acumulado) o el voucher solo, y actualiza el canal con lo que el comprador mismo calculó.
POR QUÉ: Tenjin cobra su router (`POST https://tenjin.sh/api/x402-router/route`, US$ 0,003 por respuesta) con este
esquema y el SDK sólo lo reconocía para rechazarlo (`policy.py`); Paybox `pay_x402` tampoco lo firma. El dueño pidió
dejar a Tenjin funcionando y que a las 2 pm la flota lo use.
RIESGO: bajo en el SDK (módulo nuevo, nada existente cambia). Mueve plata sólo cuando un consumidor lo llame: el
depósito está topado por `max_deposit` (default 1 USDC) y el comprador nunca copia el `channelState` del vendedor.

## Verificación
- `tests/test_batch_settlement.py` (9): los vectores de oro (id del canal, firma del voucher, nonce ERC-3009, firma
  de la autorización del depósito) salen de la referencia oficial (`x402` 2.25.0, `x402.mechanisms.evm.batch_settlement`)
  y **coinciden byte a byte** con el módulo. La llave se deriva de una semilla en tiempo de ejecución.
- Suite completa del SDK en mi entorno: 4600 pasan; 36 fallan y 3 no recolectan en `test_dx402*`, `test_receipts`,
  `test_publish_workflow`, `test_release_tag`, `test_solana_feedback` por extras sin instalar en ese venv (ningún
  archivo de esos toca `batch_settlement`). El CI instala los extras: ahí se decide.

## Para publicar (tuyo)
`pyproject.toml` ya dice `0.100.0`; la ruta de siempre: merge a `main` con CI verde y `gh workflow run publish.yml
--ref main -f version=0.100.0`. Después, KK sube su pin y reescribe el cuerpo hook de Tenjin.

## Lo que NO está (a propósito)
Permit2, reembolso cooperativo y resincronización por 402 correctivo (lanzan `BatchSettlementError`); `PurchasePolicy`
sigue tratando el esquema como ilegible (un depósito es un compromiso mayor que la petición: el presupuesto necesita su
propia contabilidad antes de que la política lo cuente). El gemelo TypeScript va en su propio release.

## Preguntas para Master 5
1. ¿`facilitator.ultravioletadao.xyz` / x402-rs ya soporta `batch-settlement`? (no se toca desde KK; sólo para saber si
   un vendedor nuestro podría cobrar así.)
2. ¿Hay otro consumidor que ya implementó esto por su cuenta y deba subir su mejora al SDK antes (upstream-first)?
