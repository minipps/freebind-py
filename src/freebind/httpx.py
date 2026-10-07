"""HTTPX transports that bind connections through Freebind."""

from __future__ import annotations

import socket
from typing import Any, Iterable

import anyio
import httpcore
from anyio.abc import SocketStream
from httpcore._backends.anyio import AnyIOStream as _AnyIOStream
from httpcore._backends.sync import SyncStream as _SyncStream
from httpx import AsyncHTTPTransport as _AsyncHTTPTransport
from httpx import HTTPTransport as _HTTPTransport

from ._socket import (
    _close_failed_socket,
    async_create_connection,
    create_connection,
)
from ._source import Source


def _install_network_backend(pool: Any, backend: Any, *, fresh: bool = False) -> None:
    """Replace HTTPcore's pool backend before its first request."""
    try:
        current = pool._network_backend
    except AttributeError as exc:
        raise RuntimeError("Unsupported HTTPcore connection pool interface") from exc
    if not callable(getattr(current, "connect_tcp", None)):
        raise RuntimeError("Unsupported HTTPcore network backend interface")
    pool._network_backend = backend
    if fresh:
        if not hasattr(pool, "_max_keepalive_connections"):
            raise RuntimeError("Unsupported HTTPcore keepalive limit interface")
        pool._max_keepalive_connections = 0


def _validate_fresh(kwargs: dict[str, Any]) -> None:
    if kwargs.get("http2", False):
        raise ValueError("fresh connections do not support HTTP/2")
    if not kwargs.get("http1", True):
        raise ValueError("fresh connections require HTTP/1.1")


class _SyncFreebindBackend(httpcore.SyncBackend):
    def __init__(self, source: Source) -> None:
        self._source = source

    def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[tuple] | None = None,
    ) -> _SyncStream:
        if local_address is not None:
            raise ValueError("local_address conflicts with the Freebind source")

        options = list(socket_options or ())
        options.append((socket.IPPROTO_TCP, socket.TCP_NODELAY, 1))
        try:
            sock = create_connection(
                (host, port),
                self._source,
                timeout=timeout,
                socket_options=options,
            )
        except socket.timeout as exc:
            raise httpcore.ConnectTimeout(str(exc)) from exc
        except OSError as exc:
            raise httpcore.ConnectError(str(exc)) from exc

        try:
            return _SyncStream(sock)
        except BaseException:
            _close_failed_socket(sock)
            raise


class FreebindTransport(_HTTPTransport):
    """HTTPX synchronous transport with source-bound TCP connections."""

    def __init__(self, source: Source, *, fresh: bool = False, **kwargs: Any) -> None:
        if not isinstance(source, Source):
            raise TypeError("source must be a Source")
        if not isinstance(fresh, bool):
            raise TypeError("fresh must be a bool")
        if kwargs.get("proxy") is not None:
            raise ValueError("FreebindTransport does not support proxies")
        if kwargs.get("uds") is not None:
            raise ValueError("uds conflicts with the Freebind TCP source")
        if kwargs.get("local_address") is not None:
            raise ValueError("local_address conflicts with the Freebind source")
        if fresh:
            _validate_fresh(kwargs)

        super().__init__(**kwargs)
        self._freebind_source = source
        self.fresh = fresh
        self._freebind_backend = _SyncFreebindBackend(source)
        _install_network_backend(self._pool, self._freebind_backend, fresh=fresh)


class _AsyncFreebindBackend(httpcore.AnyIOBackend):
    def __init__(self, source: Source) -> None:
        self._source = source

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[tuple] | None = None,
    ) -> _AnyIOStream:
        if local_address is not None:
            raise ValueError("local_address conflicts with the Freebind source")

        options = list(socket_options or ())
        options.append((socket.IPPROTO_TCP, socket.TCP_NODELAY, 1))
        try:
            sock = await async_create_connection(
                (host, port),
                self._source,
                timeout=timeout,
                socket_options=options,
            )
        except TimeoutError as exc:
            raise httpcore.ConnectTimeout(str(exc)) from exc
        except OSError as exc:
            raise httpcore.ConnectError(str(exc)) from exc

        try:
            stream = await SocketStream.from_socket(sock)
        except TimeoutError as exc:
            _close_failed_socket(sock)
            raise httpcore.ConnectTimeout(str(exc)) from exc
        except (OSError, anyio.BrokenResourceError) as exc:
            _close_failed_socket(sock)
            raise httpcore.ConnectError(str(exc)) from exc
        except BaseException:
            _close_failed_socket(sock)
            raise

        try:
            return _AnyIOStream(stream)
        except BaseException:
            with anyio.CancelScope(shield=True):
                try:
                    await stream.aclose()
                except BaseException:
                    pass
            raise


class AsyncFreebindTransport(_AsyncHTTPTransport):
    """HTTPX asynchronous transport for asyncio source-bound connections."""

    def __init__(self, source: Source, *, fresh: bool = False, **kwargs: Any) -> None:
        if not isinstance(source, Source):
            raise TypeError("source must be a Source")
        if not isinstance(fresh, bool):
            raise TypeError("fresh must be a bool")
        if kwargs.get("proxy") is not None:
            raise ValueError("AsyncFreebindTransport does not support proxies")
        if kwargs.get("uds") is not None:
            raise ValueError("uds conflicts with the Freebind TCP source")
        if kwargs.get("local_address") is not None:
            raise ValueError("local_address conflicts with the Freebind source")
        if fresh:
            _validate_fresh(kwargs)

        super().__init__(**kwargs)
        self._freebind_source = source
        self.fresh = fresh
        self._freebind_backend = _AsyncFreebindBackend(source)
        _install_network_backend(self._pool, self._freebind_backend, fresh=fresh)
