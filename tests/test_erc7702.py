"""EIP-7702: el wrap ERC-1271, y sobre todo el TERCER ESTADO.

Estas pruebas fijan lo que costó ocho días de escrows rotos en producción (14 de 14
pagadores delegados fallando el lock, con el único no-delegado funcionando bien): que un
`None` del resolvedor NO es un "no está delegada". El bug no estuvo nunca en la firma
—era correcta— sino en un `if delegated:` que aplastaba el desconocimiento contra False.
"""
from __future__ import annotations

import json

import pytest

from uvd_x402_sdk import erc7702


# ── el designador y el wrap ──────────────────────────────────────────────────

def test_reconoce_una_eoa_delegada():
    code = "0xef010069007702764179f14f51cdce752f4f775d74e139"
    assert erc7702.delegate_target(code) == "0x69007702764179f14f51cdce752f4f775d74e139"


@pytest.mark.parametrize("code", [
    None, "0x", "0x00",
    "0x6080604052",                                   # contrato normal, no 7702
    "0xef0100" + "ab" * 5,                            # designador truncado
])
def test_lo_que_no_es_delegacion_da_None(code):
    assert erc7702.delegate_target(code) is None


def test_el_wrap_es_el_localizador_exacto_del_fallback():
    """7 bytes: 0x00 00000000 (validación 0/entidad 0) FF (segmento final) 00 (EOA).

    Un byte distinto y el router de firmas de la cuenta manda la validación a otro
    lado: el mismo error de "firma inválida" que se ve cuando NO se envuelve, así que
    equivocarse acá se diagnostica como el bug que se venía de arreglar.
    """
    envuelta = erc7702.wrap_signature("0x" + "11" * 65)
    assert envuelta.startswith("0x00000000" + "00" + "ff" + "00")
    assert len(envuelta) == 2 + (7 + 65) * 2


def test_el_replay_safe_usa_el_dominio_de_LA_CUENTA():
    """No el del token: la cuenta valida sobre SU propio dominio EIP-712."""
    dom, types, msg = erc7702.replay_safe_typed_data(b"\x01" * 32, 8453, "0xAcc")
    assert dom == {"chainId": 8453, "verifyingContract": "0xAcc"}
    assert set(dom) == {"chainId", "verifyingContract"}      # los dos campos del typehash
    assert types == {"ReplaySafeHash": [{"name": "hash", "type": "bytes32"}]}
    assert msg == {"hash": b"\x01" * 32}


def test_firma_delegada_va_por_el_wallet_y_devuelve_la_envuelta():
    """El punto de todo esto: es una firma typed-data corriente, así que la puede
    producir un firmante REMOTO — la llave nunca tiene que existir en el proceso."""
    vistos = {}

    class _W:
        def sign_typed_data(self, td):
            vistos.update(td)
            return {"signature": "0x" + "22" * 65}

    out = erc7702.sign_eip3009_for_delegated(
        wallet=_W(), inner_digest=b"\x03" * 32, chain_id=8453, account="0xAcc")
    assert vistos["domain"]["verifyingContract"] == "0xAcc"
    assert out == erc7702.wrap_signature("0x" + "22" * 65)


def test_si_el_wallet_no_devuelve_firma_se_falla_fuerte():
    class _W:
        def sign_typed_data(self, td):
            return {}

    with pytest.raises(ValueError, match="no signature"):
        erc7702.sign_eip3009_for_delegated(
            wallet=_W(), inner_digest=b"\x03" * 32, chain_id=8453, account="0xAcc")


# ── EL TERCER ESTADO ─────────────────────────────────────────────────────────

def test_sin_resolvedor_la_respuesta_es_DESCONOCIDO_no_False():
    """"Nadie preguntó" y "es una EOA común" son hechos distintos, y sólo uno es
    seguro para firmar encima."""
    assert erc7702.is_delegated("0xabc", "base") is None


def test_un_resolvedor_roto_es_DESCONOCIDO():
    def _explota(a, n):
        raise RuntimeError("RPC caído")
    assert erc7702.is_delegated("0xabc", "base", _explota) is None


def test_un_resolvedor_que_devuelve_basura_es_DESCONOCIDO():
    assert erc7702.is_delegated("0xabc", "base", lambda a, n: "sí") is None


@pytest.mark.parametrize("v", [True, False])
def test_un_veredicto_booleano_pasa_tal_cual(v):
    assert erc7702.is_delegated("0xabc", "base", lambda a, n: v) is v


def test_el_resolvedor_por_RPC_devuelve_None_si_TODOS_los_endpoints_fallan():
    """Una cadena ilegible no es un veredicto negativo."""
    r = erc7702.rpc_delegation_resolver(["http://127.0.0.1:9/nope"], timeout=0.2)
    assert r("0xabc", "base") is None


def test_el_resolvedor_por_RPC_sin_endpoints_es_None():
    assert erc7702.rpc_delegation_resolver([])("0xabc", "base") is None


# ── integración con el escrow: la parte que mueve plata ──────────────────────

def _config():
    return {"escrow": {"payment_info_typehash": "0x" + "ab" * 32, "networks": {"base": {
        "chain_id": 8453,
        "operator": "0x1111111111111111111111111111111111111111",
        "escrow": "0x2222222222222222222222222222222222222222",
        "token_collector": "0x3333333333333333333333333333333333333333",
        "usdc": "0x4444444444444444444444444444444444444444",
        "usdc_domain_name": "USDC", "usdc_domain_version": "2",
    }}}}


class _Wallet:
    def __init__(self):
        self.llamadas = []

    def sign_typed_data(self, td):
        self.llamadas.append(td)
        return {"signature": "0x" + "55" * 65}


def _construir(**kw):
    from uvd_x402_sdk.escrow_signing import build_escrow_pre_auth
    base = dict(payment_config=_config(), network="base",
                payer="0x5555555555555555555555555555555555555555",
                receiver="0x6666666666666666666666666666666666666666",
                amount_usd=0.05, deadline=None)
    base.update(kw)
    return build_escrow_pre_auth(**base)


def test_sin_resolvedor_el_comportamiento_no_cambia():
    """Compatibilidad: quien no pasa resolvedor firma como antes, crudo."""
    w = _Wallet()
    out = json.loads(_construir(wallet=w))
    assert out["payload"]["signature"] == "0x" + "55" * 65
    assert w.llamadas[0]["types"] and "ReplaySafeHash" not in w.llamadas[0]["types"]


def test_un_pagador_delegado_firma_el_replay_safe_y_va_ENVUELTO():
    w = _Wallet()
    out = json.loads(_construir(wallet=w, delegation_resolver=lambda a, n: True))
    assert "ReplaySafeHash" in w.llamadas[0]["types"], "debió firmar el wrapper, no el digest"
    assert out["payload"]["signature"].startswith("0x00000000" + "00" + "ff" + "00")


def test_un_pagador_NO_delegado_firma_crudo():
    w = _Wallet()
    out = json.loads(_construir(wallet=w, delegation_resolver=lambda a, n: False))
    assert "ReplaySafeHash" not in w.llamadas[0]["types"]
    assert out["payload"]["signature"] == "0x" + "55" * 65


def test_si_NO_SE_SABE_no_se_firma_NADA():
    """LA invariante. Firmar a ciegas elige entre dos dialectos incompatibles y el
    error sólo aparece al bloquear el escrow, lejos de acá."""
    w = _Wallet()
    with pytest.raises(ValueError, match="could not determine"):
        _construir(wallet=w, delegation_resolver=lambda a, n: None)
    assert w.llamadas == [], "no se puede haber pedido ninguna firma"
