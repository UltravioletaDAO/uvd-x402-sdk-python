"""Payer-funded, unsigned NEP-145 storage transactions for Arras and native USDC.

The payer signs a normal NEAR transaction and pays both deposit and gas. These
calls never go through a facilitator delegate. Deposits are integer yoctoNEAR;
query storage_balance_bounds on the target before choosing a live amount.
"""

from __future__ import annotations

from typing import Literal, TypedDict

from .near_escrow import NEAR_ESCROW_CONTRACTS, NEAR_ESCROW_TOKENS, Unsigned, _account, _uint

NEAR_ARRAS_STORAGE_DEPOSIT = "50000000000000000000000"
NEAR_USDC_STORAGE_DEPOSIT = "1250000000000000000000"


class _StorageArgs(TypedDict):
    account_id: str
    registration_only: bool


class _StorageParams(TypedDict):
    methodName: Literal["storage_deposit"]
    args: _StorageArgs
    gas: str
    deposit: str


class _StorageAction(TypedDict):
    type: Literal["FunctionCall"]
    params: _StorageParams


class NearStorageTransaction(TypedDict):
    signerId: str
    receiverId: str
    actions: list[_StorageAction]


def _storage(
    network: str,
    payer_id: str,
    account_id: str | None,
    receiver_id: str,
    deposit: Unsigned,
    registration_only: bool,
) -> NearStorageTransaction:
    if network not in ("near:mainnet", "near:testnet"):
        raise ValueError("unsupported NEAR network")
    _account(payer_id, "payer_id")
    account_id = payer_id if account_id is None else account_id
    _account(account_id, "account_id")
    _account(receiver_id, "receiver_id")
    if receiver_id == "intents.near":
        raise ValueError("intents.near is not an allowed storage target")
    amount = _uint(deposit, 128, "deposit")
    if amount == 0:
        raise ValueError("storage deposit must be positive")
    return {
        "signerId": payer_id,
        "receiverId": receiver_id,
        "actions": [
            {
                "type": "FunctionCall",
                "params": {
                    "methodName": "storage_deposit",
                    "args": {"account_id": account_id, "registration_only": registration_only},
                    "gas": "30000000000000",
                    "deposit": str(amount),
                },
            }
        ],
    }


def build_arras_storage_deposit(
    *,
    network: str,
    payer_id: str,
    escrow_contract: str,
    account_id: str | None = None,
    deposit: Unsigned = NEAR_ARRAS_STORAGE_DEPOSIT,
) -> NearStorageTransaction:
    """Fund Arras storage with registration_only=False so surplus remains usable.

    escrow_contract must be registered for network in NEAR_ESCROW_CONTRACTS, never inferred
    from an account parent. The 0.05 NEAR default funds roughly five payments,
    not a guarantee of capacity. This builder neither signs nor submits it.
    """
    if network not in NEAR_ESCROW_CONTRACTS:
        raise ValueError("unsupported NEAR network")
    if escrow_contract not in NEAR_ESCROW_CONTRACTS[network]:
        raise ValueError("escrow_contract is not registered for this network")
    return _storage(network, payer_id, account_id, escrow_contract, deposit, False)


def build_usdc_storage_deposit(
    *,
    network: str,
    payer_id: str,
    account_id: str | None = None,
    deposit: Unsigned = NEAR_USDC_STORAGE_DEPOSIT,
) -> NearStorageTransaction:
    """Register account_id (payer by default) in network-native USDC; refund surplus."""
    if network not in ("near:mainnet", "near:testnet"):
        raise ValueError("unsupported NEAR network")
    return _storage(network, payer_id, account_id, NEAR_ESCROW_TOKENS[network], deposit, True)
