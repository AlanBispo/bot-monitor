"""Adapter contract and common errors."""

from __future__ import annotations

from abc import ABC, abstractmethod

from ..models import StoreOffer, StoreSearchResult


class StoreError(RuntimeError):
    """Expected external-store failure."""


class StoreUnavailable(StoreError):
    """The adapter is unavailable or blocked without bypassing protection."""


class StoreParseError(StoreError):
    """The response cannot be classified as a valid store observation."""


class StoreAdapter(ABC):
    name: str
    domains: set[str]

    @abstractmethod
    async def search(self, query: str) -> list[StoreSearchResult]:
        raise NotImplementedError

    @abstractmethod
    async def get_offer(self, url: str) -> StoreOffer:
        raise NotImplementedError
