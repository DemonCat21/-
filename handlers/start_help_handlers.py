# start_help_handlers.py
# -*- coding: utf-8 -*-
"""
Обробники для команд /start, /help, /profile та навігаційних меню.
Це "вхідна точка" та келія допомоги для користувачів. 🐾
"""
import logging
import html
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, CallbackQuery
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import (
    ContextTypes,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
    Application,
)

from bot.utils.utils import get_about_bot_text, get_start_menu_text, cancel_auto_close, set_auto_close_payload, start_auto_close

logger = logging.getLogger(__name__)

# Автозакриття для стартового меню
START_AUTO_CLOSE_KEY = "start_menu"

async def _arm_start_auto_close(context: ContextTypes.DEFAULT_TYPE, message) -> None:
    if not message:
        return
    cancel_auto_close(context, START_AUTO_CLOSE_KEY)
    set_auto_close_payload(
        context,
        START_AUTO_CLOSE_KEY,
        chat_id=message.chat_id,
        message_id=message.message_id,
        fallback_text="Стартове меню закрито через бездіяльність.",
    )
    # Check if auto_delete_actions is enabled
    from bot.core.database import get_chat_settings
    settings = await get_chat_settings(message.chat_id)
    if settings.get('auto_delete_actions', 0) == 1:
        start_auto_close(context, START_AUTO_CLOSE_KEY, timeout=420)  # 7 minutes


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обробляє /start — показує головний хаб."""
    source = "command" if update.message and update.message.text.startswith("/") else "text_alias"
    logger.info(f"[START_HELP] Команда /start отримана від {update.effective_user.id} (джерело: {source}).")
    await send_main_menu(update, context, is_callback=False)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обробляє /help — короткий гайд по всьому функціоналу."""
    await send_help_page(update, context, is_callback=False)

async def start_command_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Обробляє повернення до головного меню з кнопок.
    """
    logger.info(f"Callback 'back_to_main_menu' від {update.callback_query.from_user.id}.")
    await query_answer_safe(update.callback_query) # (НОВЕ) Безпечна відповідь
    await send_main_menu(update, context, is_callback=True)

async def query_answer_safe(query: CallbackQuery) -> None:
    """
    (НОВЕ) Безпечна відповідь на callback, ігноруючи помилки.
    """
    try:
        await query.answer()
    except BadRequest:
        pass # Часто буває, якщо юзер клікає занадто швидко

async def send_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, is_callback: bool = False) -> None:
    """
    Відправляє або редагує повідомлення, показуючи головне меню.
    """
    user = update.effective_user if not is_callback else update.callback_query.from_user
    chat_id = update.effective_chat.id if not is_callback else update.callback_query.message.chat.id

    # Динамічний заголовок/привітання з теми + наш UX-хаб
    start_text = await get_start_menu_text()
    text = (
        f"{start_text.format(name=html.escape(user.first_name))}\n\n"
        "<i>Обирай, що робити ↓</i>"
    )

    # Головне меню: всі функції, але без профілю/про мене (за вимогою)
    keyboard = [
        [InlineKeyboardButton("💬 Як зі мною говорити?", callback_data="show_communication_short_guide")],
        [
            InlineKeyboardButton("🎮 Ігри", callback_data="show_games_menu"),
            InlineKeyboardButton("📊 Топи та статистика", callback_data="show_stats_menu"),
        ],
        [
            InlineKeyboardButton("⏰ Нагадування", callback_data="show_reminders_menu"),
            InlineKeyboardButton("💍 Шлюби", callback_data="show_marriage_menu"),
        ],
        [
            InlineKeyboardButton("⚙️ Налаштування чату", callback_data="show_chat_settings_help"),
            InlineKeyboardButton("📜 Команди чату", callback_data="show_chat_commands"),
        ],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    try:
        if is_callback:
            await update.callback_query.edit_message_text(
                text=text,
                reply_markup=reply_markup,
                parse_mode=ParseMode.HTML
            )
        else:
            sent = await context.bot.send_message(
                chat_id=chat_id,
                text=text,
                reply_markup=reply_markup,
                parse_mode=ParseMode.HTML
            )
            await _arm_start_auto_close(context, sent)
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            logger.error(f"Помилка оновлення головного меню для {chat_id}: {e}")
    except Exception as e:
        logger.error(f"Неочікувана помилка в send_main_menu для {chat_id}: {e}", exc_info=True)


async def send_help_page(update: Update, context: ContextTypes.DEFAULT_TYPE, is_callback: bool) -> None:
    """Показує /help як гайд (без зміни вкладки 'як зі мною спілкуватись')."""
    if is_callback:
        query = update.callback_query
        await query_answer_safe(query)
        chat_id = query.message.chat.id
    else:
        chat_id = update.effective_chat.id

    text = (
        "<b>❓ Довідка</b>\n\n"
        "Нижче розжовано все, навіть дрібниці. Читай, як казку перед сном.\n\n"
        "<b>🚪 Старт і спілкування</b>\n"
        "• /start — відкриває головне меню з кнопками. Загубився? Тисни сюди.\n"
        "• /help — ця довідка.\n"
        "• Просто пиши. У групі кликай мене по імені (котику/кошеня/кіт) або відповідай на моє повідомлення, щоб я почув.\n"
        "• /about_bot — хто я і навіщо.\n\n"
        "<b>🎮 Ігри (все про розваги)</b>\n"
        "• /newgame — створити лобі. Кнопками обери режим: «Мемчики та котики» або «Хрестики-Нулики».\n"
        "• /stop або /stopgame — зупинити поточну гру.\n"
        "• /leave — вийти самому.\n"
        "• /kick — прибрати гравця (для ведучого/адміна гри).\n"
        "• /add_sit — додати свою ситуацію в Мемчики (адмін).\n"
        "• /top — топ гравців у Мемчиках цього чату.\n"
        "• Мемчики: я даю ситуацію → ви кидаєте мем з руки → голосуєте кнопками → хтось бере очки. Ліміт очок і таймери налаштовуються в /settings.\n"
        "• Хрестики-Нулики: можна грати з ботом чи людьми. Перемоги/нічії/програші йдуть у статистику профілю.\n"
        "• Міні-ігри: /rps (камінь-ножиці-папір), /guess (вгадай число 1-9).\n\n"
        "<b>🧠 Режими відповіді бота</b>\n"
        "• /set_mode — вибрати стиль: академічний (факти, спокій) або харизматичний (сарказм, котячий вайб).\n"
        "• /current_mode — який стиль зараз.\n"
        "• Теми (зима/дефолт) впливають на харизматичний стиль і іконки ігор.\n\n"
        "<b>🗒️ Пам'ять (потрібно звертання)</b>\n"
        "• «котику, запам'ятай що ...» — зберігаю факт про чат/тебе.\n"
        "• «котику, забудь ...» — видаляю факт за ключем.\n"
        "• /memories — показую, що пам'ятаю (для чату і тебе).\n"
        "• Пам'ять можна чистити адміном через /settings (видалення фактів).\n\n"
        "<b>⏰ Нагадування</b>\n"
        "• Проста фраза: «котику, нагадай завтра о 9 купити хліб» — я сам витягну час і текст.\n"
        "• /myreminders — список твоїх нагадувань з кнопками видалення.\n"
        "• Підтримка повторів (recur_interval) і статусів (ACTIVE/PAUSED) — адміни можуть керувати через меню нагадувань.\n"
        "• Автозакриття меню ~60 с бездіяльності, кнопка «Закрити» є.\n\n"
        "<b>📊 Профіль і баланс</b>\n"
        "• /profile — картка з балансом 🌿, статтю, містом, цитатою, статистикою ігор. Кнопками можна редагувати статтю/місто/цитату.\n"
        "• Баланс використовується у казино та шлюбах.\n"
        "• /balance — скільки в тебе м'яти зараз.\n\n"
        "<b>🎰 Казино</b>\n"
        "• /casino [ставка] — крутити слоти (напр.: /casino 100).\n"
        "• /casino all — поставити все.\n"
        "• /casino_help — правила і множники.\n"
        "• Слоти йдуть з емо-іконками теми (дефолт/зима).\n\n"
        "<b>💍 Шлюби</b>\n"
        "• /propose або «одружитися ...» — відправити пропозицію. Має ціну в м'яті.\n"
        "• /marriage — показати свій статус.\n"
        "• /divorce — розірвати союз.\n"
        "• Час дії пропозиції обмежений, чужу пропозицію прийняти не можна.\n\n"
        "<b>🔮 Передбачення</b>\n"
        "• /prediction — персональне пророцтво на сьогодні (оновлюється щодня).\n"
        "• Є щоденне авто-роздавання передбачень усім користувачам (завдання cron).\n\n"
        "<b>📈 Топи та рейтинги</b>\n"
        "• /score — рейтинг гравців у цьому чаті.\n"
        "• /globaltop — глобальний рейтинг усіх чатів.\n"
        "• /memtop — топ по Мемчиках.\n"
        "• Статистика ігор (wins/losses/draws) зберігається й показується у профілі.\n\n"
        "<b>🧹 Модерація і фільтри (адміни)</b>\n"
        "• /settings у групі — відкриває меню в особисті повідомлення. Там: увімк/вимк модулі (AI, команди, ігри, шлюби, нагадування, фільтр слів), ліміти, таймери.\n"
        "• Фільтр слів: додати/видалити слова, список слів, автоматичні попередження.\n"
        "• Попередження: система warn, ліміт покарання задається в /settings.\n"
        "• New Year mode: авто/вкл/викл зимового оформлення (іконки/тексти).\n"
        "• Мемчики: можна міняти час на хід/голосування, ліміт очок, макс. гравців, розмір руки.\n"
        "• Привітання/правила: редагуються через /settings (welcome_message, rules).\n\n"
        "<b>🤖 ШІ (якщо увімкнено)</b>\n"
        "• Я відповідаю на текстові питання за обраним режимом.\n"
        "• Глобальний перемикач AI: в /settings можна вимкнути для всіх чатів або для конкретного.\n"
        "• Історія діалогів кешується, очищається адміном (clear history в /settings або окрема команда).\n\n"
        "<b>🛠 Інше</b>\n"
        "• Кнопка «Команди чату» в головному меню — стислий список дозволених команд для групи.\n"
        "• Якщо щось зламалось — /start оновить меню. Якщо й далі біда — пиши власнику (OWNER_ID у конфіг)."
    )

    keyboard = [[InlineKeyboardButton("⬅️ Назад", callback_data="back_to_main_menu")]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    try:
        if is_callback:
            await update.callback_query.edit_message_text(text=text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)
        else:
            await context.bot.send_message(chat_id=chat_id, text=text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            logger.error(f"Помилка показу help для {chat_id}: {e}")


async def show_help_page(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await send_help_page(update, context, is_callback=True)


async def show_stats_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Інфо-сторінка зі статистикою/топами (без зміни логіки команд)."""
    query = update.callback_query
    await query_answer_safe(query)
    text = (
        "<b>📊 Топи та статистика</b>\n\n"
        "<code>/score</code> — рейтинг цього чату\n"
        "<code>/globaltop</code> — світовий рейтинг\n"
        "<code>/memtop</code> — топ по Мемчиках\n\n"
        "<i>Пізніше зробимо вибір топів кнопками. 😼</i>"
    )
    keyboard = [[InlineKeyboardButton("⬅️ Назад", callback_data="back_to_main_menu")]]
    await _edit_callback_page(query, text, keyboard)


async def show_reminders_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Інфо-сторінка по нагадуваннях (логіку не змінюємо)."""
    query = update.callback_query
    await query_answer_safe(query)
    text = (
        "<b>⏰ Нагадування</b>\n\n"
        "Щоб створити нагадування, напиши мені в ПП або в групі:\n"
        "«кошеня/котику/бот, нагадай [коли] [що]»\n\n"
        "видалення: <code>/myreminders</code> або «Мої нагадування»\n\n"
    )
    keyboard = [[InlineKeyboardButton("⬅️ Назад", callback_data="back_to_main_menu")]]
    await _edit_callback_page(query, text, keyboard)


async def show_marriage_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Інфо-сторінка про шлюби (логіку не змінюємо)."""
    query = update.callback_query
    await query_answer_safe(query)
    text = (
        "<b>💍 Шлюби</b>\n\n"
        "Це ігрово-соціальна механіка: пропозиції, союз, розлучення.\n\n"
        "• <code>/propose</code> або <code>одружитися …</code>\n"
        "• <code>/marriage</code> або <code>шлюб</code>\n"
        "• <code>/divorce</code> або <code>розлучитися</code>"
    )
    keyboard = [[InlineKeyboardButton("⬅️ Назад", callback_data="back_to_main_menu")]]
    await _edit_callback_page(query, text, keyboard)


async def show_chat_settings_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Пояснює, як відкривати /settings (меню настоятеля) — без зміни логіки."""
    query = update.callback_query
    await query_answer_safe(query)
    text = (
        "<b>⚙️ Налаштування чату</b>\n\n"
        "Меню налаштувань відкривається командою <code>/settings</code> у вашій групі.\n"
        "Бот надішле керування в приватні повідомлення (ПП).\n\n"
        "<i>Доступно лише адмінам.</i>"
    )
    keyboard = [[InlineKeyboardButton("⬅️ Назад", callback_data="back_to_main_menu")]]
    await _edit_callback_page(query, text, keyboard)


async def _edit_callback_page(query: CallbackQuery, text: str, keyboard_rows) -> None:
    reply_markup = InlineKeyboardMarkup(keyboard_rows)
    try:
        await query_answer_safe(query)
        await query.edit_message_text(text=text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            logger.error(f"Помилка редагування сторінки меню: {e}")


async def show_games_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Показує меню вибору категорії ігор.
    """
    query = update.callback_query
    await query_answer_safe(query)

    text = "Ігри - це мирська суєта, але іноді вона необхідна для смирення... 😼\nОбери свій шлях:"
    keyboard = [
        [
            InlineKeyboardButton("❌ Хрестики-Нулики ⭕️", callback_data="show_tic_tac_toe_menu"),
            InlineKeyboardButton("🤔 Мемчики", callback_data="mems_games_menu"),
        ],
        [InlineKeyboardButton("🎲 Міні-ігри", callback_data="show_mini_games_menu")],
        [InlineKeyboardButton("🎰 Мур-Казино", callback_data="show_casino_menu")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="back_to_main_menu")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    try:
        await query.edit_message_text(text=text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)
    except BadRequest as e:
        if "Message is not modified" not in str(e):
             logger.error(f"Помилка оновлення меню ігор: {e}")


async def show_tic_tac_toe_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Показує команди для гри в Хрестики-Нулики."""
    query = update.callback_query
    await query_answer_safe(query)

    text = (
        "<b>❌⭕️ Хрестики-Нулики ⭕️❌</b>\n\n"
        "<code>/newgame</code>\n"
        "<i>(Меню ігор → обери «Хрестики-Нулики» і збери 2 гравців кнопками)</i>\n\n"
        "<code>!гра</code>\n"
        "<i>(Швидка дуель ⚔️: дай відповідь на повідомлення друга і напиши !гра)</i>\n\n"
        "<code>/stopgame</code>\n"
        "<i>(Завершити гру 🛑. У відповідь на гру)</i>\n\n"
        "<code>/score</code>\n"
        "<i>(Рейтинг цього чату 📊)</i>\n\n"
        "<code>/globaltop</code>\n"
        "<i>(Світовий рейтинг 🌍)</i>"
    )
    keyboard = [[InlineKeyboardButton("⬅️ Назад до ігор", callback_data="show_games_menu")]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    try:
        await query.edit_message_text(text=text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)
    except BadRequest as e:
        if "Message is not modified" not in str(e):
             logger.error(f"Помилка оновлення меню XO: {e}")

async def show_mems_games_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Показує команди для гри в Мемчики."""
    query = update.callback_query
    await query_answer_safe(query)

    text = (
        "<b>😼 Мемчики та котики 😼</b>\n\n"
        "<code>/newgame</code> - Створити гру\n"
        "<code>/stop</code> - Зупинити гру\n"
        "<code>/leave</code> - Вийти з гри\n"
        "<code>/kick</code> - Вигнати гравця\n"
        "<code>/add_sit</code> - Додати ситуацію (адмін)"
    )
    keyboard = [
        [InlineKeyboardButton("Детальніше? 🤔", callback_data="show_mems_full_guide")],
        [InlineKeyboardButton("⬅️ Назад до ігор", callback_data="show_games_menu")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    try:
        await query.edit_message_text(text=text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)
    except BadRequest as e:
        if "Message is not modified" not in str(e):
             logger.error(f"Помилка оновлення меню Мемчиків: {e}")

async def show_mems_full_guide(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None: 
    """Показує детальний гайд по Мемчикам."""
    query = update.callback_query
    await query_answer_safe(query)

    text = (
        "<b>1. Набір:</b> Запускаєш /newgame, приєднуєшся.\n"
        "<b>2. Хід:</b> Бот дає ситуацію, ти обираєш найприкольніший мем зі своєї колоди\n"
        "<b>3. Голосування:</b> Гравці голосують за найкращий прікол.\n"
        "<b>4. Перемога:</b> Граємо до досягнення ліміту очок (за замовчуванням 10, але все можна фіксити в налаштуваннях).\n\n"
        "до речі про налаштування - їх можна змінювати прямо в групі командою /settings.\n\n"
        "<b>Мої команди:</b>\n"
        "<code>/newgame</code> - Створити гру\n"
        "<code>/stop</code> - Зупинити гру\n"
        "<code>/leave</code> - Вийти з гри\n"
        "<code>/kick</code> - Вигнати гравця\n"
        "<code>/add_sit</code> - Додати ситуацію (адмін)\n"
        "<code>/top</code> - Топ гравців\n\n"
        "пе.ес. мінімальна кількість гравців 2, але тоді ви граєте по фану, сенсу 0"
    )
    keyboard = [
        [InlineKeyboardButton("⬅️ Назад", callback_data="show_games_menu")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    try:
        await query.edit_message_text(text=text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)
    except BadRequest as e:
        if "Message is not modified" not in str(e):
             logger.error(f"Помилка оновлення full_guide: {e}")
async def show_mini_games_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Показує команди для міні-ігор."""
    query = update.callback_query
    await query_answer_safe(query)

    text = (
        "<b>🎲 Смиренні міні-ігри 🎲</b>\n\n"
        "Використовуй ці команди для короткої розваги:\n\n"
        "<code>/rps</code> (або кнп) - <b>🪨📄✂️</b>\n"
        "<i>(Перевір свою удачу проти моєї котячої лапки)</i>\n\n"
        "<code>/guess</code> (або вгадай) - <b>🔢 Вгадай число</b>\n"
        "<i>(Я загадав число від 1 до 9. Спробуй вгадати)</i>\n\n"
        "<i>(Відрізни котячу правду від монашої вигадки)</i>\n\n"
        "<code>/prediction</code> (або моє передбачення) - <b>🌠 Передбачення</b>\n"
        "<i>(Дізнайся, що зірки шепочуть тобі сьогодні)</i>"
    )
    keyboard = [[InlineKeyboardButton("⬅️ Назад до ігор", callback_data="show_games_menu")]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    try:
        await query.edit_message_text(text=text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)
    except BadRequest as e:
        if "Message is not modified" not in str(e):
             logger.error(f"Помилка оновлення меню міні-ігор: {e}")


async def show_casino_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Показує команди для казино."""
    query = update.callback_query
    await query_answer_safe(query)

    text = (
        "<b>🎰 Мур-Казино 🎰</b>\n\n"
        "Випробуй свою фортуну. Але пам'ятай, азарт - це гріх... солодкий гріх. 😼\n\n"
        "<code>/casino</code> або <code>казино [ставка]</code>\n"
        "<i>(Крутити слоти. Напр.: <code>casino 100</code>)</i>\n\n"
        "<code>/casino all</code> або <code>казино все</code>\n"
        "<i>(Ризикнути всім. Поставити всю свою м'яту 🌿)</i>\n\n"
        "<code>/balance</code> або <code>баланс</code>\n"
        "<i>(Перевірити келію. Дізнатися, скільки 🌿 в тебе залишилось)</i>\n\n"
        "<code>/casino_help</code> або <code>казино допомога</code>\n"
        "<i>(Правила. Дізнайся, як виграти джекпот ✝️✝️✝️)</i>"
    )
    keyboard = [[InlineKeyboardButton("⬅️ Назад до ігор", callback_data="show_games_menu")]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    try:
        await query.edit_message_text(text=text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)
    except BadRequest as e:
        if "Message is not modified" not in str(e):
             logger.error(f"Помилка оновлення меню казино: {e}")


async def show_communication_short_guide(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Показує короткий посібник зі спілкування."""
    query = update.callback_query
    await query_answer_safe(query)

    text = (
        "<b>🐾 Як зі мною говорити? 🐾</b>\n\n"
        "<b>У приватних чатах:</b>\n"
        "Просто муркай мені, що спаде на думку. Я завжди слухаю. 🤫\n\n"
        "<b>У групових чатах:</b>\n"
        "Щоб привернути мою котячу увагу, поклич мене (<code>котик</code>, <code>кошеня</code>, <code>кіт</code>) "
        "або дай відповідь на моє повідомлення. 🗣️"
    )
    keyboard = [
        [InlineKeyboardButton("Детальніше? 🤔", callback_data="show_communication_full_guide")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="back_to_main_menu")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    try:
        await query.edit_message_text(text=text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)
    except BadRequest as e:
        if "Message is not modified" not in str(e):
             logger.error(f"Помилка оновлення short_guide: {e}")


async def show_communication_full_guide(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Показує повний посібник (зі зміненими командами пам'яті)."""
    query = update.callback_query
    await query_answer_safe(query)

    text = (
        "<b>ℹ️ Детальний посібник ℹ️</b>\n\n"
        "Я можу спілкуватися у двох режимах:\n"
        "• <b>Академічний 🎓</b>: Смиренні відповіді, засновані на фактах.\n"
        "• <b>Харизматичний 😼</b>: З гумором, іронією та котячою величчю.\n\n"
        "<b>Змінити мій режим:</b>\n"
        "<code>/set_mode</code> або <code>/режим</code>\n"
        "<b>Мій поточний режим:</b> <code>/current_mode</code>\n\n"
        "<b>Керування моєю пам'яттю (потрібне звернення!):</b>\n"
        "<code>[звернення] запам'ятай що [факт]</code>\n"
        "<i>(Напр.: «котик, запам'ятай що я люблю м'яту»)</i> 🧠\n\n"
        "<code>[звернення] забудь [ключ]</code>\n"
        "<i>(Напр.: «@bot забудь уподобання»)</i> 🗑️\n\n"
        "<code>/memories</code> або <code>/память</code>\n"
        "<i>(Показує, що я пам'ятаю про тебе та цей чат)</i> 📖"
    )
    keyboard = [[InlineKeyboardButton("⬅️ Назад", callback_data="back_to_main_menu")]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    try:
        await query.edit_message_text(text=text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)
    except BadRequest as e:
        if "Message is not modified" not in str(e):
             logger.error(f"Помилка оновлення full_guide: {e}")


async def about_bot_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Надсилає інформацію про бота."""
    is_callback = update.callback_query is not None
    chat_id = update.effective_chat.id

    if is_callback:
        await query_answer_safe(update.callback_query)

    text = await get_about_bot_text()
    keyboard = [[InlineKeyboardButton("⬅️ Назад", callback_data="back_to_main_menu")]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    try:
        if is_callback:
            await update.callback_query.edit_message_text(
                text=text,
                reply_markup=reply_markup,
                parse_mode=ParseMode.HTML
            )
        else:
            await context.bot.send_message(
                chat_id=chat_id,
                text=text,
                reply_markup=reply_markup,
                parse_mode=ParseMode.HTML
            )
    except BadRequest as e:
        if "Message is not modified" not in str(e):
             logger.error(f"Помилка оновлення about_bot: {e}")
    except Exception as e:
        logger.error(f"Неочікувана помилка в about_bot_command: {e}", exc_info=True)


def register_start_help_handlers(application: Application):
    """Реєструє обробники для команд старту, допомоги та меню."""
    
    # (ВИПРАВЛЕНО) Додано українські аліаси до КОМАНД
    application.add_handler(CommandHandler(["start"], start_command))
    application.add_handler(CommandHandler(["help"], help_command))
    application.add_handler(CommandHandler(["about_bot"], about_bot_command))

    # Обгортка для логування текстових матчів
    async def logged_start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
        msg_text = update.message.text if update.message else "N/A"
        logger.info(f"[START_HELP] Текстовий матч для '/start': '{msg_text}' від {update.effective_user.id}")
        await start_command(update, context)
    
    
    async def logged_about_bot(update: Update, context: ContextTypes.DEFAULT_TYPE):
        msg_text = update.message.text if update.message else "N/A"
        logger.info(f"[START_HELP] Текстовий матч для 'про бота': '{msg_text}' від {update.effective_user.id}")
        await about_bot_command(update, context)

    # Текстові аліаси (гнучкіші: дозволяємо пробіли та кінцеві пунктуації)
    application.add_handler(MessageHandler(filters.TEXT & filters.Regex(r"(?i)^\s*(старт|почати|меню)\s*[!\.,]?\s*$"), logged_start_command))
    async def logged_help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
        msg_text = update.message.text if update.message else "N/A"
        logger.info(f"[START_HELP] Текстовий матч для '/help': '{msg_text}' від {update.effective_user.id}")
        await help_command(update, context)

    application.add_handler(MessageHandler(filters.TEXT & filters.Regex(r"(?i)^\s*(допомога|хелп)\s*[!\.,]?\s*$"), logged_help_command))
    application.add_handler(MessageHandler(filters.TEXT & filters.Regex(r"(?i)^\s*про\s+бота\s*[!\.,]?\s*$"), logged_about_bot))

    # --- Головні Кнопки Меню ---
    application.add_handler(CallbackQueryHandler(start_command_callback, pattern="^back_to_main_menu$"))
    application.add_handler(CallbackQueryHandler(show_games_menu, pattern="^show_games_menu$"))
    application.add_handler(CallbackQueryHandler(show_stats_menu, pattern="^show_stats_menu$"))
    application.add_handler(CallbackQueryHandler(show_reminders_menu, pattern="^show_reminders_menu$"))
    application.add_handler(CallbackQueryHandler(show_marriage_menu, pattern="^show_marriage_menu$"))
    application.add_handler(CallbackQueryHandler(show_chat_settings_help, pattern="^show_chat_settings_help$"))
    application.add_handler(CallbackQueryHandler(show_help_page, pattern="^show_help_page$"))
    application.add_handler(CallbackQueryHandler(show_communication_short_guide, pattern="^show_communication_short_guide$"))
    application.add_handler(CallbackQueryHandler(show_communication_full_guide, pattern="^show_communication_full_guide$"))
    # Меню Комунікації
    application.add_handler(CallbackQueryHandler(show_communication_short_guide, pattern="^show_communication_short_guide$"))
    application.add_handler(CallbackQueryHandler(show_communication_full_guide, pattern="^show_communication_full_guide$"))

    # Детальний гайд по Мемчикам (окремий, щоб не конфліктувати з гідом спілкування)
    application.add_handler(CallbackQueryHandler(show_mems_full_guide, pattern="^show_mems_full_guide$"))
    
    # Меню Про Бота
    application.add_handler(CallbackQueryHandler(about_bot_command, pattern="^about_bot_info$"))

    # --- Меню Ігор (Інформаційні) ---
    application.add_handler(CallbackQueryHandler(show_tic_tac_toe_menu, pattern="^show_tic_tac_toe_menu$"))
    application.add_handler(CallbackQueryHandler(show_mems_games_menu, pattern="^mems_games_menu$"))
    application.add_handler(CallbackQueryHandler(show_mini_games_menu, pattern="^show_mini_games_menu$"))
    application.add_handler(CallbackQueryHandler(show_casino_menu, pattern="^show_casino_menu$"))

    logger.info("Обробники Start/Help (start_help_handlers.py) завантажено. 🐾")