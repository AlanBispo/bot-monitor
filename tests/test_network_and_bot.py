import asyncio
import json
import logging
import socket
import sqlite3
import sys
import time
from decimal import Decimal

import httpx
import pytest

from app.bot import TelegramBot, split_telegram_text
from app.database import Database
from app.models import StoreOffer
from app.network import _PinnedNetworkBackend
from app.security import UnsafeURL, resolve_public_host
from app.stores.amazon import AmazonAdapter
from app.stores.base import StoreAdapter, StoreError
from app.stores.http import request_limited
from app.stores.magalu import MagaluAdapter
from app.stores.mercadolivre import MercadoLivreAdapter
from main import SecretRedactionFilter


@pytest.mark.asyncio
async def test_redirect_is_validated_before_second_request():
    requests = []

    def handler(request):
        requests.append(str(request.url))
        return httpx.Response(
            302,
            headers={"location": "http://127.0.0.1/private"},
            request=request,
        )

    adapter = AmazonAdapter()
    adapter._client = lambda: httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=False
    )
    with pytest.raises(StoreError):
        await adapter._get("https://www.amazon.com.br/start")
    assert requests == ["https://www.amazon.com.br/start"]


@pytest.mark.asyncio
async def test_cross_domain_store_redirect_is_rejected_before_second_request():
    requests = []

    def handler(request):
        requests.append(str(request.url))
        return httpx.Response(
            302, headers={"location": "https://shop.example/other"}, request=request
        )

    adapter = AmazonAdapter()
    adapter._client = lambda: httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=False
    )
    with pytest.raises(StoreError):
        await adapter._get("https://www.amazon.com.br/start")
    assert requests == ["https://www.amazon.com.br/start"]


def test_mixed_dns_answers_are_rejected(monkeypatch):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.2", 443)),
        ],
    )
    with pytest.raises(UnsafeURL):
        resolve_public_host("example.com", 443)


def test_network_backend_connects_to_vetted_ip():
    called = []

    class StubBackend:
        async def connect_tcp(self, host, port, **kwargs):
            called.append((host, port))
            return object()

        async def sleep(self, seconds):
            return None

    backend = _PinnedNetworkBackend(lambda host, port: "93.184.216.34")
    backend._backend = StubBackend()
    asyncio.run(backend.connect_tcp("example.com", 443))
    assert called == [("93.184.216.34", 443)]


def test_dns_resolution_is_bounded_by_connect_timeout():
    def slow_resolver(host, port):
        time.sleep(0.1)
        return "93.184.216.34"

    backend = _PinnedNetworkBackend(slow_resolver)
    with pytest.raises(TimeoutError):
        asyncio.run(backend.connect_tcp("example.com", 443, timeout=0.01))


@pytest.mark.asyncio
async def test_http_response_body_is_capped_while_streaming():
    async def handler(request):
        return httpx.Response(200, content=b"x" * 100, request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(StoreError, match="limite"):
            await request_limited(client, "https://shop.example/item", 20)


@pytest.mark.parametrize(
    ("adapter", "url"),
    [
        (AmazonAdapter(), "https://www.amazon.com.br/dp/blocked"),
        (MercadoLivreAdapter(), "https://www.mercadolivre.com.br/item/blocked"),
        (MagaluAdapter(), "https://www.magazineluiza.com.br/item/blocked"),
    ],
)
@pytest.mark.asyncio
async def test_required_store_blocked_response_fails_without_retry_or_bypass(adapter, url):
    requests = []

    def handler(request):
        requests.append(str(request.url))
        return httpx.Response(403, text="blocked", request=request)

    adapter._client = lambda: httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=False
    )
    with pytest.raises(StoreError):
        await adapter._get(url)
    assert requests == [url]


def test_telegram_text_is_split_under_platform_limit_and_long_records_are_split():
    records = ["Produto\nhttps://amazon.example/" + "x" * 1900 for _ in range(10)]
    chunks = split_telegram_text("\n".join(records))
    assert len(chunks) > 1
    assert all(len(chunk) <= 4096 for chunk in chunks)
    oversized = split_telegram_text("x" * 5000)
    assert [len(chunk) for chunk in oversized] == [4096, 904]


def test_secret_redaction_filter_removes_token_from_log_record():
    sentinel = "TOKEN_SENTINEL_VALUE"
    record = logging.LogRecord("httpx", logging.INFO, __file__, 1, "URL %s", (sentinel,), None)
    assert SecretRedactionFilter([sentinel]).filter(record)
    assert sentinel not in record.getMessage()
    assert "[REDACTED]" in record.getMessage()


def test_secret_redaction_filter_removes_exception_details():
    sentinel = "TOKEN_EXCEPTION_SENTINEL"
    try:
        raise RuntimeError(sentinel)
    except RuntimeError:
        record = logging.LogRecord(
            "telegram", logging.ERROR, __file__, 1, "falha", (), sys.exc_info()
        )
    assert SecretRedactionFilter([sentinel]).filter(record)
    assert record.exc_info is None


class FakeMessage:
    def __init__(self):
        self.replies = []

    async def reply_text(self, message, **kwargs):
        self.replies.append(message)


class FakeUpdate:
    def __init__(self, chat_id=999):
        self.effective_chat = type("Chat", (), {"id": chat_id, "type": "private"})()
        self.effective_message = FakeMessage()


class RegistrationAdapter(StoreAdapter):
    name = "shop"
    domains = {"shop.example"}

    async def search(self, query):
        return []

    async def get_offer(self, url):
        return StoreOffer("shop", "Produto normalizado", url, 20, 0)


class UnavailableRegistrationAdapter(RegistrationAdapter):
    async def get_offer(self, url):
        return StoreOffer(
            "shop",
            "Produto indisponível",
            url,
            20,
            0,
            observation_status="unavailable",
        )


@pytest.mark.asyncio
async def test_all_registered_commands_reject_unauthorized_chat(tmp_path):
    database = Database(tmp_path / "prices.db")
    database.initialize()
    bot = TelegramBot(database, {}, 123)
    update = FakeUpdate()
    context = type("Context", (), {"args": [], "application": None, "bot": None})()
    handlers = (
        bot.help,
        bot.search,
        bot.select,
        bot.confirm,
        bot.add,
        bot.attach,
        bot.list_products,
        bot.history,
        bot.remove,
        bot.status,
        bot.check,
    )
    for handler in handlers:
        await handler(update, context)
    assert update.effective_message.replies == ["Acesso não autorizado."] * len(handlers)


@pytest.mark.asyncio
async def test_callback_rejects_unauthorized_chat(tmp_path):
    database = Database(tmp_path / "prices.db")
    database.initialize()
    bot = TelegramBot(database, {}, 123)
    answers = []

    message = type(
        "Message", (), {"chat": type("Chat", (), {"type": "private"})(), "chat_id": 999}
    )()

    class Query:
        data = "sel:x:R1"

        def __init__(self, callback_message):
            self.message = callback_message

        async def answer(self, text=None, **kwargs):
            answers.append(text)

    query = Query(message)
    update = type("Update", (), {"callback_query": query})()
    await bot.selection_button(update, None)
    assert answers == ["Acesso não autorizado."]


@pytest.mark.asyncio
async def test_bot_can_attach_multiple_listings_to_one_product(tmp_path):
    database = Database(tmp_path / "prices.db")
    database.initialize()
    bot = TelegramBot(database, {"shop": RegistrationAdapter()}, 123)
    update = FakeUpdate(123)
    context = type("Context", (), {"args": ["15,00", "https://shop.example/one"]})()
    await bot.add(update, context)

    attach_update = FakeUpdate(123)
    attach_context = type("Context", (), {"args": ["1", "https://shop.example/two"]})()
    await bot.attach(attach_update, attach_context)

    products = database.list_products()
    listings = database.list_listings(active_only=False)
    assert len(products) == 1
    assert len(listings) == 2
    assert {listing.product_id for listing in listings} == {products[0].id}


@pytest.mark.asyncio
async def test_direct_registration_persists_identity_and_shipping_uncertainty(tmp_path):
    database = Database(tmp_path / "prices.db")
    database.initialize()

    class UnknownShippingRegistrationAdapter(RegistrationAdapter):
        async def get_offer(self, url):
            return StoreOffer(
                "shop", "Celular modelo X 128GB", url, Decimal("100"), None
            )

    bot = TelegramBot(database, {"shop": UnknownShippingRegistrationAdapter()}, 123)
    update = FakeUpdate(123)
    await bot.add(
        update,
        type("Context", (), {"args": ["100", "https://shop.example/item"]})(),
    )
    product = database.list_products()[0]
    listing = database.list_listings()[0]
    identity = json.loads(product.identity_json)
    assert identity["attributes"]["model"] == "x"
    assert identity["attributes"]["capacity"] == "128 gb"
    assert identity["registration_source"] == "direct_url"
    assert listing.uncertainty == "shipping_unknown"


@pytest.mark.asyncio
async def test_registration_rejects_unavailable_status_even_when_flag_is_true(tmp_path):
    database = Database(tmp_path / "prices.db")
    database.initialize()
    bot = TelegramBot(database, {"shop": UnavailableRegistrationAdapter()}, 123)
    update = FakeUpdate(123)
    context = type("Context", (), {"args": ["15,00", "https://shop.example/one"]})()

    await bot.add(update, context)

    assert update.effective_message.replies == [
        "Não foi possível validar um produto nessa URL."
    ]
    assert database.list_products() == []

    product = database.add_product("Produto", Decimal("15.00"))
    attach_update = FakeUpdate(123)
    attach_context = type(
        "Context", (), {"args": [str(product.id), "https://shop.example/two"]}
    )()

    await bot.attach(attach_update, attach_context)

    assert attach_update.effective_message.replies == [
        "Não foi possível validar um produto nessa URL."
    ]
    assert database.list_listings(active_only=False) == []


@pytest.mark.asyncio
async def test_attach_rejects_a_different_capacity_variant(tmp_path):
    database = Database(tmp_path / "prices.db")
    database.initialize()
    product = database.add_product(
        "Celular modelo X 128GB", Decimal("100"), source_query="Celular modelo X 128GB"
    )

    class WrongVariantAdapter(RegistrationAdapter):
        async def get_offer(self, url):
            return StoreOffer(
                "shop", "Celular modelo X 256GB", url, Decimal("80"), Decimal("0")
            )

    bot = TelegramBot(database, {"shop": WrongVariantAdapter()}, 123)
    update = FakeUpdate(123)
    await bot.attach(
        update,
        type("Context", (), {"args": [str(product.id), "https://shop.example/other"]})(),
    )
    assert "hard_mismatch_capacity" in update.effective_message.replies[0]
    assert database.list_listings(active_only=False) == []


@pytest.mark.asyncio
async def test_history_reports_current_uncertainty_after_latest_unavailable_observation(tmp_path):
    database = Database(tmp_path / "prices.db")
    database.initialize()
    product = database.add_product("Produto", 30)
    listing = database.add_listing(product.id, "shop", "https://shop.example/item", "Produto")
    database.add_snapshot(
        listing.id, 29.90, 0, 0, None, 29.90, None, True, None
    )
    database.add_snapshot(listing.id, None, None, None, None, None, None, False, None)
    bot = TelegramBot(database, {}, 123)
    update = FakeUpdate(123)
    context = type("Context", (), {"args": [str(product.id)]})()

    await bot.history(update, context)

    assert "Atual: indisponível ou não calculável" in update.effective_message.replies[0]


@pytest.mark.asyncio
async def test_database_failure_in_status_is_reported_without_leaking_details(
    tmp_path, monkeypatch
):
    database = Database(tmp_path / "prices.db")
    database.initialize()
    bot = TelegramBot(database, {}, 123)
    monkeypatch.setattr(
        database, "list_products", lambda **kwargs: (_ for _ in ()).throw(
            sqlite3.OperationalError("private database path")
        )
    )
    update = FakeUpdate(123)
    await bot.status(update, type("Context", (), {"args": []})())
    assert update.effective_message.replies == [
        "Falha no banco de dados; tente novamente."
    ]
