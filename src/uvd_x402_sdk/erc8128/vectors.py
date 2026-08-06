"""Conformance vectors, shipped INSIDE the package, plus :func:`run_conformance`.

The vectors are the tripwire: a consumer's CI runs ``run_conformance()``
against the version of the SDK it actually installed, so a byte of drift in
the signature base fails there instead of failing authentication in
production.

Two loading traps this closes:

* setuptools autodiscovery ships only ``.py``. Without the ``package-data``
  stanza in ``pyproject.toml`` the vectors load fine from a source checkout
  and raise ``FileNotFoundError`` from an installed wheel.
* the files are resolved through ``importlib.resources`` (package resolution),
  never by a path relative to a monorepo — a missing file is an error at load,
  not a silently skipped suite.

``CONFORMANCE_SHA256`` is a hardcoded constant compared against the bytes that
actually shipped, so a hand-edited copy fails before it can sign anything.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from uvd_x402_sdk.erc8128.core import WIRE_CONTRACT_VERSION
from uvd_x402_sdk.erc8128.presets import policy_from_preset, preset_as_data
from uvd_x402_sdk.erc8128.verifier import VerifiableRequest, verify_request

_RESOURCES = {
    "f3-1": "erc8128.f3-1.json",
    "f3-3": "erc8128.f3-3.json",
}

#: sha256 of the shipped vector files, byte for byte. The TypeScript package
#: exports the same values under the SAME KEYS — ``'f3-1'`` / ``'f3-3'``, the
#: generation ids, not a JS-flavoured ``f3_1`` — so the spec's cross-language
#: equality check is a plain dict comparison. ``scripts/xlang/`` in the
#: TypeScript SDK computes both and diffs them on every CI run.
#:
#: These are the hashes of the LF bytes — the form all three repos actually
#: STORE (``git show HEAD:shared/test-vectors/erc8128.json`` is 9275 bytes,
#: md5 ``95ea41b7e939ba6a5b8aca136142b013``, in execution-market, the TS SDK
#: and here). The md5 ``54969151…`` quoted around the fleet is a Windows
#: working copy with CRLF: pinning that one makes every fresh clone and every
#: CI checkout fail the integrity check. ``.gitattributes`` pins these files to
#: LF in all three repos so ``core.autocrlf`` cannot move the hash.
CONFORMANCE_SHA256: Dict[str, str] = {
    "f3-1": "3c82d81f66cc95c452dbb2892c4aee97c688dc5fe03b721d06c92ba98e4f9bfd",
    "f3-3": "154ef31dc3e704c376e282ca5ca5dccde00877328cbab250001dbb20ba2e91ac",
}

_cache: Dict[str, Dict[str, Any]] = {}
_bytes_cache: Dict[str, bytes] = {}


def vector_bytes(generation: str) -> bytes:
    """The raw shipped bytes of a vector file (what CONFORMANCE_SHA256 pins)."""
    if generation not in _RESOURCES:
        raise ValueError(f"unknown vector generation: {generation!r}")
    if generation not in _bytes_cache:
        try:
            from importlib.resources import files  # Python >= 3.9

            _bytes_cache[generation] = (
                files("uvd_x402_sdk.erc8128").joinpath(_RESOURCES[generation]).read_bytes()
            )
        except ImportError:  # pragma: no cover - 3.8 and older
            import pkgutil

            data = pkgutil.get_data("uvd_x402_sdk.erc8128", _RESOURCES[generation])
            if data is None:
                raise FileNotFoundError(_RESOURCES[generation])
            _bytes_cache[generation] = data
    return _bytes_cache[generation]


def load_vectors(generation: str) -> Dict[str, Any]:
    """The parsed vector document for a generation (``"f3-1"`` / ``"f3-3"``)."""
    if generation not in _cache:
        _cache[generation] = json.loads(vector_bytes(generation).decode("utf-8"))
    return _cache[generation]


def __getattr__(name: str) -> Any:
    # Lazy so `import uvd_x402_sdk` does not parse two JSON files.
    if name == "CONFORMANCE_VECTORS_F3_1":
        return load_vectors("f3-1")
    if name == "CONFORMANCE_VECTORS_F3_3":
        return load_vectors("f3-3")
    raise AttributeError(name)


@dataclass(frozen=True)
class ConformanceReport:
    """``failed`` is empty or the run failed. Each entry names the case and
    what differed, so CI output points at the divergent line, not at "false".

    ``ok``, ``passed``, ``total`` and ``failed`` are spelled the SAME in the
    TypeScript package's ``ConformanceReport``, so one green condition —
    ``report.ok``, or ``report.passed == report.total`` — transliterates
    between the two. Each side keeps its own extras (``generations`` and
    ``wire_contract_version`` here; ``cases``, ``sha256`` and
    ``wireContractVersion`` there).
    """

    passed: int = 0
    failed: List[Dict[str, Any]] = field(default_factory=list)
    total: int = 0
    generations: Tuple[str, ...] = ()
    wire_contract_version: str = WIRE_CONTRACT_VERSION

    @property
    def ok(self) -> bool:
        return not self.failed


class _MemoryNonceStore:
    """First-use-wins, in-process. Deliberately PRIVATE: shipping this as a
    convenient default would give zero replay protection across processes,
    which is the one property a nonce store exists to provide.
    """

    def __init__(self) -> None:
        self._seen: set = set()

    def consume(self, nonce: str, *, wallet: str, chain_id: int, **_: Any) -> str:
        key = f"erc8128:{chain_id}:{wallet}:{nonce}"
        if key in self._seen:
            return "replayed"
        self._seen.add(key)
        return "ok"


def _fixed_clock(value: int) -> Callable[[], int]:
    return lambda: value


def _all_requests() -> Dict[str, Dict[str, Any]]:
    requests = dict(load_vectors("f3-1")["requests"])
    requests.update(load_vectors("f3-3")["requests"])
    return requests


def _all_vectors() -> Dict[str, Dict[str, Any]]:
    """``family/request`` → vector, across both generations."""
    merged: Dict[str, Dict[str, Any]] = {}
    for generation in ("f3-1", "f3-3"):
        for family, cases in load_vectors(generation)["vectors"].items():
            for name, vector in cases.items():
                merged[f"{family}/{name}"] = vector
    return merged


def build_verifiable_request(
    request_spec: Dict[str, Any], headers: Dict[str, str]
) -> VerifiableRequest:
    """The request as it would arrive on the wire: the signed headers plus the
    framing headers a real client sets for its body.
    """
    body = request_spec["body"]
    wire_headers = dict(headers)
    if body is not None:
        wire_headers["Content-Length"] = str(len(body.encode("utf-8")))
    return VerifiableRequest(
        method=request_spec["method"],
        url=request_spec["url"],
        headers=wire_headers,
        raw_body=body.encode("utf-8") if body is not None else None,
    )


def _sign_cases() -> List[Dict[str, Any]]:
    """Every vector the SIGNER is expected to reproduce byte for byte. The
    checksummed-keyid family is excluded: the signer lowercases the keyid on
    purpose, so that family is verify-only.
    """
    cases = []
    for generation in ("f3-1", "f3-3"):
        doc = load_vectors(generation)
        requests = doc["requests"]
        for family, entries in doc["vectors"].items():
            if family == "legacy_alg_checksum_keyid":
                continue
            profile = "canonical" if family == "canonical" else "legacy-no-alg"
            for name, vector in entries.items():
                if name not in requests:
                    continue
                cases.append(
                    {
                        "vector_id": f"{family}/{name}",
                        "generation": generation,
                        "profile": profile,
                        "request": requests[name],
                        "expected": vector["headers"],
                        "signature_base": vector["signature_base"],
                    }
                )
    return cases


def _run_sign_section(failed: List[Dict[str, Any]]) -> int:
    from uvd_x402_sdk.erc8128.signer import sign_request
    from uvd_x402_sdk.wallet import EnvKeyAdapter

    frozen = load_vectors("f3-1")["frozen"]
    # Public synthetic test key from the fixture, stored 0x-less so scanners
    # never see 0x + 64 hex. It never held funds.
    wallet = EnvKeyAdapter(private_key="0x" + frozen["private_key"])
    passed = 0
    for case in _sign_cases():
        spec = case["request"]
        headers = sign_request(
            wallet,
            method=spec["method"],
            url=spec["url"],
            body=spec["body"],
            nonce=frozen["nonce"],
            chain_id=frozen["chain_id"],
            profile=case["profile"],
            now=_fixed_clock(int(frozen["created"])),
        )
        if headers != case["expected"]:
            failed.append(
                {
                    "section": "sign",
                    "vector_id": case["vector_id"],
                    "expected": case["expected"],
                    "actual": headers,
                }
            )
        else:
            passed += 1
    return passed


async def _run_verify_section(failed: List[Dict[str, Any]]) -> int:
    vectors = _all_vectors()
    requests = _all_requests()
    # The authority a case does not name. It is NOT derived from the request
    # URL any more: that read the CONFIGURED value out of the thing it is
    # supposed to be checked against, so a request written
    # ``https://host:443/x`` silently configured ``host:443`` and the case
    # tested a rule nobody wrote down. The TypeScript runner read it from
    # ``frozen`` instead — one file, two different tests.
    default_authority = load_vectors("f3-3")["frozen"]["authority"]
    passed = 0
    for case in load_vectors("f3-3")["verify_cases"]:
        vector_id = case["vector_id"]
        vector = vectors[vector_id]
        request_name = vector_id.split("/", 1)[1]
        spec = requests[request_name]
        request = build_verifiable_request(spec, vector["headers"])
        verify_at = int(load_vectors("f3-1")["frozen"]["created"]) + 1
        policy = policy_from_preset(
            case["policy"],
            authority=case.get("authority", default_authority),
            nonce_store=_MemoryNonceStore(),
            now=_fixed_clock(verify_at),
        )
        result = await verify_request(request, policy)
        expected_ok = case["expect"] == "accept"
        problems = []
        if result.ok != expected_ok:
            problems.append(f"ok={result.ok} expected={expected_ok} code={result.code}")
        if expected_ok:
            if result.wallet != case.get("wallet"):
                problems.append(f"wallet={result.wallet}")
        else:
            if result.code != case.get("code"):
                problems.append(f"code={result.code} expected={case.get('code')}")
            # 401 blames the client, 503 blames the operator. A misconfigured
            # authority answering 401 is the failure this pin exists to catch.
            if case.get("status") is not None and result.status != case["status"]:
                problems.append(f"status={result.status} expected={case['status']}")
        # Pinned on accept AND reject rows, ``None`` included: a request
        # refused before ``Signature-Input`` is parsed has no observed profile,
        # and that is part of the contract too.
        if result.observed_profile != case.get("observed_profile"):
            problems.append(f"observed_profile={result.observed_profile}")
        if problems:
            failed.append(
                {
                    "section": "verify",
                    "vector_id": vector_id,
                    "policy": case["policy"],
                    "authority": case.get("authority", default_authority),
                    "problems": problems,
                }
            )
        else:
            passed += 1
    return passed


def _check_integrity(failed: List[Dict[str, Any]]) -> int:
    passed = 0
    for generation, expected in CONFORMANCE_SHA256.items():
        actual = hashlib.sha256(vector_bytes(generation)).hexdigest()
        if actual != expected:
            failed.append(
                {
                    "section": "integrity",
                    "generation": generation,
                    "expected": expected,
                    "actual": actual,
                }
            )
        else:
            passed += 1
    for name, pinned in load_vectors("f3-3")["policies"].items():
        if preset_as_data(name) != pinned:
            failed.append(
                {
                    "section": "policy",
                    "preset": name,
                    "expected": pinned,
                    "actual": preset_as_data(name),
                }
            )
        else:
            passed += 1
    return passed


def run_conformance(only: Optional[str] = None) -> ConformanceReport:
    """Run the shipped conformance vectors against THIS build of the SDK.

    ``only`` restricts to ``"sign"`` or ``"verify"``; the integrity checks
    (vector hashes and the pinned policy presets) always run. Signing and
    recovery need the ``signer`` extra (``eth-account``).
    """
    if only not in (None, "sign", "verify"):
        raise ValueError("only must be None, 'sign' or 'verify'")
    failed: List[Dict[str, Any]] = []
    passed = _check_integrity(failed)
    if only in (None, "sign"):
        passed += _run_sign_section(failed)
    if only in (None, "verify"):
        passed += asyncio.run(_run_verify_section(failed))
    return ConformanceReport(
        passed=passed,
        failed=failed,
        total=passed + len(failed),
        generations=tuple(_RESOURCES),
    )


__all__ = [
    "CONFORMANCE_SHA256",
    "ConformanceReport",
    "build_verifiable_request",
    "load_vectors",
    "run_conformance",
    "vector_bytes",
]
