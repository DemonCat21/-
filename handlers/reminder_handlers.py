# reminder_handlers.py
# -*- coding: utf-8 -*-
"""
Модуль нагадувань. 🗓️
Дозволяє користувачам зберігати
та керувати своїми нагадуваннями. 🌿
"""

import logging
import re
import html
import asyncio
import dateparser
from dateparser.search import search_dates
import pytz
from datetime import datetime, timedelta, date, time
from dateutil.relativedelta import relativedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
    CallbackQueryHandler,
    JobQueue,
    ConversationHandler,
)
from telegram.ext._utils.types import HandlerCallback
from telegram.ext.filters import MessageFilter
try:
    from telegram.ext import ApplicationHandlerStop
except ImportError:
    # Для старіших версій PTB
    class ApplicationHandlerStop(Exception):
        pass

from bot.core.database import (
    add_reminder,
    get_user_reminders_count,
    get_user_reminders,
    set_reminder_job_name,
    get_reminder,
    remove_reminder,
    get_all_reminders,
    update_reminder_time_and_job,
    get_chat_settings,
    set_reminder_status,
    set_module_status,
)
from bot.utils.utils import (
    cancel_auto_close,
    get_user_addressing,
    mention,
    set_auto_close_payload,
    start_auto_close,
)

logger = logging.getLogger(__name__)

# --- Налаштування ---
REMINDER_LIMIT = 10  # Макс. активних нагадувань
USER_TIMEZONE = pytz.timezone("Europe/Kyiv")
REMINDERS_AUTO_CLOSE_KEY = "reminders_menu"
CB_REMINDERS_CLOSE = "reminders:close"

# =============================================================================
# Активація "звичайних" нагадувань (лише з префіксом звернення)
# =============================================================================

# === Строгі тригери нагадувань (ТІЛЬКИ ці, без варіацій) ===
# ВАЖЛИВО: перевіряємо вже НОРМАЛІЗОВАНИЙ текст (NFC + прибрані zero-width/NBSP).
REMINDER_TRIGGERS = (
    "кошеня, нагадай",
    "кошеня нагадай",
    "котик, нагадай",
    "котик нагадай",
    "котику, нагадай",
    "котику нагадай",
    "бот, нагадай",
    "бот нагадай",
    "ботик, нагадай",
    "ботик нагадай",
    "ботику, нагадай",
    "ботику нагадай",
)

def _starts_with_trigger(s: str) -> tuple[bool, int]:
    """Повертає (True, len(trigger)) якщо рядок починається з дозволеного тригера і має межу.
    Межа: кінець рядка або пробільний символ після тригера.
    """
    for trig in REMINDER_TRIGGERS:
        if s.startswith(trig):
            nxt = s[len(trig):len(trig)+1]
            if nxt == "" or nxt.isspace():
                return True, len(trig)
    return False, 0

def is_reminder_trigger(text: str) -> bool:
    s = normalize_text(text or "").lower().lstrip()
    ok, _ = _starts_with_trigger(s)
    return ok

def strip_trigger_prefix(text: str) -> tuple[bool, str]:
    """(activated, rest_text). Якщо не активовано — повертає (False, original)."""
    s_orig = normalize_text(text or "")
    s = s_orig.lower().lstrip()
    ok, n = _starts_with_trigger(s)
    if not ok:
        return False, text
    # Відрізаємо від ОРИГІНАЛЬНОГО рядка: вирівнюємо індекс через lstrip
    lstrip_len = len(s_orig) - len(s_orig.lstrip())
    cut = lstrip_len + n
    return True, (s_orig[cut:]).strip()

# PTB custom filter: гарантує, що тригери ловляться ДО AI.

class ReminderTriggerFilter(MessageFilter):
    def filter(self, message) -> bool:  # message is telegram.Message
        try:
            return bool(message and message.text and is_reminder_trigger(message.text))
        except Exception:
            return False

MIN_REMINDER_TIME_SEC = 30 # Мінімальний час для нагадування

# (ОНОВЛЕНО) Розширені патерни, які враховують різні закінчення та помилки
# Апострофи тут не обов'язкові, бо ми нормалізуємо текст перед перевіркою.
RECUR_PATTERNS = {
    "daily": r"\b(щодня|кожен день|every day|щоденно)\b",
    "weekly": r"\b(щотижня|кожен тиждень|every week|щопонеділк[ау]|щовівторк[ау]|щосеред[иа]|щочетверг[ау]|щоп['’`]?ятниц[ію]|пятниц[ію]|щосубот[иу]|щонеділ[ію]|кожної неділі|кожного понеділка|кожного вівторка|кожної середи|кожного четверга|кожної п['’`]?ятниці|кожної суботи)\b",
    "monthly": r"\b(щомісяця|кожен місяць|every month|щомісячно)\b",
}

RECUR_STRINGS = {
    "daily": "🌿 Щодня (о",
    "weekly": "🌿 Щотижня (по %A, о",
    "monthly": "🌿 Щомісяця (%d числа, о",
}


def normalize_text(text: str) -> str:
    """
    Нормалізує текст для надійного парсингу (UA):
    - NFC (щоб 'й' не приходило як 'и'+combining)
    - прибирає zero-width
    - NBSP -> звичайний пробіл
    - уніфікує апострофи
    - виправляє типові помилки вводу
    """
    import unicodedata

    if not text:
        return ""

    # 0) Unicode normalization + cleanup (Telegram/Windows часто дає combining)
    text = unicodedata.normalize("NFC", text)
    text = text.replace("\u00A0", " ")  # NBSP
    text = re.sub(r"[\u200B-\u200D\uFEFF]", "", text)  # zero-width
    # 1. Нормалізація апострофів (’, `, ‘, ʼ -> ')
    text = re.sub(r"[’`‘ʼ]", "'", text)
    
    # 2. Виправлення "пятниця" -> "п'ятниця" (для dateparser)
    text = re.sub(r"\bпятниц", "п'ятниц", text, flags=re.IGNORECASE)
    

    # 3. Скорочення днів тижня -> повні назви (для dateparser)
    day_map = {
        "пн": "понеділок",
        "вт": "вівторок",
        "ср": "середа",
        "чт": "четвер",
        "пт": "п'ятниця",
        "сб": "субота",
        "нд": "неділя",
    }
    for short, full in day_map.items():
        text = re.sub(rf"\b{short}\b", full, text, flags=re.IGNORECASE)

    return text

def _extract_explicit_datetime_parts(src: str):
    """Витягає явну числову дату/час з тексту (ДД.ММ[.РРРР], ДД/ММ, YYYY-MM-DD, 14:30, 14.30, 14 30, 'о 14', 'о 14:30').
    Повертає: (date_obj|None, (h,m)|None, cleaned_text, had_explicit_time: bool)
    """
    s = src

    date_obj = None
    time_hm = None
    had_explicit_time = False

    # YYYY-MM-DD
    m = re.search(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b", s)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        try:
            date_obj = date(y, mo, d)
        except ValueError:
            date_obj = None
        s = (s[:m.start()] + " " + s[m.end():]).strip()

    # DD.MM[.YYYY] or DD/MM[/YYYY]
    if date_obj is None:
        m = re.search(r"\b(\d{1,2})[\./](\d{1,2})(?:[\./](\d{2,4}))?\b", s)
        if m:
            d, mo = int(m.group(1)), int(m.group(2))
            y_raw = m.group(3)
            try:
                if y_raw:
                    y = int(y_raw)
                    if y < 100:
                        y += 2000
                    date_obj = date(y, mo, d)
                else:
                    # без року: беремо поточний рік, але якщо дата вже минула — наступний
                    today = datetime.now(USER_TIMEZONE).date()
                    y = today.year
                    tmp = date(y, mo, d)
                    if tmp < today:
                        tmp = date(y + 1, mo, d)
                    date_obj = tmp
            except ValueError:
                date_obj = None
            s = (s[:m.start()] + " " + s[m.end():]).strip()

    # explicit time: HH:MM / HH.MM / HH MM / 'о HH' / 'о HH:MM'
    # HH:MM / HH.MM
    m = re.search(r"\b(?:о\s*)?(\d{1,2})\s*[:\.](\d{2})\b", s)
    if m:
        h, mi = int(m.group(1)), int(m.group(2))
        if 0 <= h <= 23 and 0 <= mi <= 59:
            time_hm = (h, mi)
            had_explicit_time = True
            s = (s[:m.start()] + " " + s[m.end():]).strip()

    # HH MM
    if time_hm is None:
        m = re.search(r"\b(?:о\s*)?(\d{1,2})\s+(\d{2})\b", s)
        if m:
            h, mi = int(m.group(1)), int(m.group(2))
            if 0 <= h <= 23 and 0 <= mi <= 59:
                time_hm = (h, mi)
                had_explicit_time = True
                s = (s[:m.start()] + " " + s[m.end():]).strip()

    # 'о HH'
    if time_hm is None:
        m = re.search(r"\bо\s*(\d{1,2})\b", s)
        if m:
            h = int(m.group(1))
            if 0 <= h <= 23:
                time_hm = (h, 0)
                had_explicit_time = True
                s = (s[:m.start()] + " " + s[m.end():]).strip()

    return date_obj, time_hm, re.sub(r"\s+", " ", s).strip(), had_explicit_time



def _format_target_mention(user) -> str:
    """Для груп: спочатку @username, інакше HTML mention."""
    try:
        if getattr(user, "username", None):
            return f"@{user.username}"
    except Exception:
        pass
    return mention(user)


def _dedup_job_by_name(job_queue: JobQueue, job_name: str) -> None:
    """Запобігає дублю jobʼів на один reminder_id."""
    try:
        for j in job_queue.get_jobs_by_name(job_name):
            j.schedule_removal()
    except Exception:
        pass



def _is_duplicate_update(context: ContextTypes.DEFAULT_TYPE, update: Update) -> bool:
    """Легка страховка від повторних апдейтів (інколи Telegram може надсилати дубль).
    Тримаємо невелике LRU-вікно в bot_data.
    """
    try:
        upd_id = getattr(update, "update_id", None)
        if upd_id is None:
            return False
        key = "recent_update_ids"
        recent = context.application.bot_data.get(key)
        if recent is None:
            recent = []
            context.application.bot_data[key] = recent
        if upd_id in recent:
            return True
        recent.append(upd_id)
        if len(recent) > 500:
            del recent[:200]
        return False
    except Exception:
        return False


async def _create_and_schedule_reminder(
    *,
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    run_at_local: datetime,
    message_text: str,
    recur_interval: str | None,
    delivery_chat_id: int,
) -> None:
    """Єдина точка створення нагадування: БД -> JobQueue -> підтвердження."""
    user = update.effective_user
    if not user or not update.message:
        return

    reminder_time_utc = run_at_local.astimezone(pytz.utc)
    reminder_id = await add_reminder(
        user.id,
        delivery_chat_id,
        message_text,
        reminder_time_utc.isoformat(),
        None,
        recur_interval=recur_interval,
    )

    job_queue = context.application.job_queue
    if job_queue and reminder_id:
        job_name = f"reminder_{reminder_id}"
        _dedup_job_by_name(job_queue, job_name)
        job_queue.run_once(
            reminder_job_callback,
            when=reminder_time_utc,
            data={"reminder_id": reminder_id},
            name=job_name,
        )
        await set_reminder_job_name(reminder_id, job_name)

    when_str = run_at_local.strftime("%d.%m %H:%M")
    extra = ""
    if recur_interval:
        extra = " і повторюватиму " + (
            "щодня"
            if recur_interval == "daily"
            else "щотижня"
            if recur_interval == "weekly"
            else "щомісяця"
        )

    # Отримуємо контекст звернення
    ctx = await get_user_addressing(user.id)
    
    if delivery_chat_id < 0:
        mention_str = _format_target_mention(user)
        await update.message.reply_html(
            f"😼 Ок, {mention_str}. {ctx.past('Нагадаю', 'Нагадаю', 'Нагадаю')} тут <b>{when_str}</b>: «{html.escape(message_text)}»{extra}"
        )
    else:
        await update.message.reply_html(
            f"😼 {ctx.past('Запамʼятав', 'Запамʼятала', 'Запамʼятав')}. {ctx.past('Нагадаю', 'Нагадаю', 'Нагадаю')} <b>{when_str}</b>: «{html.escape(message_text)}»{extra}"
        )


async def _handle_pending_reminder(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Підхоплює 1–2 уточнення після запуску нагадування."""
    if not update.message or not update.message.text:
        return False

    pending = context.user_data.get("reminder_pending")
    if not pending:
        return False

    txt_norm = normalize_text(update.message.text).strip()
    txt_low = txt_norm.lower()

    if re.fullmatch(r"(скасуй|відміни|cancel|стоп)", txt_low, flags=re.IGNORECASE):
        context.user_data.pop("reminder_pending", None)
        ctx = await get_user_addressing(update.effective_user.id)
        await update.message.reply_html(f"😼 Ок. {ctx.past('Скасував', 'Скасувала', 'Скасував')}.")
        return True

    try:
        expires_at = pending.get("expires_at")
        if expires_at:
            exp = datetime.fromisoformat(expires_at)
            if exp.tzinfo is None:
                exp = USER_TIMEZONE.localize(exp)
            if datetime.now(USER_TIMEZONE) > exp:
                context.user_data.pop("reminder_pending", None)
                return False
    except Exception:
        pass

    stage = pending.get("stage")
    delivery_chat_id = int(pending.get("delivery_chat_id", update.effective_chat.id))

    if stage == "when":
        what = pending.get("what") or ""
        combined = (txt_norm + " " + what).strip()
        run_at_local, message_text, recur_interval = _parse_reminder_text(combined)
        if not run_at_local:
            await update.message.reply_html(message_text)
            return True

        await _create_and_schedule_reminder(
            update=update,
            context=context,
            run_at_local=run_at_local,
            message_text=message_text,
            recur_interval=recur_interval,
            delivery_chat_id=delivery_chat_id,
        )
        context.user_data.pop("reminder_pending", None)
        return True

    if stage == "what":
        run_at_iso = pending.get("run_at_local")
        recur_interval = pending.get("recur_interval")
        if not run_at_iso:
            context.user_data.pop("reminder_pending", None)
            return False

        try:
            run_at_local = datetime.fromisoformat(run_at_iso)
            if run_at_local.tzinfo is None:
                run_at_local = USER_TIMEZONE.localize(run_at_local)
            else:
                run_at_local = run_at_local.astimezone(USER_TIMEZONE)
        except Exception:
            context.user_data.pop("reminder_pending", None)
            return False

        message_text = update.message.text.strip()
        if not message_text:
            ctx = await get_user_addressing(update.effective_user.id)
            await update.message.reply_html(f"😼 Окей. А про що {ctx.verb('нагадати', 'нагадати', 'нагадати', 'нагадати')}?")
            return True

        await _create_and_schedule_reminder(
            update=update,
            context=context,
            run_at_local=run_at_local,
            message_text=message_text,
            recur_interval=recur_interval,
            delivery_chat_id=delivery_chat_id,
        )
        context.user_data.pop("reminder_pending", None)
        return True

    return False



async def _send_reminder_messages(
    context: ContextTypes.DEFAULT_TYPE,
    reminder_data: dict,
    missed_at_iso: str | None,
):
    """Ізольована логіка надсилання повідомлень з кнопками 'Відкласти'."""
    user_id = reminder_data["user_id"]
    # Розширена модель: куди доставляти та кого тегати
    delivery_chat_id = reminder_data.get("delivery_chat_id") or reminder_data.get("chat_id")
    target_user_id = reminder_data.get("target_user_id") or user_id
    message_text = reminder_data["message_text"]

    missed_text = ""
    if missed_at_iso:
        try:
            missed_time = datetime.fromisoformat(missed_at_iso).astimezone(
                USER_TIMEZONE
            )
            missed_text = f"\n\n(❗️<i>Це нагадування мало спрацювати {missed_time.strftime('%d.%m %H:%M')}, але я був офлайн 😴</i>)"
        except Exception:
            pass

    final_text = (
        f"🔔 <b>Нагадування!</b> Час настав!\n\n"
        f"Ви просили не забути:\n<i>{html.escape(message_text)}</i>"
        f"{missed_text}"
    )

    # Кнопки "Відкласти"
    keyboard = [
        [
            InlineKeyboardButton("💤 10 хв", callback_data="snooze_10"),
            InlineKeyboardButton("💤 30 хв", callback_data="snooze_30"),
            InlineKeyboardButton("💤 1 год", callback_data="snooze_60"),
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    if delivery_chat_id == target_user_id:
        try:
            await context.bot.send_message(
                chat_id=delivery_chat_id,
                text=final_text,
                parse_mode=ParseMode.HTML,
                reply_markup=reply_markup
            )
        except Exception as e:
            logger.warning(f"Не вдалося надіслати ПП-нагадування {user_id}: {e}")

    # Якщо доставка в групу/супергрупу — робимо повідомлення з тегом автора
    if delivery_chat_id != target_user_id:
        try:
            mention_str = None
            name_str = None
            try:
                cm = await context.bot.get_chat_member(delivery_chat_id, target_user_id)
                u = cm.user
                name_str = getattr(u, "first_name", None) or getattr(u, "full_name", None)
                mention_str = _format_target_mention(u)
            except Exception:
                pass

            if not mention_str:
                # Фолбек: без згадки, але з імʼям якщо дістали
                mention_str = html.escape(name_str) if name_str else "хтось"

            await context.bot.send_message(
                chat_id=delivery_chat_id,
                text=f"⏰ {mention_str}, <b>нагадую:</b> <i>{html.escape(message_text)}</i>{missed_text}",
                parse_mode=ParseMode.HTML,
                reply_markup=reply_markup
            )
        except Exception as e:
            logger.warning(f"Не вдалося надіслати нагадування в чат {delivery_chat_id}: {e}")


async def _reschedule_recurring_job(
    context: ContextTypes.DEFAULT_TYPE, reminder_data: dict
):
    """Ізольована логіка перепланування."""
    reminder_id = reminder_data["id"]
    recur_interval = reminder_data.get("recur_interval")

    try:
        current_time_utc = datetime.fromisoformat(reminder_data["reminder_time"])
        if current_time_utc.tzinfo is None:
            current_time_utc = pytz.utc.localize(current_time_utc)

        next_time_utc = None
        if recur_interval == "daily":
            next_time_utc = current_time_utc + timedelta(days=1)
        elif recur_interval == "weekly":
            next_time_utc = current_time_utc + timedelta(weeks=1)
        elif recur_interval == "monthly":
            next_time_utc = current_time_utc + relativedelta(months=1)

        if next_time_utc:
            new_job_name = f"reminder_{reminder_id}"
            _dedup_job_by_name(context.job_queue, new_job_name)
            context.job_queue.run_once(
                reminder_job_callback,
                next_time_utc,
                data={"reminder_id": reminder_id},
                name=new_job_name,
            )
            await update_reminder_time_and_job(
                reminder_id, next_time_utc.isoformat(), new_job_name
            )
            logger.info(
                f"Нагадування {reminder_id} переплановано на {next_time_utc.isoformat()}"
            )
    except Exception as e:
        logger.error(
            f"Помилка при переплануванні нагадування {reminder_id}: {e}", exc_info=True
        )


async def reminder_job_callback(context: ContextTypes.DEFAULT_TYPE):
    """Виконується, коли настає час нагадування."""
    job = context.job
    reminder_id = job.data.get("reminder_id")

    if not reminder_id:
        logger.error(f"Job {job.name} (ID: {reminder_id}) виконавcя без reminder_id.")
        return

    reminder_data = None
    try:
        reminder_data = await get_reminder(reminder_id)
        if not reminder_data:
            logger.warning(
                f"Job {job.name} (ID: {reminder_id}) ran, але дані в БД не знайдено."
            )
            return

        logger.info(
            f"Виконую нагадування {reminder_id} (інтервал: {reminder_data.get('recur_interval')}) для user {reminder_data['user_id']}."
        )


        # Перевірка: чи дозволені нагадування в чаті доставки (для груп/супергруп)
        delivery_chat_id = reminder_data.get("delivery_chat_id") or reminder_data.get("chat_id")
        if isinstance(delivery_chat_id, int) and delivery_chat_id < 0:
            settings = await get_chat_settings(delivery_chat_id)
            if int(settings.get("reminders_enabled", 1) or 1) == 0:
                await set_reminder_status(reminder_id, "SUPPRESSED")
                logger.info(f"Нагадування {reminder_id} приглушено: reminders_enabled=0 для чату {delivery_chat_id}.")
                return

        await _send_reminder_messages(context, reminder_data, job.data.get("missed_at"))

        if reminder_data.get("recur_interval"):
            await _reschedule_recurring_job(context, reminder_data)
        else:
            await remove_reminder(reminder_id)

    except Exception as e:
        logger.error(
            f"Критична помилка під час reminder_job_callback (ID: {reminder_id}): {e}",
            exc_info=True,
        )
        if reminder_data and not reminder_data.get("recur_interval"):
            logger.warning(
                f"Видаляю невдале одноразове нагадування {reminder_id}, щоб запобігти циклу помилок."
            )
            await remove_reminder(reminder_id)



def _parse_reminder_text(text: str) -> tuple[datetime | None, str, str | None]:
    """
    Двоетапний парсинг:
    A) витяг datetime
    B) решта тексту = reminder_text (без вимоги "про")

    Повертає: (run_at_local, reminder_text, recur_interval)
      - run_at_local: aware datetime у Europe/Kyiv (або None + пояснення в reminder_text)
      - reminder_text: або текст нагадування, або повідомлення-підказка для юзера
      - recur_interval: None | "daily" | "weekly" | "weekly_by_weekday" | "monthly"
    """
    cleaned_text = normalize_text(text or "").strip()
    if not cleaned_text:
        return None, "😼 Зрозуміло. Коли нагадати?", None

    # 1) Витягаємо repeat-правило (і прибираємо його з тексту)
    recur_interval = None
    for interval, pattern in RECUR_PATTERNS.items():
        if re.search(pattern, cleaned_text, re.IGNORECASE):
            recur_interval = interval
            cleaned_text = re.sub(pattern, "", cleaned_text, flags=re.IGNORECASE).strip()
            break

    now_local = datetime.now(USER_TIMEZONE)

    # 2) Шукаємо дату/час у довільному тексті
    
    # 1.5) Спершу пробуємо руками витягнути явні числові дату/час (бо dateparser інколи "роз'єднує" 'сьогодні' і '15:17'
    explicit_date, explicit_time, cleaned_wo_explicit, had_explicit_time = _extract_explicit_datetime_parts(cleaned_text)

    # Також підтримуємо "сьогодні/завтра/післязавтра" разом з явним часом
    explicit_day_offset = None
    if explicit_date is None and explicit_time is not None:
        if re.search(r"\bсьогодні\b", cleaned_text, flags=re.IGNORECASE):
            explicit_day_offset = 0
        elif re.search(r"\bзавтра\b", cleaned_text, flags=re.IGNORECASE):
            explicit_day_offset = 1
        elif re.search(r"\bпіслязавтра\b", cleaned_text, flags=re.IGNORECASE):
            explicit_day_offset = 2

    manual_found = None
    if explicit_date is not None or (explicit_time is not None and explicit_day_offset is not None):
        try:
            base_date = explicit_date
            if base_date is None and explicit_day_offset is not None:
                base_date = (now_local + timedelta(days=explicit_day_offset)).date()

            if base_date is not None:
                if explicit_time is None:
                    # дата без часу -> 09:00
                    run_dt = datetime.combine(base_date, time(9, 0))
                else:
                    run_dt = datetime.combine(base_date, time(explicit_time[0], explicit_time[1]))
            else:
                # лише час без дати
                h, mi = explicit_time
                tentative = now_local.replace(hour=h, minute=mi, second=0, microsecond=0)
                if tentative <= now_local:
                    tentative = tentative + timedelta(days=1)
                run_dt = tentative

            if run_dt.tzinfo is None:
                run_dt = USER_TIMEZONE.localize(run_dt)
            else:
                run_dt = run_dt.astimezone(USER_TIMEZONE)

            manual_found = [("__manual__", run_dt)]
            cleaned_text = cleaned_wo_explicit
        except Exception:
            manual_found = None
    found = manual_found if manual_found is not None else search_dates(
        text,
        languages=["uk"],
        settings={
            "TIMEZONE": "Europe/Kyiv",
            "RETURN_AS_TIMEZONE_AWARE": True,
            "PREFER_DATES_FROM": "future",
        },
    )
    if not found:
        # Не відмовляємо “в нуль” — просимо уточнення
        return None, "😼 Зрозуміло. Коли нагадати?", recur_interval

    # --- ВАЖЛИВО ---
    # dateparser часто повертає окремо "сьогодні" (00:00) і окремо "15:02".
    # Якщо ми сліпо беремо found[0], то отримаємо 00:00 -> 09:00 і це вже "в минулому".
    # Тому:
    # 1) пробуємо зібрати (дата/день) + (час) у один datetime
    # 2) якщо не вийшло — беремо перший кандидат у майбутньому

    time_re = re.compile(r"\b(?:[01]?\d|2[0-3])(?:[:\.\s])[0-5]\d\b|\b(?:[01]?\d|2[0-3])\b")
    date_keywords_re = re.compile(
        r"\b(сьогодні|завтра|післязавтра|понеділок|вівторок|середа|четвер|п'ятниця|субота|неділя)\b|\b\d{1,2}[./-]\d{1,2}(?:[./-]\d{2,4})?\b|\b\d{4}-\d{2}-\d{2}\b",
        re.IGNORECASE,
    )

    date_part = None
    time_part = None
    date_match_text = None
    time_match_text = None

    for mt, dt in found:
        if not dt or not mt:
            continue
        if date_part is None and date_keywords_re.search(mt):
            date_part = dt
            date_match_text = mt
            continue
        # time-only: містить цифри часу, але НЕ містить ключів дати
        if time_part is None and time_re.search(mt) and not date_keywords_re.search(mt):
            time_part = dt
            time_match_text = mt

    combined_candidate = None
    combined_matched = None
    if date_part is not None and time_part is not None:
        try:
            # беремо дату з date_part і час з time_part
            dp_local = date_part.astimezone(USER_TIMEZONE) if getattr(date_part, "tzinfo", None) else USER_TIMEZONE.localize(date_part)
            tp_local = time_part.astimezone(USER_TIMEZONE) if getattr(time_part, "tzinfo", None) else USER_TIMEZONE.localize(time_part)
            combined_candidate = dp_local.replace(hour=tp_local.hour, minute=tp_local.minute, second=0, microsecond=0)
            # matched_text для видалення: спробуємо прибрати обидва шматки
            combined_matched = (date_match_text or "") + " " + (time_match_text or "")
        except Exception:
            combined_candidate = None

    # Збираємо список кандидатів
    candidates: list[tuple[str, datetime]] = []
    if combined_candidate is not None:
        candidates.append((combined_matched or "", combined_candidate))

    for mt, dt in found:
        if not dt:
            continue
        try:
            dt_local = dt.astimezone(USER_TIMEZONE)
        except Exception:
            dt_local = USER_TIMEZONE.localize(dt) if dt.tzinfo is None else dt
        candidates.append((mt or "", dt_local))

    # Фільтр: беремо перший кандидат у майбутньому
    min_future_time = now_local + timedelta(seconds=MIN_REMINDER_TIME_SEC)
    chosen_mt = None
    chosen_dt = None
    for mt, dt_local in candidates:
        # Дефолт 09:00 тільки якщо це "дата без часу" (немає явного часу в matched_text)
        if dt_local.hour == 0 and dt_local.minute == 0 and not time_re.search(mt or ""):
            dt_local = dt_local.replace(hour=9, minute=0, second=0, microsecond=0)
        explicit_time_in_mt = bool(time_re.search(mt or "")) or had_explicit_time
        if dt_local >= min_future_time or (explicit_time_in_mt and dt_local > now_local):
            chosen_mt, chosen_dt = mt, dt_local
            break

    if chosen_dt is None:
        # якщо нічого в майбутньому — даємо дружнє уточнення
        # якщо хоч щось на сьогодні було — підкажемо "сьогодні?"
        for mt, dt_local in candidates:
            try:
                dt_cmp = dt_local
                if dt_cmp.hour == 0 and dt_cmp.minute == 0 and not time_re.search(mt or ""):
                    dt_cmp = dt_cmp.replace(hour=9, minute=0, second=0, microsecond=0)
                if dt_cmp.date() == now_local.date():
                    return None, "😼 Підійде на сьогодні? Якщо ні — напишіть 'завтра' або точну дату.", recur_interval
            except Exception:
                pass
        return None, "😼 Це вже в минулому. Напишіть, на коли перенести?", recur_interval

    matched_text, parsed_time_local = chosen_mt, chosen_dt

    # 3) Мінімальний час у майбутньому (додаткова страховка)
    min_future_time = now_local + timedelta(seconds=MIN_REMINDER_TIME_SEC)
    if parsed_time_local < min_future_time:
        if parsed_time_local.date() == now_local.date():
            return None, "😼 Підійде на сьогодні? Якщо ні — напишіть 'завтра' або точну дату.", recur_interval
        return None, "😼 Це вже в минулому. Напишіть, на коли перенести?", recur_interval

    # 5) Ремайндер-текст = решта без datetime-фрагмента
    reminder_text = cleaned_text
    
    # Видаляємо matched_text (основний datetime-фрагмент)
    if matched_text:
        reminder_text = re.sub(re.escape(matched_text), " ", reminder_text, count=1, flags=re.IGNORECASE)
    
    # Видаляємо ВСІ залишки слів дати/часу
    # Ключові слова днів
    reminder_text = re.sub(r"\b(сьогодні|завтра|післязавтра|позавчора)\b", " ", reminder_text, flags=re.IGNORECASE)
    reminder_text = re.sub(r"\b(понеділок|вівторок|середа|середу|четвер|п[''`]?ятниц[яюі]|субота|суботу|неділ[яюі])\b", " ", reminder_text, flags=re.IGNORECASE)
    
    # Прийменники часу
    reminder_text = re.sub(r"\b(о|в|на|через|за)\s+(?=\d)", " ", reminder_text, flags=re.IGNORECASE)
    
    # Залишки часових конструкцій
    reminder_text = re.sub(r"\b(годин[иу]|хвилин[иу]|днів|день|тижд[ень|ня]|місяц[ьяі])\b", " ", reminder_text, flags=re.IGNORECASE)
    
    # Числові дати (що могли залишитись)
    reminder_text = re.sub(r"\b\d{1,2}[./-]\d{1,2}(?:[./-]\d{2,4})?\b", " ", reminder_text)
    reminder_text = re.sub(r"\b\d{4}-\d{2}-\d{2}\b", " ", reminder_text)
    
    # Залишки часу
    reminder_text = re.sub(r"\b(?:[01]?\d|2[0-3])[:\s.]\d{2}\b", " ", reminder_text)
    
    # Прибрати службові слова/зайві пробіли/пунктуацію
    reminder_text = re.sub(r"\s+", " ", reminder_text).strip(" ,.-\n\t:;")

    if not reminder_text:
        return None, "😼 Окей. Про що нагадати?", recur_interval

    return parsed_time_local, reminder_text, recur_interval


async def remind_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Обробляє створення нагадувань.
    СУВОРІ ПРАВИЛА:
    1. Має бути слово "нагадай".
    2. Має бути (Відповідь боту) АБО (Звернення до бота).
    """
    user = update.effective_user
    chat = update.effective_chat
    # Політика чату: можна вимкнути нагадування для групи/супергрупи
    if chat and isinstance(chat.id, int) and chat.id < 0:
        settings = await get_chat_settings(chat.id)
        if int(settings.get("reminders_enabled", 1) or 1) == 0:
            await update.message.reply_html(
                "😼 Нагадування в цьому чаті вимкнені.\n"
                "Адмін може увімкнути: <code>/reminders_chat on</code> (або через /settings)."
            )
            return

    if not update.message or not update.message.text:
        return

    if _is_duplicate_update(context, update):
        return

    command_text = update.message.text
    # (ОНОВЛЕНО) Нормалізуємо текст одразу, щоб "нагадай" шукалось коректно
    text_normalized = normalize_text(command_text) 
    text_lower = text_normalized.lower()
    is_slash_command = command_text.strip().startswith("/")

    # Активація:
    # - /remind завжди активний
    # - звичайне нагадування — тільки якщо текст починається з: [звернення][,] [нагадай|нагада]
    full_text = text_normalized

    if is_slash_command:
        # прибираємо саму команду (/remind ...)
        full_text = re.sub(r"^\/\w+\s*", "", full_text).strip()
    else:
        activated, rest = strip_trigger_prefix(full_text)
        if not activated:
            return
        full_text = rest
    if not full_text:
        if is_slash_command:
             await update.message.reply_html(
                f"<b>Створити нагадування 🐾</b>\n"
                f"Використовуйте:\n"
                f"• <code>/remind [коли] [текст]</code>\n"
                f"• <code>Кошеня, нагадай [коли] [текст]</code>\n"
                f"• Або: <code>котик, нагадай ...</code> (звернення на початку)."
            )
        return

    # 1. Перевірка ліміту
    count = await get_user_reminders_count(user.id)
    if count >= REMINDER_LIMIT:
        await update.message.reply_html(
            f"Ой, у вас забагато активних нагадувань! 😿\n"
            f"У вас вже {count} (максимум {REMINDER_LIMIT}).\n"
            f"Видаліть старі через <code>/myreminders</code>."
        )
        return

    # 2. Парсинг
    
    reminder_time_local, message_text, recur_interval = _parse_reminder_text(full_text)

    if not reminder_time_local:
        # 1–2 уточнення без повторного префіксу "котик, нагадай"
        msg_low = message_text.lower()
        if "коли" in msg_low:
            context.user_data["reminder_pending"] = {
                "stage": "when",
                "what": full_text.strip(),
                "delivery_chat_id": chat.id,
                "expires_at": (datetime.now(USER_TIMEZONE) + timedelta(minutes=10)).isoformat(),
            }
        elif "про що" in msg_low:
            # Якщо час/дата розпізнані, але текст порожній — запитаємо "що"
            found = search_dates(
                full_text,
                languages=["uk"],
                settings={
                    "TIMEZONE": "Europe/Kyiv",
                    "RETURN_AS_TIMEZONE_AWARE": True,
                    "PREFER_DATES_FROM": "future",
                },
            )
            if found:
                run_at = found[0][1]
                try:
                    run_at = run_at.astimezone(USER_TIMEZONE)
                except Exception:
                    run_at = USER_TIMEZONE.localize(run_at) if run_at.tzinfo is None else run_at
                context.user_data["reminder_pending"] = {
                    "stage": "what",
                    "run_at_local": run_at.isoformat(),
                    "recur_interval": recur_interval,
                    "delivery_chat_id": chat.id,
                    "expires_at": (datetime.now(USER_TIMEZONE) + timedelta(minutes=10)).isoformat(),
                }

        await update.message.reply_html(message_text)
        return

    # 3. Створення + планування
    await _create_and_schedule_reminder(
        update=update,
        context=context,
        run_at_local=reminder_time_local,
        message_text=message_text,
        recur_interval=recur_interval,
        delivery_chat_id=chat.id,
    )

    context.user_data.pop("reminder_wizard", None)
    context.user_data.pop("reminder_pending", None)
    return ConversationHandler.END


async def wizard_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop("reminder_wizard", None)
    if update.message:
        ctx = await get_user_addressing(update.effective_user.id)
        await update.message.reply_html(f"😼 Ок. {ctx.past('Скасував', 'Скасувала', 'Скасував')}.")
    return ConversationHandler.END



# =============================================================================
# Wizard: "!нагадування" (покрокове)
# =============================================================================

WIZ_WHEN, WIZ_WHAT, WIZ_REPEAT = range(3)

async def wizard_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat = update.effective_chat
    user = update.effective_user
    if not update.message or not chat or not user:
        return ConversationHandler.END

    if chat.id < 0:
        settings = await get_chat_settings(chat.id)
        if int(settings.get("reminders_enabled", 1) or 1) == 0:
            await update.message.reply_html(
                "😾 У цьому чаті нагадування вимкнені. Увімкни в налаштуваннях чату."
            )
            return ConversationHandler.END

    context.user_data["reminder_wizard"] = {
        "run_at": None,
        "text": None,
        "recur_interval": None,
        "delivery_chat_id": chat.id,
    }
    await update.message.reply_html("😼 Коли нагадати? (напр.: <code>завтра о 10</code>)")
    return WIZ_WHEN


async def wizard_when(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message or not update.message.text:
        return WIZ_WHEN

    run_at, msg, _ = _parse_reminder_text(update.message.text.strip())
    if not run_at:
        await update.message.reply_html(msg)
        return WIZ_WHEN

    context.user_data["reminder_wizard"]["run_at"] = run_at
    await update.message.reply_html("😼 Про що нагадати?")
    return WIZ_WHAT


async def wizard_what(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message or not update.message.text:
        return WIZ_WHAT

    text = update.message.text.strip()
    if not text:
        await update.message.reply_html("😼 Напишіть текст нагадування.")
        return WIZ_WHAT

    context.user_data["reminder_wizard"]["text"] = text
    await update.message.reply_html("😼 Повторювати? (ні/щодня/щотижня/щомісяця)")
    return WIZ_REPEAT


async def wizard_repeat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message or not update.message.text:
        return WIZ_REPEAT

    data = context.user_data.get("reminder_wizard") or {}
    answer = normalize_text(update.message.text).strip().lower()

    mapping = {
        "ні": None,
        "нет": None,
        "no": None,
        "щодня": "daily",
        "щотижня": "weekly",
        "щомісяця": "monthly",
    }
    if answer not in mapping:
        await update.message.reply_html("😼 Оберіть: <b>ні</b>, <b>щодня</b>, <b>щотижня</b> або <b>щомісяця</b>.")
        return WIZ_REPEAT

    recur_interval = mapping[answer]
    run_at = data.get("run_at")
    text = data.get("text")
    delivery_chat_id = data.get("delivery_chat_id")

    if not (run_at and text and delivery_chat_id is not None):
        await update.message.reply_html("😿 Щось пішло не так. Спробуйте ще раз: <code>!нагадування</code>")
        return ConversationHandler.END

    reminder_time_utc = run_at.astimezone(pytz.utc)
    reminder_id = await add_reminder(
        update.effective_user.id,
        delivery_chat_id,
        text,
        reminder_time_utc.isoformat(),
        None,
        recur_interval=recur_interval,
    )

    # Плануємо job (dedup)
    job_queue = context.application.job_queue
    if job_queue and reminder_id:
        job_name = f"reminder_{reminder_id}"
        _dedup_job_by_name(job_queue, job_name)
        job_queue.run_once(reminder_job_callback, when=run_at, data={"reminder_id": reminder_id}, name=job_name)
        await set_reminder_job_name(reminder_id, job_name)

    when_str = run_at.strftime("%d.%m %H:%M")
    extra = ""
    if recur_interval:
        extra = " і повторюватиму " + ("щодня" if recur_interval == "daily" else "щотижня" if recur_interval == "weekly" else "щомісяця")

    ctx = await get_user_addressing(update.effective_user.id)

    if delivery_chat_id < 0:
        mention = _format_target_mention(update.effective_user)
        await update.message.reply_html(
            f"😼 Ок, {mention}. {ctx.past('Нагадаю', 'Нагадаю', 'Нагадаю')} тут {when_str}: «{html.escape(text)}»{extra}"
        )
    else:
        await update.message.reply_html(
            f"😼 {ctx.past('Запамʼятав', 'Запамʼятала', 'Запамʼятав')}. {ctx.past('Нагадаю', 'Нагадаю', 'Нагадаю')} {when_str}: «{html.escape(text)}»{extra}"
        )

    context.user_data.pop("reminder_wizard", None)
    return ConversationHandler.END






def _arm_reminders_autoclose(context: ContextTypes.DEFAULT_TYPE, message, *, fallback_text: str) -> None:
    if not message:
        return
    set_auto_close_payload(
        context,
        REMINDERS_AUTO_CLOSE_KEY,
        chat_id=message.chat_id,
        message_id=message.message_id,
        fallback_text=fallback_text,
    )
    start_auto_close(context, REMINDERS_AUTO_CLOSE_KEY)


async def _build_reminders_view(user_id: int, *, prefix: str | None = None) -> tuple[str, InlineKeyboardMarkup]:
    reminders = await get_user_reminders(user_id)

    response_text = ""
    if prefix:
        response_text += prefix.rstrip() + "\n\n"

    if not reminders:
        response_text += "У вас немає активних нагадувань. 🌿"
        return response_text, InlineKeyboardMarkup([[InlineKeyboardButton("❌ Закрити", callback_data=CB_REMINDERS_CLOSE)]])

    now_local = datetime.now(USER_TIMEZONE)
    actual_reminders = []
    keyboard_buttons = []

    for rem in reminders:
        try:
            time_utc = datetime.fromisoformat(rem["reminder_time"])
            if time_utc.tzinfo is None:
                time_utc = pytz.utc.localize(time_utc)
            time_local = time_utc.astimezone(USER_TIMEZONE)

            if time_local < now_local and not rem.get("recur_interval"):
                continue

            actual_reminders.append((time_local, rem["id"], rem["message_text"], rem.get("recur_interval")))
        except Exception as e:
            logger.error(f"Помилка форматування нагадування {rem['id']}: {e}")

    if not actual_reminders:
        response_text += "У вас немає активних нагадувань. 🌿"
        return response_text, InlineKeyboardMarkup([[InlineKeyboardButton("❌ Закрити", callback_data=CB_REMINDERS_CLOSE)]])

    response_text += "<b>📜 Ваші активні нагадування:</b>\n\n"
    actual_reminders.sort(key=lambda x: x[0])

    for i, (time_local, rem_id, message_text, recur_interval) in enumerate(actual_reminders, 1):
        recur_str = ""
        if recur_interval == "daily":
            recur_str = " (🔁 Щодня)"
        elif recur_interval == "weekly":
            recur_str = time_local.strftime(" (🔁 Щотижня, по %A)")
        elif recur_interval == "monthly":
            recur_str = f" (🔁 Щомісяця, {time_local.day} числа)"

        response_text += f"<b>{i}.</b> <code>{time_local.strftime('%d.%m.%Y %H:%M')}</code>{recur_str}\n"
        escaped_message = html.escape(message_text)
        if len(escaped_message) > 50:
            escaped_message = escaped_message[:50] + "..."
        response_text += f"   <i>└ {escaped_message}</i>\n\n"

        keyboard_buttons.append([InlineKeyboardButton(f"❌ Видалити {i}", callback_data=f"delrem_{rem_id}")])

    keyboard_buttons.append([InlineKeyboardButton("❌ Закрити", callback_data=CB_REMINDERS_CLOSE)])
    return response_text, InlineKeyboardMarkup(keyboard_buttons)


async def my_reminders_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Показує список активних нагадувань користувача."""
    cancel_auto_close(context, REMINDERS_AUTO_CLOSE_KEY)

    user_id = update.effective_user.id
    msg = update.effective_message
    if not msg:
        return

    text, markup = await _build_reminders_view(user_id)
    sent = await msg.reply_html(text, reply_markup=markup)
    _arm_reminders_autoclose(
        context,
        sent,
        fallback_text="Меню нагадувань закрито через бездіяльність.",
    )


async def handle_delete_reminder_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обробляє callback для видалення нагадування та одразу повертає список."""
    cancel_auto_close(context, REMINDERS_AUTO_CLOSE_KEY)

    query = update.callback_query
    if not query:
        return
    await query.answer()

    user_id = query.from_user.id
    try:
        reminder_id = int(query.data.split("_")[1])
    except (ValueError, IndexError):
        await query.edit_message_text("Помилка. Не вдалося розпізнати ID нагадування.")
        return

    reminder_data = await get_reminder(reminder_id)

    if not reminder_data:
        await query.edit_message_text("🗑️ Це нагадування вже видалено або виконано.")
        return

    if reminder_data["user_id"] != user_id:
        await context.bot.send_message(
            chat_id=user_id,
            text="❗️ Няв! Ви не можете видалити чуже нагадування. 😼",
        )
        return

    job_name = reminder_data.get("job_name")
    if job_name:
        jobs = context.job_queue.get_jobs_by_name(job_name)
        if jobs:
            for job in jobs:
                job.schedule_removal()

    await remove_reminder(reminder_id)

    prefix = f"🗑 Нагадування видалено.\n<i>Про: {html.escape(reminder_data['message_text'])}</i>"
    text, markup = await _build_reminders_view(user_id, prefix=prefix)

    await query.edit_message_text(text=text, parse_mode=ParseMode.HTML, reply_markup=markup)
    if query.message:
        _arm_reminders_autoclose(
            context,
            query.message,
            fallback_text="Меню нагадувань закрито через бездіяльність.",
        )


async def handle_snooze_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Обробляє callback для відкладання нагадування (кнопки 'Відкласти').
    Створює нове нагадування з тим самим текстом через N хвилин.
    """
    query = update.callback_query
    await query.answer("Відкладаю... 💤")

    user = query.from_user
    chat = query.message.chat
    
    try:
        minutes = int(query.data.split("_")[1])
    except (ValueError, IndexError):
        return

    msg_html = query.message.text_html or query.message.caption_html or ""
    
    # Шукаємо текст всередині <i>...</i> після фрази "не забути"
    match = re.search(r"не забути:.*?<i>(.*?)</i>", msg_html, re.DOTALL | re.IGNORECASE)
    
    if match:
        message_text = html.unescape(match.group(1).strip())
    else:
        message_text = "Відкладене нагадування (текст втрачено)"

    new_time_local = datetime.now(USER_TIMEZONE) + timedelta(minutes=minutes)
    new_time_utc = new_time_local.astimezone(pytz.utc)

    reminder_id = await add_reminder(
        user_id=user.id,
        chat_id=chat.id,
        message_text=message_text,
        reminder_time=new_time_utc.isoformat(),
        job_name=None,
        recur_interval=None
    )

    if reminder_id:
        job_name = f"reminder_{reminder_id}"
        context.job_queue.run_once(
            reminder_job_callback,
            new_time_utc,
            data={"reminder_id": reminder_id},
            name=job_name,
        )
        await set_reminder_job_name(reminder_id, job_name)
        
        time_str = new_time_local.strftime('%H:%M')
        await query.edit_message_text(
            text=f"{msg_html}\n\n💤 <i>(Відкладено користувачем {mention(user)} на {minutes} хв — до {time_str})</i>",
            parse_mode=ParseMode.HTML,
            reply_markup=None 
        )
    else:
        await query.message.reply_text("Не вдалося відкласти нагадування. Помилка БД.")


async def reminders_close(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Закриває меню нагадувань вручну без помилок."""
    query = update.callback_query
    if not query:
        return

    cancel_auto_close(context, REMINDERS_AUTO_CLOSE_KEY)
    await query.answer()

    try:
        if query.message:
            await query.message.delete()
    except Exception:
        try:
            if query.message:
                await query.message.edit_text("Меню нагадувань закрито.")
        except Exception:
            pass

async def load_persistent_reminders(application: Application):
    """Відновлює активні нагадування з БД при старті бота (без дублів jobs).

    Важливо: job.data містить тільки reminder_id; усе інше дістаємо з БД в callback.
    """
    logger.info("Завантаження персистентних нагадувань...")
    job_queue = application.job_queue
    if job_queue is None:
        logger.warning("JobQueue відсутній — не можу відновити нагадування.")
        return

    all_reminders = await get_all_reminders()
    if not all_reminders:
        logger.info("Активних нагадувань для відновлення не знайдено.")
        return

    now_utc = datetime.now(pytz.utc)
    tasks = []
    for rem in all_reminders:
        tasks.append(_schedule_job_from_db(job_queue, now_utc, rem))

    # не падаємо, якщо одне нагадування криве
    await asyncio.gather(*tasks, return_exceptions=True)
    logger.info("Відновлення нагадувань завершено.")


async def _schedule_job_from_db(job_queue: JobQueue, now_utc: datetime, rem: dict):
    """Планує одне нагадування з рядка БД."""
    try:
        reminder_id = rem.get("id")
        if not reminder_id:
            return "skipped:no_id"

        # статус (якщо міграція вже додала поле)
        if rem.get("status") and rem.get("status") != "ACTIVE":
            return f"skipped:{rem.get('status')}"

        job_name = rem.get("job_name") or f"reminder_{reminder_id}"
        if not rem.get("job_name"):
            await set_reminder_job_name(reminder_id, job_name)

        # reminder_time зберігається як ISO (UTC або naive)
        reminder_time_utc = datetime.fromisoformat(rem["reminder_time"])
        if reminder_time_utc.tzinfo is None:
            reminder_time_utc = pytz.utc.localize(reminder_time_utc)

        data = {"reminder_id": reminder_id}

        _dedup_job_by_name(job_queue, job_name)
        if reminder_time_utc > now_utc:
            job_queue.run_once(reminder_job_callback, reminder_time_utc, data=data, name=job_name)
            return True

        # Якщо вже в минулому — запускаємо дуже скоро, щоб не "губити" (або щоб recurring сам перескочив)
        job_queue.run_once(reminder_job_callback, timedelta(seconds=5), data={**data, "missed_at": reminder_time_utc.isoformat()}, name=job_name)
        return "missed"

    except Exception as e:
        logger.exception(f"Помилка при відновленні нагадування {rem.get('id')}: {e}")
        return e



async def pending_reminder_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Роутер, який підхоплює уточнення для нагадувань.
    
    КРИТИЧНО: Якщо є pending reminder - обробляємо ТУТ і не пускаємо далі в AI.
    Якщо немає pending - пропускаємо далі (повертаємо None).
    """
    # Перевіряємо чи є активний pending reminder
    pending = context.user_data.get("reminder_pending")
    if not pending:
        # Немає pending - пропускаємо далі в AI та інші handlers
        return
    
    # Є pending - обробляємо тут і НЕ пропускаємо далі
    handled = await _handle_pending_reminder(update, context)
    if handled:
        # Нагадування оброблено - зупиняємо propagation
        raise ApplicationHandlerStop

def register_reminder_handlers(application):
    # КРИТИЧНО: Handler для pending_reminder МАЄ БУТИ ПЕРШИЙ (group=-2)
    # щоб перехоплювати відповіді до AI та інших handlers
    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            pending_reminder_router,
            block=True,
        ),
        group=-2,
    )
    
    application.add_handler(
        MessageHandler(
            ReminderTriggerFilter(),
            remind_command,
            block=True,
        ),
        group=-1,
    )

    application.add_handler(
        CommandHandler(["myreminders", "reminders"], my_reminders_command)
    )
    application.add_handler(
        MessageHandler(
            filters.TEXT
            & ~filters.COMMAND
            & filters.Regex(r"(?i)^(мої нагадування|моїнагадування)$"),
            my_reminders_command,
        )
    )
    application.add_handler(
        CallbackQueryHandler(handle_delete_reminder_callback, pattern=r"^delrem_\d+$")
    )
    application.add_handler(
        CallbackQueryHandler(reminders_close, pattern=fr"^{CB_REMINDERS_CLOSE}$")
    )
    # Реєструємо handler для snoozin
    application.add_handler(
        CallbackQueryHandler(handle_snooze_callback, pattern=r"^snooze_\d+$")
    )
    logger.info("Обробники Нагадувань (reminder_handlers.py) завантажені.")