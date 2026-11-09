"""Shared deterministic extraction of explicit identity evidence, never guesses."""

from __future__ import annotations

import re
import unicodedata

MEASURE = re.compile(r"\b(\d+(?:[.,]\d+)?)\s*(gb|tb|mb|ml|litros?|l|cm|mm|kg|g|m)\b", re.I)
MODEL = re.compile(
    r"\b(?:modelo|model|ref(?:erencia)?|codigo)\s*[:#-]?\s*([a-z0-9][a-z0-9._/-]{0,40})\b"
)
QUANTITY = re.compile(
    r"\b(?:com|pack(?:\s+com)?|kit(?:\s+com)?|caixa\s+com)\s*(?P<count>\d+)"
    r"\s*(?:un(?:idades?)?|pcs?|pecas?)?\b|"
    r"\b(?P<units>\d+)\s*(?:unidades?|und|pcs?|pecas?)\b"
)
CONDITIONS = {
    "novo": "novo",
    "nova": "novo",
    "new": "novo",
    "usado": "usado",
    "usada": "usado",
    "used": "usado",
    "seminovo": "seminovo",
    "seminova": "seminovo",
    "recondicionado": "recondicionado",
    "recondicionada": "recondicionado",
    "refurbished": "recondicionado",
}
COLORS = {
    "preto": "preto",
    "preta": "preto",
    "black": "preto",
    "branco": "branco",
    "branca": "branco",
    "white": "branco",
    "azul": "azul",
    "blue": "azul",
    "vermelho": "vermelho",
    "vermelha": "vermelho",
    "red": "vermelho",
    "verde": "verde",
    "green": "verde",
    "amarelo": "amarelo",
    "amarela": "amarelo",
    "yellow": "amarelo",
    "rosa": "rosa",
    "pink": "rosa",
    "cinza": "cinza",
    "gray": "cinza",
    "grey": "cinza",
    "prata": "prata",
    "silver": "prata",
}
VARIANTS = {"air", "pro", "max", "plus", "ultra", "mini", "lite", "fe", "se"}
ACCESSORIES = {
    "capa": "capa",
    "capinha": "capa",
    "case": "capa",
    "pelicula": "pelicula",
    "carregador": "carregador",
    "cabo": "cabo",
    "suporte": "suporte",
    "adaptador": "adaptador",
}


def normalize_text(value: str) -> str:
    folded = unicodedata.normalize("NFKD", value.casefold())
    return "".join(char for char in folded if not unicodedata.combining(char))


def explicit_attributes(value: str) -> dict[str, str]:
    text = normalize_text(value)
    attrs: dict[str, str] = {}

    def single(key, values):
        values = set(values)
        if len(values) == 1:
            attrs[key] = values.pop()
        elif values:
            attrs[f"conflict_{key}"] = "true"

    model = MODEL.search(text)
    if model:
        attrs["model"] = model.group(1)
    measures: dict[str, list[str]] = {}
    for match in MEASURE.finditer(text):
        number, unit = match.groups()
        if unit in {"gb", "tb", "mb"}:
            before, after = text[max(0, match.start() - 15) : match.start()], text[match.end() :]
            prefix_ram = re.search(r"\bram\s*$", before) and not re.search(
                r"\d\s*(?:gb|tb|mb)\s*(?:de\s+)?ram\s*$",
                before,
            )
            is_ram = bool(prefix_ram or re.match(r"\s*(?:de\s+)?ram\b", after))
            key = "ram" if is_ram else "capacity"
        elif unit in {"ml", "l", "litro", "litros"}:
            key = "volume"
        elif unit in {"kg", "g"}:
            key = "weight"
        else:
            key = "size"
        measures.setdefault(key, []).append(f"{number.replace(',', '.')} {unit}")
    for key, values in measures.items():
        single(key, values)
    clothing = re.search(r"\btamanho\s+(p|m|g|gg|xg|medio)\b", text)
    if clothing:
        attrs["size"] = clothing.group(1)
    quantity = QUANTITY.search(text)
    if quantity:
        attrs["package_quantity"] = quantity.group("count") or quantity.group("units")
    tokens = set(re.findall(r"\b[a-z]+\b", text))
    single("condition", (CONDITIONS[token] for token in tokens & CONDITIONS.keys()))
    colors = {COLORS[token] for token in tokens & COLORS.keys()}
    slash_groups = [
        {COLORS[part.strip()] for part in match.group(0).split("/")}
        for match in re.finditer(r"\b[a-z]+(?:\s*/\s*[a-z]+)+\b", text)
        if all(part.strip() in COLORS for part in match.group(0).split("/"))
    ]
    if len(colors) > 1 and colors in slash_groups:
        # "Branco/Cinza" names one two-tone item; separate colors stay a conflict.
        attrs["color"] = "/".join(sorted(colors))
    else:
        single("color", colors)
    single("accessory", (ACCESSORIES[token] for token in tokens & ACCESSORIES.keys()))
    variants = tokens & VARIANTS
    if variants:
        attrs["variant"] = " ".join(sorted(variants))
    unmeasured = QUANTITY.sub(" ", MEASURE.sub(" ", text))
    numbers = re.findall(r"\b([a-z][a-z0-9]*)\s+(\d+(?:[.,]\d+)?)\b", unmeasured)
    if numbers:
        attrs["model_numbers"] = " ".join(sorted({
            f"{label}:{number.replace(',', '.')}" for label, number in numbers
        }))
    codes = {
        token
        for token in re.findall(r"\b[a-z][a-z0-9]*(?:[-/][a-z0-9]+)*\b", text)
        if any(char.isdigit() for char in token)
    }
    if codes:
        attrs["model_codes"] = " ".join(sorted(codes))
    return attrs
