# tops_menu_handlers.py
# -*- coding: utf-8 -*-
"""Єдиний вхід у топи/лідерборди: /top, /topchat, /top_chat, /leaderboard, 'топ', 'топ чат'.

Правила:
- команди не показують статистику одразу
- спочатку inline-меню вибору гри (UX як /newgame)
- після вибору показуємо або глобальний топ, або топ цього чату
"""

import asyncio
import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.constants import ParseMode
from telegram.ext import (
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

from bot.utils.utils import (
    cancel_auto_close,
    set_auto_close_payload,
    start_auto_close,
)

from bot.core.database import DB_PATH

logger = logging.getLogger(__name__)

CB_PREFIX = "tops:"  # tops:<scope>:<game> або tops:back:<scope>
SCOPE_GLOBAL = "global"
SCOPE_CHAT = "chat"

GAME_MEMS = "mems"
GAME_TTT = "ttt"

TOPS_AUTO_CLOSE_KEY = "tops_menu"


def _choose_game_keyboard(scope: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("😼 Мемчики та котики", callback_data=f"{CB_PREFIX}{scope}:{GAME_MEMS}:0")],
            [InlineKeyboardButton("❌⭕ Хрестики-Нулики", callback_data=f"{CB_PREFIX}{scope}:{GAME_TTT}:0")],
        ]
    )


def _back_keyboard(scope: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data=f"{CB_PREFIX}back:{scope}")]])

async def _arm_tops_auto_close(context: ContextTypes.DEFAULT_TYPE, message) -> None:
    if not message:
        return
    cancel_auto_close(context, TOPS_AUTO_CLOSE_KEY)
    set_auto_close_payload(
        context,
        TOPS_AUTO_CLOSE_KEY,
        chat_id=message.chat_id,
        message_id=message.message_id,
        fallback_text="Екран топів закрито через бездіяльність.",
    )
    # Check if auto_delete_actions is enabled
    from bot.core.database import get_chat_settings
    settings = await get_chat_settings(message.chat_id)
    if settings.get('auto_delete_actions', 0) == 1:
        start_auto_close(context, TOPS_AUTO_CLOSE_KEY, timeout=420)  # 7 minutes


async def _send_or_edit(query, context: ContextTypes.DEFAULT_TYPE, text: str, reply_markup: InlineKeyboardMarkup):
    try:
        await query.message.edit_text(text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)
    except Exception:
        await context.bot.send_message(query.message.chat.id, text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)


async def _read_json_file(path: str) -> Dict[str, Any]:
    p = Path(path)

    def _load() -> Dict[str, Any]:
        if not p.exists():
            return {}
        try:
            with p.open("r", encoding="utf-8") as f:
                return json.load(f) or {}
        except Exception:
            return {}

    return await asyncio.to_thread(_load)


async def _ttt_top(scope: str, chat_id: Optional[int], limit: int = 10, offset: int = 0) -> tuple[List[Dict[str, Any]], bool]:
    """Топ по хрестиках-нуликах з існуючої таблиці game_stats (без змін БД). Returns (rows, has_more)"""
    import aiosqlite

    where = "WHERE gs.game_name = ?"
    params: List[Any] = ["tic_tac_toe"]
    if scope == SCOPE_CHAT and chat_id:
        where += " AND gs.chat_id = ?"
        params.append(chat_id)

    sql = f"""
        SELECT
            gs.user_id as user_id,
            COALESCE(ud.first_name, ud.username, 'Unknown') as name,
            SUM(gs.wins_vs_human) as wins_vs_human,
            SUM(gs.wins_vs_bot) as wins_vs_bot,
            SUM(gs.wins + gs.losses + gs.draws) as total_games
        FROM game_stats gs
        LEFT JOIN user_data ud ON ud.user_id = gs.user_id
        {where}
        GROUP BY gs.user_id
        ORDER BY wins_vs_human DESC, wins_vs_bot DESC, total_games DESC
        LIMIT ? OFFSET ?
    """
    params.extend([limit + 1, offset])  # +1 to check has_more

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(sql, tuple(params))
        rows = await cur.fetchall()

    result: List[Dict[str, Any]] = []
    for r in rows[:limit]:  # take only limit
        result.append(
            {
                "user_id": r["user_id"],
                "name": r["name"] or "Unknown",
                "wins_vs_human": int(r["wins_vs_human"] or 0),
                "wins_vs_bot": int(r["wins_vs_bot"] or 0),
                "total_games": int(r["total_games"] or 0),
            }
        )
    has_more = len(rows) > limit
    return result, has_more


async def _mems_top_global(chat_id: Optional[int] = None, limit: int = 10, offset: int = 0) -> tuple[List[Dict[str, Any]], bool]:
    """Топ мемчиків з бази даних. Returns (rows, has_more)"""
    from bot.core.database import mems_get_top
    all_rows = await mems_get_top(chat_id=chat_id, limit=1000)  # large limit
    rows = all_rows[offset:offset + limit]
    has_more = len(all_rows) > offset + limit
    return rows, has_more


def _rank_icon(i: int) -> str:
    medals = ["👑", "🥈", "🥉"]
    return medals[i] if i < 3 else "😼"


def _escape(name: str) -> str:
    return (
        str(name)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


async def top_entry_global(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    if not chat:
        return
    sent = await context.bot.send_message(
        chat.id,
        "🏆 <b>ВИБЕРИ ГРУ ДЛЯ ТОПУ</b> 🏆\n\n"
        "📊 Подивісь найкращих гравців!",
        reply_markup=_choose_game_keyboard(SCOPE_GLOBAL),
        parse_mode=ParseMode.HTML,
    )
    await _arm_tops_auto_close(context, sent)


async def top_entry_chat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    if not chat:
        return

    if chat.type == "private":
        await context.bot.send_message(chat.id, "🏘 Топ чату працює тільки в групах �")
        return

    sent = await context.bot.send_message(
        chat.id,
        "🏆 <b>ВИБЕРИ ГРУ ДЛЯ ТОПУ ЧАТУ</b> 🏆\n\n"
        "📊 Подивісь найкращих гравців цього чату!",
        reply_markup=_choose_game_keyboard(SCOPE_CHAT),
        parse_mode=ParseMode.HTML,
    )
    await _arm_tops_auto_close(context, sent)


async def tops_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.message:
        return

    await query.answer()

    data = query.data or ""
    if not data.startswith(CB_PREFIX):
        return

    payload = data[len(CB_PREFIX):]  # <scope>:<game>[:<page>] | back:<scope>

    if payload.startswith("back:"):
        scope = payload.split(":", 1)[1] if ":" in payload else SCOPE_GLOBAL
        await _send_or_edit(query, context, "🏆 <b>ВИБЕРИ ГРУ ДЛЯ ТОПУ</b> 🏆\n\n📊 Подивісь найкращих гравців!", _choose_game_keyboard(scope))
        await _arm_tops_auto_close(context, query.message)
        return

    parts = payload.split(":")
    if len(parts) < 2:
        await _send_or_edit(query, context, "😿 <b>Помилка!</b>\n\nНе зрозуміла команда. Спробуй ще раз.", _back_keyboard(SCOPE_GLOBAL))
        return

    scope = parts[0]
    game = parts[1]
    page = int(parts[2]) if len(parts) > 2 else 0

    chat_id = query.message.chat.id
    is_chat_scope = scope == SCOPE_CHAT

    # --- MEMS ---
    if game == GAME_MEMS:
        if is_chat_scope:
            rows, has_more = await _mems_top_global(chat_id=chat_id, limit=10, offset=page*10)
            title = "🏆 <b>ТОП МЕМЧИКІВ ЧАТУ</b> 🏆"
            limit_used = 10
        else:
            rows, _ = await _mems_top_global(limit=7, offset=0)  # global top 7, no pagination
            title = "🏆 <b>ТОП МЕМЧИКІВ</b> 🏆"
            has_more = False
            limit_used = 7
        
        if not rows:
            await _send_or_edit(query, context, f"{title}\n\n😴 <i>Поки що ніхто не грав...</i>\n\nСпробуй сам: /newgame", _back_keyboard(scope))
            return

        lines = [f"{title}\n"]
        
        for i, r in enumerate(rows):
            icon = _rank_icon(i + page * limit_used)  # adjust rank for page
            safe_name = _escape(r["name"])
            total_score = r['total_score']
            wins = r['wins']
            games = r['games']
            
            # Розраховуємо відсоток виграшів
            win_rate = f"{wins/games*100:.1f}%" if games > 0 else "0%"
            
            lines.append(f"{icon} <b>{safe_name}</b>")
            lines.append(f"   🦾 {total_score} балів  🏆 {wins} виграшів  🎮 {games} ігор  📈 {win_rate}")
            lines.append("")  # Порожній рядок між гравцями
        
        scope_text = "цього чату" if is_chat_scope else "всього бота"
        lines.append(f"💡 <i>Топ {scope_text}</i>")

        # Pagination buttons
        keyboard = []
        if page > 0:
            keyboard.append(InlineKeyboardButton("⬅️ Попередня", callback_data=f"{CB_PREFIX}{scope}:{game}:{page-1}"))
        if has_more:
            keyboard.append(InlineKeyboardButton("Наступна ➡️", callback_data=f"{CB_PREFIX}{scope}:{game}:{page+1}"))
        if keyboard:
            keyboard.append(InlineKeyboardButton("⬅️ Назад", callback_data=f"{CB_PREFIX}back:{scope}"))
            reply_markup = InlineKeyboardMarkup([keyboard])
        else:
            reply_markup = _back_keyboard(scope)

        await _send_or_edit(query, context, "\n".join(lines), reply_markup)
        await _arm_tops_auto_close(context, query.message)
        return

    # --- TTT ---
    if game == GAME_TTT:
        if is_chat_scope and query.message.chat.type == "private":
            await _send_or_edit(query, context, "🏘 Топ чату працює тільки в групах 😼", _choose_game_keyboard(SCOPE_GLOBAL))
            return

        if is_chat_scope:
            rows, has_more = await _ttt_top(scope=scope, chat_id=chat_id, limit=10, offset=page*10)
            title = "🏘 <b>Хрестики-Нулики — топ чату</b>"
            limit_used = 10
        else:
            rows, _ = await _ttt_top(scope=scope, chat_id=None, limit=7, offset=0)  # global top 7
            title = "🌍 <b>Хрестики-Нулики — глобальний топ</b>"
            has_more = False
            limit_used = 7

        if not rows:
            scope_emoji = "🏘" if is_chat_scope else "🌍"
            await _send_or_edit(query, context, f"{scope_emoji} <b>ТОП ХРЕСТИКІВ-НУЛИКІВ</b> {scope_emoji}\n\n😴 <i>Поки що ніхто не грав...</i>\n\nСпробуй сам: /ttt", _back_keyboard(scope))
            return

        scope_emoji = "🏘" if is_chat_scope else "🌍"
        lines = [f"{scope_emoji} <b>ТОП ХРЕСТИКІВ-НУЛИКІВ</b> {scope_emoji}\n"]
        
        for i, r in enumerate(rows):
            icon = _rank_icon(i + page * limit_used)
            safe_name = _escape(r["name"])
            wins_human = r['wins_vs_human']
            wins_bot = r['wins_vs_bot']
            total_games = r['total_games']
            
            # Розраховуємо відсоток виграшів
            win_rate = f"{(wins_human + wins_bot)/total_games*100:.1f}%" if total_games > 0 else "0%"
            
            lines.append(f"{icon} <b>{safe_name}</b>")
            lines.append(f"   ⚔️ {wins_human} vs 👤  🤖 {wins_bot} vs 🤖  🎮 {total_games} ігор  📈 {win_rate}")
            lines.append("")  # Порожній рядок між гравцями
        
        scope_text = "цього чату" if is_chat_scope else "всього бота"
        lines.append(f"💡 <i>Топ {scope_text}</i>")

        # Pagination buttons for chat
        if is_chat_scope:
            keyboard = []
            if page > 0:
                keyboard.append(InlineKeyboardButton("⬅️ Попередня", callback_data=f"{CB_PREFIX}{scope}:{game}:{page-1}"))
            if has_more:
                keyboard.append(InlineKeyboardButton("Наступна ➡️", callback_data=f"{CB_PREFIX}{scope}:{game}:{page+1}"))
            if keyboard:
                keyboard.append(InlineKeyboardButton("⬅️ Назад", callback_data=f"{CB_PREFIX}back:{scope}"))
                reply_markup = InlineKeyboardMarkup([keyboard])
            else:
                reply_markup = _back_keyboard(scope)
        else:
            reply_markup = _back_keyboard(scope)

        await _send_or_edit(query, context, "\n".join(lines), reply_markup)
        await _arm_tops_auto_close(context, query.message)
        return

    await _send_or_edit(query, context, "😿 <b>Помилка!</b>\n\nТака гра не знайдена. Спробуй іншу.", _back_keyboard(scope))
    await _arm_tops_auto_close(context, query.message)


def register_tops_menu_handlers(application) -> None:
    # EN/UA команди
    application.add_handler(CommandHandler(["top", "leaderboard"], top_entry_global))
    application.add_handler(CommandHandler(["topchat", "top_chat"], top_entry_chat))

    # Текстові аліаси UA
    application.add_handler(MessageHandler(filters.Regex(r"(?i)^\s*топ\s*$"), top_entry_global))
    application.add_handler(MessageHandler(filters.Regex(r"(?i)^\s*топ\s+чат\s*$"), top_entry_chat))

    # Callback
    application.add_handler(CallbackQueryHandler(tops_callback, pattern=r"^tops:"))
