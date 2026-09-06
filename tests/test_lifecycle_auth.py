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
    build_lifecycle_auth,
    build_lifecycle_typed_data,
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


def test_el_salt_entra_como_entero_no_como_hex():
    """`salt` es bytes32 en el wire y uint256 en la firma.

    El facilitador lo convierte con `U256::from_be_bytes` (types.rs:288).
    Firmarlo como string produce otro digest y un `bad_signature` mudo — el
    unico sintoma seria que ninguna orden verifica jamas.
    """
    pi = _payment_info()
    typed = build_lifecycle_typed_data(
        action="release", payment_info=pi, payer=_payer().get_address(),
        amount=AMOUNT, chain_id=CHAIN, deadline=NOW + 600,
        nonce="0x" + "01" * 32,
    )
    assert typed["message"]["paymentInfo"]["salt"] == 0x3039
    assert isinstance(typed["message"]["paymentInfo"]["salt"], int)


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
    # El salt entra como el ENTERO 12345, no como el string hex.
    assert typed["message"]["paymentInfo"]["salt"] == 12345

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
