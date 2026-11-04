"""Arras registry v1: offline transactions, strict views and reputation display.

Transactions attach NEAR and need a FullAccess key. The caller supplies the next
nonce and a recent block hash; no helper submits or allocates either. State-based
errors (registration, storage growth, payment eligibility) remain contract checks.
"""

from __future__ import annotations

import base64
import hashlib
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Any, Literal, TypedDict, cast

import httpx

from .bounded_json import loads_bounded
from .near_escrow import NearLocalSigner, Unsigned, _account, _json, _uint
from .near_events import _boolean, _integer, _record
from .networks.near import NEAR, BorshSerializer
from .solana_signing import _b58decode

NEAR_REGISTRY_CONTRACTS: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {"near:mainnet": ("registry.arras-escrow.near",), "near:testnet": ()}
)
NEAR_REGISTRY_ESCROWS: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {"near:mainnet": ("v1.arras-escrow.near",), "near:testnet": ()}
)
REGISTER_DEPOSIT = "17510000000000000000000"
FEEDBACK_DEPOSIT = "18640000000000000000000"
REGISTRY_GAS = "30000000000000"
FEEDBACK_GAS = "100000000000000"
MIN_FEEDBACK_GAS = 60_000_000_000_000


class RegistryErrorCode(str, Enum):
    E01 = "E01_AGENT_URI_INVALIDA"
    E02 = "E02_METADATA_INVALIDA"
    E03 = "E03_YA_REGISTRADO"
    E04 = "E04_NO_REGISTRADO"
    E05 = "E05_DEPOSITO_INSUFICIENTE"
    E06 = "E06_REQUIERE_DEPOSITO"
    E10 = "E10_ESCROW_NO_PERMITIDO"
    E11 = "E11_PAYMENT_ID_INVALIDO"
    E12 = "E12_SCORE_INVALIDO"
    E13 = "E13_TAG_INVALIDO"
    E14 = "E14_FEEDBACK_URI_INVALIDA"
    E15 = "E15_FEEDBACK_HASH_INVALIDO"
    E16 = "E16_DEPOSITO_FEEDBACK"
    E17 = "E17_GAS_INSUFICIENTE"
    E18 = "E18_YA_CALIFICADO"
    E19 = "E19_EN_VUELO"
    E20 = "E20_GET_PAYMENT_FALLO"
    E21 = "E21_RESPUESTA_INVALIDA"
    E22 = "E22_PAGO_INEXISTENTE"
    E23 = "E23_PAGO_SIN_LIBERAR"
    E24 = "E24_NO_ES_PARTE"
    E25 = "E25_AUTO_CALIFICACION"
    E26 = "E26_REEMPLAZADO"
    E30 = "E30_FEEDBACK_INEXISTENTE"
    E31 = "E31_YA_REVOCADO"
    E40 = "E40_ESCROWS_INVALIDOS"


class RegistryError(ValueError):
    """A locally detected input error or a contract panic, with a stable code."""

    def __init__(self, code: RegistryErrorCode, detail: str = "") -> None:
        self.code = code
        super().__init__(f"ARRAS_REG_{code.name}: {code.value}" + (f": {detail}" if detail else ""))


def _require(ok: bool, code: RegistryErrorCode) -> None:
    if not ok:
        raise RegistryError(code)


def _uri(value: object, code: RegistryErrorCode) -> None:
    _require(isinstance(value, str), code)
    _require(1 <= len(value) <= 512, code)
    _require(all(0x21 <= ord(c) <= 0x7E for c in value), code)
    _require(re.fullmatch(r"[A-Za-z][A-Za-z0-9+.-]*:.+", value) is not None, code)


def _metadata(value: object) -> None:
    if value is None:
        return
    _require(isinstance(value, str), RegistryErrorCode.E02)
    try:
        size = len(value.encode("utf-8"))
    except UnicodeEncodeError:
        raise RegistryError(RegistryErrorCode.E02) from None
    _require(1 <= size <= 1024, RegistryErrorCode.E02)
    _require(all(ord(c) >= 0x20 and ord(c) != 0x7F for c in value), RegistryErrorCode.E02)


def _tag(value: object) -> None:
    if value is None:
        return
    _require(isinstance(value, str), RegistryErrorCode.E13)
    _require(1 <= len(value) <= 64, RegistryErrorCode.E13)
    _require(re.fullmatch(r"[A-Za-z0-9._:/-]+", value) is not None, RegistryErrorCode.E13)


def _payment(value: object) -> None:
    _require(isinstance(value, str), RegistryErrorCode.E11)
    _require(1 <= len(value) <= 64, RegistryErrorCode.E11)
    _require(all(0x21 <= ord(c) <= 0x7E for c in value), RegistryErrorCode.E11)


def _hash(value: object) -> None:
    if value is None:
        return
    _require(isinstance(value, str), RegistryErrorCode.E15)
    _require(len(value) == 64, RegistryErrorCode.E15)
    _require(re.fullmatch(r"[0-9a-f]+", value) is not None, RegistryErrorCode.E15)


def _receiver(network: str, receiver_id: str | None) -> str:
    if network not in NEAR_REGISTRY_CONTRACTS:
        raise ValueError("unsupported NEAR registry network")
    contracts = NEAR_REGISTRY_CONTRACTS[network]
    receiver = receiver_id if receiver_id is not None else next(iter(contracts), None)
    if receiver not in contracts:
        raise ValueError("receiver_id is not registered for this network")
    return cast(str, receiver)


def _escrow(network: str, escrow: str) -> None:
    _require(escrow in NEAR_REGISTRY_ESCROWS[network], RegistryErrorCode.E10)


def _amount(value: Unsigned, code: RegistryErrorCode) -> int:
    try:
        return _uint(value, 128, "deposit")
    except ValueError:
        raise RegistryError(code) from None


class RegistryCallParams(TypedDict):
    methodName: str
    args: dict[str, Any]
    gas: str
    deposit: str


class RegistryAction(TypedDict):
    type: Literal["FunctionCall"]
    params: RegistryCallParams


class NearRegistryTransaction(TypedDict):
    """Wallet-selector shape, identical field names to NearStorageTransaction."""

    signerId: str
    receiverId: str
    actions: list[RegistryAction]


def _build(network, signer_id, receiver_id, method, args, deposit, gas):
    receiver = _receiver(network, receiver_id)
    _account(signer_id, "signer_id")
    gas_units = _uint(gas, 64, "gas")
    if method == "give_feedback":
        _require(gas_units >= MIN_FEEDBACK_GAS, RegistryErrorCode.E17)
    if not 0 < gas_units <= 300_000_000_000_000:
        raise ValueError("gas must be positive and at most 300 TGas")
    return {
        "signerId": signer_id,
        "receiverId": receiver,
        "actions": [
            {
                "type": "FunctionCall",
                "params": {
                    "methodName": method,
                    "args": args,
                    "gas": str(gas_units),
                    "deposit": str(deposit),
                },
            }
        ],
    }


def build_registry_register(
    *,
    network: str,
    signer_id: str,
    agent_uri: str,
    metadata: str | None = None,
    receiver_id: str | None = None,
    deposit: Unsigned = REGISTER_DEPOSIT,
    gas: Unsigned = REGISTRY_GAS,
) -> NearRegistryTransaction:
    """Build a registration. Pass get_config's deposit if it exceeds the default."""
    _uri(agent_uri, RegistryErrorCode.E01)
    _metadata(metadata)
    amount = _amount(deposit, RegistryErrorCode.E05)
    _require(amount >= int(REGISTER_DEPOSIT), RegistryErrorCode.E05)
    args = {"agent_uri": agent_uri}
    if metadata is not None:
        args["metadata"] = metadata
    return _build(network, signer_id, receiver_id, "register", args, amount, gas)


def build_registry_update(
    *,
    network: str,
    signer_id: str,
    agent_uri: str,
    metadata: str | None = None,
    receiver_id: str | None = None,
    deposit: Unsigned = REGISTER_DEPOSIT,
    gas: Unsigned = REGISTRY_GAS,
) -> NearRegistryTransaction:
    """Replace both fields; omitted metadata clears it. Growth is charged on-chain.

    The conservative default covers a full record. A known non-growing update
    can attach 1 yocto; the contract checks the actual storage cost atomically.
    """
    amount = _amount(deposit, RegistryErrorCode.E06)
    _require(amount >= 1, RegistryErrorCode.E06)
    _uri(agent_uri, RegistryErrorCode.E01)
    _metadata(metadata)
    args = {"agent_uri": agent_uri}
    if metadata is not None:
        args["metadata"] = metadata
    return _build(network, signer_id, receiver_id, "update", args, amount, gas)


def build_give_feedback(
    *,
    network: str,
    signer_id: str,
    escrow: str,
    payment_id: str,
    score: int,
    tag1: str | None = None,
    tag2: str | None = None,
    feedback_uri: str | None = None,
    feedback_hash: str | None = None,
    receiver_id: str | None = None,
    deposit: Unsigned = FEEDBACK_DEPOSIT,
    gas: Unsigned = FEEDBACK_GAS,
) -> NearRegistryTransaction:
    """Build payment-backed feedback, omitting absent optional args."""
    _receiver(network, receiver_id)
    _escrow(network, escrow)
    _payment(payment_id)
    _require(type(score) is int and 0 <= score <= 100, RegistryErrorCode.E12)
    _tag(tag1)
    _tag(tag2)
    if feedback_uri is not None:
        _uri(feedback_uri, RegistryErrorCode.E14)
    _hash(feedback_hash)
    amount = _amount(deposit, RegistryErrorCode.E16)
    _require(amount >= int(FEEDBACK_DEPOSIT), RegistryErrorCode.E16)
    args = {"escrow": escrow, "payment_id": payment_id, "score": score}
    for key, value in (
        ("tag1", tag1),
        ("tag2", tag2),
        ("feedback_uri", feedback_uri),
        ("feedback_hash", feedback_hash),
    ):
        if value is not None:
            args[key] = value
    return _build(network, signer_id, receiver_id, "give_feedback", args, amount, gas)


def build_revoke_feedback(
    *,
    network: str,
    signer_id: str,
    escrow: str,
    payment_id: str,
    receiver_id: str | None = None,
    deposit: Unsigned = "1",
    gas: Unsigned = REGISTRY_GAS,
) -> NearRegistryTransaction:
    """Revoke the signer's feedback, attaching exactly one yoctoNEAR."""
    _receiver(network, receiver_id)
    _escrow(network, escrow)
    _payment(payment_id)
    if _uint(deposit, 128, "deposit") != 1:
        raise ValueError("revoke_feedback requires exactly 1 yoctoNEAR")
    return _build(
        network,
        signer_id,
        receiver_id,
        "revoke_feedback",
        {"escrow": escrow, "payment_id": payment_id},
        1,
        gas,
    )


_BUILDERS = {
    "register": build_registry_register,
    "update": build_registry_update,
    "give_feedback": build_give_feedback,
    "revoke_feedback": build_revoke_feedback,
}


def _validated_transaction(descriptor: NearRegistryTransaction) -> NearRegistryTransaction:
    # Rebuild untrusted wallet descriptors before touching any signing key. This
    # snapshots the args too: caller-supplied signers cannot change the wire.
    try:
        if not isinstance(descriptor, dict) or set(descriptor) != {
            "signerId",
            "receiverId",
            "actions",
        }:
            raise ValueError("invalid registry descriptor")
        networks = [
            n for n, row in NEAR_REGISTRY_CONTRACTS.items() if descriptor["receiverId"] in row
        ]
        if len(networks) != 1:
            raise ValueError("receiver_id is not registered unambiguously")
        actions = descriptor["actions"]
        if not isinstance(actions, list) or len(actions) != 1:
            raise ValueError("exactly one registry action is required")
        action = actions[0]
        if set(action) != {"type", "params"} or action["type"] != "FunctionCall":
            raise ValueError("expected a FunctionCall")
        params = action["params"]
        if set(params) != {"methodName", "args", "gas", "deposit"}:
            raise ValueError("invalid FunctionCall params")
        method = params["methodName"]
        if not isinstance(method, str) or method not in _BUILDERS:
            raise ValueError("unsupported registry method")
        args = params["args"]
        if not isinstance(args, dict):
            raise ValueError("registry args must be an object")
        result = _BUILDERS[method](
            network=networks[0],
            signer_id=descriptor["signerId"],
            receiver_id=descriptor["receiverId"],
            gas=params["gas"],
            deposit=params["deposit"],
            **args,
        )
    except (KeyError, TypeError) as exc:
        raise ValueError("invalid registry descriptor") from exc
    return result


def registry_args_json(descriptor: NearRegistryTransaction) -> bytes:
    """Validated compact UTF-8 args, in ABI order; absent optionals are omitted."""
    return _json(_validated_transaction(descriptor)["actions"][0]["params"]["args"]).encode("utf-8")


def sign_registry_transaction(
    descriptor: NearRegistryTransaction,
    signer: NearLocalSigner,
    nonce: Unsigned,
    block_hash: str,
) -> str:
    """Return base64 Borsh SignedTransaction (ordinary NEAR V0, not a delegate).

    The caller must verify the selected key is FullAccess on signerId, supply its
    NEXT nonce and a recent block hash, and persist the result before send_tx.
    Key permissions cannot be discovered offline.
    """
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    tx = _validated_transaction(descriptor)
    n = _uint(nonce, 64, "nonce")
    if n == 0:
        raise ValueError("nonce must be positive")
    if not isinstance(block_hash, str) or not 32 <= len(block_hash) <= 44:
        raise ValueError("block_hash must be base58 of 32 bytes")
    block = _b58decode(block_hash)
    if len(block) != 32:
        raise ValueError("block_hash must decode to 32 bytes")
    params = tx["actions"][0]["params"]
    args = _json(params["args"]).encode("utf-8")
    public_key = signer.public_key
    if not isinstance(public_key, bytes) or len(public_key) != 32:
        raise ValueError("expected 32-byte Ed25519 public key")
    wire = (
        BorshSerializer()
        .write_string(tx["signerId"])
        .write_u8(0)
        .write_fixed_bytes(public_key)
        .write_u64(n)
        .write_string(tx["receiverId"])
        .write_fixed_bytes(block)
        .write_u32(1)
        .write_u8(2)
        .write_string(params["methodName"])
        .write_bytes(args)
        .write_u64(int(params["gas"]))
        .write_u128(int(params["deposit"]))
        .get_bytes()
    )
    digest = hashlib.sha256(wire).digest()
    signature = signer.sign(digest)
    if not isinstance(signature, bytes) or len(signature) != 64:
        raise ValueError("invalid Ed25519 transaction signature")
    try:
        Ed25519PublicKey.from_public_bytes(public_key).verify(signature, digest)
    except InvalidSignature:
        raise ValueError("invalid Ed25519 transaction signature") from None
    return base64.b64encode(wire + b"\x00" + signature).decode("ascii")


class AgentView(TypedDict):
    account_id: str
    agent_uri: str
    metadata: str | None
    registered_at: str
    updated_at: str


class FeedbackGivenData(TypedDict):
    feedback_id: int
    rater: str
    ratee: str
    ratee_role: Literal["payer", "receiver"]
    escrow: str
    payment_id: str
    operator_id: str
    released: str
    score: int
    tag1: str | None
    tag2: str | None
    feedback_uri: str | None
    feedback_hash: str | None
    created_at: str


class FeedbackView(FeedbackGivenData):
    revoked: bool
    revoked_at: str | None


class RoleSummaryView(TypedDict):
    count: int
    revoked: int
    score_sum: str
    average_x100: int | None


class _SummaryRequired(RoleSummaryView):
    account_id: str
    as_payer: RoleSummaryView
    as_receiver: RoleSummaryView
    last_feedback_id: int | None
    last_at: str | None


class SummaryView(_SummaryRequired, total=False):
    operator_id: str


class RegistryEvent(TypedDict):
    standard: Literal["arras-registry"]
    version: Literal["1.0.0"]
    event: Literal["agent_registered", "agent_updated", "feedback_given", "feedback_revoked"]
    data: list[dict[str, Any]]


Check = Callable[[object], bool]
RPC = Callable[[str, dict[str, Any]], dict[str, Any]]


def _nullable(check: Check) -> Check:
    return lambda value: value is None or check(value)


def _number(bits: int) -> Check:
    return lambda value: type(value) is int and 0 <= value < 1 << bits


def _valid(validator: Callable[..., None], *args: object) -> bool:
    """Use the write validators on untrusted view/event content as predicates."""
    try:
        validator(*args)
    except ValueError:
        return False
    return True


def _operator(value: object) -> bool:
    # Arras v1 allows empty and Unicode operator ids, bounded in UTF-8 bytes.
    if not isinstance(value, str):
        return False
    try:
        return len(value.encode("utf-8")) <= 64
    except UnicodeEncodeError:
        return False


_u32, _u64, _u128 = _number(32), _integer(64), _integer(128)


def _account_check(value: object) -> bool:
    return _valid(_account, value, "account")


def _uri_check(value: object) -> bool:
    return _valid(_uri, value, RegistryErrorCode.E01)


_agent_fields = {
    "account_id": _account_check,
    "agent_uri": _uri_check,
    "metadata": lambda v: _valid(_metadata, v),
}
_agent_view = _record({**_agent_fields, "registered_at": _u64, "updated_at": _u64})
_feedback_fields = {
    "feedback_id": _u32,
    "rater": _account_check,
    "ratee": _account_check,
    "ratee_role": lambda v: v in ("payer", "receiver"),
    "escrow": _account_check,
    "payment_id": lambda v: _valid(_payment, v),
    "operator_id": _operator,
    "released": _u128,
    "score": lambda v: type(v) is int and 0 <= v <= 100,
    "tag1": lambda v: _valid(_tag, v),
    "tag2": lambda v: _valid(_tag, v),
    "feedback_uri": _nullable(_uri_check),
    "feedback_hash": lambda v: (
        v is None or (isinstance(v, str) and re.fullmatch(r"[0-9a-f]{64}", v) is not None)
    ),
    "created_at": _u64,
}
_feedback_view = _record({**_feedback_fields, "revoked": _boolean, "revoked_at": _nullable(_u64)})
_role_fields = {
    "count": _u32,
    "revoked": _u32,
    "score_sum": _u64,
    "average_x100": _nullable(lambda v: type(v) is int and 0 <= v <= 10000),
}
_summary_view = _record(
    {
        **_role_fields,
        "account_id": _account_check,
        "as_payer": _record(_role_fields),
        "as_receiver": _record(_role_fields),
        "last_feedback_id": _nullable(_u32),
        "last_at": _nullable(_u64),
    },
    {"operator_id": _operator},
)
_config_view = _record(
    {
        "version": lambda v: v == "arras-registry-v1",
        "escrows": lambda v: isinstance(v, list) and all(_account_check(s) for s in v),
        "agents": _u32,
        "feedbacks": _u32,
        "register_deposit": _u128,
        "feedback_deposit": _u128,
        "min_gas_give_feedback": _u64,
        "limits": _record(
            {
                **dict.fromkeys(
                    (
                        "max_agent_uri_len",
                        "max_metadata_len",
                        "max_feedback_uri_len",
                        "max_tag_len",
                        "max_payment_id_len",
                        "max_list_limit",
                    ),
                    _u32,
                ),
                "max_score": _number(8),
                "pending_ttl_blocks": _u64,
            }
        ),
    }
)
_EVENT_CHECKS = {
    "agent_registered": _record({**_agent_fields, "at": _u64}),
    "agent_updated": _record({**_agent_fields, "at": _u64}),
    "feedback_given": _record(_feedback_fields),
    "feedback_revoked": _record(
        {
            "feedback_id": _u32,
            "rater": _account_check,
            "ratee": _account_check,
            "escrow": _account_check,
            "payment_id": lambda v: _valid(_payment, v),
            "revoked_at": _u64,
        }
    ),
}


def _view(value: Any, check: Check) -> Any:
    if not check(value):
        raise ValueError("unexpected registry view shape")
    return value


def _page(from_index: int | None, limit: int | None) -> dict[str, int]:
    result = {}
    for key, value in (("from_index", from_index), ("limit", limit)):
        if value is not None:
            if not _u32(value):
                raise ValueError(f"{key}: expected u32 JSON number")
            result[key] = value
    return result


class NearRegistryClient:
    """Read-only, synchronous client with an injectable rpc(method, params).

    The transport returns the complete JSON-RPC response dict. A default httpx
    call uses the network RPC URL with a 30s timeout. Methods preserve U64/U128
    strings and reject incomplete or malformed views. No write is ever sent.
    """

    def __init__(
        self,
        network: str = "near:mainnet",
        *,
        rpc: RPC | None = None,
        receiver_id: str | None = None,
    ) -> None:
        self.receiver_id = _receiver(network, receiver_id)
        self.network = network
        self.rpc = rpc if rpc is not None else self._http_rpc

    def _http_rpc(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        # Only mainnet has a registered deployment. Testnet is refused in __init__.
        response = httpx.post(
            NEAR.rpc_url,
            json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
            timeout=30,
        )
        response.raise_for_status()
        return loads_bounded(response.content)

    def _call(self, method: str, args: dict[str, Any], check: Check) -> Any:
        _receiver(self.network, self.receiver_id)
        response = self.rpc(
            "query",
            {
                "request_type": "call_function",
                "finality": "final",
                "account_id": self.receiver_id,
                "method_name": method,
                "args_base64": base64.b64encode(_json(args).encode("utf-8")).decode("ascii"),
            },
        )
        if not isinstance(response, dict) or "error" in response:
            raise ValueError("registry RPC failed")
        result = response.get("result")
        if not isinstance(result, dict) or "error" in result:
            raise ValueError("invalid registry call_function response")
        raw = result.get("result")
        if (
            not isinstance(raw, list)
            or len(raw) > 512 * 1024
            or not all(type(b) is int and 0 <= b <= 255 for b in raw)
        ):
            raise ValueError("invalid registry result bytes")
        return _view(loads_bounded(bytes(raw)), check)

    def get_config(self) -> dict[str, Any]:
        return self._call("get_config", {}, _config_view)

    def get_agent(self, account_id: str) -> AgentView | None:
        _account(account_id, "account_id")
        return self._call("get_agent", {"account_id": account_id}, _nullable(_agent_view))

    def list_agents(
        self, *, from_index: int | None = None, limit: int | None = None
    ) -> list[AgentView]:
        return self._call(
            "list_agents",
            _page(from_index, limit),
            lambda v: isinstance(v, list) and all(_agent_view(x) for x in v),
        )

    def get_feedback(self, feedback_id: int) -> FeedbackView | None:
        if not _u32(feedback_id):
            raise ValueError("feedback_id: expected u32 JSON number")
        return self._call("get_feedback", {"feedback_id": feedback_id}, _nullable(_feedback_view))

    def _payment_args(self, escrow: str, payment_id: str, rater: str) -> dict[str, str]:
        _escrow(self.network, escrow)
        _payment(payment_id)
        _account(rater, "rater")
        return {"escrow": escrow, "payment_id": payment_id, "rater": rater}

    def get_feedback_for_payment(
        self, escrow: str, payment_id: str, rater: str
    ) -> FeedbackView | None:
        return self._call(
            "get_feedback_for_payment",
            self._payment_args(escrow, payment_id, rater),
            _nullable(_feedback_view),
        )

    def list_feedback(
        self, account_id: str, *, from_index: int | None = None, limit: int | None = None
    ) -> list[FeedbackView]:
        _account(account_id, "account_id")
        return self._call(
            "list_feedback",
            {"account_id": account_id, **_page(from_index, limit)},
            lambda v: isinstance(v, list) and all(_feedback_view(x) for x in v),
        )

    def get_summary(self, account_id: str) -> SummaryView:
        _account(account_id, "account_id")
        return self._call(
            "get_summary",
            {"account_id": account_id},
            lambda v: _summary_view(v) and "operator_id" not in v,
        )

    def get_summary_by_operator(self, account_id: str, operator_id: str) -> SummaryView:
        _account(account_id, "account_id")
        if not isinstance(operator_id, str):
            raise ValueError("operator_id must be a string")
        return self._call(
            "get_summary_by_operator",
            {"account_id": account_id, "operator_id": operator_id},
            lambda v: _summary_view(v) and v.get("operator_id") == operator_id,
        )

    def is_feedback_pending(self, escrow: str, payment_id: str, rater: str) -> bool:
        return self._call(
            "is_feedback_pending", self._payment_args(escrow, payment_id, rater), _boolean
        )


def parse_registry_events(logs: Iterable[object]) -> list[RegistryEvent]:
    """Like parse_arras_events: skip foreign/malformed logs and invalid batches.

    Unknown fields are retained in data; integer strings stay strings. Safe only
    when the caller has already filtered receipts by the registry executor_id.
    Prefer parse_registry_receipt_events for RPC receipts_outcome arrays. Neither
    parser proves settlement or authenticates an untrusted RPC.
    """
    result = []
    if not isinstance(logs, Iterable) or isinstance(logs, (str, bytes, dict)):
        return result
    for log in logs:
        if not isinstance(log, str) or not log.startswith("EVENT_JSON:"):
            continue
        try:
            value = loads_bounded(log[11:])
        except ValueError:
            continue
        if (
            not isinstance(value, dict)
            or value.get("standard") != "arras-registry"
            or value.get("version") != "1.0.0"
            or not isinstance(value.get("event"), str)
            or value["event"] not in _EVENT_CHECKS
            or not isinstance(value.get("data"), list)
        ):
            continue
        if not all(_EVENT_CHECKS[value["event"]](item) for item in value["data"]):
            continue
        result.append(
            {
                "standard": "arras-registry",
                "version": "1.0.0",
                "event": value["event"],
                "data": value["data"],
            }
        )
    return result


def parse_registry_receipt_events(receipts_outcome: object, network: str) -> list[RegistryEvent]:
    """Parse only successful receipts from a registered contract on this network.

    Pass the JSON-RPC transaction result's receipts_outcome array. Malformed
    receipts and logs are skipped; an unknown network raises ValueError. This
    function requires a string SuccessValue or SuccessReceiptId in outcome.status
    and rejects any status containing Failure, even alongside a success field.
    Missing, unknown and malformed statuses discard the entire receipt. Other
    fields are allowed, matching TypeScript; success strings are not decoded.
    A known network with no registered deployment yields no events. The RPC must still
    be trusted: executor_id is provenance within its response, not a proof.
    """
    if not isinstance(network, str) or network not in NEAR_REGISTRY_CONTRACTS:
        raise ValueError("unsupported NEAR registry network")
    contracts = NEAR_REGISTRY_CONTRACTS[network]
    result = []
    if not isinstance(receipts_outcome, list):
        return result
    for receipt in receipts_outcome:
        if not isinstance(receipt, dict):
            continue
        outcome = receipt.get("outcome")
        if not isinstance(outcome, dict) or outcome.get("executor_id") not in contracts:
            continue
        status = outcome.get("status")
        if (
            not isinstance(status, dict)
            or "Failure" in status
            or not (
                isinstance(status.get("SuccessValue"), str)
                or isinstance(status.get("SuccessReceiptId"), str)
            )
        ):
            continue
        result.extend(parse_registry_events(outcome.get("logs")))
    return result


@dataclass(frozen=True)
class FeedbackOutcome:
    status: Literal["accepted", "rejected", "error"]
    feedback_id: int | None = None
    code: RegistryErrorCode | None = None


_REJECTIONS = frozenset(
    RegistryErrorCode[n] for n in ("E18", "E20", "E21", "E22", "E23", "E24", "E25", "E26")
)


def parse_give_feedback_outcome(outcome: Mapping[str, Any]) -> FeedbackOutcome:
    """Decode a final NEAR execution outcome, optionally in a JSON-RPC envelope.

    SuccessValue is base64 JSON. Only a known rejection log explains null; an
    incomplete/pending/unknown response raises ValueError, never means accepted.
    A known final contract panic returns status='error' with its enum code.
    Receipt logs do not authenticate their emitter: use a trusted RPC/outcome.
    """
    if not isinstance(outcome, Mapping):
        raise ValueError("expected a final registry execution outcome")
    if "result" in outcome and "status" not in outcome:
        outcome = outcome["result"]
    if not isinstance(outcome, Mapping) or not isinstance(outcome.get("status"), dict):
        raise ValueError("missing final registry outcome status")
    status = outcome["status"]
    if set(status) == {"Failure"}:
        failure = _json(status["Failure"])
        match = re.search(r"ARRAS_REG_(E[0-9]{2}):", failure)
        if match and match[1] in RegistryErrorCode.__members__:
            return FeedbackOutcome("error", code=RegistryErrorCode[match[1]])
        raise ValueError("unrecognized registry transaction failure")
    if set(status) != {"SuccessValue"} or not isinstance(status["SuccessValue"], str):
        raise ValueError("registry outcome is not a final SuccessValue")
    encoded = status["SuccessValue"]
    if len(encoded) > 512 * 1024:
        raise ValueError("registry outcome is too large")
    try:
        value = loads_bounded(base64.b64decode(encoded, validate=True))
    except ValueError:
        raise ValueError("invalid registry SuccessValue") from None
    if _u32(value):
        return FeedbackOutcome("accepted", feedback_id=value)
    if value is not None:
        raise ValueError("expected u32 feedback_id or null")
    root_logs = outcome.get("logs", [])
    receipts = outcome.get("receipts_outcome", [])
    if not isinstance(root_logs, list) or not isinstance(receipts, list):
        raise ValueError("invalid registry outcome logs")
    logs = list(root_logs)
    for receipt in receipts:
        if isinstance(receipt, dict) and isinstance(receipt.get("outcome"), dict):
            receipt_logs = receipt["outcome"].get("logs", [])
            if not isinstance(receipt_logs, list):
                raise ValueError("invalid registry receipt logs")
            logs.extend(receipt_logs)
    codes = set()
    for log in logs:
        if isinstance(log, str) and log.startswith("arras-registry:rechazo:"):
            try:
                code = RegistryErrorCode(log[len("arras-registry:rechazo:") :])
            except ValueError:
                continue
            if code in _REJECTIONS:
                codes.add(code)
    if len(codes) != 1:
        raise ValueError("null feedback result needs one recognized rejection code")
    return FeedbackOutcome("rejected", code=codes.pop())


class FeedbackSummary(TypedDict):
    count: int
    distinct_raters: int
    revoked: int
    released_total: str
    average_x100: int | None
    weighted_average_x100: int | None


def summarize_feedback(
    feedbacks: Iterable[FeedbackView], escrow: str, operator_id: str
) -> FeedbackSummary:
    """Summarize one ratee's complete, deduplicated FeedbackView list locally.

    Exact escrow/operator match; revoked rows count only in revoked. Distinct
    raters counts active raters. Both averages use integer floor, weighted by
    the released snapshot. Empty sets (or zero weight) have None averages.
    released_total is an atomic-unit decimal string, potentially above U128.
    No pagination or deduplication is implicit; fetch each page once per ratee.
    """
    selected = [
        f
        for f in feedbacks
        if _view(f, _feedback_view)["escrow"] == escrow and f["operator_id"] == operator_id
    ]
    active = [f for f in selected if not f["revoked"]]
    count = len(active)
    released = sum(int(f["released"]) for f in active)
    score_sum = sum(f["score"] for f in active)
    weighted = sum(f["score"] * int(f["released"]) for f in active)
    return {
        "count": count,
        "distinct_raters": len({f["rater"] for f in active}),
        "revoked": len(selected) - count,
        "released_total": str(released),
        "average_x100": score_sum * 100 // count if count else None,
        "weighted_average_x100": weighted * 100 // released if released else None,
    }


def safe_link(uri: str) -> str | None:
    """Allow only valid registry URIs with https, ipfs or ar schemes; preserve bytes."""
    try:
        _uri(uri, RegistryErrorCode.E01)
    except RegistryError:
        return None
    return uri if uri.split(":", 1)[0].lower() in ("https", "ipfs", "ar") else None


# Unicode 17.0.0: Cc/Cf/Cs union Default_Ignorable_Code_Point, adjacent
# ranges merged. Pinning also removes dependence on Python's Unicode version.
# Sources: https://www.unicode.org/Public/17.0.0/ucd/UnicodeData.txt
# https://www.unicode.org/Public/17.0.0/ucd/DerivedCoreProperties.txt
_DISPLAY_REMOVED_RANGES = (
    (0x0000, 0x001F),
    (0x007F, 0x009F),
    (0x00AD, 0x00AD),
    (0x034F, 0x034F),
    (0x0600, 0x0605),
    (0x061C, 0x061C),
    (0x06DD, 0x06DD),
    (0x070F, 0x070F),
    (0x0890, 0x0891),
    (0x08E2, 0x08E2),
    (0x115F, 0x1160),
    (0x17B4, 0x17B5),
    (0x180B, 0x180F),
    (0x200B, 0x200F),
    (0x202A, 0x202E),
    (0x2060, 0x206F),
    (0x3164, 0x3164),
    (0xD800, 0xDFFF),
    (0xFE00, 0xFE0F),
    (0xFEFF, 0xFEFF),
    (0xFFA0, 0xFFA0),
    (0xFFF0, 0xFFFB),
    (0x110BD, 0x110BD),
    (0x110CD, 0x110CD),
    (0x13430, 0x1343F),
    (0x1BCA0, 0x1BCA3),
    (0x1D173, 0x1D17A),
    (0xE0000, 0xE0FFF),
)


def display_text(s: str) -> str:
    """Remove controls, bidi, surrogates and Unicode default-ignorable points.

    This is plain text, not HTML escaping. Render through the UI's text API.
    """
    return "".join(
        c for c in s if not any(lo <= ord(c) <= hi for lo, hi in _DISPLAY_REMOVED_RANGES)
    )


__all__ = [
    "NEAR_REGISTRY_CONTRACTS",
    "NEAR_REGISTRY_ESCROWS",
    "REGISTER_DEPOSIT",
    "FEEDBACK_DEPOSIT",
    "REGISTRY_GAS",
    "FEEDBACK_GAS",
    "MIN_FEEDBACK_GAS",
    "RegistryErrorCode",
    "RegistryError",
    "NearRegistryTransaction",
    "RegistryCallParams",
    "RegistryAction",
    "build_registry_register",
    "build_registry_update",
    "build_give_feedback",
    "build_revoke_feedback",
    "registry_args_json",
    "sign_registry_transaction",
    "NearRegistryClient",
    "AgentView",
    "FeedbackView",
    "SummaryView",
    "RoleSummaryView",
    "FeedbackGivenData",
    "RegistryEvent",
    "FeedbackOutcome",
    "FeedbackSummary",
    "parse_give_feedback_outcome",
    "parse_registry_events",
    "parse_registry_receipt_events",
    "summarize_feedback",
    "safe_link",
    "display_text",
]
