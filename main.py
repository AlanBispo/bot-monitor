"""Application entry point for local Windows/WSL execution."""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Iterable
from datetime import UTC, datetime
from decimal import Decimal

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dotenv import load_dotenv

from app.alerts import AlertEngine, AlertSettings
from app.bot import build_application, send_daily_summary
from app.database import Database
from app.discovery import (
    AdapterDiscoveryProvider,
    DiscoveryOrchestrator,
    JsonWebDiscoveryProvider,
)
from app.monitor import Monitor, ScheduleSettings
from app.stores.amazon import AmazonAdapter
from app.stores.generic import GenericStructuredAdapter
from app.stores.magalu import MagaluAdapter
from app.stores.mercadolivre import MercadoLivreAdapter

MONITOR_TICK_MINUTES = 5


class SecretRedactionFilter(logging.Filter):
    def __init__(self, secrets: Iterable[str] = ()):
        super().__init__()
        self._secrets = tuple(secret for secret in secrets if secret)

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        for secret in self._secrets:
            message = message.replace(secret, "[REDACTED]")
        record.msg = message
        record.args = ()
        if record.exc_info and any(
            secret in "".join(logging.Formatter().formatException(record.exc_info))
            for secret in self._secrets
        ):
            record.exc_info = None
        return True


def configure_logging(secrets: Iterable[str] = ()) -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    root = logging.getLogger()
    redaction = SecretRedactionFilter(secrets)
    for handler in root.handlers:
        handler.addFilter(redaction)
    # HTTPX/httpcore can include request URLs in diagnostic logs.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def required_chat_id() -> int:
    raw = os.getenv("TELEGRAM_ALLOWED_CHAT_ID", "").strip()
    try:
        return int(raw)
    except ValueError:
        raise ValueError("TELEGRAM_ALLOWED_CHAT_ID deve ser um número inteiro") from None


async def run() -> None:
    load_dotenv()
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    web_api_key = os.getenv("GENERIC_DISCOVERY_API_KEY", "").strip()
    configure_logging((token, web_api_key))
    chat_id = required_chat_id()
    database = Database(
        os.getenv("DATABASE_PATH", "data/prices.db"),
        search_session_max=int(os.getenv("SEARCH_SESSION_MAX", "100")),
        search_session_max_age_hours=int(os.getenv("SEARCH_SESSION_MAX_AGE_HOURS", "24")),
    )
    database.initialize()
    timeout = float(os.getenv("HTTP_TIMEOUT_SECONDS", "15"))
    max_response_bytes = int(os.getenv("MAX_RESPONSE_BYTES", "2000000"))
    factories = {
        "amazon": AmazonAdapter, "mercadolivre": MercadoLivreAdapter, "magalu": MagaluAdapter,
    }
    enabled_stores = {
        item.strip().casefold()
        for item in os.getenv("ENABLED_STORES", ",".join(factories)).split(",")
        if item.strip()
    }
    if not enabled_stores or enabled_stores - factories.keys():
        raise ValueError("ENABLED_STORES contém loja desconhecida ou está vazio")
    adapters = {
        name: factories[name](timeout, max_response_bytes) for name in sorted(enabled_stores)
    }
    if os.getenv("GENERIC_EXTRACTION_ENABLED", "true").strip().lower() in {"1", "true", "yes"}:
        adapters["generic"] = GenericStructuredAdapter(
            timeout, max_response_bytes
        )
    providers = [
        AdapterDiscoveryProvider(adapter)
        for adapter in adapters.values()
        if adapter.name != "generic"
    ]
    discovery_endpoint = os.getenv("GENERIC_DISCOVERY_ENDPOINT", "").strip()
    if discovery_endpoint:
        providers.append(
            JsonWebDiscoveryProvider(
                discovery_endpoint,
                api_key=web_api_key or None,
                max_response_bytes=max_response_bytes,
                timeout_seconds=timeout,
                api_format=os.getenv("GENERIC_DISCOVERY_FORMAT", "json"),
            )
        )
    discovery = DiscoveryOrchestrator(
        adapters,
        providers,
        source_timeout=float(os.getenv("SOURCE_TIMEOUT_SECONDS", str(timeout))),
        max_candidates=int(os.getenv("MAX_CANDIDATES", "50")),
        concurrency=int(os.getenv("DISCOVERY_CONCURRENCY", "4")),
        generic_adapter=adapters.get("generic"),
    )
    interval = max(5, int(os.getenv("CHECK_INTERVAL_MINUTES", "30")))
    schedule = ScheduleSettings(
        default_minutes=interval,
        near_target_minutes=max(5, int(os.getenv("CHECK_INTERVAL_NEAR_TARGET_MINUTES", "10"))),
        near_target_percent=Decimal(os.getenv("NEAR_TARGET_PERCENT", "10")),
        store_failure_threshold=max(1, int(os.getenv("STORE_FAILURE_ALERT_CYCLES", "3"))),
    )
    alert_settings = AlertSettings(
        drop_percent=Decimal(os.getenv("PRICE_DROP_ALERT_PERCENT", "15")),
        daily_summary=os.getenv("DAILY_SUMMARY_ENABLED", "true").strip().lower() != "false",
    )
    summary_hour, summary_minute = map(int, os.getenv("DAILY_SUMMARY_TIME", "09:00").split(":"))
    fetch_timeout = float(os.getenv("FETCH_TOTAL_TIMEOUT_SECONDS", "30"))
    monitor = Monitor(
        database, adapters, AlertEngine(database, alert_settings), 4, fetch_timeout, schedule,
    )
    application = build_application(database, adapters, token, chat_id, monitor, discovery)
    scheduler = AsyncIOScheduler()
    try:
        async with application:
            await application.start()
            try:
                await application.updater.start_polling(
                    allowed_updates=["message", "callback_query"]
                )
                def send(message):
                    return application.bot.send_message(chat_id, message)

                # Short ticks; each listing is checked only when its own interval is due.
                scheduler.add_job(
                    monitor.check_once,
                    "interval",
                    minutes=MONITOR_TICK_MINUTES,
                    args=[send],
                    kwargs={"due_only": True},
                    id="price-check",
                    replace_existing=True,
                    next_run_time=datetime.now(UTC),
                )
                if alert_settings.daily_summary:
                    scheduler.add_job(
                        send_daily_summary,
                        "cron",
                        hour=summary_hour,
                        minute=summary_minute,
                        args=[database, send],
                        id="daily-summary",
                        replace_existing=True,
                    )
                scheduler.start()
                logging.getLogger(__name__).info(
                    "Price Monitor iniciado; intervalo padrão %s min, perto do alvo %s min",
                    schedule.default_minutes, schedule.near_target_minutes,
                )
                await asyncio.Event().wait()
            finally:
                if application.updater.running:
                    await application.updater.stop()
                if application.running:
                    await application.stop()
    finally:
        if scheduler.running:
            scheduler.shutdown(wait=False)


if __name__ == "__main__":
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        logging.getLogger(__name__).error(
            "falha ao iniciar ou executar o Price Monitor (%s)", type(exc).__name__
        )
        raise SystemExit(1) from None
