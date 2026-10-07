import asyncio
import errno
import socket
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from aiohttp import ClientConnectorDNSError, ClientSession
from aiohttp.abc import AbstractResolver

from freebind._source import FamilyMismatchError, Source
from freebind.aiohttp import FreebindConnector


class StubResolver(AbstractResolver):
    def __init__(self, addresses):
        self.addresses = addresses
        self.calls = 0

    async def resolve(self, host, port=0, family=socket.AF_INET):
        self.calls += 1
        return [dict(address, hostname=host, port=port) for address in self.addresses]

    async def close(self):
        pass


def resolved(host, family):
    return {
        "host": host,
        "family": family,
        "proto": socket.IPPROTO_TCP,
        "flags": 0,
    }


class FreebindConnectorTests(unittest.IsolatedAsyncioTestCase):
    async def test_filters_mixed_and_cached_dns_answers(self):
        resolver = StubResolver(
            (resolved("192.0.2.10", socket.AF_INET), resolved("2001:db8::10", socket.AF_INET6))
        )
        connector = FreebindConnector(
            Source("192.0.2.1"), resolver=resolver, use_dns_cache=True
        )
        try:
            first = await connector._resolve_host("example.test", 80)
            second = await connector._resolve_host("example.test", 80)
            self.assertEqual([entry["family"] for entry in first], [socket.AF_INET])
            self.assertEqual([entry["family"] for entry in second], [socket.AF_INET])
            self.assertEqual(resolver.calls, 1)
        finally:
            await connector.close()

    async def test_non_strict_falls_back_only_without_a_matching_answer(self):
        resolver = StubResolver((resolved("2001:db8::10", socket.AF_INET6),))
        strict = FreebindConnector(
            Source("192.0.2.1"), resolver=resolver, use_dns_cache=False
        )
        try:
            with self.assertRaises(FamilyMismatchError):
                await strict._resolve_host("example.test", 80)
        finally:
            await strict.close()

        fallback = FreebindConnector(
            Source("192.0.2.1", strict=False), resolver=resolver, use_dns_cache=False
        )
        try:
            answers = await fallback._resolve_host("example.test", 80)
            self.assertEqual([entry["family"] for entry in answers], [socket.AF_INET6])
            factory = fallback._socket_factory
            with patch("freebind.aiohttp.new_socket", return_value=object()) as make_socket:
                factory((socket.AF_INET6, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("2001:db8::10", 80, 0, 0)))
            make_socket.assert_called_once_with(
                fallback._freebind_source,
                family=socket.AF_INET6,
                type=socket.SOCK_STREAM,
            )
        finally:
            await fallback.close()

    async def test_factory_setup_failure_closes_core_owned_socket(self):
        connector = FreebindConnector(Source("192.0.2.1"))
        sock = MagicMock()
        sock.family = socket.AF_INET
        sock.getpeername.side_effect = OSError(errno.ENOTCONN, "not connected")
        sock.getsockname.return_value = ("0.0.0.0", 0)
        try:
            with (
                patch("freebind._socket._create_unpatched_socket", return_value=sock),
                patch("freebind._socket.enable_freebind", side_effect=OSError(errno.EPERM, "denied")),
            ):
                with self.assertRaises(OSError):
                    connector._socket_factory(
                        (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("192.0.2.10", 80))
                    )
            sock.close.assert_called_once_with()
        finally:
            await connector.close()

    async def test_family_mismatch_keeps_aiohttp_error_and_cause(self):
        resolver = StubResolver((resolved("2001:db8::10", socket.AF_INET6),))
        connector = FreebindConnector(
            Source("192.0.2.1"), resolver=resolver, use_dns_cache=False
        )
        async with ClientSession(connector=connector) as session:
            with self.assertRaises(ClientConnectorDNSError) as caught:
                await session.get("http://example.test/")
        self.assertIsInstance(caught.exception.__cause__, FamilyMismatchError)

    async def test_numeric_host_family_comes_from_literal(self):
        for host, family, source in (
            ("192.0.2.10", socket.AF_INET, Source("192.0.2.1")),
            ("2001:db8::10", socket.AF_INET6, Source("2001:db8::1")),
        ):
            with self.subTest(host=host):
                connector = FreebindConnector(source)
                try:
                    answers = await connector._resolve_host(host, 80)
                    self.assertEqual([entry["family"] for entry in answers], [family])
                finally:
                    await connector.close()

    async def test_rejects_conflicts_and_applies_fresh_mode(self):
        with self.assertRaises(ValueError):
            FreebindConnector(Source("192.0.2.1"), local_addr=("127.0.0.1", 0))
        with self.assertRaises(ValueError):
            FreebindConnector(Source("192.0.2.1"), socket_factory=lambda _: None)

        connector = FreebindConnector(Source("192.0.2.1"), fresh=True)
        try:
            self.assertTrue(connector._force_close)
        finally:
            await connector.close()

    async def test_proxy_rejected_before_pool_lookup(self):
        connector = FreebindConnector(Source("192.0.2.1"))
        try:
            with patch.object(connector, "_get", side_effect=AssertionError("pool lookup")):
                with self.assertRaisesRegex(ValueError, "does not support proxies"):
                    await connector.connect(
                        SimpleNamespace(proxy=object()), [], SimpleNamespace()
                    )
        finally:
            await connector.close()

    async def test_fresh_mode_opens_a_new_loopback_connection_per_request(self):
        accepted = 0

        async def handle(reader, writer):
            nonlocal accepted
            accepted += 1
            try:
                while await reader.readline() not in (b"\r\n", b"\n", b""):
                    pass
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")
                await writer.drain()
                while await reader.readline() not in (b"\r\n", b"\n", b""):
                    pass
            except (ConnectionError, OSError):
                pass
            finally:
                writer.close()
                await writer.wait_closed()

        server = await asyncio.start_server(handle, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        try:
            async with ClientSession(
                connector=FreebindConnector(Source("127.0.0.1"), fresh=True)
            ) as session:
                for _ in range(2):
                    async with session.get(f"http://127.0.0.1:{port}/") as response:
                        self.assertEqual(await response.read(), b"ok")
            self.assertEqual(accepted, 2)
        finally:
            server.close()
            await server.wait_closed()


if __name__ == "__main__":
    unittest.main()
