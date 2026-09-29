from decimal import Decimal

import pytest

from app.stores.base import StoreParseError
from app.stores.magalu import parse_offer as parse_magalu_offer
from app.stores.mercadolivre import parse_offer as parse_mercado_offer
from app.stores.structured import _availability


@pytest.mark.parametrize(
    ("parser", "store", "url"),
    [
        (parse_mercado_offer, "mercadolivre", "https://www.mercadolivre.com.br/item/1"),
        (parse_magalu_offer, "magalu", "https://www.magazineluiza.com.br/item/1"),
    ],
)
def test_required_store_jsonld_offer_parser(parser, store, url):
    html = """
    <script type="application/ld+json">
    {"@context":"https://schema.org","@type":"Product","name":"Notebook modelo Z 512GB",
      "model":"Z","offers":{"@type":"Offer","price":"1234.56","priceCurrency":"BRL",
      "availability":"https://schema.org/InStock","shippingDetails":{"shippingRate":{"value":"12.90"}}}}
    </script>
    """
    offer = parser(html, url)
    assert offer.store == store
    assert offer.product_price == Decimal("1234.56")
    assert offer.shipping_price == Decimal("12.90")
    assert offer.available


@pytest.mark.parametrize("parser", [parse_mercado_offer, parse_magalu_offer])
def test_required_store_changed_or_unavailable_page_fails_closed(parser):
    offer = None
    with pytest.raises(StoreParseError):
        offer = parser("<html><body>captcha or changed page</body></html>", "https://store.example/item")
    assert offer is None


@pytest.mark.parametrize("status", ["NotInStock", "https://schema.org/NotInStock", "Unknown"])
def test_jsonld_availability_is_fail_closed_for_non_affirmative_statuses(status):
    assert _availability(status) is not True


@pytest.mark.parametrize("parser", [parse_mercado_offer, parse_magalu_offer])
def test_structured_offer_requires_explicit_brl_currency(parser):
    html = '''
    <script type="application/ld+json">
    {"@type":"Product","name":"Produto","offers":{"@type":"Offer",
     "price":"10.00","availability":"https://schema.org/InStock"}}
    </script>
    '''
    with pytest.raises(StoreParseError):
        parser(html, "https://store.example/item")


@pytest.mark.parametrize("parser", [parse_mercado_offer, parse_magalu_offer])
def test_structured_offer_rejects_conflicting_currency_offers(parser):
    html = '''
    <script type="application/ld+json">
    {"@type":"Product","name":"Produto","offers":[
     {"@type":"Offer","price":"10.00","priceCurrency":"BRL",
      "availability":"https://schema.org/InStock"},
     {"@type":"Offer","price":"10.00","priceCurrency":"USD",
      "availability":"https://schema.org/InStock"}]}
    </script>
    '''
    with pytest.raises(StoreParseError):
        parser(html, "https://store.example/item")


@pytest.mark.parametrize(
    ("parser", "store", "url"),
    [
        (parse_mercado_offer, "mercadolivre", "https://www.mercadolivre.com.br/item/1"),
        (parse_magalu_offer, "magalu", "https://www.magazineluiza.com.br/item/1"),
    ],
)
def test_required_store_unavailable_fixture_never_keeps_displayed_price(parser, store, url):
    html = '''
    <script type="application/ld+json">
    {"@type":"Product","name":"Produto","offers":{"@type":"Offer",
     "price":"1.00","priceCurrency":"BRL",
     "availability":"https://schema.org/OutOfStock"}}
    </script>
    '''
    offer = parser(html, url)
    assert offer.store == store
    assert offer.is_available is False
    assert offer.product_price is None


@pytest.mark.parametrize("parser", [parse_mercado_offer, parse_magalu_offer])
def test_required_store_malformed_price_fixture_fails_closed(parser):
    html = '''
    <script type="application/ld+json">
    {"@type":"Product","name":"Produto","offers":{"@type":"Offer",
     "price":"1.2,34","priceCurrency":"BRL",
     "availability":"https://schema.org/InStock"}}
    </script>
    '''
    with pytest.raises(StoreParseError):
        parser(html, "https://store.example/item")


def test_jsonld_does_not_treat_destination_dependent_shipping_as_universal():
    html = '''
    <script type="application/ld+json">
    {"@type":"Product","name":"Produto","offers":{"@type":"Offer",
     "price":"10.00","priceCurrency":"BRL","availability":"https://schema.org/InStock",
     "shippingDetails":{"shippingDestination":{"addressCountry":"BR"},
      "shippingRate":{"value":"0.00","currency":"BRL"}}}}
    </script>
    '''
    offer = parse_magalu_offer(html, "https://store.example/item")
    assert offer.shipping_price is None


def test_jsonld_does_not_choose_shipping_from_multiple_seller_offers():
    html = '''
    <script type="application/ld+json">
    {"@type":"Product","name":"Produto","offers":[
     {"@type":"Offer","price":"10.00","priceCurrency":"BRL",
      "availability":"https://schema.org/InStock",
      "shippingDetails":{"shippingRate":{"value":"0.00","currency":"BRL"}}},
     {"@type":"Offer","price":"10.00","priceCurrency":"BRL",
      "availability":"https://schema.org/InStock",
      "shippingDetails":{"shippingRate":{"value":"8.00","currency":"BRL"}}}]}
    </script>
    '''
    offer = parse_mercado_offer(html, "https://store.example/item")
    assert offer.shipping_price is None


@pytest.mark.parametrize("currency", ["USD", "R$", 1, None])
def test_generic_jsonld_requires_unambiguous_iso_brl_currency(currency):
    import json

    from app.stores.structured import parse_jsonld_offer

    html = '<script type="application/ld+json">' + json.dumps({
        "@type": "Product", "name": "Produto",
        "offers": {"@type": "Offer", "price": "10.00", "priceCurrency": currency,
                   "availability": "https://schema.org/InStock"},
    }) + "</script>"
    with pytest.raises(StoreParseError):
        parse_jsonld_offer(html, "https://shop.example/item", "generic")


def test_generic_jsonld_rejects_multiple_distinct_products():
    from app.stores.structured import parse_jsonld_offer

    html = '''
    <script type="application/ld+json">
    [{"@type":"Product","name":"A","offers":{"price":"10","priceCurrency":"BRL",
      "availability":"https://schema.org/InStock"}},
     {"@type":"Product","name":"B","offers":{"price":"1","priceCurrency":"BRL",
      "availability":"https://schema.org/InStock"}}]
    </script>
    '''
    with pytest.raises(StoreParseError, match="múltiplos produtos"):
        parse_jsonld_offer(html, "https://shop.example/item", "generic")


def test_generic_explicit_unavailability_without_price_is_a_valid_unavailable_observation():
    from app.stores.structured import parse_jsonld_offer

    html = '''
    <script type="application/ld+json">
    {"@type":"Product","name":"Produto","offers":{"@type":"Offer",
     "availability":"https://schema.org/OutOfStock"}}
    </script>
    '''
    offer = parse_jsonld_offer(html, "https://shop.example/item", "generic")
    assert not offer.is_available
    assert offer.product_price is None


def test_generic_unknown_availability_fails_without_claiming_unavailable():
    from app.stores.structured import parse_jsonld_offer

    html = '''
    <script type="application/ld+json">
    {"@type":"Product","name":"Produto","offers":{"@type":"Offer",
     "price":"10.00","priceCurrency":"BRL","availability":"Unknown"}}
    </script>
    '''
    with pytest.raises(StoreParseError, match="disponibilidade"):
        parse_jsonld_offer(html, "https://shop.example/item", "generic")


def test_generic_accepts_schema_type_urls_and_case_insensitive_iso_currency():
    from app.stores.structured import parse_jsonld_offer

    html = '''
    <script type="application/ld+json">
    {"@type":"https://schema.org/Product","name":"Produto",
     "offers":{"@type":"https://schema.org/Offer","price":"10.00",
      "priceCurrency":"brl","availability":"https://schema.org/InStock",
      "shippingDetails":{"shippingRate":{"value":"0.00","currency":"BRL"}}}}
    </script>
    '''
    offer = parse_jsonld_offer(html, "https://shop.example/item", "generic")
    assert offer.product_price == Decimal("10.00")
    assert offer.shipping_price == Decimal("0.00")
