"""The escrow contract registry: what is deployed on each chain, and its operator.

Plain dicts and no imports, so the modules that read them stay importable on
any install: :mod:`uvd_x402_sdk.advanced_escrow` (which needs web3) and
:mod:`uvd_x402_sdk.escrow_signing` (which needs only eth-account to sign, and
nothing to import). ``advanced_escrow`` imports ``ESCROW_CONTRACTS`` from here
(the same object, so ``advanced_escrow.ESCROW_CONTRACTS`` keeps working).

* ``ESCROW_CONTRACTS``: chain id -> the escrow deployment on it. Addresses
  only; ``tests/fixtures/escrow-calldata-snapshot.json`` pins every row of the
  chains it recorded.
* ``ESCROW_OPERATORS``: chain id -> the PaymentOperator that
  ``AdvancedEscrowClient`` uses on that chain when none is given. A chain
  without a row has no default operator in this SDK.

``escrow_signing.build_escrow_pre_auth`` reads both: on a chain with a row,
an address of the payment config that is not the registered one is refused
before anything is signed.
"""

from __future__ import annotations

# ============================================================
# Multi-chain Escrow Contract Registry
# ============================================================
# Contract addresses from x402r-sdk (A1igator/multichain-config).
# Keys:
#   escrow           - AuthCaptureEscrow contract
#   operator_factory - PaymentOperatorFactory contract
#   token_collector  - TokenCollector contract
#   protocol_fee_config - ProtocolFeeConfig contract
#   refund_request   - RefundRequest contract
#   usdc             - USDC token address

ESCROW_CONTRACTS: dict[int, dict[str, str]] = {
    # ----- Testnets -----
    84532: {  # Base Sepolia
        "escrow": "0x29025c0E9D4239d438e169570818dB9FE0A80873",
        "operator_factory": "0x97d53e63A9CB97556c00BeFd325AF810c9b267B2",
        "token_collector": "0x5cA789000070DF15b4663DB64a50AeF5D49c5Ee0",
        "protocol_fee_config": "0x8F96C493bAC365E41f0315cf45830069EBbDCaCe",
        "refund_request": "0x1C2Ab244aC8bDdDB74d43389FF34B118aF2E90F4",
        "usdc": "0x036CbD53842c5426634e7929541eC2318f3dCF7e",
    },
    11155111: {  # Ethereum Sepolia
        "escrow": "0x320a3c35F131E5D2Fb36af56345726B298936037",
        "operator_factory": "0x32d6AC59BCe8DFB3026F10BcaDB8D00AB218f5b6",
        "token_collector": "0x230fd3A171750FA45db2976121376b7F47Cba308",
        "protocol_fee_config": "0xD979dBfBdA5f4b16AAF60Eaab32A44f352076838",
        "refund_request": "0xc1256Bb30bd0cdDa07D8C8Cf67a59105f2EA1b98",
        "usdc": "0x1c7D4B196Cb0C7B01d743Fbc6116a902379C7238",
    },
    # ----- Mainnets -----
    8453: {  # Base Mainnet
        "escrow": "0xb9488351E48b23D798f24e8174514F28B741Eb4f",
        "operator_factory": "0x3D0837fF8Ea36F417261577b9BA568400A840260",
        "token_collector": "0x48ADf6E37F9b31dC2AAD0462C5862B5422C736B8",
        "protocol_fee_config": "0x59314674BAbb1a24Eb2704468a9cCdD50668a1C6",
        "refund_request": "0x35fb2EFEfAc3Ee9f6E52A9AAE5C9655bC08dEc00",
        "usdc": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
    },
    1: {  # Ethereum Mainnet (Ali redeploy 2026-02-20)
        "escrow": "0x9D4146EF898c8E60B3e865AE254ef438E7cEd2A0",
        "operator_factory": "0x1e52a74cE6b69F04a506eF815743E1052A1BD28F",
        "token_collector": "0x206D4DbB6E7b876e4B5EFAAD2a04e7d7813FB6ba",
        "protocol_fee_config": "0x5b3e33791C1764cF7e2573Bf8116F1D361FD97Cd",
        "refund_request": "0xFa8C4Cb156053b867Ae7489220A29b5939E3Df70",
        "usdc": "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
    },
    10: {  # Optimism
        "escrow": "0x320a3c35F131E5D2Fb36af56345726B298936037",
        "operator_factory": "0x32d6AC59BCe8DFB3026F10BcaDB8D00AB218f5b6",
        "token_collector": "0x230fd3A171750FA45db2976121376b7F47Cba308",
        "protocol_fee_config": "0xD979dBfBdA5f4b16AAF60Eaab32A44f352076838",
        "refund_request": "0xc1256Bb30bd0cdDa07D8C8Cf67a59105f2EA1b98",
        "usdc": "0x0b2C639c533813f4Aa9D7837CAf62653d097Ff85",
    },
    137: {  # Polygon
        "escrow": "0x32d6AC59BCe8DFB3026F10BcaDB8D00AB218f5b6",
        "operator_factory": "0xb33D6502EdBbC47201cd1E53C49d703EC0a660b8",
        "token_collector": "0xc1256Bb30bd0cdDa07D8C8Cf67a59105f2EA1b98",
        "protocol_fee_config": "0xE78648e7af7B1BaDE717FF6E410B922F92adE80f",
        "refund_request": "0xed02d3E5167BCc9582D851885A89b050AB816a56",
        "usdc": "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359",
    },
    42161: {  # Arbitrum
        "escrow": "0x320a3c35F131E5D2Fb36af56345726B298936037",
        "operator_factory": "0x32d6AC59BCe8DFB3026F10BcaDB8D00AB218f5b6",
        "token_collector": "0x230fd3A171750FA45db2976121376b7F47Cba308",
        "protocol_fee_config": "0xD979dBfBdA5f4b16AAF60Eaab32A44f352076838",
        "refund_request": "0xc1256Bb30bd0cdDa07D8C8Cf67a59105f2EA1b98",
        "usdc": "0xaf88d065e77c8cC2239327C5EDb3A432268e5831",
    },
    42220: {  # Celo
        "escrow": "0x320a3c35F131E5D2Fb36af56345726B298936037",
        "operator_factory": "0x32d6AC59BCe8DFB3026F10BcaDB8D00AB218f5b6",
        "token_collector": "0x230fd3A171750FA45db2976121376b7F47Cba308",
        "protocol_fee_config": "0xD979dBfBdA5f4b16AAF60Eaab32A44f352076838",
        "refund_request": "0xc1256Bb30bd0cdDa07D8C8Cf67a59105f2EA1b98",
        "usdc": "0xcebA9300f2b948710d2653dD7B07f33A8B32118C",
    },
    143: {  # Monad
        "escrow": "0x320a3c35F131E5D2Fb36af56345726B298936037",
        "operator_factory": "0x32d6AC59BCe8DFB3026F10BcaDB8D00AB218f5b6",
        "token_collector": "0x230fd3A171750FA45db2976121376b7F47Cba308",
        "protocol_fee_config": "0xD979dBfBdA5f4b16AAF60Eaab32A44f352076838",
        "refund_request": "0xc1256Bb30bd0cdDa07D8C8Cf67a59105f2EA1b98",
        "usdc": "0x754704Bc059F8C67012fEd69BC8A327a5aafb603",
    },
    43114: {  # Avalanche
        "escrow": "0x320a3c35F131E5D2Fb36af56345726B298936037",
        "operator_factory": "0x32d6AC59BCe8DFB3026F10BcaDB8D00AB218f5b6",
        "token_collector": "0x230fd3A171750FA45db2976121376b7F47Cba308",
        "protocol_fee_config": "0xD979dBfBdA5f4b16AAF60Eaab32A44f352076838",
        "refund_request": "0xc1256Bb30bd0cdDa07D8C8Cf67a59105f2EA1b98",
        "usdc": "0xB97EF9Ef8734C71904D8002F8b6Bc66Dd9c48a6E",
    },
    # ----- CREATE3 Networks (new deployments) -----
    1187947933: {  # SKALE Base (gasless L3, CREDIT gas token)
        "escrow": "0xBC151792f80C0EB1973d56b0235e6bee2A60e245",
        "operator_factory": "0x3Cd5c76Fefe46CB07788Ee8f80B93B20D81941D4",
        "token_collector": "0x9A12A116a44636F55c9e135189A1321Abcfe2f30",
        "protocol_fee_config": "0xf62788834C99B2E85a6891C0b46D1EB996f8f596",
        "refund_request": "0x69e9BF2b40Ed472b55E47e9D4205d93Ed673093F",
        "usdc": "0x85889c8c714505E0c94b30fcfcF64fE3Ac8FCb20",
    },
    # ----- Canonical x402r deployments -----
    # Same addresses on every chain (CREATE2). Sources: BackTrackCo/x402r-sdk
    # packages/core/src/config/index.ts @ bbfec12c and BackTrackCo/x402r-contracts
    # deployments/canonical-v1.0.1.json + canonical-v1.0.2.json @ c5223eaa.
    # Registry key <- upstream contract:
    #   escrow           <- AuthCaptureEscrow (commerce-payments v1.0.0)
    #   operator_factory <- PaymentOperatorFactory v1.0.2
    #   token_collector  <- ERC3009PaymentCollector
    #   refund_request   <- RefundRequestFactory v1.0.1
    # Code at each one read on both chains: tests/fixtures/arc-escrow-d.json.
    5042: {  # Arc
        "escrow": "0xBdEA0D1bcC5966192B070Fdf62aB4EF5b4420cff",
        "operator_factory": "0xc24153B7ED8DC03e551F29DDEeA5CadFe57e2716",
        "token_collector": "0x0E3dF9510de65469C4518D7843919c0b8C7A7757",
        "protocol_fee_config": "0xBe2d24614F339a1eB103A399F93AA2a39Ca815Bc",
        "refund_request": "0xe971C674fD5c3462023f3F891dF6289DFbC9CEFC",
        "usdc": "0x3600000000000000000000000000000000000000",
    },
    5042002: {  # Arc Testnet
        "escrow": "0xBdEA0D1bcC5966192B070Fdf62aB4EF5b4420cff",
        "operator_factory": "0xc24153B7ED8DC03e551F29DDEeA5CadFe57e2716",
        "token_collector": "0x0E3dF9510de65469C4518D7843919c0b8C7A7757",
        "protocol_fee_config": "0xBe2d24614F339a1eB103A399F93AA2a39Ca815Bc",
        "refund_request": "0xe971C674fD5c3462023f3F891dF6289DFbC9CEFC",
        "usdc": "0x3600000000000000000000000000000000000000",
    },
}

# ============================================================
# The PaymentOperator of each chain
# ============================================================
# ESCROW_CONTRACTS is addresses of shared deployments; the operator is not one
# of them (each marketplace deploys its own from the factory), so it has its
# own table, as the generation has (advanced_escrow.ESCROW_GENERATIONS), and
# the snapshot of ESCROW_CONTRACTS stays as it was recorded.
#
# A row is an operator this SDK already used as a default:
# AdvancedEscrowClient resolves it when no ``operator_address`` is given
# (tests/test_escrow_signing_registry.py pins that the two agree). A chain without a
# row has no default operator here: AdvancedEscrowClient asks for one, and
# escrow_signing signs the payment config's operator with a warning.
ESCROW_OPERATORS: dict[int, str] = {
    # Base: Fase 5 PaymentOperator (advanced_escrow.BASE_MAINNET_CONTRACTS),
    # the operator of Execution Market's Base config (tests/fixtures/escrow-preauth.json).
    8453: "0x271f9fa7f8907aCf178CCFB470076D9129D8F0Eb",
    # SKALE Base: EM PaymentOperator Fase 5 (1300bps fee, facilitator-as-arbiter).
    1187947933: "0x43E46d4587fCCc382285C52012227555ed78D183",
    # Arc / Arc Testnet: EM PaymentOperator, same address on both
    # (computeAddress on the v1.0.2 factory, tests/fixtures/arc-escrow-d.json).
    5042: "0x0258472A1410Ac3Ad720f1BC83f22B3c0af1Fd9D",
    5042002: "0x0258472A1410Ac3Ad720f1BC83f22B3c0af1Fd9D",
}
