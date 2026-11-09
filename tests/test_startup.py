import asyncio
from types import SimpleNamespace

import pytest

import main


class FakeUpdater:
    running = False

    async def start_polling(self, **kwargs):
        self.running = True

    async def stop(self):
        self.running = False


class FakeApplication:
    running = False

    def __init__(self):
        self.updater = FakeUpdater()
        self.bot = SimpleNamespace(send_message=None)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def start(self):
        self.running = True

    async def stop(self):
        self.running = False


@pytest.mark.asyncio
@pytest.mark.parametrize("startup_delay", [0])
async def test_startup_checks_immediately_after_telegram_is_ready(
    monkeypatch, tmp_path, startup_delay,
):
    application = FakeApplication()
    checked = asyncio.Event()
    calls = []

    async def start_polling(**kwargs):
        await asyncio.sleep(startup_delay)
        application.updater.running = True

    application.updater.start_polling = start_polling

    async def check_once(send_alert, *, due_only=False):
        calls.append((application.running, application.updater.running))
        checked.set()

    monkeypatch.setattr(main, "load_dotenv", lambda: None)
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_ID", "123")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "prices.db"))
    monkeypatch.setenv("GENERIC_DISCOVERY_ENDPOINT", "")
    monkeypatch.setenv("CHECK_INTERVAL_MINUTES", "30")
    monkeypatch.setattr(main, "build_application", lambda *args: application)
    monkeypatch.setattr(main, "Monitor", lambda *args: SimpleNamespace(check_once=check_once))

    task = asyncio.create_task(main.run())
    try:
        await asyncio.wait_for(checked.wait(), timeout=5)
        assert calls == [(True, True)]
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert not application.running
    assert not application.updater.running


@pytest.mark.asyncio
async def test_polling_failure_preserves_original_error_and_stops_application(
    monkeypatch, tmp_path,
):
    application = FakeApplication()

    async def fail_polling(**kwargs):
        raise RuntimeError("polling unavailable")

    application.updater.start_polling = fail_polling
    monkeypatch.setattr(main, "load_dotenv", lambda: None)
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_ID", "123")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "prices.db"))
    monkeypatch.setenv("GENERIC_DISCOVERY_ENDPOINT", "")
    monkeypatch.setattr(main, "build_application", lambda *args: application)

    with pytest.raises(RuntimeError, match="polling unavailable"):
        await main.run()
    assert not application.running
