"""La guarda del cambio de riel: no volver a actuar sin PRUEBA de que no se movió plata.

Un cliente con dos caminos de liquidación que reintenta por el segundo tras fallar el
primero paga DOS VECES si el primero alcanzó a transmitir. El costo de equivocarse no es
simétrico: no cambiar de riel pierde una compra, cambiar de más la paga dos veces.
"""
from __future__ import annotations

import pytest

from uvd_x402_sdk.money_safety import SAFE_TO_FALLBACK, is_fallback_safe


def test_si_ya_se_TRANSMITIO_no_se_cambia_de_riel_JAMAS():
    """`submitted` manda sobre el texto: una autorizacion transmitida es dinero al
    portador y el que la firmo ya no controla si se liquida."""
    for marcador in SAFE_TO_FALLBACK:
        assert is_fallback_safe(submitted=True, error=marcador) is False, marcador


def test_un_error_previo_reconocido_SI_habilita_el_otro_riel():
    assert is_fallback_safe(submitted=False, error="network not supported") is True
    assert is_fallback_safe(submitted=False, error="No route for this asset") is True


def test_un_error_DESCONOCIDO_no_habilita_nada():
    """La lista blanca es el punto: lo que no se reconoce, se detiene."""
    assert is_fallback_safe(submitted=False, error="boom") is False
    assert is_fallback_safe(submitted=False, error="internal server error") is False
    assert is_fallback_safe(submitted=False, error="connection reset by peer") is False


def test_sin_error_tampoco():
    assert is_fallback_safe(submitted=False, error=None) is False
    assert is_fallback_safe(submitted=False, error="") is False


@pytest.mark.parametrize("estado", ["success", "pending", "pending_confirmation",
                                    "pending_settlement", "SUBMITTED", " Broadcast "])
def test_un_estado_VIVO_bloquea_el_fallback(estado):
    """Un pendiente no es un fallo: es una operacion que sigue viva del otro lado."""
    assert is_fallback_safe(submitted=False, error="no route", status=estado) is False


def test_marcadores_propios_del_llamador():
    """Cada capa conoce sus propios errores previos a transmitir; se suman, no se
    reemplazan — nadie puede AFLOJAR la lista blanca base pasando la suya."""
    assert is_fallback_safe(submitted=False, error="mi sdk no esta") is False
    assert is_fallback_safe(submitted=False, error="mi sdk no esta",
                            extra_safe_markers=("mi sdk no esta",)) is True
    # y aun con marcadores propios, `submitted` sigue mandando
    assert is_fallback_safe(submitted=True, error="mi sdk no esta",
                            extra_safe_markers=("mi sdk no esta",)) is False


def test_la_comparacion_no_depende_de_mayusculas():
    assert is_fallback_safe(submitted=False, error="NETWORK NOT SUPPORTED") is True
