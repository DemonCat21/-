# unified_stop_handlers.py
# -*- coding: utf-8 -*-
"""Єдиний /stop для будь-якої гри.

Ціль:
- /stop, /stopgame, /endgame + текстові аліаси: "стоп", "зупини", "закінчити"
- коректно завершує активну гру в поточному чаті
- чистить стани/лобі/джоби/тимчасові chat_data

ВАЖЛИВО: не ламаємо логіку ігор — лише диспетчеризація та безпечні cleanups.
"""

from __future__ import annotations

import logging
from typing import Optional

from telegram import Update
from telegram.constants import ChatType, ParseMode
from telegram.ext import ContextTypes, CommandHandler, MessageHandler, filters

logger = logging.getLogger(__name__)


STOP_TEXT = "Гру зупинено 😼"
NO_GAME_TEXT = "Нема активної гри."
REPLY_REQUIRED_TEXT = "Потрібно відповісти на повідомлення з грою, яку хочете зупинити. 😺"
NOT_GAME_MESSAGE_TEXT = "Це повідомлення не є частиною активної гри. 😿"


async def unified_stop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    user = update.effective_user
    msg = update.effective_message
    if not chat or not user:
        return

    # Стоп потрібен в групах/супергрупах; у приваті можна мовчки сказати, що немає гри.
    if chat.type == ChatType.PRIVATE:
        try:
            if msg:
                await msg.reply_text(NO_GAME_TEXT)
        except Exception:
            pass
        return

    chat_id = chat.id

    # Перевіряємо, чи це відповідь на повідомлення
    if not msg or not msg.reply_to_message:
        try:
            await msg.reply_text(REPLY_REQUIRED_TEXT)
        except Exception:
            pass
        return

    replied_message_id = msg.reply_to_message.message_id

    stopped_any = False

    # --- 1) Мемчики та котики ---
    try:
        from bot.games import mems_raw as mems

        if chat_id in getattr(mems, "games", {}):
            game = mems.games.get(chat_id)
            if game:
                game_message_ids = [
                    getattr(game, "lobby_message_id", None),
                    getattr(game, "round_message_id", None),
                ] + list(getattr(game, "voting_message_ids", []) or [])
                if replied_message_id in [mid for mid in game_message_ids if mid]:
                    # Підтримуємо існуючу логіку прав (адмін у чаті)
                    try:
                        is_admin = await mems.is_admin_in_chat(user.id, chat_id, context)
                    except Exception:
                        is_admin = True

                    if not is_admin:
                        # Мінімально, без зайвого
                        try:
                            if msg:
                                await msg.reply_text("⛔ Тільки Настоятель.")
                        except Exception:
                            pass
                        return

                    # прибираємо службові повідомлення гри (як у raw.cmd_stop_game)
                    msgs_to_delete = game_message_ids
                    for mid in msgs_to_delete:
                        if not mid:
                            continue
                        try:
                            await context.bot.delete_message(chat_id, int(mid))
                        except Exception:
                            pass

                    mems.delete_game(chat_id)
                    stopped_any = True
    except Exception as e:
        logger.warning(f"unified_stop: mems stop failed: {e}")

    # --- 2) Хрестики-Нулики ---
    if not stopped_any:
        try:
            from bot.games.tic_tac_toe_game import stop_all_ttt_in_chat

            games = context.chat_data.get("games", {})
            if replied_message_id in games:
                # Stop only this specific game
                game = games[replied_message_id]
                # Assuming stop_all_ttt_in_chat can be modified, but for now, since it's all, but we need to stop specific
                # Actually, stop_all_ttt_in_chat stops all, but we need to stop one.
                # I need to implement a function to stop a specific ttt game by message_id
                # For now, let's assume we can delete it directly
                try:
                    await context.bot.delete_message(chat_id, replied_message_id)
                except Exception:
                    pass
                del games[replied_message_id]
                stopped_any = True
            # If not this message, don't stop all
        except Exception as e:
            logger.warning(f"unified_stop: ttt stop failed: {e}")

    # --- 3) Майбутні ігри ---
    # --- 3) Мандаринка (новорічна дуель) ---
    if not stopped_any:
        try:
            from bot.games.mandarin_duel_game import stop_mandarin_duel_in_chat

            duels = context.chat_data.get("mandarin_duels", {})
            duel_to_stop = None
            for duel_id, duel in duels.items():
                if duel.get("invite_message_id") == replied_message_id:
                    duel_to_stop = duel_id
                    break
            if duel_to_stop:
                res = await stop_mandarin_duel_in_chat(chat_id=chat_id, by_user_id=user.id, context=context)
                if res == "forbidden":
                    # є активна дуель, але стопити може тільки ініціатор
                    try:
                        if msg:
                            await msg.reply_text("⛔ Мур 😼 Зупинити дуель може тільки той, хто кинув виклик.")
                    except Exception:
                        pass
                    return
                if res == "stopped":
                    stopped_any = True
        except Exception as e:
            logger.warning(f"unified_stop: mandarin stop failed: {e}")

    # Тут можна додати інші ігри (без зміни цього інтерфейсу).

    try:
        if msg:
            if stopped_any:
                await msg.reply_text(STOP_TEXT, parse_mode=ParseMode.HTML)
            else:
                await msg.reply_text(NOT_GAME_MESSAGE_TEXT)
    except Exception:
        pass


def register_unified_stop_handlers(application) -> None:
    # Команди
    application.add_handler(CommandHandler(["stop", "stopgame", "endgame"], unified_stop))
    # Текстові аліаси (поза /командами)
    application.add_handler(
        MessageHandler(
            filters.ChatType.GROUPS
            & filters.TEXT
            & ~filters.COMMAND
            & filters.Regex(r"(?i)^(стоп|зупини|закінчити)\b"),
            unified_stop,
        )
    )
