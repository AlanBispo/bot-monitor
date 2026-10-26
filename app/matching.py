"""Explainable, deterministic product identity matching."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from decimal import Decimal

from .identity import COLORS, CONDITIONS, explicit_attributes, normalize_text
from .models import Product, StoreOffer
from .query import SearchQuery


@dataclass(frozen=True)
class MatchDecision:
    status: str
    score: int
    reason: str
    attributes: dict[str, str]


def _same(expected: str | int | None, actual: str | None) -> bool:
    if expected is None or actual is None:
        return True
    expected_text = re.sub(r"\s+", "", normalize_text(str(expected)))
    actual_text = re.sub(r"\s+", "", normalize_text(str(actual)))
    units = {
        "tb": 1024, "gb": 1, "mb": Decimal("0.0009765625"),
        "l": 1000, "litro": 1000, "litros": 1000, "ml": 1,
        "m": 1000, "cm": 10, "mm": 1,
        "kg": 1000, "g": 1,
    }
    unit_pattern = r"(\d+(?:[.,]\d+)?)(tb|gb|mb|l|litro|litros|ml|cm|mm|m|kg|g)"
    expected_unit = re.fullmatch(unit_pattern, expected_text)
    actual_unit = re.fullmatch(unit_pattern, actual_text)
    if expected_unit and actual_unit:
        def category(unit: str) -> str:
            if unit in {"tb", "gb", "mb"}:
                return "storage"
            if unit in {"l", "litro", "litros", "ml"}:
                return "volume"
            if unit in {"kg", "g"}:
                return "weight"
            return "size"

        if category(expected_unit.group(2)) != category(actual_unit.group(2)):
            return False
        expected_value = Decimal(expected_unit.group(1).replace(",", "."))
        actual_value = Decimal(actual_unit.group(1).replace(",", "."))
        return (
            expected_value * units[expected_unit.group(2)]
            == actual_value * units[actual_unit.group(2)]
        )
    expected_text = COLORS.get(expected_text, CONDITIONS.get(expected_text, expected_text))
    actual_text = COLORS.get(actual_text, CONDITIONS.get(actual_text, actual_text))
    return expected_text == actual_text


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
        if token not in ignored and not token.isdigit()
        and not re.fullmatch(r"\d+(?:gb|tb|mb|ml|l|cm|mm|m)", token)
    }


def offer_identity_attributes(offer: StoreOffer) -> dict[str, str]:
    attributes = explicit_attributes(offer.title)
    if offer.attributes:
        for key, value in offer.attributes.items():
            if key == "sku":
                continue
            normalized = normalize_text(value)
            if key in attributes and not _same(attributes[key], normalized):
                attributes[f"conflict_{key}"] = "true"
            attributes[key] = normalized
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
        "ram": query.ram,
        "weight": query.weight,
        "variant": query.variant,
        "accessory": query.accessory,
        "model_codes": query.model_codes,
    }
    if any(key.startswith("conflict_") for key in attributes):
        return MatchDecision("rejected", 0, "conflicting_identity_evidence", attributes)
    if attributes.get("accessory") != query.accessory and attributes.get("accessory"):
        return MatchDecision("rejected", 0, "hard_mismatch_item_kind", attributes)
    # Compare each number with its label: iPhone 15 is independent of Tela 6.1.
    actual_numbers = attributes.get("model_numbers", "").split()
    for term in (query.model_numbers or "").split():
        label, number = term.split(":", 1)
        values = {item.split(":", 1)[1] for item in actual_numbers
                  if item.startswith(f"{label}:")}
        if values and values != {number}:
            return MatchDecision("rejected", 0, "hard_mismatch_model", attributes)
        if not values:
            return MatchDecision("ambiguous", 0, "model_unknown", attributes)
    for key, wanted in expected.items():
        if wanted is not None and key in attributes and not _same(wanted, attributes[key]):
            reason_key = "model" if key == "model_codes" else key
            return MatchDecision("rejected", 0, f"hard_mismatch_{reason_key}", attributes)
    if query.model_codes and not attributes.get("model_codes"):
        return MatchDecision("ambiguous", 0, "model_unknown", attributes)
    if query.variant is None and attributes.get("variant"):
        return MatchDecision("ambiguous", 0, "variant_unspecified", attributes)
    if query.package_quantity is None and attributes.get("package_quantity"):
        return MatchDecision("ambiguous", 0, "package_quantity_unspecified", attributes)
    if query.condition is None and attributes.get("condition") in {
        "usado", "seminovo", "recondicionado", "danificado",
    }:
        return MatchDecision("ambiguous", 0, "condition_unspecified", attributes)
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


def match_product(product: Product, offer: StoreOffer) -> MatchDecision:
    """Compare a confirmed identity, including evidence learned at registration."""
    from .query import parse_query

    saved = json.loads(product.identity_json) if product.identity_json else {}
    original = match_offer(parse_query((product.source_query or product.name)[:200]), offer)
    if original.status == "rejected":
        return original
    known = {key: value for key, value in saved.get("attributes", {}).items()
             if key not in {"sku", "model_numbers"}}
    if (saved.get("manual_confirmation") and not known and saved.get("confirmed_title")
            and normalize_text(saved["confirmed_title"]) != normalize_text(offer.title)):
        return MatchDecision("ambiguous", 0, "manual_identity_changed",
                             offer_identity_attributes(offer))
    current = offer_identity_attributes(offer)
    for key, wanted in known.items():
        if key.startswith("conflict_"):
            return MatchDecision("rejected", 0, "saved_identity_conflict", current)
        if key in {"brand", "model"} and key not in current and re.search(
            rf"(?<!\w){re.escape(normalize_text(wanted))}(?!\w)", normalize_text(offer.title),
        ):
            current[key] = normalize_text(wanted)
        if key not in current:
            return MatchDecision("ambiguous", 0, f"saved_attribute_unknown_{key}", current)
        if not _same(str(wanted), current[key]):
            return MatchDecision("rejected", 0, f"hard_mismatch_{key}", current)
    query_text = saved.get("confirmed_title") or product.source_query or product.name
    decision = match_offer(parse_query(query_text[:200]), offer)
    if decision.status == "ambiguous" and saved.get("manual_confirmation") and known:
        return MatchDecision("matched", decision.score, "confirmed_saved_identity", current)
    return decision
