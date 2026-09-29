"""Deterministic parsing of user product queries."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass

from .pricing import InvalidMoney, parse_money

MAX_QUERY_LENGTH = 200
_QUOTED = re.compile(r'(["\u201c\u201d])([^"\u201c\u201d]+)["\u201c\u201d]')
_MONEY = r"(?:R\$\s*)?(?:\d{1,3}(?:\.\d{3})+|\d+)(?:,\d{1,2})?"
_MAX_PRICE = re.compile(
    rf"(?:até|ate|máximo|max(?:imo)?|no\s+máximo|no\s+maximo)\s*[:=]?\s*({_MONEY})(?=$|[\s;!?)]|\.(?=$|\s))",
    re.IGNORECASE,
)
_MAX_INTRO = re.compile(r"\b(?:até|ate|máximo|maximo|max|no\s+máximo|no\s+maximo)\b", re.I)
_CONDITION = re.compile(r"\b(novo|nova|usado|usada|seminovo|seminova|recondicionado)\b", re.I)
_COLOR = re.compile(
    r"\b(preto|preta|black|branco|branca|white|azul|blue|vermelho|vermelha|red|"
    r"verde|green|amarelo|amarela|yellow|rosa|pink|cinza|gray|grey|prata|silver)\b",
    re.I,
)
_CAPACITY = re.compile(r"\b(\d+(?:[.,]\d+)?)\s*(gb|tb|mb|g[b]?|t[b]?)\b", re.I)
_VOLUME = re.compile(r"\b(\d+(?:[.,]\d+)?)\s*(ml|l|litros?)\b", re.I)
_SIZE = re.compile(r"\b(\d+(?:[.,]\d+)?)\s*(cm|mm|m|p|m[eé]dio|g|gg|xg)\b", re.I)
_CLOTHING_SIZE = re.compile(r"\btamanho\s+(p|m|g|gg|xg|m[eé]dio)\b", re.I)
_QUANTITY = re.compile(
    r"\b(?:com|pack(?:\s+com)?|kit(?:\s+com)?|caixa\s+com)\s*(\d+)\s*(?:un(?:idades?)?|pcs?|peças?)?\b",
    re.I,
)
_MODEL = re.compile(
    r"\b(?:modelo|model|ref(?:er[eê]ncia)?|c[oó]d(?:igo)?)\s*[:#-]?\s*([A-Za-z0-9][A-Za-z0-9._/-]{0,40})\b",
    re.I,
)


class InvalidQuery(ValueError):
    """Raised for an empty or oversized search request."""


@dataclass(frozen=True)
class SearchQuery:
    original_text: str
    normalized_text: str
    terms: tuple[str, ...]
    quoted_phrases: tuple[str, ...]
    model: str | None = None
    capacity: str | None = None
    volume: str | None = None
    size: str | None = None
    package_quantity: int | None = None
    condition: str | None = None
    color: str | None = None
    max_price: str | None = None

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, sort_keys=True)

    @property
    def variants(self) -> tuple[str, ...]:
        # The full request retains model and variant constraints. A phrase-only
        # variant would discard them and could discover a different product.
        return (self.normalized_text,)


def _normalize(text: str) -> str:
    return " ".join(text.replace("\u2013", "-").replace("\u2014", "-").split())


def _unit_value(match: re.Match[str] | None) -> str | None:
    if not match:
        return None
    return f"{match.group(1).replace(',', '.')} {match.group(2).lower()}"


def parse_query(text: str) -> SearchQuery:
    if not isinstance(text, str):
        raise InvalidQuery("a busca deve ser texto")
    original = text.strip()
    if not original:
        raise InvalidQuery("informe um produto para buscar")
    if len(original) > MAX_QUERY_LENGTH:
        raise InvalidQuery(f"a busca deve ter no máximo {MAX_QUERY_LENGTH} caracteres")
    normalized = _normalize(original)
    quoted = tuple(match.group(2).strip() for match in _QUOTED.finditer(normalized))
    max_match = _MAX_PRICE.search(normalized)
    if _MAX_INTRO.search(normalized) and max_match is None:
        raise InvalidQuery("preço máximo inválido")
    max_price = None
    if max_match:
        try:
            max_price = str(parse_money(max_match.group(1)))
        except InvalidMoney as exc:
            raise InvalidQuery("preço máximo inválido") from exc
    condition_match = _CONDITION.search(normalized)
    color_match = _COLOR.search(normalized)
    model_match = _MODEL.search(normalized)
    quantity_match = _QUANTITY.search(normalized)
    clothing_size = _CLOTHING_SIZE.search(normalized)
    size = (
        clothing_size.group(1).casefold()
        if clothing_size else _unit_value(_SIZE.search(normalized))
    )
    terms = tuple(token for token in re.findall(r"[\wÀ-ÿ./-]+", normalized) if token)
    return SearchQuery(
        original_text=original,
        normalized_text=normalized,
        terms=terms,
        quoted_phrases=quoted,
        model=model_match.group(1).casefold() if model_match else None,
        capacity=_unit_value(_CAPACITY.search(normalized)),
        volume=_unit_value(_VOLUME.search(normalized)),
        size=size,
        package_quantity=int(quantity_match.group(1)) if quantity_match else None,
        condition=condition_match.group(1).casefold() if condition_match else None,
        color=color_match.group(1).casefold() if color_match else None,
        max_price=max_price,
    )
