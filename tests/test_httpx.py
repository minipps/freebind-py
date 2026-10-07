import socket
import unittest
from unittest.mock import patch

import httpcore
import httpx

from freebind._source import Source
from freebind.httpx import FreebindTransport, _SyncFreebindBackend, _SyncStream


class SyncBackendTests(unittest.TestCase):
    def test_uses_source_helper_and_httpcore_stream(self):
        source = Source("192.0.2.1")
        sock = object()
        backend = _SyncFreebindBackend(source)
        options = ((socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1),)

        with patch("freebind.httpx.create_connection", return_value=sock) as connect:
            stream = backend.connect_tcp(
                "example.test", 443, timeout=3, socket_options=options
            )

        self.assertIs(type(stream), _SyncStream)
        self.assertIs(stream.get_extra_info("socket"), sock)
        connect.assert_called_once_with(
            ("example.test", 443),
            source,
            timeout=3,
            socket_options=[
                *options,
                (socket.IPPROTO_TCP, socket.TCP_NODELAY, 1),
            ],
        )
        for name in ("read", "write", "close", "start_tls", "get_extra_info"):
            self.assertTrue(callable(getattr(_SyncStream, name, None)))

    def test_maps_connect_errors_to_httpcore_errors(self):
        backend = _SyncFreebindBackend(Source("192.0.2.1"))
        for error, expected in (
            (socket.timeout("timed out"), httpcore.ConnectTimeout),
            (OSError("blocked"), httpcore.ConnectError),
        ):
            with self.subTest(expected=expected), patch(
                "freebind.httpx.create_connection", side_effect=error
            ):
                with self.assertRaises(expected) as caught:
                    backend.connect_tcp("example.test", 80)
                self.assertIs(caught.exception.__cause__, error)


class SyncTransportTests(unittest.TestCase):
    def setUp(self):
        self.transport = FreebindTransport(Source("192.0.2.1"))

    def tearDown(self):
        self.transport.close()

    def test_injects_backend_before_requests_and_keeps_native_streaming(self):
        self.assertIs(self.transport._pool._network_backend, self.transport._freebind_backend)
        self.assertIsInstance(self.transport._pool._network_backend, httpcore.SyncBackend)
        core_response = httpcore.Response(
            200,
            headers=[(b"content-length", b"5")],
            content=[b"hello"],
        )
        with patch.object(
            self.transport._pool, "handle_request", return_value=core_response
        ) as handle_request:
            response = self.transport.handle_request(
                httpx.Request("GET", "http://example.test/")
            )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.read(), b"hello")
            core_request = handle_request.call_args.args[0]
            self.assertEqual(core_request.method, b"GET")
            self.assertEqual(core_request.url.host, b"example.test")

    def test_preserves_httpx_exception_translation_and_cause(self):
        failure = httpcore.ConnectError("blocked")
        with patch.object(self.transport._pool, "handle_request", side_effect=failure):
            with self.assertRaises(httpx.ConnectError) as caught:
                self.transport.handle_request(
                    httpx.Request("GET", "http://example.test/")
                )
        self.assertIs(caught.exception.__cause__, failure)

    def test_rejects_proxy_uds_and_local_address(self):
        source = Source("192.0.2.1")
        for options in (
            {"proxy": "http://proxy.test"},
            {"uds": "/tmp/http.sock"},
            {"local_address": "192.0.2.2"},
        ):
            with self.subTest(options=options), self.assertRaises(ValueError):
                FreebindTransport(source, **options)


if __name__ == "__main__":
    unittest.main()
