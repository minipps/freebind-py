import asyncio
import errno
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from curl_cffi import CurlOpt
from curl_cffi.requests.exceptions import RequestException

from freebind import Source, patch as install_patch
from freebind.curl_cffi import AsyncFreebindSession, FreebindSession


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self):
        if self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/")
            body = b""
        else:
            self.send_response(200)
            body = json.dumps({"source": self.client_address[0],
                               "port": self.client_address[1]}).encode()
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class CurlCffiTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        cls.server.daemon_threads = True
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def test_validation(self):
        for cls in (FreebindSession, AsyncFreebindSession):
            for kwargs, error in (({}, TypeError), ({"source": Source("127.0.0.2"), "fresh": 1}, TypeError)):
                with self.subTest(cls=cls, kwargs=kwargs), self.assertRaises(error):
                    cls(**({"source": "invalid"} | kwargs))
            for key in ("proxy", "proxies", "interface", "curl", "async_curl"):
                with self.subTest(cls=cls, key=key), self.assertRaises(ValueError):
                    cls(Source("127.0.0.2"), **{key: "conflict"})

    async def test_pooling_freshness_resets_and_patch_bypass(self):
        for fresh in (False, True):
            for entrypoint in ("connect", "socket"):
                with install_patch(Source("127.0.0.9"), entrypoint=entrypoint):
                    with FreebindSession(Source("127.0.0.2"), fresh=fresh) as client:
                        reports = [client.get(self.url, timeout=2).json() for _ in range(2)]
                    async with AsyncFreebindSession(Source("127.0.0.3"), fresh=fresh) as client:
                        async_reports = [(await client.get(self.url, timeout=2)).json() for _ in range(2)]
                for results, source in ((reports, "127.0.0.2"), (async_reports, "127.0.0.3")):
                    self.assertEqual([r["source"] for r in results], [source, source])
                    self.assertEqual(results[0]["port"] != results[1]["port"], fresh)

    async def test_streaming_and_redirects(self):
        with FreebindSession(Source("127.0.0.2"), fresh=True) as client:
            for _ in range(2):
                with client.stream("GET", self.url + "/redirect", timeout=2) as response:
                    self.assertEqual(json.loads(b"".join(response.iter_content()))["source"], "127.0.0.2")
        async with AsyncFreebindSession(Source("127.0.0.3"), fresh=True) as client:
            for _ in range(2):
                async with client.stream("GET", self.url + "/redirect", timeout=2) as response:
                    body = b"".join([chunk async for chunk in response.aiter_content()])
                    self.assertEqual(json.loads(body)["source"], "127.0.0.3")

    async def test_setup_errors_preserve_cause_and_recover(self):
        error = OSError(errno.EPERM, "Freebind denied")
        with FreebindSession(Source("127.0.0.2")) as client:
            with patch("freebind.curl_cffi.bind_socket", side_effect=error), self.assertRaises(RequestException) as caught:
                client.get(self.url, timeout=2)
            self.assertIs(caught.exception.__cause__.__cause__, error)
            self.assertEqual(client.get(self.url, timeout=2).json()["source"], "127.0.0.2")
        async with AsyncFreebindSession(Source("127.0.0.3"), max_clients=1) as client:
            with patch("freebind.curl_cffi.bind_socket", side_effect=error), self.assertRaises(RequestException) as caught:
                await client.get(self.url, timeout=2)
            self.assertIs(caught.exception.__cause__.__cause__, error)
            self.assertEqual((await client.get(self.url, timeout=2)).json()["source"], "127.0.0.3")

    async def test_streaming_setup_errors_do_not_hang(self):
        error = OSError(errno.EPERM, "Freebind denied")
        with FreebindSession(Source("127.0.0.2")) as client:
            with patch("freebind.curl_cffi.bind_socket", side_effect=error), self.assertRaises(RequestException):
                with client.stream("GET", self.url, timeout=2):
                    self.fail("binding failure must abort the stream")
        async with AsyncFreebindSession(Source("127.0.0.3"), max_clients=1) as client:
            with patch("freebind.curl_cffi.bind_socket", side_effect=error), self.assertRaises(RequestException):
                await asyncio.wait_for(client.get(self.url, stream=True, timeout=2), 3)
            self.assertEqual((await client.get(self.url, timeout=2)).json()["source"], "127.0.0.3")

    async def test_proxy_and_binding_overrides_are_rejected(self):
        with FreebindSession(Source("127.0.0.2"), fresh=True) as client:
            for kwargs in ({"proxy": "http://127.0.0.1:1"}, {"interface": "127.0.0.1"},
                           {"proxies": {"http": "http://127.0.0.1:1"}}):
                with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                    client.get(self.url, **kwargs)
            for option, value in ((CurlOpt.SOCKOPTFUNCTION, None), (CurlOpt.IPRESOLVE, 2),
                                  (CurlOpt.FRESH_CONNECT, 0), (CurlOpt.UNIX_SOCKET_PATH, "/tmp/socket")):
                client.curl_options = {option: value}
                with self.subTest(option=option), self.assertRaises(ValueError):
                    client.get(self.url)
        async with AsyncFreebindSession(Source("127.0.0.2")) as client:
            with self.assertRaises(ValueError):
                await client.get(self.url, proxy="http://127.0.0.1:1")

    async def test_environment_proxies_are_disabled_and_concurrency_works(self):
        with patch.dict("os.environ", {"http_proxy": "http://127.0.0.1:1", "ALL_PROXY": "http://127.0.0.1:1", "NO_PROXY": ""}):
            with FreebindSession(Source("127.0.0.2"), trust_env=True, curl_options={CurlOpt.PROXY: None}) as client:
                self.assertEqual(client.get(self.url, timeout=2).json()["source"], "127.0.0.2")
            async with AsyncFreebindSession(Source("127.0.0.3"), max_clients=2) as client:
                responses = await asyncio.gather(*(client.get(self.url, timeout=2) for _ in range(4)))
                self.assertTrue(all(r.json()["source"] == "127.0.0.3" for r in responses))

    async def test_strict_family_and_non_strict_fallback(self):
        for strict in (True, False):
            source = Source("2001:db8::1", strict=strict)
            with FreebindSession(source) as client:
                if strict:
                    with self.assertRaises(RequestException):
                        client.get(self.url, timeout=2)
                else:
                    self.assertEqual(client.get(self.url, timeout=2).json()["source"], "127.0.0.1")
            async with AsyncFreebindSession(source) as client:
                if strict:
                    with self.assertRaises(RequestException):
                        await client.get(self.url, timeout=2)
                else:
                    self.assertEqual((await client.get(self.url, timeout=2)).json()["source"], "127.0.0.1")

    async def test_failed_handle_creation_returns_pool_slot(self):
        async with AsyncFreebindSession(Source("127.0.0.2"), max_clients=1) as client:
            with patch("freebind.curl_cffi._FreebindCurl", side_effect=RuntimeError("setup")):
                with self.assertRaises(RuntimeError):
                    await client.get(self.url)
            self.assertEqual((await asyncio.wait_for(client.get(self.url), 2)).json()["source"], "127.0.0.2")

    def test_session_creates_bound_handles_in_other_threads(self):
        with FreebindSession(Source("127.0.0.2")) as client:
            def request():
                try:
                    return client.get(self.url, timeout=2).json()["source"]
                finally:
                    client.curl.close()
            self.assertEqual(client.executor.submit(request).result(timeout=3), "127.0.0.2")


if __name__ == "__main__":
    unittest.main()
