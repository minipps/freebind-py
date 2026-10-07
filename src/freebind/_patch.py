"""Opt-in process-wide socket patching."""

from __future__ import annotations

import functools
import socket
import threading
from typing import Callable

from . import _socket
from ._source import Source


_PATCHED_METHODS = {
    "connect": _socket._ORIGINAL_CONNECT,
    "connect_ex": _socket._ORIGINAL_CONNECT_EX,
}
_ACTIVE_PATCH: PatchHandle | None = None
_PATCH_LOCK = threading.RLock()
_MISSING = object()


def _forward(original: Callable) -> Callable:
    @functools.wraps(original)
    def method(sock, *args, **kwargs):
        return original(sock, *args, **kwargs)

    return method


def _restore_attribute(cls: type, name: str, previous: object) -> None:
    if previous is _MISSING:
        delattr(cls, name)
    else:
        setattr(cls, name, previous)


def _socket_types(values) -> frozenset[int]:
    try:
        values = tuple(values)
    except TypeError as exc:
        raise TypeError("socket_types must be an iterable of socket types") from exc
    if not values:
        raise ValueError("socket_types must not be empty")
    flags = getattr(socket, "SOCK_NONBLOCK", 0) | getattr(socket, "SOCK_CLOEXEC", 0)
    return frozenset(_socket._socket_type(value) & ~flags for value in values)


class PatchHandle:
    """Own an active patch on the existing ``socket.socket`` class."""

    def __init__(self, source: Source, *, entrypoint: str, socket_types):
        self.source = source
        self.entrypoint = entrypoint
        self.socket_types = _socket_types(socket_types)
        self._socket_class = _socket._ORIGINAL_SOCKET
        self._previous: dict[str, object] = {}
        self._installed: dict[str, Callable] = {}
        self._restored = False

    def restore(self) -> None:
        """Restore the class if its patched methods still belong to this handle."""
        global _ACTIVE_PATCH
        with _PATCH_LOCK:
            if self._restored:
                return
            if _ACTIVE_PATCH is not self:
                raise RuntimeError("this Freebind patch is no longer active")
            changed = [
                name
                for name, method in self._installed.items()
                if getattr(self._socket_class, name, _MISSING) is not method
            ]
            if changed:
                methods = ", ".join(f"socket.socket.{name}" for name in changed)
                raise RuntimeError(f"{methods} changed while patched; refusing to restore")
            for name, previous in self._previous.items():
                _restore_attribute(self._socket_class, name, previous)
            self._restored = True
            _ACTIVE_PATCH = None

    def __enter__(self) -> PatchHandle:
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> bool:
        self.restore()
        return False


def patch(
    source: Source,
    *,
    entrypoint: str = "connect",
    socket_types=(socket.SOCK_STREAM, socket.SOCK_DGRAM),
) -> PatchHandle:
    """Install Freebind hooks on ``socket.socket`` until the handle is restored."""
    global _ACTIVE_PATCH
    if not isinstance(source, Source):
        raise TypeError("source must be a Source")
    if entrypoint not in ("connect", "socket"):
        raise ValueError("entrypoint must be 'connect' or 'socket'")

    handle = PatchHandle(source, entrypoint=entrypoint, socket_types=socket_types)
    with _PATCH_LOCK:
        if _ACTIVE_PATCH is not None:
            raise RuntimeError("a Freebind socket patch is already active")
        cls = handle._socket_class
        handle._previous = {
            name: cls.__dict__.get(name, _MISSING) for name in _PATCHED_METHODS
        }
        handle._installed = {
            name: _forward(original) for name, original in _PATCHED_METHODS.items()
        }
        installed = []
        try:
            for name, method in handle._installed.items():
                setattr(cls, name, method)
                installed.append(name)
        except BaseException:
            for name in reversed(installed):
                _restore_attribute(cls, name, handle._previous[name])
            raise
        _ACTIVE_PATCH = handle
    return handle
