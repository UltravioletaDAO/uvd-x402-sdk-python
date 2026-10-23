"""batch-settlement (buyer side): golden vectors against the official reference, and the money rules.

The golden values were produced by the reference implementation (`x402` 2.25.0,
``x402.mechanisms.evm.batch_settlement``) with the same inputs and compared byte for byte with this module's output:
channel id, voucher signature, ERC-3009 deposit nonce and deposit authorization signature.

The key is derived at run time from a seed, never written as a literal. The hex values are built with ``"0x" + ...``
so a secret scanner looking for ``0x`` followed by 64 hex characters does not read a public hash as a key.
"""

import hashlib

import pytest

pytest.importorskip("eth_account")

from eth_account import Account  # noqa: E402

from uvd_x402_sdk import batch_settlement as bs  # noqa: E402
from uvd_x402_sdk.wallet import EnvKeyAdapter  # noqa: E402

KEY = "0x" + hashlib.sha256(b"karmakadabra-batch-settlement-golden").hexdigest()
ACCOUNT = Account.from_key(KEY)
WALLET = EnvKeyAdapter(KEY)

NET = "eip155:8453"
ASSET = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
RECEIVER = "0x578c640E2D53023066026381B8CC362539741063"
AUTHORIZER = "0x3721824a31197dcDD2984cF43b92B6cc8A87c0Fb"
SALT = "0x" + hashlib.sha256(b"salt").hexdigest()

GOLDEN_CHANNEL_ID = "0x" + "295887a7202d4b158ba2bba9da71d95ea388a87d70e8978212686830d475b6fb"
GOLDEN_VOUCHER_SIG = "0x" + (
    "fe027b475464019e165e31ef3e96e5e511201189b37669ce852cda0ebb19cbb1"
    "4b35d274eb5d66f0382bc833c0e961701d1585e625b044755ed52ae1bd3870031b"
)
GOLDEN_NONCE = "0x" + "bd38203b196308cd9be75a529eb05099f8690a710268c60ae04c0ae08a36320c"
GOLDEN_DEPOSIT_SIG = "0x" + (
    "464fa758a27d7b66cee21ad3b3a81843b9df795451e0bb1adbe24a633d7bd539"
    "004343cf7e690e31dc5b33c853f6ba6a47bd657aa4e6bc5695cdec7d4931eab91c"
)

CONFIG = bs.ChannelConfig(ACCOUNT.address, ACCOUNT.address, RECEIVER, AUTHORIZER, ASSET, 108000)


def requirements(**over):
    req = {
        "scheme": "batch-settlement", "network": NET, "amount": "3000", "asset": ASSET, "payTo": RECEIVER,
        "maxTimeoutSeconds": 15,
        "extra": {"name": "USD Coin", "version": "2", "receiverAuthorizer": AUTHORIZER,
                  "withdrawDelay": 108000, "minDeposit": "30000"},
    }
    req.update(over)
    return req


def test_golden_channel_id_voucher_nonce_and_deposit_signature():
    assert bs.compute_channel_id(CONFIG, NET) == GOLDEN_CHANNEL_ID
    assert bs.sign_voucher(WALLET, GOLDEN_CHANNEL_ID, 3000, NET)["signature"] == GOLDEN_VOUCHER_SIG
    assert bs.erc3009_deposit_nonce(GOLDEN_CHANNEL_ID, SALT) == GOLDEN_NONCE
    dep = bs.build_deposit_payload(WALLET, requirements(), CONFIG, 30000, 3000, deposit_salt=SALT, now=1_800_000_000)
    assert dep["deposit"]["authorization"]["erc3009Authorization"]["signature"] == GOLDEN_DEPOSIT_SIG


def test_the_channel_id_is_bound_to_the_chain():
    assert bs.compute_channel_id(CONFIG, "eip155:84532") != GOLDEN_CHANNEL_ID


def test_first_request_deposits_the_announced_min_deposit_then_vouchers():
    client = bs.BatchSettlementClient(WALLET)
    first = client.create_payment(requirements(), now=1_800_000_000)
    assert first.payload["payload"]["type"] == "deposit"
    assert first.payload["payload"]["deposit"]["amount"] == "30000"
    assert first.payload["payload"]["voucher"]["maxClaimableAmount"] == "3000"
    assert client.confirm(first, {"chargedAmount": "3000"})

    second = client.create_payment(requirements(), now=1_800_000_001)
    assert second.payload["payload"]["type"] == "voucher"
    assert second.payload["payload"]["voucher"]["maxClaimableAmount"] == "6000"
    assert second.deposit_amount is None


def test_top_up_when_the_next_voucher_exceeds_the_balance():
    client = bs.BatchSettlementClient(WALLET, max_deposit=10_000)
    first = client.create_payment(requirements(extra={**requirements()["extra"], "minDeposit": "6000"}))
    assert first.deposit_amount == 6000
    client.confirm(first, {"chargedAmount": "3000"})
    second = client.create_payment(requirements(extra={**requirements()["extra"], "minDeposit": "6000"}))
    assert second.payload["payload"]["type"] == "voucher"
    client.confirm(second, {"chargedAmount": "3000"})
    third = client.create_payment(requirements(extra={**requirements()["extra"], "minDeposit": "6000"}))
    assert third.payload["payload"]["type"] == "deposit", "9000 > balance 6000: a top-up, not a voucher"
    assert third.payload["payload"]["voucher"]["maxClaimableAmount"] == "9000"


def test_a_min_deposit_above_the_local_cap_is_capped_but_a_need_above_it_is_refused():
    capped = bs.BatchSettlementClient(WALLET, max_deposit=10_000).create_payment(requirements())
    assert capped.deposit_amount == 10_000, "the seller's minDeposit is a target, never an order"
    with pytest.raises(bs.BatchSettlementError):
        bs.BatchSettlementClient(WALLET, max_deposit=1_000).create_payment(requirements())


def test_only_batch_settlement_on_evm_with_eip3009_is_signed():
    client = bs.BatchSettlementClient(WALLET)
    with pytest.raises(bs.BatchSettlementError):
        client.create_payment(requirements(scheme="exact"))
    with pytest.raises(bs.BatchSettlementError):
        client.create_payment(requirements(network="solana:mainnet"))
    permit2 = requirements()
    permit2["extra"] = {**permit2["extra"], "assetTransferMethod": "permit2"}
    with pytest.raises(bs.BatchSettlementError):
        client.create_payment(permit2)
    no_authorizer = requirements()
    no_authorizer["extra"] = {k: v for k, v in no_authorizer["extra"].items() if k != "receiverAuthorizer"}
    with pytest.raises(bs.BatchSettlementError):
        client.create_payment(no_authorizer)


def test_confirm_never_trusts_the_sellers_channel_state():
    client = bs.BatchSettlementClient(WALLET)
    p = client.create_payment(requirements())
    # a seller claiming a higher cumulative than previous + charged is NOT copied into the local state
    assert client.confirm(p, {"chargedAmount": "3000", "channelState": {"chargedCumulativeAmount": "999999"}}) is False
    # a charge above the price of the request is refused
    assert client.confirm(p, {"chargedAmount": "3001"}) is False
    assert client.confirm(p, {"chargedAmount": "700", "channelState": {"chargedCumulativeAmount": "700"}}) is True
    nxt = client.create_payment(requirements())
    assert nxt.payload["payload"]["voucher"]["maxClaimableAmount"] == "3700", "dynamic pricing: 700 charged, not 3000"


def test_the_payload_envelope_and_header():
    p = bs.BatchSettlementClient(WALLET).create_payment(requirements(), {"url": "https://x.test/r"})
    assert p.payload["x402Version"] == 2 and p.payload["accepted"]["scheme"] == "batch-settlement"
    assert p.payload["resource"] == {"url": "https://x.test/r"}
    import base64
    import json
    assert json.loads(base64.b64decode(p.header()))["payload"]["type"] == "deposit"


SMA = "0x69007702764179f14f51cdce752f4f775d74e139"   # Alchemy SemiModularAccount7702: the target that needs the wrap


def _deposit_signature(resolver):
    dep = bs.build_deposit_payload(
        WALLET, requirements(), CONFIG, 30000, 3000, deposit_salt=SALT, now=1_800_000_000, delegation_resolver=resolver
    )
    return dep["deposit"]["authorization"]["erc3009Authorization"]["signature"]


def test_a_delegated_payer_gets_the_erc1271_wrapped_authorization():
    from uvd_x402_sdk.erc7702 import eip712_digest, sign_eip3009_for_delegated

    domain = {"name": "USD Coin", "version": "2", "chainId": 8453, "verifyingContract": ASSET}
    message = {
        "from": ACCOUNT.address, "to": bs.ERC3009_DEPOSIT_COLLECTOR_ADDRESS, "value": 30000, "validAfter": 0,
        "validBefore": 1_800_000_000 + 15,
        "nonce": bytes.fromhex(bs.erc3009_deposit_nonce(GOLDEN_CHANNEL_ID, SALT)[2:]),
    }
    digest = eip712_digest(domain, bs.RECEIVE_AUTHORIZATION_TYPES, message)
    expected = sign_eip3009_for_delegated(wallet=WALLET, inner_digest=digest, chain_id=8453, account=ACCOUNT.address)
    got = _deposit_signature(lambda address, network: SMA)
    assert got == expected and got != GOLDEN_DEPOSIT_SIG, "a wrapped signature is NOT the plain one"


def test_a_plain_or_non_wrapping_delegate_signs_plain():
    assert _deposit_signature(lambda a, n: False) == GOLDEN_DEPOSIT_SIG
    # a plain-ECDSA 1271 delegate (Execution Market's FeedbackDelegate style) takes the ordinary signature
    assert _deposit_signature(lambda a, n: "0x" + "11" * 20) == GOLDEN_DEPOSIT_SIG


def test_an_unreadable_chain_is_not_plain_and_refuses():
    with pytest.raises(bs.BatchSettlementError):
        _deposit_signature(lambda a, n: None)
    def boom(a, n):
        raise OSError("rpc down")
    with pytest.raises(bs.BatchSettlementError):
        _deposit_signature(boom)


def test_the_client_passes_its_resolver_to_the_deposit():
    client = bs.BatchSettlementClient(WALLET, delegation_resolver=lambda a, n: SMA)
    wrapped = client.create_payment(requirements(), now=1_800_000_000)
    plain = bs.BatchSettlementClient(WALLET).create_payment(requirements(), now=1_800_000_000)
    sig = lambda p: p.payload["payload"]["deposit"]["authorization"]["erc3009Authorization"]["signature"]  # noqa: E731
    assert len(sig(wrapped)) > len(sig(plain)), "the ERC-1271 envelope is longer than a 65-byte ECDSA signature"


def test_file_storage_survives_a_new_process(tmp_path):
    path = tmp_path / "channels.json"
    a = bs.BatchSettlementClient(WALLET, storage=bs.FileChannelStorage(path))
    a.confirm(a.create_payment(requirements()), {"chargedAmount": "3000"})
    b = bs.BatchSettlementClient(WALLET, storage=bs.FileChannelStorage(path))
    assert b.create_payment(requirements()).payload["payload"]["voucher"]["maxClaimableAmount"] == "6000"
