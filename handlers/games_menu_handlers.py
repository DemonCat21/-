# games_menu_handlers.py
# -*- coding: utf-8 -*-
"""Єдина точка входу в ігри: /newgame → вибір гри.

Правила:
- /newgame ніколи не стартує гру одразу
- тільки показує inline-меню з вибором гри
- після вибору запускається той самий флоу, що був раніше для кожної гри
"""

import logging
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.constants import ParseMode
from telegram.ext import (
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

logger = logging.getLogger(__name__)

CB_PREFIX = "choose_game:"


def _games_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("😼 Мемчики та котики", callback_data=f"{CB_PREFIX}mems"),
            ],
            [
                InlineKeyboardButton("❌⭕ Хрестики-Нулики", callback_data=f"{CB_PREFIX}ttt"),
            ],
            [
                InlineKeyboardButton("Гра з ботом 🤖", callback_data=f"{CB_PREFIX}ttt_bot"),
            ],
        ]
    )


async def newgame_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Показує меню вибору гри."""
    chat = update.effective_chat
    if not chat:
        return

    text = "🎮 <b>Обери гру:</b>"
    if update.message:
        await update.message.reply_text(text, reply_markup=_games_keyboard(), parse_mode=ParseMode.HTML)
        return

    # fallback (на випадок, якщо хтось викликає через інші апдейти)
    await context.bot.send_message(chat.id, text, reply_markup=_games_keyboard(), parse_mode=ParseMode.HTML)


async def choose_game_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.message:
        return

    await query.answer()

    data = query.data or ""
    game = data.replace(CB_PREFIX, "", 1).strip()

    # прибираємо меню після вибору
    try:
        await query.message.delete()
    except Exception:
        pass

    try:
        if game == "mems":
            # Мемчики: старт тим самим флоу, просто з меню
            from bot.games import mems_raw as mems
            await mems.cmd_newgame(update, context)
            return

        if game == "ttt":
            # Хрестики: відкриваємо лобі (новий UX)
            from bot.games.tic_tac_toe_game import ttt_open_lobby
            await ttt_open_lobby(update, context)
            return

        if game == "ttt_bot":
            # Хрестики з ботом: той самий флоу, що /playwithbot
            from bot.games.tic_tac_toe_game import play_with_bot_command
            await play_with_bot_command(update, context)
            return

    except Exception:
        logger.exception("Failed to start game from /newgame menu")
        await context.bot.send_message(query.message.chat.id, "Щось пішло не так 😼")
        return

    await context.bot.send_message(query.message.chat.id, "Не зрозуміла вибір 😼")


def register_games_menu_handlers(application) -> None:
    """Реєструє /newgame та меню вибору гри."""
    application.add_handler(CommandHandler(["newgame"], newgame_entry))

    # текстові аліаси (не стартують гру напряму — тільки меню)
    application.add_handler(MessageHandler(filters.Regex(r"(?i)^\s*новагра\b"), newgame_entry))
    application.add_handler(MessageHandler(filters.Regex(r"(?i)^\s*нова\s+гра\b"), newgame_entry))

    application.add_handler(CallbackQueryHandler(choose_game_callback, pattern=r"^choose_game:"))
