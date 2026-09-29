"""Conservative JSON-LD Product/Offer extraction for public pages."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from ..models import StoreOffer
from ..pricing import InvalidMoney, parse_money
from .base import StoreParseError


def _schema_type(value: object) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip().rstrip("/").rsplit("/", 1)[-1].rsplit("#", 1)[-1].casefold()


def _jsonld_nodes(value) -> Iterable[dict]:
    if isinstance(value, dict):
        if isinstance(value.get("@graph"), list):
            yield from _jsonld_nodes(value["@graph"])
        else:
            yield value
    elif isinstance(value, list):
        for item in value:
            yield from _jsonld_nodes(item)


def _availability(value: str | None) -> bool | None:
    if not isinstance(value, str) or not value:
        return None
    # Schema.org values are URLs or enum names. Match the final enum token exactly:
    # substring checks incorrectly classify e.g. `NotInStock` as `InStock`.
    normalized = value.strip().casefold().rstrip("/").rsplit("/", 1)[-1]
    if normalized in {
        "outofstock", "soldout", "unavailable", "discontinued", "notinstock",
        "esgotado", "indisponivel",
    }:
        return False
    if normalized in {
        "instock", "in_stock", "available", "limitedavailability", "instoreonly",
        "onlineonly", "disponivel",
    }:
        return True
    return None


def _first_price(offers) -> tuple[str | None, str | None]:
    values = offers if isinstance(offers, list) else [offers]
    found: list[tuple[str, str | None]] = []
    for offer in values:
        if not isinstance(offer, dict):
            continue
        price = offer.get("price")
        if price is None and offer.get("lowPrice") == offer.get("highPrice"):
            price = offer.get("lowPrice")
        if price is not None:
            currency = offer.get("priceCurrency") or offer.get("currency")
            found.append((str(price), currency if isinstance(currency, str) else None))
    if (
        not found
        or len({item[0] for item in found}) > 1
        or len({(item[1] or "").upper() for item in found}) > 1
    ):
        return None, None
    return found[0]


def parse_jsonld_offer(html: str, url: str, store: str) -> StoreOffer:
    soup = BeautifulSoup(html, "html.parser")
    products: list[dict] = []
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            payload = json.loads(script.string or script.get_text())
        except (TypeError, json.JSONDecodeError):
            continue
        for node in _jsonld_nodes(payload):
            kind = node.get("@type")
            kinds = kind if isinstance(kind, list) else [kind]
            if any(_schema_type(item) == "product" for item in kinds):
                if node not in products:
                    products.append(node)
    if not products:
        raise StoreParseError("dados estruturados de produto ausentes")
    if len(products) != 1:
        raise StoreParseError("múltiplos produtos estruturados ambíguos")
    product = products[0]
    name = product.get("name")
    offer_data = product.get("offers")
    values = offer_data if isinstance(offer_data, list) else [offer_data]
    if not isinstance(name, str) or not name.strip() or not all(
        isinstance(item, dict) for item in values
    ):
        raise StoreParseError("produto ou oferta estruturada ausente")
    if store == "generic" and any(
        _schema_type(item.get("@type")) not in {"offer", "aggregateoffer"}
        for item in values
    ):
        raise StoreParseError("tipo de oferta estruturada inválido")
    statuses = {
        _availability(item.get("availability"))
        for item in values
    }
    if statuses == {False}:
        return StoreOffer(
            store, name.strip(), url, None, None, seller=None, available=False,
            observation_status="unavailable", attributes=_attributes(product),
        )
    if statuses != {True}:
        raise StoreParseError("disponibilidade estruturada ambígua")
    raw_price, currency = _first_price(offer_data)
    if not raw_price:
        raise StoreParseError("preço estruturado ausente")
    if not isinstance(currency, str) or currency.upper() != "BRL":
        raise StoreParseError("moeda da oferta não é BRL")
    try:
        price = parse_money(raw_price)
    except InvalidMoney as exc:
        raise StoreParseError("preço estruturado inválido") from exc
    # Only accept shipping that is unambiguous for this single offer and has no
    # destination condition. Multi-seller arrays can have different freight.
    shipping = None
    if len(values) == 1 and isinstance(values[0], dict):
        details = values[0].get("shippingDetails")
        rate = details.get("shippingRate") if isinstance(details, dict) else None
        if (
            isinstance(details, dict)
            and not details.get("shippingDestination")
            and isinstance(rate, dict)
        ):
            shipping_currency = rate.get("currency") or rate.get("priceCurrency") or currency
            if (
                rate.get("value") is not None
                and isinstance(shipping_currency, str)
                and shipping_currency.upper() in {"BRL", "R$", "REAL", "REAIS"}
            ):
                try:
                    shipping = parse_money(rate["value"], allow_zero=True)
                except InvalidMoney:
                    shipping = None
    canonical = product.get("url") if isinstance(product.get("url"), str) else None
    return StoreOffer(
        store, name.strip(), url, price, shipping,
        seller=None, available=True, observation_status="available",
        attributes=_attributes(product),
        canonical_url=urljoin(url, canonical) if canonical else None,
    )


def _attributes(product: dict) -> dict[str, str]:
    attrs: dict[str, str] = {}
    for key in ("brand", "model", "sku", "color", "size"):
        value = product.get(key)
        if isinstance(value, dict):
            value = value.get("name")
        if isinstance(value, str) and value.strip():
            attrs[key] = value.strip()
    return attrs


def _visible_price(text: str | None):
    if not text:
        return None
    match = re.search(r"R\$\s*[\d.]+(?:,\d{1,2})?", text)
    if not match:
        return None
    try:
        return parse_money(match.group(0))
    except InvalidMoney:
        return None
