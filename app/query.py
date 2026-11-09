"""Deterministic parsing of user product queries."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass

from .identity import explicit_attributes
from .pricing import InvalidMoney, parse_money

MAX_QUERY_LENGTH = 200
_MONEY = r"(?:R\$\s*)?(?:\d{1,3}(?:\.\d{3})+|\d+)(?:,\d{1,2})?"
_PRICE_INTRO = r"\b(?:até|ate|máximo|maximo|no\s+máximo|no\s+maximo|max(?=\s*[:=]|\s+R\$))\b"
_MAX_PRICE = re.compile(
    rf"{_PRICE_INTRO}\s*[:=]?\s*({_MONEY})(?=$|[\s;!?)]|\.(?=$|\s))",
    re.IGNORECASE,
)


class InvalidQuery(ValueError):
    """Raised for an empty or oversized search request."""


@dataclass(frozen=True)
class SearchQuery:
    original_text: str
    normalized_text: str
    model: str | None = None
    capacity: str | None = None
    volume: str | None = None
    size: str | None = None
    package_quantity: int | None = None
    condition: str | None = None
    color: str | None = None
    max_price: str | None = None
    ram: str | None = None
    weight: str | None = None
    variant: str | None = None
    accessory: str | None = None
    model_numbers: str | None = None
    model_codes: str | None = None

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, sort_keys=True)


def _normalize(text: str) -> str:
    return " ".join(text.replace("\u2013", "-").replace("\u2014", "-").split())


def parse_query(text: str) -> SearchQuery:
    original = text.strip()
    if not original:
        raise InvalidQuery("informe um produto para buscar")
    if len(original) > MAX_QUERY_LENGTH:
        raise InvalidQuery(f"a busca deve ter no máximo {MAX_QUERY_LENGTH} caracteres")
    normalized = _normalize(original)
    max_match = _MAX_PRICE.search(normalized)
    max_price = None
    if max_match:
        try:
            max_price = str(parse_money(max_match.group(1)))
        except InvalidMoney as exc:
            raise InvalidQuery("preço máximo inválido") from exc
    attributes = explicit_attributes(_MAX_PRICE.sub(" ", normalized))
    if any(key.startswith("conflict_") for key in attributes):
        raise InvalidQuery("atributos conflitantes ou ambíguos; especifique a variante")
    return SearchQuery(
        original_text=original,
        normalized_text=normalized,
        model=attributes.get("model"),
        capacity=attributes.get("capacity"),
        volume=attributes.get("volume"),
        size=attributes.get("size"),
        package_quantity=int(attributes["package_quantity"]) if "package_quantity" in attributes
        else None,
        condition=attributes.get("condition"),
        color=attributes.get("color"),
        max_price=max_price,
        ram=attributes.get("ram"),
        weight=attributes.get("weight"),
        variant=attributes.get("variant"),
        accessory=attributes.get("accessory"),
        model_numbers=attributes.get("model_numbers"),
        model_codes=attributes.get("model_codes"),
    )
