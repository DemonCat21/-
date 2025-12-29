# casino_handlers.py
# -*- coding: utf-8 -*-
"""
Модуль азартних спокус. 🎰
Керує Мур-Казино, ставками та балансом м'яти.
"""

import logging
import random
from datetime import datetime, timedelta

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters, # (НОВЕ) Додано для фільтрів
)

from bot.core.database import get_user_balance, update_user_balance
from bot.utils.utils import mention, get_casino_slots, get_casino_multipliers
from bot.handlers.chat_admin_handlers import is_chat_module_enabled # (ДОБРЕ) Вже було

logger = logging.getLogger(__name__)

# Налаштування казино
MIN_BET = 10
MAX_BET = 100000
COOLDOWN_SECONDS = 2  # Cooldown між іграми

async def delete_message_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Job для видалення повідомлення казино через 3 хвилини.
    """
    data = context.job.data
    chat_id = data["chat_id"]
    message_id = data["message_id"]
    try:
        await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception as e:
        logger.debug(f"Не вдалося видалити повідомлення {message_id} у {chat_id}: {e}")

# "Слоти" та їх "вага" (шанс випадіння)
# 🐾 (Кіт), 🌿 (М'ята), 🐟 (Риба), ✝️ (Хрест/Монашка - Джекпот)
SLOTS = [
    ("🐾", 8),  # Котик (звичайний)
    ("🌿", 7),   # М'ятка (звичайний)
    ("🐟", 5),   # Рибка (рідкісний)
    ("✝️", 3),   # Монашка (джекпот)
]

# Виграшні комбінації та їх множники (ставка x Множник)
WIN_MULTIPLIERS = {
    # Три в ряд
    ("✝️", "✝️", "✝️"): 25,  # Джекпот!
    ("🐟", "🐟", "🐟"): 10,  # Велика риба
    ("🌿", "🌿", "🌿"): 5,   # М'ятний рай
    ("🐾", "🐾", "🐾"): 3,   # Мур-комбо

    # Два в ряд (втішний приз - повернення ставки)
    # (ВИПРАВЛЕНО) Додано комбінації для calculate_winnings
    ("✝️", "✝️"): 1,
    ("🐟", "🐟"): 1,
    ("🌿", "🌿"): 1,
    ("🐾", "🐾"): 1,
}
# Розпаковуємо слоти та ваги для random.choices
SLOT_ITEMS, SLOT_WEIGHTS = zip(*SLOTS)


# (НОВЕ) Функція для динамічного оновлення констант казино
async def initialize_casino() -> None:
    """
    Ініціалізує слоти та множники для поточної теми.
    Оновлює глобальні константи для казино згідно з темою.
    """
    global SLOTS, WIN_MULTIPLIERS, SLOT_ITEMS, SLOT_WEIGHTS
    try:
        SLOTS = await get_casino_slots()
        WIN_MULTIPLIERS = await get_casino_multipliers()
        if SLOTS:
            SLOT_ITEMS, SLOT_WEIGHTS = zip(*SLOTS)
        logger.info("Casino constants updated for current theme.")
    except Exception as e:
        logger.warning(f"Failed to update casino constants: {e}. Using defaults.")


def get_spin() -> tuple[str, str, str]:
    """
    Генерує три символи для слотів.
    """
    return tuple(random.choices(SLOT_ITEMS, weights=SLOT_WEIGHTS, k=3))


def calculate_winnings(bet: int, spin: tuple[str, str, str]) -> int:
    """
    Розраховує виграш на основі комбінації.
    """
    # Перевірка на три в ряд
    if spin in WIN_MULTIPLIERS:
        return bet * WIN_MULTIPLIERS[spin]

    # (ВИПРАВЛЕНО) Більш чиста перевірка "двійок".
    # Враховуємо зліва, справа та по краях.
    pairs = [spin[:2], spin[1:], (spin[0], spin[2])]
    for pair in pairs:
        if pair in WIN_MULTIPLIERS:
            return bet * WIN_MULTIPLIERS[pair] # Повертаємо ставку

    return 0


async def casino_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Обробляє гру в казино. Перевіряє права, cooldown, баланс, обробляє ставку та надсилає результат.
    """
    user = update.effective_user
    chat = update.effective_chat
    if not user or not chat:
        return

    # --- ПЕРЕВІРКА ПРАВ ---
    if not await is_chat_module_enabled(chat, "games"):
        logger.debug(f"Module 'games' (casino) disabled for chat {chat.id}.")
        return
    # --- КІНЕЦЬ ПЕРЕВІРКИ ---

    # 1. Перевірка Cooldown
    now = datetime.now()
    last_played_key = f"casino_last_played_{user.id}"
    last_played_time = context.user_data.get(last_played_key)

    if last_played_time:
        time_diff = now - last_played_time
        if time_diff.total_seconds() < COOLDOWN_SECONDS:
            remaining = COOLDOWN_SECONDS - time_diff.total_seconds()
            await update.message.reply_text(
                f"✙ Зачекай, котику... М'ята ще не виросла. ✙\n(Залишилось {remaining:.1f} сек)"
            )
            return
    
    context.user_data[last_played_key] = now

    # 2. Обробка ставки
    try:
        if context.args:
            # Command mode: /casino <bet>
            bet_arg = context.args[0].lower()
        else:
            # Message mode: "казино <bet>"
            text = update.message.text.strip().lower()
            parts = text.split()
            if len(parts) < 2:
                raise ValueError("Не вказано ставку.")
            bet_arg = parts[1]
        
        if bet_arg in ["all", "all-in", "все", "ва-банк"]:
            bet = await get_user_balance(user.id)
            if bet == 0:
                 await update.message.reply_text("✙ У тебе 0 🌿 на балансі. Нема чим ризикувати. ✙")
                 return
        else:
            bet = int(bet_arg)

    except ValueError:
        await update.message.reply_html(
            f"✙ Введи свою ставку, котику. ✙\n"
            f"Наприклад: <code>/casino {MIN_BET}</code> (мін: {MIN_BET} 🌿, макс: {MAX_BET} 🌿)\n"
            f"Або ризикни усім: <code>/casino all</code>"
        )
        return

    if bet < MIN_BET:
        await update.message.reply_text(f"✙ Мінімальна ставка: {MIN_BET} 🌿 ✙")
        return
    if bet > MAX_BET:
        await update.message.reply_text(f"✙ Максимальна ставка: {MAX_BET} 🌿 ✙")
        return

    # 3. Перевірка балансу
    current_balance = await get_user_balance(user.id)
    if current_balance < bet:
        await update.message.reply_text(
            f"✙ У тебе недостатньо м'яти. ✙\n(Твій баланс: {current_balance} 🌿)"
        )
        return

    # 4. Гра
    # Знімаємо ставку
    await update_user_balance(user.id, -bet)
    
    spin = get_spin()
    winnings = calculate_winnings(bet, spin)

    result_text = "[ {} | {} | {} ]".format(*spin)
    
    if winnings > 0:
        # Додаємо виграш
        await update_user_balance(user.id, winnings)
        new_balance = current_balance - bet + winnings
        
        win_amount = winnings - bet # Чистий виграш
        
        # Визначаємо рівень виграшу для більшого "ВАУ!"
        if winnings >= bet * 25:  # Джекпот рівень
            wow_text = "🎉 <b>ДЖЕКПОТ!!! ВАУ!!!</b> 🎉"
            extra_emoji = "💰💎✨"
        elif winnings >= bet * 10:  # Великий виграш
            wow_text = "🌟 <b>ВАУ! ФАНТАСТИЧНИЙ ВИГРАШ!</b> 🌟"
            extra_emoji = "💎🎊"
        elif winnings >= bet * 5:  # Добрий виграш
            wow_text = "🎊 <b>ВАУ! ЧУДОВИЙ ВИГРАШ!</b> 🎊"
            extra_emoji = "🎉💫"
        elif winnings > bet:  # Невеликий виграш
            wow_text = "✨ <b>Мяу! Ти виграв!</b> ✨"
            extra_emoji = "🌟"
        else:  # Повернення ставки
            wow_text = "😺 <b>Уф! Ставка врятована!</b>"
            extra_emoji = "😌"
        
        message = (
            f"✙ <b>Мур-казино</b> ✙\n\n"
            f"{result_text}\n\n"
            f"{wow_text}\n"
            f"<b>+{win_amount} 🌿</b> (всього {winnings} 🌿) {extra_emoji}\n"
            f"<i>Баланс: {new_balance} 🌿</i>"
        )
    else:
        new_balance = current_balance - bet
        loss_messages = [
            "М'ятка не вродила... 😿",
            "Свята фортуна сьогодні не на твоїй стороні 🥺",
            "Котики теж іноді програють! 😸",
            "Спробуй ще раз, удача може повернутися! 🍀",
            "Не засмучуйся, це ж гра! 🎲"
        ]
        random_loss = random.choice(loss_messages)
        message = (
            f"✙ <b>Мур-казино</b> ✙\n\n"
            f"{result_text}\n\n"
            f"{random_loss}\n"
            f"Ти програв: <b>-{bet} 🌿</b>\n"
            f"<i>Баланс: {new_balance} 🌿</i>"
        )

    sent = await update.message.reply_html(message)

    # (НОВЕ) Автовидалення повідомлення казино через 3 хвилини, якщо увімкнено
    if await is_chat_module_enabled(chat, "auto_delete_actions"):
        # Видаляємо відповідь бота
        context.job_queue.run_once(
            delete_message_job,
            60,  # 3 хвилини
            data={"chat_id": chat.id, "message_id": sent.message_id}
        )
        # Видаляємо виклики команди користувача
        context.job_queue.run_once(
            delete_message_job,
            60,  # 3 хвилини
            data={"chat_id": chat.id, "message_id": update.message.message_id}
        )


async def balance_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Показує баланс користувача.
    """
    user = update.effective_user
    chat = update.effective_chat
    if not user or not chat:
        return

    # --- ПЕРЕВІРКА ПРАВ ---
    if not await is_chat_module_enabled(chat, "games"):
        logger.debug(f"Module 'games' (casino) disabled for chat {chat.id}. Ignoring balance.")
        return
    # --- КІНЕЦЬ ПЕРЕВІРКИ ---

    balance = await get_user_balance(user.id)
    sender_mention = mention(user)
    
    await update.message.reply_html(
        f"✙ {sender_mention}, твій запас м'яти: <b>{balance}</b> 🌿 ✙"
    )

async def casino_help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Показує правила казино.
    """
    chat = update.effective_chat
    if not chat:
        return

    # --- ПЕРЕВІРКА ПРАВ ---
    if not await is_chat_module_enabled(chat, "games"):
        logger.debug(f"Module 'games' (casino) disabled for chat {chat.id}. Ignoring casino_help.")
        return
    # --- КІНЕЦЬ ПЕРЕВІРКИ ---

    # Отримуємо поточні слоти та множники
    current_slots = await get_casino_slots()
    current_multipliers = await get_casino_multipliers()
    
    # Створюємо опис комбінацій
    combinations_text = ""
    if current_multipliers:
        # Три в ряд
        triple_combos = [combo for combo in current_multipliers.keys() if len(combo) == 3]
        for combo in sorted(triple_combos, key=lambda x: current_multipliers[x], reverse=True):
            emoji1, emoji2, emoji3 = combo
            multiplier = current_multipliers[combo]
            combinations_text += f"{emoji1} {emoji2} {emoji3} — x{multiplier}\n"
        
        # Пара
        pair_combos = [combo for combo in current_multipliers.keys() if len(combo) == 2]
        if pair_combos:
            combinations_text += "\n<i>Будь-які два однакові символи:</i>\n"
            for pair in sorted(pair_combos, key=lambda x: current_multipliers[x], reverse=True):
                emoji1, emoji2 = pair
                multiplier = current_multipliers[pair]
                combinations_text += f"{emoji1} {emoji2} — x{multiplier}\n"
    
    rules = (
        f"✙ <b>Правила Мур-казино</b> ✙\n\n"
        f"Гра проста, як котяче життя. Робиш ставку, крутиш слоти.\n\n"
        f"<b>Комбінації:</b>\n{combinations_text}\n"
        f"<b>Команди:</b>\n"
        f"<code>/casino [ставка]</code> — Зіграти (напр. <code>/casino 100</code>)\n"
        f"<code>/casino all</code> — Ризикнути усім.\n"
        f"<code>/balance</code> — Перевірити свій запас м'яти.\n"
    )
    await update.message.reply_html(rules)

def register_casino_handlers(application: Application) -> None:
    """
    Реєструє обробники для казино.
    """
    
    # --- Команда /casino ---
    application.add_handler(CommandHandler(
        ["casino", "slots"], 
        casino_command, 
        filters=filters.ChatType.GROUPS
    ))
    application.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND & filters.Regex(r"(?i)^(казино|слоти|ставка)\b.*") & filters.ChatType.GROUPS,
        casino_command
    ))
    
    # --- Команда /balance ---
    application.add_handler(CommandHandler(
        ["balance", "bal"], 
        balance_command, 
        filters=filters.ChatType.GROUPS
    ))
    application.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND & filters.Regex(r"(?i)^(баланс)$") & filters.ChatType.GROUPS,
        balance_command
    ))
    
    # --- Команда /casino_help ---
    application.add_handler(CommandHandler(
        ["casino_help"], 
        casino_help_command, 
        filters=filters.ChatType.GROUPS
    ))
    application.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND & filters.Regex(r"(?i)^(казино допомога)$") & filters.ChatType.GROUPS,
        casino_help_command
    ))
    
    logger.info("Обробники Мур-Казино (casino_handlers.py) завантажено. 🎰")