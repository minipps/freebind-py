"""curl_cffi sessions with source-bound libcurl sockets."""

from __future__ import annotations

import socket
from typing import Any

from curl_cffi import Curl, CurlECode, CurlError, CurlOpt
from curl_cffi import AsyncSession as _AsyncSession
from curl_cffi import Session as _Session
from curl_cffi._wrapper import ffi, lib

from ._socket import _ORIGINAL_SOCKET, bind_socket
from ._source import Source


_CONFLICTING_OPTIONS = {
    CurlOpt.INTERFACE, CurlOpt.LOCALPORT, CurlOpt.LOCALPORTRANGE,
    CurlOpt.SOCKOPTFUNCTION, CurlOpt.SOCKOPTDATA,
    CurlOpt.OPENSOCKETFUNCTION, CurlOpt.OPENSOCKETDATA,
    CurlOpt.CLOSESOCKETFUNCTION, CurlOpt.CLOSESOCKETDATA,
    CurlOpt.UNIX_SOCKET_PATH, CurlOpt.ABSTRACT_UNIX_SOCKET,
    CurlOpt.CONNECT_ONLY,
}


class _FreebindCurl(Curl):
    def __init__(self, source: Source, fresh: bool, **kwargs: Any) -> None:
        self._source = source
        self._fresh = fresh
        self._bind_error: BaseException | None = None
        # ponytail: curl_cffi has no public socket callback; recheck this bridge before widening its version range.
        self._socket_callback = ffi.callback("int(void *, int, int)", self._bind, error=1)
        super().__init__(**kwargs)
        try:
            self._configure()
        except BaseException:
            self.close()
            raise

    def _bind(self, _data, fd: int, purpose: int) -> int:
        if self._bind_error is not None:
            return 1
        sock = None
        try:
            if purpose != 0:
                raise ValueError("Freebind requires an outgoing IP socket")
            sock = _ORIGINAL_SOCKET(fileno=fd)
            bind_socket(sock, self._source)
            return 0
        except BaseException as exc:
            self._bind_error = exc
            return 1
        finally:
            if sock is not None:
                sock.detach()  # libcurl owns and closes this descriptor.

    def _configure(self) -> None:
        if self._curl is None:
            return
        self._bind_error = None
        code = lib._curl_easy_setopt(
            self._curl, CurlOpt.SOCKOPTFUNCTION, self._socket_callback
        )
        self._check_error(code, "install Freebind socket callback")
        super().setopt(CurlOpt.PROXY, "")
        if self._source.strict and len(self._source.families) == 1:
            family = next(iter(self._source.families))
            super().setopt(CurlOpt.IPRESOLVE, 1 if family == socket.AF_INET else 2)
        if self._fresh:
            super().setopt(CurlOpt.FRESH_CONNECT, 1)
            super().setopt(CurlOpt.FORBID_REUSE, 1)

    def setopt(self, option: CurlOpt, value: Any) -> int:
        if option in _CONFLICTING_OPTIONS:
            raise ValueError(f"{option.name} conflicts with the Freebind source")
        if option in (CurlOpt.PROXY, CurlOpt.PRE_PROXY) and value:
            raise ValueError("Freebind sessions do not support proxies")
        if option == CurlOpt.PROXY:
            value = ""  # NULL would re-enable libcurl's environment proxy discovery.
        if option == CurlOpt.IPRESOLVE and self._source.strict:
            if len(self._source.families) == 1:
                family = next(iter(self._source.families))
                expected = 1 if family == socket.AF_INET else 2
                if value != expected:
                    raise ValueError("IPRESOLVE conflicts with the Freebind source")
        if self._fresh and option in (CurlOpt.FRESH_CONNECT, CurlOpt.FORBID_REUSE):
            if value != 1:
                raise ValueError(f"{option.name} conflicts with fresh=True")
        return super().setopt(option, value)

    def _get_callback_exception(self) -> BaseException | None:
        if self._bind_error is not None:
            error = CurlError("Freebind socket binding failed", CurlECode.ABORTED_BY_CALLBACK)
            error.__cause__ = self._bind_error
            return error
        return super()._get_callback_exception()

    def reset(self) -> None:
        super().reset()
        self._configure()

    def duphandle(self) -> _FreebindCurl:
        if self._curl is None:
            raise CurlError("Cannot duplicate closed handle")
        return _FreebindCurl(
            self._source, self._fresh, cacert=self._cacert, debug=self._debug,
            handle=lib.curl_easy_duphandle(self._curl),
        )


def _validate(source: Source, fresh: bool, kwargs: dict[str, Any]) -> None:
    if not isinstance(source, Source):
        raise TypeError("source must be a Source")
    if not isinstance(fresh, bool):
        raise TypeError("fresh must be a bool")
    for key in ("curl", "async_curl", "interface", "proxy", "proxies"):
        if kwargs.get(key) is not None:
            raise ValueError(f"{key} conflicts with the Freebind session")
    kwargs.setdefault("trust_env", False)


class FreebindSession(_Session):
    """Synchronous curl_cffi session; each new socket uses ``source``."""

    def __init__(self, source: Source, *, fresh: bool = False, **kwargs: Any) -> None:
        _validate(source, fresh, kwargs)
        self._freebind_source = source
        self.fresh = fresh
        curl = _FreebindCurl(source, fresh, debug=kwargs.get("debug", False))
        try:
            super().__init__(curl=curl, **kwargs)
        except BaseException:
            curl.close()
            raise

    @property
    def curl(self) -> _FreebindCurl:
        if not self._use_thread_local_curl:
            return self._curl
        if not getattr(self._local, "curl", None):
            self._local.curl = _FreebindCurl(
                self._freebind_source, self.fresh, debug=self.debug
            )
        return self._local.curl


class AsyncFreebindSession(_AsyncSession):
    """Asyncio curl_cffi session; each new socket uses ``source``."""

    def __init__(self, source: Source, *, fresh: bool = False, **kwargs: Any) -> None:
        _validate(source, fresh, kwargs)
        self._freebind_source = source
        self.fresh = fresh
        super().__init__(**kwargs)

    async def pop_curl(self) -> _FreebindCurl:
        curl = await self.pool.get()
        if curl is None:
            try:
                curl = _FreebindCurl(
                    self._freebind_source, self.fresh,
                    cacert=self.acurl._cacert, debug=self.debug,
                )
            except BaseException:
                self.push_curl(None)
                raise
        return curl
