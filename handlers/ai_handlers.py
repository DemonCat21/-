#-*- coding: utf-8 -*-
"""
ai_handlers.py

Цей модуль відповідає за взаємодію з ШІ.
Він керує чергами запитів, обробляє пам'ять,
встановлює режими спілкування та реагує на стікери.

Виправлення:
- Покращено визначення ID бота (перевірка по username як резерв).
- Прибрано Rate Limit для прямих відповідей (reply) боту.
- Видалено налаштування температури для різних режимів.
"""

import logging
import httpx
import asyncio
import random
import re
import json
import time
import pytz 
import os
from datetime import datetime
from typing import Optional, Dict

# --- Telegram Imports ---
from telegram.constants import ParseMode, ChatMemberStatus, ChatAction
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, Bot
)

from telegram.ext import (
    Application,
    ContextTypes,
    CommandHandler,
    MessageHandler,
    filters,
    CallbackQueryHandler,
    ConversationHandler,
)

# --- Local Imports ---
from bot.core.database import (
    save_message, get_recent_messages, save_sticker, get_all_stickers,
    save_memory, get_memories_for_scope, remove_memory,
    is_ai_enabled_for_chat,
    get_user_info,
    get_chat_settings,
    clear_conversations,
)
from bot.handlers.reminder_handlers import is_reminder_trigger
from bot.utils.utils import (
    DEEPSEEK_API_KEY,
    DEEPSEEK_API_URL,
    DEEPSEEK_MODEL,
    AI_HTTP_TIMEOUT_SEC,
    AI_HTTP_CONNECT_TIMEOUT_SEC,
    AI_RETRIES,
    AI_BACKOFF_BASE_SEC,
    AI_BACKOFF_MAX_SEC,
    AI_MAX_TOKENS,
    BOT_MODES,
    DEFAULT_BOT_MODE,
    sanitize_reply,
    send_typing_periodically,
    get_mode_prompt,
    get_theme_value,
    get_user_addressing,
)

# --- Module Constants ---

logger = logging.getLogger(__name__)
KYIV_TZ = pytz.timezone('Europe/Kyiv') 

DEFAULT_TEMP = 0.7


def _get_api_key() -> str:
    """Отримує ключ DeepSeek: спершу з env, далі з utils default."""
    try:
        raw = os.environ.get("DEEPSEEK_API_KEY")
        if raw:
            raw = raw.strip()
            if raw:
                if __debug__:
                    logger.debug("DeepSeek ключ взято з env (len=%d)", len(raw))
                return raw
    except Exception:
        pass
    if DEEPSEEK_API_KEY:
        if __debug__:
            logger.debug("DeepSeek ключ взято з utils default (len=%d)", len(DEEPSEEK_API_KEY))
        return DEEPSEEK_API_KEY
    logger.error("DeepSeek API key відсутній навіть у дефолті")
    return ""


def _min(a: float, b: float) -> float:
    return a if a < b else b

def _calc_backoff(attempt: int) -> float:
    # Експоненційний backoff + джиттер. Без "дедупів" і заглушок.
    base = AI_BACKOFF_BASE_SEC * (2 ** max(0, attempt))
    delay = _min(base, AI_BACKOFF_MAX_SEC)
    jitter = random.uniform(0, 0.35 * delay)
    return delay + jitter

def _retry_after_seconds(headers: dict) -> Optional[float]:
    try:
        ra = headers.get("retry-after") or headers.get("Retry-After")
        if not ra:
            return None
        return float(ra)
    except Exception:
        return None

def _truncate_for_log(s: str, limit: int = 500) -> str:
    if s is None:
        return ""
    s = str(s)
    return s if len(s) <= limit else s[:limit] + "…"
# Rate Limiting
USER_RATE_LIMIT = 0.5 
_user_last_request: Dict[int, float] = {} # Кеш останніх запитів

# --- Conversation States ---
STATE_REMEMBER_SCOPE, STATE_FORGET_SCOPE = range(2)

# Прості відповіді для частих запитань
SIMPLE_RESPONCES = {
    ("привіт", "привет", "привіт котик", "привіт мур", "hi", "hello"): [
        "Привіт! 🐾",
        "Мур! 😼",
        "Вітаю! 🌿",
    ],
    ("як справи", "как дела", "як себе почуваєш", "як ти", "як дела ты"): [
        "Зі мною все добре, спасибі! 🐾",
        "Мур, спасибі за питання! 😸",
        "Все як завжди - спокійно та з гідністю. 🧘",
    ],
    ("спасибі", "спасибо", "дякую", "дякую!", "thanks", "thank you"): [
        "Будь ласка! 🌿",
        "Радий допомогти! 😽",
        "Не за що! 🐾",
    ],
    ("пока", "bye", "до свидання", "до побачення", "чао"): [
        "До зустрічі! 🐾",
        "Мур! 😼",
        "Грішити в міру! 😈",
    ],
}

# =============================================================================
# 0. New AI Commands (Нові команди режимів)
# =============================================================================

async def aimode_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    args = context.args or []
    
    if not args:
        modes_list = ", ".join(BOT_MODES.keys())
        await update.message.reply_text(
            f"<b>Використання:</b> /aimode <mode>\n"
            f"Наприклад: <code>/aimode humor</code>\n"
            f"Доступні режими: {modes_list}",
            parse_mode=ParseMode.HTML
        )
        return
        
    mode = args[0].lower()
    
    if mode not in BOT_MODES:
        modes_list = ", ".join(BOT_MODES.keys())
        await update.message.reply_text(
            f"Невідомий режим: <b>{mode}</b>.\nДоступні режими: {modes_list}",
            parse_mode=ParseMode.HTML
        )
        return
        
    if 'user_ai_modes' not in context.chat_data:
        context.chat_data['user_ai_modes'] = {}
        
    context.chat_data['user_ai_modes'][user_id] = mode
    await clear_conversations(user_id=user_id, chat_id=update.effective_chat.id)
    ctx = await get_user_addressing(user_id)
    await update.message.reply_text(
        f"Мур. Мій режим для {ctx.you} в <b>цьому чаті</b> змінено на: <b>{mode}</b>. Починаю з чистого контексту.",
        parse_mode=ParseMode.HTML
    )


async def aireset_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    
    await save_message(user_id, chat_id, "system", "[AI RESET]")
    ctx = await get_user_addressing(user_id)
    await update.message.reply_text(
        f"Контекст ШІ для {ctx.you} в цьому чаті скинуто. Починаємо з чистого аркуша! 🧹",
        parse_mode=ParseMode.HTML
    )

async def aiclear_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Очищає чергу повідомлень для поточного чату (тільки адміни)."""
    chat = update.effective_chat
    user = update.effective_user
    
    # Перевірка прав адміна
    member = await chat.get_member(user.id)
    if member.status not in [ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER] and user.id != 1064174112: 
        await update.message.reply_text("Мур! Ця команда тільки для настоятелів (адмінів). 😾")
        return

    chat_id = chat.id
    async with ai_queue_manager.lock:
        if chat_id in ai_queue_manager.queues:
            # Створюємо нову пусту чергу
            ai_queue_manager.queues[chat_id] = asyncio.Queue(maxsize=5)
            await update.message.reply_text("🧹 Черга AI для цього чату примусово очищена.")
        else:
            await update.message.reply_text("Черга і так пуста. 🍃")


async def aihelp_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    help_text = (
        "<b>🤖 AI-режими котика</b>\n\n"
        "Ви можете перемикати стиль відповіді ШІ командою <code>/aimode режим</code> (наприклад, <code>/aimode humor</code>).\n\n"
        "Доступні режими:\n"
        "<b>charismatic</b> — харизматичний, флірт, чорний гумор (за замовчуванням)\n"
        "<b>academic</b> — серйозний, тільки факти, без гумору\n"
        "<b>humor</b> — меми, жарти, тролінг\n"
        "\n<b>/aireset</b> — скинути контекст\n"
        "<b>/aiclear</b> — очистити чергу (для адмінів)\n"
        "<b>/aihelp</b> — ця довідка"
    )
    await update.message.reply_text(help_text, parse_mode=ParseMode.HTML)


# =============================================================================
# 1. AI Queue Manager (Менеджер черг ШІ)
# =============================================================================

class AIChatQueueManager:
    def __init__(self) -> None:
        self.queues = {}
        self.workers = {}
        self.lock = asyncio.Lock()
        logger.info("AIChatQueueManager ініціалізовано. Мур.")

    async def _worker(self, chat_id: int, bot: Bot) -> None:
        queue = self.queues.get(chat_id)
        if not queue:
            async with self.lock:
                if chat_id in self.workers: del self.workers[chat_id]
            return

        try:
            while True:
                try:
                    task_data = await queue.get()
                except asyncio.CancelledError:
                    break

                try:
                    await process_ai_response(
                        user_id=task_data['user_id'],
                        chat_id=chat_id,
                        user_input=task_data['user_input'],
                        bot=bot,
                        application=task_data['application'],
                        mode=task_data['mode'],
                        message_to_reply_id=task_data['message_to_reply_id'],
                        reply_context=task_data.get('reply_context') 
                    )
                except Exception as e:
                    logger.error(f"Помилка під час виконання process_ai_response: {e}", exc_info=True)
                finally:
                    queue.task_done()

        except asyncio.CancelledError:
            pass
        finally:
            async with self.lock:
                if chat_id in self.workers: del self.workers[chat_id]
                if chat_id in self.queues: del self.queues[chat_id]

    async def add_task(self, chat_id: int, bot: Bot, task_data: dict) -> None:
        async with self.lock:
            if chat_id not in self.queues:
                # Ліміт черги
                self.queues[chat_id] = asyncio.Queue(maxsize=5) 
            
            queue = self.queues[chat_id]
            
            if queue.full():
                try:
                    # Викидаємо найстаріший запит, якщо черга переповнена
                    queue.get_nowait()
                    queue.task_done()
                    logger.warning(f"Черга для чату {chat_id} переповнена. Старий запит відкинуто.")
                except asyncio.QueueEmpty:
                    pass

            await queue.put(task_data)
            try:
                await bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
            except Exception:
                pass
            
            if chat_id not in self.workers or self.workers[chat_id].done():
                self.workers[chat_id] = asyncio.create_task(self._worker(chat_id, bot))

ai_queue_manager = AIChatQueueManager()


# =============================================================================
# 2. Core AI Functions (Ядро ШІ)
# =============================================================================

def _clean_deepseek_thinking(text: str) -> str:
    """Очищає теги <think>...</think>, якщо модель їх повернула."""
    return re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL).strip()

async def safe_send_message(
    bot: Bot, chat_id: int, text: str, reply_to_message_id: int = None
) -> list[int]:
    """
    Розбиває довге повідомлення на частини (по 4096 символів) і відправляє їх.
    """
    MAX_LENGTH = 4096
    sent_ids: list[int] = []
    
    if len(text) <= MAX_LENGTH:
        sent = await bot.send_message(
            chat_id=chat_id,
            text=text,
            reply_to_message_id=reply_to_message_id
        )
        if sent:
            sent_ids.append(sent.message_id)
    else:
        parts = [text[i:i+MAX_LENGTH] for i in range(0, len(text), MAX_LENGTH)]
        first_msg = await bot.send_message(
            chat_id=chat_id,
            text=parts[0],
            reply_to_message_id=reply_to_message_id
        )
        last_msg_id = first_msg.message_id
        sent_ids.append(first_msg.message_id)
        for part in parts[1:]:
            await asyncio.sleep(0.3) 
            next_msg = await bot.send_message(
                chat_id=chat_id,
                text=part,
                reply_to_message_id=last_msg_id
            )
            last_msg_id = next_msg.message_id
            sent_ids.append(next_msg.message_id)
    return sent_ids


# --- Main AI Response Logic ---

async def get_ai_response(
    user_id: int,
    chat_id: int,
    user_input: str,
    bot: Bot,
    mode: str,
    reply_context: Optional[str] = None 
) -> str:
    api_key = _get_api_key()
    if not api_key:
        logger.warning("DeepSeek API key відсутній, роблю запит без нього (можливий 401)")

    system_prompt = await get_mode_prompt(mode)
    
    # Розумна температура
    ai_temperature = DEFAULT_TEMP
    
    ai_max_history_chars = await get_theme_value("ai_max_history_chars", 2500)

    # -------------------------------------------------------------------------
    # 1. КОНТЕКСТ ЧАСУ ТА ДАТИ (УКРАЇНСЬКОЮ)
    # -------------------------------------------------------------------------
    now = datetime.now(KYIV_TZ)
    days_ua = {
        "Monday": "Понеділок", "Tuesday": "Вівторок", "Wednesday": "Середа",
        "Thursday": "Четвер", "Friday": "П'ятниця", "Saturday": "Субота", "Sunday": "Неділя"
    }
    months_ua = [
        "", "січня", "лютого", "березня", "квітня", "травня", "червня",
        "липня", "серпня", "вересня", "жовтня", "листопада", "грудня"
    ]
    
    day_name = days_ua.get(now.strftime("%A"), now.strftime("%A"))
    month_name = months_ua[now.month]
    date_str = f"{day_name}, {now.day} {month_name} {now.year} року"
    time_str = now.strftime("%H:%M")
    
    time_context = (
        f"--- CURRENT CONTEXT (KYIV TIME) ---\n"
        f"📅 Date: {date_str}\n"
        f"⏰ Time: {time_str}\n"
        f"-----------------------------------\n"
    )
    system_prompt = f"{time_context}\n{system_prompt}"

    # -------------------------------------------------------------------------
    # 2. ПЕРСОНАЛІЗАЦІЯ (Інфо про юзера)
    # -------------------------------------------------------------------------
    user_info = await get_user_info(user_id)
    user_name_context = ""
    if user_info and user_info.get("first_name"):
        user_name_context = f"\nUser's Name: {user_info.get('first_name')}"
        if user_info.get("username"):
            user_name_context += f" (@{user_info.get('username')})"
    
    # -------------------------------------------------------------------------
    # 2.1. ЗВЕРНЕННЯ ЗА СТАТТЮ (з профілю)
    # -------------------------------------------------------------------------
    addr = await get_user_addressing(user_id)
    gender_contract_rule = (
        "Стать користувача береш тільки з поля gender його профілю. "
        "Не вгадуй стать за ім'ям, ніком, аватаром чи текстом. "
        "Якщо профіль відсутній або gender=null/not_set/невідомо — звертайся виключно на «Ви», без родових форм. "
        "Не переносиш стать одного користувача на іншого і не змінюєш стиль звертання посеред діалогу."
    )
    bot_gender_rule = (
        "Ти завжди хлопець-бот (котик) і говориш про себе в чоловічому роді незалежно від статі користувача."
    )

    # Правило: якщо стать не вказана → звертайся на "Ви" і без форм у роді.
    if getattr(addr, "you", "") == "Ви":
        addressing_rule = (
            "Стать користувача не визначена. "
            "Звертайся до нього виключно на «Ви». "
            "Уникай форм у роді (зробив/зробила, готовий/готова). "
            "Використовуй нейтральні конструкції: «можете», «зробіть», «підкажіть»."
        )
    elif getattr(addr, "noun", "") == "він":
        addressing_rule = (
            "Користувач обрав чоловічу стать. "
            "Звертайся на «ти» та використовуй чоловічий рід у формулюваннях (зробив, готовий, радий)."
        )
    else:
        addressing_rule = (
            "Користувач обрав жіночу стать. "
            "Звертайся на «ти» та використовуй жіночий рід у формулюваннях (зробила, готова, рада)."
        )


    # -------------------------------------------------------------------------
    # 3. ІНСТРУКЦІЇ
    # -------------------------------------------------------------------------
    dialogue_instructions = (
        "\n\n[ПРАВИЛА ДІАЛОГУ]\n"
        "1. Пиши українською, коротко й по суті.\n"
        "2. Не використовуй Markdown/HTML/посилання-розмітку — тільки простий текст.\n"
        "3. Тримай відповідь лаконічною (≈ до 40 слів), без води.\n"
        "4. Використовуй контекст дати/часу (Київ) лише якщо це реально доречно.\n"
        f"5. Спілкуєшся з: {user_name_context if user_name_context else 'користувачем'}.\n"
        f"6. {gender_contract_rule}\n"
        f"7. {addressing_rule}\n"
        f"8. {bot_gender_rule}\n"
    )
    system_prompt += dialogue_instructions

    # -------------------------------------------------------------------------
    # 4. ІСТОРІЯ ТА ПАМ'ЯТЬ
    # -------------------------------------------------------------------------
    history = await get_recent_messages(user_id, chat_id, max_chars=ai_max_history_chars)
    
    cleaned_history = []
    for msg in history:
        r = msg.get("role")
        if r not in ("system", "user", "assistant"):
            # Якщо раптом в базі залишилися повідомлення з роллю "tool", міняємо їх на user
            msg["role"] = "user"
        cleaned_history.append(msg)
    history = cleaned_history
    
    if reply_context:
        history.append({"role": "system", "content": f"CONTEXT: User replied to this message: '{reply_context}'"})

    user_memories = await get_memories_for_scope(user_id, 'user')
    chat_memories = await get_memories_for_scope(chat_id, 'chat')

    memory_parts = []
    if user_memories:
        user_mem_str = "\n".join([f"- {m['key']}: {m['value']}" for m in user_memories])
        memory_parts.append(f"Ось що ти знаєш про користувача (user {user_id}):\n{user_mem_str}")

    if chat_memories:
        chat_mem_str = "\n".join([f"- {m['key']}: {m['value']}" for m in chat_memories])
        memory_parts.append(f"Ось що ти знаєш про цей чат (chat {chat_id}):\n{chat_mem_str}")

    if memory_parts:
        full_system_prompt = f"{system_prompt}\n\n" + "\n\n".join(memory_parts)
    else:
        full_system_prompt = system_prompt

    messages_to_send = [{"role": "system", "content": full_system_prompt}]
    messages_to_send.extend(history)
    messages_to_send.append({"role": "user", "content": user_input})

    typing_task = asyncio.create_task(send_typing_periodically(bot, chat_id))

    try:
        timeout = httpx.Timeout(AI_HTTP_TIMEOUT_SEC, connect=AI_HTTP_CONNECT_TIMEOUT_SEC)

        async with httpx.AsyncClient(timeout=timeout) as client:
            last_err: Optional[Exception] = None

            for attempt in range(AI_RETRIES):
                try:
                    response = await client.post(
                        DEEPSEEK_API_URL,
                        headers={
                            "Authorization": f"Bearer {api_key}",
                            "Content-Type": "application/json",
                        },
                        json={
                            "model": DEEPSEEK_MODEL,
                            "messages": messages_to_send,
                            "max_tokens": AI_MAX_TOKENS,
                            "temperature": ai_temperature,
                        },
                    )

                    status = response.status_code

                    # 429 / 5xx -> ретраї з backoff (або Retry-After)
                    if status == 429 or status >= 500:
                        ra = _retry_after_seconds(response.headers)
                        delay = ra if ra is not None else _calc_backoff(attempt)

                        logger.warning(
                            f"DeepSeek тимчасово недоступний (status={status}), ретрай через {delay:.1f}s"
                        )

                        if attempt == AI_RETRIES - 1:
                            response.raise_for_status()

                        await asyncio.sleep(delay)
                        continue

                    # Інші 4xx — без ретраю
                    if 400 <= status < 500:
                        logger.error(
                            f"DeepSeek API error status={status}, body={_truncate_for_log(response.text)}"
                        )
                        return "Мур... Я заплутався в клубочку (API Error). 😿"

                    response.raise_for_status()

                    data = response.json()
                    if not data.get("choices"):
                        raise ValueError("Empty response")

                    message_response = data["choices"][0]["message"]

                    # Фінальна відповідь
                    ai_content = message_response.get("content", "")
                    return sanitize_reply(_clean_deepseek_thinking(ai_content))

                except (httpx.RequestError, httpx.HTTPStatusError, ValueError, json.JSONDecodeError) as e:
                    last_err = e
                    if attempt == AI_RETRIES - 1:
                        raise
                    await asyncio.sleep(_calc_backoff(attempt))

            # якщо сюди дійшли — піднімемо останню помилку (а не вигадуватимемо заглушки)
            if last_err:
                raise last_err

    except Exception as e:
        logger.error(f"Помилка AI: {e}", exc_info=True)
        return "Ой, щось пішло не так. Можливо, в мене заплуталися клубки ниток. 🧶"
    finally:
        typing_task.cancel()
        try:
            await typing_task
        except Exception:
            pass

    return "Ой, щось пішло не так. 🧶"


async def process_ai_response(
    user_id: int,
    chat_id: int,
    user_input: str,
    bot: Bot,
    application: Application,
    mode: str,
    message_to_reply_id: int,
    reply_context: str = None,
) -> None:
    try:
        await save_message(user_id, chat_id, "user", user_input)
        
        response_text = await get_ai_response(user_id, chat_id, user_input, bot, mode, reply_context)
        ai_message_ids: list[int] = []
        sticker_message_id: int | None = None

        # --- Sticker marker support (AI can request a sticker) ---
        response_text, sticker_keyword = _extract_sticker_marker(response_text)
        if sticker_keyword:
            try:
                if 'all_stickers_cache' not in application.bot_data:
                    await refresh_sticker_cache(application)
                stickers = application.bot_data.get('all_stickers_cache', [])
                match = next((s for s in stickers if (s.get('keyword') or '').strip().lower() == sticker_keyword), None)
                if match and match.get('file_unique_id'):
                    sticker_msg = await bot.send_sticker(
                        chat_id=chat_id,
                        sticker=match['file_unique_id'],
                        reply_to_message_id=message_to_reply_id
                    )
                    if sticker_msg:
                        sticker_message_id = sticker_msg.message_id
            except Exception:
                # Sticker is optional — never fail the whole response
                pass
        
        # If only sticker requested and no text left — do not send empty message
        if response_text:
            await save_message(user_id, chat_id, "assistant", response_text)

            # Використовуємо безпечну відправку
            ai_message_ids = await safe_send_message(
                bot, chat_id, response_text, message_to_reply_id
            )

        settings = await get_chat_settings(chat_id)
        if settings.get("ai_auto_clear_conversations", 0) == 1:
            await _schedule_ai_auto_clear(application, chat_id, user_id)
        if settings.get("auto_delete_actions", 0) == 1:
            await _schedule_ai_auto_delete(
                application,
                chat_id=chat_id,
                message_id=message_to_reply_id,
            )
            for msg_id in ai_message_ids:
                await _schedule_ai_auto_delete(
                    application,
                    chat_id=chat_id,
                    message_id=msg_id,
                )
            if sticker_message_id:
                await _schedule_ai_auto_delete(
                    application,
                    chat_id=chat_id,
                    message_id=sticker_message_id,
                )
        
    except Exception as e:
        logger.error(f"Помилка в process_ai_response: {e}")
        try:
            await bot.send_message(
                chat_id=chat_id,
                text="Мур... Щось пішло не так. 😿",
                reply_to_message_id=message_to_reply_id
            )
        except:
            pass


async def _ai_auto_clear_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    data = context.job.data or {}
    chat_id = data.get("chat_id")
    user_id = data.get("user_id")
    if chat_id is None or user_id is None:
        return
    await clear_conversations(user_id=user_id, chat_id=chat_id)


async def _schedule_ai_auto_clear(application: Application, chat_id: int, user_id: int) -> None:
    job_queue = application.job_queue if application else None
    if not job_queue:
        return
    job_name = f"ai_auto_clear:{chat_id}:{user_id}"
    for job in job_queue.get_jobs_by_name(job_name):
        job.schedule_removal()
    job_queue.run_once(
        _ai_auto_clear_job,
        when=600,
        name=job_name,
        data={"chat_id": chat_id, "user_id": user_id},
    )


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


async def _schedule_ai_auto_delete(
    application: Application,
    *,
    chat_id: int,
    message_id: int,
    timeout: int = 420,
) -> None:
    job_queue = application.job_queue if application else None
    if not job_queue:
        return
    job_queue.run_once(
        delete_message_job,
        timeout,
        data={"chat_id": chat_id, "message_id": message_id},
        name=f"delete_ai_{chat_id}_{message_id}",
    )


# =============================================================================
# 3. Private Helper Functions (Внутрішні помічники)
# =============================================================================

async def _is_ai_invocation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    chat = update.effective_chat
    message = update.message

    if not chat or not message:
        return False

    text_lower = message.text.lower() if message.text else ""
    # Видалено перевірку на підпис фото, бо ми не обробляємо фото
        
    # 1. Приватні повідомлення
    if chat.type == 'private':
        return True

    # 2. Отримуємо дані бота (надійно)
    if 'bot_id' not in context.application.bot_data or 'bot_username' not in context.application.bot_data:
         try:
             bot_info = await context.bot.get_me()
             context.application.bot_data['bot_username'] = bot_info.username.lower()
             context.application.bot_data['bot_id'] = bot_info.id
         except Exception as e:
             logger.error(f"Не вдалося отримати дані бота: {e}")

    bot_id = context.application.bot_data.get('bot_id')
    bot_username = context.application.bot_data.get('bot_username')

    # 3. Звернення за @username
    if bot_username and f"@{bot_username}" in text_lower:
        return True

    # 4. Ключове слово
    if re.search(r"\b(кошеня|котик|кіт|котику|кошенятко|котяра)\b", text_lower):
        return True

    # 5. Відповідь на повідомлення БОТА (покращена перевірка)
    if message.reply_to_message:
        reply = message.reply_to_message
        
        # Перевірка по ID (основна)
        if reply.from_user and reply.from_user.id == bot_id:
            return True
            
        # Резервна перевірка по username (якщо ID чомусь не співпав)
        if reply.from_user and reply.from_user.username and bot_username:
             if reply.from_user.username.lower() == bot_username:
                 return True

    return False


def _has_pending_reminder(context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Повертає True, якщо користувач зараз уточнює нагадування."""
    try:
        return bool(context.user_data.get("reminder_pending"))
    except Exception:
        return False


def _is_reminder_text(message) -> bool:
    """Перевіряє, чи є текст нагадування (щоб не дублюватися з remind router)."""
    if not message:
        return False
    txt = (message.text or message.caption or "").strip()
    if not txt:
        return False
    try:
        return is_reminder_trigger(txt)
    except Exception:
        return False


def _should_ai_process(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Єдиний вхідний фільтр.

    ВАЖЛИВО: не блокуємо AI через `reminder_pending`.
    Pending-нагадування має перехоплюватися router'ом у reminder_handlers (group=-2).
    Інакше, якщо pending "завис", AI починає ігнорувати *всі* повідомлення.
    """
    message = update.message
    if not message or not message.text:
        return False
    if message.text.startswith('/'):
        return False
    if _is_reminder_text(message):
        return False
    return True


_STICKER_MARKER_RE = re.compile(
    r"(?:\[\[sticker:(?P<kw1>[^\]]+)\]\])|(?:<sticker:(?P<kw2>[^>]+)>)",
    flags=re.IGNORECASE,
)


def _extract_sticker_marker(text: str) -> tuple[str, Optional[str]]:
    """Дістає з AI-відповіді маркер стікера і повертає (clean_text, keyword).

    Підтримує:
    - [[sticker: ключ]]
    - <sticker:ключ>
    """
    if not text:
        return text, None
    m = _STICKER_MARKER_RE.search(text)
    if not m:
        return text, None
    kw = (m.group("kw1") or m.group("kw2") or "").strip().lower()
    cleaned = (_STICKER_MARKER_RE.sub("", text, count=1)).strip()
    return cleaned, (kw or None)


async def _parse_natural_memory(fact_text: str) -> Optional[dict]:
    api_key = _get_api_key()
    if not api_key:
        logger.warning("DeepSeek API key відсутній для парсингу памʼяті, роблю запит без нього (можливий 401)")

    system_prompt = (
        "Ти — сервіс парсингу. "
        "Відповідай ТІЛЬКИ у форматі JSON: {\"key\": \"...\", \"value\": \"...\"}."
    )

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": fact_text}
    ]

    try:
        timeout = httpx.Timeout(20.0, connect=AI_HTTP_CONNECT_TIMEOUT_SEC)
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(
                DEEPSEEK_API_URL,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": DEEPSEEK_MODEL,
                    "messages": messages,
                    "max_tokens": 160,
                    "temperature": 0,
                },
            )
            response.raise_for_status()
            content = response.json()["choices"][0]["message"]["content"]
            # Clean possible markdown
            content_cleaned = re.sub(r"```json\s*|\s*```", "", content).strip()
            # Find first { and last }
            start = content_cleaned.find('{')
            end = content_cleaned.rfind('}')
            if start != -1 and end != -1:
                content_cleaned = content_cleaned[start:end+1]
                
            parsed_json = json.loads(content_cleaned)
            return parsed_json
    except Exception as e:
        logger.error(f"Помилка парсингу пам'яті: {e}")
        return None


async def _process_remember_logic(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat = update.effective_chat
    message = update.message
    if not message or not message.text: return ConversationHandler.END
    
    command_text = message.text
    text_lower = command_text.lower()
    user = update.effective_user
    adding_user_id = user.id

    # === РЕЖИМ 1: Природна мова ===
    cleaned_text_match = re.search(r"запам'ятай\s+що\s+(.+)$", text_lower, re.IGNORECASE | re.DOTALL)
    if cleaned_text_match:
        fact_start_index = cleaned_text_match.start(1)
        fact_text = command_text[fact_start_index:].strip()
        sent_msg = await update.message.reply_text("Аналізую, що треба запам'ятати... 🧠")
        parsed_kv = await _parse_natural_memory(fact_text)

        if not parsed_kv:
            await context.bot.edit_message_text(
                chat_id=chat.id, message_id=sent_msg.message_id,
                text="На жаль, я не зміг розібрати цей факт. 😿"
            )
            return ConversationHandler.END

        key, value = parsed_kv.get('key'), parsed_kv.get('value')
        if not key or not value:
             await context.bot.edit_message_text(chat_id=chat.id, message_id=sent_msg.message_id, text="Не вдалося виділити суть.")
             return ConversationHandler.END

        target_user = None
        if fact_text.lower().startswith('я '):
            target_user = user
        
        if target_user and target_user.id == user.id:
            await save_memory(user.id, 'user', key, value, adding_user_id)
            ctx = await get_user_addressing(user.id)
            await context.bot.edit_message_text(
                chat_id=chat.id, message_id=sent_msg.message_id,
                text=f"✅ {ctx.past('Запамʼятав', 'Запамʼятала', 'Запамʼятав')} для {ctx.you}: <b>{key}</b> = <b>{value}</b>", parse_mode='HTML'
            )
        else:
            await save_memory(chat.id, 'chat', key, value, adding_user_id)
            await context.bot.edit_message_text(
                chat_id=chat.id, message_id=sent_msg.message_id,
                text=f"✅ Запам'ятав для чату: <b>{key}</b> = <b>{value}</b>", parse_mode='HTML'
            )
        return ConversationHandler.END

    # === РЕЖИМ 2: Ручний ===
    command_name_match = re.search(r"(/remember|запам'ятай)\b", text_lower)
    if not command_name_match: return ConversationHandler.END
    
    args_text = command_text[command_name_match.end():].strip()
    args = args_text.split()
    
    if len(args) < 2:
        await update.message.reply_text("Потрібно вказати ключ та значення.")
        return ConversationHandler.END
        
    key_kv = args[0]
    value_kv = " ".join(args[1:])
    
    context.user_data['remember_key'] = key_kv
    context.user_data['remember_value'] = value_kv

    keyboard = [
        [InlineKeyboardButton("👤 Для мене (всюди)", callback_data="remember_scope_user")],
        [InlineKeyboardButton("👥 Для цього чату", callback_data="remember_scope_chat")],
        [InlineKeyboardButton("✖️ Скасувати", callback_data="remember_scope_cancel")],
    ]
    await update.message.reply_text(
        f"Гаразд, я готовий запам'ятати:\n<b>{key_kv}</b> = <b>{value_kv}</b>\nКуди зберегти?",
        reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML'
    )
    return STATE_REMEMBER_SCOPE


async def _process_forget_logic(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message or not update.message.text: return ConversationHandler.END
    
    command_text = update.message.text.strip()
    text_lower = command_text.lower()
    
    args_list = context.args if context.args else []
    if not args_list:
        match = re.search(r"(забудь)\s+(.+)", text_lower)
        if match: args_list = match.group(2).split()

    if not args_list:
        await update.message.reply_text("Що забути? Вкажи ключ.")
        return ConversationHandler.END

    key = " ".join(args_list)
    context.user_data['forget_key'] = key

    keyboard = [
        [InlineKeyboardButton("👤 З моєї пам'ять", callback_data="forget_scope_user")],
        [InlineKeyboardButton("👥 З пам'яті чату", callback_data="forget_scope_chat")],
        [InlineKeyboardButton("✖️ Скасувати", callback_data="forget_scope_cancel")],
    ]
    await update.message.reply_text(
        f"Я маю забути про <b>{key}</b>. Звідки?",
        reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML'
    )
    return STATE_FORGET_SCOPE


# =============================================================================
# 4. Memory Handlers (Обробники пам'яті)
# =============================================================================

async def remember_command_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await _is_ai_invocation(update, context): return ConversationHandler.END
    return await _process_remember_logic(update, context)

async def remember_scope_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    action = query.data.split('_')[-1]

    if action == 'cancel':
        await query.edit_message_text("Скасовано.")
        return ConversationHandler.END

    key = context.user_data.get('remember_key')
    value = context.user_data.get('remember_value')
    if not key: return ConversationHandler.END

    if action == 'user':
        await save_memory(update.effective_user.id, 'user', key, value, update.effective_user.id)
    elif action == 'chat':
        await save_memory(update.effective_chat.id, 'chat', key, value, update.effective_user.id)
        
    await query.edit_message_text(f"✅ Запам'ятав: {key} = {value}")
    return ConversationHandler.END

async def forget_command_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await _is_ai_invocation(update, context): return ConversationHandler.END
    return await _process_forget_logic(update, context)

async def forget_scope_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    action = query.data.split('_')[-1]
    
    if action == 'cancel':
        await query.edit_message_text("Скасовано.")
        return ConversationHandler.END
        
    key = context.user_data.get('forget_key')
    if action == 'user':
        await remove_memory(update.effective_user.id, 'user', key)
    elif action == 'chat':
        await remove_memory(update.effective_chat.id, 'chat', key)
        
    await query.edit_message_text(f"🗑️ Забув про {key}")
    return ConversationHandler.END

async def cancel_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.callback_query: await update.callback_query.edit_message_text("Скасовано.")
    else: await update.message.reply_text("Скасовано.")
    return ConversationHandler.END

async def memories_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("👤 Мою пам'ять", callback_data="show_mem_user")],
        [InlineKeyboardButton("👥 Пам'ять чату", callback_data="show_mem_chat")],
    ]
    await update.message.reply_text("Яку пам'ять показати?", reply_markup=InlineKeyboardMarkup(keyboard))

async def show_memories_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    scope = query.data.split('_')[-1]
    scope_id = update.effective_user.id if scope == 'user' else update.effective_chat.id
    memories = await get_memories_for_scope(scope_id, scope)
    
    if not memories:
        text = "Тут поки порожньо."
    else:
        text = "\n".join([f"- <b>{m['key']}</b>: {m['value']}" for m in memories])
    
    try: await query.edit_message_text(text, parse_mode='HTML')
    except: pass


# =============================================================================
# 5. Sticker & Mode Handlers
# =============================================================================

async def set_mode_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("🎓 Академічний", callback_data="set_mode_academic")],
        [InlineKeyboardButton("😼 Харизматичний", callback_data="set_mode_charismatic")],
    ]
    await update.message.reply_text("Обери режим:", reply_markup=InlineKeyboardMarkup(keyboard))

async def set_mode_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    mode = query.data.split('_')[-1]
    if mode in BOT_MODES:
        if 'user_ai_modes' not in context.chat_data: context.chat_data['user_ai_modes'] = {}
        context.chat_data['user_ai_modes'][query.from_user.id] = mode
        await query.edit_message_text(f"Режим змінено на: {mode}")

async def current_mode_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    mode = context.chat_data.get('user_ai_modes', {}).get(update.effective_user.id, DEFAULT_BOT_MODE)
    await update.message.reply_text(f"Поточний режим: {mode}")

async def refresh_sticker_cache(application: Application):
    try:
        all_stickers = await get_all_stickers()
        application.bot_data['all_stickers_cache'] = all_stickers
    except: pass

async def handle_sticker(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.sticker: return
    user = update.message.from_user
    pending_key = f"pending_sticker_{user.id}"
    
    if pending_key in context.user_data:
        alias = context.user_data.pop(pending_key).strip().lower()
        await save_sticker(alias, update.message.sticker.file_unique_id)
        await refresh_sticker_cache(context.application)
        await update.message.reply_text(f"Стікер збережено для '{alias}'")

async def handle_sticker_keyword(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text: return
    if 'all_stickers_cache' not in context.application.bot_data:
        await refresh_sticker_cache(context.application)
    
    stickers = context.application.bot_data.get('all_stickers_cache', [])
    text = update.message.text.lower()
    
    for s in stickers:
        if re.search(rf"(^|\W){re.escape(s['keyword'])}(\W|$)", text):
            try: await update.message.reply_sticker(s['file_unique_id'])
            except: pass
            return

async def handle_katya_reaction(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message and update.message.text:
        if re.search(r"\b(катя|русня)\b", update.message.text.lower()):
            try: await context.bot.set_message_reaction(update.message.chat.id, update.message.message_id, "🤮")
            except: pass

async def set_emoji_reactions_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args or []
    if not args or args[0].lower() not in ("on", "off"):
        status = "увімкнені" if context.chat_data.get('emoji_reactions_enabled', True) else "вимкнені"
        await update.message.reply_text(f"Emoji-реакції {status}. /setemojireactions on|off")
        return
    context.chat_data['emoji_reactions_enabled'] = (args[0].lower() == "on")
    await update.message.reply_text(f"Emoji-реакції {'увімкнено' if args[0].lower()=='on' else 'вимкнено'}.")

# =============================================================================
# 6. Main Message Handler
# =============================================================================

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat = update.effective_chat
    message = update.message

    if not user or not chat or not message or not message.text:
        return

    if not _should_ai_process(update, context):
        return

    if not await is_ai_enabled_for_chat(chat.id):
        return
    
    # Визначаємо, чи це звернення до ШІ
    is_invocation = await _is_ai_invocation(update, context)
    if not is_invocation: return

    # Прості відповіді
    text_lower = message.text.lower()
    for keys, resps in SIMPLE_RESPONCES.items():
        if any(k in text_lower for k in keys):
            await message.reply_text(random.choice(resps))
            return

    try:
        await context.bot.send_chat_action(chat_id=chat.id, action=ChatAction.TYPING)
    except Exception:
        pass

    # Rate Limit Check
    # Якщо це пряма відповідь (reply) на повідомлення бота — ігноруємо rate limit
    is_direct_reply = False
    if message.reply_to_message:
        bot_id = context.application.bot_data.get('bot_id')
        if bot_id and message.reply_to_message.from_user and message.reply_to_message.from_user.id == bot_id:
            is_direct_reply = True

    if not is_direct_reply:
        now = time.time()
        last_req = _user_last_request.get(user.id, 0)
        if now - last_req < USER_RATE_LIMIT:
            return 
        _user_last_request[user.id] = now

    reply_context = None
    if message.reply_to_message:
        reply_txt = message.reply_to_message.text or message.reply_to_message.caption
        if reply_txt:
            reply_context = reply_txt

    # Черга ШІ
    mode = context.chat_data.get('user_ai_modes', {}).get(user.id, DEFAULT_BOT_MODE)
    task_data = {
        'user_id': user.id, 'user_input': message.text,
        'mode': mode, 'message_to_reply_id': message.message_id,
        'reply_context': reply_context 
    }
    # Передаємо application, щоб у воркері був доступ до bot_data (кеш стікерів тощо)
    task_data['application'] = context.application
    asyncio.create_task(ai_queue_manager.add_task(chat.id, context.bot, task_data))


# =============================================================================
# 7. Registration
# =============================================================================

def register_ai_handlers(application: Application):
    # Команди
    application.add_handler(CommandHandler("aimode", aimode_command))
    application.add_handler(CommandHandler("aireset", aireset_command))
    application.add_handler(CommandHandler("aiclear", aiclear_command))
    application.add_handler(CommandHandler("aihelp", aihelp_command))
    application.add_handler(CommandHandler("setemojireactions", set_emoji_reactions_command))
    
    # Режими (Legacy)
    application.add_handler(CommandHandler("set_mode", set_mode_command))
    application.add_handler(CommandHandler("current_mode", current_mode_command))
    application.add_handler(CallbackQueryHandler(set_mode_callback, pattern=r"^set_mode_"))

    # Пам'ять
    remember_conv = ConversationHandler(
        entry_points=[
            CommandHandler("remember", remember_command_entry),
            MessageHandler(filters.TEXT & filters.Regex(r"(?i).*\b(запам'ятай|запамʼятай)\b.*"), remember_command_entry)
        ],
        states={
            STATE_REMEMBER_SCOPE: [CallbackQueryHandler(remember_scope_callback, pattern=r"^remember_scope_")]
        },
        fallbacks=[CommandHandler("cancel", cancel_conversation)],
    )
    application.add_handler(remember_conv)

    forget_conv = ConversationHandler(
        entry_points=[
            CommandHandler("forget", forget_command_entry),
            MessageHandler(filters.TEXT & filters.Regex(r"(?i).*\b(забудь)\b.*"), forget_command_entry)
        ],
        states={
            STATE_FORGET_SCOPE: [CallbackQueryHandler(forget_scope_callback, pattern=r"^forget_scope_")]
        },
        fallbacks=[CommandHandler("cancel", cancel_conversation)],
    )
    application.add_handler(forget_conv)
    
    application.add_handler(CommandHandler("memories", memories_command))
    application.add_handler(CallbackQueryHandler(show_memories_callback, pattern=r"^show_mem_"))

    # Стікери та текст
    application.add_handler(MessageHandler(filters.Sticker.ALL, handle_sticker), group=0)
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_katya_reaction), group=2)
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_sticker_keyword), group=2)
    
    # Видалено обробник фотографій (handle_photo)
    
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message), group=5)

    logger.info("Всі обробники ШІ (текстові) успішно зареєстровані. 🌿")
