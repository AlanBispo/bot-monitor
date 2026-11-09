"""Telegram interface. Imports Telegram lazily so domain tests need no bot token."""

from __future__ import annotations

import logging

from ..database import Database
from ..discovery import DiscoveryOrchestrator
from ..security import is_authorized_chat
from ..stores.base import StoreAdapter
from .buttons import Buttons
from .commands import Commands, send_daily_summary, send_summary_if_due
from .sender import make_sender

__all__ = [
    "TelegramBot", "build_application", "make_sender", "register_commands", "send_daily_summary",
    "send_summary_if_due",
]

LOGGER = logging.getLogger(__name__)
# Slow network commands must not hold up the other commands while they run.
BACKGROUND_COMMANDS = {"buscar", "check"}
COMMAND_MENU = (
    ("lista", "Produtos monitorados"),
    ("buscar", "Procurar um produto"),
    ("add", "Monitorar um link"),
    ("add_alvo", "Monitorar um link com preço-alvo"),
    ("check", "Verificar preços agora"),
    ("resumo", "Enviar o resumo agora"),
    ("status", "Situação das lojas e ofertas"),
    ("exportar", "Baixar o histórico em CSV"),
    ("help", "Todos os comandos"),
)


async def register_commands(bot) -> None:
    """Fill Telegram's command menu; the bot works normally if this fails."""
    from telegram import BotCommand

    try:
        await bot.set_my_commands([BotCommand(*item) for item in COMMAND_MENU])
    except Exception as exc:
        LOGGER.warning("falha ao registrar o menu de comandos: %s", type(exc).__name__)


class TelegramBot(Commands, Buttons):
    def __init__(
        self,
        database: Database,
        adapters: dict[str, StoreAdapter],
        allowed_chat_id: int,
        discovery: DiscoveryOrchestrator | None = None,
        fetch_timeout_seconds: float = 30,
        quiet_hours=None,
    ):
        self.database = database
        self.adapters = adapters
        self.allowed_chat_id = allowed_chat_id
        self.discovery = discovery
        self.fetch_timeout_seconds = fetch_timeout_seconds
        self.quiet_hours = quiet_hours
        self.pending_selection: dict[int, tuple[str, str]] = {}
        self.pending_target: dict[int, int] = {}

    def authorized(self, update) -> bool:
        chat = update.effective_chat
        return (
            chat is not None
            and getattr(chat, "type", None) == "private"
            and is_authorized_chat(chat.id, self.allowed_chat_id)
        )

    async def _reject_if_unauthorized(self, update) -> bool:
        if self.authorized(update):
            return False
        if update.effective_message:
            await update.effective_message.reply_text("Acesso não autorizado.")
        return True

    async def on_error(self, update, context) -> None:
        LOGGER.error("falha ao processar atualização do Telegram", exc_info=context.error)
        chat = getattr(update, "effective_chat", None)
        if chat is None or chat.id != self.allowed_chat_id:
            return
        try:
            await context.bot.send_message(
                self.allowed_chat_id, "Não consegui concluir essa ação; tente novamente.",
            )
        except Exception as exc:
            LOGGER.warning("falha ao avisar erro no Telegram: %s", type(exc).__name__)


def build_application(
    database: Database,
    adapters: dict[str, StoreAdapter],
    token: str,
    allowed_chat_id: int,
    monitor,
    discovery: DiscoveryOrchestrator | None = None,
    quiet_hours=None,
):
    """Build the PTB application only at runtime, after configuration is validated."""
    if not token.strip():
        raise ValueError("TELEGRAM_BOT_TOKEN não configurado")
    from telegram.ext import (
        ApplicationBuilder,
        CallbackQueryHandler,
        CommandHandler,
        MessageHandler,
        filters,
    )

    bot = TelegramBot(database, adapters, allowed_chat_id, discovery,
                      getattr(monitor, "fetch_timeout_seconds", 30), quiet_hours)
    application = ApplicationBuilder().token(token).build()
    application.bot_data["monitor"] = monitor
    application.add_error_handler(bot.on_error)
    for command, callback in {
        "help": bot.help,
        "start": bot.help,
        "buscar": bot.search,
        "selecionar": bot.select,
        "confirmar": bot.confirm,
        "add": bot.add,
        "add_alvo": bot.add_target,
        "anexar": bot.attach,
        "lista": bot.list_products,
        "historico": bot.history,
        "remover": bot.remove,
        "pausar": bot.pause,
        "exportar": bot.export,
        "resumo": bot.summary,
        "intervalo": bot.set_interval,
        "alvo": bot.set_target,
        "reativar": bot.reactivate,
        "cancelar": bot.cancel_selection,
        "status": bot.status,
        "check": bot.check,
    }.items():
        application.add_handler(
            CommandHandler(command, callback, block=command not in BACKGROUND_COMMANDS)
        )
    application.add_handler(CallbackQueryHandler(bot.selection_button, pattern=r"^sel:"))
    application.add_handler(CallbackQueryHandler(bot.product_button, pattern=r"^prd:"))
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, bot.selection_price, block=False)
    )
    return application
