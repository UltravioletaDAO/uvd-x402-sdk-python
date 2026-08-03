"""¿Puedo volver a actuar después de un fallo que PUDO haber movido plata?

Hay DOS preguntas distintas detrás de un pago fallido, y confundirlas cuesta un cobro
doble:

1. **¿Reintento la misma llamada?** — vive en :mod:`uvd_x402_sdk.client`
   (``_is_retryable_settle_error``). Nunca un 4xx, nunca un fallo de negocio dentro de
   un 2xx, y **nunca un 5xx cuyo cuerpo ya trae un tx hash** (el facilitador puede
   responder 5xx DESPUÉS de difundir la transacción). Para una excepción desconocida
   responde ``False``: falla cerrado.

2. **¿Me paso al OTRO riel?** — es esta función. Un cliente con dos caminos de
   liquidación (por ejemplo un firmante-difusor propio y un facilitador externo) que
   reintenta por el segundo después de fallar el primero **paga dos veces** si el
   primero alcanzó a mover la plata.

El principio es el mismo en las dos: *no vuelvas a actuar salvo que esté PROBADO que
no se movió nada*. La diferencia es la capa — (1) es transporte, (2) es enrutado.

POR QUÉ ES LISTA BLANCA
-----------------------
Se enumera lo que es SEGURO, no lo que es peligroso. Con lista negra, un error que
nadie enumeró —uno nuevo del servidor, un texto que cambió— cae en "no está prohibido"
y se reintenta. Con lista blanca cae en "no lo reconozco" y se detiene, que ante plata
es la respuesta correcta. El costo de equivocarse no es simétrico: no cambiar de riel
pierde una compra, cambiar de más la paga dos veces.

``submitted`` MANDA SOBRE EL TEXTO DEL ERROR
--------------------------------------------
Es el único hecho que no se puede racionalizar. Si la orden salió —los sobres llegaron
al firmante remoto, o la cabecera ``X-PAYMENT`` se transmitió— entonces la plata pudo
moverse **por más que el error parezca previo al pago**. Una autorización EIP-3009
transmitida es dinero al portador: quien la tenga puede liquidarla, y el que la firmó
ya no controla si eso pasa.
"""

from __future__ import annotations

from typing import Iterable, Optional

__all__ = ["SAFE_TO_FALLBACK", "is_fallback_safe"]

#: Marcadores que prueban que el fallo ocurrió ANTES de transmitir nada. Todos son
#: condiciones de descubrimiento/preparación: red no soportada, sin ruta, sin precio,
#: sin invoice 402, o la firma que ni siquiera se pudo construir.
SAFE_TO_FALLBACK: tuple[str, ...] = (
    "network not supported", "unsupported network", "no route",
    "chain not supported", "unsupported chain", "not settleable",
    "no accepts", "unsupported asset", "asset not supported",
    "could not determine a usdc price", "probe failed",
    "no payment requirements", "402 not returned",
    "no payable option", "expected a 402 invoice", "402 body was not json",
    "could not sign the authorization",
)

# Estados que significan "esto sigue vivo del otro lado". Un pendiente NO es un fallo:
# tratarlo como tal y reintentar por el otro riel es la forma más directa de pagar dos
# veces por lo mismo.
_ESTADOS_VIVOS = ("success", "pending", "pending_confirmation",
                  "pending_settlement", "submitted", "broadcast")


def is_fallback_safe(
    *,
    submitted: bool,
    error: Optional[str],
    status: Optional[str] = None,
    extra_safe_markers: Optional[Iterable[str]] = None,
) -> bool:
    """``True`` sólo cuando el riel que acaba de fallar **probadamente** no movió plata.

    Args:
        submitted: ¿la orden salió? Es AUTORITATIVO y agnóstico del riel. ``True``
            devuelve ``False`` sin mirar nada más, por más previo al pago que suene el
            error.
        error: el texto del error. Sólo se acepta si coincide con la lista blanca.
        status: estado reportado por el riel. Cualquier estado vivo (``pending*``,
            ``success``) bloquea el fallback.
        extra_safe_markers: marcadores propios del llamador, para errores de su capa
            que sabe que son previos a transmitir. Se suman a la lista blanca.

    Returns:
        ``True`` = se puede intentar por el otro riel. Ante la duda, ``False``.
    """
    if submitted:
        return False
    if status and str(status).strip().lower() in _ESTADOS_VIVOS:
        return False
    if not error:
        # Un fallo SIN error es justo el caso que no se entiende. No se cambia de riel
        # sobre algo que no se pudo leer.
        return False
    e = str(error).lower()
    marcadores = tuple(SAFE_TO_FALLBACK) + tuple(extra_safe_markers or ())
    return any(m in e for m in marcadores)
