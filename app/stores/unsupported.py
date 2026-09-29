"""Explicit placeholders for stores planned after the MVP."""

from __future__ import annotations

from ..models import StoreOffer, StoreSearchResult
from .base import StoreAdapter, StoreUnavailable


class UnsupportedStoreAdapter(StoreAdapter):
    def __init__(self, name: str, domains: set[str]):
        self.name = name
        self.domains = domains

    async def search(self, query: str) -> list[StoreSearchResult]:
        raise StoreUnavailable(f"adapter {self.name} ainda não está disponível no MVP")

    async def get_offer(self, url: str) -> StoreOffer:
        raise StoreUnavailable(f"adapter {self.name} ainda não está disponível no MVP")
