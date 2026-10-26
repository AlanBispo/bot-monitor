"""Alert decisions and Telegram alert formatting."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from decimal import Decimal

from .database import Database
from .models import Listing, PriceSnapshot, Product

LOGGER = logging.getLogger(__name__)


def target_reached(snapshot: PriceSnapshot, target_price: Decimal) -> bool:
    return (
        snapshot.available
        and snapshot.effective_price is not None
        and snapshot.effective_price <= target_price
    )


class AlertEngine:
    def __init__(self, database: Database):
        self.database = database
        self._lock = asyncio.Lock()

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


def format_target_alert(product: Product, listing: Listing, snapshot: PriceSnapshot) -> str:
    shipping = (
        "desconhecido" if snapshot.shipping_price is None else f"R$ {snapshot.shipping_price:.2f}"
    )
    return (
        f"🎯 Preço-alvo atingido\n{product.name}\n"
        f"Loja: {listing.store}\nPreço efetivo: R$ {snapshot.effective_price:.2f}\n"
        f"Produto: R$ {snapshot.product_price:.2f}\nFrete: {shipping}\n{listing.url}"
    )
