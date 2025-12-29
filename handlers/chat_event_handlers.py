# chat_event_handlers.py
# -*- coding: utf-8 -*-
"""
Цей модуль, немов тихе кошеня 🐾, спостерігає за чатом
та обробляє пасивні події:

- Привітання нових душ (учасників)
- Фільтрація єресі (авто-модерація)
- Автоматичні мур-реакції
"""

import logging
import html
import re
import random
import asyncio # (НОВЕ) Додано для затримок

from telegram import Update
from telegram.ext import Application, ContextTypes, MessageHandler, filters, ChatMemberHandler
from telegram.constants import ParseMode
from telegram.error import Forbidden, BadRequest
# (ВИПРАВЛЕНО) Чистіші імпорти
from bot.core.database import get_chat_settings, get_filtered_words, add_user_warn, get_user_warns, reset_user_warns
from bot.handlers.chat_admin_handlers import is_chat_module_enabled, _check_admin_rights

logger = logging.getLogger(__name__)


async def handle_new_members(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Надсилає привітальне повідомлення новим учасникам.
    """
    chat = update.effective_chat
    if not chat or not update.message or not update.message.new_chat_members:
        return

    # Отримуємо налаштування чату
    settings = await get_chat_settings(chat.id)
    welcome_message = settings.get('welcome_message')

    if not welcome_message:
        logger.debug(f"Привітання для чату {chat.id} не встановлено (тиша в келії).")
        return
        
    for member in update.message.new_chat_members:
        if member.is_bot:
            continue
            
        logger.info(f"Нова душа {member.id} в чаті {chat.id}. Вітаємо. 🕊️")
        
        # Форматуємо повідомлення
        user_mention = f"<a href='tg://user?id={member.id}'>{html.escape(member.first_name)}</a>"
        chat_title = html.escape(chat.title or "цей святий чат")
        
        try:
            # (ВИПРАВЛЕНО) Додано відсутні теги
            formatted_message = welcome_message.format(
                user=user_mention,
                chat=chat_title,
                username=f"@{member.username}" if member.username else user_mention,
                user_id=member.id,
                first_name=html.escape(member.first_name)
            )
        except KeyError as e:
            logger.warning(f"У привітанні {chat.id} відсутній тег {e}. Використовую базове.")
            # (ВИПРАВЛЕНО) Запасний варіант, якщо адмін помилився в тегах
            formatted_message = f"Вітаємо в чаті, {user_mention}! 🌿"

        
        try:
            await update.message.reply_html(formatted_message, disable_web_page_preview=True)
        except Exception as e:
            logger.error(f"Не вдалося надіслати привітання в чат {chat.id}: {e}")

async def word_filter_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Перевіряє повідомлення на єресь (заборонені слова).
    (ПОВНІСТЮ ПЕРЕРОБЛЕНО ЛОГІКУ)
    """
    if not update.message or not update.message.text or not update.effective_chat:
        return
        
    chat = update.effective_chat
    user = update.effective_user

    # Модуль вимкнено?
    if not await is_chat_module_enabled(chat, "word_filter"):
        return
        
    # Настоятелі (адміни) мають імунітет
    if await _check_admin_rights(context.bot, user.id, chat.id, needs_ban_right=False):
        return

    # Отримуємо список слів
    filtered_words = await get_filtered_words(chat.id)
    if not filtered_words:
        return
        
    message_text = update.message.text.lower()
    
    for word in filtered_words:
        # Використовуємо \b для пошуку цілих слів
        if re.search(rf'\b{re.escape(word)}\b', message_text, re.IGNORECASE):
            logger.info(f"Знайдено єресь '{word}' від {user.id} в чаті {chat.id}.")
            
            try:
                # 1. Видаляємо єресь
                await update.message.delete()
            except (Forbidden, BadRequest) as e:
                logger.warning(f"Не вдалося видалити повідомлення (немає прав?): {e}")
                # Якщо не можемо видалити, не можемо й банити. Просто виходимо.
                return

            user_mention = f"<a href='tg://user?id={user.id}'>{html.escape(user.first_name)}</a>"
            
            # 2. Надсилаємо тимчасове повідомлення про покаяння
            try:
                warn_msg = await context.bot.send_message(
                    chat.id,
                    f"✝️ {user_mention}, покайся. Не поширюй єресь. 🌿",
                    parse_mode=ParseMode.HTML
                )
            except Exception as e:
                logger.warning(f"Не вдалося надіслати повідомлення про єресь: {e}")
                return # Не можемо продовжити

            # 3. Додаємо варн (чиста логіка, без фейкових апдейтів)
            new_warns = await add_user_warn(chat.id, user.id)
            settings = await get_chat_settings(chat.id)
            max_warns = settings.get('max_warns', 3)
            
            # 4. Вирішуємо долю
            if new_warns >= max_warns:
                logger.info(f"Користувач {user.id} досяг ліміту ({new_warns}/{max_warns}) в чаті {chat.id}. Покута (бан).")
                try:
                    await context.bot.ban_chat_member(chat.id, user.id)
                    await reset_user_warns(chat.id, user.id) # Очищуємо
                    await warn_msg.edit_text(
                         f"✝️ {user_mention} відправляється на покуту (<b>бан</b>) "
                         f"за досягнення ліміту ({new_warns}/{max_warns}) автоматичних попереджень.",
                         parse_mode=ParseMode.HTML
                    )
                except Exception as e:
                    logger.error(f"Не вдалося забанити {user.id}: {e}")
                    await warn_msg.edit_text(f"Хотіла забанити {user_mention}, але не маю прав... 😿", parse_mode=ParseMode.HTML)
            else:
                # Оновлюємо тимчасове повідомлення
                await warn_msg.edit_text(
                     f"✝️ {user_mention}, не поширюй єресь. Повідомлення видалено.\n"
                     f"Це твоє <b>попередження {new_warns}/{max_warns}</b>. Слідкуй за мовою. 🌿",
                     parse_mode=ParseMode.HTML
                )
                
            return # Одне порушення за повідомлення

def register_chat_event_handlers(application: Application):
    """Реєструє обробники для пасивних подій чату."""
    
    # Привітання нових учасників
    application.add_handler(
        MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, handle_new_members)
    )
    
    # Фільтр слів (високий пріоритет)
    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND & filters.ChatType.GROUPS,
            word_filter_handler
        ),
        group=3
    )
    
    logger.info("Модуль Подій Чату (chat_event_handlers.py) завантажено. Мур... 🐾")