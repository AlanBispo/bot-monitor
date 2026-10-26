"""Telegram command handlers. Imports Telegram lazily so domain tests need no bot token."""

from __future__ import annotations

import json
import sqlite3
from decimal import Decimal
from urllib.parse import urlparse

from .database import Database, DatabaseError
from .discovery import DiscoveryOrchestrator, SearchOutcome
from .matching import match_offer, match_product, offer_identity_attributes
from .pricing import calculate_price, parse_money
from .query import InvalidQuery, parse_query
from .security import UnsafeURL, is_authorized_chat, validate_url
from .stores.base import StoreAdapter, StoreError, collect_offer

MAX_QUERY_LENGTH = 200
MAX_EXTERNAL_TITLE_LENGTH = 300
TELEGRAM_TEXT_LIMIT = 4096
MAX_DISPLAY_URL_LENGTH = 1800
DATABASE_FAILURE_MESSAGE = "Falha no banco de dados; tente novamente."


def product_identifier(raw: str) -> int:
    try:
        value = int(raw)
    except ValueError:
        raise ValueError("ID de produto inválido") from None
    if not 0 < value <= 2**63 - 1:
        raise ValueError("ID de produto inválido")
    return value


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
    """Parse `/add_alvo TARGET URL` (also accepted as legacy `/add TARGET URL`)."""
    parts = text.strip().split(maxsplit=1)
    if len(parts) != 2:
        raise ValueError("uso: /add LINK ou /add_alvo 29,90 LINK")
    target = parse_money(parts[0])
    return target, parts[1].strip()


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
            price = (f"produto R$ {result.pricing.product_price:.2f}; "
                     "frete desconhecido; total não calculável; não é o menor garantido")
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
        fetch_timeout_seconds: float = 30,
    ):
        self.database = database
        self.adapters = adapters
        self.allowed_chat_id = allowed_chat_id
        self.discovery = discovery
        self.fetch_timeout_seconds = fetch_timeout_seconds
        self.pending_selection: dict[int, tuple[str, str]] = {}

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
            "/add URL — salvar preço atual e avisar novos menores preços do produto\n"
            "/add_alvo PREÇO_ALVO URL — avisar quando atingir o preço efetivo desejado\n"
            "/anexar ID_PRODUTO URL — adicionar outra oferta\n"
            "/alvo ID PREÇO — alterar o alvo\n"
            "/reativar ID — retomar um produto\n"
            "/cancelar — cancelar seleção guiada\n"
            "/lista [todos] — listar produtos, inclusive inativos\n"
            "/check — verificar agora\n"
            "/historico ID — mostrar histórico\n"
            "/remover ID — desativar produto\n"
            "/status — mostrar status\n/help — esta ajuda"
        )

    async def search(self, update, context) -> None:
        if await self._reject_if_unauthorized(update):
            return
        self.pending_selection.pop(update.effective_chat.id, None)
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
            except (InvalidQuery, DatabaseError, ValueError) as exc:
                await update.effective_message.reply_text(str(exc))
                return
            except sqlite3.Error:
                await update.effective_message.reply_text(DATABASE_FAILURE_MESSAGE)
                return
        await update.effective_message.reply_text("Busca não configurada.")

    async def add(self, update, context) -> None:
        await self._add(update, context, require_target=False)

    async def add_target(self, update, context) -> None:
        await self._add(update, context, require_target=True)

    async def _add(self, update, context, *, require_target: bool) -> None:
        if await self._reject_if_unauthorized(update):
            return
        try:
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
                f"Alvo: R$ {target:.2f} (preço efetivo com frete conhecido)."
                if target is not None else
                "Avisarei a cada novo menor preço do produto desde o cadastro."
            )
            shipping = (
                (
                    "Frete desconhecido; o alerta de preço-alvo aguarda um total confiável."
                    if target is not None else
                    "Frete desconhecido; os avisos de queda comparam apenas o preço do produto."
                ) if pricing.shipping_price is None else f"Frete: R$ {pricing.shipping_price:.2f}."
            )
            await update.effective_message.reply_text(
                f"Produto cadastrado: {product.name}\n"
                f"Preço atual do produto salvo: R$ {pricing.product_price:.2f}\n"
                f"{mode}\n{shipping}\nListing {listing.id} será monitorado no próximo ciclo."
            )
        except (ValueError, StoreError, DatabaseError) as exc:
            await update.effective_message.reply_text(str(exc))
        except sqlite3.Error:
            await update.effective_message.reply_text(DATABASE_FAILURE_MESSAGE)

    async def select(self, update, context, allow_ambiguous: bool = False) -> None:
        if await self._reject_if_unauthorized(update):
            return
        if len(context.args) not in {3, 4}:
            await update.effective_message.reply_text(
                "Uso: /selecionar BUSCA RESULTADO PREÇO_ALVO [ID_PRODUTO_EXISTENTE]"
            )
            return
        try:
            target = parse_money(context.args[2])
        except ValueError as exc:
            await update.effective_message.reply_text(str(exc))
            return
        await self._select_offer(
            update, context.args[0], context.args[1], target,
            context.args[3] if len(context.args) == 4 else None,
            allow_ambiguous=allow_ambiguous,
        )

    async def _select_offer(
        self, update, session_id: str, short_id: str, target: Decimal,
        existing_product_id: str | None = None, *, allow_ambiguous: bool = False,
    ) -> None:
        try:
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
        self.pending_selection.pop(self.allowed_chat_id, None)
        try:
            entry = self.database.get_search_entry(
                session_id, short_id, chat_id=self.allowed_chat_id,
            )
            if entry.match_status != "matched":
                raise ValueError("Use /confirmar para uma escolha ambígua.")
        except (KeyError, ValueError) as exc:
            await message.reply_text(f"{exc}. Faça uma nova /buscar se a sessão expirou.")
            return
        except sqlite3.Error:
            await message.reply_text(DATABASE_FAILURE_MESSAGE)
            return
        self.pending_selection[self.allowed_chat_id] = (session_id, short_id)
        await message.reply_text(
            f"Oferta {short_id}: {short_text(entry.title, 180)}. "
            "Envie somente o preço-alvo (ex.: 99,90), ou /cancelar.\n"
            "Para anexar a um produto existente, envie:\n"
            f"/selecionar {session_id} {short_id} PREÇO_ALVO ID_PRODUTO"
        )

    async def selection_price(self, update, context) -> None:
        if await self._reject_if_unauthorized(update):
            return
        selection = self.pending_selection.get(update.effective_chat.id)
        if selection is None:
            await update.effective_message.reply_text("Use /buscar ou /help para começar.")
            return
        raw = getattr(update.effective_message, "text", "") or ""
        try:
            target = parse_money(raw)
        except ValueError:
            await update.effective_message.reply_text("Envie um preço positivo, como 99,90.")
            return
        await self._select_offer(update, *selection, target)
        self.pending_selection.pop(update.effective_chat.id, None)

    async def cancel_selection(self, update, context) -> None:
        if await self._reject_if_unauthorized(update):
            return
        self.pending_selection.pop(update.effective_chat.id, None)
        await update.effective_message.reply_text("Seleção cancelada. Use /buscar para recomeçar.")

    async def attach(self, update, context) -> None:
        if await self._reject_if_unauthorized(update):
            return
        if len(context.args) != 2 or not context.args[0].isdigit():
            await update.effective_message.reply_text("Uso: /anexar ID_DO_PRODUTO URL")
            return
        try:
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
        except (KeyError, ValueError, StoreError, DatabaseError) as exc:
            await update.effective_message.reply_text(str(exc))
        except sqlite3.Error:
            await update.effective_message.reply_text(DATABASE_FAILURE_MESSAGE)

    async def list_products(self, update, context) -> None:
        if await self._reject_if_unauthorized(update):
            return
        try:
            products = self.database.list_products(active_only="todos" not in context.args)
            listings = self.database.list_listings(active_only=False)
        except sqlite3.Error:
            await update.effective_message.reply_text(DATABASE_FAILURE_MESSAGE)
            return
        if not products:
            await update.effective_message.reply_text("Nenhum produto ativo.")
            return
        lines = []
        for product in products:
            mode = (
                "avisar novos menores preços do produto" if product.monitoring_mode == "price_drop"
                else f"alvo R$ {product.target_price:.2f}"
            )
            lines.append(
                f"{product.id}. {short_text(product.name)} — {mode}"
                + (" [inativo]" if not product.active else "")
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
        try:
            product_id = product_identifier(context.args[0])
            product = self.database.get_product(product_id)
            if product.monitoring_mode == "price_drop":
                await self._product_price_history(update, product)
                return
            stats = self.database.product_history_stats(product_id)
        except ValueError as exc:
            await update.effective_message.reply_text(str(exc))
            return
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
                f"R$ {latest.product_price:.2f}"
                if latest.available and latest.product_price is not None and not listing.last_error
                else "indisponível ou consulta mais recente falhou"
            )
            shipping = (
                "desconhecido" if latest.shipping_price is None
                else f"R$ {latest.shipping_price:.2f}"
            )
            lines.append(
                f"Listing {listing.id} [{listing.store}]: {len(prices)} observações\n"
                f"Inicial: R$ {prices[0]:.2f}\nAtual: {current}\n"
                f"Mínimo: R$ {min(prices):.2f}\nMáximo: R$ {max(prices):.2f}\n"
                f"Frete da última observação: {shipping}"
            )
        if len(lines) == 2:
            lines.append("Nenhum preço do produto salvo ainda.")
        await self._reply(update, "\n".join(lines))

    async def remove(self, update, context) -> None:
        if await self._reject_if_unauthorized(update):
            return
        if len(context.args) != 1 or not context.args[0].isdigit():
            await update.effective_message.reply_text("Uso: /remover ID_DO_PRODUTO")
            return
        try:
            changed = self.database.deactivate_product(product_identifier(context.args[0]))
        except ValueError as exc:
            await update.effective_message.reply_text(str(exc))
            return
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
        lines = [f"Ativos: {len(products)} produtos, {len(listings)} URLs monitoradas."]

        def timestamp(value):
            return value.astimezone().strftime("%d/%m %H:%M") if value else "ainda não ocorreu"

        for listing in listings:
            lines.append(
                f"Listing {listing.id} [{listing.store}]: {short_text(listing.title, 100)}\n"
                f"Última tentativa: {timestamp(listing.last_checked_at)}\n"
                f"Último sucesso: {timestamp(listing.last_success_at)}"
            )
            if listing.uncertainty == "duplicate_conflict":
                lines.append("Oferta repetida em produtos diferentes; confira /lista todos.")
            if listing.last_error:
                lines.append(f"Erro: {listing.last_error}.")
                lines.append("Preço anterior é histórico; a oferta atual não foi confirmada.")
            else:
                try:
                    snapshot = self.database.latest_snapshot(listing.id)
                except sqlite3.Error:
                    await update.effective_message.reply_text(DATABASE_FAILURE_MESSAGE)
                    return
                if snapshot and snapshot.product_price is not None:
                    shipping = ("frete desconhecido" if snapshot.shipping_price is None else
                                f"frete R$ {snapshot.shipping_price:.2f}")
                    lines.append(
                        f"Última coleta: produto R$ {snapshot.product_price:.2f}; {shipping}."
                    )
        await self._reply(update, "\n".join(lines))

    async def set_target(self, update, context) -> None:
        if await self._reject_if_unauthorized(update):
            return
        try:
            if len(context.args) != 2:
                raise ValueError("Uso: /alvo ID_PRODUTO PREÇO")
            product = self.database.update_target(
                product_identifier(context.args[0]), parse_money(context.args[1]),
            )
            await update.effective_message.reply_text(
                f"Alvo do produto {product.id}: R$ {product.target_price:.2f}. "
                "O alerta considera o total com frete conhecido."
            )
        except (KeyError, ValueError) as exc:
            await update.effective_message.reply_text(str(exc))
        except sqlite3.Error:
            await update.effective_message.reply_text(DATABASE_FAILURE_MESSAGE)

    async def reactivate(self, update, context) -> None:
        if await self._reject_if_unauthorized(update):
            return
        try:
            if len(context.args) != 1:
                raise ValueError("Uso: /reativar ID_PRODUTO")
            product = self.database.reactivate_product(product_identifier(context.args[0]))
            await update.effective_message.reply_text(
                f"Produto {product.id} reativado; histórico preservado."
            )
        except (KeyError, ValueError) as exc:
            await update.effective_message.reply_text(str(exc))
        except sqlite3.Error:
            await update.effective_message.reply_text(DATABASE_FAILURE_MESSAGE)

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
        summary = getattr(monitor, "last_run", {})
        await update.effective_message.reply_text(
            f"Verificação concluída; {summary.get('checked', 0)} consultas válidas; "
            f"{summary.get('failed', 0)} falhas; "
            f"{summary.get('unknown_shipping', 0)} com frete desconhecido; "
            f"{count} alerta(s) enviado(s); "
            f"{summary.get('delivery_failed', 0)} falhas de envio."
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
    from telegram.ext import (
        ApplicationBuilder,
        CallbackQueryHandler,
        CommandHandler,
        MessageHandler,
        filters,
    )

    bot = TelegramBot(database, adapters, allowed_chat_id, discovery,
                      getattr(monitor, "fetch_timeout_seconds", 30))
    application = ApplicationBuilder().token(token).build()
    application.bot_data["monitor"] = monitor
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
        "alvo": bot.set_target,
        "reativar": bot.reactivate,
        "cancelar": bot.cancel_selection,
        "status": bot.status,
        "check": bot.check,
    }.items():
        application.add_handler(CommandHandler(command, callback))
    application.add_handler(CallbackQueryHandler(bot.selection_button, pattern=r"^sel:"))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, bot.selection_price))
    return application
