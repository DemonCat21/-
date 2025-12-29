import logging
import os
import random
import json
import asyncio
import html
import functools
from typing import Dict, List, Optional, Union, Set

from bot.core.database import (
    update_user_balance,
    mems_load_games_state,
    mems_save_game_state,
    mems_delete_game_state,
    mems_update_global_stats,
    mems_get_global_stats,
    mems_get_situations,
    mems_insert_situations_if_empty,
    mems_get_cards_cache,
    mems_upsert_card,
    get_mems_settings_for_chat,
    set_mems_setting_for_chat,
)
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InlineQueryResultCachedPhoto,
    InputTextMessageContent,
    ChatMember,
    InlineQueryResultArticle,
    InputMediaPhoto,
)
from telegram.constants import ParseMode, ChatType
from telegram.ext import (
    ApplicationBuilder,
    ContextTypes,
    CommandHandler,
    CallbackQueryHandler,
    InlineQueryHandler,
    MessageHandler,
    filters,
)
from telegram.error import BadRequest, Forbidden, TelegramError, RetryAfter

# ==============================================================================
# КОНФІГУРАЦІЯ
# ==============================================================================
# ⚠️ УВАГА: Заміни на свій токен або використовуй os.getenv("BOT_TOKEN")
TOKEN = "8460777745:AAEH2VqOJd1r-UOwQHVAQsf5cMEwiqxkEv4" 
OWNER_ID = 1064174112 
MEMES_FOLDER = "memes"

DB_FILE = "card_ids.json"
SETTINGS_FILE = "settings.json"
GAMES_STATE_FILE = "games_state.json" 
GLOBAL_STATS_FILE = "global_stats.json"
SITUATIONS_FILE = "situations.json"

DEFAULT_SETTINGS = {
    "turn_time": 60,
    "vote_time": 45,
    "max_players": 10,
    "min_players": 2,
    "win_score": 10,
    "hand_size": 6,
    "max_rounds": 10
}

SETTINGS_PRESETS = {
    "turn_time": [30, 45, 60, 90, 120, 180],
    "vote_time": [15, 30, 45, 60, 90, 120],
    "max_players": [2, 3, 4, 5, 6, 7, 8, 10, 15],
    "min_players": [2, 3, 4, 5],
    "win_score": [5, 10, 15, 20, 25, 30, 50, 0],
    "hand_size": [4, 5, 6, 7, 8],
    "max_rounds": [5, 10, 15, 20, 0]
}

MIN_PLAYERS = 2
LOBBY_TIME = 120 
AFK_LIMIT = 3 

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# ==============================================================================
# АСИНХРОННІ УТИЛІТИ (Щоб не блокувати бота)
# ==============================================================================

async def run_async_io(func, *args):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, functools.partial(func, *args))

def _load_json_sync(filename):
    if os.path.exists(filename):
        try:
            with open(filename, 'r', encoding='utf-8') as f:
                content = f.read().strip()
                if not content: return {}
                return json.loads(content)
        except Exception as e:
            logger.error(f"Error loading {filename}: {e}")
            return {}
    return {}

def _save_json_sync(filename, data):
    try:
        temp_file = filename + ".tmp"
        with open(temp_file, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=4, ensure_ascii=False)
        os.replace(temp_file, filename)
    except Exception as e:
        logger.error(f"Error saving {filename}: {e}")

async def load_json(filename):
    return await run_async_io(_load_json_sync, filename)

async def save_json(filename, data):
    await run_async_io(_save_json_sync, filename, data)

# ==============================================================================
# ЛОГІКА ДАНИХ
# ==============================================================================

DEFAULT_SITUATIONS = [
    "Коли Настоятелька знайшла у твоїй келії не Біблію, а...",
    "Твоє обличчя, коли кіт перекинув вино на білу рясу.",
    "Сестра Агата помітила, що ти дивишся не на ікону, а на...",
    "Коли випадково нявкнув під час обітниці мовчання.",
    "Що насправді ховають коти під рясами монашок?",
    "Заходить кіт у сповідальню і каже..."
]

# Кеш в пам'яті для швидкого доступу
CACHED_SITUATIONS = []
CACHED_CARDS = {}

# Винагорода за перемогу (м'ятки)
MEMS_WIN_REWARD = 40

async def init_data_cache():
    global CACHED_SITUATIONS, CACHED_CARDS
    situations = await mems_get_situations()
    if situations:
        CACHED_SITUATIONS = situations
    else:
        CACHED_SITUATIONS = list(DEFAULT_SITUATIONS)
        await mems_insert_situations_if_empty(CACHED_SITUATIONS)
    
    CACHED_CARDS = await mems_get_cards_cache()

async def add_new_situation(text: str):
    if text not in CACHED_SITUATIONS:
        CACHED_SITUATIONS.append(text)
        await save_json(SITUATIONS_FILE, CACHED_SITUATIONS)
        return True
    return False

class BotKickedError(Exception):
    pass

async def safe_send(bot, chat_id, text=None, photo=None, **kwargs):
    retries = 3
    while retries > 0:
        try:
            if photo:
                return await bot.send_photo(chat_id, photo=photo, caption=text, **kwargs)
            else:
                return await bot.send_message(chat_id, text=text, **kwargs)
        except RetryAfter as e:
            wait_time = e.retry_after + 1
            logger.warning(f"FloodWait hit! Sleeping for {wait_time}s...")
            await asyncio.sleep(wait_time)
            retries -= 1
        except Forbidden:
            logger.warning(f"Bot kicked from chat {chat_id}.")
            raise BotKickedError()
        except BadRequest as e:
            if "chat not found" in str(e).lower():
                raise BotKickedError()
            logger.warning(f"BadRequest to {chat_id}: {e}")
            return None
        except Exception as e:
            logger.error(f"Unexpected error sending to {chat_id}: {e}")
            return None
    return None

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Exception while handling an update:", exc_info=context.error)

# ==============================================================================
# КЛАСИ ГРИ
# ==============================================================================

class Player:
    def __init__(self, user_id: int, first_name: str, username: str, score=0, cards=None):
        self.id = user_id
        self.first_name = first_name
        self.username = username or ""
        self.score = score
        self.cards: List[str] = cards if cards else []
        self.chosen_card: Optional[str] = None
        self.round_votes = 0
        self.has_voted = False
        self.afk_rounds = 0

    def get_link(self):
        safe_name = html.escape(self.first_name)
        return f"<a href='tg://user?id={self.id}'>{safe_name}</a>"

    def to_dict(self):
        return {
            "id": self.id,
            "first_name": self.first_name,
            "username": self.username,
            "score": self.score,
            "cards": self.cards
        }

    @classmethod
    def from_dict(cls, data):
        return cls(
            user_id=data["id"],
            first_name=data["first_name"],
            username=data.get("username", ""),
            score=data["score"],
            cards=data["cards"]
        )

class Game:
    def __init__(self, chat_id: int, settings: dict):
        self.chat_id = chat_id
        self.players: Dict[int, Player] = {}
        self.is_started = False
        self.state = "LOBBY"
        self.round_number = 0
        self.current_situation = ""
        self.settings = settings
        # Ensure all default settings are present
        for key, val in DEFAULT_SETTINGS.items():
            if key not in self.settings:
                self.settings[key] = val
        self.deck: List[str] = []
        self.used_cards: List[str] = [] 
        self.used_situations: List[str] = [] # Щоб не повторюватись
        
        self.lobby_message_id: Optional[int] = None
        self.round_message_id: Optional[int] = None
        self.voting_message_ids: List[int] = []
        
        self.lobby_timer_job = None
        self.timer_job = None
        self.processing_lock = False # Простий лок для запобігання race conditions
        self.voters: Set[int] = set() # Хто вже проголосував у поточному раунді

    def get_situation(self):
        available = [s for s in CACHED_SITUATIONS if s not in self.used_situations]
        if not available:
            self.used_situations = []
            available = list(CACHED_SITUATIONS)
        
        sit = random.choice(available)
        self.used_situations.append(sit)
        return sit

    def deal_cards(self, all_cards_ids: List[str]):
        if not all_cards_ids: return
        hand_size = self.settings.get('hand_size', 6)
        
        # Перевірка безпеки: чи достатньо карт взагалі існує
        total_needed = len(self.players) * hand_size
        if len(all_cards_ids) < total_needed and len(all_cards_ids) < 10:
             # Якщо карт критично мало, нічого не робимо, це хендлиться в логіці
             pass

        for player in self.players.values():
            needed = hand_size - len(player.cards)
            attempts = 0 # Захист від нескінченного циклу
            
            while needed > 0 and attempts < 50:
                attempts += 1
                if not self.deck:
                    # Решафл
                    available_for_deck = [c for c in all_cards_ids if c not in self.used_cards]
                    # Якщо навіть після викидання використаних карт пусто (всі карти на руках або використані)
                    # Тоді скидаємо used_cards
                    if not available_for_deck:
                        self.used_cards = [] 
                        available_for_deck = list(all_cards_ids)

                    # Фільтруємо карти, які вже на руках у гравців
                    cards_in_hands = {c for p in self.players.values() for c in p.cards}
                    self.deck = [c for c in available_for_deck if c not in cards_in_hands]
                    
                    random.shuffle(self.deck)
                    
                    # Якщо все ще пусто - значить карт фізично не вистачає
                    if not self.deck: 
                        break 
                
                if self.deck:
                    card = self.deck.pop()
                    player.cards.append(card)
                    needed -= 1

    def cleanup_jobs(self):
        for job in [self.lobby_timer_job, self.timer_job]:
            if job:
                try: job.schedule_removal()
                except Exception: pass
        self.lobby_timer_job = None
        self.timer_job = None

    def to_dict(self):
        return {
            "chat_id": self.chat_id,
            "is_started": self.is_started,
            "state": self.state,
            "round_number": self.round_number,
            "current_situation": self.current_situation,
            "settings": self.settings,
            "deck": self.deck,
            "used_cards": self.used_cards,
            "used_situations": self.used_situations,
            "players": {str(uid): p.to_dict() for uid, p in self.players.items()},
            "voters": list(self.voters)
        }

    @classmethod
    def from_dict(cls, data):
        game = cls(data["chat_id"], data["settings"])
        game.is_started = data["is_started"]
        game.state = data["state"]
        game.round_number = data["round_number"]
        game.current_situation = data["current_situation"]
        game.deck = data.get("deck", [])
        game.used_cards = data.get("used_cards", [])
        game.used_situations = data.get("used_situations", [])
        game.voters = set(data.get("voters", []))
        for uid, p_data in data["players"].items():
            game.players[int(uid)] = Player.from_dict(p_data)
        return game

games: Dict[int, Game] = {}

# ==============================================================================
# ЗБЕРЕЖЕННЯ ТА СТАТИСТИКА
# ==============================================================================

async def save_games_state():
    for gid, game in games.items():
        await mems_save_game_state(gid, game.to_dict())

def delete_game(chat_id: int):
    if chat_id in games:
        games[chat_id].cleanup_jobs()
        del games[chat_id]
        # Запускаємо збереження "на фоні"
        asyncio.create_task(mems_delete_game_state(chat_id))

async def load_games_on_startup(application):
    await init_data_cache() # Завантажуємо кеш ситуацій і карт
    
    data = await mems_load_games_state()
    if not data: return
    
    logger.info(f"Відновлення {len(data)} ігор...")
    for str_gid, g_data in data.items():
        gid = int(str_gid)
        try:
            game = Game.from_dict(g_data)
            games[gid] = game
            
            if not game.is_started and game.state == "LOBBY":
                game.lobby_timer_job = application.job_queue.run_once(
                    timer_lobby_end, LOBBY_TIME, chat_id=gid, name=f"lobby_{gid}"
                )
                try:
                    await safe_send(application.bot, gid, "🐈 <b>Бот перезапущено.</b> Таймер лобі скинуто.", parse_mode=ParseMode.HTML)
                except BotKickedError:
                    delete_game(gid)

            elif game.is_started:
                asyncio.create_task(restore_round(application, gid))
                
        except Exception as e:
            logger.error(f"Failed to load game {str_gid}: {e}")

async def restore_round(app, chat_id):
    game = games.get(chat_id)
    if not game: return
    await asyncio.sleep(2)
    try:
        await safe_send(app.bot, chat_id, "🐈 <b>Бот перезапущено.</b> Продовжуємо гру...", parse_mode=ParseMode.HTML)
        # Перезапускаємо поточний раунд, щоб оновити таймери і стани
        game.round_number -= 1 
        app.job_queue.run_once(
            lambda ctx: start_round_logic(ctx.bot, ctx.job.chat_id, app.job_queue), 
            1, chat_id=chat_id
        )
    except BotKickedError:
        delete_game(chat_id)
    except Exception as e:
        logger.error(f"Failed restore game {chat_id}: {e}")

async def update_global_stats(user_id: int, chat_id: int, name: str, is_win: bool = False, score_add: int = 0, games_played_add: int = 0):
    await mems_update_global_stats(user_id, chat_id, name, is_win, score_add, games_played_add)

async def get_chat_settings(chat_id: int) -> dict:
    settings = await get_mems_settings_for_chat(chat_id)
    # Мапимо ключі з mems_ префіксу
    mapped = {}
    for key, val in settings.items():
        if key.startswith("mems_"):
            short_key = key[5:]  # remove "mems_"
            mapped[short_key] = val
    # Додаємо відсутні з DEFAULT_SETTINGS
    for key, val in DEFAULT_SETTINGS.items():
        if key not in mapped:
            mapped[key] = val
    return mapped

async def update_chat_setting(chat_id: int, key: str, value: int):
    await set_mems_setting_for_chat(chat_id, f"mems_{key}", value)

# ==============================================================================
# КОМАНДИ
# ==============================================================================

async def is_admin_in_chat(user_id: int, chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if user_id == OWNER_ID: return True
    try:
        member = await context.bot.get_chat_member(chat_id, user_id)
        return member.status in [ChatMember.OWNER, ChatMember.ADMINISTRATOR]
    except:
        return False

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if args and args[0].startswith("set_"):
        try:
            target_chat_id = int(args[0].split("_")[1])
            await show_settings_menu_pm(update, context, target_chat_id)
        except:
            await update.message.reply_text("Помилка доступу.")
        return

    await update.message.reply_text(
        "🐈 <b>Вітаю в Келії.</b>\n\n"
        "Я бот для гри в меми.\n\n"
        "1. <b>Набір:</b> Запускаєш /newgame, приєднуєшся.\n"
        "2. <b>Хід:</b> Бот дає ситуацію, ти обираєш найприкольніший мем зі своєї колоди\n"
        "3. <b>Голосування:</b> Гравці голосують за найкращий прікол.\n"
        "4. <b>Перемога:</b> Граємо до досягнення ліміту очок.\n\n"
        "<b>Мої команди:</b>\n"
        "/newgame - Створити гру\n"
        "/stop - Зупинити гру\n"
        "/leave - Вийти з гри\n"
        "/kick - Вигнати гравця\n"
        "/add_sit - Додати ситуацію (адмін)\n"
        "/top - Топ гравців\n",
        parse_mode=ParseMode.HTML
    )

async def cmd_add_situation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID and not await is_admin_in_chat(update.effective_user.id, update.effective_chat.id, context):
        await update.message.reply_text("Тільки для Настоятельки (адміна).")
        return

    if not context.args:
        await update.message.reply_text("Пиши: /add_sit Текст ситуації...")
        return

    text = " ".join(context.args)
    if await add_new_situation(text):
        await update.message.reply_text(f"✅ Додано: <i>{html.escape(text)}</i>", parse_mode=ParseMode.HTML)
    else:
        await update.message.reply_text("Така вже є.")

async def cmd_init_cards(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID: return
    
    if not os.path.exists(MEMES_FOLDER):
        try:
            os.makedirs(MEMES_FOLDER)
            await update.message.reply_text(f"Створено папку {MEMES_FOLDER}. Закинь туди картинки.")
        except Exception as e:
            await update.message.reply_text(f"Помилка створення папки: {e}")
        return

    force = (context.args and context.args[0].lower() == "force")
    
    # Використовуємо глобальний кеш
    global CACHED_CARDS
    if not force and CACHED_CARDS:
        existing = CACHED_CARDS.copy()
    else:
        existing = {}
    
    try:
        files = [f for f in os.listdir(MEMES_FOLDER) if f.lower().endswith(('.png', '.jpg', '.jpeg', '.gif', '.webp'))]
    except Exception as e:
        await update.message.reply_text(f"Помилка читання папки: {e}")
        return

    if not files:
        await update.message.reply_text(f"Папка {MEMES_FOLDER} пуста!")
        return
    
    status = await update.message.reply_text(f"⏳ Скануємо архіви ({len(files)})...")
    
    count = 0
    for f in files:
        if f in existing and not force: continue
        try:
            path = os.path.join(MEMES_FOLDER, f)
            with open(path, 'rb') as ph:
                try:
                    m = await context.bot.send_photo(update.effective_chat.id, ph, disable_notification=True)
                    file_id = m.photo[-1].file_id
                    existing[f] = file_id
                    await mems_upsert_card(f, file_id)
                    await m.delete()
                    count += 1
                except RetryAfter as e:
                    logger.warning(f"Flood limit in init. Sleep {e.retry_after}")
                    await asyncio.sleep(e.retry_after + 2)
                
                await asyncio.sleep(0.5) 
        except Exception as e:
            logger.error(f"Err {f}: {e}")

    # await save_json(DB_FILE, existing)  # Видалено, тепер зберігається в БД
    CACHED_CARDS = existing
    try:
        await safe_send(context.bot, update.effective_chat.id, f"✅ Оброблено. Нових карт: {count}. Всього: {len(existing)}")
    except: pass

async def cmd_top(update: Update, context: ContextTypes.DEFAULT_TYPE):
    stats = await mems_get_global_stats()
    if not stats:
        await update.message.reply_text("📜 Списки ще порожні.")
        return

    top_list = sorted(
        stats.items(), 
        key=lambda item: (item[1]['wins'], item[1]['total_score']), 
        reverse=True
    )[:10]

    text = "🍷 <b>ТОП ГРІШНИКІВ</b> 🍷\n\n"
    medals = ["👑", "🥈", "🥉"]
    
    for i, (uid, data) in enumerate(top_list):
        icon = medals[i] if i < 3 else "😼"
        safe_name = html.escape(data['name'])
        text += f"{icon} <b>{safe_name}</b>: {data['wins']} п. | {data['total_score']} б.\n"

    await update.message.reply_text(text, parse_mode=ParseMode.HTML)

# ==============================================================================
# УНІВЕРСАЛЬНИЙ ЧЕКЕР ФАЗ (Вирішує проблему "Зомбі-фаз")
# ==============================================================================

async def check_phase_completion(context: ContextTypes.DEFAULT_TYPE, game: Game):
    """
    Перевіряє, чи не час переходити до наступної фази,
    якщо хтось вийшов або всі зробили дію.
    """
    if game.processing_lock: return
    game.processing_lock = True
    
    try:
        chat_id = game.chat_id
        
        # Перевірка: чи достатньо гравців
        if len(game.players) < MIN_PLAYERS:
            delete_game(chat_id)
            try: await safe_send(context.bot, chat_id, "🚫 Гравців замало. Гру завершено.")
            except BotKickedError: pass
            return

        # Фаза ходу (PICK)
        if game.state == "PLAYING":
            if all(p.chosen_card for p in game.players.values()):
                if game.timer_job: game.timer_job.schedule_removal()
                await start_voting_phase(context, chat_id)
        
        # Фаза голосування (VOTE)
        elif game.state == "VOTING":
            if all(p.has_voted for p in game.players.values()):
                if game.timer_job: game.timer_job.schedule_removal()
                await show_results(context, chat_id)
                
    finally:
        game.processing_lock = False

# ==============================================================================
# УПРАВЛІННЯ ГРАВЦЯМИ (Kick/Leave)
# ==============================================================================

async def cmd_kick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not await is_admin_in_chat(update.effective_user.id, chat_id, context):
        await update.message.reply_text("⛔ Тільки адмін може виганяти.")
        return

    game = get_game(chat_id)
    if not game:
        await update.message.reply_text("Гра не йде.")
        return

    if not context.args:
        await update.message.reply_text("Вкажи: /kick @username або ім'я")
        return

    target = context.args[0].lstrip('@').lower()
    kicked_pid = None
    
    for pid, p in game.players.items():
        if p.username and p.username.lower() == target:
            kicked_pid = pid
            break
            
    if not kicked_pid:
        for pid, p in game.players.items():
            if p.first_name.lower() == target:
                kicked_pid = pid
                break
            
    if kicked_pid:
        kicked_p = game.players.pop(kicked_pid)
        await save_games_state()
        try: await update_lobby_message(update, context, game)
        except: pass
        await update.message.reply_text(f"👢 <b>{html.escape(kicked_p.first_name)}</b> вигнаний з келії.", parse_mode=ParseMode.HTML)
        
        # ВАЖЛИВО: Перевіряємо, чи не вплинуло це на фазу гри
        if game.is_started:
            await check_phase_completion(context, game)
    else:
        await update.message.reply_text("Гравця не знайдено (спробуй @username).")

async def cmd_leave_game(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    game = get_game(chat_id)
    
    if not game:
        await update.message.reply_text("Гра не йде.")
        return

    if user_id in game.players:
        p = game.players.pop(user_id)
        await save_games_state()
        try:
            await update.message.reply_text(f"👋 {html.escape(p.first_name)} покинув гру.")
        except: pass
        
        if not game.is_started:
            if len(game.players) == 0:
                # Немає гравців, видаляємо гру
                try:
                    if game.lobby_message_id:
                        await context.bot.delete_message(chat_id, game.lobby_message_id)
                        await context.bot.unpin_chat_message(chat_id=chat_id, message_id=game.lobby_message_id)
                except Exception:
                    pass
                delete_game(chat_id)
                try:
                    await update.message.reply_text("🛑 Гра скасована — немає учасників.")
                except: pass
            else:
                try: await update_lobby_message(update, context, game)
                except: pass
        else:
             # ВАЖЛИВО: Перевіряємо фазу при виході
             await check_phase_completion(context, game)
    else:
        await update.message.reply_text("Ти й не грав.")

# ==============================================================================
# НАЛАШТУВАННЯ
# ==============================================================================

async def cmd_settings_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if update.effective_chat.type == ChatType.PRIVATE:
        await update.message.reply_text("Налаштування доступні тільки для груп.")
        return

    if not await is_admin_in_chat(update.effective_user.id, chat_id, context):
        await update.message.reply_text("⛔ Тільки адмін змінює правила.")
        return

    bot_username = context.bot.username
    url = f"https://t.me/{bot_username}?start=set_{chat_id}"
    await update.message.reply_text(
        "⚙️ <b>Правила Келії</b>\nНалаштуй гру під себе в приваті.",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🛠 Налаштувати", url=url)]]),
        parse_mode=ParseMode.HTML
    )

async def show_settings_menu_pm(update: Update, context: ContextTypes.DEFAULT_TYPE, chat_id: int, sub_menu=None):
    settings = await get_chat_settings(chat_id)
    try:
        chat = await context.bot.get_chat(chat_id)
        title = html.escape(chat.title or "Чат")
    except:
        title = "Чат"

    base = f"set_{chat_id}"
    meta = {
        "turn": {"key": "turn_time", "label": "⏳ Хід", "unit": "с"},
        "vote": {"key": "vote_time", "label": "🙏 Голос", "unit": "с"},
        "max":  {"key": "max_players", "label": "👥 Місця", "unit": ""},
        "win":  {"key": "win_score",   "label": "🏆 Перемога", "unit": ""},
        "hand": {"key": "hand_size",   "label": "🃏 Рука", "unit": ""}
    }

    if sub_menu and sub_menu in meta:
        info = meta[sub_menu]
        real_key = info["key"]
        current_val = settings[real_key]
        options = SETTINGS_PRESETS[real_key]
        
        text = f"⚙️ <b>{info['label']}</b>\n\nЗараз: <b>{current_val} {info['unit']}</b>\nОбери:"
        kb = []
        row = []
        for opt in options:
            label = f"{opt} {info['unit']}"
            if opt == 0 and real_key == "win_score": label = "∞"
            if opt == current_val: label = f"✅ {label}"
            row.append(InlineKeyboardButton(label, callback_data=f"{base}_val_{sub_menu}_{opt}"))
            if len(row) >= 3:
                kb.append(row)
                row = []
        if row: kb.append(row)
        kb.append([InlineKeyboardButton("🔙 Назад", callback_data=f"{base}_main")])
        
    else:
        text = f"⚙️ <b>Налаштування: {title}</b>"
        def btn_text(alias):
            m = meta[alias]
            val = settings[m["key"]]
            val_str = "∞" if m["key"] == "win_score" and val == 0 else str(val)
            return f"{m['label']}: {val_str} {m['unit']}"

        kb = [
            [InlineKeyboardButton(btn_text("turn"), callback_data=f"{base}_menu_turn"),
             InlineKeyboardButton(btn_text("vote"), callback_data=f"{base}_menu_vote")],
            [InlineKeyboardButton(btn_text("max"), callback_data=f"{base}_menu_max"),
             InlineKeyboardButton(btn_text("win"), callback_data=f"{base}_menu_win")],
            [InlineKeyboardButton(btn_text("hand"), callback_data=f"{base}_menu_hand")],
            [InlineKeyboardButton("✅ Закрити", callback_data="close_settings_pm")]
        ]

    markup = InlineKeyboardMarkup(kb)
    if update.callback_query:
        await update.callback_query.message.edit_text(text, reply_markup=markup, parse_mode=ParseMode.HTML)
    else:
        await update.message.reply_text(text, reply_markup=markup, parse_mode=ParseMode.HTML)

async def cb_settings_pm_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    
    if data == "close_settings_pm":
        await query.message.delete()
        return

    try:
        parts = data.split("_")
        chat_id = int(parts[1])
        action = parts[2]
        
        if not await is_admin_in_chat(update.effective_user.id, chat_id, context):
             await query.answer("Ти більше не адмін там.", show_alert=True)
             return

        if action == "main":
            await show_settings_menu_pm(update, context, chat_id, sub_menu=None)
            await query.answer()
        elif action == "menu":
            await show_settings_menu_pm(update, context, chat_id, sub_menu=parts[3])
            await query.answer()
        elif action == "val":
            alias = parts[3]
            value = int(parts[4])
            meta_key = {
                "turn": "turn_time", "vote": "vote_time", "max": "max_players", 
                "win": "win_score", "hand": "hand_size"
            }.get(alias)
            
            if meta_key:
                await update_chat_setting(chat_id, meta_key, value)
                if chat_id in games:
                    games[chat_id].settings[meta_key] = value
                    await save_games_state()
            
            await show_settings_menu_pm(update, context, chat_id, sub_menu=None)
            await query.answer("Збережено!")

    except Exception as e:
        logger.error(f"Set err: {e}")
        await query.answer("Помилка налаштувань")

# ==============================================================================
# УПРАВЛІННЯ ГРОЮ
# ==============================================================================

def get_game(chat_id: int) -> Optional[Game]:
    return games.get(chat_id)

async def cmd_stop_game(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not await is_admin_in_chat(update.effective_user.id, chat_id, context):
        await update.message.reply_text("⛔ Тільки Настоятель.")
        return
        
    if chat_id in games:
        game = games[chat_id]
        
        msgs_to_delete = [game.lobby_message_id, game.round_message_id] + game.voting_message_ids
        for mid in msgs_to_delete:
            if mid:
                try: await context.bot.delete_message(chat_id, mid)
                except: pass

        # Відкріплюємо повідомлення гри
        try:
            await context.bot.unpin_all_chat_messages(chat_id)
        except Exception:
            pass

        delete_game(chat_id)
        try:
            await safe_send(context.bot, chat_id, "🛑 <b>Кінець гри.</b>", parse_mode=ParseMode.HTML)
        except BotKickedError: pass
    else:
        await update.message.reply_text("Тут і так тихо.")

async def cmd_newgame(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if not chat:
        return
    chat_id = chat.id

    # підтримка старту як з команди, так і з inline-меню (/newgame)
    msg = update.message or (update.callback_query.message if update.callback_query else None)

    if chat.type == ChatType.PRIVATE:
        if msg:
            await msg.reply_text("Ігри тільки в групах!")
        else:
            await context.bot.send_message(chat_id, "Ігри тільки в групах!")
        return

    if msg:
        try:
            await msg.delete()
        except Exception:
            pass

    if chat_id in games and games[chat_id].state not in ["LOBBY_END", ""]:
        try: await safe_send(context.bot, chat_id, "❌ Гра вже йде. /stop щоб зупинити.")
        except BotKickedError: delete_game(chat_id)
        return
    
    settings = await get_chat_settings(chat_id)
    game = Game(chat_id, settings)
    games[chat_id] = game
    await save_games_state()
    
    game.lobby_timer_job = context.job_queue.run_once(
        timer_lobby_end, LOBBY_TIME, chat_id=chat_id, name=f"lobby_{chat_id}"
    )
    
    try:
        await update_lobby_message(update, context, game, new=True)
    except BotKickedError:
        delete_game(chat_id)

async def update_lobby_message(update: Update, context: ContextTypes.DEFAULT_TYPE, game: Game, new=False):
    players_list = "\n".join([f"🐾 {p.get_link()}" for p in game.players.values()]) or "Поки що пусто..."
    
    text = (
        f"🍷 <b>ЗБІР У КЕЛІЇ</b>\n\n"
        f"Учасники [{len(game.players)}/{game.settings['max_players']}]:\n{players_list}\n"
        f"\n<i>Старт автоматично через {LOBBY_TIME}с або кнопкою.</i>"
    )
    
    kb = [
        [InlineKeyboardButton("➕ Зайти / Вийти", callback_data="join_leave")],
        [InlineKeyboardButton("🔔 Почати гру", callback_data="start_game_force")]
    ]

    if new:
        msg = await safe_send(context.bot, game.chat_id, text, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.HTML)
        if msg: 
            game.lobby_message_id = msg.message_id
            # Прикріплюємо повідомлення гри
            try:
                await context.bot.pin_chat_message(chat_id=game.chat_id, message_id=msg.message_id, disable_notification=True)
            except Exception:
                pass  # Якщо немає прав, ігноруємо
    elif game.lobby_message_id:
        try:
            await context.bot.edit_message_text(chat_id=game.chat_id, message_id=game.lobby_message_id, text=text, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.HTML)
        except BadRequest: pass 
        except Exception: pass

async def timer_lobby_end(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.chat_id
    game = get_game(chat_id)
    if not game or game.is_started: return
    
    if len(game.players) >= game.settings['min_players']:
        try:
            await safe_send(context.bot, chat_id, f"✅ Час вийшов. Починаємо.")
            await start_game_logic(context.bot, game, context.job_queue)
        except BotKickedError: delete_game(chat_id)
    else:
        try:
            if game.lobby_message_id:
                await context.bot.delete_message(chat_id, game.lobby_message_id)
                await context.bot.unpin_chat_message(chat_id=chat_id, message_id=game.lobby_message_id)
        except Exception:
            pass
        delete_game(chat_id)
        try: await safe_send(context.bot, chat_id, "❌ Нікого немає. Розходимось.")
        except BotKickedError: pass

async def start_game_logic(bot, game: Game, job_queue):
    # Оновлюємо кеш карт перед стартом, щоб бути впевненими
    global CACHED_CARDS
    if not CACHED_CARDS:
        CACHED_CARDS = await load_json(DB_FILE)

    card_db = list(CACHED_CARDS.values())
    if not card_db:
        try: await safe_send(bot, game.chat_id, "❌ База картинок пуста! Адмін має запустити /init_cards")
        except BotKickedError: pass
        delete_game(game.chat_id)
        return

    game.cleanup_jobs()
    if game.lobby_message_id:
        try: await bot.delete_message(game.chat_id, game.lobby_message_id)
        except: pass
            
    game.is_started = True
    
    random.shuffle(card_db)
    game.deck = list(card_db)
    game.deal_cards(card_db)
    
    await save_games_state()
    
    try:
        await safe_send(bot, game.chat_id, "🕯 <b>ЗАЧИНЯЙТЕ ДВЕРІ. ГРА ПОЧАЛАСЬ.</b>", parse_mode=ParseMode.HTML)
        await start_round_logic(bot, game.chat_id, job_queue)
    except BotKickedError:
        delete_game(game.chat_id)

async def cb_join(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    game = get_game(query.message.chat_id)
    if not game or game.is_started: 
        await query.answer("Гра вже йде або закінчилась.", show_alert=True)
        return
    
    user = query.from_user
    if user.id in game.players:
        del game.players[user.id]
        res = "Вийшов."
        # Перевіряємо, чи залишилися гравці
        if len(game.players) == 0:
            await save_games_state()
            try:
                if game.lobby_message_id:
                    await context.bot.delete_message(game.chat_id, game.lobby_message_id)
                    await context.bot.unpin_chat_message(chat_id=game.chat_id, message_id=game.lobby_message_id)
            except Exception:
                pass
            delete_game(game.chat_id)
            await query.answer("Гра скасована — немає учасників.")
            return
    else:
        if len(game.players) >= game.settings['max_players']:
            await query.answer("Немає місць!", show_alert=True)
            return
        game.players[user.id] = Player(user.id, user.first_name, user.username)
        res = "Ти з нами."
        
    await save_games_state()
    try:
        await update_lobby_message(update, context, game)
        await query.answer(res)
    except BotKickedError:
        delete_game(game.chat_id)

async def cb_start_game(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    chat_id = query.message.chat_id
    
    game = get_game(chat_id)
    if not game or len(game.players) < MIN_PLAYERS:
        await query.answer(f"Треба мінімум {MIN_PLAYERS} гравців!", show_alert=True)
        return
        
    await query.answer("Поїхали!")
    try:
        await start_game_logic(context.bot, game, context.job_queue)
    except BotKickedError:
        delete_game(chat_id)

# ==============================================================================
# ЛОГІКА РАУНДІВ
# ==============================================================================

async def start_round_logic(bot, chat_id: int, job_queue):
    game = get_game(chat_id)
    if not game: return
    
    # --- АВТО-КІК AFK ГРАВЦІВ ---
    kicked_players = []
    for uid, p in list(game.players.items()):
        if p.afk_rounds >= AFK_LIMIT:
            del game.players[uid]
            kicked_players.append(p)
            
    if kicked_players:
        kicked_names = ", ".join([html.escape(p.first_name) for p in kicked_players])
        try:
            await safe_send(bot, chat_id, 
                f"🥾 <b>Авто-кік:</b> Гравці <b>{kicked_names}</b> вигнані за неактивність ({AFK_LIMIT} раундів).", 
                parse_mode=ParseMode.HTML
            )
        except BotKickedError:
            delete_game(chat_id)
            return

    if len(game.players) < MIN_PLAYERS:
        delete_game(chat_id)
        try: await safe_send(bot, chat_id, "🚫 Гравців замало. Гру завершено.")
        except BotKickedError: pass
        return

    game.round_number += 1
    game.state = "PLAYING"
    
    for p in game.players.values():
        p.chosen_card = None
        p.has_voted = False
        p.round_votes = 0

    game.voting_message_ids = []
    game.current_situation = game.get_situation()
    
    kb = [[InlineKeyboardButton("😼 Обрати карту", switch_inline_query_current_chat="")]]
    
    if game.round_message_id:
        try: await bot.delete_message(chat_id, game.round_message_id)
        except: pass

    safe_situation = html.escape(game.current_situation)
    
    try:
        msg = await safe_send(
            bot, chat_id,
            f"📝 <b>РАУНД {game.round_number}</b>\n\n"
            f"<i>{safe_situation}</i>\n\n"
            f"⏳ Час на хід: {game.settings['turn_time']} с.",
            reply_markup=InlineKeyboardMarkup(kb),
            parse_mode=ParseMode.HTML
        )
        if msg: 
            game.round_message_id = msg.message_id
            # Відкріплюємо лобі, раунд не прикріплюємо
            try:
                if game.lobby_message_id:
                    await bot.unpin_chat_message(chat_id=chat_id, message_id=game.lobby_message_id)
            except Exception:
                pass
    except BotKickedError:
        delete_game(chat_id)
        return
        
    await save_games_state()
    game.timer_job = job_queue.run_once(timer_turn_end, game.settings['turn_time'], chat_id=chat_id)

async def inline_query_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.inline_query
    user_id = query.from_user.id
    
    active_game = None
    for g in games.values():
        if user_id in g.players and g.state == "PLAYING":
            active_game = g
            break
            
    if not active_game: return

    player = active_game.players[user_id]
    results = []
    
    if not player.cards:
        results.append(InlineQueryResultArticle("no", "Немає карт", InputTextMessageContent("У мене скінчились карти :(")))
    elif player.chosen_card:
        results.append(InlineQueryResultArticle("done", "Вже обрано", InputTextMessageContent("Я вже зробив хід.")))
    else:
        for idx, file_id in enumerate(player.cards):
            unique_id = f"{active_game.chat_id}_{active_game.round_number}_{idx}"
            command_val = f"{active_game.chat_id}:{active_game.round_number}:{idx}"
            
            try:
                results.append(
                    InlineQueryResultCachedPhoto(
                        id=unique_id,
                        photo_file_id=file_id,
                        title=f"Варіант {idx + 1}", 
                        input_message_content=InputTextMessageContent(f"/pick {command_val}")
                    )
                )
            except Exception as e:
                logger.warning(f"Invalid photo file_id {file_id}: {e}")
                # Пропускаємо невалідну карту
        
    try:
        await query.answer(results, cache_time=0, is_personal=True)
    except Exception as e:
        logger.error(f"Error answering inline query: {e}")
        # Логуємо невалідні file_id для діагностики
        invalid_ids = [file_id for idx, file_id in enumerate(player.cards) if len(results) > idx and isinstance(results[idx], InlineQueryResultCachedPhoto)]
        logger.warning(f"Invalid photo file_ids: {invalid_ids}")
        # Спробувати без фото, тільки текст
        text_results = []
        for idx, file_id in enumerate(player.cards):
            unique_id = f"{active_game.chat_id}_{active_game.round_number}_{idx}"
            command_val = f"{active_game.chat_id}:{active_game.round_number}:{idx}"
            text_results.append(
                InlineQueryResultArticle(
                    id=unique_id,
                    title=f"Варіант {idx + 1}",
                    input_message_content=InputTextMessageContent(f"/pick {command_val}")
                )
            )
        await query.answer(text_results, cache_time=0, is_personal=True)

async def cmd_pick_card(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try: await update.message.delete()
    except: pass

    try:
        data = context.args[0].split(":")
        chat_id = int(data[0])
        round_num = int(data[1])
        idx = int(data[2])
    except: return

    game = get_game(chat_id)
    if not game or game.state != "PLAYING": return
    
    if game.round_number != round_num:
        return

    player = game.players.get(update.effective_user.id)
    if not player or player.chosen_card: return

    if 0 <= idx < len(player.cards):
        # Блокування для атомарності
        player.chosen_card = player.cards.pop(idx)
        player.afk_rounds = 0 
        game.used_cards.append(player.chosen_card)
        
        await save_games_state()
        
        safe_name = html.escape(player.first_name)
        try:
            msg = await safe_send(context.bot, chat_id, f"✅ <b>{safe_name}</b> зробив хід.", parse_mode=ParseMode.HTML)
            if msg:
                context.job_queue.run_once(lambda ctx: ctx.bot.delete_message(chat_id, msg.message_id), 3)
        except BotKickedError:
            delete_game(chat_id)
            return
        
        await check_phase_completion(context, game)

async def timer_turn_end(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.chat_id
    game = get_game(chat_id)
    if not game: return

    afk_names = []
    for p in game.players.values():
        if not p.chosen_card and p.cards:
            p.chosen_card = p.cards.pop(random.randint(0, len(p.cards) - 1))
            game.used_cards.append(p.chosen_card)
            p.afk_rounds += 1 
            afk_names.append(html.escape(p.first_name))
    
    if afk_names:
        txt = ", ".join(afk_names)
        try:
            await safe_send(context.bot, chat_id, f"💤 <b>Проспали:</b> {txt}. Бот обрав за них.", parse_mode=ParseMode.HTML)
        except BotKickedError:
            delete_game(chat_id)
            return

    await start_voting_phase(context, chat_id)

async def start_voting_phase(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    game = get_game(chat_id)
    game.state = "VOTING"
    game.voters.clear()  # Очищаємо список проголосувавших
    
    candidates = [p for p in game.players.values() if p.chosen_card]
    random.shuffle(candidates)
    
    if not candidates:
        try: await safe_send(context.bot, chat_id, "🗿 Технічна заминка. Переграємо раунд.")
        except BotKickedError: 
            delete_game(chat_id)
            return
        await start_round_logic(context.bot, chat_id, context.job_queue)
        return
        
    if game.round_message_id:
        try: await context.bot.delete_message(chat_id, game.round_message_id)
        except: pass

    safe_situation = html.escape(game.current_situation)
    try:
        m1 = await safe_send(context.bot, chat_id, 
            f"😶‍🌫️ <b>ГОЛОСУВАННЯ</b>\n\nСитуація: <b>{safe_situation}</b>", 
            parse_mode=ParseMode.HTML
        )
        if m1: game.voting_message_ids.append(m1.message_id)
    except BotKickedError:
        delete_game(chat_id)
        return

    media_group = []
    vote_buttons = []
    
    for idx, p in enumerate(candidates):
        num = idx + 1
        media_group.append(InputMediaPhoto(media=p.chosen_card, caption=f"Варіант {num}"))
        vote_buttons.append(InlineKeyboardButton(f"🖤 {num}", callback_data=f"vote_{p.id}"))

    # Відправка карток
    chunk_size = 10
    for i in range(0, len(media_group), chunk_size):
        chunk = media_group[i:i + chunk_size]
        try:
            msgs = await context.bot.send_media_group(chat_id, chunk)
            for m in msgs:
                game.voting_message_ids.append(m.message_id)
        except Exception as e:
            logger.error(f"Media group failed, trying fallback: {e}")
            for item in chunk:
                try:
                    m = await safe_send(context.bot, chat_id, photo=item.media, text=item.caption)
                    if m: game.voting_message_ids.append(m.message_id)
                except BotKickedError:
                    delete_game(chat_id)
                    return

    rows = [vote_buttons[i:i + 4] for i in range(0, len(vote_buttons), 4)]
    try:
        m2 = await safe_send(
            context.bot, chat_id, 
            f"👇 Обирай смішніше (ще {game.settings['vote_time']}с):", 
            reply_markup=InlineKeyboardMarkup(rows), parse_mode=ParseMode.HTML
        )
        if m2: game.voting_message_ids.append(m2.message_id)
    except BotKickedError:
        delete_game(chat_id)
        return
    
    await save_games_state()
    game.timer_job = context.job_queue.run_once(timer_vote_end, game.settings['vote_time'], chat_id=chat_id)

async def cb_vote(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    try: target_id = int(query.data.split("_")[1])
    except: return

    game = get_game(query.message.chat_id)
    
    if not game or game.state != "VOTING":
        await query.answer("Голосування закрите.", show_alert=True)
        return
        
    voter = game.players.get(query.from_user.id)
    # Дозволяємо голосувати всім, не тільки гравцям
    if query.from_user.id in game.voters:
        await query.answer("Ти вже віддав голос.", show_alert=True)
        return
    if query.from_user.id == target_id:
        await query.answer("За себе не можна! Нарцисизм - це гріх.", show_alert=True)
        return
        
    # Позначаємо що користувач проголосував
    game.voters.add(query.from_user.id)
    
    # Якщо це гравець, позначаємо що він проголосував
    if voter:
        voter.has_voted = True
        
    if target_id in game.players:
        game.players[target_id].round_votes += 1
        
    await query.answer("Голос враховано.")
    await save_games_state()
    
    await check_phase_completion(context, game)

async def timer_vote_end(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.chat_id
    await show_results(context, chat_id)

async def show_results(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    game = get_game(chat_id)
    if not game: return
    
    for msg_id in game.voting_message_ids:
        try: await context.bot.delete_message(chat_id, msg_id)
        except: pass
    game.voting_message_ids = []

    candidates = [p for p in game.players.values() if p.chosen_card]
    candidates.sort(key=lambda x: x.round_votes, reverse=True)
    
    text = "🏁 <b>РЕЗУЛЬТАТИ РАУНДУ</b>\n\n"
    safe_situation = html.escape(game.current_situation)

    if candidates and candidates[0].round_votes > 0:
        winner = candidates[0]
        max_votes = winner.round_votes
        
        winners = [p for p in candidates if p.round_votes == max_votes]
        for w in winners:
            w.score += 1
            await update_global_stats(w.id, chat_id, w.first_name, is_win=False, score_add=1)

        win_names = ", ".join([html.escape(w.first_name) for w in winners])
        caption = (
            f"Тема: <b>{safe_situation}</b>\n\n"
            f"👑 <b>Переміг: {win_names}</b> (+1)\n"
            f"❤️ Голосів: {max_votes}"
        )
        
        try:
            if len(winners) == 1:
                # Один переможець - показуємо його мем
                try:
                    await safe_send(
                        context.bot, chat_id, 
                        text=caption, 
                        photo=winners[0].chosen_card, 
                        parse_mode=ParseMode.HTML
                    )
                except Exception as e:
                    logger.error(f"Failed to send winner meme: {e}")
            else:
                # Кілька переможців - відправляємо всі меми одним повідомленням
                media_group = []
                for i, winner in enumerate(winners):
                    if i == len(winners) - 1:
                        # Останній мем має повну інформацію
                        media_group.append(InputMediaPhoto(
                            media=winner.chosen_card, 
                            caption=caption, 
                            parse_mode=ParseMode.HTML
                        ))
                    else:
                        # Попередні меми без підписів
                        media_group.append(InputMediaPhoto(
                            media=winner.chosen_card
                        ))
                
                try:
                    await context.bot.send_media_group(chat_id, media_group)
                except Exception as e:
                    logger.error(f"Failed to send winners media group: {e}")
                    # Fallback: відправляємо окремо
                    try:
                        # Відправляємо всі фото без підписів
                        for winner in winners[:-1]:
                            await safe_send(
                                context.bot, chat_id, 
                                photo=winner.chosen_card
                            )
                        # Останній з caption
                        await safe_send(
                            context.bot, chat_id, 
                            text=caption, 
                            photo=winners[-1].chosen_card, 
                            parse_mode=ParseMode.HTML
                        )
                    except Exception as e2:
                        logger.error(f"Failed to send winners fallback: {e2}")
        except BotKickedError:
            delete_game(chat_id)
            return
        except:
            text += f"👑 <b>{win_names}</b> виграє цей раунд!\n"
    else:
        text += "🗿 <b>Гробова тиша.</b> Нікому не сподобався жоден варіант.\n"

    win_score = game.settings.get('win_score', 0)
    sorted_players = sorted(game.players.values(), key=lambda x: x.score, reverse=True)
    
    text += "\n📊 <b>Рахунок:</b>\n"
    for p in sorted_players:
        safe_name = html.escape(p.first_name)
        mark = "✨"
        text += f"{mark} {safe_name}: <b>{p.score}</b>\n"

    # Перевірка ліміту раундів
    max_rounds = game.settings.get('max_rounds', 0)
    if max_rounds > 0 and game.round_number >= max_rounds:
        # Гра закінчується через ліміт раундів
        if sorted_players:
            max_score = sorted_players[0].score
            game_winners = [p for p in sorted_players if p.score == max_score]
        else:
            game_winners = []
        
        if game_winners:
            if len(game_winners) == 1:
                grand_winner = game_winners[0]
                final_text = (
                    f"⏰ <b>ГРА ЗАКІНЧЕНА!</b> ⏰\n\n"
                    f"Досягнуто ліміт раундів ({max_rounds}).\n\n"
                    f"🐈 <b>{grand_winner.get_link()}</b> лідирує!\n"
                    f"Фінальний рахунок: {grand_winner.score}\n"
                    f"✨ Нараховано <b>{MEMS_WIN_REWARD} м'яток</b> 🌿"
                )
                await update_global_stats(grand_winner.id, chat_id, grand_winner.first_name, is_win=True)
                await update_user_balance(grand_winner.id, MEMS_WIN_REWARD)
            else:
                final_text = "⏰ <b>ГРА ЗАКІНЧЕНА!</b> ⏰\n\nДосягнуто ліміт раундів. Нічія!\n\n"
                for w in game_winners:
                    final_text += f"🐈 {w.get_link()} ({w.score})\n"
                    await update_global_stats(w.id, chat_id, w.first_name, is_win=True)
                    await update_user_balance(w.id, MEMS_WIN_REWARD)
                final_text += f"\n✨ Кожен отримує <b>{MEMS_WIN_REWARD} м'яток</b> 🌿"
        else:
            final_text = "⏰ <b>ГРА ЗАКІНЧЕНА!</b> ⏰\n\nДосягнуто ліміт раундів. Немає переможця."
        
        # Оновлюємо статистику для всіх гравців
        for p in sorted_players:
            await update_global_stats(p.id, chat_id, p.first_name, is_win=False, score_add=0, games_played_add=1)
        
        # Додаємо список всіх учасників з балами
        final_text += "\n\n📊 <b>Результати гри:</b>\n"
        for p in sorted_players:
            safe_name = html.escape(p.first_name)
            final_text += f"• {safe_name}: <b>{p.score}</b>\n"
            
        final_text += "\n/newgame - Зіграти ще раз!"
        delete_game(chat_id)
        try: await safe_send(context.bot, chat_id, final_text, parse_mode=ParseMode.HTML)
        except BotKickedError: pass
        return

    game_winners = [p for p in sorted_players if win_score > 0 and p.score >= win_score]
    
    if game_winners:
        top_score = game_winners[0].score
        actual_winners = [p for p in game_winners if p.score == top_score]
        
        if len(actual_winners) > 1:
            final_text = "🎆 <b>ФІНАЛ! НІЧИЯ!</b> 🎆\n\n"
            for w in actual_winners:
                final_text += f"🐈 {w.get_link()} ({w.score})\n"
                await update_global_stats(w.id, chat_id, w.first_name, is_win=True)
                await update_user_balance(w.id, MEMS_WIN_REWARD)
            final_text += f"\n✨ Кожен отримує <b>{MEMS_WIN_REWARD} м'яток</b> 🌿"
        else:
            grand_winner = actual_winners[0]
            final_text = (
                f"🎉 <b>АБСОЛЮТНА ПЕРЕМОГА!</b> 🎉\n\n"
                f"🐈 <b>{grand_winner.get_link()}</b> знищив суперників!\n"
                f"Фінальний рахунок: {grand_winner.score}\n"
                f"✨ Нараховано <b>{MEMS_WIN_REWARD} м'яток</b> 🌿"
            )
            await update_global_stats(grand_winner.id, chat_id, grand_winner.first_name, is_win=True)
            await update_user_balance(grand_winner.id, MEMS_WIN_REWARD)
        
        # Оновлюємо статистику для всіх гравців
        for p in sorted_players:
            await update_global_stats(p.id, chat_id, p.first_name, is_win=False, score_add=0, games_played_add=1)
        
        # Додаємо список всіх учасників з балами
        final_text += "\n\n📊 <b>Результати гри:</b>\n"
        for p in sorted_players:
            safe_name = html.escape(p.first_name)
            final_text += f"• {safe_name}: <b>{p.score}</b>\n"
            
        final_text += "\n/newgame - Зіграти ще раз!"
        delete_game(chat_id)
        try: await safe_send(context.bot, chat_id, final_text, parse_mode=ParseMode.HTML)
        except BotKickedError: pass
        return

    card_db = list(CACHED_CARDS.values())
    game.deal_cards(card_db)
    await save_games_state()
    
    try: await safe_send(context.bot, chat_id, text + "\n<i>Наступний раунд через 8 сек...</i>", parse_mode=ParseMode.HTML)
    except BotKickedError: 
        delete_game(chat_id)
        return
    
    context.job_queue.run_once(lambda ctx: start_round_logic(ctx.bot, chat_id, ctx.job_queue), 8, chat_id=chat_id)


if __name__ == "__main__":
    if TOKEN == "YOUR_NEW_TOKEN_HERE":
        print("Помилка: Вкажіть токен бота в коді!")
        exit()
        
    app = ApplicationBuilder().token(TOKEN).post_init(load_games_on_startup).build()
    
    app.add_error_handler(error_handler)

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("top", cmd_top)) 
    app.add_handler(CommandHandler(["init_cards"], cmd_init_cards))
    app.add_handler(CommandHandler("pick", cmd_pick_card))
    app.add_handler(CommandHandler(["stop", "stopgame"], cmd_stop_game))
    app.add_handler(CommandHandler(["newgame"], cmd_newgame))
    app.add_handler(CommandHandler("leave", cmd_leave_game)) 
    app.add_handler(CommandHandler("kick", cmd_kick))
    app.add_handler(CommandHandler(["add_sit", "add_situation"], cmd_add_situation))
    app.add_handler(CommandHandler(["settings"], cmd_settings_request))

    # Текстові аліаси
    app.add_handler(MessageHandler(filters.Regex(r'(?i)^новагра\b'), cmd_newgame))
    app.add_handler(MessageHandler(filters.Regex(r'(?i)^нова гра\b'), cmd_newgame))
    app.add_handler(MessageHandler(filters.Regex(r'(?i)^стоп\b'), cmd_stop_game))
    app.add_handler(MessageHandler(filters.Regex(r'(?i)^вийти\b'), cmd_leave_game)) 
    app.add_handler(MessageHandler(filters.Regex(r'(?i)^налаштування\b'), cmd_settings_request))

    app.add_handler(CallbackQueryHandler(cb_join, pattern="^join_leave$"))
    app.add_handler(CallbackQueryHandler(cb_start_game, pattern="^start_game_force$"))
    app.add_handler(CallbackQueryHandler(cb_settings_pm_action, pattern="^set_")) 
    app.add_handler(CallbackQueryHandler(cb_settings_pm_action, pattern="^close_settings_pm$"))
    app.add_handler(CallbackQueryHandler(cb_vote, pattern="^vote_"))
    
    app.add_handler(InlineQueryHandler(inline_query_handler))

    print("Бот Келії запущений і готовий до гріхів...")
    app.run_polling()