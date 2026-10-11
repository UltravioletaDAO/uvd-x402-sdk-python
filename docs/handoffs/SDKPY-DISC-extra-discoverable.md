# SDKPY-DISC - `extra.discoverable` opt-in

## Estado

- Hecho (ronda 1): `X402Config.discoverable` (`Optional[bool]`, default `None` = no se escribe nada) + `X402Config.with_discoverable(extra)`. Esta cableado en `create_402_response_v2` (todas las opciones salvo Hedera nativo) y en `X402Client._build_payment_requirements` (verify + settle, sobre v1 y v2). Hay guarda de no-bool en el config, en el uso y en `_merge_caller_extra`.
- Hecho (ronda 2, paridad con TS #52): `False` escribe un `false` JSON explicito, como `discoverable: false` en TS; `None` es "no seteado". Nuevo `with_discoverable(accept, discoverable=True)` por accept, exportado en la raiz, que lanza ante Hedera nativo con el nombre de red sin distinguir mayusculas (`HEDERA:MAINNET`, `Hedera:Testnet`, `HEDERA`, `hedera-testnet`). A nivel config, Hedera queda como viene. README, CHANGELOG y CLAUDE.md estan al dia.
- Falta: refutacion de c0der; decidir version/release (`pyproject.toml` no se bumpeo).
- Proximo paso: si c0der lo pide, alinear la regla de choque con TS (aca un `extra` del caller que contradice al config se rechaza; en TS la opcion pisa el valor previo de la accept).

## Medicion

- El facilitador (x402-rs `src/handlers.rs`, "Phase 2: Settlement Tracking") lee
  `body.payment_requirements.extra.discoverable` con `as_bool()`: un `"true"` string no registra.
- `bazaar_extension(discoverable=...)` escribe en `extensions.bazaar`, que ese camino no lee.
- `process_payment` no tiene parametro `extra`: por eso la opcion vive en `X402Config`, que usan
  tanto el 402 v2 como los requisitos de verify/settle (coherencia entre ambos).
- Hedera nativo: `validate_hedera_requirements` exige `set(extra) == {"feePayer"}`; agregarle la
  clave romperia a sus compradores, asi que queda fuera.
- El 402 v1 (`create_402_response`) no tiene `extra` por opcion: sin cambios.

## Linea base

`origin/main` 3c81050: `python -m pytest -q` -> 5514 passed, 1 skipped (py 3.11, red cerrada).

## Medido en la ronda 2

- `create_402_response_v2(networks=[...])` descarta las grafias no canonicas (`HEDERA:TESTNET`, `hedera-testnet`) porque `X402Config.is_network_enabled` compara exacto. Pasa igual en la base, con o sin flag; queda fuera de esta tarea.
