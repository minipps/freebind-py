import asyncio
import socket
import unittest
from unittest.mock import AsyncMock, patch

import httpcore
import httpx

from freebind._source import Source
from freebind.httpx import (
    AsyncFreebindTransport,
    FreebindTransport,
    SocketStream,
    _AnyIOStream,
    _AsyncFreebindBackend,
    _SyncFreebindBackend,
    _SyncStream,
)


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



class FakeSocket:
    def __init__(self):
        self.closed = False
        self.close_calls = 0

    def close(self):
        self.closed = True
        self.close_calls += 1


class FakeByteStream:
    def __init__(self, sock):
        self.sock = sock
        self.closed = False
        self.close_calls = 0

    def extra(self, attribute, default=None):
        return ("192.0.2.1", 12345) if default is None else default

    async def aclose(self):
        self.closed = True
        self.close_calls += 1
        self.sock.close()


class AsyncBackendTests(unittest.IsolatedAsyncioTestCase):
    async def test_uses_asyncio_source_helper_and_transfers_to_anyio(self):
        source = Source("192.0.2.1")
        sock = FakeSocket()
        byte_stream = FakeByteStream(sock)
        options = ((socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1),)
        backend = _AsyncFreebindBackend(source)

        with (
            patch(
                "freebind.httpx.async_create_connection",
                new=AsyncMock(return_value=sock),
            ) as connect,
            patch(
                "freebind.httpx.SocketStream.from_socket",
                new=AsyncMock(return_value=byte_stream),
            ) as wrap,
        ):
            stream = await backend.connect_tcp(
                "example.test", 443, timeout=3, socket_options=options
            )

        self.assertIs(type(stream), _AnyIOStream)
        self.assertEqual(stream.get_extra_info("client_addr"), ("192.0.2.1", 12345))
        self.assertTrue(callable(getattr(_AnyIOStream, "start_tls", None)))
        self.assertFalse(sock.closed)
        connect.assert_awaited_once_with(
            ("example.test", 443),
            source,
            timeout=3,
            socket_options=[
                *options,
                (socket.IPPROTO_TCP, socket.TCP_NODELAY, 1),
            ],
        )
        wrap.assert_awaited_once_with(sock)
        await stream.aclose()
        self.assertTrue(byte_stream.closed)
        self.assertTrue(sock.closed)

    async def test_maps_failures_and_closes_socket_during_wrapping(self):
        backend = _AsyncFreebindBackend(Source("192.0.2.1"))
        for error, expected in (
            (TimeoutError("timed out"), httpcore.ConnectTimeout),
            (OSError("wrap failed"), httpcore.ConnectError),
            (asyncio.CancelledError(), asyncio.CancelledError),
        ):
            sock = FakeSocket()
            with (
                self.subTest(expected=expected),
                patch(
                    "freebind.httpx.async_create_connection",
                    new=AsyncMock(return_value=sock),
                ),
                patch(
                    "freebind.httpx.SocketStream.from_socket",
                    new=AsyncMock(side_effect=error),
                ),
            ):
                with self.assertRaises(expected) as caught:
                    await backend.connect_tcp("example.test", 80)
                if expected is not asyncio.CancelledError:
                    self.assertIs(caught.exception.__cause__, error)
                self.assertTrue(sock.closed)

    async def test_closes_anyio_owner_if_httpcore_wrapper_fails(self):
        backend = _AsyncFreebindBackend(Source("192.0.2.1"))
        sock = FakeSocket()
        byte_stream = FakeByteStream(sock)
        with (
            patch(
                "freebind.httpx.async_create_connection",
                new=AsyncMock(return_value=sock),
            ),
            patch(
                "freebind.httpx.SocketStream.from_socket",
                new=AsyncMock(return_value=byte_stream),
            ),
            patch("freebind.httpx._AnyIOStream", side_effect=RuntimeError("wrap")),
        ):
            with self.assertRaisesRegex(RuntimeError, "wrap"):
                await backend.connect_tcp("example.test", 80)
        self.assertTrue(byte_stream.closed)
        self.assertEqual(byte_stream.close_calls, 1)
        self.assertEqual(sock.close_calls, 1)

    async def test_live_anyio_wrapper_reads_and_writes_socketpair(self):
        left, right = socket.socketpair()
        left.setblocking(False)
        stream = None
        try:
            with patch(
                "freebind.httpx.async_create_connection",
                new=AsyncMock(return_value=left),
            ):
                stream = await _AsyncFreebindBackend(Source("192.0.2.1")).connect_tcp(
                    "example.test", 80
                )
            right.sendall(b"hi")
            self.assertEqual(await stream.read(2, timeout=1), b"hi")
            await stream.write(b"ok", timeout=1)
            self.assertEqual(right.recv(2), b"ok")
        finally:
            if stream is None:
                left.close()
            else:
                await stream.aclose()
            right.close()

    async def test_maps_connection_errors(self):
        backend = _AsyncFreebindBackend(Source("192.0.2.1"))
        for error, expected in (
            (TimeoutError("timed out"), httpcore.ConnectTimeout),
            (OSError("blocked"), httpcore.ConnectError),
        ):
            with (
                self.subTest(expected=expected),
                patch(
                    "freebind.httpx.async_create_connection",
                    new=AsyncMock(side_effect=error),
                ),
            ):
                with self.assertRaises(expected) as caught:
                    await backend.connect_tcp("example.test", 80)
                self.assertIs(caught.exception.__cause__, error)


class AsyncTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_injects_backend_and_preserves_httpx_streaming(self):
        transport = AsyncFreebindTransport(Source("192.0.2.1"))
        self.assertIs(
            transport._pool._network_backend,
            transport._freebind_backend,
        )
        self.assertIsInstance(transport._pool._network_backend, httpcore.AnyIOBackend)

        async def body():
            yield b"hello "
            yield b"world"

        core_response = httpcore.Response(
            200,
            headers=[(b"content-length", b"11")],
            content=body(),
        )
        try:
            async with httpx.AsyncClient(transport=transport) as client:
                with patch.object(
                    transport._pool,
                    "handle_async_request",
                    new=AsyncMock(return_value=core_response),
                ) as handle_request:
                    response = await client.get("http://example.test/")
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.content, b"hello world")
                core_request = handle_request.call_args.args[0]
                self.assertEqual(core_request.method, b"GET")
                self.assertEqual(core_request.url.host, b"example.test")
        finally:
            await transport.aclose()

    async def test_preserves_httpx_exception_translation_and_cause(self):
        transport = AsyncFreebindTransport(Source("192.0.2.1"))
        failure = httpcore.ConnectError("blocked")
        try:
            async with httpx.AsyncClient(transport=transport) as client:
                with patch.object(
                    transport._pool,
                    "handle_async_request",
                    new=AsyncMock(side_effect=failure),
                ):
                    with self.assertRaises(httpx.ConnectError) as caught:
                        await client.get("http://example.test/")
            self.assertIs(caught.exception.__cause__, failure)
        finally:
            await transport.aclose()

    async def test_rejects_proxy_uds_and_local_address(self):
        source = Source("192.0.2.1")
        for options in (
            {"proxy": "http://proxy.test"},
            {"uds": "/tmp/http.sock"},
            {"local_address": "192.0.2.2"},
        ):
            with self.subTest(options=options), self.assertRaises(ValueError):
                AsyncFreebindTransport(source, **options)


if __name__ == "__main__":
    unittest.main()
