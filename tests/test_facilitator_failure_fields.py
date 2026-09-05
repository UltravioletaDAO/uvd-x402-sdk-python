"""El `502` que no se reintenta tiene que decir DONDE mirar.

El facilitador contesta, cuando un cobro expira sin poder confirmarse::

    502 {"error":"settlement_unconfirmed","transaction":"0x…",
         "paymentId":"0x…","retryable":false}

La transaccion PUDO haberse minado. Reintentar es gastar dos veces, y este SDK
ya se negaba a hacerlo (`_is_retryable_settle_error`, `is_transient_error`).
Pero el hash se leia para el veredicto, se logueaba en un warning y se
DESCARTABA: el llamador recibia "no reintentable" y **nada que consultar**. Es
el mismo callejon sin salida una capa mas arriba — negarse a reintentar sin
decir donde mirar deja al que pago sin forma de saber si su plata salio.

Dos mitades que caen por separado, y por eso hay un test para cada una:

1. **El cable al llamador** — `transaction`, `payment_id` y el codigo de error
   llegan a la excepcion, a su ``to_dict()`` y al dict de
   ``try_settle_payment()``.
2. **El control** — el `502` transitorio, el que trae ``Retry-After`` y ningun
   hash, sigue reintentandose exactamente como hoy. Si eso se rompe, se rompio
   el camino feliz.

Simetria con el SDK de TypeScript 2.80.0 (`FacilitatorFailureFields`): mismos
tres campos, mismo criterio de que el status es el **techo** del veredicto y el
cuerpo solo puede BAJARLO, nunca subirlo.
"""
from __future__ import annotations

import json
from decimal import Decimal

import pytest

from uvd_x402_sdk import X402Client, is_transient_error
from uvd_x402_sdk.exceptions import FacilitatorError
from uvd_x402_sdk.models import PaymentPayload

RECIPIENT = "0x1234567890123456789012345678901234567890"

# La forma exacta que pinea el facilitador (x402-rs, `SettlementUnconfirmedResponse`).
UNCONFIRMED_TX = "0x" + "ab" * 32
UNCONFIRMED_PAYMENT_ID = "0x" + "cd" * 32
UNCONFIRMED_BODY = {
    "error": "settlement_unconfirmed",
    "transaction": UNCONFIRMED_TX,
    "paymentId": UNCONFIRMED_PAYMENT_ID,
    "retryable": False,
}
# El OTRO 502 de /settle, el unico que existia hasta ahora: nada se difundio y
# el facilitador pide expresamente que se reintente.
TRANSIENT_BODY = {"error": "upstream_rpc_unavailable (ref: 7f3a)"}


def _payload(network: str = "base") -> PaymentPayload:
    return PaymentPayload(
        x402Version=1,
        scheme="exact",
        network=network,
        payload={
            "signature": "0xsig",
            "authorization": {
                "from": "0xSender",
                "to": RECIPIENT,
                "value": "10000",
                "validAfter": "0",
                "validBefore": "9999999999",
                "nonce": "0x01",
            },
        },
    )


class _FakeResponse:
    def __init__(self, status_code=200, body=None, headers=None):
        self.status_code = status_code
        self._body = body if body is not None else {}
        self.text = json.dumps(self._body)
        self.headers = headers or {}

    def json(self):
        return self._body


class _FakeHttpClient:
    """Contesta siempre lo mismo y cuenta cuantos POST recibio."""

    def __init__(self, response):
        self.response = response
        self.posts = 0

    def post(self, url, json=None, headers=None, timeout=None):
        self.posts += 1
        return self.response


def _wire(client, monkeypatch, response):
    fake = _FakeHttpClient(response)
    monkeypatch.setattr(client, "_get_http_client", lambda: fake)
    monkeypatch.setattr("uvd_x402_sdk.client.time.sleep", lambda _s: None)
    return fake


@pytest.fixture
def client():
    return X402Client(recipient_address=RECIPIENT)


def _unconfirmed_error(body=None) -> FacilitatorError:
    return FacilitatorError(
        message="Facilitator settle failed with status 502",
        status_code=502,
        response_body=json.dumps(UNCONFIRMED_BODY if body is None else body),
    )


# ---------------------------------------------------------------------------
# 1. El cable: los tres campos llegan al llamador.
# ---------------------------------------------------------------------------

class TestElHashLlegaAlLlamador:
    def test_la_excepcion_carga_los_tres_campos(self):
        exc = _unconfirmed_error()
        assert exc.transaction == UNCONFIRMED_TX
        assert exc.payment_id == UNCONFIRMED_PAYMENT_ID
        assert exc.error_code == "settlement_unconfirmed"

    def test_to_dict_los_repite_para_quien_serializa(self):
        details = _unconfirmed_error().to_dict()["details"]
        assert details["transaction"] == UNCONFIRMED_TX
        assert details["paymentId"] == UNCONFIRMED_PAYMENT_ID
        assert details["errorCode"] == "settlement_unconfirmed"
        # Y el veredicto que acompaña: parar, no reintentar.
        assert details["retryable"] is False

    def test_settle_payment_se_los_entrega_al_que_pago(self, client, monkeypatch):
        _wire(client, monkeypatch, _FakeResponse(502, UNCONFIRMED_BODY))
        with pytest.raises(FacilitatorError) as caught:
            client.settle_payment(_payload(), Decimal("0.01"), retry=True)
        exc = caught.value
        assert exc.transaction == UNCONFIRMED_TX, "sin hash no hay nada que consultar"
        assert exc.payment_id == UNCONFIRMED_PAYMENT_ID
        assert exc.error_code == "settlement_unconfirmed"

    def test_try_settle_payment_los_devuelve_como_datos(self, client, monkeypatch):
        _wire(client, monkeypatch, _FakeResponse(502, UNCONFIRMED_BODY))
        result = client.try_settle_payment(_payload(), Decimal("0.01"))
        assert result["success"] is False
        assert result["tx_hash"] == UNCONFIRMED_TX
        assert result["payment_id"] == UNCONFIRMED_PAYMENT_ID
        assert result["error_code"] == "settlement_unconfirmed"

    def test_el_hash_va_verbatim(self):
        """Algorand imprime base32 y Solana base58.

        Reformatearlo lo vuelve impegable en un explorador, y pegarlo es el
        remedio entero que ofrecemos.
        """
        algorand = "ZJ4RMKPTJGKUOX6BZQPT7VXFVDCTQCJEHNHVGF2VGKW6M4GLXQ7A"
        exc = FacilitatorError(
            "settle failed",
            status_code=502,
            response_body=json.dumps({"error": "x", "transaction": algorand}),
        )
        assert exc.transaction == algorand

    def test_un_cuerpo_sin_los_campos_no_inventa_ninguno(self):
        exc = FacilitatorError("boom", status_code=502, response_body=json.dumps(TRANSIENT_BODY))
        assert exc.transaction is None
        assert exc.payment_id is None
        assert exc.error_code == "upstream_rpc_unavailable (ref: 7f3a)"

    def test_un_cuerpo_ilegible_no_revienta(self):
        exc = FacilitatorError("boom", status_code=502, response_body="<html>502</html>")
        assert exc.transaction is None
        assert exc.payment_id is None
        assert exc.error_code is None
        assert exc.retryable is True


# ---------------------------------------------------------------------------
# 2. El control: lo que NO cambia.
# ---------------------------------------------------------------------------

class TestElTransitorioSigueIgual:
    def test_el_502_transitorio_se_sigue_reintentando(self, client, monkeypatch):
        fake = _wire(
            client, monkeypatch,
            _FakeResponse(502, TRANSIENT_BODY, headers={"retry-after": "5"}),
        )
        with pytest.raises(FacilitatorError):
            client.settle_payment(_payload(), Decimal("0.01"), retry=True)
        assert fake.posts == 3, "el camino feliz del 502 transitorio son 3 intentos"

    def test_el_502_transitorio_sigue_siendo_transitorio(self):
        exc = FacilitatorError(
            "settle failed",
            status_code=502,
            response_body=json.dumps(TRANSIENT_BODY),
            retry_after=5.0,
        )
        assert exc.retryable is True
        assert is_transient_error(exc) is True

    def test_el_no_confirmado_no_se_reintenta_jamas(self, client, monkeypatch):
        fake = _wire(client, monkeypatch, _FakeResponse(502, UNCONFIRMED_BODY))
        with pytest.raises(FacilitatorError):
            client.settle_payment(_payload(), Decimal("0.01"), retry=True)
        assert fake.posts == 1, "un settle que quiza ya se mino no se re-POSTea"


# ---------------------------------------------------------------------------
# 3. El status es el techo; el cuerpo solo BAJA.
# ---------------------------------------------------------------------------

class TestElCuerpoSoloBaja:
    def test_el_flag_explicito_se_honra_aunque_no_venga_hash(self):
        """El facilitador esta declarando un contrato; inferirlo del hash cubre
        la forma de hoy, no la que declare mañana."""
        exc = FacilitatorError(
            "settle failed",
            status_code=502,
            response_body=json.dumps({"error": "settlement_unconfirmed", "retryable": False}),
        )
        assert exc.retryable is False
        assert is_transient_error(exc) is False

    def test_el_cuerpo_nunca_sube_un_4xx_a_reintentable(self):
        exc = FacilitatorError(
            "bad request",
            status_code=400,
            response_body=json.dumps({"error": "invalid_signature", "retryable": True}),
        )
        assert exc.retryable is False
        assert is_transient_error(exc) is False

    def test_cualquier_5xx_con_hash_para_se_llame_como_se_llame(self):
        """503 — un status que SI se reintenta — donde el hash es lo unico que
        lo puede dar vuelta."""
        exc = FacilitatorError(
            "some future code",
            status_code=503,
            response_body=json.dumps(
                {"error": "un_codigo_que_no_existe_hoy", "tx_hash": UNCONFIRMED_TX}
            ),
        )
        assert exc.retryable is False
        assert is_transient_error(exc) is False

    def test_control_un_5xx_sin_hash_ni_flag_sigue_reintentable(self):
        exc = FacilitatorError(
            "rpc down",
            status_code=503,
            response_body=json.dumps({"error": "upstream_rpc_unavailable"}),
        )
        assert exc.retryable is True
        assert is_transient_error(exc) is True

    def test_el_opt_out_desarma_la_inferencia_no_el_contrato(self):
        """``anti_double_settle=False`` deja que el llamador se coma el riesgo
        de una inferencia del SDK. No lo autoriza a contradecir al facilitador."""
        inferido = FacilitatorError(
            "post-settle hook failed",
            status_code=500,
            response_body=json.dumps({"txHash": UNCONFIRMED_TX}),
        )
        assert is_transient_error(inferido, anti_double_settle=False) is True

        declarado = FacilitatorError(
            "settle failed",
            status_code=502,
            response_body=json.dumps(UNCONFIRMED_BODY),
        )
        assert is_transient_error(declarado, anti_double_settle=False) is False


# ---------------------------------------------------------------------------
# 4. Por construccion: no hay un camino que se olvide.
# ---------------------------------------------------------------------------

class TestPorConstruccion:
    def test_verify_tambien_los_entrega(self, client, monkeypatch):
        """El veredicto vive en el constructor de FacilitatorError, asi que
        TODO camino que levante una — verify, settle, escrow, events,
        ERC-8004 — llega al mismo resultado sin re-derivarlo."""
        _wire(client, monkeypatch, _FakeResponse(502, UNCONFIRMED_BODY))
        with pytest.raises(FacilitatorError) as caught:
            client.verify_payment(_payload(), Decimal("0.01"))
        assert caught.value.transaction == UNCONFIRMED_TX
        assert caught.value.payment_id == UNCONFIRMED_PAYMENT_ID
        assert caught.value.retryable is False

    def test_el_503_de_un_paywall_repite_los_campos(self):
        """Lo que el SDK le entrega al llamador tiene que sobrevivir hasta el
        que pago: ``transient_503_response`` copia ``to_dict()``, asi que los
        tres campos viajan sin que el paywall tenga que saber de ellos."""
        from uvd_x402_sdk.client import transient_503_response

        exc = FacilitatorError(
            "settle failed",
            status_code=502,
            response_body=json.dumps(TRANSIENT_BODY),
            retry_after=5.0,
        )
        body, headers = transient_503_response(exc)
        assert headers["Retry-After"] == "5"
        assert body["details"]["errorCode"] == "upstream_rpc_unavailable (ref: 7f3a)"
        assert body["details"]["retryable"] is True


class TestElHashSubeAlTopLevel:
    """La tercera mitad: el 503 que un paywall EMITE cuando el hash ya existe.

    ``transient_503_response`` se llama despues de que ``is_transient_error``
    dijo transitorio. Con el default eso nunca ocurre sobre un cuerpo con hash
    — el guard anti-doble-settle lo declara final. Pero con
    ``anti_double_settle=False`` el llamador ASUME ese riesgo y entra igual, y
    entonces el documento que sale al comprador decia ``retryable: true`` arriba
    y escondia en ``details`` la unica prueba de que el facilitador ya
    DIFUNDIO. Arriba, la senal de peligro no existia: ni ``safeToRetry``, ni el
    hash.

    Es el mismo defecto que 0.76.0 arreglo en el constructor — dos verdades
    sobre el mismo objeto y la equivocada es la barata de leer — un nivel mas
    arriba, en el JSON que cruza la red hasta el que pago.
    """

    @staticmethod
    def _con_hash(reason=None):
        from uvd_x402_sdk.client import transient_503_response

        exc = FacilitatorError(
            "settle failed",
            status_code=502,
            response_body=json.dumps(UNCONFIRMED_BODY),
            reason=reason,
            retry_after=5.0,
        )
        body, _ = transient_503_response(exc)
        return body

    def test_el_hash_viaja_en_el_top_level(self):
        """El comprador lee el top level. Si el hash solo vive en ``details``,
        reintentar es lo unico que puede hacer con lo que ve."""
        body = self._con_hash()
        assert body["transaction"] == UNCONFIRMED_TX

    def test_safe_to_retry_es_falso_por_el_hash_aunque_no_haya_reason(self):
        """``safeToRetry`` se agregaba SOLO si el facilitador mando ``reason``.
        Un ``settlement_unconfirmed`` con hash y sin ``reason`` salia sin
        ninguna marca de peligro arriba."""
        body = self._con_hash()
        assert body["safeToRetry"] is False

    def test_el_hash_gana_sobre_un_reason_que_autoriza_reintentar(self):
        """El peor de los cinco. ``holder_unknown`` esta en
        ``WRITE_NOT_ATTEMPTED_REASONS``: significa "el write nunca corrio,
        re-presenta". Con un hash en el mismo cuerpo eso es falso, y el body
        salia diciendole al comprador ``safeToRetry: true`` con la prueba de la
        difusion adentro. La evidencia dura gana sobre la etiqueta."""
        body = self._con_hash(reason="holder_unknown")
        assert body["safeToRetry"] is False

    def test_el_payment_id_tambien_sube(self):
        """Decir donde mirar es decirlo entero: el hash se busca en el
        explorador, el ``paymentId`` se busca en el facilitador."""
        body = self._con_hash()
        assert body["paymentId"] == UNCONFIRMED_PAYMENT_ID

    def test_control_el_502_sin_hash_no_cambia_en_nada(self):
        """La guarda del camino feliz: el 502 transitorio de siempre — sin
        hash — sigue saliendo exactamente como salia, sin campos nuevos."""
        from uvd_x402_sdk.client import transient_503_response

        exc = FacilitatorError(
            "settle failed",
            status_code=502,
            response_body=json.dumps(TRANSIENT_BODY),
            retry_after=5.0,
        )
        body, headers = transient_503_response(exc)
        assert body["retryable"] is True
        assert headers["Retry-After"] == "5"
        assert "transaction" not in body
        assert "paymentId" not in body
        assert "safeToRetry" not in body
