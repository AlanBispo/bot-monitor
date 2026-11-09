"""Money parsing and effective-price calculation."""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from .identity import explicit_attributes

CENT = Decimal("0.01")
UNITS = {
    "ml": ("L", Decimal("0.001")), "l": ("L", Decimal(1)), "litro": ("L", Decimal(1)),
    "litros": ("L", Decimal(1)), "g": ("kg", Decimal("0.001")), "kg": ("kg", Decimal(1)),
}
_BRAZILIAN_MONEY_RE = re.compile(
    r"[-+]?(?:\d+|\d{1,3}(?:\.\d{3})+)(?:,\d{1,2})?$"
)
_PLAIN_DECIMAL_RE = re.compile(r"[-+]?\d+\.\d{1,2}$")


class InvalidMoney(ValueError):
    """Raised for missing, malformed, or non-positive product money."""


def money(value: Decimal) -> Decimal:
    if not value.is_finite():
        raise InvalidMoney("valor monetário não finito")
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def format_brl(value: Decimal) -> str:
    text = f"{value:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"R$ {text}"


def unit_price(title: str, price: Decimal) -> str | None:
    """Price per litre or kilo when the title states one volume or weight."""
    attributes = explicit_attributes(title)
    measure = attributes.get("volume") or attributes.get("weight")
    if measure is None:
        return None
    number, unit = measure.split()
    label, factor = UNITS[unit]
    total = Decimal(number) * factor * int(attributes.get("package_quantity", "1"))
    return f"{format_brl(money(price / total))}/{label}" if total > 0 else None


def parse_money(value: str | Decimal | int | float, *, allow_zero: bool = False) -> Decimal:
    """Parse common Brazilian or plain decimal notation without accepting junk."""
    if isinstance(value, Decimal):
        parsed = value
    elif isinstance(value, (int, float)):
        parsed = Decimal(str(value))
    elif isinstance(value, str):
        text = " ".join(value.strip().split())
        text = text.removeprefix("R$").strip()
        if not (
            _BRAZILIAN_MONEY_RE.fullmatch(text) or _PLAIN_DECIMAL_RE.fullmatch(text)
        ):
            raise InvalidMoney("valor monetário inválido")
        if "," in text:
            integer, fraction = text.split(",")
            parsed = Decimal(f"{integer.replace('.', '')}.{fraction.ljust(2, '0')}")
        else:
            if _PLAIN_DECIMAL_RE.fullmatch(text):
                parsed = Decimal(text)
            else:
                parsed = Decimal(text.replace(".", ""))
    else:
        raise InvalidMoney("valor monetário inválido")
    try:
        result = money(parsed)
    except (InvalidOperation, ValueError):
        raise InvalidMoney("valor monetário inválido") from None
    if result < 0 or (result == 0 and not allow_zero):
        raise InvalidMoney("valor monetário deve ser positivo")
    return result


@dataclass(frozen=True)
class PricingResult:
    product_price: Decimal
    shipping_price: Decimal | None
    confirmed_discount: Decimal
    # Conditional discount is an additional discount on top of the confirmed one.
    possible_discount: Decimal | None
    effective_price: Decimal | None
    possible_effective_price: Decimal | None


def calculate_price(
    product_price: Decimal,
    shipping_price: Decimal | None,
    confirmed_discount: Decimal | None = None,
    possible_discount: Decimal | None = None,
) -> PricingResult:
    """Calculate guaranteed and conditional prices, preserving unknown freight."""
    product = parse_money(product_price)
    shipping = None if shipping_price is None else parse_money(shipping_price, allow_zero=True)
    confirmed = (
        Decimal("0.00")
        if confirmed_discount is None
        else parse_money(confirmed_discount, allow_zero=True)
    )
    possible = (
        None if possible_discount is None else parse_money(possible_discount, allow_zero=True)
    )
    if confirmed > product:
        raise InvalidMoney("desconto confirmado maior que o preço")
    if possible is not None and confirmed + possible > product + (shipping or Decimal("0.00")):
        raise InvalidMoney("descontos combinados maiores que o preço")
    guaranteed = None if shipping is None else money(product + shipping - confirmed)
    conditional = (
        None
        if shipping is None or possible is None
        else money(product + shipping - confirmed - possible)
    )
    return PricingResult(product, shipping, confirmed, possible, guaranteed, conditional)
