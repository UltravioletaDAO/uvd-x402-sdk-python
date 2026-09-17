"""Native wire, authorization boundaries, money units and real buyer loop."""
import base64
import json
from decimal import Decimal

import httpx
import pytest
from uvd_x402_sdk import X402Client, X402Config
from uvd_x402_sdk.hedera import HederaSigner, build_hedera_requirements, build_hedera_request, validate_hedera_requirements
from uvd_x402_sdk.networks import get_network, normalize_network, to_caip2_network, get_token_config
from uvd_x402_sdk.response import create_402_response, create_402_response_v2

hiero = pytest.importorskip("hiero_sdk_python")
from hiero_sdk_python.hapi.sdk.transaction_list_pb2 import TransactionList
from hiero_sdk_python.hapi.services.transaction_contents_pb2 import SignedTransaction
from hiero_sdk_python.hapi.services.transaction_pb2 import TransactionBody
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey


def offer(network="hedera:testnet", asset="usdc", amount="1000"):
    return build_hedera_requirements(network, "0.0.222", amount, asset=asset)


def signer(network="hedera:testnet"):
    return HederaSigner("0.0.111", hiero.PrivateKey.generate_ed25519().to_string_der(), network=network)


@pytest.mark.parametrize("network,usdc,sponsor", [("hedera:mainnet", "0.0.456858", "0.0.10868300"), ("hedera:testnet", "0.0.429274", "0.0.10576385")])
def test_registry_network_asset_and_sponsor(network, usdc, sponsor):
    assert normalize_network(network) == to_caip2_network(network) == network
    assert get_network(network).chain_id == 0
    assert get_network(network).usdc_address == usdc
    assert offer(network)["extra"] == {"feePayer": sponsor}
    assert get_token_config(network, "hbar").decimals == 8
    assert get_token_config(network, "hbar").usd_pegged is False


@pytest.mark.parametrize("network", ["hedera:mainnet", "hedera:testnet"])
@pytest.mark.parametrize("asset", ["usdc", "hbar"])
def test_signed_transaction_list_exact_principal_and_all_variant_signatures(network, asset):
    r = offer(network, asset, "9007199254740993")  # beyond JS Number precision
    p = signer(network).create_payment_payload(r)
    transactions = TransactionList.FromString(base64.b64decode(p["payload"]["transaction"]))
    assert len(transactions.transaction_list) == 3
    ids = set()
    bodies = []
    for tx in transactions.transaction_list:
        signed = SignedTransaction.FromString(tx.signedTransactionBytes)
        assert len(signed.sigMap.sigPair) == 1  # only buyer, sponsor signature absent
        sig = signed.sigMap.sigPair[0]
        Ed25519PublicKey.from_public_bytes(sig.pubKeyPrefix).verify(sig.ed25519, signed.bodyBytes)
        body = TransactionBody.FromString(signed.bodyBytes)
        assert body.transactionID.accountID.accountNum == int(r["extra"]["feePayer"].split(".")[2])
        assert body.transactionFee == 100000000
        assert body.transactionValidDuration.seconds == 180
        ids.add(body.transactionID.SerializeToString())
        transfer = body.cryptoTransfer
        legs = transfer.transfers.accountAmounts if asset == "hbar" else transfer.tokenTransfers[0].transfers
        assert {x.accountID.accountNum: x.amount for x in legs} == {111: -9007199254740993, 222: 9007199254740993}
        assert all(not x.is_approval for x in legs)
        if asset == "usdc":
            assert len(transfer.tokenTransfers) == 1
            assert transfer.tokenTransfers[0].token.tokenNum == int(r["asset"].split(".")[2])
        body.ClearField("nodeAccountID")
        bodies.append(body.SerializeToString())
    assert len(ids) == 1 and len(set(bodies)) == 1
    assert build_hedera_request(p, r)["paymentRequirements"] == r


@pytest.mark.parametrize("field,value", [
    ("amount", "0"), ("amount", "-1"), ("amount", "1.1"), ("amount", "01"), ("amount", "9223372036854775808"),
    ("asset", "0.0.456858"), ("network", "eip155:296"), ("scheme", "upto"),
    ("maxTimeoutSeconds", 14), ("maxTimeoutSeconds", 181), ("maxTimeoutSeconds", True),
    ("extra", {"feePayer": "0.0.10576385", "hook": True}), ("payTo", "0.0.0"),
])
def test_reject_unsupported_or_unsafe_offers(field, value):
    r=offer();r[field]=value
    with pytest.raises(ValueError): validate_hedera_requirements(r)


def test_signer_refuses_other_ledger_sponsor_and_self_payment():
    s=signer()
    for r in [offer("hedera:mainnet"), {**offer(), "extra":{"feePayer":"0.0.999"}}, {**offer(), "payTo":"0.0.111"}]:
        with pytest.raises(ValueError):s.create_payment_payload(r)


def test_merchant_rejects_changed_price_and_extensions():
    r=offer();p=signer().create_payment_payload(r)
    with pytest.raises(ValueError):build_hedera_request(p,{**r,"amount":"999"})
    with pytest.raises(ValueError):build_hedera_request({**p,"extensions":{"x":True}},r)


def test_native_challenge_is_only_v2_and_has_fee_payer():
    config=X402Config(recipient_hedera="0.0.222", supported_networks=["hedera:testnet"])
    assert create_402_response("0.001", config)["supportedChains"] == []
    challenge=create_402_response_v2("0.001",config,max_timeout_seconds=180)
    assert challenge["accepts"] == [offer()]


def test_native_usdc_price_preserves_atomic_precision():
    config=X402Config(recipient_hedera="0.0.222", supported_networks=["hedera:testnet"])
    challenge=create_402_response_v2(Decimal("9007199254.740993"),config)
    assert challenge["accepts"][0]["amount"] == "9007199254740993"
    with pytest.raises(ValueError,match="6 decimal"):
        create_402_response_v2(Decimal("0.0000001"),config)


@pytest.mark.parametrize("asset,amount,ceiling", [("usdc","1000","0.001"),("hbar","10000","0.0001")])
def test_native_buyer_loop_echoes_accepted_and_correct_units(asset,amount,ceiling):
    r=offer(asset=asset,amount=amount);seen=[]
    def handler(request):
        seen.append(request)
        if len(seen)==1:return httpx.Response(402,json={"x402Version":2,"accepts":[r]})
        p=json.loads(base64.b64decode(request.headers["payment-signature"]))
        assert p["accepted"]==r
        assert build_hedera_request(p,r)["x402Version"]==2
        return httpx.Response(200,json={"paid":True})
    client=X402Client(config=X402Config(recipient_hedera="0.0.222",supported_networks=["hedera:testnet"]))
    client.connect_with_hedera("0.0.111",hiero.PrivateKey.generate_ed25519().to_string_der(),network="hedera:testnet")
    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        result=client.fetch("https://merchant.example/paid",max_amount=ceiling,token_type=asset,http_client=http)
    assert result.status_code==200 and len(seen)==2


def test_usd_merchant_api_rejects_hbar():
    c=X402Client(config=X402Config(recipient_hedera="0.0.222",supported_networks=["hedera:testnet"]))
    p=c.extract_payload(signer().create_payment_header(offer(asset="hbar")))
    with pytest.raises(ValueError,match="HBAR"):
        c._build_payment_requirements(p,Decimal("1"),asset="0.0.0",token_decimals=8)
