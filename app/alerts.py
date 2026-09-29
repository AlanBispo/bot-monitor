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
            if not target_reached(snapshot, product.target_price):
                return False
            previous = self.database.last_alert(listing.id, "TARGET_REACHED")
            if previous and snapshot.effective_price >= previous.price:
                history = self.database.history(listing.id)
                prior_rows = [row for row in history if row.id != snapshot.id]
                reentered = bool(
                    prior_rows
                    and prior_rows[-1].effective_price is not None
                    and prior_rows[-1].effective_price > product.target_price
                )
                if not reentered:
                    return False
            message = format_target_alert(product, listing, snapshot)
            alert = self.database.add_alert(
                listing.id, "TARGET_REACHED", snapshot.effective_price, product.target_price
            )
            try:
                await send(message)
            except Exception:
                # The send was not confirmed. Let the next check retry.
                self.database.delete_alert(alert.id)
                raise
            LOGGER.info("alerta TARGET_REACHED enviado para listing %s", listing.id)
            return True


def format_target_alert(product: Product, listing: Listing, snapshot: PriceSnapshot) -> str:
    shipping = (
        "desconhecido" if snapshot.shipping_price is None else f"R$ {snapshot.shipping_price:.2f}"
    )
    return (
        f"🎯 Preço-alvo atingido\n{product.name}\n"
        f"Loja: {listing.store}\nPreço efetivo: R$ {snapshot.effective_price:.2f}\n"
        f"Produto: R$ {snapshot.product_price:.2f}\nFrete: {shipping}\n{listing.url}"
    )
