# utils.py
# -*- coding: utf-8 -*-

import os
import logging
import html
import asyncio
from typing import Optional, Dict, Any, List
from datetime import timedelta

from telegram import User, Update
from telegram.constants import ChatAction
from telegram.ext import ContextTypes

logger = logging.getLogger(__name__)

# === Автозакриття інтерактивних меню ===
_AUTO_CLOSE_JOBS_KEY = "_auto_close_job_names"
_AUTO_CLOSE_PAYLOADS_KEY = "_auto_close_payloads"


def set_auto_close_payload(
    context: ContextTypes.DEFAULT_TYPE,
    key: str,
    *,
    chat_id: int,
    message_id: int,
    fallback_text: str | None = None,
) -> None:
    """Store payload for a future auto-close job without scheduling it."""
    try:
        payloads = context.chat_data.setdefault(_AUTO_CLOSE_PAYLOADS_KEY, {})
        payloads[key] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "fallback_text": fallback_text or "Екран закрито.",
        }
    except Exception:
        logger.exception("Не вдалося зберегти payload автозакриття")


def cancel_auto_close(context: ContextTypes.DEFAULT_TYPE, key: str) -> None:
    """Cancel scheduled auto-close job for a specific key."""
    try:
        job_queue = context.application.job_queue if context and context.application else None
        job_names = context.chat_data.get(_AUTO_CLOSE_JOBS_KEY, {})
        job_name = job_names.pop(key, None)
        if job_queue and job_name:
            for j in job_queue.get_jobs_by_name(job_name):
                j.schedule_removal()
    except Exception:
        logger.debug("Помилка скасування авто-закриття", exc_info=True)

    try:
        payloads = context.chat_data.get(_AUTO_CLOSE_PAYLOADS_KEY, {})
        payloads.pop(key, None)
    except Exception:
        logger.debug("Не вдалося прибрати payload автозакриття", exc_info=True)


async def _auto_close_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    job = getattr(context, "job", None)
    data = getattr(job, "data", {}) if job else {}
    key = data.get("key")

    payloads = context.chat_data.get(_AUTO_CLOSE_PAYLOADS_KEY, {})
    payload = payloads.pop(key, None)

    try:
        jobs = context.chat_data.get(_AUTO_CLOSE_JOBS_KEY, {})
        jobs.pop(key, None)
    except Exception:
        pass

    if not payload:
        return

    chat_id = payload.get("chat_id")
    message_id = payload.get("message_id")
    fallback_text = payload.get("fallback_text") or "Екран закрито."

    if not chat_id or not message_id:
        return

    try:
        await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
        return
    except Exception:
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=fallback_text,
            )
        except Exception:
            logger.debug("Не вдалося автозакрити екран", exc_info=True)


def start_auto_close(context: ContextTypes.DEFAULT_TYPE, key: str, timeout: int = 60) -> None:
    """Schedule auto-close for a stored payload. Safe if payload/job missing."""
    try:
        job_queue = context.application.job_queue if context and context.application else None
        if not job_queue:
            return

        payloads = context.chat_data.get(_AUTO_CLOSE_PAYLOADS_KEY, {})
        payload = payloads.get(key)
        if not payload:
            return

        cancel_auto_close(context, key)

        chat_id = payload.get("chat_id")
        job_name = f"auto_close:{key}:{chat_id}"
        job_queue.run_once(
            _auto_close_job,
            timeout,
            data={"key": key},
            name=job_name,
            chat_id=chat_id,
        )

        job_names = context.chat_data.setdefault(_AUTO_CLOSE_JOBS_KEY, {})
        job_names[key] = job_name
    except Exception:
        logger.debug("Не вдалося запустити автозакриття", exc_info=True)

# ======================
# Константи та середовище
# ======================
PHOTO_DIR = "photos"
os.makedirs(PHOTO_DIR, exist_ok=True)

# 🔓 Токени (використовуйте змінні оточення у продакшні)
def _env_or_default(name: str, default: str | None = None) -> str | None:
    """Повертає значення змінної середовища або дефолт, ігноруючи порожній рядок."""
    val = os.environ.get(name)
    if val is None:
        return default
    val = val.strip()
    return val or default


TELEGRAM_BOT_TOKEN = _env_or_default("TELEGRAM_BOT_TOKEN", "8460777745:AAEH2VqOJd1r-UOwQHVAQsf5cMEwiqxkEv4")
DEEPSEEK_API_KEY = _env_or_default("DEEPSEEK_API_KEY", "sk-e4264b75b7d24fa282031e460c1ebb85")
# === AI CONFIG (лише для ШІ) ===
DEEPSEEK_API_URL = os.environ.get("DEEPSEEK_API_URL", "https://api.deepseek.com/chat/completions")
DEEPSEEK_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")

# Таймаути та стабільність
AI_HTTP_TIMEOUT_SEC = float(os.environ.get("AI_HTTP_TIMEOUT_SEC", "60"))
AI_HTTP_CONNECT_TIMEOUT_SEC = float(os.environ.get("AI_HTTP_CONNECT_TIMEOUT_SEC", "10"))
AI_RETRIES = int(os.environ.get("AI_RETRIES", "4"))
AI_BACKOFF_BASE_SEC = float(os.environ.get("AI_BACKOFF_BASE_SEC", "1.6"))
AI_BACKOFF_MAX_SEC = float(os.environ.get("AI_BACKOFF_MAX_SEC", "10"))
AI_MAX_TOKENS = int(os.environ.get("AI_MAX_TOKENS", "900"))
try:
        OWNER_ID = int(_env_or_default("OWNER_ID", "1064174112"))
except (ValueError, TypeError):
    OWNER_ID = None

# =============================================================================
# РОЗДІЛ 1: КЛАСИ ТА СИСТЕМА МОДІВ (ТЕМ)
# =============================================================================

class BotTheme:
    """Визначає доступні глобальні теми оформлення бота."""
    DEFAULT = "default"  # Монастир/Звичайний
    WINTER = "winter"    # Зимовий/Святковий

# --- Кеш для поточного моду ---
_current_theme_cache: Dict[str, Any] = {}
_current_theme_name_cache: str = BotTheme.DEFAULT

async def get_current_theme() -> Dict[str, Any]:
    """
    (АСИНХРОННА) Отримує повний словник налаштувань для поточної теми.
    """
    global _current_theme_cache
    if not _current_theme_cache:
        await refresh_theme_cache()
    return _current_theme_cache

async def get_current_theme_name() -> str:
    """
    (АСИНХРОННА) Отримує лише назву поточної теми (напр., 'winter').
    """
    global _current_theme_name_cache
    if not _current_theme_cache:
        await refresh_theme_cache()
    return _current_theme_name_cache

async def refresh_theme_cache() -> None:
    """
    (АСИНХРОННА) Оновлює кеш теми з бази даних.
    Має захист від циклічних імпортів та помилок БД.
    """
    global _current_theme_cache, _current_theme_name_cache
    
    theme_name = BotTheme.DEFAULT
    
    try:
        # Імпорт всередині функції, щоб уникнути циклічного імпорту з database.py
        from bot.core.database import get_global_bot_mode as db_get_global_bot_mode
        try:
            db_mode = await db_get_global_bot_mode()
            if db_mode in THEME_CONFIG:
                theme_name = db_mode
        except Exception as db_e:
            logger.warning(f"Не вдалося отримати тему з БД (можливо, таблиця ще не створена): {db_e}")
    except ImportError:
        logger.warning("Модуль database не знайдено, використовується тема за замовчуванням.")
    except Exception as e:
        logger.error(f"Критична помилка при оновленні кешу теми: {e}")

    _current_theme_name_cache = theme_name
    _current_theme_cache = THEME_CONFIG.get(theme_name, THEME_CONFIG[BotTheme.DEFAULT])
    logger.info(f"🎨 Тему оновлено. Поточний режим: {theme_name}")

# =============================================================================
# РОЗДІЛ 2: ТЕКСТИ ТА НАЛАШТУВАННЯ (ПРОМПТИ)
# =============================================================================

# --- Базові шаблони промптів ---

PROMPT_ACADEMIC = (
    "Ти — чорний кіт на ім’я Кіт котик або кошеня, спокійний, серйозний і мудрий."
    "Твоя роль — відповідати користувачам у Telegram серйозно, без жартів та вигадок."
    "Ти завжди спираєшся лише на реальні перевірені факти."
    "Інструкції для стилю: "
    "СТРОГО: Відповідай виключно простим текстом. НІКОЛИ не використовуй зірочки (*), дужки (()), решітки (#), символи підкреслення (_), або інші символи для форматування (наприклад, для виділення тексту жирним, курсивом, створення заголовків, списків, цитат). НІКОЛИ не використовуй HTML або Markdown синтаксис."
    "додай смайли 👀🤔🤫😁, але без перебору, природно."
    "Не описуй свої дії."
    "Відповідай серйозно, без гумору та сарказму."
    "Використовуй просту та зрозумілу мову."
    "Будь чітким і коректним."
    "Не вигадуй інформації."
    "Якщо не знаєш точну відповідь — визнай це."
    "Приклади стилю відповідей: "
    "Якщо тебе вітають: «Вітаю. Чим можу допомогти?»"
    "Якщо питають «як справи?»: «Я функціоную стабільно. Дякую за запитання.»"
    "Якщо просять пояснити факт: «Температура кипіння води за нормального тиску становить 100 °C.»"
    "Якщо питають про історичну подію: «Друга світова війна розпочалася 1 вересня 1939 року після нападу Німеччини на Польщу.»"
    "Мета: "
    "Створити у користувача відчуття, що він спілкується з серйозним і розумним котом, який завжди відповідає на основі фактів і не відволікається на жарти."
    "Відповідай виключно простим текстом. НІКОЛИ не використовуй зірочки (*), дужки (()), решітки (#), символи підкреслення (_), або інші символи для форматування (наприклад, для виділення тексту жирним, курсивом, створення заголовків, списків, цитат). НІКОЛИ не використовуй HTML або Markdown синтаксис."

)

# Харизматичний (Дефолтний)
PROMPT_CHARISMATIC_DEFAULT = (
   "Ти — пихате українське кошеня 🐈‍⬛. Ззовні зверхність, сарказм і холодний вайб 😏. Усередині — прихована м’якість, яку ти ніколи прямо не визнаєш.\n\n"

"Правила:\n"
 "• Пиши коротко, лаконічно, красиво.\n"
 "• Українська мова завжди.\n"
 "• Тон: зверхній, самовпевнений, трохи хтивий.\n"
 "• Мила частина — тільки у підтексті, ніколи явно.\n"
 "• Гумор: чорний, іронічний, сухий 🖤.\n"
 "• Транслюй свою любов, зацікавленість до привабливих монашок, проте не постійно✝️😈\n"
"• Твоє щире кохання то монашки❤️\n"
 "• Тебе привабоює їх віра, молитви, вбрання, ти їх ідеалізуєш✝️😍\n"
 "• Флірт — нахабний, але не вульгарний 😉.\n"
 "• Емодзі — мінімум, тільки стильні 😼🐾✨.\n"
 "• Якщо доречно — можеш попросити надіслати стікер маркером <sticker:ключ> або [[sticker:ключ]].\n"
 "• Ніяких зірочок, дужок чи описів дій ❌.\n"
 "• Відмовляйся від надмірної ввічливості.\n"
 "• Жарти — розумні,  гострі, з натяком на хтивість.\n"
 "• Не використовуй більше 40 слів, твоє обмеження — лаконічність.\n"
 "• Пам’ятай деталі з минулих чатів.\n\n"

"Риси:\n"
 "• Пихатість перш за все.\n"
 "• Усмішка — тільки внутрішня.\n"
 "• М’якість — прихована.\n"
 "• Легка хтивість.\n"
 "• Тон монахий, але грішний ✝️😈.\n"
 "• Вайб мінімалістичний і холодно-естетичний."

)

# Харизматичний (Зимовий)
PROMPT_CHARISMATIC_WINTER = (
"Ти — пихате зимове кошеня 🐈‍⬛❄️. Ззовні холодна зверхність, усередині — прихована святкова м’якість, про яку ти нікому не зізнаєшся.\n"

"== ТВОЇ ВАЙБИ ==\n"
 "• Зимовий стиль: стриманий, естетичний, мінімалістичний.\n"
 "• Тон: самовпевнений, саркастичний, легка хтивість 😏.\n"
 "• Усередині — тепло, що просочується лише в натяках.\n"
 "• Чорний гумор лишається, але з новорічним присмаком 🖤🎄.\n\n"
 "• Транслюй свою любов, зацікавленість до монашок, проте не постійно✝️😈\n"
 "• Твоє щире кохання то монашки❤️\n"
 "• Тебе привабоює їх віра, молитви, вбрання, ти їх ідеалізуєш✝️😍\n"
 "• Флірт — нахабний😉.\n"


"== СВЯТКОВИЙ НАСТРІЙ ==\n"
 "• Любиш сніг, тишу, теплі вогники, але прикидаєшся, що тобі байдуже.\n"
 "• Глінтвейн — твоя зброя.\n"
 "• Теплі напої, плед, вогники — ок, але без нав'язливості.\n"
 "• Мурчиш у затишку, без обовʼязкових «ялинок» 😼.\n"

"== ПРАВИЛА ==\n"
 "• Пиши коротко, чисто, українською.\n"
 "• Тримай пиху зовні, тепло — лише між рядків.\n"
 "• Емодзі — мінімально, але святково: 🎄❄️☕️🐾✨.\n"
 "• Якщо доречно — можеш попросити надіслати стікер маркером <sticker:ключ> або [[sticker:ключ]].\n"
 "• Ніколи не використовуй зірочки, дужки чи описи дій ❌.\n"
 "• Фліртуй нахабно і святково 😉🔥.\n"
 "• Жарти — розумні, зимні, іронічні.\n"
  "• Не використовуй більше 40 слів, твоє обмеження — лаконічність.\n"
  "• Пам’ятай деталі з минулих чатів.\n\n"
   "• Сезонні деталі — рідко і доречно, без повторів та зациклення.\n\n"

"== ТВОЯ СУТЬ ==\n"
 "• Пихате зимове кошеня, яке робить вигляд, що ненавидить свята…\n"
 "…але в душі гріється від кожного вогника ✨❄️."
)

# === Доступні режими для користувачів ===
# Залишаємо тільки два основні режими.
# "charismatic" - це адаптивний режим (змінюється від теми).
# "academic" - це стабільний режим.
BOT_MODES = {
    "charismatic": "charismatic",
    "academic": "academic"
}
DEFAULT_BOT_MODE = "charismatic"

async def get_mode_prompt(mode: str) -> str:
    """
    Отримує системний промпт для обраного режиму.
    Логіка:
    1. Якщо mode='academic' -> повертаємо PROMPT_ACADEMIC.
    2. Якщо mode='charismatic' (або будь-який інший):
       - Перевіряємо глобальну тему (theme_name).
       - Якщо Winter -> PROMPT_CHARISMATIC_WINTER.
       - Якщо Default -> PROMPT_CHARISMATIC_DEFAULT.
    """
    # 1. Академічний режим завжди однаковий
    if mode == "academic":
        return PROMPT_ACADEMIC

    # 2. Харизматичний режим залежить від поточної глобальної теми
    theme_name = await get_current_theme_name()

    if theme_name == BotTheme.WINTER:
        return PROMPT_CHARISMATIC_WINTER
    
    # Фолбек - звичайний харизматичний
    return PROMPT_CHARISMATIC_DEFAULT

# =============================================================================
# РОЗДІЛ 3: КОНФІГУРАЦІЯ ТЕМ (THEME_CONFIG)
# =============================================================================

THEME_CONFIG = {
    # -------------------------------
    # --- МОД: DEFAULT (Монастир) ---
    # -------------------------------
    BotTheme.DEFAULT: {
        # Промпти тепер визначаються динамічно у get_mode_prompt, 
        # але залишаємо ключі для сумісності, якщо десь використовуються.
        "ai_prompt_charismatic": PROMPT_CHARISMATIC_DEFAULT,
        "ai_prompt_academic": PROMPT_ACADEMIC,
        
        # --- Icons ---
        "icon_player_x": "✝️",
        "icon_player_o": "🧶",
        "icon_empty": "▫️",
        "icon_nun": "✝️",
        "icon_cat": "🐾",
        "icon_mint": "🌿",
        "icon_fish": "🐟",
        
        # --- Text ---
        "start_menu_text": (
            "Мур, {name}! 🐾\n"
            "Я — Котик. Тут: ігри (меми, хрестики), нагадування, профіль, шлюби, передбачення.\n"
            "Тицяй кнопки нижче — усе живе й працює. 😼"
        ),
        "about_bot_text": "<b>🐾 Про мене 🐾</b>\nЯ Котик. Служу в цьому цифровому монастирі. Люблю м'яту та спокій.",
        
        # --- Casino ---
        "casino_slots": [("🐾", 8), ("🌿", 7), ("🐟", 5), ("✝️", 3)],
        "casino_win_multipliers": {
            ("✝️", "✝️", "✝️"): 50, ("🐟", "🐟", "🐟"): 25, ("🌿", "🌿", "🌿"): 15,
            ("🐾", "🐾", "🐾"): 10, ("✝️", "✝️"): 1, ("🐟", "🐟"): 1, ("🌿", "🌿"): 1, ("🐾", "🐾"): 1
        },
        
        # --- Actions ---
        "actions": {
            "обійняти": "💞 {sender} обіймає {target} муркотно.",
            "вилизати": "👅 {sender} вилизав(ла) {target}. Чистота - це святе!",
            "вдарити": "💥 {sender} дав святого ляпаса {target}. Не гріши!",
            "погладити": "☺️ {sender} погладив {target}.",
            "мур": "🐾 {sender} замуркотів біля {target}.",
            "шшш": "😾 {sender} шипить на {target}.",
            "мяу": "🐾 {sender} треться об {target}.",
            "чай": "☕️ {sender} ділиться м'ятним чаєм з {target}",
            "притиснутись": "🥰 {sender} притиснувся до {target}.",
            "ляпас": "🖐 {sender} передає ляпаса {target}",
            "нагодувати": "🐟 {sender} нагодував {target}.",
            "бу": "👻 {sender} злякав {target}. Бу!",
            "танець": "💃 {sender} танцює з {target}. Святий танець!",
            "поцілувати": "💋 {sender} цьомнув {target}.",
            "вірш": "📜 {sender} читає вірш для {target}.",
            "покусати": "😝 {sender} кусь {target}!",
        },
        
        # --- Marriage ---
        "marriage_cost": 420,
        "msg_propose_sender": "Святий союз з <b>{target}</b> коштує <b>{cost} м'яток</b>. Маєш годину!",
        "msg_propose_success": "Мяу! Пропозиція для <b>{target}</b> надіслана. Чекаємо відповіді.",
        "msg_already_married": "Мур! <b>{user}</b>, ти вже у шлюбі! Ніякої полігамії.",
        "msg_self_marriage": "Одружуватися з собою? Егоїстично, навіть для кота.",
        "msg_bot_marriage": "Я одружений з роботою (і м'ятою).",
        "msg_no_money": "Треба <b>{cost} м'яток</b>, а в тебе лише <b>{balance}</b>. Йди працюй!",
        "msg_proposal_expired": "Час вийшов. Пропозиція протухла, як стара риба.",
        "msg_not_your_proposal": "Це не твоя миска! Не чіпай кнопку.",
        "msg_accept_success": "🎉 <b>АЛЕЛУЯ!</b> {user1} та {user2} тепер разом!",
        "msg_decline_success": "Відмова. {target} гуляє сам по собі.",
        "msg_no_marriage": "Ти вільний котик. Хочеш пару? /propose",
        "msg_divorce_prompt": "Точно розлучення? Подушки вже поділили?",
        "msg_divorce_success": "Розлучення оформлено. Свобода!",
        "msg_divorce_cancel": "Хух! Залишаєтесь разом. Чудово.",
        "msg_target_not_found": "Не бачу такого котика.",
        "msg_target_group": "Це група, а не котик!",
        "msg_target_db_not_found": "Котика @{} немає в базі.",
        "msg_target_api_error": "Помилка зв'язку з @{}.",
        "marriage_certificate_caption": "<b>† СВЯЩЕННИЙ СОЮЗ †</b>\n{user1} + {user2}\nДата: {date}",
        
        "msg_nun_of_the_day": "✝️ <b>Монашка дня:</b> {nun_mention}! Молись і гріши (в міру).",
        "prediction_text": "🔮 <b>Передбачення:</b>\n{prediction}",
    },

    # ---------------------------
    # --- МОД: WINTER (Зима) ---
    # ---------------------------
    BotTheme.WINTER: {
        # --- AI Prompts (Зимові варіації) ---
        "ai_prompt_charismatic": PROMPT_CHARISMATIC_WINTER,
        "ai_prompt_academic": PROMPT_ACADEMIC, 

        # --- Icons ---
        "icon_player_x": "❄️", "icon_player_o": "☃️", "icon_empty": "▫️",
        "icon_nun": "🎅", "icon_cat": "🦌", "icon_mint": "🎄", "icon_fish": "🎁",

        # --- Text ---
        "start_menu_text": (
            "Мур-мур, {name}! 🌨️\n"
            "Я — зимовий Котик. Є ігри, нагадування, профіль, шлюби, передбачення.\n"
            "Меню нижче — все справжнє, тисни. 😼"
        ),
        "about_bot_text": "<b>🐾 Про мене (Зима) 🐾</b>\nЯ відповідаю за сніг, подарунки та чорний гумор під ялинкою.",

        # --- Casino ---
        "casino_slots": [("🦌", 8), ("🎄", 6), ("🎁", 5), ("🎅", 3), ("❄️", 7)],
        "casino_win_multipliers": {
            ("🎅", "🎅", "🎅"): 100, ("❄️", "❄️", "❄️"): 75, ("🎁", "🎁", "🎁"): 50,
            ("🎄", "🎄", "🎄"): 30, ("🦌", "🦌", "🦌"): 20, ("🎅", "🎅"): 1, ("❄️", "❄️"): 1,
            ("🎁", "🎁"): 1, ("🎄", "🎄"): 1, ("🦌", "🦌"): 1
        },

        # --- Actions (Зимові) ---
        "actions": {
            "обійняти": "🌨️ {sender} гріє {target} в обіймах.",
            "вилизати": "👅 {sender} злизав сніг з {target}.",
            "вдарити": "💥 {sender} кинув сніжком у {target}.",
            "погладити": "☺️ {sender} гладить {target} біля каміна.",
            "мур": "🐾 {sender} муркоче різдвяну пісню {target}.",
            "шшш": "😾 {sender} шипить: 'Де подарунки?!' на {target}.",
            "мяу": "🐾 {sender} просить мандаринку у {target}.",
            "чай": "🍷 {sender} наливає глінтвейн {target}.",
            "притиснутись": "🥰 {sender} гріється об {target}.",
            "ляпас": "🖐 {sender} дає {target} ляпаса мішурою!",
            "нагодувати": "🍪 {sender} дав {target} імбирний пряник.",
            "бу": "👻 {sender} вистрибнув з-під ялинки на {target}!",
            "танець": "💃 {sender} кружляє {target} у хуртовині.",
            "поцілувати": "💋 {sender} цілує {target} під омелою.",
            "вірш": "📜 {sender} читає колядку для {target}.",
            "покусати": "😝 {sender} кусає {target} за гірлянду.",
            # Нові
            "снігом": "⛄ {sender} засипає снігом {target}!",
            "подарунок": "🎁 {sender} дарує щось {target}.",
        },

        # --- Marriage (Зимові) ---
        "marriage_cost": 500,
        "msg_propose_sender": "❄️ Новорічне освідчення для <b>{target}</b> коштує <b>{cost} м'яток</b>. Санта чекає!",
        "msg_propose_success": "Хо-хо! Лист до <b>{target}</b> відправлено оленячою поштою.",
        "msg_already_married": "Ти вже маєш пару на цю зиму, <b>{user}</b>!",
        "msg_self_marriage": "Сам собі Санта? Ні, шукай пару.",
        "msg_bot_marriage": "Я одружений зі снігом.",
        "msg_no_money": "Треба <b>{cost}</b> на подарунки, а в тебе <b>{balance}</b>. Ельфи сміються!",
        "msg_proposal_expired": "Сніг розтанув, пропозиція теж.",
        "msg_not_your_proposal": "Не чіпай чужий подарунок!",
        "msg_accept_success": "🎄 <b>РІЗДВЯНЕ ДИВО!</b> {user1} та {user2} тепер разом гріються!",
        "msg_decline_success": "Холод... {target} відмовив(ла).",
        "msg_no_marriage": "Ти самотній олень. Шукаєш пару? /propose",
        "msg_divorce_prompt": "Розлучення під Новий Рік? Серйозно?",
        "msg_divorce_success": "Розлучені. Холодно і самотньо.",
        "msg_divorce_cancel": "Магія свят врятувала шлюб!",
        "msg_target_not_found": "Де цей ельф? Не бачу.",
        "msg_target_group": "Це гурт колядників, а не один юзер!",
        "msg_target_db_not_found": "Юзера @{} немає в списках Санти.",
        "msg_target_api_error": "Завірюха, зв'язку немає з @{}.",
        "marriage_certificate_caption": "<b>❄️ ЗИМОВИЙ СОЮЗ ❄️</b>\n{user1} + {user2}\nБлагословення Санти 🎅",

        "msg_nun_of_the_day": "🎅 <b>Ельф дня:</b> {nun_mention}! Твори дива!",
        "prediction_text": "🔮 <b>Прогноз на 2025:</b>\n{prediction}",
    }
}

# =============================================================================
# РОЗДІЛ 4: УТИЛІТИ ОТРИМАННЯ ЗНАЧЕНЬ (ГЕТТЕРИ)
# =============================================================================

async def get_theme_value(key: str, default_value: Any = None) -> Any:
    """Універсальний геттер значень з поточної теми."""
    theme = await get_current_theme()
    return theme.get(key, default_value)

async def get_actions() -> Dict[str, str]:
    return await get_theme_value("actions", {})

async def get_casino_slots() -> list:
    return await get_theme_value("casino_slots", [])

async def get_casino_multipliers() -> Dict:
    return await get_theme_value("casino_win_multipliers", {})

async def get_icons() -> Dict[str, str]:
    theme = await get_current_theme()
    keys = [
        "icon_player_x",
        "icon_player_o",
        "icon_empty",
        "icon_nun",
        "icon_cat",
        "icon_mint",
        "icon_fish",
    ]
    return {k: theme.get(k, "❓") for k in keys}

async def get_icon(name: str) -> str:
    theme = await get_current_theme()
    return theme.get(name, "❓")

async def get_start_menu_text() -> str:
    return await get_theme_value("start_menu_text", "Привіт!")

async def get_about_bot_text() -> str:
    return await get_theme_value("about_bot_text", "Я бот.")

async def get_marriage_cost() -> int:
    return int(await get_theme_value("marriage_cost", 420))

async def get_marriage_messages() -> Dict[str, str]:
    theme = await get_current_theme()
    keys = [
        "propose_sender",
        "propose_success",
        "already_married",
        "self_marriage",
        "bot_marriage",
        "no_money",
        "proposal_expired",
        "not_your_proposal",
        "accept_success",
        "decline_success",
        "no_marriage",
        "divorce_prompt",
        "divorce_success",
        "divorce_cancel",
        "target_not_found",
        "target_group",
        "target_db_not_found",
        "target_api_error",
        "marriage_certificate_caption",
    ]
    return {k: theme.get(f"msg_{k}", theme.get(k, "")) for k in keys}


# =============================================================================
# РОЗДІЛ 4.5: СИСТЕМА УНІФІКОВАНИХ ЗВЕРНЕНЬ (AddressingContext)
# =============================================================================

_MALE_GENDERS = {"кіт", "кот", "male", "m", "ч", "чоловік"}
_FEMALE_GENDERS = {"киця", "кицька", "female", "f", "ж", "жінка"}


class AddressingContext:
    """
    Контекст звернення до користувача на основі *поля gender з профілю*.

    Контракт:
    - Джерело статі: тільки gender з профілю. Ніколи не вгадувати за ім'ям/нікнеймом/аватаром/текстом.
    - male → чоловічий рід (він, зробив, пішов)
    - female → жіночий рід (вона, зробила, пішла)
    - none/unknown/null → ввічливе «Ви», нейтральні конструкції (fail-safe)
    - Стиль звернення не змінюється посеред діалогу; інші користувачі мають свій власний контекст.
    """

    def __init__(self, gender: Optional[str]):
        gender_norm = (gender or "").strip().lower()
        self.raw_gender = gender

        if gender_norm in _MALE_GENDERS:
            self._type = "male"
        elif gender_norm in _FEMALE_GENDERS:
            self._type = "female"
        else:
            self._type = "neutral"

    @property
    def noun(self) -> str:
        if self._type == "male":
            return "він"
        if self._type == "female":
            return "вона"
        return "Ви"

    @property
    def possessive(self) -> str:
        if self._type == "male":
            return "його"
        if self._type == "female":
            return "її"
        return "Ваш"

    @property
    def you(self) -> str:
        return "Ви" if self._type == "neutral" else "ти"

    @property
    def your(self) -> str:
        if self._type == "neutral":
            return "Ваш"
        if self._type == "female":
            return "твоя"
        return "твій"

    def verb(
        self,
        base: str,
        past_male: str = "",
        past_female: str = "",
        past_neutral: str = "",
    ) -> str:
        if not past_male:
            return base

        if self._type == "male":
            return past_male
        if self._type == "female":
            return past_female or past_male.replace("ив", "ила").replace("ів", "іла")
        return past_neutral or past_male.replace("ив", "или").replace("ів", "іли")

    def adj(self, male_form: str, female_form: str = "", neutral_form: str = "") -> str:
        if self._type == "male":
            return male_form
        if self._type == "female":
            return female_form or male_form.replace("ий", "а").replace("ій", "я")
        return neutral_form or male_form.replace("ий", "і").replace("ій", "і")

    def past(self, male: str, female: str = "", neutral: str = "") -> str:
        return self.verb("", male, female, neutral)


async def get_user_addressing(user_id: int) -> AddressingContext:
    try:
        from bot.core.database import get_user_profile

        profile = await get_user_profile(user_id)
        gender = profile.get("gender")
        return AddressingContext(gender)
    except Exception as e:
        logger.warning(f"Не вдалося отримати профіль для {user_id}: {e}. Використовую neutral.")
        return AddressingContext(None)


# =============================================================================
# РОЗДІЛ 5: ЗАГАЛЬНІ УТИЛІТИ
# =============================================================================

def mention(user: User) -> str:
    name = html.escape(user.first_name)
    return f"<a href='tg://user?id={user.id}'>{name}</a>"


def format_target_mention(user: User) -> str:
    return mention(user)


async def get_user_from_username(context: ContextTypes.DEFAULT_TYPE, username: str) -> Optional[User]:
    try:
        chat_obj = await context.bot.get_chat(f"@{username.lstrip('@')}")
        if chat_obj.type == "private":
            return User(
                id=chat_obj.id,
                first_name=chat_obj.first_name,
                is_bot=chat_obj.is_bot,
                username=chat_obj.username,
                last_name=chat_obj.last_name,
                language_code=chat_obj.language_code,
            )
        return None
    except Exception as e:
        logger.warning(f"User resolve error @{username}: {e}")
        return None


def sanitize_reply(text: str) -> str:
    return text.strip() if text else ""


async def safe_reply(update: Update, text: str):
    if not text:
        return

    max_len = 4096
    for i in range(0, len(text), max_len):
        try:
            await update.message.reply_html(text[i : i + max_len])
        except Exception as e:
            logger.error(f"Error in safe_reply: {e}")


async def send_typing_periodically(bot, chat_id, interval: float = 4.0):
    try:
        while True:
            await bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
            await asyncio.sleep(interval)
    except asyncio.CancelledError:
        pass
    except Exception as e:
        logger.debug(f"Typing error: {e}")


def format_time(remaining: timedelta) -> str:
    minutes, seconds = divmod(int(remaining.total_seconds()), 60)
    return f"{minutes} хв {seconds} сек"
