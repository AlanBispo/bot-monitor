"""Explainable, deterministic product identity matching."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from decimal import Decimal

from .models import StoreOffer
from .query import SearchQuery


@dataclass(frozen=True)
class MatchDecision:
    status: str
    score: int
    reason: str
    attributes: dict[str, str]


def normalize_text(value: str) -> str:
    folded = unicodedata.normalize("NFKD", value.casefold())
    return "".join(char for char in folded if not unicodedata.combining(char))


def _title_attributes(title: str) -> dict[str, str]:
    text = normalize_text(title)
    attrs: dict[str, str] = {}
    model = re.search(
        r"\b(?:modelo|model|ref|referencia|codigo)\s*[:#-]?\s*([a-z0-9][a-z0-9._/-]{0,40})\b",
        text,
    )
    if model:
        attrs["model"] = model.group(1)
    capacity = re.search(r"\b(\d+(?:[.,]\d+)?)\s*(gb|tb|mb)\b", text)
    if capacity:
        attrs["capacity"] = f"{capacity.group(1).replace(',', '.')} {capacity.group(2)}"
    volume = re.search(r"\b(\d+(?:[.,]\d+)?)\s*(ml|l|litros?)\b", text)
    if volume:
        attrs["volume"] = f"{volume.group(1).replace(',', '.')} {volume.group(2)}"
    size = re.search(r"\b(\d+(?:[.,]\d+)?)\s*(cm|mm|m)\b", text)
    if size:
        attrs["size"] = f"{size.group(1).replace(',', '.')} {size.group(2)}"
    clothing_size = re.search(r"\btamanho\s+(p|m|g|gg|xg|medio)\b", text)
    if clothing_size:
        attrs["size"] = clothing_size.group(1)
    color = re.search(
        r"\b(preto|preta|black|branco|branca|white|azul|blue|vermelho|vermelha|red|"
        r"verde|green|amarelo|amarela|yellow|rosa|pink|cinza|gray|grey|prata|silver)\b",
        text,
    )
    if color:
        attrs["color"] = color.group(1)
    quantity = re.search(r"\b(?:pack|kit|com)\s*(\d+)\b", text)
    if quantity:
        attrs["package_quantity"] = quantity.group(1)
    for condition in ("novo", "nova", "usado", "usada", "seminovo", "seminova", "recondicionado"):
        if re.search(rf"\b{condition}\b", text):
            attrs["condition"] = {
                "nova": "novo", "usada": "usado", "seminova": "seminovo"
            }.get(condition, condition)
            break
    return attrs


def _same(expected: str | int | None, actual: str | None) -> bool:
    if expected is None or actual is None:
        return True
    expected_text = re.sub(r"\s+", "", normalize_text(str(expected)))
    actual_text = re.sub(r"\s+", "", normalize_text(str(actual)))
    units = {
        "tb": 1024, "gb": 1, "mb": Decimal("0.0009765625"),
        "l": 1000, "litro": 1000, "litros": 1000, "ml": 1,
        "m": 1000, "cm": 10, "mm": 1,
    }
    unit_pattern = r"(\d+(?:[.,]\d+)?)(tb|gb|mb|l|litro|litros|ml|cm|mm|m)"
    expected_unit = re.fullmatch(unit_pattern, expected_text)
    actual_unit = re.fullmatch(unit_pattern, actual_text)
    if expected_unit and actual_unit:
        def category(unit: str) -> str:
            if unit in {"tb", "gb", "mb"}:
                return "storage"
            if unit in {"l", "litro", "litros", "ml"}:
                return "volume"
            return "size"

        if category(expected_unit.group(2)) != category(actual_unit.group(2)):
            return False
        expected_value = Decimal(expected_unit.group(1).replace(",", "."))
        actual_value = Decimal(actual_unit.group(1).replace(",", "."))
        return (
            expected_value * units[expected_unit.group(2)]
            == actual_value * units[actual_unit.group(2)]
        )
    conditions = {"nova": "novo", "usada": "usado", "seminova": "seminovo"}
    colors = {
        "preta": "preto", "black": "preto", "branca": "branco", "white": "branco",
        "blue": "azul", "vermelha": "vermelho", "red": "vermelho",
        "green": "verde", "amarela": "amarelo", "yellow": "amarelo",
        "pink": "rosa", "gray": "cinza", "grey": "cinza", "silver": "prata",
    }
    expected_text = conditions.get(expected_text, expected_text)
    actual_text = conditions.get(actual_text, actual_text)
    expected_text = colors.get(expected_text, expected_text)
    actual_text = colors.get(actual_text, actual_text)
    return expected_text == actual_text


def _model_tokens(text: str) -> set[str]:
    tokens = re.findall(r"\b[a-z][a-z0-9]*(?:[-/][a-z0-9]+)*\b", normalize_text(text))
    return {
        token for token in tokens
        if any(char.isdigit() for char in token)
        and not re.fullmatch(r"\d+(?:gb|tb|mb|ml|cm|mm)", token)
    }


def _lexical_tokens(text: str) -> set[str]:
    ignored = {
        "modelo", "model", "ref", "referencia", "codigo", "tamanho", "pack", "kit",
        "com", "unidades", "novo", "nova", "usado", "usada", "seminovo", "seminova",
        "recondicionado", "ate", "maximo", "max", "preco", "r",
        "preto", "preta", "black", "branco", "branca", "white", "azul", "blue",
        "vermelho", "vermelha", "red", "verde", "green", "amarelo", "amarela",
        "yellow", "rosa", "pink", "cinza", "gray", "grey", "prata", "silver",
    }
    return {
        token for token in re.findall(r"[a-z0-9]+", normalize_text(text))
        if len(token) > 1 and token not in ignored and not token.isdigit()
        and not re.fullmatch(r"\d+(?:gb|tb|mb|ml|l|cm|mm|m)", token)
    }


def offer_identity_attributes(offer: StoreOffer) -> dict[str, str]:
    attributes = dict(_title_attributes(offer.title))
    if offer.attributes:
        attributes.update({key: normalize_text(value) for key, value in offer.attributes.items()})
    return attributes


def match_offer(query: SearchQuery, offer: StoreOffer) -> MatchDecision:
    attributes = offer_identity_attributes(offer)
    expected = {
        "model": query.model,
        "capacity": query.capacity,
        "volume": query.volume,
        "size": query.size,
        "package_quantity": query.package_quantity,
        "condition": query.condition,
        "color": query.color,
    }
    for key, wanted in expected.items():
        if wanted is not None and key in attributes and not _same(wanted, attributes[key]):
            return MatchDecision("rejected", 0, f"hard_mismatch_{key}", attributes)
    query_models = _model_tokens(query.normalized_text)
    actual_models = _model_tokens(attributes.get("model", offer.title))
    if query_models and actual_models and query_models.isdisjoint(actual_models):
        return MatchDecision("rejected", 0, "hard_mismatch_model", attributes)
    if query_models and not actual_models:
        return MatchDecision("ambiguous", 0, "model_unknown", attributes)
    title_tokens = _lexical_tokens(offer.title)
    query_tokens = _lexical_tokens(query.normalized_text)
    score = len(title_tokens & query_tokens)
    if query_tokens and score == 0:
        return MatchDecision("rejected", score, "no_query_terms_in_title", attributes)
    required_known = sum(value is not None for value in expected.values())
    matched_known = sum(
        wanted is not None and key in attributes and _same(wanted, attributes[key])
        for key, wanted in expected.items()
    )
    if required_known and matched_known < required_known:
        return MatchDecision("ambiguous", score, "required_attribute_unknown", attributes)
    if score < max(1, min(2, len(query_tokens))):
        return MatchDecision("ambiguous", score, "low_lexical_score", attributes)
    return MatchDecision("matched", score, "matched_explicit_constraints", attributes)
