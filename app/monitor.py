"""Periodic checks for confirmed listings only."""

from __future__ import annotations

import asyncio
import json
import logging

from .alerts import AlertEngine
from .database import Database
from .matching import match_offer
from .pricing import InvalidMoney, calculate_price
from .query import InvalidQuery, parse_query
from .stores.base import StoreAdapter, StoreError

LOGGER = logging.getLogger(__name__)


class Monitor:
    def __init__(
        self, database: Database, adapters: dict[str, StoreAdapter], alert_engine: AlertEngine,
        max_concurrent_checks: int = 4,
    ):
        self.database = database
        self.adapters = adapters
        self.alert_engine = alert_engine
        self.max_concurrent_checks = max(1, max_concurrent_checks)
        self._cycle_lock = asyncio.Lock()

    async def check_once(self, send_alert) -> int:
        async with self._cycle_lock:
            listings = self.database.list_listings(active_only=True, confirmed_only=True)
            semaphore = asyncio.Semaphore(self.max_concurrent_checks)

            async def bounded_check(listing):
                async with semaphore:
                    return await self._check_listing(listing, send_alert)

            results = await asyncio.gather(
                *(bounded_check(listing) for listing in listings), return_exceptions=True
            )
            for listing, result in zip(listings, results, strict=True):
                if isinstance(result, Exception):
                    LOGGER.error("falha no listing %s: %s", listing.id, type(result).__name__)
            return sum(result is True for result in results)

    async def _check_listing(self, listing, send_alert) -> bool:
        adapter = self.adapters.get(listing.store)
        if adapter is None:
            LOGGER.error("adapter ausente para loja %s", listing.store)
            return False
        product = self.database.get_product(listing.product_id)
        try:
            offer = await adapter.get_offer(listing.url)
            if offer.store != listing.store:
                raise StoreError("loja da oferta divergente")
            if offer.is_available:
                identity_query = product.source_query or listing.title[:200]
                decision = match_offer(parse_query(identity_query), offer)
                manual_confirmation = (
                    bool(json.loads(product.identity_json).get("manual_confirmation"))
                    if product.identity_json else False
                )
                if decision.status == "rejected" or (
                    decision.status == "ambiguous"
                    and product.source_query
                    and not manual_confirmation
                ):
                    raise StoreError(f"produto divergente: {decision.reason}")
            if not offer.is_available:
                self.database.add_snapshot(
                    listing.id, None, None, None, None, None, None, False, offer.seller
                )
                return False
            if offer.product_price is None:
                raise StoreError("oferta sem preço válido")
            pricing = calculate_price(
                offer.product_price,
                offer.shipping_price,
                offer.confirmed_discount,
                offer.possible_discount,
            )
            snapshot = self.database.add_snapshot(
                listing.id,
                pricing.product_price,
                pricing.shipping_price,
                pricing.confirmed_discount,
                pricing.possible_discount,
                pricing.effective_price,
                pricing.possible_effective_price,
                True,
                offer.seller,
            )
            if pricing.effective_price is None:
                LOGGER.warning("frete desconhecido no listing %s; alerta não avaliado", listing.id)
                return False
            return await self.alert_engine.process(product, listing, snapshot, send_alert)
        except (StoreError, InvalidMoney, InvalidQuery, ValueError) as exc:
            LOGGER.warning("falha ao consultar listing %s: %s", listing.id, type(exc).__name__)
            return False
