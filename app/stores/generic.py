"""Safe generic adapter for public Product/Offer JSON-LD pages."""

from __future__ import annotations

from urllib.parse import urljoin

import httpx

from ..models import StoreOffer, StoreSearchResult
from ..network import PinnedAsyncHTTPTransport
from ..security import UnsafeURL, validate_url
from .base import StoreAdapter, StoreError
from .http import request_limited
from .structured import parse_jsonld_offer


class GenericStructuredAdapter(StoreAdapter):
    name = "generic"
    domains: set[str] = set()

    def __init__(
        self,
        fetcher=None,
        timeout_seconds: float = 15,
        max_response_bytes: int = 2_000_000,
    ):
        self.fetcher = fetcher
        self.timeout_seconds = timeout_seconds
        self.max_response_bytes = max(1024, max_response_bytes)

    async def search(self, query: str) -> list[StoreSearchResult]:
        return []

    async def get_offer(self, url: str) -> StoreOffer:
        safe = validate_url(url)
        html = await self.fetcher(safe) if self.fetcher else await self._get(safe)
        return parse_jsonld_offer(html, safe, "generic")

    async def _get(self, url: str) -> str:
        current = url
        for _ in range(4):
            try:
                async with httpx.AsyncClient(
                    transport=PinnedAsyncHTTPTransport(),
                    timeout=self.timeout_seconds,
                    follow_redirects=False,
                    trust_env=False,
                    headers={"User-Agent": "price-monitor/0.1 (personal use)"},
                ) as client:
                    response = await request_limited(
                        client, current, self.max_response_bytes
                    )
                if response.is_redirect:
                    location = response.headers.get("location")
                    if not location:
                        raise StoreError("redirect sem destino")
                    current = validate_url(urljoin(current, location))
                    continue
                response.raise_for_status()
                return response.text
            except (httpx.HTTPError, TimeoutError, UnsafeURL, OSError) as exc:
                raise StoreError(f"falha HTTP no extrator genérico: {type(exc).__name__}") from exc
        raise StoreError("redirecionamentos demais no extrator genérico")
