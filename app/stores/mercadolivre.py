"""Mercado Livre Brasil adapter: official API for catalog links, public HTML otherwise."""

from __future__ import annotations

import re
import time
from decimal import Decimal
from urllib.parse import quote_plus, urlencode, urljoin, urlsplit

import httpx
from bs4 import BeautifulSoup

from ..models import StoreOffer, StoreSearchResult
from ..pricing import InvalidMoney, parse_money
from ..security import UnsafeURL, validate_url
from ..urls import STORE_DOMAINS
from .base import StoreError, StoreParseError, StoreUnavailable, validate_search_page
from .http import HTTPStoreAdapter, request_limited
from .structured import (
    StructuredDataMissing,
    _visible_price,
    parse_jsonld_offer,
    parse_microdata_offer,
)

API = "https://api.mercadolibre.com"
CATALOG_PATH = re.compile(r"/p/(MLB\d+)", re.IGNORECASE)
TOKEN_EARLY_RENEWAL = 60
AIR_CONDITIONERS_DOMAIN = "MLB-AIR_CONDITIONERS"
CATALOG_SEARCH_LIMIT = 20


def _cheapest_new_offer(listing: dict | None) -> dict | None:
    rows = [
        row for row in (listing or {}).get("results", [])
        if row.get("price") and row.get("currency_id") == "BRL"
        and row.get("condition") == "new"
    ]
    return min(rows, key=lambda row: parse_money(row["price"]), default=None)


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
                "Use o link de catálogo do produto, com /p/MLB no endereço."
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
    domains = STORE_DOMAINS["mercadolivre"]

    def __init__(
        self,
        timeout_seconds: float = 15,
        max_response_bytes: int = 2_000_000,
        client_id: str | None = None,
        client_secret: str | None = None,
    ):
        super().__init__(timeout_seconds, max_response_bytes)
        self.client_id = client_id
        self.client_secret = client_secret
        self._token: str | None = None
        self._token_expires_at = 0.0

    async def search(self, query: str):
        html = await self._get(f"https://lista.mercadolivre.com.br/{quote_plus(query)}")
        return parse_search_results(html)

    async def get_offer(self, url: str):
        safe = validate_url(url, self.domains)
        catalog = CATALOG_PATH.search(urlsplit(safe).path)
        if catalog and self.client_id and self.client_secret:
            return await self._catalog_offer(catalog.group(1).upper(), safe)
        return parse_offer(await self._get(safe), safe)

    async def _catalog_offer(self, product_id: str, url: str) -> StoreOffer:
        """Cheapest new offer of a catalog product, read from the official API."""
        product = await self._api_get(f"/products/{product_id}")
        if product is None or not product.get("name"):
            raise StoreError("produto de catálogo não encontrado no Mercado Livre")
        title = product["name"]
        canonical = f"https://www.mercadolivre.com.br/p/{product_id}"
        # The API answers 404 when an existing catalog product has no offer to show.
        listing = await self._api_get(f"/products/{product_id}/items")
        best = _cheapest_new_offer(listing)
        if best is None:
            return StoreOffer(
                self.name, title, url, None, None, available=False,
                observation_status="unavailable", canonical_url=canonical,
            )
        # The API quotes a cost without any destination, so only free shipping is reliable.
        shipping = Decimal("0.00") if (best.get("shipping") or {}).get("free_shipping") else None
        return StoreOffer(
            self.name, title, url, parse_money(best["price"]), shipping, canonical_url=canonical,
        )

    async def catalog_search(self, query: str, accept) -> list[dict]:
        """Catalog products accepted by the filter that have an offer, across all sellers."""
        params = urlencode({
            "status": "active", "site_id": "MLB", "q": query, "limit": CATALOG_SEARCH_LIMIT,
        })
        found = []
        for row in (await self._api_get(f"/products/search?{params}") or {}).get("results", []):
            if not accept(row):
                continue
            best = _cheapest_new_offer(await self._api_get(f"/products/{row['id']}/items"))
            if best is not None:
                found.append({
                    "name": row["name"],
                    "attributes": row.get("attributes", []),
                    "price": parse_money(best["price"]),
                    "url": f"https://www.mercadolivre.com.br/p/{row['id']}",
                })
        return found

    async def _access_token(self) -> str:
        if self._token and time.monotonic() < self._token_expires_at:
            return self._token
        try:
            async with self._client() as client:
                response = await client.post(f"{API}/oauth/token", data={
                    "grant_type": "client_credentials",
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                })
        except (httpx.HTTPError, TimeoutError, OSError) as exc:
            raise StoreError(f"falha HTTP no Mercado Livre: {type(exc).__name__}") from exc
        if response.status_code != 200:
            raise StoreUnavailable(
                f"O Mercado Livre recusou as credenciais do app (HTTP {response.status_code})."
            )
        payload = response.json()
        self._token = payload["access_token"]
        self._token_expires_at = time.monotonic() + payload["expires_in"] - TOKEN_EARLY_RENEWAL
        return self._token

    async def _api_get(self, path: str) -> dict | None:
        """Return the JSON body, or None for 404."""
        token = await self._access_token()
        try:
            async with self._client() as client:
                response = await request_limited(
                    client, f"{API}{path}", self.max_response_bytes,
                    headers={"Authorization": f"Bearer {token}"},
                )
        except (httpx.HTTPError, TimeoutError, OSError) as exc:
            raise StoreError(f"falha HTTP no Mercado Livre: {type(exc).__name__}") from exc
        if response.status_code == 404:
            return None
        if response.status_code == 401:
            self._token = None
        # 206 is a normal answer here: the body simply omits some optional fields.
        if response.status_code not in {200, 206}:
            raise StoreUnavailable(f"A API do Mercado Livre recusou (HTTP {response.status_code}).")
        return response.json()
