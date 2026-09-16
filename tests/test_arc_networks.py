"""Arc payments retain six decimals and cannot cross network signing domains."""
import base64
import json
from decimal import Decimal

import pytest
from eth_account import Account
from eth_account.messages import encode_typed_data

from uvd_x402_sdk import X402Client, X402Config
from uvd_x402_sdk.networks import get_network, normalize_network, to_caip2_network


@pytest.mark.parametrize("network,chain_id,other_chain_id", [
    ("arc", 5042, 5042002), ("arc-testnet", 5042002, 5042),
])
@pytest.mark.parametrize("amount,atomic", [("0.000001", "1"), ("0.01", "10000")])
def test_payment_signature_amount_and_network_isolation(
    network, chain_id, other_chain_id, amount, atomic
):
    wallet = Account.create()
    recipient = Account.create().address
    client = X402Client(config=X402Config(recipient_evm=recipient, supported_networks=[network]))
    client.connect_with_private_key(wallet.key.hex(), chain_name=network)
    caip2 = f"eip155:{chain_id}"
    assert normalize_network(caip2) == network
    assert to_caip2_network(network) == caip2
    net = get_network(network)
    assert net.usdc_decimals == net.tokens["usdc"].decimals == 6
    assert net.extra_config["native_gas_decimals"] == 18
    assert network in X402Config(recipient_evm=recipient).supported_networks
    header = client.create_authorization(recipient, Decimal(amount), chain_name=caip2)
    inner = json.loads(base64.b64decode(header))["payload"]
    assert inner["authorization"]["value"] == atomic
    domain = {"name": "USDC", "version": "2", "chainId": chain_id,
              "verifyingContract": "0x3600000000000000000000000000000000000000"}
    types = {"TransferWithAuthorization": [
        {"name": name, "type": kind} for name, kind in [
            ("from", "address"), ("to", "address"), ("value", "uint256"),
            ("validAfter", "uint256"), ("validBefore", "uint256"), ("nonce", "bytes32"),
        ]
    ]}
    def recover():
        return Account.recover_message(
            encode_typed_data(domain, types, inner["authorization"]),
            signature=inner["signature"],
        )
    assert recover() == wallet.address
    domain["chainId"] = other_chain_id
    assert recover() != wallet.address


@pytest.mark.parametrize("network,chain_id", [("arc", 5042), ("arc-testnet", 5042002)])
def test_v2_client_echoes_arc_offer_and_signs_micro_usdc(network, chain_id):
    wallet = Account.create()
    client = X402Client(recipient_address=wallet.address)
    client.connect_with_private_key(wallet.key.hex(), chain_name=network)
    accepted = {
        "scheme": "exact", "network": f"eip155:{chain_id}", "amount": "1",
        "asset": "0x3600000000000000000000000000000000000000",
        "payTo": wallet.address, "maxTimeoutSeconds": 300,
        "extra": {"name": "USDC", "version": "2"},
    }
    header = client.create_authorization(wallet.address, Decimal("0.000001"),
                                         x402_version=2, accepted=accepted,
                                         resource={"url": "https://example.com/arc"})
    payload = json.loads(base64.b64decode(header))
    assert payload["x402Version"] == 2
    assert payload["accepted"] == accepted
    assert payload["payload"]["authorization"]["value"] == "1"
