# Cómo entra una app nueva al stack

Especificación de interop del stack, **v1**. No normativo: es el recorrido que aplica las capas 1 a 9
a una app que llega. Cada paso cierra con algo que se puede verificar.

Sin el contrato, entrar son varios registros a mano, un dialecto de webhook y un secreto compartido
por cada par de apps, un módulo cliente por vecino y una tabla de errores aprendida a golpes. Con el
contrato son una wallet, un manifiesto, el SDK, una corrida de conformidad y los cambios de allowlist,
que se siguen revisando como hasta ahora.

| # | Paso | Cómo | Cierra cuando |
|---|---|---|---|
| 0 | **¿Pertenece al stack?** | Lo decide la casa, no esta especificación | Está en el registro de alcance de la casa |
| 1 | **Wallet de servicio** | Dedicada y sin fondos, guardada en `<app>/service-signer` y nunca impresa ([R2.1](02-identidad.md), [R8.2](08-configuracion.md)). Si la app ya es partner de describe-net, es esa misma wallet | La dirección está en `identity.service_signer`; el valor no aparece en ningún lado |
| 2 | **Manifiesto** | `/.well-known/uvd-stack.json` en el host de la API, generado desde el código y enlazado desde `api-catalog`, `llms.txt` y el sitio ([capa 1](01-descubrimiento.md)) | Valida contra `schemas/manifest.schema.json` |
| 3 | **Superficies agénticas** | `llms.txt`, agent card, server card del MCP, `api-catalog`, y `/.well-known/x402` si cobra (o `payments.charges: false`) | El runner de conformidad las encuentra |
| 4 | **Cara MCP**, si tiene | Fachada sin estado sobre el REST, anotaciones, `_meta["uvd/clase"]`, `outputSchema` en las de lectura, `untrustedContentHint` en texto ajeno y el mismo auth que el REST ([capa 9](09-mcp.md)) | El runner, verde en la sección MCP |
| 5 | **Llamar a otras apps** | El cliente S2S del SDK: `User-Agent`, `traceparent`, firma ERC-8128 en escrituras y en lecturas que necesitan identidad y **nunca en una lectura pública**, límites leídos del manifiesto del otro, y reintento solo con `retryable` ([R3.1](03-autenticacion.md), [capa 7](07-observabilidad.md), [R6.5](06-errores.md)) | Cero URLs de otras apps escritas en el código; ningún GET público sale firmado |
| 6 | **Que la llamen** | Verificador ERC-8128 del SDK (o uno que pase los mismos vectores) con allowlist de direcciones públicas en un archivo revisado, que acepta solo su propia authority; su política, publicada en el manifiesto ([R3.2](03-autenticacion.md) a [R3.4](03-autenticacion.md)) | Los vectores del verificador pasan |
| 7 | **Si cobra o paga** | Vendedor del SDK con la clave persistida antes de llamar; alta en el bazar del facilitador derivada del 402 real. Pagador del SDK con `Idempotency-Key`, y un import que falla cerrado si el SDK falta ([capa 4](04-pago.md)) | Pasan los vectores de pago |
| 8 | **Si emite eventos** | Con ingreso HTTP: outbox en la misma transacción, despachador del SDK, cursor y los tipos en `events.emits`. Sin ingreso: instantánea publicada con escritura condicional ([capa 5](05-eventos.md)) | Los vectores de eventos pasan; el cursor o la instantánea responden |
| 9 | **Si califica** | ERC-8004 a través del facilitador, con el host de sus documentos en `identity.reputation_hosts` ([R2.6](02-identidad.md)) | El indexador de reputación la reconoce |
| 10 | **Altas en los proveedores** | Un cambio revisado por cada proveedor que la necesita (hoy, la allowlist de partners de describe-net), con la dirección tomada del manifiesto ([R2.7](02-identidad.md)) | Integrado y aplicado |
| 11 | **Conformidad contra lo vivo** | El runner, en solo lectura, en serie, con al menos 1 s de pausa, cortando en el primer 429 o en el tercer 401 ([R7.6](07-observabilidad.md)) | Verde, con la salida adjunta al cambio |
| 12 | **Entra al mapa** | Los registros de la casa leen su manifiesto ([R1.9](01-descubrimiento.md)); una persona decide qué se muestra | La app aparece en el mapa y en el catálogo |
