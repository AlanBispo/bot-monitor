"""End-to-end flows: Telegram handlers, monitor and SQLite, with only the network faked."""

import logging
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from telegram.ext import CommandHandler

import main
from app.alerts import AlertActions, AlertEngine, AlertSettings
from app.bot import (
    TelegramBot,
    build_application,
    make_sender,
    send_daily_summary,
    send_summary_if_due,
)
from app.bot.sender import is_quiet
from app.database import Database
from app.discovery import CandidateURL, DiscoveryOrchestrator, DiscoveryProvider
from app.identity import explicit_attributes
from app.models import SearchEntry, StoreOffer
from app.monitor import Monitor, ScheduleSettings
from app.pricing import InvalidMoney, calculate_price, format_brl, parse_money, unit_price
from app.security import UnsafeURL, validate_url
from app.settings import Settings
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
    async def deliver(message, actions=None):
        sent.append(f"{message}\n{actions.url}" if actions else message)

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
    assert len(sent) == 1 and "R$ 100,00" in sent[0]


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


def catalog_api(items_status=200, calls=None):
    calls = [] if calls is None else calls
    offers = [
        {"price": 3149, "currency_id": "BRL", "condition": "new",
         "shipping": {"free_shipping": True}},
        {"price": 2929, "currency_id": "BRL", "condition": "new",
         "shipping": {"free_shipping": False, "cost": 145.49}},
        {"price": 100, "currency_id": "BRL", "condition": "used", "shipping": {}},
    ]

    def handler(request):
        calls.append(request.url.path)
        if request.url.path == "/oauth/token":
            return httpx.Response(200, json={"access_token": "t", "expires_in": 21600})
        assert request.headers["authorization"] == "Bearer t"
        if request.url.path.endswith("/items"):
            return httpx.Response(items_status, json={"results": offers})
        return httpx.Response(200, json={"name": "Ar Condicionado Midea 12000 Btus"})

    adapter = MercadoLivreAdapter(15, 2_000_000, "id", "fixture")
    adapter._client = lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return adapter, calls


CATALOG_URL = "https://www.mercadolivre.com.br/p/MLB123?sid=cart"


async def test_mercadolivre_catalog_link_uses_api_and_reuses_token(db):
    adapter, calls = catalog_api()
    response = update()
    await TelegramBot(db, {adapter.name: adapter}, 123).add(response, args(CATALOG_URL))
    assert "Produto cadastrado" in response.effective_message.replies[0]
    assert db.list_products()[0].name == "Ar Condicionado Midea 12000 Btus"
    offer = await adapter.get_offer(CATALOG_URL)
    assert offer.product_price == Decimal("2929.00") and offer.shipping_price is None
    assert calls.count("/oauth/token") == 1


async def test_mercadolivre_catalog_without_offers_is_unavailable_and_403_is_a_failure():
    adapter, _ = catalog_api(items_status=404)
    assert not (await adapter.get_offer(CATALOG_URL)).is_available
    adapter, _ = catalog_api(items_status=403)
    with pytest.raises(StoreError, match="HTTP 403"):
        await adapter.get_offer(CATALOG_URL)


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
    assert len(sent) == 1 and "shop: sem resposta válida há 2" in sent[0]
    assert len(db.history(listing.id)) == 1
    assert db.product_history_stats(product.id).current_price is None
    shop.error = None
    await monitor.check_once(send)
    assert sent[-1].startswith("✅ shop voltou")


async def test_status_separates_current_price_from_failed_check(db, send):
    _, healthy = register(db, target="1", url=URL + "/a")
    _, broken = db.register_product_listing(
        "Outro", Decimal("1"), "amazon", "https://www.amazon.com.br/dp/X", "Outro",
    )
    observe(db, healthy.id, "2750.28")
    observe(db, broken.id, "382.50")
    db.record_check(healthy.id)
    db.record_check(broken.id, error="HTTP 503")
    db.record_store_result("amazon", error="HTTP 503", threshold=6)
    response = update()
    await TelegramBot(db, {}, 123).status(response, args())
    text = response.effective_message.replies[0]
    assert "⚠️ Amazon: sem resposta válida há 1 verificações" in text
    assert "✅ Produto X\nshop · R$ 2.750,28 · frete não informado" in text
    assert "⚠️ Outro\nAmazon · último preço R$ 382,50" in text
    assert "HTTP 503" in text


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


async def test_summary_due_sends_once_a_day_and_only_with_news(db, send, sent):
    await send_summary_if_due(db, send, 0, 0)
    assert sent == []
    _, listing = register(db)
    db.add_queued_alert(listing.id, "BACK_IN_STOCK", Decimal("10"), Decimal("10"))
    db.set_state("last_daily_summary", "2000-01-01")
    await send_summary_if_due(db, send, 0, 0)
    await send_summary_if_due(db, send, 0, 0)
    assert len(sent) == 1


def test_backup_runs_once_a_day_and_keeps_recent_copies(db):
    folder = db.path.parent / "backups"
    folder.mkdir()
    for day in range(1, 4):
        (folder / f"prices-2000-01-0{day}.db").touch()
    db.backup_if_due(2)
    db.backup_if_due(2)
    names = sorted(path.name for path in folder.iterdir())
    assert len(names) == 2 and names[0] == "prices-2000-01-03.db"
    assert (folder / names[1]).stat().st_size > 0


async def test_opening_a_search_choice_discards_pending_target_edit(db):
    product, _ = register(db, target="50")
    session = db.create_search_session(123, "Produto X", "{}", "complete", {}, [SearchEntry(
        "", "R1", 1, "shop", URL + "/novo", None, "Produto X", None, Decimal("10"), Decimal("0"),
        Decimal("10"), None, "matched", "ok", None, "fixture",
    )])
    bot = TelegramBot(db, {"shop": Shop()}, 123)
    await bot.product_button(tap(f"prd:target:{product.id}")[0], None)
    await bot.selection_button(tap(f"sel:{session.id}:R1")[0], None)
    assert not bot.pending_target
    await bot.selection_price(update("42,90"), None)
    assert db.get_product(product.id).target_price == Decimal("50")


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


async def test_generic_sites_have_independent_health(db, send, sent):
    class Sites(Shop):
        name = "generic"
        domains = set()

        async def get_offer(self, url):
            if "ruim" in url:
                raise StoreError("HTTP 500")
            return await super().get_offer(url)

    for url in ("https://www.ruim.example/a", "https://bom.example/a"):
        db.register_product_listing("Produto X", Decimal("1"), "generic", url, "Produto X")
    monitor = Monitor(db, {"generic": Sites()}, AlertEngine(db),
                      schedule=ScheduleSettings(store_failure_threshold=1))
    await monitor.check_once(send)
    assert len(sent) == 1 and "ruim.example: sem resposta" in sent[0]


def test_settings_error_names_the_variable(monkeypatch):
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_ID", "1")
    monkeypatch.setenv("DAILY_SUMMARY_TIME", "abc")
    with pytest.raises(ValueError, match="DAILY_SUMMARY_TIME"):
        Settings.from_env()


def test_slow_commands_run_in_background_and_errors_are_handled(db):
    application = build_application(db, {}, "123:test-token", 123, None)
    blocking = {
        name: handler.block
        for handler in application.handlers[0] if isinstance(handler, CommandHandler)
        for name in handler.commands
    }
    assert not blocking["buscar"] and not blocking["check"] and blocking["lista"]
    assert application.error_handlers


async def test_error_handler_only_notifies_the_authorized_chat(db):
    messages = []

    async def send_message(chat_id, text):
        messages.append(chat_id)

    context = SimpleNamespace(
        error=RuntimeError("x"), bot=SimpleNamespace(send_message=send_message),
    )
    bot = TelegramBot(db, {}, 123)
    await bot.on_error(update(chat_id=999), context)
    await bot.on_error(None, context)
    assert messages == []
    await bot.on_error(update(), context)
    assert messages == [123]


async def test_pasted_link_registers_product_and_clears_pending_edit(db):
    product, _ = register(db, url=URL + "/velho")
    bot = TelegramBot(db, {"shop": Shop()}, 123)
    await bot.product_button(tap(f"prd:target:{product.id}")[0], None)
    response = update(f"olha esse {URL}.")
    await bot.selection_price(response, None)
    assert "Produto cadastrado" in response.effective_message.replies[0]
    assert not bot.pending_target
    assert db.list_listings()[-1].url == URL


class TwoStores(Shop):
    def __init__(self, prices):
        super().__init__()
        self.prices = prices

    async def get_offer(self, url):
        return StoreOffer("shop", "Produto X", url, Decimal(self.prices[url]), Decimal("0"))


async def test_product_alerts_only_cheapest_store_and_reports_new_leader(db, send, sent):
    product, first = register(db, target="200", url=URL + "/a")
    second = db.add_listing(product.id, "shop", URL + "/b", "Produto X")
    shop = TwoStores({first.url: "100", second.url: "102"})
    monitor = Monitor(db, {"shop": shop}, AlertEngine(db))
    assert await monitor.check_once(send) == 1
    assert len(sent) == 1 and first.url in sent[0]
    assert db.queued_alerts() == []

    shop.prices[second.url] = "99.50"
    await monitor.check_once(send)
    assert db.queued_alerts() == []

    shop.prices[second.url] = "90"
    assert await monitor.check_once(send) == 1
    assert second.url in sent[-1]
    assert [alert.alert_type for alert in db.queued_alerts()] == ["CHEAPER_STORE"]


async def test_price_drop_message_leads_with_percent_and_price(db, send, sent):
    title = "Ar Condicionado Split Midea Inverter AI 12000 BTU/h Frio 42EFVCA12M8 - 127 Volts"
    _, listing = db.register_product_listing(
        title, Decimal("100"), "amazon", "https://www.amazon.com.br/dp/X", title,
        monitoring_mode="price_drop",
    )
    product = db.get_product(listing.product_id)
    observe(db, listing.id, "2664.28")

    async def collect(message, actions):
        sent.append((message, actions))

    snapshot = db.add_snapshot(
        listing.id, Decimal("2344.00"), None, None, None, None, None, True, None,
    )
    assert await AlertEngine(db).process(product, listing, snapshot, collect)
    message, actions = sent[0]
    lines = message.split("\n")
    assert lines[0] == "📉 Caiu 12% → R$ 2.344,00"
    assert len(lines[1]) <= 80
    assert lines[3] == "Antes R$ 2.664,28 · economia de R$ 320,28"
    assert lines[4] == "Amazon · frete não informado"
    assert listing.url not in message and actions.url == listing.url


async def test_alert_says_how_good_the_price_is(db, send, sent):
    _, listing = register(db, target="70")
    for day in range(1, 13):
        observe(db, listing.id, str(100 + day), days_ago=day)
    await Monitor(db, {"shop": Shop("60")}, AlertEngine(db)).check_once(send)
    assert "entre os 10% menores" in sent[0]


async def test_snooze_silences_alerts_but_keeps_collecting(db, send, sent):
    product, listing = register(db, target="200")
    bot = TelegramBot(db, {}, 123)
    await bot.product_button(tap(f"prd:snooze:{product.id}")[0], None)
    await Monitor(db, {"shop": Shop()}, AlertEngine(db)).check_once(send)
    assert sent == [] and len(db.history(listing.id)) == 1
    db.reactivate_product(product.id)
    await Monitor(db, {"shop": Shop()}, AlertEngine(db)).check_once(send)
    assert len(sent) == 1


async def test_sender_adds_alert_buttons_and_silences_quiet_hours():
    calls = []

    async def send_message(chat_id, text, **kwargs):
        calls.append(kwargs)

    bot = SimpleNamespace(send_message=send_message)
    always = (time(0, 0), time(23, 59, 59, 999999))
    await make_sender(bot, 1, always)("oi", AlertActions(URL, 7))
    assert calls[0]["disable_notification"] is True
    rows = calls[0]["reply_markup"].inline_keyboard
    assert rows[0][0].url == URL and rows[1][2].callback_data == "prd:snooze:7"
    await make_sender(bot, 1, None)("oi")
    assert calls[1]["disable_notification"] is False
    assert is_quiet(time(23, 0), (time(22, 0), time(7, 0)))
    assert not is_quiet(time(12, 0), (time(22, 0), time(7, 0)))


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


def test_unit_price_uses_measure_and_pack():
    assert unit_price("Sabão líquido 3L", Decimal("29.90")) == "R$ 9,97/L"
    assert unit_price("Arroz 500g", Decimal("5")) == "R$ 10,00/kg"
    assert unit_price("Refrigerante 2L pack com 6 unidades", Decimal("60")) == "R$ 5,00/L"
    assert unit_price("Celular 128GB", Decimal("1000")) is None


def test_pricing_rules():
    assert format_brl(Decimal("2664.28")) == "R$ 2.664,28"
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
