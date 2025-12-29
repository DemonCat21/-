# daily_tasks.py
"""
daily_tasks.py

Цей модуль - наш монастирський дзвін. 🔔
Він відповідає за щоденні ритуали:
призначення "Монашки дня" та роздачу "Передбачень".
Все відбувається згідно з божественним розкладом.
"""

import logging
import random
import asyncio
from datetime import date
from telegram.ext import ContextTypes
from telegram.error import Forbidden, BadRequest

from bot.core.database import (
    get_all_chats, get_users_in_chat, get_all_user_ids, set_daily_prediction
)
from bot.services.predictions import load_predictions

logger = logging.getLogger(__name__)


async def assign_daily_predictions_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    (Щоденно) Призначає передбачення кожному відомому користувачу.
    Намагається призначити унікальні передбачення, якщо це можливо.
    Якщо користувачів більше, ніж передбачень, використовує повтори.
    """
    logger.info("Запускаю щоденне завдання 'Передбачення дня'...")
    
    predictions = load_predictions()
    if not predictions or "мовчать" in predictions[0]:
        logger.warning("Немає доступних передбачень для призначення.")
        return

    all_user_ids = await get_all_user_ids()
    if not all_user_ids:
        logger.info("Не знайдено користувачів для призначення передбачень.")
        return

    today_str = date.today().isoformat()
    num_users = len(all_user_ids)
    num_predictions = len(predictions)

    # --- ВИПРАВЛЕННЯ ЛОГІКИ: Унікальні передбачення ---
    if num_users <= num_predictions:
        # Якщо передбачень вистачає, видаємо унікальні
        logger.info(f"Видаю {num_users} унікальних передбачень.")
        chosen_predictions = random.sample(predictions, num_users)
    else:
        # Якщо користувачів більше, видаємо з повторами
        logger.warning(
            f"Користувачів ({num_users}) більше, ніж передбачень ({num_predictions}). "
            "Використовую повтори."
        )
        chosen_predictions = random.choices(predictions, k=num_users)
    
    successful_assignments = 0
    for user_id, prediction in zip(all_user_ids, chosen_predictions):
        try:
            await set_daily_prediction(user_id, prediction, today_str)
            successful_assignments += 1
        except Exception as e:
            logger.error(f"Помилка при призначенні передбачення для {user_id}: {e}")
        
        # Невелика затримка, щоб не перевантажувати БД
        await asyncio.sleep(0.05) 

    logger.info(
        f"Завдання 'Передбачення дня' завершено. "
        f"Оброблено {successful_assignments}/{num_users} користувачів."
    )


async def nun_of_the_day_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    (Щоденно) Обирає "Монашку дня" в кожному груповому чаті та надсилає вітальне повідомлення.
    """
    logger.info("Запускаю щоденне завдання 'Монашка дня'...")
    all_chats = await get_all_chats(page_size=None)
    
    for chat_info in all_chats:
        chat_id = chat_info['chat_id']
        # Пропускаємо приватні чати
        if chat_id > 0:
            continue

        try:
            user_ids = await get_users_in_chat(chat_id)
            bot_id = context.bot.id
            # Обираємо тільки реальних користувачів, а не бота
            active_user_ids = [uid for uid in user_ids if uid != bot_id]

            if not active_user_ids:
                logger.info(f"В чаті {chat_id} немає активних користувачів.")
                continue

            # Обираємо щасливчика
            nun_id = random.choice(active_user_ids)
            
            try:
                nun_member = await context.bot.get_chat(nun_id)
                nun_mention = nun_member.mention_html()
                
                message = (
                    f"✝️ <b>Монашка сьогоднішнього дня</b> ✝️\n\n"
                    f"Вітаємо {nun_mention}, зірки пророкують вам "
                    "цікавий та насичений день! ✨\n\n"
                    f"<i>Нехай Господь береже вас... або ні.</i> 😏"
                )
                
                await context.bot.send_message(chat_id, text=message, parse_mode='HTML')
                logger.info(f"Монашка дня' надіслано в чат {chat_id}. Обрано: {nun_id}")

            except (Forbidden, BadRequest) as e:
                logger.warning(f"Не вдалося надіслати повідомлення / "
                                f"отримати інфо про {nun_id}: {e}")

        except (Forbidden, BadRequest) as e:
            logger.warning(f"Не вдалося обробити чат {chat_id} (можливо, бота видалено): {e}")
        except Exception as e:
            logger.error(f"Неочікувана помилка в 'nun_of_the_day_job' "
                         f"для чату {chat_id}: {e}", exc_info=True)
        
        # Чекаємо 1 секунду між відправками, щоб не отримати бан
        await asyncio.sleep(1)

    logger.info("Щоденне завдання 'Монашка дня' завершено.")