"""Delivery of automatic messages to the authorized chat."""

from __future__ import annotations

from datetime import datetime, time

from .buttons import alert_keyboard


def is_quiet(moment: time, quiet_hours: tuple[time, time] | None) -> bool:
    if quiet_hours is None:
        return False
    start, end = quiet_hours
    return start <= moment < end if start <= end else moment >= start or moment < end


def make_sender(bot, chat_id: int, quiet_hours: tuple[time, time] | None):
    """Build the send function; during quiet hours messages arrive without a sound."""

    async def send(text: str, actions=None) -> None:
        from telegram.error import BadRequest

        silent = is_quiet(datetime.now().time(), quiet_hours)
        markup = alert_keyboard(actions.url, actions.product_id) if actions else None
        try:
            await bot.send_message(
                chat_id, text, reply_markup=markup, disable_notification=silent,
            )
        except BadRequest:
            if markup is None:
                raise
            # Telegram refuses some store URLs as buttons; the alert still has to arrive.
            await bot.send_message(chat_id, text, disable_notification=silent)

    return send
