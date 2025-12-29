# -*- coding: utf-8 -*-
"""mandarin_duel_game.py

Новорічна дуель: «Хто більше мандаринок скушає 🍊»
- виклик: "мандаринка" (обов'язково reply)
- інвайт: кнопки прийняти/відмовитись + таймаут
- переможець випадковий (без нічиї)
- ставка: 10 м'яток (перевірки ДО старту)
- анти-абʼюз: self-duel, active duel lock, cooldown
"""

from __future__ import annotations

import asyncio
import logging
import random
import secrets
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from typing import Dict, Any, Optional

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode, ChatType
from telegram.ext import (
    Application,
    ContextTypes,
    CallbackQueryHandler,
    MessageHandler,
    filters,
)
from telegram.helpers import mention_html

from bot.core.database import get_user_balance, update_user_balance, transfer_user_balance_atomic, add_mandarin_duel_stats
from bot.handlers.chat_admin_handlers import is_chat_module_enabled
from bot.features.new_year_mode import is_new_year_mode, apply_new_year_style

logger = logging.getLogger(__name__)

KYIV_TZ = ZoneInfo("Europe/Kyiv")

STAKE = 10
INVITE_TIMEOUT = 60  # seconds
COOLDOWN = 45        # seconds

DUELS_KEY = "mandarin_duels"
COOLDOWN_KEY = "mandarin_duel_cooldowns"


# -------- Тексти (варіативні) --------

INVITE_TEMPLATES = [
    (
        "🍊 <b>Мандаринкова дуель!</b>\n"
        "{challenger} викликає тебе на поїдання мандаринок\n"
        "Ставка: <b>{stake}</b> 🌿\n\n"
        "Без обмеження часу на відповідь"
    ),
    (
        "🍊 <b>Дуель на мандаринки</b>\n"
        "{challenger} штовхає лапкою: 'ну шо, зʼїси більше?' 😼\n"
        "Ставка: <b>{stake}</b> 🌿\n\n"
        "Без обмеження часу на рішення"
    ),
]

DECLINE_TEMPLATES = [
    "Ой-йой 😿 Дуель відхилено. Мандаринки лишились на потім.",
    "Ну нічо 😺 Мандаринки почекають. Дуель скасовано.",
]

TIMEOUT_TEMPLATES = [
    "⏳ Мандаринки охололи… Запрошення згоріло. Спробуйте ще раз 🧡",
    "⏳ Час вийшов. Мандаринки розбіглись по мисках 😼",
]

BALANCE_FAIL_TEMPLATES = [
    "У когось не вистачило мʼяток 🌿 Дуель скасовано, мур.",
    "Мʼяток не вистачило… Дуель не стартує 😿",
]

RESULT_TEMPLATES = [
    (
        "🍊 <b>Починаємо батл!</b>\n"
        "Хрум-хрум… мандаринки летять 🍊🍊🍊\n\n"
        "👀 Підрахунок…\n\n"
        "🏆 Переміг(ла): {winner}!\n"
        "Він(вона) скушав(ла) <b>{w_cnt}</b> мандаринок 🍊\n\n"
        "😿 {loser} скушав(ла) <b>{l_cnt}</b> мандаринки\n\n"
        "🔻 {loser_plain} −{stake} 🌿\n"
        "🔺 {winner_plain} +{stake} 🌿"
    ),
    (
        "🍊 <b>Мандаринковий батл!</b>\n"
        "Сніжок летить, лапки липнуть, але ми тримаємось 😼\n\n"
        "🏆 {winner} бере верх! (<b>{w_cnt}</b> 🍊)\n"
        "😿 {loser} відстав(ла)… (<b>{l_cnt}</b> 🍊)\n\n"
        "Баланс: +{stake} / -{stake} 🌿"
    ),
]


def _now() -> datetime:
    return datetime.now(KYIV_TZ)


def _get_lock(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> asyncio.Lock:
    """Пер-чатовий lock проти race condition (accept/timeout/stop)."""
    locks = context.application.bot_data.setdefault("mandarin_duel_locks", {})
    lock = locks.get(chat_id)
    if lock is None:
        lock = asyncio.Lock()
        locks[chat_id] = lock
    return lock


def _job_name(chat_id: int, duel_id: str) -> str:
    return f"mandarin_duel_timeout_{chat_id}_{duel_id}"


def _cancel_timeout_job(context: ContextTypes.DEFAULT_TYPE, chat_id: int, duel_id: str) -> None:
    """Скасовує timeout job (якщо він ще існує)."""
    try:
        name = _job_name(chat_id, duel_id)
        jobs = context.job_queue.get_jobs_by_name(name) if context.job_queue else []
        for j in jobs:
            try:
                j.schedule_removal()
            except Exception:
                pass
    except Exception:
        logger.debug("Failed to cancel mandarin duel timeout job", exc_info=True)


def _get_duels(context: ContextTypes.DEFAULT_TYPE, chat_id: int | None = None) -> Dict[str, Dict[str, Any]]:
    """Return duel storage dict.

    - In normal update handlers, use context.chat_data.
    - In JobQueue callbacks, context.chat_data can be None unless job was scheduled with chat_id.
      We therefore allow passing chat_id and use application.chat_data as a stable storage.
    """
    if chat_id is None:
        if getattr(context, "chat_data", None) is None:
            # Fallback (shouldn't happen in normal update flow)
            return context.application.bot_data.setdefault(DUELS_KEY, {})  # type: ignore[return-value]
        return context.chat_data.setdefault(DUELS_KEY, {})  # type: ignore[return-value]

    # In JobQueue the application.chat_data is MappingProxy (read-only), so we cannot call setdefault on it.
    app_chat_data = getattr(context.application, "chat_data", {})
    per_chat = app_chat_data.get(chat_id)

    if per_chat is None:
        # If no chat_data exists yet (e.g., job fired after restart), use bot_data as fallback storage.
        per_chat = context.application.bot_data.setdefault("mandarin_duels_fallback", {}).setdefault(chat_id, {})

    return per_chat.setdefault(DUELS_KEY, {})


def _get_cooldowns(context: ContextTypes.DEFAULT_TYPE) -> Dict[int, float]:
    # user_id -> unix ts
    return context.chat_data.setdefault(COOLDOWN_KEY, {})


def _user_in_active_duel(duels: Dict[str, Dict[str, Any]], user_id: int) -> bool:
    for d in duels.values():
        if d.get("status") in ("invited", "active") and user_id in (d.get("challenger_id"), d.get("target_id")):
            return True
    return False


async def mandarinka_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or not update.effective_user or not update.message:
        return

    chat = update.effective_chat
    user = update.effective_user

    # Доступ тільки у новорічному режимі
    if not await is_new_year_mode(chat.id):
        await update.message.reply_text(apply_new_year_style("Новорічний режим вимкнено. Попроси адміна увімкнути 🎄"))
        return

    # Логіка дуелі — тільки в групах/супергрупах
    if chat.type == ChatType.PRIVATE:
        await update.message.reply_text(apply_new_year_style("Ця дуель працює в групах 😺 Додай мене в чатик і граємо!"))
        return

    # Доступ тільки якщо модуль ігор увімкнений (налаштування чату)
    if not await is_chat_module_enabled(chat, "games_enabled"):
        await update.message.reply_text(apply_new_year_style("Ігри в цьому чаті вимкнені адміном 😿"))
        return

    # Анти-спам: cooldown
    cooldowns = _get_cooldowns(context)
    ts = cooldowns.get(user.id, 0.0)
    now_ts = _now().timestamp()
    if now_ts - ts < COOLDOWN:
        wait_s = int(COOLDOWN - (now_ts - ts))
        await update.message.reply_text(apply_new_year_style(f"Тихіше, котику 😺 Дай мандаринкам {wait_s}с перепочити."))
        return

    # Потрібен reply
    if not update.message.reply_to_message or not update.message.reply_to_message.from_user:
        await update.message.reply_text(
            apply_new_year_style("Мур 😺 Щоб кинути дуель, напиши <b>мандаринка</b> у відповідь на повідомлення суперника."),
            parse_mode=ParseMode.HTML)
        return

    target = update.message.reply_to_message.from_user
    if target.id == user.id:
        await update.message.reply_text(apply_new_year_style("Ей, котику… самому собі дуель не кидають 😼"))
        return

    if target.is_bot:
        await update.message.reply_text(apply_new_year_style("Мур 😼 З ботом дуель нечесна. Клич справжнього суперника!"))
        return

    # Перевірка активних дуелей
    duels = _get_duels(context)
    if _user_in_active_duel(duels, user.id) or _user_in_active_duel(duels, target.id):
        await update.message.reply_text(apply_new_year_style("Хтось із вас вже в дуелі 🍊 Спочатку завершимо ту, мур."))
        return

    # Перевірки балансу ДО старту
    bal_ch = await get_user_balance(user.id)
    bal_tg = await get_user_balance(target.id)

    if bal_ch < STAKE:
        await update.message.reply_text(apply_new_year_style(f"У тебе замало мʼяток 🌿 Потрібно {STAKE}, а є {bal_ch}."))
        return
    if bal_tg < STAKE:
        await update.message.reply_text(apply_new_year_style(f"У {target.first_name} замало мʼяток 🌿 (потрібно {STAKE})."))
        return

    duel_id = secrets.token_hex(4)  # короткий
    expires_at = _now() + timedelta(seconds=INVITE_TIMEOUT)

    duels[duel_id] = {
        "chat_id": chat.id,
        "challenger_id": user.id,
        "target_id": target.id,
        "challenger_name": user.first_name or "котик",
        "target_name": target.first_name or "котик",
        "status": "invited",
        "created_at": _now().timestamp(),
        "expires_at": expires_at.timestamp(),
        "invite_message_id": None,
        "settled": False,
    }

    # ставимо кулдаун ініціатору
    cooldowns[user.id] = now_ts

    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🍊 Прийняти", callback_data=f"mandarin_duel:accept:{duel_id}"),
            InlineKeyboardButton("❌ Відмовитись", callback_data=f"mandarin_duel:decline:{duel_id}"),
        ]
    ])

    challenger_m = mention_html(user.id, user.first_name)
    target_m = mention_html(target.id, target.first_name)
    text = random.choice(INVITE_TEMPLATES).format(
        challenger=challenger_m,
        target=target_m,
        stake=STAKE,
    )

    sent = await update.message.reply_text(apply_new_year_style(text), parse_mode=ParseMode.HTML, reply_markup=kb)
    duels[duel_id]["invite_message_id"] = sent.message_id

    logger.info(
        "mandarin_duel invited: chat=%s duel=%s challenger=%s target=%s",
        chat.id,
        duel_id,
        user.id,
        target.id,
    )

    # Таймаут через JobQueue - ВИМКНЕНО
    # try:
    #     context.job_queue.run_once(
    #         mandarin_duel_timeout,
    #         when=INVITE_TIMEOUT,
    #         data={"chat_id": chat.id, "duel_id": duel_id},
    #         name=_job_name(chat.id, duel_id),
    #     )
    # except Exception:
    #     logger.exception("Failed to schedule mandarin duel timeout")


async def mandarin_duel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.data or not update.effective_user:
        return

    parts = query.data.split(":")
    if len(parts) != 3:
        await query.answer()
        return

    _, action, duel_id = parts
    await query.answer()

    # Працюємо під lock — проти подвійних кліків/timeout race
    # chat_id беремо з persisted дуелі, але для цього треба спершу знайти її.
    duels = _get_duels(context)
    duel = duels.get(duel_id)
    if not duel:
        return

    chat_id = duel.get("chat_id")
    if not chat_id:
        duels.pop(duel_id, None)
        return

    user = update.effective_user
    challenger_id = duel["challenger_id"]
    target_id = duel["target_id"]

    # Тільки ціль може прийняти/відмовити
    if user.id != target_id:
        await query.answer("Це не твоя дуель 😼", show_alert=False)
        return

    lock = _get_lock(context, int(chat_id))
    async with lock:
        # Перевірка статусу/таймауту
        duel = duels.get(duel_id)
        if not duel:
            return

        if duel.get("settled"):
            return

        if duel.get("status") != "invited":
            return

        if _now().timestamp() > float(duel.get("expires_at", 0)):
            duel["status"] = "finished"
            duel["settled"] = True
            await _finish_duel_message(context, chat_id, duel, apply_new_year_style(random.choice(TIMEOUT_TEMPLATES)))
            duels.pop(duel_id, None)
            return

        if action == "decline":
            duel["status"] = "finished"
            duel["settled"] = True
            _cancel_timeout_job(context, int(chat_id), duel_id)
            await _finish_duel_message(
                context,
                chat_id,
                duel,
                apply_new_year_style(random.choice(DECLINE_TEMPLATES)),
            )
            duels.pop(duel_id, None)
            return

        if action != "accept":
            return

        # accept
        duel["status"] = "active"
        _cancel_timeout_job(context, int(chat_id), duel_id)

        # Повторна перевірка балансу (на випадок змін)
        bal_ch = await get_user_balance(challenger_id)
        bal_tg = await get_user_balance(target_id)
        if bal_ch < STAKE or bal_tg < STAKE:
            duel["status"] = "finished"
            duel["settled"] = True
            await _finish_duel_message(
                context,
                chat_id,
                duel,
                apply_new_year_style(random.choice(BALANCE_FAIL_TEMPLATES)),
            )
            duels.pop(duel_id, None)
            return

        # Рандом, але контрольований: генеруємо обидва результати без нічиї
        a_cnt = random.randint(3, 10)
        b_cnt = random.randint(3, 10)
        if a_cnt == b_cnt:
            b_cnt = 10 if b_cnt < 10 else 9

        if a_cnt > b_cnt:
            winner_id, loser_id = challenger_id, target_id
            w_cnt, l_cnt = a_cnt, b_cnt
        else:
            winner_id, loser_id = target_id, challenger_id
            w_cnt, l_cnt = b_cnt, a_cnt

        # Атомарний переказ мʼяток (захист від подвійного списання)
        ok = await transfer_user_balance_atomic(loser_id, winner_id, STAKE)
        if not ok:
            duel["status"] = "finished"
            duel["settled"] = True
            await _finish_duel_message(
                context,
                chat_id,
                duel,
                apply_new_year_style(random.choice(BALANCE_FAIL_TEMPLATES)),
            )
            duels.pop(duel_id, None)
            return

        # Оновлюємо статистику "Мандаринки" у профілі.
        # Важливо: робимо це *після* атомарного переказу мʼяток і тільки один раз (під settled/lock).
        try:
            await add_mandarin_duel_stats(winner_id, eaten_delta=w_cnt, wins_delta=1, played_delta=1)
            await add_mandarin_duel_stats(loser_id, eaten_delta=l_cnt, wins_delta=0, played_delta=1)
        except Exception:
            logger.exception("Failed to update mandarin duel stats", exc_info=True)

        # Красивий фінал (без зайвих API-викликів: імена з інвайту)
        ch_name = duel.get("challenger_name") or "котик"
        tg_name = duel.get("target_name") or "котик"
        winner_name = ch_name if winner_id == challenger_id else tg_name
        loser_name = tg_name if winner_id == challenger_id else ch_name

        winner_m = mention_html(winner_id, winner_name)
        loser_m = mention_html(loser_id, loser_name)
        winner_plain = winner_name
        loser_plain = loser_name
        result_text = random.choice(RESULT_TEMPLATES).format(
            winner=winner_m,
            loser=loser_m,
            winner_plain=winner_plain,
            loser_plain=loser_plain,
            w_cnt=w_cnt,
            l_cnt=l_cnt,
            stake=STAKE,
        )

        logger.info(
            "mandarin_duel finished: chat=%s duel=%s winner=%s loser=%s score=%s:%s stake=%s",
            chat_id,
            duel_id,
            winner_id,
            loser_id,
            w_cnt,
            l_cnt,
            STAKE,
        )

        duel["status"] = "finished"
        duel["settled"] = True
        await _finish_duel_message(context, chat_id, duel, apply_new_year_style(result_text), parse_mode=ParseMode.HTML)
        duels.pop(duel_id, None)

async def mandarin_duel_timeout(context: ContextTypes.DEFAULT_TYPE) -> None:
    """JobQueue callback: скасовує інвайт по таймауту."""
    data = getattr(context.job, "data", None) or {}
    chat_id = data.get("chat_id")
    duel_id = data.get("duel_id")
    if not chat_id or not duel_id:
        return

    duels = _get_duels(context, int(chat_id))
    duel = duels.get(duel_id)
    if not duel:
        return

    lock = _get_lock(context, int(chat_id))
    async with lock:
        duel = duels.get(duel_id)
        if not duel:
            return
        if duel.get("settled"):
            return
        if duel.get("status") != "invited":
            return

        duel["status"] = "finished"
        duel["settled"] = True
        await _finish_duel_message(
            context,
            chat_id,
            duel,
            apply_new_year_style(random.choice(TIMEOUT_TEMPLATES)),
        )
        duels.pop(duel_id, None)


def _find_duel_in_chat(duels: Dict[str, Dict[str, Any]], user_id: Optional[int] = None) -> Optional[tuple[str, Dict[str, Any]]]:
    for did, d in duels.items():
        if d.get("status") in ("invited", "active") and not d.get("settled"):
            if user_id is None or user_id in (d.get("challenger_id"), d.get("target_id")):
                return did, d
    return None


async def stop_mandarin_duel_in_chat(chat_id: int, by_user_id: int, context: ContextTypes.DEFAULT_TYPE) -> str:
    """Зупиняє мандаринкову дуель у чаті.

    Право: тільки ініціатор (challenger).

    Повертає:
    - 'stopped'  : зупинено
    - 'forbidden': дуель є, але зупинити не можна
    - 'none'     : дуелі немає
    """
    duels = _get_duels(context, chat_id)
    found = _find_duel_in_chat(duels)
    if not found:
        return "none"

    duel_id, duel = found
    if int(duel.get("challenger_id")) != int(by_user_id):
        return "forbidden"

    lock = _get_lock(context, int(chat_id))
    async with lock:
        duel = duels.get(duel_id)
        if not duel or duel.get("settled"):
            return "none"

        duel["status"] = "finished"
        duel["settled"] = True
        _cancel_timeout_job(context, int(chat_id), duel_id)
        await _finish_duel_message(
            context,
            chat_id,
            duel,
            apply_new_year_style("🛑 Дуель зупинено ініціатором. Мандаринки — в мисочку, мур 😺"),
            parse_mode=ParseMode.HTML,
        )
        duels.pop(duel_id, None)
        return "stopped"


async def cleanup_mandarin_duels_after_restart(application: Application) -> None:
    """Очищає завислі дуелі після рестарту (через PicklePersistence чат_data зберігається)."""
    try:
        now_ts = _now().timestamp()
        for chat_id, data in list(application.chat_data.items()):
            duels = (data or {}).get(DUELS_KEY)
            if not isinstance(duels, dict) or not duels:
                continue
            for duel_id, duel in list(duels.items()):
                status = duel.get("status")
                settled = duel.get("settled")
                expires_at = float(duel.get("expires_at", 0))
                created_at = float(duel.get("created_at", 0))

                # pending/active, але без job'ів після рестарту → чистимо.
                is_stale = False
                if settled:
                    is_stale = True
                elif status in ("invited", "active") and (expires_at and now_ts > expires_at + 5):
                    is_stale = True
                elif status in ("invited", "active") and (created_at and now_ts - created_at > 5 * 60):
                    is_stale = True

                if not is_stale:
                    continue

                # Пробуємо акуратно оновити інвайт (якщо ще можна)
                try:
                    msg_id = duel.get("invite_message_id")
                    if msg_id:
                        await application.bot.edit_message_text(
                            chat_id=int(chat_id),
                            message_id=int(msg_id),
                            text=apply_new_year_style("♻️ Бот перезапустився — дуель скасовано. Кинь виклик ще раз 🍊"),
                            parse_mode=ParseMode.HTML,
                            reply_markup=None,
                            disable_web_page_preview=True,
                        )
                except Exception:
                    pass

                duels.pop(duel_id, None)
    except Exception:
        logger.exception("cleanup_mandarin_duels_after_restart failed")


async def _finish_duel_message(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    duel: Dict[str, Any],
    text: str,
    parse_mode: Optional[str] = None,
) -> None:
    """Акуратно завершує повідомлення інвайту: прибирає кнопки і (за бажанням) оновлює текст."""
    msg_id = duel.get("invite_message_id")
    if not msg_id:
        return
    try:
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=msg_id,
            text=text,
            parse_mode=parse_mode,
            reply_markup=None,
            disable_web_page_preview=True,
        )
    except Exception:
        # якщо не можна редагувати текст — хоча б прибираємо кнопки
        try:
            await context.bot.edit_message_reply_markup(chat_id=chat_id, message_id=msg_id, reply_markup=None)
        except Exception:
            logger.debug("Failed to edit mandarin duel message", exc_info=True)


def register_mandarin_duel_handlers(application: Application) -> None:
    """Реєстрація хендлерів гри."""
    # текстова команда "мандаринка" у групах/супергрупах і ПП
    application.add_handler(
        MessageHandler(
            filters.Regex(r"(?i)^\s*мандаринка\b") & (filters.ChatType.GROUPS | filters.ChatType.PRIVATE),
            mandarinka_command,
        ),
        group=6,  # щоб не перебивати більш важливі роутери
    )

    application.add_handler(
        CallbackQueryHandler(mandarin_duel_callback, pattern=r"^mandarin_duel:"),
        group=6,
    )

    logger.info("Новорічна дуель 'мандаринка' завантажена 🍊")