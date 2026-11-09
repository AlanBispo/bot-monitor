"""Alert decisions and Telegram alert formatting."""

from __future__ import annotations

import asyncio
import logging
import textwrap
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from urllib.parse import urlsplit

from .database import Database
from .models import Alert, Listing, PriceSnapshot, Product
from .pricing import format_brl, unit_price

LOGGER = logging.getLogger(__name__)

DROP_WINDOW_DAYS = 7
LOW_WINDOW_DAYS = 90
# A "lowest in N days" claim needs history that actually spans a meaningful period.
LOW_MIN_HISTORY_DAYS = 7
RATING_MIN_SAMPLES = 10
MAX_TITLE_LENGTH = 80
STORE_LABELS = {"amazon": "Amazon", "mercadolivre": "Mercado Livre", "magalu": "Magalu"}
# A new cheapest offer must beat the previous one by this share to be reported.
LEADER_MARGIN = Decimal("0.01")
LOW_PRICE = "LOW_PRICE"
PERCENT_DROP = "PERCENT_DROP"
BACK_IN_STOCK = "BACK_IN_STOCK"
CHEAPER_STORE = "CHEAPER_STORE"
EVENT_LABELS = {
    LOW_PRICE: "📉 Menor preço do período",
    PERCENT_DROP: "🔻 Queda percentual",
    BACK_IN_STOCK: "📦 Voltou ao estoque",
    CHEAPER_STORE: "🏷️ Outra loja ficou mais barata",
}


@dataclass(frozen=True)
class AlertActions:
    """What the delivery layer can offer next to an alert."""

    url: str
    product_id: int


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

    def _snoozed(self, product: Product) -> bool:
        return bool(product.snoozed_until and product.snoozed_until > datetime.now(UTC))

    async def process_best_offer(self, product: Product, observed, send) -> int:
        """Run the main alert only for the cheapest offer among the ones just observed."""
        offers = self.database.current_offers(product.id)
        if product.monitoring_mode == "target":
            offers = [offer for offer in offers if offer[1].effective_price is not None]
            best = min(offers, key=lambda offer: offer[1].effective_price, default=None)
        else:
            best = min(offers, key=lambda offer: offer[1].product_price, default=None)
        count = 0
        for listing, snapshot, _ in observed:
            if self._snoozed(product) or best is None or listing.id != best[0].id:
                self.database.cancel_pending_alert(listing.id)
            else:
                count += int(await self.process(product, listing, snapshot, send))
        return count

    async def process_product_events(self, product: Product, observed, send) -> int:
        """Evaluate secondary alerts, recorded before delivery so a failed send is not lost."""
        if self._snoozed(product):
            return 0
        count = 0
        async with self._lock:
            offers = self.database.current_offers(product.id)
            cheapest = min(offers, key=lambda offer: offer[1].product_price, default=None)
            found: dict[int, tuple] = {}
            for listing, snapshot, previous in observed:
                is_cheapest = cheapest is not None and cheapest[0].id == listing.id
                events = self._events(product, listing, snapshot, previous, is_cheapest)
                if events:
                    found[listing.id] = (listing, snapshot, events)
            change = self._leader_change(product, offers, cheapest)
            if change:
                entry = found.setdefault(cheapest[0].id, (*cheapest, []))
                entry[2].append(change)
            for listing, snapshot, events in found.values():
                alerts = [
                    self.database.add_queued_alert(
                        listing.id, kind, snapshot.product_price, reference,
                    )
                    for kind, reference, _ in events
                ]
                if self.settings.daily_summary:
                    continue
                notes = self._notes(product, listing, snapshot)
                await send(
                    format_event_alert(product, listing, snapshot, events, notes),
                    AlertActions(listing.url, product.id),
                )
                self.database.mark_alerts_sent([alert.id for alert in alerts])
                count += 1
        return count

    def _leader_change(self, product, offers, cheapest) -> tuple[str, Decimal, str] | None:
        """Report a new cheapest offer once; small differences do not change the leader."""
        if len(offers) < 2:
            return None
        key = f"leader:{product.id}"
        stored = self.database.get_state(key)
        prices = {listing.id: (listing, snap.product_price) for listing, snap in offers}
        winner, winner_price = cheapest[0], cheapest[1].product_price
        if stored is None or int(stored) not in prices:
            self.database.set_state(key, str(winner.id))
            return None
        leader, leader_price = prices[int(stored)]
        if leader.id == winner.id or winner_price > leader_price * (1 - LEADER_MARGIN):
            return None
        self.database.set_state(key, str(winner.id))
        return CHEAPER_STORE, leader_price, f"antes {leader.store} a {format_brl(leader_price)}"

    def _notes(self, product, listing, snapshot) -> list[str]:
        price = snapshot.effective_price or snapshot.product_price
        notes = []
        per_unit = unit_price(listing.title, price)
        if per_unit:
            notes.append(f"Por unidade: {per_unit}")
        rating = self._rating(product, snapshot)
        if rating:
            notes.append(rating)
        return notes

    def _rating(self, product, snapshot) -> str | None:
        """Place the price within the product history, when there is enough of it."""
        now = datetime.now(UTC)
        rows = [
            row for row in self.database.product_history(
                product.id, now - timedelta(days=LOW_WINDOW_DAYS),
            )
            if row.id != snapshot.id and row.available and row.product_price is not None
        ]
        prices = [row.product_price for row in rows]
        days = (now - rows[0].captured_at).days if rows else 0
        if len(prices) < RATING_MIN_SAMPLES or days < LOW_MIN_HISTORY_DAYS:
            return None
        if min(prices) == max(prices):
            return None
        at_or_below = sum(price <= snapshot.product_price for price in prices)
        for percent, label in ((10, "Preço ótimo"), (25, "Preço bom"), (50, "Preço razoável")):
            if at_or_below * 100 <= percent * len(prices):
                return f"💡 {label}: entre os {percent}% menores dos últimos {days} dias"
        return f"💡 Preço comum: acima da metade dos últimos {days} dias"

    def _events(
        self, product, listing, snapshot, previous, is_cheapest: bool,
    ) -> list[tuple[str, Decimal, str]]:
        price = snapshot.product_price
        events: list[tuple[str, Decimal, str]] = []
        if previous is not None and not previous.available:
            last_price = next(
                (row.product_price for row in reversed(self.database.history(listing.id))
                 if row.id < snapshot.id and row.available and row.product_price is not None),
                None,
            )
            detail = ""
            if last_price:
                detail = f"último preço antes de esgotar: {format_brl(last_price)}"
            events.append((BACK_IN_STOCK, last_price or price, detail))
        # Price-drop products already alert on every new low, and only the cheapest offer counts.
        if product.monitoring_mode != "target" or not is_cheapest:
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
                        f"-{percent:.0f}% em relação a {format_brl(reference)} "
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
            previous = self.database.last_product_alert(product.id, "TARGET_REACHED")
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
            message = format_target_alert(
                product, listing, snapshot, self._notes(product, listing, snapshot),
            )
            alert = self.database.add_alert(
                listing.id, "TARGET_REACHED", snapshot.effective_price, product.target_price,
            )
            return await self._deliver(alert, message, send, listing)

    async def _deliver(self, alert, message, send, listing) -> bool:
        # Failure or cancellation leaves the pending row for the next observation.
        await send(message, AlertActions(listing.url, listing.product_id))
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
        previous = self.database.last_product_alert(product.id, "PRICE_DROP")
        reference = min(baseline, previous.price) if previous else baseline
        if snapshot.product_price >= reference:
            self.database.cancel_pending_alert(listing.id)
            return False
        alert = self.database.add_alert(
            listing.id, "PRICE_DROP", snapshot.product_price, reference,
        )
        return await self._deliver(
            alert,
            format_drop_alert(
                product, listing, snapshot, reference, self._notes(product, listing, snapshot),
            ),
            send,
            listing,
        )


def _short_title(name: str) -> str:
    return textwrap.shorten(name, width=MAX_TITLE_LENGTH, placeholder="…")


def store_label(listing: Listing) -> str:
    if listing.store == "generic":
        return (urlsplit(listing.url).hostname or "site").removeprefix("www.")
    return STORE_LABELS.get(listing.store, listing.store)


def shipping_text(snapshot: PriceSnapshot) -> str:
    if snapshot.shipping_price is None:
        return "frete não informado"
    if snapshot.shipping_price == 0:
        return "frete grátis"
    return f"frete {format_brl(snapshot.shipping_price)}"


def _delivery_line(listing: Listing, snapshot: PriceSnapshot) -> str:
    return f"{store_label(listing)} · {shipping_text(snapshot)}"


def format_drop_alert(
    product: Product, listing: Listing, snapshot: PriceSnapshot, reference: Decimal,
    notes: list[str],
) -> str:
    saved = reference - snapshot.product_price
    percent = saved / reference * 100
    shown = f"{percent:.0f}" if percent >= 1 else f"{percent:.1f}".replace(".", ",")
    lines = [
        f"📉 Caiu {shown}% → {format_brl(snapshot.product_price)}",
        _short_title(product.name),
        "",
        f"Antes {format_brl(reference)} · economia de {format_brl(saved)}",
        _delivery_line(listing, snapshot),
        *notes,
    ]
    return "\n".join(lines)


def format_event_alert(
    product: Product, listing: Listing, snapshot: PriceSnapshot,
    events: list[tuple[str, Decimal, str]],
    notes: list[str],
) -> str:
    lines = [
        *(f"{EVENT_LABELS[kind]}" + (f": {detail}" if detail else "")
          for kind, _, detail in events),
        _short_title(product.name),
        "",
        f"{format_brl(snapshot.product_price)} · {_delivery_line(listing, snapshot)}",
        *notes,
    ]
    return "\n".join(lines)


def describe_queued_alert(alert: Alert) -> str:
    """One-line description of a stored event for the daily summary."""
    label = EVENT_LABELS.get(alert.alert_type, alert.alert_type)
    if alert.alert_type == BACK_IN_STOCK:
        return f"{label} por {format_brl(alert.price)}"
    return f"{label}: {format_brl(alert.target_price)} → {format_brl(alert.price)}"


def format_target_alert(
    product: Product, listing: Listing, snapshot: PriceSnapshot, notes: list[str],
) -> str:
    lines = [
        f"🎯 Atingiu seu alvo → {format_brl(snapshot.effective_price)}",
        _short_title(product.name),
        "",
        f"Alvo {format_brl(product.target_price)} · produto {format_brl(snapshot.product_price)}",
        _delivery_line(listing, snapshot),
        *notes,
    ]
    return "\n".join(lines)
