"""Offline exact NEAR FT delegates; reuse the Arras NEP-366 encoder and verifier."""

from __future__ import annotations

from dataclasses import dataclass

from .near_escrow import (
    NEAR_ESCROW_TOKENS,
    NearEscrowDelegateOptions,
    NearLocalSigner,
    Unsigned,
    _account,
    _json,
    _sign_delegate,
    _uint,
)


@dataclass(frozen=True)
class NearExactDelegateOptions:
    """Trusted network, FullAccess payer key, NEXT nonce and expiry BLOCK height.

    The caller must enforce its recipient/code policy and freshness against the
    chain. This offline helper cannot query access-key permissions or code_hash.
    Gas is atomic gas units, not TGas; default and ceiling are 30 TGas.
    """

    network: str
    signer: NearLocalSigner
    sender_id: str
    nonce: Unsigned
    max_block_height: Unsigned
    gas: Unsigned = 30_000_000_000_000


def sign_exact_near_delegate(
    options: NearExactDelegateOptions,
    *,
    token: str,
    pay_to: str,
    amount: Unsigned,
) -> dict:
    """Return x402 v2 exact payment with base64 SignedDelegateAction.

    Signs exactly one ft_transfer on network-native USDC, deposit 1 yoctoNEAR.
    No nonce is allocated, no network is contacted, no transaction is submitted.
    """
    network, signer, sender = options.network, options.signer, options.sender_id
    if network not in ("near:mainnet", "near:testnet"):
        raise ValueError("unsupported NEAR network")
    if token != NEAR_ESCROW_TOKENS[network]:
        raise ValueError("token does not match network USDC")
    _account(sender, "sender_id")
    _account(pay_to, "pay_to")
    if pay_to == "intents.near":
        raise ValueError("intents.near is not an allowed payee")
    nonce = _uint(options.nonce, 64, "nonce")
    height = _uint(options.max_block_height, 64, "max_block_height")
    gas = _uint(options.gas, 64, "gas")
    value = _uint(amount, 128, "amount")
    if nonce == 0 or height == 0 or value == 0:
        raise ValueError("nonce, max_block_height and amount must be positive")
    if nonce >= height * 1_000_000:
        raise ValueError("nonce must be below max_block_height * 1_000_000")
    if not 0 < gas <= 30_000_000_000_000:
        raise ValueError("exact gas outside 1..30 TGas ceiling (atomic units)")
    delegate = {
        "sender_id": sender,
        "receiver_id": token,
        "nonce": nonce,
        "max_block_height": height,
        "actions": [
            {
                "method_name": "ft_transfer",
                "args": _json(
                    {
                        "receiver_id": pay_to,
                        "amount": str(value),
                    }
                ),
                "gas": gas,
                "deposit": 1,
            }
        ],
    }
    context = NearEscrowDelegateOptions(
        network=network,
        signer=signer,
        sender_id=sender,
        escrow_contract=token,
        nonce=nonce,
        max_block_height=height,
    )
    payment = _sign_delegate(context, delegate)
    payment["scheme"] = "exact"
    return payment
