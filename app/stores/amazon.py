"""Amazon Brasil adapter using ordinary HTTP and HTML parsing."""

from __future__ import annotations

import asyncio
import logging
import re
from urllib.parse import quote_plus, urljoin, urlsplit

import httpx
from bs4 import BeautifulSoup

from ..models import StoreOffer, StoreSearchResult
from ..pricing import InvalidMoney, parse_money
from ..security import UnsafeURL, validate_url
from .base import StoreError, StoreParseError, StoreUnavailable, validate_search_page
from .http import HTTPStoreAdapter, request_limited

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
_UNAVAILABLE_AVAILABILITY = re.compile(
    r"\b(?:indispon[ií]vel|esgotad[oa]?|fora\s+de\s+estoque|out[-\s]+of[-\s]+stock|unavailable"
    r"|not\s+(?:currently\s+)?available|no\s+longer\s+available"
    r"|(?:not|never)\s+in[-\s]+stock|no\s+(?:available\s+)?offers?)\b"
)
_AVAILABLE_AVAILABILITY = re.compile(
    r"(?:(?:somente|apenas) [1-9][0-9]* (?:unidades? )?)?em\s+estoque[.!]?"
    r"|in\s+stock[.!]?|dispon[ií]vel[.!]?|available[.!]?"
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
    if _UNAVAILABLE_AVAILABILITY.search(normalized):
        return False
    if re.search(r"\b(?:não|nao)\b", normalized) and re.search(
        r"\b(?:dispon|estoque|ofert|venda)", normalized
    ):
        return False
    return True if _AVAILABLE_AVAILABILITY.fullmatch(normalized) else None


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
    title = _first_text(soup, ("#productTitle",))
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
        if soup.select_one("#buybox-see-all-buying-choices"):
            raise StoreParseError(
                "A Amazon mostrou apenas 'Ver todas as opções de compra'. "
                "A página não confirma preço e disponibilidade de uma oferta específica."
            )
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
    elif _EXPLICIT_SHIPPING_AMOUNT.fullmatch(normalized_shipping):
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
    )


class AmazonAdapter(HTTPStoreAdapter):
    name = "amazon"
    product_domains = {"amazon.com.br", "amazon.com"}
    domains = product_domains | {"a.co"}
    throttle_retry_delays: tuple[float, ...] = (4, 8)

    async def _get_page(self, url: str) -> tuple[str, str]:
        # Amazon intermittently answers 429/503; retries stay few, spaced and bounded.
        for delay in self.throttle_retry_delays:
            try:
                return await self._fetch_page(url)
            except StoreUnavailable:
                LOGGER.info("Amazon recusou a consulta; nova tentativa em %s s", delay)
                await asyncio.sleep(delay)
        return await self._fetch_page(url)

    async def _fetch_page(self, url: str) -> tuple[str, str]:
        current_url = validate_url(url)
        short_link = urlsplit(current_url).hostname == "a.co"
        if not short_link:
            current_url = validate_url(current_url, self.product_domains)
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
                    current_url = validate_url(
                        urljoin(current_url, location), self.product_domains
                    )
                    continue
                response.raise_for_status()
                if urlsplit(current_url).hostname == "a.co":
                    raise StoreError("O link curto da Amazon não redirecionou para uma oferta.")
                return response.text, current_url
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code
                if status in {429, 503}:
                    raise StoreUnavailable(
                        f"A Amazon recusou a consulta temporariamente (HTTP {status}). "
                        "Tente de novo em alguns minutos."
                    ) from exc
                raise StoreError(f"falha HTTP na Amazon: HTTP {status}") from exc
            except (httpx.HTTPError, TimeoutError, UnsafeURL, OSError) as exc:
                raise StoreError(f"falha HTTP na Amazon: {type(exc).__name__}") from exc
        raise StoreError("redirecionamentos demais na Amazon")

    async def search(self, query: str) -> list[StoreSearchResult]:
        url = f"https://www.amazon.com.br/s?k={quote_plus(query)}"
        html, _ = await self._get_page(url)
        return parse_search_results(html)[:10]

    async def get_offer(self, url: str) -> StoreOffer:
        safe_url = validate_url(url, self.domains)
        html, final_url = await self._get_page(safe_url)
        return parse_offer(html, final_url if urlsplit(safe_url).hostname == "a.co" else safe_url)
