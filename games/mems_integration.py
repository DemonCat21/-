# mems_integration.py
# Інтеграція гри "Мемчики та котики" в основного бота.

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
from pathlib import Path
from typing import Any, Dict, Optional

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    InlineQueryHandler,
    MessageHandler,
    filters,
)

from bot.core.database import (
    mems_get_cards_cache,
    mems_upsert_card,
    mems_load_games_state,
    mems_save_game_state,
    mems_delete_game_state,
    mems_get_global_stats,
    mems_update_global_stats as db_update_global_stats,
    mems_get_situations as db_get_situations,
    mems_insert_situations_if_empty,
    get_mems_settings_for_chat,
)


logger = logging.getLogger(__name__)


# --- Підключаємо "оригінальну" гру як модуль ---
from bot.games import mems_raw as raw


BASE_DIR = Path(__file__).resolve().parents[1]  # bot/
ASSETS_MEMES_DIR = BASE_DIR / "assets" / "mems_memes"


async def _ensure_situations_seeded() -> None:
    """Заливає situations.json у БД один раз (якщо таблиця порожня)."""
    try:
        data_path = Path(__file__).resolve().parent / "mems_situations.json"
        if not data_path.exists():
            return
        texts = json.loads(data_path.read_text(encoding="utf-8"))
        if isinstance(texts, list):
            await mems_insert_situations_if_empty([str(t).strip() for t in texts if str(t).strip()])
    except Exception as e:
        logger.warning(f"mems: не вдалося засіяти ситуації: {e}")


# -----------------------------------------------------------------------------
# Monkeypatch: JSON storage -> DB
# -----------------------------------------------------------------------------

async def _load_json_db(filename: str) -> Dict[str, Any]:
    """Емуляція raw.load_json для ключових файлів гри, але з БД."""
    if filename == raw.DB_FILE:
        return await mems_get_cards_cache()
    if filename == raw.GLOBAL_STATS_FILE:
        return await mems_get_global_stats()
    if filename == raw.GAMES_STATE_FILE:
        return await mems_load_games_state()
    if filename == raw.SITUATIONS_FILE:
        # raw очікує list
        return {"_": await db_get_situations()}  # тимчасовий хак, нижче підміняємо raw.load_situations
    if filename == raw.SETTINGS_FILE:
        # raw.get_chat_settings / update_chat_setting ми теж підміняємо, тож сюди зазвичай не прийде
        return {}

    # fallback: файл (для сумісності) — читаємо поза event loop
    async def _read_file() -> Dict[str, Any]:
        try:
            if not os.path.exists(filename):
                return {}
            def _load_sync():
                with open(filename, "r", encoding="utf-8") as f:
                    return json.load(f)
            return await asyncio.to_thread(_load_sync)
        except Exception:
            return {}

    return await _read_file()


async def _save_json_db(filename: str, data: Any) -> None:
    if filename == raw.DB_FILE:
        # data: dict filename -> file_id
        if isinstance(data, dict):
            for k, v in data.items():
                if k and v:
                    await mems_upsert_card(str(k), str(v))
        return

    if filename == raw.GLOBAL_STATS_FILE:
        # raw пише повний dict; ми оновлюємо лише через db_update_global_stats в процесі гри
        return

    if filename == raw.GAMES_STATE_FILE:
        # data: dict str(chat_id)->state
        if isinstance(data, dict):
            for chat_id_str, state in data.items():
                try:
                    chat_id = int(chat_id_str)
                except Exception:
                    continue
                if not state:
                    await mems_delete_game_state(chat_id)
                else:
                    await mems_save_game_state(chat_id, state)
        return

    # fallback: файл — пишемо в executor
    async def _write_file() -> None:
        def _write_sync():
            with open(filename, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        await asyncio.to_thread(_write_sync)

    try:
        await _write_file()
    except Exception:
        pass


async def _get_chat_settings_db(chat_id: int) -> Dict[str, int]:
    return await get_mems_settings_for_chat(chat_id)


async def _update_chat_setting_db(chat_id: int, key: str, value: int):
    # key тут raw-ключі гри: turn_time/vote_time/max_players/win_score/hand_size
    # в БД зберігаємо як mems_{key}
    from bot.core.database import set_mems_setting_for_chat

    await set_mems_setting_for_chat(chat_id, key, int(value))


async def _update_global_stats_db(user_id: int, chat_id: int, name: str, is_win: bool = False, score_add: int = 0, games_played_add: int = 0):
    await db_update_global_stats(user_id, chat_id, name, is_win=is_win, score_add=score_add, games_played_add=games_played_add)


async def _load_situations_db() -> None:
    """Підміняємо raw.load_situations, щоб брати з БД."""
    raw.CACHED_SITUATIONS = await db_get_situations()


def _apply_monkeypatches() -> None:
    # не чіпаємо логіку гри — лише сховище/шляхи
    raw.load_json = _load_json_db
    raw.save_json = _save_json_db
    raw.get_chat_settings = _get_chat_settings_db
    raw.update_chat_setting = _update_chat_setting_db
    raw.update_global_stats = _update_global_stats_db
    raw.load_situations = _load_situations_db

    # Папка з мемами всередині бота
    raw.MEMES_FOLDER = str(ASSETS_MEMES_DIR)


# -----------------------------------------------------------------------------
# Lazy cache: підвантажуємо file_id, якщо карт замало
# -----------------------------------------------------------------------------

async def _ensure_cards_cached(bot, chat_id: int, min_count: int = 30, silent: bool = False) -> int:
    """Довантажує мінімум min_count карт у кеш (mems_cards). Повертає скільки додали."""
    try:
        cache = await mems_get_cards_cache()
        if len(cache) >= min_count:
            raw.CACHED_CARDS = cache
            return 0

        files = [
            f
            for f in os.listdir(ASSETS_MEMES_DIR)
            if f.lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".webp"))
        ]
        random.shuffle(files)

        added = 0
        for fn in files:
            if fn in cache:
                continue
            path = ASSETS_MEMES_DIR / fn
            try:
                data = await asyncio.to_thread(path.read_bytes)
                m = await bot.send_photo(chat_id, data, disable_notification=True)
                file_id = m.photo[-1].file_id
                await mems_upsert_card(fn, file_id)
                cache[fn] = file_id
                added += 1
                # прибираємо технічне повідомлення
                try:
                    await m.delete()
                except Exception:
                    pass
            except Exception:
                continue

            # легкий анти-флуд
            await asyncio.sleep(1.0)

            if len(cache) >= min_count:
                break

        raw.CACHED_CARDS = cache
        if not silent and added > 0:
            try:
                await bot.send_message(chat_id, f"🐾 Підготувала колоду: +{added} мемчиків (разово).", disable_notification=True)
            except Exception:
                pass
        return added
    except Exception:
        return 0


# -----------------------------------------------------------------------------
# Команди/реєстрація
# -----------------------------------------------------------------------------

async def mems_about(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    await update.message.reply_html(
        "🐾 <b>Мемчики та котики</b>\n"
        "Старт: /newgame\n"
        "Стоп: /stopgame\n"
        "Топ: /top\n"
        "\n(Налаштування — у /settings → «Мемчики та котики».)"
    )


async def _guard_games_module(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    # якщо у чаті вимкнені ігри — не пхаємось
    try:
        from bot.handlers.chat_admin_handlers import is_chat_module_enabled

        if update.effective_chat and not await is_chat_module_enabled(update.effective_chat, "games_enabled"):
            return False
    except Exception:
        pass
    return True


async def cmd_reload_cards(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Перезавантажує всі карти з папки (тільки адмін)."""
    if not await _guard_games_module(update, context):
        return

    await update.message.reply_text("🔄 Перезавантаження карт...")

    # Очищаємо кеш
    from bot.core.database import aiosqlite, DB_PATH
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM mems_cards")
        await db.commit()

    # Довантажуємо з папки
    added = await _ensure_cards_cached(context.bot, update.effective_chat.id, min_count=999, silent=True)
    
    await update.message.reply_text(f"✅ Перезавантажено! Додано {added} нових карт.")


def register_mems_handlers(application) -> None:
    """Реєструє хендлери гри у головному Application."""
    _apply_monkeypatches()

    # Команди (без /start і /settings, щоб не конфліктувати з основним ботом)
    application.add_handler(CommandHandler(["mems", "memgame"], mems_about))
    application.add_handler(CommandHandler(["reload_cards"], _wrap_guard(cmd_reload_cards)))
    application.add_handler(CommandHandler(["stopgame", "stop"], _wrap_guard(raw.cmd_stop_game)))
    application.add_handler(CommandHandler(["leave"], _wrap_guard(raw.cmd_leave_game)))
    application.add_handler(CommandHandler(["kick"], _wrap_guard(raw.cmd_kick)))
    application.add_handler(CommandHandler(["add_sit", "add_situation"], _wrap_guard(raw.cmd_add_situation)))
    application.add_handler(CommandHandler(["pick"], _wrap_guard(raw.cmd_pick_card)))

    # Текстові аліаси з оригіналу (не конфліктують)
    # Старт гри тепер централізований через /newgame (меню вибору гри),
    # тому локальні аліаси старту прибрані.
    application.add_handler(MessageHandler(filters.Regex(r"(?i)^стоп\b"), _wrap_guard(raw.cmd_stop_game)))
    application.add_handler(MessageHandler(filters.Regex(r"(?i)^вийти\b"), _wrap_guard(raw.cmd_leave_game)))

    # Callback-и
    application.add_handler(CallbackQueryHandler(_wrap_guard(raw.cb_join), pattern=r"^join_leave$"))
    application.add_handler(CallbackQueryHandler(_wrap_guard(raw.cb_start_game), pattern=r"^start_game_force$"))
    application.add_handler(CallbackQueryHandler(_wrap_guard(raw.cb_vote), pattern=r"^vote_"))

    # Inline (залишаємо, бо приємно працює в грі)
    application.add_handler(InlineQueryHandler(_wrap_guard(raw.inline_query_handler)))


def _wrap_guard(fn):
    async def _inner(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await _guard_games_module(update, context):
            return

        # На перший запуск — засіваємо ситуації і підвантажуємо кеш карт
        await _ensure_situations_seeded()
        # оновлюємо кеші в raw
        try:
            await raw.load_situations()
        except Exception:
            pass
        if update.effective_chat:
            added = await _ensure_cards_cached(context.bot, update.effective_chat.id, min_count=30)
            try:
                raw.CACHED_CARDS = await mems_get_cards_cache()
            except Exception:
                pass
            if added and update.message:
                try:
                    await update.message.reply_text(
                        f"🐾 Підготувала колоду: +{added} мемчиків (разово).",
                        disable_notification=True,
                    )
                except Exception:
                    pass

        return await fn(update, context)

    return _inner