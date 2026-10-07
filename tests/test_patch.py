import errno
import socket
import unittest
from unittest import mock

from freebind import _patch
from freebind._source import Source


class PatchLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.socket_class = socket.socket
        self.original = {
            name: self.socket_class.__dict__.get(name)
            for name in _patch._PATCHED_METHODS
        }
        self.was_direct = {
            name: name in self.socket_class.__dict__
            for name in _patch._PATCHED_METHODS
        }

    def test_patch_keeps_socket_class_identity_and_aliases(self):
        alias = socket.socket
        handle = _patch.patch(Source("192.0.2.8"))
        try:
            self.assertIs(socket.socket, alias)
            self.assertIs(self.socket_class, alias)
            for name in _patch._PATCHED_METHODS:
                self.assertIsNot(getattr(alias, name), self.original[name])
        finally:
            handle.restore()

    def test_nested_patch_is_rejected_and_restore_is_idempotent(self):
        handle = _patch.patch(Source("192.0.2.8"))
        try:
            with self.assertRaises(RuntimeError):
                _patch.patch(Source("192.0.2.9"))
        finally:
            handle.restore()
        handle.restore()
        self.assertIsNone(_patch._ACTIVE_PATCH)
        self.assert_restored()

    def test_context_restores_after_exception(self):
        with self.assertRaisesRegex(ValueError, "original failure"):
            with _patch.patch(Source("192.0.2.8")):
                raise ValueError("original failure")
        self.assert_restored()

    def test_restore_refuses_to_replace_a_later_patch(self):
        handle = _patch.patch(Source("192.0.2.8"))
        replacement = lambda self, address: None
        self.socket_class.connect = replacement
        try:
            with self.assertRaisesRegex(RuntimeError, "refusing to restore"):
                handle.restore()
            self.assertIs(self.socket_class.connect, replacement)
        finally:
            self.socket_class.connect = handle._installed["connect"]
            handle.restore()
        self.assert_restored()

    def test_restore_preserves_an_inherited_method(self):
        name = "connect"
        had_direct = name in self.socket_class.__dict__
        previous = self.socket_class.__dict__.get(name)
        inherited = next(
            getattr(base, name)
            for base in self.socket_class.__mro__[1:]
            if hasattr(base, name)
        )
        if had_direct:
            delattr(self.socket_class, name)
        try:
            handle = _patch.patch(Source("192.0.2.8"))
            handle.restore()
            self.assertNotIn(name, self.socket_class.__dict__)
            self.assertIs(getattr(self.socket_class, name), inherited)
        finally:
            if _patch._ACTIVE_PATCH is not None:
                _patch._ACTIVE_PATCH.restore()
            if had_direct:
                setattr(self.socket_class, name, previous)

    def test_restore_preserves_an_existing_direct_override(self):
        name = "connect"
        had_direct = name in self.socket_class.__dict__
        previous = self.socket_class.__dict__.get(name)
        replacement = lambda self, address: None
        setattr(self.socket_class, name, replacement)
        try:
            handle = _patch.patch(Source("192.0.2.8"))
            handle.restore()
            self.assertIs(self.socket_class.__dict__[name], replacement)
        finally:
            if _patch._ACTIVE_PATCH is not None:
                _patch._ACTIVE_PATCH.restore()
            if had_direct:
                setattr(self.socket_class, name, previous)
            else:
                delattr(self.socket_class, name)

    def assert_restored(self):
        for name in _patch._PATCHED_METHODS:
            if self.was_direct[name]:
                self.assertIs(self.socket_class.__dict__[name], self.original[name])
            else:
                self.assertNotIn(name, self.socket_class.__dict__)


class _RecordingSocket:
    def __init__(self, family, type, *, local=None, peer=None):
        self.family = family
        self.type = type
        self.local = local or {
            socket.AF_INET: ("0.0.0.0", 0),
            socket.AF_INET6: ("::", 0, 0, 0),
        }.get(family, ("", 0))
        self.peer = peer

    def getpeername(self):
        if self.peer is None:
            raise OSError(errno.ENOTCONN, "not connected")
        return self.peer

    def getsockname(self):
        return self.local


class OutgoingPatchTests(unittest.TestCase):
    def setUp(self):
        from freebind import _socket

        self.socket_helpers = _socket
        self.source = Source("192.0.2.8")

    def mark_bound(self, sock, source, port=0):
        self.assertIs(source, self.source)
        sock.local = ("192.0.2.8", 49152)
        self.socket_helpers._mark_explicit_socket(sock)

    def test_constructor_alias_connect_binds_before_connect(self):
        events = []
        target = _RecordingSocket(socket.AF_INET, socket.SOCK_STREAM)

        def bind(sock, source, port=0):
            events.append(("bind", source))
            self.mark_bound(sock, source, port)

        def connect(sock, address):
            events.append(("connect", address))
            return "connected"

        alias = socket.socket
        with mock.patch.dict(_patch._PATCHED_METHODS, {"connect": connect}):
            with mock.patch.object(_patch._socket, "bind_socket", side_effect=bind):
                with _patch.patch(self.source):
                    result = alias.connect(target, ("198.51.100.9", 443))

        self.assertEqual(result, "connected")
        self.assertEqual(events, [("bind", self.source), ("connect", ("198.51.100.9", 443))])

    def test_explicit_or_already_bound_sockets_are_preserved(self):
        connected = _RecordingSocket(
            socket.AF_INET,
            socket.SOCK_STREAM,
            peer=("198.51.100.9", 443),
        )
        explicit = _RecordingSocket(socket.AF_INET, socket.SOCK_STREAM)
        self.socket_helpers._mark_explicit_socket(explicit)
        address_only = _RecordingSocket(
            socket.AF_INET,
            socket.SOCK_STREAM,
            local=("192.0.2.4", 0),
        )
        targets = (connected, explicit, address_only)
        originals = {"connect": lambda sock, address: None}
        with mock.patch.dict(_patch._PATCHED_METHODS, originals):
            with mock.patch.object(_patch._socket, "bind_socket") as bind:
                with _patch.patch(self.source):
                    for target in targets:
                        socket.socket.connect(target, ("198.51.100.9", 443))
        bind.assert_not_called()

    def test_connect_ex_returns_setup_errno_and_keeps_nonblocking_result(self):
        failed = _RecordingSocket(socket.AF_INET, socket.SOCK_STREAM)
        with mock.patch.dict(_patch._PATCHED_METHODS, {"connect_ex": lambda *a: 0}):
            with mock.patch.object(
                _patch._socket,
                "bind_socket",
                side_effect=OSError(errno.EPERM, "denied"),
            ) as bind:
                with _patch.patch(self.source):
                    result = socket.socket.connect_ex(failed, ("198.51.100.9", 443))
        self.assertEqual(result, errno.EPERM)
        bind.assert_called_once_with(failed, self.source)

        pending = _RecordingSocket(socket.AF_INET, socket.SOCK_STREAM)
        with mock.patch.dict(
            _patch._PATCHED_METHODS,
            {"connect_ex": lambda *args: errno.EINPROGRESS},
        ):
            with mock.patch.object(
                _patch._socket, "bind_socket", side_effect=self.mark_bound
            ):
                with _patch.patch(self.source):
                    result = socket.socket.connect_ex(pending, ("198.51.100.9", 443))
        self.assertEqual(result, errno.EINPROGRESS)

    def test_udp_sends_bind_once_across_destinations_and_methods(self):
        events = []
        target = _RecordingSocket(socket.AF_INET, socket.SOCK_DGRAM)

        def bind(sock, source, port=0):
            events.append("bind")
            self.mark_bound(sock, source, port)

        originals = {
            "sendto": lambda sock, *args, **kwargs: events.append("sendto"),
        }
        if "sendmsg" in _patch._PATCHED_METHODS:
            originals["sendmsg"] = lambda sock, *args, **kwargs: events.append("sendmsg")
        with mock.patch.dict(_patch._PATCHED_METHODS, originals):
            with mock.patch.object(_patch._socket, "bind_socket", side_effect=bind):
                with _patch.patch(self.source):
                    socket.socket.sendto(target, b"one", ("198.51.100.1", 53))
                    socket.socket.sendto(target, b"two", ("198.51.100.2", 53))
                    if "sendmsg" in originals:
                        socket.socket.sendmsg(target, [b"three"], (), 0, ("198.51.100.3", 53))
        self.assertEqual(events, ["bind", "sendto", "sendto"] + (["sendmsg"] if "sendmsg" in originals else []))

    def test_unix_and_raw_sockets_are_not_prepared(self):
        events = []
        targets = (
            _RecordingSocket(socket.AF_UNIX, socket.SOCK_STREAM),
            _RecordingSocket(socket.AF_INET, socket.SOCK_RAW),
        )
        with mock.patch.dict(
            _patch._PATCHED_METHODS,
            {"connect": lambda sock, address: events.append(sock)},
        ):
            with mock.patch.object(_patch._socket, "bind_socket") as bind:
                with _patch.patch(self.source):
                    for target in targets:
                        socket.socket.connect(target, ("198.51.100.9", 443))
        bind.assert_not_called()
        self.assertEqual(events, list(targets))

    def test_ssl_super_connect_uses_the_socket_class_hook(self):
        import ssl
        from types import SimpleNamespace
        from unittest.mock import Mock

        class TestSSLSocket(ssl.SSLSocket):
            @property
            def context(self):
                return self._test_context

            @context.setter
            def context(self, value):
                self._test_context = value

        target = TestSSLSocket.__new__(TestSSLSocket)
        target.server_side = False
        target._connected = False
        target._sslobj = None
        context = SimpleNamespace(_wrap_socket=Mock(return_value=object()))
        target.context = context
        target.server_hostname = "example.test"
        target._session = None
        target.do_handshake_on_connect = False
        connect = Mock(return_value=None)
        with mock.patch.dict(_patch._PATCHED_METHODS, {"connect": connect}):
            with mock.patch.object(_patch, "_prepare") as prepare:
                with _patch.patch(self.source) as handle:
                    ssl.SSLSocket.connect(target, ("198.51.100.9", 443))
        prepare.assert_called_once_with(target, handle, udp_only=False)
        connect.assert_called_once_with(target, ("198.51.100.9", 443))
        self.assertTrue(target._connected)


if __name__ == "__main__":
    unittest.main()
