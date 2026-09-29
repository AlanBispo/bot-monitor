from decimal import Decimal
from pathlib import Path

import pytest

from app.stores.amazon import parse_offer, parse_search_results
from app.stores.base import StoreParseError


def fixture(name):
    return (Path(__file__).parent / name).read_text(encoding="utf-8")


def test_amazon_search_parser():
    results = parse_search_results(fixture("fixtures_amazon_search.html"))
    assert len(results) == 1
    assert results[0].url == "https://www.amazon.com.br/dp/A1"
    assert results[0].product_price == Decimal("29.90")


def test_amazon_offer_parser_detects_explicit_free_shipping():
    offer = parse_offer(fixture("fixtures_amazon_product.html"), "https://www.amazon.com.br/dp/A1")
    assert offer.product_price == Decimal("29.90")
    assert offer.shipping_price == Decimal("0.00")
    assert offer.available


def test_amazon_offer_without_price_is_parse_failure():
    with pytest.raises(StoreParseError):
        parse_offer(
            "<span id='productTitle'>Produto sem preço</span>", "https://www.amazon.com.br/dp/A2"
        )


def test_amazon_offer_requires_affirmative_unavailable_signal():
    offer = parse_offer(
        "<span id='productTitle'>Produto esgotado</span>"
        "<div id='availability'>Currently unavailable.</div>",
        "https://www.amazon.com.br/dp/A3",
    )
    assert offer.observation_status == "unavailable"
    assert not offer.available


@pytest.mark.parametrize(
    "availability_text",
    [
        "Não disponível.",
        "Temporariamente fora de estoque.",
        "Não há ofertas em destaque disponíveis.",
        "Consulte a disponibilidade.",
        "Not available.",
        "Currently not available.",
        "Not in stock.",
        "Out-of-stock.",
        "No offers are available.",
        "Disponível somente para membros Prime.",
    ],
)
def test_amazon_offer_rejects_negative_or_unknown_availability(availability_text):
    html = f"""
    <span id="productTitle">Produto</span>
    <div id="corePriceDisplay_desktop_feature_div"><span class="a-offscreen">R$ 1,00</span></div>
    <div id="availability">{availability_text}</div>
    <div id="mir-layout-DELIVERY_BLOCK-slot-PRIMARY_DELIVERY_MESSAGE_LARGE">Frete grátis</div>
    """
    if availability_text in {
        "Consulte a disponibilidade.", "Disponível somente para membros Prime."
    }:
        with pytest.raises(StoreParseError, match="disponibilidade"):
            parse_offer(html, "https://www.amazon.com.br/dp/A11")
    else:
        offer = parse_offer(html, "https://www.amazon.com.br/dp/A11")
        assert offer.observation_status == "unavailable"
        assert not offer.available
        assert offer.product_price is None


def test_amazon_offer_accepts_affirmative_availability():
    html = """
    <span id="productTitle">Produto</span>
    <div id="corePriceDisplay_desktop_feature_div"><span class="a-offscreen">R$ 1,00</span></div>
    <div id="availability">Em estoque.</div>
    <div id="mir-layout-DELIVERY_BLOCK-slot-PRIMARY_DELIVERY_MESSAGE_LARGE">Frete grátis</div>
    """
    offer = parse_offer(html, "https://www.amazon.com.br/dp/A12")
    assert offer.observation_status == "available"
    assert offer.available
    assert offer.product_price == Decimal("1.00")


def test_amazon_offer_does_not_use_installment_or_shipping_price_as_product_price():
    html = """
    <span id="productTitle">Produto</span>
    <div id="availability">Em estoque</div>
    <span id="installmentPriceDisplay">R$ 9,90</span>
    <div id="mir-layout-DELIVERY_BLOCK-slot-PRIMARY_DELIVERY_MESSAGE_LARGE">R$ 4,90</div>
    """
    with pytest.raises(StoreParseError):
        parse_offer(html, "https://www.amazon.com.br/dp/A4")


def test_conditional_free_shipping_remains_unknown():
    html = """
    <span id="productTitle">Produto Prime</span>
    <div id="availability">Em estoque</div>
    <div id="corePriceDisplay_desktop_feature_div"><span class="a-offscreen">R$ 29,90</span></div>
    <div id="mir-layout-DELIVERY_BLOCK-slot-PRIMARY_DELIVERY_MESSAGE_LARGE">
      Frete grátis apenas para membros Prime
    </div>
    """
    offer = parse_offer(html, "https://www.amazon.com.br/dp/A5")
    assert offer.is_available
    assert offer.shipping_price is None


def test_conditional_free_shipping_with_embedded_zero_remains_unknown():
    html = """
    <span id="productTitle">Produto Prime</span>
    <div id="availability">Em estoque</div>
    <div id="corePriceDisplay_desktop_feature_div"><span class="a-offscreen">R$ 29,90</span></div>
    <div id="mir-layout-DELIVERY_BLOCK-slot-PRIMARY_DELIVERY_MESSAGE_LARGE">
      Frete grátis apenas para membros Prime (R$ 0,00)
    </div>
    """
    offer = parse_offer(html, "https://www.amazon.com.br/dp/A8")
    assert offer.is_available
    assert offer.shipping_price is None


@pytest.mark.parametrize(
    "shipping_text",
    [
        "Frete grátis para clientes selecionados",
        "Frete grátis a partir de R$ 79,00",
        "Frete grátis em compras de R$ 79,00 ou mais",
        "Entrega grátis para a sua região",
        "Frete grátis somente pelo app",
        "Frete grátis na primeira compra",
        "Frete não grátis para este endereço",
        "Frete grátis ou R$ 12,90 para entrega expressa",
    ],
)
def test_unqualified_or_ambiguous_free_shipping_remains_unknown(shipping_text):
    html = f"""
    <span id="productTitle">Produto</span>
    <div id="availability">Em estoque</div>
    <div id="corePriceDisplay_desktop_feature_div"><span class="a-offscreen">R$ 29,90</span></div>
    <div id="mir-layout-DELIVERY_BLOCK-slot-PRIMARY_DELIVERY_MESSAGE_LARGE">
      {shipping_text}
    </div>
    """
    offer = parse_offer(html, "https://www.amazon.com.br/dp/A9")
    assert offer.is_available
    assert offer.shipping_price is None


def test_explicit_paid_shipping_amount_is_parsed():
    html = """
    <span id="productTitle">Produto</span>
    <div id="corePriceDisplay_desktop_feature_div"><span class="a-offscreen">R$ 29,90</span></div>
    <div id="availability">Em estoque</div>
    <div id="mir-layout-DELIVERY_BLOCK-slot-PRIMARY_DELIVERY_MESSAGE_LARGE">
      Frete: R$ 12,90
    </div>
    """
    offer = parse_offer(html, "https://www.amazon.com.br/dp/A10")
    assert offer.shipping_price == Decimal("12.90")


def test_unavailable_signal_wins_over_displayed_price():
    html = """
    <span id="productTitle">Produto indisponível</span>
    <div id="corePriceDisplay_desktop_feature_div"><span class="a-offscreen">R$ 29,90</span></div>
    <div id="availability">Currently unavailable.</div>
    """
    offer = parse_offer(html, "https://www.amazon.com.br/dp/A6")
    assert offer.observation_status == "unavailable"
    assert offer.product_price is None
    assert not offer.available


def test_brand_byline_is_not_used_as_seller():
    html = """
    <span id="productTitle">Produto</span>
    <div id="availability">Em estoque</div>
    <div id="corePriceDisplay_desktop_feature_div"><span class="a-offscreen">R$ 29,90</span></div>
    <a id="bylineInfo">Visite a loja Marca X</a>
    """
    offer = parse_offer(html, "https://www.amazon.com.br/dp/A7")
    assert offer.seller is None
