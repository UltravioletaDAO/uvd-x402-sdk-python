import base64
import copy
import json
from pathlib import Path

import httpx
import pytest
from uvd_x402_sdk.receipts import (PurchaseContext, commitment, fetch_with_receipt,
    parse_receipt, payment_response_headers, response_receipt, validate_purchase_context, verify_receipt)

VECTORS = json.loads((Path(__file__).parent / 'fixtures/facilitator-receipts-v1.json').read_text())


@pytest.mark.parametrize('case', VECTORS['cases'])
def test_shared_receipt_vectors_and_signatures(case):
    receipt = parse_receipt(case['receipt'])
    assert verify_receipt(receipt, VECTORS['jwks'])
    assert not verify_receipt(receipt, VECTORS['jwks'], issuer='https://impostor.example')
    forged = receipt.model_copy(deep=True)
    forged.amount = '2000'
    forged.request['amount'] = '2000'
    forged.requestHash = commitment(forged.requestHashVersion, forged.request)
    assert not verify_receipt(forged, VECTORS['jwks'])


def wire_receipt(context, status='confirmed'):
    receipt = copy.deepcopy(VECTORS['cases'][-1]['receipt'])
    receipt.update(operation='settle', purchaseId=context['purchaseId'], status=status,
        settlement={'id': '0.0.3003@1700000000.000000001', 'idType': 'hedera-transaction-id'}, proof=None)
    for key in ('purchaseId','method','url','bodySha256'):
        receipt['request'][key] = context[key]
    receipt['requestHash'] = commitment(receipt['requestHashVersion'], receipt['request'])
    return receipt


class Buyer:
    def __init__(self, http):
        self.http = http
        self.signatures = 0

    def _get_http_client(self):
        return self.http

    def fetch(self, url, *, http_client, method='GET', **kwargs):
        probe = http_client.request(method, url, **kwargs)
        if probe.status_code != 402:
            return probe
        self.signatures += 1
        headers = dict(kwargs.pop('headers', {}))
        headers['X-PAYMENT'] = 'one-authorization'
        return http_client.request(method, url, headers=headers, **kwargs)


def test_lost_response_restart_reuses_authorization_and_preserves_merchant_500():
    sent, saved = [], []

    def merchant(request):
        if not request.headers.get('X-PAYMENT'):
            return httpx.Response(402, json={'accepts': []})
        sent.append(request.headers['X-PAYMENT'])
        if len(sent) == 1:
            raise httpx.ReadTimeout('lost after settlement', request=request)
        context = json.loads(base64.b64decode(request.headers['X-UVD-Purchase']))
        validate_purchase_context(request.headers['X-UVD-Purchase'], request.method, str(request.url), request.content)
        receipt = wire_receipt(context)
        return httpx.Response(500, content=b'merchant failed after payment', headers=payment_response_headers({'success': True, 'receipt': receipt}))

    with httpx.Client(transport=httpx.MockTransport(merchant)) as http:
        client = Buyer(http)
        first = fetch_with_receipt(client, 'https://merchant.example/data', context=PurchaseContext(), persist=saved.append)
        assert first.payment_state == 'unknown' and first.response is None
        restarted = PurchaseContext.from_dict(saved[-1])
        second = fetch_with_receipt(client, 'https://merchant.example/data', context=restarted, persist=saved.append)
        assert client.signatures == 1 and sent == ['one-authorization', 'one-authorization']
        assert second.payment_state == 'confirmed'
        assert second.response.status_code == 500
        assert second.response.content == b'merchant failed after payment'
        assert second.proof_verified is False
        with pytest.raises(ValueError, match='another HTTP request'):
            fetch_with_receipt(client, 'https://merchant.example/other', context=restarted, persist=saved.append)
        assert client.signatures == 1


def test_persist_failure_prevents_authorized_request():
    requests = []
    def merchant(request):
        requests.append(request)
        return httpx.Response(402, json={})
    def fail(_):
        raise OSError('durable storage unavailable')
    with httpx.Client(transport=httpx.MockTransport(merchant)) as http:
        with pytest.raises(OSError):
            fetch_with_receipt(Buyer(http), 'https://merchant.example/data', context=PurchaseContext(), persist=fail)
    assert len(requests) == 1 and 'X-PAYMENT' not in requests[0].headers


def test_old_merchant_and_header_ambiguity_do_not_invent_receipts():
    assert response_receipt(httpx.Response(200, content=b'untouched')) is None
    header = payment_response_headers({'receipt': VECTORS['cases'][0]['receipt']})['PAYMENT-RESPONSE']
    with pytest.raises(ValueError, match='duplicate'):
        response_receipt(httpx.Response(200, headers=[('PAYMENT-RESPONSE', header), ('PAYMENT-RESPONSE', header)]))
    future = copy.deepcopy(VECTORS['cases'][0]['receipt'])
    future['schemaVersion'] = 2
    with pytest.raises(ValueError, match='unsupported'):
        parse_receipt(future)


def test_merchant_checks_method_url_and_exact_body():
    context = PurchaseContext()
    context.bind(httpx.Request('POST', 'https://merchant.example/data', content=b'{"a":1}'))
    assert validate_purchase_context(context.header(), 'POST', context.url, b'{"a":1}')
    for method, url, body in [('GET', context.url, b'{"a":1}'), ('POST', context.url+'?other=1', b'{"a":1}'), ('POST', context.url, b'{ "a": 1 }')]:
        with pytest.raises(ValueError):
            validate_purchase_context(context.header(), method, url, body)


def test_fastapi_propagates_receipt_and_validates_context(monkeypatch):
    pytest.importorskip('fastapi')
    import asyncio
    from decimal import Decimal
    from fastapi import FastAPI, Depends, Response
    from uvd_x402_sdk import X402Client, X402Config
    from uvd_x402_sdk.models import PaymentResult
    from uvd_x402_sdk.integrations.fastapi_integration import FastAPIX402

    context = PurchaseContext()
    context.bind(httpx.Request('GET', 'https://merchant.example/data'))
    receipt = wire_receipt(json.loads(base64.b64decode(context.header())))
    calls = []
    def settle(self, **kwargs):
        calls.append(kwargs)
        assert kwargs['receipt_context'] == context.header()
        return PaymentResult(payer_address=receipt['payer'], network='hedera:testnet',
                             amount_usd=Decimal('0.001'), receipt=receipt)
    monkeypatch.setattr(X402Client, 'process_payment', settle)
    app = FastAPI()
    integration = FastAPIX402(app, config=X402Config(recipient_evm='0x'+'11'*20, supported_networks=['arc-testnet']))
    @app.get('/data')
    async def paid(response: Response, payment=Depends(integration.require_payment(Decimal('0.001')))):
        response.status_code = 500  # Business handling failed after successful settlement.
        return {'delivered': False}

    async def check():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='https://merchant.example') as http:
            result = await http.get('/data', headers={'X-PAYMENT':'fixture', 'X-UVD-Purchase':context.header()})
            assert result.status_code == 500
            assert response_receipt(result).model_dump() == receipt
            assert result.json() == {'delivered':False}
            bad = await http.get('/data?changed=1', headers={'X-PAYMENT':'fixture', 'X-UVD-Purchase':context.header()})
            assert bad.status_code == 400
            assert len(calls) == 1
    asyncio.run(check())
