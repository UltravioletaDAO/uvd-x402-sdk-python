"""Monad's USDT0 EIP-712 name is ASCII, unlike Arbitrum and Optimism."""

import base64
import json
from decimal import Decimal

from eth_account import Account
from eth_account.messages import encode_typed_data, hash_domain

from tests.test_hashkey_networks import TRANSFER_TYPES
from uvd_x402_sdk import X402Client
from uvd_x402_sdk.networks import get_network

# eth_call DOMAIN_SEPARATOR(), Monad block 117915137, 2026-10-30.
# Reported by hey-research-lab in uvd-x402-sdk-typescript PR #62.
SEPARATOR = "101a90213d0b0d9d607fe3b94dcb41e91f20ab1432d4cd23ae9f820c0968452a"


def test_monad_usdt0_separator_and_signature() -> None:
    net = get_network("monad")
    token = net.tokens["usdt"]
    domain = {
        "name": token.name,
        "version": token.version,
        "chainId": net.chain_id,
        "verifyingContract": token.address,
    }
    assert hash_domain(domain).hex() == SEPARATOR
    old_domain = {**domain, "name": "USD\u20ae0"}
    assert hash_domain(old_domain).hex() != SEPARATOR

    wallet = Account.create()
    recipient = Account.create().address
    with X402Client(recipient_address=recipient) as client:
        client.connect_with_private_key(wallet.key.hex(), chain_name="monad")
        header = client.create_authorization(recipient, Decimal("0.000001"), token_type="usdt")
    inner = json.loads(base64.b64decode(header))["payload"]
    assert inner["token"]["eip712"] == {"name": "USDT0", "version": "1"}
    assert inner["authorization"]["value"] == "1"
    message = encode_typed_data(domain, TRANSFER_TYPES, inner["authorization"])
    assert message.header.hex() == SEPARATOR
    assert Account.recover_message(message, signature=inner["signature"]) == wallet.address
    old_message = encode_typed_data(old_domain, TRANSFER_TYPES, inner["authorization"])
    assert Account.recover_message(old_message, signature=inner["signature"]) != wallet.address


def test_other_usdt_domains_are_unchanged() -> None:
    assert get_network("arbitrum").tokens["usdt"].name == "USD\u20ae0"
    assert get_network("optimism").tokens["usdt"].name == "USD\u20ae0"
    assert get_network("celo").tokens["usdt"].name == "Tether USD"
