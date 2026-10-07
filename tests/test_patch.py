import socket
import unittest

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


if __name__ == "__main__":
    unittest.main()
