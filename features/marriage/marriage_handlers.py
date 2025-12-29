# marriage_handlers.py
"""
Обробники для системи шлюбів.

Стиль: † Котячі Монашки 🌿
(Цей файл вже був у чудовому стані, тому змін мінімум)
"""
import logging
import html
import re
from datetime import datetime
from typing import Optional

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    User,
    Chat,
)
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    CallbackQueryHandler,
    filters,
    MessageHandler,
)
from telegram.constants import ParseMode
from telegram.error import BadRequest

# Припускаємо, що ці функції існують у вашому модулі database.py
import bot.core.database as database
# Додаємо імпорт функції перевірки
from bot.handlers.chat_admin_handlers import is_chat_module_enabled
# (НОВЕ) Імпортуємо динамічну функцію для отримання вартості одруження
from bot.utils.utils import get_marriage_cost, get_user_addressing

logger = logging.getLogger(__name__)

# --- Налаштування Модуля ---
MARRIAGE_COST = 420  # (на м'ятку 🌿) - ЗАСТАРІЛО, користуйте get_marriage_cost()
PROPOSAL_TIMEOUT_SECONDS = 3600  # 1 година на роздуми

# (НОВЕ) Функція для отримання актуальної вартості одруження
async def get_current_marriage_cost() -> int:
    """Отримує актуальну вартість одруження для поточної теми."""
    return await get_marriage_cost()

# --- Котячо-Монанські Тексти (Благословенні 🐾) ---
# (Тексти залишені без змін, вони ідеальні 😽)
MSG_PROPOSE_SENDER = (
    "<b>† Святі котики †</b>\n\n"
    "<b>{}</b> простягає лапку і повну миску валеріанки. 🌿\n\n"
    "Союз із <b>{}</b> коштуватиме <b>{} м'яток</b> (нашій церкві потрібна нова кігтеточка).\n\n"
    "Є 1 година, щоб прийняти цю пропозицію, поки ми не пішли спати. 😴"
)
MSG_PROPOSE_SUCCESS = "Мяу! Повідомлення для <b>{}</b> надіслано. 🕊️ Чекаємо на відповідь."
MSG_ALREADY_MARRIED = "Мур-мур... <b>{}</b>, ви вже пов'язані священними узами (або просто міцно спите в одному кошику) з кимось іншим. 😽 Наша котяча церква не схвалює полігамію. Спочатку /розлучення, грішику!"
MSG_SELF_MARRIAGE = "Мяу? Любити себе — це, звісно, святе діло, але навіть наша розпусна монашка-скарбнича не додумалася одружитися сама з собою. 😹 Знайдіть собі іншу грішну душу для пари!"
MSG_BOT_MARRIAGE = "Мяу! 😽 Я, звісно, божественне створіння, але моє серце належить... ну, м'ятці. 🌿 Боти не можуть одружуватися. Ми тут, щоб спостерігати за вашими грішками і муркотіти."
MSG_NO_MONEY = (
    "Ой, мяу... 😿\n"
    "Щоб приєднатися до нашого святого (і трішки розпусного) ордену, потрібно <b>{} м'яток</b> на пожертви. У вашій мисочці лише <b>{}</b>.\n"
    "Йдіть, полюйте, і не повертайтеся з порожніми лапками! 🐾"
)
MSG_PROPOSAL_EXPIRED = "Мяу... ⏳ Здається, хтось занадто довго спав на сонечку. Час на роздуми вийшов, пропозиція більше недійсна!"
MSG_NOT_YOUR_PROPOSAL = "Мурк! Це не ваша миска з валеріанкою! Не пхайте свого цікавого носика! 😼"
MSG_ACCEPT_SUCCESS = (
    "🎉 <b>† АЛЕЛУЯ, МЯУ! †</b> 🎉\n\n"
    "Відтепер <b>{}</b> та <b>{}</b> офіційно поєднані узами святої валеріанки! 🌿\n"
    "Ви можете офіційно обмінятися... поглядами. Або подряпати диван. 🐱❤️🐱\n\n"
    "<i>Ідіть, і грішіть (але не дуже голосно).</i>"
)
MSG_DECLINE_SUCCESS = "😿 Сумний мяу... Пропозицію відхилено. 💔\nБільше м'ятки залишиться для вас!"
MSG_NO_MARRIAGE = "Мяу? Ви — вільний котик, що гуляє сам по собі. 🐾 Ваші лапки ще не пов'язані священними узами. Хочете знайти собі грішну пару? /propose"
MSG_DIVORCE_PROMPT = "Мяу... <b>{}</b>, ви впевнені, що хочете розірвати ваш священний союз із <b>{}</b>? 😿 Подушки вже поділили? А миску з м'яткою? Це серйозне рішення. Ви точно-точно впевнені?"
MSG_DIVORCE_SUCCESS = "Мур... Ви офіційно розбіглися. 💔 Свобода! (Чи ні?). Тепер ви знову вільний котик, що гуляє сам по собі. Можете йти грішити з кимось новим."
MSG_DIVORCE_CANCEL = "Мур! 💖 Хух, це було близько! Чудово, що ви передумали. Залишайтеся разом і муркотіть... або грішіть. На ваш вибір. 😽"
MSG_TARGET_NOT_FOUND = "Мяу! Вкажіть @username або дайте відповідь на повідомлення."
MSG_TARGET_GROUP = "Мяу... Здається, {} - це не котик, а ціла група! 😿"
MSG_TARGET_DB_NOT_FOUND = "Мяу... Я не можу знайти котика з ніком @{}. 😿"
MSG_TARGET_API_ERROR = "Мяу... Не можу зв'язатися з @{}. 😿"

# === Допоміжні функції ===

def get_user_mention(user: User | Chat) -> str:
    """
    Повертає форматований HTML-тег для користувача або чату.
    """
    full_name = html.escape(user.full_name if hasattr(user, 'full_name') else user.title)
    return f'<a href="tg://user?id={user.id}">{full_name}</a>'


async def get_target_user(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> Optional[User | Chat]:
    """
    Визначає користувача, до якого звертаються (через @username або reply).
    Повертає об'єкт User або Chat.
    """
    target_user: Optional[User | Chat] = None

    if update.message.reply_to_message:
        target_user = update.message.reply_to_message.from_user
    elif context.args:
        username = context.args[0].replace("@", "")
        if not username:
            await update.message.reply_text(MSG_TARGET_NOT_FOUND)
            return None

        # Шукаємо користувача в базі (найнадійніший спосіб)
        user_data = await database.get_user_by_username(username)
        if not user_data:
            await update.message.reply_text(MSG_TARGET_DB_NOT_FOUND.format(username))
            return None

        target_id = user_data["user_id"]

        try:
            # get_chat() - найнадійніший спосіб отримати об'єкт за ID
            chat_obj = await context.bot.get_chat(target_id)

            if target_id == context.bot.id:
                target_user = await context.bot.get_me()
            elif chat_obj.type == 'private':
                target_user = chat_obj  # Це користувач
            else:
                await update.message.reply_text(MSG_TARGET_GROUP.format(f"@{username}"))
                return None
        except BadRequest as e:
            logger.error(f"Не вдалося отримати chat_obj для @{username} (ID: {target_id}): {e}")
            await update.message.reply_text(MSG_TARGET_API_ERROR.format(username))
            return None
    else:
        await update.message.reply_text(MSG_TARGET_NOT_FOUND)
        return None

    return target_user


async def send_marriage_certificate(
    update: Update, context: ContextTypes.DEFAULT_TYPE, user1_id: int, user2_id: int
):
    """Надсилає стильне, мінімалістичне свідоцтво про шлюб."""
    marriage = await database.get_marriage_by_user_id(user1_id)
    if not marriage:
        logger.warning(f"Не знайдено шлюб для {user1_id} при відправці свідоцтва")
        return

    try:
        user1 = await context.bot.get_chat(user1_id)
        user2 = await context.bot.get_chat(user2_id)
    except BadRequest as e:
        logger.error(f"Не вдалося отримати інфо про {user1_id} або {user2_id}: {e}")
        await update.effective_message.reply_text("Мяу... Не можу отримати інформацію про пару, але вітаю!")
        return

    user1_mention = get_user_mention(user1)
    user2_mention = get_user_mention(user2)

    try:
        marriage_date = datetime.fromisoformat(marriage["marriage_date"]).strftime("%d.%m.%Y")
    except (ValueError, TypeError):
        marriage_date = "невідомо"
        logger.warning(f"Невірний формат дати шлюбу: {marriage.get('marriage_date')}")

    # Мінімалістичний дизайн "Котячої Церкви"
    caption = (
        f"<b>† СВЯЩЕННИЙ СОЮЗ †</b>\n\n"
        f"▷ {user1_mention}\n"
        f"▷ {user2_mention}\n\n"
        f"Поєднали свої лапки та серця у вічному муркотінні.\n"
        f"<i>Дата: {marriage_date}</i>\n"
        f"<i>Благословення: 🌿 </i>"
    )

    # Використовуємо effective_message для роботи і в /marriage, і в callback
    await update.effective_message.reply_html(caption)

# === Обробники Команд ===

async def propose_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обробляє команду /propose."""
    from_user = update.effective_user
    chat = update.effective_chat
    
    if not from_user or not chat:
        return

    # === ПЕРЕВІРКА ПРАВ МОДУЛЯ ===
    # Ключ 'marriage_enabled' відповідає ключу "marriage" в MODULES_CONFIG
    if not await is_chat_module_enabled(chat, "marriage_enabled"):
        logger.debug(f"Модуль 'marriage' вимкнено для чату {chat.id}. /propose ігнорується.")
        return
    # =============================

    target_user = await get_target_user(update, context)
    if not target_user:
        return

    # --- Перевірки ---
    if target_user.id == from_user.id:
        await update.message.reply_text(MSG_SELF_MARRIAGE)
        return

    if hasattr(target_user, 'is_bot') and target_user.is_bot:
        await update.message.reply_text(MSG_BOT_MARRIAGE)
        return

    from_user_marriage = await database.get_marriage_by_user_id(from_user.id)
    if from_user_marriage:
        await update.message.reply_html(MSG_ALREADY_MARRIED.format(get_user_mention(from_user)))
        return

    target_user_marriage = await database.get_marriage_by_user_id(target_user.id)
    if target_user_marriage:
        await update.message.reply_html(MSG_ALREADY_MARRIED.format(get_user_mention(target_user)))
        return

    from_user_balance = await database.get_user_balance(from_user.id)
    if from_user_balance < MARRIAGE_COST:
        await update.message.reply_html(MSG_NO_MONEY.format(MARRIAGE_COST, from_user_balance))
        return

    # --- Створення пропозиції ---
    # (Зберігається в chat_data - зникне при перезапуску, але це мінімалістично)
    proposal_id = f"proposal_{from_user.id}_{target_user.id}"
    context.chat_data[proposal_id] = {
        "from_id": from_user.id,
        "to_id": target_user.id,
        "timestamp": datetime.now().timestamp(),
        "message_id": None,  # Оновимо після відправки
    }

    keyboard = [
        [
            InlineKeyboardButton("✅ Прийняти", callback_data=f"marriage:accept:{proposal_id}"),
            InlineKeyboardButton("❌ Відхилити", callback_data=f"marriage:decline:{proposal_id}"),
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    # Повідомляємо того, хто робить пропозицію
    await update.message.reply_html(MSG_PROPOSE_SUCCESS.format(get_user_mention(target_user)))

    # Надсилаємо пропозицію в чат
    sent_message = await context.bot.send_message(
        chat.id,
        text=MSG_PROPOSE_SENDER.format(
            get_user_mention(from_user),
            get_user_mention(target_user),
            MARRIAGE_COST,
        ),
        reply_markup=reply_markup,
        parse_mode=ParseMode.HTML,
    )
    # Зберігаємо ID повідомлення для редагування
    context.chat_data[proposal_id]["message_id"] = sent_message.message_id


async def marriage_button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обробляє натискання кнопок 'Прийняти'/'Відхилити'."""
    query = update.callback_query
    await query.answer()

    user_who_clicked = query.from_user

    # === ПЕРЕВІРКА ПРАВ МОДУЛЯ ===
    # Перевіряємо, чи модуль досі ввімкнено в чаті, де була створена пропозиція
    if query.message and query.message.chat:
        if not await is_chat_module_enabled(query.message.chat, "marriage_enabled"):
            logger.debug(f"Модуль 'marriage' вимкнено для чату {query.message.chat.id}. Кнопка ігнорується.")
            await query.answer("Мяу... Адміністратор вимкнув шлюби в цьому чаті. 😿", show_alert=True)
            try:
                # Видаляємо кнопки, щоб не плутати
                await query.edit_message_reply_markup(reply_markup=None)
            except Exception as e:
                logger.debug(f"Не вдалося видалити кнопки шлюбу: {e}")
            return
    # =============================

    try:
        prefix, action, proposal_id = query.data.split(":")
    except (ValueError, AttributeError):
        logger.warning(f"Невірний формат marriage callback: {query.data}")
        await query.edit_message_text("Мяу! Сталася дивна помилка з кнопкою. 😿")
        return

    proposal_data = context.chat_data.get(proposal_id)

    if not proposal_data:
        await query.edit_message_text(MSG_PROPOSAL_EXPIRED, reply_markup=None)
        return

    to_id = proposal_data["to_id"]
    from_id = proposal_data["from_id"]
    timestamp = proposal_data["timestamp"]

    # Перевірка, що час не вийшов
    if (datetime.now().timestamp() - timestamp) > PROPOSAL_TIMEOUT_SECONDS:
        await query.edit_message_text(MSG_PROPOSAL_EXPIRED, reply_markup=None)
        if proposal_id in context.chat_data:
            del context.chat_data[proposal_id]
        return

    # Перевірка, що натиснув той, кому пропозиція
    if user_who_clicked.id != to_id:
        await query.answer(MSG_NOT_YOUR_PROPOSAL, show_alert=True)
        return

    # Видаляємо пропозицію, щоб уникнути повторних натискань
    if proposal_id in context.chat_data:
        del context.chat_data[proposal_id]

    try:
        proposer = await context.bot.get_chat(from_id)
        target = await context.bot.get_chat(to_id)
    except BadRequest:
        await query.edit_message_text("Мяу... Не можу знайти одного з котиків. 😿", reply_markup=None)
        return

    if action == "accept":
        # === ПРИЙНЯТИ ===
        
        # Повторна "атомарна" перевірка (на випадок, якщо щось змінилося)
        from_user_balance = await database.get_user_balance(from_id)
        if from_user_balance < MARRIAGE_COST:
            await query.edit_message_text(
                MSG_NO_MONEY.format(MARRIAGE_COST, from_user_balance),
                parse_mode=ParseMode.HTML,
                reply_markup=None
            )
            return

        from_user_marriage = await database.get_marriage_by_user_id(from_id)
        target_user_marriage = await database.get_marriage_by_user_id(to_id)

        if from_user_marriage or target_user_marriage:
            user_mention = get_user_mention(proposer if from_user_marriage else target)
            await query.edit_message_text(
                MSG_ALREADY_MARRIED.format(user_mention),
                parse_mode=ParseMode.HTML,
                reply_markup=None
            )
            return

        # Все добре! Одружуємо!
        try:
            # 1. Зняти гроші
            await database.update_user_balance(from_id, -MARRIAGE_COST)

            # 2. Створити запис в БД (використовуємо UTC)
            marriage_date_str = datetime.utcnow().isoformat() + "+00:00"
            await database.create_marriage(from_id, to_id, marriage_date_str)

            # 3. Редагувати повідомлення
            await query.edit_message_text(
                MSG_ACCEPT_SUCCESS.format(get_user_mention(proposer), get_user_mention(target)),
                parse_mode=ParseMode.HTML,
                reply_markup=None
            )

            # 4. Надіслати свідоцтво
            await send_marriage_certificate(update, context, from_id, to_id)

        except Exception as e:
            logger.error(f"Помилка під час фіналізації шлюбу {from_id}-{to_id}: {e}", exc_info=True)
            # Повертаємо гроші, якщо щось пішло не так
            await database.update_user_balance(from_id, MARRIAGE_COST)
            await query.edit_message_text("Мяу... Сталася помилка у монастирі! 😿 Гроші повернуто.")

    elif action == "decline":
        # === ВІДХИЛИТИ ===
        ctx_target = await get_user_addressing(target.id)
        await query.edit_message_text(
            f"😿 Сумний мяу... {ctx_target.past('Відхилив', 'Відхилила', 'Відхилив')} пропозицію. 💔\nБільше м'ятки залишиться для вас!",
            parse_mode=ParseMode.HTML,
            reply_markup=None
        )


async def marriage_info_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обробляє команду /marriage (показує свідоцтво)."""
    user = update.effective_user
    if not user:
        return

    # === ПЕРЕВІРКА ПРАВ МОДУЛЯ ===
    if not await is_chat_module_enabled(update.effective_chat, "marriage_enabled"):
        logger.debug(f"Модуль 'marriage' вимкнено для чату {update.effective_chat.id}. /marriage ігнорується.")
        return
    # =============================

    marriage = await database.get_marriage_by_user_id(user.id)

    if not marriage:
        await update.message.reply_text(MSG_NO_MARRIAGE)
        return

    user1_id = marriage["user1_id"]
    user2_id = marriage["user2_id"]

    await send_marriage_certificate(update, context, user1_id, user2_id)


async def divorce_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обробляє команду /divorce (показує підтвердження)."""
    user = update.effective_user
    if not user:
        return

    # === ПЕРЕВІРКА ПРАВ МОДУЛЯ ===
    if not await is_chat_module_enabled(update.effective_chat, "marriage_enabled"):
        logger.debug(f"Модуль 'marriage' вимкнено для чату {update.effective_chat.id}. /divorce ігнорується.")
        return
    # =============================

    marriage = await database.get_marriage_by_user_id(user.id)

    if not marriage:
        await update.message.reply_text(MSG_NO_MARRIAGE)
        return

    partner_id = marriage["user2_id"] if marriage["user1_id"] == user.id else marriage["user1_id"]
    
    try:
        partner = await context.bot.get_chat(partner_id)
        partner_name = get_user_mention(partner)
    except BadRequest:
        partner_name = f"котиком з ID {partner_id}"

    keyboard = [
        [
            InlineKeyboardButton("Так, розірвати ці пута 😿", callback_data="divorce:confirm"),
            InlineKeyboardButton("Ні, лишаємо все як є", callback_data="divorce:cancel"),
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_html(
        MSG_DIVORCE_PROMPT.format(get_user_mention(user), partner_name),
        reply_markup=reply_markup
    )


async def divorce_button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обробляє кнопки підтвердження розлучення."""
    query = update.callback_query
    user = query.from_user

    # === ПЕРЕВІРКА ПРАВ МОДУЛЯ ===
    if query.message and query.message.chat:
        if not await is_chat_module_enabled(query.message.chat, "marriage_enabled"):
            logger.debug(f"Модуль 'marriage' вимкнено для чату {query.message.chat.id}. Кнопка розлучення ігнорується.")
            await query.answer("Мяу... Адміністратор вимкнув шлюби в цьому чаті. 😿", show_alert=True)
            try:
                await query.edit_message_reply_markup(reply_markup=None)
            except Exception as e:
                logger.debug(f"Не вдалося видалити кнопки розлучення: {e}")
            return
    # =============================

    try:
        prefix, action = query.data.split(":")  # divorce:confirm або divorce:cancel
    except (ValueError, AttributeError):
        logger.warning(f"Невірний формат divorce callback: {query.data}")
        await query.edit_message_text("Мяу! Сталася дивна помилка з кнопкою. 😿")
        return

    marriage = await database.get_marriage_by_user_id(user.id)
    if not marriage:
        await query.edit_message_text(MSG_NO_MARRIAGE, reply_markup=None)
        return

    if action == "confirm":
        # === ПІДТВERДИТИ РОЗЛУЧЕННЯ ===
        partner_id = marriage["user2_id"] if marriage["user1_id"] == user.id else marriage["user1_id"]
        
        try:
            await database.delete_marriage_by_user_id(user.id)
            await query.edit_message_text(MSG_DIVORCE_SUCCESS, reply_markup=None)
            logger.info(f"Розлучення: {user.id} та {partner_id}")
        except Exception as e:
            logger.error(f"Помилка під час видалення шлюбу {user.id}: {e}", exc_info=True)
            await query.edit_message_text("Мяу... не вдалося розлучитися. Спробуйте ще раз. 😿", reply_markup=None)
            return

        # Сповіщаємо партнера (в приватному чаті)
        try:
            user_mention = get_user_mention(user)
            ctx_initiator = await get_user_addressing(user.id)
            await context.bot.send_message(
                chat_id=partner_id,
                text=f"Мяу... {user_mention} {ctx_initiator.past('розірвав', 'розірвала', 'розірвав')} союз з вами. 💔",
                parse_mode=ParseMode.HTML,
            )
        except Exception as e:
            logger.info(f"Не вдалося сповістити {partner_id} про розлучення: {e}")

    elif action == "cancel":
        # === СКАСУВАТИ ===
        await query.edit_message_text(MSG_DIVORCE_CANCEL, reply_markup=None)

# === Обгортки для обробки context.args з MessageHandler ===

def _extract_args_from_message(message_text: str) -> list[str]:
    """Допоміжна функція для парсингу аргументів з тексту (для команд без /)."""
    parts = message_text.strip().split(maxsplit=1)
    return parts[1].split() if len(parts) > 1 else []


async def propose_command_wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обгортка для 'пропозиція' (без /), щоб заповнити context.args."""
    if update.message and update.message.text:
        context.args = _extract_args_from_message(update.message.text)
    # Перевірка прав відбудеться всередині propose_command
    await propose_command(update, context)

# === Реєстрація обробників ===

def register_marriage_handlers(application: Application):
    """(Назву відновлено) Реєструє всі обробники, пов'язані зі шлюбами."""
    logger.info("Реєстрація обробників шлюбів (у стилі 'М'ятні Монашки' 🐾)...")

    # 1. Команди з / (латиницею)
    application.add_handler(CommandHandler("propose", propose_command, filters=filters.ChatType.GROUPS))
    application.add_handler(CommandHandler("marriage", marriage_info_command))
    application.add_handler(CommandHandler("divorce", divorce_command))

    # 2. Команди кирилицею БЕЗ / (через MessageHandler + Regex)
    common_text_filter = filters.TEXT & ~filters.COMMAND & ~filters.UpdateType.EDITED

    application.add_handler(
        MessageHandler(
            filters.ChatType.GROUPS
            & common_text_filter
            & filters.Regex(r"(?i)^одружитися\s+.+"),  # 'пропозиція' + пробіл + аргументи
            propose_command_wrapper,
        )
    )
    application.add_handler(
        MessageHandler(
            common_text_filter
            & filters.Regex(r"(?i)^шлюб$"),  # Тільки 'шлюб'
            marriage_info_command,
        )
    )
    application.add_handler(
        MessageHandler(
            common_text_filter
            & filters.Regex(r"(?i)^розлучитися$"),  # Тільки 'розлучення'
            divorce_command,
        )
    )

    # 3. Обробники кнопок
    application.add_handler(CallbackQueryHandler(marriage_button_callback, pattern=r"^marriage:"))
    application.add_handler(CallbackQueryHandler(divorce_button_callback, pattern=r"^divorce:"))