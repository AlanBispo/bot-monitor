"""Adapter contract and common errors."""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod

from ..models import StoreOffer, StoreSearchResult


class StoreError(RuntimeError):
    """Expected external-store failure."""


class StoreUnavailable(StoreError):
    """The adapter is unavailable or blocked without bypassing protection."""


class StoreParseError(StoreError):
    """The response cannot be classified as a valid store observation."""


def validate_search_page(html: str, results: list[StoreSearchResult]) -> list[StoreSearchResult]:
    """Empty is a successful observation only on a recognizable empty-results page."""
    if results:
        return results
    text = html.casefold()
    if any(marker in text for marker in (
        "captcha", "verify you are human", "verifique se você é humano",
        "access denied", "acesso negado", "robot check",
        "para continuar, acesse sua conta", "entre na sua conta para continuar",
    )):
        raise StoreUnavailable("busca bloqueada pela loja")
    if any(marker in text for marker in (
        "sem resultados", "nenhum resultado", "não encontramos", "nao encontramos",
        "não foram encontrados", "nenhum produto", "0 resultados", "no results",
        "did not match any", "no search results",
    )):
        return []
    raise StoreParseError("página de busca desconhecida; cobertura não comprovada")


class StoreAdapter(ABC):
    name: str
    domains: set[str]

    @abstractmethod
    async def search(self, query: str) -> list[StoreSearchResult]:
        raise NotImplementedError

    @abstractmethod
    async def get_offer(self, url: str) -> StoreOffer:
        raise NotImplementedError


async def collect_offer(adapter: StoreAdapter, url: str, timeout: float = 30) -> StoreOffer:
    """Bound the entire fetch, including redirects and slow streaming responses."""
    try:
        return await asyncio.wait_for(adapter.get_offer(url), timeout=timeout)
    except TimeoutError as exc:
        raise StoreError("tempo total de consulta esgotado") from exc
