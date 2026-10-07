"""Real-network Freebind checks; run only inside ``network_harness.py``."""

import asyncio
from contextlib import contextmanager
import ipaddress
import json
import os
import socket
import ssl
import unittest
from unittest.mock import patch as mock_patch


_HARNESS_MARKER = "FREEBIND_HARNESS_CAPS_DROPPED"
_HARNESS_COMMAND = (
    ".venv/bin/python tests/network_harness.py -- .venv/bin/python -m unittest "
    "discover -s tests -p test_network.py -v"
)
_requires_harness = unittest.skipUnless(
    os.environ.get(_HARNESS_MARKER) == "1",
    f"requires the dropped-capability network harness; run: {_HARNESS_COMMAND}",
)
_TLS_HOST = "peer.freebind.test"


def _env(name):
    return os.environ[f"FREEBIND_HARNESS_{name}"]


def _families():
    return (
        (socket.AF_INET, _env("PEER4"), _env("CLIENT4"), "4"),
        (socket.AF_INET6, _env("PEER6"), _env("CLIENT6"), "6"),
    )


def _source_address(family, offset):
    suffix = "4" if family == socket.AF_INET else "6"
    network = ipaddress.ip_network(_env(f"SOURCE{suffix}"))
    return str(network.network_address + offset)


def _random_prefix(family):
    suffix = "4" if family == socket.AF_INET else "6"
    network = ipaddress.ip_network(_env(f"SOURCE{suffix}"))
    return f"{network.network_address}/{network.max_prefixlen - 8}"


def _tcp_report(peer, source):
    from freebind import create_connection

    sock = create_connection((peer, int(_env("TCP_PORT"))), source, timeout=4)
    with sock:
        local = sock.getsockname()[0]
        sock.sendall(b"report\n")
        with sock.makefile("rb") as reader:
            report = json.loads(reader.readline())
    return report, local


def _udp_report(family, peer, source):
    from freebind import new_socket

    with new_socket(source, family=family, type=socket.SOCK_DGRAM) as sock:
        sock.settimeout(4)
        sock.sendto(b"report", (peer, int(_env("UDP_PORT"))))
        payload, address = sock.recvfrom(4096)
        return json.loads(payload), address[0]


def _assert_source(test, report, local, expected):
    test.assertEqual(report["source"], expected)
    test.assertEqual(local, expected)
    test.assertTrue(report["connection_id"])


def _http_url(family):
    suffix = "4" if family == socket.AF_INET else "6"
    return _env(f"HTTP{suffix}_URL")


def _https_url():
    return f"https://{_TLS_HOST}:{_env('HTTPS_PORT')}/report"


@contextmanager
def _resolve_test_hostname(peer):
    original = socket.getaddrinfo

    def resolve(host, *args, **kwargs):
        return original(peer if host == _TLS_HOST else host, *args, **kwargs)

    with mock_patch("socket.getaddrinfo", side_effect=resolve):
        yield


def _sync_reports(client, url):
    reports = []
    for _ in range(2):
        response = client.get(url, timeout=4)
        try:
            response.raise_for_status()
            reports.append(response.json())
        finally:
            response.close()
    return reports


def _assert_pool_reports(test, pooled, fresh, expected):
    for report in (*pooled, *fresh):
        test.assertEqual(report["source"], expected)
    test.assertEqual(pooled[0]["connection_id"], pooled[1]["connection_id"])
    test.assertNotEqual(fresh[0]["connection_id"], fresh[1]["connection_id"])


def _assert_fresh_source_reports(test, reports, expected):
    test.assertEqual([report["source"] for report in reports], list(expected))
    test.assertNotEqual(reports[0]["connection_id"], reports[1]["connection_id"])


@_requires_harness
class NetworkParityTests(unittest.TestCase):
    def test_raw_sync_tcp_udp_and_controlled_source_modes(self):
        from freebind import Source

        for family, peer, _client, _suffix in _families():
            with self.subTest(family=family, mode="fixed"):
                fixed = _source_address(family, 7)
                report, local = _tcp_report(peer, Source(fixed))
                _assert_source(self, report, local, fixed)
                report, returned_from = _udp_report(family, peer, Source(fixed))
                self.assertEqual(report["source"], fixed)
                self.assertEqual(returned_from, peer)

            with self.subTest(family=family, mode="random"):
                draws = (17, 18)
                with mock_patch(
                    "freebind._source.secrets.randbits", side_effect=draws
                ):
                    random_source = Source(_random_prefix(family), bits=8)
                    reports = [
                        _tcp_report(peer, random_source)
                        for _ in draws
                    ]
                expected = [
                    str(ipaddress.ip_address(_source_address(family, 0)) + draw)
                    for draw in draws
                ]
                for (report, local), address in zip(reports, expected):
                    _assert_source(self, report, local, address)

            with self.subTest(family=family, mode="sticky"):
                sticky_address = str(
                    ipaddress.ip_address(_source_address(family, 0)) + 23
                )
                with mock_patch("freebind._source.secrets.randbits", return_value=23):
                    sticky = Source(_random_prefix(family), mode="sticky", bits=8)
                reports = [_tcp_report(peer, sticky) for _ in range(2)]
                for report, local in reports:
                    _assert_source(self, report, local, sticky_address)

    def test_both_patch_timings_bind_tcp_and_udp_for_both_families(self):
        from freebind import Source, patch

        for family, peer, _client, _suffix in _families():
            expected = _source_address(family, 31)
            for entrypoint in ("connect", "socket"):
                for socket_type in (socket.SOCK_STREAM, socket.SOCK_DGRAM):
                    with self.subTest(
                        family=family, entrypoint=entrypoint, type=socket_type
                    ):
                        with patch(Source(expected), entrypoint=entrypoint):
                            with socket.socket(family, socket_type) as sock:
                                sock.settimeout(4)
                                if socket_type == socket.SOCK_STREAM:
                                    sock.connect((peer, int(_env("TCP_PORT"))))
                                    sock.sendall(b"report\n")
                                    with sock.makefile("rb") as reader:
                                        report = json.loads(reader.readline())
                                else:
                                    sock.sendto(
                                        b"report", (peer, int(_env("UDP_PORT")))
                                    )
                                    report = json.loads(sock.recvfrom(4096)[0])
                                local = sock.getsockname()[0]
                        _assert_source(self, report, local, expected)

    def test_interface_family_fallback_and_unavailable_routes(self):
        from freebind import FamilyMismatchError, Source, create_connection

        for family, peer, _client, _suffix in _families():
            with self.subTest(family=family, behavior="interface"):
                expected = _source_address(family, 33)
                report, local = _tcp_report(
                    peer, Source(expected, interface="eth0")
                )
                _assert_source(self, report, local, expected)

        family4, _peer4, _client4, _suffix4 = _families()[0]
        peer6 = _families()[1][1]
        with self.assertRaises(FamilyMismatchError):
            create_connection(
                (peer6, int(_env("TCP_PORT"))),
                Source(_source_address(family4, 34)),
                timeout=2,
            )

        report, local = _tcp_report(
            peer6,
            Source(_source_address(family4, 34), strict=False),
        )
        expected_fallback = _families()[1][2]
        self.assertEqual(report["source"], expected_fallback)
        self.assertEqual(local, expected_fallback)

        for family, _peer, _client, suffix in _families():
            unreachable = "198.51.100.254" if suffix == "4" else "2001:db8:ffff::1"
            with self.subTest(family=family, behavior="unavailable-route"):
                with self.assertRaises(OSError):
                    create_connection(
                        (unreachable, int(_env("TCP_PORT"))),
                        Source(_source_address(family, 35)),
                        timeout=0.5,
                    )

    def test_requests_and_httpx_default_pooling_and_fresh_connections(self):
        import httpx
        import requests
        from freebind import Source
        from freebind.httpx import FreebindTransport
        from freebind.requests import FreebindAdapter

        for family, _peer, _client, _suffix in _families():
            expected = _source_address(family, 41)
            source = Source(expected)
            url = _http_url(family)

            request_reports = []
            for fresh in (False, True):
                with requests.Session() as client:
                    client.trust_env = False
                    client.mount("http://", FreebindAdapter(source, fresh=fresh))
                    request_reports.append(_sync_reports(client, url))
            _assert_pool_reports(self, *request_reports, expected)

            httpx_reports = []
            for fresh in (False, True):
                transport = FreebindTransport(source, fresh=fresh)
                with httpx.Client(transport=transport, trust_env=False) as client:
                    httpx_reports.append(_sync_reports(client, url))
            _assert_pool_reports(self, *httpx_reports, expected)

            sticky_address = str(ipaddress.ip_address(_source_address(family, 0)) + 29)
            with mock_patch("freebind._source.secrets.randbits", return_value=29):
                sticky = Source(_random_prefix(family), mode="sticky", bits=8)
                transport = FreebindTransport(sticky, fresh=True)
                with httpx.Client(transport=transport, trust_env=False) as client:
                    reports = _sync_reports(client, url)
            _assert_fresh_source_reports(
                self, reports, (sticky_address, sticky_address)
            )

            draws = (37, 38)
            random_source = Source(_random_prefix(family), bits=8)
            transport = FreebindTransport(random_source, fresh=True)
            with mock_patch("freebind._source.secrets.randbits", side_effect=draws):
                with httpx.Client(transport=transport, trust_env=False) as client:
                    reports = _sync_reports(client, url)
            expected_draws = tuple(
                str(ipaddress.ip_address(_source_address(family, 0)) + draw)
                for draw in draws
            )
            _assert_fresh_source_reports(self, reports, expected_draws)

    def test_core_requests_and_httpx_tls_trust_sni_and_rejection(self):
        import httpx
        import requests
        from http.client import HTTPResponse
        from freebind import Source, create_connection
        from freebind.httpx import FreebindTransport
        from freebind.requests import FreebindAdapter

        ca_file = _env("CA_FILE")
        url = _https_url()
        for family, peer, _client, _suffix in _families():
            expected = _source_address(family, 43)
            source = Source(expected)

            with _resolve_test_hostname(peer):
                raw = create_connection(
                    (_TLS_HOST, int(_env("HTTPS_PORT"))), source, timeout=4
                )
            context = ssl.create_default_context(cafile=ca_file)
            with context.wrap_socket(raw, server_hostname=_TLS_HOST) as tls:
                tls.sendall(
                    f"GET /report HTTP/1.1\r\nHost: {_TLS_HOST}\r\n\r\n".encode()
                )
                response = HTTPResponse(tls)
                response.begin()
                report = json.loads(response.read())
            self.assertEqual(report["source"], expected)
            self.assertEqual(report["tls_server_name"], _TLS_HOST)

            with requests.Session() as client:
                client.trust_env = False
                client.mount("https://", FreebindAdapter(source))
                with _resolve_test_hostname(peer):
                    with client.get(url, verify=ca_file, timeout=4) as response:
                        requests_report = response.json()
            self.assertEqual(requests_report["source"], expected)
            self.assertEqual(requests_report["tls_server_name"], _TLS_HOST)

            with requests.Session() as client:
                client.trust_env = False
                client.mount("https://", FreebindAdapter(source))
                with self.assertRaises(requests.exceptions.SSLError) as failure:
                    with _resolve_test_hostname(peer):
                        client.get(url, verify=True, timeout=4)
                self.assertIn("CERTIFICATE_VERIFY_FAILED", str(failure.exception))

            transport = FreebindTransport(source, verify=ssl.create_default_context(cafile=ca_file))
            with httpx.Client(transport=transport, trust_env=False) as client:
                with _resolve_test_hostname(peer):
                    response = client.get(url, timeout=4)
                httpx_report = response.json()
            self.assertEqual(httpx_report["source"], expected)
            self.assertEqual(httpx_report["tls_server_name"], _TLS_HOST)

            transport = FreebindTransport(source)
            with httpx.Client(transport=transport, trust_env=False) as client:
                with self.assertRaises(httpx.ConnectError) as failure:
                    with _resolve_test_hostname(peer):
                        client.get(url, timeout=4)
                self.assertIn("CERTIFICATE_VERIFY_FAILED", str(failure.exception))


@_requires_harness
class AsyncNetworkParityTests(unittest.IsolatedAsyncioTestCase):
    async def test_async_tcp_source_and_return_traffic_for_both_families(self):
        from freebind import Source, async_create_connection

        for family, peer, _client, _suffix in _families():
            expected = _source_address(family, 51)
            sock = await async_create_connection(
                (peer, int(_env("TCP_PORT"))), Source(expected), timeout=4
            )
            try:
                reader, writer = await asyncio.open_connection(sock=sock)
            except BaseException:
                sock.close()
                raise
            try:
                local = writer.get_extra_info("sockname")[0]
                writer.write(b"report\n")
                await writer.drain()
                report = json.loads(await asyncio.wait_for(reader.readline(), 4))
            finally:
                writer.close()
                await writer.wait_closed()
            _assert_source(self, report, local, expected)

    async def test_aiohttp_and_async_httpx_pooling_and_fresh_connections(self):
        import aiohttp
        import httpx
        from freebind import Source
        from freebind.aiohttp import FreebindConnector
        from freebind.httpx import AsyncFreebindTransport

        for family, _peer, _client, _suffix in _families():
            expected = _source_address(family, 53)
            source = Source(expected)
            url = _http_url(family)

            aio_reports = []
            for fresh in (False, True):
                connector = FreebindConnector(source, fresh=fresh)
                async with aiohttp.ClientSession(
                    connector=connector, trust_env=False
                ) as client:
                    reports = []
                    for _ in range(2):
                        async with client.get(url, timeout=4) as response:
                            response.raise_for_status()
                            reports.append(await response.json())
                    aio_reports.append(reports)
            _assert_pool_reports(self, *aio_reports, expected)

            httpx_reports = []
            for fresh in (False, True):
                transport = AsyncFreebindTransport(source, fresh=fresh)
                async with httpx.AsyncClient(
                    transport=transport, trust_env=False
                ) as client:
                    reports = []
                    for _ in range(2):
                        response = await client.get(url, timeout=4)
                        response.raise_for_status()
                        reports.append(response.json())
                    httpx_reports.append(reports)
            _assert_pool_reports(self, *httpx_reports, expected)

    async def test_aiohttp_and_async_httpx_tls_trust_sni_and_rejection(self):
        import aiohttp
        import httpx
        from freebind import Source
        from freebind.aiohttp import FreebindConnector
        from freebind.httpx import AsyncFreebindTransport

        url = _https_url()
        ca_file = _env("CA_FILE")
        context = ssl.create_default_context(cafile=ca_file)
        for family, peer, _client, _suffix in _families():
            expected = _source_address(family, 55)
            source = Source(expected)

            connector = FreebindConnector(source)
            async with aiohttp.ClientSession(
                connector=connector, trust_env=False
            ) as client:
                with _resolve_test_hostname(peer):
                    async with client.get(url, ssl=context, timeout=4) as response:
                        response.raise_for_status()
                        report = await response.json()
            self.assertEqual(report["source"], expected)
            self.assertEqual(report["tls_server_name"], _TLS_HOST)

            connector = FreebindConnector(source)
            async with aiohttp.ClientSession(
                connector=connector, trust_env=False
            ) as client:
                with self.assertRaises(aiohttp.ClientConnectorCertificateError):
                    with _resolve_test_hostname(peer):
                        await client.get(url, timeout=4)

            transport = AsyncFreebindTransport(source, verify=ssl.create_default_context(cafile=ca_file))
            async with httpx.AsyncClient(
                transport=transport, trust_env=False
            ) as client:
                with _resolve_test_hostname(peer):
                    response = await client.get(url, timeout=4)
                report = response.json()
            self.assertEqual(report["source"], expected)
            self.assertEqual(report["tls_server_name"], _TLS_HOST)

            transport = AsyncFreebindTransport(source)
            async with httpx.AsyncClient(
                transport=transport, trust_env=False
            ) as client:
                with self.assertRaises(httpx.ConnectError) as failure:
                    with _resolve_test_hostname(peer):
                        await client.get(url, timeout=4)
                self.assertIn("CERTIFICATE_VERIFY_FAILED", str(failure.exception))
