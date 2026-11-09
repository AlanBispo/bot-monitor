"""End-to-end flows: Telegram handlers, monitor and SQLite, with only the network faked."""

import logging
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

import main
from app.alerts import AlertEngine, AlertSettings
from app.bot import TelegramBot, send_daily_summary
from app.database import Database
from app.discovery import CandidateURL, DiscoveryOrchestrator, DiscoveryProvider
from app.identity import explicit_attributes
from app.models import StoreOffer
from app.monitor import Monitor, ScheduleSettings
from app.pricing import InvalidMoney, calculate_price, parse_money
from app.security import UnsafeURL, validate_url
from app.stores import amazon
from app.stores.amazon import AmazonAdapter
from app.stores.base import StoreAdapter, StoreError
from app.stores.magalu import MagaluAdapter
from app.stores.mercadolivre import MercadoLivreAdapter

URL = "https://shop.example/item"
AMAZON_HTML = """
<span id="productTitle">Celular modelo X 128GB</span>
<div id="availability">Em estoque.</div>
<div id="corePriceDisplay_desktop_feature_div"><span class="a-offscreen">R$ 100,00</span></div>
<div id="mir-layout-DELIVERY_BLOCK-slot-PRIMARY_DELIVERY_MESSAGE_LARGE">Frete grátis</div>
"""
JSONLD_HTML = """
<script type="application/ld+json">
{"@type":"Product","name":"Celular modelo X 128GB","model":"X",
 "offers":{"@type":"Offer","price":"100.00","priceCurrency":"BRL",
 "availability":"https://schema.org/InStock",
 "shippingDetails":{"shippingRate":{"value":"0.00","currency":"BRL"}}}}
</script>
"""


class Shop(StoreAdapter):
    name = "shop"
    domains = {"shop.example"}

    def __init__(self, price="100", shipping="0"):
        self.price = Decimal(price) if price else None
        self.shipping = Decimal(shipping) if shipping is not None else None
        self.available = True
        self.error = None

    async def search(self, query):
        return []

    async def get_offer(self, url):
        if self.error:
            raise self.error
        return StoreOffer(
            self.name, "Produto X", url, self.price if self.available else None, self.shipping,
            available=self.available,
            observation_status="available" if self.available else "unavailable",
        )


class Message:
    def __init__(self, text="", chat_id=123):
        self.text, self.chat_id = text, chat_id
        self.chat = SimpleNamespace(type="private")
        self.replies, self.markups, self.photos, self.documents = [], [], [], []

    async def reply_text(self, text, reply_markup=None):
        self.replies.append(text)
        self.markups.append(reply_markup)

    async def reply_photo(self, photo, caption=None):
        self.photos.append(photo.getvalue())

    async def reply_document(self, document, filename=None, caption=None):
        self.documents.append(document.getvalue())


def update(text="", chat_id=123):
    return SimpleNamespace(
        effective_chat=SimpleNamespace(id=chat_id, type="private"),
        effective_message=Message(text, chat_id),
    )


def args(*values):
    return SimpleNamespace(args=list(values))


def tap(data, chat_id=123):
    message, answers = Message(chat_id=chat_id), []

    async def answer(text=None, show_alert=False):
        answers.append(text)

    query = SimpleNamespace(data=data, message=message, answer=answer)
    return SimpleNamespace(callback_query=query), message, answers


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "prices.db")
    database.initialize()
    return database


@pytest.fixture
def sent():
    return []


@pytest.fixture
def send(sent):
    async def deliver(message):
        sent.append(message)

    return deliver


def register(db, target="50", url=URL):
    return db.register_product_listing("Produto X", Decimal(target), "shop", url, "Produto X")


def observe(db, listing_id, price, days_ago=0):
    snapshot = db.add_snapshot(listing_id, Decimal(price), None, None, None, None, None, True, None)
    moment = (datetime.now(UTC) - timedelta(days=days_ago)).isoformat(timespec="microseconds")
    with db.connection() as connection:
        connection.execute(
            "UPDATE price_history SET captured_at = ? WHERE id = ?", (moment, snapshot.id)
        )


@pytest.mark.parametrize(("adapter", "url", "html"), [
    (AmazonAdapter(), "https://www.amazon.com.br/dp/X", AMAZON_HTML),
    (MercadoLivreAdapter(), "https://www.mercadolivre.com.br/item/X", JSONLD_HTML),
    (MagaluAdapter(), "https://www.magazineluiza.com.br/item/X", JSONLD_HTML),
])
async def test_each_store_monitors_page_and_alerts_target_once(adapter, url, html, db, send, sent):
    adapter._client = lambda: httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, text=html, request=request)
    ))
    title = "Celular modelo X 128GB"
    _, listing = db.register_product_listing(
        title, Decimal("100"), adapter.name, url, title, source_query=title,
    )
    monitor = Monitor(db, {adapter.name: adapter}, AlertEngine(db))
    assert await monitor.check_once(send) == 1
    assert await monitor.check_once(send) == 0
    assert len(db.history(listing.id)) == 2
    assert len(sent) == 1 and "R$ 100.00" in sent[0]


async def test_mercadolivre_login_wall_reports_and_registers_nothing(db):
    adapter = MercadoLivreAdapter()
    adapter._client = lambda: httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(
            200, text="<body>Olá! Para continuar, acesse sua conta</body>", request=request,
        )
    ))
    response = update()
    await TelegramBot(db, {adapter.name: adapter}, 123).add(
        response, args("https://www.mercadolivre.com.br/p/MLB1"),
    )
    assert "Mercado Livre exigiu login" in response.effective_message.replies[0]
    assert db.list_products() == []


def test_amazon_fixtures_parse():
    folder = Path(__file__).parent
    html = (folder / "fixtures_amazon_product.html").read_text(encoding="utf-8")
    assert amazon.parse_offer(html, "https://www.amazon.com.br/dp/X").product_price is not None
    search = (folder / "fixtures_amazon_search.html").read_text(encoding="utf-8")
    assert amazon.parse_search_results(search)


async def test_search_ranks_reliable_first_then_select_registers(db):
    cheap, known = Shop("80", None), Shop("100", "0")
    known.name, cheap.name = "known", "cheap"

    class Provider(DiscoveryProvider):
        name = "fixture"

        async def discover(self, query):
            return [CandidateURL(URL + "/a", "fixture", "cheap"),
                    CandidateURL(URL + "/b", "fixture", "known")]

    adapters = {"cheap": cheap, "known": known}
    bot = TelegramBot(db, adapters, 123, DiscoveryOrchestrator(adapters, [Provider()]))
    response = update()
    await bot.search(response, args("Produto", "X"))
    text = response.effective_message.replies[0]
    session = text.split()[1]
    assert text.index("[known]") < text.index("[cheap]")
    assert "frete desconhecido" in text

    await bot.select(update(), args(session, "R1", "90,00"))
    product = db.list_products()[0]
    assert product.target_price == Decimal("90.00")
    assert db.list_listings()[0].store == "known"


async def test_failures_keep_history_warn_once_and_recover(db, send, sent):
    product, listing = register(db, target="200")
    observe(db, listing.id, "100")
    shop = Shop()
    shop.error = StoreError("HTTP 403")
    monitor = Monitor(db, {"shop": shop}, AlertEngine(db),
                      schedule=ScheduleSettings(store_failure_threshold=2))
    for _ in range(4):
        await monitor.check_once(send)
    assert len(sent) == 1 and "shop falhou em 2" in sent[0]
    assert len(db.history(listing.id)) == 1
    assert db.product_history_stats(product.id).current_price is None
    shop.error = None
    await monitor.check_once(send)
    assert sent[-1].startswith("✅ shop voltou")


async def test_unknown_shipping_never_triggers_target_alert(db, send, sent):
    _, listing = register(db, target="200")
    await Monitor(db, {"shop": Shop(shipping=None)}, AlertEngine(db)).check_once(send)
    assert sent == []
    assert db.latest_snapshot(listing.id).effective_price is None


async def test_extra_alerts_arrive_once_in_daily_summary(db, send, sent):
    _, listing = register(db)
    observe(db, listing.id, "200", days_ago=10)
    observe(db, listing.id, "200", days_ago=1)
    shop = Shop("150", None)
    monitor = Monitor(db, {"shop": shop}, AlertEngine(db))
    await monitor.check_once(send)
    await monitor.check_once(send)
    assert sent == []

    shop.available = False
    await monitor.check_once(send)
    shop.available = True
    await monitor.check_once(send)
    assert sorted(alert.alert_type for alert in db.queued_alerts()) == [
        "BACK_IN_STOCK", "LOW_PRICE", "PERCENT_DROP",
    ]
    await send_daily_summary(db, send)
    assert all(label in sent[0] for label in ("Menor preço", "Queda percentual", "Voltou ao"))
    sent.clear()
    await send_daily_summary(db, send)
    assert "Nenhuma novidade" in sent[0]


async def test_immediate_mode_sends_event_right_away(db, send, sent):
    _, listing = register(db)
    observe(db, listing.id, "200", days_ago=1)
    engine = AlertEngine(db, AlertSettings(daily_summary=False))
    await Monitor(db, {"shop": Shop("150", None)}, engine).check_once(send)
    assert len(sent) == 1 and "Queda percentual" in sent[0]
    assert db.queued_alerts() == []


async def test_interval_near_target_and_due_only(db, send):
    product, listing = register(db, target="100")
    monitor = Monitor(db, {"shop": Shop("500")}, AlertEngine(db))
    await monitor.check_once(send, due_only=True)
    assert monitor.last_run["checked"] == 1
    await monitor.check_once(send, due_only=True)
    assert monitor.last_run["checked"] == 0
    assert monitor.check_interval(product, db.latest_snapshot(listing.id)) == 30
    observe(db, listing.id, "109")
    assert monitor.check_interval(product, db.latest_snapshot(listing.id)) == 10
    assert monitor.check_interval(
        db.set_check_interval(product.id, 120), db.latest_snapshot(listing.id)
    ) == 120


async def test_list_buttons_pause_target_and_remove(db):
    product, listing = register(db)
    observe(db, listing.id, "100")
    bot = TelegramBot(db, {}, 123)
    response = update()
    await bot.list_products(response, args())
    assert response.effective_message.markups[-1].inline_keyboard[0][0].callback_data == (
        f"prd:pause:{product.id}"
    )

    await bot.product_button(tap(f"prd:pause:{product.id}")[0], None)
    assert not db.get_product(product.id).active
    await bot.product_button(tap(f"prd:resume:{product.id}")[0], None)
    assert db.get_product(product.id).active

    await bot.product_button(tap(f"prd:target:{product.id}")[0], None)
    await bot.selection_price(update("42,90"), None)
    assert db.get_product(product.id).target_price == Decimal("42.90")

    await bot.product_button(tap(f"prd:remove:{product.id}")[0], None)
    assert db.get_product(product.id).active
    await bot.product_button(tap(f"prd:confirm_remove:{product.id}")[0], None)
    response = update()
    await bot.list_products(response, args("todos"))
    assert response.effective_message.replies == ["Nenhum produto ativo."]
    assert len(db.history(listing.id)) == 1


async def test_history_chart_and_excel_safe_export(db):
    product, listing = db.register_product_listing(
        '=HYPERLINK("x")', Decimal("50"), "shop", URL, "t",
    )
    observe(db, listing.id, "1234.5")
    bot = TelegramBot(db, {}, 123)
    response = update()
    await bot.history(response, args(str(product.id)))
    assert response.effective_message.photos[0].startswith(b"\x89PNG")
    response = update()
    await bot.export(response, args(str(product.id)))
    content = response.effective_message.documents[0].decode("utf-8-sig")
    assert "'=HYPERLINK" in content and ";1234,5;" in content


async def test_unauthorized_chat_cannot_control_the_bot(db):
    product, _ = register(db)
    bot = TelegramBot(db, {}, 123)
    for handler, values in ((bot.remove, [str(product.id)]), (bot.export, []), (bot.status, [])):
        response = update(chat_id=999)
        await handler(response, args(*values))
        assert response.effective_message.replies == ["Acesso não autorizado."]
    event, _, answers = tap(f"prd:pause:{product.id}", chat_id=999)
    await bot.product_button(event, None)
    assert answers == ["Acesso não autorizado."]
    assert db.get_product(product.id).active


@pytest.mark.parametrize("url", [
    "file:///etc/passwd", "http://localhost/", "http://127.0.0.1/", "http://10.0.0.1/",
    "http://169.254.169.254/latest", "https://user:pw@shop.example/",
])
def test_unsafe_urls_are_blocked(url):
    with pytest.raises(UnsafeURL):
        validate_url(url)


def test_token_is_redacted_from_logs():
    record = logging.LogRecord("x", logging.INFO, "", 0, "token %s", ("123:SECRET",), None)
    main.SecretRedactionFilter(["123:SECRET"]).filter(record)
    assert "SECRET" not in record.getMessage()


def test_pricing_rules():
    assert parse_money("R$ 1.234,56") == Decimal("1234.56")
    with pytest.raises(InvalidMoney):
        parse_money("abc")
    result = calculate_price(Decimal("100"), Decimal("10"), Decimal("5"), Decimal("20"))
    assert (result.effective_price, result.possible_effective_price) == (
        Decimal("105.00"), Decimal("85.00"),
    )
    assert calculate_price(Decimal("100"), None).effective_price is None


def test_two_tone_color_is_not_a_conflict():
    attributes = explicit_attributes("Climatizador 16L Branco/Cinza")
    assert attributes["color"] == "branco/cinza"
    assert "conflict_color" in explicit_attributes("Camiseta Preta Branca")
