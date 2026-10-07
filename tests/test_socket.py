import errno
import socket
import unittest
from contextlib import ExitStack, contextmanager
from unittest.mock import patch

from freebind import _socket


class RecordingSocket:
    def __init__(self, family, error=None):
        self.family = family
        self.error = error
        self.calls = []
        self.operations = []
        self.local = {
            socket.AF_INET: ("0.0.0.0", 0),
            socket.AF_INET6: ("::", 0, 0, 0),
        }.get(family, ("", 0))
        self.closed = False
        self.type = socket.SOCK_STREAM
        self.proto = 0
        self.peer = None
        self.timeouts = []

    def setsockopt(self, level, option, value):
        self.calls.append((level, option, value))
        self.operations.append(("setsockopt", level, option, value))
        if self.error:
            error, self.error = self.error, None
            raise error

    def getsockname(self):
        return self.local

    def getpeername(self):
        if self.peer is None:
            raise OSError(errno.ENOTCONN, "not connected")
        return self.peer

    def bind(self, address):
        self.operations.append(("bind", address))
        self.local = address

    def settimeout(self, timeout):
        self.operations.append(("settimeout", timeout))
        self.timeouts.append(timeout)

    def close(self):
        self.closed = True


class EnableFreebindTests(unittest.TestCase):
    def test_ipv4_uses_linux_option(self):
        sock = RecordingSocket(socket.AF_INET)

        _socket.enable_freebind(sock)

        self.assertEqual(
            sock.calls,
            [(socket.IPPROTO_IP, getattr(socket, "IP_FREEBIND", 15), 1)],
        )

    def test_ipv6_uses_family_option(self):
        sock = RecordingSocket(socket.AF_INET6)

        _socket.enable_freebind(sock)

        self.assertEqual(
            sock.calls,
            [(socket.IPPROTO_IPV6, getattr(socket, "IPV6_FREEBIND", 78), 1)],
        )

    def test_ipv6_falls_back_only_for_unsupported_option(self):
        sock = RecordingSocket(
            socket.AF_INET6,
            OSError(errno.ENOPROTOOPT, "unsupported option"),
        )

        _socket.enable_freebind(sock)

        self.assertEqual(
            sock.calls,
            [
                (socket.IPPROTO_IPV6, getattr(socket, "IPV6_FREEBIND", 78), 1),
                (socket.IPPROTO_IP, getattr(socket, "IP_FREEBIND", 15), 1),
            ],
        )

    def test_ipv6_permission_error_propagates_without_fallback(self):
        error = OSError(errno.EPERM, "permission denied")
        sock = RecordingSocket(socket.AF_INET6, error)

        with self.assertRaises(OSError) as raised:
            _socket.enable_freebind(sock)

        self.assertIs(raised.exception, error)
        self.assertEqual(
            sock.calls,
            [(socket.IPPROTO_IPV6, getattr(socket, "IPV6_FREEBIND", 78), 1)],
        )

    def test_non_linux_is_explicitly_unsupported(self):
        sock = RecordingSocket(socket.AF_INET)
        with patch.object(_socket.sys, "platform", "darwin"):
            with self.assertRaises(NotImplementedError):
                _socket.enable_freebind(sock)
        self.assertEqual(sock.calls, [])

    def test_other_families_are_rejected(self):
        sock = RecordingSocket(socket.AF_UNIX)
        with self.assertRaises(OSError) as raised:
            _socket.enable_freebind(sock)
        self.assertEqual(raised.exception.errno, errno.EAFNOSUPPORT)

    def test_original_methods_are_saved_for_internal_bypass(self):
        self.assertIs(_socket._ORIGINAL_SOCKET, socket.socket)
        self.assertTrue(callable(_socket._ORIGINAL_NEW))
        self.assertTrue(callable(_socket._ORIGINAL_INIT))
        self.assertTrue(callable(_socket._ORIGINAL_BIND))
        self.assertTrue(callable(_socket._ORIGINAL_CONNECT))
        self.assertTrue(callable(_socket._ORIGINAL_CONNECT_EX))
        self.assertTrue(callable(_socket._ORIGINAL_SENDTO))
        self.assertTrue(callable(_socket._ORIGINAL_SENDMSG))


class BindSocketTests(unittest.TestCase):
    def bind(self, sock, source, *, port=0):
        with patch.object(
            _socket,
            "_ORIGINAL_BIND",
            side_effect=lambda target, address: target.bind(address),
        ):
            return _socket.bind_socket(sock, source, port=port)

    def test_interface_freebind_and_bind_order(self):
        sock = RecordingSocket(socket.AF_INET)
        source = _socket.Source("192.0.2.8", interface="eth0")

        selected = self.bind(sock, source, port=8080)

        self.assertEqual(selected, "192.0.2.8")
        self.assertEqual(
            sock.operations,
            [
                ("setsockopt", socket.SOL_SOCKET, getattr(socket, "SO_BINDTODEVICE", 25), b"eth0"),
                ("setsockopt", socket.IPPROTO_IP, getattr(socket, "IP_FREEBIND", 15), 1),
                ("bind", ("192.0.2.8", 8080)),
            ],
        )
        self.assertTrue(_socket._is_explicit_socket(sock))

    def test_address_only_bind_is_rejected(self):
        sock = RecordingSocket(socket.AF_INET)
        sock.local = ("192.0.2.9", 0)

        with self.assertRaises(OSError) as raised:
            self.bind(sock, _socket.Source("192.0.2.8"))

        self.assertEqual(raised.exception.errno, errno.EINVAL)
        self.assertEqual(sock.operations, [])

    def test_non_strict_missing_family_binds_wildcard_and_marks_socket(self):
        sock = RecordingSocket(socket.AF_INET6)
        source = _socket.Source("192.0.2.8", strict=False, interface="eth0")

        selected = self.bind(sock, source, port=5300)

        self.assertIsNone(selected)
        self.assertEqual(sock.local, ("::", 5300, 0, 0))
        self.assertEqual(
            sock.operations,
            [
                ("setsockopt", socket.SOL_SOCKET, getattr(socket, "SO_BINDTODEVICE", 25), b"eth0"),
                ("bind", ("::", 5300, 0, 0)),
            ],
        )
        self.assertTrue(_socket._is_explicit_socket(sock))

    def test_bad_interface_and_port_fail_before_socket_changes(self):
        for interface in ("", "eth\0x", "x" * getattr(socket, "IFNAMSIZ", 16)):
            with self.subTest(interface=interface):
                sock = RecordingSocket(socket.AF_INET)
                with self.assertRaises(ValueError):
                    self.bind(sock, _socket.Source("192.0.2.8", interface=interface))
                self.assertEqual(sock.operations, [])

        for port in (-1, 65536):
            with self.subTest(port=port):
                sock = RecordingSocket(socket.AF_INET)
                with self.assertRaises(ValueError):
                    self.bind(sock, _socket.Source("192.0.2.8"), port=port)
                self.assertEqual(sock.operations, [])

    def test_caller_owned_socket_remains_open_on_failure(self):
        sock = RecordingSocket(socket.AF_INET, OSError(errno.EPERM, "denied"))

        with self.assertRaises(OSError):
            self.bind(sock, _socket.Source("192.0.2.8"))

        self.assertFalse(sock.closed)
        self.assertNotIn("bind", [operation[0] for operation in sock.operations])


class NewSocketTests(unittest.TestCase):
    def fake_factory(self, sock):
        def initialize(target, family, type, proto):
            target.family = family
            target.type = type
            target.local = {
                socket.AF_INET: ("0.0.0.0", 0),
                socket.AF_INET6: ("::", 0, 0, 0),
            }[family]

        return (
            patch.object(_socket, "_ORIGINAL_NEW", return_value=sock),
            patch.object(_socket, "_ORIGINAL_INIT", side_effect=initialize),
            patch.object(
                _socket,
                "_ORIGINAL_BIND",
                side_effect=lambda target, address: target.bind(address),
            ),
        )

    def test_infers_family_and_builds_unpatched_udp_socket(self):
        source = _socket.Source("192.0.2.8")
        sock = RecordingSocket(None)
        patches = self.fake_factory(sock)
        with patches[0], patches[1], patches[2]:
            result = _socket.new_socket(
                source,
                type=socket.SOCK_DGRAM | getattr(socket, "SOCK_NONBLOCK", 0),
                port=53,
            )

        self.assertIs(result, sock)
        self.assertEqual(sock.family, socket.AF_INET)
        self.assertEqual(sock.type, socket.SOCK_DGRAM | getattr(socket, "SOCK_NONBLOCK", 0))
        self.assertEqual(sock.local, ("192.0.2.8", 53))
        self.assertTrue(_socket._is_explicit_socket(sock))

    def test_mixed_family_requires_explicit_family_before_construction(self):
        source = _socket.Source(("192.0.2.8", "2001:db8::8"))
        create = patch.object(_socket, "_ORIGINAL_NEW")
        with create as original_new:
            with self.assertRaises(ValueError):
                _socket.new_socket(source)
        original_new.assert_not_called()

    def test_non_strict_unmatched_family_binds_wildcard_and_marks_socket(self):
        source = _socket.Source("192.0.2.8", strict=False, interface="eth0")
        sock = RecordingSocket(None)
        patches = self.fake_factory(sock)
        with patches[0], patches[1], patches[2]:
            result = _socket.new_socket(source, family=socket.AF_INET6, port=5300)

        self.assertIs(result, sock)
        self.assertEqual(sock.local, ("::", 5300, 0, 0))
        self.assertTrue(_socket._is_explicit_socket(sock))
        self.assertEqual(
            sock.operations,
            [
                ("setsockopt", socket.SOL_SOCKET, getattr(socket, "SO_BINDTODEVICE", 25), b"eth0"),
                ("bind", ("::", 5300, 0, 0)),
            ],
        )

    def test_owned_socket_closes_when_setup_fails(self):
        source = _socket.Source("192.0.2.8")
        sock = RecordingSocket(None, OSError(errno.EPERM, "denied"))
        patches = self.fake_factory(sock)
        with patches[0], patches[1], patches[2]:
            with self.assertRaises(OSError):
                _socket.new_socket(source)

        self.assertTrue(sock.closed)
        self.assertNotIn("bind", [operation[0] for operation in sock.operations])


@contextmanager
def fake_network(addresses, *, connect_errors=None, times=None, dns_error=None):
    created = []
    connect_errors = connect_errors or {}

    def make_socket(family, socktype, proto):
        sock = RecordingSocket(family)
        sock.type = socktype
        sock.proto = proto
        created.append(sock)
        return sock

    def bind(sock, address):
        sock.bind(address)

    def connect(sock, address):
        sock.operations.append(("connect", address))
        if address in connect_errors:
            raise connect_errors[address]

    with ExitStack() as stack:
        stack.enter_context(
            patch.object(socket, "getaddrinfo", side_effect=dns_error, return_value=addresses)
        )
        stack.enter_context(patch.object(_socket, "_create_unpatched_socket", side_effect=make_socket))
        stack.enter_context(patch.object(_socket, "_ORIGINAL_BIND", side_effect=bind))
        stack.enter_context(patch.object(_socket, "_ORIGINAL_CONNECT", side_effect=connect))
        if times is not None:
            stack.enter_context(patch.object(_socket.time, "monotonic", side_effect=times))
        yield created


class CreateConnectionTests(unittest.TestCase):
    def info(self, family, host):
        sockaddr = (host, 443) if family == socket.AF_INET else (host, 443, 0, 0)
        return family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", sockaddr

    def test_options_binding_connect_order_and_timeout_restoration(self):
        info = self.info(socket.AF_INET, "198.51.100.1")
        source = _socket.Source("192.0.2.8", interface="eth0")
        options = ((socket.SOL_SOCKET, socket.SO_REUSEADDR, 1),)
        with fake_network([info], times=[0.0, 1.0, 2.0]) as sockets:
            result = _socket.create_connection(
                ("example.test", 443), source, timeout=10.0, socket_options=options
            )

        self.assertIs(result, sockets[0])
        self.assertEqual(
            sockets[0].calls,
            [
                options[0],
                (socket.SOL_SOCKET, getattr(socket, "SO_BINDTODEVICE", 25), b"eth0"),
                (socket.IPPROTO_IP, getattr(socket, "IP_FREEBIND", 15), 1),
            ],
        )
        self.assertEqual(
            [op[0] for op in sockets[0].operations if op[0] in ("bind", "connect")],
            ["bind", "connect"],
        )
        self.assertEqual(sockets[0].timeouts, [9.0, 8.0, 10.0])

    def test_matching_families_are_tried_without_unconfigured_fallback(self):
        ipv4 = self.info(socket.AF_INET, "198.51.100.1")
        ipv6 = self.info(socket.AF_INET6, "2001:db8::1")
        error = OSError(errno.ECONNREFUSED, "refused")
        with fake_network([ipv4, ipv6], connect_errors={ipv6[4]: error}) as sockets:
            with self.assertRaises(OSError) as raised:
                _socket.create_connection(("example.test", 443), _socket.Source("2001:db8::8"))

        self.assertIs(raised.exception, error)
        self.assertEqual([sock.family for sock in sockets], [socket.AF_INET6])
        self.assertTrue(sockets[0].closed)

    def test_strict_family_mismatch_fails_before_socket_creation(self):
        with fake_network([self.info(socket.AF_INET, "198.51.100.1")]) as sockets:
            with self.assertRaises(_socket.FamilyMismatchError):
                _socket.create_connection(("example.test", 443), _socket.Source("2001:db8::8"))
        self.assertEqual(sockets, [])

    def test_non_strict_family_miss_uses_wildcard_bind(self):
        info = self.info(socket.AF_INET6, "2001:db8::1")
        source = _socket.Source("192.0.2.8", strict=False, interface="eth0")
        with fake_network([info]) as sockets:
            result = _socket.create_connection(("example.test", 443), source)

        self.assertIs(result, sockets[0])
        self.assertEqual(sockets[0].local, ("::", 0, 0, 0))
        self.assertEqual(
            sockets[0].calls,
            [(socket.SOL_SOCKET, getattr(socket, "SO_BINDTODEVICE", 25), b"eth0")],
        )
        self.assertTrue(_socket._is_explicit_socket(sockets[0]))

    def test_timeout_budget_spans_candidates_and_failed_socket_closes(self):
        first = self.info(socket.AF_INET, "198.51.100.1")
        second = self.info(socket.AF_INET, "198.51.100.2")
        error = socket.timeout("candidate timed out")
        with fake_network(
            [first, second],
            connect_errors={first[4]: error},
            times=[0.0, 0.5, 1.0, 2.0, 2.5, 3.0],
        ) as sockets:
            result = _socket.create_connection(
                ("example.test", 443), _socket.Source("192.0.2.8"), timeout=10.0
            )

        self.assertIs(result, sockets[1])
        self.assertTrue(sockets[0].closed)
        self.assertEqual(sockets[1].timeouts, [7.5, 7.0, 10.0])

    def test_dns_failure_and_elapsed_dns_timeout_propagate(self):
        error = socket.gaierror(socket.EAI_AGAIN, "temporary failure")
        with fake_network([], dns_error=error) as sockets:
            with self.assertRaises(socket.gaierror) as raised:
                _socket.create_connection(("example.test", 443), _socket.Source("192.0.2.8"))
        self.assertIs(raised.exception, error)
        self.assertEqual(sockets, [])

        with fake_network(
            [self.info(socket.AF_INET, "198.51.100.1")], times=[0.0, 2.0]
        ) as sockets:
            with self.assertRaises(socket.timeout):
                _socket.create_connection(
                    ("example.test", 443), _socket.Source("192.0.2.8"), timeout=1.0
                )
        self.assertEqual(sockets, [])


if __name__ == "__main__":
    unittest.main()
