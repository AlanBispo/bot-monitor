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

from .identity import explicit_attributes
from .models import (
    Alert,
    HistoryStats,
    Listing,
    PriceSnapshot,
    Product,
    SearchEntry,
    SearchSession,
)
from .pricing import PricingResult, parse_money
from .urls import listing_key

LOGGER = logging.getLogger(__name__)


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
        self.search_session_max = max(1, search_session_max)
        self.search_session_max_age_hours = max(1, search_session_max_age_hours)
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
                    source_query TEXT
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
                    uncertainty TEXT
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
                    sent_at TEXT NOT NULL
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
                """
            )
            self._ensure_column(connection, "products", "identity_json", "TEXT")
            self._ensure_column(connection, "products", "source_query", "TEXT")
            self._ensure_column(
                connection, "products", "monitoring_mode", "TEXT NOT NULL DEFAULT 'target'"
            )
            self._ensure_column(connection, "listings", "canonical_url", "TEXT")
            self._ensure_column(connection, "listings", "discovery_source", "TEXT")
            self._ensure_column(
                connection, "listings", "validation_status", "TEXT NOT NULL DEFAULT 'confirmed'"
            )
            self._ensure_column(connection, "listings", "uncertainty", "TEXT")
            for column, definition in {
                "identity_key": "TEXT", "last_checked_at": "TEXT", "last_success_at": "TEXT",
                "last_error": "TEXT",
            }.items():
                self._ensure_column(connection, "listings", column, definition)
            self._ensure_column(connection, "alerts", "status", "TEXT NOT NULL DEFAULT 'sent'")
            connection.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_listing_identity ON listings(identity_key)"
            )
            connection.execute(
                """DELETE FROM alerts WHERE status IN ('pending', 'failed') AND id NOT IN
                   (SELECT MAX(id) FROM alerts WHERE status IN ('pending', 'failed')
                    GROUP BY listing_id, alert_type)"""
            )
            connection.execute("UPDATE alerts SET status = 'pending' WHERE status = 'failed'")
            connection.execute(
                """CREATE UNIQUE INDEX IF NOT EXISTS idx_pending_alert
                   ON alerts(listing_id, alert_type) WHERE status = 'pending'"""
            )
            self._migrate_listing_identities(connection)
            legacy = connection.execute(
                """SELECT p.id, l.title FROM products p JOIN listings l ON l.product_id = p.id
                   WHERE p.identity_json IS NULL AND l.validation_status = 'confirmed'
                   ORDER BY l.id"""
            ).fetchall()
            seen_products: set[int] = set()
            for row in legacy:
                if row["id"] in seen_products:
                    continue
                seen_products.add(row["id"])
                connection.execute(
                    "UPDATE products SET identity_json = ? WHERE id = ?",
                    (json.dumps({"attributes": explicit_attributes(row["title"]),
                                 "confirmed_title": row["title"],
                                 "registration_source": "legacy_listing"},
                                ensure_ascii=False, sort_keys=True), row["id"]),
                )

    @staticmethod
    def _migrate_listing_identities(connection: sqlite3.Connection) -> None:
        rows = connection.execute(
            """SELECT l.*, p.active AS product_active FROM listings l
               JOIN products p ON p.id = l.product_id
               ORDER BY p.active DESC, l.active DESC, l.id"""
        ).fetchall()
        groups = defaultdict(list)
        for row in rows:
            groups[listing_key(row["store"], row["url"], row["canonical_url"])].append(row)
        for key, listings in groups.items():
            winner = listings[0]
            connection.execute("UPDATE listings SET identity_key = NULL WHERE identity_key = ?",
                               (key,))
            connection.execute("UPDATE listings SET identity_key = ? WHERE id = ?",
                               (key, winner["id"]))
            # Recover active products disabled by the previous duplicate migration.
            if winner["product_active"] and winner["validation_status"] == "duplicate":
                connection.execute(
                    """UPDATE listings SET active = 1, validation_status = 'confirmed',
                       uncertainty = NULL WHERE id = ?""", (winner["id"],),
                )
            for row in listings[1:]:
                if row["product_active"] and row["active"] and (
                    row["product_id"] != winner["product_id"]
                ):
                    LOGGER.warning("oferta duplicada nos produtos %s e %s; cadastros preservados",
                                   winner["product_id"], row["product_id"])
                    connection.execute(
                        "UPDATE listings SET uncertainty = 'duplicate_conflict' WHERE id = ?",
                        (row["id"],),
                    )
                else:
                    connection.execute(
                        """UPDATE listings SET active = 0, validation_status = 'duplicate',
                           uncertainty = ? WHERE id = ?""",
                        (f"duplicate_of:{winner['id']}", row["id"]),
                    )

    @staticmethod
    def _ensure_column(connection: sqlite3.Connection, table: str, column: str, definition: str):
        columns = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
        if column not in columns:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def add_product(
        self,
        name: str,
        target_price: Decimal,
        *,
        identity_json: str | None = None,
        source_query: str | None = None,
        monitoring_mode: str = "target",
    ) -> Product:
        if monitoring_mode not in {"target", "price_drop"}:
            raise ValueError("modo de monitoramento inválido")
        now = utc_now()
        with self.connection() as connection:
            cursor = connection.execute(
                """INSERT INTO products
                   (name, target_price, created_at, updated_at, identity_json, source_query,
                    monitoring_mode)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (name.strip(), str(target_price), now, now, identity_json, source_query,
                 monitoring_mode),
            )
            product_id = int(cursor.lastrowid)
        return self.get_product(product_id)

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
        if monitoring_mode not in {"target", "price_drop"}:
            raise ValueError("modo de monitoramento inválido")
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

    def list_products(self, active_only: bool = False) -> list[Product]:
        query = "SELECT * FROM products"
        params: tuple[object, ...] = ()
        if active_only:
            query += " WHERE active = 1"
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
                "UPDATE products SET active = 1, updated_at = ? WHERE id = ?",
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

    def get_listing_by_url(self, url: str) -> Listing | None:
        with self.connection() as connection:
            row = connection.execute("SELECT * FROM listings WHERE url = ?", (url,)).fetchone()
        return None if row is None else self._listing(row)

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

    def deactivate_listing(self, listing_id: int) -> None:
        with self.connection() as connection:
            connection.execute(
                "UPDATE listings SET active = 0, updated_at = ? WHERE id = ?",
                (utc_now(), listing_id),
            )

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

    def cleanup_search_sessions(
        self, *, max_sessions: int | None = None, max_age_hours: int | None = None
    ) -> None:
        max_sessions = self.search_session_max if max_sessions is None else max(1, max_sessions)
        max_age_hours = (
            self.search_session_max_age_hours if max_age_hours is None else max(1, max_age_hours)
        )
        cutoff = (datetime.now(UTC) - timedelta(hours=max_age_hours)).isoformat(
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
                (max_sessions,),
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

    def history_stats(self, listing_id: int, since: datetime | None = None) -> HistoryStats:
        stats = self._stats(self.history(listing_id, since))
        listing = self.get_listing(listing_id)
        if listing.last_error or not listing.active:
            return replace(stats, current_price=None, current_uncertain=True)
        return stats

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

    def last_alert(self, listing_id: int, alert_type: str) -> Alert | None:
        with self.connection() as connection:
            row = connection.execute(
                """SELECT * FROM alerts WHERE listing_id = ? AND alert_type = ? AND status = 'sent'
                   ORDER BY sent_at DESC, id DESC LIMIT 1""",
                (listing_id, alert_type),
            ).fetchone()
        return None if row is None else self._alert(row)

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
