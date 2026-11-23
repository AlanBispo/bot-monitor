"""Saved searches: find the cheapest matching offer in each source and warn when it drops."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from .alerts import STORE_LABELS
from .database import Database
from .identity import normalize_text
from .models import Watch, WatchState
from .pricing import InvalidMoney, format_brl
from .stores.base import StoreAdapter, StoreError, collect_offer
from .stores.mercadolivre import AIR_CONDITIONERS_DOMAIN

LOGGER = logging.getLogger(__name__)
ANOMALY_DROPS = (Decimal("1000"), Decimal("500"))
VALIDATION_FETCH_TIMEOUT = 45
NOT_THE_PRODUCT = {"capa", "controle", "suporte", "filtro", "tubulacao", "compativel"}
VOLTAGE_LABELS = {"127": "127V", "220": "220V", "?": "voltagem não informada"}


@dataclass(frozen=True)
class WatchActions:
    """Buttons offered with a saved-search alert; urgent ones ignore quiet hours."""

    url: str
    state_id: int
    urgent: bool = False


@dataclass(frozen=True)
class WatchOffer:
    source: str
    voltage: str
    title: str
    url: str
    price: Decimal


def _words(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", normalize_text(text)))


def voltage_group(text: str) -> str:
    """Bivolt and 110/127V count as the same outlet; 220V is kept apart."""
    normalized = normalize_text(text)
    if re.search(r"\b(?:127|110)\s*v?\b|bivolt", normalized):
        return "127"
    return "220" if re.search(r"\b220\s*v?\b", normalized) else "?"


def matches_watch(watch: Watch, brand_text: str, text: str) -> bool:
    thousands = watch.btu // 1000
    capacity = rf"\b{thousands}[.,]?000\b|\b{thousands}\s*(?:k|mil)\b"
    return (
        normalize_text(watch.brand) in normalize_text(brand_text)
        and _words(watch.line) <= _words(text)
        and re.search(capacity, normalize_text(text)) is not None
    )


def describe_watch(watch: Watch) -> str:
    return f"{watch.brand} {watch.line} {watch.btu:,} BTU".replace(",", ".")


class WatchRunner:
    def __init__(
        self, database: Database, adapters: dict[str, StoreAdapter], interval_hours: float = 6,
    ):
        self.database = database
        self.adapters = adapters
        self.interval = timedelta(hours=interval_hours)

    async def run_due(self, send) -> None:
        now = datetime.now(UTC)
        for watch in self.database.list_watches():
            if watch.last_run_at is None or now - watch.last_run_at >= self.interval:
                await self.run_watch(watch, send)

    async def run_watch(self, watch: Watch, send) -> None:
        """A source that fails keeps its references and does not affect the other one."""
        query = f"ar condicionado {watch.brand} {watch.line} {watch.btu} btus"
        errors = []
        states = {(s.source, s.voltage): s for s in self.database.watch_states(watch.id)}
        offers: list[WatchOffer] = []
        for name, collect in (
            ("mercadolivre", self._mercadolivre_offers), ("amazon", self._amazon_offers),
        ):
            adapter = self.adapters.get(name)
            if adapter is None:
                continue
            try:
                offers.extend(await collect(adapter, watch, query, states))
            except (StoreError, InvalidMoney, ValueError, KeyError) as exc:
                LOGGER.warning("vigia %s falhou em %s: %s", watch.id, name, exc)
                errors.append(f"{STORE_LABELS.get(name, name)}: {str(exc)[:100]}")
        cheapest: dict[tuple[str, str], WatchOffer] = {}
        for offer in sorted(offers, key=lambda item: item.price):
            cheapest.setdefault((offer.source, offer.voltage), offer)
        for key, offer in cheapest.items():
            await self._evaluate(watch, offer, states.get(key), send)
        self.database.record_watch_run(watch.id, "; ".join(errors) or None)

    async def _mercadolivre_offers(self, adapter, watch, query, states) -> list[WatchOffer]:
        if not adapter.client_id or not adapter.client_secret:
            return []

        def accept(row: dict) -> bool:
            attributes = {a.get("id"): a.get("value_name") or "" for a in row.get("attributes", [])}
            text = " ".join([row.get("name", ""), attributes.get("LINE", ""),
                             attributes.get("MODEL", "")])
            return row.get("domain_id") == AIR_CONDITIONERS_DOMAIN and matches_watch(
                watch, attributes.get("BRAND", ""), text,
            )

        offers = []
        for found in await adapter.catalog_search(query, accept):
            voltage = next(
                (a.get("value_name") or "" for a in found["attributes"]
                 if a.get("id") == "VOLTAGE"),
                found["name"],
            )
            offers.append(WatchOffer(
                "mercadolivre", voltage_group(voltage or found["name"]), found["name"],
                found["url"], found["price"],
            ))
        return offers

    async def _amazon_offers(self, adapter, watch, query, states) -> list[WatchOffer]:
        """Search prices only pick candidates; an alert always needs the validated product page."""
        groups: dict[str, list] = {}
        for result in await adapter.search(query):
            words = _words(result.title)
            if (
                result.product_price is not None
                and {"ar", "condicionado"} <= words
                and not words & NOT_THE_PRODUCT
                and matches_watch(watch, result.title, result.title)
            ):
                groups.setdefault(voltage_group(result.title), []).append(result)
        offers = []
        for voltage, results in groups.items():
            candidate = min(results, key=lambda item: item.product_price)
            known = states.get(("amazon", voltage))
            if known is not None and candidate.product_price >= known.reference_price:
                continue
            offer = await collect_offer(adapter, candidate.url, VALIDATION_FETCH_TIMEOUT)
            if offer.is_available and offer.product_price is not None:
                offers.append(WatchOffer(
                    "amazon", voltage, offer.title, candidate.url, offer.product_price,
                ))
        return offers

    async def _evaluate(self, watch, offer: WatchOffer, state: WatchState | None, send) -> None:
        store = STORE_LABELS.get(offer.source, offer.source)
        heading = f"{describe_watch(watch)} ({VOLTAGE_LABELS[offer.voltage]})"
        if state is None:
            self.database.save_watch_reference(
                watch.id, offer.source, offer.voltage, offer.price, offer.title, offer.url,
            )
            await send(
                f"📌 Vigia: referência salva\n{heading}\n"
                f"{store} · {format_brl(offer.price)}\nAviso quando ficar mais barato que isso."
            )
            return
        if offer.price >= state.reference_price:
            return
        drop = state.reference_price - offer.price
        urgent = drop >= ANOMALY_DROPS[-1]
        if drop >= ANOMALY_DROPS[0]:
            first = f"🚨🚨 ANOMALIA: caiu {format_brl(drop)} → {format_brl(offer.price)}"
        elif urgent:
            first = f"🚨 Queda forte: caiu {format_brl(drop)} → {format_brl(offer.price)}"
        else:
            first = f"📉 Vigia: caiu {format_brl(drop)} → {format_brl(offer.price)}"
        lines = [
            first, heading, offer.title[:100], "",
            f"{store} · antes {format_brl(state.reference_price)}",
            f"Primeira busca: {format_brl(state.baseline_price)}",
        ]
        if urgent:
            lines.append("Confira logo: pode ser erro de preço ou oferta relâmpago.")
        saved = self.database.save_watch_reference(
            watch.id, offer.source, offer.voltage, state.reference_price, offer.title, offer.url,
        )
        # The reference only moves after delivery, so a failed send is retried on the next run.
        await send("\n".join(lines), WatchActions(offer.url, saved.id, urgent))
        self.database.save_watch_reference(
            watch.id, offer.source, offer.voltage, offer.price, offer.title, offer.url,
        )
