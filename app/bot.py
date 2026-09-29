"""Telegram command handlers. Imports Telegram lazily so domain tests need no bot token."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from decimal import Decimal
from urllib.parse import urlparse

from .database import Database, DatabaseError
from .discovery import DiscoveryOrchestrator, SearchOutcome
from .matching import match_offer, offer_identity_attributes
from .models import StoreSearchResult
from .pricing import calculate_price, parse_money
from .query import InvalidQuery, parse_query
from .security import UnsafeURL, is_authorized_chat, validate_url
from .stores.base import StoreAdapter, StoreError

MAX_QUERY_LENGTH = 200
MAX_EXTERNAL_TITLE_LENGTH = 300
TELEGRAM_TEXT_LIMIT = 4096
MAX_DISPLAY_URL_LENGTH = 1800
DATABASE_FAILURE_MESSAGE = "Falha no banco de dados; tente novamente."


def short_text(value: str, limit: int = MAX_EXTERNAL_TITLE_LENGTH) -> str:
    text = " ".join(value.split())
    return text if len(text) <= limit else f"{text[: limit - 1]}…"


def split_telegram_text(text: str, limit: int = TELEGRAM_TEXT_LIMIT) -> list[str]:
    """Split long responses at line boundaries, splitting only oversized records."""
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    current = ""
    for record in text.splitlines():
        if len(record) > limit:
            if current:
                chunks.append(current)
                current = ""
            while len(record) > limit:
                chunks.append(record[:limit])
                record = record[limit:]
            current = record
            continue
        candidate = record if not current else f"{current}\n{record}"
        if len(candidate) <= limit:
            current = candidate
        else:
            chunks.append(current)
            current = record
    if current or not chunks:
        chunks.append(current)
    return chunks


def parse_add_arguments(text: str) -> tuple[Decimal, str]:
    """Parse `/add TARGET URL`, keeping URL parsing separate from Telegram."""
    parts = text.strip().split(maxsplit=1)
    if len(parts) != 2:
        raise ValueError("uso: /add 29,90 https://...")
    target = parse_money(parts[0])
    return target, parts[1].strip()


def format_search_results(results: Iterable[StoreSearchResult]) -> str:
    rows = list(results)
    if not rows:
        return "Nenhum resultado encontrado."
    lines = ["Resultados encontrados. Para cadastrar um resultado:", "/add PREÇO_ALVO URL", ""]
    for index, result in enumerate(rows, 1):
        price = "indisponível" if result.product_price is None else f"R$ {result.product_price:.2f}"
        lines.append(
            f"{index}. [{result.store}] {short_text(result.title, 180)} — {price}\n"
            f"{short_text(result.url, MAX_DISPLAY_URL_LENGTH)}"
        )
    return "\n".join(lines)


def format_discovery_results(session_id: str, outcome: SearchOutcome) -> str:
    coverage = outcome.source_coverage
    sources = coverage.get("sources", {})
    completed = [name for name, data in sources.items() if data.get("status") == "completed"]
    failed = [name for name, data in sources.items() if data.get("status") == "failed"]
    lines = [
        f"Busca {session_id} ({outcome.status}); fontes concluídas: "
        f"{', '.join(completed) or 'nenhuma'}.",
    ]
    if failed:
        lines.append(f"Fontes com falha/timeout: {', '.join(failed)}. Resultado parcial.")
    lines.append(
        f"Candidatos: {coverage.get('candidates_discovered', 0)}; "
        f"validados: {coverage.get('validated', 0)}; "
        f"falhas de validação: {coverage.get('validation_failed', 0)}; "
        f"ambíguos: {coverage.get('ambiguous', 0)}; excluídos: {coverage.get('rejected', 0)}."
    )
    lines.append(
        "Selecione: /selecionar SESSAO ID PREÇO_ALVO (ou use um botão); "
        "para ambíguo, use /confirmar."
    )
    lines.append("")
    if not outcome.results:
        lines.append("Nenhuma oferta validada.")
        return "\n".join(lines)
    for result in outcome.results:
        if result.pricing is None:
            price = "sem preço confiável"
        elif result.pricing.effective_price is not None:
            price = f"efetivo R$ {result.pricing.effective_price:.2f}"
            if result.pricing.possible_effective_price is not None:
                possible = result.pricing.possible_effective_price
                price += f"; possível R$ {possible:.2f} (condicional)"
            if result.uncertainty:
                price += f" ({result.uncertainty})"
        else:
            price = "frete desconhecido; não é o menor garantido"
            if result.pricing.possible_effective_price is not None:
                price += f"; possível R$ {result.pricing.possible_effective_price:.2f}"
        marker = result.decision.status
        lines.append(
            f"{result.short_id}. [{result.offer.store}] "
            f"{short_text(result.offer.title, 180)} — {price} "
            f"[{marker}: {result.decision.reason}]\n"
            f"{short_text(result.offer.url, MAX_DISPLAY_URL_LENGTH)}"
        )
    if coverage.get("unknown_shipping"):
        lines.append(
            "Ofertas com frete desconhecido foram exibidas, mas não entram "
            "no menor preço garantido."
        )
    return "\n".join(lines)


class TelegramBot:
    def __init__(
        self,
        database: Database,
        adapters: dict[str, StoreAdapter],
        allowed_chat_id: int,
        discovery: DiscoveryOrchestrator | None = None,
    ):
        self.database = database
        self.adapters = adapters
        self.allowed_chat_id = allowed_chat_id
        self.discovery = discovery

    def authorized(self, update) -> bool:
        chat = update.effective_chat
        return (
            chat is not None
            and getattr(chat, "type", None) == "private"
            and is_authorized_chat(chat.id, self.allowed_chat_id)
        )

    def adapter_for_url(self, safe_url: str) -> StoreAdapter | None:
        hostname = urlparse(safe_url).hostname or ""
        adapter = next(
            (
                candidate
                for candidate in self.adapters.values()
                if any(
                    hostname == domain or hostname.endswith(f".{domain}")
                    for domain in candidate.domains
                )
            ),
            None,
        )
        return adapter or self.adapters.get("generic")

    @staticmethod
    def validated_offer(offer, adapter: StoreAdapter) -> bool:
        if not offer.is_available or not offer.title or offer.product_price is None:
            return False
        if offer.store != adapter.name:
            return False
        calculate_price(
            offer.product_price, offer.shipping_price,
            offer.confirmed_discount, offer.possible_discount,
        )
        return True

    @staticmethod
    def safe_canonical_url(offer, adapter: StoreAdapter, source_url: str) -> str:
        try:
            canonical = validate_url(offer.canonical_url or source_url, adapter.domains or None)
            if adapter.name == "generic" and (
                urlparse(canonical).hostname != urlparse(source_url).hostname
            ):
                return source_url
            return canonical
        except UnsafeURL:
            return source_url

    @staticmethod
    def offer_uncertainty(offer) -> str | None:
        flags = []
        if offer.shipping_price is None:
            flags.append("shipping_unknown")
        if offer.possible_discount is not None:
            flags.append("conditional_discount")
        return ",".join(flags) or None

    async def _reject_if_unauthorized(self, update) -> bool:
        if self.authorized(update):
            return False
        if update.effective_message:
            await update.effective_message.reply_text("Acesso não autorizado.")
        return True

    async def _reply(self, update, text: str) -> None:
        for chunk in split_telegram_text(text):
            await update.effective_message.reply_text(chunk)

    async def help(self, update, context) -> None:
        if await self._reject_if_unauthorized(update):
            return
        await update.effective_message.reply_text(
            "/buscar NOME — procurar produtos\n"
            "/selecionar BUSCA RESULTADO ALVO — confirmar resultado\n"
            "/confirmar BUSCA RESULTADO ALVO — confirmar resultado ambíguo\n"
            "/add PREÇO_ALVO URL — cadastrar uma URL\n"
            "/anexar ID_PRODUTO URL — adicionar outra oferta\n"
            "/lista — listar produtos\n"
            "/check — verificar agora\n"
            "/historico ID — mostrar histórico\n"
            "/remover ID — desativar produto\n"
            "/status — mostrar status\n/help — esta ajuda"
        )

    async def search(self, update, context) -> None:
        if await self._reject_if_unauthorized(update):
            return
        query = " ".join(context.args).strip()
        if not query or len(query) > MAX_QUERY_LENGTH:
            await update.effective_message.reply_text(
                "Uso: /buscar nome do produto (até 200 caracteres)."
            )
            return
        if self.discovery is not None:
            try:
                outcome = await self.discovery.search(parse_query(query))
                session = self.database.create_search_session(
                    update.effective_chat.id,
                    outcome.query.original_text,
                    outcome.query.to_json(),
                    outcome.status,
                    outcome.source_coverage,
                    outcome.entries(),
                )
                await self._reply(update, format_discovery_results(session.id, outcome))
                try:
                    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
                    buttons = [
                        [
                            InlineKeyboardButton(
                                f"{result.short_id} · {result.offer.store}",
                                callback_data=f"sel:{session.id}:{result.short_id}",
                            )
                        ]
                        for result in outcome.results
                        if result.decision.status == "matched"
                    ][:20]
                except ImportError:
                    buttons = []
                if buttons:
                    try:
                        await update.effective_message.reply_text(
                            "Seleção rápida (o botão exibirá o comando com o preço-alvo):",
                            reply_markup=InlineKeyboardMarkup(buttons),
                        )
                    except TypeError:
                        # Domain tests and minimal Telegram fakes do not implement markup.
                        pass
                return
            except (InvalidQuery, DatabaseError, ValueError) as exc:
                await update.effective_message.reply_text(str(exc))
                return
            except sqlite3.Error:
                await update.effective_message.reply_text(DATABASE_FAILURE_MESSAGE)
                return
        await update.effective_message.reply_text("Busca não configurada.")

    async def add(self, update, context) -> None:
        if await self._reject_if_unauthorized(update):
            return
        try:
            target, raw_url = parse_add_arguments(" ".join(context.args))
            safe_url = validate_url(raw_url)
            adapter = self.adapter_for_url(safe_url)
            if adapter is None:
                raise UnsafeURL("domínio não suportado")
            offer = await adapter.get_offer(safe_url)
            if not self.validated_offer(offer, adapter):
                await update.effective_message.reply_text(
                    "Não foi possível validar um produto nessa URL."
                )
                return
            product, listing = self.database.register_product_listing(
                short_text(offer.title),
                target,
                offer.store,
                safe_url,
                short_text(offer.title),
                offer.seller,
                canonical_url=self.safe_canonical_url(offer, adapter, safe_url),
                source_query=short_text(offer.title, 200),
                identity_json=json.dumps(
                    {"attributes": offer_identity_attributes(offer),
                     "registration_source": "direct_url"},
                    ensure_ascii=False, sort_keys=True,
                ),
                uncertainty=self.offer_uncertainty(offer),
            )
            await update.effective_message.reply_text(
                f"Produto cadastrado: {product.name}\nAlvo: R$ {target:.2f}\n"
                f"Listing {listing.id} será monitorado no próximo ciclo."
            )
        except (ValueError, StoreError, DatabaseError) as exc:
            await update.effective_message.reply_text(str(exc))
        except sqlite3.Error:
            await update.effective_message.reply_text(DATABASE_FAILURE_MESSAGE)

    async def select(self, update, context, allow_ambiguous: bool = False) -> None:
        if await self._reject_if_unauthorized(update):
            return
        if len(context.args) != 3:
            await update.effective_message.reply_text(
                "Uso: /selecionar ID_DA_BUSCA ID_DO_RESULTADO PREÇO_ALVO"
            )
            return
        session_id, short_id, raw_target = context.args
        try:
            target = parse_money(raw_target)
            entry = self.database.get_search_entry(
                session_id, short_id, chat_id=update.effective_chat.id
            )
            session = self.database.get_search_session(session_id, update.effective_chat.id)
            if entry.match_status != "matched" and not (
                allow_ambiguous and entry.match_status == "ambiguous"
            ):
                await update.effective_message.reply_text(
                    f"Resultado {entry.short_id} não é uma correspondência confiável: "
                    f"{entry.match_reason}."
                )
                return
            adapter = self.adapters.get(entry.store)
            if adapter is None and self.discovery is not None:
                adapter = self.discovery.generic_adapter
            if adapter is None:
                await update.effective_message.reply_text(
                    "A loja desse resultado não está configurada."
                )
                return
            safe_url = validate_url(entry.url, adapter.domains or None)
            offer = await adapter.get_offer(safe_url)
            if not self.validated_offer(offer, adapter):
                await update.effective_message.reply_text(
                    "A oferta mudou e não pôde ser confirmada; busque novamente."
                )
                return
            query = parse_query(session.original_query)
            decision = match_offer(query, offer)
            if decision.status == "rejected" or (
                decision.status == "ambiguous" and not allow_ambiguous
            ):
                await update.effective_message.reply_text(
                    f"A oferta mudou ({decision.reason}); busque novamente."
                )
                return
            if query.max_price is not None:
                pricing = calculate_price(
                    offer.product_price, offer.shipping_price,
                    offer.confirmed_discount, offer.possible_discount,
                )
                if pricing.effective_price is None or pricing.effective_price > parse_money(
                    query.max_price
                ):
                    await update.effective_message.reply_text(
                        "A oferta não atende mais ao preço máximo; busque novamente."
                    )
                    return
            product, listing = self.database.register_product_listing(
                short_text(offer.title), target, offer.store, safe_url, short_text(offer.title),
                offer.seller, canonical_url=self.safe_canonical_url(offer, adapter, safe_url),
                discovery_source=entry.discovery_source,
                source_query=session.original_query,
                identity_json=json.dumps(
                    {"attributes": decision.attributes,
                     "manual_confirmation": decision.status == "ambiguous"},
                    ensure_ascii=False, sort_keys=True,
                ),
                uncertainty=self.offer_uncertainty(offer),
            )
            await update.effective_message.reply_text(
                f"Resultado {entry.short_id} confirmado: produto {product.id}, "
                f"listing {listing.id}."
            )
        except (KeyError, ValueError, StoreError, DatabaseError) as exc:
            await update.effective_message.reply_text(str(exc))
        except sqlite3.Error:
            await update.effective_message.reply_text(DATABASE_FAILURE_MESSAGE)

    async def confirm(self, update, context) -> None:
        await self.select(update, context, allow_ambiguous=True)

    async def selection_button(self, update, context) -> None:
        query = update.callback_query
        message = query.message if query else None
        if (
            query is None
            or message is None
            or getattr(getattr(message, "chat", None), "type", None) != "private"
            or not is_authorized_chat(getattr(message, "chat_id", None), self.allowed_chat_id)
        ):
            if query:
                await query.answer("Acesso não autorizado.", show_alert=True)
            return
        parts = query.data.split(":", 2) if isinstance(query.data, str) else []
        if len(parts) != 3 or not parts[1] or not parts[2]:
            await query.answer("Seleção inválida.", show_alert=True)
            return
        _, session_id, short_id = parts
        await query.answer()
        await message.reply_text(
            f"Para confirmar {short_id}, envie: /selecionar {session_id} {short_id} PREÇO_ALVO"
        )

    async def attach(self, update, context) -> None:
        if await self._reject_if_unauthorized(update):
            return
        if len(context.args) != 2 or not context.args[0].isdigit():
            await update.effective_message.reply_text("Uso: /anexar ID_DO_PRODUTO URL")
            return
        try:
            product = self.database.get_product(int(context.args[0]))
            if not product.active:
                await update.effective_message.reply_text("Esse produto está inativo.")
                return
            safe_url = validate_url(context.args[1])
            adapter = self.adapter_for_url(safe_url)
            if adapter is None:
                raise UnsafeURL("domínio não suportado")
            offer = await adapter.get_offer(safe_url)
            if not self.validated_offer(offer, adapter):
                await update.effective_message.reply_text(
                    "Não foi possível validar um produto nessa URL."
                )
                return
            decision = match_offer(parse_query((product.source_query or product.name)[:200]), offer)
            if decision.status != "matched":
                await update.effective_message.reply_text(
                    f"Oferta não corresponde ao produto ({decision.reason})."
                )
                return
            listing = self.database.add_listing(
                product.id,
                offer.store,
                safe_url,
                short_text(offer.title),
                offer.seller,
                canonical_url=self.safe_canonical_url(offer, adapter, safe_url),
                discovery_source="direct_url",
                uncertainty=self.offer_uncertainty(offer),
            )
            await update.effective_message.reply_text(
                f"Listing {listing.id} anexado ao produto {product.id}."
            )
        except (KeyError, ValueError, StoreError, DatabaseError) as exc:
            await update.effective_message.reply_text(str(exc))
        except sqlite3.Error:
            await update.effective_message.reply_text(DATABASE_FAILURE_MESSAGE)

    async def list_products(self, update, context) -> None:
        if await self._reject_if_unauthorized(update):
            return
        try:
            products = self.database.list_products(active_only=True)
            listings = self.database.list_listings(active_only=False)
        except sqlite3.Error:
            await update.effective_message.reply_text(DATABASE_FAILURE_MESSAGE)
            return
        if not products:
            await update.effective_message.reply_text("Nenhum produto ativo.")
            return
        lines = []
        for product in products:
            lines.append(
                f"{product.id}. {short_text(product.name)} — alvo R$ {product.target_price:.2f}"
            )
            for listing in listings:
                if listing.product_id == product.id and listing.active:
                    lines.append(f"   listing {listing.id} [{listing.store}] {listing.url}")
        await self._reply(update, "\n".join(lines))

    async def history(self, update, context) -> None:
        if await self._reject_if_unauthorized(update):
            return
        if len(context.args) != 1 or not context.args[0].isdigit():
            await update.effective_message.reply_text("Uso: /historico ID_DO_PRODUTO")
            return
        product_id = int(context.args[0])
        try:
            stats = self.database.product_history_stats(product_id)
        except KeyError:
            await update.effective_message.reply_text("Produto não encontrado.")
            return
        except sqlite3.Error:
            await update.effective_message.reply_text(DATABASE_FAILURE_MESSAGE)
            return
        if stats.samples == 0:
            await update.effective_message.reply_text("Nenhum preço confiável no histórico.")
            return
        lines = [
            f"Produto {product_id}: {stats.samples} observações confiáveis",
            (
                (
                    f"Atual: R$ {stats.current_price:.2f}"
                    + (
                        " (menor oferta confiável; há listing sem preço atual)"
                        if stats.current_uncertain
                        else ""
                    )
                )
                if stats.current_price is not None
                else "Atual: indisponível ou não calculável"
            ),
            f"Mínimo: R$ {stats.lowest_price:.2f}",
            f"Máximo: R$ {stats.highest_price:.2f}",
            f"Média: R$ {stats.average_price:.2f}",
            f"Menor no período consultado: R$ {stats.lowest_in_window:.2f}",
        ]
        try:
            current_by_store = self.database.current_state_by_store(product_id)
            history_by_store = self.database.history_by_store(product_id)
        except sqlite3.Error:
            await update.effective_message.reply_text(DATABASE_FAILURE_MESSAGE)
            return
        for store, rows in history_by_store.items():
            valid = [
                row.effective_price
                for row in rows
                if row.available and row.effective_price is not None
            ]
            if valid:
                current_price, current_uncertain = current_by_store[store]
                current = (
                    f"R$ {current_price:.2f}"
                    + (
                        " (menor oferta confiável; há listing sem preço atual)"
                        if current_uncertain
                        else ""
                    )
                    if current_price is not None
                    else "indisponível ou não calculável"
                )
                lines.append(f"{store}: atual {current}, mínimo R$ {min(valid):.2f}")
        await self._reply(update, "\n".join(lines))

    async def remove(self, update, context) -> None:
        if await self._reject_if_unauthorized(update):
            return
        if len(context.args) != 1 or not context.args[0].isdigit():
            await update.effective_message.reply_text("Uso: /remover ID_DO_PRODUTO")
            return
        try:
            changed = self.database.deactivate_product(int(context.args[0]))
        except sqlite3.Error:
            await update.effective_message.reply_text(DATABASE_FAILURE_MESSAGE)
            return
        message = "Produto desativado." if changed else "Produto não encontrado ou já inativo."
        await update.effective_message.reply_text(message)

    async def status(self, update, context) -> None:
        if await self._reject_if_unauthorized(update):
            return
        try:
            products = self.database.list_products(active_only=True)
            listings = self.database.list_listings(active_only=True, confirmed_only=True)
        except sqlite3.Error:
            await update.effective_message.reply_text(DATABASE_FAILURE_MESSAGE)
            return
        await update.effective_message.reply_text(
            f"Ativos: {len(products)} produtos, {len(listings)} URLs monitoradas."
        )

    async def check(self, update, context) -> None:
        if await self._reject_if_unauthorized(update):
            return
        monitor = context.application.bot_data.get("monitor")
        if monitor is None:
            await update.effective_message.reply_text("Monitor indisponível.")
            return
        try:
            count = await monitor.check_once(
                lambda message: context.bot.send_message(self.allowed_chat_id, message)
            )
        except sqlite3.Error:
            await update.effective_message.reply_text(DATABASE_FAILURE_MESSAGE)
            return
        await update.effective_message.reply_text(
            f"Verificação concluída; {count} alerta(s) enviado(s)."
        )


def build_application(
    database: Database,
    adapters: dict[str, StoreAdapter],
    token: str,
    allowed_chat_id: int,
    monitor,
    discovery: DiscoveryOrchestrator | None = None,
):
    """Build the PTB application only at runtime, after configuration is validated."""
    if not token.strip():
        raise ValueError("TELEGRAM_BOT_TOKEN não configurado")
    from telegram.ext import ApplicationBuilder, CommandHandler

    bot = TelegramBot(database, adapters, allowed_chat_id, discovery)
    application = ApplicationBuilder().token(token).build()
    application.bot_data["monitor"] = monitor
    for command, callback in {
        "help": bot.help,
        "buscar": bot.search,
        "selecionar": bot.select,
        "confirmar": bot.confirm,
        "add": bot.add,
        "anexar": bot.attach,
        "lista": bot.list_products,
        "historico": bot.history,
        "remover": bot.remove,
        "status": bot.status,
        "check": bot.check,
    }.items():
        application.add_handler(CommandHandler(command, callback))
    try:
        from telegram.ext import CallbackQueryHandler

        application.add_handler(CallbackQueryHandler(bot.selection_button, pattern=r"^sel:"))
    except ImportError:
        pass
    return application
