"""NEP-413 HTTP authentication: EM's vector byte for byte, EM's verifier, refusals, mutants.

The fixture is an UNCHANGED byte copy of Execution Market's public vector
``shared/test-vectors/near-http-auth.json`` (origin/main c2828143, file from EM
PR #667). Re-copy it from there with ``git show``; never edit it here. Its seed
is ``bytes(range(32))``: public test material, never a funded key.

"Verifies as EM does" is checked against ``tests/near_http_auth_em.py``, a
transcription of EM's verifier that shares no code with the SDK, fed the
request as an HTTP client sends it and EM's Starlette reads it. The clients are
httpx (``em.wire_request``) and, in ``tests/http_clients_wire.py``, requests,
urllib and aiohttp, read without a socket. ``WALKS`` vary every RFC 3986
character of the path, query and host and every percent-encoded octet of the
query: the SDK signs exactly what no client rewrites.
"""

from __future__ import annotations

import base64
import functools
import hashlib
import importlib.util
import ipaddress
import json
import socket
import string
import sys
from pathlib import Path
from types import ModuleType
from urllib.parse import urlsplit

import pytest

pytest.importorskip("cryptography")

from tests import http_clients_wire as wire  # noqa: E402
from tests import near_http_auth_em as em  # noqa: E402
from tests.test_near_ows import backend_signer  # noqa: E402,F401
from uvd_x402_sdk import near_http_auth as auth  # noqa: E402
from uvd_x402_sdk.erc8128.core import normalize_authority  # noqa: E402
from uvd_x402_sdk.near_escrow import NearEd25519Signer  # noqa: E402

FIXTURE = Path(__file__).parent / "fixtures/near-http-auth.json"
FIXTURE_SHA256 = "160486a60cf48d326d2e7e58f19f06af809d2945ab59f67f23b7e9a0a8630be2"
V = json.loads(FIXTURE.read_text(encoding="utf-8"))
SEED = bytes(range(32))
LOCAL = NearEd25519Signer(SEED)
OTHER = NearEd25519Signer(bytes(range(1, 33)))
NOW = V["created"] + 10
SUBJECT = "near:" + V["account_id"]
API = "https://api.execution.market"


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*args, **kwargs):
        pytest.fail("NEAR HTTP signing must never open a network connection")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)


def vector_args(**changes):
    args = dict(
        account_id=V["account_id"],
        method=V["method"],
        url=V["url"],
        nonce=V["nonce"],
        recipient=V["recipient"],
        body=V["body"],
        created=V["created"],
        validity_sec=V["expires"] - V["created"],
    )
    args.update(changes)
    return args


def sign(signer=LOCAL, module=auth, **changes):
    args = vector_args(**changes)
    return module.sign_near_http_request(
        signer, args.pop("account_id"), args.pop("method"), args.pop("url"), **args
    )


def em_verifies(headers, *, method=None, url=None, body=..., **verify_options):
    request = em.wire_request(
        method or V["method"], url or V["url"], headers, V["body"] if body is ... else body
    )
    return em.verify(request, now=NOW, **verify_options)


def lower(headers):
    return {k.lower(): v for k, v in headers.items()}


# -- the vector ---------------------------------------------------------------------
def test_fixture_is_ems_vector_unchanged():
    assert hashlib.sha256(FIXTURE.read_bytes()).hexdigest() == FIXTURE_SHA256


def test_the_transcribed_em_verifier_reproduces_ems_vector():
    # The double is checked against EM's own numbers before it judges the SDK.
    request = em.wire_request(V["method"], V["url"], V["headers"], V["body"])
    _, covered, params = em.parse_signature_input(request.headers["signature-input"])
    base = em.build_signature_base(request, covered, params, em.PRODUCTION_AUTHORITIES)
    assert base == V["signature_base"]
    nonce = hashlib.sha256(V["nonce"].encode()).digest()
    assert nonce.hex() == V["nep413_nonce_hex"]
    assert em.nep413_hash(base, nonce, V["recipient"]).hex() == V["nep413_hash_hex"]
    assert (
        em._extract_signature_bytes(request.headers["signature"], "near").hex()
        == (V["signature_hex"])
    )
    assert em.verify(request, now=NOW) == em.Result(True, address=SUBJECT)


@pytest.mark.parametrize("case", ["title", "lower"])
def test_signs_ems_vector_byte_for_byte(case):
    headers = sign(header_case=case)
    assert lower(headers) == lower(V["headers"])
    names = {
        "title": ["Content-Digest", "Signature-Input", "Signature"],
        "lower": ["content-digest", "signature-input", "signature"],
    }[case]
    assert list(headers) == names


def test_signs_ems_vector_through_ows(backend_signer):  # noqa: F811
    # In memory AND in a real OWS 1.4.2 vault holding the vector's test seed.
    assert lower(sign(backend_signer)) == lower(V["headers"])


def test_the_vector_signature_is_ed25519_over_the_nep413_hash():
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    signature = base64.b64decode(sign()["Signature"][len("near=:") : -1])
    assert signature.hex() == V["signature_hex"]
    Ed25519PublicKey.from_public_bytes(bytes.fromhex(V["public_key_hex"])).verify(
        signature, bytes.fromhex(V["nep413_hash_hex"])
    )


# -- what EM's verifier accepts -------------------------------------------------------
SHAPES = {
    "post-json": ("POST", V["url"], V["body"], {}),
    "get-no-body": ("GET", "https://api.execution.market/api/v1/tasks/available", None, {}),
    "get-query": (
        "GET",
        "https://api.execution.market/api/v1/tasks?network=near&limit=5&q=a%20b",
        None,
        {},
    ),
    "post-empty-body": ("POST", "https://api.execution.market/api/v1/tasks", "", {}),
    "bytes-body": ("POST", "https://mcp.execution.market/mcp/", '{"t":"ñandú ✓"}'.encode(), {}),
    "str-body-unicode": ("POST", "https://mcp.execution.market/mcp/", '{"t":"ñandú ✓"}', {}),
    "named-account": ("POST", V["url"], "{}", {"account_id": "alice.near"}),
    "query-encoded-octets": (
        "GET",
        "https://api.execution.market/api/v1/tasks?q=%E2%9C%93&r=%26%3D%2B%20%25%23%5B%5D%3B",
        None,
        {},
    ),
    "query-reserved-written-literally": (
        "GET",
        "https://api.execution.market/api/v1/tasks?next=/a/b?c=d:e@f!$'()*,+;",
        None,
        {},
    ),
    "lowercase-method": ("post", V["url"], V["body"], {}),
    "empty-query": ("GET", "https://api.execution.market/api/v1/tasks?", None, {}),
    "no-path": ("GET", "https://api.execution.market", None, {}),
    "sub-delims-in-path": (
        "GET",
        "https://api.execution.market/api/v1/a;b=c/d'(e)*!$,:@x+y",
        None,
        {},
    ),
    "lower-header-names": ("PUT", V["url"], "x", {"header_case": "lower"}),
    "short-window": ("GET", V["url"], None, {"validity_sec": 1, "created": NOW}),
}


@pytest.mark.parametrize("shape", list(SHAPES))
def test_ems_verifier_accepts_what_the_sdk_signs(shape):
    method, url, body, extra = SHAPES[shape]
    headers = sign(method=method, url=url, body=body, nonce=f"shape-{shape}", **extra)
    account = extra.get("account_id", V["account_id"])
    result = em_verifies(headers, method=method, url=url, body=body)
    assert result == em.Result(True, address=f"near:{account}")


AUTHORITIES = [
    ("https://api.execution.market:8443/x", "api.execution.market:8443"),
    ("https://api.execution.market:80/x", "api.execution.market:80"),
    ("http://api.execution.market:443/x", "api.execution.market:443"),
    ("http://[::1]:8000/x", "[::1]:8000"),
    ("https://[2001:db8::1]/x", "[2001:db8::1]"),
]


@pytest.mark.parametrize("url,authority", AUTHORITIES)
def test_the_authority_is_what_the_host_header_carries(url, authority):
    headers = sign(url=url, body=None)
    assert em_verifies(headers, url=url, body=None, allowed=frozenset({authority})).ok


# -- every measured HTTP client sends what was signed ------------------------------
@pytest.mark.parametrize("client", wire.CLIENTS)
@pytest.mark.parametrize("shape", list(SHAPES))
def test_every_client_sends_what_was_signed(shape, client):
    method, url, body, extra = SHAPES[shape]
    headers = sign(method=method, url=url, body=body, nonce=f"shape-{shape}", **extra)
    request = wire.server_request(client, method, url, headers, body)
    account = extra.get("account_id", V["account_id"])
    assert em.verify(request, now=NOW) == em.Result(True, address=f"near:{account}")


@pytest.mark.parametrize("client", wire.CLIENTS)
@pytest.mark.parametrize("url,authority", AUTHORITIES)
def test_every_client_sends_the_signed_authority(url, authority, client):
    headers = sign(method="GET", url=url, body=None)
    request = wire.server_request(client, "GET", url, headers)
    assert em.verify(request, now=NOW, allowed=frozenset({authority})).ok


@pytest.mark.parametrize("url", [SHAPES[s][1] for s in SHAPES] + [a[0] for a in AUTHORITIES])
def test_the_signed_authority_is_the_netloc_as_written_and_already_normal(url):
    # What EM's own signer signs (the netloc verbatim) and RFC 9421's normalised
    # authority (lowercase, no default port) agree on every URL this signer accepts.
    parts = urlsplit(url)
    assert auth._url_parts(url)[0] == parts.netloc
    assert normalize_authority(parts.netloc, parts.scheme) == parts.netloc


def test_an_ipv4_mapped_literal_is_signed_as_this_python_compresses_it():
    # CPython 3.11.14 writes ::ffff:7f00:1 as ::ffff:127.0.0.1 (older ones did not);
    # yarl writes what ipaddress writes, so the accepted spelling follows the runtime.
    form = ipaddress.IPv6Address("::ffff:7f00:1").compressed
    other = "::ffff:127.0.0.1" if form == "::ffff:7f00:1" else "::ffff:7f00:1"
    url = f"https://[{form}]/x"
    headers = sign(method="GET", url=url, body=None)
    for client in wire.CLIENTS:
        request = wire.server_request(client, "GET", url, headers)
        assert em.verify(request, now=NOW, allowed=frozenset({f"[{form}]"})).ok, client
    assert refused_before_key(url=f"https://[{other}]/x")


def test_the_aiohttp_model_is_what_aiohttp_writes():
    # tests/http_clients_wire.py reads aiohttp's request off yarl: the session builds
    # yarl.URL(url) and ClientRequest writes these two. A new aiohttp is re-measured.
    import inspect

    from aiohttp.client import ClientSession
    from aiohttp.client_reqrep import ClientRequest

    assert "URL(str_or_url)" in inspect.getsource(ClientSession._build_url)
    assert "path = self.url.raw_path_qs" in inspect.getsource(ClientRequest.send)
    assert "self.url.host_port_subcomponent" in inspect.getsource(ClientRequest.update_headers)


# -- every character and octet: signed <=> every client sends it as written ----------
# RFC 3986's characters: the class of near_http_auth._URL_RE (pinned below).
RFC3986 = string.ascii_letters + string.digits + "-._~:/?#[]@!$&'()*+,;=%"


def hex_spellings(octet: int) -> list[str]:
    """``%XX`` with each hex digit in each case: ``%7E`` ``%7e``, ``%9C`` ``%9c``, ``%Ea``..."""
    digits = [sorted({d.upper(), d.lower()}) for d in f"{octet:02X}"]
    return ["%" + a + b for a in digits[0] for b in digits[1]]


# Each walk varies one component of a URL that is signed as it is. The query walks put
# the character in a SECOND parameter, after an octet that is signed, so a guard that
# reads only the first octet is seen. `#` starts the fragment (policy, M05); in the host,
# `: / ? # [ ] @` delimit it and a `%` is policy (M36).
WALKS = {
    "query-octet": [API + "/x?a=%26&q=" + s for octet in range(256) for s in hex_spellings(octet)],
    "query-character": [API + "/x?a=%26&q=a" + c + "z" for c in RFC3986 if c != "#"],
    "path-character": [API + "/a" + c + "z" for c in RFC3986 if c != "#"],
    "host-character": [
        f"https://{host}/x"
        for c in RFC3986
        if c not in ":/?#[]@%"
        for host in (c + "ab.cd", "ab" + c + "cd.ef", "ab.cd" + c)
    ],
}
# A client that refuses a URL sends nothing and spends no nonce. requests refuses a host
# that starts with `.` or `*` ("URL has an invalid label"); the other three send both.
CLIENT_REFUSALS = {
    "host-character": {("https://.ab.cd/x", "requests"), ("https://*ab.cd/x", "requests")},
}


@functools.cache
def client_sends(client: str, url: str):
    """``wire.sends``, or ``None`` when the client refuses the URL."""
    try:
        return wire.sends(client, url)
    except Exception:  # noqa: BLE001 - which refusals are expected is CLIENT_REFUSALS
        return None


def walk_mismatches(walk: str, module=auth) -> list:
    """The URLs of ``walk`` signed while a client rewrites them, or refused while none does.

    A URL signed must also verify under EM's verifier as each client that sends it does.
    """
    mismatches: list = []
    refusals = set()
    for url in WALKS[walk]:
        parts = urlsplit(url)
        written = ((parts.path or "/") + ("?" + parts.query if "?" in url else ""), parts.netloc)
        sent = {client: client_sends(client, url) for client in wire.CLIENTS}
        rewritten = {c: s for c, s in sent.items() if s is not None and s != written}
        try:
            headers = sign(module=module, method="GET", url=url, body=None, created=NOW)
        except ValueError:
            headers = None
        if (headers is None) != bool(rewritten):
            what = "signed, a client rewrites it" if rewritten else "refused, sent as written"
            mismatches.append((url, what, rewritten))
            continue
        for client, sent_by_client in sent.items() if headers is not None else ():
            if sent_by_client is None:
                refusals.add((url, client))
                continue
            request = wire.server_request(client, "GET", url, headers, sent=sent_by_client)
            if not em.verify(request, now=NOW, allowed=frozenset({parts.netloc})).ok:
                mismatches.append((url, "signed, EM rejects what the client sends", client))
    if refusals != CLIENT_REFUSALS.get(walk, set()):
        mismatches.append(("client refusals of signed URLs", sorted(refusals)))
    return mismatches


@pytest.mark.parametrize("walk", list(WALKS))
def test_signed_iff_every_client_sends_it_as_written(walk):
    # When a client's release changes what it sends for one character or octet, this is red.
    assert walk_mismatches(walk) == []


def test_the_walks_cover_every_rfc3986_character_and_every_query_octet():
    assert {chr(i) for i in range(128) if auth._URL_RE.fullmatch(chr(i))} == set(RFC3986)
    octets = WALKS["query-octet"]
    # 100 octets of two decimal digits, 120 with one hex letter (2 spellings), 36 with two (4).
    assert len(octets) == len(set(octets)) == 100 + 120 * 2 + 36 * 4
    assert {url.rsplit("=", 1)[1].upper() for url in octets} == {f"%{o:02X}" for o in range(256)}


def test_a_non_default_port_stays_in_the_authority():
    url = "http://localhost:8000/api/v1/tasks"
    headers = sign(url=url, body=None)
    assert em_verifies(headers, url=url, body=None, allowed=frozenset({"localhost:8000"})).ok
    assert not em_verifies(headers, url=url, body=None, allowed=frozenset({"localhost"})).ok


def test_empty_body_still_covers_content_digest():
    # EM's own signer digests whenever body is not None; so does this one.
    headers = sign(body="")
    assert headers["Content-Digest"] == "sha-256=:47DEQpj8HBSa+/TImW+5JCeuQeRkm5NMpJWZG3hSuFU=:"
    assert '"content-digest");' in headers["Signature-Input"]


def test_no_body_covers_no_digest_and_no_query_no_query_component():
    headers = sign(url="https://api.execution.market/api/v1/tasks", body=None)
    assert set(headers) == {"Signature-Input", "Signature"}
    assert headers["Signature-Input"].startswith('near=("@method" "@authority" "@path");')


def test_the_recipient_is_the_callers():
    headers = sign(recipient="other.example")
    assert not em_verifies(headers).ok
    assert em_verifies(headers, recipient="other.example").ok


def test_default_created_is_now(monkeypatch):
    monkeypatch.setattr(auth.time, "time", lambda: 1_800_000_000.7)
    headers = sign(created=None)
    assert ";created=1800000000;expires=1800000300;" in headers["Signature-Input"]


def sign_with_the_default_validity(module=auth):
    args = vector_args(created=NOW)
    del args["validity_sec"]
    return module.sign_near_http_request(
        LOCAL, args.pop("account_id"), args.pop("method"), args.pop("url"), **args
    )


def test_the_default_validity_is_300_seconds():
    # EM's maximum, as README and CHANGELOG say; a shorter default is not a refusal anywhere.
    headers = sign_with_the_default_validity()
    assert f";created={NOW};expires={NOW + 300};" in headers["Signature-Input"]
    assert em_verifies(headers).ok


@pytest.mark.parametrize(
    "tamper",
    [
        "body",
        "method",
        "path",
        "query",
        "host",
        "signature",
        "recipient",
        "replay",
        "function-call",
    ],
)
def test_ems_verifier_refuses_a_tampered_request(tamper):
    # The double must not be trivially green: each binding is load-bearing.
    headers = sign(created=NOW)
    if tamper == "signature":
        headers["Signature"] = sign(OTHER, created=NOW)["Signature"]
    options = dict(
        method={"method": "PUT"}.get(tamper),
        url={
            "path": V["url"].replace("/tasks", "/tasks/x"),
            "query": V["url"].replace("near", "base"),
            "host": V["url"].replace("api.", "mcp."),
        }.get(tamper),
    )
    if tamper == "body":
        options["body"] = V["body"] + " "
    if tamper == "recipient":
        options["recipient"] = "other.example"
    if tamper == "function-call":
        options["full_access"] = False
    if tamper == "replay":
        store = em.NonceStore()
        assert em_verifies(headers, store=store).ok
        options["store"] = store
    assert not em_verifies(headers, **options).ok


# -- refusals before the key ----------------------------------------------------------
class CountingSigner:
    """A real key that counts how often its public key is read."""

    def __init__(self, inner=LOCAL):
        self.inner = inner
        self.reads = 0
        self.signatures = 0

    @property
    def public_key(self):
        self.reads += 1
        return self.inner.public_key

    def sign(self, digest):
        self.signatures += 1
        return self.inner.sign(digest)


BAD_INPUTS = {
    "account-uppercase": {"account_id": "Alice.near"},
    "account-trailing-newline": {"account_id": "alice.near\n"},
    "account-none": {"account_id": None},
    "account-cyrillic": {"account_id": "аlice.near"},
    "account-colon": {"account_id": "alice:near"},
    "method-none": {"method": None},
    "method-empty": {"method": ""},
    "method-newline": {"method": "POST\n"},
    "method-space": {"method": "PO ST"},
    "url-none": {"url": None},
    "url-bytes": {"url": V["url"].encode()},
    "url-space": {"url": "https://api.execution.market/a b"},
    "url-non-ascii": {"url": "https://api.execution.market/ñ"},
    "url-newline": {"url": "https://api.execution.market/a\n"},
    "url-quote": {"url": 'https://api.execution.market/a"b'},
    "url-bad-percent": {"url": "https://api.execution.market/x?a=%zz"},
    "url-short-percent": {"url": "https://api.execution.market/x?a=%2"},
    "url-fragment": {"url": "https://api.execution.market/x#frag"},
    "url-relative": {"url": "/api/v1/tasks"},
    "url-ftp": {"url": "ftp://api.execution.market/x"},
    "url-userinfo": {"url": "https://user:pass@api.execution.market/x"},
    "url-bare-at": {"url": "https://@api.execution.market/x"},
    # No colon: only the user-info guard refuses it (the port guard reads `user:pass@`).
    "url-user-only": {"url": "https://user@api.execution.market/x"},
    "url-bad-port": {"url": "https://api.execution.market:99999/x"},
    "url-text-port": {"url": "https://api.execution.market:abc/x"},
    "url-no-host": {"url": "https://:443/x"},
    "url-no-host-port": {"url": "https://:8443/x"},
    "url-bracket-path": {"url": "https://api.execution.market/a[b]"},
    "url-bracket-query": {"url": "https://api.execution.market/a?x=[1]"},
    "url-percent-path": {"url": "https://api.execution.market/a%20b"},
    "url-encoded-slash": {"url": "https://api.execution.market/a%2Fb"},
    "url-port-leading-zero": {"url": "https://api.execution.market:0443/x"},
    "url-port-empty": {"url": "https://api.execution.market:/x"},
    "url-port-padded": {"url": "https://api.execution.market:08443/x"},
    "url-port-zero": {"url": "https://api.execution.market:0/x"},
    "url-ipv6-uppercase": {"url": "https://[::FFFF:7F00:1]/x"},
    "url-ipvfuture": {"url": "https://[v1.abc]/x"},
    "url-ipv6-not-compressed": {"url": "http://[0::1]:8000/x"},
    "url-ipv6-leading-zeros": {"url": "https://[2001:0db8::1]/x"},
    "url-ipv4-in-brackets": {"url": "https://[127.0.0.1]/x"},
    "url-host-uppercase": {"url": "https://API.execution.market/x"},
    "url-host-mixed-case": {"url": "https://api.Execution.market/x"},
    "url-host-trailing-dot": {"url": "https://api.execution.market./x"},
    "url-host-trailing-dot-port": {"url": "https://api.execution.market.:8443/x"},
    "url-default-port-https": {"url": "https://api.execution.market:443/x"},
    "url-default-port-http": {"url": "http://api.execution.market:80/x"},
    # CPython 3.9.6's urlsplit accepts it; newer ones raise. Refused on both.
    "url-ipv6-port-without-colon": {"url": "https://[::1]443/x"},
    "url-scheme-uppercase": {"url": "HTTPS://api.execution.market/x"},
    "url-scheme-uppercase-port": {"url": "HTTPS://api.execution.market:443/x"},
    "url-percent-host": {"url": "https://api%2eexecution.market/x"},
    "url-dot-dot": {"url": "https://api.execution.market/a/../b"},
    "url-dot": {"url": "https://api.execution.market/a/./b"},
    "url-trailing-dot-dot": {"url": "https://api.execution.market/a/.."},
    "url-query-lowercase-hex": {"url": "https://api.execution.market/x?q=%e2%9c%93"},
    "url-query-unreserved": {"url": "https://api.execution.market/x?q=%7E"},
    "url-query-unreserved-lowercase": {"url": "https://api.execution.market/x?q=%7e"},
    "url-query-encoded-slash": {"url": "https://api.execution.market/x?next=%2Fa"},
    "url-query-encoded-comma": {"url": "https://api.execution.market/x?q=a%2Cb"},
    "nonce-none": {"nonce": None},
    "nonce-empty": {"nonce": ""},
    "nonce-int": {"nonce": 1},
    "nonce-too-long": {"nonce": "n" * 257},
    "nonce-quote": {"nonce": 'a"b'},
    "nonce-backslash": {"nonce": "a\\b"},
    "nonce-space": {"nonce": "a b"},
    "nonce-semicolon": {"nonce": "a;b"},
    "nonce-non-ascii": {"nonce": "ñ"},
    "nonce-newline": {"nonce": "abc\n"},
    "recipient-none": {"recipient": None},
    "recipient-empty": {"recipient": ""},
    "recipient-leading-space": {"recipient": " execution.market"},
    "recipient-trailing-newline": {"recipient": "execution.market\n"},
    "recipient-intents": {"recipient": "intents.near"},
    "recipient-intents-case": {"recipient": "Intents.Near."},
    "recipient-surrogate": {"recipient": "\udfff"},
    "body-int": {"body": 1},
    "body-bytearray": {"body": bytearray(b"{}")},
    "body-memoryview": {"body": memoryview(b"{}")},
    "body-dict": {"body": {"title": "x"}},
    "body-surrogate": {"body": "\ud800"},
    "validity-zero": {"validity_sec": 0},
    "validity-301": {"validity_sec": 301},
    "validity-negative": {"validity_sec": -1},
    "validity-bool": {"validity_sec": True},
    "validity-float": {"validity_sec": 300.0},
    "created-str": {"created": str(V["created"])},
    "created-float": {"created": float(V["created"])},
    "created-bool": {"created": True},
    "created-negative": {"created": -1},
    "created-past-js-safe": {"created": 2**53 - 300},
    "header-case": {"header_case": "TITLE"},
    "header-case-list": {"header_case": []},
    "header-case-dict": {"header_case": {}},
}


def refused_before_key(module=auth, **changes):
    signer = CountingSigner()
    try:
        sign(signer, module, **changes)
    except ValueError:
        return signer.reads == 0
    except Exception:  # noqa: BLE001 - any other outcome is not the documented refusal
        return False
    return False


@pytest.mark.parametrize("case", list(BAD_INPUTS))
def test_refuses_before_reading_the_key(case):
    assert refused_before_key(**BAD_INPUTS[case])


def test_the_largest_accepted_inputs_sign():
    headers = sign(nonce="n" * 256, created=2**53 - 1 - 300, validity_sec=300)
    assert 'nonce="' + "n" * 256 + '"' in headers["Signature-Input"]


# -- what the signer returns is checked --------------------------------------------
class Broken:
    def __init__(self, public_key, signature):
        self.public_key = public_key
        self.signature = signature

    def sign(self, digest):
        return self.signature


class Flipping:
    """Reports one key in the keyid and signs with another, both consistently."""

    def __init__(self):
        self.reads = 0

    @property
    def public_key(self):
        self.reads += 1
        return (LOCAL if self.reads == 1 else OTHER).public_key

    def sign(self, digest):
        return OTHER.sign(digest)


@pytest.mark.parametrize(
    "signer",
    [
        Broken(None, bytes(64)),
        Broken("ed25519:FAe4sisG95oZ42w7buUn5qEE4TAnfTTFPiguZUHmhiF", bytes(64)),
        Broken(bytes(31), bytes(64)),
        Broken(bytes(33), bytes(64)),
        Broken(LOCAL.public_key, None),
        Broken(LOCAL.public_key, bytes(63)),
        Broken(LOCAL.public_key, bytes(65)),
        Broken(LOCAL.public_key, bytes(64)),
        Broken(LOCAL.public_key, OTHER.sign(bytes(32))),
        Flipping(),
    ],
    ids=[
        "key-none",
        "key-text",
        "key-31",
        "key-33",
        "signature-none",
        "signature-63",
        "signature-65",
        "signature-zero",
        "signature-other-key",
        "key-changes-while-signing",
    ],
)
def test_checks_the_signers_output(signer):
    with pytest.raises(ValueError):
        sign(signer)


def test_public_exports():
    import uvd_x402_sdk as sdk

    assert auth.__all__ == [
        "EXECUTION_MARKET_NEP413_RECIPIENT",
        "NEAR_HTTP_AUTH_ALG",
        "NEAR_HTTP_AUTH_LABEL",
        "NEAR_HTTP_AUTH_MAX_NONCE_LENGTH",
        "NEAR_HTTP_AUTH_MAX_VALIDITY_SEC",
        "sign_near_http_request",
    ]
    for name in auth.__all__:
        assert name in sdk.__all__
        assert getattr(sdk, name) is getattr(auth, name)
    assert auth.EXECUTION_MARKET_NEP413_RECIPIENT == em.EM_RECIPIENT == V["recipient"]
    assert auth.NEAR_HTTP_AUTH_MAX_VALIDITY_SEC == em.Policy().max_validity_sec


# -- mutants: each guard is what refuses -------------------------------------------
def mutant(old: str, new: str) -> ModuleType:
    """A copy of ``uvd_x402_sdk.near_http_auth`` with ``old`` -> ``new`` (exactly once)."""
    source = Path(auth.__file__).read_text(encoding="utf-8")
    assert source.count(old) == 1, f"mutation site not found exactly once: {old!r}"
    spec = importlib.util.spec_from_loader("uvd_x402_sdk._mutant_near_http_auth", loader=None)
    module = importlib.util.module_from_spec(spec)
    module.__package__ = "uvd_x402_sdk"
    module.__file__ = auth.__file__
    sys.modules[spec.name] = module
    exec(compile(source.replace(old, new), auth.__file__, "exec"), module.__dict__)
    return module


GUARD_MUTANTS = [
    ("M01-account", '    _account(account_id, "account_id")\n', "", "account-uppercase"),
    (
        "M02-method-token",
        "if not isinstance(method, str) or _METHOD_RE.fullmatch(method) is None:",
        "if not isinstance(method, str):",
        "method-newline",
    ),
    (
        "M03-url-characters",
        "if not isinstance(url, str) or _URL_RE.fullmatch(url) is None:",
        "if not isinstance(url, str):",
        "url-space",
    ),
    (
        "M04-bad-percent",
        "if _BAD_PERCENT_RE.search(url) is not None:",
        "if False:",
        "url-bad-percent",
    ),
    ("M05-fragment", 'if "#" in url:', "if False:", "url-fragment"),
    (
        "M06-scheme",
        'if not url.startswith(("http://", "https://")):',
        "if False:",
        "url-scheme-uppercase",
    ),
    ("M07-userinfo", 'if "@" in netloc:', "if False:", "url-user-only"),
    ("M08-port", "        port = parts.port\n", "        port = None\n", "url-bad-port"),
    ("M09-host", "if not parts.hostname:", "if False:", "url-no-host-port"),
    (
        "M10-brackets",
        'if "[" in path or "]" in path or "[" in parts.query or "]" in parts.query:',
        "if False:",
        "url-bracket-path",
    ),
    ("M11-percent-path", 'if "%" in path:', "if False:", "url-percent-path"),
    (
        "M12-dot-segments",
        'if any(segment in (".", "..") for segment in path.split("/")):',
        "if False:",
        "url-dot-dot",
    ),
    (
        "M13-nonce",
        "if not isinstance(nonce, str) or _NONCE_RE.fullmatch(nonce) is None:",
        "if not isinstance(nonce, str):",
        "nonce-quote",
    ),
    (
        "M14-recipient-whitespace",
        "if not isinstance(recipient, str) or not recipient or recipient.strip() != recipient:",
        "if False:",
        "recipient-trailing-newline",
    ),
    (
        "M15-recipient-intents",
        'if recipient.lower().removesuffix(".") == "intents.near":',
        "if False:",
        "recipient-intents",
    ),
    ("M16-recipient-unicode", '    recipient.encode("utf-8")\n', "", "recipient-surrogate"),
    (
        "M17-body-type",
        '    raise ValueError("body must be str, bytes or None")',
        "    return body",
        "body-bytearray",
    ),
    (
        "M18-validity",
        "if type(validity_sec) is not int or not 1 <= validity_sec <= "
        "NEAR_HTTP_AUTH_MAX_VALIDITY_SEC:",
        "if False:",
        "validity-301",
    ),
    ("M19-created-type", "elif type(created) is not int:", "elif False:", "created-bool"),
    (
        "M20-created-range",
        "if not 0 <= created <= _MAX_TIMESTAMP - validity_sec:",
        "if False:",
        "created-negative",
    ),
    (
        "M21-header-case",
        "if not isinstance(header_case, str) or header_case not in _HEADER_NAMES:",
        "if not isinstance(header_case, str):",
        "header-case",
    ),
    (
        "M34-port-text",
        'if _PORT_RE.fullmatch(netloc[netloc.rfind(":") + 1 :]) is None:',
        "if False:",
        "url-port-padded",
    ),
    (
        "M35-ipv6-form",
        'if ("[" in netloc or "]" in netloc) and not _compressed_ipv6_netloc(netloc):',
        "if False:",
        "url-ipvfuture",
    ),
    ("M36-percent-host", 'if "%" in netloc:', "if False:", "url-percent-host"),
    (
        "M37-header-case-type",
        "if not isinstance(header_case, str) or header_case not in _HEADER_NAMES:",
        "if header_case not in _HEADER_NAMES:",
        "header-case-list",
    ),
    ("M38-host-case", "if netloc != netloc.lower():", "if False:", "url-host-uppercase"),
    (
        "M39-default-port",
        "if port == _DEFAULT_PORTS[parts.scheme]:",
        "if False:",
        "url-default-port-https",
    ),
    ("M40-query-hex-case", "if octet != octet.upper():", "if False:", "url-query-lowercase-hex"),
    (
        "M41-query-decoded",
        "if chr(int(octet, 16)) in _QUERY_DECODED_BY_CLIENTS:",
        "if False:",
        "url-query-unreserved",
    ),
    (
        "M43-ipv6-compressed",
        "return ipaddress.IPv6Address(match.group(1)).compressed == match.group(1)",
        "return ipaddress.IPv6Address(match.group(1)).compressed is not None",
        "url-ipv6-not-compressed",
    ),
    (
        "M44-host-trailing-dot",
        'if parts.hostname.endswith("."):',
        "if False:",
        "url-host-trailing-dot",
    ),
]


@pytest.mark.parametrize(
    "old,new,case", [m[1:] for m in GUARD_MUTANTS], ids=[m[0] for m in GUARD_MUTANTS]
)
def test_guard_mutant_goes_through(old, new, case):
    assert refused_before_key(**BAD_INPUTS[case])
    assert not refused_before_key(mutant(old, new), **BAD_INPUTS[case])


# Narrower guards, not removed ones: each still refuses most of what it should and lets
# through a URL a client rewrites, which only a walk sees (the refutador's Q1-Q5 and Q8).
WALK_MUTANTS = [
    ("M45-decoded-set-quote", "-._~/?:@!$'()*,\"", '-._~/?:@!$()*,"', "query-octet"),
    ("M46-decoded-set-A", '"ABCDEFGHIJKLMNOPQRS', '"BCDEFGHIJKLMNOPQRS', "query-octet"),
    ("M47-decoded-set-question", "-._~/?:@!$'()*,\"", "-._~/:@!$'()*,\"", "query-octet"),
    (
        "M48-hex-case-first-digit",
        "if octet != octet.upper():",
        "if octet[0] != octet[0].upper():",
        "query-octet",
    ),
    (
        "M49-first-octet-only",
        "for octet in _PERCENT_OCTET_RE.findall(parts.query):",
        "for octet in _PERCENT_OCTET_RE.findall(parts.query)[:1]:",
        "query-octet",
    ),
    (
        "M50-host-case-first-character",
        "if netloc != netloc.lower():",
        "if netloc[:1] != netloc[:1].lower():",
        "host-character",
    ),
]


@pytest.mark.parametrize(
    "old,new,walk", [m[1:] for m in WALK_MUTANTS], ids=[m[0] for m in WALK_MUTANTS]
)
def test_walk_mutant_signs_what_a_client_rewrites(old, new, walk):
    assert walk_mismatches(walk) == []
    found = walk_mismatches(walk, mutant(old, new))
    assert found and {m[1] for m in found} == {"signed, a client rewrites it"}


def em_accepts(module, signer=LOCAL, allowed=em.PRODUCTION_AUTHORITIES, **changes):
    try:
        headers = sign(signer, module, created=NOW, **changes)
    except Exception:  # noqa: BLE001
        return False
    verify = {k: changes[k] for k in ("method", "url", "body") if k in changes}
    return em_verifies(headers, allowed=allowed, **verify).ok


# The guards whose case EM rejects as a measured HTTP client sends it: the guard
# removed, the URL signs and what each named client sends does not verify; written
# canonically, it verifies through every client. Measured with EM's real verifier
# and the real clients too (tests/http_clients_wire.py). The other URL guards are
# policy (see the module docstring).
EM_MEASURED = [
    ("M04-bad-percent", API + "/x?a=%zz", API + "/x?a=%25zz", ("requests", "aiohttp")),
    ("M10-brackets", API + "/x?x=[1]", API + "/x?x=%5B1%5D", ("requests", "aiohttp")),
    ("M11-percent-path", API + "/a%20b", API + "/ab", wire.CLIENTS),
    ("M12-dot-segments", API + "/a/../b", API + "/b", ("httpx", "requests", "aiohttp")),
    ("M34-port-text", API + ":08443/x", API + ":8443/x", wire.CLIENTS),
    (
        "M38-host-case",
        "https://API.execution.market/x",
        API + "/x",
        ("httpx", "requests", "aiohttp"),
    ),
    ("M39-default-port", API + ":443/x", API + "/x", wire.CLIENTS),
    ("M40-query-hex-case", API + "/x?q=%e2%9c%93", API + "/x?q=%E2%9C%93", ("requests", "aiohttp")),
    ("M41-query-decoded", API + "/x?q=%7E", API + "/x?q=~", ("requests", "aiohttp")),
    ("M41-query-decoded", API + "/x?next=%2Fa", API + "/x?next=/a", ("aiohttp",)),
    ("M43-ipv6-compressed", "http://[0::1]:8000/x", "http://[::1]:8000/x", ("aiohttp",)),
    ("M44-host-trailing-dot", API + "./x", API + "/x", ("requests", "aiohttp")),
]


@pytest.mark.parametrize(
    "mid,url,canonical,clients",
    EM_MEASURED,
    ids=[
        f"{m[0]}-{urlsplit(m[1]).netloc}{urlsplit(m[1]).path}?{urlsplit(m[1]).query}"
        for m in EM_MEASURED
    ],
)
def test_without_the_guard_em_rejects_what_a_client_sends(mid, url, canonical, clients):
    old, new = next(m[1:3] for m in GUARD_MUTANTS if m[0] == mid)
    allowed = frozenset({urlsplit(canonical).netloc})
    assert refused_before_key(url=url)
    headers = sign(module=mutant(old, new), method="GET", url=url, body=None, created=NOW)
    for client in clients:
        request = wire.server_request(client, "GET", url, headers)
        assert not em.verify(request, now=NOW, allowed=allowed).ok, client
    headers = sign(method="GET", url=canonical, body=None, created=NOW)
    for client in wire.CLIENTS:
        request = wire.server_request(client, "GET", canonical, headers)
        assert em.verify(request, now=NOW, allowed=allowed).ok, client


def test_m22_the_alg_is_ed25519_nep413():
    assert em_accepts(auth)
    module = mutant('NEAR_HTTP_AUTH_ALG = "ed25519-nep413"', 'NEAR_HTTP_AUTH_ALG = "ed25519"')
    assert not em_accepts(module)


def test_m23_the_label_is_near():
    # Parity with EM's vector only: EM's verifier also accepts a near: keyid under "eth".
    module = mutant('NEAR_HTTP_AUTH_LABEL = "near"', 'NEAR_HTTP_AUTH_LABEL = "eth"')
    assert lower(sign(module=module)) != lower(V["headers"])


def test_m24_the_nep413_nonce_is_sha256_of_the_rfc9421_nonce():
    module = mutant(
        'nonce=hashlib.sha256(nonce.encode("utf-8")).digest(),',
        'nonce=hashlib.sha256(nonce.encode("utf-16-le")).digest(),',
    )
    assert not em_accepts(module)


def test_m25_the_callers_recipient_is_signed():
    module = mutant(
        "        recipient=recipient,\n    )",
        "        recipient=EXECUTION_MARKET_NEP413_RECIPIENT,\n    )",
    )
    assert em_verifies(sign(recipient="other.example"), recipient="other.example").ok
    headers = sign(module=module, recipient="other.example")
    assert not em_verifies(headers, recipient="other.example").ok


def test_m26_an_empty_body_is_covered():
    # Parity with EM's signer (http_auth.sign_near_request digests whenever body is not
    # None); EM's verifier accepts an empty body with or without content-digest.
    module = mutant(
        "covered = select_covered(url, raw_body is not None)",
        "covered = select_covered(url, bool(raw_body))",
    )
    assert '"content-digest"' not in sign(module=module, body="")["Signature-Input"]


def test_m27_the_query_keeps_its_question_mark():
    module = mutant('query=f"?{query}" if query else None,', "query=query or None,")
    assert not em_accepts(module)


def test_m28_the_authority_keeps_its_port():
    url = API + ":8443/api/v1/tasks"
    allowed = frozenset({"api.execution.market:8443"})
    module = mutant(
        "return netloc, path, parts.query",
        "return parts.hostname, path, parts.query",
    )
    assert em_accepts(auth, url=url, body=None, allowed=allowed)
    assert not em_accepts(module, url=url, body=None, allowed=allowed)


def test_m29_an_empty_path_is_slash():
    url = "https://api.execution.market"
    module = mutant('path = parts.path or "/"', "path = parts.path")
    assert em_accepts(auth, url=url, body=None)
    assert not em_accepts(module, url=url, body=None)


def test_m30_a_key_that_changes_while_signing_is_refused():
    module = mutant('if signed["publicKey"] != public_text:', "if False:")
    with pytest.raises(ValueError):
        sign(Flipping())
    headers = sign(Flipping(), module, created=NOW)
    assert not em_verifies(headers).ok


def test_m31_the_public_key_is_checked_before_anything_is_built():
    text = Broken("ed25519:FAe4sisG95oZ42w7buUn5qEE4TAnfTTFPiguZUHmhiF", bytes(64))
    module = mutant("if not isinstance(public_key, bytes) or len(public_key) != 32:", "if False:")
    with pytest.raises(ValueError):
        sign(text)
    with pytest.raises(TypeError):
        sign(text, module)


def test_m32_created_defaults_to_now(monkeypatch):
    module = mutant("created = int(time.time())", "created = 0")
    monkeypatch.setattr(auth.time, "time", lambda: 1_800_000_000)
    assert ";created=1800000000;" in sign(created=None)["Signature-Input"]
    assert ";created=1800000000;" not in sign(module=module, created=None)["Signature-Input"]


def test_m33_the_content_digest_header_is_sent():
    module = mutant("        headers[digest_name] = digest\n", "        pass\n")
    assert em_accepts(auth)
    assert not em_accepts(module)


def test_m42_the_default_validity_is_ems_maximum():
    module = mutant(
        "    validity_sec: int = NEAR_HTTP_AUTH_MAX_VALIDITY_SEC,\n",
        "    validity_sec: int = 299,\n",
    )
    expires = f";expires={NOW + 300};"
    assert expires in sign_with_the_default_validity()["Signature-Input"]
    assert expires not in sign_with_the_default_validity(module)["Signature-Input"]
