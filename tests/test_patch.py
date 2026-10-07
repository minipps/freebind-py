import errno
import os
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
                if name == "__init__":
                    continue
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
        self.closed = False

    def getpeername(self):
        if self.peer is None:
            raise OSError(errno.ENOTCONN, "not connected")
        return self.peer

    def getsockname(self):
        return self.local

    def close(self):
        self.closed = True


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


class SocketEntrypointTests(unittest.TestCase):
    def setUp(self):
        self.source = Source("192.0.2.8")

    def initialize(self, events):
        def init(sock, family, type, proto, fileno=None):
            events.append(("init", fileno))
            if fileno is None:
                sock.family = family
                sock.type = type
                sock.local = {
                    socket.AF_INET: ("0.0.0.0", 0),
                    socket.AF_INET6: ("::", 0, 0, 0),
                }.get(family, ("", 0))

        return init

    def test_new_socket_binds_during_construction_and_later_bind_conflicts(self):
        events = []
        target = _RecordingSocket(socket.AF_INET, socket.SOCK_DGRAM)
        socket_type = socket.SOCK_DGRAM | getattr(socket, "SOCK_NONBLOCK", 0)

        def bind(sock, source, port=0):
            events.append(("bind", source))
            sock.local = ("192.0.2.8", 49152)
            _patch._socket._mark_explicit_socket(sock)

        with mock.patch.dict(
            _patch._PATCHED_METHODS,
            {"__init__": self.initialize(events)},
        ):
            with mock.patch.object(_patch._socket, "bind_socket", side_effect=bind):
                with _patch.patch(
                    self.source,
                    entrypoint="socket",
                    socket_types=(socket_type,),
                ):
                    socket.socket.__init__(target, socket.AF_INET, socket_type, 0)

        self.assertEqual(events, [("init", None), ("bind", self.source)])
        self.assertFalse(_patch._socket._is_unbound(target))
        with self.assertRaises(OSError) as raised:
            _patch._socket.bind_socket(target, Source("192.0.2.9"))
        self.assertEqual(raised.exception.errno, errno.EINVAL)

    def test_type_filter_uses_actual_type_and_creation_flags(self):
        target = _RecordingSocket(socket.AF_INET, socket.SOCK_DGRAM)
        socket_type = socket.SOCK_DGRAM | getattr(socket, "SOCK_CLOEXEC", 0)
        initialize = self.initialize([])
        with mock.patch.dict(_patch._PATCHED_METHODS, {"__init__": initialize}):
            with mock.patch.object(_patch._socket, "bind_socket") as bind:
                with _patch.patch(
                    self.source,
                    entrypoint="socket",
                    socket_types=(socket.SOCK_STREAM,),
                ):
                    socket.socket.__init__(target, socket.AF_INET, socket_type, 0)
        bind.assert_not_called()

    def test_setup_failure_closes_fresh_socket(self):
        target = _RecordingSocket(socket.AF_INET, socket.SOCK_STREAM)
        error = OSError(errno.EPERM, "denied")
        with mock.patch.dict(
            _patch._PATCHED_METHODS,
            {"__init__": self.initialize([])},
        ):
            with mock.patch.object(
                _patch._socket, "bind_socket", side_effect=error
            ):
                with _patch.patch(self.source, entrypoint="socket"):
                    with self.assertRaises(OSError) as raised:
                        socket.socket.__init__(target, socket.AF_INET, socket.SOCK_STREAM, 0)
        self.assertIs(raised.exception, error)
        self.assertTrue(target.closed)

    def test_adopted_accepted_and_duplicated_descriptors_are_skipped(self):
        events = []
        timeouts = []

        class Listener:
            family = socket.AF_INET
            type = socket.SOCK_STREAM
            proto = 0

            def _accept(self):
                return 77, ("198.51.100.4", 40000)

            def gettimeout(self):
                return None

        class DuplicateSocket(socket.socket):
            @property
            def family(self):
                return socket.AF_INET

            @property
            def type(self):
                return socket.SOCK_STREAM

            @property
            def proto(self):
                return 0

            def fileno(self):
                return 3

            def gettimeout(self):
                return 0.25

            def settimeout(self, timeout):
                timeouts.append(timeout)

        duplicate_source = DuplicateSocket.__new__(DuplicateSocket)
        with mock.patch.dict(
            _patch._PATCHED_METHODS,
            {"__init__": self.initialize(events)},
        ):
            with mock.patch.object(socket, "dup", return_value=88):
                with mock.patch.object(_patch._socket, "bind_socket") as bind:
                    with _patch.patch(self.source, entrypoint="socket"):
                        accepted, _ = socket.socket.accept(Listener())
                        duplicate = _patch._socket._ORIGINAL_SOCKET.dup(duplicate_source)
                        adopted = _RecordingSocket(socket.AF_INET, socket.SOCK_STREAM)
                        socket.socket.__init__(
                            adopted, socket.AF_INET, socket.SOCK_STREAM, 0, 99
                        )
                        socket.socket.__init__(
                            adopted,
                            socket.AF_INET,
                            socket.SOCK_STREAM,
                            0,
                            fileno=100,
                        )
        bind.assert_not_called()
        self.assertEqual(events, [("init", 77), ("init", 88), ("init", 99), ("init", 100)])
        self.assertEqual(timeouts, [0.25])
        self.assertIsInstance(accepted, socket.socket)
        self.assertIsInstance(duplicate, DuplicateSocket)


class PatchEnvironmentTests(unittest.TestCase):
    def test_defaults_and_environment_values_are_applied_only_when_called(self):
        with mock.patch.dict(os.environ, {"FREEBIND_RANDOM": "192.0.2.8"}, clear=True):
            original = os.environ.copy()
            handle = _patch.patch_from_env()
            try:
                self.assertEqual(handle.entrypoint, "socket")
                self.assertEqual(
                    handle.socket_types,
                    frozenset((socket.SOCK_STREAM, socket.SOCK_DGRAM)),
                )
                self.assertEqual(handle.source.mode, "random")
                self.assertIsNone(handle.source.bits)
                self.assertTrue(handle.source.strict)
                self.assertIsNone(handle.source.interface)
                self.assertEqual(os.environ, original)
            finally:
                handle.restore()

        environment = {
            "FREEBIND_RANDOM": " 192.0.2.8, 2001:db8::8\n192.0.2.9 ",
            "FREEBIND_TYPE_FILTER": "dGrAm",
            "FREEBIND_ENTRYPOINT": "CoNnEcT",
            "FREEBIND_IFACE": "lo",
        }
        with mock.patch.dict(os.environ, environment, clear=True):
            original = os.environ.copy()
            handle = _patch.patch_from_env()
            try:
                self.assertEqual(handle.entrypoint, "connect")
                self.assertEqual(handle.socket_types, frozenset((socket.SOCK_DGRAM,)))
                self.assertEqual(
                    handle.source.families,
                    frozenset((socket.AF_INET, socket.AF_INET6)),
                )
                self.assertEqual(handle.source.interface, "lo")
                self.assertEqual(os.environ, original)
            finally:
                handle.restore()

    def test_missing_or_empty_prefixes_fail_without_installing(self):
        configurations = ({}, {"FREEBIND_RANDOM": ""}, {"FREEBIND_RANDOM": ", \t "})
        for environment in configurations:
            with self.subTest(environment=environment):
                with mock.patch.dict(os.environ, environment, clear=True):
                    with self.assertRaises(ValueError):
                        _patch.patch_from_env()
                self.assertIsNone(_patch._ACTIVE_PATCH)

    def test_malformed_prefix_tokens_fail_without_installing(self):
        for prefixes in ("192.0.2.8,not-a-prefix", "192.0.2.8/99"):
            with self.subTest(prefixes=prefixes):
                with mock.patch.dict(
                    os.environ,
                    {"FREEBIND_RANDOM": prefixes},
                    clear=True,
                ):
                    with self.assertRaises(ValueError):
                        _patch.patch_from_env()
                self.assertIsNone(_patch._ACTIVE_PATCH)

    def test_invalid_filter_entrypoint_and_interface_fail_before_install(self):
        invalid = (
            ({"FREEBIND_TYPE_FILTER": ""}, "FREEBIND_TYPE_FILTER"),
            ({"FREEBIND_TYPE_FILTER": "STREAM,DGRAM"}, "FREEBIND_TYPE_FILTER"),
            ({"FREEBIND_ENTRYPOINT": "listen"}, "FREEBIND_ENTRYPOINT"),
            ({"FREEBIND_ENTRYPOINT": ""}, "FREEBIND_ENTRYPOINT"),
            ({"FREEBIND_IFACE": ""}, "FREEBIND_IFACE"),
            ({"FREEBIND_IFACE": "x" * getattr(socket, "IFNAMSIZ", 16)}, "FREEBIND_IFACE"),
        )
        for values, name in invalid:
            with self.subTest(values=values):
                environment = {"FREEBIND_RANDOM": "192.0.2.8", **values}
                with mock.patch.dict(os.environ, environment, clear=True):
                    with self.assertRaises((TypeError, ValueError)):
                        _patch.patch_from_env()
                self.assertIsNone(_patch._ACTIVE_PATCH)


if __name__ == "__main__":
    unittest.main()
