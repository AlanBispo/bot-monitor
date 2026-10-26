"""Periodic checks for confirmed listings only."""

from __future__ import annotations

import asyncio
import logging

from .alerts import AlertEngine
from .database import Database
from .matching import match_product
from .pricing import InvalidMoney, calculate_price
from .query import InvalidQuery
from .stores.base import StoreAdapter, StoreError, collect_offer

LOGGER = logging.getLogger(__name__)


class Monitor:
    def __init__(
        self, database: Database, adapters: dict[str, StoreAdapter], alert_engine: AlertEngine,
        max_concurrent_checks: int = 4,
        fetch_timeout_seconds: float = 30,
    ):
        self.database = database
        self.adapters = adapters
        self.alert_engine = alert_engine
        self.max_concurrent_checks = max(1, max_concurrent_checks)
        self.fetch_timeout_seconds = fetch_timeout_seconds
        self.last_run: dict[str, int] = {}
        self._cycle_lock = asyncio.Lock()

    async def check_once(self, send_alert) -> int:
        async with self._cycle_lock:
            listings = self.database.list_listings(active_only=True, confirmed_only=True)
            LOGGER.info("Verificando %s anúncios confirmados ativos", len(listings))
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
            self.last_run = {
                "checked": sum(isinstance(item, dict) and item["checked"] for item in results),
                "failed": sum(isinstance(item, Exception) or (
                    isinstance(item, dict) and item["failed"]
                ) for item in results),
                "unknown_shipping": sum(
                    isinstance(item, dict) and item["unknown_shipping"] for item in results
                ),
                "delivery_failed": sum(
                    isinstance(item, dict) and item["delivery_failed"] for item in results
                ),
                "alerts": sum(item["alerts"] for item in results if isinstance(item, dict)),
            }
            return self.last_run["alerts"]

    async def _check_listing(self, listing, send_alert) -> dict[str, int]:
        result = {"checked": 0, "failed": 0, "unknown_shipping": 0,
                  "delivery_failed": 0, "alerts": 0}
        adapter = self.adapters.get(listing.store)
        try:
            if adapter is None:
                raise StoreError("loja não configurada")
            offer = await collect_offer(adapter, listing.url, self.fetch_timeout_seconds)
            product = self.database.get_product(listing.product_id)
            if not product.active or not self.database.get_listing(listing.id).active:
                self.database.cancel_pending_alert(listing.id)
                return result
            if offer.store != listing.store:
                raise StoreError("loja da oferta divergente")
            decision = match_product(product, offer)
            if decision.status == "rejected" or (
                decision.status == "ambiguous" and product.source_query
            ):
                raise StoreError(f"produto divergente: {decision.reason}")
            if not offer.is_available:
                self.database.add_snapshot(
                    listing.id, None, None, None, None, None, None, False, offer.seller
                )
                self.database.cancel_pending_alert(listing.id)
                self.database.record_check(listing.id)
                result["checked"] = 1
                return result
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
            self.database.record_check(listing.id)
            result["checked"] = 1
            if pricing.shipping_price is None:
                result["unknown_shipping"] = 1
            if pricing.effective_price is None and product.monitoring_mode == "target":
                LOGGER.warning("frete desconhecido no listing %s; alerta não avaliado", listing.id)
                self.database.cancel_pending_alert(listing.id)
                return result
            try:
                result["alerts"] = int(
                    await self.alert_engine.process(product, listing, snapshot, send_alert)
                )
            except Exception as exc:
                LOGGER.warning("falha de entrega no listing %s: %s", listing.id, type(exc).__name__)
                result["delivery_failed"] = 1
            return result
        except (StoreError, InvalidMoney, InvalidQuery, ValueError) as exc:
            LOGGER.warning("falha ao consultar listing %s: %s", listing.id, str(exc))
            self.database.record_check(listing.id, error=str(exc)[:160])
            result["failed"] = 1
            return result
