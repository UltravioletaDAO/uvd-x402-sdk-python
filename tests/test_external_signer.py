"""connect_with_signer: the key can live outside the process.

The SDK could previously only sign with a raw private key loaded into memory, which rules out
every setup where the key is deliberately somewhere else — HSM, KMS, MPC, or a delegated
agentic wallet. Those are the production setups.

The load-bearing test here is `test_both_paths_produce_the_same_signature`: if the local and
remote seams ever drift, the SDK silently produces two different authorizations for the same
payment, and only one of them settles.
"""
from __future__ import annotations

import base64
import json

import pytest

from uvd_x402_sdk import X402Client

# Well-known test key (Hardhat account #0). NOT a real wallet.
TEST_KEY = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
TEST_ADDR = "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266"


class RemoteSigner:
    """A signer that holds no key here — it delegates, like an HSM would.

    For the equivalence test it delegates to eth_account so the bytes are comparable; a real
    one would call out to a remote service. The SDK cannot tell the difference, which is the
    whole point of the seam.
    """

    def __init__(self, private_key: str):
        from eth_account import Account
        self._acct = Account.from_key(private_key)
        self.calls = 0

    @property
    def address(self) -> str:
        return self._acct.address

    def sign_typed_data(self, domain, types, message) -> str:
        from eth_account.messages import encode_typed_data
        self.calls += 1
        signable = encode_typed_data(
            domain_data=domain, message_types=types, message_data=message
        )
        sig = self._acct.sign_message(signable).signature.hex()
        return sig if sig.startswith("0x") else "0x" + sig


def _payload(header: str) -> dict:
    return json.loads(base64.b64decode(header))


def _args():
    return {"pay_to": "0x1111111111111111111111111111111111111111",
            "amount_usd": "0.01", "chain_name": "base"}


def test_connect_with_signer_reports_the_address_and_connects():
    c = X402Client(recipient_address="0x1234567890123456789012345678901234567890")
    assert c.is_connected is False
    addr = c.connect_with_signer(RemoteSigner(TEST_KEY), chain_name="base")
    assert addr == TEST_ADDR
    assert c.is_connected is True
    assert c.address == TEST_ADDR


def test_both_paths_produce_the_same_signature():
    """THE test. A remote signer must be byte-identical to a local key — if the two seams ever
    drift, the SDK silently produces two different authorizations for the same payment and only
    one of them settles.

    It compares the seam directly instead of two full headers, because `create_authorization`
    draws a random nonce and a time-based window: comparing headers would be comparing
    randomness, and the assertion that matters is about the SIGNATURE.
    """
    dominio = {"name": "USDC", "version": "2", "chainId": 8453,
               "verifyingContract": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"}
    tipos = {"TransferWithAuthorization": [
        {"name": "from", "type": "address"}, {"name": "to", "type": "address"},
        {"name": "value", "type": "uint256"}, {"name": "validAfter", "type": "uint256"},
        {"name": "validBefore", "type": "uint256"}, {"name": "nonce", "type": "bytes32"}]}
    mensaje = {"from": TEST_ADDR, "to": "0x1111111111111111111111111111111111111111",
               "value": 10000, "validAfter": 0, "validBefore": 9999999999,
               "nonce": "0x" + "11" * 32}

    local = X402Client(recipient_address="0x1234567890123456789012345678901234567890")
    local.connect_with_private_key(TEST_KEY, chain_name="base")
    remoto = X402Client(recipient_address="0x1234567890123456789012345678901234567890")
    remoto.connect_with_signer(RemoteSigner(TEST_KEY), chain_name="base")

    firma_local = local._sign_typed_data(dominio, tipos, mensaje)
    firma_remota = remoto._sign_typed_data(dominio, tipos, mensaje)

    assert firma_local == firma_remota, "the two seams produced different signatures"
    assert firma_local.startswith("0x") and len(firma_local) == 132   # 0x + 65 bytes


def test_both_paths_produce_a_wellformed_header():
    """Y el header completo sigue armándose igual por los dos caminos (nonce aparte)."""
    for c in (X402Client(recipient_address="0x1234567890123456789012345678901234567890"),
              X402Client(recipient_address="0x1234567890123456789012345678901234567890")):
        pass
    local = X402Client(recipient_address="0x1234567890123456789012345678901234567890")
    local.connect_with_private_key(TEST_KEY, chain_name="base")
    remoto = X402Client(recipient_address="0x1234567890123456789012345678901234567890")
    remoto.connect_with_signer(RemoteSigner(TEST_KEY), chain_name="base")

    for cliente in (local, remoto):
        p = _payload(cliente.create_authorization(**_args()))
        assert p["payload"]["signature"].startswith("0x")
        assert len(p["payload"]["signature"]) == 132
        assert p["payload"]["authorization"]["from"] == TEST_ADDR
        assert p["scheme"] == "exact"


def test_the_external_signer_is_actually_the_one_signing():
    """Guards against the seam silently falling back to a local key."""
    s = RemoteSigner(TEST_KEY)
    c = X402Client(recipient_address="0x1234567890123456789012345678901234567890")
    c.connect_with_signer(s, chain_name="base")
    assert s.calls == 0
    c.create_authorization(**_args())
    assert s.calls == 1


@pytest.mark.parametrize("malo, motivo", [
    (object(), "no address"),
    (type("S", (), {"address": "nope", "sign_typed_data": lambda *a: "0x"})(), "bad address"),
    (type("S", (), {"address": TEST_ADDR})(), "no sign_typed_data"),
])
def test_a_bad_signer_is_rejected_at_connect_time_not_at_signing_time(malo, motivo):
    """Checked UP FRONT on purpose: a missing method discovered while signing fails after the
    caller already believes it is connected — and, for a payment, possibly mid-flow."""
    c = X402Client(recipient_address="0x1234567890123456789012345678901234567890")
    with pytest.raises(TypeError):
        c.connect_with_signer(malo)
    assert c.is_connected is False, motivo


def test_a_signer_returning_bytes_is_accepted():
    """Remote services commonly hand back raw bytes rather than a hex string."""
    class Bytes(RemoteSigner):
        def sign_typed_data(self, domain, types, message):
            return bytes.fromhex(super().sign_typed_data(domain, types, message)[2:])

    c = X402Client(recipient_address="0x1234567890123456789012345678901234567890")
    c.connect_with_signer(Bytes(TEST_KEY), chain_name="base")
    p = _payload(c.create_authorization(**_args()))
    assert len(p["payload"]["signature"]) == 132


def test_a_signer_returning_a_non_signature_is_rejected_loudly():
    """A None slipping through would be base64'd into the header and rejected by the
    facilitator with an opaque error, after the request already went out."""
    class Roto(RemoteSigner):
        def sign_typed_data(self, domain, types, message):
            return None

    c = X402Client(recipient_address="0x1234567890123456789012345678901234567890")
    c.connect_with_signer(Roto(TEST_KEY), chain_name="base")
    with pytest.raises(TypeError, match="hex string"):
        c.create_authorization(**_args())


def test_create_authorization_without_any_signer_names_both_options():
    c = X402Client(recipient_address="0x1234567890123456789012345678901234567890")
    with pytest.raises(RuntimeError, match="connect_with_signer"):
        c.create_authorization(**_args())
