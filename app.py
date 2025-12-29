# main.py — Точка входу для Telegram-бота "Котик"
"""
Головний модуль бота "Котик".
Мета: мінімалізм, стиль, українська мова та зручність.
"""
import logging
import datetime
import os
import html
import traceback
import pytz

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    MessageHandler,
    filters,
    ContextTypes,
    PicklePersistence,
    ApplicationBuilder
)

# === Імпорти модулів ===
# (Перевіряємо, чи є config, інакше беремо з utils або з environment)
try:
    from config import TELEGRAM_BOT_TOKEN, OWNER_ID
except Exception:
    try:
        from bot.utils.utils import TELEGRAM_BOT_TOKEN, OWNER_ID
    except Exception:
        # Якщо немає ні config, ні utils — беремо з змінних оточення
        TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        owner_env = os.environ.get("OWNER_ID")
        try:
            OWNER_ID = int(owner_env) if owner_env is not None else None
        except (ValueError, TypeError):
            OWNER_ID = None

from bot.core.database import (
    init_db,
    upsert_chat_info,
    ensure_user_data,
)

# Імпорт обробників (Handlers)
from bot.handlers.start_help_handlers import register_start_help_handlers
from bot.handlers.system_handlers import register_system_handlers
from bot.handlers.game_handlers import register_game_handlers
from bot.handlers.admin_handlers import register_admin_handlers, secret_admin_trigger
from bot.handlers.command_handlers import register_command_handlers
from bot.handlers.profile_handlers import register_profile_handlers
from bot.handlers.unified_stop_handlers import register_unified_stop_handlers
from bot.handlers.ai_handlers import register_ai_handlers
from bot.handlers.games_menu_handlers import register_games_menu_handlers
from bot.handlers.tops_menu_handlers import register_tops_menu_handlers
from bot.games.tic_tac_toe_game import register_tic_tac_toe_handlers
from bot.games.mandarin_duel_game import register_mandarin_duel_handlers
from bot.games.mems_integration import register_mems_handlers
# Нагадування (з функцією відновлення)
from bot.handlers.reminder_handlers import register_reminder_handlers, load_persistent_reminders
from bot.features.marriage.marriage_handlers import register_marriage_handlers
from bot.handlers.casino_handlers import register_casino_handlers, initialize_casino
from bot.core.daily_tasks import nun_of_the_day_job, assign_daily_predictions_job
from bot.features.weather.weather_handlers import register_weather_handlers

# Адмін-керування та події
from bot.handlers.chat_admin_handlers import register_chat_admin_handlers, handle_admin_text_input
from bot.handlers.chat_event_handlers import register_chat_event_handlers

# === Налаштування логування ===
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", 
    level=logging.INFO
)
# Приглушуємо занадто балакучі бібліотеки
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("dateparser").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)


async def post_init(application: Application):
    """
    Виконується один раз після запуску бота.
    Ініціалізує базу даних та відновлює нагадування.
    """
    logger.info("⚙️ Виконується post_init...")

    # 0. Кешуємо дані бота (ID/username) один раз на старті.
    # Це критично для стабільного визначення reply-to-bot у групах.
    try:
        me = await application.bot.get_me()
        application.bot_data["bot_id"] = me.id
        application.bot_data["bot_username"] = (me.username or "").lower()
        logger.info("🤖 Bot cache: id=%s username=@%s", me.id, me.username)
    except Exception as e:
        logger.warning(f"⚠️ Не вдалося закешувати дані бота: {e}")
    
    # 1. Ініціалізація БД
    await init_db()
    logger.info("✅ База даних ініціалізована.")

    # 2. Ініціалізація казино
    try:
        await initialize_casino()
        logger.info("🎰 Казино ініціалізовано.")
    except Exception as e:
        logger.warning(f"⚠️ Не вдалося ініціалізувати казино: {e}")

    # 3. Відновлення нагадувань
    logger.info("🔄 Відновлення нагадувань...")
    await load_persistent_reminders(application)

    # 4. Безпечна очистка завислих ігор, що можуть зберігатися через PicklePersistence
    try:
        from bot.games.mandarin_duel_game import cleanup_mandarin_duels_after_restart

        await cleanup_mandarin_duels_after_restart(application)
    except Exception as e:
        logger.warning(f"⚠️ Не вдалося очистити мандаринкові дуелі після рестарту: {e}")


async def update_chat_and_user_info(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Фоновий запис інформації про користувачів та чати в БД.
    """
    user = update.effective_user
    chat = update.effective_chat

    if user:
        await ensure_user_data(
            user_id=user.id,
            username=user.username,
            first_name=user.first_name,
            last_name=user.last_name,
            update_names=True,
        )

    if chat:
        await upsert_chat_info(
            chat_id=chat.id,
            chat_type=chat.type,
            chat_title=chat.title,
            chat_username=chat.username,
        )


async def handle_bot_join(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Привітання, коли бота додають у новий чат."""
    if not update.message or not update.message.new_chat_members:
        return

    chat = update.effective_chat
    for member in update.message.new_chat_members:
        if member.id == context.bot.id:
            logger.info(f"Бота додали до чату: {chat.title} ({chat.id})")
            await context.bot.send_message(
                chat.id,
                "Мур — я тут! 🐾\n"
                "Напишіть /start, щоб почати.\n\n"
                "Адміністратори можуть налаштувати мене командою /settings.",
                parse_mode=ParseMode.HTML,
            )
            break


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Логування критичних помилок та сповіщення власника."""
    logger.error("Exception while handling an update:", exc_info=context.error)
    
    # Формуємо трасування стека
    tb_list = traceback.format_exception(None, context.error, context.error.__traceback__)
    tb_string = "".join(tb_list)
    
    # Сповіщення в консоль
    print(f"🔥 EXCEPTION: {tb_string}")

    # Сповіщення власнику, якщо він заданий
    if OWNER_ID:
        try:
            err_txt = html.escape(str(context.error))
            if len(err_txt) > 1500:
                err_txt = err_txt[:1500] + "… (обрізано)"
            error_message = f"🔥 <b>Критична помилка!</b>\n<pre>{err_txt}</pre>"
            await context.bot.send_message(
                chat_id=OWNER_ID, 
                text=error_message, 
                parse_mode=ParseMode.HTML
            )
        except Exception as e:
            logger.error(f"Не вдалося надіслати звіт про помилку власнику: {e}")


def main() -> None:
    """Основна функція запуску."""
    print("🚀 Запускаю Котика... (Натисніть Ctrl+C для зупинки)")

    if not TELEGRAM_BOT_TOKEN:
        logger.critical("❌ TELEGRAM_BOT_TOKEN не знайдено! Перевірте config.py або змінні середовища.")
        return

    # === ВИПРАВЛЕННЯ: Налаштування Persistence для старої версії PTB ===
    # Замінюємо 'directory' на 'filepath', щоб бути сумісним зі старими версіями
    # python-telegram-bot (до v20.0). Вказуємо шлях до одного з файлів (bot_state.pkl).
    # Бібліотека автоматично визначить, що це шлях до файлів persistence.
    persistence_dir = "data"
    persistence_filepath = os.path.join(persistence_dir, "bot_data.pkl")
    
    # Забезпечуємо існування директорії 'data'
    if not os.path.exists(persistence_dir):
        try:
            os.makedirs(persistence_dir, exist_ok=True)
            logger.info(f"📁 Створено директорію для пам'яті: {persistence_dir}")
        except OSError as e:
            logger.error(f"❌ Не вдалося створити директорію {persistence_dir}: {e}")
            return
            
    # Створюємо об'єкт persistence, використовуючи filepath
    persistence = PicklePersistence(filepath=persistence_filepath)

    application = (
        ApplicationBuilder()
        .token(TELEGRAM_BOT_TOKEN)
        .post_init(post_init)
        .persistence(persistence)
        .build()
    )

    # === РЕЄСТРАЦІЯ ОБРОБНИКІВ ===
    
    # 1. Системні (найвищий пріоритет)
    register_system_handlers(application)
    application.add_error_handler(error_handler)
    application.add_handler(MessageHandler(filters.ALL, update_chat_and_user_info), group=10)

    # 2. Секретні
    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE & filters.Regex(r"^Адмін-панель котика$"),
            secret_admin_trigger,
        ),
        group=1,
    )
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, handle_admin_text_input),
        group=2
    )

    # 3. Модулі (Функціонал)
    register_start_help_handlers(application)
    register_games_menu_handlers(application)
    register_tops_menu_handlers(application)
    register_game_handlers(application)
    register_admin_handlers(application)
    register_command_handlers(application)
    register_profile_handlers(application)

    # Єдиний /stop для всіх ігор (має йти ДО реєстрації конкретних ігор,
    # щоб перехопити /stop та ...)
    register_unified_stop_handlers(application)

    register_tic_tac_toe_handlers(application)
    register_mandarin_duel_handlers(application)
    register_mems_handlers(application)
    register_weather_handlers(application)
    register_ai_handlers(application)        # Ваш AI модуль
    register_reminder_handlers(application)  # Ваш модуль нагадувань
    register_marriage_handlers(application)
    register_casino_handlers(application)
    register_chat_admin_handlers(application)
    register_chat_event_handlers(application)

    # 4. Події чату
    application.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, handle_bot_join))

    # === ПЛАНУВАЛЬНИК (JobQueue) ===
    job_queue = application.job_queue
    utc_timezone = pytz.utc
    
    # Монашка дня (05:00 UTC)
    job_queue.run_daily(
        nun_of_the_day_job,
        time=datetime.time(hour=5, minute=0, second=0, tzinfo=utc_timezone),
        name="nun_of_the_day_job",
    )
    
    # Передбачення дня (21:01 UTC)
    job_queue.run_daily(
        assign_daily_predictions_job,
        time=datetime.time(hour=21, minute=1, second=0, tzinfo=utc_timezone),
        name="assign_daily_predictions_job",
    )

    logger.info("✅ Бот ініціалізований і готовий до роботи.")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("🛑 Бот зупинений.")
    except Exception as e:
        logger.critical(f"🔥 Критична помилка при запуску: {e}", exc_info=True)