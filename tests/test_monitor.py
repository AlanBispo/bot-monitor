from decimal import Decimal

import pytest

from app.alerts import AlertEngine
from app.database import Database
from app.models import StoreOffer
from app.monitor import Monitor
from app.stores.amazon import parse_offer
from app.stores.base import StoreAdapter, StoreError


class GoodAdapter(StoreAdapter):
    name = "good"
    domains = {"good.example"}

    async def search(self, query):
        raise AssertionError("monitor não deve executar busca ampla")

    async def get_offer(self, url):
        return StoreOffer("good", "Produto", url, Decimal("10.00"), Decimal("0.00"))


class BrokenAdapter(StoreAdapter):
    name = "broken"
    domains = {"broken.example"}

    async def search(self, query):
        raise StoreError("falha")

    async def get_offer(self, url):
        raise StoreError("timeout")


class UnknownShippingAdapter(GoodAdapter):
    async def get_offer(self, url):
        return StoreOffer("good", "Produto", url, Decimal("10.00"), None)


class UnavailableWithDisplayedPriceAdapter(GoodAdapter):
    async def get_offer(self, url):
        return StoreOffer(
            "good",
            "Produto",
            url,
            Decimal("1.00"),
            Decimal("0.00"),
            available=False,
            observation_status="unavailable",
        )


class UnavailableFlagAdapter(GoodAdapter):
    async def get_offer(self, url):
        return StoreOffer(
            "good", "Produto", url, Decimal("1.00"), Decimal("0.00"), available=False
        )


class UnavailableStatusAdapter(GoodAdapter):
    async def get_offer(self, url):
        return StoreOffer(
            "good",
            "Produto",
            url,
            Decimal("1.00"),
            Decimal("0.00"),
            observation_status="unavailable",
        )


class ConditionalAmazonShippingAdapter(GoodAdapter):
    name = "amazon"

    async def get_offer(self, url):
        return parse_offer(
            """
            <span id="productTitle">Produto</span>
            <div id="availability">Em estoque</div>
            <div id="corePriceDisplay_desktop_feature_div">
              <span class="a-offscreen">R$ 10,00</span>
            </div>
            <div id="mir-layout-DELIVERY_BLOCK-slot-PRIMARY_DELIVERY_MESSAGE_LARGE">
              Frete grátis para clientes selecionados
            </div>
            """,
            url,
        )


class AmazonAvailabilityAdapter(GoodAdapter):
    name = "amazon"

    def __init__(self, availability_text):
        self.availability_text = availability_text

    async def get_offer(self, url):
        return parse_offer(
            f"""
            <span id="productTitle">Produto</span>
            <div id="corePriceDisplay_desktop_feature_div">
              <span class="a-offscreen">R$ 1,00</span>
            </div>
            <div id="availability">{self.availability_text}</div>
            <div id="mir-layout-DELIVERY_BLOCK-slot-PRIMARY_DELIVERY_MESSAGE_LARGE">
              Frete grátis
            </div>
            """,
            url,
        )


class MalformedMoneyAdapter(GoodAdapter):
    async def get_offer(self, url):
        return parse_offer(
            """
            <span id="productTitle">Produto</span>
            <div id="availability">Em estoque</div>
            <div id="corePriceDisplay_desktop_feature_div">
              <span class="a-offscreen">R$ 1.2,34</span>
            </div>
            <div id="mir-layout-DELIVERY_BLOCK-slot-PRIMARY_DELIVERY_MESSAGE_LARGE">
              Frete grátis
            </div>
            """,
            url,
        )


@pytest.mark.asyncio
async def test_monitor_isolates_store_failure_and_alerts_valid_listing(tmp_path):
    db = Database(tmp_path / "prices.db")
    db.initialize()
    good_product = db.add_product("Produto bom", Decimal("10.00"))
    broken_product = db.add_product("Produto indisponível", Decimal("10.00"))
    good_listing = db.add_listing(
        good_product.id, "good", "https://good.example/item", "Produto bom"
    )
    broken_listing = db.add_listing(
        broken_product.id, "broken", "https://broken.example/item", "Produto indisponível"
    )
    monitor = Monitor(
        db,
        {"good": GoodAdapter(), "broken": BrokenAdapter()},
        AlertEngine(db),
    )
    messages = []

    async def send(message):
        messages.append(message)

    assert await monitor.check_once(send) == 1
    assert len(messages) == 1
    assert db.history(good_listing.id)[0].effective_price == Decimal("10.00")
    assert db.history(broken_listing.id) == []


@pytest.mark.asyncio
async def test_unknown_shipping_is_persisted_as_uncalculated_without_alert(tmp_path):
    db = Database(tmp_path / "prices.db")
    db.initialize()
    product = db.add_product("Produto", Decimal("10.00"))
    listing = db.add_listing(product.id, "good", "https://good.example/item", "Produto")
    monitor = Monitor(
        db,
        {"good": UnknownShippingAdapter()},
        AlertEngine(db),
    )
    sent = []

    async def send(message):
        sent.append(message)

    assert await monitor.check_once(send) == 0
    snapshot = db.history(listing.id)[0]
    assert snapshot.available
    assert snapshot.shipping_price is None
    assert snapshot.effective_price is None
    assert sent == []


@pytest.mark.asyncio
async def test_explicit_unavailable_observation_never_alerts_even_with_displayed_price(tmp_path):
    db = Database(tmp_path / "prices.db")
    db.initialize()
    product = db.add_product("Produto", Decimal("10.00"))
    listing = db.add_listing(product.id, "good", "https://good.example/unavailable", "Produto")
    monitor = Monitor(db, {"good": UnavailableWithDisplayedPriceAdapter()}, AlertEngine(db))
    sent = []

    async def send(message):
        sent.append(message)

    assert await monitor.check_once(send) == 0
    snapshot = db.history(listing.id)[0]
    assert snapshot.available is False
    assert snapshot.effective_price is None
    assert sent == []


@pytest.mark.asyncio
async def test_available_false_never_alerts_even_with_default_available_status(tmp_path):
    db = Database(tmp_path / "prices.db")
    db.initialize()
    product = db.add_product("Produto", Decimal("10.00"))
    listing = db.add_listing(product.id, "good", "https://good.example/unavailable", "Produto")
    monitor = Monitor(db, {"good": UnavailableFlagAdapter()}, AlertEngine(db))
    sent = []

    async def send(message):
        sent.append(message)

    assert await monitor.check_once(send) == 0
    snapshot = db.history(listing.id)[0]
    assert snapshot.available is False
    assert snapshot.effective_price is None
    assert sent == []


@pytest.mark.asyncio
async def test_unavailable_status_never_alerts_even_with_default_available_flag(tmp_path):
    db = Database(tmp_path / "prices.db")
    db.initialize()
    product = db.add_product("Produto", Decimal("10.00"))
    listing = db.add_listing(product.id, "good", "https://good.example/unavailable", "Produto")
    monitor = Monitor(db, {"good": UnavailableStatusAdapter()}, AlertEngine(db))
    sent = []

    async def send(message):
        sent.append(message)

    assert await monitor.check_once(send) == 0
    snapshot = db.history(listing.id)[0]
    assert snapshot.available is False
    assert snapshot.effective_price is None
    assert sent == []


@pytest.mark.asyncio
async def test_conditional_shipping_never_alerts(tmp_path):
    db = Database(tmp_path / "prices.db")
    db.initialize()
    product = db.add_product("Produto", Decimal("10.00"))
    listing = db.add_listing(product.id, "amazon", "https://good.example/item", "Produto")
    monitor = Monitor(db, {"amazon": ConditionalAmazonShippingAdapter()}, AlertEngine(db))
    sent = []

    async def send(message):
        sent.append(message)

    assert await monitor.check_once(send) == 0
    snapshot = db.history(listing.id)[0]
    assert snapshot.available
    assert snapshot.shipping_price is None
    assert snapshot.effective_price is None
    assert sent == []


@pytest.mark.parametrize(
    "availability_text",
    ["Não disponível.", "Consulte a disponibilidade.", "Not available."],
)
@pytest.mark.asyncio
async def test_negative_or_unknown_availability_never_alerts(availability_text, tmp_path):
    db = Database(tmp_path / "prices.db")
    db.initialize()
    product = db.add_product("Produto", Decimal("1.00"))
    listing = db.add_listing(product.id, "amazon", "https://good.example/item", "Produto")
    monitor = Monitor(
        db,
        {"amazon": AmazonAvailabilityAdapter(availability_text)},
        AlertEngine(db),
    )
    sent = []

    async def send(message):
        sent.append(message)

    assert await monitor.check_once(send) == 0
    rows = db.history(listing.id)
    if availability_text == "Consulte a disponibilidade.":
        assert rows == []
    else:
        assert len(rows) == 1
        assert rows[0].available is False
        assert rows[0].effective_price is None
    assert sent == []


@pytest.mark.asyncio
async def test_unknown_availability_preserves_previous_valid_observation(tmp_path):
    db = Database(tmp_path / "prices.db")
    db.initialize()
    product = db.add_product("Produto", Decimal("1"))
    listing = db.add_listing(product.id, "amazon", "https://good.example/item", "Produto")
    prior = db.add_snapshot(
        listing.id, Decimal("2"), Decimal("0"), Decimal("0"), None,
        Decimal("2"), None, True, None,
    )
    monitor = Monitor(
        db, {"amazon": AmazonAvailabilityAdapter("Consulte a disponibilidade.")},
        AlertEngine(db),
    )
    sent = []

    async def send(message):
        sent.append(message)

    assert await monitor.check_once(send) == 0
    assert db.history(listing.id) == [prior]
    assert sent == []


@pytest.mark.asyncio
async def test_malformed_product_money_creates_no_snapshot_or_alert(tmp_path):
    db = Database(tmp_path / "prices.db")
    db.initialize()
    product = db.add_product("Produto", Decimal("1.00"))
    listing = db.add_listing(product.id, "good", "https://good.example/item", "Produto")
    monitor = Monitor(db, {"good": MalformedMoneyAdapter()}, AlertEngine(db))
    sent = []

    async def send(message):
        sent.append(message)

    assert await monitor.check_once(send) == 0
    assert db.history(listing.id) == []
    assert sent == []


@pytest.mark.asyncio
async def test_monitor_ignores_unconfirmed_listing(tmp_path):
    db = Database(tmp_path / "prices.db")
    db.initialize()
    product = db.add_product("Produto", Decimal("10.00"))
    listing = db.add_listing(
        product.id,
        "good",
        "https://good.example/unconfirmed",
        "Produto",
        validation_status="candidate",
    )
    adapter = GoodAdapter()
    monitor = Monitor(db, {"good": adapter}, AlertEngine(db))
    sent = []

    async def send(message):
        sent.append(message)

    assert await monitor.check_once(send) == 0
    assert db.history(listing.id) == []


@pytest.mark.asyncio
async def test_monitor_skips_product_changed_at_confirmed_url(tmp_path):
    db = Database(tmp_path / "prices.db")
    db.initialize()
    product = db.add_product(
        "Celular modelo X 128GB", Decimal("100"), source_query="Celular modelo X 128GB"
    )
    listing = db.add_listing(
        product.id, "good", "https://good.example/item", "Celular modelo X 128GB"
    )

    class ChangedPageAdapter(GoodAdapter):
        async def get_offer(self, url):
            return StoreOffer(
                "good", "Celular modelo X 256GB", url, Decimal("1"), Decimal("0")
            )

    sent = []

    async def send(message):
        sent.append(message)

    monitor = Monitor(db, {"good": ChangedPageAdapter()}, AlertEngine(db))
    assert await monitor.check_once(send) == 0
    assert db.history(listing.id) == []
    assert sent == []
