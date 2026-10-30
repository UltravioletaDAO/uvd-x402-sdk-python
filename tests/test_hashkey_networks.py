"""HashKey USDC domains pinned to eth_call and x402-rs at 38d67a47."""

import base64
import json
from decimal import Decimal

import pytest
from eth_account import Account
from eth_account.messages import encode_typed_data, hash_domain

from uvd_x402_sdk import X402Client, X402Config
from uvd_x402_sdk.networks import (
    get_network,
    get_network_by_chain_id,
    get_supported_tokens,
    normalize_network,
    to_caip2_network,
)
from uvd_x402_sdk.networks.evm import get_usdc_domain_name

# eth_call DOMAIN_SEPARATOR(), 2026-10-30; blocks 29510624 and 35090865.
CASES = [
    (
        "hashkey",
        177,
        "0x054ed45810DbBAb8B27668922D110669c9D88D0a",
        "Bridged USDC",
        "USD Coin",
        "78101f50ab6b299bde76a9973e97b1c9ead7df26f6a40692f4440296a507259d",
        "https://mainnet.hsk.xyz",
        "https://hsk.blockscout.com",
    ),
    (
        "hashkey-testnet",
        133,
        "0x8FE3cB719Ee4410E236Cd6b72ab1fCDC06eF53c6",
        "USD Coin",
        "Bridged USDC",
        "4b73b8fe157f795886e2aa243f2ccddcc00b80eaa9f9b4b368b716895be71d09",
        "https://testnet.hsk.xyz",
        "https://testnet-explorer.hskchain.net",
    ),
]
TRANSFER_TYPES = {
    "TransferWithAuthorization": [
        {"name": name, "type": kind}
        for name, kind in [
            ("from", "address"),
            ("to", "address"),
            ("value", "uint256"),
            ("validAfter", "uint256"),
            ("validBefore", "uint256"),
            ("nonce", "bytes32"),
        ]
    ]
}


@pytest.mark.parametrize("network,chain_id,address,name,wrong,separator,rpc,explorer", CASES)
def test_hashkey_registry_and_domain(
    network, chain_id, address, name, wrong, separator, rpc, explorer
):
    net = get_network(network)
    token = net.tokens["usdc"]
    domain = {
        "name": token.name,
        "version": token.version,
        "chainId": net.chain_id,
        "verifyingContract": token.address,
    }
    assert hash_domain(domain).hex() == separator
    assert hash_domain({**domain, "name": wrong}).hex() != separator
    assert net is get_network(network.upper())
    assert net is get_network_by_chain_id(chain_id)
    assert net.chain_id == chain_id
    assert net.usdc_address == address
    assert net.usdc_domain_name == name
    assert net.usdc_domain_version == "2"
    assert net.usdc_decimals == net.tokens["usdc"].decimals == 6
    assert net.rpc_url == rpc
    assert net.extra_config["explorer"] == explorer
    assert net.extra_config["native_gas_asset"] == "HSK"
    assert net.enabled
    assert net.default_token == "usdc"
    assert get_supported_tokens(network) == ["usdc"]
    assert network in X402Config(recipient_evm=Account.create().address).supported_networks
    assert to_caip2_network(network) == f"eip155:{chain_id}"
    assert normalize_network(f"eip155:{chain_id}") == network
    assert get_usdc_domain_name(network.upper()) == name


@pytest.mark.parametrize("network,chain_id,address,name,wrong,separator,rpc,explorer", CASES)
def test_hashkey_authorization_uses_the_pinned_domain(
    network,
    chain_id,
    address,
    name,
    wrong,
    separator,
    rpc,
    explorer,
):
    wallet = Account.create()
    recipient = Account.create().address
    with X402Client(recipient_address=recipient) as client:
        client.connect_with_private_key(wallet.key.hex(), chain_name=network)
        header = client.create_authorization(
            recipient,
            Decimal("0.000001"),
            chain_name=f"eip155:{chain_id}",
        )
    inner = json.loads(base64.b64decode(header))["payload"]
    assert inner["authorization"]["value"] == "1"
    domain = {"name": name, "version": "2", "chainId": chain_id, "verifyingContract": address}
    message = encode_typed_data(domain, TRANSFER_TYPES, inner["authorization"])
    assert message.header.hex() == separator
    assert Account.recover_message(message, signature=inner["signature"]) == wallet.address
    other = encode_typed_data({**domain, "name": wrong}, TRANSFER_TYPES, inner["authorization"])
    assert Account.recover_message(other, signature=inner["signature"]) != wallet.address
