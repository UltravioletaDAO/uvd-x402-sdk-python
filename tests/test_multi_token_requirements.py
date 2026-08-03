"""Los decimales del token, y el reto v2 que un crawler sí puede leer.

Dos huecos que 402milly tuvo que tapar aguas abajo y que ahora cubre el SDK:

1. `asset=` permitía cobrar en un token distinto al USDC de la red, pero el monto
   se seguía convirtiendo con los decimales de ESE USDC. USDC son 7 decimales en
   Stellar y 18 en BSC, mientras AUSD son 6 en todas — el mismo override
   desprecia el cobro por órdenes de magnitud sin decir nada.

2. El reto v2 no podía llevar `extensions` ni un `resource` como objeto, que es
   justo lo que hace que un crawler de discovery pueda clasificar el recurso y
   llamarlo sin leer un OpenAPI aparte.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from uvd_x402_sdk import X402Config, bazaar_extension, create_402_response_v2
from uvd_x402_sdk.client import X402Client
from uvd_x402_sdk.models import PaymentPayload

EVM_RECIPIENT = "0xe4dc963c56979E0260fc146b87eE24F18220e545"
AUSD_POLYGON = "0x00000000eFE302BEAA2b3e6e1b18d08D69a9012a"


@pytest.fixture
def client() -> X402Client:
    return X402Client(recipient_address=EVM_RECIPIENT)


def _payload(network: str = "polygon") -> PaymentPayload:
    return PaymentPayload(
        x402Version=1,
        scheme="exact",
        network=network,
        payload={
            "signature": "0x" + "11" * 65,
            "authorization": {
                "from": "0x7052cA449702e5ffafbE3dc63b74C7b7d8aF402B",
                "to": EVM_RECIPIENT,
                "value": "1000000",
                "validAfter": "0",
                "validBefore": "1799999999",
                "nonce": "0x" + "ab" * 32,
            },
        },
    )


class TestTokenDecimals:
    def test_sin_override_usa_los_decimales_de_la_red(self, client: X402Client):
        """El comportamiento previo no cambia: 6 decimales en polygon."""
        req = client._build_payment_requirements(_payload(), Decimal("1.00"))
        assert req.maxAmountRequired == "1000000"

    def test_el_override_manda_sobre_los_decimales_de_la_red(self, client: X402Client):
        req = client._build_payment_requirements(
            _payload(),
            Decimal("1.00"),
            asset=AUSD_POLYGON,
            token_decimals=18,
        )
        assert req.maxAmountRequired == "1000000000000000000"
        assert req.asset == AUSD_POLYGON

    def test_stellar_usa_7_decimales_no_6(self, client: X402Client):
        """USDC en Stellar son 7 decimales. Asumir 6 cobra 10x de menos."""
        req = client._build_payment_requirements(
            _payload("stellar"), Decimal("1.00"), token_decimals=7
        )
        assert req.maxAmountRequired == "10000000"

    def test_la_conversion_se_queda_en_Decimal(self, client: X402Client):
        """float(Decimal('0.07')) es 0.070000000000000007: a 18 decimales eso
        redondea a un monto distinto del que el pagador firmó, y el facilitador
        lo rechaza."""
        req = client._build_payment_requirements(
            _payload(), Decimal("0.07"), token_decimals=18
        )
        assert req.maxAmountRequired == "70000000000000000"

    def test_decimales_negativos_fallan_ruidosamente(self, client: X402Client):
        with pytest.raises(ValueError, match="non-negative"):
            client._build_payment_requirements(
                _payload(), Decimal("1.00"), token_decimals=-1
            )

    def test_cero_decimales_es_valido_y_no_se_confunde_con_None(self, client: X402Client):
        """0 es falsy: un `if token_decimals:` lo trataría como 'sin override'."""
        req = client._build_payment_requirements(
            _payload(), Decimal("5"), token_decimals=0
        )
        assert req.maxAmountRequired == "5"


class TestRetoV2ParaDiscovery:
    @pytest.fixture
    def config(self) -> X402Config:
        return X402Config(
            recipient_evm=EVM_RECIPIENT,
            supported_networks=["base"],
            resource_url="https://402milly.xyz/purchase",
        )

    def test_cada_opcion_de_pago_lleva_scheme_y_timeout(self, config: X402Config):
        """El cliente elige UNA entrada de accepts: tiene que poder leer sus
        términos sin mirar a otro lado. Los crawlers saltan las que no los traen."""
        body = create_402_response_v2(Decimal("1.00"), config)

        assert body["accepts"], "no se generó ninguna opción de pago"
        for option in body["accepts"]:
            assert option["scheme"] == "exact"
            assert option["maxTimeoutSeconds"] == 60

    def test_el_timeout_es_configurable_por_opcion(self, config: X402Config):
        body = create_402_response_v2(
            Decimal("1.00"), config, max_timeout_seconds=300
        )
        assert all(o["maxTimeoutSeconds"] == 300 for o in body["accepts"])
        assert body["maxTimeoutSeconds"] == 300

    def test_un_resource_string_conserva_el_formato_previo(self, config: X402Config):
        body = create_402_response_v2(
            Decimal("1.00"), config, resource="/api/premium", description="Premium"
        )
        assert body["resource"] == "/api/premium"
        assert body["description"] == "Premium"
        assert body["mimeType"] == "application/json"

    def test_un_resource_dict_se_emite_como_objeto(self, config: X402Config):
        resource = {
            "url": "https://402milly.xyz/purchase",
            "description": "Buy pixels on the 402M board",
            "mimeType": "application/json",
        }
        body = create_402_response_v2(Decimal("1.00"), config, resource=resource)

        assert body["resource"] == resource
        # description/mimeType viven DENTRO del objeto, no duplicados fuera.
        assert "description" not in body
        assert "mimeType" not in body

    def test_sin_extensions_no_aparece_la_llave(self, config: X402Config):
        body = create_402_response_v2(Decimal("1.00"), config)
        assert "extensions" not in body

    def test_el_bloque_bazaar_viaja_dentro_del_reto(self, config: X402Config):
        input_schema = {
            "type": "object",
            "properties": {"x": {"type": "integer"}, "y": {"type": "integer"}},
        }
        output_example = {"success": True, "purchaseId": "abc123"}

        body = create_402_response_v2(
            Decimal("1.00"),
            config,
            extensions=bazaar_extension(input_schema, output_example),
        )

        schema = body["extensions"]["bazaar"]["schema"]["properties"]
        assert schema["input"]["properties"]["body"] == input_schema
        assert schema["output"]["properties"]["example"] == output_example
