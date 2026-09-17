"""Native Hedera ledger IDs, distinct from Hedera EVM chain IDs 295/296."""
from uvd_x402_sdk.networks.base import NetworkConfig, NetworkType, TokenConfig, register_network
from uvd_x402_sdk.hedera import HEDERA_NETWORKS

for _name, _info in HEDERA_NETWORKS.items():
    register_network(NetworkConfig(
        name=_name, display_name="Hedera" + (" Testnet" if _name.endswith("testnet") else ""),
        network_type=NetworkType.HEDERA, usdc_address=_info["usdc"], usdc_decimals=6,
        usdc_domain_name="USDC", usdc_domain_version="",  # label only; no EIP-712
        rpc_url=("https://testnet.mirrornode.hedera.com" if _name.endswith("testnet")
                 else "https://mainnet-public.mirrornode.hedera.com"),
        tokens={"usdc": TokenConfig(_info["usdc"], 6, "USDC", ""),
                "hbar": TokenConfig("0.0.0", 8, "HBAR", "", usd_pegged=False)},
        extra_config={"fee_payer": _info["feePayer"], "x402_versions": [2],
                      "schemes": ["exact"], "native_asset": "HBAR"},
    ))
