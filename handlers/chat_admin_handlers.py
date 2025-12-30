# -*- coding: utf-8 -*-

"""
chat_admin_handlers.py

Модуль для настоятелів (адміністраторів) групових чатів.
Дозволяє керувати ботом із легким котячим муркотінням. 🐾

Керування:
1. /settings в групі -> меню в ПП.
2. Команди модерації (/warn, /unwarn, /rules) в групі.
"""

import logging
import html
import asyncio
from typing import Optional, Dict, Any, Tuple, Union

from telegram import (
    Update,
    Chat,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ChatMember,
    ChatMemberAdministrator,
    ChatMemberOwner,
    User,
    CallbackQuery,
)
from telegram.ext import (
    Application,
    ContextTypes,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
)
from telegram.constants import ParseMode, ChatType
from telegram.error import BadRequest, Forbidden

# Імпортуємо необхідні функції БД
from bot.core.database import (
    get_chat_settings,
    set_module_status,
    set_chat_setting_flag,
    upsert_chat_info,
    set_chat_welcome_message,
    set_chat_rules,
    set_max_warns,
    add_filtered_word,
    remove_filtered_word,
    get_filtered_words,
    add_user_warn,
    get_user_warns,
    reset_user_warns,
    set_mems_setting_for_chat,
)

from bot.features.new_year_mode import is_in_new_year_period, format_new_year_mode


logger = logging.getLogger(__name__)

# Словник модулів та їхні назви для кнопок
MODULES_CONFIG = {
    "ai_enabled": "🤖 Штучний Інтелект",
    "commands_enabled": "💬 Команди Дій",
    "games_enabled": "🎲 Ігри",
    "marriage_enabled": "❤️ Шлюби",
    "emoji_reactions_enabled": "💬 Emoji-реакції",
    "word_filter_enabled": "🚫 Фільтр (Єресь)",
    "reminders_enabled": "⏰ Нагадування в цьому чаті",
}

# =============================================================================
# 1. Допоміжні функції
# =============================================================================

async def _safe_edit_message(query: CallbackQuery, *args, **kwargs):
    """
    Обгортка для query.edit_message_text, яка ігнорує помилки
    'Message is not modified'. Мур! 🐾
    """
    try:
        await query.edit_message_text(*args, **kwargs)
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            logger.warning(f"Unexpected BadRequest during _safe_edit_message: {e}")
        # Якщо "not modified", просто ігноруємо.
    except Exception as e:
        logger.error(f"Error in _safe_edit_message: {e}", exc_info=True)


async def is_chat_module_enabled(chat: Optional[Chat], module_key: str) -> bool:
    """
    Перевіряє, чи ввімкнено модуль для цього чату.
    'module_key' має відповідати ключу в БД (напр., 'ai_enabled' або 'games').
    """
    # Завжди ввімкнено в приватних чатах
    if not chat or chat.type == ChatType.PRIVATE:
        return True
    
    # Додаємо суфікс, якщо його немає, для сумісності
    if not module_key.endswith("_enabled"):
        module_key = f"{module_key}_enabled"

    try:
        settings = await get_chat_settings(chat.id)
        # Повертаємо 1 (ввімкнено) за замовчуванням, окрім фільтру слів
        default_val = 0 if module_key == "word_filter_enabled" else 1
        return settings.get(module_key, default_val) == 1
    except Exception as e:
        logger.error(
            f"Помилка в is_chat_module_enabled при перевірці {module_key} "
            f"для чату {chat.id}: {e}"
        )
        return True # Безпечне замовчування


async def _check_admin_rights(
    bot, user_id: int, chat_id: int, needs_ban_right: bool = True
) -> bool:
    """
    Перевіряє, чи є користувач настоятелем (адміном).
    Якщо needs_ban_right=True, перевіряє право банити.
    """
    try:
        chat_member = await bot.get_chat_member(chat_id, user_id)
        
        if isinstance(chat_member, (ChatMemberAdministrator, ChatMemberOwner)):
            # Власник чату має всі права
            if isinstance(chat_member, ChatMemberOwner):
                return True
            # Якщо потрібні права на бан (покуту)
            if needs_ban_right:
                return chat_member.can_restrict_members
            # Якщо достатньо бути просто адміном
            return True
                
        return False
    except Exception as e:
        logger.warning(
            f"Не вдалося перевірити права настоятеля для {user_id} в {chat_id}: {e}"
        )
        return False

async def _send_admin_rights_error(update: Update):
    """Надсилає повідомлення про відсутність прав."""
    try:
        sent_msg = await update.message.reply_text(
            "Ця команда доступна тільки настоятелям чату (адмінам). 🌿"
        )
        await asyncio.sleep(5)
        # Спробуємо видалити повідомлення користувача та бота, щоб не смітити
        try:
            await update.message.delete()
            await sent_msg.delete()
        except Exception:
            pass
    except Exception:
        pass

async def _get_target_user(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> Tuple[Optional[User], Optional[str]]:
    """
    Визначає ціль команди (з реплаю, @username або user_id).
    Повертає (User, error_message).
    """
    if update.message.reply_to_message:
        return update.message.reply_to_message.from_user, None
        
    if not context.args:
        return None, "Потрібно відповісти на повідомлення або вказати ID. 🌿"
        
    target_arg = context.args[0]
    
    # Спроба знайти за @username (ненадійно, але спробуємо)
    if target_arg.startswith("@"):
        return None, "Не можу знайти за @username. 😿 Використовуйте ID або реплай."
             
    # Спроба знайти за ID
    try:
        user_id = int(target_arg)
        target_user = await context.bot.get_chat(user_id)
        if isinstance(target_user, User):
            return target_user, None
        # get_chat може повернути Chat об'єкт для юзера, спробуємо привести
        if isinstance(target_user, Chat) and target_user.type == ChatType.PRIVATE:
             # Створюємо схожий на User об'єкт, якщо це можливо, або просто використовуємо дані
             return User(id=target_user.id, first_name=target_user.first_name, username=target_user.username, is_bot=False), None
        else:
            return None, "Це ID каналу/чату, а не користувача. 🌿"
    except ValueError:
        return None, "Не можу розпізнати ID. 😿"
    except Exception as e:
        logger.warning(f"Помилка _get_target_user при пошуку ID {target_arg}: {e}")
        return None, f"Не вдалося знайти користувача з ID {target_arg}. 😿"

# =============================================================================
# 2. Обробники Команд
# =============================================================================

async def adminhelp_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Відправляє коротку інструкцію для адміністраторів по налаштуванню чату.
    """
    help_text = (
        "<b>👑 Довідка для настоятелів (адмінів)</b>\n\n"
        "• <b>/settings</b> — відкриває меню налаштувань чату (в ПП).\n"
        "• <b>/rules</b> — показати правила чату.\n"
        "• <b>/warn</b>, <b>/unwarn</b>, <b>/warns</b> — керування попередженнями.\n"
        "• <b>Модулі</b> — вмикайте/вимикайте AI, ігри, шлюби, emoji-реакції тощо.\n"
        "• <b>Устав</b> — змінюйте привітання, правила, ліміт варнів.\n\n"
        "<b>Підказка:</b> Всі налаштування зберігаються для кожного чату окремо!\n"
        "<b>Права:</b> Для більшості дій потрібні права адміністратора."
    )
    await update.message.reply_html(help_text)

async def warn_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/warn - Додає попередження користувачу."""
    chat = update.effective_chat
    admin = update.effective_user
    
    # 1. Перевірка прав (треба право банити)
    if not await _check_admin_rights(context.bot, admin.id, chat.id, needs_ban_right=True):
        await _send_admin_rights_error(update)
        return
        
    # 2. Визначення цілі
    target_user, error_msg = await _get_target_user(update, context)
    if error_msg:
        await update.message.reply_text(error_msg, quote=True)
        return
        
    # 3. Перевірка, чи не адмін
    if await _check_admin_rights(context.bot, target_user.id, chat.id, needs_ban_right=False):
        await update.message.reply_text("Не можу попередити іншого настоятеля. 🕊️", quote=True)
        return
        
    # 4. Отримуємо налаштування та додаємо варн
    settings = await get_chat_settings(chat.id)
    max_warns = settings.get('max_warns', 3)
    new_warn_count = await add_user_warn(chat.id, target_user.id)
    
    target_mention = f"<a href='tg://user?id={target_user.id}'>{html.escape(target_user.first_name)}</a>"
    
    # 5. Дія
    if new_warn_count >= max_warns:
        logger.info(f"Настоятель {admin.id} заблокував {target_user.id} в чаті {chat.id} (досягнуто ліміту варнів).")
        try:
            await context.bot.ban_chat_member(chat.id, target_user.id)
            await reset_user_warns(chat.id, target_user.id) # Очищуємо після бану
            await update.message.reply_html(
                f"⚠️ {target_mention} отримує останнє попередження ({new_warn_count}/{max_warns}) "
                f"і вирушає на покуту (<b>бан</b>)."
            )
        except Exception as e:
            await update.message.reply_html(f"Не вдалося заблокувати {target_mention}: {e}")
    else:
        logger.info(f"Настоятель {admin.id} попередив {target_user.id} в чаті {chat.id}.")
        await update.message.reply_html(
            f"⚠️ {target_mention} отримує попередження. "
            f"Поточний стан: <b>{new_warn_count}/{max_warns}</b>."
        )

async def unwarn_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/unwarn - Знімає всі попередження."""
    chat = update.effective_chat
    admin = update.effective_user
    
    if not await _check_admin_rights(context.bot, admin.id, chat.id, needs_ban_right=True):
        await _send_admin_rights_error(update)
        return
        
    target_user, error_msg = await _get_target_user(update, context)
    if error_msg:
        await update.message.reply_text(error_msg, quote=True)
        return
        
    await reset_user_warns(chat.id, target_user.id)
    target_mention = f"<a href='tg://user?id={target_user.id}'>{html.escape(target_user.first_name)}</a>"
    
    settings = await get_chat_settings(chat.id)
    max_warns = settings.get('max_warns', 3)
    
    await update.message.reply_html(f"🌿 Усі попередження для {target_mention} знято (0/{max_warns}). Душа очищена. 🕊️")

async def warns_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/warns - Перевіряє кількість попереджень."""
    chat = update.effective_chat
    admin = update.effective_user
    
    if not await _check_admin_rights(context.bot, admin.id, chat.id, needs_ban_right=False):
        await _send_admin_rights_error(update)
        return
        
    target_user, error_msg = await _get_target_user(update, context)
    if error_msg:
        await update.message.reply_text(error_msg, quote=True)
        return

    warn_count = await get_user_warns(chat.id, target_user.id)
    settings = await get_chat_settings(chat.id)
    max_warns = settings.get('max_warns', 3)
    target_mention = f"<a href='tg://user?id={target_user.id}'>{html.escape(target_user.first_name)}</a>"
    
    await update.message.reply_html(f"Кількість попереджень для {target_mention}: <b>{warn_count}/{max_warns}</b>. 📜")

async def rules_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/rules - Показує правила чату (устав)."""
    chat = update.effective_chat
    settings = await get_chat_settings(chat.id)
    rules = settings.get('rules')
    
    if rules:
        await update.message.reply_html(f"<b>📜 Устав (правила) чату:</b>\n\n{rules}", disable_web_page_preview=True)
    else:
        await update.message.reply_html("📜 Настоятелі ще не встановили устав (правила) для цього чату. 🌿")

# =============================================================================
# 3. Обробники Меню Налаштувань (в ПП)
# =============================================================================

async def settings_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Обробляє /settings. (Викликається ТІЛЬКИ в групових чатах).
    Надсилає меню в ПП.
    """
    if not update.message or not update.effective_user or not update.effective_chat:
        return

    user = update.effective_user
    chat = update.effective_chat
    
    await upsert_chat_info(chat.id, chat.type, chat.title, chat.username)

    # 1. Перевіряємо права (тут достатньо бути адміном)
    if not await _check_admin_rights(context.bot, user.id, chat.id, needs_ban_right=False):
        await _send_admin_rights_error(update)
        return

    # 2. Генеруємо Головне Меню
    try:
        reply_markup = await _build_main_menu(chat.id)
        text = (
            f"<b>⚙️ Келія Настоятеля:</b>\n"
            f"<i>{html.escape(chat.title or 'Цей чат')}</i>\n\n"
            "Мур... purr... 🐾 Вітаю. Оберіть розділ:"
        )

        # 3. Намагаємось надіслати в ПП
        await context.bot.send_message(
            chat_id=user.id,
            text=text,
            reply_markup=reply_markup,
            parse_mode=ParseMode.HTML
        )
        
        sent_msg = await update.message.reply_text(
            f"{user.first_name}, я відправила налаштування вам у приватні повідомлення. Мур! 🐾"
        )
        await asyncio.sleep(5)
        try:
            await update.message.delete()
            await sent_msg.delete()
        except Exception:
            pass

    except Forbidden:
        sent_msg = await update.message.reply_html(
            f"{user.first_name}, ой, не можу вам написати. 😿\n"
            f"Будь ласка, почніть діалог зі мною (@{context.bot.username}) "
            "та натисніть 'Start', а потім повертайтесь. 🌿"
        )
        await asyncio.sleep(10)
        try:
            await update.message.delete()
            await sent_msg.delete()
        except Exception:
            pass
    except Exception as e:
        logger.error(f"Помилка при відправці /settings меню: {e}", exc_info=True)

# --- Побудова Меню (Стилізовано) ---

async def _build_main_menu(chat_id: int) -> InlineKeyboardMarkup:
    """Будує Головне Меню."""
    keyboard = [
        [
            InlineKeyboardButton("📜 Налаштування", callback_data=f"admin_chat_settings_{chat_id}"),
            InlineKeyboardButton("🐈‍⬛ Модулі", callback_data=f"admin_chat_modules_{chat_id}"),
        ],
        [
            InlineKeyboardButton("⚖️ Модерація", callback_data=f"admin_chat_moderation_{chat_id}"),
            InlineKeyboardButton("🎮 Мемчики", callback_data=f"admin_chat_mems_{chat_id}"),
        ],
        [InlineKeyboardButton("✨ Оновити", callback_data=f"admin_chat_main_{chat_id}")],
    ]
    return InlineKeyboardMarkup(keyboard)

async def _build_modules_menu(chat_id: int) -> InlineKeyboardMarkup:
    """Будує Меню Модулів."""
    settings = await get_chat_settings(chat_id)
    keyboard = []
    
    for key, name in MODULES_CONFIG.items():
        default_val = 0 if key == "word_filter_enabled" else 1
        is_enabled = settings.get(key, default_val) == 1
        
        emoji = "✅" if is_enabled else "❌"
        keyboard.append([
            InlineKeyboardButton(
                f"{emoji} {name}",
                callback_data=f"admin_chat_toggle_{key}_{chat_id}"
            )
        ])
    
    keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data=f"admin_chat_main_{chat_id}")])
    return InlineKeyboardMarkup(keyboard)

async def _build_settings_menu(chat_id: int) -> InlineKeyboardMarkup:
    """Будує Меню Налаштувань Чату."""
    settings = await get_chat_settings(chat_id)
    
    auto_delete_actions_enabled = (settings.get('auto_delete_actions', 0) == 1)
    auto_delete_status = 'ON ✅' if auto_delete_actions_enabled else 'OFF ❌'
    
    ai_auto_clear_enabled = (settings.get('ai_auto_clear_conversations', 0) == 1)
    ai_auto_clear_status = 'ON ✅' if ai_auto_clear_enabled else 'OFF ❌'


    keyboard = [

        [
            InlineKeyboardButton(f"🧹 AI автоочистка 10 хв · {ai_auto_clear_status}", callback_data=f"admin_chat_toggle_ai_auto_clear_conversations_{chat_id}"),
        ],
        [
            InlineKeyboardButton(f"🗑 Дії · {auto_delete_status}", callback_data=f"admin_chat_toggle_auto_delete_actions_{chat_id}"),
        ],
        [InlineKeyboardButton("⬅️ Назад", callback_data=f"admin_chat_main_{chat_id}")],
    ]
    return InlineKeyboardMarkup(keyboard)


async def _build_mems_settings_menu(chat_id: int) -> InlineKeyboardMarkup:
    """Будує меню налаштувань гри "Мемчики та котики"."""
    settings = await get_chat_settings(chat_id)
    turn_time = int(settings.get("mems_turn_time", 60) or 60)
    vote_time = int(settings.get("mems_vote_time", 45) or 45)
    max_players = int(settings.get("mems_max_players", 10) or 10)
    win_score = int(settings.get("mems_win_score", 10) or 10)
    hand_size = int(settings.get("mems_hand_size", 6) or 6)
    registration_time = int(settings.get("mems_registration_time", 120) or 120)

    # UX: натиснув параметр -> бачиш ВСІ варіанти (без циклічного перемикання)
    keyboard = [
        [InlineKeyboardButton(f"⏱ Хід: {turn_time}с", callback_data=f"admin_chat_mems_choose_turn_time_{chat_id}")],
        [InlineKeyboardButton(f"🗳 Голос: {vote_time}с", callback_data=f"admin_chat_mems_choose_vote_time_{chat_id}")],
        [InlineKeyboardButton(f"👥 Гравців: до {max_players}", callback_data=f"admin_chat_mems_choose_max_players_{chat_id}")],
        [InlineKeyboardButton(f"🏆 До: {win_score} очок", callback_data=f"admin_chat_mems_choose_win_score_{chat_id}")],
        [InlineKeyboardButton(f"⏱ Реєстрація: {registration_time}с", callback_data=f"admin_chat_mems_choose_registration_time_{chat_id}")],
        [InlineKeyboardButton(f"🃏 В лапці: {hand_size}", callback_data=f"admin_chat_mems_choose_hand_size_{chat_id}")],
        [InlineKeyboardButton("⬅️ Назад", callback_data=f"admin_chat_settings_{chat_id}")],
    ]
    return InlineKeyboardMarkup(keyboard)


async def _build_mems_choose_menu(chat_id: int, key: str) -> InlineKeyboardMarkup:
    """Показує список всіх варіантів для конкретного параметра мемчиків."""
    settings = await get_chat_settings(chat_id)

    current_map = {
        "turn_time": int(settings.get("mems_turn_time", 60) or 60),
        "vote_time": int(settings.get("mems_vote_time", 45) or 45),
        "max_players": int(settings.get("mems_max_players", 10) or 10),
        "win_score": int(settings.get("mems_win_score", 10) or 10),
        "hand_size": int(settings.get("mems_hand_size", 6) or 6),
        "registration_time": int(settings.get("mems_registration_time", 120) or 120),
    }

    presets = {
        "turn_time": [30, 45, 60, 75, 90],
        "vote_time": [20, 30, 45, 60],
        "max_players": [4, 6, 8, 10, 12, 16],
        "win_score": [5, 8, 10, 12, 15],
        "hand_size": [4, 5, 6, 7, 8],
        "registration_time": [30, 60, 90, 120, 180, 240],
    }

    labels = {
        "turn_time": "⏱ Час ходу (сек)",
        "vote_time": "🗳 Час голосування (сек)",
        "max_players": "👥 Макс. гравців",
        "win_score": "🏆 До скількох очок",
        "hand_size": "🃏 Карт в лапці",
        "registration_time": "⏱ Час реєстрації (сек)",
    }

    cur = current_map.get(key)
    options = presets.get(key, [])

    keyboard = []
    for v in options:
        mark = "✅" if v == cur else "▫️"
        title = f"{mark} {v}"
        keyboard.append([InlineKeyboardButton(title, callback_data=f"admin_chat_mems_set_{key}_{v}_{chat_id}")])

    keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data=f"admin_chat_mems_{chat_id}")])
    return InlineKeyboardMarkup(keyboard)

async def _build_moderation_menu(chat_id: int) -> InlineKeyboardMarkup:
    """Будує Меню Модерації."""
    words = await get_filtered_words(chat_id)
    settings = await get_chat_settings(chat_id)
    welcome_status = "Встановлено 🥰" if settings.get('welcome_message') else "Немає 🌿"
    rules_status = "Встановлено 📜" if settings.get('rules') else "Немає 🌿"
    keyboard = [
        [InlineKeyboardButton(f"👋 Привітання · {welcome_status}", callback_data=f"admin_chat_set_welcome_{chat_id}")],
        [InlineKeyboardButton(f"📜 Правила · {rules_status}", callback_data=f"admin_chat_set_rules_{chat_id}")],
        [InlineKeyboardButton(f"⚖️ Ліміт варнів · {settings.get('max_warns', 3)}", callback_data=f"admin_chat_set_warns_{chat_id}")],
        [InlineKeyboardButton(f"🗒️ Список фільтрів ({len(words)})", callback_data=f"admin_chat_list_words_{chat_id}")],
        [InlineKeyboardButton("➕ Додати єресь", callback_data=f"admin_chat_add_word_{chat_id}")],
        [InlineKeyboardButton("➖ Пробачити (видалити)", callback_data=f"admin_chat_del_word_{chat_id}")],
        [InlineKeyboardButton("⬅️ Назад", callback_data=f"admin_chat_main_{chat_id}")],
    ]
    return InlineKeyboardMarkup(keyboard)

# --- Обробники кнопок (Callbacks) ---

async def admin_chat_callback_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Єдиний роутер для всіх кнопок адмін-меню."""
    query = update.callback_query
    if not query or not query.data:
        return
        
    await query.answer()
    
    parts = query.data.split("_")
    if len(parts) < 3:
        return
        
    action_type = parts[2]
    try:
        chat_id = int(parts[-1])
    except (ValueError, IndexError):
        await _safe_edit_message(query, "Ой. 😿 Невірний ID чату.")
        return

    # 0. Перевірка прав (чи юзер досі адмін)
    if not await _check_admin_rights(context.bot, query.from_user.id, chat_id, needs_ban_right=False):
        await _safe_edit_message(query,
            "Мур... Схоже, ви більше не настоятель у цьому чаті. 🌿 Меню неактивне."
        )
        return
        
    # Отримуємо заголовок
    try:
        chat = await context.bot.get_chat(chat_id)
        title = f"<b>⚙️ Келія Настоятеля:</b>\n<i>{html.escape(chat.title or 'Цей чат')}</i>\n\n"
    except Exception:
        title = "<b>⚙️ Керування чатом (не вдалося отримати назву)</b>\n\n"
        
    # 1. Навігація
    if action_type == "main":
        await _safe_edit_message(query, title + "Мур... 🐾 Оберіть розділ:", reply_markup=await _build_main_menu(chat_id), parse_mode=ParseMode.HTML)
    
    elif action_type == "modules":
        await _safe_edit_message(query, title + "Керуйте модулями (кігтик 'вкл' / 'викл') 🐈‍⬛", reply_markup=await _build_modules_menu(chat_id), parse_mode=ParseMode.HTML)

    elif action_type == "settings":
        await _safe_edit_message(query, title + "Налаштування уставу чату: 📜", reply_markup=await _build_settings_menu(chat_id), parse_mode=ParseMode.HTML)

    elif action_type == "newyear":
        # Перемикаємо AUTO -> ON -> OFF -> AUTO
        settings = await get_chat_settings(chat_id)
        cur = str(settings.get("new_year_mode", "auto") or "auto").lower().strip()
        order = ["auto", "on", "off"]
        try:
            nxt = order[(order.index(cur) + 1) % len(order)]
        except ValueError:
            nxt = "auto"

        from bot.core.database import set_new_year_mode
        await set_new_year_mode(chat_id, nxt)

        await _safe_edit_message(
            query,
            title + "Готово, кошеня 🐾 Новорічний режим оновлено.\n\n",
            reply_markup=await _build_settings_menu(chat_id),
            parse_mode=ParseMode.HTML
        )
    

    elif action_type == "moderation":
        await _safe_edit_message(query, title + "Керування фільтром слів (єресь): ⚖️", reply_markup=await _build_moderation_menu(chat_id), parse_mode=ParseMode.HTML)

    elif action_type == "mems":
        # admin_chat_mems_{chat_id} -> відкриває меню
        if len(parts) == 4:
            await _safe_edit_message(
                query,
                title + "Налаштування гри: <b>Мемчики та котики</b> 🎮",
                reply_markup=await _build_mems_settings_menu(chat_id),
                parse_mode=ParseMode.HTML,
            )
            return

        # admin_chat_mems_choose_{key}_{chat_id} -> показує всі варіанти
        if len(parts) >= 6 and parts[3] == "choose":
            key = "_".join(parts[4:-1])
            await _safe_edit_message(
                query,
                title + "Налаштування гри: <b>Мемчики та котики</b> 🎮\n\n" + "<i>Обери значення:</i>",
                reply_markup=await _build_mems_choose_menu(chat_id, key),
                parse_mode=ParseMode.HTML,
            )
            return

        # admin_chat_mems_set_{key}_{value}_{chat_id} -> встановлює значення
        if len(parts) >= 7 and parts[3] == "set":
            key = "_".join(parts[4:-2])
            try:
                new_val = int(parts[-2])
            except ValueError:
                new_val = None

            if new_val is not None:
                await set_mems_setting_for_chat(chat_id, key, int(new_val))

            await _safe_edit_message(
                query,
                title + "Налаштування гри: <b>Мемчики та котики</b> 🎮",
                reply_markup=await _build_mems_settings_menu(chat_id),
                parse_mode=ParseMode.HTML,
            )
            return

    # 2. Дії (Перемикачі)
    elif action_type == "toggle":
        module_key = "_".join(parts[3:-1])
        if module_key in {"auto_delete_actions", "reminders_enabled", "ai_auto_clear_conversations"}:
            settings = await get_chat_settings(chat_id)
            current_status = settings.get(module_key, 0) == 1
            new_status = not current_status
            await set_chat_setting_flag(chat_id, module_key, new_status)
            new_reply_markup = await _build_settings_menu(chat_id)
            await _safe_edit_message(
                query,
                title + f"Мур! Налаштування <b>{module_key}</b> "
                f"{'УВІМКНЕНО' if new_status else 'ВИМКНЕНО'}. 🐾",
                reply_markup=new_reply_markup,
                parse_mode=ParseMode.HTML,
            )
            return
        settings = await get_chat_settings(chat_id)
        default_val = 0 if module_key == "word_filter_enabled" else 1
        current_status = settings.get(module_key, default_val)
        new_status = not (current_status == 1)
        await set_module_status(chat_id, module_key, new_status)
        new_reply_markup = await _build_modules_menu(chat_id)

        await _safe_edit_message(query, 
            title + f"Мур! Модуль '{MODULES_CONFIG.get(module_key, module_key)}' "
            f"<b>{'УВІМКНЕНО' if new_status else 'ВИМКНЕНО'}</b>. 🐾",
            reply_markup=new_reply_markup,
            parse_mode=ParseMode.HTML
        )
        
    # 3. Дії (Запит вводу)
    elif action_type == "set":
        action = parts[3]
        actions_map = {
            "welcome": ("awaiting_welcome", "🥰 Надішліть мені нове привітальне повідомлення.\n\nТеги:\n• <code>{user}</code> - згадка\n• <code>{chat}</code> - назва чату\n• <code>{first_name}</code> - ім'я\n\nНадішліть <code>-</code> або <code>/clear</code>, щоб видалити. 🌿"),
            "rules": ("awaiting_rules", "📜 Надішліть мені новий устав (правила) чату.\n\nНадішліть <code>-</code> або <code>/clear</code>, щоб видалити. 🌿"),
            "warns": ("awaiting_warns", "⚖️ Надішліть нове число (напр. <code>3</code>) для ліміту попереджень перед покутою (баном). 🌿"),
        }
        if action in actions_map:
            state_key, prompt_text = actions_map[action]
            context.user_data['admin_action'] = {'action': state_key, 'chat_id': chat_id}
            await _safe_edit_message(query,
                title + prompt_text,
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Скасувати", callback_data=f"admin_chat_settings_{chat_id}")]]),
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True
            )
            
    elif action_type == "add" and parts[3] == "word":
        context.user_data['admin_action'] = {'action': 'awaiting_add_word', 'chat_id': chat_id}
        await _safe_edit_message(query,
            title + "🖊️ Надішліть одне або декілька єретичних слів (через кому). 🌿",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Скасувати", callback_data=f"admin_chat_moderation_{chat_id}")]]),
            parse_mode=ParseMode.HTML
        )

    elif action_type == "del" and parts[3] == "word":
        context.user_data['admin_action'] = {'action': 'awaiting_del_word', 'chat_id': chat_id}
        await _safe_edit_message(query,
            title + "🗑️ Надішліть слово, яке потрібно пробачити (видалити з фільтру). 🌿",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Скасувати", callback_data=f"admin_chat_moderation_{chat_id}")]]),
            parse_mode=ParseMode.HTML
        )
        
    elif action_type == "list" and parts[3] == "words":
        words = await get_filtered_words(chat_id)
        if not words:
            text = "Список єретичних слів порожній. 🕊️"
        else:
            text = "<b>⚖️ Єретичні слова в чаті:</b>\n• <code>" + "</code>\n• <code>".join(html.escape(w) for w in words) + "</code>"
        
        await _safe_edit_message(query,
            title + text,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data=f"admin_chat_moderation_{chat_id}")]]),
            parse_mode=ParseMode.HTML
        )

# =============================================================================
# 4. Обробник Текстового Вводу для Адмінів (в ПП)
# =============================================================================

async def handle_admin_text_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Обробляє звичайний текст в ПП, якщо адмін
    перебуває в "стані вводу" (напр., 'awaiting_welcome').
    """
    if not update.message or not update.message.text or not context.user_data:
        return
        
    admin_state = context.user_data.pop('admin_action', None)
    
    if not admin_state:
        return # Це звичайне повідомлення, не для адмін-панелі
        
    action = admin_state.get('action')
    chat_id = admin_state.get('chat_id')
    text_input = update.message.text
    
    if not action or not chat_id:
        return
        
    # Отримуємо заголовок
    try:
        chat = await context.bot.get_chat(chat_id)
        title = f"<b>⚙️ Келія Настоятеля:</b>\n<i>{html.escape(chat.title or 'Цей чат')}</i>\n\n"
    except Exception:
        title = f"<b>⚙️ Керування чатом (ID: {chat_id})</b>\n\n"
        
    
    # 1. Обробка Привітання
    if action == "awaiting_welcome":
        if text_input == "-" or text_input.lower() == "/clear":
            await set_chat_welcome_message(chat_id, None)
            await update.message.reply_html(title + "✅ Привітання видалено. 🕊️", reply_markup=await _build_settings_menu(chat_id))
        else:
            await set_chat_welcome_message(chat_id, text_input)
            await update.message.reply_html(title + "✅ Мур! Привітання оновлено. 🥰", reply_markup=await _build_settings_menu(chat_id))

    # 2. Обробка Правил
    elif action == "awaiting_rules":
        if text_input == "-" or text_input.lower() == "/clear":
            await set_chat_rules(chat_id, None)
            await update.message.reply_html(title + "✅ Устав чату очищено. 🕊️", reply_markup=await _build_settings_menu(chat_id))
        else:
            await set_chat_rules(chat_id, text_input)
            await update.message.reply_html(title + "✅ Устав (правила) оновлено. 📜", reply_markup=await _build_settings_menu(chat_id))

    # 3. Обробка Ліміту Варнів
    elif action == "awaiting_warns":
        try:
            new_limit = int(text_input)
            if new_limit <= 0:
                raise ValueError
            await set_max_warns(chat_id, new_limit)
            await update.message.reply_html(title + f"✅ Ліміт попереджень: {new_limit}. ⚖️", reply_markup=await _build_settings_menu(chat_id))
        except ValueError:
            await update.message.reply_html(title + f"Ой. 😿 Введіть позитивне число (напр. <code>3</code>).")
            # Повертаємо стан, щоб юзер спробував ще
            context.user_data['admin_action'] = admin_state
            
    # 4. Обробка Фільтру (Додавання)
    elif action == "awaiting_add_word":
        words = [w.strip().lower() for w in text_input.split(',') if w.strip()]
        for word in words:
            await add_filtered_word(chat_id, word)
        await update.message.reply_html(
            title + f"✅ Додано єретичних слів: {len(words)}. ⚖️",
            reply_markup=await _build_moderation_menu(chat_id)
        )

    # 5. Обробка Фільтру (Видалення)
    elif action == "awaiting_del_word":
        word = text_input.strip().lower()
        await remove_filtered_word(chat_id, word)
        await update.message.reply_html(
            title + f"✅ Слово '<code>{html.escape(word)}</code>' пробачено (видалено з фільтру). 🕊️",
            reply_markup=await _build_moderation_menu(chat_id),
            parse_mode=ParseMode.HTML
        )

# =============================================================================
# 5. Реєстрація
# =============================================================================

def register_chat_admin_handlers(application: Application):
    """Реєструє обробники для адміністрування чатів."""
    
    # /adminhelp — довідка для адмінів
    application.add_handler(CommandHandler("adminhelp", adminhelp_command, filters=filters.ChatType.GROUPS))

    # --- Команди в чаті ---
    
    # /settings (тільки в групах)
    application.add_handler(
        CommandHandler(
            "settings",
            settings_command,
            filters=filters.ChatType.GROUPS
        )
    )
    
    # /rules (публічна)
    application.add_handler(
        CommandHandler(
            "rules",  # <-- Тільки валідна команда
            rules_command,
            filters=filters.ChatType.GROUPS
        )
    )
    # Аліас /правила
    application.add_handler(
        MessageHandler(
            filters.Regex(r'^правила(?:@\w+)?$') & filters.ChatType.GROUPS,
            rules_command
        )
    )
    
    # /warn, /unwarn, /warns (адмінські)
    application.add_handler(
        CommandHandler(
            "warn",  # <-- Тільки валідна команда
            warn_command,
            filters=filters.ChatType.GROUPS
        )
    )
    # Аліас /варн
    application.add_handler(
        MessageHandler(
            filters.Regex(r'^варн(?:@\w+)?$') & filters.ChatType.GROUPS,
            warn_command
        )
    )

    application.add_handler(
        CommandHandler(
            "unwarn",  # <-- Тільки валідна команда
            unwarn_command,
            filters=filters.ChatType.GROUPS
        )
    )
    # Аліаси /знятиварн, /зняти_варн
    application.add_handler(
        MessageHandler(
            filters.Regex(r'^знятиварн(?:@\w+)?$') & filters.ChatType.GROUPS,
            unwarn_command
        )
    )
    application.add_handler(
        MessageHandler(
            filters.Regex(r'^зняти варн(?:@\w+)?$') & filters.ChatType.GROUPS,
            unwarn_command
        )
    )
    
    application.add_handler(
        CommandHandler(
            "warns",  # <-- Тільки валідна команда
            warns_command,
            filters=filters.ChatType.GROUPS
        )
    )
    # Аліас /варни
    application.add_handler(
        MessageHandler(
            filters.Regex(r'^варни(?:@\w+)?$') & filters.ChatType.GROUPS,
            warns_command
        )
    )
    
    # --- Обробники в ПП ---
    
    # Роутер для всіх кнопок меню
    application.add_handler(
        CallbackQueryHandler(
            admin_chat_callback_router,
            pattern=r"^admin_chat_"
        )
    )
    
    # Обробник текстового вводу для налаштувань
    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE,
            handle_admin_text_input
        ),
        group=2 # Вищий пріоритет, щоб перехопити ввід
    )
    
    logger.info("Модуль Настоятеля (chat_admin_handlers.py) завантажено. 🌿")
