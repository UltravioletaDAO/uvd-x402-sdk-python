"""La orden firmada de ciclo de vida del escrow — `release` / `refundInEscrow`.

Estas dos acciones mueven plata que YA esta depositada, asi que no llevan firma
ERC-3009: no queda transferencia que autorizar. Hasta el PR #21 del facilitador
lo unico que decidia quien podia pedir el movimiento era el hecho de haber
llamado. El 2026-08-30 un tercero lo sondeo con `paymentInfo` fabricado — cinco
llamadas, dos minadas, gas gastado.

Lo que se prueba aca es el otro extremo del cable: que este SDK produzca una
orden que el facilitador acepte, y que NO produzca una que acepte de mas.

El pin duro es el digest: el type string EIP-712 esta tecleado a mano desde el
orden de campos de `lifecycle_auth.rs:73-99`, y `test_el_digest_es_el_del_
facilitador` compara ese calculo independiente contra lo que sale del SDK. Si
alguien reordena un campo de `PaymentInfo` —lo que invalida toda orden ya
emitida— este test se pone rojo antes que la produccion.
"""

from __future__ import annotations

import json
import time

import pytest

eth_account = pytest.importorskip("eth_account")
pytest.importorskip("eth_abi")

from eth_abi import encode  # noqa: E402
from eth_account import Account  # noqa: E402
from eth_account.messages import encode_typed_data  # noqa: E402
from eth_utils import keccak  # noqa: E402

from uvd_x402_sdk.escrow_signing import (  # noqa: E402
    LIFECYCLE_MAX_DEADLINE_SECS,
    LIFECYCLE_PRIMARY_TYPE,
    build_lifecycle_auth,
    build_lifecycle_typed_data,
    lifecycle_auth_from_signature,
)
from uvd_x402_sdk.wallet import EnvKeyAdapter  # noqa: E402

# Claves de PRUEBA, generadas para este archivo. Nunca tocan fondos: las
# direcciones que derivan solo aparecen dentro de estos asserts.
PAYER_KEY = "0x" + "11" * 32
RECEIVER_KEY = "0x" + "22" * 32
EXTRANO_KEY = "0x" + "33" * 32

CHAIN = 8453  # base
NOW = 1_757_000_000
AMOUNT = 1_000_000


def _payer() -> EnvKeyAdapter:
    return EnvKeyAdapter(PAYER_KEY)


def _receiver() -> EnvKeyAdapter:
    return EnvKeyAdapter(RECEIVER_KEY)


def _extrano() -> EnvKeyAdapter:
    return EnvKeyAdapter(EXTRANO_KEY)


def _payment_info(authorization_expiry: int = NOW + 7200) -> dict:
    """paymentInfo en formato de wire — camelCase, `salt` en hex."""
    return {
        "operator": "0x271f9fa7f8907aCf178CCFB470076D9129D8F0Eb",
        "receiver": _receiver().get_address(),
        "token": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
        "maxAmount": "1000000",
        "preApprovalExpiry": NOW + 3600,
        "authorizationExpiry": authorization_expiry,
        "refundExpiry": NOW + 30 * 86400,
        "minFeeBps": 0,
        "maxFeeBps": 1300,
        "feeReceiver": "0xaE07cEB6b395BC685a776a0b4c489E8d9cE9A6ad",
        "salt": "0x" + "00" * 30 + "3039",
    }


def _digest(typed: dict) -> bytes:
    signable = encode_typed_data(
        domain_data=typed["domain"],
        message_types=typed["types"],
        message_data=typed["message"],
    )
    return keccak(b"\x19" + signable.version + signable.header + signable.body)


def _recover(typed: dict, signature: str) -> str:
    signable = encode_typed_data(
        domain_data=typed["domain"],
        message_types=typed["types"],
        message_data=typed["message"],
    )
    return Account.recover_message(signable, signature=bytes.fromhex(signature[2:]))


# ---------------------------------------------------------------------------
# El digest, contra un calculo independiente del `.rs`
# ---------------------------------------------------------------------------

# Type strings LITERALES, tecleados desde lifecycle_auth.rs:73-99. No se derivan
# de `LIFECYCLE_ORDER_TYPES`: si vinieran de ahi el test no probaria nada, solo
# que el SDK coincide consigo mismo.
_PI_TYPE = (
    b"PaymentInfo(address operator,address payer,address receiver,"
    b"address token,uint120 maxAmount,uint48 preApprovalExpiry,"
    b"uint48 authorizationExpiry,uint48 refundExpiry,uint16 minFeeBps,"
    b"uint16 maxFeeBps,address feeReceiver,uint256 salt)"
)
_ORDER_TYPE = (
    b"LifecycleOrder(string action,uint256 amount,uint256 deadline,"
    b"bytes32 nonce,PaymentInfo paymentInfo)" + _PI_TYPE
)
_DOMAIN_TYPE = b"EIP712Domain(string name,string version,uint256 chainId)"


def _digest_a_mano(action: str, pi: dict, payer: str, amount: int, deadline: int,
                   nonce: bytes, chain_id: int) -> bytes:
    pi_hash = keccak(
        encode(
            ["bytes32", "address", "address", "address", "address", "uint120",
             "uint48", "uint48", "uint48", "uint16", "uint16", "address",
             "uint256"],
            [keccak(_PI_TYPE), pi["operator"], payer, pi["receiver"], pi["token"],
             int(pi["maxAmount"]), pi["preApprovalExpiry"],
             pi["authorizationExpiry"], pi["refundExpiry"], pi["minFeeBps"],
             pi["maxFeeBps"], pi["feeReceiver"], int(pi["salt"], 16)],
        )
    )
    struct_hash = keccak(
        encode(
            ["bytes32", "bytes32", "uint256", "uint256", "bytes32", "bytes32"],
            [keccak(_ORDER_TYPE), keccak(action.encode()), amount, deadline,
             nonce, pi_hash],
        )
    )
    domain_sep = keccak(
        encode(
            ["bytes32", "bytes32", "bytes32", "uint256"],
            [keccak(_DOMAIN_TYPE), keccak(b"x402 escrow lifecycle"), keccak(b"1"),
             chain_id],
        )
    )
    return keccak(b"\x19\x01" + domain_sep + struct_hash)


def test_el_digest_es_el_del_facilitador():
    """El EIP-712 que firma el SDK es el que arma `lifecycle_auth.rs`.

    Reordenar un campo de `PaymentInfo` invalida toda orden ya emitida. Este es
    el unico test que lo caza sin salir a la red.
    """
    pi = _payment_info()
    nonce = bytes.fromhex("01" * 32)
    typed = build_lifecycle_typed_data(
        action="release", payment_info=pi, payer=_payer().get_address(),
        amount=AMOUNT, chain_id=CHAIN, deadline=NOW + 600, nonce=nonce,
    )
    assert _digest(typed) == _digest_a_mano(
        "release", pi, _payer().get_address(), AMOUNT, NOW + 600, nonce, CHAIN
    )


# ---------------------------------------------------------------------------
# 1. Firma valida
# ---------------------------------------------------------------------------


def test_firma_valida_recupera_al_firmante_declarado():
    """`Verdict::Ok`: la firma recupera exactamente al `signer` declarado.

    Es la primera cosa que el facilitador comprueba
    (`pre_evaluate`, lifecycle_auth.rs:381-384): recuperar algo distinto de
    `auth.signer` es `bad_signature`, no "otro firmante".
    """
    pi = _payment_info()
    auth = build_lifecycle_auth(
        action="release", payment_info=pi, payer=_payer().get_address(),
        amount=AMOUNT, chain_id=CHAIN, wallet=_payer(), now=NOW,
    )

    assert auth["signer"] == _payer().get_address()
    assert set(auth) == {"signer", "deadline", "nonce", "signature"}
    assert auth["deadline"] == NOW + 600
    assert len(bytes.fromhex(auth["nonce"][2:])) == 32
    assert len(bytes.fromhex(auth["signature"][2:])) == 65

    typed = build_lifecycle_typed_data(
        action="release", payment_info=pi, payer=_payer().get_address(),
        amount=AMOUNT, chain_id=CHAIN, deadline=auth["deadline"],
        nonce=auth["nonce"],
    )
    assert _recover(typed, auth["signature"]) == auth["signer"]


def test_el_receiver_firma_el_refund_y_el_payer_el_release():
    """Los dos caminos que el facilitador admite sin leer la cadena.

    `local_role` (lifecycle_auth.rs:349-364): para `release` solo el payer;
    para `refundInEscrow` el receiver, o el payer pasado
    `authorizationExpiry`.
    """
    pi = _payment_info()
    refund = build_lifecycle_auth(
        action="refundInEscrow", payment_info=pi, payer=_payer().get_address(),
        amount=AMOUNT, chain_id=CHAIN, wallet=_receiver(), now=NOW,
    )
    assert refund["signer"] == pi["receiver"]

    # El payer refundeando DESPUES del vencimiento: la cadena ya le deja
    # `reclaim()`, asi que el facilitador tambien.
    vencido = _payment_info(authorization_expiry=NOW - 1)
    tarde = build_lifecycle_auth(
        action="refundInEscrow", payment_info=vencido,
        payer=_payer().get_address(), amount=AMOUNT, chain_id=CHAIN,
        wallet=_payer(), now=NOW,
    )
    assert tarde["signer"] == _payer().get_address()


# ---------------------------------------------------------------------------
# 2. Firmante no autorizado
# ---------------------------------------------------------------------------


def test_un_firmante_extrano_queda_a_la_vista_en_el_signer():
    """`Verdict::UnauthorizedRole`: la firma es real, el derecho no.

    El SDK NO puede rechazar esto por su cuenta —el dueno del operador se lee
    on-chain (`FEE_RECIPIENT()`) y aca no hay cadena—, pero la orden que emite
    tiene que declarar honestamente QUIEN firmo. Un `signer` que no es ni el
    payer ni el receiver es exactamente lo que el facilitador manda al camino
    `NeedsOwner` y termina rechazando.
    """
    pi = _payment_info()
    auth = build_lifecycle_auth(
        action="release", payment_info=pi, payer=_payer().get_address(),
        amount=AMOUNT, chain_id=CHAIN, wallet=_extrano(), now=NOW,
    )

    extrano = _extrano().get_address()
    assert auth["signer"] == extrano
    assert extrano != _payer().get_address()
    assert extrano != pi["receiver"]

    # Y la firma es de el, no de otro: no hay forma de hacerla pasar por la
    # del payer.
    typed = build_lifecycle_typed_data(
        action="release", payment_info=pi, payer=_payer().get_address(),
        amount=AMOUNT, chain_id=CHAIN, deadline=auth["deadline"],
        nonce=auth["nonce"],
    )
    assert _recover(typed, auth["signature"]) == extrano


# ---------------------------------------------------------------------------
# 3. Deadline
# ---------------------------------------------------------------------------


def test_una_deadline_vencida_no_se_llega_a_firmar():
    """`Verdict::Expired`, pero atajado ACA.

    Firmar una orden muerta y mandarla es gastar un viaje para que el
    facilitador la tire; en `enforce` es plata atascada y el llamador no tiene
    el log para entender por que.
    """
    with pytest.raises(ValueError, match="ya paso"):
        build_lifecycle_auth(
            action="release", payment_info=_payment_info(),
            payer=_payer().get_address(), amount=AMOUNT, chain_id=CHAIN,
            wallet=_payer(), deadline=NOW - 1, now=NOW,
        )


def test_una_deadline_mas_alla_del_techo_tampoco():
    """`Verdict::DeadlineTooFar`: el techo del facilitador es 900 s.

    Una orden filtrada no puede ser un permiso permanente
    (`DEFAULT_MAX_DEADLINE_SECS`, lifecycle_auth.rs:64).
    """
    with pytest.raises(ValueError, match="deadline_too_far"):
        build_lifecycle_auth(
            action="release", payment_info=_payment_info(),
            payer=_payer().get_address(), amount=AMOUNT, chain_id=CHAIN,
            wallet=_payer(),
            deadline=NOW + LIFECYCLE_MAX_DEADLINE_SECS + 1, now=NOW,
        )

    # El borde exacto SI se firma: 900 s es el techo, no el primer valor de mas.
    ok = build_lifecycle_auth(
        action="release", payment_info=_payment_info(),
        payer=_payer().get_address(), amount=AMOUNT, chain_id=CHAIN,
        wallet=_payer(), deadline=NOW + LIFECYCLE_MAX_DEADLINE_SECS, now=NOW,
    )
    assert ok["deadline"] == NOW + LIFECYCLE_MAX_DEADLINE_SECS


def test_el_default_deja_colchon_contra_el_techo():
    """Firmar los 900 exactos por default seria un rechazo por reloj ajeno.

    El facilitador compara contra SU reloj: cinco segundos de atraso convierten
    una orden de 900 s en `deadline_too_far`. El default de 600 s es lo que
    hace que la diferencia de relojes no decida el veredicto.
    """
    auth = build_lifecycle_auth(
        action="release", payment_info=_payment_info(),
        payer=_payer().get_address(), amount=AMOUNT, chain_id=CHAIN,
        wallet=_payer(), now=NOW,
    )
    margen = (NOW + LIFECYCLE_MAX_DEADLINE_SECS) - auth["deadline"]
    assert margen >= 120, f"solo {margen}s de colchon contra el techo"


# ---------------------------------------------------------------------------
# 4. Nonce
# ---------------------------------------------------------------------------


def test_cada_orden_trae_un_nonce_distinto():
    """`Verdict::Replayed`: el facilitador consume el nonce al aceptar.

    Dos ordenes con el mismo nonce = la segunda se descarta. Los settles
    parciales de un stream emiten una orden por delta, asi que repetir el nonce
    no es un caso raro: es el caso normal si el default no es aleatorio.
    """
    pi = _payment_info()
    nonces = {
        build_lifecycle_auth(
            action="release", payment_info=pi, payer=_payer().get_address(),
            amount=AMOUNT, chain_id=CHAIN, wallet=_payer(), now=NOW,
        )["nonce"]
        for _ in range(20)
    }
    assert len(nonces) == 20


def test_un_nonce_repetido_a_proposito_da_la_misma_firma():
    """Reusar el nonce produce una orden BYTE-IDENTICA: nada nuevo que mandar.

    Es lo que hace que el `replayed` del facilitador sea diagnosticable desde
    aca: si dos llamadas dan la misma firma, el reenvio no es un pedido
    distinto, es el mismo otra vez.
    """
    pi = _payment_info()
    fijo = "0x" + "ab" * 32
    kwargs = dict(
        action="release", payment_info=pi, payer=_payer().get_address(),
        amount=AMOUNT, chain_id=CHAIN, wallet=_payer(), deadline=NOW + 600,
        nonce=fijo, now=NOW,
    )
    primera = build_lifecycle_auth(**kwargs)
    segunda = build_lifecycle_auth(**kwargs)
    assert primera == segunda
    assert primera["nonce"] == fijo


def test_un_nonce_que_no_mide_32_bytes_se_rechaza():
    with pytest.raises(ValueError, match="32 bytes"):
        build_lifecycle_auth(
            action="release", payment_info=_payment_info(),
            payer=_payer().get_address(), amount=AMOUNT, chain_id=CHAIN,
            wallet=_payer(), nonce="0xdeadbeef", now=NOW,
        )


# ---------------------------------------------------------------------------
# 5. PaymentInfo manoseado
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "campo, valor",
    [
        ("receiver", "0x000000000000000000000000000000000000dEaD"),
        ("operator", "0x000000000000000000000000000000000000bEEF"),
        ("token", "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"),
        ("maxAmount", "999999999"),
        ("authorizationExpiry", NOW + 999999),
        ("maxFeeBps", 1800),
        ("salt", "0x" + "00" * 30 + "beef"),
    ],
)
def test_cambiar_un_campo_del_paymentInfo_rompe_la_firma(campo, valor):
    """La orden se firma sobre el paymentInfo que se envia. Si viajan
    distintos, la firma no verifica.

    Es la propiedad que hace que no exista un `getHash` que recomputar en
    ninguna de las dos puntas — y la que impide que un intermediario cambie el
    receiver despues de firmada.
    """
    pi = _payment_info()
    auth = build_lifecycle_auth(
        action="release", payment_info=pi, payer=_payer().get_address(),
        amount=AMOUNT, chain_id=CHAIN, wallet=_payer(), now=NOW,
    )

    manoseado = dict(pi)
    manoseado[campo] = valor
    assert manoseado[campo] != pi[campo], "el caso de prueba no cambio nada"

    typed = build_lifecycle_typed_data(
        action="release", payment_info=manoseado, payer=_payer().get_address(),
        amount=AMOUNT, chain_id=CHAIN, deadline=auth["deadline"],
        nonce=auth["nonce"],
    )
    assert _recover(typed, auth["signature"]) != auth["signer"]


def test_cambiar_la_accion_el_monto_o_la_cadena_rompe_la_firma():
    """El digest ata accion, monto y chainId, no solo el paymentInfo.

    Sin esto una orden de `refundInEscrow` valdria como `release` —el reverso
    exacto del movimiento de plata— y una orden de Base se podria replicar en
    Polygon.
    """
    pi = _payment_info()
    base = dict(
        payment_info=pi, payer=_payer().get_address(), amount=AMOUNT,
        chain_id=CHAIN, deadline=NOW + 600, nonce="0x" + "cd" * 32,
    )
    auth = build_lifecycle_auth(
        action="release", wallet=_payer(), now=NOW, **base
    )

    for cambio in (
        {"action": "refundInEscrow"},
        {"action": "release", "amount": AMOUNT + 1},
        {"action": "release", "chain_id": 137},
    ):
        kwargs = {**base, "action": "release", **cambio}
        typed = build_lifecycle_typed_data(**kwargs)
        assert _recover(typed, auth["signature"]) != auth["signer"], (
            f"{cambio} no cambio el digest"
        )


def test_un_paymentInfo_incompleto_no_se_firma_por_default():
    """Completar un campo faltante firmaria un struct distinto del que viaja.

    El rechazo resultante seria `bad_signature`, que no nombra el campo. Falla
    aca, donde si se puede nombrar.
    """
    incompleto = _payment_info()
    del incompleto["feeReceiver"]
    with pytest.raises(ValueError, match="feeReceiver"):
        build_lifecycle_auth(
            action="release", payment_info=incompleto,
            payer=_payer().get_address(), amount=AMOUNT, chain_id=CHAIN,
            wallet=_payer(), now=NOW,
        )


def test_una_accion_desconocida_no_se_firma():
    with pytest.raises(ValueError, match="desconocida"):
        build_lifecycle_auth(
            action="capture", payment_info=_payment_info(),
            payer=_payer().get_address(), amount=AMOUNT, chain_id=CHAIN,
            wallet=_payer(), now=NOW,
        )


def test_el_salt_entra_por_su_valor_entero_no_por_su_hex():
    """`salt` es bytes32 en el wire y uint256 en la firma.

    El facilitador lo convierte con `U256::from_be_bytes` (types.rs:288).
    Firmar el hex tal cual produce otro digest y un `bad_signature` mudo — el
    unico sintoma seria que ninguna orden verifica jamas.

    Lo que se fija es el VALOR, no el tipo de Python: desde 0.80.0 los uint del
    mensaje van como string decimal para que el documento sobreviva el viaje
    por JSON al navegador (ver `_uint_a_str`). El digest es el mismo — lo
    comprueba `test_primaryType_no_movio_la_firma_del_vector_fijado`.
    """
    pi = _payment_info()
    typed = build_lifecycle_typed_data(
        action="release", payment_info=pi, payer=_payer().get_address(),
        amount=AMOUNT, chain_id=CHAIN, deadline=NOW + 600,
        nonce="0x" + "01" * 32,
    )
    assert int(typed["message"]["paymentInfo"]["salt"]) == 0x3039
    assert typed["message"]["paymentInfo"]["salt"] == "12345"


# ---------------------------------------------------------------------------
# Compatibilidad hacia atras: sin firmante, el pedido sale como antes
# ---------------------------------------------------------------------------


class _FakeResponse:
    status_code = 200

    def __init__(self, body: dict) -> None:
        self._body = body

    def json(self) -> dict:
        return self._body


def _cliente_falso(monkeypatch, capturado: list):
    """Un AdvancedEscrowClient sin cadena ni red: solo la construccion del
    payload, que es lo unico que este cambio toca."""
    web3 = pytest.importorskip("web3")  # noqa: F841
    from uvd_x402_sdk import advanced_escrow as ae

    cliente = object.__new__(ae.AdvancedEscrowClient)
    cliente.payer = _payer().get_address()
    cliente.chain_id = CHAIN
    cliente.facilitator_url = "https://facilitator.invalid"
    cliente.contracts = {
        "escrow": "0xBdEA0D1bcC5966192B070Fdf62aB4EF5b4420cff",
        "operator": "0x271f9fa7f8907aCf178CCFB470076D9129D8F0Eb",
        "token_collector": "0x2f8EB6D5eA9c2b1e0a3bF1B4f0d43dAE9CF7BdD1",
    }

    def _post(url, json=None, timeout=None):
        capturado.append(json)
        return _FakeResponse({"success": True, "transaction": "0x" + "ff" * 32})

    monkeypatch.setattr(ae.httpx, "post", _post)
    return cliente


def _payment_info_dataclass():
    from uvd_x402_sdk.advanced_escrow import PaymentInfo

    return PaymentInfo(
        operator="0x271f9fa7f8907aCf178CCFB470076D9129D8F0Eb",
        receiver=_receiver().get_address(),
        token="0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
        max_amount=AMOUNT,
        pre_approval_expiry=NOW + 3600,
        authorization_expiry=NOW + 7200,
        refund_expiry=NOW + 30 * 86400,
        min_fee_bps=0,
        max_fee_bps=1300,
        fee_receiver="0xaE07cEB6b395BC685a776a0b4c489E8d9cE9A6ad",
        salt="0x" + "00" * 30 + "3039",
    )


def test_sin_firmante_el_payload_no_trae_lifecycleAuth(monkeypatch):
    """Compatibilidad: el facilitador en `off`/`log` no rechaza, y todo llamador
    que existe hoy sigue funcionando sin tocar una linea."""
    capturado: list = []
    cliente = _cliente_falso(monkeypatch, capturado)

    resultado = cliente.release_via_facilitator(_payment_info_dataclass())

    assert resultado.success is True
    assert "lifecycleAuth" not in capturado[0]["payload"]
    assert set(capturado[0]["payload"]) == {"paymentInfo", "payer", "amount"}


def test_con_firmante_el_payload_trae_la_orden_sobre_el_mismo_paymentInfo(
    monkeypatch,
):
    """Y firma el paymentInfo EXACTO que viaja — no una copia recomputada."""
    capturado: list = []
    cliente = _cliente_falso(monkeypatch, capturado)

    resultado = cliente.release_via_facilitator(
        _payment_info_dataclass(), lifecycle_signer=_payer()
    )

    assert resultado.success is True
    enviado = capturado[0]["payload"]
    auth = enviado["lifecycleAuth"]
    assert auth["signer"] == _payer().get_address()

    typed = build_lifecycle_typed_data(
        action="release", payment_info=enviado["paymentInfo"],
        payer=enviado["payer"], amount=int(enviado["amount"]),
        chain_id=CHAIN, deadline=auth["deadline"], nonce=auth["nonce"],
    )
    assert _recover(typed, auth["signature"]) == auth["signer"]


def test_el_monto_firmado_es_el_monto_enviado(monkeypatch):
    """`amount` entra al digest. Firmar el max_amount y enviar un parcial es
    una orden que no verifica — y es el caso normal de un stream."""
    capturado: list = []
    cliente = _cliente_falso(monkeypatch, capturado)

    parcial = AMOUNT // 4
    cliente.release_via_facilitator(
        _payment_info_dataclass(), amount=parcial, lifecycle_signer=_payer()
    )

    enviado = capturado[0]["payload"]
    assert enviado["amount"] == str(parcial)
    auth = enviado["lifecycleAuth"]
    typed = build_lifecycle_typed_data(
        action="release", payment_info=enviado["paymentInfo"],
        payer=enviado["payer"], amount=parcial, chain_id=CHAIN,
        deadline=auth["deadline"], nonce=auth["nonce"],
    )
    assert _recover(typed, auth["signature"]) == auth["signer"]


def test_el_refund_firma_la_accion_refundInEscrow(monkeypatch):
    """La accion del digest sigue a la del pedido, no al reves."""
    capturado: list = []
    cliente = _cliente_falso(monkeypatch, capturado)

    cliente.refund_via_facilitator(
        _payment_info_dataclass(), lifecycle_signer=_receiver()
    )

    enviado = capturado[0]["payload"]
    assert capturado[0]["action"] == "refundInEscrow"
    auth = enviado["lifecycleAuth"]
    typed = build_lifecycle_typed_data(
        action="refundInEscrow", payment_info=enviado["paymentInfo"],
        payer=enviado["payer"], amount=int(enviado["amount"]), chain_id=CHAIN,
        deadline=auth["deadline"], nonce=auth["nonce"],
    )
    assert _recover(typed, auth["signature"]) == _receiver().get_address()


def test_el_deadline_por_default_usa_el_reloj_real():
    """Sin `now`, la orden se ata al reloj — no a una constante de test."""
    antes = int(time.time())
    auth = build_lifecycle_auth(
        action="release", payment_info=_payment_info(),
        payer=_payer().get_address(), amount=AMOUNT, chain_id=CHAIN,
        wallet=_payer(),
    )
    despues = int(time.time())
    assert antes + 600 <= auth["deadline"] <= despues + 600


# ---------------------------------------------------------------------------
# Vector fijado para el gemelo TypeScript
# ---------------------------------------------------------------------------


def test_vector_fijado_para_el_gemelo_typescript():
    """El vector que el SDK de TypeScript tiene que reproducir byte a byte.

    Esta escrito en `docs/handoffs/2026-09-05-lifecycle-auth-firma.md` para que
    el gemelo lo persiga. Un vector que vive solo en un documento deriva en
    silencio del codigo que lo genero, y el que lo persigue no se entera: por
    eso esta clavado aca tambien.

    Los numeros salen de esta implementacion, no del `.rs` — lo que ata esta
    implementacion al facilitador es `test_el_digest_es_el_del_facilitador`,
    que teclea el type string a mano. Este otro fija el resultado para que las
    dos puntas comparen contra lo MISMO.
    """
    clave = "0x" + "11" * 32
    wallet = EnvKeyAdapter(clave)
    pi = {
        "operator": "0x271f9fa7f8907aCf178CCFB470076D9129D8F0Eb",
        "receiver": "0x2222222222222222222222222222222222222222",
        "token": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
        "maxAmount": "1000000",
        "preApprovalExpiry": 1757003600,
        "authorizationExpiry": 1757007200,
        "refundExpiry": 1759592000,
        "minFeeBps": 0,
        "maxFeeBps": 1300,
        "feeReceiver": "0xaE07cEB6b395BC685a776a0b4c489E8d9cE9A6ad",
        "salt": "0x0000000000000000000000000000000000000000000000000000000000003039",
    }
    assert wallet.get_address() == "0x19E7E376E7C213B7E7e7e46cc70A5dD086DAff2A"

    typed = build_lifecycle_typed_data(
        action="release", payment_info=pi, payer=wallet.get_address(),
        amount=1_000_000, chain_id=8453, deadline=1757000600,
        nonce="0x" + "01" * 32,
    )
    assert typed["domain"] == {
        "name": "x402 escrow lifecycle",
        "version": "1",
        "chainId": 8453,
    }
    # El salt entra por su valor 12345, no por su hex. Va como string decimal
    # (0.80.0) para sobrevivir el JSON; el digest de abajo no se movio.
    assert typed["message"]["paymentInfo"]["salt"] == "12345"

    assert _digest(typed).hex() == (
        "3dbd8a90a80785131a198a685f9f7400b1bf9a48d998e3aa1853abae56921918"
    )

    auth = build_lifecycle_auth(
        action="release", payment_info=pi, payer=wallet.get_address(),
        amount=1_000_000, chain_id=8453, wallet=wallet,
        deadline=1757000600, nonce="0x" + "01" * 32, now=1757000000,
    )
    assert auth == {
        "signer": "0x19E7E376E7C213B7E7e7e46cc70A5dD086DAff2A",
        "deadline": 1757000600,
        "nonce": "0x0101010101010101010101010101010101010101010101010101010101010101",
        "signature": (
            "0x78fe143886ee329e235cd7735e948f50ecd2ef32b20a4c88c46767cdd63b33ca"
            "77553f16c73adacf754e34b2cc988137fd77b843649d990c9a1a5001b28a13a71c"
        ),
    }


# ---------------------------------------------------------------------------
# Orden firmada AFUERA - el payer firma en un navegador y este proceso
# solo transporta (0.79.0)
# ---------------------------------------------------------------------------
#
# El dueno decidio que la orden la firma el PAYER, y que quien pide el
# movimiento (Execution Market) la transporta. Ese proceso no tiene -ni debe
# tener- la llave del payer: recibe 65 bytes de un navegador. Hasta 0.78.0 el
# unico camino era `lifecycle_signer=`, que exige la llave en el proceso; una
# orden ajena no tenia por donde entrar.


def _firma_ajena(typed: dict, wallet: EnvKeyAdapter) -> str:
    """Los 65 bytes tal como los devolveria un navegador: solo la firma."""
    return wallet.sign_typed_data(typed)["signature"]


def test_una_orden_firmada_afuera_arma_el_mismo_bloque_que_si_la_firmaramos():
    """Paridad interna: los dos caminos producen el MISMO wire.

    Es lo que hace que el camino de navegador no sea un dialecto aparte. Si
    divergieran en una clave, el facilitador veria dos formatos y solo uno
    estaria cubierto por los vectores fijados.
    """
    pi = _payment_info()
    nonce = "0x" + "ab" * 32
    deadline = NOW + 600

    propia = build_lifecycle_auth(
        action="release", payment_info=pi, payer=_payer().get_address(),
        amount=AMOUNT, chain_id=CHAIN, wallet=_payer(), deadline=deadline,
        nonce=nonce, now=NOW,
    )

    typed = build_lifecycle_typed_data(
        action="release", payment_info=pi, payer=_payer().get_address(),
        amount=AMOUNT, chain_id=CHAIN, deadline=deadline, nonce=nonce,
    )
    ajena = lifecycle_auth_from_signature(
        typed_data=typed,
        signature=_firma_ajena(typed, _payer()),
        signer=_payer().get_address(),
    )

    assert ajena == propia


def test_el_deadline_y_el_nonce_salen_del_typed_data_firmado():
    """No se pasan aparte: entran al digest, asi que declararlos por separado
    dejaria el wire diciendo una ventana y la firma cubriendo otra."""
    pi = _payment_info()
    typed = build_lifecycle_typed_data(
        action="refundInEscrow", payment_info=pi, payer=_payer().get_address(),
        amount=AMOUNT, chain_id=CHAIN, deadline=NOW + 42,
        nonce=bytes.fromhex("7f" * 32),
    )
    auth = lifecycle_auth_from_signature(
        typed_data=typed,
        signature=_firma_ajena(typed, _receiver()),
        signer=_receiver().get_address(),
    )
    assert auth["deadline"] == NOW + 42
    assert auth["nonce"] == "0x" + "7f" * 32
    assert auth["signer"] == _receiver().get_address()


def test_una_firma_que_recupera_a_otro_no_se_transporta():
    """`bad_signature` remoto no dice cual de los dos estaba mal. Aca si.

    Es el error real del camino de navegador: la pagina firma con la cuenta
    que tiene conectada y el backend cree que es otra.
    """
    pi = _payment_info()
    typed = build_lifecycle_typed_data(
        action="release", payment_info=pi, payer=_payer().get_address(),
        amount=AMOUNT, chain_id=CHAIN, deadline=NOW + 600, nonce="0x" + "01" * 32,
    )
    with pytest.raises(ValueError, match="bad_signature"):
        lifecycle_auth_from_signature(
            typed_data=typed,
            signature=_firma_ajena(typed, _extrano()),
            signer=_payer().get_address(),
        )


def test_una_firma_de_largo_equivocado_no_se_transporta():
    pi = _payment_info()
    typed = build_lifecycle_typed_data(
        action="release", payment_info=pi, payer=_payer().get_address(),
        amount=AMOUNT, chain_id=CHAIN, deadline=NOW + 600, nonce="0x" + "01" * 32,
    )
    with pytest.raises(ValueError, match="65 bytes"):
        lifecycle_auth_from_signature(
            typed_data=typed, signature="0xdeadbeef",
            signer=_payer().get_address(),
        )


def test_un_typed_data_que_no_es_una_orden_de_ciclo_de_vida_se_rechaza():
    """Pasar el typed data equivocado -el de ERC-3009, por ejemplo- produciria
    una firma valida sobre otra cosa. El dominio lo delata."""
    pi = _payment_info()
    typed = build_lifecycle_typed_data(
        action="release", payment_info=pi, payer=_payer().get_address(),
        amount=AMOUNT, chain_id=CHAIN, deadline=NOW + 600, nonce="0x" + "01" * 32,
    )
    firma = _firma_ajena(typed, _payer())

    otro = {**typed, "domain": {**typed["domain"], "name": "USD Coin"}}
    with pytest.raises(ValueError, match="dominio"):
        lifecycle_auth_from_signature(
            typed_data=otro, signature=firma, signer=_payer().get_address()
        )


def test_lifecycle_auth_viaja_byte_a_byte_en_el_release(monkeypatch):
    """Lo que el transportador recibio es lo que sale por el cable.

    Normalizar un campo aca -recortar un hex, recalcular la ventana- seria
    enviar algo distinto de lo que entro al digest.
    """
    capturado: list = []
    cliente = _cliente_falso(monkeypatch, capturado)

    pi_wire = cliente._payment_info_to_camel_dict(_payment_info_dataclass())
    typed = build_lifecycle_typed_data(
        action="release", payment_info=pi_wire, payer=cliente.payer,
        amount=AMOUNT, chain_id=CHAIN, deadline=NOW + 600, nonce="0x" + "5e" * 32,
    )
    ajena = lifecycle_auth_from_signature(
        typed_data=typed,
        signature=_firma_ajena(typed, _payer()),
        signer=_payer().get_address(),
    )

    resultado = cliente.release_via_facilitator(
        _payment_info_dataclass(), lifecycle_auth=ajena
    )

    assert resultado.success is True
    enviado = capturado[0]["payload"]["lifecycleAuth"]
    assert enviado == ajena
    assert enviado["nonce"] == "0x" + "5e" * 32
    assert enviado["deadline"] == NOW + 600

    # Y sigue verificando contra el paymentInfo que efectivamente viajo.
    revisado = build_lifecycle_typed_data(
        action="release", payment_info=capturado[0]["payload"]["paymentInfo"],
        payer=capturado[0]["payload"]["payer"],
        amount=int(capturado[0]["payload"]["amount"]), chain_id=CHAIN,
        deadline=enviado["deadline"], nonce=enviado["nonce"],
    )
    assert _recover(revisado, enviado["signature"]) == enviado["signer"]


def test_lifecycle_auth_viaja_byte_a_byte_en_el_refund(monkeypatch):
    """El refund lo firma el receiver, y el transportador tampoco es el."""
    capturado: list = []
    cliente = _cliente_falso(monkeypatch, capturado)

    pi_wire = cliente._payment_info_to_camel_dict(_payment_info_dataclass())
    typed = build_lifecycle_typed_data(
        action="refundInEscrow", payment_info=pi_wire, payer=cliente.payer,
        amount=AMOUNT, chain_id=CHAIN, deadline=NOW + 600, nonce="0x" + "6f" * 32,
    )
    ajena = lifecycle_auth_from_signature(
        typed_data=typed,
        signature=_firma_ajena(typed, _receiver()),
        signer=_receiver().get_address(),
    )

    cliente.refund_via_facilitator(_payment_info_dataclass(), lifecycle_auth=ajena)

    assert capturado[0]["action"] == "refundInEscrow"
    assert capturado[0]["payload"]["lifecycleAuth"] == ajena
    assert capturado[0]["payload"]["lifecycleAuth"]["signer"] == pi_wire["receiver"]


def test_los_dos_parametros_juntos_son_un_error_explicito(monkeypatch):
    """Uno firma una orden nueva y el otro trae una ajena. Elegir en silencio
    manda una orden distinta de la que el llamador cree - sobre `release`, eso
    es plata que se mueve."""
    capturado: list = []
    cliente = _cliente_falso(monkeypatch, capturado)

    pi_wire = cliente._payment_info_to_camel_dict(_payment_info_dataclass())
    typed = build_lifecycle_typed_data(
        action="release", payment_info=pi_wire, payer=cliente.payer,
        amount=AMOUNT, chain_id=CHAIN, deadline=NOW + 600, nonce="0x" + "7a" * 32,
    )
    ajena = lifecycle_auth_from_signature(
        typed_data=typed,
        signature=_firma_ajena(typed, _payer()),
        signer=_payer().get_address(),
    )

    for metodo in ("release_via_facilitator", "refund_via_facilitator"):
        with pytest.raises(ValueError, match="excluyentes"):
            getattr(cliente, metodo)(
                _payment_info_dataclass(),
                lifecycle_signer=_payer(),
                lifecycle_auth=ajena,
            )

    # Y no se mando nada: el error es ANTES del POST.
    assert capturado == []


def test_sin_ninguno_de_los_dos_el_payload_sigue_saliendo_como_antes(monkeypatch):
    """El parametro nuevo no cambia el default de nadie."""
    capturado: list = []
    cliente = _cliente_falso(monkeypatch, capturado)

    cliente.release_via_facilitator(_payment_info_dataclass())

    assert set(capturado[0]["payload"]) == {"paymentInfo", "payer", "amount"}


# ---------------------------------------------------------------------------
# `primaryType`: el campo sin el cual viem no firma (0.80.0)
# ---------------------------------------------------------------------------
#
# Lo destapo el worker de execution-market al hacer que el publisher firmara la
# orden EN EL NAVEGADOR. `types` tiene dos entradas -`LifecycleOrder` y
# `PaymentInfo`- y solo una es la raiz. ethers y eth-account la DEDUCEN; viem
# no: exige que el documento la nombre, y sin eso `signTypedData` tira antes de
# mostrarle nada al usuario. El gemelo TypeScript lo emitia desde su primer dia
# (`lifecycle-auth.ts:422`), este SDK no, y el gate de conformidad cruzada
# estuvo verde con la divergencia adentro porque no miraba el campo.
#
# El campo NO entra al digest -EIP-712 hashea dominio, tipos y mensaje-, asi
# que la firma no se mueve. Eso no se supone: se mide, abajo.


def test_el_typed_data_nombra_su_struct_raiz():
    """Sin esto, la ruta del navegador esta cerrada del lado Python."""
    typed = build_lifecycle_typed_data(
        action="release", payment_info=_payment_info(),
        payer=_payer().get_address(), amount=AMOUNT, chain_id=CHAIN,
        deadline=NOW + 600, nonce="0x" + "01" * 32,
    )

    assert typed["primaryType"] == "LifecycleOrder"
    assert typed["primaryType"] == LIFECYCLE_PRIMARY_TYPE
    # Y nombra un tipo que existe: un `primaryType` que no esta en `types` es
    # un documento que viem rechaza igual que si faltara.
    assert typed["primaryType"] in typed["types"]


def test_el_documento_es_exactamente_lo_que_viem_recibe():
    """Las cuatro claves de `signTypedData`, y ninguna de mas.

    viem pide `domain`, `types`, `primaryType` y `message`. Ademas `types` NO
    puede traer `EIP712Domain`: viem y ethers lo derivan del dominio, y traerlo
    escrito hace que ethers tire `ambiguous primary types`.
    """
    typed = build_lifecycle_typed_data(
        action="release", payment_info=_payment_info(),
        payer=_payer().get_address(), amount=AMOUNT, chain_id=CHAIN,
        deadline=NOW + 600, nonce="0x" + "01" * 32,
    )

    assert set(typed) == {"domain", "types", "primaryType", "message"}
    assert "EIP712Domain" not in typed["types"]
    assert set(typed["types"]) == {"LifecycleOrder", "PaymentInfo"}


def test_primaryType_no_movio_la_firma_del_vector_fijado():
    """El riesgo real de este cambio, medido y no supuesto.

    Esto toca el cuerpo de una orden FIRMADA. Si agregar el campo moviera los
    65 bytes, toda orden emitida antes de 0.80.0 dejaria de verificar. La firma
    de abajo es la misma que fija
    `test_vector_fijado_para_el_gemelo_typescript` desde 0.78.0, byte por byte,
    y es tambien la del `.rs` del facilitador.
    """
    wallet = EnvKeyAdapter("0x" + "11" * 32)
    pi = {
        "operator": "0x271f9fa7f8907aCf178CCFB470076D9129D8F0Eb",
        "receiver": "0x2222222222222222222222222222222222222222",
        "token": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
        "maxAmount": "1000000",
        "preApprovalExpiry": 1757003600,
        "authorizationExpiry": 1757007200,
        "refundExpiry": 1759592000,
        "minFeeBps": 0,
        "maxFeeBps": 1300,
        "feeReceiver": "0xaE07cEB6b395BC685a776a0b4c489E8d9cE9A6ad",
        "salt": "0x0000000000000000000000000000000000000000000000000000000000003039",
    }
    auth = build_lifecycle_auth(
        action="release", payment_info=pi, payer=wallet.get_address(),
        amount=1_000_000, chain_id=8453, wallet=wallet,
        deadline=1757000600, nonce="0x" + "01" * 32, now=1757000000,
    )

    assert auth["signature"] == (
        "0x78fe143886ee329e235cd7735e948f50ecd2ef32b20a4c88c46767cdd63b33ca"
        "77553f16c73adacf754e34b2cc988137fd77b843649d990c9a1a5001b28a13a71c"
    )

    # Y el porque: borrar el campo del documento da EL MISMO digest. Lo que
    # entra al hash es dominio + tipos + mensaje; `primaryType` es metadato
    # para el firmante.
    typed = build_lifecycle_typed_data(
        action="release", payment_info=pi, payer=wallet.get_address(),
        amount=1_000_000, chain_id=8453, deadline=1757000600,
        nonce="0x" + "01" * 32,
    )
    sin_el_campo = {k: v for k, v in typed.items() if k != "primaryType"}
    assert _digest(typed) == _digest(sin_el_campo)


def test_una_orden_que_dice_ser_otro_struct_no_se_transporta():
    """El documento se identifica antes de leerle nada.

    Un typed data de PAGO tiene otro dominio y ya lo agarra el chequeo de
    dominio; pero un documento que declara otra raiz sobre ESTE dominio es una
    firma sobre un struct distinto del que el bloque de wire dice llevar.
    """
    typed = build_lifecycle_typed_data(
        action="release", payment_info=_payment_info(),
        payer=_payer().get_address(), amount=AMOUNT, chain_id=CHAIN,
        deadline=NOW + 600, nonce="0x" + "01" * 32,
    )
    firma = _firma_ajena(typed, _payer())

    otro = {**typed, "primaryType": "PaymentInfo"}
    with pytest.raises(ValueError, match="primaryType"):
        lifecycle_auth_from_signature(
            typed_data=otro, signature=firma, signer=_payer().get_address()
        )


def test_un_documento_de_0_79_0_sin_el_campo_sigue_entrando():
    """Compatibilidad hacia atras, y es deliberada.

    El gemelo TypeScript EXIGE el campo, porque nunca emitio un documento sin
    el. Este SDK si: todo lo que salio de 0.78.0 y 0.79.0 no lo trae. Un
    backend que guardo el documento antes de mandarlo a firmar tiene uno de
    esos en la mano, y rechazarlo seria romper una orden buena por un campo
    que no entra al digest. Ausente se tolera; presente y equivocado no.
    """
    typed = build_lifecycle_typed_data(
        action="release", payment_info=_payment_info(),
        payer=_payer().get_address(), amount=AMOUNT, chain_id=CHAIN,
        deadline=NOW + 600, nonce="0x" + "01" * 32,
    )
    firma = _firma_ajena(typed, _payer())

    viejo = {k: v for k, v in typed.items() if k != "primaryType"}
    bloque = lifecycle_auth_from_signature(
        typed_data=viejo, signature=firma, signer=_payer().get_address()
    )
    assert bloque["signature"] == firma


def test_el_documento_sobrevive_el_viaje_por_json():
    """El documento es un tipo de WIRE, y el wire es JSON.

    Dos cosas lo rompian antes de 0.80.0, las dos medidas contra viem 2.56.3:

    1. `nonce` salia como `bytes`. `json.dumps` tira `TypeError` sobre bytes,
       asi que el documento no se podia ni mandar. Execution Market lo estaba
       parcheando en el consumidor (`mcp_server/integrations/x402/
       lifecycle_auth.py`), que es justo lo que upstream-first evita.

    2. Los uint salian como enteros de Python. `salt` es de 32 bytes: como
       numero JSON, `JSON.parse` lo entrega como `double` y el navegador firma
       OTRO struct. Medido con `salt = 0xab*32`: Python firmaba
       `0x15a8587e...` y viem, leyendo ese mismo documento por JSON,
       `0x4c88c56a...`. Sin error y sin advertencia — solo una orden que el
       facilitador rechaza como `bad_signature`. Con los uint como string las
       dos firmas son la misma.
    """
    pi = {**_payment_info(), "salt": "0x" + "ab" * 32}
    typed = build_lifecycle_typed_data(
        action="release", payment_info=pi, payer=_payer().get_address(),
        amount=AMOUNT, chain_id=CHAIN, deadline=NOW + 600,
        nonce="0x" + "01" * 32,
    )

    # 1. Se puede serializar, y vuelve identico.
    ida_y_vuelta = json.loads(json.dumps(typed))
    assert ida_y_vuelta == typed
    assert _digest(ida_y_vuelta) == _digest(typed)

    # 2. Ningun valor del mensaje es un numero: un numero de 32 bytes no
    #    sobrevive un `JSON.parse` del otro lado.
    def _hojas(valor):
        if isinstance(valor, dict):
            for v in valor.values():
                yield from _hojas(v)
        else:
            yield valor

    for hoja in _hojas(typed["message"]):
        assert isinstance(hoja, str), f"{hoja!r} viaja como numero por el JSON"

    # Y el salt sigue siendo el uint256 que el facilitador espera, escrito en
    # decimal — no el hex del wire.
    assert typed["message"]["paymentInfo"]["salt"] == str(int("ab" * 32, 16))
