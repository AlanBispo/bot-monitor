"""Shared bounded HTTP behavior for store adapters."""

from __future__ import annotations

from urllib.parse import urljoin

import httpx

from ..network import PinnedAsyncHTTPTransport
from ..security import UnsafeURL, validate_url
from .base import StoreAdapter, StoreError


async def request_limited(
    client: httpx.AsyncClient, url: str, max_response_bytes: int, **kwargs
) -> httpx.Response:
    """Read a response incrementally and stop before exceeding the configured cap."""
    async with client.stream("GET", url, **kwargs) as response:
        body = bytearray()
        async for chunk in response.aiter_bytes():
            if len(body) + len(chunk) > max_response_bytes:
                raise StoreError("resposta excede o limite configurado")
            body.extend(chunk)
        headers = response.headers.copy()
        # aiter_bytes has already decoded Content-Encoding. Reusing that header
        # would ask HTTPX to decompress the decoded body a second time.
        for name in ("content-encoding", "content-length", "transfer-encoding"):
            headers.pop(name, None)
        return httpx.Response(
            status_code=response.status_code,
            headers=headers,
            content=bytes(body),
            request=response.request,
        )


class HTTPStoreAdapter(StoreAdapter):
    def __init__(self, timeout_seconds: float = 15, max_response_bytes: int = 2_000_000):
        self.timeout_seconds = timeout_seconds
        self.max_response_bytes = max(1024, max_response_bytes)

    def _client(self):
        return httpx.AsyncClient(
            transport=PinnedAsyncHTTPTransport(),
            timeout=self.timeout_seconds,
            follow_redirects=False,
            trust_env=False,
            headers={"User-Agent": "price-monitor/0.1 (personal use)"},
        )

    async def _get(self, url: str) -> str:
        current = validate_url(url, self.domains)
        for _ in range(4):
            try:
                async with self._client() as client:
                    response = await request_limited(client, current, self.max_response_bytes)
                if response.is_redirect:
                    location = response.headers.get("location")
                    if not location:
                        raise StoreError("redirect sem destino")
                    current = validate_url(urljoin(current, location), self.domains)
                    continue
                response.raise_for_status()
                return response.text
            except (httpx.HTTPError, TimeoutError, UnsafeURL, OSError) as exc:
                raise StoreError(f"falha HTTP em {self.name}: {type(exc).__name__}") from exc
        raise StoreError(f"redirecionamentos demais em {self.name}")
