"""El 402 v1 anunciaba USDC aunque el servidor cobrara EURC.

`create_402_response` escribia `token="USDC"` como literal (`response.py:131`)
y armaba el mensaje por defecto con `f"Payment of ${amount} USDC required"`
(`response.py:117`). Un servidor que cobra EURC, AUSD, PYUSD o USDT servia un
desafio que nombra la moneda equivocada en los DOS lugares que un comprador
lee: el campo legible por maquina y la frase legible por humano.

El literal era ademas redundante — `Payment402Response.token` ya trae
`default="USDC"` (`models.py:497`), asi que pasarlo a mano solo servia para
que no se pudiera cambiar.

Sigue la disciplina de v0.70.0 de este mismo builder: **opt-in y apagado por
defecto**. Sin el parametro la salida es byte por byte la de siempre, y hay una
guarda que lo pinea. Los dos flags que ya tiene la funcion
(`omit_unused_solana_facilitator`, `require_recipient`) se agregaron asi.

NO cubre `create_402_response_v2`, que hardcodea la DIRECCION de USDC como
`asset` — es un defecto mas ancho, anotado como fila propia en
`docs/planning/BACKLOG.md`.
"""

import json

import pytest

from uvd_x402_sdk.config import X402Config
from uvd_x402_sdk.response import create_402_response

EVM_ONLY = X402Config(
    recipient_evm="0x" + "11" * 20,
    supported_networks=["base", "avalanche"],
)


class TestElTokenDelDesafio:
    def test_guarda_sin_el_parametro_la_salida_no_se_mueve(self):
        """Un consumidor con pin `>=` adopta esto sin preguntar: el default
        tiene que seguir produciendo el MISMO cuerpo. Pasa en los dos estados
        del arreglo, que es lo que la vuelve una guarda y no una prueba."""
        body = create_402_response(1, EVM_ONLY)
        assert body["token"] == "USDC"
        assert body["message"] == "Payment of $1 USDC required"

    def test_el_campo_token_nombra_la_moneda_que_se_cobra(self):
        body = create_402_response(1, EVM_ONLY, token="EURC")
        assert body["token"] == "EURC"

    def test_el_mensaje_generado_tambien_la_nombra(self):
        """El otro sitio, el que se leia menos y decia lo mismo mal. Anunciar
        `token: EURC` junto a "Payment of $1 USDC required" es peor que
        cualquiera de los dos errores por separado: el cuerpo se contradice."""
        body = create_402_response(1, EVM_ONLY, token="EURC")
        assert body["message"] == "Payment of $1 EURC required"

    def test_el_mensaje_explicito_del_llamador_sigue_ganando(self):
        """El parametro nuevo alimenta el mensaje POR DEFECTO. Un `message`
        propio no se toca."""
        body = create_402_response(1, EVM_ONLY, message="Pay up", token="EURC")
        assert body["message"] == "Pay up"
        assert body["token"] == "EURC"

    def test_la_descripcion_del_recurso_sigue_concatenando(self):
        body = create_402_response(
            1, EVM_ONLY, resource_description="the report", token="AUSD"
        )
        assert body["message"] == "Payment of $1 AUSD required for the report"

    def test_un_token_vacio_es_un_error_del_llamador_no_un_desafio_mudo(self):
        """Fail-loud: un `token=""` que pasara silenciosamente serviria un 402
        sin moneda, y el comprador no tiene con que decidir que firmar."""
        with pytest.raises(ValueError):
            create_402_response(1, EVM_ONLY, token="")

    def test_el_resto_del_cuerpo_no_se_mueve_al_cambiar_el_token(self):
        """Cambiar la moneda no puede mover recipients, chains ni facilitator."""
        base = create_402_response(1, EVM_ONLY)
        eurc = create_402_response(1, EVM_ONLY, token="EURC")
        for k in ("error", "recipient", "facilitator", "supportedChains", "amount"):
            assert json.dumps(base.get(k), sort_keys=True) == json.dumps(
                eurc.get(k), sort_keys=True
            ), k
