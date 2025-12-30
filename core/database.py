# database.py
import logging
import aiosqlite
import os
import json
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any
from pathlib import Path

# (НОВЕ) Імпортуємо константи модів з utils
# Використовуємо try-except, щоб уникнути циклічних імпортів при ініціалізації
try:
    from bot.utils.constants import BotTheme
except ImportError:
    # Фалбек, якщо constants ще не створено
    class BotTheme:
        DEFAULT = "default"
        WINTER = "winter"

logger = logging.getLogger(__name__)


# === КОНСТАНТИ ===
BASE_DIR = Path(__file__).resolve().parents[2]
DATA_DIR = BASE_DIR / "data"
DB_PATH = str(DATA_DIR / "memory.db")  # абсолютний шлях до БД

ALLOWED_MODULE_COLUMNS = [
    "ai_enabled",
    "commands_enabled",
    "games_enabled",
    "marriage_enabled",
    "word_filter_enabled",
    "reminders_enabled",
    "auto_delete_actions",
]

CHAT_SETTINGS_BOOL_COLUMNS = {
    "reminders_enabled",
    "auto_delete_actions",
    "ai_auto_clear_conversations",
}

async def column_exists(db: aiosqlite.Connection, table_name: str, column_name: str) -> bool:
    """Перевіряє, чи існує стовпець у вказаній таблиці."""
    cursor = await db.execute(f"PRAGMA table_info({table_name})")
    columns = await cursor.fetchall()
    return any(col[1] == column_name for col in columns)


async def init_db() -> None:
    """
    Ініціалізує базу даних, створюючи таблиці та виконуючи міграцію схеми, якщо необхідно.
    """
    db_dir = os.path.dirname(DB_PATH)
    if db_dir and not os.path.exists(db_dir):
        try:
            os.makedirs(db_dir)
            logger.info(f"Створено директорію для бази даних: {db_dir}")
        except OSError as e:
            logger.error(f"Не вдалося створити директорію для бази даних {db_dir}: {e}")
            return
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("PRAGMA foreign_keys = ON")
            
            # Таблиця для історії розмов (для ШІ)
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS conversations (
                    user_id INTEGER,
                    chat_id INTEGER,
                    role TEXT,
                    content TEXT,
                    ts TEXT
                )
                """
            )

            # Таблиця для стікерів
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS stickers (
                    keyword TEXT PRIMARY KEY,
                    file_unique_id TEXT NOT NULL
                )
                """
            )

            # Таблиця для пам'яті
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS memories (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    scope_id INTEGER NOT NULL, -- user_id або chat_id
                    scope_type TEXT NOT NULL, -- 'user' або 'chat'
                    memory_key TEXT NOT NULL,
                    memory_value TEXT NOT NULL,
                    added_by_user_id INTEGER NOT NULL,
                    timestamp TEXT NOT NULL,
                    UNIQUE(scope_id, scope_type, memory_key)
                )
                """
            )

            # Таблиця для щоденних передбачень
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS daily_predictions (
                    user_id INTEGER PRIMARY KEY,
                    prediction_text TEXT,
                    date TEXT
                )
                """
            )

            # Таблиця для статистики ігор (Хрестики-Нулики)
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS game_stats (
                    user_id INTEGER,
                    chat_id INTEGER,
                    game_name TEXT,
                    wins INTEGER DEFAULT 0,
                    losses INTEGER DEFAULT 0,
                    draws INTEGER DEFAULT 0,
                    wins_vs_bot INTEGER DEFAULT 0,
                    wins_vs_human INTEGER DEFAULT 0,
                    PRIMARY KEY (user_id, chat_id, game_name)
                )
                """
            )

            # Таблиця для зберігання глобальних налаштувань
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS global_settings (
                    setting_name TEXT PRIMARY KEY,
                    setting_value TEXT
                )
                """
            )
            
            # --- (НОВЕ) Встановлюємо мод за замовчуванням, якщо його немає ---
            await db.execute(
                "INSERT OR IGNORE INTO global_settings (setting_name, setting_value) VALUES (?, ?)",
                ('global_bot_mode', BotTheme.DEFAULT)
            )

            # === РОЗШИРЕНА Таблиця для зберігання налаштувань чату ===
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS chat_settings (
                    chat_id INTEGER PRIMARY KEY,
                    chat_title TEXT,
                    chat_username TEXT,
                    chat_type TEXT,
                    
                    -- Модулі
                    ai_enabled INTEGER DEFAULT 1,
                    commands_enabled INTEGER DEFAULT 1,
                    games_enabled INTEGER DEFAULT 1,
                    marriage_enabled INTEGER DEFAULT 1, 
                    word_filter_enabled INTEGER DEFAULT 0,
                    reminders_enabled INTEGER DEFAULT 1, 
                    
                    -- Налаштування
                    welcome_message TEXT, 
                    rules TEXT, 
                    max_warns INTEGER DEFAULT 3,
                    auto_delete_actions INTEGER DEFAULT 0,
                    ai_auto_clear_conversations INTEGER DEFAULT 0,

                    -- Сезонні режими
                    new_year_mode TEXT DEFAULT 'auto',


                    -- Мемчики та котики (налаштування гри)
                    mems_turn_time INTEGER DEFAULT 60,
                    mems_vote_time INTEGER DEFAULT 45,
                    mems_max_players INTEGER DEFAULT 10,
                    mems_min_players INTEGER DEFAULT 2,
                    mems_win_score INTEGER DEFAULT 10,
                    mems_hand_size INTEGER DEFAULT 6,
                    mems_max_rounds INTEGER DEFAULT 10,
                    mems_registration_time INTEGER DEFAULT 120
                )
                """
            )

            # === (НОВЕ) Міграція chat_settings (додаємо всі нові стовпці) ===
            columns_to_add = [
                ("commands_enabled", "INTEGER DEFAULT 1"),
                ("games_enabled", "INTEGER DEFAULT 1"),
                ("marriage_enabled", "INTEGER DEFAULT 1"),
                ("word_filter_enabled", "INTEGER DEFAULT 0"),
                ("reminders_enabled", "INTEGER DEFAULT 1"),
                ("new_year_mode", "TEXT DEFAULT 'auto'"),
                ("welcome_message", "TEXT"),
                ("rules", "TEXT"),
                ("max_warns", "INTEGER DEFAULT 3"),
                ("auto_delete_actions", "INTEGER DEFAULT 0"),
                ("mems_turn_time", "INTEGER DEFAULT 60"),
                ("mems_vote_time", "INTEGER DEFAULT 45"),
                ("mems_max_players", "INTEGER DEFAULT 10"),
                ("mems_min_players", "INTEGER DEFAULT 2"),
                ("mems_win_score", "INTEGER DEFAULT 10"),
                ("mems_hand_size", "INTEGER DEFAULT 6"),
                ("mems_max_rounds", "INTEGER DEFAULT 10"),
            ]
            
            for col_name, col_type in columns_to_add:
                if not await column_exists(db, "chat_settings", col_name):
                    logger.info(f"Міграція 'chat_settings': додаю '{col_name}'...")
                    await db.execute(f"ALTER TABLE chat_settings ADD COLUMN {col_name} {col_type}")

            # === (НОВА) Таблиця для попереджень (warns) ===
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS chat_warnings (
                    chat_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    warn_count INTEGER DEFAULT 0,
                    PRIMARY KEY (chat_id, user_id)
                )
                """
            )

            # === (НОВА) Таблиця для фільтру слів ===
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS filtered_words (
                    chat_id INTEGER NOT NULL,
                    word TEXT NOT NULL,
                    PRIMARY KEY (chat_id, word)
                )
                """
            )

            # === (НОВЕ) Мемчики та котики: кеш картинок (filename -> Telegram file_id) ===
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS mems_cards (
                    file_name TEXT PRIMARY KEY,
                    file_id TEXT NOT NULL,
                    added_ts TEXT
                )
                """
            )

            # === (НОВЕ) Мемчики та котики: стан ігор по чатах (для відновлення після рестарту) ===
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS mems_games_state (
                    chat_id INTEGER PRIMARY KEY,
                    state_json TEXT NOT NULL,
                    updated_ts TEXT
                )
                """
            )

            # === (НОВЕ) Мемчики та котики: глобальна статистика ===
            await db.execute("DROP TABLE IF EXISTS mems_global_stats")
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS mems_global_stats (
                    user_id INTEGER,
                    chat_id INTEGER,
                    name TEXT,
                    wins INTEGER DEFAULT 0,
                    total_score INTEGER DEFAULT 0,
                    games_played INTEGER DEFAULT 0,
                    PRIMARY KEY (user_id, chat_id)
                )
                """
            )

            # === (НОВЕ) Мемчики та котики: ситуації ===
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS mems_situations (
                    text TEXT PRIMARY KEY
                )
                """
            )

            # Таблиця для інформації про користувачів
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS user_data (
                    user_id INTEGER PRIMARY KEY,
                    balance INTEGER DEFAULT 0,
                    is_banned INTEGER DEFAULT 0,
                    username TEXT,
                    first_name TEXT,
                    last_name TEXT
                )
                """
            )

            # === Профіль користувача: міграція полів (gender, city, quote + статистика Мандаринки) ===
            cursor = await db.execute("PRAGMA table_info(user_data)")
            existing_cols = [row[1] for row in await cursor.fetchall()]
            for col_name, col_type in (
                ("gender", "TEXT"),
                ("city", "TEXT"),
                ("quote", "TEXT"),
                # статистика дуелі "Мандаринка" (загальна для всіх чатів)
                ("mandarin_eaten", "INTEGER DEFAULT 0"),
                ("mandarin_duel_wins", "INTEGER DEFAULT 0"),
                ("mandarin_duel_played", "INTEGER DEFAULT 0"),
            ):
                if col_name not in existing_cols:
                    await db.execute(f"ALTER TABLE user_data ADD COLUMN {col_name} {col_type}")


            # === (НОВЕ) Мемчики та котики: кеш карт та стан ігор ===
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS mems_card_cache (
                    file_name TEXT PRIMARY KEY,
                    file_id TEXT NOT NULL,
                    added_ts TEXT
                )
                """
            )

            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS mems_games_state (
                    chat_id INTEGER PRIMARY KEY,
                    state_json TEXT NOT NULL,
                    updated_ts TEXT
                )
                """
            )



            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS mems_situations (
                    text TEXT PRIMARY KEY
                )
                """
            )

            # Таблиця для шлюбів
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS marriages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user1_id INTEGER NOT NULL UNIQUE,
                    user2_id INTEGER NOT NULL UNIQUE,
                    marriage_date TEXT NOT NULL,
                    FOREIGN KEY (user1_id) REFERENCES user_data(user_id) ON DELETE SET NULL,
                    FOREIGN KEY (user2_id) REFERENCES user_data(user_id) ON DELETE SET NULL,
                    CHECK(user1_id != user2_id)
                )
                """
            )

            # Таблиця для нагадувань
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS reminders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    chat_id INTEGER NOT NULL,
                    message_text TEXT NOT NULL,
                    reminder_time TEXT NOT NULL,
                    job_name TEXT,
                    recur_interval TEXT 
                )
                """
            )

            # (НОВЕ) Міграція для reminders: додаємо recur_interval
            if not await column_exists(db, "reminders", "recur_interval"):
                logger.info("Міграція 'reminders': додаю 'recur_interval'...")
                await db.execute("ALTER TABLE reminders ADD COLUMN recur_interval TEXT DEFAULT NULL")
            # (НОВЕ) Міграція для reminders: розширена модель (без ламання старих записів)
            extra_cols = [
                ("creator_user_id", "INTEGER"),
                ("target_user_id", "INTEGER"),
                ("delivery_chat_id", "INTEGER"),
                ("created_in_chat_id", "INTEGER"),
                ("status", "TEXT DEFAULT 'ACTIVE'"),
            ]
            for col_name, col_type in extra_cols:
                if not await column_exists(db, "reminders", col_name):
                    logger.info(f"Міграція 'reminders': додаю '{col_name}'...")
                    await db.execute(f"ALTER TABLE reminders ADD COLUMN {col_name} {col_type}")


            # (НОВЕ) Таблиця для підрахунку дрочок
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS jerk_stats (
                    user_id INTEGER PRIMARY KEY,
                    total_jerks INTEGER DEFAULT 0
                )
                """
            )

            await db.commit()
            logger.info("База даних ініціалізована успішно.")
    except Exception as e:
        logger.error(f"Помилка ініціалізації бази даних: {e}", exc_info=True)


# --- (Розділ AI: Збереження, Отримання, Очищення Повідомлень) ---
async def save_message(user_id: int, chat_id: int, role: str, content: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO conversations (user_id, chat_id, role, content, ts) VALUES (?, ?, ?, ?, ?)",
            (user_id, chat_id, role, content, datetime.now().isoformat()),
        )
        await db.commit()

async def get_recent_messages(
    user_id: int, chat_id: int, max_chars: int = 2000
) -> List[Dict[str, str]]:
    """
    Отримує останні повідомлення для ШІ, обмежуючи їх за загальною кількістю символів.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT role, content FROM conversations WHERE user_id = ? AND chat_id = ? ORDER BY ts DESC",
            (user_id, chat_id),
        )
        rows = await cursor.fetchall()

    recent_messages = []
    current_chars = 0

    for row in rows:
        message_content = row["content"]
        message_len = len(message_content)

        if current_chars + message_len > max_chars:
            break
        
        recent_messages.append({"role": row["role"], "content": message_content})
        current_chars += message_len
    
    return list(reversed(recent_messages))

async def clear_conversations(user_id: int = None, chat_id: int = None):
    async with aiosqlite.connect(DB_PATH) as db:
        if user_id is not None and chat_id is not None:
            await db.execute(
                "DELETE FROM conversations WHERE user_id = ? AND chat_id = ?",
                (user_id, chat_id),
            )
        else:
            await db.execute("DELETE FROM conversations")
        await db.commit()

# --- (Розділ Стікерів) ---
async def save_sticker(keyword: str, file_unique_id: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO stickers (keyword, file_unique_id) VALUES (?, ?)",
            (keyword.lower(), file_unique_id),
        )
        await db.commit()

async def get_sticker(keyword: str) -> Optional[str]:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT file_unique_id FROM stickers WHERE keyword = ?", (keyword.lower(),)
        )
        row = await cursor.fetchone()
    return row[0] if row else None

async def get_all_stickers() -> List[Dict[str, str]]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT keyword, file_unique_id FROM stickers")
        rows = await cursor.fetchall()
    return [dict(row) for row in rows]

async def remove_sticker_db(keyword: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM stickers WHERE keyword = ?", (keyword.lower(),))
        await db.commit()

# --- (Розділ Пам'яті) ---
async def save_memory(
    scope_id: int, scope_type: str, key: str, value: str, added_by_user_id: int
):
    if scope_type not in ["user", "chat"]:
        logger.error(f"Невірний scope_type '{scope_type}' при спробі зберегти пам'ять.")
        return
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT OR REPLACE INTO memories 
            (scope_id, scope_type, memory_key, memory_value, added_by_user_id, timestamp) 
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                scope_id,
                scope_type,
                key,
                value,
                added_by_user_id,
                datetime.now().isoformat(),
            ),
        )
        await db.commit()

async def get_memories_for_scope(
    scope_id: int, scope_type: str
) -> List[Dict[str, str]]:
    if scope_type not in ["user", "chat"]:
        return []
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT memory_key, memory_value FROM memories WHERE scope_id = ? AND scope_type = ?",
            (scope_id, scope_type),
        )
        rows = await cursor.fetchall()
    return [{"key": row["memory_key"], "value": row["memory_value"]} for row in rows]

async def remove_memory(scope_id: int, scope_type: str, key: str):
    if scope_type not in ["user", "chat"]:
        return
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "DELETE FROM memories WHERE scope_id = ? AND scope_type = ? AND memory_key = ?",
            (scope_id, scope_type, key),
        )
        await db.commit()

# --- (Розділ Налаштувань Чату) ---
async def upsert_chat_info(
    chat_id: int,
    chat_type: str,
    chat_title: Optional[str] = None,
    chat_username: Optional[str] = None,
):
    """Оновлює інформацію про чат або додає її, якщо чат новий."""
    async with aiosqlite.connect(DB_PATH) as db:
        # Спочатку перевіримо, чи змінилось щось, щоб не робити зайвий запис
        cursor = await db.execute("SELECT chat_title, chat_username, chat_type FROM chat_settings WHERE chat_id = ?", (chat_id,))
        row = await cursor.fetchone()
        
        if row and row[0] == chat_title and row[1] == chat_username and row[2] == chat_type:
             return # Нічого не змінилось

        await db.execute(
            """
            INSERT INTO chat_settings (chat_id, chat_title, chat_username, chat_type)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET
                chat_title = excluded.chat_title,
                chat_username = excluded.chat_username,
                chat_type = excluded.chat_type
            """,
            (chat_id, chat_title, chat_username, chat_type),
        )
        await db.commit()


async def get_chat_settings(chat_id: int) -> Dict[str, Any]:
    """
    Отримує повні налаштування для чату.
    """
    defaults = {
        "chat_id": chat_id,
        "chat_title": None,
        "chat_username": None,
        "chat_type": None,
        "ai_enabled": 1,
        "commands_enabled": 1,
        "games_enabled": 1,
        "marriage_enabled": 1,
        "word_filter_enabled": 0,

        "reminders_enabled": 1,
        "new_year_mode": "auto",
        "welcome_message": None,
        "rules": None,
        "max_warns": 3,
        "auto_delete_actions": 0,

        # Мемчики та котики (дефолти)
        "mems_turn_time": 60,
        "mems_vote_time": 45,
        "mems_max_players": 10,
        "mems_win_score": 10,
        "mems_hand_size": 6,
    }
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM chat_settings WHERE chat_id = ?", (chat_id,))
        row = await cursor.fetchone()

    if row:
        defaults.update(dict(row))
    
    return defaults


async def set_module_status(chat_id: int, module_key: str, enabled: bool) -> None:
    key_map = {
        "ai": "ai_enabled",
        "commands": "commands_enabled",
        "games": "games_enabled",
        "marriage": "marriage_enabled",
        "word": "word_filter_enabled"
    }
    if module_key in key_map:
        module_key = key_map[module_key]
    if module_key not in ALLOWED_MODULE_COLUMNS:
        logger.error(f"Спроба оновити недійсний стовпець: {module_key}")
        return
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "INSERT OR IGNORE INTO chat_settings (chat_id) VALUES (?)", (chat_id,)
            )
            await db.execute(
                f"UPDATE chat_settings SET {module_key} = ? WHERE chat_id = ?",
                (int(enabled), chat_id),
            )
            await db.commit()
        logger.info(f"Статус модуля {module_key} для чату {chat_id} змінено на {enabled}.")
    except Exception as e:
        logger.error(f"Помилка при оновленні статусу модуля {module_key} для чату {chat_id}: {e}", exc_info=True)

async def set_chat_welcome_message(chat_id: int, message: Optional[str]):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO chat_settings (chat_id) VALUES (?)", (chat_id,)
        )
        await db.execute(
            "UPDATE chat_settings SET welcome_message = ? WHERE chat_id = ?",
            (message, chat_id),
        )
        await db.commit()

async def set_chat_rules(chat_id: int, rules_text: Optional[str]):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO chat_settings (chat_id) VALUES (?)", (chat_id,)
        )
        await db.execute(
            "UPDATE chat_settings SET rules = ? WHERE chat_id = ?",
            (rules_text, chat_id),
        )
        await db.commit()

async def set_max_warns(chat_id: int, limit: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO chat_settings (chat_id) VALUES (?)", (chat_id,)
        )
        await db.execute(
            "UPDATE chat_settings SET max_warns = ? WHERE chat_id = ?",
            (limit, chat_id),
        )
        await db.commit()


# =============================================================================
# Мемчики та котики — налаштування/стан/кеш
# =============================================================================

MEMS_ALLOWED_SETTINGS = {
    "turn_time": ("mems_turn_time", 60),
    "vote_time": ("mems_vote_time", 45),
    "max_players": ("mems_max_players", 10),
    "min_players": ("mems_min_players", 2),
    "win_score": ("mems_win_score", 10),
    "hand_size": ("mems_hand_size", 6),
    "max_rounds": ("mems_max_rounds", 10),
}


async def get_mems_settings_for_chat(chat_id: int) -> Dict[str, int]:
    """Повертає налаштування гри для конкретного чату в ключах гри."""
    settings = await get_chat_settings(chat_id)
    out: Dict[str, int] = {}
    for game_key, (col, default_val) in MEMS_ALLOWED_SETTINGS.items():
        try:
            out[game_key] = int(settings.get(col, default_val))
        except Exception:
            out[game_key] = int(default_val)
    return out


async def set_mems_setting_for_chat(chat_id: int, game_key: str, value: int) -> None:
    if game_key not in MEMS_ALLOWED_SETTINGS:
        return
    col, _default_val = MEMS_ALLOWED_SETTINGS[game_key]
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR IGNORE INTO chat_settings (chat_id) VALUES (?)", (chat_id,))
        await db.execute(f"UPDATE chat_settings SET {col} = ? WHERE chat_id = ?", (int(value), chat_id))
        await db.commit()

async def set_chat_setting_flag(chat_id: int, column: str, enabled: bool) -> None:
    if column not in CHAT_SETTINGS_BOOL_COLUMNS:
        logger.error(f"Спроба оновити недійсний стовпець налаштувань: {column}")
        return
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            if not await column_exists(db, "chat_settings", column):
                logger.info(f"Міграція 'chat_settings': додаю '{column}'...")
                await db.execute(
                    f"ALTER TABLE chat_settings ADD COLUMN {column} INTEGER DEFAULT 0"
                )
            await db.execute(
                "INSERT OR IGNORE INTO chat_settings (chat_id) VALUES (?)", (chat_id,)
            )
            await db.execute(
                f"UPDATE chat_settings SET {column} = ? WHERE chat_id = ?",
                (int(enabled), chat_id),
            )
            await db.commit()
    except Exception as e:
        logger.error(
            f"Помилка при оновленні налаштування {column} для чату {chat_id}: {e}",
            exc_info=True,
        )




async def mems_get_cards_cache() -> Dict[str, str]:
    """filename -> file_id"""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT file_name, file_id FROM mems_cards")
        rows = await cur.fetchall()
    return {r["file_name"]: r["file_id"] for r in rows}


async def mems_upsert_card(file_name: str, file_id: str) -> None:
    ts = datetime.utcnow().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO mems_cards (file_name, file_id, added_ts)
            VALUES (?, ?, ?)
            ON CONFLICT(file_name) DO UPDATE SET
                file_id = excluded.file_id,
                added_ts = excluded.added_ts
            """,
            (file_name, file_id, ts),
        )
        await db.commit()


async def mems_load_games_state() -> Dict[str, Any]:
    """Повертає dict як у games_state.json (ключі — chat_id як str)."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT chat_id, state_json FROM mems_games_state")
        rows = await cur.fetchall()
    out: Dict[str, Any] = {}
    for r in rows:
        try:
            out[str(r["chat_id"])] = json.loads(r["state_json"]) if r["state_json"] else {}
        except Exception:
            out[str(r["chat_id"])] = {}
    return out


async def mems_save_game_state(chat_id: int, state: Dict[str, Any]) -> None:
    ts = datetime.utcnow().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO mems_games_state (chat_id, state_json, updated_ts)
            VALUES (?, ?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET
                state_json = excluded.state_json,
                updated_ts = excluded.updated_ts
            """,
            (chat_id, json.dumps(state, ensure_ascii=False), ts),
        )
        await db.commit()


async def mems_delete_game_state(chat_id: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM mems_games_state WHERE chat_id = ?", (chat_id,))
        await db.commit()


async def mems_get_global_stats() -> Dict[str, Any]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """
            SELECT user_id, name, SUM(wins) as wins, SUM(total_score) as total_score, SUM(games_played) as games_played
            FROM mems_global_stats
            GROUP BY user_id
            """
        )
        rows = await cur.fetchall()
    out: Dict[str, Any] = {}
    for r in rows:
        out[str(r["user_id"])] = {
            "name": r["name"],
            "wins": int(r["wins"] or 0),
            "total_score": int(r["total_score"] or 0),
            "games_played": int(r["games_played"] or 0),
        }
    return out


async def mems_get_situations() -> List[str]:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT text FROM mems_situations")
        rows = await cur.fetchall()
    return [r[0] for r in rows]


async def mems_insert_situations_if_empty(texts: List[str]) -> None:
    if not texts:
        return
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT COUNT(1) FROM mems_situations")
        (cnt,) = await cur.fetchone()
        if cnt and int(cnt) > 0:
            return
        await db.executemany(
            "INSERT OR IGNORE INTO mems_situations (text) VALUES (?)",
            [(t,) for t in texts],
        )
        await db.commit()


# --- (Розділ Фільтру Слів) ---
async def add_filtered_word(chat_id: int, word: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO filtered_words (chat_id, word) VALUES (?, ?)",
            (chat_id, word.lower()),
        )
        await db.commit()

async def remove_filtered_word(chat_id: int, word: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "DELETE FROM filtered_words WHERE chat_id = ? AND word = ?",
            (chat_id, word.lower()),
        )
        await db.commit()

async def get_filtered_words(chat_id: int) -> List[str]:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT word FROM filtered_words WHERE chat_id = ?",
            (chat_id,),
        )
        rows = await cursor.fetchall()
    return [row[0] for row in rows]

# --- (Розділ Попереджень) ---
async def add_user_warn(chat_id: int, user_id: int) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO chat_warnings (chat_id, user_id, warn_count)
            VALUES (?, ?, 1)
            ON CONFLICT(chat_id, user_id) DO UPDATE SET
                warn_count = warn_count + 1
            """,
            (chat_id, user_id),
        )
        await db.commit()
        cursor = await db.execute(
             "SELECT warn_count FROM chat_warnings WHERE chat_id = ? AND user_id = ?",
            (chat_id, user_id),
        )
        row = await cursor.fetchone()
        return row[0] if row else 0

async def get_user_warns(chat_id: int, user_id: int) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT warn_count FROM chat_warnings WHERE chat_id = ? AND user_id = ?",
            (chat_id, user_id),
        )
        row = await cursor.fetchone()
    return row[0] if row else 0

async def reset_user_warns(chat_id: int, user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE chat_warnings SET warn_count = 0 WHERE chat_id = ? AND user_id = ?",
            (chat_id, user_id),
        )
        await db.commit()


# --- (Розділ Статистики) ---
async def get_all_chats(
    page_offset: int = 0, page_size: Optional[int] = None
) -> List[Dict[str, Any]]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        query = "SELECT * FROM chat_settings ORDER BY chat_title"
        params = []
        if page_size is not None:
            query += " LIMIT ? OFFSET ?"
            params.extend([page_size, page_offset * page_size])
        cursor = await db.execute(query, tuple(params))
        rows = await cursor.fetchall()
    return [dict(row) for row in rows]

async def get_total_chats_count() -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT COUNT(DISTINCT chat_id) FROM chat_settings")
        count = (await cursor.fetchone())[0]
    return count

async def get_total_users() -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT COUNT(DISTINCT user_id) FROM user_data")
        count = (await cursor.fetchone())[0]
    return count

async def get_all_user_ids() -> List[int]:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT user_id FROM user_data")
        rows = await cursor.fetchall()
    return [row[0] for row in rows]

async def get_all_users_info(
    page_offset: int = 0, page_size: Optional[int] = None
) -> List[Dict[str, Any]]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        query = "SELECT user_id, balance, is_banned, username, first_name, last_name FROM user_data ORDER BY first_name"
        params = []
        if page_size is not None:
            query += " LIMIT ? OFFSET ?"
            params.extend([page_size, page_offset * page_size])
        cursor = await db.execute(query, tuple(params))
        rows = await cursor.fetchall()
    return [dict(row) for row in rows]

async def get_users_in_chat(chat_id: int) -> List[int]:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT DISTINCT user_id FROM conversations WHERE chat_id = ?",
            (chat_id,),
        )
        rows = await cursor.fetchall()
    return [row[0] for row in rows]

async def set_daily_prediction(user_id: int, prediction: str, date: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO daily_predictions (user_id, prediction_text, date) VALUES (?, ?, ?)",
            (user_id, prediction, date),
        )
        await db.commit()

async def get_daily_prediction(user_id: int, date: str) -> Optional[str]:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT prediction_text FROM daily_predictions WHERE user_id = ? AND date = ?",
            (user_id, date),
        )
        row = await cursor.fetchone()
    return row[0] if row else None

# --- (Розділ Глобального AI) ---
async def get_global_ai_status() -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT setting_value FROM global_settings WHERE setting_name = 'global_ai_enabled'"
        )
        row = await cursor.fetchone()
    return bool(int(row[0])) if row and row[0] is not None else True

async def set_global_ai_status(enabled: bool):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO global_settings (setting_name, setting_value) VALUES (?, ?)",
            ("global_ai_enabled", str(int(enabled))),
        )
        await db.commit()

# --- (Розділ Глобального Моду Бота) ---
async def get_global_bot_mode() -> str:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT setting_value FROM global_settings WHERE setting_name = 'global_bot_mode'"
        )
        row = await cursor.fetchone()
    return row[0] if row and row[0] else BotTheme.DEFAULT

async def set_global_bot_mode(mode_name: str):
    if mode_name not in [BotTheme.DEFAULT, BotTheme.WINTER]:
        logger.warning(f"Спроба встановити неіснуючий мод: {mode_name}")
        return
        
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO global_settings (setting_name, setting_value) VALUES (?, ?)",
            ("global_bot_mode", mode_name),
        )
        await db.commit()
    logger.info(f"Глобальний мод бота змінено на: {mode_name}")


async def is_ai_enabled_for_chat(
    chat_id: int, ignore_global: bool = False
) -> bool:
    chat_settings = await get_chat_settings(chat_id)
    chat_ai_status = chat_settings.get("ai_enabled", 1) == 1

    if ignore_global:
        return chat_ai_status
    else:
        global_ai_status = await get_global_ai_status()
        return global_ai_status and chat_ai_status

async def set_chat_ai_status(chat_id: int, enabled: bool):
    await set_module_status(chat_id, "ai_enabled", enabled)

# --- (Розділ Статистики Ігор) ---
async def update_game_stats(
    user_id: int, game_name: str, result_type: str, chat_id: int, is_vs_bot: bool
):
    if result_type == "loss":
        column_to_update = "losses"
    else:
        column_to_update = f"{result_type}s"

    vs_column_update_sql = ""
    if result_type == "win":
        vs_column = "wins_vs_bot" if is_vs_bot else "wins_vs_human"
        vs_column_update_sql = f", {vs_column} = {vs_column} + 1"

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            f"""
            INSERT INTO game_stats (user_id, chat_id, game_name, {column_to_update})
            VALUES (?, ?, ?, 1)
            ON CONFLICT(user_id, chat_id, game_name) DO UPDATE SET
                {column_to_update} = {column_to_update} + 1 {vs_column_update_sql}
            """,
            (user_id, chat_id, game_name),
        )
        await db.commit()

async def admin_set_game_stats(
    user_id: int, chat_id: int, game_name: str, wins: int, losses: int, draws: int
):
    await ensure_user_data(user_id, None, None, None, update_names=False)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO game_stats (user_id, chat_id, game_name, wins, losses, draws, wins_vs_bot, wins_vs_human)
            VALUES (?, ?, ?, ?, ?, ?, 0, 0)
            ON CONFLICT(user_id, chat_id, game_name) DO UPDATE SET
                wins = excluded.wins,
                losses = excluded.losses,
                draws = excluded.draws,
                wins_vs_bot = 0,
                wins_vs_human = 0
            """,
            (user_id, chat_id, game_name, wins, losses, draws),
        )
        await db.commit()

async def get_game_stats(
    user_id: int, game_name: str, chat_id: Optional[int] = None
) -> Dict[str, int]:
    query = "SELECT SUM(wins) as total_wins, SUM(losses) as total_losses, SUM(draws) as total_draws, SUM(wins_vs_bot) as wins_vs_bot, SUM(wins_vs_human) as wins_vs_human FROM game_stats WHERE user_id = ? AND game_name = ?"
    params = [user_id, game_name]
    if chat_id:
        query += " AND chat_id = ?"
        params.append(chat_id)
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(query, tuple(params))
        row = await cursor.fetchone()
    if row and row["total_wins"] is not None:
        stats = dict(row)
        stats["total_played"] = (
            stats["total_wins"] + stats["total_losses"] + stats["total_draws"]
        )
        return stats
    return {
        "total_wins": 0, "total_losses": 0, "total_draws": 0,
        "wins_vs_bot": 0, "wins_vs_human": 0, "total_played": 0,
    }

async def get_chat_game_top(
    chat_id: int, game_name: str, limit: int = 10, offset: int = 0
) -> List[Dict[str, Any]]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT user_id, wins, wins_vs_bot, wins_vs_human
            FROM game_stats
            WHERE chat_id = ? AND game_name = ?
            ORDER BY wins DESC
            LIMIT ? OFFSET ?
            """,
            (chat_id, game_name, limit, offset),
        )
        rows = await cursor.fetchall()
    return [dict(row) for row in rows]

async def get_chat_game_top_count(chat_id: int, game_name: str) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT COUNT(DISTINCT user_id) FROM game_stats WHERE chat_id = ? AND game_name = ?",
            (chat_id, game_name),
        )
        count = (await cursor.fetchone())[0]
    return count

async def get_global_game_top(
    game_name: str, limit: int = 10
) -> List[Dict[str, Any]]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT user_id, SUM(wins) as total_wins
            FROM game_stats
            WHERE game_name = ?
            GROUP BY user_id
            ORDER BY total_wins DESC
            LIMIT ?
            """,
            (game_name, limit),
        )
        rows = await cursor.fetchall()
    return [dict(row) for row in rows]

# --- (Розділ Користувачів: Баланс, Бан, Інфо) ---
# [ОПТИМІЗОВАНО]
async def ensure_user_data(
    user_id: int,
    username: Optional[str],
    first_name: Optional[str],
    last_name: Optional[str],
    update_names: bool = True,
):
    """
    Записує користувача в БД.
    Оптимізація: не робить UPDATE, якщо дані не змінилися.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        
        # 1. Спробуємо знайти користувача
        cursor = await db.execute(
            "SELECT username, first_name, last_name FROM user_data WHERE user_id = ?",
            (user_id,)
        )
        row = await cursor.fetchone()

        if not row:
            # Користувача немає, вставляємо
            await db.execute(
                "INSERT INTO user_data (user_id, username, first_name, last_name) VALUES (?, ?, ?, ?)",
                (user_id, username, first_name, last_name),
            )
            await db.commit()
        elif update_names:
            # Користувач є. Перевіряємо, чи змінилися дані.
            db_username = row["username"]
            db_first = row["first_name"]
            db_last = row["last_name"]

            if db_username != username or db_first != first_name or db_last != last_name:
                await db.execute(
                    """
                    UPDATE user_data
                    SET username = ?, first_name = ?, last_name = ?
                    WHERE user_id = ?
                    """,
                    (username, first_name, last_name, user_id),
                )
                await db.commit()
                # logger.debug(f"Оновлено дані користувача {user_id}")
            else:
                pass # Дані актуальні, економимо ресурс диска


async def get_user_balance(user_id: int) -> int:
    await ensure_user_data(user_id, None, None, None, update_names=False)
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT balance FROM user_data WHERE user_id = ?", (user_id,)
        )
        row = await cursor.fetchone()
    return row[0] if row else 0

async def update_user_balance(user_id: int, amount: int):
    await ensure_user_data(user_id, None, None, None, update_names=False)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE user_data SET balance = balance + ? WHERE user_id = ?",
            (amount, user_id),
        )
        await db.commit()


async def transfer_user_balance_atomic(from_user_id: int, to_user_id: int, amount: int) -> bool:
    """Атомарний переказ мʼяток між двома користувачами.

    Гарантії:
    - 1 транзакція SQLite (BEGIN IMMEDIATE → COMMIT)
    - перевірка балансу списання всередині транзакції
    - неможливість подвійного списання при паралельних апдейтах

    Повертає True, якщо переказ виконано, інакше False.
    """
    if amount <= 0:
        return False
    if from_user_id == to_user_id:
        return False

    # Гарантуємо наявність рядків до транзакції
    await ensure_user_data(from_user_id, None, None, None, update_names=False)
    await ensure_user_data(to_user_id, None, None, None, update_names=False)

    async with aiosqlite.connect(DB_PATH) as db:
        try:
            await db.execute("PRAGMA foreign_keys = ON")
            # IMMEDIATE → одразу беремо write-lock, щоб уникнути гонок
            await db.execute("BEGIN IMMEDIATE")

            # 1) списання тільки якщо є достатньо
            cur = await db.execute(
                "UPDATE user_data SET balance = balance - ? WHERE user_id = ? AND balance >= ?",
                (amount, from_user_id, amount),
            )
            if cur.rowcount != 1:
                await db.execute("ROLLBACK")
                return False

            # 2) нарахування
            await db.execute(
                "UPDATE user_data SET balance = balance + ? WHERE user_id = ?",
                (amount, to_user_id),
            )

            await db.commit()
            return True
        except Exception:
            try:
                await db.execute("ROLLBACK")
            except Exception:
                pass
            logger.exception("transfer_user_balance_atomic failed")
            return False


async def add_mandarin_duel_stats(
    user_id: int,
    *,
    eaten_delta: int = 0,
    wins_delta: int = 0,
    played_delta: int = 0,
) -> None:
    """Інкрементує статистику дуелі "Мандаринка" в user_data.

    Статистика глобальна для користувача (не по чатах):
    - mandarin_eaten
    - mandarin_duel_wins
    - mandarin_duel_played
    """ 
    if eaten_delta == 0 and wins_delta == 0 and played_delta == 0:
        return
    await ensure_user_data(user_id, None, None, None, update_names=False)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            UPDATE user_data
            SET
              mandarin_eaten = COALESCE(mandarin_eaten,0) + ?,
              mandarin_duel_wins = COALESCE(mandarin_duel_wins,0) + ?,
              mandarin_duel_played = COALESCE(mandarin_duel_played,0) + ?
            WHERE user_id = ?
            """,
            (int(eaten_delta), int(wins_delta), int(played_delta), user_id),
        )
        await db.commit()

async def get_top_balances(limit: int = 10) -> List[Dict[str, Any]]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT user_id, balance, first_name, username FROM user_data ORDER BY balance DESC LIMIT ?",
            (limit,),
        )
        rows = await cursor.fetchall()
    return [dict(row) for row in rows]

async def get_user_info(user_id: int) -> Optional[Dict[str, Any]]:
    await ensure_user_data(user_id, None, None, None, update_names=False)
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT user_id, balance, is_banned, username, first_name, last_name FROM user_data WHERE user_id = ?",
            (user_id,),
        )
        row = await cursor.fetchone()
    return dict(row) if row else None

async def ban_user(user_id: int):
    await ensure_user_data(user_id, None, None, None, update_names=False)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE user_data SET is_banned = 1 WHERE user_id = ?", (user_id,)
        )
        await db.commit()

async def unban_user(user_id: int):
    await ensure_user_data(user_id, None, None, None, update_names=False)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE user_data SET is_banned = 0 WHERE user_id = ?", (user_id,)
        )
        await db.commit()

async def is_user_banned(user_id: int) -> bool:
    user_info = await get_user_info(user_id)
    return user_info["is_banned"] == 1 if user_info else False

async def get_banned_users() -> List[Dict[str, Any]]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT user_id, first_name, username FROM user_data WHERE is_banned = 1"
        )
        rows = await cursor.fetchall()
    return [dict(row) for row in rows]

async def get_bot_stats() -> Dict[str, Any]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor_messages = await db.execute("SELECT COUNT(*) FROM conversations")
        total_messages = (await cursor_messages.fetchone())[0]
        cursor_users = await db.execute(
            "SELECT COUNT(DISTINCT user_id) FROM user_data"
        )
        total_users = (await cursor_users.fetchone())[0]
        one_day_ago = (datetime.now() - timedelta(hours=24)).isoformat()
        cursor_active_chats = await db.execute(
            "SELECT COUNT(DISTINCT chat_id) FROM conversations WHERE ts >= ?",
            (one_day_ago,),
        )
        active_chats_24h = (await cursor_active_chats.fetchone())[0]
        
        # Популярні команди
        cursor_popular_commands = await db.execute(
            """
            SELECT
                CASE
                    WHEN INSTR(content, ' ') > 0 THEN SUBSTR(content, 1, INSTR(content, ' ') - 1)
                    ELSE content
                END as command,
                COUNT(*) as count
            FROM conversations
            WHERE role = 'user' AND content LIKE '/%'
            GROUP BY command
            ORDER BY count DESC
            LIMIT 5
            """
        )
        popular_commands = await cursor_popular_commands.fetchall()
    return {
        "total_messages": total_messages,
        "total_users": total_users,
        "active_users_24h": active_chats_24h,
        "popular_commands": [
            (row["command"], row["count"]) for row in popular_commands
        ],
    }

async def get_user_by_username(username: str) -> Optional[Dict[str, Any]]:
    """Повертає запис користувача з `user_data` по username (без @)."""
    if not username:
        return None
    username_clean = username.replace('@', '')
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT user_id, balance, is_banned, username, first_name, last_name FROM user_data WHERE username = ? COLLATE NOCASE",
            (username_clean,),
        )
        row = await cursor.fetchone()
    return dict(row) if row else None

# --- (Розділ Шлюбів) ---
async def get_marriage_by_user_id(user_id: int) -> Optional[Dict[str, Any]]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM marriages WHERE user1_id = ? OR user2_id = ?",
            (user_id, user_id),
        )
        row = await cursor.fetchone()
    return dict(row) if row else None

async def create_marriage(user1_id: int, user2_id: int, date_str: str):
    if user1_id > user2_id:
        user1_id, user2_id = user2_id, user1_id
    async with aiosqlite.connect(DB_PATH) as db:
        try:
            await db.execute(
                "INSERT INTO marriages (user1_id, user2_id, marriage_date) VALUES (?, ?, ?)",
                (user1_id, user2_id, date_str),
            )
            await db.commit()
            logger.info(f"Створено шлюб між {user1_id} та {user2_id}")
        except aiosqlite.IntegrityError as e:
            logger.error(
                f"Помилка цілісності при створенні шлюбу: {e}."
            )
            raise

async def delete_marriage_by_user_id(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "DELETE FROM marriages WHERE user1_id = ? OR user2_id = ?",
            (user_id, user_id),
        )
        await db.commit()
        if cursor.rowcount > 0:
            logger.info(f"Користувач {user_id} розлучився.")

# --- (Розділ Нагадувань) ---
async def add_reminder(
    user_id: int,
    chat_id: int,
    message_text: str,
    reminder_time: str,
    job_name: Optional[str],
    recur_interval: Optional[str] = None, 
) -> Optional[int]:
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute(
                "INSERT INTO reminders (user_id, chat_id, message_text, reminder_time, job_name, recur_interval, creator_user_id, target_user_id, delivery_chat_id, created_in_chat_id, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (user_id, chat_id, message_text, reminder_time, job_name, recur_interval, user_id, user_id, chat_id, chat_id, 'ACTIVE'),
            )
            await db.commit()
            return cursor.lastrowid
    except Exception as e:
        logger.error(f"Failed to add reminder for user {user_id}: {e}", exc_info=True)
        return None

async def set_reminder_job_name(reminder_id: int, job_name: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE reminders SET job_name = ? WHERE id = ?", (job_name, reminder_id)
        )
        await db.commit()

async def get_user_reminders_count(user_id: int) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT COUNT(*) FROM reminders WHERE user_id = ?", (user_id,)
        )
        count = (await cursor.fetchone())[0]
    return count

async def get_user_reminders(user_id: int) -> List[Dict[str, Any]]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT id, message_text, reminder_time, recur_interval FROM reminders WHERE user_id = ? ORDER BY reminder_time ASC",
            (user_id,),
        )
        rows = await cursor.fetchall()
    return [dict(row) for row in rows]

async def get_reminder(reminder_id: int) -> Optional[Dict[str, Any]]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM reminders WHERE id = ?", (reminder_id,)
        )
        row = await cursor.fetchone()
    return dict(row) if row else None

async def get_all_reminders() -> List[Dict[str, Any]]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM reminders")
        rows = await cursor.fetchall()
    return [dict(row) for row in rows]

async def remove_reminder(reminder_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM reminders WHERE id = ?", (reminder_id,))
        await db.commit()
    logger.info(f"Видалено нагадування (ID: {reminder_id}) з БД.")

async def remove_reminder_by_job_name(job_name: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM reminders WHERE job_name = ?", (job_name,))
        await db.commit()

async def update_reminder_time_and_job(reminder_id: int, new_time_iso: str, new_job_name: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE reminders SET reminder_time = ?, job_name = ? WHERE id = ?",
            (new_time_iso, new_job_name, reminder_id)
        )
        await db.commit()

async def set_reminder_status(reminder_id: int, status: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE reminders SET status = ? WHERE id = ?", (status, reminder_id))
        await db.commit()

async def get_reminders_by_delivery_chat(chat_id: int, statuses: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        if statuses:
            placeholders = ",".join("?" for _ in statuses)
            cursor = await db.execute(
                f"SELECT * FROM reminders WHERE delivery_chat_id = ? AND status IN ({placeholders})",
                (chat_id, *statuses),
            )
        else:
            cursor = await db.execute(
                "SELECT * FROM reminders WHERE delivery_chat_id = ?",
                (chat_id,),
            )
        rows = await cursor.fetchall()
    return [dict(r) for r in rows]

async def set_reminders_status_by_delivery_chat(chat_id: int, status: str, prev_statuses: Optional[List[str]] = None) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        if prev_statuses:
            placeholders = ",".join("?" for _ in prev_statuses)
            await db.execute(
                f"UPDATE reminders SET status = ? WHERE delivery_chat_id = ? AND status IN ({placeholders})",
                (status, chat_id, *prev_statuses),
            )
        else:
            await db.execute(
                "UPDATE reminders SET status = ? WHERE delivery_chat_id = ?",
                (status, chat_id),
            )
        await db.commit()


# --- (Розділ Дрочок) ---
async def increment_jerk_count(user_id: int) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "UPDATE jerk_stats SET total_jerks = total_jerks + 1 WHERE user_id = ?",
            (user_id,)
        )
        if cursor.rowcount == 0:
            await db.execute(
                "INSERT INTO jerk_stats (user_id, total_jerks) VALUES (?, 1)",
                (user_id,)
            )
        await db.commit()
        
        cursor = await db.execute(
            "SELECT total_jerks FROM jerk_stats WHERE user_id = ?",
            (user_id,)
        )
        row = await cursor.fetchone()
        return row[0] if row else 0

async def get_jerk_count(user_id: int) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT total_jerks FROM jerk_stats WHERE user_id = ?",
            (user_id,)
        )
        row = await cursor.fetchone()
        return row[0] if row else 0

async def get_top_jerkers(limit: int = 10) -> List[Dict[str, Any]]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT user_id, total_jerks FROM jerk_stats ORDER BY total_jerks DESC LIMIT ?",
            (limit,)
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

# ======================
# Профіль користувача
# ======================

async def get_user_profile(user_id: int) -> Dict[str, Any]:
    """Повертає профіль користувача (gender, city, quote, balance + статистика ігор)."""
    await ensure_user_data(user_id, None, None, None, update_names=False)
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT
              gender,
              city,
              quote,
              balance,
              COALESCE(mandarin_eaten,0) as mandarin_eaten,
              COALESCE(mandarin_duel_wins,0) as mandarin_duel_wins,
              COALESCE(mandarin_duel_played,0) as mandarin_duel_played
            FROM user_data
            WHERE user_id = ?
            """,
            (user_id,),
        )
        row = await cursor.fetchone()
    if not row:
        return {
            "gender": None,
            "city": None,
            "quote": None,
            "balance": 0,
            "mandarin_eaten": 0,
            "mandarin_duel_wins": 0,
            "mandarin_duel_played": 0,
        }
    return dict(row)


async def update_user_profile(
    user_id: int,
    gender: Optional[str] = None,
    city: Optional[str] = None,
    quote: Optional[str] = None,
):
    """Оновлює поля профілю. None означає 'не змінювати'. Порожній рядок → NULL."""
    await ensure_user_data(user_id, None, None, None, update_names=False)
    fields = {}
    if gender is not None:
        fields["gender"] = gender.strip() or None
    if city is not None:
        fields["city"] = city.strip() or None
    if quote is not None:
        fields["quote"] = quote.strip() or None
    if not fields:
        return
    set_sql = ", ".join([f"{k} = ?" for k in fields.keys()])
    params = list(fields.values()) + [user_id]
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(f"UPDATE user_data SET {set_sql} WHERE user_id = ?", tuple(params))
        await db.commit()


# --- (НОВЕ) Новорічний режим ---
async def set_new_year_mode(chat_id: int, mode: str) -> None:
    """Встановлює режим нового року для чату: 'auto' | 'on' | 'off'."""
    mode = (mode or "auto").lower().strip()
    if mode not in ("auto", "on", "off"):
        mode = "auto"
    async with aiosqlite.connect(DB_PATH) as db:
        # гарантуємо, що рядок існує
        await db.execute("INSERT OR IGNORE INTO chat_settings (chat_id) VALUES (?)", (chat_id,))
        await db.execute("UPDATE chat_settings SET new_year_mode = ? WHERE chat_id = ?", (mode, chat_id))
        await db.commit()



async def mems_update_global_stats(user_id: int, chat_id: int, name: str, is_win: bool = False, score_add: int = 0, games_played_add: int = 0):
    """Оновлює глобальну статистику для гри 'Мемчики та котики'."""
    async with aiosqlite.connect(DB_PATH) as db:
        # Спочатку перевіряємо, чи існує запис
        cursor = await db.execute(
            "SELECT wins, total_score, games_played FROM mems_global_stats WHERE user_id = ? AND chat_id = ?",
            (user_id, chat_id)
        )
        row = await cursor.fetchone()
        
        if row:
            # Оновлюємо існуючий запис
            new_wins = row[0] + (1 if is_win else 0)
            new_score = row[1] + score_add
            new_games = row[2] + games_played_add
            await db.execute(
                "UPDATE mems_global_stats SET name = ?, wins = ?, total_score = ?, games_played = ? WHERE user_id = ? AND chat_id = ?",
                (name, new_wins, new_score, new_games, user_id, chat_id)
            )
        else:
            # Створюємо новий запис
            wins = 1 if is_win else 0
            score = score_add
            games = games_played_add
            await db.execute(
                "INSERT INTO mems_global_stats (user_id, chat_id, name, wins, total_score, games_played) VALUES (?, ?, ?, ?, ?, ?)",
                (user_id, chat_id, name, wins, score, games)
            )
        await db.commit()


async def mems_get_global_stats() -> Dict[str, Dict[str, Any]]:
    """Повертає глобальну статистику у форматі {user_id: stats_dict} для сумісності з raw грою."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT user_id, chat_id, name, wins, total_score, games_played
            FROM mems_global_stats
            """
        )
        rows = await cursor.fetchall()
    
    stats: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        user_id = str(row["user_id"])
        chat_id = row["chat_id"]
        name = row["name"] or "Unknown"
        wins = row["wins"] or 0
        total_score = row["total_score"] or 0
        games_played = row["games_played"] or 0
        
        if user_id not in stats:
            stats[user_id] = {
                "name": name,
                "wins": 0,
                "total_score": 0,
                "games_played": 0,
            }
        # Aggregate across chats
        stats[user_id]["wins"] += wins
        stats[user_id]["total_score"] += total_score
        stats[user_id]["games_played"] += games_played
        # Update name if different (take the latest)
        stats[user_id]["name"] = name
    
    return stats


async def mems_get_top(limit: int = 10, chat_id: Optional[int] = None) -> List[Dict[str, Any]]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        if chat_id is None:
            # Global top: aggregate across all chats
            cur = await db.execute(
                """
                SELECT user_id, name, SUM(total_score) as total_score, SUM(wins) as wins, SUM(games_played) as games_played
                FROM mems_global_stats
                GROUP BY user_id
                ORDER BY total_score DESC, wins DESC, games_played DESC
                LIMIT ?
                """,
                (limit,),
            )
        else:
            # Chat-specific top
            cur = await db.execute(
                """
                SELECT user_id, name, total_score, wins, games_played
                FROM mems_global_stats
                WHERE chat_id = ?
                ORDER BY total_score DESC, wins DESC, games_played DESC
                LIMIT ?
                """,
                (chat_id, limit),
            )
        rows = await cur.fetchall()
    result: List[Dict[str, Any]] = []
    for r in rows:
        result.append(
            {
                "user_id": int(r["user_id"]),
                "name": str(r["name"] or "Unknown"),
                "total_score": int(r["total_score"] or 0),
                "wins": int(r["wins"] or 0),
                "games": int(r["games_played"] or 0),
            }
        )
    return result


async def get_new_year_mode(chat_id: int) -> str:
    """Повертає режим нового року для чату: 'auto' | 'on' | 'off'."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT new_year_mode FROM chat_settings WHERE chat_id = ?", (chat_id,))
        row = await cursor.fetchone()
    if not row or row[0] is None:
        return "auto"
    val = str(row[0]).lower().strip()
    return val if val in ("auto", "on", "off") else "auto"
