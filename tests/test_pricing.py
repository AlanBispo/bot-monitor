from decimal import Decimal

import pytest

from app.pricing import InvalidMoney, calculate_price, money, parse_money


def test_parse_brazilian_money_and_rounding():
    assert parse_money("R$ 29,90") == Decimal("29.90")
    assert parse_money("0,01") == Decimal("0.01")
    assert parse_money("1.234,56") == Decimal("1234.56")
    assert parse_money("1234.56") == Decimal("1234.56")
    assert money(Decimal("1.005")) == Decimal("1.01")


def test_effective_price_includes_shipping_and_confirmed_discount():
    result = calculate_price(Decimal("29.90"), Decimal("4.99"), Decimal("3.00"))
    assert result.effective_price == Decimal("31.89")


def test_possible_discount_is_not_guaranteed():
    result = calculate_price(Decimal("29.90"), Decimal("0"), possible_discount=Decimal("5"))
    assert result.effective_price == Decimal("29.90")
    assert result.possible_effective_price == Decimal("24.90")


def test_possible_discount_is_additional_to_confirmed_discount():
    result = calculate_price(Decimal("100.00"), Decimal("10.00"), Decimal("5.00"), Decimal("10.00"))
    assert result.effective_price == Decimal("105.00")
    assert result.possible_effective_price == Decimal("95.00")


def test_combined_discounts_cannot_produce_negative_price():
    with pytest.raises(InvalidMoney):
        calculate_price(Decimal("10.00"), Decimal("0"), Decimal("6.00"), Decimal("5.00"))


def test_unknown_shipping_does_not_assume_free():
    result = calculate_price(Decimal("29.90"), None)
    assert result.effective_price is None


@pytest.mark.parametrize(
    "value",
    [
        "",
        "0",
        "-1",
        "abc",
        "R$ 1,999.90",
        "1.2,34",
        "1..234,56",
        "12.34,56",
        "1234.567",
        "1.234.56,78",
        "123.4567,89",
        "12.345.67",
    ],
)
def test_invalid_or_ambiguous_money_is_rejected(value):
    with pytest.raises(InvalidMoney):
        parse_money(value)


@pytest.mark.parametrize("value", [Decimal("NaN"), Decimal("Infinity"), Decimal("-Infinity")])
def test_non_finite_money_is_rejected_as_invalid_money(value):
    with pytest.raises(InvalidMoney):
        parse_money(value)
