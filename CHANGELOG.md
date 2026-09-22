# Changelog

## [Unreleased]

- Docs: EURC on Arc mainnet is confirmed with funded payments through the public facilitator (x402 v1 and v2, 2026-09-22, hashes in `docs/networks/arc.md`); Arc testnet funded acceptance stays pending. Remove three stale notes that still said EURC was absent from Arc (`CLAUDE.md`, the `networks` docstring, the README `get_networks_by_token` example output); `tests/test_readme_eurc_networks.py` pins that printed output to the registry. No runtime change.

## [0.88.0] - 2026-09-17

- Add portable signed facilitator receipts for Arc exact USDC/EURC and Hedera USDC, including both mainnet and testnet.
- Bind receipts to purchase and authorization; preserve payment state independently of the merchant HTTP result.
- Persist and resume the original authorization, expose private receipt lookup, and verify Ed25519 provenance with trusted issuer keys.
- Document recovery limits, merchant propagation and the pending live EURC acceptance.
