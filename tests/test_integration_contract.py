import errno
import inspect
import os
import socket
import subprocess
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import aiohttp
import httpx
import requests

from freebind import _socket, patch as install_patch
from freebind._source import Source
from freebind.aiohttp import FreebindConnector
from freebind.httpx import AsyncFreebindTransport, FreebindTransport
from freebind.requests import FreebindAdapter


def _addrinfo(family, host):
    sockaddr = (host, 443) if family == socket.AF_INET else (host, 443, 0, 0)
    return family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", sockaddr


def _wrapped_errors(error):
    pending = [error]
    seen = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        yield current
        for name in ("__cause__", "__context__", "reason", "os_error"):
            wrapped = getattr(current, name, None)
            if isinstance(wrapped, BaseException):
                pending.append(wrapped)


class _FailingSocket:
    def __init__(self, family):
        self.family = family
        self.closed = False
        self.connected = False

    def getpeername(self):
        if not self.connected:
            raise OSError(errno.ENOTCONN, "not connected")
        return ("198.51.100.1", 443)

    def getsockname(self):
        return ("0.0.0.0", 0) if self.family == socket.AF_INET else ("::", 0, 0, 0)

    def settimeout(self, timeout):
        pass

    def close(self):
        self.closed = True


class _PeerHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self):
        body = self.client_address[0].encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True

    def log_message(self, _format, *args):
        pass


class IntegrationContractTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _PeerHandler)
        cls.server.daemon_threads = True
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.address = ("127.0.0.1", cls.server.server_port)
        cls.url = f"http://127.0.0.1:{cls.server.server_port}/"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def test_matching_family_setup_failures_never_fall_back(self):
        answers = [
            _addrinfo(socket.AF_INET, "198.51.100.1"),
            _addrinfo(socket.AF_INET6, "2001:db8::1"),
        ]
        for failure_site in ("option", "bind"):
            with self.subTest(failure_site=failure_site):
                error = OSError(errno.EPERM, "Freebind denied")
                sock = _FailingSocket(socket.AF_INET)
                with (
                    patch.object(socket, "getaddrinfo", return_value=answers),
                    patch.object(
                        _socket, "_create_unpatched_socket", return_value=sock
                    ) as create,
                    patch.object(_socket, "_ORIGINAL_CONNECT") as connect,
                ):
                    option = patch.object(
                        _socket,
                        "enable_freebind",
                        side_effect=error if failure_site == "option" else None,
                    )
                    binding = patch.object(
                        _socket,
                        "_ORIGINAL_BIND",
                        side_effect=error if failure_site == "bind" else None,
                    )
                    with option, binding, self.assertRaises(OSError) as caught:
                        _socket.create_connection(
                            ("example.test", 443), Source("192.0.2.1", strict=False)
                        )

                self.assertIs(caught.exception, error)
                create.assert_called_once_with(
                    socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP
                )
                connect.assert_not_called()
                self.assertTrue(sock.closed)

    async def _assert_setup_failure(self, request, error_type):
        error = OSError(errno.EPERM, "Freebind denied")
        created = []
        original_create = _socket._create_unpatched_socket

        def record_socket(*args):
            sock = original_create(*args)
            created.append(sock)
            return sock

        with (
            patch.object(_socket, "enable_freebind", side_effect=error),
            patch.object(_socket, "_create_unpatched_socket", side_effect=record_socket),
            self.assertRaises(error_type) as caught,
        ):
            result = request()
            if inspect.isawaitable(result):
                await result

        self.assertTrue(
            any(wrapped is error for wrapped in _wrapped_errors(caught.exception))
        )
        self.assertTrue(created)
        self.assertTrue(all(sock.fileno() == -1 for sock in created))

    async def test_sdk_setup_errors_keep_errno_and_close_owned_sockets(self):
        source = Source("127.0.0.8")

        def requests_request():
            with requests.Session() as session:
                session.trust_env = False
                session.mount("http://", FreebindAdapter(source))
                session.get(self.url, timeout=2)

        async def aiohttp_request():
            async with aiohttp.ClientSession(
                connector=FreebindConnector(source)
            ) as client:
                await client.get(self.url, timeout=2)

        def httpx_request():
            with httpx.Client(
                transport=FreebindTransport(source), trust_env=False, timeout=2
            ) as client:
                client.get(self.url)

        async def async_httpx_request():
            async with httpx.AsyncClient(
                transport=AsyncFreebindTransport(source), trust_env=False, timeout=2
            ) as client:
                await client.get(self.url)

        for request, error_type in (
            (requests_request, requests.ConnectionError),
            (aiohttp_request, aiohttp.ClientConnectorError),
            (httpx_request, httpx.ConnectError),
            (async_httpx_request, httpx.ConnectError),
        ):
            await self._assert_setup_failure(request, error_type)

    def _request_with_core(self, source):
        sock = _socket.create_connection(self.address, source, timeout=2)
        try:
            self.assertEqual(sock.getsockname()[0], source.select(socket.AF_INET))
            sock.sendall(
                b"GET / HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n"
            )
            response = bytearray()
            while chunk := sock.recv(4096):
                response.extend(chunk)
            return bytes(response).split(b"\r\n\r\n", 1)[1].decode()
        finally:
            sock.close()

    async def test_explicit_core_and_client_policies_survive_both_patch_timings(self):
        sources = {
            "core": Source("127.0.0.3"),
            "requests": Source("127.0.0.4"),
            "aiohttp": Source("127.0.0.5"),
            "httpx": Source("127.0.0.6"),
            "httpx_async": Source("127.0.0.7"),
        }

        async def request_aiohttp(source):
            async with aiohttp.ClientSession(
                connector=FreebindConnector(source),
                timeout=aiohttp.ClientTimeout(total=2),
            ) as client:
                async with client.get(self.url) as response:
                    return (await response.text()).strip()

        async def request_httpx_async(source):
            async with httpx.AsyncClient(
                transport=AsyncFreebindTransport(source),
                trust_env=False,
                timeout=2,
            ) as client:
                return (await client.get(self.url)).text.strip()

        for entrypoint in ("connect", "socket"):
            with self.subTest(entrypoint=entrypoint):
                with install_patch(Source("127.0.0.2"), entrypoint=entrypoint):
                    observed = {"core": self._request_with_core(sources["core"])}

                    session = requests.Session()
                    session.trust_env = False
                    session.mount("http://", FreebindAdapter(sources["requests"]))
                    try:
                        observed["requests"] = session.get(self.url, timeout=2).text.strip()
                    finally:
                        session.close()

                    observed["aiohttp"] = await request_aiohttp(sources["aiohttp"])

                    with httpx.Client(
                        transport=FreebindTransport(sources["httpx"]),
                        trust_env=False,
                        timeout=2,
                    ) as client:
                        observed["httpx"] = client.get(self.url).text.strip()

                    observed["httpx_async"] = await request_httpx_async(
                        sources["httpx_async"]
                    )

                self.assertEqual(
                    observed,
                    {
                        name: source.select(socket.AF_INET)
                        for name, source in sources.items()
                    },
                )

    def test_base_import_needs_no_optional_client_or_environment_settings(self):
        root = Path(__file__).resolve().parents[1]
        script = """
import importlib.abc
import os
import sys

blocked = {"requests", "aiohttp", "httpx", "httpcore", "anyio"}
class BlockOptional(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split(".", 1)[0] in blocked:
            raise RuntimeError("optional import attempted: " + fullname)

class CheckedEnviron(dict):
    def get(self, key, *args):
        if key.startswith("FREEBIND_"):
            raise RuntimeError("environment read during import: " + key)
        return super().get(key, *args)

os.environ["FREEBIND_RANDOM"] = "malformed-but-ignored"
os.environ = CheckedEnviron(os.environ)
sys.meta_path.insert(0, BlockOptional())
import freebind
assert freebind._patch._ACTIVE_PATCH is None
assert not any(name.split(".", 1)[0] in blocked for name in sys.modules)
"""
        environment = os.environ.copy()
        environment["PYTHONPATH"] = os.pathsep.join(
            (str(root / "src"), environment.get("PYTHONPATH", ""))
        )
        result = subprocess.run(
            [sys.executable, "-c", script],
            cwd=root,
            env=environment,
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
