"""Offline cryptographic acceptance; no funded EURC settlement is claimed."""
import base64
import json
from decimal import Decimal

import pytest
from eth_account import Account
from eth_account.messages import encode_typed_data

from uvd_x402_sdk import X402Client
from uvd_x402_sdk.envelope_v2 import build_verify_request_v2
from uvd_x402_sdk.networks import get_network


@pytest.mark.parametrize("network,chain_id,address,separator", [
    ("arc", 5042, "0xbEf5f6d51CB62b58e6A8f77868681825C6fe21c1",
     "25fe3beaae16ef5c1cb9757c6efc1bf33f81ecd4c7dae191320372013b7d2175"),
    ("arc-testnet", 5042002, "0x89B50855Aa3bE2F677cD6303Cec089B5F319D72a",
     "649ec6b0634bd74f28684781d2c9ae49dff14ba3d5f9bb5d70c1e1f0e1ebf160"),
])
@pytest.mark.parametrize("version", [1, 2])
def test_eurc_signature_token_units_and_domain_isolation(
    network, chain_id, address, separator, version
):
    wallet = Account.create()
    buyer = X402Client(recipient_address=wallet.address)
    buyer.connect_with_private_key(wallet.key.hex(), chain_name=network)
    token = get_network(network).tokens["eurc"]
    assert (token.address, token.decimals, token.usd_pegged) == (address, 6, False)
    accepted = {"scheme": "exact", "network": f"eip155:{chain_id}", "asset": address,
                "amount": "10000", "payTo": wallet.address, "maxTimeoutSeconds": 300,
                "extra": {"name": "EURC", "version": "2"}}
    resource = {"url": "https://example.com/eurc"}
    header = buyer.create_authorization(wallet.address, Decimal("0.01"), token_type="eurc",
                                        x402_version=version, accepted=accepted, resource=resource)
    wire = json.loads(base64.b64decode(header))
    inner = wire["payload"]
    assert inner["authorization"]["value"] == "10000"  # 0.01 euros, no FX conversion
    types = {"TransferWithAuthorization": [
        {"name": n, "type": t} for n, t in [("from", "address"), ("to", "address"),
        ("value", "uint256"), ("validAfter", "uint256"),
        ("validBefore", "uint256"), ("nonce", "bytes32")]
    ]}
    domain = {"name": "EURC", "version": "2", "chainId": chain_id, "verifyingContract": address}
    message = encode_typed_data(domain, types, inner["authorization"])
    assert message.header.hex() == separator
    assert Account.recover_message(message, signature=inner["signature"]) == wallet.address
    for field, value in [("chainId", 5042002 if chain_id == 5042 else 5042),
                         ("name", "USDC"),
                         ("verifyingContract", get_network(network).usdc_address)]:
        wrong = encode_typed_data({**domain, field: value}, types, inner["authorization"])
        assert Account.recover_message(wrong, signature=inner["signature"]) != wallet.address
    if version == 2:
        assert wire["accepted"] == accepted
        request = build_verify_request_v2(inner, resource, accepted)
        assert request["accepted"]["amount"] == "10000"
        assert request["accepted"]["asset"] == address
    parsed = buyer.extract_payload(header)
    with pytest.raises(ValueError, match="not pegged to USD"):
        buyer._build_payment_requirements(parsed, Decimal("0.01"), asset=address, token_decimals=6)
    for invalid in ["0.0000001", "-1", "NaN"]:
        with pytest.raises(ValueError, match="positive euros"):
            buyer.create_authorization(wallet.address, Decimal(invalid), token_type="eurc")
