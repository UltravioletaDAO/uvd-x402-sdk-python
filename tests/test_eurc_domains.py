"""EURC signs with the EIP-712 domain each contract was deployed with.

Circle did not give EURC the same name on every chain: name() is "Euro Coin"
on Ethereum and Avalanche and "EURC" on Base. An authorization signed under the
wrong name is one the contract rejects. Avalanche was "Euro Coin" here and
"EURC" in the TypeScript SDK until both pinned it.

Each row was read from the contract itself, by eth_call to name(), version()
and DOMAIN_SEPARATOR() on 2026-10-10, at the RPC and block named. The separator
is what settles it: it is the hash of exactly (name, version, chainId, address),
so only the name the contract has hashes to it. These are FiatToken v2.2
proxies, which compute the separator from the stored name on every call: a
later implementation that renamed the token would not turn this offline test
red. The TypeScript SDK pins the same rows (src/eurc-domains.test.ts). Arc's
EURC is pinned by tests/test_arc_eurc.py.
"""
import base64
import json
from decimal import Decimal
from pathlib import Path

import pytest
from eth_account import Account
from eth_account.messages import encode_typed_data

from uvd_x402_sdk import X402Client
from uvd_x402_sdk.networks import get_network
from uvd_x402_sdk.networks.base import NetworkType, get_networks_by_token

MEASURED = [
    # https://api.avax.network/ext/bc/C/rpc, block 97214492
    ("avalanche", 43114, "0xC891EB4cbdEFf6e073e859e987815Ed1505c2ACD", "Euro Coin", "2",
     "094e957ad84a5711a2d13ea2ddcfe3947fe1704472f07bf8a49718f55a65f4b1"),
    # https://mainnet.base.org, block 52429544
    ("base", 8453, "0x60a3E35Cc302bFA44Cb288Bc5a4F316Fdb1adb42", "EURC", "2",
     "ec4dcbded0afd42599589b79220899bbe000bb5ea66d6f1bc176e78d094203bb"),
    # https://ethereum-rpc.publicnode.com, block 26163122
    ("ethereum", 1, "0x1aBaEA1f7C830bD89Acc67eC4af516284b1bC33c", "Euro Coin", "2",
     "99f188f447f0c6eaf68589359cd2ead8c2faaaaee984ab926fdb734d0040073b"),
]

TYPES = {"TransferWithAuthorization": [
    {"name": n, "type": t} for n, t in [("from", "address"), ("to", "address"),
    ("value", "uint256"), ("validAfter", "uint256"),
    ("validBefore", "uint256"), ("nonce", "bytes32")]
]}
# Any message will do: the separator is the hash of the domain alone.
ZERO = {"from": "0x" + "00" * 20, "to": "0x" + "00" * 20, "value": 0,
        "validAfter": 0, "validBefore": 0, "nonce": "0x" + "00" * 32}


@pytest.mark.parametrize("network,chain_id,address,name,version,separator", MEASURED)
def test_registry_domain_is_the_measured_one_and_hashes_to_domain_separator(
    network, chain_id, address, name, version, separator
):
    config = get_network(network)
    token = config.tokens["eurc"]
    assert (config.chain_id, token.address, token.name, token.version) == (
        chain_id, address, name, version)
    domain = {"name": token.name, "version": token.version, "chainId": chain_id,
              "verifyingContract": token.address}
    assert encode_typed_data(domain, TYPES, ZERO).header.hex() == separator


@pytest.mark.parametrize("network,chain_id,address,name,version,separator", MEASURED)
def test_create_authorization_signs_under_that_domain_and_not_the_other_name(
    network, chain_id, address, name, version, separator
):
    wallet = Account.create()
    buyer = X402Client(recipient_address=wallet.address)
    buyer.connect_with_private_key(wallet.key.hex(), chain_name=network)
    header = buyer.create_authorization(wallet.address, Decimal("0.01"), token_type="eurc")
    inner = json.loads(base64.b64decode(header))["payload"]
    domain = {"name": name, "version": version, "chainId": chain_id, "verifyingContract": address}
    signed = encode_typed_data(domain, TYPES, inner["authorization"])
    assert Account.recover_message(signed, signature=inner["signature"]) == wallet.address
    other = "Euro Coin" if name == "EURC" else "EURC"
    wrong = encode_typed_data({**domain, "name": other}, TYPES, inner["authorization"])
    assert Account.recover_message(wrong, signature=inner["signature"]) != wallet.address


def test_every_enabled_evm_network_with_eurc_is_pinned_here_or_by_a_row_of_test_arc_eurc():
    arc_rows = (Path(__file__).parent / "test_arc_eurc.py").read_text(encoding="utf-8")
    measured = {row[0] for row in MEASURED}
    with_eurc = [n for n in get_networks_by_token("eurc") if n.network_type == NetworkType.EVM]
    assert len(with_eurc) > len(measured)
    for network in with_eurc:
        if network.name in measured:
            continue
        row = f'("{network.name}", {network.chain_id}, "{network.tokens["eurc"].address}",'
        assert row in arc_rows, f"EURC on {network.name} is pinned nowhere"
