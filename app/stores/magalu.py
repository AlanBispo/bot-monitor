"""Magalu adapter using public HTML and JSON-LD."""

from __future__ import annotations

from urllib.parse import quote_plus, urljoin

from bs4 import BeautifulSoup

from ..models import StoreOffer, StoreSearchResult
from ..pricing import InvalidMoney, parse_money
from ..security import UnsafeURL, validate_url
from ..urls import STORE_DOMAINS
from .base import StoreParseError, validate_search_page
from .http import HTTPStoreAdapter
from .structured import (
    StructuredDataMissing,
    _visible_price,
    parse_jsonld_offer,
    parse_microdata_offer,
)


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
            results.append(StoreSearchResult("magalu", title, url, price))
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
        return parse_microdata_offer(soup, url, "magalu", title_node, price)


class MagaluAdapter(HTTPStoreAdapter):
    name = "magalu"
    domains = STORE_DOMAINS["magalu"]

    async def search(self, query: str):
        html = await self._get(f"https://www.magazineluiza.com.br/busca/{quote_plus(query)}/")
        return parse_search_results(html)

    async def get_offer(self, url: str):
        safe = validate_url(url, self.domains)
        return parse_offer(await self._get(safe), safe)
