# command_handlers.py
# -*- coding: utf-8 -*-
"""
command_handlers.py

Цей модуль - серце котячих пустощів та громадського життя. 🐾
Він відповідає за всі мирські команди: від ніжних "обійняти"
до божественних "передбачень".

(Адмін-команди знаходяться в admin_handlers.py)
"""

import logging
import os
import re
import io
import asyncio
from datetime import date
import html

# --- Telegram Imports ---
from telegram import (
    Update,
    InputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    CallbackQuery,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# --- Local Imports ---
from bot.core.database import (
    get_daily_prediction,
    set_daily_prediction,
    increment_jerk_count,
    get_jerk_count,
    get_user_by_username,
    get_chat_settings,
)
from bot.services.predictions import get_random_prediction
from bot.utils.utils import (
    PHOTO_DIR,
    format_target_mention,
    get_user_from_username,
    mention,
)
from bot.handlers.chat_admin_handlers import is_chat_module_enabled # Перевірка прав

# --- Module Constants ---

logger = logging.getLogger(__name__)

# (СТИЛІЗОВАНО) Словник дій та відповідей.
# {sender} - той, хто діє. {target} - той, на кого діють.
ACTIONS = {
    "обійняти": "💞 {sender} обіймає {target} муркотно й без зайвих слів",
    "вилизати": "👅 {sender} вилизує {target}. Чистота понад усе, мяу!",
    "вдарити": "💥 {sender} дає святого ляпаса {target}. Не гріши!",
    "погладити": "☺️ {sender} погладжує {target}. Трохи ніжності не завадить",
    "мур": "🐾 {sender} муркоче біля {target} і лягає спатки",
    "шшш": "😾 {sender} шипить на {target}. Не той настрій.",
    "мяу": "🐾 {sender} треться об {target} муурр",
    "чай": "☕️ {sender} ділиться м'ятним чаєм з {target}",
    "притиснутись": "🥰 {sender} притискається до {target}. Так краще",
    "нагодувати": "🐟 {sender} годує {target}. Смачного!",
    "бу": "👻 {sender} лякає {target}. Бу!",
    "танець": "💃 {sender} запрошує {target} на святий танець. Не відмовляйся!",
    "поцілувати": "💋 {sender} цілує {target}. Схоже, це любов",
    "покусати": "😝 {sender} грайливо покусує {target}",
    "виїбати": "🍑 {sender} виїбує {target} у найсвятіший спосіб👅",
    "трахнути": "🍆 {sender} трахує {target} з пристрастю та ніжністю🔥",
    "дроч": "💦 {sender} дрочить на {target} і йде по горішки"
}

async def delete_message_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Job для видалення повідомлення дії через 3 хвилини.
    """
    data = context.job.data
    chat_id = data["chat_id"]
    message_id = data["message_id"]
    try:
        await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception as e:
        logger.debug(f"Не вдалося видалити повідомлення {message_id} у {chat_id}: {e}")

# (НОВЕ) Безпечна відповідь на callback
async def _query_answer_safe(query: CallbackQuery) -> None:
    """
    Безпечна відповідь на callback, ігноруючи помилки.
    """
    try:
        await query.answer()
    except Exception:
        pass  # Ігноруємо, якщо юзер клікає занадто швидко

# =============================================================================
# 1. Action Handlers (Обробники Дій)
# =============================================================================


async def handle_action_commands(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Обробляє команди дій (наприклад, "обійняти @user").
    Спеціальна обробка для команди "дроч" з підрахунком.
    """
    if not update.message or not update.message.text:
        logger.warning("handle_action_commands: оновлення без тексту.")
        return

    # --- ПЕРЕВІРКА ПРАВ ---
    if not await is_chat_module_enabled(update.effective_chat, "commands_enabled"):
        logger.debug(
            f"Module 'commands_enabled' disabled for chat {update.effective_chat.id}. Ignoring action."
        )
        return
    # --- КІНЕЦЬ ПЕРЕВІРКИ ---

    text = update.message.text.strip()
    user = update.message.from_user
    sender = mention(user)

    for action_key, action_response_template in ACTIONS.items():
        pattern = rf"^\s*{re.escape(action_key)}(?:\s.*)?$"

        if re.match(pattern, text, re.IGNORECASE):
            action = action_key
            target_user_resolved = None
            target_string_display = None
            target_user_id = None
            target_username_mentioned = None

            # 1. Визначення цілі (Target)
            if update.message.reply_to_message:
                target_user_resolved = update.message.reply_to_message.from_user
                target_user_id = getattr(target_user_resolved, "id", None)
            elif update.message.entities:
                for entity in update.message.entities:
                    if entity.type == "text_mention" and entity.user:
                        target_user_resolved = entity.user
                        target_user_id = getattr(target_user_resolved, "id", None)
                        break
                    elif entity.type == "mention":
                        username_from_mention = update.message.text[
                            entity.offset + 1 : entity.offset + entity.length
                        ]
                        target_username_mentioned = username_from_mention
                        # 1) Спробуємо знайти в локальній БД (швидше та приватніше)
                        try:
                            db_user = await get_user_by_username(username_from_mention)
                        except Exception:
                            db_user = None

                        if db_user:
                            # Відомий у БД: відображаємо через ID/ім'я
                            target_user_id = db_user.get("user_id")
                            target_string_display = (
                                "<a href='tg://user?id={user_id}'>{label}</a>".format(
                                    user_id=db_user["user_id"],
                                    label=html.escape(
                                        db_user.get("first_name")
                                        or db_user.get("username")
                                        or str(db_user["user_id"])
                                    ),
                                )
                            )
                        else:
                            # 2) Як запасний варіант — пробуємо отримати через API
                            potential_user = await get_user_from_username(
                                context,
                                username_from_mention,
                            )
                            if potential_user and not potential_user.is_bot:
                                target_user_resolved = potential_user
                                target_user_id = getattr(target_user_resolved, "id", None)
                            else:
                                try:
                                    chat_obj = await context.bot.get_chat(
                                        f"@{username_from_mention}"
                                    )
                                    if getattr(chat_obj, "first_name", None):
                                        target_user_id = getattr(chat_obj, "id", None)
                                        target_string_display = (
                                            "<a href='tg://user?id={user_id}'>{label}</a>".format(
                                                user_id=chat_obj.id,
                                                label=html.escape(chat_obj.first_name),
                                            )
                                        )
                                    else:
                                        target_string_display = (
                                            "<a href='https://t.me/{username}'>@{username}</a>".format(
                                                username=html.escape(username_from_mention)
                                            )
                                        )
                                except Exception:
                                    target_string_display = (
                                        "<a href='https://t.me/{username}'>@{username}</a>".format(
                                            username=html.escape(username_from_mention)
                                        )
                                    )
                        break

            if target_user_id is not None and target_user_id == user.id:
                return

            if (
                target_username_mentioned
                and user.username
                and target_username_mentioned.lower() == user.username.lower()
            ):
                return

            if not target_user_resolved and not target_string_display:
                return

            # 2. Форматування відповіді
            if target_user_resolved:
                # Якщо дія була у відповідь на повідомлення — показуємо клікабельну згадку через ID
                try:
                    if (
                        update.message.reply_to_message
                        and update.message.reply_to_message.from_user
                        and getattr(target_user_resolved, "id", None)
                        == update.message.reply_to_message.from_user.id
                    ):
                        # clickable mention for reply targets
                        target_for_response = mention(target_user_resolved)
                    else:
                        # Інакше показуємо plain text ім'я (щоб не було посилань)
                        target_name = (
                            getattr(target_user_resolved, "first_name", None)
                            or getattr(target_user_resolved, "username", None)
                            or str(getattr(target_user_resolved, "id", ""))
                        )
                        target_for_response = html.escape(str(target_name))
                except Exception:
                    target_for_response = html.escape(
                        str(
                            getattr(
                                target_user_resolved,
                                "first_name",
                                getattr(
                                    target_user_resolved,
                                    "username",
                                    getattr(target_user_resolved, "id", ""),
                                ),
                            )
                        )
                    )
            else:
                target_for_response = target_string_display or "себе"

            response = action_response_template.format(
                sender=sender,
                target=target_for_response,
            )

            # (НОВЕ) Спеціальна обробка для дрочок
            if action == "дроч":
                # Збільшуємо лічильник дрочок
                new_count = await increment_jerk_count(user.id)
                response += f"
Всього горішків з'їдено: <b>{new_count}</b>👅"

            photo_path = os.path.join(PHOTO_DIR, f"{action}.jpg")

            # 3. Надсилання
            try:
                if os.path.exists(photo_path):
                    def _read_bytes(path: str) -> bytes:
                        with open(path, "rb") as f:
                            return f.read()

                    data = await asyncio.to_thread(_read_bytes, photo_path)
                    sent_message = await update.message.reply_photo(
                        photo=InputFile(
                            io.BytesIO(data),
                            filename=os.path.basename(photo_path),
                        ),
                        caption=response,
                        parse_mode=ParseMode.HTML,
                    )
                else:
                    sent_message = await update.message.reply_html(response)

                # Автовидалення через 3 хвилини, якщо ввімкнено
                settings = await get_chat_settings(update.effective_chat.id)
                if settings.get("auto_delete_actions", 0) == 1:
                    # Видаляємо відповідь бота
                    context.job_queue.run_once(
                        delete_message_job,
                        180,  # 3 хвилини
                        data={
                            "chat_id": sent_message.chat_id,
                            "message_id": sent_message.message_id,
                        },
                        name=f"delete_action_{sent_message.message_id}",
                    )
                    # Видаляємо виклики команди користувача
                    context.job_queue.run_once(
                        delete_message_job,
                        180,  # 3 хвилини
                        data={
                            "chat_id": update.effective_chat.id,
                            "message_id": update.message.message_id,
                        },
                        name=f"delete_command_{update.message.message_id}",
                    )

            except Exception as e:
                logger.error(
                    f"Помилка виконання дії '{action}' для {user.id}: {e}",
                    exc_info=True,
                )
                # (СТИЛІЗОВАНО)
                await update.message.reply_text(
                    "Ой, мур... 😿 Щось пішло не так. Не можу цього зробити."
                )

            break # Дія виконана, виходимо
# =============================================================================
# 2. Prediction Handlers (Обробники Передбачень)
# =============================================================================


async def prediction_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Надсилає користувачу його персональне передбачення на сьогодні."""
    if not update.effective_user:
        return

    # --- ПЕРЕВІРКА ПРАВ ---
    # (ВИПРАВЛЕНО) Використовуємо ключ 'commands_enabled'
    if not await is_chat_module_enabled(update.effective_chat, "commands_enabled"):
        logger.debug(
            f"Module 'commands_enabled' disabled for chat {update.effective_chat.id}. Ignoring prediction."
        )
        return
    # --- КІНЕЦЬ ПЕРЕВІРКИ ---

    user_id = update.effective_user.id
    today_str = date.today().isoformat()

    user_prediction = await get_daily_prediction(user_id, today_str)

    if not user_prediction:
        logger.info(f"Генерую нове передбачення для {user_id}.")
        user_prediction = await get_random_prediction()
        await set_daily_prediction(user_id, user_prediction, today_str)

    # (СТИЛІЗОВАНО)
    message = (
        f"🔮 <b>Святе передбачення на сьогодні</b> 🔮\n\n"
        f"<i>{user_prediction}</i>\n\n"
        f"✨ Нехай цей день буде благословенним. 🌿"
    )
    if update.message:
        sent_message = await update.message.reply_html(message)
        
        # Автовидалення через 10 хвилин, якщо ввімкнено
        settings = await get_chat_settings(update.effective_chat.id)
        if settings.get('auto_delete_actions', 0) == 1:
            # Видаляємо відповідь бота
            context.job_queue.run_once(
                delete_message_job,
                600,  # 10 хвилин
                data={"chat_id": sent_message.chat_id, "message_id": sent_message.message_id},
                name=f"delete_prediction_{sent_message.message_id}"
            )
            # Видаляємо виклики команди користувача
            context.job_queue.run_once(
                delete_message_job,
                600,  # 10 хвилин
                data={"chat_id": update.effective_chat.id, "message_id": update.message.message_id},
                name=f"delete_command_{update.message.message_id}"
            )


# =============================================================================
# (НОВЕ) Обробник Статистики Дрочок
# =============================================================================

async def my_jerk_stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показує скільки дрочок у користувача."""
    user = update.effective_user
    chat = update.effective_chat
    
    # --- ПЕРЕВІРКА ПРАВ ---
    if not await is_chat_module_enabled(chat, "commands_enabled"):
        return
    # --- КІНЕЦЬ ПЕРЕВІРКИ ---
    
    jerk_count = await get_jerk_count(user.id)
    
    message = (
        f"🌰 <b>Статистика дрочок</b> 🌰\n\n"
        f"Користувач {mention(user)} з'їдав горішків: <code>{jerk_count}</code> разів"
    )
    
    if update.message:
        await update.message.reply_html(message)


# =============================================================================
# 3. Menu Handlers (Обробники Меню)
# =============================================================================


async def show_chat_commands(
    update: Update, context: ContextTypes.DEFAULT_TYPE, from_callback: bool = False
):
    """
    Надсилає список доступних чат-команд та інтеракцій.
    """
    query = update.callback_query if from_callback else None
    chat_for_check = update.effective_chat
    if from_callback and query and query.message:
        await _query_answer_safe(query)
        chat_for_check = query.message.chat

    # --- ПЕРЕВІРКА ПРАВ ---
    # (ВИПРАВЛЕНО) Використовуємо ключ 'commands_enabled'
    if not await is_chat_module_enabled(chat_for_check, "commands_enabled"):
        logger.debug(
            f"Module 'commands_enabled' disabled for chat {chat_for_check.id}. Ignoring show_chat_commands."
        )
        if from_callback and query:
            # (СТИЛІЗОВАНО)
            await query.answer("Модуль команд дій вимкнено в цьому чаті. 🕊️", show_alert=True)
        return
    # --- КІНЕЦЬ ПЕРЕВІРКИ ---

    command_list = ""
    sorted_actions = sorted(ACTIONS.keys())
    for cmd in sorted_actions:
        command_list += f"• <code>{cmd}</code>\n"

    # (СТИЛІЗОВАНО)
    base_text = (
        f"📜 <b>Список мирських дій</b> 📜\n\n"
        "Ось мої смиренні дії для чату:\n\n"
        f"{command_list}\n"
        "<i>Просто напиши команду в чаті або у відповідь на повідомлення.</i>"
    )

    chat_id = (
        query.message.chat.id
        if from_callback and query and query.message
        else update.effective_chat.id
    )

    keyboard = [
        [
            InlineKeyboardButton(
                "⬅️ Назад у келію", callback_data="back_to_main_menu"
            )
        ]
    ]
    
    reply_markup = InlineKeyboardMarkup(keyboard)

    try:
        if from_callback and query:
            # (ВИПРАВЛЕНО) Більш м'який спосіб: редагувати, а не видаляти/надсилати
            await query.edit_message_text(
                text=base_text,
                parse_mode=ParseMode.HTML,
                reply_markup=reply_markup
            )
        elif update.effective_chat:
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=base_text,
                parse_mode=ParseMode.HTML,
                reply_markup=reply_markup,
            )
    except Exception as e:
        logger.error(f"Помилка обробки меню чат-команд: {e}", exc_info=True)


# =============================================================================
# 4. Handlers Registration (Реєстрація обробників)
# =============================================================================


def register_command_handlers(application: Application):
    """Реєструє обробники для команд дій та передбачень."""

    # --- Передбачення ---
    # (ВИПРАВЛЕНО) Додано українські аліаси
    application.add_handler(CommandHandler(
        "prediction",  # (ВИПРАВЛЕНО)
        prediction_command
    ))
    application.add_handler(
        MessageHandler(
            filters.TEXT & filters.Regex(r"(?i)^(моє передбачення|прогноз|передбачення)$"), # (ВИПРАВЛЕНО)
            prediction_command,
        )
    )

    # --- Меню ---
    # (ВИПРАВЛЕНО) Додано українські аліаси
    application.add_handler(CommandHandler(
        "commands",  # (ВИПРАВЛЕНО)
        show_chat_commands
    ))
    # (НОВЕ)
    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND & filters.ChatType.GROUPS & filters.Regex(r"(?i)^(команди|дії)$"),
            show_chat_commands
        )
    )
    application.add_handler(
        CallbackQueryHandler(
            lambda u, c: show_chat_commands(u, c, from_callback=True),
            pattern=r"^show_chat_commands$",
        )
    )

    # --- Статистика Дрочок ---
    # (НОВЕ) Команда для перегляду кількості дрочок
    # Примітка: /jerkstats - англійська команда (Telegram не підтримує кирилицю в командах)
    application.add_handler(CommandHandler(
        ["jerkstats", "stats"],
        my_jerk_stats_command
    ))
    # Українські текстові варіанти для груп
    application.add_handler(
        MessageHandler(
            filters.TEXT & filters.Regex(r"(?i)^(мідрочок|моя\s+квота|статистика\s+дрочок)$"),
            my_jerk_stats_command
        )
    )

    # --- Команди Дій (зі словника ACTIONS) ---
    for action_key in ACTIONS.keys():
        application.add_handler(
            MessageHandler(
                filters.TEXT
                & ~filters.COMMAND # (ДОДАНО) Ігноруємо команди /
                & filters.ChatType.GROUPS # (ДОДАНО) Тільки в групах
                & filters.Regex(rf"(?i)^\s*{re.escape(action_key)}\b(?:\s.*)?$"),
                handle_action_commands,
            )
        )

    # (СТИЛІЗОВАНО)
    logger.info("Обробники Команд Дій (command_handlers.py) завантажено. 📜")
