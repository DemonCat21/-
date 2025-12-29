# profile_handlers.py
# -*- coding: utf-8 -*-
"""
Профіль користувача + редагування (крок 4).
/profile, /me, /профіль — показ профілю (HTML)
/editprofile — редагування (лише команда, без кнопок у профілі)
"""

import logging
import html
import asyncio
from typing import Optional, Dict, Any

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import Forbidden, BadRequest
from telegram.constants import ParseMode, ChatType
from telegram.ext import (
    ContextTypes,
    CommandHandler,
    CallbackQueryHandler,
    ConversationHandler,
    MessageHandler,
    filters,
)

from bot.utils.utils import (
    AddressingContext,
    cancel_auto_close,
    get_user_addressing,
    mention,
    set_auto_close_payload,
    start_auto_close,
)
from bot.core.database import (
    ensure_user_data,
    get_user_balance,
    get_user_profile,
    update_user_profile,
)

logger = logging.getLogger(__name__)


async def _pm_edit_link(context: "ContextTypes.DEFAULT_TYPE", payload: str = "editprofile") -> str:
    """HTML deeplink to open bot in PM."""
    username = getattr(context.bot, "username", None)
    if not username:
        try:
            me = await context.bot.get_me()
            username = getattr(me, "username", None)
        except Exception:
            username = None
    if not username:
        return ""
    return f"https://t.me/{username}?start={payload}"

# ====== callbacks ======
CB_STATS_OPEN = "profile_stats:open"
CB_STATS_PAGE = "profile_stats:page:"  # +1/2
CB_BACK_TO_PROFILE = "profile_stats:back_profile"
CB_PROFILE_CLOSE = "profile:close"
PROFILE_AUTO_CLOSE_KEY = "profile_screen"

# ====== edit profile conversation ======
EP_GENDER, EP_CITY, EP_QUOTE = range(3)

# callbacks (edit flow)
CB_GENDER = "editprofile:gender:"  # +value
CB_SKIP_GENDER = "editprofile:gender:skip"
CB_SKIP_CITY = "editprofile:city:skip"
CB_SKIP_QUOTE = "editprofile:quote:skip"
CB_CANCEL = "editprofile:cancel"

# callbacks (entry from profile buttons)
CB_EDIT_START = "profile_edit:start"
CB_EDIT_GENDER = "profile_edit:gender"
CB_EDIT_CITY = "profile_edit:city"
CB_EDIT_QUOTE = "profile_edit:quote"


def _safe_text(v: Optional[str], fallback: str = "—") -> str:
    v = (v or "").strip()
    return html.escape(v) if v else fallback


async def _xo_stats_for_user(user_id: int) -> Dict[str, int]:
    """
    Витягує статистику ХН з існуючої таблиці game_stats.
    Не змінює логіку/дані.
    """
    # Щоб не залежати від поламаного/змінного get_game_stats в старих версіях,
    # робимо прямий селект через aiosqlite в database.py вже наявні поля.
    from bot.core.database import DB_PATH
    import aiosqlite

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """
            SELECT
              COALESCE(SUM(wins),0) as total_wins,
              COALESCE(SUM(losses),0) as total_losses,
              COALESCE(SUM(draws),0) as total_draws,
              COALESCE(SUM(wins_vs_bot),0) as wins_vs_bot,
              COALESCE(SUM(wins_vs_human),0) as wins_vs_human
            FROM game_stats
            WHERE user_id = ? AND game_name = ?
            """,
            (user_id, "tic_tac_toe"),
        )
        row = await cur.fetchone()

    total_wins = int(row["total_wins"] or 0)
    total_losses = int(row["total_losses"] or 0)
    total_draws = int(row["total_draws"] or 0)
    wins_vs_bot = int(row["wins_vs_bot"] or 0)
    wins_vs_human = int(row["wins_vs_human"] or 0)

    return {
        "wins_vs_bot": wins_vs_bot,
        "wins_vs_human": wins_vs_human,
        "total_wins": total_wins,
        "total_losses": total_losses,
        "total_draws": total_draws,
    }


async def _mems_stats_for_user(user_id: int) -> Dict[str, int]:
    from bot.core.database import mems_get_global_stats
    stats = await mems_get_global_stats()
    row = stats.get(str(user_id)) or {}
    return {
        "total_games": int(row.get("games_played", 0) or 0),
        "total_points": int(row.get("total_score", 0) or 0),
        "total_wins": int(row.get("wins", 0) or 0),
    }


def _blockquote(text: str) -> str:
    """
    Реальна Telegram-цитата в HTML через <blockquote>.
    Важливо: екрануємо весь user input.
    """
    t = (text or "").strip()
    if not t:
        t = "…"
    # підтримка багаторядкових цитат
    t = html.escape(t).replace("\r\n", "\n").replace("\r", "\n")
    return f"<blockquote>{t}</blockquote>"


def _profile_text(
    ctx_or_user_mention,
    user_mention: str | None = None,
    gender: str | None = None,
    city: str | None = None,
    quote: str | None = None,
    mandarin_eaten: int = 0,
    mandarin_duel_wins: int = 0,
    balance: int = 0,
) -> str:
    """Формує текст профілю, сумісно з новим і старим викликом.

    Новий виклик: _profile_text(ctx, mention, gender, city, quote, mandarin_eaten, mandarin_duel_wins, balance)
    Старий виклик (fallback): _profile_text(mention, gender, city, quote, balance)
    """

    if isinstance(ctx_or_user_mention, AddressingContext):
        ctx = ctx_or_user_mention
        um = user_mention or ""
        me = mandarin_eaten
        mdw = mandarin_duel_wins
        bal = balance
        g = gender or "—"
        c = city or "—"
        q = quote or "…"
    else:
        # Backward compatibility: calls без ctx та без мандаринових статів
        ctx = AddressingContext(None)
        um = ctx_or_user_mention or ""
        g = user_mention or "—"
        c = gender or "—"
        q = (city or "…")
        me = 0
        mdw = 0
        bal = quote if isinstance(quote, int) else balance

    your = ctx.your.capitalize()
    about_you = "Вас" if ctx.you == "Ви" else "тебе"

    return (
        f"✨<b> {your} профіль, {um}</b>\n\n"
        f"<b>Про {about_you}:</b>\n"
        f"🐈‍⬛ <u>Стать</u> {g}\n"
        f"🌃 <u>Місто</u> {c}\n"
        f"{_blockquote(q)}\n\n"
        f"<b>🍊 Рейтинг мандаринок:</b>\n"
        f"🍊 <u>Зʼїдено</u> {int(me)}\n"
        f"🏆 <u>Виграно дуелей</u> {int(mdw)}\n\n"
        f"<i><b>✙{your} запас м'яти: {bal} 🌿✙</b></i>"
    )


def _profile_keyboard(is_private: bool) -> InlineKeyboardMarkup:
    """In groups/channels show only Stats; in PM also show edit buttons."""
    if not is_private:
        return InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("📊 Статистика", callback_data=CB_STATS_OPEN)],
                [InlineKeyboardButton("❌ Закрити", callback_data=CB_PROFILE_CLOSE)],
            ]
        )

    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("📊 Статистика", callback_data=CB_STATS_OPEN)],
            [
                InlineKeyboardButton("✏️ Стать", callback_data=CB_EDIT_GENDER),
                InlineKeyboardButton("🌃 Місто", callback_data=CB_EDIT_CITY),
            ],
            [
                InlineKeyboardButton("💬 Цитата", callback_data=CB_EDIT_QUOTE),
                InlineKeyboardButton("✏️ Редагувати", callback_data=CB_EDIT_START),
            ],
            [InlineKeyboardButton("❌ Закрити", callback_data=CB_PROFILE_CLOSE)],
        ]
    )


async def _arm_profile_auto_close(context: ContextTypes.DEFAULT_TYPE, message, *, fallback_text: str) -> None:
    if not message:
        return
    cancel_auto_close(context, PROFILE_AUTO_CLOSE_KEY)
    set_auto_close_payload(
        context,
        PROFILE_AUTO_CLOSE_KEY,
        chat_id=message.chat_id,
        message_id=message.message_id,
        fallback_text=fallback_text,
    )
    # Check if auto_delete_actions is enabled
    from bot.core.database import get_chat_settings
    settings = await get_chat_settings(message.chat_id)
    if settings.get('auto_delete_actions', 0) == 1:
        start_auto_close(context, PROFILE_AUTO_CLOSE_KEY, timeout=420)  # 7 minutes


def _stats_keyboard(page: int) -> InlineKeyboardMarkup:
    if page == 1:
        return InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("➡️", callback_data=f"{CB_STATS_PAGE}:2")],
                [InlineKeyboardButton("⬅️ Назад", callback_data=CB_BACK_TO_PROFILE)],
            ]
        )
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("⬅️", callback_data=f"{CB_STATS_PAGE}:1")],
            [InlineKeyboardButton("⬅️ Назад", callback_data=CB_BACK_TO_PROFILE)],
        ]
    )


async def profile_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    msg = update.effective_message
    if not user or not msg:
        return

    await ensure_user_data(user.id, user.username, user.first_name, user.last_name)

    prof = await get_user_profile(user.id)
    gender = _safe_text(prof.get("gender"))
    city = _safe_text(prof.get("city"))
    quote_raw = (prof.get("quote") or "").strip()
    balance = int(prof.get("balance") or 0)
    mandarin_eaten = int(prof.get("mandarin_eaten") or 0)
    mandarin_wins = int(prof.get("mandarin_duel_wins") or 0)
    ctx = await get_user_addressing(user.id)

    text = _profile_text(ctx, mention(user), gender, city, quote_raw, mandarin_eaten, mandarin_wins, balance)
    sent = await msg.reply_text(
        text=text,
        parse_mode=ParseMode.HTML,
        reply_markup=_profile_keyboard(update.effective_chat and update.effective_chat.type == ChatType.PRIVATE),
        disable_web_page_preview=True,
    )
    _arm_profile_auto_close(
        context,
        sent,
        fallback_text="Екран профілю закрито через бездіяльність.",
    )


async def profile_stats_open(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    await query.answer()

    user = query.from_user
    xo = await _xo_stats_for_user(user.id)

    total_games = xo.get('total_wins', 0) + xo.get('total_losses', 0) + xo.get('total_draws', 0)
    win_rate = f"{(xo.get('total_wins', 0) / total_games * 100):.1f}%" if total_games > 0 else "0%"

    text = (
        "<b>🐾 Статистика Хрестиків-Нуликів:</b>\n\n"
        f"🎮 <i><b>Всього ігор:</b></i> {total_games}\n"
        f"🏆 <i><b>Перемог:</b></i> {xo.get('total_wins', 0)}\n"
        f"💔 <i><b>Поразок:</b></i> {xo.get('total_losses', 0)}\n"
        f"🤝 <i><b>Нічиїх:</b></i> {xo.get('total_draws', 0)}\n"
        f"📈 <i><b>Відсоток перемог:</b></i> {win_rate}\n\n"
        f"<b>🤖 Проти бота:</b> {xo.get('wins_vs_bot', 0)} перемог\n"
        f"<b>👥 Проти людей:</b> {xo.get('wins_vs_human', 0)} перемог\n\n"
        f"{_blockquote('Мур... Продовжуй грати! 🌿')}"
    )

    await query.edit_message_text(
        text=text,
        parse_mode=ParseMode.HTML,
        reply_markup=_stats_keyboard(page=1),
        disable_web_page_preview=True,
    )
    if query.message:
        _arm_profile_auto_close(
            context,
            query.message,
            fallback_text="Екран профілю закрито через бездіяльність.",
        )


async def profile_stats_page(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    await query.answer()

    data = query.data or ""
    if not data.startswith(CB_STATS_PAGE):
        return

    try:
        page = int(data.split(":")[-1])
    except Exception:
        page = 1

    user = query.from_user
    if page == 1:
        await profile_stats_open(update, context)
        return

    mems = await _mems_stats_for_user(user.id)

    # "Мандаринка" — беремо з профілю (user_data)
    prof = await get_user_profile(user.id)
    mandarin_eaten = int(prof.get("mandarin_eaten") or 0)
    mandarin_wins = int(prof.get("mandarin_duel_wins") or 0)


    footer = "Мур... Грай далі, але не забувай про молитву ✝️"

    text = (
        "<b>🐾 Статистика Мемчиків:</b>\n\n"
        f"🎮 <i><b>Всього зіграно ігор:</b></i> {mems.get('total_games', 0)}\n"
        f"🦾 <i><b>Всього балів:</b></i> {mems.get('total_points', 0)}\n"
        f"🏆 <i><b>Всього перемог:</b></i> {mems.get('total_wins', 0)}\n\n"
        f"{_blockquote(footer)}"
    )

    try:
        await query.edit_message_text(
            text=text,
            parse_mode=ParseMode.HTML,
            reply_markup=_stats_keyboard(page=2),
            disable_web_page_preview=True,
        )
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise
    if query.message:
        await _arm_profile_auto_close(
            context,
            query.message,
            fallback_text="Екран профілю закрито через бездіяльність.",
        )


async def profile_back(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    await query.answer()

    user = query.from_user
    prof = await get_user_profile(user.id)
    gender = _safe_text(prof.get("gender"))
    city = _safe_text(prof.get("city"))
    quote_raw = (prof.get("quote") or "").strip()
    quote = html.escape(quote_raw) if quote_raw else "…"
    balance = int(prof.get("balance") or 0)
    mandarin_eaten = int(prof.get("mandarin_eaten") or 0)
    mandarin_wins = int(prof.get("mandarin_duel_wins") or 0)
    ctx = await get_user_addressing(user.id)

    text = _profile_text(ctx, mention(user), gender, city, quote, mandarin_eaten, mandarin_wins, balance)
    try:
        await query.edit_message_text(
            text=text,
            parse_mode=ParseMode.HTML,
            reply_markup=_profile_keyboard(update.effective_chat and update.effective_chat.type == ChatType.PRIVATE),
            disable_web_page_preview=True,
        )
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise
    if query.message:
        await _arm_profile_auto_close(
            context,
            query.message,
            fallback_text="Екран профілю закрито через бездіяльність.",
        )


async def profile_close(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return

    cancel_auto_close(context, PROFILE_AUTO_CLOSE_KEY)
    await query.answer()

    try:
        if query.message:
            await query.message.delete()
            return
    except Exception:
        pass

    try:
        if query.message:
            await query.message.edit_text("Екран профілю закрито.")
    except Exception:
        logger.debug("Не вдалося закрити профіль вручну", exc_info=True)
    if query.message:
        _arm_profile_auto_close(
            context,
            query.message,
            fallback_text="Екран профілю закрито через бездіяльність.",
        )


# ======================
# /editprofile flow
# ======================

def _gender_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("😻 Киця", callback_data=f"{CB_GENDER}Киця"),
                InlineKeyboardButton("😼 Кіт", callback_data=f"{CB_GENDER}Кіт"),
            ],
            [
                InlineKeyboardButton("🌿 Інше", callback_data=f"{CB_GENDER}інше"),
                InlineKeyboardButton("🗑 Прибрати", callback_data=f"{CB_GENDER}"),
            ],
            [
                InlineKeyboardButton("Пропустити", callback_data=CB_SKIP_GENDER),
                InlineKeyboardButton("Скасувати", callback_data=CB_CANCEL),
            ],
        ]
    )


def _text_step_keyboard(skip_cb: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("Пропустити", callback_data=skip_cb), InlineKeyboardButton("Скасувати", callback_data=CB_CANCEL)]]
    )


def _clean_city(city: str) -> str:
    c = " ".join((city or "").strip().split())
    return c[:32]


def _clean_quote(quote: str) -> str:
    q = (quote or "").strip()
    # зберігаємо як plain text; екрануємо на відображенні
    return q[:220]


async def editprofile_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user = update.effective_user
    if not user:
        return ConversationHandler.END

    await ensure_user_data(user.id, user.username, user.first_name, user.last_name)

    chat = update.effective_chat
    if chat and chat.type != ChatType.PRIVATE:
        link = await _pm_edit_link(context, "editprofile")
        text = "Пиши мені в ПП для редагування профілю 😼"
        if link:
            text += f"\n\n<a href=\"{html.escape(link)}\">Відкрити ПП</a>"
        await update.effective_message.reply_text(
            text,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
        return ConversationHandler.END

    # Приватний чат — стартуємо FSM
    await context.bot.send_message(
        chat_id=user.id,
        text="✏️ Редагуємо профіль.\n\n👤 Стать:",
        reply_markup=_gender_keyboard(),
        parse_mode=ParseMode.HTML,
    )
    return EP_GENDER


async def editprofile_start_from_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Entry point з кнопок у профілі (callback). Працює ТІЛЬКИ в ПП."""
    query = update.callback_query
    if not query:
        return ConversationHandler.END

    msg_chat = query.message.chat if query.message else None
    if msg_chat and msg_chat.type != ChatType.PRIVATE:
        link = await _pm_edit_link(context, "editprofile")
        await query.answer("Пиши мені в ПП для редагування профілю 😼", show_alert=True)
        if link:
            await context.bot.send_message(
                chat_id=msg_chat.id,
                text=f"Пиши мені в ПП для редагування профілю 😼\n\n<a href=\"{html.escape(link)}\">Відкрити ПП</a>",
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
        return ConversationHandler.END

    await query.answer()

    user = query.from_user
    await ensure_user_data(user.id, user.username, user.first_name, user.last_name)

    # Який саме крок просили
    data = query.data or ""
    desired_state = EP_GENDER
    text = "✏️ Редагуємо профіль.\n\n👤 Стать:"
    markup = _gender_keyboard()
    if data == CB_EDIT_CITY:
        desired_state = EP_CITY
        text = "🌃 Місто?"
        markup = _text_step_keyboard(CB_SKIP_CITY)
    elif data == CB_EDIT_QUOTE:
        desired_state = EP_QUOTE
        text = "💬 Цитата?"
        markup = _text_step_keyboard(CB_SKIP_QUOTE)

    await context.bot.send_message(
        chat_id=user.id,
        text=text,
        reply_markup=markup,
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )
    return desired_state


async def editprofile_gender_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if not query:
        return EP_GENDER
    await query.answer()

    data = query.data or ""
    gender: Optional[str] = None
    if data == CB_SKIP_GENDER:
        gender = None
    elif data.startswith(CB_GENDER):
        gender = data.replace(CB_GENDER, "", 1).strip() or None

    if (query.data or "") == CB_CANCEL:
        await query.edit_message_text("Ок. Не чіпаю 😼")
        return ConversationHandler.END

    if gender is not None:
        # "" => прибрати
        await update_user_profile(query.from_user.id, gender=gender)

    await query.edit_message_text(
        "🌃 Місто?",
        reply_markup=_text_step_keyboard(CB_SKIP_CITY),
        parse_mode=ParseMode.HTML,
    )
    return EP_CITY


async def editprofile_city(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user = update.effective_user
    if not user or not update.effective_message:
        return EP_CITY
    city = (update.effective_message.text or "")
    if city.strip() in {"-", ""} or city.strip().lower() in {"skip", "пропустити"}:
        city = ""
    city = _clean_city(city)
    await update_user_profile(user.id, city=city)
    await update.effective_message.reply_text("💬 Цитата?", reply_markup=_text_step_keyboard(CB_SKIP_QUOTE))
    return EP_QUOTE


async def editprofile_city_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if not query:
        return EP_CITY
    await query.answer()
    if (query.data or "") == CB_CANCEL:
        await query.edit_message_text("Ок. Не чіпаю 😼")
        return ConversationHandler.END
    if (query.data or "") == CB_SKIP_CITY:
        await query.edit_message_text("💬 Цитата?", reply_markup=_text_step_keyboard(CB_SKIP_QUOTE), parse_mode=ParseMode.HTML)
        return EP_QUOTE
    return EP_CITY


async def editprofile_quote(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user = update.effective_user
    if not user or not update.effective_message:
        return EP_QUOTE
    quote = (update.effective_message.text or "")
    if quote.strip() in {"-", ""} or quote.strip().lower() in {"skip", "пропустити"}:
        quote = ""
    quote = _clean_quote(quote)
    await update_user_profile(user.id, quote=quote)

    # показ оновленого профілю в ПП
    prof = await get_user_profile(user.id)
    gender = _safe_text(prof.get("gender"))
    city = _safe_text(prof.get("city"))
    quote_raw = (prof.get("quote") or "").strip()
    balance = int(prof.get("balance") or 0)
    mandarin_eaten = int(prof.get("mandarin_eaten") or 0)
    mandarin_wins = int(prof.get("mandarin_duel_wins") or 0)
    ctx = await get_user_addressing(user.id)
    text = _profile_text(ctx, mention(user), gender, city, quote_raw, mandarin_eaten, mandarin_wins, balance)
    await update.effective_message.reply_text("Збережено 😼")
    await update.effective_message.reply_text(
        text=text,
        parse_mode=ParseMode.HTML,
        reply_markup=_profile_keyboard(update.effective_chat and update.effective_chat.type == ChatType.PRIVATE),
        disable_web_page_preview=True,
    )
    return ConversationHandler.END


async def editprofile_quote_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if not query:
        return EP_QUOTE
    await query.answer()
    if (query.data or "") == CB_CANCEL:
        await query.edit_message_text("Ок. Не чіпаю 😼")
        return ConversationHandler.END
    if (query.data or "") == CB_SKIP_QUOTE:
        # завершуємо без зміни, але показуємо профіль
        await query.edit_message_text("Збережено 😼")
        try:
            prof = await get_user_profile(query.from_user.id)
            gender = _safe_text(prof.get("gender"))
            city = _safe_text(prof.get("city"))
            quote_raw = (prof.get("quote") or "").strip()
            balance = int(prof.get("balance") or 0)
            mandarin_eaten = int(prof.get("mandarin_eaten") or 0)
            mandarin_wins = int(prof.get("mandarin_duel_wins") or 0)
            ctx = await get_user_addressing(query.from_user.id)
            text = _profile_text(
                ctx,
                mention(query.from_user),
                gender,
                city,
                quote_raw,
                mandarin_eaten,
                mandarin_wins,
                balance,
            )
            await context.bot.send_message(
                chat_id=query.from_user.id,
                text=text,
                parse_mode=ParseMode.HTML,
                reply_markup=_profile_keyboard(update.effective_chat and update.effective_chat.type == ChatType.PRIVATE),
                disable_web_page_preview=True,
            )
        except Exception:
            pass
        return ConversationHandler.END
    return EP_QUOTE


async def editprofile_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.effective_message:
        await update.effective_message.reply_text("Ок. Не чіпаю 😼")
    return ConversationHandler.END


def register_profile_handlers(application) -> None:
    # /profile
    application.add_handler(CommandHandler(["profile", "me", "myprofile"], profile_command))
    application.add_handler(
    MessageHandler(
        filters.TEXT & filters.Regex(r"^(профіль|мій профіль)$"),
        profile_command
    )
)
    application.add_handler(
    MessageHandler(
        filters.TEXT & filters.Regex(r"^(змінити профіль)$"),
        editprofile_start
    )
)
    # profile stats callbacks
    application.add_handler(CallbackQueryHandler(profile_stats_open, pattern=f"^{CB_STATS_OPEN}$"))
    application.add_handler(CallbackQueryHandler(profile_stats_page, pattern=f"^{CB_STATS_PAGE}"))
    application.add_handler(CallbackQueryHandler(profile_back, pattern=f"^{CB_BACK_TO_PROFILE}$"))
    application.add_handler(CallbackQueryHandler(profile_close, pattern=f"^{CB_PROFILE_CLOSE}$"))

    # /editprofile + кнопки з профілю
    conv = ConversationHandler(
        entry_points=[
            CommandHandler(["editprofile"], editprofile_start),
            CallbackQueryHandler(editprofile_start_from_button, pattern=f"^({CB_EDIT_START}|{CB_EDIT_GENDER}|{CB_EDIT_CITY}|{CB_EDIT_QUOTE})$")
        ],
        states={
            EP_GENDER: [
                CallbackQueryHandler(editprofile_gender_cb, pattern=f"^({CB_GENDER}.*|{CB_SKIP_GENDER}|{CB_CANCEL})$")
            ],
            EP_CITY: [
                CallbackQueryHandler(editprofile_city_cb, pattern=f"^({CB_SKIP_CITY}|{CB_CANCEL})$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, editprofile_city),
            ],
            EP_QUOTE: [
                CallbackQueryHandler(editprofile_quote_cb, pattern=f"^({CB_SKIP_QUOTE}|{CB_CANCEL})$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, editprofile_quote),
            ],
        },
        fallbacks=[CommandHandler(["cancel"], editprofile_cancel)],
        per_user=True,
        per_chat=False,
        name="editprofile_conv",
        persistent=False,
    )
    application.add_handler(conv)