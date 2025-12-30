# system_handlers.py
# -*- coding: utf-8 -*-
"""
Системні обробники:
- /cancel: універсальне скасування діалогів
- unknown command: ввічливий фолбек на невідомі команди
- safety: безпечні відповіді на помилки вводу, щоб юзер не зависав

Це НЕ нові фічі — це UX-запобіжники.
"""

import logging
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes, CommandHandler, MessageHandler, filters

logger = logging.getLogger(__name__)


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Універсально скасовує поточний крок та повертає в меню."""
    # Чистимо тимчасові ключі, не чіпаючи persistence в цілому
    for k in ("awaiting_admin_input", "awaiting_ai_prompt", "state", "tmp", "pending"):
        context.user_data.pop(k, None)

    text = "Скасовано. Повертаю в меню. 🐾"
    if update.effective_message:
        await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML)

    # Повертаємо в головне меню, якщо воно є
    try:
        from bot.handlers.start_help_handlers import send_main_menu  # локальний імпорт (уникаємо циклів)
        await send_main_menu(update, context, is_callback=False)
    except Exception:
        # Якщо меню недоступне з будь-яких причин — не валимо бота
        logger.debug("Не вдалося показати меню після /cancel", exc_info=True)

    # Для ConversationHandler це буде трактуватись як END, якщо хендлер використають як fallback
    return -1


async def unknown_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Раніше відповідав на невідомі команди. Тепер — тиша (за вимогою UX)."""
    return


async def delete_message_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    job_data = getattr(context.job, "data", {}) if context.job else {}
    chat_id = job_data.get("chat_id")
    message_id = job_data.get("message_id")
    if not chat_id or not message_id:
        return
    try:
        bot = getattr(context, "bot", None) or context.application.bot
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception:
        pass


async def auto_delete_command_invocation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    chat = update.effective_chat
    if not message or not chat:
        return
    from bot.core.database import get_chat_settings

    settings = await get_chat_settings(chat.id)
    if settings.get("auto_delete_actions", 0) != 1:
        return
    context.job_queue.run_once(
        delete_message_job,
        420,
        data={"chat_id": chat.id, "message_id": message.message_id},
        name=f"delete_command_{chat.id}_{message.message_id}",
    )


def register_system_handlers(application) -> None:
    """Реєстрація системних обробників."""
    application.add_handler(CommandHandler("cancel", cancel_command), group=0)
    # Фолбек на невідомі команди — ВИМКНЕНО. Невідома команда = тиша.
    application.add_handler(
        MessageHandler(filters.COMMAND, auto_delete_command_invocation),
        group=99,
    )
