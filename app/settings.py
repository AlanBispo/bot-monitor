"""Application configuration read from the environment."""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import time
from decimal import Decimal
from typing import TypeVar

from .alerts import AlertSettings
from .monitor import ScheduleSettings
from .urls import STORE_DOMAINS

T = TypeVar("T")


def _read(name: str, default: str, convert: Callable[[str], T]) -> T:
    raw = os.getenv(name, default).strip()
    try:
        return convert(raw)
    except (ValueError, ArithmeticError):
        raise ValueError(f"{name} inválido: {raw!r}") from None


def _flag(name: str, default: str) -> bool:
    return os.getenv(name, default).strip().lower() not in {"0", "false", "no"}


def _time(raw: str) -> tuple[int, int]:
    hour, minute = (int(part) for part in raw.split(":"))
    return hour, minute


def _quiet_hours(raw: str) -> tuple[time, time] | None:
    if not raw:
        return None
    start, end = raw.split("-")
    return time(*_time(start)), time(*_time(end))


def _stores(raw: str) -> frozenset[str]:
    stores = frozenset(item.strip().casefold() for item in raw.split(",") if item.strip())
    if not stores or stores - STORE_DOMAINS.keys():
        raise ValueError
    return stores


@dataclass(frozen=True)
class Settings:
    token: str
    chat_id: int
    database_path: str
    search_session_max: int
    search_session_max_age_hours: int
    http_timeout: float
    source_timeout: float
    fetch_timeout: float
    max_response_bytes: int
    enabled_stores: frozenset[str]
    generic_extraction: bool
    discovery_endpoint: str
    discovery_api_key: str
    discovery_format: str
    max_candidates: int
    discovery_concurrency: int
    schedule: ScheduleSettings
    alerts: AlertSettings
    summary_time: tuple[int, int]
    backup_keep: int
    quiet_hours: tuple[time, time] | None

    @classmethod
    def from_env(cls) -> Settings:
        http_timeout = _read("HTTP_TIMEOUT_SECONDS", "15", float)
        return cls(
            token=os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
            chat_id=_read("TELEGRAM_ALLOWED_CHAT_ID", "", int),
            database_path=os.getenv("DATABASE_PATH", "data/prices.db"),
            search_session_max=_read("SEARCH_SESSION_MAX", "100", int),
            search_session_max_age_hours=_read("SEARCH_SESSION_MAX_AGE_HOURS", "24", int),
            http_timeout=http_timeout,
            source_timeout=_read("SOURCE_TIMEOUT_SECONDS", str(http_timeout), float),
            fetch_timeout=_read("FETCH_TOTAL_TIMEOUT_SECONDS", "30", float),
            max_response_bytes=_read("MAX_RESPONSE_BYTES", "2000000", int),
            enabled_stores=_read("ENABLED_STORES", ",".join(STORE_DOMAINS), _stores),
            generic_extraction=_flag("GENERIC_EXTRACTION_ENABLED", "true"),
            discovery_endpoint=os.getenv("GENERIC_DISCOVERY_ENDPOINT", "").strip(),
            discovery_api_key=os.getenv("GENERIC_DISCOVERY_API_KEY", "").strip(),
            discovery_format=os.getenv("GENERIC_DISCOVERY_FORMAT", "json"),
            max_candidates=_read("MAX_CANDIDATES", "50", int),
            discovery_concurrency=_read("DISCOVERY_CONCURRENCY", "4", int),
            schedule=ScheduleSettings(
                default_minutes=max(5, _read("CHECK_INTERVAL_MINUTES", "30", int)),
                near_target_minutes=max(5, _read("CHECK_INTERVAL_NEAR_TARGET_MINUTES", "10", int)),
                near_target_percent=_read("NEAR_TARGET_PERCENT", "10", Decimal),
                store_failure_threshold=max(1, _read("STORE_FAILURE_ALERT_CYCLES", "3", int)),
            ),
            alerts=AlertSettings(
                drop_percent=_read("PRICE_DROP_ALERT_PERCENT", "15", Decimal),
                daily_summary=_flag("DAILY_SUMMARY_ENABLED", "true"),
            ),
            summary_time=_read("DAILY_SUMMARY_TIME", "09:00", _time),
            backup_keep=_read("BACKUP_KEEP_DAYS", "7", int),
            quiet_hours=_read("QUIET_HOURS", "22:00-07:00", _quiet_hours),
        )
