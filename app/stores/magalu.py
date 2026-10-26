"""Magalu adapter using public HTML and JSON-LD."""

from __future__ import annotations

from urllib.parse import quote_plus, urljoin

from bs4 import BeautifulSoup

from ..models import StoreOffer, StoreSearchResult
from ..pricing import InvalidMoney, parse_money
from ..security import UnsafeURL, validate_url
from .base import StoreParseError, validate_search_page
from .http import HTTPStoreAdapter
from .structured import StructuredDataMissing, _availability, _visible_price, parse_jsonld_offer


def parse_search_results(html: str, base_url: str = "https://www.magazineluiza.com.br"):
    soup = BeautifulSoup(html, "html.parser")
    results = []
    for link in soup.select("a[href]"):
        title = link.get("title") or link.get_text(" ", strip=True)
        if not title or len(title) < 4:
            continue
        url = urljoin(base_url, link["href"])
        try:
            url = validate_url(url, MagaluAdapter.domains)
        except UnsafeURL:
            continue
        price = _visible_price(link.get_text(" ", strip=True))
        if price is not None or "produto" in title.casefold():
            results.append(StoreSearchResult("magalu", title, url, price, source="magalu"))
    return validate_search_page(html, list({item.url: item for item in results}.values()))


def parse_offer(html: str, url: str) -> StoreOffer:
    try:
        return parse_jsonld_offer(html, url, "magalu")
    except StructuredDataMissing:
        soup = BeautifulSoup(html, "html.parser")
        title_node = soup.select_one("h1[data-testid='heading-product-title'], h1")
        price_node = soup.select_one("[data-testid='price-value'], .price-template")
        if not title_node or not price_node:
            raise StoreParseError("página Magalu sem produto e preço") from None
        try:
            price = parse_money(price_node.get_text(" ", strip=True))
        except InvalidMoney as exc:
            raise StoreParseError("preço Magalu inválido") from exc
        availability = soup.select_one("meta[itemprop='availability']")
        available = _availability(availability.get("content") if availability else None)
        if available is None:
            raise StoreParseError("disponibilidade Magalu desconhecida") from None
        currency = soup.select_one("meta[itemprop='priceCurrency']")
        if currency is None or currency.get("content", "").upper() != "BRL":
            raise StoreParseError("moeda Magalu não comprovada como BRL") from None
        if available is False:
            return StoreOffer(
                "magalu",
                title_node.get_text(" ", strip=True),
                url,
                None,
                None,
                available=False,
                observation_status="unavailable",
            )
        return StoreOffer("magalu", title_node.get_text(" ", strip=True), url, price, None)


class MagaluAdapter(HTTPStoreAdapter):
    name = "magalu"
    domains = {"magazineluiza.com.br", "magalu.com"}

    async def search(self, query: str):
        if not query or len(query) > 200:
            raise StoreParseError("termo de busca inválido")
        html = await self._get(f"https://www.magazineluiza.com.br/busca/{quote_plus(query)}/")
        return parse_search_results(html)

    async def get_offer(self, url: str):
        safe = validate_url(url, self.domains)
        return parse_offer(await self._get(safe), safe)
