"""Safe generic adapter for public Product/Offer JSON-LD pages."""

from __future__ import annotations

from ..models import StoreOffer, StoreSearchResult
from ..security import validate_url
from .http import HTTPStoreAdapter
from .structured import parse_jsonld_offer


class GenericStructuredAdapter(HTTPStoreAdapter):
    name = "generic"
    domains: set[str] = set()

    async def search(self, query: str) -> list[StoreSearchResult]:
        return []

    async def get_offer(self, url: str) -> StoreOffer:
        safe = validate_url(url)
        return parse_jsonld_offer(await self._get(safe), safe, "generic")
