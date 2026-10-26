"""Amazon Brasil adapter using ordinary HTTP and HTML parsing."""

from __future__ import annotations

import logging
import re
from urllib.parse import quote_plus, urljoin

import httpx
from bs4 import BeautifulSoup

from ..models import StoreOffer, StoreSearchResult
from ..network import PinnedAsyncHTTPTransport
from ..pricing import InvalidMoney, parse_money
from ..security import UnsafeURL, validate_url
from .base import StoreAdapter, StoreError, StoreParseError, validate_search_page
from .http import request_limited

LOGGER = logging.getLogger(__name__)
_SHIPPING_AMOUNT = r"R\$\s*[\d.]+(?:,[\d]{1,2})?"
_UNCONDITIONAL_FREE_SHIPPING = re.compile(
    r"^(?:frete|entrega)\s+grátis[.!]?$", re.IGNORECASE
)
_EXPLICIT_SHIPPING_AMOUNT = re.compile(
    rf"^(?:(?:frete|entrega)(?:\s+(?:de|por))?\s*:?\s*{_SHIPPING_AMOUNT}|"
    rf"{_SHIPPING_AMOUNT})[.!]?$",
    re.IGNORECASE,
)
_UNAVAILABLE_AVAILABILITY = (
    re.compile(r"\b(?:não|nao)\s+(?:(?:está|esta)\s+)?dispon[ií]vel\b"),
    re.compile(r"\b(?:não|nao)\s+(?:há|ha)\b.*\b(?:oferta|estoque|venda)"),
    re.compile(r"\b(?:temporariamente\s+)?fora\s+de\s+estoque\b"),
    re.compile(r"\b(?:out[-\s]+of[-\s]+stock|currently\s+unavailable|unavailable)\b"),
    re.compile(
        r"\b(?:(?:currently|temporarily)\s+)?not\s+(?:currently\s+)?available\b"
    ),
    re.compile(r"\b(?:not|never)\s+in[-\s]+stock\b"),
    re.compile(r"\bno\s+(?:available\s+)?offers?\b"),
    re.compile(r"\bno\s+(?:longer\s+)?available\s+offers?\b"),
    re.compile(r"\bno\s+longer\s+available\b"),
    re.compile(r"\b(?:indispon[ií]vel|esgotad[oa]?)\b"),
)
_AVAILABLE_AVAILABILITY = (
    re.compile(r"em\s+estoque[.!]?"),
    re.compile(r"in\s+stock[.!]?"),
    re.compile(r"dispon[ií]vel[.!]?"),
    re.compile(r"available[.!]?"),
)


def _first_text(soup: BeautifulSoup, selectors: tuple[str, ...]) -> str | None:
    for selector in selectors:
        node = soup.select_one(selector)
        if node:
            text = node.get_text(" ", strip=True)
            if text:
                return text
    return None


def _price(text: str | None, *, allow_zero: bool = False):
    if not text:
        return None
    try:
        return parse_money(text, allow_zero=allow_zero)
    except InvalidMoney:
        return None


def _availability_state(text: str) -> bool | None:
    normalized = " ".join(text.casefold().split())
    if any(pattern.search(normalized) for pattern in _UNAVAILABLE_AVAILABILITY):
        return False
    if re.search(r"\b(?:não|nao)\b", normalized) and re.search(
        r"\b(?:dispon|estoque|ofert|venda)", normalized
    ):
        return False
    if any(pattern.fullmatch(normalized) for pattern in _AVAILABLE_AVAILABILITY):
        return True
    return None


def parse_search_results(
    html: str, base_url: str = "https://www.amazon.com.br"
) -> list[StoreSearchResult]:
    soup = BeautifulSoup(html, "html.parser")
    results: list[StoreSearchResult] = []
    for item in soup.select('div[data-component-type="s-search-result"][data-asin]'):
        title = _first_text(item, ("h2 span", "h2"))
        link = item.select_one("h2 a, a:has(h2)")
        if not title or not link or not link.get("href"):
            continue
        url = urljoin(base_url, link["href"])
        try:
            url = validate_url(url, AmazonAdapter.domains)
        except UnsafeURL:
            continue
        price = _price(_first_text(item, (".a-price .a-offscreen",)))
        results.append(StoreSearchResult("amazon", title, url, price))
    return validate_search_page(html, results)


def parse_offer(html: str, url: str) -> StoreOffer:
    soup = BeautifulSoup(html, "html.parser")
    title = _first_text(soup, ("#productTitle", "meta[property='og:title']"))
    if not title:
        meta = soup.select_one("meta[property='og:title']")
        title = meta.get("content", "") if meta else ""
    price_text = _first_text(
        soup,
        (
            "#corePriceDisplay_desktop_feature_div .a-offscreen",
            "#priceblock_ourprice",
            "#priceblock_dealprice",
            "#price_inside_buybox",
            "#buybox .a-price .a-offscreen",
            "#apex_desktop .a-price .a-offscreen",
        ),
    )
    product_price = _price(price_text)
    seller = _first_text(soup, ("#sellerProfileTriggerId", "#merchant-info"))
    availability_text = _first_text(
        soup,
        ("#availability", "#outOfStock", "#availability_feature_div"),
    )
    availability = _availability_state(availability_text) if availability_text else None
    if availability is None:
        raise StoreParseError("disponibilidade da oferta desconhecida")
    if availability is False:
        return StoreOffer(
            store="amazon",
            title=title.strip(),
            url=url,
            product_price=None,
            shipping_price=None,
            seller=seller,
            available=False,
            observation_status="unavailable",
        )
    shipping_text = _first_text(
        soup,
        (
            "#mir-layout-DELIVERY_BLOCK-slot-PRIMARY_DELIVERY_MESSAGE_LARGE",
            "#delivery-block-outer-container",
        ),
    )
    normalized_shipping = " ".join(shipping_text.split()) if shipping_text else ""
    if _UNCONDITIONAL_FREE_SHIPPING.fullmatch(normalized_shipping):
        shipping_price = _price("0", allow_zero=True)
    elif shipping_text and _EXPLICIT_SHIPPING_AMOUNT.fullmatch(normalized_shipping):
        match = re.search(_SHIPPING_AMOUNT, normalized_shipping, re.IGNORECASE)
        shipping_price = _price(match.group(0), allow_zero=True) if match else None
    else:
        # Free-shipping marketing and qualified delivery text are ambiguous.  In
        # particular, never treat a purchase threshold as the shipping charge.
        shipping_price = None
    if product_price is None:
        raise StoreParseError("preço da oferta não encontrado")
    return StoreOffer(
        store="amazon",
        title=title.strip(),
        url=url,
        product_price=product_price,
        shipping_price=shipping_price,
        seller=seller,
        available=product_price is not None,
    )


class AmazonAdapter(StoreAdapter):
    name = "amazon"
    domains = {"amazon.com.br", "amazon.com"}

    def __init__(self, timeout_seconds: float = 15, max_response_bytes: int = 2_000_000):
        self.timeout_seconds = timeout_seconds
        self.max_response_bytes = max(1024, max_response_bytes)

    def _client(self):
        return httpx.AsyncClient(
            transport=PinnedAsyncHTTPTransport(),
            timeout=self.timeout_seconds,
            follow_redirects=False,
            trust_env=False,
            headers={"User-Agent": "price-monitor/0.1 (personal use)"},
        )

    async def _get(self, url: str) -> str:
        current_url = validate_url(url, self.domains)
        for _redirect_count in range(4):
            try:
                async with self._client() as client:
                    response = await request_limited(
                        client, current_url, self.max_response_bytes
                    )
                if response.is_redirect:
                    location = response.headers.get("location")
                    if not location:
                        raise StoreError("redirect sem destino")
                    current_url = validate_url(urljoin(current_url, location), self.domains)
                    continue
                response.raise_for_status()
                return response.text
            except (httpx.HTTPError, TimeoutError, UnsafeURL, OSError) as exc:
                raise StoreError(f"falha HTTP na Amazon: {type(exc).__name__}") from exc
        raise StoreError("redirecionamentos demais na Amazon")

    async def search(self, query: str) -> list[StoreSearchResult]:
        query = query.strip()
        if not query or len(query) > 200:
            raise StoreError("termo de busca ausente ou longo demais")
        url = f"https://www.amazon.com.br/s?k={quote_plus(query)}"
        html = await self._get(url)
        results = parse_search_results(html)
        if not results:
            LOGGER.info("Amazon retornou zero resultados ou mudou o HTML")
        return results[:10]

    async def get_offer(self, url: str) -> StoreOffer:
        safe_url = validate_url(url, self.domains)
        return parse_offer(await self._get(safe_url), safe_url)
