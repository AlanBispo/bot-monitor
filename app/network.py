"""HTTPX transport that connects only to DNS addresses vetted by security.py."""

from __future__ import annotations

import asyncio
import ssl
from collections.abc import Callable
from contextlib import contextmanager

import httpcore
import httpx

from .security import resolve_public_host


@contextmanager
def mapped_network_errors():
    """Expose the same error contract as HTTPX's built-in transport."""
    mapping = (
        (httpcore.ConnectTimeout, httpx.ConnectTimeout),
        (httpcore.ReadTimeout, httpx.ReadTimeout),
        (httpcore.WriteTimeout, httpx.WriteTimeout),
        (httpcore.PoolTimeout, httpx.PoolTimeout),
        (httpcore.ConnectError, httpx.ConnectError),
        (httpcore.ReadError, httpx.ReadError),
        (httpcore.WriteError, httpx.WriteError),
        (httpcore.RemoteProtocolError, httpx.RemoteProtocolError),
        (httpcore.LocalProtocolError, httpx.LocalProtocolError),
        (httpcore.UnsupportedProtocol, httpx.UnsupportedProtocol),
        (httpcore.NetworkError, httpx.NetworkError),
        (httpcore.TimeoutException, httpx.TimeoutException),
        (httpcore.ProtocolError, httpx.ProtocolError),
    )
    try:
        yield
    except tuple(source for source, _ in mapping) as exc:
        for source, target in mapping:
            if isinstance(exc, source):
                raise target(str(exc)) from exc
        raise


class _PinnedNetworkBackend(httpcore.AsyncNetworkBackend):
    def __init__(self, resolver: Callable[[str, int], str] = resolve_public_host):
        from httpcore._backends.auto import AutoBackend

        self._resolver = resolver
        self._backend = AutoBackend()

    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        resolve_task = asyncio.to_thread(self._resolver, host, port)
        try:
            address = (
                await asyncio.wait_for(resolve_task, timeout=timeout)
                if timeout is not None
                else await resolve_task
            )
        except TimeoutError:
            raise TimeoutError("DNS resolution timed out") from None
        return await self._backend.connect_tcp(
            address,
            port,
            timeout=timeout,
            local_address=local_address,
            socket_options=socket_options,
        )

    async def connect_unix_socket(self, path, timeout=None, socket_options=None):
        raise OSError("Unix sockets não são permitidos")

    async def sleep(self, seconds):
        await self._backend.sleep(seconds)


class _ResponseStream(httpx.AsyncByteStream):
    def __init__(self, stream):
        self._stream = stream

    async def __aiter__(self):
        with mapped_network_errors():
            async for chunk in self._stream:
                yield chunk

    async def aclose(self):
        with mapped_network_errors():
            await self._stream.aclose()


class PinnedAsyncHTTPTransport(httpx.AsyncBaseTransport):
    """Minimal HTTP/1.1 transport using the vetted IP for the TCP connection."""

    def __init__(self, resolver: Callable[[str, int], str] = resolve_public_host):
        self._pool = httpcore.AsyncConnectionPool(
            ssl_context=ssl.create_default_context(),
            http1=True,
            http2=False,
            network_backend=_PinnedNetworkBackend(resolver),
        )

    async def __aenter__(self):
        await self._pool.__aenter__()
        return self

    async def __aexit__(self, exc_type, exc_value, traceback):
        await self._pool.__aexit__(exc_type, exc_value, traceback)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        core_request = httpcore.Request(
            method=request.method,
            url=httpcore.URL(
                scheme=request.url.raw_scheme,
                host=request.url.raw_host,
                port=request.url.port,
                target=request.url.raw_path,
            ),
            headers=request.headers.raw,
            content=request.stream,
            extensions=request.extensions,
        )
        with mapped_network_errors():
            response = await self._pool.handle_async_request(core_request)
        return httpx.Response(
            status_code=response.status,
            headers=response.headers,
            stream=_ResponseStream(response.stream),
            extensions=response.extensions,
            request=request,
        )

    async def aclose(self):
        await self._pool.aclose()
