import sqlite3
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.alerts import AlertEngine
from app.bot import TelegramBot, build_application
from app.database import Database
from app.models import StoreOffer
from app.monitor import Monitor
from app.stores.base import StoreAdapter, StoreError


class PriceAdapter(StoreAdapter):
    name = "shop"
    domains = {"shop.example"}

    def __init__(self, shipping=None):
        self.price = Decimal("100")
        self.shipping = shipping
        self.available = True
        self.broken = False
        self.calls = []

    async def search(self, query):
        raise AssertionError("Monitoring must only fetch saved URLs")

    async def get_offer(self, url):
        self.calls.append(url)
        if self.broken:
            raise StoreError("falha de consulta")
        return StoreOffer(
            self.name, "Produto modelo X 128GB", url, self.price, self.shipping,
            available=self.available,
        )


class Message:
    def __init__(self):
        self.replies = []

    async def reply_text(self, text):
        self.replies.append(text)


def update(chat_id=123):
    return SimpleNamespace(
        effective_chat=SimpleNamespace(id=chat_id, type="private"),
        effective_message=Message(),
    )


@pytest.fixture
def setup(tmp_path):
    database = Database(tmp_path / "prices.db")
    database.initialize()
    adapter = PriceAdapter()
    bot = TelegramBot(database, {"shop": adapter}, 123)
    return database, adapter, bot


@pytest.mark.parametrize("shipping", [None, Decimal("10")])
async def test_link_only_captures_initial_price_and_alerts_each_lower_price(setup, shipping):
    database, adapter, bot = setup
    adapter.shipping = shipping
    registration = update()
    url = "https://shop.example/item"
    await bot.add(registration, SimpleNamespace(args=[url]))
    product = database.list_products()[0]
    listing = database.list_listings()[0]
    assert product.monitoring_mode == "price_drop"
    assert database.history(listing.id)[0].product_price == Decimal("100")
    assert "Preço atual do produto salvo: R$ 100.00" in registration.effective_message.replies[0]
    sent = []

    async def send(message):
        sent.append(message)

    for price, expected in [("100", 0), ("110", 0), ("90", 1), ("90", 0),
                            ("95", 0), ("80", 1)]:
        # Recreate the monitor to prove the reference survives restarts.
        adapter.price = Decimal(price)
        monitor = Monitor(database, {"shop": adapter}, AlertEngine(database))
        assert await monitor.check_once(send) == expected
    assert len(sent) == 2
    assert "Referência anterior: R$ 100.00" in sent[0]
    assert "Referência anterior: R$ 90.00" in sent[1]
    assert "sem frete" in sent[0]
    if shipping is None:
        assert "Frete: desconhecido" in sent[0]
    assert len(database.history(listing.id)) == 7
    assert set(adapter.calls) == {url}


@pytest.mark.parametrize("handler_name", ["add", "add_target"])
async def test_explicit_target_and_legacy_add_compare_effective_price(setup, handler_name):
    database, adapter, bot = setup
    adapter.shipping = Decimal("20")
    await getattr(bot, handler_name)(
        update(), SimpleNamespace(args=["90,00", "https://shop.example/item"]),
    )
    product = database.list_products()[0]
    assert product.monitoring_mode == "target"
    assert product.target_price == Decimal("90")
    assert len(database.history(database.list_listings()[0].id)) == 1
    sent = []

    async def send(message):
        sent.append(message)

    monitor = Monitor(database, {"shop": adapter}, AlertEngine(database))
    adapter.price = Decimal("90")
    assert await monitor.check_once(send) == 0  # Product is at target, total is 110.
    adapter.price = Decimal("70")
    assert await monitor.check_once(send) == 1  # Total is exactly 90.
    assert await monitor.check_once(send) == 0
    assert "Preço-alvo atingido" in sent[0]


@pytest.mark.parametrize("handler_name", ["add", "add_target"])
async def test_both_registration_modes_reject_unauthorized_chat(setup, handler_name):
    database, adapter, bot = setup
    attempt = update(999)
    await getattr(bot, handler_name)(
        attempt, SimpleNamespace(args=["https://shop.example/item"]),
    )
    assert attempt.effective_message.replies == ["Acesso não autorizado."]
    assert not database.list_products()
    assert not adapter.calls


async def test_target_without_price_is_rejected(setup):
    database, adapter, bot = setup
    attempt = update()
    await bot.add_target(attempt, SimpleNamespace(args=["https://shop.example/item"]))
    assert "uso:" in attempt.effective_message.replies[0]
    assert not database.list_products()
    assert not adapter.calls


@pytest.mark.parametrize("failure", ["unavailable", "broken"])
async def test_failed_check_preserves_history_and_sends_no_drop_alert(setup, failure):
    database, adapter, bot = setup
    await bot.add(update(), SimpleNamespace(args=["https://shop.example/item"]))
    adapter.price = Decimal("1")
    adapter.available = failure != "unavailable"
    adapter.broken = failure == "broken"
    sent = []

    async def send(message):
        sent.append(message)

    monitor = Monitor(database, {"shop": adapter}, AlertEngine(database))
    assert await monitor.check_once(send) == 0
    assert not sent
    listing = database.list_listings()[0]
    assert database.history(listing.id)[0].product_price == Decimal("100")
    assert database.last_alert(listing.id, "PRICE_DROP") is None


async def test_history_shows_captured_product_price_with_unknown_shipping(setup):
    database, adapter, bot = setup
    await bot.add(update(), SimpleNamespace(args=["https://shop.example/item"]))
    response = update()
    await bot.history(response, SimpleNamespace(args=["1"]))
    text = response.effective_message.replies[0]
    assert "sem frete" in text
    assert "Inicial: R$ 100.00" in text
    assert "Atual: R$ 100.00" in text
    assert "Frete da última observação: desconhecido" in text

    adapter.broken = True

    async def send(message):
        raise AssertionError("No alert is allowed after a collection failure")

    await Monitor(database, {"shop": adapter}, AlertEngine(database)).check_once(send)
    response = update()
    await bot.history(response, SimpleNamespace(args=["1"]))
    text = response.effective_message.replies[0]
    assert "Atual: indisponível ou consulta mais recente falhou" in text


def test_both_commands_are_registered(setup):
    database, adapter, _ = setup
    application = build_application(database, {"shop": adapter}, "123:test-token", 123, None)
    commands = {command for handlers in application.handlers.values()
                for handler in handlers for command in getattr(handler, "commands", ())}
    assert {"add", "add_alvo"} <= commands


def test_existing_database_keeps_target_mode_and_preserves_price_history(tmp_path):
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as connection:
        connection.executescript("""
            CREATE TABLE products (
                id INTEGER PRIMARY KEY, name TEXT NOT NULL, target_price TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            INSERT INTO products VALUES (
                7, 'Produto legado', '30.00', 1,
                '2026-10-26T12:00:00+00:00', '2026-10-26T12:00:00+00:00'
            );
        """)
    database = Database(path)
    database.initialize()
    product = database.get_product(7)
    assert product.target_price == Decimal("30")
    assert product.monitoring_mode == "target"
    listing = database.add_listing(product.id, "shop", "https://shop.example/item", product.name)
    database.add_snapshot(
        listing.id, Decimal("40"), None, Decimal("0"), None, None, None, True, None,
    )
    database.initialize()
    migrated = database.get_product(7)
    assert (migrated.id, migrated.name, migrated.target_price, migrated.active,
            migrated.monitoring_mode) == (7, "Produto legado", Decimal("30"), True, "target")
    assert database.history(listing.id)[0].product_price == Decimal("40")


async def test_failed_initial_snapshot_rolls_back_entire_registration(setup):
    database, _, bot = setup
    with database.connection() as connection:
        connection.executescript("""
            CREATE TRIGGER fail_initial_snapshot BEFORE INSERT ON price_history
            BEGIN SELECT RAISE(ABORT, 'snapshot failure'); END;
        """)
    attempt = update()
    await bot.add(attempt, SimpleNamespace(args=["https://shop.example/item"]))
    assert "não foi possível salvar o cadastro" in attempt.effective_message.replies[0]
    assert not database.list_products()
    assert not database.list_listings(active_only=False)
