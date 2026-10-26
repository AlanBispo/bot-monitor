"""Deterministic multi-source discovery, validation, matching, and ranking."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, replace
from decimal import Decimal
from urllib.parse import urlsplit, urlunsplit

from .matching import MatchDecision, match_offer
from .models import SearchEntry, StoreOffer
from .pricing import InvalidMoney, PricingResult, calculate_price, parse_money
from .query import SearchQuery
from .security import UnsafeURL, validate_url
from .stores.base import StoreAdapter, StoreError
from .stores.http import request_limited
from .urls import listing_url

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class CandidateURL:
    url: str
    source: str
    store_hint: str | None = None
    title: str = ""
    snippet_price: object | None = None


@dataclass(frozen=True)
class ValidatedResult:
    short_id: str
    offer: StoreOffer
    decision: MatchDecision
    pricing: PricingResult | None
    source: str
    uncertainty: str | None

    @property
    def reliable(self) -> bool:
        return (
            self.decision.status == "matched"
            and self.offer.is_available
            and self.pricing is not None
            and self.pricing.effective_price is not None
        )


@dataclass(frozen=True)
class SearchOutcome:
    query: SearchQuery
    results: tuple[ValidatedResult, ...]
    source_coverage: dict[str, object]
    status: str

    def entries(self) -> list[SearchEntry]:
        return [
            SearchEntry(
                session_id="",
                short_id=result.short_id,
                position=index,
                store=result.offer.store,
                url=result.offer.url,
                canonical_url=result.offer.canonical_url,
                title=result.offer.title,
                seller=result.offer.seller,
                product_price=None if result.pricing is None else result.pricing.product_price,
                shipping_price=None if result.pricing is None else result.pricing.shipping_price,
                effective_price=None if result.pricing is None else result.pricing.effective_price,
                possible_effective_price=(
                    None if result.pricing is None else result.pricing.possible_effective_price
                ),
                match_status=result.decision.status,
                match_reason=result.decision.reason,
                uncertainty=result.uncertainty,
                discovery_source=result.source,
            )
            for index, result in enumerate(self.results, 1)
        ]


class DiscoveryProvider:
    """Provider boundary: it only discovers URLs and never validates prices."""

    name: str

    async def discover(self, query: SearchQuery) -> list[CandidateURL]:
        raise NotImplementedError


class AdapterDiscoveryProvider(DiscoveryProvider):
    def __init__(self, adapter: StoreAdapter):
        self.adapter = adapter
        self.name = f"store:{adapter.name}"

    async def discover(self, query: SearchQuery) -> list[CandidateURL]:
        found: list[CandidateURL] = []
        for variant in query.variants:
            for result in await self.adapter.search(variant):
                found.append(
                    CandidateURL(
                        result.url,
                        self.name,
                        result.store,
                        result.title,
                        # This field is deliberately retained only as provenance/display input.
                        result.product_price,
                    )
                )
        return found


class StaticDiscoveryProvider(DiscoveryProvider):
    """Small deterministic provider useful for fixtures and local acceptance tests."""

    def __init__(self, name: str, results: list[CandidateURL], error: Exception | None = None):
        self.name = name
        self.results = results
        self.error = error
        self.queries: list[SearchQuery] = []

    async def discover(self, query: SearchQuery) -> list[CandidateURL]:
        self.queries.append(query)
        if self.error:
            raise self.error
        return list(self.results)


class JsonWebDiscoveryProvider(DiscoveryProvider):
    """Configurable no-paid-service web discovery boundary.

    The endpoint is expected to return JSON with a `results` array containing at
    least `url`; snippet prices are never used as offer data.
    """

    def __init__(
        self,
        endpoint: str,
        *,
        api_key: str | None = None,
        client=None,
        name="web",
        max_response_bytes: int = 1_000_000,
        timeout_seconds: float = 10,
        api_format: str = "json",
    ):
        self.endpoint = validate_url(endpoint)
        self.api_key = api_key
        self.client = client
        self.name = name
        self.max_response_bytes = max(1024, max_response_bytes)
        self.timeout_seconds = max(0.1, timeout_seconds)
        if api_format not in {"json", "searxng"}:
            raise ValueError("formato de descoberta deve ser json ou searxng")
        self.api_format = api_format

    async def discover(self, query: SearchQuery) -> list[CandidateURL]:
        params = {"q": query.normalized_text}
        if self.api_format == "searxng":
            params.update({"format": "json", "language": "pt-BR", "categories": "general"})
        if self.client is None:
            import httpx

            from .network import PinnedAsyncHTTPTransport

            async with httpx.AsyncClient(
                transport=PinnedAsyncHTTPTransport(),
                timeout=self.timeout_seconds,
                follow_redirects=False,
                trust_env=False,
            ) as client:
                response = await request_limited(
                    client,
                    self.endpoint,
                    self.max_response_bytes,
                    params=params,
                    headers={"Authorization": f"Bearer {self.api_key}"} if self.api_key else {},
                )
        else:
            response = await request_limited(
                self.client,
                self.endpoint,
                self.max_response_bytes,
                params=params,
                headers={"Authorization": f"Bearer {self.api_key}"} if self.api_key else {},
            )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
            raise StoreError("resposta de descoberta sem lista de resultados válida")
        found = []
        for item in payload.get("results", []):
            if not isinstance(item, dict) or not isinstance(item.get("url"), str):
                continue
            found.append(
                CandidateURL(
                    item["url"], self.name, item.get("store"), item.get("title", ""),
                    item.get("price"),
                )
            )
        return found


class DiscoveryOrchestrator:
    def __init__(
        self,
        adapters: dict[str, StoreAdapter],
        providers: list[DiscoveryProvider] | None = None,
        *,
        source_timeout: float | dict[str, float] = 15,
        max_candidates: int = 50,
        concurrency: int = 4,
        generic_adapter: StoreAdapter | None = None,
    ):
        self.adapters = adapters
        self.generic_adapter = generic_adapter
        self.providers = providers if providers is not None else [
            AdapterDiscoveryProvider(adapter)
            for adapter in adapters.values()
            if adapter.name != "generic"
        ]
        if isinstance(source_timeout, dict):
            self.source_timeouts = {
                name: max(0.1, float(timeout)) for name, timeout in source_timeout.items()
            }
            self.source_timeout = 15.0
        else:
            self.source_timeout = max(0.1, float(source_timeout))
            self.source_timeouts = {}
        self.max_candidates = max(1, max_candidates)
        self.semaphore = asyncio.Semaphore(max(1, concurrency))

    def _timeout_for(self, source: str) -> float:
        if source in self.source_timeouts:
            return self.source_timeouts[source]
        if source.startswith("store:"):
            store_name = source.removeprefix("store:")
            if store_name in self.source_timeouts:
                return self.source_timeouts[store_name]
        return self.source_timeout

    @staticmethod
    def _canonical(url: str) -> str:
        parsed = urlsplit(url)
        path = parsed.path or "/"
        return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), path, parsed.query, ""))

    def _adapter_for(self, candidate: CandidateURL, safe_url: str) -> StoreAdapter | None:
        if candidate.store_hint and candidate.store_hint in self.adapters:
            return self.adapters[candidate.store_hint]
        host = urlsplit(safe_url).hostname or ""
        for adapter in self.adapters.values():
            if any(host == domain or host.endswith(f".{domain}") for domain in adapter.domains):
                return adapter
        return self.generic_adapter

    async def _run_provider(self, provider: DiscoveryProvider, query: SearchQuery):
        try:
            async with self.semaphore:
                values = await asyncio.wait_for(
                    provider.discover(query), timeout=self._timeout_for(provider.name)
                )
            return provider.name, values, None
        except Exception as exc:  # external source isolation is intentional
            LOGGER.warning("falha na fonte %s: %s", provider.name, type(exc).__name__)
            return provider.name, [], type(exc).__name__

    async def _validate_candidate(
        self, candidate: CandidateURL, query: SearchQuery
    ) -> tuple[
        CandidateURL,
        StoreOffer | None,
        MatchDecision | None,
        PricingResult | None,
        str | None,
    ]:
        try:
            safe_url = validate_url(candidate.url)
            adapter = self._adapter_for(candidate, safe_url)
            if adapter is None:
                return candidate, None, None, None, "unsupported_store"
            async with self.semaphore:
                offer = await asyncio.wait_for(
                    adapter.get_offer(safe_url), timeout=self._timeout_for(adapter.name)
                )
            if not offer.canonical_url:
                offer = replace(offer, canonical_url=safe_url, discovery_source=candidate.source)
            else:
                try:
                    canonical = validate_url(offer.canonical_url, adapter.domains or None)
                except UnsafeURL:
                    canonical = safe_url
                offer = replace(offer, canonical_url=canonical, discovery_source=candidate.source)
            decision = match_offer(query, offer)
            pricing = None
            if offer.is_available and offer.product_price is not None:
                pricing = calculate_price(
                    offer.product_price,
                    offer.shipping_price,
                    offer.confirmed_discount,
                    offer.possible_discount,
                )
            if query.max_price is not None and decision.status != "rejected":
                max_price = parse_money(query.max_price)
                if pricing is None or pricing.effective_price is None:
                    decision = MatchDecision(
                        "ambiguous", decision.score, "max_price_unverified", decision.attributes
                    )
                elif pricing.effective_price > max_price:
                    decision = MatchDecision(
                        "rejected", decision.score, "over_max_price", decision.attributes
                    )
            uncertainty = None
            if offer.shipping_price is None and offer.is_available:
                uncertainty = "shipping_unknown"
            if pricing is not None and pricing.possible_effective_price is not None:
                uncertainty = (
                    "conditional_discount"
                    if uncertainty is None
                    else f"{uncertainty},conditional_discount"
                )
            return candidate, offer, decision, pricing, uncertainty
        except (TimeoutError, UnsafeURL, StoreError, InvalidMoney, ValueError) as exc:
            return candidate, None, None, None, type(exc).__name__
        except Exception as exc:  # a malformed external page must not abort other candidates
            LOGGER.warning("falha inesperada na validação: %s", type(exc).__name__)
            return candidate, None, None, None, type(exc).__name__

    async def search(self, query: SearchQuery) -> SearchOutcome:
        provider_results = await asyncio.gather(
            *(self._run_provider(provider, query) for provider in self.providers)
        )
        coverage: dict[str, object] = {"sources": {}, "candidates_discovered": 0}
        candidates: list[CandidateURL] = []
        for name, values, error in provider_results:
            coverage["sources"][name] = {
                "status": "failed" if error else "completed",
                "error": error,
                "candidates": len(values),
            }
            candidates.extend(values)
        coverage["candidates_discovered"] = len(candidates)
        unique: list[CandidateURL] = []
        seen: set[str] = set()
        for candidate in candidates:
            try:
                key = self._canonical(validate_url(candidate.url))
            except UnsafeURL:
                continue
            if key not in seen:
                seen.add(key)
                unique.append(candidate)
            if len(unique) >= self.max_candidates:
                break
        validated = await asyncio.gather(
            *(self._validate_candidate(item, query) for item in unique)
        )
        results: list[ValidatedResult] = []
        seen_listings: set[str] = set()
        for candidate, offer, decision, pricing, error in validated:
            if offer is None or decision is None:
                continue
            listing_key = listing_url(offer.url, offer.canonical_url)
            if listing_key in seen_listings:
                continue
            seen_listings.add(listing_key)
            results.append(
                ValidatedResult("", offer, decision, pricing, candidate.source, error)
            )
        results.sort(
            key=lambda result: (
                0 if result.reliable else 1 if result.decision.status == "matched" else 2,
                result.pricing.effective_price
                if result.reliable and result.pricing
                else Decimal("Infinity"),
                result.offer.store.casefold(),
                result.offer.canonical_url or result.offer.url,
            )
        )
        results = [
            ValidatedResult(
                f"R{index}", result.offer, result.decision, result.pricing,
                result.source, result.uncertainty,
            )
            for index, result in enumerate(results, 1)
        ]
        coverage["validated"] = len(results)
        coverage["validation_failed"] = sum(item[1] is None for item in validated)
        coverage["duplicates"] = len(unique) - coverage["validation_failed"] - len(results)
        coverage["ambiguous"] = sum(result.decision.status == "ambiguous" for result in results)
        coverage["rejected"] = sum(result.decision.status == "rejected" for result in results)
        coverage["unknown_shipping"] = sum(
            result.uncertainty is not None
            and "shipping_unknown" in result.uncertainty.split(",")
            for result in results
        )
        status = "partial" if (
            any(item[2] for item in provider_results) or coverage["validation_failed"]
        ) else "complete"
        coverage["status"] = status
        return SearchOutcome(query, tuple(results), coverage, status)
