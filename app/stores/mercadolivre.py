"""Mercado Livre Brasil adapter using public HTML and JSON-LD."""

from __future__ import annotations

from urllib.parse import quote_plus, urljoin

from bs4 import BeautifulSoup

from ..models import StoreOffer, StoreSearchResult
from ..pricing import InvalidMoney, parse_money
from ..security import UnsafeURL, validate_url
from .base import StoreParseError, validate_search_page
from .http import HTTPStoreAdapter
from .structured import StructuredDataMissing, _availability, _visible_price, parse_jsonld_offer


def parse_search_results(html: str, base_url: str = "https://lista.mercadolivre.com.br"):
    soup = BeautifulSoup(html, "html.parser")
    results = []
    for item in soup.select("li.ui-search-layout__item, .poly-card"):
        link = item.select_one("a[href]")
        title = item.select_one("h2, .poly-component__title")
        if not link or not title:
            continue
        url = urljoin(base_url, link["href"])
        try:
            url = validate_url(url, MercadoLivreAdapter.domains)
        except UnsafeURL:
            continue
        results.append(
            StoreSearchResult(
                "mercadolivre",
                title.get_text(" ", strip=True),
                url,
                _visible_price(item.get_text(" ", strip=True)),
                source="mercadolivre",
            )
        )
    return validate_search_page(html, results)


def parse_offer(html: str, url: str) -> StoreOffer:
    try:
        return parse_jsonld_offer(html, url, "mercadolivre")
    except StructuredDataMissing:
        soup = BeautifulSoup(html, "html.parser")
        title_node = soup.select_one("h1.ui-pdp-title, h1[data-testid='heading-product-title']")
        price_node = soup.select_one(".ui-pdp-price__second-line .andes-money-amount__fraction")
        if not title_node or not price_node:
            raise StoreParseError("página Mercado Livre sem produto e preço") from None
        cents = soup.select_one(".ui-pdp-price__second-line .andes-money-amount__cents")
        raw = price_node.get_text(strip=True) + (
            "," + cents.get_text(strip=True) if cents else ",00"
        )
        try:
            price = parse_money(raw)
        except InvalidMoney as exc:
            raise StoreParseError("preço Mercado Livre inválido") from exc
        availability_node = soup.select_one("meta[itemprop='availability']")
        available = _availability(
            availability_node.get("content") if availability_node else None
        )
        if available is None:
            raise StoreParseError("disponibilidade Mercado Livre desconhecida") from None
        currency = soup.select_one("meta[itemprop='priceCurrency']")
        if currency is None or currency.get("content", "").upper() != "BRL":
            raise StoreParseError("moeda Mercado Livre não comprovada como BRL") from None
        if available is False:
            return StoreOffer(
                "mercadolivre",
                title_node.get_text(" ", strip=True),
                url,
                None,
                None,
                available=False,
                observation_status="unavailable",
            )
        return StoreOffer("mercadolivre", title_node.get_text(" ", strip=True), url, price, None)


class MercadoLivreAdapter(HTTPStoreAdapter):
    name = "mercadolivre"
    domains = {"mercadolivre.com.br", "mercadolibre.com.br"}

    async def search(self, query: str):
        if not query or len(query) > 200:
            raise StoreParseError("termo de busca inválido")
        html = await self._get(f"https://lista.mercadolivre.com.br/{quote_plus(query)}")
        return parse_search_results(html)

    async def get_offer(self, url: str):
        safe = validate_url(url, self.domains)
        return parse_offer(await self._get(safe), safe)
