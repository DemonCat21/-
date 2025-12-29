# admin_handlers.py
# -*- coding: utf-8 -*-
"""
admin_handlers.py

Повністю перероблений модуль адміністрування.
Включає безпечну активацію, логічне меню та розширені функції.
Усі функції захищені декоратором @owner_only.
(Оновлено для підтримки модів 🎭)
"""
import logging
import math
import asyncio
import html
import functools
import re
import os
from typing import Callable, Awaitable, Any, Optional

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, Message, InputFile, CallbackQuery
from telegram.constants import ParseMode, ChatType
from telegram.ext import (
    Application,
    ContextTypes,
    CommandHandler,
    CallbackQueryHandler,
    ConversationHandler,
    MessageHandler,
    filters,
)
from telegram.error import TelegramError, Forbidden, BadRequest

from bot.core.database import (
    get_total_users,
    get_all_chats,
    get_total_chats_count,
    get_global_ai_status,
    set_global_ai_status,
    set_chat_ai_status,
    get_bot_stats,
    is_ai_enabled_for_chat,
    get_all_user_ids,
    clear_conversations,
    get_user_info,
    update_user_balance,
    admin_set_game_stats,
    ban_user,
    unban_user,
    save_sticker,
    remove_sticker_db,
    # --- НОВІ ІМПОРТИ ---
    get_top_balances,
    get_banned_users,
    get_all_stickers,
    get_all_users_info,
    # --- (НОВЕ) ІМПОРТИ ДЛЯ МОДІВ ---
    get_global_bot_mode,
    set_global_bot_mode,
)
# --- (НОВЕ) ІМПОРТИ ДЛЯ МОДІВ ---
from bot.utils.utils import OWNER_ID, PHOTO_DIR, BotTheme, refresh_theme_cache
from bot.core.daily_tasks import nun_of_the_day_job, assign_daily_predictions_job

logger = logging.getLogger(__name__)

# --- Декоратор для перевірки власника ---

Decorator = Callable[
    [Callable[[Update, ContextTypes.DEFAULT_TYPE], Awaitable[Any]]],
    Callable[[Update, ContextTypes.DEFAULT_TYPE], Awaitable[Any]],
]


def owner_only(func: Callable) -> Callable:
    """Обмежує доступ до команди лише для OWNER_ID."""

    @functools.wraps(func)
    async def wrapper(
        update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs
    ):
        if not update:
            logger.error(f"Функція {func.__name__} викликана без 'update'.")
            return
            
        user_id = update.effective_user.id if update.effective_user else None
        
        if not user_id:
             # Це може бути, наприклад, job_queue callback, який не має юзера
             logger.warning(f"owner_only: не вдалося отримати user_id для {func.__name__}.")
             if update.message or update.callback_query:
                 logger.error("owner_only: update є, але user_id відсутній.")
                 return
             
        if user_id != OWNER_ID:
            if update.callback_query:
                await update.callback_query.answer(
                    "Ця функція доступна тільки власнику бота. 📿", show_alert=True
                )
            elif update.message:
                await update.message.reply_text(
                    "Ця функція доступна тільки власнику бота. 📿"
                )
            logger.warning(f"Користувач {user_id} спробував отримати доступ до {func.__name__}.")
            return
        
        # Виконуємо функцію
        return await func(update, context, *args, **kwargs)

    return wrapper


# --- Стани для розмов (ConversationHandler) ---
(
    # Розсилка
    BROADCAST_MESSAGE,
    BROADCAST_CONFIRM,
    # Керування юзерами
    GET_USER_ID_INFO,
    GET_USER_ID_BALANCE,
    GET_BALANCE_AMOUNT,
    GET_USER_ID_STATS,
    GET_CHAT_ID_STATS,
    GET_STATS_VALUES,
    GET_USER_ID_BAN,
    BAN_CONFIRM,
    GET_USER_ID_MESSAGE,
    GET_MESSAGE_TEXT,
    # Керування контентом
    CONTENT_ADD_PHOTO_AWAIT_IMG,
    CONTENT_REMOVE_PHOTO_AWAIT_NAME,
    CONTENT_ADD_STICKER_AWAIT_ALIAS,
    CONTENT_ADD_STICKER_AWAIT_STICKER,
    CONTENT_REMOVE_STICKER_AWAIT_NAME,
) = range(17)


# =============================================================================
# 1. Головне Меню та Навігація
# =============================================================================


@owner_only
async def admin_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE, from_callback: bool = False
) -> None:
    """Відображає головну адмін-панель."""
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id

    try:
        total_users = await get_total_users()
        total_chats = await get_total_chats_count()
        stats = await get_bot_stats()
        active_chats_24h = stats.get("active_users_24h", 0)
    except Exception as e:
        logger.error(f"Не вдалося отримати статистику для адмін-панелі: {e}")
        total_users, total_chats, active_chats_24h = "Помилка", "Помилка", "Помилка"

    text = (
        f"<b>👑 Панель Керування Котом 🐾</b>\n\n"
        f"На зв'язку, мій Повелителю! Вітаю зі світанком 🌿:\n\n"
        f"👥 <b>Всього послідовників:</b> {total_users}\n"
        f"💬 <b>Всього чатів-келій:</b> {total_chats}\n"
        f"⚡️ <b>Активних за 24г:</b> {active_chats_24h}\n\n"
        f"<i>Оберіть, як направити котячий дух:</i>"
    )

    async def _edit_admin_message(q: CallbackQuery) -> None:
        """Редагує текст або підпис вихідного повідомлення, якщо воно було медіа."""
        try:
            if q and q.message:
                if q.message.text:
                    await q.edit_message_text(text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)
                else:
                    await q.edit_message_caption(text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)
            else:
                await context.bot.send_message(chat_id=chat_id, text=text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)
        except BadRequest as e:
            if "Message is not modified" in str(e):
                logger.info("Admin menu is already up to date.")
            elif "Message to edit not found" in str(e):
                logger.warning("Message to edit not found. Sending new message.")
                await context.bot.send_message(chat_id=chat_id, text=text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)
            else:
                raise

    keyboard = [
        [
            InlineKeyboardButton("📜 Списки & Статистика", callback_data="admin_lists_menu"),
            InlineKeyboardButton("👥 Керув. Послідовниками", callback_data="admin_user_menu"),
        ],
        [
            InlineKeyboardButton("🤖 Керування AI", callback_data="admin_ai_menu"),
            InlineKeyboardButton("🎨 Керув. Контентом", callback_data="admin_content_menu"),
        ],
        [
            InlineKeyboardButton("📢 Розсилка", callback_data="admin_broadcast_start"),
            InlineKeyboardButton("🛠️ Обслуговування", callback_data="admin_maint_menu"),
        ],
        # --- (НОВЕ) Кнопка Керування Модами ---
        [
            InlineKeyboardButton("🎭 Керування Модами", callback_data="admin_mode_menu")
        ],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    try:
        if update.callback_query or from_callback:
            # `from_callback` використовується, коли ми повертаємось з ConversationHandler
            # і `update.callback_query` може бути відсутнім
            
            query = update.callback_query
            if query:
                await query.answer()
                await _edit_admin_message(query)
            else:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=text,
                    reply_markup=reply_markup,
                    parse_mode=ParseMode.HTML,
                )
        else:
            if update.message:
                try:
                    await update.message.delete()
                except TelegramError:
                    pass
            await context.bot.send_message(
                chat_id=chat_id,
                text=text,
                reply_markup=reply_markup,
                parse_mode=ParseMode.HTML,
            )
    except BadRequest as e:
        logger.error(f"Помилка BadRequest при відправці адмін-меню: {e}", exc_info=True)
    except TelegramError as e:
        logger.error(f"Помилка при відправці/редагуванні адмін-меню: {e}", exc_info=True)


@owner_only
async def cancel_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Універсальна функція для скасування будь-якого поточного діалогу."""
    query = update.callback_query
    if query:
        await query.answer()
        try:
            await query.edit_message_text("Дію скасовано. ✖️")
        except TelegramError as e:
            logger.debug(f"Не вдалося відредагувати повідомлення після скасування: {e}")

    context.user_data.clear()
    await asyncio.sleep(1)
    await admin_command(update, context, from_callback=True)
    return ConversationHandler.END


# === (НОВЕ) Меню Списків & Статистики ===
@owner_only
async def admin_lists_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """(НОВЕ) Меню для списків та статистики."""
    query = update.callback_query
    await query.answer()
    keyboard = [
        [
            InlineKeyboardButton("📋 Список Послідовників", callback_data="admin_list_users_0"),
            InlineKeyboardButton("📋 Список Келій (Чатів)", callback_data="admin_list_chats_0"),
        ],
        [
            InlineKeyboardButton("📊 Загальна Статистика", callback_data="admin_stats"),
        ],
        [InlineKeyboardButton("↩️ Назад", callback_data="admin_menu")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text(
        "<b>📜 Списки & Статистика:</b>\n\nОберіть, що бажаєте переглянути.",
        reply_markup=reply_markup,
        parse_mode=ParseMode.HTML,
    )


@owner_only
async def show_statistics_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Показує детальну статистику бота."""
    query = update.callback_query
    await query.answer("Очищаю лічильник...")
    stats = await get_bot_stats()
    text = (
        f"📊 <b>Статистика Бота (Моніторинг)</b>\n"
        f"Усього муркотінь (повідомлень): <b>{stats.get('total_messages', 0)}</b>\n"
        f"Усього послідовників: <b>{stats.get('total_users', 0)}</b>\n"
        f"Активних чатів (24 год): <b>{stats.get('active_users_24h', 0)}</b>\n\n"
        f"<b>Популярні погладжування (команди):</b>\n"
    )
    if stats.get("popular_commands"):
        for cmd, count in stats["popular_commands"]:
            text += f" - <code>{cmd}</code>: {count}\n"
    else:
        text += "<i>Немає даних про популярні команди.</i>\n"
    keyboard = [[InlineKeyboardButton("↩️ Назад", callback_data="admin_lists_menu")]] # <-- ОНОВЛЕНО
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text(
        text=text, reply_markup=reply_markup, parse_mode=ParseMode.HTML
    )


# === (НОВІ) Функції Списків ===
@owner_only
async def show_user_list(
    update: Update, context: ContextTypes.DEFAULT_TYPE, page_offset: Optional[int] = None
) -> None:
    """(НОВЕ) Показує список користувачів з пагінацією."""
    query = update.callback_query
    is_refresh_call = page_offset is not None

    if not is_refresh_call:
        await query.answer()
        try:
            page_offset = int(query.data.split("_")[-1])
        except (ValueError, IndexError):
            page_offset = 0
    
    page_size = 10 # 10 users per page
    all_users = await get_all_users_info(page_offset, page_size)
    total_users_count = await get_total_users() # Use existing function
    total_pages = math.ceil(total_users_count / page_size)
    
    text = f"<b>📋 Список Послідовників</b> (стор. {page_offset + 1}/{total_pages}, всього {total_users_count}):\n\n"
    keyboard = []

    if not all_users:
        text += "<i>Немає користувачів.</i>"
    else:
        for user in all_users:
            user_id = user['user_id']
            name = html.escape(user.get('first_name') or f"ID: {user_id}")
            username_str = f" (@{user['username']})" if user.get('username') else ""
            banned_str = " 🚫" if user.get('is_banned') else ""
            
            # Clickable link tg://user?id=...
            text += f'• <a href="tg://user?id={user_id}">{name}</a>{username_str} [<code>{user_id}</code>]{banned_str}\n'

    nav_buttons = []
    if page_offset > 0:
        nav_buttons.append(
            InlineKeyboardButton("⬅️", callback_data=f"admin_list_users_{page_offset - 1}")
        )
    if page_offset < total_pages - 1:
        nav_buttons.append(
            InlineKeyboardButton("➡️", callback_data=f"admin_list_users_{page_offset + 1}")
        )
    if nav_buttons:
        keyboard.append(nav_buttons)

    keyboard.append([InlineKeyboardButton("↩️ Назад", callback_data="admin_lists_menu")])
    reply_markup = InlineKeyboardMarkup(keyboard)

    try:
        await query.edit_message_text(
            text=text,
            reply_markup=reply_markup,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True, # Important for tg:// links
        )
    except BadRequest as e:
        if "Message is not modified" in str(e):
            logger.info(f"Список користувачів (стор. {page_offset}) не було змінено.")
        else:
            logger.error(f"Помилка BAdRequest при показі списку користувачів: {e}", exc_info=True)
    except Exception as e:
         logger.error(f"Неочікувана помилка при показі списку користувачів: {e}", exc_info=True)


@owner_only
async def show_chat_list(
    update: Update, context: ContextTypes.DEFAULT_TYPE, page_offset: Optional[int] = None
) -> None:
    """(НОВЕ) Показує список чатів з пагінацією та посиланнями."""
    query = update.callback_query
    is_refresh_call = page_offset is not None

    if not is_refresh_call:
        await query.answer()
        try:
            page_offset = int(query.data.split("_")[-1])
        except (ValueError, IndexError):
            page_offset = 0
    
    page_size = 5 # 5 chats per page, like in the AI list
    all_chats = await get_all_chats(page_offset, page_size)
    total_chats_count = await get_total_chats_count()
    total_pages = math.ceil(total_chats_count / page_size)
    
    text = f"<b>📋 Список Келій (Чатів)</b> (стор. {page_offset + 1}/{total_pages}, всього {total_chats_count}):\n\n"
    keyboard = []

    if not all_chats:
        text += "<i>Немає чатів.</i>"
    else:
        for chat in all_chats:
            chat_id = chat['chat_id']
            title = html.escape(chat.get('chat_title') or f"ID: {chat_id}")
            chat_type = chat.get('chat_type', 'N/A')
            username = chat.get('chat_username')
            
            link_str = f" (@{username})" if username else ""
            text += f"• {title} ({chat_type}){link_str} [<code>{chat_id}</code>]\n"

    nav_buttons = []
    if page_offset > 0:
        nav_buttons.append(
            InlineKeyboardButton("⬅️", callback_data=f"admin_list_chats_{page_offset - 1}")
        )
    if page_offset < total_pages - 1:
        nav_buttons.append(
            InlineKeyboardButton("➡️", callback_data=f"admin_list_chats_{page_offset + 1}")
        )
    if nav_buttons:
        keyboard.append(nav_buttons)

    keyboard.append([InlineKeyboardButton("↩️ Назад", callback_data="admin_lists_menu")])
    reply_markup = InlineKeyboardMarkup(keyboard)

    try:
        await query.edit_message_text(
            text=text,
            reply_markup=reply_markup,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
    except BadRequest as e:
        if "Message is not modified" in str(e):
            logger.info(f"Список чатів (стор. {page_offset}) не було змінено.")
        else:
            logger.error(f"Помилка BAdRequest при показі списку чатів: {e}", exc_info=True)
    except Exception as e:
         logger.error(f"Неочікувана помилка при показі списку чатів: {e}", exc_info=True)


# =============================================================================
# 2. Керування Послідовниками (Покращене меню)
# =============================================================================


@owner_only
async def user_management_menu(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """(Оновлене) Меню керування користувачами."""
    query = update.callback_query
    await query.answer()
    keyboard = [
        [
            InlineKeyboardButton("ℹ️ Інфо про послідовника", callback_data="admin_user_info"),
            InlineKeyboardButton("💰 Змінити м'ятки", callback_data="admin_user_balance"),
        ],
        [
            InlineKeyboardButton("📊 Змінити статистику", callback_data="admin_user_stats"),
            InlineKeyboardButton("🚫 Бан / Розбан", callback_data="admin_user_ban"),
        ],
        [
            InlineKeyboardButton("✉️ Надіслати повідомлення", callback_data="admin_user_msg"),
            InlineKeyboardButton("💰 Топ-10 Балансів", callback_data="admin_user_top_balance"),
        ],
        [
            InlineKeyboardButton("🚫 Список Забанених", callback_data="admin_user_banned_list"),
        ],
        [
            InlineKeyboardButton("↩️ Назад", callback_data="admin_menu"),
        ],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text(
        "<b>👥 Керування послідовниками:</b>",
        reply_markup=reply_markup,
        parse_mode=ParseMode.HTML,
    )


async def _ask_for_user_id(
    update: Update, context: ContextTypes.DEFAULT_TYPE, next_state: int, text: str
) -> int:
    """Універсальна функція для запиту ID користувача."""
    query = update.callback_query
    await query.answer()
    context.user_data["admin_next_state"] = next_state
    keyboard = [[InlineKeyboardButton("✖️ Скасувати", callback_data="admin_cancel")]]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
    return next_state


@owner_only
async def get_user_info_prompt(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Запускає діалог для отримання інформації про користувача."""
    return await _ask_for_user_id(
        update,
        context,
        GET_USER_ID_INFO,
        "Надішліть ID послідовника для отримання інформації.",
    )


@owner_only
async def process_user_id_for_info(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Обробляє ID користувача для відображення інформації."""
    if not update.message or not update.message.text:
        return GET_USER_ID_INFO

    try:
        user_id_to_find = int(update.message.text)
    except ValueError:
        await update.message.reply_text("Некоректний ID. Будь ласка, надішліть число.")
        return GET_USER_ID_INFO

    user_info = await get_user_info(user_id_to_find)

    if not user_info:
        await update.message.reply_text(
            f"Послідовника з ID <code>{user_id_to_find}</code> не знайдено.",
            parse_mode=ParseMode.HTML,
        )
    else:
        balance = user_info.get("balance", 0)
        is_banned = "Так" if user_info.get("is_banned", 0) == 1 else "Ні"
        text = (
            f"<b>ℹ️ Інформація про послідовника:</b> <code>{user_id_to_find}</code>\n"
            f"Ім'я: {html.escape(user_info.get('first_name', 'N/A'))}\n"
            f"Юзернейм: @{user_info.get('username', 'N/A')}\n"
            f"💰 М'ятки: {balance} 🌿\n"
            f"🚫 Забанений: {is_banned}\n"
        )
        await update.message.reply_text(text, parse_mode=ParseMode.HTML)

    await admin_command(update, context, from_callback=True)
    return ConversationHandler.END


@owner_only
async def change_balance_prompt(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Запускає діалог для зміни балансу."""
    return await _ask_for_user_id(
        update,
        context,
        GET_USER_ID_BALANCE,
        "Надішліть ID послідовника, м'ятки якого потрібно змінити.",
    )


@owner_only
async def process_user_id_for_balance(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Обробляє ID користувача для зміни балансу і просить ввести суму."""
    if not update.message or not update.message.text:
        return GET_USER_ID_BALANCE

    try:
        user_id_to_change = int(update.message.text)
        user_exists = await get_user_info(user_id_to_change)

        if not user_exists:
            await update.message.reply_text(
                f"Послідовника з ID <code>{user_id_to_change}</code> не знайдено. Будь ласка, надішліть існуючий ID.",
                parse_mode=ParseMode.HTML,
            )
            return GET_USER_ID_BALANCE

        context.user_data["user_id_for_balance"] = user_id_to_change
        keyboard = [[InlineKeyboardButton("✖️ Скасувати", callback_data="admin_cancel")]]

        await update.message.reply_html(
            f"Вкажіть суму для зміни м'яток послідовника <code>{user_id_to_change}</code>.\n"
            f"<i>(Використовуйте від'ємне число, щоб відібрати м'ятки, напр., -50)</i>",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return GET_BALANCE_AMOUNT
    except ValueError:
        await update.message.reply_text("Некоректний ID. Будь ласка, надішліть число.")
        return GET_USER_ID_BALANCE


@owner_only
async def process_balance_amount(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Обробляє суму, змінює баланс і завершує діалог."""
    if not update.message or not update.message.text:
        return GET_BALANCE_AMOUNT

    try:
        amount = int(update.message.text)
        user_id = context.user_data.get("user_id_for_balance")

        if not user_id:
            await update.message.reply_text("Помилка: ID послідовника не знайдено.")
            return await cancel_action(update, context)

        await update_user_balance(user_id, amount)
        current_balance = await get_user_info(user_id)
        current_balance_value = (
            current_balance.get("balance", 0) if current_balance else 0
        )

        await update.message.reply_html(
            f"Баланс послідовника <code>{user_id}</code> змінено на {amount} м'яток. "
            f"Новий баланс: <b>{current_balance_value} 🌿</b>",
        )
    except ValueError:
        await update.message.reply_text("Некоректна сума. Будь ласка, надішліть число.")
        return GET_BALANCE_AMOUNT

    context.user_data.pop("user_id_for_balance", None)
    await admin_command(update, context, from_callback=True)
    return ConversationHandler.END


@owner_only
async def change_game_stats_prompt(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Запускає діалог для отримання ID користувача для зміни статистики."""
    return await _ask_for_user_id(
        update,
        context,
        GET_USER_ID_STATS,
        "Надішліть ID послідовника, статистику гри якого потрібно змінити.",
    )


@owner_only
async def process_user_id_for_stats(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Обробляє ID користувача і просить ID чату."""
    if not update.message or not update.message.text:
        return GET_USER_ID_STATS

    try:
        user_id_to_change = int(update.message.text)
        user_exists = await get_user_info(user_id_to_change)
        if not user_exists:
            await update.message.reply_text(
                f"Послідовника з ID <code>{user_id_to_change}</code> не знайдено.",
                parse_mode=ParseMode.HTML,
            )
            return GET_USER_ID_STATS

        context.user_data["user_id_for_stats"] = user_id_to_change
        keyboard = [[InlineKeyboardButton("✖️ Скасувати", callback_data="admin_cancel")]]
        await update.message.reply_html(
            f"Тепер надішліть ID келії (чату) для послідовника <code>{user_id_to_change}</code>.",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return GET_CHAT_ID_STATS
    except ValueError:
        await update.message.reply_text("Некоректний ID. Надішліть число.")
        return GET_USER_ID_STATS


@owner_only
async def process_chat_id_for_stats(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Обробляє ID чату і просить ввести нові значення статистики."""
    if not update.message or not update.message.text:
        return GET_CHAT_ID_STATS

    try:
        chat_id_to_change = int(update.message.text)
        context.user_data["chat_id_for_stats"] = chat_id_to_change
        keyboard = [[InlineKeyboardButton("✖️ Скасувати", callback_data="admin_cancel")]]
        await update.message.reply_html(
            "Введіть нову статистику для гри 'tic_tac_toe' у форматі: "
            "<b>перемоги поразки нічиї</b>\n"
            "Наприклад: <code>10 5 3</code>",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return GET_STATS_VALUES
    except ValueError:
        await update.message.reply_text("Некоректний ID келії. Надішліть число.")
        return GET_CHAT_ID_STATS


@owner_only
async def process_stats_values(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Обробляє нові значення статистики і оновлює їх."""
    if not update.message or not update.message.text:
        return GET_STATS_VALUES

    try:
        wins, losses, draws = map(int, update.message.text.split())
        user_id = context.user_data.get("user_id_for_stats")
        chat_id = context.user_data.get("chat_id_for_stats")

        if user_id and chat_id:
            await admin_set_game_stats(
                user_id, chat_id, "tic_tac_toe", wins, losses, draws
            )
            await update.message.reply_html(
                f"✅ Статистику для послідовника <code>{user_id}</code> в келії <code>{chat_id}</code> оновлено:\n"
                f"Перемоги: {wins}, Поразки: {losses}, Нічиї: {draws}",
            )
        else:
            await update.message.reply_text("Помилка: ID послідовника або келії не знайдено.")

    except ValueError:
        await update.message.reply_text(
            "Некоректний формат. Надішліть три числа, розділені пробілом."
        )
        return GET_STATS_VALUES
    except Exception as e:
        logger.error(f"Помилка при оновленні статистики гри: {e}")
        await update.message.reply_text(f"Сталася помилка: {e}")

    context.user_data.pop("user_id_for_stats", None)
    context.user_data.pop("chat_id_for_stats", None)
    await admin_command(update, context, from_callback=True)
    return ConversationHandler.END


@owner_only
async def ban_user_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Запускає діалог для бану/розбану користувача."""
    return await _ask_for_user_id(
        update, context, GET_USER_ID_BAN, "Надішліть ID послідовника для бану/розбану."
    )


@owner_only
async def process_user_id_for_ban(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Обробляє ID користувача, показує статус та кнопки бану/розбану."""
    if not update.message or not update.message.text:
        return GET_USER_ID_BAN

    try:
        user_id_to_ban = int(update.message.text)
        user_info = await get_user_info(user_id_to_ban)

        if not user_info:
            await update.message.reply_text(
                f"Послідовника з ID <code>{user_id_to_ban}</code> не знайдено.",
                parse_mode=ParseMode.HTML,
            )
            return GET_USER_ID_BAN

        context.user_data["user_id_for_ban"] = user_id_to_ban
        is_banned = user_info.get("is_banned", 0) == 1
        status_text = "🚫 <b>ЗАБЛОКОВАНИЙ</b>" if is_banned else "✅ <b>Активний</b>"

        keyboard = [
            [
                InlineKeyboardButton(
                    "✅ Розблокувати", callback_data=f"admin_ban_unban"
                )
            ]
            if is_banned
            else [
                InlineKeyboardButton("🚫 Заблокувати", callback_data=f"admin_ban_ban")
            ]
        ]
        keyboard.append(
            [InlineKeyboardButton("✖️ Скасувати", callback_data="admin_cancel")]
        )
        reply_markup = InlineKeyboardMarkup(keyboard)

        await update.message.reply_html(
            f"Послідовник: <code>{user_id_to_ban}</code>\n"
            f"Ім'я: {html.escape(user_info.get('first_name', 'N/A'))}\n"
            f"Статус: {status_text}\n\nОберіть дію:",
            reply_markup=reply_markup,
        )
        return BAN_CONFIRM

    except ValueError:
        await update.message.reply_text("Некоректний ID. Будь ласка, надішліть число.")
        return GET_USER_ID_BAN


@owner_only
async def process_ban_confirm(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Обробляє натискання кнопки 'Бан' або 'Розбан'."""
    query = update.callback_query
    await query.answer()

    action = query.data.split("_")[-1]  # 'ban' або 'unban'
    user_id = context.user_data.get("user_id_for_ban")

    if not user_id:
        await query.edit_message_text("Помилка: ID послідовника не знайдено.")
        return await cancel_action(update, context)

    if action == "ban":
        await ban_user(user_id)
        await query.edit_message_text(
            f"🚫 Послідовника <code>{user_id}</code> було <b>заблоковано</b>.",
            parse_mode=ParseMode.HTML,
        )
    elif action == "unban":
        await unban_user(user_id)
        await query.edit_message_text(
            f"✅ Послідовника <code>{user_id}</code> було <b>розблоковано</b>.",
            parse_mode=ParseMode.HTML,
        )

    context.user_data.pop("user_id_for_ban", None)
    await asyncio.sleep(2)
    await admin_command(update, context, from_callback=True)
    return ConversationHandler.END


@owner_only
async def send_message_prompt(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Запускає діалог для відправки повідомлення: просить ID чату."""
    return await _ask_for_user_id(
        update,
        context,
        GET_USER_ID_MESSAGE,
        "Надішліть <b>Chat ID</b> (або User ID), куди потрібно відправити повідомлення.",
    )


@owner_only
async def send_message_get_chat_id(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Обробляє ID чату і просить текст повідомлення."""
    if not update.message or not update.message.text:
        return GET_USER_ID_MESSAGE

    try:
        chat_id = int(update.message.text)
        context.user_data["send_message_chat_id"] = chat_id
        keyboard = [[InlineKeyboardButton("✖️ Скасувати", callback_data="admin_cancel")]]
        await update.message.reply_html(
            f"Добре. Тепер надішліть повідомлення (текст, фото, стікер), яке потрібно відправити в келію <code>{chat_id}</code>.",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return GET_MESSAGE_TEXT
    except ValueError:
        await update.message.reply_text("Некоректний ID. Будь ласка, надішліть число.")
        return GET_USER_ID_MESSAGE


@owner_only
async def send_message_execute(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Надсилає повідомлення вказаному чату/користувачу."""
    chat_id = context.user_data.get("send_message_chat_id")
    message_to_send: Optional[Message] = update.message

    if not chat_id or not message_to_send:
        await update.message.reply_text("Помилка: Chat ID або повідомлення не знайдено.")
        return await cancel_action(update, context)

    try:
        await context.bot.copy_message(
            chat_id=chat_id,
            from_chat_id=message_to_send.chat.id,
            message_id=message_to_send.message_id,
        )
        await update.message.reply_html(
            f"✅ Повідомлення успішно надіслано в келію <code>{chat_id}</code>."
        )
    except TelegramError as e:
        await update.message.reply_html(
            f"❌ Не вдалося надіслати повідомлення в келію <code>{chat_id}</code>.\nПомилка: {e}"
        )

    context.user_data.pop("send_message_chat_id", None)
    await admin_command(update, context, from_callback=True)
    return ConversationHandler.END


# --- НОВІ ФУНКЦІЇ ДЛЯ КЕРУВАННЯ КОРИСТУВАЧАМИ ---

@owner_only
async def show_top_balances(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """(НОВА ФУНКЦІЯ) Показує топ-10 користувачів за балансом м'ятки."""
    query = update.callback_query
    await query.answer("Шукаю найбагатших котиків...")
    
    try:
        top_users = await get_top_balances(10)
        
        if not top_users:
            text = "<b>💰 Топ-10 Балансів:</b>\n\n<i>Ні в кого ще немає м'ятки.</i>"
        else:
            text = "<b>💰 Топ-10 Балансів:</b>\n\n"
            for i, user in enumerate(top_users):
                name = html.escape(user.get('first_name') or user.get('username') or f"ID: {user['user_id']}")
                text += f"{i+1}. {name}: <b>{user['balance']} 🌿</b>\n"
                
        keyboard = [[InlineKeyboardButton("↩️ Назад", callback_data="admin_user_menu")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(text, reply_markup=reply_markup, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
        
    except Exception as e:
        logger.error(f"Помилка при отриманні топ балансів: {e}", exc_info=True)
        await query.edit_message_text("❌ Помилка при отриманні даних.")


@owner_only
async def show_banned_users(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """(НОВА ФУНКЦІЯ) Показує список забанених користувачів."""
    query = update.callback_query
    await query.answer("Збираю список грішників...")
    
    try:
        banned_users = await get_banned_users()
        
        if not banned_users:
            text = "<b>🚫 Список Забанених:</b>\n\n<i>Немає заблокованих послідовників.</i>"
        else:
            text = "<b>🚫 Список Забанених:</b>\n\n"
            for user in banned_users:
                name = html.escape(user.get('first_name') or f"ID: {user['user_id']}")
                username = f" (@{user['username']})" if user.get('username') else ""
                text += f"- {name}{username} (<code>{user['user_id']}</code>)\n"
                
        keyboard = [[InlineKeyboardButton("↩️ Назад", callback_data="admin_user_menu")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(text, reply_markup=reply_markup, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
        
    except Exception as e:
        logger.error(f"Помилка при отриманні списку забанених: {e}", exc_info=True)
        await query.edit_message_text("❌ Помилка при отриманні даних.")


# =============================================================================
# 3. Керування AI
# =============================================================================


@owner_only
async def ai_control_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Меню керування глобальними та чатовими налаштуваннями AI."""
    query = update.callback_query
    await query.answer()
    global_ai_status = await get_global_ai_status()
    global_ai_text = "✅ Увімкнено" if global_ai_status else "❌ Вимкнено"
    text = (
        f"<b>🤖 Керування Штучним Інтелектом</b>\n\n"
        f"Поточний глобальний статус: <b>{global_ai_text}</b>\n\n"
    )
    keyboard = [
        [
            InlineKeyboardButton(
                "🌍 Перемкнути AI глобально", callback_data="admin_ai_toggle_global"
            )
        ],
        [
            InlineKeyboardButton(
                "💬 Налаштувати келії (чати)", callback_data="admin_ai_chats_list_0"
            )
        ],
        [InlineKeyboardButton("↩️ Назад", callback_data="admin_menu")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text(
        text, reply_markup=reply_markup, parse_mode=ParseMode.HTML
    )


@owner_only
async def toggle_global_ai(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Перемикає глобальний статус AI."""
    query = update.callback_query
    await query.answer()
    current_status = await get_global_ai_status()
    new_status = not current_status
    await set_global_ai_status(new_status)
    await ai_control_menu(update, context)


@owner_only
async def show_ai_chats_list(
    update: Update, context: ContextTypes.DEFAULT_TYPE, page_offset: Optional[int] = None
) -> None:
    """Показує список чатів для індивідуального налаштування AI."""
    query = update.callback_query
    is_refresh_call = page_offset is not None

    if not is_refresh_call:
        await query.answer()
        try:
            page_offset = int(query.data.split("_")[-1])
        except (ValueError, IndexError):
            page_offset = 0

    page_size = 5
    all_chats = await get_all_chats(page_offset, page_size)
    total_chats_count = await get_total_chats_count()
    total_pages = math.ceil(total_chats_count / page_size)
    text = f"<b>🔧 Налаштування AI для Келій</b> (стор. {page_offset + 1}/{total_pages}):"
    keyboard = []

    if not all_chats:
        text += "\n\n<i>Немає чатів для налаштування.</i>"
    else:
        for chat in all_chats:
            chat_id, chat_title, ai_status = (
                chat["chat_id"],
                chat.get("chat_title"),
                chat["ai_enabled"],
            )
            chat_title_escaped = html.escape(chat_title or f"ID: {chat_id}")
            if len(chat_title_escaped) > 25:
                chat_title_escaped = chat_title_escaped[:22] + "..."
            ai_status_emoji = "✅" if ai_status else "❌"

            callback_data = f"admin_ai_toggle_chat_{chat_id}_{page_offset}"
            keyboard.append(
                [
                    InlineKeyboardButton(
                        f"{ai_status_emoji} {chat_title_escaped}",
                        callback_data=callback_data,
                    )
                ]
            )

    nav_buttons = []
    if page_offset > 0:
        nav_buttons.append(
            InlineKeyboardButton("⬅️", callback_data=f"admin_ai_chats_list_{page_offset - 1}")
        )
    if page_offset < total_pages - 1:
        nav_buttons.append(
            InlineKeyboardButton("➡️", callback_data=f"admin_ai_chats_list_{page_offset + 1}")
        )
    if nav_buttons:
        keyboard.append(nav_buttons)

    keyboard.append([InlineKeyboardButton("↩️ Назад", callback_data="admin_ai_menu")])
    reply_markup = InlineKeyboardMarkup(keyboard)

    try:
        await query.edit_message_text(
            text=text,
            reply_markup=reply_markup,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
    except BadRequest as e:
        if "Message is not modified" in str(e):
            logger.info(f"Меню AI для чатів (стор. {page_offset}) не було змінено.")
        else:
            logger.error(
                f"Помилка BadRequest при показі списку чатів AI: {e}", exc_info=True
            )
    except Exception as e:
        logger.error(f"Неочікувана помилка при показі списку чатів AI: {e}", exc_info=True)


@owner_only
async def toggle_chat_ai(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Перемикає індивідуальний статус AI для чату."""
    query = update.callback_query
    await query.answer()

    parts = query.data.split("_")
    chat_id_to_toggle, page_offset = int(parts[4]), int(parts[5])

    current_status = await is_ai_enabled_for_chat(
        chat_id_to_toggle, ignore_global=True
    )
    await set_chat_ai_status(chat_id_to_toggle, not current_status)
    await show_ai_chats_list(update, context, page_offset=page_offset)


# =============================================================================
# 4. Керування Контентом (Оновлене Меню)
# =============================================================================


@owner_only
async def content_management_menu(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """(Оновлене) Меню керування контентом (фото, стікери)."""
    query = update.callback_query
    await query.answer()
    keyboard = [
        [
            InlineKeyboardButton("🖼️ Додати/Змінити Фото", callback_data="admin_content_add_photo"),
            InlineKeyboardButton("🗑️ Видалити Фото", callback_data="admin_content_rem_photo"),
        ],
        [
            InlineKeyboardButton("✨ Додати Стікер", callback_data="admin_content_add_sticker"),
            InlineKeyboardButton("🗑️ Видалити Стікер", callback_data="admin_content_rem_sticker"),
        ],
        [
            InlineKeyboardButton("✨ Список Стікерів", callback_data="admin_content_list_stickers"),
        ],
        [InlineKeyboardButton("↩️ Назад", callback_data="admin_menu")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text(
        "<b>🎨 Керування контентом:</b>",
        reply_markup=reply_markup,
        parse_mode=ParseMode.HTML,
    )


@owner_only
async def add_photo_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Просить надіслати фото з підписом <команда>."""
    query = update.callback_query
    await query.answer()
    keyboard = [[InlineKeyboardButton("✖️ Скасувати", callback_data="admin_cancel")]]
    await query.edit_message_text(
        "Надішли мені <b>фото</b>.\nУ <b>підписі</b> до фото вкажи <b>одним словом</b> команду, "
        "до якої його прив'язати (напр., <code>обійняти</code>).",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.HTML,
    )
    return CONTENT_ADD_PHOTO_AWAIT_IMG


@owner_only
async def process_add_photo(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Обробляє отримане фото та підпис."""
    message = update.message
    if not message or (not message.photo and not message.document):
        await message.reply_text("Це не фото. Будь ласка, надішли фото.")
        return CONTENT_ADD_PHOTO_AWAIT_IMG

    action = None
    if message.caption:
        action = message.caption.strip().lower().split()[0]
    
    if not action:
        await message.reply_text("Не бачу підпису. Будь ласка, надішли фото з підписом (командою).")
        return CONTENT_ADD_PHOTO_AWAIT_IMG

    file_to_process = None
    if message.photo:
        file_to_process = message.photo[-1]
    elif (
        message.document
        and message.document.mime_type
        and message.document.mime_type.startswith("image/")
    ):
        file_to_process = message.document
    
    if file_to_process:
        file = await file_to_process.get_file()
        photo_path = os.path.join(PHOTO_DIR, f"{action}.jpg")
        try:
            os.makedirs(PHOTO_DIR, exist_ok=True)
            await file.download_to_drive(photo_path)
            await message.reply_text(f"✅ Фото для «<b>{action}</b>» додано/оновлено.", parse_mode=ParseMode.HTML)
        except Exception as e:
            logger.error(f"Помилка завантаження фото '{action}': {e}", exc_info=True)
            await message.reply_text("Вибач, не зміг зберегти фото.")
    else:
        await message.reply_text("Сталася помилка при обробці файлу.")

    await admin_command(update, context, from_callback=True)
    return ConversationHandler.END


@owner_only
async def remove_photo_prompt(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Просить назву команди для видалення фото."""
    query = update.callback_query
    await query.answer()
    keyboard = [[InlineKeyboardButton("✖️ Скасувати", callback_data="admin_cancel")]]
    await query.edit_message_text(
        "Надішли <b>назву команди</b> (напр., <code>обійняти</code>), "
        "для якої потрібно <b>видалити</b> фото.",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.HTML,
    )
    return CONTENT_REMOVE_PHOTO_AWAIT_NAME


@owner_only
async def process_remove_photo(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Обробляє назву та видаляє фото."""
    if not update.message or not update.message.text:
        return CONTENT_REMOVE_PHOTO_AWAIT_NAME

    action = update.message.text.strip().lower().split()[0]
    photo_path = os.path.join(PHOTO_DIR, f"{action}.jpg")

    if os.path.exists(photo_path):
        os.remove(photo_path)
        await update.message.reply_html(f"✅ Фото для «<b>{action}</b>» видалено.")
    else:
        await update.message.reply_html(f"ℹ️ Фото для «<b>{action}</b>» не знайдено.")

    await admin_command(update, context, from_callback=True)
    return ConversationHandler.END


@owner_only
async def add_sticker_prompt(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Просить надіслати назву-тригер для стікера."""
    query = update.callback_query
    await query.answer()
    keyboard = [[InlineKeyboardButton("✖️ Скасувати", callback_data="admin_cancel")]]
    await query.edit_message_text(
        "Надішли <b>назву-тригер</b> (1-3 слова, напр., <code>мур котик</code>), "
        "яка буде викликати стікер.",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.HTML,
    )
    return CONTENT_ADD_STICKER_AWAIT_ALIAS


@owner_only
async def process_sticker_alias(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Обробляє назву-тригер і просить надіслати стікер."""
    if not update.message or not update.message.text:
        return CONTENT_ADD_STICKER_AWAIT_ALIAS
        
    alias = update.message.text.strip().lower()
    
    if not alias:
        await update.message.reply_text("Тригер не може бути порожнім.")
        return CONTENT_ADD_STICKER_AWAIT_ALIAS
        
    if len(alias.split()) > 3:
        await update.message.reply_text("Помилка: тригер має більше 3 слів. Дозволено максимум 3.")
        return CONTENT_ADD_STICKER_AWAIT_ALIAS

    context.user_data["sticker_alias_to_add"] = alias
    keyboard = [[InlineKeyboardButton("✖️ Скасувати", callback_data="admin_cancel")]]
    await update.message.reply_html(
        f"Добре. Тепер надішли мені <b>стікер</b>, щоб прив'язати його до «<b>{alias}</b>».",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return CONTENT_ADD_STICKER_AWAIT_STICKER


@owner_only
async def process_sticker_add(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Обробляє стікер і зберігає його."""
    message = update.message
    alias = context.user_data.get("sticker_alias_to_add")
    
    if not message or not message.sticker:
        await message.reply_text("Це не стікер. Будь ласка, надішли стікер.")
        return CONTENT_ADD_STICKER_AWAIT_STICKER
        
    if not alias:
        await message.reply_text("Помилка: загубилася назва-тригер. Почніть спочатку.")
        return await cancel_action(update, context)

    file_uid = update.message.sticker.file_unique_id
    await save_sticker(alias, file_uid)
    await update.message.reply_html(f"✅ Стікер для «<b>{alias}</b>» успішно додано!")
    
    context.user_data.pop("sticker_alias_to_add", None)
    await admin_command(update, context, from_callback=True)
    return ConversationHandler.END


@owner_only
async def remove_sticker_prompt(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Просить назву-тригер для видалення стікера."""
    query = update.callback_query
    await query.answer()
    keyboard = [[InlineKeyboardButton("✖️ Скасувати", callback_data="admin_cancel")]]
    await query.edit_message_text(
        "Надішли <b>назву-тригер</b> (напр., <code>мур котик</code>), "
        "який потрібно <b>видалити</b>.",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.HTML,
    )
    return CONTENT_REMOVE_STICKER_AWAIT_NAME


@owner_only
async def process_remove_sticker(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Обробляє назву та видаляє стікер."""
    if not update.message or not update.message.text:
        return CONTENT_REMOVE_STICKER_AWAIT_NAME

    alias = update.message.text.strip().lower()
    await remove_sticker_db(alias)
    await update.message.reply_html(
        f"✅ Стікер «<b>{alias}</b>» видалено (якщо він існував)."
    )

    await admin_command(update, context, from_callback=True)
    return ConversationHandler.END


@owner_only
async def show_all_stickers(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """(НОВА ФУНКЦІЯ) Показує список усіх збережених стікер-тригерів."""
    query = update.callback_query
    await query.answer("Шукаю стікери...")
    
    try:
        all_stickers = await get_all_stickers()
        
        if not all_stickers:
            text = "<b>✨ Список Стікерів:</b>\n\n<i>Немає збережених стікерів.</i>"
        else:
            text = "<b>✨ Список Стікерів (Тригери):</b>\n\n"
            all_stickers.sort(key=lambda x: x['keyword'])
            for sticker in all_stickers:
                text += f"- <code>{html.escape(sticker['keyword'])}</code>\n"
            text += "\n<i>(file_id приховані для стислості)</i>"
                
        keyboard = [[InlineKeyboardButton("↩️ Назад", callback_data="admin_content_menu")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)
        
    except Exception as e:
        logger.error(f"Помилка при отриманні списку стікерів: {e}", exc_info=True)
        await query.edit_message_text("❌ Помилка при отриманні даних.")


# =============================================================================
# 5. Розсилка
# =============================================================================


@owner_only
async def start_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Ініціює розсилку: просить надіслати повідомлення."""
    query = update.callback_query
    await query.answer()
    context.user_data["broadcast_message"] = None
    keyboard = [[InlineKeyboardButton("✖️ Скасувати", callback_data="admin_cancel")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text(
        text="Будь ласка, надішліть повідомлення для розсилки (фото, текст, стікер — все, що завгодно).",
        reply_markup=reply_markup,
    )
    return BROADCAST_MESSAGE


@owner_only
async def receive_broadcast_message(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Отримує повідомлення для розсилки та просить підтвердження."""
    message: Optional[Message] = update.effective_message
    if not message:
        return ConversationHandler.END

    context.user_data["broadcast_message"] = message

    keyboard = [
        [
            InlineKeyboardButton(
                "✅ Підтвердити і надіслати", callback_data="admin_broadcast_confirm"
            ),
            InlineKeyboardButton("✖️ Скасувати", callback_data="admin_cancel"),
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await context.bot.copy_message(
        chat_id=message.chat.id,
        from_chat_id=message.chat.id,
        message_id=message.message_id,
        caption=(message.caption or "")
        + "\n\n<b>Надіслати це повідомлення всім чатам та послідовникам?</b>",
        reply_markup=reply_markup,
        parse_mode=ParseMode.HTML,
    )
    return BROADCAST_CONFIRM


@owner_only
async def send_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Виконує розсилку повідомлення."""
    user_id = update.effective_user.id
    query = update.callback_query
    await query.answer()
    broadcast_message: Optional[Message] = context.user_data.get("broadcast_message")

    async def _safe_edit(text: str) -> None:
        """Редагує текст або підпис, залежно від типу повідомлення."""
        try:
            if query.message and query.message.text:
                await query.edit_message_text(text, reply_markup=None)
            else:
                await query.edit_message_caption(text, reply_markup=None)
        except BadRequest as e:
            logger.warning(f"Не вдалося відредагувати повідомлення розсилки: {e}")

    if not broadcast_message:
        await _safe_edit("Помилка: повідомлення не знайдено.")
        return ConversationHandler.END

    await _safe_edit("Починаю розсилку... 💌")

    all_chats_info = await get_all_chats(page_size=None)
    chat_ids_to_broadcast = {chat["chat_id"] for chat in all_chats_info}
    all_users_ids = await get_all_user_ids()
    user_ids_to_broadcast = set(all_users_ids)
    target_ids = chat_ids_to_broadcast.union(user_ids_to_broadcast)

    if user_id in target_ids:
        target_ids.remove(user_id)

    success_count, fail_count = 0, 0
    for target_chat_id in target_ids:
        try:
            await context.bot.copy_message(
                chat_id=target_chat_id,
                from_chat_id=broadcast_message.chat.id,
                message_id=broadcast_message.message_id,
            )
            success_count += 1
            await asyncio.sleep(0.05)
        except (Forbidden, BadRequest, TelegramError) as e:
            logger.warning(
                f"Помилка розсилки в чат/користувача {target_chat_id}: {e}"
            )
            fail_count += 1

    await context.bot.send_message(
        chat_id=user_id,
        text=f"Розсилку завершено! 😼\nУспішно: <b>{success_count}</b>\nПомилки: <b>{fail_count}</b>",
        parse_mode=ParseMode.HTML,
    )
    context.user_data.pop("broadcast_message", None)
    return ConversationHandler.END


# =============================================================================
# 6. Обслуговування
# =============================================================================


@owner_only
async def maintenance_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Меню обслуговування ботом."""
    query = update.callback_query
    await query.answer()
    keyboard = [
        [
            InlineKeyboardButton(
                "🗑️ Очистити історію розмов", callback_data="admin_maint_clear_convos"
            )
        ],
        [
            InlineKeyboardButton(
                "✝️ Запустити 'Монашку Дня'", callback_data="admin_maint_run_nun"
            )
        ],
        [
            InlineKeyboardButton(
                "🌠 Запустити 'Передбачення'", callback_data="admin_maint_run_preds"
            )
        ],
        [
            InlineKeyboardButton(
                "🔄 Перезавантажити (Сигнал)", callback_data="admin_maint_reboot"
            )
        ],
        [InlineKeyboardButton("↩️ Назад до адмін-меню", callback_data="admin_menu")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text(
        "<b>🛠️ Обслуговування Кота:</b>\n\nОберіть інструмент для прямого керування.",
        reply_markup=reply_markup,
        parse_mode=ParseMode.HTML,
    )


@owner_only
async def clear_convos_prompt(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Просить підтвердження очищення історії розмов."""
    query = update.callback_query
    await query.answer()
    keyboard = [
        [
            InlineKeyboardButton(
                "🔴 Так, очистити все", callback_data="admin_maint_clear_convos_confirm"
            )
        ],
        [InlineKeyboardButton("🟢 Ні, скасувати", callback_data="admin_maint_menu")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text(
        "<b>⚠️ Ви впевнені?</b>\n\nЦя дія видалить <b>ВСЮ</b> історію розмов з ботом з бази даних. Це не можна буде скасувати.",
        reply_markup=reply_markup,
        parse_mode=ParseMode.HTML,
    )


@owner_only
async def clear_convos_confirm(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Виконує очищення історії розмов."""
    query = update.callback_query
    await query.answer("Очищення...", show_alert=False)
    await clear_conversations()
    logger.info(f"Адмін {OWNER_ID} очистив історію розмов.")
    await query.edit_message_text("✅ Історія розмов успішно очищена.")
    await asyncio.sleep(2)
    await maintenance_menu(update, context)


@owner_only
async def manual_nun_of_the_day(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """(Admin) Вручну запускає функцію "Монашка дня"."""
    query = update.callback_query
    await query.answer("Запускаю 'Монашку дня' вручну...", show_alert=True)
    logger.info(f"Власник {OWNER_ID} викликав 'Монашку дня' вручну.")
    try:
        await nun_of_the_day_job(context)
        await context.bot.send_message(
            OWNER_ID, "✅ Виконання 'Монашки дня' завершено."
        )
    except Exception as e:
        logger.error(f"Помилка ручного запуску 'Монашки дня': {e}", exc_info=True)
        await context.bot.send_message(
            OWNER_ID, f"❌ Помилка ручного запуску 'Монашки дня':\n<pre>{e}</pre>", parse_mode=ParseMode.HTML
        )


@owner_only
async def manual_predictions(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """(Admin) Вручну запускає функцію "Призначення Передбачень"."""
    query = update.callback_query
    await query.answer("Запускаю 'Призначення Передбачень'...", show_alert=True)
    logger.info(f"Власник {OWNER_ID} викликав 'Передбачення' вручну.")
    try:
        await assign_daily_predictions_job(context)
        await context.bot.send_message(
            OWNER_ID, "✅ Виконання 'Призначення Передбачень' завершено."
        )
    except Exception as e:
        logger.error(f"Помилка ручного запуску 'Передбачень': {e}", exc_info=True)
        await context.bot.send_message(
            OWNER_ID,
            f"❌ Помилка ручного запуску 'Передбачень':\n<pre>{e}</pre>",
            parse_mode=ParseMode.HTML,
        )


@owner_only
async def reboot_bot(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Сигналізує про необхідність перезавантаження бота."""
    query = update.callback_query
    await query.answer("Надсилаю сигнал до перезавантаження... 🔄", show_alert=True)
    logger.critical(f"Адмін {OWNER_ID} ініціював перезавантаження.")
    await context.bot.send_message(
        OWNER_ID, "🔄 Бот перезавантажується... Буду на зв'язку за мить!"
    )
    
    asyncio.create_task(context.application.stop())


# =============================================================================
# 7. (НОВЕ) Керування Модами
# =============================================================================

@owner_only
async def mode_management_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """(НОВЕ) Меню керування глобальними модами (темами) бота."""
    query = update.callback_query
    await query.answer()
    
    try:
        current_mode = await get_global_bot_mode()
    except Exception as e:
        logger.error(f"Не вдалося отримати поточний мод: {e}", exc_info=True)
        current_mode = BotTheme.DEFAULT
        await query.edit_message_text(f"❌ Помилка отримання моду: {e}")
        await asyncio.sleep(2)
        await admin_command(update, context, from_callback=True)
        return

    mode_text = "🌿 Монастир (Default)"
    if current_mode == BotTheme.WINTER:
        mode_text = "❄️ Зимовий (Winter)"

    text = (
        f"<b>🎭 Керування Модами</b>\n\n"
        f"Оберіть, який настрій сьогодні у котика. 🐈\n"
        f"Поточний мод: <b>{mode_text}</b>\n\n"
        f"<i>Зміна моду оновить тексти, емодзі та промпти ШІ для всіх користувачів.</i>"
    )
    
    keyboard = [
        [
            InlineKeyboardButton(
                f"{'✅' if current_mode == BotTheme.DEFAULT else '🌿'} Монастир (Default)", 
                callback_data=f"admin_mode_set_{BotTheme.DEFAULT}"
            ),
        ],
        [
            InlineKeyboardButton(
                f"{'✅' if current_mode == BotTheme.WINTER else '❄️'} Зимовий (Winter)", 
                callback_data=f"admin_mode_set_{BotTheme.WINTER}"
            ),
        ],
        [InlineKeyboardButton("↩️ Назад", callback_data="admin_menu")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(
        text, reply_markup=reply_markup, parse_mode=ParseMode.HTML
    )

@owner_only
async def set_bot_mode(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """(НОВЕ) Встановлює обраний мод і оновлює кеш."""
    query = update.callback_query
    
    try:
        mode_name = query.data.split("_")[-1]
        if mode_name not in [BotTheme.DEFAULT, BotTheme.WINTER]:
            logger.warning(f"Отримана невірна назва моду: {mode_name}")
            await query.answer("❌ Помилка: Невірний мод.", show_alert=True)
            return

        current_mode = await get_global_bot_mode()
        if current_mode == mode_name:
            await query.answer("Цей мод вже активовано.", show_alert=False)
            return

        await query.answer(f"Перемикаю мод на {mode_name}...")
        
        # 1. Встановити в БД
        await set_global_bot_mode(mode_name)
        
        # 2. Оновити кеш (з utils.py)
        await refresh_theme_cache()
        
        # 3. (НОВЕ) Оновити іконки та значення в інших модулях
        try:
            from bot.games.tic_tac_toe_game import Style
            from bot.utils.utils import get_icon
            Style.PLAYER_X = await get_icon("icon_player_x")
            Style.PLAYER_O = await get_icon("icon_player_o")
            Style.EMPTY_CELL = await get_icon("icon_empty")
        except Exception as e:
            logger.warning(f"Не вдалося оновити іконки tic_tac_toe: {e}")
        
        # Оновити казино
        try:
            from bot.handlers.casino_handlers import initialize_casino
            await initialize_casino()
        except Exception as e:
            logger.warning(f"Не вдалося оновити казино: {e}")
        
        # Оновити вартість одруження в marriage_handlers
        try:
            from marriage import marriage_handlers
            from bot.utils.utils import get_marriage_cost
            marriage_handlers.MARRIAGE_COST = await get_marriage_cost()
        except Exception as e:
            logger.warning(f"Не вдалося оновити вартість одруження: {e}")
        
        logger.info(f"Власник {OWNER_ID} змінив мод на {mode_name}")
        
        # 4. Показати оновлене меню
        await mode_management_menu(update, context)
        
    except Exception as e:
        logger.error(f"Помилка при зміні моду: {e}", exc_info=True)
        await query.answer("❌ Сталася помилка при оновленні.", show_alert=True)


# =============================================================================
# 8. Реєстрація обробників
# =============================================================================


def register_admin_handlers(application: Application):
    """Реєструє всі обробники команд адміністратора."""

    # --- Секретна активація (вже в main.py) ---
    # `secret_admin_trigger` має бути зареєстрована в main.py з group=1

    # --- Скасування ---
    cancel_handler = CallbackQueryHandler(
        cancel_action, pattern="^admin_cancel$"
    )

    # --- Розсилка ---
    broadcast_conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(start_broadcast, pattern="^admin_broadcast_start$")
        ],
        states={
            BROADCAST_MESSAGE: [
                MessageHandler(
                    filters.ALL & ~filters.COMMAND, receive_broadcast_message
                ),
            ],
            BROADCAST_CONFIRM: [
                CallbackQueryHandler(
                    send_broadcast, pattern="^admin_broadcast_confirm$"
                ),
            ],
        },
        fallbacks=[cancel_handler],
        conversation_timeout=600,
    )

    # --- Керування Користувачами ---
    user_info_conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(get_user_info_prompt, pattern="^admin_user_info$")
        ],
        states={
            GET_USER_ID_INFO: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, process_user_id_for_info)
            ]
        },
        fallbacks=[cancel_handler],
    )

    change_balance_conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(change_balance_prompt, pattern="^admin_user_balance$")
        ],
        states={
            GET_USER_ID_BALANCE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, process_user_id_for_balance)
            ],
            GET_BALANCE_AMOUNT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, process_balance_amount)
            ],
        },
        fallbacks=[cancel_handler],
    )

    change_stats_conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(change_game_stats_prompt, pattern="^admin_user_stats$")
        ],
        states={
            GET_USER_ID_STATS: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, process_user_id_for_stats)
            ],
            GET_CHAT_ID_STATS: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, process_chat_id_for_stats)
            ],
            GET_STATS_VALUES: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, process_stats_values)
            ],
        },
        fallbacks=[cancel_handler],
    )

    ban_user_conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(ban_user_prompt, pattern="^admin_user_ban$")
        ],
        states={
            GET_USER_ID_BAN: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, process_user_id_for_ban)
            ],
            BAN_CONFIRM: [
                CallbackQueryHandler(process_ban_confirm, pattern="^admin_ban_")
            ],
        },
        fallbacks=[cancel_handler],
    )

    send_message_conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(send_message_prompt, pattern="^admin_user_msg$")
        ],
        states={
            GET_USER_ID_MESSAGE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, send_message_get_chat_id)
            ],
            GET_MESSAGE_TEXT: [
                MessageHandler(filters.ALL & ~filters.COMMAND, send_message_execute)
            ],
        },
        fallbacks=[cancel_handler],
    )

    # --- Керування Контентом ---
    add_photo_conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(add_photo_prompt, pattern="^admin_content_add_photo$")
        ],
        states={
            CONTENT_ADD_PHOTO_AWAIT_IMG: [
                MessageHandler(filters.PHOTO | filters.Document.IMAGE, process_add_photo)
            ]
        },
        fallbacks=[cancel_handler],
    )

    remove_photo_conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(remove_photo_prompt, pattern="^admin_content_rem_photo$")
        ],
        states={
            CONTENT_REMOVE_PHOTO_AWAIT_NAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, process_remove_photo)
            ]
        },
        fallbacks=[cancel_handler],
    )

    add_sticker_conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(add_sticker_prompt, pattern="^admin_content_add_sticker$")
        ],
        states={
            CONTENT_ADD_STICKER_AWAIT_ALIAS: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, process_sticker_alias)
            ],
            CONTENT_ADD_STICKER_AWAIT_STICKER: [
                MessageHandler(filters.Sticker.ALL, process_sticker_add)
            ],
        },
        fallbacks=[cancel_handler],
    )

    remove_sticker_conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(
                remove_sticker_prompt, pattern="^admin_content_rem_sticker$"
            )
        ],
        states={
            CONTENT_REMOVE_STICKER_AWAIT_NAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, process_remove_sticker)
            ]
        },
        fallbacks=[cancel_handler],
    )

    # --- Реєстрація ConversationHandlers ---
    conv_handlers = [
        broadcast_conv,
        user_info_conv,
        change_balance_conv,
        change_stats_conv,
        ban_user_conv,
        send_message_conv,
        add_photo_conv,
        remove_photo_conv,
        add_sticker_conv,
        remove_sticker_conv,
    ]
    for handler in conv_handlers:
        application.add_handler(handler)

    # --- Реєстрація CallbackHandlers (Меню) ---
    application.add_handler(
        CallbackQueryHandler(
            lambda u, c: admin_command(u, c, from_callback=True),
            pattern="^admin_menu$",
        )
    )
    
    # (Нове) Списки & Статистика
    application.add_handler(
        CallbackQueryHandler(admin_lists_menu, pattern="^admin_lists_menu$")
    )
    application.add_handler(
        CallbackQueryHandler(show_user_list, pattern=r"^admin_list_users_\d+$")
    )
    application.add_handler(
        CallbackQueryHandler(show_chat_list, pattern=r"^admin_list_chats_\d+$")
    )
    
    # Статистика (тепер всередині admin_lists_menu)
    application.add_handler(
        CallbackQueryHandler(show_statistics_command, pattern="^admin_stats$")
    )
    
    # Головне меню
    application.add_handler(
        CallbackQueryHandler(user_management_menu, pattern="^admin_user_menu$")
    )
    application.add_handler(
        CallbackQueryHandler(ai_control_menu, pattern="^admin_ai_menu$")
    )
    application.add_handler(
        CallbackQueryHandler(content_management_menu, pattern="^admin_content_menu$")
    )
    application.add_handler(
        CallbackQueryHandler(maintenance_menu, pattern="^admin_maint_menu$")
    )

    # User-Меню (включно з новими)
    application.add_handler(
        CallbackQueryHandler(show_top_balances, pattern="^admin_user_top_balance$")
    )
    application.add_handler(
        CallbackQueryHandler(show_banned_users, pattern="^admin_user_banned_list$")
    )

    # AI-Меню
    application.add_handler(
        CallbackQueryHandler(toggle_global_ai, pattern="^admin_ai_toggle_global$")
    )
    application.add_handler(
        CallbackQueryHandler(show_ai_chats_list, pattern=r"^admin_ai_chats_list_\d+$")
    )
    application.add_handler(
        CallbackQueryHandler(toggle_chat_ai, pattern=r"^admin_ai_toggle_chat_-?\d+_\d+$")
    )

    # Content-Меню (включно з новими)
    application.add_handler(
        CallbackQueryHandler(show_all_stickers, pattern="^admin_content_list_stickers$")
    )

    # Maintenance-Меню
    application.add_handler(
        CallbackQueryHandler(clear_convos_prompt, pattern="^admin_maint_clear_convos$")
    )
    application.add_handler(
        CallbackQueryHandler(
            clear_convos_confirm, pattern="^admin_maint_clear_convos_confirm$"
        )
    )
    application.add_handler(
        CallbackQueryHandler(manual_nun_of_the_day, pattern="^admin_maint_run_nun$")
    )
    application.add_handler(
        CallbackQueryHandler(manual_predictions, pattern="^admin_maint_run_preds$")
    )
    application.add_handler(
        CallbackQueryHandler(reboot_bot, pattern="^admin_maint_reboot$")
    )
    
    # --- (НОВЕ) Керування Модами ---
    application.add_handler(
        CallbackQueryHandler(mode_management_menu, pattern="^admin_mode_menu$")
    )
    application.add_handler(
        CallbackQueryHandler(set_bot_mode, pattern=r"^admin_mode_set_")
    )
    # ---
    
    application.add_handler(cancel_handler)

    logger.info("Обробники адміністратора (admin_handlers.py) зареєстровані.")


# =============================================================================
# 9. Функція активації (для main.py)
# =============================================================================

async def secret_admin_trigger(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Ця функція викликається на ВСІ приватні повідомлення.
    Вона перевіряє, чи це власник і чи текст є секретною фразою.
    """
    if (
        not update.message
        or not update.message.text
        or not update.effective_user
    ):
        return

    # Перевіряємо ID та секретну фразу
    if (
        update.effective_user.id == OWNER_ID
        and update.message.text.strip() == "Адмін-панель котика"
    ):
        logger.info(f"Власник {OWNER_ID} активував адмін-панель через секретну фразу.")
        await admin_command(update, context, from_callback=False)