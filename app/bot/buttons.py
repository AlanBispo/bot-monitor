"""Inline keyboard callbacks."""

from __future__ import annotations

import sqlite3

from ..security import is_authorized_chat
from .formatting import DATABASE_FAILURE_MESSAGE, product_identifier, short_text

MAX_LIST_BUTTON_PRODUCTS = 25


def product_keyboard(products):
    """Inline actions per product: pause/resume, target, chart and remove."""
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    rows = []
    for product in products[:MAX_LIST_BUTTON_PRODUCTS]:
        toggle = (InlineKeyboardButton(f"⏸ {product.id}", callback_data=f"prd:pause:{product.id}")
                  if product.active else
                  InlineKeyboardButton(f"▶ {product.id}", callback_data=f"prd:resume:{product.id}"))
        rows.append([
            toggle,
            InlineKeyboardButton(f"🎯 {product.id}", callback_data=f"prd:target:{product.id}"),
            InlineKeyboardButton(f"📈 {product.id}", callback_data=f"prd:chart:{product.id}"),
            InlineKeyboardButton(f"🗑 {product.id}", callback_data=f"prd:remove:{product.id}"),
        ])
    return InlineKeyboardMarkup(rows) if rows else None


class Buttons:
    def _authorized_callback(self, query) -> bool:
        message = query.message if query else None
        return (
            message is not None
            and getattr(getattr(message, "chat", None), "type", None) == "private"
            and is_authorized_chat(getattr(message, "chat_id", None), self.allowed_chat_id)
        )

    async def selection_button(self, update, context) -> None:
        query = update.callback_query
        if not self._authorized_callback(query):
            if query:
                await query.answer("Acesso não autorizado.", show_alert=True)
            return
        message = query.message
        parts = query.data.split(":", 2) if isinstance(query.data, str) else []
        if len(parts) != 3 or not parts[1] or not parts[2]:
            await query.answer("Seleção inválida.", show_alert=True)
            return
        _, session_id, short_id = parts
        await query.answer()
        self.pending_selection.pop(self.allowed_chat_id, None)
        self.pending_target.pop(self.allowed_chat_id, None)
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

    async def product_button(self, update, context) -> None:
        query = update.callback_query
        if not self._authorized_callback(query):
            if query:
                await query.answer("Acesso não autorizado.", show_alert=True)
            return
        parts = query.data.split(":") if isinstance(query.data, str) else []
        if len(parts) != 3 or parts[1] not in {
            "pause", "resume", "target", "chart", "remove", "confirm_remove", "cancel",
        }:
            await query.answer("Ação inválida.", show_alert=True)
            return
        await query.answer()
        message = query.message
        action = parts[1]
        try:
            product = self.database.get_product(product_identifier(parts[2]))
            if product.removed and action != "cancel":
                raise KeyError("produto removido")
            if action == "pause":
                self.database.deactivate_product(product.id)
                text = f"⏸ Produto {product.id} pausado. Use ▶ em /lista todos para retomar."
            elif action == "resume":
                self.database.reactivate_product(product.id)
                text = f"▶ Produto {product.id} retomado; histórico preservado."
            elif action == "target":
                self.pending_selection.pop(self.allowed_chat_id, None)
                self.pending_target[self.allowed_chat_id] = product.id
                text = (f"Envie o novo preço-alvo do produto {product.id} "
                        f"({short_text(product.name, 120)}), ex.: 99,90 — ou /cancelar.")
            elif action == "chart":
                await self._send_chart(message, product, 90)
                return
            elif action == "remove":
                from telegram import InlineKeyboardButton, InlineKeyboardMarkup

                await message.reply_text(
                    f"Remover o produto {product.id} ({short_text(product.name, 120)}) da lista? "
                    "O histórico é preservado para /exportar.",
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton(
                            "Sim, remover", callback_data=f"prd:confirm_remove:{product.id}"
                        ),
                        InlineKeyboardButton("Cancelar", callback_data=f"prd:cancel:{product.id}"),
                    ]]),
                )
                return
            elif action == "confirm_remove":
                self.database.remove_product(product.id)
                text = f"🗑 Produto {product.id} removido. /reativar {product.id} desfaz."
            else:
                text = "Remoção cancelada."
        except (KeyError, ValueError):
            text = "Produto não encontrado."
        except sqlite3.Error:
            text = DATABASE_FAILURE_MESSAGE
        await message.reply_text(text)
