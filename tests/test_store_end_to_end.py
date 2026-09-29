"""Deterministic direct monitoring through each required store adapter."""

from decimal import Decimal

import httpx
import pytest

from app.alerts import AlertEngine
from app.database import Database
from app.monitor import Monitor
from app.stores.amazon import AmazonAdapter
from app.stores.amazon import parse_search_results as amazon_search
from app.stores.base import StoreError
from app.stores.magalu import MagaluAdapter
from app.stores.magalu import parse_search_results as magalu_search
from app.stores.mercadolivre import MercadoLivreAdapter
from app.stores.mercadolivre import parse_search_results as mercado_search

AMAZON_HTML = """
<span id="productTitle">Celular modelo X 128GB</span>
<div id="availability">Em estoque.</div>
<div id="corePriceDisplay_desktop_feature_div"><span class="a-offscreen">R$ 100,00</span></div>
<div id="mir-layout-DELIVERY_BLOCK-slot-PRIMARY_DELIVERY_MESSAGE_LARGE">Frete grátis</div>
"""
STRUCTURED_HTML = """
<script type="application/ld+json">
{"@type":"Product","name":"Celular modelo X 128GB","model":"X",
 "offers":{"@type":"Offer","price":"100.00","priceCurrency":"BRL",
 "availability":"https://schema.org/InStock",
 "shippingDetails":{"shippingRate":{"value":"0.00","currency":"BRL"}}}}
</script>
"""


@pytest.mark.parametrize(
    ("adapter", "url", "html"),
    [
        (AmazonAdapter(), "https://www.amazon.com.br/dp/X", AMAZON_HTML),
        (MercadoLivreAdapter(), "https://www.mercadolivre.com.br/item/X", STRUCTURED_HTML),
        (MagaluAdapter(), "https://www.magazineluiza.com.br/item/X", STRUCTURED_HTML),
    ],
)
@pytest.mark.asyncio
async def test_required_store_adapter_monitor_history_and_duplicate_alert(
    adapter, url, html, tmp_path
):
    requests = []

    def handler(request):
        requests.append(str(request.url))
        return httpx.Response(200, text=html, request=request)

    adapter._client = lambda: httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=False
    )
    db = Database(tmp_path / "prices.db")
    db.initialize()
    product = db.add_product(
        "Celular modelo X 128GB", Decimal("100"), source_query="Celular modelo X 128GB"
    )
    listing = db.add_listing(product.id, adapter.name, url, product.name)
    sent = []

    async def send(message):
        sent.append(message)

    monitor = Monitor(db, {adapter.name: adapter}, AlertEngine(db))
    assert await monitor.check_once(send) == 1
    assert await monitor.check_once(send) == 0
    assert requests == [url, url]
    assert len(db.history(listing.id)) == 2
    assert db.history_stats(listing.id).current_price == Decimal("100.00")
    assert len(sent) == 1


@pytest.mark.parametrize(
    ("parser", "html"),
    [
        (amazon_search, '<div data-component-type="s-search-result" data-asin="X">'
                        '<h2><a href="/dp/X"><span>Produto</span></a></h2></div>'),
        (mercado_search, '<li class="ui-search-layout__item"><a href="/item/X">'
                         '<h2>Produto</h2></a></li>'),
        (magalu_search, '<a href="/produto/X" title="Produto teste">'
                        'Produto teste R$ 100,00</a>'),
    ],
)
def test_required_store_native_search_success_and_empty(parser, html):
    assert len(parser(html)) == 1
    assert parser("<html><body>sem resultados</body></html>") == []


@pytest.mark.parametrize(
    ("adapter", "url"),
    [
        (AmazonAdapter(), "https://www.amazon.com.br/dp/X"),
        (MercadoLivreAdapter(), "https://www.mercadolivre.com.br/item/X"),
        (MagaluAdapter(), "https://www.magazineluiza.com.br/item/X"),
    ],
)
@pytest.mark.asyncio
async def test_required_store_timeout_is_reported_without_retry(adapter, url):
    requests = []

    def handler(request):
        requests.append(str(request.url))
        raise httpx.ReadTimeout("fixture timeout", request=request)

    adapter._client = lambda: httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=False
    )
    with pytest.raises(StoreError):
        await adapter.get_offer(url)
    assert requests == [url]
