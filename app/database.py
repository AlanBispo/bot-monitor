"""SQLite persistence with append-only price snapshots."""

from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from collections import defaultdict
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

from .models import (
    Alert,
    HistoryStats,
    Listing,
    PriceSnapshot,
    Product,
    SearchEntry,
    SearchSession,
    StoreHealth,
)
from .pricing import PricingResult, parse_money
from .urls import listing_key

LOGGER = logging.getLogger(__name__)
MIN_CHECK_INTERVAL = 5
MAX_CHECK_INTERVAL = 1440


class DatabaseError(RuntimeError):
    """Safe, user-facing persistence failure."""


class DuplicateListingError(DatabaseError):
    """The listing URL is already registered."""


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


def as_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value)


def utc_since(since: datetime) -> str:
    """Use UTC for history cutoffs; naive local inputs are interpreted as UTC."""
    aware = since.replace(tzinfo=UTC) if since.tzinfo is None else since.astimezone(UTC)
    return aware.isoformat(timespec="microseconds")


class Database:
    def __init__(
        self,
        path: str | Path,
        *,
        search_session_max: int = 100,
        search_session_max_age_hours: int = 24,
    ):
        self.path = Path(path)
        self.search_session_max = search_session_max
        self.search_session_max_age_hours = search_session_max_age_hours
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self) -> None:
        with self.connection() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS products (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    target_price TEXT NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    identity_json TEXT,
                    source_query TEXT,
                    monitoring_mode TEXT NOT NULL DEFAULT 'target',
                    check_interval_minutes INTEGER,
                    removed_at TEXT
                );
                CREATE TABLE IF NOT EXISTS listings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    product_id INTEGER NOT NULL REFERENCES products(id),
                    store TEXT NOT NULL,
                    url TEXT NOT NULL UNIQUE,
                    title TEXT NOT NULL,
                    seller TEXT,
                    active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    canonical_url TEXT,
                    discovery_source TEXT,
                    validation_status TEXT NOT NULL DEFAULT 'confirmed',
                    uncertainty TEXT,
                    identity_key TEXT,
                    last_checked_at TEXT,
                    last_success_at TEXT,
                    last_error TEXT
                );
                CREATE TABLE IF NOT EXISTS price_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    listing_id INTEGER NOT NULL REFERENCES listings(id),
                    product_price TEXT,
                    shipping_price TEXT,
                    confirmed_discount TEXT,
                    possible_discount TEXT,
                    effective_price TEXT,
                    possible_effective_price TEXT,
                    available INTEGER NOT NULL,
                    seller TEXT,
                    captured_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS alerts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    listing_id INTEGER NOT NULL REFERENCES listings(id),
                    alert_type TEXT NOT NULL,
                    price TEXT NOT NULL,
                    target_price TEXT NOT NULL,
                    sent_at TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'sent'
                );
                CREATE INDEX IF NOT EXISTS idx_history_listing_time
                    ON price_history(listing_id, captured_at);
                CREATE INDEX IF NOT EXISTS idx_alerts_listing_type_time
                    ON alerts(listing_id, alert_type, sent_at);
                CREATE TABLE IF NOT EXISTS search_sessions (
                    id TEXT PRIMARY KEY,
                    chat_id INTEGER NOT NULL,
                    original_query TEXT NOT NULL,
                    structured_query_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    source_coverage_json TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    completed_at TEXT
                );
                CREATE TABLE IF NOT EXISTS search_entries (
                    session_id TEXT NOT NULL REFERENCES search_sessions(id) ON DELETE CASCADE,
                    short_id TEXT NOT NULL,
                    position INTEGER NOT NULL,
                    store TEXT NOT NULL,
                    url TEXT NOT NULL,
                    canonical_url TEXT,
                    title TEXT NOT NULL,
                    seller TEXT,
                    product_price TEXT,
                    shipping_price TEXT,
                    effective_price TEXT,
                    possible_effective_price TEXT,
                    match_status TEXT NOT NULL,
                    match_reason TEXT NOT NULL,
                    uncertainty TEXT,
                    discovery_source TEXT,
                    PRIMARY KEY (session_id, short_id)
                );
                CREATE INDEX IF NOT EXISTS idx_search_sessions_chat_time
                    ON search_sessions(chat_id, started_at);
                CREATE TABLE IF NOT EXISTS store_health (
                    store TEXT PRIMARY KEY,
                    consecutive_failures INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT,
                    last_failure_at TEXT,
                    last_success_at TEXT,
                    notified INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS app_state (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_listing_identity ON listings(identity_key);
                CREATE UNIQUE INDEX IF NOT EXISTS idx_pending_alert
                    ON alerts(listing_id, alert_type) WHERE status = 'pending';
                """
            )

    def register_product_listing(
        self,
        name: str,
        target_price: Decimal,
        store: str,
        url: str,
        title: str,
        seller: str | None = None,
        *,
        canonical_url: str | None = None,
        discovery_source: str | None = None,
        identity_json: str | None = None,
        source_query: str | None = None,
        uncertainty: str | None = None,
        monitoring_mode: str = "target",
        initial_pricing: PricingResult | None = None,
    ) -> tuple[Product, Listing]:
        """Create a product, first listing and optional initial observation atomically."""
        now = utc_now()
        try:
            with self.connection() as connection:
                product_cursor = connection.execute(
                    """INSERT INTO products
                       (name, target_price, created_at, updated_at, identity_json, source_query,
                        monitoring_mode)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (name.strip(), str(target_price), now, now, identity_json, source_query,
                     monitoring_mode),
                )
                product_id = int(product_cursor.lastrowid)
                listing_cursor = connection.execute(
                    """INSERT INTO listings
                       (product_id, store, url, title, seller, created_at, updated_at,
                        canonical_url, discovery_source, validation_status,
                        uncertainty, identity_key)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'confirmed', ?, ?)""",
                    (
                        product_id, store, url, title, seller, now, now,
                        canonical_url, discovery_source, uncertainty,
                        listing_key(store, url, canonical_url),
                    ),
                )
                listing_id = int(listing_cursor.lastrowid)
                if initial_pricing is not None:
                    connection.execute(
                        """INSERT INTO price_history
                           (listing_id, product_price, shipping_price, confirmed_discount,
                            possible_discount, effective_price, possible_effective_price,
                            available, seller, captured_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?)""",
                        (listing_id, self._decimal(initial_pricing.product_price),
                         self._decimal(initial_pricing.shipping_price),
                         self._decimal(initial_pricing.confirmed_discount),
                         self._decimal(initial_pricing.possible_discount),
                         self._decimal(initial_pricing.effective_price),
                         self._decimal(initial_pricing.possible_effective_price), seller, now),
                    )
        except sqlite3.IntegrityError as exc:
            if "listings.url" in str(exc) or "listings.identity_key" in str(exc):
                raise DuplicateListingError(
                    "Essa oferta já está cadastrada. Consulte /lista ou /reativar ID."
                ) from None
            raise DatabaseError("não foi possível salvar o cadastro") from exc
        except sqlite3.Error as exc:
            raise DatabaseError("não foi possível salvar o cadastro") from exc
        return self.get_product(product_id), self.get_listing(listing_id)

    def get_product(self, product_id: int) -> Product:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM products WHERE id = ?", (product_id,)
            ).fetchone()
        if row is None:
            raise KeyError(f"produto {product_id} não encontrado")
        return self._product(row)

    def list_products(
        self, active_only: bool = False, *, include_removed: bool = True
    ) -> list[Product]:
        query = "SELECT * FROM products"
        params: tuple[object, ...] = ()
        clauses = []
        if active_only:
            clauses.append("active = 1")
        if not include_removed:
            clauses.append("removed_at IS NULL")
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY id"
        with self.connection() as connection:
            rows = connection.execute(query, params).fetchall()
        return [self._product(row) for row in rows]

    def deactivate_product(self, product_id: int) -> bool:
        now = utc_now()
        with self.connection() as connection:
            cursor = connection.execute(
                """UPDATE products SET active = 0, updated_at = ?
                   WHERE id = ? AND active = 1""",
                (now, product_id),
            )
            if cursor.rowcount == 0:
                return False
            connection.execute(
                "UPDATE listings SET active = 0, updated_at = ? WHERE product_id = ?",
                (now, product_id),
            )
            connection.execute(
                """DELETE FROM alerts WHERE status = 'pending'
                   AND listing_id IN (SELECT id FROM listings WHERE product_id = ?)""",
                (product_id,),
            )
        return True

    def remove_product(self, product_id: int) -> bool:
        """Hide a product from listings and stop monitoring; history stays for /exportar."""
        now = utc_now()
        with self.connection() as connection:
            cursor = connection.execute(
                """UPDATE products SET active = 0, removed_at = ?, updated_at = ?
                   WHERE id = ? AND removed_at IS NULL""",
                (now, now, product_id),
            )
            if cursor.rowcount == 0:
                return False
            connection.execute(
                "UPDATE listings SET active = 0, updated_at = ? WHERE product_id = ?",
                (now, product_id),
            )
            connection.execute(
                """DELETE FROM alerts WHERE status IN ('pending', 'queued')
                   AND listing_id IN (SELECT id FROM listings WHERE product_id = ?)""",
                (product_id,),
            )
        return True

    def set_check_interval(self, product_id: int, minutes: int | None) -> Product:
        if minutes is not None and not MIN_CHECK_INTERVAL <= minutes <= MAX_CHECK_INTERVAL:
            raise ValueError(
                f"intervalo deve ficar entre {MIN_CHECK_INTERVAL} e {MAX_CHECK_INTERVAL} minutos"
            )
        with self.connection() as connection:
            cursor = connection.execute(
                """UPDATE products SET check_interval_minutes = ?, updated_at = ?
                   WHERE id = ?""",
                (minutes, utc_now(), product_id),
            )
            if cursor.rowcount == 0:
                raise KeyError("produto não encontrado")
        return self.get_product(product_id)

    def update_target(self, product_id: int, target: Decimal) -> Product:
        target = parse_money(target)
        with self.connection() as connection:
            cursor = connection.execute(
                """UPDATE products SET target_price = ?, monitoring_mode = 'target', updated_at = ?
                   WHERE id = ?""",
                (str(target), utc_now(), product_id),
            )
            if cursor.rowcount == 0:
                raise KeyError("produto não encontrado")
            connection.execute(
                """DELETE FROM alerts WHERE status = 'pending'
                   AND listing_id IN (SELECT id FROM listings WHERE product_id = ?)""",
                (product_id,),
            )
        return self.get_product(product_id)

    def reactivate_product(self, product_id: int) -> Product:
        self.get_product(product_id)
        with self.connection() as connection:
            connection.execute(
                "UPDATE products SET active = 1, removed_at = NULL, updated_at = ? WHERE id = ?",
                (utc_now(), product_id),
            )
            connection.execute(
                """UPDATE listings SET active = 1, updated_at = ?
                   WHERE product_id = ? AND validation_status = 'confirmed'""",
                (utc_now(), product_id),
            )
        return self.get_product(product_id)

    def add_listing(
        self,
        product_id: int,
        store: str,
        url: str,
        title: str,
        seller: str | None = None,
        *,
        canonical_url: str | None = None,
        discovery_source: str | None = None,
        validation_status: str = "confirmed",
        uncertainty: str | None = None,
    ) -> Listing:
        now = utc_now()
        try:
            with self.connection() as connection:
                cursor = connection.execute(
                    """INSERT INTO listings
                       (product_id, store, url, title, seller, created_at, updated_at,
                        canonical_url, discovery_source, validation_status,
                        uncertainty, identity_key)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        product_id, store, url, title, seller, now, now,
                        canonical_url, discovery_source, validation_status, uncertainty,
                        listing_key(store, url, canonical_url),
                    ),
                )
                listing_id = int(cursor.lastrowid)
        except sqlite3.IntegrityError as exc:
            if "listings.url" in str(exc) or "listings.identity_key" in str(exc):
                raise DuplicateListingError("Essa oferta já está cadastrada.") from None
            raise DatabaseError("não foi possível salvar o listing") from exc
        except sqlite3.Error as exc:
            raise DatabaseError("não foi possível salvar o listing") from exc
        return self.get_listing(listing_id)

    def get_listing(self, listing_id: int) -> Listing:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM listings WHERE id = ?", (listing_id,)
            ).fetchone()
        if row is None:
            raise KeyError(f"listing {listing_id} não encontrado")
        return self._listing(row)

    def list_listings(
        self, active_only: bool = True, *, confirmed_only: bool = False
    ) -> list[Listing]:
        query = "SELECT * FROM listings"
        clauses: list[str] = []
        if active_only:
            clauses.append("active = 1")
        if confirmed_only:
            clauses.append("validation_status = 'confirmed'")
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY id"
        with self.connection() as connection:
            rows = connection.execute(query).fetchall()
        return [self._listing(row) for row in rows]

    def record_check(self, listing_id: int, *, error: str | None = None) -> None:
        now = utc_now()
        with self.connection() as connection:
            connection.execute(
                """UPDATE listings SET last_checked_at = ?, last_error = ?,
                   last_success_at = CASE WHEN ? IS NULL THEN ? ELSE last_success_at END
                   WHERE id = ?""",
                (now, error, error, now, listing_id),
            )

    def add_snapshot(
        self,
        listing_id: int,
        product_price: Decimal | None,
        shipping_price: Decimal | None,
        confirmed_discount: Decimal | None,
        possible_discount: Decimal | None,
        effective_price: Decimal | None,
        possible_effective_price: Decimal | None,
        available: bool,
        seller: str | None,
    ) -> PriceSnapshot:
        captured_at = utc_now()
        with self.connection() as connection:
            cursor = connection.execute(
                """INSERT INTO price_history
                   (listing_id, product_price, shipping_price, confirmed_discount,
                    possible_discount, effective_price, possible_effective_price,
                    available, seller, captured_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    listing_id,
                    self._decimal(product_price),
                    self._decimal(shipping_price),
                    self._decimal(confirmed_discount),
                    self._decimal(possible_discount),
                    self._decimal(effective_price),
                    self._decimal(possible_effective_price),
                    int(available),
                    seller,
                    captured_at,
                ),
            )
            snapshot_id = int(cursor.lastrowid)
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM price_history WHERE id = ?", (snapshot_id,)
            ).fetchone()
        return self._snapshot(row)

    def create_search_session(
        self,
        chat_id: int,
        original_query: str,
        structured_query_json: str,
        status: str,
        source_coverage: dict,
        entries: list[SearchEntry],
        *,
        session_id: str | None = None,
    ) -> SearchSession:
        session_id = session_id or uuid.uuid4().hex[:16]
        timestamp = utc_now()
        with self.connection() as connection:
            connection.execute(
                """INSERT INTO search_sessions
                   (id, chat_id, original_query, structured_query_json, status,
                    source_coverage_json, started_at, completed_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    session_id, chat_id, original_query, structured_query_json, status,
                    json.dumps(source_coverage, ensure_ascii=False, sort_keys=True),
                    timestamp, timestamp,
                ),
            )
            for entry in entries:
                connection.execute(
                    """INSERT INTO search_entries
                       (session_id, short_id, position, store, url, canonical_url, title, seller,
                        product_price, shipping_price, effective_price, possible_effective_price,
                        match_status, match_reason, uncertainty, discovery_source)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        session_id, entry.short_id, entry.position, entry.store, entry.url,
                        entry.canonical_url, entry.title, entry.seller,
                        self._decimal(entry.product_price), self._decimal(entry.shipping_price),
                        self._decimal(entry.effective_price),
                        self._decimal(entry.possible_effective_price),
                        entry.match_status, entry.match_reason, entry.uncertainty,
                        entry.discovery_source,
                    ),
                )
        self.cleanup_search_sessions()
        return self.get_search_session(session_id)

    def get_search_session(self, session_id: str, chat_id: int | None = None) -> SearchSession:
        query = "SELECT * FROM search_sessions WHERE id = ?"
        params: list[object] = [session_id]
        if chat_id is not None:
            query += " AND chat_id = ?"
            params.append(chat_id)
        with self.connection() as connection:
            row = connection.execute(query, params).fetchone()
        if row is None:
            raise KeyError("sessão de busca não encontrada ou expirada")
        if as_datetime(row["started_at"]) < datetime.now(UTC) - timedelta(
            hours=self.search_session_max_age_hours
        ):
            raise KeyError("sessão de busca expirada; faça uma nova /buscar")
        return SearchSession(
            row["id"], int(row["chat_id"]), row["original_query"], row["structured_query_json"],
            row["status"], row["source_coverage_json"], as_datetime(row["started_at"]),
            as_datetime(row["completed_at"]) if row["completed_at"] else None,
        )

    def get_search_entry(
        self, session_id: str, short_id: str, *, chat_id: int | None = None
    ) -> SearchEntry:
        self.get_search_session(session_id, chat_id)
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM search_entries WHERE session_id = ? AND short_id = ?",
                (session_id, short_id.upper()),
            ).fetchone()
        if row is None:
            raise KeyError("resultado não encontrado ou sessão expirada")

        def decimal(key: str):
            return None if row[key] is None else Decimal(row[key])

        return SearchEntry(
            row["session_id"], row["short_id"], int(row["position"]), row["store"], row["url"],
            row["canonical_url"], row["title"], row["seller"], decimal("product_price"),
            decimal("shipping_price"), decimal("effective_price"),
            decimal("possible_effective_price"),
            row["match_status"], row["match_reason"], row["uncertainty"], row["discovery_source"],
        )

    def cleanup_search_sessions(self) -> None:
        cutoff = (datetime.now(UTC) - timedelta(hours=self.search_session_max_age_hours)).isoformat(
            timespec="microseconds"
        )
        with self.connection() as connection:
            connection.execute(
                "DELETE FROM search_sessions WHERE started_at < ?",
                (cutoff,),
            )
            connection.execute(
                """DELETE FROM search_sessions WHERE id NOT IN
                   (SELECT id FROM search_sessions ORDER BY started_at DESC LIMIT ?)""",
                (self.search_session_max,),
            )

    def history(self, listing_id: int, since: datetime | None = None) -> list[PriceSnapshot]:
        query = "SELECT * FROM price_history WHERE listing_id = ?"
        params: list[object] = [listing_id]
        if since is not None:
            query += " AND captured_at >= ?"
            params.append(utc_since(since))
        query += " ORDER BY captured_at, id"
        with self.connection() as connection:
            rows = connection.execute(query, params).fetchall()
        return [self._snapshot(row) for row in rows]

    def latest_snapshot(self, listing_id: int) -> PriceSnapshot | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM price_history WHERE listing_id = ? ORDER BY id DESC LIMIT 1",
                (listing_id,),
            ).fetchone()
        return self._snapshot(row) if row else None

    def product_history(
        self, product_id: int, since: datetime | None = None
    ) -> list[PriceSnapshot]:
        self.get_product(product_id)
        query = """SELECT h.* FROM price_history h
                   JOIN listings l ON l.id = h.listing_id
                   WHERE l.product_id = ?"""
        params: list[object] = [product_id]
        if since is not None:
            query += " AND h.captured_at >= ?"
            params.append(utc_since(since))
        query += " ORDER BY h.captured_at, h.id"
        with self.connection() as connection:
            rows = connection.execute(query, params).fetchall()
        return [self._snapshot(row) for row in rows]

    def history_by_store(self, product_id: int) -> dict[str, list[PriceSnapshot]]:
        self.get_product(product_id)
        grouped: dict[str, list[PriceSnapshot]] = {}
        for listing in self.list_listings(active_only=False):
            if listing.product_id == product_id:
                grouped.setdefault(listing.store, []).extend(self.history(listing.id))
        for rows in grouped.values():
            rows.sort(key=lambda row: (row.captured_at, row.id))
        return grouped

    def product_history_stats(self, product_id: int, since: datetime | None = None) -> HistoryStats:
        self.get_product(product_id)
        rows = self.product_history(product_id, since)
        stats = self._stats(rows)
        current_price, current_uncertain = self._current_from_healthy_listings(
            rows, product_id=product_id,
        )
        return replace(
            stats,
            current_price=current_price,
            current_uncertain=current_uncertain,
        )

    def current_state_by_store(
        self, product_id: int, since: datetime | None = None
    ) -> dict[str, tuple[Decimal | None, bool]]:
        self.get_product(product_id)
        grouped: dict[str, list[PriceSnapshot]] = defaultdict(list)
        listings = {listing.id: listing for listing in self.list_listings(active_only=False)}
        for row in self.product_history(product_id, since):
            grouped[listings[row.listing_id].store].append(row)
        return {
            store: self._current_from_healthy_listings(rows, product_id=product_id, store=store)
            for store, rows in grouped.items()
        }

    def _current_from_healthy_listings(
        self, rows: list[PriceSnapshot], *, product_id: int, store: str | None = None,
    ) -> tuple[Decimal | None, bool]:
        listings = {
            listing.id: listing for listing in self.list_listings(active_only=True)
            if listing.product_id == product_id and (store is None or listing.store == store)
        }
        healthy_rows = [row for row in rows if row.listing_id in listings
                        and listings[row.listing_id].last_error is None]
        price, uncertain = self._current_from_latest_per_listing(healthy_rows)
        observed = {row.listing_id for row in healthy_rows}
        incomplete = any(item.last_error or item.id not in observed for item in listings.values())
        return price, uncertain or incomplete

    @staticmethod
    def _stats(rows: list[PriceSnapshot]) -> HistoryStats:
        prices = [
            row.effective_price for row in rows if row.available and row.effective_price is not None
        ]
        latest = rows[-1] if rows else None
        current = (
            latest.effective_price
            if latest is not None and latest.available and latest.effective_price is not None
            else None
        )
        if not prices:
            return HistoryStats(0, current, None, None, None, None)
        average = (sum(prices, Decimal("0.00")) / len(prices)).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
        return HistoryStats(len(prices), current, min(prices), max(prices), average, min(prices))

    @staticmethod
    def _current_from_latest_per_listing(
        rows: list[PriceSnapshot],
    ) -> tuple[Decimal | None, bool]:
        latest_by_listing: dict[int, PriceSnapshot] = {}
        for row in rows:
            latest_by_listing[row.listing_id] = row
        current_prices = [
            row.effective_price
            for row in latest_by_listing.values()
            if row.available and row.effective_price is not None
        ]
        uncertain = any(
            not row.available or row.effective_price is None
            for row in latest_by_listing.values()
        )
        return (min(current_prices) if current_prices else None, uncertain)

    def last_alert(
        self, listing_id: int, alert_type: str, *, include_queued: bool = False
    ) -> Alert | None:
        with self.connection() as connection:
            row = connection.execute(
                """SELECT * FROM alerts WHERE listing_id = ? AND alert_type = ?
                   AND (status = 'sent' OR (? AND status = 'queued'))
                   ORDER BY sent_at DESC, id DESC LIMIT 1""",
                (listing_id, alert_type, int(include_queued)),
            ).fetchone()
        return None if row is None else self._alert(row)

    def add_queued_alert(
        self, listing_id: int, alert_type: str, price: Decimal, reference: Decimal
    ) -> Alert:
        """Record an event for the daily summary; it counts for de-duplication immediately."""
        with self.connection() as connection:
            row = connection.execute(
                """INSERT INTO alerts (listing_id, alert_type, price, target_price, sent_at, status)
                   VALUES (?, ?, ?, ?, ?, 'queued') RETURNING *""",
                (listing_id, alert_type, str(price), str(reference), utc_now()),
            ).fetchone()
        return self._alert(row)

    def queued_alerts(self) -> list[Alert]:
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM alerts WHERE status = 'queued' ORDER BY id"
            ).fetchall()
        return [self._alert(row) for row in rows]

    def mark_alerts_sent(self, alert_ids: list[int]) -> None:
        with self.connection() as connection:
            connection.executemany(
                "UPDATE alerts SET status = 'sent' WHERE id = ? AND status = 'queued'",
                [(alert_id,) for alert_id in alert_ids],
            )

    def get_state(self, key: str) -> str | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT value FROM app_state WHERE key = ?", (key,)
            ).fetchone()
        return None if row is None else row["value"]

    def set_state(self, key: str, value: str) -> None:
        with self.connection() as connection:
            connection.execute(
                """INSERT INTO app_state (key, value) VALUES (?, ?)
                   ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
                (key, value),
            )

    def store_health(self) -> list[StoreHealth]:
        with self.connection() as connection:
            rows = connection.execute("SELECT * FROM store_health ORDER BY store").fetchall()
        return [self._store_health(row) for row in rows]

    def record_store_result(
        self, store: str, *, error: str | None, threshold: int
    ) -> str | None:
        """Track consecutive failed cycles; return 'failing' or 'recovered' once per transition."""
        now = utc_now()
        with self.connection() as connection:
            connection.execute(
                "INSERT INTO store_health (store) VALUES (?) ON CONFLICT(store) DO NOTHING",
                (store,),
            )
            row = connection.execute(
                "SELECT * FROM store_health WHERE store = ?", (store,)
            ).fetchone()
            if error is None:
                connection.execute(
                    """UPDATE store_health SET consecutive_failures = 0, last_success_at = ?,
                       notified = 0 WHERE store = ?""",
                    (now, store),
                )
                return "recovered" if row["notified"] else None
            failures = int(row["consecutive_failures"]) + 1
            notify = failures >= max(1, threshold) and not row["notified"]
            connection.execute(
                """UPDATE store_health SET consecutive_failures = ?, last_error = ?,
                   last_failure_at = ?, notified = ? WHERE store = ?""",
                (failures, error, now, int(bool(row["notified"]) or notify), store),
            )
            return "failing" if notify else None

    def export_rows(self, product_id: int | None = None) -> list[sqlite3.Row]:
        query = """SELECT p.id AS product_id, p.name AS product_name, l.id AS listing_id,
                          l.store, l.url, h.captured_at, h.available, h.product_price,
                          h.shipping_price, h.confirmed_discount, h.possible_discount,
                          h.effective_price, h.possible_effective_price, h.seller
                   FROM price_history h
                   JOIN listings l ON l.id = h.listing_id
                   JOIN products p ON p.id = l.product_id"""
        params: tuple[object, ...] = ()
        if product_id is not None:
            self.get_product(product_id)
            query += " WHERE p.id = ?"
            params = (product_id,)
        query += " ORDER BY p.id, l.id, h.captured_at, h.id"
        with self.connection() as connection:
            return connection.execute(query, params).fetchall()

    def add_alert(
        self, listing_id: int, alert_type: str, price: Decimal, target_price: Decimal
    ) -> Alert:
        with self.connection() as connection:
            cursor = connection.execute(
                """INSERT INTO alerts (listing_id, alert_type, price, target_price, sent_at, status)
                   VALUES (?, ?, ?, ?, '', 'pending')
                   ON CONFLICT(listing_id, alert_type) WHERE status = 'pending'
                   DO UPDATE SET price = excluded.price, target_price = excluded.target_price
                   RETURNING *""",
                (listing_id, alert_type, str(price), str(target_price)),
            )
            row = cursor.fetchone()
        return self._alert(row)

    def pending_alert(self, listing_id: int, alert_type: str) -> Alert | None:
        with self.connection() as connection:
            row = connection.execute(
                """SELECT * FROM alerts WHERE listing_id = ? AND alert_type = ?
                   AND status = 'pending' ORDER BY id DESC LIMIT 1""",
                (listing_id, alert_type),
            ).fetchone()
        return None if row is None else self._alert(row)

    def mark_alert_sent(self, alert_id: int) -> None:
        with self.connection() as connection:
            connection.execute(
                "UPDATE alerts SET status = 'sent', sent_at = ? WHERE id = ?",
                (utc_now(), alert_id),
            )

    def cancel_pending_alert(self, listing_id: int) -> None:
        with self.connection() as connection:
            connection.execute(
                "DELETE FROM alerts WHERE listing_id = ? AND status = 'pending'", (listing_id,),
            )

    @staticmethod
    def _decimal(value: Decimal | None) -> str | None:
        return None if value is None else str(value)

    @staticmethod
    def _product(row: sqlite3.Row) -> Product:
        return Product(
            int(row["id"]),
            row["name"],
            Decimal(row["target_price"]),
            bool(row["active"]),
            as_datetime(row["created_at"]),
            as_datetime(row["updated_at"]),
            row["identity_json"],
            row["source_query"],
            row["monitoring_mode"],
            row["check_interval_minutes"],
            row["removed_at"] is not None,
        )

    @staticmethod
    def _store_health(row: sqlite3.Row) -> StoreHealth:
        def moment(key: str):
            return as_datetime(row[key]) if row[key] else None

        return StoreHealth(
            row["store"], int(row["consecutive_failures"]), row["last_error"],
            moment("last_failure_at"), moment("last_success_at"), bool(row["notified"]),
        )

    @staticmethod
    def _listing(row: sqlite3.Row) -> Listing:
        return Listing(
            int(row["id"]),
            int(row["product_id"]),
            row["store"],
            row["url"],
            row["title"],
            row["seller"],
            bool(row["active"]),
            as_datetime(row["created_at"]),
            as_datetime(row["updated_at"]),
            row["canonical_url"],
            row["discovery_source"],
            row["validation_status"],
            row["uncertainty"],
            as_datetime(row["last_checked_at"]) if row["last_checked_at"] else None,
            as_datetime(row["last_success_at"]) if row["last_success_at"] else None,
            row["last_error"],
        )

    @staticmethod
    def _snapshot(row: sqlite3.Row) -> PriceSnapshot:
        def decimal(key: str) -> Decimal | None:
            return None if row[key] is None else Decimal(row[key])

        return PriceSnapshot(
            int(row["id"]),
            int(row["listing_id"]),
            decimal("product_price"),
            decimal("shipping_price"),
            decimal("confirmed_discount"),
            decimal("possible_discount"),
            decimal("effective_price"),
            decimal("possible_effective_price"),
            bool(row["available"]),
            row["seller"],
            as_datetime(row["captured_at"]),
        )

    @staticmethod
    def _alert(row: sqlite3.Row) -> Alert:
        return Alert(
            int(row["id"]),
            int(row["listing_id"]),
            row["alert_type"],
            Decimal(row["price"]),
            Decimal(row["target_price"]),
            as_datetime(row["sent_at"]) if row["sent_at"] else None,
            row["status"],
        )
