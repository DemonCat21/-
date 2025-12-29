# tic_tac_toe_refactored.py
"""
Модуль гри 'Хрестики-нулики' (🐾🌿) для Telegram-бота.

Стиль: Чистий, мінімалістичний, легкий.
Вайб: Кошенята (🐾), М'ята (🌿), Монашки (▫️🕊️).
"""

import logging
import random
import html
import asyncio
import math
from telegram import CallbackQuery
from typing import Optional, TYPE_CHECKING
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.error import RetryAfter, BadRequest
from telegram.ext import ContextTypes, CommandHandler, CallbackQueryHandler, MessageHandler, filters

# Уникаємо циклічного імпорту, якщо Application потрібен лише для типізації
if TYPE_CHECKING:
    from telegram.ext import Application

# Імпортуємо необхідні функції з бази даних
# (Переконайтеся, що ці файли існують у вашому проєкті)
try:
    from bot.core.database import (
        update_game_stats, update_user_balance, get_game_stats,
        get_global_game_top, get_chat_game_top,
        get_chat_game_top_count, ensure_user_data
    )
    # --- ДОДАНО: Перевірка прав ---
    from bot.handlers.chat_admin_handlers import is_chat_module_enabled
    # --- (НОВЕ) Імпортуємо функції для отримання іконок ---
    from bot.utils.utils import get_icon
    # --- ---
except ImportError:
    logging.critical("Помилка: Не вдалося імпортувати 'database' або 'chat_admin_handlers'. Переконайтеся, що файли існують.")
    # Створюємо заглушки, щоб код хоча б завантажився
    async def _db_stub(*args, **kwargs):
        logging.warning(f"Викликано заглушку DB. Функція '{kwargs.get('name', 'db')}' не працює.")
        if "get" in kwargs.get("name", ""):
            return [] if "top" in kwargs.get("name", "") else 0
        return
    update_game_stats = lambda *args, **kwargs: _db_stub(name="update_game_stats")
    update_user_balance = lambda *args, **kwargs: _db_stub(name="update_user_balance")
    get_game_stats = lambda *args, **kwargs: _db_stub(name="get_game_stats")
    get_global_game_top = lambda *args, **kwargs: _db_stub(name="get_global_game_top")
    get_chat_game_top = lambda *args, **kwargs: _db_stub(name="get_chat_game_top")
    get_chat_game_top_count = lambda *args, **kwargs: _db_stub(name="get_chat_game_top_count")
    ensure_user_data = lambda *args, **kwargs: _db_stub(name="ensure_user_data")
    async def is_chat_module_enabled(*args, **kwargs):
        logging.warning("Викликано заглушку is_chat_module_enabled. Перевірка прав не працює.")
        return True

logger = logging.getLogger(__name__)

# ======================================================================
# РОЗДІЛ 1: СТИЛЬ ТА НАЛАШТУВАННЯ
# ======================================================================

async def get_style_icons() -> dict:
    """
    Отримує іконки стилю для поточної теми.
    Повертає словник з динамічними іконками для гри.
    """
    return {
        "PLAYER_X": await get_icon("icon_player_x"),
        "PLAYER_O": await get_icon("icon_player_o"),
        "EMPTY_CELL": await get_icon("icon_empty"),
        "E_MEDALS": ["🥇", "🥈", "🥉"],
    }

# (ЛИШАЄМО СТАРИЙ КЛАС для зворотної сумісності, але його переважатиме динамічна версія)
class Style:
    """Константи для іконок, що створюють настрій. (ЗАСТАРІЛО - користуйте get_style_icons())"""
    # 🐾 (лапка) та 🌿 (м'ята) для гравців. ▫️ (простота) для порожніх клітинок.
    PLAYER_X, PLAYER_O, EMPTY_CELL = "✝️", "🧶", "▫️"

    # 🕊️ (мир) для дуелі, ✨ (магія) для перемоги, 🤝 (злагода) для нічиєї.
    E_DUEL, E_TURN, E_WIN, E_DRAW, E_SCORE, E_GLOBAL, E_INFO, E_STOP, E_ERROR, E_SETUP, E_CANCEL, E_TIMEOUT, E_REMATCH, E_BOT_GAME = (
        "🕊️", "⏳", "✨", "🤝", "📊", "🌍", "ℹ️", "🛑", "⚠️", "⚙️", "✖️", "⌛", "🔂", "🤖"
    )
    E_MEDALS = ["🥇", "🥈", "🥉"]

# Налаштування гри
GAME_PRESETS = {
    "3x3": {"size": 3, "win": 3, "name": "3x3"},
    "4x4": {"size": 4, "win": 4, "name": "4x4"},
    "5x5": {"size": 5, "win": 4, "name": "5x5"},
    "6x6": {"size": 6, "win": 4, "name": "6x6"},
    "10x10": {"size": 10, "win": 5, "name": "10x10"},
}

# Глобальні константи
INVITATION_TIMEOUT_SECONDS = 90
TIC_TAC_TOE_WIN_REWARD = 20  # Кількість м'яток 🌿
PLAYERS_PER_PAGE = 10       # Для пагінації лідерборду

# ======================================================================
# РОЗДІЛ 2: ЛОГІКА ГРИ ТА UI (ЧИСТІ ФУНКЦІЇ / ДОПОМІЖНІ)
# ======================================================================

def create_keyboard(board: list, action_prefix: str = "move") -> InlineKeyboardMarkup:
    """
    Створює інлайн-клавіатуру для ігрової дошки.
    """
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(cell, callback_data=f"{action_prefix}_{i}_{j}") for j, cell in enumerate(row)] for i, row in enumerate(board)]
    )

def create_rematch_keyboard(p1_id: int, p2_id: int, mode: str) -> InlineKeyboardMarkup:
    """
    Створює клавіатуру для рематчу, зміни режиму та закриття гри.
    """
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(f"{Style.E_REMATCH} Зіграти ще", callback_data=f"rematch_{p1_id}_{p2_id}_{mode}"),
                InlineKeyboardButton(f"{Style.E_SETUP} Змінити режим", callback_data=f"change_mode_{p1_id}_{p2_id}"),
            ],
            [
                InlineKeyboardButton(f"{Style.E_CANCEL} Закрити", callback_data="cancel_rematch"),
            ]
        ]
    )

def _create_mode_selection_keyboard(p1_id: int, p2_id: int) -> InlineKeyboardMarkup:
    """(Helper) Створює клавіатуру вибору режиму гри для p1 та p2."""
    keyboard, row = [], []
    for mode, config in GAME_PRESETS.items():
        row.append(InlineKeyboardButton(text=config["name"], callback_data=f"select_{mode}_{p1_id}_{p2_id}"))
        if len(row) == 2:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)
    keyboard.append([InlineKeyboardButton(f"{Style.E_CANCEL} Скасувати", callback_data=f"cancel_invite_{p1_id}_{p2_id}")])
    return InlineKeyboardMarkup(keyboard)

def check_winner(board: list, last_move: tuple[int, int], symbol: str, board_size: int, win_condition: int) -> Optional[str]:
    """
    Перевіряє, чи є переможець після останнього ходу.
    Повертає 'symbol' при перемозі, 'нічия' при нічиїй, або None.
    """
    if symbol not in [Style.PLAYER_X, Style.PLAYER_O]:
        logger.warning(f"check_winner: Недійсний символ '{symbol}'.")
        return None

    r, c = last_move
    directions = [(0, 1), (1, 0), (1, 1), (1, -1)]  # Горизонталь, Вертикаль, 2 Діагоналі

    for dr, dc in directions:
        count = 1
        # Перевірка в одному напрямку
        for i in range(1, win_condition):
            nr, nc = r + dr * i, c + dc * i
            if 0 <= nr < board_size and 0 <= nc < board_size and board[nr][nc] == symbol:
                count += 1
            else:
                break
        # Перевірка в протилежному напрямку
        for i in range(1, win_condition):
            nr, nc = r - dr * i, c - dc * i
            if 0 <= nr < board_size and 0 <= nc < board_size and board[nr][nc] == symbol:
                count += 1
            else:
                break

        if count >= win_condition:
            logger.debug(f"Переможець: {symbol} на {last_move}. Умова: {count}/{win_condition}.")
            return symbol

    # Перевірка на нічию (вся дошка заповнена)
    if all(cell != Style.EMPTY_CELL for row in board for cell in row):
        logger.debug("Нічия: дошка заповнена.")
        return "нічия"

    return None

# ======================================================================
# РОЗДІЛ 3: ЛОГІКА ШТУЧНОГО ІНТЕЛЕКТУ (ЧИСТІ ФУНКЦІЇ)
# ======================================================================

def _check_line_length(board: list, start_move: tuple[int, int], symbol: str, board_size: int) -> int:
    """
    Допоміжна функція для АІ.
    Перевіряє максимальну довжину лінії (включно з `start_move`) у всіх напрямках
    для символу, ЯКИЙ ВЖЕ РОЗМІЩЕНО на `start_move`.
    """
    r, c = start_move
    max_length = 0
    directions = [(0, 1), (1, 0), (1, 1), (1, -1)]

    for dr, dc in directions:
        current_length = 0
        # Перевірка в одному напрямку (включно зі стартовою точкою)
        for i in range(board_size):
            nr, nc = r + dr * i, c + dc * i
            if 0 <= nr < board_size and 0 <= nc < board_size and board[nr][nc] == symbol:
                current_length += 1
            else:
                break
        # Перевірка в протилежному напрямку (не включно зі стартовою, бо вже порахували)
        for i in range(1, board_size):
            nr, nc = r - dr * i, c - dc * i
            if 0 <= nr < board_size and 0 <= nc < board_size and board[nr][nc] == symbol:
                current_length += 1
            else:
                break
        max_length = max(max_length, current_length)
    return max_length

def find_best_move(board: list, bot_symbol: str, player_symbol: str, board_size: int, win_condition: int) -> Optional[tuple[int, int]]:
    """
    Знаходить найкращий хід для бота, використовуючи ієрархію пріоритетів.
    1. Негайний виграш.
    2. Блокування негайного виграшу суперника.
    3. Створення загрози (N-1).
    4. Блокування загрози (N-1).
    5. Центр.
    6. Випадковий хід.
    """
    empty_cells = [(r, c) for r in range(board_size) for c in range(board_size) if board[r][c] == Style.EMPTY_CELL]
    if not empty_cells:
        return None

    # 1. Негайний виграш
    for r, c in empty_cells:
        board[r][c] = bot_symbol
        if check_winner(board, (r, c), bot_symbol, board_size, win_condition) == bot_symbol:
            board[r][c] = Style.EMPTY_CELL  # Відкат
            logger.debug(f"АІ: (1) Знайшов виграшний хід на ({r}, {c}).")
            return r, c
        board[r][c] = Style.EMPTY_CELL

    # 2. Блокування негайного виграшу суперника
    for r, c in empty_cells:
        board[r][c] = player_symbol
        if check_winner(board, (r, c), player_symbol, board_size, win_condition) == player_symbol:
            board[r][c] = Style.EMPTY_CELL
            logger.debug(f"АІ: (2) Знайшов блокуючий хід на ({r}, {c}).")
            return r, c
        board[r][c] = Style.EMPTY_CELL

    # 3. Створення загрози (N-1 у ряд)
    # 4. Блокування загрози (N-1 у ряд)
    bot_threat_moves = []
    player_threat_moves = []

    for r, c in empty_cells:
        # Перевірка загрози для бота
        board[r][c] = bot_symbol
        if _check_line_length(board, (r, c), bot_symbol, board_size) >= win_condition - 1:
            bot_threat_moves.append((r, c))
        board[r][c] = Style.EMPTY_CELL

        # Перевірка загрози для гравця
        board[r][c] = player_symbol
        if _check_line_length(board, (r, c), player_symbol, board_size) >= win_condition - 1:
            player_threat_moves.append((r, c))
        board[r][c] = Style.EMPTY_CELL

    if bot_threat_moves:
        move = random.choice(bot_threat_moves)
        logger.debug(f"АІ: (3) Створення загрози на {move}.")
        return move

    if player_threat_moves:
        move = random.choice(player_threat_moves)
        logger.debug(f"АІ: (4) Блокування загрози на {move}.")
        return move

    # 5. Спроба зайняти центр (або один з центрів)
    centers = []
    if board_size % 2 == 1:
        m = board_size // 2
        if board[m][m] == Style.EMPTY_CELL:
            centers.append((m, m))
    else:
        m1, m2 = board_size // 2 - 1, board_size // 2
        for r_c in [m1, m2]:
            for c_c in [m1, m2]:
                if board[r_c][c_c] == Style.EMPTY_CELL:
                    centers.append((r_c, c_c))

    if centers:
        move = random.choice(centers)
        logger.debug(f"АІ: (5) Зайняв центр на {move}.")
        return move

    # 6. Випадковий хід
    move = random.choice(empty_cells)
    logger.debug(f"АІ: (6) Випадковий хід на {move}.")
    return move

# ======================================================================
# РОЗДІЛ 4: ФОНОВІ ЗАВДАННЯ (JOBS)
# ======================================================================

async def cleanup_invitation(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Видаляє прострочене запрошення на гру."""
    job = context.job
    chat_id, message_id = job.data["chat_id"], job.data["message_id"]

    if context.chat_data.get("invitations", {}).pop(message_id, None):
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=f"{Style.E_TIMEOUT} Час на вибір режиму гри вичерпано. Запрошення скасовано.",
                reply_markup=None,
            )
            logger.info(f"Запрошення {message_id} у чаті {chat_id} прострочено.")
        except Exception as e:
            logger.warning(f"Не вдалося оновити прострочене запрошення {message_id}: {e}")

# ======================================================================
# РОЗДІЛ 5: ОБРОБНИКИ КОМАНД (/newgame, /score, ...)
# ======================================================================

# --- КРОК 2 (уніфікація запуску ігор) ---
# /newgame більше не стартує хрестики-нулики напряму.
# Старт гри відбувається через:
# 1) /newgame → меню → «Хрестики-Нулики» → лобі з кнопками
# 2) !гра (у відповідь на повідомлення) → швидка дуель (старий флоу)

# Час на набір 2 гравців у лобі (після цього лобі скасовується і повідомлення видаляється)
TTT_LOBBY_TIMEOUT_SECONDS = 60


def _ttt_lobby_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("➕ Зайти / Вийти", callback_data="ttt_lobby_join")],
            [InlineKeyboardButton("🔔 Почати", callback_data="ttt_lobby_start")],
            [InlineKeyboardButton(f"{Style.E_CANCEL} Скасувати", callback_data="ttt_lobby_cancel")],
        ]
    )


def _ttt_render_lobby_text(players: dict[int, str]) -> str:
    plist = "\n".join([f"🐾 {mention}" for mention in players.values()]) or "Поки що пусто…"
    return (
        f"{Style.E_SETUP} <b>Хрестики-Нулики</b>\n\n"
        f"Гравці [{len(players)}/2]:\n{plist}\n\n"
        f"<i>Другий гравець натискає «➕ Зайти» — і гра стартує автоматично (відкриється вибір режиму).</i>"
    )



async def _ttt_lobby_to_mode_selection(
    *, chat_id: int, message_id: int, players: dict[int, str], context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Перетворює лобі на вибір режиму (інвайт) і ставить таймаут."""
    # прибираємо job таймауту лобі (воно перетворюється на інвайт)
    try:
        for j in context.job_queue.get_jobs_by_name(f"ttt_lobby_cleanup_{chat_id}_{message_id}"):
            j.schedule_removal()
    except Exception:
        pass

    p1_id, p2_id = list(players.keys())[:2]
    reply_markup = _create_mode_selection_keyboard(p1_id, p2_id)

    # переносимо стан в "invitation" (старий механізм) — щоб не ламати гру
    context.chat_data.setdefault("invitations", {})[message_id] = {"p1_id": p1_id, "p2_id": p2_id}

    # лобі більше не потрібно
    try:
        (context.chat_data.get("ttt_lobbies", {}) or {}).pop(message_id, None)
    except Exception:
        pass

    try:
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=f"{Style.E_SETUP} Оберіть режим гри:",
            reply_markup=reply_markup,
            parse_mode=ParseMode.HTML,
        )
    except Exception:
        pass

    # таймаут на вибір режиму
    context.job_queue.run_once(
        cleanup_invitation,
        INVITATION_TIMEOUT_SECONDS,
        data={"chat_id": chat_id, "message_id": message_id},
        name=f"cleanup_{chat_id}_{message_id}",
    )

async def cleanup_ttt_lobby(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Гасить прострочене лобі хрестиків-нуликів."""
    job = context.job
    chat_id, message_id = job.data["chat_id"], job.data["message_id"]

    # JobQueue callback може не мати context.chat_data (None).
    # Беремо chat_data напряму з application, бо він завжди доступний та стабільний.
    chat_data = {}
    try:
        chat_data = (context.application.chat_data.get(chat_id) or {}) if context.application else {}
    except Exception:
        chat_data = {}

    lobbies = chat_data.get("ttt_lobbies", {})
    lobby = lobbies.pop(message_id, None)
    if not lobby:
        return
    # За вимогою: якщо за відведений час гра не стартує — скасовуємо і видаляємо повідомлення набору.
    try:
        await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception:
        # Якщо вже видалено/немає прав — не падаємо
        pass


def _cancel_jobs_by_prefix(job_queue, prefix: str) -> int:
    """Скасовує всі jobs, name яких починається з prefix. Повертає кількість."""
    cancelled = 0
    try:
        for job in list(job_queue.jobs() or []):
            name = getattr(job, "name", "") or ""
            if name.startswith(prefix):
                try:
                    job.schedule_removal()
                    cancelled += 1
                except Exception:
                    pass
    except Exception:
        pass
    return cancelled


async def stop_all_ttt_in_chat(chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Зупиняє всі активні ХН-об'єкти в чаті (ігри, інвайти, лобі) і чистить джоби.

    Повертає True, якщо щось було зупинено/очищено.
    Ідемпотентно: повторний виклик нічого не ламає.
    """
    stopped_any = False

    # chat_data чату
    chat_data = {}
    try:
        chat_data = context.application.chat_data.get(chat_id) or {}
    except Exception:
        chat_data = {}

    # 1) Лобі
    lobbies = chat_data.get("ttt_lobbies") or {}
    if lobbies:
        for lobby_mid in list(lobbies.keys()):
            try:
                await context.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=int(lobby_mid),
                    text=f"{Style.E_STOP} Гру скасовано.",
                    reply_markup=None,
                    parse_mode=ParseMode.HTML,
                )
            except Exception:
                pass
            # job name: ttt_lobby_cleanup_{chat_id}_{message_id}
            try:
                _cancel_jobs_by_prefix(context.job_queue, f"ttt_lobby_cleanup_{chat_id}_{int(lobby_mid)}")
            except Exception:
                pass

        lobbies.clear()
        stopped_any = True

    # 2) Запрошення/таймаути вибору режиму
    invitations = chat_data.get("invitations") or {}
    if invitations:
        for inv_mid in list(invitations.keys()):
            # job name: cleanup_{chat_id}_{message_id}
            try:
                _cancel_jobs_by_prefix(context.job_queue, f"cleanup_{chat_id}_{int(inv_mid)}")
            except Exception:
                pass
            try:
                await context.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=int(inv_mid),
                    text=f"{Style.E_STOP} Гру скасовано.",
                    reply_markup=None,
                    parse_mode=ParseMode.HTML,
                )
            except Exception:
                pass

        invitations.clear()
        stopped_any = True

    # 3) Активні ігри (словник 'games' за message_id)
    games = chat_data.get("games") or {}
    if games:
        for game_mid in list(games.keys()):
            try:
                await context.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=int(game_mid),
                    text=f"{Style.E_STOP} Гру скасовано.",
                    reply_markup=None,
                    parse_mode=ParseMode.HTML,
                )
            except Exception:
                pass
        games.clear()
        stopped_any = True

    return stopped_any


async def ttt_open_lobby(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Стартує лобі (2 гравці) для хрестиків-нуликів. Викликається з /newgame-меню."""
    chat = update.effective_chat
    user = update.effective_user
    if not chat or not user:
        return

    # --- ПЕРЕВІРКА ПРАВ ---
    if not await is_chat_module_enabled(chat, "games"):
        return
    # --- КІНЕЦЬ ПЕРЕВІРКИ ---

    if chat.type == "private":
        # коротко й без технічного тексту
        try:
            if update.message:
                await update.message.reply_html(f"{Style.E_INFO} Ігри — тільки в групах 😼")
        except Exception:
            pass
        return

    # створюємо лобі
    players = {user.id: user.mention_html()}
    text = _ttt_render_lobby_text(players)

    msg = await context.bot.send_message(
        chat_id=chat.id,
        text=text,
        reply_markup=_ttt_lobby_keyboard(),
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )

    context.chat_data.setdefault("ttt_lobbies", {})[msg.message_id] = {
        "owner_id": user.id,
        "players": {user.id: user.mention_html()},
    }

    # context.job_queue.run_once(  # ВИМКНЕНО таймер лобі
    #     cleanup_ttt_lobby,
    #     TTT_LOBBY_TIMEOUT_SECONDS,
    #     data={"chat_id": chat.id, "message_id": msg.message_id},
    #     name=f"ttt_lobby_cleanup_{chat.id}_{msg.message_id}",
    # )


async def ttt_lobby_join_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.message:
        return
    await query.answer()

    chat_id = query.message.chat_id
    message_id = query.message.message_id
    lobbies = context.chat_data.get("ttt_lobbies", {})
    lobby = lobbies.get(message_id)
    if not lobby:
        await query.answer("Лобі вже закрите.", show_alert=True)
        return

    user = query.from_user
    players: dict[int, str] = lobby.get("players", {})

    if user.id in players:
        players.pop(user.id, None)
        res = "Вийшов."
    else:
        if len(players) >= 2:
            await query.answer("Максимум 2 гравці 😼", show_alert=True)
            return
        if user.is_bot:
            await query.answer("Боти — мимо 😼", show_alert=True)
            return
        players[user.id] = user.mention_html()
        res = "Ти в грі."

    # автостарт: коли набралось 2 гравці — одразу переходимо на вибір режиму
    if len(players) == 2:
        lobby["players"] = players
        await _ttt_lobby_to_mode_selection(chat_id=chat_id, message_id=message_id, players=players, context=context)
        await query.answer("Удвох! Оберіть режим")
        return

    lobby["players"] = players
    try:
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=_ttt_render_lobby_text(players),
            reply_markup=_ttt_lobby_keyboard(),
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
    except Exception:
        pass
    await query.answer(res)


async def ttt_lobby_cancel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.message:
        return
    chat_id = query.message.chat_id
    message_id = query.message.message_id

    lobbies = context.chat_data.get("ttt_lobbies", {})
    lobby = lobbies.get(message_id)
    if not lobby:
        await query.answer("Вже.")
        return

    if query.from_user.id != lobby.get("owner_id"):
        await query.answer("Скасувати може лише автор лобі.", show_alert=True)
        return

    # прибираємо job таймауту для цього лобі
    try:
        for j in context.job_queue.get_jobs_by_name(f"ttt_lobby_cleanup_{chat_id}_{message_id}"):
            j.schedule_removal()
    except Exception:
        pass

    lobbies.pop(message_id, None)
    await query.answer("Скасовано")
    try:
        await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception:
        pass


async def ttt_lobby_start_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.message:
        return
    chat_id = query.message.chat_id
    message_id = query.message.message_id

    lobbies = context.chat_data.get("ttt_lobbies", {})
    lobby = lobbies.get(message_id)
    if not lobby:
        await query.answer("Лобі вже закрите.", show_alert=True)
        return

    players: dict[int, str] = lobby.get("players", {})
    if len(players) != 2:
        await query.answer("Потрібно 2 гравці.", show_alert=True)
        return

    await _ttt_lobby_to_mode_selection(chat_id=chat_id, message_id=message_id, players=players, context=context)
    await query.answer("Оберіть режим")


async def _send_duel_mode_invite(update: Update, context: ContextTypes.DEFAULT_TYPE, player2) -> None:
    """Спільний код старого /newgame: надсилає вибір режиму для дуелі."""
    user = update.effective_user
    chat = update.effective_chat

    if not user or not chat:
        return

    # --- ПЕРЕВІРКА ПРАВ ---
    if not await is_chat_module_enabled(chat, "games"):
        return
    # --- КІНЕЦЬ ПЕРЕВІРКИ ---

    reply_markup = _create_mode_selection_keyboard(user.id, player2.id)
    message = await context.bot.send_message(
        chat_id=chat.id,
        text=f"{Style.E_SETUP} {user.mention_html()} викликає {player2.mention_html()}!\n<b>Оберіть режим гри:</b>",
        reply_markup=reply_markup,
        parse_mode=ParseMode.HTML,
    )

    invitation_context = {"chat_id": message.chat_id, "message_id": message.message_id}
    context.chat_data.setdefault("invitations", {})[message.message_id] = {"p1_id": user.id, "p2_id": player2.id}
    context.job_queue.run_once(
        cleanup_invitation,
        INVITATION_TIMEOUT_SECONDS,
        data=invitation_context,
        name=f"cleanup_{message.chat_id}_{message.message_id}",
    )


async def bang_game_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """!гра — швидка дуель у відповідь на повідомлення (тільки хрестики-нулики)."""
    chat = update.effective_chat
    user = update.effective_user

    if not update.message or not chat or not user:
        return

    # тільки групи
    if chat.type == "private":
        await update.message.reply_html(f"{Style.E_INFO} Тільки в групах 😼")
        return

    # має бути відповідь на повідомлення
    if (not update.message.reply_to_message or
        user.id == update.message.reply_to_message.from_user.id or
        update.message.reply_to_message.from_user.is_bot):
        await update.message.reply_html(
            f"{Style.E_INFO} Дай відповідь на повідомлення друга і напиши <code>!гра</code>."
        )
        return

    try:
        await update.message.delete()
    except Exception:
        pass

    player2 = update.message.reply_to_message.from_user
    await _send_duel_mode_invite(update, context, player2)

async def new_game_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Ініціює дуель з іншим гравцем."""
    user = update.effective_user
    chat = update.effective_chat

    # --- ПЕРЕВІРКА ПРАВ ---
    if not await is_chat_module_enabled(chat, "games"):
        logger.debug(f"Module 'games' (tic_tac_toe) disabled for chat {chat.id}. Ignoring new_game.")
        try:
            await update.message.delete() # Все одно видаляємо
        except Exception:
            pass
        return
    # --- КІНЕЦЬ ПЕРЕВІРКИ ---

    # Перевірка умов для дуелі
    if (chat.type == "private" or
        not update.message.reply_to_message or
        user.id == update.message.reply_to_message.from_user.id or
        update.message.reply_to_message.from_user.is_bot):

        await update.message.reply_html(
            f"{Style.E_INFO} <b>Як грати з другом:</b>\n"
            f"У груповому чаті дайте відповідь на будь-яке повідомлення друга командою <code>/newgame</code>.",
            disable_web_page_preview=True
        )
        logger.info(f"{user.id} некоректно викликав /newgame.")
        return

    try:
        await update.message.delete()
    except Exception as e:
        logger.warning(f"Не вдалося видалити /newgame: {e}")

    player2 = update.message.reply_to_message.from_user
    logger.info(f"{user.id} викликав {player2.id} на дуель.")

    # Використовуємо 'чисту' допоміжну функцію
    reply_markup = _create_mode_selection_keyboard(user.id, player2.id)

    try:
        message = await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=f"{Style.E_SETUP} {user.mention_html()} викликає {player2.mention_html()}!\n"
                 f"<b>Оберіть режим гри:</b>",
            reply_markup=reply_markup,
            parse_mode=ParseMode.HTML,
        )

        # Реєструємо запрошення для подальшого очищення
        invitation_context = {"chat_id": message.chat_id, "message_id": message.message_id}
        context.chat_data.setdefault("invitations", {})[message.message_id] = {"p1_id": user.id, "p2_id": player2.id}
        # context.job_queue.run_once(  # ВИМКНЕНО таймер
        #     cleanup_invitation, INVITATION_TIMEOUT_SECONDS, data=invitation_context, name=f"cleanup_{message.chat_id}_{message.message_id}"
        # )
    except Exception as e:
        logger.error(f"Помилка надсилання запрошення на гру: {e}", exc_info=True)

async def play_with_bot_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Ініціює гру з ботом."""
    user = update.effective_user
    chat = update.effective_chat
    chat_id = chat.id

    # --- ПЕРЕВІРКА ПРАВ ---
    if not await is_chat_module_enabled(chat, "games"):
        logger.debug(f"Module 'games' (tic_tac_toe) disabled for chat {chat.id}. Ignoring play_with_bot.")
        try:
            if update.message:
                await update.message.delete() # Все одно видаляємо
        except Exception:
            pass
        return
    # --- КІНЕЦЬ ПЕРЕВІРКИ ---

    try:
        if update.message:
            await update.message.delete()
    except Exception as e:
        logger.warning(f"Не вдалося видалити /playwithbot: {e}")

    bot_id = context.bot.id
    logger.info(f"{user.id} почав гру з ботом.")

    # Використовуємо 'чисту' допоміжну функцію
    reply_markup = _create_mode_selection_keyboard(user.id, bot_id)

    try:
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"{Style.E_BOT_GAME} {user.mention_html()}, оберіть режим гри проти мене:",
            reply_markup=reply_markup,
            parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        logger.error(f"Помилка надсилання запрошення на гру з ботом: {e}", exc_info=True)

async def stop_game_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Зупиняє активну гру за відповіддю на повідомлення."""
    # Примітка: Ця команда не перевіряється, оскільки зупинка гри
    # має бути дозволена, навіть якщо модуль 'games' вимкнено (щоб прибрати старі ігри).
    user = update.effective_user
    if not update.message.reply_to_message:
        await update.message.reply_html(f"{Style.E_INFO} Щоб зупинити гру, дайте відповідь на її повідомлення цією командою.")
        return

    message_id = update.message.reply_to_message.message_id
    games = context.chat_data.get("games", {})

    if message_id in games:
        game = games[message_id]
        if user.id in [game["player1"]["id"], game["player2"]["id"]]:
            del games[message_id]
            try:
                await context.bot.edit_message_text(
                    chat_id=update.effective_chat.id, message_id=message_id, text=f"{Style.E_STOP} Гру скасовано гравцем.", reply_markup=None
                )
                logger.info(f"Гру {message_id} зупинено {user.id}.")
            except Exception as e:
                logger.error(f"Помилка редагування повідомлення про зупинку гри {message_id}: {e}", exc_info=True)
        else:
            await update.message.reply_text("Лише учасники можуть скасувати свою гру.")
    else:
        await update.message.reply_text("Це повідомлення не є активною грою.")

# --------------------------
# Обробники Лідербордів
# --------------------------

async def score_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Показує топ перемог у чаті (з пагінацією)."""
    # --- ПЕРЕВІРКА ПРАВ ---
    if not await is_chat_module_enabled(update.effective_chat, "games"):
        logger.debug(f"Module 'games' (tic_tac_toe) disabled for chat {update.effective_chat.id}. Ignoring score.")
        return
    # --- КІНЕЦЬ ПЕРЕВІРКИ ---
    await send_chat_leaderboard(update, context, update.effective_chat.id, page_number=0)

async def global_top_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Показує глобальний топ-10 гравців."""
    # --- ПЕРЕВІРКА ПРАВ ---
    if not await is_chat_module_enabled(update.effective_chat, "games"):
        logger.debug(f"Module 'games' (tic_tac_toe) disabled for chat {update.effective_chat.id}. Ignoring globaltop.")
        return
    # --- КІНЕЦЬ ПЕРЕВІРКИ ---
    
    top_players = await get_global_game_top('tic_tac_toe', limit=10)

    if not top_players:
        await update.message.reply_html(f"{Style.E_GLOBAL} Світовий рейтинг 🐾🌿 ще порожній.")
        return

    leaderboard = f"{Style.E_GLOBAL} <b>Світовий рейтинг майстрів 🐾🌿:</b>\n\n"
    medals = Style.E_MEDALS
    for i, player in enumerate(top_players):
        user_mention = f"Гравець (ID: {player['user_id']})"
        try:
            # Намагаємося отримати дані користувача для @mention
            chat_member = await context.bot.get_chat(player['user_id'])
            user_mention = chat_member.mention_html()
        except Exception:
            pass  # Залишаємо ID, якщо не вдалося

        place = medals[i] if i < len(medals) else f"<b>{i+1}.</b>"
        leaderboard += f"{place} {user_mention}: <b>{player['total_wins']}</b> перемог\n"

    await update.message.reply_html(leaderboard, disable_web_page_preview=True)

async def score_command_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обробляє кнопки пагінації для /score."""
    query = update.callback_query
    await query.answer()
    
    # Перевірка прав не потрібна, оскільки це лише пагінація вже
    # існуючого (і перевіреного) повідомлення.

    try:
        parts = query.data.split('_')
        game_name = parts[2]
        chat_id = int(parts[3])
        page_number = int(parts[4])

        if game_name != 'tic_tac_toe': return

        await send_chat_leaderboard(update, context, chat_id, page_number, is_callback=True)
    except (IndexError, ValueError):
        logger.warning(f"Недійсний callback пагінації: {query.data}")

async def send_chat_leaderboard(update: Update, context: ContextTypes.DEFAULT_TYPE, chat_id: int, page_number: int, is_callback: bool = False) -> None:
    """Відправляє або редагує повідомлення з топом гравців чату."""
    game_name = 'tic_tac_toe'
    
    try:
        total_players_count = await get_chat_game_top_count(chat_id, game_name)
        total_pages = math.ceil(total_players_count / PLAYERS_PER_PAGE)
        offset = page_number * PLAYERS_PER_PAGE
        top_players = await get_chat_game_top(chat_id, game_name, PLAYERS_PER_PAGE, offset)

        if not top_players:
            text = f"{Style.E_SCORE} У цьому чаті ще немає статистики 🐾🌿. Будьте першими!"
            keyboard_markup = None
        else:
            leaderboard = f"{Style.E_SCORE} <b>Топ гравців у цьому чаті (Сторінка {page_number + 1}/{total_pages}):</b>\n\n"
            medals = Style.E_MEDALS
            for i, player in enumerate(top_players):
                rank = offset + i + 1
                place = medals[rank - 1] if rank <= len(medals) else f"<b>{rank}.</b>"
                
                user_mention = f"Гравець (ID: {player['user_id']})"
                try:
                    chat_member = await context.bot.get_chat_member(chat_id=chat_id, user_id=player['user_id'])
                    user_mention = chat_member.user.mention_html()
                except Exception:
                    pass # Залишаємо ID, якщо не вдалося

                leaderboard += f"{place} {user_mention}: <b>{player['wins']}</b> перемог\n"
            
            text = leaderboard
            
            keyboard = []
            row_buttons = []
            if page_number > 0:
                row_buttons.append(InlineKeyboardButton("⬅️ Попередня", callback_data=f"score_page_{game_name}_{chat_id}_{page_number - 1}"))
            if page_number < total_pages - 1:
                row_buttons.append(InlineKeyboardButton("Наступна ➡️", callback_data=f"score_page_{game_name}_{chat_id}_{page_number + 1}"))
            if row_buttons:
                keyboard.append(row_buttons)
            keyboard_markup = InlineKeyboardMarkup(keyboard) if keyboard else None

        # Використовуємо правильний об'єкт для відповіді
        if is_callback:
            await update.callback_query.edit_message_text(text=text, reply_markup=keyboard_markup, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
        else:
            await update.message.reply_html(text, reply_markup=keyboard_markup, disable_web_page_preview=True)

    except BadRequest as e:
        if "Message is not modified" not in str(e):
            logger.error(f"Помилка відправки топу чату {chat_id}: {e}", exc_info=True)
            if is_callback: await update.callback_query.answer("Ой, сталася помилка 😿", show_alert=True)
    except Exception as e:
        logger.error(f"Неочікувана помилка топу чату {chat_id}: {e}", exc_info=True)
        if is_callback: await update.callback_query.answer("Ой, сталася помилка 😿", show_alert=True)


# ======================================================================
# РОЗДІЛ 6: ГОЛОВНИЙ ОБРОБНИК CALLBACK (РОУТЕР)
# ======================================================================

async def handle_tic_tac_toe_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Головний роутер для всіх callback-запитів гри 'Хрестики-нулики'.
    Розпізнає дію та викликає відповідний допоміжний обробник.
    """
    query = update.callback_query
    if not query or not query.data:
        logger.warning("Отримано callback без даних.")
        return

    # Перевірка прав не потрібна, оскільки це callback-и
    # для вже створених ігор/запрошень. Логіка вимкнення модуля
    # має бути лише на *початку* гри (/newgame, /playwithbot).

    await query.answer()
    user = query.from_user
    bot_id = context.bot.id
    data = query.data
    
    logger.info(f"Callback від {user.id}: {data}")

    try:
        # ------------------
        # Маршрут: Вибір режиму гри (початок)
        # ------------------
        if data.startswith("select_"):
            parts = data.split("_")
            mode, p1_id, p2_id = parts[1], int(parts[2]), int(parts[3])
            
            # Перевірка, чи може користувач вибрати режим
            if user.id != p1_id and user.id != p2_id:
                await query.answer("Ви не учасник цієї гри. Лише гравці можуть обрати режим.", show_alert=True)
                return
            
            await _handle_select_mode(query, context, mode, p1_id, p2_id, bot_id)

        # ------------------
        # Маршрут: Скасування запрошення
        # ------------------
        elif data.startswith("cancel_invite_"):
            parts = data.split("_")
            p1_id, p2_id = int(parts[2]), int(parts[3])
            
            if user.id != p1_id and user.id != p2_id:
                await query.answer("Ви не можете скасувати чуже запрошення.", show_alert=True)
                return
                
            await _handle_cancel_invite(query, context)

        # ------------------
        # Маршрут: Закриття гри (після завершення)
        # ------------------
        elif data == "cancel_rematch":
            await _handle_cancel_rematch(query, context)

        # ------------------
        # Маршрут: Рематч (той самий режим)
        # ------------------
        elif data.startswith("rematch_"):
            parts = data.split("_")
            p1_id, p2_id, mode = int(parts[1]), int(parts[2]), parts[3]
            
            if user.id not in [p1_id, p2_id]:
                await query.answer("Ви не були учасником цієї гри.", show_alert=True)
                return
                
            await _handle_rematch(query, context, p1_id, p2_id, mode, bot_id)

        # ------------------
        # Маршрут: Зміна режиму (після гри)
        # ------------------
        elif data.startswith("change_mode_"):
            parts = data.split("_")
            p1_id, p2_id = int(parts[2]), int(parts[3])
            
            if user.id not in [p1_id, p2_id]:
                await query.answer("Ви не були учасником цієї гри.", show_alert=True)
                return
                
            await _handle_change_mode(query, context, p1_id, p2_id, bot_id)

        # ------------------
        # Маршрут: Хід гравця
        # ------------------
        elif data.startswith("move_"):
            parts = data.split("_")
            r_str, c_str = parts[1], parts[2]
            await _handle_move(query, context, r_str, c_str, bot_id)

        # ------------------
        # Маршрут: Невідомий
        # ------------------
        else:
            logger.warning(f"Невідомий callback: '{data}' від {user.id}.")

    except Exception as e:
        logger.error(f"Необроблений виняток у роутері callback {data}: {e}", exc_info=True)
        try:
            await query.message.reply_text(f"{Style.E_ERROR} Ой, щось пішло не так... Спробуйте ще раз.")
        except Exception as e_reply:
            logger.error(f"Не вдалося надіслати повідомлення про помилку: {e_reply}")

# ======================================================================
# РОЗДІЛ 7: ДОПОМІЖНІ ОБРОБНИКИ CALLBACK (Логіка)
# ======================================================================

async def _handle_select_mode(query: CallbackQuery, context: ContextTypes.DEFAULT_TYPE, mode: str, p1_id: int, p2_id: int, bot_id: int):
    """(Helper) Обробляє вибір режиму гри та починає гру."""
    try:
        config = GAME_PRESETS[mode]
        p1_user = await context.bot.get_chat(p1_id)
        p2_user = await context.bot.get_chat(p2_id)

        # Переконуємось, що гравці є в БД
        await ensure_user_data(p1_user.id, p1_user.username, p1_user.first_name, p1_user.last_name)
        await ensure_user_data(p2_user.id, p2_user.username, p2_user.first_name, p2_user.last_name)

        # Очищення старого запрошення та фонового завдання
        if query.message.message_id in context.chat_data.get("invitations", {}):
            del context.chat_data["invitations"][query.message.message_id]
        
        job_name = f"cleanup_{query.message.chat_id}_{query.message.message_id}"
        for job in context.job_queue.get_jobs_by_name(job_name):
            job.schedule_removal()

        # Створюємо новий стан гри
        game_state = {
            "board": [[Style.EMPTY_CELL] * config["size"] for _ in range(config["size"])],
            "player1": {"id": p1_id, "mention": p1_user.mention_html(), "symbol": Style.PLAYER_X},
            "player2": {"id": p2_id, "mention": p2_user.mention_html(), "symbol": Style.PLAYER_O},
            "current_turn_id": p1_id,
            "board_size": config["size"],
            "win_condition": config["win"],
            "mode": mode,
            "chat_id": query.message.chat_id,
            "move_count": 0,
        }

        duel_type = f"{Style.E_BOT_GAME} Гра з ботом!" if p2_id == bot_id else f"{Style.E_DUEL} Дуель!"
        text = (
            f"{duel_type} <b>({config['name']})</b>\n{game_state['player1']['mention']} ({Style.PLAYER_X}) vs {game_state['player2']['mention']} ({Style.PLAYER_O})\n\n"
            f"{Style.E_TURN} Хід за <b>{game_state['player1']['mention']}</b>"
        )

        await query.delete_message()
        game_message = await context.bot.send_message(
            chat_id=query.message.chat_id, text=text, reply_markup=create_keyboard(game_state["board"]), parse_mode=ParseMode.HTML
        )
        
        # Зберігаємо стан гри
        context.chat_data.setdefault("games", {})[game_message.message_id] = game_state
        logger.info(f"Гру {game_message.message_id} розпочато: {mode} між {p1_id} та {p2_id}.")

    except Exception as e:
        logger.error(f"Помилка запуску гри (select_mode): {e}", exc_info=True)

async def _handle_cancel_invite(query: CallbackQuery, context: ContextTypes.DEFAULT_TYPE):
    """(Helper) Обробляє скасування запрошення на гру."""
    try:
        await query.delete_message()
        await context.bot.send_message(chat_id=query.message.chat_id, text=f"{Style.E_CANCEL} Запрошення на гру скасовано.")
        logger.info(f"Запрошення скасовано {query.from_user.id}.")

        # Очищення стану запрошення та фонового завдання
        if query.message.message_id in context.chat_data.get("invitations", {}):
            del context.chat_data["invitations"][query.message.message_id]
        
        job_name = f"cleanup_{query.message.chat_id}_{query.message.message_id}"
        for job in context.job_queue.get_jobs_by_name(job_name):
            job.schedule_removal()
    except Exception as e:
        logger.error(f"Помилка скасування запрошення: {e}", exc_info=True)

async def _handle_cancel_rematch(query: CallbackQuery, context: ContextTypes.DEFAULT_TYPE):
    """(Helper) Обробляє кнопку 'Закрити' після гри."""
    message_id = query.message.message_id
    try:
        await query.edit_message_text(
            text=f"{Style.E_STOP} Гру закрито. До наступних зустрічей! 🕊️",
            reply_markup=None
        )
        logger.info(f"Гру {message_id} закрито через 'cancel_rematch'.")
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            logger.error(f"BadRequest при 'cancel_rematch' {message_id}: {e}", exc_info=True)
    except Exception as e:
        logger.error(f"Помилка при 'cancel_rematch' {message_id}: {e}", exc_info=True)
    finally:
        # Гра вже видалена зі стану в _process_end_game, але про всяк випадок:
        context.chat_data.get("games", {}).pop(message_id, None)

async def _handle_rematch(query: CallbackQuery, context: ContextTypes.DEFAULT_TYPE, p1_id_orig: int, p2_id_orig: int, mode: str, bot_id: int):
    """(Helper) Обробляє запит на рематч."""
    lock_key = f"rematch_lock_{query.message.message_id}"
    if context.chat_data.get(lock_key):
        await query.answer("Рематч вже починається...", show_alert=False)
        return
    context.chat_data[lock_key] = True
    try:
        config = GAME_PRESETS[mode]
        # Новий p1 - це той, хто натиснув кнопку
        p1_new_id = query.from_user.id
        p2_new_id = p2_id_orig if p1_new_id == p1_id_orig else p1_id_orig
        
        p1_user = await context.bot.get_chat(p1_new_id)
        p2_user = await context.bot.get_chat(p2_new_id)

        # Переконуємось, що гравці є в БД (про всяк випадок)
        await ensure_user_data(p1_user.id, p1_user.username, p1_user.first_name, p1_user.last_name)
        await ensure_user_data(p2_user.id, p2_user.username, p2_user.first_name, p2_user.last_name)

        # Створюємо новий стан гри
        game_state = {
            "board": [[Style.EMPTY_CELL] * config["size"] for _ in range(config["size"])],
            "player1": {"id": p1_new_id, "mention": p1_user.mention_html(), "symbol": Style.PLAYER_X},
            "player2": {"id": p2_new_id, "mention": p2_user.mention_html(), "symbol": Style.PLAYER_O},
            "current_turn_id": p1_new_id, # Той, хто натиснув, ходить першим
            "board_size": config["size"],
            "win_condition": config["win"],
            "mode": mode,
            "chat_id": query.message.chat_id,
            "move_count": 0,
        }

        duel_type = f"{Style.E_BOT_GAME} Гра з ботом!" if p2_new_id == bot_id else f"{Style.E_DUEL} Дуель!"
        text = (
            f"{duel_type} <b>({config['name']}) - РЕМАТЧ!</b>\n{game_state['player1']['mention']} ({Style.PLAYER_X}) vs {game_state['player2']['mention']} ({Style.PLAYER_O})\n\n"
            f"{Style.E_TURN} Хід за <b>{game_state['player1']['mention']}</b>"
        )
        
        await query.delete_message()
        
        game_message = await context.bot.send_message(
            chat_id=query.message.chat_id,
            text=text, 
            reply_markup=create_keyboard(game_state["board"]), 
            parse_mode=ParseMode.HTML
        )
        # Зберігаємо стан гри
        context.chat_data.setdefault("games", {})[game_message.message_id] = game_state
        logger.info(f"Рематч ініційовано. Нова гра: {game_message.message_id}.")
        
    except RetryAfter as e:
        await query.answer(f"Зачекайте, Telegram просить нас пригальмувати... {e.retry_after} сек.", show_alert=True)
    except Exception as e:
        logger.error(f"Помилка рематчу: {e}", exc_info=True)
    finally:
        context.chat_data.pop(lock_key, None) # Знімаємо замок

async def _handle_change_mode(query: CallbackQuery, context: ContextTypes.DEFAULT_TYPE, p1_id: int, p2_id: int, bot_id: int):
    """(Helper) Обробляє запит на зміну режиму, повертаючи до екрану вибору."""
    try:
        # Визначаємо, хто натиснув кнопку (новий p1) та іншого гравця (новий p2)
        p1_new_id = query.from_user.id
        p2_new_id = p2_id if p1_new_id == p1_id else p1_id
        
        p1_user = await context.bot.get_chat(p1_new_id)
        p2_user = await context.bot.get_chat(p2_new_id)

        # Використовуємо 'чисту' допоміжну функцію
        reply_markup = _create_mode_selection_keyboard(p1_new_id, p2_new_id)

        # Видаляємо старе повідомлення (результат гри)
        await query.delete_message()

        # Надсилаємо нове повідомлення
        message_text = ""
        if p2_new_id == bot_id:
             message_text = f"{Style.E_BOT_GAME} {p1_user.mention_html()}, оберіть новий режим гри проти мене:"
        else:
            message_text = f"{Style.E_SETUP} {p1_user.mention_html()} пропонує {p2_user.mention_html()} зіграти знову!\n<b>Оберіть новий режим:</b>"

        message = await context.bot.send_message(
            chat_id=query.message.chat_id,
            text=message_text,
            reply_markup=reply_markup,
            parse_mode=ParseMode.HTML,
        )

        # Реєструємо це як нове запрошення
        invitation_context = {"chat_id": message.chat_id, "message_id": message.message_id}
        context.chat_data.setdefault("invitations", {})[message.message_id] = {"p1_id": p1_new_id, "p2_id": p2_new_id}
        # context.job_queue.run_once(  # ВИМКНЕНО таймер
        #     cleanup_invitation, INVITATION_TIMEOUT_SECONDS, data=invitation_context, name=f"cleanup_{message.chat_id}_{message.message_id}"
        # )
        logger.info(f"Зміна режиму: {p1_new_id} та {p2_new_id}. Нове запрошення {message.message_id}.")

    except Exception as e:
        logger.error(f"Помилка зміни режиму (change_mode): {e}", exc_info=True)
        await query.answer("Ой, не вдалося змінити режим.", show_alert=True)

async def _handle_move(query: CallbackQuery, context: ContextTypes.DEFAULT_TYPE, r_str: str, c_str: str, bot_id: int):
    """(Helper) Обробляє хід гравця."""
    message_id = query.message.message_id
    game = context.chat_data.get("games", {}).get(message_id)
    user = query.from_user

    if not game:
        await query.answer("Ця гра вже не активна.", show_alert=True)
        return
    if user.id != game["current_turn_id"]:
        await query.answer("Зараз не ваш хід!", show_alert=False)
        return

    try:
        row, col = int(r_str), int(c_str)
    except ValueError:
        logger.warning(f"Недійсні координати ходу: {r_str}, {c_str}")
        return

    if game["board"][row][col] != Style.EMPTY_CELL:
        await query.answer("Ця клітинка вже зайнята!", show_alert=False)
        return

    # --- Хід Гравця ---
    current_player = game["player1"] if user.id == game["player1"]["id"] else game["player2"]
    other_player = game["player2"] if user.id == game["player1"]["id"] else game["player1"]

    game["board"][row][col] = current_player["symbol"]
    game["move_count"] += 1
    logger.info(f"Хід {user.id} в ({row},{col}). Хід №{game['move_count']}.")

    # Перевірка перемоги/нічиєї гравця
    winner_symbol = check_winner(
        game["board"], (row, col), current_player["symbol"], game["board_size"], game["win_condition"]
    )
    if winner_symbol:
        await _process_end_game(context, query, game, winner_symbol, bot_id)
        return

    # --- Хід Бота (якщо це гра з ботом) ---
    is_bot_game = other_player["id"] == bot_id
    if is_bot_game:
        bot_move = find_best_move(
            game["board"], other_player["symbol"], current_player["symbol"], game["board_size"], game["win_condition"]
        )
        if bot_move:
            r_bot, c_bot = bot_move
            game["board"][r_bot][c_bot] = other_player["symbol"]
            game["move_count"] += 1
            logger.info(f"Хід Бота в ({r_bot},{c_bot}). Хід №{game['move_count']}.")
            
            # Перевірка перемоги/нічиєї бота
            winner_symbol_bot = check_winner(
                game["board"], (r_bot, c_bot), other_player["symbol"], game["board_size"], game["win_condition"]
            )
            if winner_symbol_bot:
                await _process_end_game(context, query, game, winner_symbol_bot, bot_id)
                return
        else:
            logger.warning(f"Бот не зміг знайти хід у грі {message_id}, хоча гра не закінчена.")

        # Хід повертається до гравця
        game["current_turn_id"] = current_player["id"]
        next_turn_mention = current_player["mention"]
    
    # --- Гра 1v1 ---
    else:
        # Хід переходить до іншого гравця
        game["current_turn_id"] = other_player["id"]
        next_turn_mention = other_player["mention"]

    # --- Оновлення Дошки ---
    duel_type = f"{Style.E_BOT_GAME} Гра з ботом!" if is_bot_game else f"{Style.E_DUEL} Дуель!"
    text = (
        f"{duel_type} <b>({game['mode']})</b>\n{game['player1']['mention']} ({Style.PLAYER_X}) vs {game['player2']['mention']} ({Style.PLAYER_O})\n\n"
        f"{Style.E_TURN} Хід за <b>{next_turn_mention}</b>"
    )

    # Оновлюємо повідомлення (з захистом від "Message is not modified")
    if game["move_count"] > 0 and game["move_count"] % 3 == 0:
        await _refresh_game_message(context, query, game, text)
    else:
        try:
            await query.edit_message_text(text=text, reply_markup=create_keyboard(game["board"]), parse_mode=ParseMode.HTML)
        except RetryAfter as e:
            await query.answer(f"Занадто швидко! Спробуйте через {e.retry_after} сек.", show_alert=True)
            # Відкат ходу, якщо Telegram нас блокує
            game["board"][row][col] = Style.EMPTY_CELL
            game["move_count"] -= 1
            game["current_turn_id"] = current_player["id"]
            logger.warning(f"Гра {message_id}: Flood control, хід {user.id} скасовано.")
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                logger.error(f"Не вдалося відредагувати повідомлення про хід у грі {message_id}: {e}", exc_info=True)


async def _process_end_game(context: ContextTypes.DEFAULT_TYPE, query: CallbackQuery, game: dict, winner_symbol: str, bot_id: int):
    """(Helper) Завершує гру, оновлює статистику, нараховує винагороду."""
    message_id = query.message.message_id
    chat_id = game["chat_id"]
    
    winner, loser = None, None
    text = ""
    is_bot_game = (game["player1"]["id"] == bot_id or game["player2"]["id"] == bot_id)

    if winner_symbol == "нічия":
        text = f"{Style.E_DRAW} <b>Гідна битва! Нічия.</b>"
        # Оновлюємо статистику 'draw' для обох
        await update_game_stats(game["player1"]["id"], 'tic_tac_toe', 'draw', chat_id, (game["player2"]["id"] == bot_id))
        await update_game_stats(game["player2"]["id"], 'tic_tac_toe', 'draw', chat_id, (game["player1"]["id"] == bot_id))
    
    else:
        winner = game["player1"] if winner_symbol == game["player1"]["symbol"] else game["player2"]
        loser = game["player2"] if winner_symbol == game["player1"]["symbol"] else game["player1"]
        
        text = f"{Style.E_WIN} <b>Перемога!</b> {winner['mention']} ({winner['symbol']}) був неперевершеним."
        
        # Оновлення статистики переможця та нарахування м'яток
        await update_game_stats(winner["id"], 'tic_tac_toe', 'win', chat_id, (loser["id"] == bot_id))
        if winner["id"] != bot_id:
            await update_user_balance(winner["id"], TIC_TAC_TOE_WIN_REWARD)
            text += f"\n\n✨ {winner['mention']} отримує <b>{TIC_TAC_TOE_WIN_REWARD} м'ятки</b> 🌿!"
        
        # Оновлення статистики програвшого
        await update_game_stats(loser["id"], 'tic_tac_toe', 'loss', chat_id, (winner["id"] == bot_id))

    # Клавіатура для рематчу
    rematch_keyboard = create_rematch_keyboard(game["player1"]["id"], game["player2"]["id"], game["mode"])
    
    try:
        await query.edit_message_text(text=text, reply_markup=rematch_keyboard, parse_mode=ParseMode.HTML)
        logger.info(f"Гра {message_id} завершена. Результат: {winner_symbol}.")
    except RetryAfter as e:
        logger.warning(f"Flood control в кінці гри {message_id}: {e}")
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            logger.error(f"Помилка запиту в кінці гри {message_id}: {e}", exc_info=True)
    finally:
        # Видаляємо гру зі стану в будь-якому випадку
        context.chat_data.get("games", {}).pop(message_id, None)


async def _refresh_game_message(context: ContextTypes.DEFAULT_TYPE, query: CallbackQuery, game: dict, text: str):
    """
    (Helper) Оновлює повідомлення гри, надсилаючи нове та видаляючи старе.
    Це запобігає помилкам "Message not modified".
    """
    old_message_id = query.message.message_id
    
    try:
        new_game_message = await context.bot.send_message(
            chat_id=query.message.chat_id,
            text=text,
            reply_markup=create_keyboard(game["board"]),
            parse_mode=ParseMode.HTML,
        )
        
        # Оновлюємо стан гри з новим ID
        new_message_id = new_game_message.message_id
        game_state_copy = context.chat_data["games"].pop(old_message_id, None)
        
        if game_state_copy:
            context.chat_data["games"][new_message_id] = game_state_copy
            logger.info(f"Оновлено повідомлення гри {old_message_id} -> {new_message_id}.")
            
            # Видаляємо старе повідомлення
            await query.delete_message()
        else:
            # Це не повинно статися, але про всяк випадок
            logger.warning(f"Не вдалося знайти гру {old_message_id} для оновлення.")
            # Видаляємо щойно створене повідомлення
            await new_game_message.delete()
            await query.edit_message_text(text=text, reply_markup=create_keyboard(game["board"]), parse_mode=ParseMode.HTML)

    except Exception as e:
        logger.error(f"Не вдалося оновити повідомлення гри {old_message_id}: {e}", exc_info=True)
        # Якщо оновлення не вдалося, пробуємо просто відредагувати старе
        try:
            await query.edit_message_text(text=text, reply_markup=create_keyboard(game["board"]), parse_mode=ParseMode.HTML)
        except Exception as e_inner:
            logger.error(f"Вторинна спроба редагування {old_message_id} також не вдалася: {e_inner}", exc_info=True)

# ======================================================================
# РОЗДІЛ 8: РЕЄСТРАЦІЯ ОБРОБНИКІВ
# ======================================================================

def register_tic_tac_toe_handlers(application: "Application"):
    """Реєструє всі обробники для гри 'Хрестики-нулики' (🐾🌿)."""

    # Команди
    # Старт гри централізований:
    # - /newgame → меню → «Хрестики-Нулики» → лобі
    # - !гра → швидка дуель (старий флоу)
    application.add_handler(CommandHandler("stopgame", stop_game_command))
    application.add_handler(CommandHandler("playwithbot", play_with_bot_command))
    application.add_handler(CommandHandler("tttbot", play_with_bot_command))  # alias


    # Текстові аліаси
    # !гра — швидкий виклик дуелі (працює тільки для хрестиків-нуликів)
    application.add_handler(MessageHandler(filters.TEXT & filters.Regex(r"(?i)^\s*!гра\b"), bang_game_command))
    application.add_handler(MessageHandler(filters.TEXT & filters.Regex(r"(?i)^\s*(стоп|здаюсь)\s*[!\.,]?\s*$"), stop_game_command))

    # Callback-и лобі (новий UX запуску)
    application.add_handler(CallbackQueryHandler(ttt_lobby_join_callback, pattern=r"^ttt_lobby_join$") )
    application.add_handler(CallbackQueryHandler(ttt_lobby_start_callback, pattern=r"^ttt_lobby_start$") )
    application.add_handler(CallbackQueryHandler(ttt_lobby_cancel_callback, pattern=r"^ttt_lobby_cancel$") )

    # Обробники зворотних викликів
    # Використовуємо один головний роутер для чистоти
    application.add_handler(CallbackQueryHandler(
        handle_tic_tac_toe_callback, 
        pattern=r"^(select_|cancel_invite_|rematch_|change_mode_|move_|cancel_rematch$)"
    ))
    
    # Окремий обробник для пагінації лідерборду
    application.add_handler(CallbackQueryHandler(
        score_command_callback, 
        pattern=r"^score_page_tic_tac_toe_"
    ))

    logger.info("Обробники 'Хрестики-нулики' (🐾🌿) зареєстровано.")