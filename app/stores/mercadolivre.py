"""Mercado Livre Brasil adapter using public HTML and JSON-LD."""

from __future__ import annotations

from urllib.parse import quote_plus, urljoin

from bs4 import BeautifulSoup

from ..models import StoreOffer, StoreSearchResult
from ..pricing import InvalidMoney, parse_money
from ..security import UnsafeURL, validate_url
from .base import StoreParseError, StoreUnavailable, validate_search_page
from .http import HTTPStoreAdapter
from .structured import (
    StructuredDataMissing,
    _visible_price,
    parse_jsonld_offer,
    parse_microdata_offer,
)


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
            )
        )
    return validate_search_page(html, results)


def parse_offer(html: str, url: str) -> StoreOffer:
    try:
        return parse_jsonld_offer(html, url, "mercadolivre")
    except StructuredDataMissing:
        soup = BeautifulSoup(html, "html.parser")
        visible_text = " ".join(soup.get_text(" ", strip=True).casefold().split())
        if any(marker in visible_text for marker in (
            "para continuar, acesse sua conta", "entre na sua conta para continuar",
        )):
            raise StoreUnavailable(
                "Mercado Livre exigiu login e não disponibilizou o preço ao monitor. "
                "Tente um link público direto do anúncio; se o login continuar sendo exigido, "
                "essa oferta não pode ser monitorada nesta versão."
            ) from None
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
        return parse_microdata_offer(soup, url, "mercadolivre", title_node, price)


class MercadoLivreAdapter(HTTPStoreAdapter):
    name = "mercadolivre"
    domains = {"mercadolivre.com.br", "mercadolibre.com.br"}

    async def search(self, query: str):
        html = await self._get(f"https://lista.mercadolivre.com.br/{quote_plus(query)}")
        return parse_search_results(html)

    async def get_offer(self, url: str):
        safe = validate_url(url, self.domains)
        return parse_offer(await self._get(safe), safe)
