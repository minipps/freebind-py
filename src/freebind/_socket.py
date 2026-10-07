"""Low-level socket helpers."""

import errno
import os
import socket
import sys
import time
import weakref

from ._source import FamilyMismatchError, Source


# Keep descriptors captured before the process-wide patch can replace them.
_ORIGINAL_SOCKET = socket.socket
_ORIGINAL_NEW = socket.socket.__new__
_ORIGINAL_INIT = socket.socket.__init__
_ORIGINAL_BIND = socket.socket.bind
_ORIGINAL_CONNECT = socket.socket.connect
_ORIGINAL_CONNECT_EX = socket.socket.connect_ex
_ORIGINAL_SENDTO = socket.socket.sendto
_ORIGINAL_SENDMSG = getattr(socket.socket, "sendmsg", None)

_IP_FREEBIND = getattr(socket, "IP_FREEBIND", 15)
_IPV6_FREEBIND = getattr(socket, "IPV6_FREEBIND", 78)
_UNSUPPORTED_OPTION_ERRNOS = {
    errno.ENOPROTOOPT,
    errno.EOPNOTSUPP,
    errno.ENOTSUP,
}
_EXPLICIT_SOCKETS: weakref.WeakSet[socket.socket] = weakref.WeakSet()


def enable_freebind(sock: socket.socket) -> None:
    """Allow ``sock`` to bind to a nonlocal IPv4 or IPv6 source address."""
    if not sys.platform.startswith("linux"):
        raise NotImplementedError("Freebind is only supported on Linux")

    if sock.family == socket.AF_INET:
        sock.setsockopt(socket.IPPROTO_IP, _IP_FREEBIND, 1)
    elif sock.family == socket.AF_INET6:
        try:
            sock.setsockopt(socket.IPPROTO_IPV6, _IPV6_FREEBIND, 1)
        except OSError as exc:
            if exc.errno not in _UNSUPPORTED_OPTION_ERRNOS:
                raise
            sock.setsockopt(socket.IPPROTO_IP, _IP_FREEBIND, 1)
    else:
        raise OSError(errno.EAFNOSUPPORT, "Freebind requires an IPv4 or IPv6 socket")


def _validate_port(port: int) -> None:
    if not isinstance(port, int) or isinstance(port, bool):
        raise TypeError("port must be an integer")
    if not 0 <= port <= 65535:
        raise ValueError("port must be between 0 and 65535")


def _validate_interface(interface: str | None) -> bytes | None:
    if interface is None:
        return None
    if not isinstance(interface, str):
        raise TypeError("interface must be a string or None")
    if not interface or "\0" in interface:
        raise ValueError("interface must be nonempty and contain no NUL")
    encoded = os.fsencode(interface)
    if len(encoded) >= getattr(socket, "IFNAMSIZ", 16):
        raise ValueError("interface name is too long")
    if not sys.platform.startswith("linux"):
        raise NotImplementedError("interface binding is only supported on Linux")
    return encoded


def _is_unbound(sock: socket.socket) -> bool:
    try:
        sock.getpeername()
    except OSError as exc:
        if exc.errno not in (errno.ENOTCONN, errno.EINVAL):
            raise
    else:
        return False

    address = sock.getsockname()
    if sock.family == socket.AF_INET:
        return address[0] == "0.0.0.0" and address[1] == 0
    return (
        address[0] == "::"
        and address[1] == 0
        and (len(address) < 3 or address[2] == 0)
        and (len(address) < 4 or address[3] == 0)
    )


def _mark_explicit_socket(sock: socket.socket) -> None:
    """Mark sockets configured through this module for patch bypass."""
    _EXPLICIT_SOCKETS.add(sock)


def _is_explicit_socket(sock: socket.socket) -> bool:
    """Return whether ``sock`` was configured through this module."""
    return sock in _EXPLICIT_SOCKETS


def _close_failed_socket(sock: socket.socket) -> None:
    try:
        sock.close()
    except OSError:
        pass


def bind_socket(sock: socket.socket, source: Source, *, port: int = 0) -> str | None:
    """Bind an unbound caller-owned socket using ``source``."""
    if not sys.platform.startswith("linux"):
        raise NotImplementedError("Freebind is only supported on Linux")
    if not isinstance(source, Source):
        raise TypeError("source must be a Source")
    _validate_port(port)
    if sock.family not in (socket.AF_INET, socket.AF_INET6):
        raise OSError(errno.EAFNOSUPPORT, "Freebind requires an IPv4 or IPv6 socket")
    interface = _validate_interface(source.interface)
    if not _is_unbound(sock):
        raise OSError(errno.EINVAL, "socket is already bound")

    selected = source.select(sock.family)
    if interface is not None:
        sock.setsockopt(
            socket.SOL_SOCKET,
            getattr(socket, "SO_BINDTODEVICE", 25),
            interface,
        )
    if selected is None:
        address = ("0.0.0.0", port) if sock.family == socket.AF_INET else ("::", port, 0, 0)
        _ORIGINAL_BIND(sock, address)
        _mark_explicit_socket(sock)
        return None

    enable_freebind(sock)
    address = (selected, port) if sock.family == socket.AF_INET else (selected, port, 0, 0)
    _ORIGINAL_BIND(sock, address)
    _mark_explicit_socket(sock)
    return selected


def _socket_type(type: int) -> int:
    if not isinstance(type, int) or isinstance(type, bool):
        raise TypeError("type must be an integer")
    if type < 0:
        raise ValueError("type must be nonnegative")
    flags = getattr(socket, "SOCK_NONBLOCK", 0) | getattr(socket, "SOCK_CLOEXEC", 0)
    base_type = type & ~flags
    if base_type not in (socket.SOCK_STREAM, socket.SOCK_DGRAM):
        raise ValueError("type must be SOCK_STREAM or SOCK_DGRAM")
    return type


def _create_unpatched_socket(family: int, type: int, proto: int = 0) -> socket.socket:
    """Construct a socket without invoking a patched ``socket.__init__``."""
    sock = _ORIGINAL_NEW(_ORIGINAL_SOCKET)
    try:
        _ORIGINAL_INIT(sock, family, type, proto)
    except BaseException:
        try:
            sock.close()
        except BaseException:
            pass
        raise
    return sock


def _matching_addrinfo(addresses: list[tuple], source: Source) -> list[tuple]:
    matching = [info for info in addresses if info[0] in source.families]
    if matching:
        return matching
    if source.strict:
        raise FamilyMismatchError()
    return list(addresses)


def _remaining_timeout(deadline: float | None, timeout: float | None) -> float | None:
    if deadline is None:
        return timeout
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise socket.timeout("timed out")
    return remaining


def create_connection(
    address: tuple,
    source: Source,
    *,
    timeout: float | None = None,
    socket_options: tuple = (),
) -> socket.socket:
    """Resolve and connect a TCP socket using ``source``."""
    if not sys.platform.startswith("linux"):
        raise NotImplementedError("Freebind is only supported on Linux")
    if not isinstance(source, Source):
        raise TypeError("source must be a Source")
    host, port = address[:2]
    options = tuple(socket_options)
    if timeout is not None and timeout < 0:
        raise ValueError("timeout must be nonnegative")
    deadline = time.monotonic() + timeout if timeout is not None and timeout > 0 else None

    addresses = socket.getaddrinfo(
        host,
        port,
        family=socket.AF_UNSPEC,
        type=socket.SOCK_STREAM,
    )
    if not addresses:
        raise socket.gaierror(socket.EAI_NONAME, "No addresses found")
    candidates = _matching_addrinfo(addresses, source)
    last_error = None
    # ponytail: sequential candidates can add tail latency; add Happy Eyeballs if that matters.
    for family, socktype, proto, _, sockaddr in candidates:
        remaining = _remaining_timeout(deadline, timeout)
        sock = None
        try:
            sock = _create_unpatched_socket(family, socktype, proto)
            sock.settimeout(remaining)
            for option in options:
                sock.setsockopt(*option)
            bind_socket(sock, source)
            sock.settimeout(_remaining_timeout(deadline, timeout))
            _ORIGINAL_CONNECT(sock, sockaddr)
            sock.settimeout(timeout)
            return sock
        except OSError as exc:
            if sock is not None:
                _close_failed_socket(sock)
            last_error = exc
            _remaining_timeout(deadline, timeout)
        except BaseException:
            if sock is not None:
                _close_failed_socket(sock)
            raise

    if last_error is not None:
        raise last_error
    raise socket.gaierror(socket.EAI_NONAME, "No usable addresses found")


def new_socket(
    source: Source,
    *,
    family: int | None = None,
    type: int = socket.SOCK_STREAM,
    port: int = 0,
) -> socket.socket:
    """Create an unconnected TCP or UDP socket and apply ``source``."""
    if not sys.platform.startswith("linux"):
        raise NotImplementedError("Freebind is only supported on Linux")
    if not isinstance(source, Source):
        raise TypeError("source must be a Source")
    _validate_port(port)
    socket_type = _socket_type(type)
    if family is None:
        if len(source.families) != 1:
            raise ValueError("family is required for a source with multiple families")
        family = next(iter(source.families))
    if family not in (socket.AF_INET, socket.AF_INET6):
        raise OSError(errno.EAFNOSUPPORT, "family must be AF_INET or AF_INET6")

    sock = _create_unpatched_socket(family, socket_type)
    try:
        bind_socket(sock, source, port=port)
    except BaseException:
        try:
            sock.close()
        except BaseException:
            pass
        raise
    return sock
