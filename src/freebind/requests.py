"""Requests adapter that binds each new connection through Freebind."""

import socket
import sys
from typing import Any

from requests.adapters import HTTPAdapter
from urllib3.connection import HTTPConnection, HTTPSConnection
from urllib3.connectionpool import HTTPConnectionPool, HTTPSConnectionPool
from urllib3.exceptions import ConnectTimeoutError, NameResolutionError, NewConnectionError
from urllib3.poolmanager import PoolManager

from ._socket import create_connection
from ._source import Source


_CONFLICTING_OPTIONS = {"source_address", "socket_factory"}


def _reject_conflicts(options: dict[str, Any]) -> None:
    conflicts = _CONFLICTING_OPTIONS.intersection(options)
    if conflicts:
        names = ", ".join(sorted(conflicts))
        raise TypeError(f"FreebindAdapter does not support conflicting options: {names}")


class _FreebindConnection:
    def __init__(self, *args: Any, source: Source, **kwargs: Any) -> None:
        self._freebind_source = source
        super().__init__(*args, **kwargs)

    def _new_conn(self) -> socket.socket:
        try:
            sock = create_connection(
                (self._dns_host, self.port),
                self._freebind_source,
                timeout=self.timeout,
                socket_options=self.socket_options,
            )
        except socket.gaierror as exc:
            raise NameResolutionError(self.host, self, exc) from exc
        except socket.timeout as exc:
            raise ConnectTimeoutError(
                self,
                f"Connection to {self.host} timed out. (connect timeout={self.timeout})",
            ) from exc
        except OSError as exc:
            raise NewConnectionError(
                self, f"Failed to establish a new connection: {exc}"
            ) from exc

        try:
            sys.audit("http.client.connect", self, self.host, self.port)
        except BaseException:
            try:
                sock.close()
            except OSError:
                pass
            raise
        return sock


class _FreebindHTTPConnection(_FreebindConnection, HTTPConnection):
    pass


class _FreebindHTTPSConnection(_FreebindConnection, HTTPSConnection):
    pass


class _FreebindHTTPConnectionPool(HTTPConnectionPool):
    ConnectionCls = _FreebindHTTPConnection


class _FreebindHTTPSConnectionPool(HTTPSConnectionPool):
    ConnectionCls = _FreebindHTTPSConnection


class _FreebindPoolManager(PoolManager):
    def __init__(self, *args: Any, source: Source, **kwargs: Any) -> None:
        self.source = source
        super().__init__(*args, **kwargs)
        self.pool_classes_by_scheme = self.pool_classes_by_scheme.copy()
        self.pool_classes_by_scheme.update(
            http=_FreebindHTTPConnectionPool,
            https=_FreebindHTTPSConnectionPool,
        )

    def _new_pool(
        self,
        scheme: str,
        host: str,
        port: int,
        request_context: dict[str, Any] | None = None,
    ) -> HTTPConnectionPool:
        context = (
            self.connection_pool_kw.copy()
            if request_context is None
            else request_context.copy()
        )
        # Inject policy only after urllib3 has built its immutable pool key.
        context["source"] = self.source
        return super()._new_pool(scheme, host, port, request_context=context)


class FreebindAdapter(HTTPAdapter):
    """A Requests transport using ``source`` for every new TCP connection."""

    def __init__(self, source: Source, *, fresh: bool = False, **kwargs: Any) -> None:
        if not isinstance(source, Source):
            raise TypeError("source must be a Source")
        if not isinstance(fresh, bool):
            raise TypeError("fresh must be a bool")
        _reject_conflicts(kwargs)
        self.source = source
        self.fresh = fresh
        super().__init__(**kwargs)

    def init_poolmanager(
        self,
        connections: int,
        maxsize: int,
        block: bool = False,
        **pool_kwargs: Any,
    ) -> None:
        _reject_conflicts(pool_kwargs)
        self._pool_connections = connections
        self._pool_maxsize = maxsize
        self._pool_block = block
        self.poolmanager = _FreebindPoolManager(
            num_pools=connections,
            maxsize=maxsize,
            block=block,
            source=self.source,
            **pool_kwargs,
        )

    def proxy_manager_for(self, proxy: str, **proxy_kwargs: Any) -> Any:
        raise ValueError("proxies are not supported by FreebindAdapter")

    def send(
        self,
        request: Any,
        stream: bool = False,
        timeout: Any = None,
        verify: Any = True,
        cert: Any = None,
        proxies: dict[str, str] | None = None,
    ) -> Any:
        if proxies and any(value for key, value in proxies.items() if key != "no"):
            raise ValueError("proxies are not supported by FreebindAdapter")
        return super().send(request, stream, timeout, verify, cert, proxies)
