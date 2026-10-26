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
from app.bot import build_application
from app.database import Database
from app.discovery import DiscoveryOrchestrator, JsonWebDiscoveryProvider
from app.monitor import Monitor
from app.stores.amazon import AmazonAdapter
from app.stores.generic import GenericStructuredAdapter
from app.stores.magalu import MagaluAdapter
from app.stores.mercadolivre import MercadoLivreAdapter


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
    known_stores = {"amazon", "mercadolivre", "magalu"}
    enabled_stores = {
        item.strip().casefold()
        for item in os.getenv("ENABLED_STORES", "amazon,mercadolivre,magalu").split(",")
        if item.strip()
    }
    if not enabled_stores or enabled_stores - known_stores:
        raise ValueError("ENABLED_STORES contém loja desconhecida ou está vazio")
    for store_name in known_stores:
        browser_flag = os.getenv(
            f"{store_name.upper()}_BROWSER_AUTOMATION_ENABLED", "false"
        ).strip().lower()
        if browser_flag not in {"0", "1", "false", "true", "no", "yes"}:
            raise ValueError("configuração de automação de navegador inválida")
        if browser_flag in {"1", "true", "yes"}:
            raise ValueError("automação de navegador não está disponível nesta versão")
    adapters = {}
    adapter_factories = {
        "amazon": lambda store_timeout: AmazonAdapter(store_timeout, max_response_bytes),
        "mercadolivre": lambda store_timeout: MercadoLivreAdapter(
            store_timeout, max_response_bytes
        ),
        "magalu": lambda store_timeout: MagaluAdapter(store_timeout, max_response_bytes),
    }
    for store_name in sorted(enabled_stores):
        env_name = f"{store_name.upper()}_HTTP_TIMEOUT_SECONDS"
        adapters[store_name] = adapter_factories[store_name](
            float(os.getenv(env_name, str(timeout)))
        )
    if os.getenv("GENERIC_EXTRACTION_ENABLED", "true").strip().lower() in {"1", "true", "yes"}:
        adapters["generic"] = GenericStructuredAdapter(
            timeout_seconds=timeout, max_response_bytes=max_response_bytes
        )
    providers = None
    discovery_endpoint = os.getenv("GENERIC_DISCOVERY_ENDPOINT", "").strip()
    if discovery_endpoint:
        providers = [
            JsonWebDiscoveryProvider(
                discovery_endpoint,
                api_key=web_api_key or None,
                max_response_bytes=max_response_bytes,
                timeout_seconds=float(os.getenv("WEB_DISCOVERY_TIMEOUT_SECONDS", "10")),
                api_format=os.getenv("GENERIC_DISCOVERY_FORMAT", "json"),
            )
        ]
        # Store-native providers are added by the orchestrator when providers are omitted;
        # explicitly include them when an optional generic provider is configured.
        from app.discovery import AdapterDiscoveryProvider

        providers = [
            AdapterDiscoveryProvider(adapter)
            for adapter in adapters.values()
            if adapter.name != "generic"
        ] + providers
    default_source_timeout = float(os.getenv("SOURCE_TIMEOUT_SECONDS", str(timeout)))
    source_timeouts = {
        store_name: float(
            os.getenv(f"{store_name.upper()}_SOURCE_TIMEOUT_SECONDS", str(default_source_timeout))
        )
        for store_name in enabled_stores
    }
    source_timeouts["web"] = float(
        os.getenv("WEB_DISCOVERY_SOURCE_TIMEOUT_SECONDS", str(default_source_timeout))
    )
    discovery = DiscoveryOrchestrator(
        adapters,
        providers,
        source_timeout=source_timeouts,
        max_candidates=int(os.getenv("MAX_CANDIDATES", "50")),
        concurrency=int(os.getenv("DISCOVERY_CONCURRENCY", "4")),
        generic_adapter=adapters.get("generic"),
    )
    interval = max(5, int(os.getenv("CHECK_INTERVAL_MINUTES", "30")))
    fetch_timeout = float(os.getenv("FETCH_TOTAL_TIMEOUT_SECONDS", "30"))
    if not 0 < fetch_timeout <= 300:
        raise ValueError("FETCH_TOTAL_TIMEOUT_SECONDS deve estar entre 0 e 300")
    monitor = Monitor(database, adapters, AlertEngine(database), 4, fetch_timeout)
    application = build_application(database, adapters, token, chat_id, monitor, discovery)
    scheduler = AsyncIOScheduler()
    try:
        async with application:
            await application.start()
            try:
                await application.updater.start_polling(
                    allowed_updates=["message", "callback_query"]
                )
                scheduler.add_job(
                    monitor.check_once,
                    "interval",
                    minutes=interval,
                    args=[lambda message: application.bot.send_message(chat_id, message)],
                    id="price-check",
                    replace_existing=True,
                    next_run_time=datetime.now(UTC),
                )
                scheduler.start()
                logging.getLogger(__name__).info(
                    "Price Monitor iniciado; verificação imediata e intervalo %s minutos", interval
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
