"""What four HTTP clients put on the wire for a URL, read without opening a socket.

``sends(client, url)`` returns the request target and the ``Host`` header the
client writes for a GET of ``url``; ``server_request`` turns them into what
EM's Starlette reads (``tests/near_http_auth_em.ServerRequest``: the path
decoded as ASGI does, the query raw, the netloc from ``Host``).

* ``httpx``: ``httpx.Request``, the bytes h11 writes.
* ``requests`` (urllib3) and ``urllib`` (stdlib ``urlopen``): the real client,
  with the connection and the TLS wrap replaced by a socket that records what
  is sent and answers ``200``. Nothing connects.
* ``aiohttp``: ``yarl.URL(url)``, its ``raw_path_qs`` and
  ``host_port_subcomponent``: the session builds that URL from the string and
  ``ClientRequest`` writes exactly those two (``test_the_aiohttp_model_is_what_
  aiohttp_writes`` reads the installed aiohttp's source).

Cross-checked on 2026-11-06 against the real clients (httpx 0.28.1, requests
2.34.2 / urllib3 2.8.0, urllib of CPython 3.11.14, aiohttp 3.14.5 / yarl 1.25.1)
sending to a uvicorn on 127.0.0.1 and ::1: for the shapes, authorities and
``EM_MEASURED`` URLs of ``tests/test_near_http_auth.py`` (as http) and a few
probes, the target and ``Host`` uvicorn received equal these, 138 of 138.
"""

from __future__ import annotations

import contextlib
import io
from unittest import mock
from urllib.parse import unquote

from tests import near_http_auth_em as em

CLIENTS = ("httpx", "requests", "urllib", "aiohttp")


class _RecordingSocket:
    """Records what the client sends; answers an empty 200."""

    def __init__(self) -> None:
        self.sent = b""

    def sendall(self, data) -> None:
        self.sent += bytes(data)

    def makefile(self, *args, **kwargs):
        return io.BytesIO(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n")

    def settimeout(self, timeout) -> None:
        pass

    def gettimeout(self):
        return None

    def setsockopt(self, *args) -> None:
        pass

    def getsockopt(self, *args) -> int:
        return 0

    def version(self) -> str:
        return "TLSv1.3"

    def getpeercert(self, *args) -> dict:
        return {}

    def selected_alpn_protocol(self):
        return None

    def close(self) -> None:
        pass


def _recorded(send) -> tuple[str, str]:
    sock = _RecordingSocket()
    with contextlib.ExitStack() as patches:
        patches.enter_context(mock.patch("socket.create_connection", lambda *a, **k: sock))
        patches.enter_context(
            mock.patch("urllib3.util.connection.create_connection", lambda *a, **k: sock)
        )
        patches.enter_context(mock.patch("ssl.SSLContext.wrap_socket", lambda self, s, *a, **k: s))
        send()
    request_line, *lines = sock.sent.split(b"\r\n\r\n", 1)[0].split(b"\r\n")
    host = [line.split(b":", 1)[1].strip() for line in lines if line.lower().startswith(b"host:")]
    assert len(host) == 1, sock.sent
    return request_line.split(b" ")[1].decode("ascii"), host[0].decode("ascii")


def _requests(url: str) -> tuple[str, str]:
    import requests

    def send() -> None:
        with requests.Session() as session:
            session.trust_env = False  # CI points the proxy variables at a closed port
            session.get(url)

    return _recorded(send)


def _urllib(url: str) -> tuple[str, str]:
    import urllib.request

    def send() -> None:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(urllib.request.Request(url, method="GET")) as response:
            response.read()

    return _recorded(send)


def _httpx(url: str) -> tuple[str, str]:
    import httpx

    request = httpx.Request("GET", url)
    return request.url.raw_path.decode("ascii"), request.headers["host"]


def _aiohttp(url: str) -> tuple[str, str]:
    import yarl

    sent = yarl.URL(url)
    return sent.raw_path_qs, sent.host_port_subcomponent


_SENDERS = {"httpx": _httpx, "requests": _requests, "urllib": _urllib, "aiohttp": _aiohttp}


def sends(client: str, url: str) -> tuple[str, str]:
    """(request target, Host) as ``client`` writes them for a GET of ``url``."""
    return _SENDERS[client](url)


def server_request(client: str, method: str, url: str, headers: dict, body=None):
    """The request EM reads after ``client`` sent ``url`` with ``headers`` and ``body``."""
    target, host = sends(client, url)
    raw_path, _, query = target.partition("?")
    content = body.encode("utf-8") if isinstance(body, str) else body
    return em.ServerRequest(
        method=method.upper(),
        netloc=host,
        path=unquote(raw_path),
        query=query,
        headers={**{k.lower(): v for k, v in headers.items()}, "host": host},
        body=content or b"",
    )
