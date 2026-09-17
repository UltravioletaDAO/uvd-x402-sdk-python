"""Arc testnet: la red entra con SEIS decimales, y el 18 se prueba en rojo.

Circle Arc paga el gas en USDC, asi que **un solo saldo** se publica dos veces:

    eth_getBalance(a)  ->  18 decimales, es el gas
    USDC.balanceOf(a)  ->   6 decimales, es el pago
    balanceOf(a) == eth_getBalance(a) // 10**12

La segunda es la que viaja en un pago x402. La primera es el numero que la
documentacion de Arc imprime al lado de "native USDC", y es el que un editor
futuro va a querer copiar al registro. **Un 18 donde va un 6 multiplica cada
cobro por 10**12**: $0.01 se firma como 10000000000000000 unidades base en vez
de 10000, y el que firmo autorizo diez mil millones de dolares.

Este archivo hace dos cosas con eso:

  1. Fija el 6 sobre los DOS caminos reales — el importe que calcula el
     vendedor (`get_token_amount`) y el `value` que firma el pagador
     (`create_authorization`). Si alguien escribe 18 en el registro, estas
     pruebas se ponen rojas.
  2. **Monta el estado malo a proposito** y mide el dano exacto, para que el
     numero quede escrito y nadie tenga que volver a deducirlo.

Las constantes de la red se midieron en vivo contra https://rpc.testnet.arc.io
el 2026-09-15 (`eth_chainId` -> 0x4cef52, `decimals()` -> 6, `name()`/`version()`
-> "USDC"/"2", `DOMAIN_SEPARATOR()` -> 0x361191...c6b0) y el separador se
recalculo localmente a partir de los cuatro campos del registro: identico. Ese
RPC contesta **403** a un User-Agent por defecto; un fallo de transporte ahi no
es una red ausente.
"""

from __future__ import annotations

import base64
import copy
import json
from decimal import Decimal
from pathlib import Path

import pytest

from uvd_x402_sdk import X402Client
from uvd_x402_sdk.config import X402Config
from uvd_x402_sdk.networks import NetworkType
from uvd_x402_sdk.networks.base import (
    SUPPORTED_NETWORKS,
    get_network,
    get_network_by_chain_id,
    get_supported_tokens,
    get_token_config,
    normalize_network,
    parse_caip2_network,
    register_network,
    to_caip2_network,
)
from uvd_x402_sdk.networks.evm import get_usdc_domain_name

# --- medido en vivo, 2026-09-15 -------------------------------------------
ARC = "arc-testnet"
ARC_CAIP2 = "eip155:5042002"
ARC_CHAIN_ID = 5042002
ARC_USDC = "0x3600000000000000000000000000000000000000"
ARC_DOMAIN_SEPARATOR = "0x361191522483d32a83e70ae7183b4b9629442c13a78bc9921d6f707911c8c6b0"

# Indice 0 del mnemonico publico de Foundry. NO esta en la lista de bloqueo de
# Arc (el indice 1 SI, y ademas tiene fondos — ver el fixture). Aca no se
# transmite nada: solo se firma para leer el `value`.
TEST_KEY = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
# Indice 1 del mismo mnemonico: esta es la cuenta que Arc siembra bloqueada.
# No se firma nada con ella; solo se deriva su direccion para probar que la
# comparacion de `test_la_direccion_bloqueada_de_genesis_esta_anotada` mide algo.
_CLAVE_DEL_INDICE_1 = (
    "0x59c6995e998f97a5a0044966f0945389dc9e86dae88c7a8412f4603b6b78690d"
)
RECIPIENT = "0x1234567890123456789012345678901234567890"

_FIXTURE = Path(__file__).parent / "fixtures" / "arc-testnet-balances.json"


def _authorized_value(chain: str, amount_usd: str) -> int:
    """El `value` que realmente se FIRMA, leido del header X-PAYMENT."""
    client = X402Client(recipient_address=RECIPIENT)
    client.connect_with_private_key(TEST_KEY, chain_name=chain)
    header = client.create_authorization(
        pay_to=RECIPIENT, amount_usd=Decimal(amount_usd), chain_name=chain
    )
    payload = json.loads(base64.b64decode(header))
    return int(payload["payload"]["authorization"]["value"])


# =============================================================================
# La red
# =============================================================================


def test_arc_testnet_esta_registrada_con_las_constantes_medidas() -> None:
    net = get_network(ARC)
    assert net is not None, "arc-testnet no esta en el registro"
    assert net.name == ARC
    assert net.display_name == "Arc Testnet"
    assert net.network_type is NetworkType.EVM
    assert net.chain_id == ARC_CHAIN_ID
    assert net.usdc_address == ARC_USDC
    assert net.usdc_domain_name == "USDC"
    assert net.usdc_domain_version == "2"
    assert net.enabled is True
    assert net.default_token == "usdc"


def test_la_busqueda_por_chain_id_devuelve_arc() -> None:
    net = get_network_by_chain_id(ARC_CHAIN_ID)
    assert net is not None and net.name == ARC


def test_el_par_nombre_caip2_va_en_los_dos_sentidos() -> None:
    assert to_caip2_network(ARC) == ARC_CAIP2
    assert parse_caip2_network(ARC_CAIP2) == ARC
    # Los dos dialectos tienen que colapsar al MISMO nombre: una politica escrita
    # en v1 cubre un 402 cotizado en v2 y al reves.
    assert normalize_network(ARC) == ARC
    assert normalize_network(ARC_CAIP2) == ARC


def test_arc_testnet_esta_en_la_lista_por_defecto_del_cliente() -> None:
    config = X402Config(recipient_evm=RECIPIENT)
    assert ARC in config.supported_networks
    # y el cliente sin configurar tampoco lo pierde
    assert ARC in X402Client(recipient_address=RECIPIENT).config.supported_networks


def test_el_dominio_eip712_de_arc_dice_usdc_y_no_usd_coin() -> None:
    """`name()` en Arc devuelve "USDC". Un "USD Coin" ahi produce una firma
    valida sobre otro dominio: el verificador la rechaza y nadie sabe por que."""
    assert get_usdc_domain_name(ARC) == "USDC"
    token = get_token_config(ARC, "usdc")
    assert token is not None
    assert token.address == ARC_USDC
    assert token.name == "USDC"
    assert token.version == "2"


def test_el_registro_reproduce_el_domain_separator_de_la_cadena() -> None:
    """Ata los CUATRO campos del dominio a un solo valor medido on-chain.

    Si alguien cambia el nombre, la version, el chain id o el contrato, este
    hash deja de coincidir: es la prueba mas barata de que el registro describe
    la cadena y no una suposicion.
    """
    keccak = pytest.importorskip("eth_utils").keccak

    net = get_network(ARC)
    assert net is not None
    typehash = keccak(
        text="EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)"
    )
    encoded = (
        typehash
        + keccak(text=net.usdc_domain_name)
        + keccak(text=net.usdc_domain_version)
        + net.chain_id.to_bytes(32, "big")
        + bytes(12)
        + bytes.fromhex(net.usdc_address[2:])
    )
    assert "0x" + keccak(encoded).hex() == ARC_DOMAIN_SEPARATOR


# =============================================================================
# La trampa: 18 vs 6
# =============================================================================


def test_el_importe_de_un_pago_en_arc_viaja_en_6_decimales() -> None:
    """Los dos caminos reales, el del vendedor y el del pagador.

    ROJO si el registro dice 18: `get_token_amount` devolveria 10**16 y la
    autorizacion firmada llevaria ese mismo numero.
    """
    net = get_network(ARC)
    assert net is not None
    assert net.usdc_decimals == 6
    assert net.tokens["usdc"].decimals == 6

    # vendedor: $0.01 -> 10000 unidades base
    assert net.get_token_amount(0.01) == 10_000
    assert net.get_token_amount(1.0) == 1_000_000
    assert net.format_token_amount(10_000) == 0.01

    # pagador: el `value` que entra en el digest EIP-712 firmado
    assert _authorized_value(ARC, "0.01") == 10_000
    assert _authorized_value(ARC_CAIP2, "0.01") == 10_000


def test_montar_18_decimales_en_arc_cobra_un_billon_de_veces_de_mas() -> None:
    """El estado malo, montado a proposito, con el dano medido.

    No es una hipotesis: se registra Arc con los 18 decimales del activo de gas
    nativo y se miden los MISMOS dos caminos. Un pago de un centavo pasa a
    autorizar diez mil millones de dolares.
    """
    bueno = get_network(ARC)
    assert bueno is not None
    correcto = bueno.get_token_amount(0.01)

    malo = copy.deepcopy(bueno)
    malo.usdc_decimals = 18  # el numero que imprime la doc de Arc
    malo.tokens["usdc"].decimals = 18
    register_network(malo)
    try:
        roto = malo.get_token_amount(0.01)
        assert roto == 10_000_000_000_000_000
        assert roto == correcto * 10**12
        # y la firma lleva el mismo disparate: el pagador autoriza 10**16
        assert _authorized_value(ARC, "0.01") == 10_000_000_000_000_000
        # en dolares: $0.01 cobrado como $10.000.000.000
        assert malo.format_token_amount(roto) == 0.01  # el vendedor ni lo ve
        assert bueno.format_token_amount(roto) == 1e10
    finally:
        register_network(bueno)

    # el registro quedo como estaba
    restaurado = get_network(ARC)
    assert restaurado is not None and restaurado.usdc_decimals == 6
    assert _authorized_value(ARC, "0.01") == 10_000


def test_los_18_nativos_quedan_documentados_y_fuera_del_camino_de_pago() -> None:
    """El 18 es un hecho real de la cadena y esta escrito, pero no lo lee nadie
    que calcule un importe."""
    net = get_network(ARC)
    assert net is not None
    assert net.extra_config["native_gas_asset"] == "USDC"
    assert net.extra_config["native_gas_decimals"] == 18
    assert net.extra_config["erc20_view_divisor"] == 10**12
    assert net.extra_config["testnet"] is True

    # el dato documentado NUNCA es el que se usa para convertir
    assert net.usdc_decimals != net.extra_config["native_gas_decimals"]
    assert 10 ** (net.extra_config["native_gas_decimals"] - net.usdc_decimals) == (
        net.extra_config["erc20_view_divisor"]
    )


def test_la_vista_erc20_es_el_piso_del_saldo_nativo_entre_1e12() -> None:
    """El 6 no sale de un documento: sale de esta medicion.

    12 direcciones de un bloque real de Arc testnet, cada una con su saldo
    nativo (18) y su saldo ERC-20 (6). La relacion es exacta en todas.
    """
    doc = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    assert doc["chain_id"] == ARC_CHAIN_ID
    assert doc["usdc_address"] == ARC_USDC
    divisor = int(doc["erc20_view_divisor"])
    assert divisor == 10**12

    filas = doc["balances"]
    assert len(filas) >= 10, "la captura perdio filas"
    for fila in filas:
        nativo = int(fila["native_wei_18"])
        erc20 = int(fila["erc20_units_6"])
        assert erc20 == nativo // divisor, fila["address"]
        # y el redondeo va hacia abajo: el resto se queda en el saldo nativo
        assert nativo - erc20 * divisor >= 0


def test_la_direccion_bloqueada_de_genesis_esta_anotada() -> None:
    """Arc siembra bloqueada la cuenta 1 del mnemonico publico de Foundry, y
    ademas la financia. Un E2E que agarre una cuenta de Anvil puede caer justo
    ahi y leer el revert como un defecto del facilitador."""
    eth_account = pytest.importorskip("eth_account")

    doc = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    bloqueada = doc["blocked_genesis_address"]
    assert bloqueada["is_blacklisted"] is True
    assert bloqueada["address"] == "0x70997970C51812dc3A010C7d01b50e0d17dc79C8"

    # La clave con la que firman estas pruebas NO es la de esa cuenta. Se
    # compara la DIRECCION DERIVADA de la clave (una clave de 64 hex nunca es
    # igual a una direccion de 40 hex: esa comparacion no puede fallar).
    firmante = eth_account.Account.from_key(TEST_KEY).address
    assert firmante.lower() != bloqueada["address"].lower()

    # y la derivacion es la que cierra el lazo: la clave del indice 1 del mismo
    # mnemonico SI da la direccion bloqueada, asi que si TEST_KEY fuera esa,
    # la asercion de arriba se pondria roja.
    assert (
        eth_account.Account.from_key(_CLAVE_DEL_INDICE_1).address.lower()
        == bloqueada["address"].lower()
    )


def test_ninguna_red_parte_sus_decimales_entre_el_default_y_su_token_config() -> None:
    """La invariante que hace generico el defecto de Arc.

    Si `usdc_decimals` y el `TokenConfig` del activo por defecto discrepan, el
    vendedor cotiza con una precision y el pagador firma con otra, y los dos
    caminos estan verdes por separado.
    """
    for nombre, net in SUPPORTED_NETWORKS.items():
        token = net.tokens.get(net.default_token)
        if token is None:
            continue
        assert token.decimals == net.usdc_decimals, nombre
        assert token.address == net.usdc_address, nombre
        assert token.name == net.usdc_domain_name, nombre
        assert token.version == net.usdc_domain_version, nombre


# =============================================================================
# Lo que NO se encendio
# =============================================================================


def test_arc_mainnet_y_testnet_tienen_identidades_distintas() -> None:
    assert get_network("arc").chain_id == 5042
    assert get_network(ARC).chain_id == 5042002
    assert get_network("arc-mainnet") is None
    assert to_caip2_network("arc") == "eip155:5042"


def test_arc_announces_eurc_in_euro_units() -> None:
    net = get_network(ARC)
    assert net is not None
    assert get_supported_tokens(ARC) == ["usdc", "eurc"]
    assert get_token_config(ARC, "eurc").usd_pegged is False


def test_arc_no_entra_en_la_tabla_de_fee_payers() -> None:
    """Las redes EVM no llevan fee payer: el facilitador firma con su relayer."""
    from uvd_x402_sdk.facilitator import get_fee_payer

    assert get_fee_payer(ARC) is None
    assert get_fee_payer(ARC_CAIP2) is None


# =============================================================================
# El cable: lo que sale al facilitador
# =============================================================================


def test_el_sobre_v2_de_arc_lleva_el_caip2_y_el_importe_en_seis() -> None:
    """La superficie observable: el cuerpo que va a `/verify`.

    El registro puede estar perfecto y el cable equivocado. Esto arma el sobre
    v2 real y comprueba las cuatro cosas que el facilitador lee: el CAIP-2, el
    contrato, el importe en unidades base de 6 decimales y el dominio EIP-712
    en `extra` (sin el, el verificador no puede resolver "USDC"/"2").
    """
    from uvd_x402_sdk.envelope import build_verify_request_for_version
    from uvd_x402_sdk.models import PaymentPayload, PaymentRequirements

    net = get_network(ARC)
    assert net is not None
    amount = str(net.get_token_amount(0.01))

    payload = PaymentPayload(
        x402Version=1,
        scheme="exact",
        network=ARC,
        payload={
            "signature": "0x" + "00" * 65,
            "authorization": {
                "from": RECIPIENT,
                "to": RECIPIENT,
                "value": amount,
                "validAfter": "0",
                "validBefore": "1",
                "nonce": "0x" + "00" * 32,
            },
        },
    )
    requirements = PaymentRequirements(
        scheme="exact",
        network=ARC,
        maxAmountRequired=amount,
        asset=ARC_USDC,
        payTo=RECIPIENT,
        resource="https://example.invalid/paid",
        description="a paid resource",
        mimeType="application/json",
        maxTimeoutSeconds=300,
        extra={"name": "USDC", "version": "2"},
    )

    body = build_verify_request_for_version(payload, requirements, 2)
    accepted = body["paymentPayload"]["accepted"]
    assert accepted["network"] == ARC_CAIP2
    assert accepted["asset"] == ARC_USDC
    assert accepted["amount"] == "10000"  # $0.01 en la vista de 6 decimales
    assert accepted["extra"] == {"name": "USDC", "version": "2"}
    assert body["paymentPayload"]["payload"]["authorization"]["value"] == "10000"


def test_el_sobre_v1_de_arc_conserva_el_nombre_plano() -> None:
    """Arc tiene forma CAIP-2, asi que no queda atrapada en v1 como XRPL; y en
    v1 el nombre que viaja sigue siendo `arc-testnet`, no el eip155."""
    from uvd_x402_sdk.envelope import build_verify_request_for_version
    from uvd_x402_sdk.models import PaymentPayload, PaymentRequirements

    payload = PaymentPayload(
        x402Version=1, scheme="exact", network=ARC, payload={"signature": "0x00"}
    )
    requirements = PaymentRequirements(
        scheme="exact",
        network=ARC,
        maxAmountRequired="10000",
        asset=ARC_USDC,
        payTo=RECIPIENT,
        resource="https://example.invalid/paid",
        description="a paid resource",
        mimeType="application/json",
        maxTimeoutSeconds=300,
        extra={"name": "USDC", "version": "2"},
    )
    body = build_verify_request_for_version(payload, requirements, 1)
    assert body["x402Version"] == 1
    assert body["paymentPayload"]["network"] == ARC
    assert body["paymentRequirements"]["network"] == ARC
