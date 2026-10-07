import errno
import socket
import unittest
from unittest.mock import patch

from freebind import _socket


class RecordingSocket:
    def __init__(self, family, error=None):
        self.family = family
        self.error = error
        self.calls = []

    def setsockopt(self, level, option, value):
        self.calls.append((level, option, value))
        if self.error:
            error, self.error = self.error, None
            raise error


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
        self.assertTrue(callable(_socket._ORIGINAL_INIT))
        self.assertTrue(callable(_socket._ORIGINAL_BIND))
        self.assertTrue(callable(_socket._ORIGINAL_CONNECT))
        self.assertTrue(callable(_socket._ORIGINAL_CONNECT_EX))
        self.assertTrue(callable(_socket._ORIGINAL_SENDTO))
        self.assertTrue(callable(_socket._ORIGINAL_SENDMSG))


if __name__ == "__main__":
    unittest.main()
