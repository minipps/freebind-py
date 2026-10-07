"""Low-level socket helpers."""

import errno
import socket
import sys


# Keep descriptors captured before the process-wide patch can replace them.
_ORIGINAL_SOCKET = socket.socket
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
