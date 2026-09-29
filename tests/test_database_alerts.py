import sqlite3
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.alerts import AlertEngine, target_reached
from app.database import Database, DuplicateListingError


def make_db(tmp_path):
    db = Database(tmp_path / "prices.db")
    db.initialize()
    return db


def test_products_listings_and_append_only_history(tmp_path):
    db = make_db(tmp_path)
    product = db.add_product("OMO 3L", Decimal("30.00"))
    listing = db.add_listing(product.id, "amazon", "https://www.amazon.com.br/dp/X", "OMO 3L")
    db.add_snapshot(
        listing.id,
        Decimal("31.00"),
        Decimal("0"),
        Decimal("0"),
        None,
        Decimal("31.00"),
        None,
        True,
        None,
    )
    db.add_snapshot(
        listing.id,
        Decimal("29.90"),
        Decimal("0"),
        Decimal("0"),
        None,
        Decimal("29.90"),
        None,
        True,
        None,
    )
    history = db.history(listing.id)
    assert len(history) == 2
    assert history[0].effective_price == Decimal("31.00")
    assert history[1].effective_price == Decimal("29.90")


def test_alert_equal_target_and_duplicate_policy(tmp_path):
    db = make_db(tmp_path)
    product = db.add_product("Produto", Decimal("30.00"))
    listing = db.add_listing(product.id, "amazon", "https://www.amazon.com.br/dp/Y", "Produto")
    engine = AlertEngine(db)
    snapshot = db.add_snapshot(
        listing.id,
        Decimal("30.00"),
        Decimal("0"),
        Decimal("0"),
        None,
        Decimal("30.00"),
        None,
        True,
        None,
    )
    sent = []

    async def send(message):
        sent.append(message)

    import asyncio

    assert target_reached(snapshot, product.target_price)
    assert asyncio.run(engine.process(product, listing, snapshot, send))
    assert not asyncio.run(engine.process(product, listing, snapshot, send))
    assert len(sent) == 1
    assert db.last_alert(listing.id, "TARGET_REACHED") is not None


def test_concurrent_alerts_are_serialized(tmp_path):
    db = make_db(tmp_path)
    product = db.add_product("Produto", Decimal("30.00"))
    listing = db.add_listing(product.id, "amazon", "https://www.amazon.com.br/dp/Z", "Produto")
    snapshot = db.add_snapshot(
        listing.id,
        Decimal("29.90"),
        Decimal("0"),
        Decimal("0"),
        None,
        Decimal("29.90"),
        None,
        True,
        None,
    )
    engine = AlertEngine(db)
    sent = []

    async def send(message):
        sent.append(message)

    import asyncio

    async def run_checks():
        return await asyncio.gather(
            engine.process(product, listing, snapshot, send),
            engine.process(product, listing, snapshot, send),
        )

    results = asyncio.run(run_checks())
    assert sorted(results) == [False, True]
    assert len(sent) == 1


def test_alert_send_failure_is_not_persisted_and_can_be_retried(tmp_path):
    db = make_db(tmp_path)
    product = db.add_product("Produto", Decimal("30.00"))
    listing = db.add_listing(product.id, "amazon", "https://www.amazon.com.br/dp/F", "Produto")
    snapshot = db.add_snapshot(
        listing.id,
        Decimal("29.90"),
        Decimal("0"),
        Decimal("0"),
        None,
        Decimal("29.90"),
        None,
        True,
        None,
    )
    engine = AlertEngine(db)

    async def run():
        async def fail(message):
            raise RuntimeError("telegram indisponível")

        with pytest.raises(RuntimeError):
            await engine.process(product, listing, snapshot, fail)
        assert db.last_alert(listing.id, "TARGET_REACHED") is None
        sent = []

        async def succeed(message):
            sent.append(message)

        assert await engine.process(product, listing, snapshot, succeed)
        return sent

    import asyncio

    assert len(asyncio.run(run())) == 1


def test_alert_persistence_failure_prevents_delivery(tmp_path, monkeypatch):
    db = make_db(tmp_path)
    product = db.add_product("Produto", Decimal("30"))
    listing = db.add_listing(product.id, "amazon", "https://amazon.example/item", "Produto")
    snapshot = db.add_snapshot(
        listing.id, Decimal("20"), Decimal("0"), Decimal("0"), None,
        Decimal("20"), None, True, None,
    )
    engine = AlertEngine(db)
    sent = []

    async def send(message):
        sent.append(message)

    def fail_save(*args):
        raise sqlite3.OperationalError("disk full")

    monkeypatch.setattr(db, "add_alert", fail_save)
    import asyncio

    with pytest.raises(sqlite3.OperationalError):
        asyncio.run(engine.process(product, listing, snapshot, send))
    assert sent == []


def test_alerts_again_on_reentry_after_price_went_above_target(tmp_path):
    db = make_db(tmp_path)
    product = db.add_product("Produto", Decimal("30.00"))
    listing = db.add_listing(product.id, "amazon", "https://www.amazon.com.br/dp/E", "Produto")
    engine = AlertEngine(db)
    sent = []

    async def send(message):
        sent.append(message)

    import asyncio

    async def run():
        first = db.add_snapshot(
            listing.id,
            Decimal("30"),
            Decimal("0"),
            Decimal("0"),
            None,
            Decimal("30"),
            None,
            True,
            None,
        )
        assert await engine.process(product, listing, first, send)
        db.add_snapshot(
            listing.id,
            Decimal("35"),
            Decimal("0"),
            Decimal("0"),
            None,
            Decimal("35"),
            None,
            True,
            None,
        )
        reentry = db.add_snapshot(
            listing.id,
            Decimal("30"),
            Decimal("0"),
            Decimal("0"),
            None,
            Decimal("30"),
            None,
            True,
            None,
        )
        return await engine.process(product, listing, reentry, send)

    assert asyncio.run(run())
    assert len(sent) == 2


def test_failed_listing_registration_rolls_back_product(tmp_path):
    db = make_db(tmp_path)
    db.register_product_listing(
        "Produto", Decimal("30.00"), "amazon", "https://www.amazon.com.br/dp/R", "Produto"
    )
    try:
        db.register_product_listing(
            "Orfão impossível",
            Decimal("10.00"),
            "amazon",
            "https://www.amazon.com.br/dp/R",
            "Duplicado",
        )
    except DuplicateListingError:
        pass
    assert [product.name for product in db.list_products()] == ["Produto"]


def test_history_aggregates_multiple_listings_and_persists(tmp_path, monkeypatch):
    db = make_db(tmp_path)
    product = db.add_product("Produto", Decimal("30.00"))
    first = db.add_listing(product.id, "amazon", "https://www.amazon.com.br/dp/1", "A")
    second = db.add_listing(product.id, "amazon", "https://www.amazon.com.br/dp/2", "B")
    monkeypatch.setattr("app.database.utc_now", lambda: "2026-09-16T12:00:00.000000+00:00")
    db.add_snapshot(
        first.id, Decimal("20"), Decimal("0"), Decimal("0"), None, Decimal("20"), None, True, None
    )
    db.add_snapshot(
        second.id, Decimal("10"), Decimal("0"), Decimal("0"), None, Decimal("10"), None, True, None
    )
    stats = db.product_history_stats(product.id)
    assert stats.samples == 2
    assert stats.current_price == Decimal("10.00")
    assert stats.lowest_price == Decimal("10.00")
    assert stats.highest_price == Decimal("20.00")
    assert stats.average_price == Decimal("15.00")
    assert len(db.history_by_store(product.id)["amazon"]) == 2
    assert [row.id for row in db.history(first.id)] == sorted(
        row.id for row in db.history(first.id)
    )


def test_latest_unavailable_observation_does_not_report_stale_current_price(tmp_path):
    db = make_db(tmp_path)
    product = db.add_product("Produto", Decimal("30.00"))
    listing = db.add_listing(product.id, "amazon", "https://www.amazon.com.br/dp/U", "Produto")
    db.add_snapshot(
        listing.id,
        Decimal("29.90"),
        Decimal("0"),
        Decimal("0"),
        None,
        Decimal("29.90"),
        None,
        True,
        None,
    )
    db.add_snapshot(listing.id, None, None, None, None, None, None, False, None)

    stats = db.history_stats(listing.id)
    assert stats.samples == 1
    assert stats.current_price is None
    assert stats.lowest_price == Decimal("29.90")


def test_latest_unknown_shipping_observation_does_not_report_stale_current_price(tmp_path):
    db = make_db(tmp_path)
    product = db.add_product("Produto", Decimal("30.00"))
    listing = db.add_listing(product.id, "amazon", "https://www.amazon.com.br/dp/V", "Produto")
    db.add_snapshot(
        listing.id,
        Decimal("29.90"),
        Decimal("0"),
        Decimal("0"),
        None,
        Decimal("29.90"),
        None,
        True,
        None,
    )
    db.add_snapshot(listing.id, Decimal("28.90"), None, Decimal("0"), None, None, None, True, None)

    stats = db.history_stats(listing.id)
    assert stats.samples == 1
    assert stats.current_price is None


def test_product_current_price_uses_latest_state_of_each_listing(tmp_path):
    db = make_db(tmp_path)
    product = db.add_product("Produto", Decimal("30.00"))
    available = db.add_listing(product.id, "amazon", "https://www.amazon.com.br/dp/W", "A")
    unavailable = db.add_listing(product.id, "amazon", "https://www.amazon.com.br/dp/X", "B")
    other_store = db.add_listing(product.id, "magalu", "https://magalu.example/X", "C")
    db.add_snapshot(
        available.id,
        Decimal("20"),
        Decimal("0"),
        Decimal("0"),
        None,
        Decimal("20"),
        None,
        True,
        None,
    )
    db.add_snapshot(
        unavailable.id, None, None, None, None, None, None, False, None
    )
    db.add_snapshot(
        other_store.id,
        Decimal("25"),
        Decimal("0"),
        Decimal("0"),
        None,
        Decimal("25"),
        None,
        True,
        None,
    )

    stats = db.product_history_stats(product.id)
    assert stats.current_price == Decimal("20.00")
    assert stats.current_uncertain is True
    assert db.current_state_by_store(product.id) == {
        "amazon": (Decimal("20.00"), True),
        "magalu": (Decimal("25.00"), False),
    }


def test_deactivation_reports_unknown_and_disables_listings(tmp_path):
    db = make_db(tmp_path)
    product = db.add_product("Produto", Decimal("30.00"))
    listing = db.add_listing(product.id, "amazon", "https://www.amazon.com.br/dp/D", "Produto")
    assert db.deactivate_product(999) is False
    assert db.deactivate_product(product.id) is True
    assert db.deactivate_product(product.id) is False
    assert db.get_product(product.id).active is False
    assert db.get_listing(listing.id).active is False


def test_search_sessions_expire_without_another_search_and_retention_is_bounded(tmp_path):
    db = Database(tmp_path / "prices.db", search_session_max=1, search_session_max_age_hours=1)
    db.initialize()
    first = db.create_search_session(123, "produto", "{}", "complete", {}, [])
    expired = (datetime.now(UTC) - timedelta(hours=2)).isoformat(timespec="microseconds")
    with db.connection() as connection:
        connection.execute(
            "UPDATE search_sessions SET started_at = ? WHERE id = ?", (expired, first.id)
        )
    with pytest.raises(KeyError, match="expirada"):
        db.get_search_session(first.id, 123)
    second = db.create_search_session(123, "outro", "{}", "complete", {}, [])
    with db.connection() as connection:
        count = connection.execute("SELECT COUNT(*) FROM search_sessions").fetchone()[0]
    assert count == 1
    assert db.get_search_session(second.id, 123).id == second.id


def test_history_since_normalizes_naive_and_offset_datetimes_to_utc(tmp_path, monkeypatch):
    db = make_db(tmp_path)
    product = db.add_product("Produto", Decimal("30"))
    listing = db.add_listing(product.id, "amazon", "https://amazon.example/item", "Produto")
    monkeypatch.setattr("app.database.utc_now", lambda: "2026-09-23T12:00:00.000000+00:00")
    db.add_snapshot(
        listing.id, Decimal("20"), Decimal("0"), Decimal("0"), None,
        Decimal("20"), None, True, None,
    )
    monkeypatch.setattr("app.database.utc_now", lambda: "2026-09-23T13:00:00.000000+00:00")
    db.add_snapshot(
        listing.id, Decimal("10"), Decimal("0"), Decimal("0"), None,
        Decimal("10"), None, True, None,
    )
    cutoff = datetime(2026, 9, 23, 12, 30)
    assert [row.effective_price for row in db.history(listing.id, cutoff)] == [Decimal("10")]
    offset_cutoff = datetime(2026, 9, 23, 9, 30, tzinfo=timezone(timedelta(hours=-3)))
    assert [row.effective_price for row in db.history(listing.id, offset_cutoff)] == [
        Decimal("10")
    ]
    assert db.product_history_stats(product.id, cutoff).lowest_in_window == Decimal("10")


def test_initialize_adds_listing_uncertainty_to_existing_database(tmp_path):
    path = tmp_path / "existing.db"
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE products (
              id INTEGER PRIMARY KEY, name TEXT NOT NULL, target_price TEXT NOT NULL,
              active INTEGER NOT NULL DEFAULT 1,
              created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE listings (
              id INTEGER PRIMARY KEY, product_id INTEGER NOT NULL REFERENCES products(id),
              store TEXT NOT NULL, url TEXT NOT NULL UNIQUE, title TEXT NOT NULL,
              seller TEXT, active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );
            """
        )
    db = Database(path)
    db.initialize()
    product = db.add_product("Produto", Decimal("10"))
    listing = db.add_listing(
        product.id, "amazon", "https://amazon.example/item", "Produto",
        uncertainty="shipping_unknown",
    )
    assert listing.uncertainty == "shipping_unknown"
