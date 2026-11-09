"""Alert decisions and Telegram alert formatting."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from .database import Database
from .models import Alert, Listing, PriceSnapshot, Product

LOGGER = logging.getLogger(__name__)

DROP_WINDOW_DAYS = 7
LOW_WINDOW_DAYS = 90
# A "lowest in N days" claim needs history that actually spans a meaningful period.
LOW_MIN_HISTORY_DAYS = 7
LOW_PRICE = "LOW_PRICE"
PERCENT_DROP = "PERCENT_DROP"
BACK_IN_STOCK = "BACK_IN_STOCK"
EVENT_LABELS = {
    LOW_PRICE: "📉 Menor preço do período",
    PERCENT_DROP: "🔻 Queda percentual",
    BACK_IN_STOCK: "📦 Voltou ao estoque",
}


@dataclass(frozen=True)
class AlertSettings:
    """Secondary alerts compare the product price (without shipping) on each listing."""

    drop_percent: Decimal = Decimal("15")  # 0 disables percentage-drop alerts
    daily_summary: bool = True  # True: queue for the daily summary; False: send immediately


def target_reached(snapshot: PriceSnapshot, target_price: Decimal) -> bool:
    return (
        snapshot.available
        and snapshot.effective_price is not None
        and snapshot.effective_price <= target_price
    )


class AlertEngine:
    def __init__(self, database: Database, settings: AlertSettings | None = None):
        self.database = database
        self.settings = settings or AlertSettings()
        self._lock = asyncio.Lock()

    async def process_events(
        self,
        product: Product,
        listing: Listing,
        snapshot: PriceSnapshot,
        previous: PriceSnapshot | None,
        send: Callable[[str], Awaitable[None]],
    ) -> int:
        """Evaluate secondary alerts for a new available observation.

        Events are recorded before delivery, so a failed immediate send is not lost: it stays
        queued and appears in the next daily summary.
        """
        if not snapshot.available or snapshot.product_price is None:
            return 0
        async with self._lock:
            events = self._events(product, listing, snapshot, previous)
            alerts = [
                self.database.add_queued_alert(listing.id, kind, snapshot.product_price, ref)
                for kind, ref, _ in events
            ]
            if not alerts or self.settings.daily_summary:
                return 0
            await send(format_event_alert(product, listing, snapshot, events))
            self.database.mark_alerts_sent([alert.id for alert in alerts])
            return 1

    def _events(self, product, listing, snapshot, previous) -> list[tuple[str, Decimal, str]]:
        price = snapshot.product_price
        events: list[tuple[str, Decimal, str]] = []
        if previous is not None and not previous.available:
            last_price = next(
                (row.product_price for row in reversed(self.database.history(listing.id))
                 if row.id < snapshot.id and row.available and row.product_price is not None),
                None,
            )
            detail = f"último preço antes de esgotar: R$ {last_price:.2f}" if last_price else ""
            events.append((BACK_IN_STOCK, last_price or price, detail))
        # Price-drop products already alert on every new low; avoid duplicate messages.
        if product.monitoring_mode != "target":
            return events
        now = datetime.now(UTC)
        settings = self.settings
        prior = [
            row for row in self.database.history(
                listing.id, now - timedelta(days=LOW_WINDOW_DAYS)
            )
            if row.id < snapshot.id and row.available and row.product_price is not None
        ]
        if prior:
            span_days = (now - prior[0].captured_at).days
            lowest = min(row.product_price for row in prior)
            if span_days >= LOW_MIN_HISTORY_DAYS and price < lowest:
                days = min(span_days, LOW_WINDOW_DAYS)
                events.append((LOW_PRICE, lowest, f"menor preço em {days} dias"))
        if settings.drop_percent > 0:
            cutoff = now - timedelta(days=DROP_WINDOW_DAYS)
            recent = [row.product_price for row in prior if row.captured_at >= cutoff]
            if recent:
                reference = max(recent)
                last = self.database.last_alert(listing.id, PERCENT_DROP, include_queued=True)
                if last is not None and last.sent_at and last.sent_at >= cutoff:
                    # After an alert, only a further drop of the same size alerts again.
                    reference = min(reference, last.price)
                limit = reference * (1 - settings.drop_percent / 100)
                if price <= limit:
                    percent = (reference - price) / reference * 100
                    events.append((
                        PERCENT_DROP, reference,
                        f"-{percent:.0f}% em relação a R$ {reference:.2f} "
                        f"(últimos {DROP_WINDOW_DAYS} dias)",
                    ))
        return events

    async def process(
        self,
        product: Product,
        listing: Listing,
        snapshot: PriceSnapshot,
        send: Callable[[str], Awaitable[None]],
    ) -> bool:
        async with self._lock:
            if product.monitoring_mode == "price_drop":
                return await self._process_drop(product, listing, snapshot, send)
            if not target_reached(snapshot, product.target_price):
                self.database.cancel_pending_alert(listing.id)
                return False
            pending = self.database.pending_alert(listing.id, "TARGET_REACHED")
            if pending and pending.target_price != product.target_price:
                self.database.cancel_pending_alert(listing.id)
                pending = None
            previous = self.database.last_alert(listing.id, "TARGET_REACHED")
            if pending is None and previous and previous.target_price == product.target_price and (
                snapshot.effective_price >= previous.price
            ):
                history = self.database.history(listing.id)
                prior_rows = [row for row in history if row.id < snapshot.id
                              and row.available and row.effective_price is not None]
                reentered = bool(
                    prior_rows
                    and prior_rows[-1].effective_price is not None
                    and prior_rows[-1].effective_price > product.target_price
                )
                if not reentered:
                    return False
            message = format_target_alert(product, listing, snapshot)
            alert = self.database.add_alert(
                listing.id, "TARGET_REACHED", snapshot.effective_price, product.target_price,
            )
            return await self._deliver(alert, message, send)

    async def _deliver(self, alert, message, send) -> bool:
        # Failure or cancellation leaves the pending row for the next observation.
        await send(message)
        self.database.mark_alert_sent(alert.id)
        LOGGER.info("alerta %s enviado para listing %s", alert.alert_type, alert.listing_id)
        return True

    async def _process_drop(self, product, listing, snapshot, send) -> bool:
        if not snapshot.available or snapshot.product_price is None or snapshot.product_price <= 0:
            self.database.cancel_pending_alert(listing.id)
            return False
        # Each listing has its own captured baseline, including attached store offers.
        history = self.database.history(listing.id)
        baseline = next(
            (row.product_price for row in history if row.available
             and row.product_price is not None and row.product_price > 0), None,
        )
        if baseline is None:
            return False
        previous = self.database.last_alert(listing.id, "PRICE_DROP")
        reference = min(baseline, previous.price) if previous else baseline
        if snapshot.product_price >= reference:
            self.database.cancel_pending_alert(listing.id)
            return False
        alert = self.database.add_alert(
            listing.id, "PRICE_DROP", snapshot.product_price, reference,
        )
        return await self._deliver(
            alert,
            format_drop_alert(product, listing, snapshot, reference), send,
        )


def format_drop_alert(
    product: Product, listing: Listing, snapshot: PriceSnapshot, reference: Decimal,
) -> str:
    shipping = (
        "desconhecido (total com frete não confirmado)"
        if snapshot.shipping_price is None else f"R$ {snapshot.shipping_price:.2f}"
    )
    return (
        f"📉 Preço do produto caiu!\n{product.name}\nLoja: {listing.store}\n"
        f"Referência anterior: R$ {reference:.2f}\nAgora: R$ {snapshot.product_price:.2f}\n"
        f"Queda: R$ {reference - snapshot.product_price:.2f}\nFrete: {shipping}\n"
        f"Comparação do preço do produto, sem frete e sem cupons condicionais.\n{listing.url}"
    )


def format_event_alert(
    product: Product, listing: Listing, snapshot: PriceSnapshot,
    events: list[tuple[str, Decimal, str]],
) -> str:
    lines = [f"{product.name}", f"Loja: {listing.store}",
             f"Preço do produto: R$ {snapshot.product_price:.2f} (sem frete)"]
    for kind, _, detail in events:
        lines.append(f"{EVENT_LABELS[kind]}" + (f": {detail}" if detail else ""))
    lines.append(listing.url)
    return "\n".join(lines)


def describe_queued_alert(alert: Alert) -> str:
    """One-line description of a stored event for the daily summary."""
    label = EVENT_LABELS.get(alert.alert_type, alert.alert_type)
    if alert.alert_type == BACK_IN_STOCK:
        return f"{label} por R$ {alert.price:.2f}"
    return f"{label}: R$ {alert.target_price:.2f} → R$ {alert.price:.2f}"


def format_target_alert(product: Product, listing: Listing, snapshot: PriceSnapshot) -> str:
    shipping = (
        "desconhecido" if snapshot.shipping_price is None else f"R$ {snapshot.shipping_price:.2f}"
    )
    return (
        f"🎯 Preço-alvo atingido\n{product.name}\n"
        f"Loja: {listing.store}\nPreço efetivo: R$ {snapshot.effective_price:.2f}\n"
        f"Produto: R$ {snapshot.product_price:.2f}\nFrete: {shipping}\n{listing.url}"
    )
