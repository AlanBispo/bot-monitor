"""Small, normalized domain models used by the application and store adapters."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Literal


@dataclass(frozen=True)
class StoreSearchResult:
    store: str
    title: str
    url: str
    product_price: Decimal | None


@dataclass(frozen=True)
class StoreOffer:
    store: str
    title: str
    url: str
    product_price: Decimal | None
    shipping_price: Decimal | None
    confirmed_discount: Decimal | None = None
    possible_discount: Decimal | None = None
    seller: str | None = None
    available: bool = True
    observation_status: Literal["available", "unavailable"] = "available"
    attributes: dict[str, str] | None = None
    canonical_url: str | None = None
    discovery_source: str | None = None

    @property
    def is_available(self) -> bool:
        """Return the normalized, fail-closed availability state."""
        return self.available and self.observation_status == "available"


@dataclass(frozen=True)
class Product:
    id: int
    name: str
    target_price: Decimal
    active: bool
    created_at: datetime
    updated_at: datetime
    identity_json: str | None = None
    source_query: str | None = None
    # The legacy target_price column stores the captured reference in price_drop mode.
    monitoring_mode: Literal["target", "price_drop"] = "target"
    # None means automatic: shorter interval near the target, default interval otherwise.
    check_interval_minutes: int | None = None
    removed: bool = False
    snoozed_until: datetime | None = None


@dataclass(frozen=True)
class Listing:
    id: int
    product_id: int
    store: str
    url: str
    title: str
    seller: str | None
    active: bool
    created_at: datetime
    updated_at: datetime
    canonical_url: str | None = None
    discovery_source: str | None = None
    validation_status: str = "confirmed"
    uncertainty: str | None = None
    last_checked_at: datetime | None = None
    last_success_at: datetime | None = None
    last_error: str | None = None


@dataclass(frozen=True)
class PriceSnapshot:
    id: int
    listing_id: int
    product_price: Decimal | None
    shipping_price: Decimal | None
    confirmed_discount: Decimal | None
    possible_discount: Decimal | None
    effective_price: Decimal | None
    possible_effective_price: Decimal | None
    available: bool
    seller: str | None
    captured_at: datetime


@dataclass(frozen=True)
class Alert:
    id: int
    listing_id: int
    alert_type: str
    price: Decimal
    target_price: Decimal
    sent_at: datetime | None
    status: str = "sent"


@dataclass(frozen=True)
class StoreHealth:
    store: str
    consecutive_failures: int
    last_error: str | None
    last_failure_at: datetime | None
    last_success_at: datetime | None
    notified: bool


@dataclass(frozen=True)
class HistoryStats:
    """Aggregate over reliable effective-price observations only."""

    samples: int
    current_price: Decimal | None
    lowest_price: Decimal | None
    highest_price: Decimal | None
    average_price: Decimal | None
    lowest_in_window: Decimal | None
    current_uncertain: bool = False


@dataclass(frozen=True)
class SearchSession:
    id: str
    chat_id: int
    original_query: str
    structured_query_json: str
    status: str
    source_coverage_json: str
    started_at: datetime
    completed_at: datetime | None


@dataclass(frozen=True)
class SearchEntry:
    session_id: str
    short_id: str
    position: int
    store: str
    url: str
    canonical_url: str | None
    title: str
    seller: str | None
    product_price: Decimal | None
    shipping_price: Decimal | None
    effective_price: Decimal | None
    possible_effective_price: Decimal | None
    match_status: str
    match_reason: str
    uncertainty: str | None
    discovery_source: str | None
