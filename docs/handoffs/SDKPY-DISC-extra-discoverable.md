# SDKPY-DISC - `extra.discoverable` opt-in

## Estado

- Hecho: `X402Config.discoverable` (bool, default `False`) + `X402Config.with_discoverable(extra)`;
  cableado en `create_402_response_v2` (todas las opciones salvo Hedera nativo) y en
  `X402Client._build_payment_requirements` (verify + settle, sobre v1 y v2); guarda de no-bool
  en el config, en el uso y en `_merge_caller_extra`. Tests: `tests/test_discoverable_extra.py`
  (60 casos; 58 rojos sobre `origin/main` 3c81050, 60 verdes con el arreglo). Mutaciones M1..M10
  en rojo. CHANGELOG (Unreleased) y CLAUDE.md al dia.
- Falta: revision de c0der; decidir version/release (no se bumpeo `pyproject.toml`).
- Proximo paso: si c0der lo pide, exponer la opcion tambien por llamada (hoy solo config).

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
