"""Telegram command handlers."""

from __future__ import annotations

import asyncio
import io
import json
import re
import sqlite3
from datetime import datetime
from decimal import Decimal
from functools import wraps
from types import SimpleNamespace
from urllib.parse import urlparse

from ..charts import NoChartData, render_price_chart
from ..database import MAX_CHECK_INTERVAL, MIN_CHECK_INTERVAL, Database, DatabaseError
from ..digest import build_daily_summary
from ..export import history_csv
from ..matching import match_offer, match_product, offer_identity_attributes
from ..pricing import calculate_price, format_brl, parse_money
from ..query import parse_query
from ..security import UnsafeURL, validate_url
from ..stores.base import StoreAdapter, StoreError, collect_offer
from ..urls import host_matches
from .buttons import MAX_LIST_BUTTON_PRODUCTS, product_keyboard
from .formatting import (
    DATABASE_FAILURE_MESSAGE,
    format_discovery_results,
    product_identifier,
    short_text,
    split_telegram_text,
)

MAX_QUERY_LENGTH = 200
MAX_HISTORY_DAYS = 3650
LAST_SUMMARY_KEY = "last_daily_summary"
LINK = re.compile(r"https?://\S+", re.IGNORECASE)


def command(handler):
    """Authorize a Telegram command and answer expected failures without details."""

    @wraps(handler)
    async def wrapper(self, update, context, **kwargs):
        if await self._reject_if_unauthorized(update):
            return
        try:
            await handler(self, update, context, **kwargs)
        except (KeyError, ValueError, StoreError, DatabaseError) as exc:
            await update.effective_message.reply_text(exc.args[0] if exc.args else str(exc))
        except sqlite3.Error:
            await update.effective_message.reply_text(DATABASE_FAILURE_MESSAGE)

    return wrapper


def parse_add_arguments(text: str) -> tuple[Decimal, str]:
    """Parse `/add_alvo TARGET URL` (also accepted as legacy `/add TARGET URL`)."""
    parts = text.strip().split(maxsplit=1)
    if len(parts) != 2:
        raise ValueError("uso: /add LINK ou /add_alvo 29,90 LINK")
    target = parse_money(parts[0])
    return target, parts[1].strip()


async def send_daily_summary(database: Database, send) -> None:
    """Send the daily summary and mark the queued events it reported as delivered."""
    text, alerts = build_daily_summary(database)
    for chunk in split_telegram_text(text):
        await send(chunk)
    database.mark_alerts_sent([alert.id for alert in alerts])


async def send_summary_if_due(database: Database, send, hour: int, minute: int) -> None:
    """Send today's summary once, as soon as possible after its time, even after downtime."""
    now = datetime.now()
    today = now.date().isoformat()
    if (now.hour, now.minute) < (hour, minute) or database.get_state(LAST_SUMMARY_KEY) == today:
        return
    await send_daily_summary(database, send)
    database.set_state(LAST_SUMMARY_KEY, today)


class Commands:
    def adapter_for_url(self, safe_url: str) -> StoreAdapter | None:
        hostname = urlparse(safe_url).hostname or ""
        adapter = next(
            (
                candidate
                for candidate in self.adapters.values()
                if host_matches(hostname, candidate.domains)
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
        if any(key.startswith("conflict_") for key in offer_identity_attributes(offer)):
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

    async def _reply(self, update, text: str, reply_markup=None) -> None:
        chunks = split_telegram_text(text)
        for index, chunk in enumerate(chunks):
            if reply_markup is not None and index == len(chunks) - 1:
                await update.effective_message.reply_text(chunk, reply_markup=reply_markup)
            else:
                await update.effective_message.reply_text(chunk)

    @command
    async def help(self, update, context) -> None:
        await update.effective_message.reply_text(
            "Cole um link no chat — monitorar esse produto\n"
            "/buscar NOME — procurar produtos\n"
            "/selecionar BUSCA RESULTADO ALVO — confirmar resultado\n"
            "/confirmar BUSCA RESULTADO ALVO — confirmar resultado ambíguo\n"
            "/add URL — salvar preço atual e avisar novos menores preços do produto\n"
            "/add_alvo PREÇO_ALVO URL — avisar quando atingir o preço efetivo desejado\n"
            "/anexar ID_PRODUTO URL — adicionar outra oferta\n"
            "/alvo ID PREÇO — alterar o alvo\n"
            "/reativar ID — retomar um produto\n"
            "/cancelar — cancelar seleção guiada\n"
            "/lista [todos] — listar produtos com botões (todos inclui pausados)\n"
            "/check — verificar agora\n"
            "/historico ID [DIAS] — histórico com gráfico (padrão 90 dias)\n"
            "/exportar [ID] — baixar o histórico em CSV\n"
            "/resumo — enviar o resumo diário agora\n"
            "/intervalo ID MINUTOS|auto — frequência de checagem do produto\n"
            "/pausar ID — pausar o monitoramento\n"
            "/remover ID — remover da lista (histórico preservado)\n"
            "/status — mostrar status\n/help — esta ajuda"
        )

    @command
    async def search(self, update, context) -> None:
        self.pending_selection.pop(update.effective_chat.id, None)
        self.pending_target.pop(update.effective_chat.id, None)
        query = " ".join(context.args).strip()
        if not query or len(query) > MAX_QUERY_LENGTH:
            await update.effective_message.reply_text(
                "Uso: /buscar nome do produto (até 200 caracteres)."
            )
            return
        if self.discovery is not None:
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
            from telegram import InlineKeyboardButton, InlineKeyboardMarkup

            buttons = [
                [InlineKeyboardButton(
                    f"{result.short_id} · {result.offer.store}",
                    callback_data=f"sel:{session.id}:{result.short_id}",
                )]
                for result in outcome.results
                if result.decision.status == "matched"
            ][:20]
            if buttons:
                await update.effective_message.reply_text(
                    "Selecione uma oferta; depois envie somente o preço-alvo:",
                    reply_markup=InlineKeyboardMarkup(buttons),
                )
            return
        await update.effective_message.reply_text("Busca não configurada.")

    async def add(self, update, context) -> None:
        await self._add(update, context, require_target=False)

    async def add_target(self, update, context) -> None:
        await self._add(update, context, require_target=True)

    @command
    async def _add(self, update, context, *, require_target: bool) -> None:
        if not require_target and len(context.args) == 1:
            target, raw_url = None, context.args[0]
        else:
            target, raw_url = parse_add_arguments(" ".join(context.args))
        safe_url = validate_url(raw_url)
        adapter = self.adapter_for_url(safe_url)
        if adapter is None:
            raise UnsafeURL("domínio não suportado")
        offer = await collect_offer(adapter, safe_url, self.fetch_timeout_seconds)
        if not self.validated_offer(offer, adapter):
            await update.effective_message.reply_text(
                "Não foi possível validar um produto nessa URL."
            )
            return
        pricing = calculate_price(
            offer.product_price, offer.shipping_price,
            offer.confirmed_discount, offer.possible_discount,
        )
        if adapter.name == "amazon" and urlparse(safe_url).hostname == "a.co":
            safe_url = validate_url(offer.url, adapter.domains)
        product, listing = self.database.register_product_listing(
            short_text(offer.title),
            target if target is not None else pricing.product_price,
            offer.store,
            safe_url,
            short_text(offer.title),
            offer.seller,
            canonical_url=self.safe_canonical_url(offer, adapter, safe_url),
            source_query=short_text(offer.title, 200),
            identity_json=json.dumps(
                {"attributes": offer_identity_attributes(offer),
                 "confirmed_title": short_text(offer.title, 200),
                 "registration_source": "direct_url"},
                ensure_ascii=False, sort_keys=True,
            ),
            uncertainty=self.offer_uncertainty(offer),
            monitoring_mode="target" if target is not None else "price_drop",
            initial_pricing=pricing,
        )
        mode = (
            f"Alvo: {format_brl(target)} (preço efetivo com frete conhecido)."
            if target is not None else
            "Avisarei a cada novo menor preço do produto desde o cadastro."
        )
        shipping = (
            (
                "Frete desconhecido; o alerta de preço-alvo aguarda um total confiável."
                if target is not None else
                "Frete desconhecido; os avisos de queda comparam apenas o preço do produto."
            ) if pricing.shipping_price is None else f"Frete: {format_brl(pricing.shipping_price)}."
        )
        await update.effective_message.reply_text(
            f"Produto cadastrado: {product.name}\n"
            f"Preço atual do produto salvo: {format_brl(pricing.product_price)}\n"
            f"{mode}\n{shipping}\nListing {listing.id} será monitorado no próximo ciclo."
        )

    @command
    async def select(self, update, context, allow_ambiguous: bool = False) -> None:
        if len(context.args) not in {3, 4}:
            await update.effective_message.reply_text(
                "Uso: /selecionar BUSCA RESULTADO PREÇO_ALVO [ID_PRODUTO_EXISTENTE]"
            )
            return
        target = parse_money(context.args[2])
        await self._select_offer(
            update, context.args[0], context.args[1], target,
            context.args[3] if len(context.args) == 4 else None,
            allow_ambiguous=allow_ambiguous,
        )

    async def _select_offer(
        self, update, session_id: str, short_id: str, target: Decimal,
        existing_product_id: str | None = None, *, allow_ambiguous: bool = False,
    ) -> None:
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
        offer = await collect_offer(adapter, safe_url, self.fetch_timeout_seconds)
        if not self.validated_offer(offer, adapter):
            await update.effective_message.reply_text(
                "A oferta mudou e não pôde ser confirmada; busque novamente."
            )
            return
        query = parse_query(session.original_query)
        decision = match_offer(query, offer)
        selected_identity = match_offer(parse_query(entry.title[:200]), offer)
        if selected_identity.status == "rejected":
            await update.effective_message.reply_text(
                f"A variante selecionada mudou ({selected_identity.reason}); "
                "faça uma nova /buscar."
            )
            return
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
        if existing_product_id is not None:
            product = self.database.get_product(product_identifier(existing_product_id))
            if not product.active or match_product(product, offer).status != "matched":
                raise ValueError("Produto inativo ou oferta incompatível com sua identidade.")
            listing = self.database.add_listing(
                product.id, offer.store, safe_url, short_text(offer.title), offer.seller,
                canonical_url=self.safe_canonical_url(offer, adapter, safe_url),
                discovery_source=entry.discovery_source,
                uncertainty=self.offer_uncertainty(offer),
            )
            await update.effective_message.reply_text(
                f"Listing {listing.id} anexado ao produto {product.id}. "
                "Use /alvo ID PREÇO para definir ou alterar o alvo."
            )
            return
        product, listing = self.database.register_product_listing(
            short_text(offer.title), target, offer.store, safe_url, short_text(offer.title),
            offer.seller, canonical_url=self.safe_canonical_url(offer, adapter, safe_url),
            discovery_source=entry.discovery_source,
            source_query=session.original_query,
            identity_json=json.dumps(
                {"attributes": decision.attributes,
                 "confirmed_title": short_text(offer.title, 200),
                 "manual_confirmation": decision.status == "ambiguous"},
                ensure_ascii=False, sort_keys=True,
            ),
            uncertainty=self.offer_uncertainty(offer),
        )
        await update.effective_message.reply_text(
            f"Resultado {entry.short_id} confirmado: produto {product.id}, "
            f"listing {listing.id}."
        )

    async def confirm(self, update, context) -> None:
        await self.select(update, context, allow_ambiguous=True)

    @command
    async def selection_price(self, update, context) -> None:
        link = LINK.search(getattr(update.effective_message, "text", "") or "")
        if link:
            self.pending_selection.pop(update.effective_chat.id, None)
            self.pending_target.pop(update.effective_chat.id, None)
            url = link.group(0).rstrip(".,;:!)")
            await self._add(update, SimpleNamespace(args=[url]), require_target=False)
            return
        product_id = self.pending_target.get(update.effective_chat.id)
        if product_id is not None:
            await self._apply_pending_target(update, product_id)
            return
        selection = self.pending_selection.get(update.effective_chat.id)
        if selection is None:
            await update.effective_message.reply_text("Cole o link de um produto ou use /buscar.")
            return
        raw = getattr(update.effective_message, "text", "") or ""
        try:
            target = parse_money(raw)
        except ValueError:
            await update.effective_message.reply_text("Envie um preço positivo, como 99,90.")
            return
        self.pending_selection.pop(update.effective_chat.id, None)
        await self._select_offer(update, *selection, target)

    async def _apply_pending_target(self, update, product_id: int) -> None:
        raw = getattr(update.effective_message, "text", "") or ""
        try:
            target = parse_money(raw)
        except ValueError:
            await update.effective_message.reply_text(
                "Envie um preço positivo, como 99,90, ou /cancelar."
            )
            return
        self.pending_target.pop(update.effective_chat.id, None)
        product = self.database.update_target(product_id, target)
        await update.effective_message.reply_text(
            f"Alvo do produto {product.id}: {format_brl(product.target_price)}. "
            "O alerta considera o total com frete conhecido."
        )

    @command
    async def cancel_selection(self, update, context) -> None:
        self.pending_selection.pop(update.effective_chat.id, None)
        self.pending_target.pop(update.effective_chat.id, None)
        await update.effective_message.reply_text("Seleção cancelada. Use /buscar para recomeçar.")

    @command
    async def attach(self, update, context) -> None:
        if len(context.args) != 2:
            await update.effective_message.reply_text("Uso: /anexar ID_DO_PRODUTO URL")
            return
        product = self.database.get_product(product_identifier(context.args[0]))
        if not product.active:
            await update.effective_message.reply_text("Esse produto está inativo.")
            return
        safe_url = validate_url(context.args[1])
        adapter = self.adapter_for_url(safe_url)
        if adapter is None:
            raise UnsafeURL("domínio não suportado")
        offer = await collect_offer(adapter, safe_url, self.fetch_timeout_seconds)
        if not self.validated_offer(offer, adapter):
            await update.effective_message.reply_text(
                "Não foi possível validar um produto nessa URL."
            )
            return
        decision = match_product(product, offer)
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

    @command
    async def list_products(self, update, context) -> None:
        products = self.database.list_products(
            active_only="todos" not in context.args, include_removed=False,
        )
        listings = self.database.list_listings(active_only=False)
        if not products:
            await update.effective_message.reply_text("Nenhum produto ativo.")
            return
        lines = []
        for product in products:
            mode = (
                "avisar novos menores preços do produto" if product.monitoring_mode == "price_drop"
                else f"alvo {format_brl(product.target_price)}"
            )
            interval = (f"; checagem a cada {product.check_interval_minutes} min"
                        if product.check_interval_minutes else "")
            lines.append(
                f"{product.id}. {short_text(product.name)} — {mode}{interval}"
                + (" [pausado]" if not product.active else "")
            )
            for listing in listings:
                if listing.product_id == product.id and listing.active:
                    lines.append(f"   listing {listing.id} [{listing.store}] {listing.url}")
        if len(products) > MAX_LIST_BUTTON_PRODUCTS:
            lines.append(f"Botões apenas para os {MAX_LIST_BUTTON_PRODUCTS} primeiros produtos.")
        lines.append("Botões: ⏸ pausar · ▶ retomar · 🎯 alvo · 📈 gráfico · 🗑 remover")
        await self._reply(update, "\n".join(lines), reply_markup=product_keyboard(products))

    @command
    async def history(self, update, context) -> None:
        if len(context.args) not in {1, 2} or not all(arg.isdigit() for arg in context.args):
            await update.effective_message.reply_text("Uso: /historico ID_DO_PRODUTO [DIAS]")
            return
        days = min(int(context.args[1]), MAX_HISTORY_DAYS) if len(context.args) == 2 else 90
        product_id = product_identifier(context.args[0])
        product = self.database.get_product(product_id)
        if product.monitoring_mode == "price_drop":
            await self._product_price_history(update, product)
            await self._send_chart(update.effective_message, product, days)
            return
        stats = self.database.product_history_stats(product_id)
        if stats.samples == 0:
            await update.effective_message.reply_text("Nenhum preço confiável no histórico.")
            await self._send_chart(update.effective_message, product, days)
            return
        lines = [
            f"Produto {product_id}: {stats.samples} observações confiáveis",
            (
                (
                    f"Atual: {format_brl(stats.current_price)}"
                    + (
                        " (menor oferta confiável; há listing sem preço atual)"
                        if stats.current_uncertain
                        else ""
                    )
                )
                if stats.current_price is not None
                else "Atual: indisponível ou não calculável"
            ),
            f"Mínimo: {format_brl(stats.lowest_price)}",
            f"Máximo: {format_brl(stats.highest_price)}",
            f"Média: {format_brl(stats.average_price)}",
            f"Menor no período consultado: {format_brl(stats.lowest_in_window)}",
        ]
        current_by_store = self.database.current_state_by_store(product_id)
        history_by_store = self.database.history_by_store(product_id)
        for store, rows in history_by_store.items():
            valid = [
                row.effective_price
                for row in rows
                if row.available and row.effective_price is not None
            ]
            if valid:
                current_price, current_uncertain = current_by_store[store]
                current = (
                    format_brl(current_price)
                    + (
                        " (menor oferta confiável; há listing sem preço atual)"
                        if current_uncertain
                        else ""
                    )
                    if current_price is not None
                    else "indisponível ou não calculável"
                )
                lines.append(f"{store}: atual {current}, mínimo {format_brl(min(valid))}")
        await self._reply(update, "\n".join(lines))
        await self._send_chart(update.effective_message, product, days)

    async def _send_chart(self, message, product, days: int) -> None:
        try:
            image = await asyncio.to_thread(render_price_chart, self.database, product, days)
        except NoChartData:
            return
        await message.reply_photo(
            io.BytesIO(image), caption=f"Produto {product.id} — últimos {days} dias",
        )

    @command
    async def export(self, update, context) -> None:
        if len(context.args) > 1:
            raise ValueError("Uso: /exportar [ID_DO_PRODUTO]")
        product_id = product_identifier(context.args[0]) if context.args else None
        content = history_csv(self.database, product_id)
        suffix = f"-produto-{product_id}" if product_id else ""
        filename = f"historico{suffix}-{datetime.now().strftime('%Y%m%d')}.csv"
        await update.effective_message.reply_document(
            io.BytesIO(content), filename=filename,
            caption="Histórico de preços (separador ;, decimais com vírgula).",
        )

    @command
    async def summary(self, update, context) -> None:
        await send_daily_summary(self.database, update.effective_message.reply_text)

    @command
    async def set_interval(self, update, context) -> None:
        if len(context.args) != 2:
            raise ValueError("Uso: /intervalo ID_PRODUTO MINUTOS (ou auto)")
        raw = context.args[1].strip().casefold()
        if raw == "auto":
            minutes = None
        elif raw.isdigit():
            minutes = int(raw)
        else:
            raise ValueError(
                f"Informe minutos entre {MIN_CHECK_INTERVAL} e {MAX_CHECK_INTERVAL}, ou auto."
            )
        product = self.database.set_check_interval(
            product_identifier(context.args[0]), minutes,
        )
        await update.effective_message.reply_text(
            f"Produto {product.id}: checagem a cada {minutes} minutos." if minutes else
            f"Produto {product.id}: intervalo automático (mais frequente perto do alvo)."
        )

    @command
    async def pause(self, update, context) -> None:
        if len(context.args) != 1:
            await update.effective_message.reply_text("Uso: /pausar ID_DO_PRODUTO")
            return
        changed = self.database.deactivate_product(product_identifier(context.args[0]))
        await update.effective_message.reply_text(
            "Produto pausado; /reativar ID retoma." if changed
            else "Produto não encontrado ou já pausado."
        )

    async def _product_price_history(self, update, product) -> None:
        lines = [
            f"Produto {product.id}: {short_text(product.name)}",
            "Histórico do preço do produto, sem frete e sem cupons condicionais.",
        ]
        for listing in self.database.list_listings(active_only=False):
            if listing.product_id != product.id:
                continue
            rows = self.database.history(listing.id)
            prices = [row.product_price for row in rows
                      if row.available and row.product_price is not None]
            if not prices:
                continue
            latest = rows[-1]
            current = (
                format_brl(latest.product_price)
                if latest.available and latest.product_price is not None and not listing.last_error
                else "indisponível ou consulta mais recente falhou"
            )
            shipping = (
                "desconhecido" if latest.shipping_price is None
                else format_brl(latest.shipping_price)
            )
            lines.append(
                f"Listing {listing.id} [{listing.store}]: {len(prices)} observações\n"
                f"Inicial: {format_brl(prices[0])}\nAtual: {current}\n"
                f"Mínimo: {format_brl(min(prices))}\nMáximo: {format_brl(max(prices))}\n"
                f"Frete da última observação: {shipping}"
            )
        if len(lines) == 2:
            lines.append("Nenhum preço do produto salvo ainda.")
        await self._reply(update, "\n".join(lines))

    @command
    async def remove(self, update, context) -> None:
        if len(context.args) != 1:
            await update.effective_message.reply_text("Uso: /remover ID_DO_PRODUTO")
            return
        product_id = product_identifier(context.args[0])
        changed = self.database.remove_product(product_id)
        message = (
            f"Produto removido da lista; histórico preservado. /reativar {product_id} desfaz."
            if changed else "Produto não encontrado ou já removido."
        )
        await update.effective_message.reply_text(message)

    @command
    async def status(self, update, context) -> None:
        products = self.database.list_products(active_only=True)
        listings = self.database.list_listings(active_only=True, confirmed_only=True)
        lines = [f"Ativos: {len(products)} produtos, {len(listings)} URLs monitoradas."]
        health = self.database.store_health()
        for item in health:
            if item.consecutive_failures:
                lines.append(
                    f"⚠️ {item.store}: {item.consecutive_failures} ciclo(s) seguidos com falha — "
                    f"{item.last_error or 'erro desconhecido'}"
                )

        def timestamp(value):
            return value.astimezone().strftime("%d/%m %H:%M") if value else "ainda não ocorreu"

        for listing in listings:
            lines.append(
                f"Listing {listing.id} [{listing.store}]: {short_text(listing.title, 100)}\n"
                f"Última tentativa: {timestamp(listing.last_checked_at)}\n"
                f"Último sucesso: {timestamp(listing.last_success_at)}"
            )
            if listing.last_error:
                lines.append(f"Erro: {listing.last_error}.")
                lines.append("Preço anterior é histórico; a oferta atual não foi confirmada.")
            else:
                snapshot = self.database.latest_snapshot(listing.id)
                if snapshot and snapshot.product_price is not None:
                    shipping = ("frete desconhecido" if snapshot.shipping_price is None else
                                f"frete {format_brl(snapshot.shipping_price)}")
                    lines.append(
                        f"Última coleta: produto {format_brl(snapshot.product_price)}; {shipping}."
                    )
        await self._reply(update, "\n".join(lines))

    @command
    async def set_target(self, update, context) -> None:
        if len(context.args) != 2:
            raise ValueError("Uso: /alvo ID_PRODUTO PREÇO")
        product = self.database.update_target(
            product_identifier(context.args[0]), parse_money(context.args[1]),
        )
        await update.effective_message.reply_text(
            f"Alvo do produto {product.id}: {format_brl(product.target_price)}. "
            "O alerta considera o total com frete conhecido."
        )

    @command
    async def reactivate(self, update, context) -> None:
        if len(context.args) != 1:
            raise ValueError("Uso: /reativar ID_PRODUTO")
        product = self.database.reactivate_product(product_identifier(context.args[0]))
        await update.effective_message.reply_text(
            f"Produto {product.id} reativado; histórico preservado."
        )

    @command
    async def check(self, update, context) -> None:
        monitor = context.application.bot_data.get("monitor")
        if monitor is None:
            await update.effective_message.reply_text("Monitor indisponível.")
            return
        count = await monitor.check_once(
            lambda message: context.bot.send_message(self.allowed_chat_id, message)
        )
        summary = getattr(monitor, "last_run", {})
        await update.effective_message.reply_text(
            f"Verificação concluída; {summary.get('checked', 0)} consultas válidas; "
            f"{summary.get('failed', 0)} falhas; "
            f"{summary.get('unknown_shipping', 0)} com frete desconhecido; "
            f"{count} alerta(s) enviado(s); "
            f"{summary.get('delivery_failed', 0)} falhas de envio."
        )
