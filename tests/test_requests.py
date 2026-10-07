import socket
import ssl
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import Mock, patch

from urllib3.connection import HTTPSConnection
from urllib3.exceptions import NameResolutionError
from urllib3.util.retry import Retry

import requests

from freebind._source import Source
from freebind.requests import FreebindAdapter, _FreebindHTTPConnection, _FreebindHTTPSConnection


class _KeepAliveServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self):
        self.events = []
        self.lock = threading.Lock()
        self.retry_count = 0
        super().__init__(("127.0.0.1", 0), _KeepAliveHandler)

    def handle_error(self, request, client_address):
        pass


class _KeepAliveHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self):
        server = self.server
        with server.lock:
            server.events.append((self.path, self.connection))
            if self.path == "/retry":
                server.retry_count += 1
                retry_count = server.retry_count
            else:
                retry_count = 0

        location = "/ok" if self.path == "/redirect" else None
        status = 503 if self.path == "/retry" and retry_count == 1 else (302 if location else 200)
        body = b"x" * 32768 if self.path == "/large" else b"" if location else b"ok"
        self.send_response(status)
        if location:
            self.send_header("Location", location)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def log_message(self, format, *args):
        pass


class RequestsAdapterTests(unittest.TestCase):
    def setUp(self):
        self.source = Source("198.51.100.0/24")

    def test_pool_policy_is_injected_after_key_and_mappings_are_private(self):
        first = FreebindAdapter(self.source, fresh=True)
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
                        self.assertNotIn("fresh", key._fields)
                        self.assertTrue(pool._freebind_fresh)
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

    @classmethod
    def setUpClass(cls):
        cls.server = _KeepAliveServer()
        cls.server_thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.server_thread.start()
        cls.url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.server_thread.join(timeout=2)

    def setUp(self):
        super().setUp()
        self.source = Source("198.51.100.0/24")
        with self.server.lock:
            self.server.events.clear()
            self.server.retry_count = 0

    def _session(self, *, fresh=False, block=True, max_retries=0):
        session = requests.Session()
        session.trust_env = False
        session.mount(
            "http://",
            FreebindAdapter(
                Source("127.0.0.1"),
                fresh=fresh,
                pool_connections=1,
                pool_maxsize=1,
                pool_block=block,
                max_retries=max_retries,
            ),
        )
        return session

    def _connections(self, path):
        with self.server.lock:
            return [connection for requested_path, connection in self.server.events if requested_path == path]

    def test_default_pooling_reuses_keepalive_connection(self):
        session = self._session()
        try:
            self.assertEqual(session.get(self.url + "/ok").content, b"ok")
            self.assertEqual(session.get(self.url + "/ok").content, b"ok")
            connections = self._connections("/ok")
            self.assertEqual(len(connections), 2)
            self.assertIs(connections[0], connections[1])
        finally:
            session.close()

    def test_fresh_sequential_redirect_and_retry_connections(self):
        retry = Retry(
            total=1,
            connect=0,
            read=0,
            status=1,
            backoff_factor=0,
            status_forcelist={503},
            allowed_methods={"GET"},
        )
        session = self._session(fresh=True, max_retries=retry)
        try:
            self.assertEqual(session.get(self.url + "/ok").content, b"ok")
            self.assertEqual(session.get(self.url + "/ok").content, b"ok")
            ok_connections = self._connections("/ok")
            self.assertIsNot(ok_connections[0], ok_connections[1])

            response = session.get(self.url + "/redirect")
            self.assertEqual(response.status_code, 200)
            redirect_connections = self._connections("/redirect") + self._connections("/ok")[2:]
            self.assertEqual(len(redirect_connections), 2)
            self.assertIsNot(redirect_connections[0], redirect_connections[1])

            response = session.get(self.url + "/retry")
            self.assertEqual(response.status_code, 200)
            retry_connections = self._connections("/retry")
            self.assertEqual(len(retry_connections), 2)
            self.assertIsNot(retry_connections[0], retry_connections[1])
        finally:
            session.close()

    def test_closing_unconsumed_stream_releases_blocking_pool_slot(self):
        session = self._session(fresh=True, block=True)
        first = session.get(self.url + "/large", stream=True)
        first_connection = first.raw._connection
        waiting_for_pool = threading.Event()
        done = threading.Event()
        outcome = {}
        pool = session.get_adapter(self.url).poolmanager.connection_from_url(self.url)
        pool_class = type(pool)
        original_get_conn = pool_class._get_conn

        def tracked_get_conn(connection_pool, timeout=None):
            waiting_for_pool.set()
            return original_get_conn(connection_pool, timeout)

        def request_while_stream_is_open():
            try:
                outcome["response"] = session.get(self.url + "/next")
            except BaseException as exc:
                outcome["error"] = exc
            finally:
                done.set()

        worker = threading.Thread(target=request_while_stream_is_open, daemon=True)
        try:
            self.assertIsNotNone(first_connection)
            self.assertIsNotNone(first_connection.sock)
            with patch.object(pool_class, "_get_conn", tracked_get_conn):
                worker.start()
                self.assertTrue(waiting_for_pool.wait(1))
                self.assertFalse(done.is_set())
                self.assertIs(first.raw._connection, first_connection)

                first.close()
                self.assertTrue(done.wait(2), "fresh response close did not release pool capacity")
                self.assertNotIn("error", outcome)
                self.assertEqual(outcome["response"].content, b"ok")
                self.assertIsNot(self._connections("/large")[0], self._connections("/next")[0])
        finally:
            first.close()
            if "response" in outcome:
                outcome["response"].close()
            session.close()

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
