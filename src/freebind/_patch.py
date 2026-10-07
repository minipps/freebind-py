"""Opt-in process-wide socket patching."""

from __future__ import annotations

import errno
import functools
import os
import socket
import threading
from typing import Callable

from . import _socket
from ._source import Source


_PATCHED_METHODS = {
    "__init__": _socket._ORIGINAL_INIT,
    "connect": _socket._ORIGINAL_CONNECT,
    "connect_ex": _socket._ORIGINAL_CONNECT_EX,
    "sendto": _socket._ORIGINAL_SENDTO,
}
if _socket._ORIGINAL_SENDMSG is not None:
    _PATCHED_METHODS["sendmsg"] = _socket._ORIGINAL_SENDMSG
_ACTIVE_PATCH: PatchHandle | None = None
_PATCH_LOCK = threading.RLock()
_MISSING = object()


def _prepare(
    sock: socket.socket,
    handle: PatchHandle,
    *,
    udp_only: bool = False,
    construction: bool = False,
) -> None:
    entrypoint = "socket" if construction else "connect"
    with _PATCH_LOCK:
        if _ACTIVE_PATCH is not handle or handle._restored or handle.entrypoint != entrypoint:
            return
    if sock.family not in (socket.AF_INET, socket.AF_INET6):
        return
    try:
        socket_type = _socket._socket_type(sock.type)
    except (TypeError, ValueError):
        return
    flags = getattr(socket, "SOCK_NONBLOCK", 0) | getattr(socket, "SOCK_CLOEXEC", 0)
    socket_type &= ~flags
    if socket_type not in handle.socket_types or (udp_only and socket_type != socket.SOCK_DGRAM):
        return
    if _socket._is_explicit_socket(sock) or not _socket._is_unbound(sock):
        return
    _socket.bind_socket(sock, handle.source)


def _hook_init(original: Callable, handle: PatchHandle) -> Callable:
    @functools.wraps(original)
    def method(sock, *args, **kwargs):
        fileno = kwargs.get("fileno", args[3] if len(args) > 3 else None)
        result = original(sock, *args, **kwargs)
        if fileno is None and handle.entrypoint == "socket":
            try:
                _prepare(sock, handle, construction=True)
            except BaseException:
                _socket._close_failed_socket(sock)
                raise
        return result

    return method


def _hook(name: str, original: Callable, handle: PatchHandle) -> Callable:
    if name == "__init__":
        return _hook_init(original, handle)

    @functools.wraps(original)
    def method(sock, *args, **kwargs):
        try:
            _prepare(sock, handle, udp_only=name in ("sendto", "sendmsg"))
        except OSError as exc:
            if name == "connect_ex":
                return exc.errno or errno.EIO
            raise
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
    """Install hooks on ``socket.socket`` until the handle is restored.

    Socket timing binds during construction, so a later explicit bind can fail.
    """
    global _ACTIVE_PATCH
    if not isinstance(source, Source):
        raise TypeError("source must be a Source")
    if entrypoint not in ("connect", "socket"):
        raise ValueError("entrypoint must be 'connect' or 'socket'")
    _socket._validate_interface(source.interface)

    handle = PatchHandle(source, entrypoint=entrypoint, socket_types=socket_types)
    with _PATCH_LOCK:
        if _ACTIVE_PATCH is not None:
            raise RuntimeError("a Freebind socket patch is already active")
        cls = handle._socket_class
        methods = (
            {"__init__": _PATCHED_METHODS["__init__"]}
            if entrypoint == "socket"
            else {
                name: original
                for name, original in _PATCHED_METHODS.items()
                if name != "__init__"
            }
        )
        handle._previous = {
            name: cls.__dict__.get(name, _MISSING) for name in methods
        }
        handle._installed = {
            name: _hook(name, original, handle)
            for name, original in methods.items()
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


def patch_from_env() -> PatchHandle:
    """Install a patch using the documented ``FREEBIND_*`` variables."""
    raw_prefixes = os.environ.get("FREEBIND_RANDOM")
    if raw_prefixes is None:
        raise ValueError("FREEBIND_RANDOM must contain at least one prefix")
    prefixes = raw_prefixes.replace(",", " ").split()
    if not prefixes:
        raise ValueError("FREEBIND_RANDOM must contain at least one prefix")

    raw_filter = os.environ.get("FREEBIND_TYPE_FILTER")
    if raw_filter is None:
        socket_types = (socket.SOCK_STREAM, socket.SOCK_DGRAM)
    else:
        socket_types = {
            "STREAM": (socket.SOCK_STREAM,),
            "DGRAM": (socket.SOCK_DGRAM,),
        }.get(raw_filter.strip().upper())
        if socket_types is None:
            raise ValueError("FREEBIND_TYPE_FILTER must be STREAM or DGRAM")

    raw_entrypoint = os.environ.get("FREEBIND_ENTRYPOINT")
    entrypoint = "socket" if raw_entrypoint is None else raw_entrypoint.strip().lower()
    if entrypoint not in ("socket", "connect"):
        raise ValueError("FREEBIND_ENTRYPOINT must be socket or connect")

    interface = os.environ.get("FREEBIND_IFACE")
    source = Source(prefixes, interface=interface)
    return patch(source, entrypoint=entrypoint, socket_types=socket_types)
