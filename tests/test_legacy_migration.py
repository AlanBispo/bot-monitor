import sqlite3
from decimal import Decimal

import pytest

from app.database import Database
from app.urls import listing_key


@pytest.fixture
def legacy_db(tmp_path):
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as connection:
        connection.executescript("""
            CREATE TABLE products (
                id INTEGER PRIMARY KEY, name TEXT NOT NULL, target_price TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL, identity_json TEXT, source_query TEXT
            );
            CREATE TABLE listings (
                id INTEGER PRIMARY KEY, product_id INTEGER NOT NULL REFERENCES products(id),
                store TEXT NOT NULL, url TEXT NOT NULL UNIQUE, title TEXT NOT NULL,
                seller TEXT, active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL, canonical_url TEXT, discovery_source TEXT,
                validation_status TEXT NOT NULL DEFAULT 'confirmed', uncertainty TEXT
            );
            CREATE TABLE price_history (
                id INTEGER PRIMARY KEY, listing_id INTEGER NOT NULL REFERENCES listings(id),
                product_price TEXT, shipping_price TEXT, confirmed_discount TEXT,
                possible_discount TEXT, effective_price TEXT, possible_effective_price TEXT,
                available INTEGER NOT NULL, seller TEXT, captured_at TEXT NOT NULL
            );
        """)
        now = "2026-10-26T00:00:00+00:00"
        for identifier, active, tag in [(1, 0, "old"), (2, 1, "new")]:
            connection.execute(
                "INSERT INTO products VALUES (?, 'Produto', ?, ?, ?, ?, NULL, NULL)",
                (identifier, str(20 - identifier), active, now, now),
            )
            connection.execute(
                """INSERT INTO listings (id, product_id, store, url, title, active,
                   created_at, updated_at) VALUES (?, ?, 'amazon', ?, 'Produto', ?, ?, ?)""",
                (identifier, identifier,
                 f"https://www.amazon.com.br/dp/B012345678?tag={tag}", active, now, now),
            )
    database = Database(path)
    for identifier in (1, 2):
        database.add_snapshot(identifier, Decimal("10"), Decimal("0"), Decimal("0"),
                              None, Decimal("10"), None, True, None)
    return database


@pytest.mark.parametrize("already_migrated", [False, True])
def test_migration_preserves_active_listing_and_repairs_previous_migration(
    legacy_db, already_migrated,
):
    if already_migrated:
        legacy_db.initialize()
        with legacy_db.connection() as connection:
            connection.execute("UPDATE listings SET identity_key = NULL")
            connection.execute("UPDATE listings SET identity_key = ? WHERE id = 1",
                               (listing_key("amazon", legacy_db.get_listing(1).url),))
            connection.execute(
                """UPDATE listings SET active = 0, validation_status = 'duplicate',
                   uncertainty = 'duplicate_of:1' WHERE id = 2"""
            )
    legacy_db.initialize()
    legacy_db.initialize()
    assert [listing.id for listing in legacy_db.list_listings(confirmed_only=True)] == [2]
    assert legacy_db.get_product(2).active
    assert len(legacy_db.history(1)) == len(legacy_db.history(2)) == 1
    legacy_db.deactivate_product(2)
    legacy_db.reactivate_product(2)
    assert [listing.id for listing in legacy_db.list_listings(confirmed_only=True)] == [2]


def test_migration_reports_conflicting_active_products_without_disabling_them(legacy_db, caplog):
    with legacy_db.connection() as connection:
        connection.execute("UPDATE products SET active = 1")
        connection.execute("UPDATE listings SET active = 1")
    legacy_db.initialize()
    assert [listing.id for listing in legacy_db.list_listings(confirmed_only=True)] == [1, 2]
    assert legacy_db.get_listing(2).uncertainty == "duplicate_conflict"
    assert "cadastros preservados" in caplog.text
    assert len(legacy_db.history(1)) == len(legacy_db.history(2)) == 1
