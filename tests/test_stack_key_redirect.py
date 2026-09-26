"""X-UVD-Stack-Key does not follow redirects.

"casa"  = http://127.0.0.1:<p1>, listed in stack_key_hosts, answers every
          request with a redirect to "ajeno".
"ajeno" = http://localhost:<p2>, NOT listed: the gate refuses it.

Every assertion states the SAFE property: a red here is a leak. The key is
synthetic; nothing leaves the machine.
"""

import asyncio
import json
import socket
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

from uvd_x402_sdk import X402Client
from uvd_x402_sdk.dx402 import available_backends
from uvd_x402_sdk.erc8004 import Erc8004Client
from uvd_x402_sdk.receipts import PurchaseContext, get_receipt
from uvd_x402_sdk.stack_key import stack_key_allowed

KEY = "uvdsk_" + "synthetic-redirect-key_" * 3
LOCAL = ["127.0.0.1"]
RECIPIENT = "0x" + "aa" * 20


class _DualStack(ThreadingHTTPServer):
    address_family = socket.AF_INET6

    def server_bind(self):
        self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        super().server_bind()


class _Servers:
    def __init__(self):
        self.at_ajeno: list[dict] = []
        self.status = 302
        outer = self

        class Ajeno(BaseHTTPRequestHandler):
            def _any(self):
                n = int(self.headers.get("Content-Length") or 0)
                if n:
                    self.rfile.read(n)
                outer.at_ajeno.append(
                    {
                        "method": self.command,
                        "path": self.path,
                        # Booleans only: the report never carries the value.
                        "key_arrived": self.headers.get("X-UVD-Stack-Key") == KEY,
                        "auth_arrived": self.headers.get("Authorization") is not None,
                    }
                )
                body = json.dumps({"version": "x", "backends": [], "totalSupply": 1}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            do_GET = do_POST = _any

            def log_message(self, *a):
                pass

        class Casa(BaseHTTPRequestHandler):
            def _any(self):
                n = int(self.headers.get("Content-Length") or 0)
                if n:
                    self.rfile.read(n)
                self.send_response(outer.status)
                self.send_header("Location", f"{outer.ajeno_url}{self.path}")
                self.send_header("Content-Length", "0")
                self.end_headers()

            do_GET = do_POST = _any

            def log_message(self, *a):
                pass

        self.ajeno = _DualStack(("::", 0), Ajeno)
        self.ajeno_url = f"http://localhost:{self.ajeno.server_address[1]}"
        self.casa = ThreadingHTTPServer(("127.0.0.1", 0), Casa)
        self.casa_url = f"http://127.0.0.1:{self.casa.server_address[1]}"
        for s in (self.ajeno, self.casa):
            threading.Thread(target=s.serve_forever, daemon=True).start()

    def close(self):
        for s in (self.ajeno, self.casa):
            s.shutdown()
            s.server_close()

    def leaked(self):
        return [r for r in self.at_ajeno if r["key_arrived"]]


@pytest.fixture
def servers():
    s = _Servers()
    yield s
    s.close()


def test_control_the_gate_refuses_ajeno_and_allows_casa(servers):
    assert stack_key_allowed(servers.casa_url + "/verify", LOCAL) is True
    assert stack_key_allowed(servers.ajeno_url + "/verify", LOCAL) is False


@pytest.mark.parametrize("status", [301, 302, 307, 308])
def test_x402client_default_http_client_does_not_follow(servers, status):
    servers.status = status
    client = X402Client(
        recipient_address=RECIPIENT, facilitator_url=servers.casa_url,
        stack_key=KEY, stack_key_hosts=LOCAL,
    )
    try:
        client.get_version()
    except Exception:
        pass
    print(f"[REF] default client {status} ->", json.dumps(servers.at_ajeno))
    assert servers.leaked() == []


@pytest.mark.parametrize("status", [302, 307])
def test_x402client_with_a_caller_client_that_follows_redirects(servers, status):
    servers.status = status
    http = httpx.Client(follow_redirects=True)
    client = X402Client(
        recipient_address=RECIPIENT, facilitator_url=servers.casa_url,
        stack_key=KEY, stack_key_hosts=LOCAL, http_client=http,
    )
    for call in (client.get_version, client.get_supported, client.health_check, client.get_blacklist):
        try:
            call()
        except Exception:
            pass
    print(f"[REF] caller client follow_redirects {status} ->", json.dumps(servers.at_ajeno))
    assert servers.leaked() == []


def test_get_receipt_with_a_client_that_follows_redirects(servers):
    with httpx.Client(follow_redirects=True) as http:
        try:
            get_receipt(
                http, str(uuid.uuid4()), PurchaseContext(),
                issuer=servers.casa_url, stack_key=KEY, stack_key_hosts=LOCAL,
            )
        except Exception:
            pass
    print("[REF] get_receipt ->", json.dumps(servers.at_ajeno))
    assert servers.leaked() == []


def test_dx402_backends_with_a_client_that_follows_redirects(servers):
    with httpx.Client(follow_redirects=True) as http:
        available_backends(servers.casa_url, client=http, stack_key=KEY, stack_key_hosts=LOCAL)
    print("[REF] dx402 httpx ->", json.dumps(servers.at_ajeno))
    assert servers.leaked() == []


def test_dx402_backends_with_a_requests_session(servers):
    requests = pytest.importorskip("requests")
    with requests.Session() as http:
        available_backends(servers.casa_url, client=http, stack_key=KEY, stack_key_hosts=LOCAL)
    print("[REF] dx402 requests.Session ->", json.dumps(servers.at_ajeno))
    assert servers.leaked() == []


def test_erc8004_default_client_does_not_follow(servers):
    async def run():
        c = Erc8004Client(base_url=servers.casa_url, stack_key=KEY, stack_key_hosts=LOCAL)
        try:
            await c.get_identity_total_supply("base")
        except Exception:
            pass
        await c._client.aclose()

    asyncio.run(run())
    print("[REF] erc8004 default ->", json.dumps(servers.at_ajeno))
    assert servers.leaked() == []
