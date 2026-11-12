"""NEAR ERC-8004 identity, reputation and Arras proof helpers.

Builders and signing are offline. Config snapshots must come from a trusted RPC;
SDK ceilings limit spend even if a snapshot or wallet descriptor is modified.
No helper broadcasts transactions or accesses a private key implicitly.
"""

from __future__ import annotations

import base64
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Any

import httpx

from .bounded_json import loads_bounded
from .near_escrow import NearLocalSigner, Unsigned, _account, _json, _uint
from .near_events import _boolean, _record
from .near_transactions import _sign_function_call_transaction

NEAR_ERC8004_CONTRACTS = MappingProxyType(
    {
        "near:mainnet": MappingProxyType(
            {
                "identity": "identity-registry.erc-8004.near",
                "reputation": "reputation-registry.erc-8004.near",
                "verifier": "erc8004-verifier.arras-escrow.near",
            }
        ),
        "near:testnet": MappingProxyType(
            {
                "identity": "identity-registry.erc-8004.testnet",
                "reputation": "reputation-registry.erc-8004.testnet",
                "verifier": "erc8004-verifier.arras-dev.testnet",
            }
        ),
    }
)
NEAR_ERC8004_ESCROWS = MappingProxyType(
    {
        "near:mainnet": "v1.arras-escrow.near",
        "near:testnet": "arras-v1b-1101.testnet",
    }
)
# The ERC-8004 verifier that rates the payments of each registered Arras escrow
# (one row per contract of near_escrow.NEAR_ESCROW_CONTRACTS). None: no verifier is
# registered for that escrow, and rating its payments is refused (fail closed). A
# row is filled in a release only after its account is deployed on chain.
NEAR_ERC8004_ESCROW_VERIFIERS = MappingProxyType(
    {
        "near:mainnet": MappingProxyType(
            {
                "v1.arras-escrow.near": "erc8004-verifier.arras-escrow.near",
                "v2.arras-escrow.near": None,
            }
        ),
        "near:testnet": MappingProxyType(
            {
                "arras-v1b-1101.testnet": "erc8004-verifier.arras-dev.testnet",
            }
        ),
    }
)
# Recorded get_config maxima, with 5% headroom. These are ceilings, not defaults.
ERC8004_DEPOSIT_CEILINGS = MappingProxyType(
    {
        "register_deposit": 132790000000000000000000 * 105 // 100,
        "feedback_deposit": 5910000000000000000000 * 105 // 100,
        "feedback_proof_deposit": 14720000000000000000000 * 105 // 100,
    }
)
MAX_WALLET_DELAY_NS = 300_000_000_000
MAX_VIEW_BYTES = 512 * 1024
TGAS = 10**12
RPC = Callable[[str, dict[str, Any]], dict[str, Any]]


class Erc8004Rejection(str, Enum):
    E20 = "E20_VISTA_DE_IDENTIDAD_FALLO"
    E21 = "E21_RESPUESTA_DE_IDENTIDAD_INVALIDA"
    E22 = "E22_AGENTE_INEXISTENTE"
    E23 = "E23_AUTO_CALIFICACION"
    E42 = "E42_PRUEBA_YA_USADA"
    E44 = "E44_REEMPLAZADO"
    E45 = "E45_VERIFICADOR_FALLO"
    E46 = "E46_RESPUESTA_DEL_VERIFICADOR_INVALIDA"
    E47 = "E47_NO_ES_EL_AGENTE"
    E48 = "E48_AGENT_WALLET_CALIFICA"


Erc8004PanicCode = Enum(
    "Erc8004PanicCode",
    {
        **{f"ID_E{i:02}": f"ERC8004_ID_E{i:02}" for i in (*range(1, 8), *range(10, 24))},
        **{f"REP_E{i:02}": f"ERC8004_REP_E{i:02}" for i in (*range(1, 18), 30, 40, 41, 42, 43)},
        # E16: v2's verifier (payment-proof, arras branch c0-arras-verifier-v2 lib.rs:97,112),
        # a dispute expired in the receiver's favour; v1's emits E01..E15 only.
        **{f"ARV_E{i:02}": f"ERC8004_ARV_E{i:02}" for i in range(1, 17)},
    },
    type=str,
)


def _require(ok: bool, message: str) -> None:
    if not ok:
        raise ValueError(message)


def _contracts(network: str) -> Mapping[str, str]:
    _require(
        isinstance(network, str) and network in NEAR_ERC8004_CONTRACTS,
        "unsupported NEAR ERC-8004 network",
    )
    return NEAR_ERC8004_CONTRACTS[network]


def _verifiers(network: str) -> frozenset:
    """The registered verifiers of the network: every non-None row of the escrow map."""
    return frozenset(v for v in NEAR_ERC8004_ESCROW_VERIFIERS[network].values() if v is not None)


def _escrow_verifier(network: str, escrow: object) -> str | None:
    """The verifier of a registered escrow of the network; None if unregistered or without one."""
    rows = NEAR_ERC8004_ESCROW_VERIFIERS[network]
    return rows.get(escrow) if isinstance(escrow, str) else None


def near_erc8004_verifier(network: str, escrow: str) -> str:
    """The ERC-8004 verifier that rates payments made in ``escrow``, an Arras contract.

    Rate a task with the verifier of the escrow it was paid in. An escrow with no
    registered verifier yet (Arras v2 today) raises ValueError: its payments are
    not rated, rather than sent to a verifier that reads another contract.
    """
    _contracts(network)
    rows = NEAR_ERC8004_ESCROW_VERIFIERS[network]
    _require(
        isinstance(escrow, str) and escrow in rows,
        "escrow is not a registered Arras contract for this network",
    )
    _require(
        rows[escrow] is not None,
        "no ERC-8004 verifier is registered for this Arras escrow yet: "
        "its payments cannot be rated",
    )
    return rows[escrow]


def agent_registry(network: str) -> str:
    return f"{network}:{_contracts(network)['identity']}"


def _decimal(value: object, bits: int = 64, positive: bool = False) -> bool:
    if type(value) is not str:
        return False
    try:
        return _uint(value, bits, "integer") >= (1 if positive else 0)
    except ValueError:
        return False


def _signed(value: object, maximum: int = 10**38) -> bool:
    return (
        isinstance(value, str)
        and len(value) <= 57
        and re.fullmatch(r"0|-?[1-9][0-9]*", value) is not None
        and abs(int(value)) <= maximum
    )


def _number(value: object, maximum: int = 2**64 - 1) -> bool:
    return type(value) is int and 0 <= value <= maximum


def _acc(value: object) -> bool:
    try:
        _account(value, "account")
        return True
    except ValueError:
        return False


def _text(value: object, maximum: int, controls: bool = True) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return len(value.encode("utf-8")) <= maximum and (
            controls or not any(ord(c) < 32 or 127 <= ord(c) <= 159 for c in value)
        )
    except UnicodeEncodeError:
        return False


def _uri(value: object, maximum: int, empty: bool = True) -> bool:
    return isinstance(value, str) and (
        (empty and value == "")
        or (
            len(value) <= maximum
            and re.fullmatch(r"[A-Za-z][A-Za-z0-9+.-]*:.+", value) is not None
            and re.fullmatch(r"[A-Za-z0-9._~:/?#\[\]@!$&'()*+,;=%-]+", value) is not None
        )
    )


def _key(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9._:/-]{1,64}", value) is not None


def _base64(value: object, maximum: int = 1024) -> bool:
    if not isinstance(value, str) or len(value) > ((maximum + 2) // 3) * 4:
        return False
    try:
        raw = base64.b64decode(value, validate=True)
        return len(raw) <= maximum and base64.b64encode(raw).decode("ascii") == value
    except (ValueError, UnicodeError):
        return False


def _hash(value: object) -> bool:
    return value is None or (
        isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None
    )


def _label(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[a-z0-9_-]{0,32}", value) is not None


def _proof(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[!-~]{1,128}", value) is not None


def _tag(value: object) -> bool:
    return _text(value, 64, controls=False)


def _nullable(check):
    return lambda value: value is None or check(value)


def _list(check, maximum=10000):
    return lambda value: (
        isinstance(value, list) and len(value) <= maximum and all(check(v) for v in value)
    )


_ID_LIMITS = {
    "max_agent_uri_len": 8192,
    "max_metadata_key_len": 64,
    "max_metadata_value_len": 1024,
    "max_register_metadata": 4,
    "max_approvals": 8,
    "max_memo_len": 256,
    "max_list_limit": 100,
}
_REP_LIMITS = {
    "max_endpoint_len": 1024,
    "max_feedback_uri_len": 4096,
    "max_response_uri_len": 4096,
    "max_tag_len": 64,
    "max_list_limit": 100,
}


@dataclass(frozen=True)
class Erc8004Config:
    """Network-bound snapshot. Use parse_erc8004_config or client.get_config."""

    network: str
    role: str
    data: Mapping[str, Any]


def parse_erc8004_config(
    value: object, *, network: str, role: str, verifier: str | None = None
) -> Erc8004Config:
    """Validate a get_config answer and bind it to the network.

    A verifier config must read a registered escrow that has a verifier
    (NEAR_ERC8004_ESCROW_VERIFIERS), and verify_feedback_proof built with it goes
    to that escrow's verifier. ``verifier``: the account the answer was read
    from, when known (role "verifier" only); the snapshot must then read that
    verifier's escrow.
    """
    contracts = _contracts(network)
    _require(isinstance(role, str) and role in contracts, "unknown contract role")
    _require(verifier is None or role == "verifier", "verifier applies only to role verifier")
    _require(isinstance(value, Mapping), "config must be an object")
    # Snapshot without retaining caller-owned nested containers.
    snapshot = dict(value)
    if isinstance(snapshot.get("limits"), Mapping):
        snapshot["limits"] = dict(snapshot["limits"])
    raw = loads_bounded(_json(snapshot))
    expected = "erc8004-arras-verifier" if role == "verifier" else f"erc8004-{role}"
    _require(raw.get("contract") == expected, "unexpected config contract")
    if role != "verifier":
        _require(
            raw.get("erc8004_version") == "2.0.0"
            and raw.get("events_standard") == "erc8004"
            and raw.get("events_version") == "1.0.0",
            "unsupported contract version",
        )
    deposits = {
        "identity": ("register_deposit",),
        "reputation": ("feedback_deposit", "feedback_proof_deposit"),
        "verifier": (),
    }[role]
    for name in deposits:
        v = raw.get(name)
        _require(_decimal(v, 128, True), f"invalid {name}")
        _require(int(v) <= ERC8004_DEPOSIT_CEILINGS[name], f"{name} exceeds SDK ceiling")
    gas = {
        "identity": ("min_gas_nft_transfer_call",),
        "reputation": ("min_gas_give_feedback", "min_gas_give_feedback_with_proof"),
        "verifier": ("min_gas_verify", "gas_get_payment", "gas_on_payment"),
    }[role]
    gas_floors = {
        "min_gas_nft_transfer_call": 50 * TGAS,
        "min_gas_give_feedback": 40 * TGAS,
        "min_gas_give_feedback_with_proof": 120 * TGAS,
        "min_gas_verify": 25 * TGAS,
    }
    for name in gas:
        _require(
            _decimal(raw.get(name), 64, True)
            and gas_floors.get(name, 1) <= int(raw[name]) <= 300 * TGAS,
            f"invalid {name}",
        )
    if role == "identity":
        _require(_decimal(raw.get("total_agents")), "invalid total_agents")
        _require(_number(raw.get("token_reserve_bytes"), 77), "invalid token reserve")
        _require(
            _decimal(raw.get("storage_byte_cost"), 128, True)
            and int(raw["storage_byte_cost"]) <= 10**19 * 105 // 100,
            "storage byte cost exceeds SDK ceiling",
        )
        limits = raw.get("limits")
        _require(isinstance(limits, dict), "invalid identity limits")
        for name, maximum in _ID_LIMITS.items():
            _require(_number(limits.get(name), maximum) and limits[name] > 0, f"invalid {name}")
        _require(
            _decimal(limits.get("max_deadline_delay_ns"), 64, True)
            and int(limits["max_deadline_delay_ns"]) <= MAX_WALLET_DELAY_NS,
            "invalid wallet deadline window",
        )
        raw["limits"] = MappingProxyType(limits)
    elif role == "reputation":
        _require(
            raw.get("identity_registry") == contracts["identity"],
            "config identity network mismatch",
        )
        for name, maximum in _REP_LIMITS.items():
            _require(_number(raw.get(name), maximum) and raw[name] > 0, f"invalid {name}")
    else:
        # A verifier reads one escrow: it must have a registered verifier, the one read from.
        reads = _escrow_verifier(network, raw.get("escrow"))
        _require(reads is not None, "config escrow network mismatch")
        _require(
            verifier is None or reads == verifier,
            "verifier config reads the escrow of another verifier",
        )
    return Erc8004Config(network, role, MappingProxyType(raw))


def _config(config: Erc8004Config, network: str, role: str) -> Mapping[str, Any]:
    _require(
        isinstance(config, Erc8004Config) and config.network == network and config.role == role,
        "config network or role mismatch",
    )
    raw = dict(config.data)
    if isinstance(raw.get("limits"), Mapping):
        raw["limits"] = dict(raw["limits"])
    return parse_erc8004_config(raw, network=network, role=role).data


# Required and optional argument order is also the canonical signing JSON order.
_WRITE_ARGS = {
    "register": ("identity", (), ("agent_uri", "metadata")),
    "set_agent_uri": ("identity", ("agent_id", "new_uri"), ()),
    "set_metadata": ("identity", ("agent_id", "metadata_key", "metadata_value"), ()),
    "set_agent_wallet": ("identity", ("agent_id", "new_wallet", "deadline"), ()),
    "accept_agent_wallet": ("identity", ("agent_id",), ()),
    "unset_agent_wallet": ("identity", ("agent_id",), ()),
    "nft_transfer": ("identity", ("receiver_id", "token_id"), ("approval_id", "memo")),
    "nft_transfer_call": ("identity", ("receiver_id", "token_id", "msg"), ("approval_id", "memo")),
    "nft_approve": ("identity", ("token_id", "account_id"), ("msg",)),
    "nft_revoke": ("identity", ("token_id", "account_id"), ()),
    "nft_revoke_all": ("identity", ("token_id",), ()),
    "give_feedback": (
        "reputation",
        ("agent_id", "value", "value_decimals"),
        ("tag1", "tag2", "endpoint", "feedback_uri", "feedback_hash"),
    ),
    "give_feedback_with_proof": (
        "reputation",
        ("agent_id", "value", "value_decimals", "verifier", "proof"),
        ("tag1", "tag2", "endpoint", "feedback_uri", "feedback_hash"),
    ),
    "revoke_feedback": ("reputation", ("agent_id", "feedback_index"), ()),
    "append_response": (
        "reputation",
        ("agent_id", "client_address", "feedback_index", "response_uri"),
        ("response_hash",),
    ),
    "verify_feedback_proof": ("verifier", ("client", "proof"), ()),
}


def _fields() -> dict[str, Callable]:
    return {
        **dict.fromkeys(("agent_id", "token_id", "feedback_index", "deadline"), _decimal),
        **dict.fromkeys(
            (
                "new_wallet",
                "receiver_id",
                "account_id",
                "client_address",
                "verifier",
                "client",
                "spender",
                "approved_account_id",
            ),
            _acc,
        ),
        "value": _signed,
        "value_decimals": lambda v: _number(v, 18),
        "agent_uri": lambda v: _uri(v, 8192),
        "new_uri": lambda v: _uri(v, 8192),
        "endpoint": lambda v: _uri(v, 1024),
        "feedback_uri": lambda v: _uri(v, 4096),
        "response_uri": lambda v: _uri(v, 4096, False),
        "metadata_key": _key,
        "metadata_value": _base64,
        "metadata": _list(
            lambda v: (
                _record({"metadata_key": _key, "metadata_value": _base64})(v)
                and set(v) == {"metadata_key", "metadata_value"}
                and v["metadata_key"] != "agentWallet"
            ),
            4,
        ),
        "tag1": _tag,
        "tag2": _tag,
        "feedback_hash": _hash,
        "response_hash": _hash,
        "proof": _proof,
        "context": _label,
        "memo": lambda v: _text(v, 256),
        "msg": lambda v: _text(v, 64 * 1024),
        "approval_id": _number,
        "client_addresses": _list(_acc),
        "responders": _list(_acc),
        "include_revoked": _boolean,
        "limit": lambda v: _number(v, 100),
    }


def _arguments(args, required, optional, *, extra=None):
    _require(isinstance(args, Mapping), "args must be an object")
    _require(set(args) <= set(required) | set(optional), "unknown argument")
    checks = {**_fields(), **(extra or {})}
    result = {}
    for name in (*required, *optional):
        if name not in args or args[name] is None:
            _require(name not in required, f"missing {name}")
            continue
        _require(checks[name](args[name]), f"invalid {name}")
        result[name] = args[name]
    # Detach all nested args before asking a signer for its key.
    return loads_bounded(_json(result))


def build_erc8004_call(
    *,
    network: str,
    signer_id: str,
    config: Erc8004Config,
    method: str,
    args: Mapping[str, Any],
    deposit: str | None = None,
    gas: str | None = None,
    now_ns: Unsigned | None = None,
) -> dict[str, Any]:
    """Build one wallet-selector action using a validated get_config snapshot.

    Storage-growth calls require an explicit deposit (1 yocto for no growth).
    Wallet deadlines are checked again at signing against its current clock.
    """
    contracts = _contracts(network)
    _account(signer_id, "signer_id")
    _require(isinstance(method, str) and method in _WRITE_ARGS, "unsupported ERC-8004 method")
    role, required, optional = _WRITE_ARGS[method]
    cfg = _config(config, network, role)
    data = _arguments(args, required, optional)
    if method == "set_metadata":
        _require(data["metadata_key"] != "agentWallet", "agentWallet is reserved")
    if method in ("revoke_feedback", "append_response"):
        _require(int(data["feedback_index"]) > 0, "feedback_index must be positive")
    if method == "give_feedback_with_proof":
        _require(
            data["verifier"] in _verifiers(network),
            "unsupported verifier for selected network",
        )
    limits = cfg["limits"] if role == "identity" else cfg
    for field, limit in (
        ("agent_uri", "max_agent_uri_len"),
        ("new_uri", "max_agent_uri_len"),
        ("metadata_key", "max_metadata_key_len"),
        ("memo", "max_memo_len"),
        ("endpoint", "max_endpoint_len"),
        ("feedback_uri", "max_feedback_uri_len"),
        ("response_uri", "max_response_uri_len"),
        ("tag1", "max_tag_len"),
        ("tag2", "max_tag_len"),
    ):
        if field in data:
            _require(len(data[field].encode()) <= limits[limit], f"{field} exceeds config limit")
    entries = data.get("metadata", [])
    if "metadata_value" in data:
        entries = [{"metadata_key": data["metadata_key"], "metadata_value": data["metadata_value"]}]
    if role == "identity":
        _require(len(entries) <= limits["max_register_metadata"], "too many metadata entries")
        for entry in entries:
            _require(
                len(entry["metadata_key"]) <= limits["max_metadata_key_len"]
                and len(base64.b64decode(entry["metadata_value"]))
                <= limits["max_metadata_value_len"],
                "metadata exceeds config limits",
            )
    if method == "set_agent_wallet":
        now = time.time_ns() if now_ns is None else _uint(now_ns, 64, "now_ns")
        deadline = int(data["deadline"])
        _require(
            now <= deadline <= min(2**64 - 1, now + int(limits["max_deadline_delay_ns"])),
            "deadline must be within the current nanosecond wallet window",
        )
    deposit_field = {
        "register": "register_deposit",
        "give_feedback": "feedback_deposit",
        "give_feedback_with_proof": "feedback_proof_deposit",
    }.get(method)
    if deposit_field:
        minimum = int(cfg[deposit_field])
        maximum = ERC8004_DEPOSIT_CEILINGS[deposit_field]
        chosen = cfg[deposit_field] if deposit is None else deposit
    elif method in ("set_agent_uri", "set_metadata", "nft_approve", "append_response"):
        minimum = 1
        ceiling_field = "register_deposit" if role == "identity" else "feedback_proof_deposit"
        maximum = min(int(cfg[ceiling_field]), ERC8004_DEPOSIT_CEILINGS[ceiling_field])
        _require(deposit is not None, "storage growth requires an explicit deposit")
        chosen = deposit
    else:
        minimum = maximum = 0 if role == "verifier" else 1
        chosen = str(minimum) if deposit is None else deposit
    _require(_decimal(chosen, 128), "invalid deposit")
    _require(minimum <= int(chosen) <= maximum, "deposit outside SDK bounds")
    gas_key = {
        "nft_transfer_call": "min_gas_nft_transfer_call",
        "give_feedback": "min_gas_give_feedback",
        "give_feedback_with_proof": "min_gas_give_feedback_with_proof",
        "verify_feedback_proof": "min_gas_verify",
    }.get(method)
    minimum_gas = int(cfg[gas_key]) if gas_key else 30 * TGAS
    recommended = {
        "give_feedback": 60,
        "give_feedback_with_proof": 150,
        "nft_transfer_call": 60,
        "verify_feedback_proof": 50,
    }.get(method, 30) * TGAS
    chosen_gas = str(max(minimum_gas, recommended)) if gas is None else gas
    _require(
        _decimal(chosen_gas, 64) and minimum_gas <= int(chosen_gas) <= 300 * TGAS,
        "gas outside SDK bounds",
    )
    # verify_feedback_proof goes to the verifier whose config was quoted: its escrow's.
    receiver = _escrow_verifier(network, cfg["escrow"]) if role == "verifier" else contracts[role]
    return {
        "signerId": signer_id,
        "receiverId": receiver,
        "actions": [
            {
                "type": "FunctionCall",
                "params": {
                    "methodName": method,
                    "args": data,
                    "gas": chosen_gas,
                    "deposit": chosen,
                },
            }
        ],
    }


def _validated_transaction(descriptor, *, network, config, now_ns=None):
    _require(
        isinstance(descriptor, dict) and set(descriptor) == {"signerId", "receiverId", "actions"},
        "invalid transaction descriptor",
    )
    actions = descriptor["actions"]
    _require(isinstance(actions, list) and len(actions) == 1, "expected exactly one action")
    action = actions[0]
    _require(
        isinstance(action, dict)
        and set(action) == {"type", "params"}
        and action["type"] == "FunctionCall",
        "expected FunctionCall",
    )
    params = action["params"]
    _require(
        isinstance(params, dict) and set(params) == {"methodName", "args", "gas", "deposit"},
        "invalid FunctionCall params",
    )
    tx = build_erc8004_call(
        network=network,
        signer_id=descriptor["signerId"],
        config=config,
        method=params["methodName"],
        args=params["args"],
        gas=params["gas"],
        deposit=params["deposit"],
        now_ns=now_ns,
    )
    _require(
        descriptor["receiverId"] == tx["receiverId"], "transaction network or receiver mismatch"
    )
    return tx


def erc8004_args_json(
    descriptor, *, network: str, config: Erc8004Config, now_ns: Unsigned | None = None
) -> bytes:
    tx = _validated_transaction(descriptor, network=network, config=config, now_ns=now_ns)
    return _json(tx["actions"][0]["params"]["args"]).encode("utf-8")


def sign_erc8004_transaction(
    descriptor,
    signer: NearLocalSigner,
    nonce: Unsigned,
    block_hash: str,
    *,
    network: str,
    config: Erc8004Config,
    now_ns: Unsigned | None = None,
) -> str:
    """Return a verified base64 NEAR V0 SignedTransaction; never submit it.

    The caller supplies the next nonce and recent block hash and checks FullAccess
    permission separately. No offline signature establishes key permissions.
    """
    tx = _validated_transaction(descriptor, network=network, config=config, now_ns=now_ns)
    return _sign_function_call_transaction(tx, signer, nonce, block_hash, network)


# Convenience builders keep ABI arguments separate from transaction options.
def build_erc8004_register(*, agent_uri=None, metadata=None, **options):
    return build_erc8004_call(
        method="register", args={"agent_uri": agent_uri, "metadata": metadata}, **options
    )


def build_erc8004_set_agent_uri(*, agent_id, new_uri, **options):
    return build_erc8004_call(
        method="set_agent_uri", args={"agent_id": agent_id, "new_uri": new_uri}, **options
    )


def build_erc8004_set_metadata(*, agent_id, metadata_key, metadata_value, **options):
    return build_erc8004_call(
        method="set_metadata",
        args={"agent_id": agent_id, "metadata_key": metadata_key, "metadata_value": metadata_value},
        **options,
    )


def build_erc8004_set_agent_wallet(*, agent_id, new_wallet, deadline, **options):
    return build_erc8004_call(
        method="set_agent_wallet",
        args={"agent_id": agent_id, "new_wallet": new_wallet, "deadline": deadline},
        **options,
    )


def build_erc8004_accept_agent_wallet(*, agent_id, **options):
    return build_erc8004_call(method="accept_agent_wallet", args={"agent_id": agent_id}, **options)


def build_erc8004_unset_agent_wallet(*, agent_id, **options):
    return build_erc8004_call(method="unset_agent_wallet", args={"agent_id": agent_id}, **options)


def build_erc8004_nft_transfer(*, receiver_id, token_id, approval_id=None, memo=None, **options):
    return build_erc8004_call(
        method="nft_transfer",
        args={
            "receiver_id": receiver_id,
            "token_id": token_id,
            "approval_id": approval_id,
            "memo": memo,
        },
        **options,
    )


def build_erc8004_nft_transfer_call(
    *, receiver_id, token_id, msg, approval_id=None, memo=None, **options
):
    return build_erc8004_call(
        method="nft_transfer_call",
        args={
            "receiver_id": receiver_id,
            "token_id": token_id,
            "msg": msg,
            "approval_id": approval_id,
            "memo": memo,
        },
        **options,
    )


def build_erc8004_nft_approve(*, token_id, account_id, msg=None, **options):
    return build_erc8004_call(
        method="nft_approve",
        args={"token_id": token_id, "account_id": account_id, "msg": msg},
        **options,
    )


def build_erc8004_nft_revoke(*, token_id, account_id, **options):
    return build_erc8004_call(
        method="nft_revoke", args={"token_id": token_id, "account_id": account_id}, **options
    )


def build_erc8004_nft_revoke_all(*, token_id, **options):
    return build_erc8004_call(method="nft_revoke_all", args={"token_id": token_id}, **options)


def build_erc8004_give_feedback(
    *,
    agent_id,
    value,
    value_decimals,
    tag1=None,
    tag2=None,
    endpoint=None,
    feedback_uri=None,
    feedback_hash=None,
    **options,
):
    return build_erc8004_call(
        method="give_feedback",
        args={
            "agent_id": agent_id,
            "value": value,
            "value_decimals": value_decimals,
            "tag1": tag1,
            "tag2": tag2,
            "endpoint": endpoint,
            "feedback_uri": feedback_uri,
            "feedback_hash": feedback_hash,
        },
        **options,
    )


def build_erc8004_give_feedback_with_proof(
    *,
    agent_id,
    value,
    value_decimals,
    verifier,
    proof,
    tag1=None,
    tag2=None,
    endpoint=None,
    feedback_uri=None,
    feedback_hash=None,
    **options,
):
    return build_erc8004_call(
        method="give_feedback_with_proof",
        args={
            "agent_id": agent_id,
            "value": value,
            "value_decimals": value_decimals,
            "verifier": verifier,
            "proof": proof,
            "tag1": tag1,
            "tag2": tag2,
            "endpoint": endpoint,
            "feedback_uri": feedback_uri,
            "feedback_hash": feedback_hash,
        },
        **options,
    )


def build_erc8004_revoke_feedback(*, agent_id, feedback_index, **options):
    return build_erc8004_call(
        method="revoke_feedback",
        args={"agent_id": agent_id, "feedback_index": feedback_index},
        **options,
    )


def build_erc8004_append_response(
    *, agent_id, client_address, feedback_index, response_uri, response_hash=None, **options
):
    return build_erc8004_call(
        method="append_response",
        args={
            "agent_id": agent_id,
            "client_address": client_address,
            "feedback_index": feedback_index,
            "response_uri": response_uri,
            "response_hash": response_hash,
        },
        **options,
    )


def build_erc8004_verify_feedback_proof(*, client, proof, **options):
    """Verifier simulation is a gas-consuming transaction, not a JSON-RPC view."""
    return build_erc8004_call(
        method="verify_feedback_proof", args={"client": client, "proof": proof}, **options
    )


_feedback_fields = {
    "value": _signed,
    "value_decimals": lambda v: _number(v, 18),
    "tag1": _tag,
    "tag2": _tag,
    "is_revoked": _boolean,
}
_proof_fields = {
    "verifier": _acc,
    "proof": _proof,
    "counterparty": _acc,
    "role": _label,
    "context": _label,
    "weight": lambda v: _decimal(v, 128),
}
_ref_fields = {
    "agent_id": _decimal,
    "client_address": _acc,
    "feedback_index": lambda v: _decimal(v, positive=True),
}
_summary_fields = {
    "count": _decimal,
    "summary_value": lambda v: _signed(v, 10**56 - 1),
    "summary_value_decimals": lambda v: _number(v, 18),
}


def _token(value):
    metadata_fields = dict.fromkeys(
        (
            "title",
            "description",
            "media",
            "media_hash",
            "copies",
            "issued_at",
            "expires_at",
            "starts_at",
            "updated_at",
            "extra",
            "reference_hash",
        ),
        lambda v: v is None,
    )
    metadata_fields["reference"] = _nullable(lambda v: _uri(v, 8192))
    return _record(
        {
            "token_id": _decimal,
            "owner_id": _acc,
            "metadata": _record(metadata_fields),
            "approved_account_ids": lambda v: (
                isinstance(v, dict)
                and len(v) <= 8
                and all(_acc(k) and _number(i) for k, i in v.items())
            ),
        }
    )(value)


def _all_feedback(value):
    fields = {
        "clients": _acc,
        "feedback_indexes": lambda v: _decimal(v, positive=True),
        "values": _signed,
        "value_decimals": lambda v: _number(v, 18),
        "tag1s": _tag,
        "tag2s": _tag,
        "revoked_statuses": _boolean,
    }
    return (
        _record({k: _list(v) for k, v in fields.items()})(value)
        and len({len(value[k]) for k in fields}) == 1
    )


# Views are a closed allowlist; cross-contract verifier callbacks cannot be queried.
_VIEW_ARGS = {
    "identity": {
        "nft_token": (("token_id",), (), _nullable(_token)),
        "nft_tokens": ((), ("from_index", "limit"), _list(_token, 100)),
        "nft_supply_for_owner": (("account_id",), (), lambda v: _decimal(v, 128)),
        "nft_tokens_for_owner": (("account_id",), ("from_index", "limit"), _list(_token, 100)),
        "nft_total_supply": ((), (), lambda v: _decimal(v, 128)),
        "nft_metadata": (
            (),
            (),
            _record(
                {
                    "spec": lambda v: v == "nft-1.0.0",
                    "name": lambda v: _text(v, 256),
                    "symbol": lambda v: _text(v, 64),
                    **dict.fromkeys(
                        ("icon", "base_uri", "reference", "reference_hash"), lambda v: v is None
                    ),
                }
            ),
        ),
        "nft_is_approved": (("token_id", "approved_account_id"), ("approval_id",), _boolean),
        "token_uri": (("agent_id",), (), _nullable(lambda v: _uri(v, 8192))),
        "get_metadata": (("agent_id", "metadata_key"), (), _nullable(_base64)),
        "get_agent_wallet": (("agent_id",), (), _nullable(_acc)),
        "get_pending_agent_wallet": (
            ("agent_id",),
            (),
            _nullable(_record({"wallet": _acc, "deadline": _decimal})),
        ),
        "is_authorized_or_owner": (("spender", "agent_id"), (), _boolean),
        "check_agent": (
            ("agent_id", "account_id"),
            (),
            _nullable(
                _record({"owner_id": _acc, "agent_wallet": _nullable(_acc), "authorized": _boolean})
            ),
        ),
        "get_version": ((), (), lambda v: v == "2.0.0"),
    },
    "reputation": {
        "read_feedback": (
            ("agent_id", "client_address", "feedback_index"),
            (),
            _nullable(_record(_feedback_fields)),
        ),
        "read_feedback_proof": (
            ("agent_id", "client_address", "feedback_index"),
            (),
            _nullable(_record(_proof_fields)),
        ),
        "get_summary": (
            ("agent_id", "client_addresses"),
            ("tag1", "tag2"),
            _record(_summary_fields),
        ),
        "get_verified_summary": (
            ("agent_id", "verifier"),
            ("client_addresses", "tag1", "tag2", "context"),
            _record({**_summary_fields, "weight_sum": lambda v: _decimal(v, 128)}),
        ),
        "list_verified_feedback": (
            ("agent_id", "verifier"),
            ("from_index", "limit"),
            _list(_record(_ref_fields), 100),
        ),
        "read_all_feedback": (
            ("agent_id",),
            ("client_addresses", "tag1", "tag2", "include_revoked"),
            _all_feedback,
        ),
        "get_response_count": (
            ("agent_id",),
            ("client_address", "feedback_index", "responders"),
            _decimal,
        ),
        "get_clients": (("agent_id",), ("from_index", "limit"), _list(_acc)),
        "get_last_index": (("agent_id", "client_address"), (), _decimal),
        "get_identity_registry": ((), (), _acc),
        "get_version": ((), (), lambda v: v == "2.0.0"),
        "get_proof_feedback": (
            ("verifier", "proof", "client_address"),
            (),
            _nullable(_record(_ref_fields)),
        ),
        "is_proof_pending": (("verifier", "proof", "client_address"), (), _boolean),
    },
    "verifier": {},
}


class NearErc8004Client:
    """Synchronous views with rpc(method, params) injection and bounded HTTP reads.

    A custom transport returns the complete JSON-RPC envelope and is responsible
    for bounding its wire response. All decoded call_function bytes are bounded
    here. A config is bound to this network before it can be used by a builder.
    """

    def __init__(self, network: str = "near:mainnet", *, rpc: RPC | None = None):
        _contracts(network)
        self.network = network
        self.rpc = rpc if rpc is not None else self._http_rpc

    def _http_rpc(self, method, params):
        _contracts(self.network)
        url = "https://rpc." + self.network.split(":")[1] + ".near.org"
        with httpx.stream(
            "POST",
            url,
            json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
            timeout=30,
        ) as response:
            response.raise_for_status()
            raw = bytearray()
            for chunk in response.iter_bytes():
                _require(len(raw) + len(chunk) <= 4 * MAX_VIEW_BYTES, "RPC response too large")
                raw.extend(chunk)
        return loads_bounded(raw, max_bytes=4 * MAX_VIEW_BYTES)

    def _call(self, role, method, args, account=None):
        contracts = _contracts(self.network)
        _require(isinstance(role, str) and role in contracts, "unknown contract role")
        response = self.rpc(
            "query",
            {
                "request_type": "call_function",
                "finality": "final",
                "account_id": contracts[role] if account is None else account,
                "method_name": method,
                "args_base64": base64.b64encode(_json(args).encode()).decode("ascii"),
            },
        )
        _require(isinstance(response, dict) and "error" not in response, "ERC-8004 RPC failed")
        result = response.get("result")
        _require(isinstance(result, dict) and "error" not in result, "invalid RPC result")
        raw = result.get("result")
        _require(
            isinstance(raw, list)
            and len(raw) <= MAX_VIEW_BYTES
            and all(type(b) is int and 0 <= b <= 255 for b in raw),
            "invalid result bytes",
        )
        return loads_bounded(bytes(raw))

    def get_config(self, role: str = "identity", *, verifier: str | None = None) -> Erc8004Config:
        """The get_config of a contract of this network, bound to it.

        Role "verifier" reads ``verifier`` (default ``NEAR_ERC8004_CONTRACTS[network]
        ["verifier"]``, v1's), which must be a registered verifier of the network
        (refused before any RPC), and the answer must read that verifier's escrow.
        Pass ``near_erc8004_verifier(network, escrow)`` of the payment to quote
        verify_feedback_proof against it. An RPC error raises: no config, nothing
        to sign.
        """
        contracts = _contracts(self.network)
        # An unknown role is refused by _call, before the RPC.
        if role != "verifier":
            _require(verifier is None, "verifier applies only to role verifier")
            return parse_erc8004_config(
                self._call(role, "get_config", {}), network=self.network, role=role
            )
        account = contracts["verifier"] if verifier is None else verifier
        _require(
            isinstance(account, str) and account in _verifiers(self.network),
            "unsupported verifier for selected network",
        )
        return parse_erc8004_config(
            self._call(role, "get_config", {}, account),
            network=self.network,
            role=role,
            verifier=account,
        )

    def view(self, role: str, method: str, **args):
        _contracts(self.network)
        _require(
            isinstance(role, str)
            and role in _VIEW_ARGS
            and isinstance(method, str)
            and method in _VIEW_ARGS[role],
            "unsupported view",
        )
        required, optional, check = _VIEW_ARGS[role][method]
        extra = {"from_index": (lambda v: _decimal(v, 128)) if role == "identity" else _number}
        data = _arguments(args, required, optional, extra=extra)
        if method == "get_summary":
            _require(bool(data["client_addresses"]), "client_addresses must not be empty")
        value = self._call(role, method, data)
        _require(check(value), "unexpected ERC-8004 view shape")
        if method == "get_identity_registry":
            _require(
                value == _contracts(self.network)["identity"], "identity registry network mismatch"
            )
        if method == "get_metadata" and data["metadata_key"] == "agentWallet" and value is not None:
            _require(_acc(base64.b64decode(value).decode("utf-8")), "invalid agentWallet metadata")
        if method == "nft_token" and value is not None:
            _require(value["token_id"] == data["token_id"], "token result mismatch")
        if method == "get_proof_feedback" and value is not None:
            _require(value["client_address"] == data["client_address"], "feedback client mismatch")
        if method == "list_verified_feedback":
            _require(
                all(v["agent_id"] == data["agent_id"] for v in value), "feedback agent mismatch"
            )
        return value

    def get_agent(self, agent_id):
        return self.nft_token(agent_id)

    def nft_token(self, token_id):
        return self.view("identity", "nft_token", token_id=token_id)

    def nft_tokens(self, **page):
        return self.view("identity", "nft_tokens", **page)

    def nft_total_supply(self):
        return self.view("identity", "nft_total_supply")

    def nft_supply_for_owner(self, account_id):
        return self.view("identity", "nft_supply_for_owner", account_id=account_id)

    def nft_tokens_for_owner(self, account_id, **page):
        return self.view("identity", "nft_tokens_for_owner", account_id=account_id, **page)

    def nft_metadata(self):
        return self.view("identity", "nft_metadata")

    def nft_is_approved(self, token_id, approved_account_id, approval_id=None):
        return self.view(
            "identity",
            "nft_is_approved",
            token_id=token_id,
            approved_account_id=approved_account_id,
            approval_id=approval_id,
        )

    def token_uri(self, agent_id):
        return self.view("identity", "token_uri", agent_id=agent_id)

    def get_metadata(self, agent_id, metadata_key):
        return self.view("identity", "get_metadata", agent_id=agent_id, metadata_key=metadata_key)

    def get_agent_wallet(self, agent_id):
        return self.view("identity", "get_agent_wallet", agent_id=agent_id)

    def get_pending_agent_wallet(self, agent_id):
        return self.view("identity", "get_pending_agent_wallet", agent_id=agent_id)

    def is_authorized_or_owner(self, spender, agent_id):
        return self.view("identity", "is_authorized_or_owner", spender=spender, agent_id=agent_id)

    def check_agent(self, agent_id, account_id):
        return self.view("identity", "check_agent", agent_id=agent_id, account_id=account_id)

    def get_version(self, role="identity"):
        return self.view(role, "get_version")

    def read_feedback(self, agent_id, client_address, feedback_index):
        return self.view(
            "reputation",
            "read_feedback",
            agent_id=agent_id,
            client_address=client_address,
            feedback_index=feedback_index,
        )

    def read_feedback_proof(self, agent_id, client_address, feedback_index):
        return self.view(
            "reputation",
            "read_feedback_proof",
            agent_id=agent_id,
            client_address=client_address,
            feedback_index=feedback_index,
        )

    def get_summary(self, agent_id, client_addresses, **filters):
        return self.view(
            "reputation",
            "get_summary",
            agent_id=agent_id,
            client_addresses=client_addresses,
            **filters,
        )

    def get_verified_summary(self, agent_id, verifier, **filters):
        return self.view(
            "reputation", "get_verified_summary", agent_id=agent_id, verifier=verifier, **filters
        )

    def list_verified_feedback(self, agent_id, verifier, **page):
        return self.view(
            "reputation", "list_verified_feedback", agent_id=agent_id, verifier=verifier, **page
        )

    def read_all_feedback(self, agent_id, **filters):
        return self.view("reputation", "read_all_feedback", agent_id=agent_id, **filters)

    def get_response_count(self, agent_id, **filters):
        return self.view("reputation", "get_response_count", agent_id=agent_id, **filters)

    def get_clients(self, agent_id, **page):
        return self.view("reputation", "get_clients", agent_id=agent_id, **page)

    def get_last_index(self, agent_id, client_address):
        return self.view(
            "reputation", "get_last_index", agent_id=agent_id, client_address=client_address
        )

    def get_identity_registry(self):
        return self.view("reputation", "get_identity_registry")

    def get_proof_feedback(self, verifier, proof, client_address):
        return self.view(
            "reputation",
            "get_proof_feedback",
            verifier=verifier,
            proof=proof,
            client_address=client_address,
        )

    def is_proof_pending(self, verifier, proof, client_address):
        return self.view(
            "reputation",
            "is_proof_pending",
            verifier=verifier,
            proof=proof,
            client_address=client_address,
        )


_EVENT_DATA = {
    "nft_mint": (
        "identity",
        "nep171",
        _record({"owner_id": _acc, "token_ids": _list(_decimal, 1)}),
    ),
    "nft_transfer": (
        "identity",
        "nep171",
        _record(
            {"old_owner_id": _acc, "new_owner_id": _acc, "token_ids": _list(_decimal, 1)},
            {"memo": lambda v: _text(v, 256), "authorized_id": _acc},
        ),
    ),
    "registered": (
        "identity",
        "erc8004",
        _record({"agent_id": _decimal, "agent_uri": lambda v: _uri(v, 8192), "owner": _acc}),
    ),
    "metadata_set": (
        "identity",
        "erc8004",
        _record({"agent_id": _decimal, "metadata_key": _key, "metadata_value": _base64}),
    ),
    "uri_updated": (
        "identity",
        "erc8004",
        _record({"agent_id": _decimal, "new_uri": lambda v: _uri(v, 8192), "updated_by": _acc}),
    ),
    "agent_wallet_pending": (
        "identity",
        "erc8004",
        _record({"agent_id": _decimal, "wallet": _nullable(_acc), "deadline": _decimal}),
    ),
    "new_feedback": (
        "reputation",
        "erc8004",
        _record(
            {
                **_ref_fields,
                **{k: v for k, v in _feedback_fields.items() if k != "is_revoked"},
                "endpoint": lambda v: _uri(v, 1024),
                "feedback_uri": lambda v: _uri(v, 4096),
                "feedback_hash": _hash,
                "proof": _nullable(_record(_proof_fields)),
            }
        ),
    ),
    "feedback_revoked": ("reputation", "erc8004", _record(_ref_fields)),
    "response_appended": (
        "reputation",
        "erc8004",
        _record(
            {
                **_ref_fields,
                "responder": _acc,
                "response_uri": lambda v: _uri(v, 4096, False),
                "response_hash": _hash,
            }
        ),
    ),
}


def parse_erc8004_events(logs: object, *, network: str, executor_id: str) -> list[dict[str, Any]]:
    """Parse logs only after authenticating the receipt's executor and success.

    Prefer parse_erc8004_receipt_events for RPC receipts. Neither function proves
    the authenticity of the RPC itself. Invalid event batches are discarded.
    """
    contracts = _contracts(network)
    if not isinstance(logs, list) or executor_id not in (
        contracts["identity"],
        contracts["reputation"],
    ):
        return []
    events = []
    for log in logs:
        if not isinstance(log, str) or not log.startswith("EVENT_JSON:"):
            continue
        try:
            event = loads_bounded(log[11:])
        except ValueError:
            continue
        if not isinstance(event, dict) or not isinstance(event.get("event"), str):
            continue
        spec = _EVENT_DATA.get(event["event"])
        if spec is None:
            continue
        role, standard, check = spec
        if (
            executor_id != contracts[role]
            or event.get("standard") != standard
            or event.get("version") != "1.0.0"
            or not isinstance(event.get("data"), list)
            or len(event["data"]) != 1
            or not check(event["data"][0])
        ):
            continue
        data = event["data"][0]
        if (
            event["event"] == "new_feedback"
            and data["proof"] is not None
            and data["proof"]["verifier"] not in _verifiers(network)
        ):
            continue
        if event["event"] in ("nft_mint", "nft_transfer") and len(data["token_ids"]) != 1:
            continue
        if event["event"] == "agent_wallet_pending" and (data["wallet"] is None) != (
            data["deadline"] == "0"
        ):
            continue
        if (
            event["event"] == "metadata_set"
            and data["metadata_key"] == "agentWallet"
            and data["metadata_value"]
        ):
            try:
                wallet = base64.b64decode(data["metadata_value"]).decode("utf-8")
            except UnicodeError:
                continue
            if not _acc(wallet):
                continue
        events.append(event)
    return events


def _successful(status):
    return (
        isinstance(status, dict)
        and "Failure" not in status
        and (
            isinstance(status.get("SuccessValue"), str)
            or isinstance(status.get("SuccessReceiptId"), str)
        )
    )


def _receipt_outcomes(receipts, network):
    contracts = _contracts(network)
    if not isinstance(receipts, list):
        return
    for receipt in receipts:
        if not isinstance(receipt, dict):
            continue
        outcome = receipt.get("outcome")
        if (
            isinstance(outcome, dict)
            and outcome.get("executor_id") in contracts.values()
            and _successful(outcome.get("status"))
        ):
            yield outcome


def parse_erc8004_receipt_events(receipts_outcome: object, network: str) -> list[dict[str, Any]]:
    _contracts(network)
    events = []
    for outcome in _receipt_outcomes(receipts_outcome, network):
        events.extend(
            parse_erc8004_events(
                outcome.get("logs"), network=network, executor_id=outcome["executor_id"]
            )
        )
    return events


@dataclass(frozen=True)
class Erc8004Outcome:
    status: str
    value: Any = None
    rejection: Erc8004Rejection | None = None
    panic: Erc8004PanicCode | None = None


def _result_matches(data, args, method):
    if method == "register":
        return data["agent_uri"] == args.get("agent_uri", "")
    for field in (
        "agent_id",
        "value",
        "value_decimals",
        "tag1",
        "tag2",
        "endpoint",
        "feedback_uri",
        "feedback_hash",
    ):
        if data[field] != args.get(field, None if field == "feedback_hash" else ""):
            return False
    if method == "give_feedback":
        return data["proof"] is None
    proof = data["proof"]
    return (
        isinstance(proof, dict)
        and proof["verifier"] == args["verifier"]
        and proof["proof"] == args["proof"]
    )


def parse_erc8004_outcome(outcome: object, *, network: str, method: str) -> Erc8004Outcome:
    """Decode a final RPC transaction outcome, bound to method and deployment.

    register and feedback successes require a matching event from a successful
    receipt of the correct contract. Null feedback requires a known callback
    rejection from that same reputation contract, never from root/foreign logs.
    Pending and malformed responses raise ValueError.
    """
    contracts = _contracts(network)
    _require(isinstance(method, str) and method in _WRITE_ARGS, "unsupported result method")
    _require(isinstance(outcome, dict), "expected transaction outcome")
    if "result" in outcome and "status" not in outcome:
        _require("error" not in outcome, "RPC error")
        outcome = outcome["result"]
    _require(isinstance(outcome, dict), "invalid transaction result")
    tx = outcome.get("transaction")
    role = _WRITE_ARGS[method][0]
    # verify_feedback_proof goes to the verifier of its config's escrow: any registered one.
    receivers = _verifiers(network) if role == "verifier" else {contracts[role]}
    _require(
        isinstance(tx, dict)
        and isinstance(tx.get("receiver_id"), str)
        and tx["receiver_id"] in receivers
        and _acc(tx.get("signer_id")),
        "transaction provenance mismatch",
    )
    actions = tx.get("actions")
    _require(
        isinstance(actions, list)
        and len(actions) == 1
        and isinstance(actions[0], dict)
        and set(actions[0]) == {"FunctionCall"}
        and isinstance(actions[0]["FunctionCall"], dict)
        and actions[0]["FunctionCall"].get("method_name") == method,
        "transaction method mismatch",
    )
    status = outcome.get("status")
    _require(isinstance(status, dict), "missing final outcome status")
    if set(status) == {"Failure"}:
        match = re.search(r"\bERC8004_(?:ID|REP|ARV)_E[0-9]{2}\b", _json(status["Failure"]))
        _require(match is not None, "unrecognized transaction failure")
        try:
            return Erc8004Outcome("error", panic=Erc8004PanicCode(match[0]))
        except ValueError:
            raise ValueError("unrecognized transaction failure") from None
    _require(
        set(status) == {"SuccessValue"} and isinstance(status["SuccessValue"], str),
        "outcome is not final SuccessValue",
    )
    encoded = status["SuccessValue"]
    _require(len(encoded) <= MAX_VIEW_BYTES, "outcome too large")
    try:
        raw = base64.b64decode(encoded, validate=True)
        _require(base64.b64encode(raw).decode() == encoded, "noncanonical SuccessValue")
        value = loads_bounded(raw) if raw else None
    except ValueError:
        raise ValueError("invalid SuccessValue") from None
    if method == "register" or method.startswith("give_feedback"):
        if value is None and method.startswith("give_feedback"):
            _require(bool(raw), "feedback result must encode JSON null")
            codes = set()
            for receipt in _receipt_outcomes(outcome.get("receipts_outcome"), network):
                if receipt["executor_id"] != contracts["reputation"]:
                    continue
                logs = receipt.get("logs")
                if not isinstance(logs, list):
                    continue
                for log in logs:
                    if isinstance(log, str) and log.startswith("erc8004:rechazo:"):
                        try:
                            codes.add(Erc8004Rejection(log[len("erc8004:rechazo:") :]))
                        except ValueError:
                            continue
            _require(len(codes) == 1, "null feedback requires one recognized rejection")
            return Erc8004Outcome("rejected", rejection=codes.pop())
        _require(_decimal(value, positive=method != "register"), "invalid result identifier")
        encoded_args = actions[0]["FunctionCall"].get("args")
        _require(
            isinstance(encoded_args, str) and len(encoded_args) <= MAX_VIEW_BYTES,
            "invalid transaction args",
        )
        try:
            tx_args = loads_bounded(base64.b64decode(encoded_args, validate=True))
        except ValueError:
            raise ValueError("invalid transaction args") from None
        _, required, optional = _WRITE_ARGS[method]
        tx_args = _arguments(tx_args, required, optional)
        events = parse_erc8004_receipt_events(outcome.get("receipts_outcome"), network)
        name = "registered" if method == "register" else "new_feedback"
        key = "agent_id" if method == "register" else "feedback_index"
        author = "owner" if method == "register" else "client_address"
        _require(
            any(
                e["event"] == name
                and e["data"][0][key] == value
                and e["data"][0][author] == tx["signer_id"]
                and _result_matches(e["data"][0], tx_args, method)
                for e in events
            ),
            "result identifier lacks matching receipt event",
        )
    elif method in ("set_agent_wallet", "nft_transfer_call"):
        _require(_boolean(value), "expected boolean result")
    elif method == "verify_feedback_proof":
        _require(
            _record({k: v for k, v in _proof_fields.items() if k not in ("verifier", "proof")})(
                value
            ),
            "invalid verifier result",
        )
    elif method == "nft_approve":
        # An optional receiver callback chooses its own JSON return value.
        pass
    else:
        _require(value is None, "expected empty result")
    return Erc8004Outcome("accepted", value=value)


__all__ = [
    "NEAR_ERC8004_CONTRACTS",
    "NEAR_ERC8004_ESCROWS",
    "NEAR_ERC8004_ESCROW_VERIFIERS",
    "near_erc8004_verifier",
    "ERC8004_DEPOSIT_CEILINGS",
    "MAX_WALLET_DELAY_NS",
    "Erc8004Config",
    "Erc8004Rejection",
    "Erc8004PanicCode",
    "Erc8004Outcome",
    "NearErc8004Client",
    "agent_registry",
    "parse_erc8004_config",
    "build_erc8004_call",
    "erc8004_args_json",
    "sign_erc8004_transaction",
    "parse_erc8004_events",
    "parse_erc8004_receipt_events",
    "parse_erc8004_outcome",
    "build_erc8004_register",
    "build_erc8004_set_agent_uri",
    "build_erc8004_set_metadata",
    "build_erc8004_set_agent_wallet",
    "build_erc8004_accept_agent_wallet",
    "build_erc8004_unset_agent_wallet",
    "build_erc8004_nft_transfer",
    "build_erc8004_nft_transfer_call",
    "build_erc8004_nft_approve",
    "build_erc8004_nft_revoke",
    "build_erc8004_nft_revoke_all",
    "build_erc8004_give_feedback",
    "build_erc8004_give_feedback_with_proof",
    "build_erc8004_revoke_feedback",
    "build_erc8004_append_response",
    "build_erc8004_verify_feedback_proof",
]
