"""Application entry point for local Windows/WSL execution."""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Iterable
from datetime import UTC, datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dotenv import load_dotenv

from app.alerts import AlertEngine
from app.bot import build_application, send_summary_if_due
from app.database import Database
from app.discovery import (
    AdapterDiscoveryProvider,
    DiscoveryOrchestrator,
    JsonWebDiscoveryProvider,
)
from app.monitor import Monitor
from app.settings import Settings
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


def build_adapters(settings: Settings) -> dict:
    factories = {
        "amazon": AmazonAdapter, "mercadolivre": MercadoLivreAdapter, "magalu": MagaluAdapter,
    }
    limits = (settings.http_timeout, settings.max_response_bytes)
    adapters = {name: factories[name](*limits) for name in sorted(settings.enabled_stores)}
    if settings.generic_extraction:
        adapters["generic"] = GenericStructuredAdapter(*limits)
    return adapters


def build_discovery(settings: Settings, adapters: dict) -> DiscoveryOrchestrator:
    providers = [
        AdapterDiscoveryProvider(adapter)
        for adapter in adapters.values()
        if adapter.name != "generic"
    ]
    if settings.discovery_endpoint:
        providers.append(
            JsonWebDiscoveryProvider(
                settings.discovery_endpoint,
                api_key=settings.discovery_api_key or None,
                max_response_bytes=settings.max_response_bytes,
                timeout_seconds=settings.http_timeout,
                api_format=settings.discovery_format,
            )
        )
    return DiscoveryOrchestrator(
        adapters,
        providers,
        source_timeout=settings.source_timeout,
        max_candidates=settings.max_candidates,
        concurrency=settings.discovery_concurrency,
        generic_adapter=adapters.get("generic"),
    )


async def run() -> None:
    load_dotenv()
    # Before parsing, so that any configuration failure already logs through redaction.
    configure_logging(
        os.getenv(name, "").strip()
        for name in ("TELEGRAM_BOT_TOKEN", "GENERIC_DISCOVERY_API_KEY")
    )
    settings = Settings.from_env()
    chat_id = settings.chat_id
    database = Database(
        settings.database_path,
        search_session_max=settings.search_session_max,
        search_session_max_age_hours=settings.search_session_max_age_hours,
    )
    database.initialize()
    adapters = build_adapters(settings)
    discovery = build_discovery(settings, adapters)
    schedule = settings.schedule
    monitor = Monitor(
        database, adapters, AlertEngine(database, settings.alerts), 4, settings.fetch_timeout,
        schedule,
    )
    application = build_application(
        database, adapters, settings.token, chat_id, monitor, discovery,
    )
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
                if settings.alerts.daily_summary:
                    # Polling instead of cron: a PC that was off at the set time still catches up.
                    scheduler.add_job(
                        send_summary_if_due,
                        "interval",
                        minutes=MONITOR_TICK_MINUTES,
                        args=[database, send, *settings.summary_time],
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
            "falha ao iniciar ou executar o Price Monitor (%s): %s", type(exc).__name__, exc
        )
        raise SystemExit(1) from None
