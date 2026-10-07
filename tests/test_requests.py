import socket
import ssl
import unittest
from unittest.mock import Mock, patch

from urllib3.connection import HTTPSConnection
from urllib3.exceptions import NameResolutionError

from freebind._source import Source
from freebind.requests import FreebindAdapter, _FreebindHTTPConnection, _FreebindHTTPSConnection


class RequestsAdapterTests(unittest.TestCase):
    def setUp(self):
        self.source = Source("198.51.100.0/24")

    def test_pool_policy_is_injected_after_key_and_mappings_are_private(self):
        first = FreebindAdapter(self.source)
        second = FreebindAdapter(self.source)
        try:
            for scheme, cls in (
                ("http", _FreebindHTTPConnection),
                ("https", _FreebindHTTPSConnection),
            ):
                with self.subTest(scheme=scheme):
                    pool = first.poolmanager.connection_from_url(f"{scheme}://example.test")
                    key = first.poolmanager.key_fn_by_scheme[scheme](
                        {"scheme": scheme, "host": "example.test", "port": 80 if scheme == "http" else 443}
                    )
                    conn = pool._new_conn()
                    try:
                        self.assertNotIn("source", key._fields)
                        self.assertIs(type(conn), cls)
                        self.assertIs(conn._freebind_source, self.source)
                    finally:
                        conn.close()
            self.assertIsNot(
                first.poolmanager.pool_classes_by_scheme,
                second.poolmanager.pool_classes_by_scheme,
            )
        finally:
            first.close()
            second.close()

    def test_http_and_https_use_core_connector_and_retain_native_tls(self):
        context = ssl.create_default_context()
        for cls, port, kwargs in (
            (_FreebindHTTPConnection, 80, {}),
            (_FreebindHTTPSConnection, 443, {"ssl_context": context, "server_hostname": "tls.example.test"}),
        ):
            conn = cls("example.test", port, source=self.source, **kwargs)
            with self.subTest(connection=cls.__name__), patch(
                "freebind.requests.create_connection", return_value=object()
            ) as connector:
                sock = conn._new_conn()
                self.assertIs(sock, connector.return_value)
                connector.assert_called_once_with(
                    ("example.test", port),
                    self.source,
                    timeout=conn.timeout,
                    socket_options=conn.socket_options,
                )
            if cls is _FreebindHTTPSConnection:
                self.assertIs(conn.ssl_context, context)
                self.assertIs(_FreebindHTTPSConnection.connect, HTTPSConnection.connect)
            conn.close()

    def test_audit_failure_closes_connected_socket(self):
        conn = _FreebindHTTPConnection("example.test", 80, source=self.source)
        sock = Mock()
        try:
            with patch("freebind.requests.create_connection", return_value=sock), patch(
                "freebind.requests.sys.audit", side_effect=RuntimeError("blocked")
            ):
                with self.assertRaisesRegex(RuntimeError, "blocked"):
                    conn._new_conn()
            sock.close.assert_called_once_with()
        finally:
            conn.close()

    def test_connector_errors_keep_urllib3_wrapping(self):
        conn = _FreebindHTTPConnection("example.test", 80, source=self.source)
        cause = socket.gaierror(socket.EAI_NONAME, "missing")
        try:
            with patch("freebind.requests.create_connection", side_effect=cause):
                with self.assertRaises(NameResolutionError) as raised:
                    conn._new_conn()
            self.assertIs(raised.exception.__cause__, cause)
        finally:
            conn.close()

    def test_conflicting_socket_options_are_rejected(self):
        for option in ("source_address", "socket_factory"):
            with self.subTest(option=option), self.assertRaises(TypeError):
                FreebindAdapter(self.source, **{option: None})

    def test_proxy_is_rejected_before_pool_use(self):
        adapter = FreebindAdapter(self.source)
        try:
            with patch.object(adapter.poolmanager, "connection_from_url") as get_pool:
                with self.assertRaisesRegex(ValueError, "proxies are not supported"):
                    adapter.send(None, proxies={"http": "http://proxy.test"})
                get_pool.assert_not_called()
            with self.assertRaisesRegex(ValueError, "proxies are not supported"):
                adapter.proxy_manager_for("http://proxy.test")
        finally:
            adapter.close()


if __name__ == "__main__":
    unittest.main()
