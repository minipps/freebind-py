"""aiohttp connector integration for Freebind source policies."""

import ipaddress
import socket
from typing import Any

from aiohttp import TCPConnector

from ._socket import new_socket
from ._source import FamilyMismatchError, Source


def _source_socket_factory(source: Source):
    """Create source-bound sockets for aiohttp Happy Eyeballs candidates."""

    def create(addr_info):
        family, sock_type, _proto, _canonname, _sockaddr = addr_info
        return new_socket(source, family=family, type=sock_type)

    return create


class FreebindConnector(TCPConnector):
    """TCP connector that binds each new socket with a Freebind source.

    DNS caching, tracing, Happy Eyeballs, TLS, and connection cleanup remain
    managed by aiohttp. fresh=True enables aiohttp's native force-close mode.
    """

    def __init__(self, source: Source, *, fresh: bool = False, **kwargs: Any) -> None:
        if not isinstance(source, Source):
            raise TypeError("source must be a Source")
        if not isinstance(fresh, bool):
            raise TypeError("fresh must be a bool")
        if kwargs.get("local_addr") is not None:
            raise ValueError("local_addr conflicts with the Freebind source")
        if kwargs.get("socket_factory") is not None:
            raise ValueError("socket_factory conflicts with the Freebind source")

        kwargs["socket_factory"] = _source_socket_factory(source)
        if fresh:
            kwargs["force_close"] = True
        super().__init__(**kwargs)
        self._freebind_source = source

    async def _resolve_host(self, host: str, port: int, traces=None):
        """Filter DNS results before aiohttp orders Happy Eyeballs candidates."""
        addresses = await super()._resolve_host(host, port, traces=traces)
        try:
            literal_family = (
                socket.AF_INET
                if ipaddress.ip_address(host).version == 4
                else socket.AF_INET6
            )
        except ValueError:
            literal_family = None

        if literal_family is not None:
            # Numeric hosts may carry AF_UNSPEC metadata from aiohttp's resolver.
            addresses = [dict(address, family=literal_family) for address in addresses]

        matching = [
            address
            for address in addresses
            if address["family"] in self._freebind_source.families
        ]
        if matching:
            return matching
        if self._freebind_source.strict:
            raise FamilyMismatchError(
                "No resolved destination address matches the configured source families"
            )
        return addresses

    async def connect(self, req, traces, timeout):
        """Reject proxy requests before aiohttp can return a pooled connection."""
        if req.proxy is not None:
            raise ValueError("FreebindConnector does not support proxies")
        return await super().connect(req, traces, timeout)
