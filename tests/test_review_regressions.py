"""Observable regressions from the independent review, using controlled boundaries."""

import asyncio
import gzip
import json
import zlib
from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace

import httpcore
import httpx
import pytest

from app.alerts import AlertEngine
from app.bot import TelegramBot, format_discovery_results
from app.database import Database, DuplicateListingError
from app.discovery import (
    CandidateURL,
    DiscoveryOrchestrator,
    JsonWebDiscoveryProvider,
    StaticDiscoveryProvider,
)
from app.matching import match_offer, match_product
from app.models import StoreOffer
from app.monitor import Monitor
from app.network import PinnedAsyncHTTPTransport
from app.query import parse_query
from app.stores.amazon import AmazonAdapter
from app.stores.base import StoreAdapter, StoreError, StoreParseError
from app.stores.http import request_limited
from app.stores.magalu import MagaluAdapter
from app.stores.magalu import parse_offer as magalu_offer
from app.stores.mercadolivre import MercadoLivreAdapter
from app.stores.mercadolivre import parse_offer as mercado_offer
from app.stores.structured import parse_jsonld_offer
from app.urls import listing_key


class Message:
    def __init__(self, text=""):
        self.text = text
        self.replies = []

    async def reply_text(self, text, **kwargs):
        self.replies.append(text)


def update(text="", chat_id=123):
    return SimpleNamespace(
        effective_chat=SimpleNamespace(id=chat_id, type="private"), effective_message=Message(text)
    )


def context(*args):
    return SimpleNamespace(args=list(args))


class Adapter(StoreAdapter):
    name = "shop"
    domains = {"shop.example"}

    def __init__(self, title="Sabao OMO 3L", shipping=Decimal("0")):
        self.offer = StoreOffer(
            self.name, title, "https://shop.example/item", Decimal("10"), shipping
        )
        self.error = None

    async def search(self, query):
        return []

    async def get_offer(self, url):
        if self.error:
            raise self.error
        return self.offer


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "prices.db")
    database.initialize()
    return database


def snapshot(db, listing_id, value):
    value = Decimal(value)
    return db.add_snapshot(
        listing_id, value, Decimal("0"), Decimal("0"), None, value, None, True, None
    )


@pytest.mark.parametrize("attributes", [{"brand": "OMO", "sku": "other-456"}, {"brand": "OMO"}, {}])
async def test_equivalent_offers_attach_across_stores_with_different_or_missing_skus(
    db, attributes,
):
    first, other = Adapter(), Adapter()
    first.offer = replace(first.offer, attributes={"brand": "OMO", "sku": "shop-123"})
    other.name, other.domains = "other", {"other.example"}
    other.offer = replace(other.offer, store="other", url="https://other.example/item",
                          attributes=attributes)
    bot = TelegramBot(db, {"shop": first, "other": other}, 123)
    await bot.add_target(update(), context("20", first.offer.url))
    product = db.list_products()[0]
    identity = json.loads(product.identity_json)
    assert "sku" not in identity["attributes"]
    # Already saved identities from the previous version must remain usable.
    identity["attributes"]["sku"] = "legacy-shop-123"
    with db.connection() as connection:
        connection.execute("UPDATE products SET identity_json = ? WHERE id = ?",
                           (json.dumps(identity), product.id))
    attached = update()
    await bot.attach(attached, context(str(product.id), other.offer.url))
    assert "anexado" in attached.effective_message.replies[0]
    listings = db.list_listings()
    assert len(listings) == 2
    monitor = Monitor(db, bot.adapters, AlertEngine(db))
    await monitor.check_once(update().effective_message.reply_text)
    assert monitor.last_run["checked"] == 2
    assert all(db.history(listing.id) for listing in listings)


@pytest.mark.parametrize("title,status", [
    ("Apple iPhone 15 128GB Tela 6,1 polegadas", "matched"),
    ("Tela 6.1 polegadas Apple iPhone 15 128GB", "matched"),
    ("Apple iPhone 14 128GB Tela 15 polegadas", "rejected"),
    ("Apple iPhone 128GB Tela 15 polegadas", "ambiguous"),
])
def test_model_numbers_are_compared_with_their_labels(db, title, status):
    original = "Apple iPhone 15 128GB"
    offer = StoreOffer("shop", title, "https://shop.example/item", Decimal("10"), Decimal("0"))
    assert match_offer(parse_query(original), offer).status == status
    product = db.add_product(original, Decimal("20"), source_query=original,
                             identity_json=json.dumps({"attributes": {"model_numbers": "15"},
                                                       "confirmed_title": original}))
    assert match_product(product, offer).status == status


@pytest.mark.parametrize("parser", [magalu_offer, mercado_offer])
@pytest.mark.parametrize("kind", ['"Product"', '["Product"]', '"https://schema.org/Product"'])
def test_malformed_jsonld_never_reaches_valid_html_fallback(parser, kind):
    fallback = (
        '<h1 class="ui-pdp-title">Produto</h1><div data-testid="price-value">R$ 99,90</div>'
        '<div class="ui-pdp-price__second-line">'
        '<span class="andes-money-amount__fraction">99</span>'
        '<span class="andes-money-amount__cents">90</span></div>'
        '<meta itemprop="availability" content="https://schema.org/InStock">'
        '<meta itemprop="priceCurrency" content="BRL">'
    )
    assert parser(fallback, "https://store.example/item").product_price == Decimal("99.90")
    broken = '<script type="application/ld+json">{"@type":' + kind + ',"offers":</script>'
    with pytest.raises(StoreParseError, match="JSON-LD malformado"):
        parser(broken + fallback, "https://store.example/item")


async def test_previously_suspended_alert_recovers_without_manual_rearming(db):
    product, listing = db.register_product_listing(
        "Produto", Decimal("20"), "shop", "https://shop.example/item", "Produto"
    )
    pending = db.add_alert(listing.id, "TARGET_REACHED", Decimal("10"), product.target_price)
    with db.connection() as connection:
        connection.execute("UPDATE alerts SET status = 'failed' WHERE id = ?", (pending.id,))
    db.initialize()
    sent = []

    async def send(message):
        sent.append(message)

    assert await AlertEngine(db).process(product, listing, snapshot(db, listing.id, "10"), send)
    assert db.last_alert(listing.id, "TARGET_REACHED").id == pending.id
    assert len(sent) == 1


@pytest.mark.parametrize("encoding,compress", [("gzip", gzip.compress), ("deflate", zlib.compress)])
async def test_compressed_response_is_decoded_once_and_decoded_size_is_limited(encoding, compress):
    body = b"<html>product</html>" * 100

    async def handler(request):
        return httpx.Response(
            200,
            headers={"Content-Encoding": encoding},
            stream=httpx.ByteStream(compress(body)),
            request=request,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        response = await request_limited(client, "https://shop.example/item", len(body))
        assert response.content == body
        assert "content-encoding" not in response.headers
        assert int(response.headers["content-length"]) == len(body)
        with pytest.raises(StoreError, match="limite"):
            await request_limited(client, "https://shop.example/item", len(body) - 1)


@pytest.mark.parametrize(
    "error,expected",
    [
        (httpcore.ConnectError, httpx.ConnectError),
        (httpcore.ReadTimeout, httpx.ReadTimeout),
        (httpcore.RemoteProtocolError, httpx.RemoteProtocolError),
    ],
)
@pytest.mark.parametrize("streaming", [False, True])
async def test_transport_maps_core_errors_during_headers_and_body(error, expected, streaming):
    class Stream:
        async def __aiter__(self):
            raise error("controlled failure")
            yield b""  # An async iterator with no successful data.

        async def aclose(self):
            pass

    class Pool:
        async def handle_async_request(self, request):
            if not streaming:
                raise error("controlled failure")
            return httpcore.Response(200, content=Stream())

        async def aclose(self):
            pass

    transport = PinnedAsyncHTTPTransport()
    await transport.aclose()
    transport._pool = Pool()
    client = httpx.AsyncClient(transport=transport)
    try:
        with pytest.raises(expected):
            await client.get("https://shop.example/item")
    finally:
        await client.aclose()


@pytest.mark.parametrize(
    "query,title",
    [
        ("Apple iPhone 15 128GB", "Apple iPhone 14 128GB"),
        ("Samsung Galaxy S24 128GB", "Capa Samsung Galaxy S24 128GB"),
        ("Apple MacBook Air M2 256GB", "Apple MacBook Pro M2 256GB"),
        ("Samsung Galaxy S24 8GB RAM 256GB", "Samsung Galaxy S24 8GB RAM 128GB"),
        ("Samsung Galaxy S24 novo", "Samsung Galaxy S24 novo recondicionado"),
    ],
)
def test_incompatible_products_never_match(query, title):
    assert match_offer(parse_query(query), Adapter(title).offer).status == "rejected"


def test_ram_and_storage_are_separate_constraints():
    query = parse_query("Samsung Galaxy S24 8GB RAM 256GB")
    assert query.ram == "8 gb"
    assert query.capacity == "256 gb"
    assert match_offer(query, Adapter(query.original_text).offer).status == "matched"
    assert parse_query("Samsung Galaxy S24 RAM 8GB 256GB").capacity == "256 gb"


def test_bulk_pack_is_not_ranked_as_an_unspecified_single_item():
    offer = Adapter("Omo 4 Unidades Detergente Liquido Puro Cuidado 3L").offer
    decision = match_offer(parse_query("OMO 3L"), offer)
    assert decision.status == "ambiguous"
    assert decision.attributes["package_quantity"] == "4"
    assert parse_query("OMO 4 unidades 3L").package_quantity == 4


def test_short_model_names_are_not_discarded():
    assert match_offer(parse_query("Shampoo A"), Adapter("Shampoo B").offer).status != "matched"


@pytest.mark.parametrize("text", ["AirPods Max", "iPhone 15 Pro Max 256GB", "AirPods Max 2"])
def test_max_is_a_model_qualifier_not_a_price_filter(text):
    query = parse_query(text)
    assert query.max_price is None
    assert match_offer(query, Adapter(text).offer).status == "matched"
    assert parse_query(f"{text} até R$ 1999,90").max_price == "1999.90"


def structured(extra=None, shipping_extra=None, name="Produto teste novo"):
    data = {
        "@type": "Offer",
        "price": "10.00",
        "priceCurrency": "BRL",
        "availability": "https://schema.org/InStock",
        "shippingDetails": {
            "shippingRate": {"value": "0", "currency": "BRL"},
            **(shipping_extra or {}),
        },
        **(extra or {}),
    }
    return (
        '<script type="application/ld+json">'
        + json.dumps(
            {
                "@type": "Product",
                "name": name,
                "offers": data,
            }
        )
        + "</script>"
    )


@pytest.mark.parametrize(
    "extra",
    [
        {"validForMemberTier": {"name": "Clube"}},
        {"eligibleTransactionVolume": {"minPrice": "100.00"}},
        {"eligibleCustomerType": "NewCustomers"},
        {"priceSpecification": {"validForMemberTier": {"name": "Clube"}}},
        {"priceSpecification": {"billingDuration": "P1M"}},
    ],
)
def test_conditional_structured_offer_is_not_a_guaranteed_price(extra):
    with pytest.raises(StoreParseError):
        parse_jsonld_offer(structured(extra), "https://shop.example/item", "generic")


def test_conditional_shipping_remains_unknown_and_no_shipping_is_rejected():
    offer = parse_jsonld_offer(
        structured(shipping_extra={"validForMemberTier": {"name": "Clube"}}),
        "https://shop.example/item",
        "generic",
    )
    assert offer.shipping_price is None
    with pytest.raises(StoreParseError):
        parse_jsonld_offer(
            structured(shipping_extra={"doesNotShip": True}), "https://shop.example/item", "generic"
        )


def test_structured_refurbished_condition_cannot_match_new_title():
    offer = parse_jsonld_offer(
        structured({"itemCondition": "https://schema.org/RefurbishedCondition"}),
        "https://shop.example/item",
        "generic",
    )
    assert offer.attributes["condition"] == "recondicionado"
    assert match_offer(parse_query("Produto teste novo"), offer).status == "rejected"


@pytest.mark.parametrize(
    "parser,fallback",
    [
        (magalu_offer, '<h1>Produto</h1><span data-testid="price-value">10,00</span>'),
        (
            mercado_offer,
            '<h1 class="ui-pdp-title">Produto</h1><div class="ui-pdp-price__second-line">'
            '<span class="andes-money-amount__fraction">10</span></div>',
        ),
    ],
)
def test_html_does_not_override_currency_conflicts_or_invent_unavailability(parser, fallback):
    availability = '<meta itemprop="availability" content="https://schema.org/InStock">'
    currency = '<meta itemprop="priceCurrency" content="BRL">'
    with pytest.raises(StoreParseError):
        parser(
            structured({"priceCurrency": "USD"}) + fallback + availability + currency,
            "https://store.example/item",
        )
    with pytest.raises(StoreParseError, match="disponibilidade"):
        parser(fallback + currency, "https://store.example/item")
    with pytest.raises(StoreParseError, match="moeda"):
        parser(fallback + availability, "https://store.example/item")


async def search_and_select(db, adapter, query="Sabao OMO", product_id=None):
    discovery = DiscoveryOrchestrator(
        {"shop": adapter},
        [
            StaticDiscoveryProvider(
                "fixture",
                [CandidateURL(adapter.offer.url, "fixture", "shop")],
            )
        ],
    )
    bot = TelegramBot(db, {"shop": adapter}, 123, discovery)
    search = update()
    await bot.search(search, context(query))
    session = search.effective_message.replies[0].split()[1]
    selection = update()
    args = [session, "R1", "20"] + ([str(product_id)] if product_id else [])
    await bot.select(selection, context(*args))
    return bot, session, selection


async def test_known_identity_survives_broad_search_monitoring_and_attachment(db):
    adapter = Adapter()
    bot, _, selected = await search_and_select(db, adapter)
    assert "confirmado" in selected.effective_message.replies[0]
    product = db.list_products()[0]
    assert json.loads(product.identity_json)["attributes"]["volume"] == "3 l"
    adapter.offer = Adapter("Sabao OMO 5L").offer
    messages = []

    async def send(message):
        messages.append(message)

    assert await Monitor(db, {"shop": adapter}, AlertEngine(db)).check_once(send) == 0
    assert db.product_history(product.id) == []
    attach = update()
    await bot.attach(attach, context(str(product.id), "https://shop.example/other"))
    assert "não corresponde" in attach.effective_message.replies[0]
    assert len(db.list_listings()) == 1
    assert messages == []


async def test_failed_reentry_delivery_retries_even_after_more_snapshots(db):
    product, listing = db.register_product_listing(
        "Produto", Decimal("30"), "shop", "https://shop.example/item", "Produto"
    )
    messages = []

    async def send(message):
        messages.append(message)

    async def fail(message):
        raise RuntimeError("temporary Telegram failure")

    engine = AlertEngine(db)
    assert await engine.process(product, listing, snapshot(db, listing.id, "20"), send)
    assert not await engine.process(product, listing, snapshot(db, listing.id, "40"), send)
    with pytest.raises(RuntimeError):
        await engine.process(product, listing, snapshot(db, listing.id, "25"), fail)
    assert db.pending_alert(listing.id, "TARGET_REACHED") is not None
    assert await AlertEngine(db).process(product, listing, snapshot(db, listing.id, "25"), send)
    assert len(messages) == 2
    assert not await engine.process(product, listing, snapshot(db, listing.id, "25"), send)


async def test_unknown_observation_does_not_erase_a_reliable_reentry_event(db):
    product, listing = db.register_product_listing(
        "Produto", Decimal("30"), "shop", "https://shop.example/item", "Produto"
    )
    sent = []

    async def send(message):
        sent.append(message)

    engine = AlertEngine(db)
    await engine.process(product, listing, snapshot(db, listing.id, "20"), send)
    await engine.process(product, listing, snapshot(db, listing.id, "40"), send)
    unavailable = db.add_snapshot(listing.id, None, None, None, None, None, None, False, None)
    await engine.process(product, listing, unavailable, send)
    assert await engine.process(product, listing, snapshot(db, listing.id, "25"), send)
    assert len(sent) == 2


async def test_cancelled_delivery_is_pending_after_restart_not_sent(db):
    product, listing = db.register_product_listing(
        "Produto", Decimal("20"), "shop", "https://shop.example/item", "Produto"
    )

    async def cancel(message):
        raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await AlertEngine(db).process(product, listing, snapshot(db, listing.id, "10"), cancel)
    assert db.last_alert(listing.id, "TARGET_REACHED") is None
    reopened = Database(db.path)
    reopened.initialize()
    messages = []

    async def send(message):
        messages.append(message)

    assert await AlertEngine(reopened).process(
        product,
        listing,
        snapshot(reopened, listing.id, "10"),
        send,
    )
    assert len(messages) == 1
    assert reopened.last_alert(listing.id, "TARGET_REACHED").sent_at is not None


async def test_delivery_retries_once_per_observation_and_recovers_automatically(db):
    product, listing = db.register_product_listing(
        "Produto", Decimal("20"), "shop", "https://shop.example/item", "Produto"
    )
    attempts = []

    async def fail(message):
        attempts.append(message)
        raise RuntimeError("Telegram unavailable")

    engine = AlertEngine(db)
    for _ in range(4):
        with pytest.raises(RuntimeError):
            await engine.process(product, listing, snapshot(db, listing.id, "10"), fail)
    assert len(attempts) == 4
    pending = db.pending_alert(listing.id, "TARGET_REACHED")
    assert pending.status == "pending"
    sent = []

    async def send(message):
        sent.append(message)

    assert await AlertEngine(db).process(product, listing, snapshot(db, listing.id, "12"), send)
    delivered = db.last_alert(listing.id, "TARGET_REACHED")
    assert delivered.id == pending.id
    assert delivered.price == Decimal("12")
    assert not await engine.process(product, listing, snapshot(db, listing.id, "12"), send)
    assert len(sent) == 1


async def test_price_drop_pending_delivery_recovers_and_is_not_duplicated(db):
    product, listing = db.register_product_listing(
        "Produto",
        Decimal("20"),
        "shop",
        "https://shop.example/item",
        "Produto",
        monitoring_mode="price_drop",
    )
    snapshot(db, listing.id, "20")

    async def cancel(message):
        raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await AlertEngine(db).process(product, listing, snapshot(db, listing.id, "10"), cancel)
    sent = []

    async def send(message):
        sent.append(message)

    engine = AlertEngine(db)
    assert await engine.process(product, listing, snapshot(db, listing.id, "10"), send)
    assert not await engine.process(product, listing, snapshot(db, listing.id, "10"), send)
    assert len(sent) == 1


async def test_pending_alert_is_cancelled_if_fresh_price_no_longer_qualifies(db):
    product, listing = db.register_product_listing(
        "Produto", Decimal("20"), "shop", "https://shop.example/item", "Produto"
    )

    async def cancel(message):
        raise asyncio.CancelledError()

    engine = AlertEngine(db)
    with pytest.raises(asyncio.CancelledError):
        await engine.process(product, listing, snapshot(db, listing.id, "10"), cancel)
    assert not await engine.process(product, listing, snapshot(db, listing.id, "30"), cancel)
    assert db.pending_alert(listing.id, "TARGET_REACHED") is None


def test_canonical_duplicates_are_atomic_but_variant_and_seller_parameters_survive(db):
    canonical = "https://shop.example/item"
    product, listing = db.register_product_listing(
        "Produto", Decimal("20"), "shop", canonical + "?ref=one", "Produto", canonical_url=canonical
    )
    snapshot(db, listing.id, "10")
    with pytest.raises(DuplicateListingError):
        db.register_product_listing(
            "Outro",
            Decimal("20"),
            "shop",
            canonical + "?ref=two",
            "Produto",
            canonical_url=canonical,
        )
    assert len(db.list_products()) == 1
    assert len(db.history(listing.id)) == 1
    assert listing_key("shop", canonical + "?sku=1", canonical) != listing_key(
        "shop",
        canonical + "?sku=2",
        canonical,
    )
    assert listing_key("shop", canonical + "?seller=1&seller=2") != listing_key(
        "shop",
        canonical + "?seller=2",
    )
    assert listing_key("shop", canonical + "?ref=product1") != listing_key(
        "shop",
        canonical + "?ref=product2",
    )
    db.deactivate_product(product.id)
    db.reactivate_product(product.id)
    assert db.list_listings()[0].id == listing.id
    assert len(db.history(listing.id)) == 1


def test_legacy_duplicate_migration_preserves_snapshots_and_disables_extra_listing(db):
    product, listing = db.register_product_listing(
        "Produto", Decimal("20"), "shop", "https://shop.example/item", "Produto"
    )
    with db.connection() as connection:
        duplicate = connection.execute(
            """INSERT INTO listings(product_id, store, url, title, created_at, updated_at,
               canonical_url) VALUES (?, 'shop', ?, 'Produto', ?, ?, ?)""",
            (
                product.id,
                "https://shop.example/item?ref=other",
                listing.created_at.isoformat(),
                listing.updated_at.isoformat(),
                listing.url,
            ),
        ).lastrowid
    snapshot(db, duplicate, "10")
    db.initialize()
    db.initialize()
    assert len(db.history(duplicate)) == 1
    assert not db.get_listing(duplicate).active
    assert [row.id for row in db.list_listings()] == [listing.id]


def test_migration_learns_only_identity_already_saved_on_confirmed_listing(db):
    product, _ = db.register_product_listing(
        "Sabao OMO",
        Decimal("20"),
        "shop",
        "https://shop.example/item",
        "Sabao OMO 3L",
        source_query="Sabao OMO",
    )
    db.initialize()
    migrated = db.get_product(product.id)
    assert json.loads(migrated.identity_json)["attributes"]["volume"] == "3 l"
    assert match_product(migrated, Adapter("Sabao OMO 5L").offer).status == "rejected"


async def test_all_new_handlers_enforce_authorization(db):
    bot = TelegramBot(db, {"shop": Adapter()}, 123)
    for handler in (
        bot.add_target,
        bot.set_target,
        bot.reactivate,
        bot.cancel_selection,
        bot.selection_price,
    ):
        unauthorized = update(chat_id=999)
        await handler(unauthorized, context())
        assert unauthorized.effective_message.replies == ["Acesso não autorizado."]


async def test_oversized_product_id_is_rejected_without_a_sqlite_overflow(db):
    bot = TelegramBot(db, {"shop": Adapter()}, 123)
    for handler, args in ((bot.set_target, ("9" * 100, "10")), (bot.reactivate, ("9" * 100,))):
        request = update()
        await handler(request, context(*args))
        assert "ID de produto inválido" in request.effective_message.replies[0]


async def test_manual_confirmation_with_no_attributes_is_bound_to_the_selected_title(db):
    product, _ = db.register_product_listing(
        "Celular linha X",
        Decimal("20"),
        "shop",
        "https://shop.example/item",
        "Celular linha X",
        source_query="Celular 128GB",
        identity_json=json.dumps(
            {"attributes": {}, "manual_confirmation": True, "confirmed_title": "Celular linha X"}
        ),
    )
    assert match_product(product, Adapter("Celular linha X").offer).status == "matched"
    assert match_product(product, Adapter("Celular linha Y").offer).status != "matched"
    assert match_product(product, Adapter("Celular linha X 256GB").offer).status == "rejected"


async def test_entire_fetch_has_a_deadline_and_command_reports_timeout(db):
    class SlowAdapter(Adapter):
        async def get_offer(self, url):
            await asyncio.Event().wait()

    slow = SlowAdapter()
    product, listing = db.register_product_listing(
        "Sabao OMO 3L", Decimal("20"), "shop", slow.offer.url, slow.offer.title
    )
    monitor = Monitor(db, {"shop": slow}, AlertEngine(db), fetch_timeout_seconds=0.01)

    async def send(message):
        raise AssertionError("a timeout cannot create a price alert")

    assert await monitor.check_once(send) == 0
    assert monitor.last_run["failed"] == 1
    assert db.product_history(product.id) == []
    assert db.get_listing(listing.id).last_error
    bot = TelegramBot(db, {"shop": slow}, 123, fetch_timeout_seconds=0.01)
    registration = update()
    await bot.add(registration, context("20", slow.offer.url))
    assert "tempo total" in registration.effective_message.replies[0]


@pytest.mark.parametrize("adapter", [AmazonAdapter(), MercadoLivreAdapter(), MagaluAdapter()])
async def test_http_200_captcha_is_a_failed_source_not_a_completed_empty_search(adapter):
    adapter._client = lambda: httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, text="CAPTCHA: verify you are human", request=request
            ),
        )
    )
    outcome = await DiscoveryOrchestrator({adapter.name: adapter}).search(parse_query("Produto"))
    assert outcome.status == "partial"
    assert outcome.source_coverage["sources"][f"store:{adapter.name}"]["status"] == "failed"


async def test_unknown_shipping_shows_item_price_without_claiming_a_guaranteed_total(db):
    adapter = Adapter(shipping=None)
    discovery = DiscoveryOrchestrator(
        {"shop": adapter},
        [
            StaticDiscoveryProvider(
                "fixture",
                [CandidateURL(adapter.offer.url, "fixture", "shop")],
            )
        ],
    )
    outcome = await discovery.search(parse_query("Sabao OMO 3L"))
    assert not outcome.results[0].reliable
    rendered = format_discovery_results("demo", outcome)
    assert "produto R$ 10.00" in rendered
    assert "total não calculável" in rendered


async def test_collection_failures_preserve_history_and_recovery_updates_status(db):
    adapter = Adapter()
    product, listing = db.register_product_listing(
        "Sabao OMO 3L", Decimal("1"), "shop", adapter.offer.url, adapter.offer.title
    )
    snapshot(db, listing.id, "10")
    adapter.error = StoreError("controlled store failure")
    monitor = Monitor(db, {"shop": adapter}, AlertEngine(db))
    messages = []

    async def send(message):
        messages.append(message)

    for _ in range(4):
        await monitor.check_once(send)
    assert messages == []
    assert monitor.last_run["failed"] == 1
    assert db.get_listing(listing.id).last_error == "controlled store failure"
    assert db.get_listing(listing.id).last_checked_at is not None
    stats = db.product_history_stats(product.id)
    assert stats.current_price is None
    assert stats.lowest_price == Decimal("10")
    assert db.history_stats(listing.id).current_price is None
    adapter.error = None
    await monitor.check_once(send)
    assert messages == []
    assert db.get_listing(listing.id).last_error is None
    assert db.get_listing(listing.id).last_success_at is not None


async def test_guided_selection_and_target_editing_keep_history_and_authorization(db):
    adapter = Adapter()
    bot, session, _ = await search_and_select(db, adapter)
    first = db.list_products()[0]
    await bot.set_target(update(chat_id=999), context(str(first.id), "99"))
    assert db.get_product(first.id).target_price == Decimal("20")
    await bot.set_target(update(), context(str(first.id), "15"))
    assert db.get_product(first.id).target_price == Decimal("15")
    db.deactivate_product(first.id)
    await bot.reactivate(update(), context(str(first.id)))
    assert db.get_product(first.id).active
    adapter.offer = StoreOffer(
        "shop", "Sabao OMO 3L", "https://shop.example/second", Decimal("10"), Decimal("0")
    )
    _, _, attached = await search_and_select(db, adapter, product_id=first.id)
    assert "anexado" in attached.effective_message.replies[0]
    assert len(db.list_products()) == 1
    assert len(db.list_listings()) == 2
    # A new third offer can be registered by choosing a button and typing only a price.
    adapter.offer = StoreOffer(
        "shop", "Sabao OMO 3L", "https://shop.example/third", Decimal("10"), Decimal("0")
    )
    bot.discovery.providers = [
        StaticDiscoveryProvider(
            "fixture",
            [CandidateURL(adapter.offer.url, "fixture", "shop")],
        )
    ]
    outcome = await bot.discovery.search(parse_query("Sabao OMO"))
    new_session = db.create_search_session(
        123,
        "Sabao OMO",
        outcome.query.to_json(),
        outcome.status,
        outcome.source_coverage,
        outcome.entries(),
    )
    message = Message()
    message.chat = SimpleNamespace(type="private")
    message.chat_id = 123

    async def answer(*args, **kwargs):
        pass

    callback = SimpleNamespace(data=f"sel:{new_session.id}:R1", message=message, answer=answer)
    await bot.selection_button(SimpleNamespace(callback_query=callback), None)
    assert bot.pending_selection[123] == (new_session.id, "R1")
    await bot.selection_price(update("12,00"), context())
    assert len(db.list_products()) == 2
    assert not bot.pending_selection


async def test_searxng_uses_documented_json_api_and_never_uses_snippet_prices():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "results": [
                    {"url": "https://shop.example/item", "title": "Produto", "price": "0.01"},
                ]
            },
            request=request,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = JsonWebDiscoveryProvider(
            "https://search.example/search", client=client, api_format="searxng"
        )
        results = await provider.discover(parse_query("Sabao OMO 3L"))
    assert requests[0].url.params["format"] == "json"
    assert requests[0].url.params["q"] == "Sabao OMO 3L"
    assert results[0].url == "https://shop.example/item"
    assert isinstance(results[0], CandidateURL)
