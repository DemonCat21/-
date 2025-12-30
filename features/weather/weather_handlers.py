# weather_handlers.py
# -*- coding: utf-8 -*-
"""
Асинхронний модуль погоди для бота "Котик".
- Підтримка OpenWeatherMap (OneCall + геокодинг)
- Пріоритет над AI, але нижчий за нагадування (реєструвати у group=1)
- Повністю async, з кешем та автозакриттям меню
- UX: автоматичне місто з профілю, ввічливі уточнення, компактні відповіді
"""
import asyncio
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional, Tuple, List

import httpx
import pytz
import dateparser
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

from bot.core.database import get_user_profile
from bot.utils.utils import (
    AddressingContext,
    cancel_auto_close,
    get_user_addressing,
    set_auto_close_payload,
    start_auto_close,
)

logger = logging.getLogger(__name__)

# Використовуємо лише env; дефолтних ключів немає, щоб уникнути 401 і витоку ключа.
OWM_API_KEY = (os.getenv("OWM_API_KEY") or "").strip()
OWM_GEOCODE_URL = "https://api.openweathermap.org/geo/1.0/direct"
OWM_ONECALL_URL = "https://api.openweathermap.org/data/2.5/onecall"
OWM_FORECAST_URL = "https://api.openweathermap.org/data/2.5/forecast"  # 5-day / 3-hour fallback

KYIV_TZ = pytz.timezone("Europe/Kyiv")
MAX_FORECAST_DAYS = 20
CACHE_TTL_SECONDS = 600  # 10 хвилин

# Простий in-memory кеш: {(key): (expires_at, data)}
_weather_cache: Dict[str, Tuple[datetime, Any]] = {}

# Автозакриття
WEATHER_AUTO_CLOSE_KEY = "weather_screen"
CB_WEATHER_CLOSE = "weather:close"
CB_WEATHER_NOW_PREFIX = "weather:now:"
CB_WEATHER_TODAY_PREFIX = "weather:today:"

# Зберігаємо контекст погоди для кнопок (по message_id)
WEATHER_STATE_KEY = "weather_state"


async def _arm_weather_auto_close(context: ContextTypes.DEFAULT_TYPE, message) -> None:
    if not message:
        return
    cancel_auto_close(context, WEATHER_AUTO_CLOSE_KEY)
    set_auto_close_payload(
        context,
        WEATHER_AUTO_CLOSE_KEY,
        chat_id=message.chat_id,
        message_id=message.message_id,
        fallback_text="Екран погоди закрито через бездіяльність.",
    )
    # Check if auto_delete_actions is enabled
    from bot.core.database import get_chat_settings
    settings = await get_chat_settings(message.chat_id)
    if settings.get("auto_delete_actions", 0) == 1:
        start_auto_close(context, WEATHER_AUTO_CLOSE_KEY, timeout=420)  # 7 minutes


async def delete_message_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    job_data = getattr(context.job, "data", {}) if context.job else {}
    chat_id = job_data.get("chat_id")
    message_id = job_data.get("message_id")
    if not chat_id or not message_id:
        return
    try:
        bot = getattr(context, "bot", None) or context.application.bot
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception:
        pass


async def _schedule_weather_auto_delete(
    context: ContextTypes.DEFAULT_TYPE,
    *,
    chat_id: int,
    message_id: int,
    timeout: int = 420,
) -> None:
    if not context or not context.application:
        return
    from bot.core.database import get_chat_settings
    settings = await get_chat_settings(chat_id)
    if settings.get("auto_delete_actions", 0) != 1:
        return
    context.job_queue.run_once(
        delete_message_job,
        timeout,
        data={"chat_id": chat_id, "message_id": message_id},
        name=f"delete_weather_{chat_id}_{message_id}",
    )


# ==== Допоміжні функції кешу ====

def _cache_get(key: str) -> Optional[Any]:
    item = _weather_cache.get(key)
    if not item:
        return None
    expires_at, data = item
    if datetime.now(timezone.utc) > expires_at:
        _weather_cache.pop(key, None)
        return None
    return data


def _cache_set(key: str, data: Any, ttl: int = CACHE_TTL_SECONDS) -> None:
    _weather_cache[key] = (datetime.now(timezone.utc) + timedelta(seconds=ttl), data)


# ==== Геокодинг та дані погоди ====

async def _geocode_city(city: str) -> Optional[Tuple[float, float, str]]:
    """Повертає (lat, lon, normalized_city) або None."""
    if not city:
        return None
    key = f"geo:{city.lower()}"
    cached = _cache_get(key)
    if cached:
        return cached
    if not OWM_API_KEY:
        logger.warning("OWM_API_KEY відсутній")
        return None
    params = {
        "q": city,
        "limit": 1,
        "appid": OWM_API_KEY,
        "lang": "uk",
    }
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.get(OWM_GEOCODE_URL, params=params)
            resp.raise_for_status()
            data = resp.json()
            if not data:
                return None
            item = data[0]
            lat = float(item.get("lat"))
            lon = float(item.get("lon"))
            name = item.get("local_names", {}).get("uk") or item.get("name") or city
            result = (lat, lon, name)
            _cache_set(key, result, ttl=3600)
            return result
    except Exception:
        logger.exception("Помилка геокодингу")
        return None


async def _fetch_onecall(lat: float, lon: float) -> Optional[Dict[str, Any]]:
    """Отримує daily/ hourly з OneCall, кешує."""
    key = f"onecall:{lat:.4f}:{lon:.4f}"
    cached = _cache_get(key)
    if cached:
        return cached
    if not OWM_API_KEY:
        return None
    params = {
        "lat": lat,
        "lon": lon,
        "appid": OWM_API_KEY,
        "units": "metric",
        "exclude": "minutely",
        "lang": "uk",
    }
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.get(OWM_ONECALL_URL, params=params)
            resp.raise_for_status()
            data = resp.json()
            _cache_set(key, data)
            return data
    except httpx.HTTPStatusError as e:
        if e.response is not None and e.response.status_code == 401:
            logger.error("OWM OneCall 401: ключ недійсний або відсутній")
            return {"_error": "auth"}
        logger.exception("Помилка запиту OneCall")
        return None
    except Exception:
        logger.exception("Помилка запиту OneCall")
        return None


async def _fetch_forecast_fallback(lat: float, lon: float) -> Optional[Dict[str, Any]]:
    """Fallback: 5-day/3-hour forecast, агрегуємо до daily-подібного формату.

    Повертає {"daily": [...]} або None.
    """
    key = f"forecast:{lat:.4f}:{lon:.4f}"
    cached = _cache_get(key)
    if cached:
        return cached
    if not OWM_API_KEY:
        return None
    params = {
        "lat": lat,
        "lon": lon,
        "appid": OWM_API_KEY,
        "units": "metric",
        "lang": "uk",
    }
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.get(OWM_FORECAST_URL, params=params)
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPStatusError as e:
        if e.response is not None and e.response.status_code == 401:
            logger.error("OWM Forecast 401: ключ недійсний або відсутній")
            return {"_error": "auth"}
        logger.exception("Помилка forecast 5-day")
        return None
    except Exception:
        logger.exception("Помилка forecast 5-day")
        return None

    # Агрегуємо по датах
    by_date: Dict[datetime.date, list] = {}
    for item in data.get("list", []):
        ts = item.get("dt")
        if not ts:
            continue
        d = datetime.fromtimestamp(ts, tz=KYIV_TZ).date()
        by_date.setdefault(d, []).append(item)

    daily_list: List[Dict[str, Any]] = []
    for d, bucket in sorted(by_date.items())[:5]:
        temps = [b.get("main", {}).get("temp") for b in bucket if b.get("main")]
        temp_mins = [b.get("main", {}).get("temp_min") for b in bucket if b.get("main")]
        temp_maxs = [b.get("main", {}).get("temp_max") for b in bucket if b.get("main")]
        feels = [b.get("main", {}).get("feels_like") for b in bucket if b.get("main")]
        winds = [b.get("wind", {}).get("speed") for b in bucket if b.get("wind")]
        hums = [b.get("main", {}).get("humidity") for b in bucket if b.get("main")]
        pops = [b.get("pop") for b in bucket if b.get("pop") is not None]
        weathers = [b.get("weather", [{}])[0] for b in bucket if b.get("weather")]

        avg = lambda arr: sum(arr) / len(arr) if arr else 0
        choose = weathers[0] if weathers else {"main": "", "description": ""}

        daily_list.append({
            "dt": int(datetime.combine(d, datetime.min.time()).replace(tzinfo=KYIV_TZ).timestamp()),
            "temp": {
                "min": min(temp_mins) if temp_mins else avg(temps),
                "max": max(temp_maxs) if temp_maxs else avg(temps),
                "day": avg(temps),
            },
            "feels_like": {"day": avg(feels)},
            "wind_speed": avg(winds),
            "humidity": int(avg(hums)),
            "pop": avg(pops),
            "weather": [choose],
        })

    result = {"daily": daily_list}
    _cache_set(key, result)
    return result


# ==== Парсинг запитів ====

_DOW_MAP = {
    "понеділок": 0,
    "вівторок": 1,
    "середу": 2,
    "середа": 2,
    "четвер": 3,
    "п'ятницю": 4,
    "пʼятницю": 4,
    "пятницю": 4,
    "суботу": 5,
    "субота": 5,
    "неділю": 6,
    "неділя": 6,
}

_PERIOD_TOKENS = {
    "сьогодні",
    "завтра",
    "тиждень",
    "тижня",
    "тижні",
    "місяць",
    "місяця",
    "місяці",
    "на",
    "в",
    "у",
    "це",
    "цей",
    "ця",
}
_PERIOD_TOKENS.update(_DOW_MAP.keys())

_MONTH_TOKENS = {
    "січня", "лютого", "березня", "квітня", "травня", "червня",
    "липня", "серпня", "вересня", "жовтня", "листопада", "грудня",
    "січень", "лютий", "березень", "квітень", "травень", "червень",
    "липень", "серпень", "вересень", "жовтень", "листопад", "грудень",
}

# Мапінг місяців для явних дат («26 грудня», «26 груд»)
_MONTH_VARIANTS = {
    1: ["січ", "січень", "січня"],
    2: ["лют", "лютий", "лютого"],
    3: ["бер", "березень", "березня"],
    4: ["квіт", "квітень", "квітня"],
    5: ["трав", "травень", "травня"],
    6: ["чер", "червень", "червня"],
    7: ["лип", "липень", "липня"],
    8: ["серп", "серпень", "серпня"],
    9: ["вер", "вересень", "вересня"],
    10: ["жов", "жовтень", "жовтня"],
    11: ["лист", "листопад", "листопада"],
    12: ["груд", "грудень", "грудня"],
}


def _parse_period(text: str) -> Tuple[str, Optional[datetime]]:
    """Повертає (mode, target_date|None).
    mode: today, tomorrow, week, month, date
    date обмежена 20 днями від сьогодні, не в минуле.
    """
    t = (text or "").lower().strip()
    now = datetime.now(KYIV_TZ).date()

    def _explicit_day_month(raw: str) -> Optional[datetime]:
        s = raw
        for month_num, variants in _MONTH_VARIANTS.items():
            for v in variants:
                m = re.search(rf"\b(\d{{1,2}})\s+{re.escape(v)}\w*\b", s)
                if not m:
                    continue
                day = int(m.group(1))
                year = now.year
                try:
                    candidate = datetime(year, month_num, day, tzinfo=KYIV_TZ).date()
                except ValueError:
                    return None
                if candidate < now:
                    try:
                        candidate = datetime(year + 1, month_num, day, tzinfo=KYIV_TZ).date()
                    except ValueError:
                        return None
                return candidate
        return None

    # Спец кейси
    if "зараз" in t or "поточн" in t:
        return "now", now
    if "післязавтра" in t or "після завтра" in t:
        return "date", now + timedelta(days=2)
    m = re.search(r"через\s+(\d+)\s*д", t)
    if m:
        n = int(m.group(1))
        if n < 0:
            return "past", now
        if n > MAX_FORECAST_DAYS:
            return "too_far", now + timedelta(days=n)
        return "date", now + timedelta(days=n)
    if "вихідн" in t:
        return "weekend", None

    if "сьогодні" in t:
        return "today", now
    if "завтра" in t:
        return "tomorrow", now + timedelta(days=1)
    if "тижд" in t:
        return "week", None
    if "міся" in t:
        return "month", None

    # Явна дата «26 грудня»
    explicit = _explicit_day_month(t)
    if explicit:
        delta = (explicit - now).days
        if delta < 0:
            return "past", explicit
        if delta > MAX_FORECAST_DAYS:
            return "too_far", explicit
        return "date", explicit

    # День тижня
    for k, idx in _DOW_MAP.items():
        if k in t:
            # date of next occurrence including today+1
            delta = (idx - now.weekday()) % 7
            if delta == 0:
                delta = 7
            return "date", now + timedelta(days=delta)

    # Дата явна
    parsed = dateparser.parse(
        t,
        languages=["uk"],
        settings={
            "TIMEZONE": "Europe/Kyiv",
            "PREFER_DATES_FROM": "future",
            "RETURN_AS_TIMEZONE_AWARE": False,
        },
    )
    if parsed:
        date_val = parsed.date()
        if date_val < now:
            return "past", date_val
        delta = (date_val - now).days
        if delta > MAX_FORECAST_DAYS:
            return "too_far", date_val
        return "date", date_val

    # За замовчуванням — показуємо «зараз» (а «сьогодні» — кнопкою)
    return "now", now


def _extract_city_from_text(text: str) -> Optional[str]:
    """Витягує місто з рядка після слова "погода", прибираючи слова періоду.

    Приклад: "погода київ сьогодні" -> "київ".
    """
    if not text:
        return None
    parts = text.split(maxsplit=1)
    if not (parts and parts[0].lower().startswith("погода") and len(parts) > 1):
        return None
    tail = parts[1]
    tokens = [t.strip(",. ") for t in tail.split() if t.strip(",. ")]
    city_tokens: List[str] = []
    for tok in tokens:
        if tok.lower() in _PERIOD_TOKENS:
            continue
        if tok.lower() in _MONTH_TOKENS:
            continue
        if tok.isdigit():
            continue
        city_tokens.append(tok)
    if not city_tokens:
        return None
    # Якщо всі токени — цифри/місяці/періоди, не вважаємо це містом
    if all(t.isdigit() or t.lower() in _MONTH_TOKENS for t in tokens):
        return None
    return " ".join(city_tokens)


# ==== Форматування ====

_WEATHER_EMOJI = {
    "Clear": "☀️",
    "Clouds": "☁️",
    "Rain": "🌧️",
    "Drizzle": "🌦️",
    "Thunderstorm": "⛈️",
    "Snow": "❄️",
    "Mist": "🌫️",
    "Fog": "🌫️",
    "Smoke": "🌫️",
}


def _emoji_for(weather_main: str) -> str:
    return _WEATHER_EMOJI.get(weather_main, "🌡️")


def _wind_dir(deg: Optional[float]) -> str:
    if deg is None:
        return "—"
    dirs = ["пн", "пн-сх", "сх", "пд-сх", "пд", "пд-зх", "зх", "пн-зх"]
    ix = int((deg % 360) / 45 + 0.5) % 8
    return dirs[ix]


def _weekday_uk(date_val: datetime.date) -> str:
    days = [
        "Пн",
        "Вт",
        "Ср",
        "Чт",
        "Пт",
        "Сб",
        "Нд",
    ]
    return days[date_val.weekday()]


def _format_date_uk(date_val: datetime.date) -> str:
    months = [
        "",
        "січня",
        "лютого",
        "березня",
        "квітня",
        "травня",
        "червня",
        "липня",
        "серпня",
        "вересня",
        "жовтня",
        "листопада",
        "грудня",
    ]
    return f"{date_val.day} {months[date_val.month]}"


def _fmt_temp(v: float) -> str:
    v = round(v)
    sign = "" if v <= 0 else "+"
    return f"{sign}{v}°C"


def _wind_strength_label(wind_speed: Optional[float]) -> Optional[str]:
    if wind_speed is None:
        return None
    if wind_speed <= 3:
        return "слабкий"
    if wind_speed <= 8:
        return "помірний"
    return "сильний"


def _humidity_label(humidity: Optional[float]) -> Optional[str]:
    if humidity is None:
        return None
    return "висока" if humidity >= 80 else "нормальна"


def _make_city_id(lat: float, lon: float) -> str:
    return f"{lat:.4f},{lon:.4f}"


def _snow_or_rain_desc(desc: str) -> bool:
    return any(token in desc for token in ("сніг", "дощ", "мряка", "дощик", "злива"))


def _collect_precip_amount(item: Dict[str, Any]) -> float:
    rain = item.get("rain") or {}
    snow = item.get("snow") or {}
    total = 0.0
    if isinstance(rain, dict):
        total += float(rain.get("1h") or 0) + float(rain.get("3h") or 0)
    elif isinstance(rain, (int, float)):
        total += float(rain)
    if isinstance(snow, dict):
        total += float(snow.get("1h") or 0) + float(snow.get("3h") or 0)
    elif isinstance(snow, (int, float)):
        total += float(snow)
    return total


def _build_alert_summary(alerts: List[Dict[str, Any]]) -> Optional[str]:
    if not alerts:
        return None
    first = alerts[0]
    event = (first.get("event") or "Попередження").strip()
    return f"Є офіційне попередження: {event.lower()}. Будьте уважні."


def _build_warnings(
    *,
    temp: Optional[float],
    humidity: Optional[float],
    wind_speed: Optional[float],
    visibility: Optional[float],
    desc: str,
    precip_amount: float,
    alerts: List[Dict[str, Any]],
) -> List[str]:
    if alerts:
        alert_text = _build_alert_summary(alerts)
        return [alert_text] if alert_text else []

    warnings: List[str] = []
    desc_lower = desc.lower() if desc else ""
    if temp is not None:
        if -3 <= temp <= 1 and (precip_amount > 0 or _snow_or_rain_desc(desc_lower)):
            warnings.append("Можлива ожеледиця на дорогах.")
        elif -3 <= temp <= 1 and (humidity or 0) >= 85:
            warnings.append("Холод і висока вологість — слизько під ногами.")
    if visibility is not None and visibility <= 1000:
        warnings.append("Низька видимість через туман.")
    elif (humidity or 0) >= 90 and (wind_speed or 0) <= 2:
        warnings.append("Ймовірний туман через високу вологість і слабкий вітер.")
    if wind_speed is not None and wind_speed >= 10:
        warnings.append("Сильний вітер — обережно на вулиці.")
    if precip_amount >= 6:
        warnings.append("Інтенсивні опади можуть ускладнити видимість.")
    return warnings


def _build_advice(
    *,
    temp: Optional[float],
    wind_speed: Optional[float],
    precip_amount: float,
    desc: str,
) -> str:
    desc_lower = desc.lower() if desc else ""
    if precip_amount >= 2 or _snow_or_rain_desc(desc_lower):
        return "🧥 Порада: парасоля або капюшон стануть у пригоді."
    if temp is not None and temp <= -3:
        return "🧥 Порада: тепла куртка не завадить."
    if wind_speed is not None and wind_speed >= 10:
        return "🧥 Порада: уважніше з поривами вітру."
    return "🧥 Порада: одягайся по погоді."


def _build_overview_sentence(
    *,
    feels_like: Optional[float],
    desc: str,
    wind_speed: Optional[float],
    humidity: Optional[float],
) -> str:
    parts: List[str] = []
    if feels_like is not None:
        parts.append(f"Відчувається як {_fmt_temp(feels_like)}.")
    if desc:
        parts.append(f"{desc.capitalize()}.")
    wind_label = _wind_strength_label(wind_speed)
    if wind_label:
        parts.append(f"Вітер {wind_label}.")
    humidity_label = _humidity_label(humidity)
    if humidity_label == "висока":
        parts.append("Вологість висока.")
    return " ".join(parts).strip()


def _format_day(date_val: datetime.date, daily: Dict[str, Any], detailed: bool) -> str:
    weather = (daily.get("weather") or [{}])[0]
    main = weather.get("main", "")
    desc_raw = weather.get("description") or ""
    desc = desc_raw.capitalize() if desc_raw else "(опис недоступний)"
    temps = daily.get("temp", {})
    t_min = round(temps.get("min", temps.get("day", 0)))
    t_max = round(temps.get("max", temps.get("day", 0)))
    feels = round((daily.get("feels_like") or {}).get("day", temps.get("day", 0)))
    wind = daily.get("wind_speed")
    wind_txt = f"~{round(wind)} м/с" if wind is not None else "н/д"
    wind_dir = _wind_dir(daily.get("wind_deg"))
    humidity_val = daily.get("humidity")
    humidity = f"{int(humidity_val)}%" if humidity_val is not None else "н/д"
    pop_raw = daily.get("pop")
    pop = f"{int(round(pop_raw * 100))}%" if pop_raw is not None else "н/д"
    emoji = _emoji_for(main)

    if not detailed:
        return f"{_weekday_uk(date_val)} {date_val.strftime('%d.%m')} · {emoji} {_fmt_temp(t_min)}…{_fmt_temp(t_max)}"

    feel_delta = feels - ((t_min + t_max) / 2)
    feel_hint = "" if abs(feel_delta) < 2 else f", відчувається {_fmt_temp(feels)}"
    pop_hint = "Без опадів" if pop_raw is not None and pop_raw < 0.2 else None
    pop_hint = pop_hint or (f"Може покрапати ({pop})" if pop_raw is not None else None)

    main_line = f"{emoji} {desc}. {_fmt_temp(t_max)} вдень, {_fmt_temp(t_min)} вночі{feel_hint}."
    detail_lines = []
    if pop_hint:
        detail_lines.append(f"☔ {pop_hint}")
    if wind is not None:
        detail_lines.append(f"🌬️ Вітер {wind_dir}, {wind_txt}")
    if humidity_val is not None:
        detail_lines.append(f"💧 Вологість {humidity}")
    return "\n".join([main_line] + detail_lines)


def _tips(temp_min: float, temp_max: float, pop: Optional[float], wind: Optional[float]) -> List[str]:
    tips: List[str] = []
    t_mid = (temp_min + temp_max) / 2 if (temp_min is not None and temp_max is not None) else None
    if t_mid is not None:
        if t_mid <= -10:
            tips.append("🧥 Тепла куртка, шапка і рукавички")
        elif t_mid <= -3:
            tips.append("🧥 Тепла куртка не завадить")
        elif t_mid >= 28:
            tips.append("💧 Більше води та тінь")
        elif t_mid >= 22:
            tips.append("👕 Легкий одяг підійде")
    if pop is not None:
        if pop >= 0.6:
            tips.append("☂️ Парасоля точно знадобиться")
        elif pop >= 0.3:
            tips.append("🌦 Може покрапати — парасоля про всяк випадок")
    if wind is not None and wind >= 10:
        tips.append("💨 Сильний вітер, плануйте одяг зі стоячим коміром")
    if not tips:
        tips.append("🌿 Гарного дня!")
    return tips


def _heading(city: str, label: str, emoji: str) -> str:
    """Котячий заголовок: місто + період."""
    # маленький "вайб" без зайвого — працює в HTML-режимі reply_html
    return f"<b>{emoji} {city}</b> <i>· {label}</i> 🐾"


def _build_current_section(
    city: str,
    current: Dict[str, Any],
    daily: List[Dict[str, Any]],
    alerts: List[Dict[str, Any]],
    today_date: datetime.date,
    source_hint: str,
) -> List[str]:
    """Формує блок "погода зараз" у форматі сторінки."""
    header = f"😺 Зараз · {city}"

    use_current = current and current.get("temp") is not None
    data_item = current if use_current else None
    if not use_current:
        for item in daily or []:
            ts = item.get("dt")
            if not ts:
                continue
            d = datetime.fromtimestamp(ts, tz=KYIV_TZ).date()
            if d == today_date:
                data_item = item
                break

    if not data_item:
        out = [header, "Немає даних для поточної погоди."]
        if source_hint:
            out.append(source_hint)
        return out

    weather = (data_item.get("weather") or [{}])[0]
    desc = (weather.get("description") or weather.get("main") or "погода").strip()
    temp_val = data_item.get("temp") if use_current else (data_item.get("temp", {}) or {}).get("day")
    feels_val = data_item.get("feels_like") if use_current else (data_item.get("feels_like", {}) or {}).get("day")
    wind_speed = data_item.get("wind_speed") if use_current else data_item.get("wind_speed")
    humidity_val = data_item.get("humidity")
    visibility = data_item.get("visibility") if use_current else None
    precip_amount = _collect_precip_amount(data_item)

    base_lines = [header]
    if temp_val is not None and feels_val is not None:
        base_lines.append(f"🌡 {_fmt_temp(temp_val)} (відчувається як {_fmt_temp(feels_val)})")
    elif temp_val is not None:
        base_lines.append(f"🌡 {_fmt_temp(temp_val)}")
    else:
        base_lines.append("🌡 Дані про температуру недоступні")
    if desc:
        base_lines.append(f"☁️ {desc.lower()}")
    if wind_speed is not None:
        base_lines.append(f"🌬 {round(wind_speed)} м/с")

    lines = [*base_lines]

    warnings = _build_warnings(
        temp=temp_val,
        humidity=humidity_val,
        wind_speed=wind_speed,
        visibility=visibility,
        desc=desc,
        precip_amount=precip_amount,
        alerts=alerts,
    )
    if warnings:
        lines.extend([f"⚠️ {warnings[0]}"])

    if source_hint:
        lines.append(source_hint)
    return [line for line in lines if line]


def _min_feels_like(feels_like: Dict[str, Any]) -> Optional[float]:
    if not feels_like:
        return None
    values = [v for v in feels_like.values() if isinstance(v, (int, float))]
    return min(values) if values else None


def _build_today_section(
    city: str,
    daily_item: Dict[str, Any],
    alerts: List[Dict[str, Any]],
    label: str,
) -> List[str]:
    weather = (daily_item.get("weather") or [{}])[0]
    desc = (weather.get("description") or weather.get("main") or "погода").strip()
    emoji = _emoji_for(weather.get("main", ""))
    temps = daily_item.get("temp", {}) or {}
    day_temp = temps.get("max") if temps.get("max") is not None else temps.get("day")
    night_temp = temps.get("min") if temps.get("min") is not None else temps.get("night")
    feels_min = _min_feels_like(daily_item.get("feels_like") or {})

    main_line_parts = [f"{emoji} {desc.capitalize()}."]
    if day_temp is not None and night_temp is not None:
        main_line_parts.append(f"{_fmt_temp(day_temp)} вдень, {_fmt_temp(night_temp)} вночі")
    if feels_min is not None:
        main_line_parts.append(f"відчувається {_fmt_temp(feels_min)}.")
    elif main_line_parts and not main_line_parts[-1].endswith("."):
        main_line_parts[-1] = f"{main_line_parts[-1]}."
    main_line = " ".join(main_line_parts)

    lines: List[str] = [f"{emoji} {city} · {label} 🐾", main_line]

    pop = daily_item.get("pop")
    rain_amount = daily_item.get("rain") or 0
    snow_amount = daily_item.get("snow") or 0
    if pop is not None or rain_amount or snow_amount:
        if rain_amount or snow_amount:
            if snow_amount and snow_amount >= rain_amount:
                lines.append(f"☔️ Сніг до {round(snow_amount)} мм")
            else:
                lines.append(f"☔️ Дощ до {round(rain_amount)} мм")
        elif pop is not None and pop < 0.2:
            lines.append("☔️ Без опадів")
        elif pop is not None:
            lines.append("☔️ Опади ймовірні")

    wind_speed = daily_item.get("wind_speed")
    wind_gust = daily_item.get("wind_gust")
    if wind_speed is not None:
        gust_text = ""
        if wind_gust:
            gust_text = f" (пориви до {round(wind_gust)} м/с)"
        lines.append(f"🌬️ ~{round(wind_speed)} м/с{gust_text}")

    humidity_val = daily_item.get("humidity")
    if humidity_val is not None:
        lines.append(f"💧 Вологість {int(humidity_val)}%")

    precip_amount = _collect_precip_amount(daily_item)
    overview_parts: List[str] = []
    if desc:
        overview_parts.append(f"Протягом дня {desc.lower()}.")
    if feels_min is not None and night_temp is not None and feels_min < night_temp:
        overview_parts.append("Надвечір відчутно прохолодніше.")

    warning_hint = _build_warnings(
        temp=day_temp,
        humidity=humidity_val,
        wind_speed=wind_speed,
        visibility=None,
        desc=desc,
        precip_amount=precip_amount,
        alerts=alerts,
    )
    if warning_hint:
        overview_parts.append(warning_hint[0])

    if overview_parts:
        lines.append(f"🧾 {' '.join(overview_parts[:2])}")
    return [line for line in lines if line]


# ==== Основна логіка ====

async def _resolve_city(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> Tuple[Optional[str], Optional[Tuple[float, float, str]], bool]:
    """Повертає (city_name, geo_tuple, from_profile).
    Місто з профілю має пріоритет; якщо в тексті явно вказане — одноразово.
    """
    user_id = update.effective_user.id if update.effective_user else None
    explicit_city = _extract_city_from_text(text)
    city_to_use = None
    from_profile = False
    if not explicit_city and user_id:
        try:
            prof = await get_user_profile(user_id)
            prof_city = (prof.get("city") or "").strip()
            if prof_city:
                city_to_use = prof_city
                from_profile = True
        except Exception:
            logger.debug("Не зміг отримати профіль для погоди", exc_info=True)
    if explicit_city:
        city_to_use = explicit_city
        from_profile = False
    if not city_to_use:
        return None, None, False
    geo = await _geocode_city(city_to_use)
    return city_to_use, geo, from_profile


async def _build_response(update: Update, context: ContextTypes.DEFAULT_TYPE, mode: str, target_date, city_name: str, geo: Tuple[float, float, str]) -> str:
    lat, lon, normalized_city = geo
    data = await _fetch_onecall(lat, lon)
    used_fallback = False
    auth_error = data and data.get("_error") == "auth"
    if auth_error or not data:
        fallback = await _fetch_forecast_fallback(lat, lon)
        if fallback and fallback.get("_error") == "auth":
            return "😿 Ключ погоди недійсний або не дає доступу до прогнозу. Перевір OWM_API_KEY."
        if fallback:
            data = fallback
            used_fallback = True
        else:
            if auth_error:
                return "😿 Ключ погоди недійсний або не налаштований. Адмін, перевір OWM_API_KEY."
            return "😿 Не можу отримати погоду зараз. Спробуйте трохи пізніше."

    daily: List[Dict[str, Any]] = data.get("daily") or []
    current: Dict[str, Any] = data.get("current") or {}
    alerts: List[Dict[str, Any]] = data.get("alerts") or []
    if not daily:
        return "😿 Погода недоступна."

    today_date = datetime.now(KYIV_TZ).date()
    source_hint = ""

    def pick_daily_for_date(d: datetime.date) -> Optional[Dict[str, Any]]:
        for item in daily:
            ts = item.get("dt")
            if not ts:
                continue
            item_date = datetime.fromtimestamp(ts, tz=KYIV_TZ).date()
            if item_date == d:
                return item
        return None

    def append_tips(lines: List[str], item: Dict[str, Any]) -> None:
        temps = item.get("temp", {})
        tips = _tips(temps.get("min"), temps.get("max"), item.get("pop"), item.get("wind_speed"))
        lines.extend(tips)
        if source_hint:
            lines.append(source_hint)

    # Обробка режимів
    if mode in {"today", "tomorrow", "date"}:
        target = target_date or today_date
        delta = (target - today_date).days
        if delta < 0:
            return "😿 Минулу дату показати не можу."
        if delta > MAX_FORECAST_DAYS:
            return "😿 Можу показати до 20 днів наперед."
        item = pick_daily_for_date(target)
        if not item:
            return "😿 Немає даних на цю дату."
        label = "сьогодні" if delta == 0 else "завтра" if delta == 1 else f"{_weekday_uk(target)}, {_format_date_uk(target)}"
        lines = _build_today_section(normalized_city, item, alerts, label)
        if source_hint:
            lines.append(source_hint)
        return "\n".join(l for l in lines if l)

    # Поточна погода — сторінка "Зараз"
    current_section = _build_current_section(normalized_city, current, daily, alerts, today_date, source_hint)

    if mode == "weekend":
        weekend_items: List[Tuple[datetime.date, Dict[str, Any]]] = []
        for item in daily:
            ts = item.get("dt")
            if not ts:
                continue
            d = datetime.fromtimestamp(ts, tz=KYIV_TZ).date()
            if d < today_date:
                continue
            if d.weekday() in {5, 6}:
                weekend_items.append((d, item))
        if not weekend_items:
            return "😿 Немає даних на вихідні."
        head_emoji = _emoji_for((weekend_items[0][1].get("weather") or [{}])[0].get("main", ""))
        lines: List[str] = [*current_section, "", _heading(normalized_city, "вихідні", head_emoji)]
        for d, item in weekend_items:
            lines.append(_format_day(d, item, detailed=False))
        if source_hint:
            lines.append(source_hint)
        return "\n".join(l for l in lines if l)

    if mode == "now":
        return "\n".join(l for l in current_section if l)

    if mode == "week" or mode == "month":
        max_days = min(len(daily), 7)
        if max_days == 0:
            return "😿 Немає даних для прогнозу."
        head_emoji = _emoji_for((daily[0].get("weather") or [{}])[0].get("main", ""))
        lines: List[str] = [*current_section, "", _heading(normalized_city, "наступні дні" if mode == "week" else "ближчі дні", head_emoji)]
        for idx in range(max_days):
            ts = daily[idx].get("dt")
            if not ts:
                continue
            d = datetime.fromtimestamp(ts, tz=KYIV_TZ).date()
            delta = (d - today_date).days
            if delta < 0 or delta > MAX_FORECAST_DAYS:
                continue
            lines.append(_format_day(d, daily[idx], detailed=False))
        if mode == "month" and len(daily) < 20:
            lines.append("⚠️ Повний місячний прогноз недоступний через обмеження API (є до 7 днів).")
        if source_hint:
            lines.append(source_hint)
        return "\n".join(l for l in lines if l)

    return "😿 Не розпізнав період. Спробуйте: сьогодні, завтра, тиждень, місяць або дату."  # fallback


def _weather_keyboard(*, city_id: str, show_nav: bool = True) -> InlineKeyboardMarkup:
    """Клавіатура екрана погоди.

    UX-вимога: кнопки «🐾 Зараз» і «🐾 Сьогодні».
    """
    rows: List[List[InlineKeyboardButton]] = []
    if show_nav:
        rows.append(
            [
                InlineKeyboardButton("🐾 Зараз", callback_data=f"{CB_WEATHER_NOW_PREFIX}{city_id}"),
                InlineKeyboardButton("🐾 Сьогодні", callback_data=f"{CB_WEATHER_TODAY_PREFIX}{city_id}"),
            ]
        )
    rows.append([InlineKeyboardButton("😽 Закрити", callback_data=CB_WEATHER_CLOSE)])
    return InlineKeyboardMarkup(rows)


def _close_keyboard() -> InlineKeyboardMarkup:
    """Сумісність зі старими викликами (тільки «Закрити»)."""
    return InlineKeyboardMarkup([[InlineKeyboardButton("😽 Закрити", callback_data=CB_WEATHER_CLOSE)]])


def _remember_weather_state(
    context: ContextTypes.DEFAULT_TYPE,
    city_id: str,
    city_name: str,
    geo: Tuple[float, float, str],
) -> None:
    """Зберігаємо контекст запиту погоди для callback-кнопок."""
    if not context or not getattr(context, "chat_data", None):
        return
    state = context.chat_data.setdefault(WEATHER_STATE_KEY, {})
    # тримаємо невеликий розмір: тільки останні ~50 записів
    if isinstance(state, dict) and len(state) > 50:
        for k in list(state.keys())[:10]:
            state.pop(k, None)
    state[str(city_id)] = {
        "city_name": city_name,
        "lat": float(geo[0]),
        "lon": float(geo[1]),
        "label": geo[2],
    }


def _get_weather_state(
    context: ContextTypes.DEFAULT_TYPE, city_id: str
) -> Optional[Tuple[str, Tuple[float, float, str]]]:
    if not context or not getattr(context, "chat_data", None):
        return None
    state = context.chat_data.get(WEATHER_STATE_KEY) or {}
    payload = state.get(str(city_id)) if isinstance(state, dict) else None
    if not isinstance(payload, dict):
        return None
    try:
        city_name = str(payload.get("city_name") or "")
        lat = float(payload.get("lat"))
        lon = float(payload.get("lon"))
        label = str(payload.get("label") or city_name or "локація")
    except Exception:
        return None
    return city_name, (lat, lon, label)


async def _ask_city(update: Update, ctx_user: AddressingContext) -> None:
    await update.effective_message.reply_html(
        f"{ctx_user.you.capitalize()}, напишіть місто або надішліть геолокацію, щоб показати погоду.",
        reply_markup=_close_keyboard(),
    )


async def weather_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_message:
        return

    if not OWM_API_KEY:
        await update.effective_message.reply_html(
            "😿 Ключ погоди не налаштований. Адмін, додай OWM_API_KEY у змінні середовища.",
            reply_markup=_close_keyboard(),
        )
        return

    user = update.effective_user
    text = update.effective_message.text or ""
    ctx_user = await get_user_addressing(user.id) if user else AddressingContext(None)

    # Визначаємо період лише для валідації дат
    mode, target_date = _parse_period(text)
    if mode == "past":
        await update.effective_message.reply_html("😿 Минулу дату показати не можу.", reply_markup=_close_keyboard())
        return
    if mode == "too_far":
        await update.effective_message.reply_html("😿 Можу показати погоду не далі ніж на 20 днів уперед.", reply_markup=_close_keyboard())
        return

    # Місто: профіль → явне в тексті → запитати
    city_name, geo, from_profile = await _resolve_city(update, context, text)
    if not geo:
        await _ask_city(update, ctx_user)
        return

    response = await _build_response(update, context, mode, target_date, city_name, geo)
    city_id = _make_city_id(geo[0], geo[1])
    show_nav = mode == "now"
    sent = await update.effective_message.reply_html(
        response,
        reply_markup=_weather_keyboard(city_id=city_id, show_nav=show_nav),
    )
    if sent:
        _remember_weather_state(context, city_id, city_name, geo)
        await _arm_weather_auto_close(context, sent)
        await _schedule_weather_auto_delete(
            context,
            chat_id=sent.chat_id,
            message_id=sent.message_id,
        )
        await _schedule_weather_auto_delete(
            context,
            chat_id=update.effective_message.chat_id,
            message_id=update.effective_message.message_id,
        )


async def weather_location(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_message or not update.effective_message.location:
        return
    if not OWM_API_KEY:
        await update.effective_message.reply_html(
            "😿 Ключ погоди не налаштований. Адмін, додай OWM_API_KEY у змінні середовища.",
            reply_markup=_close_keyboard(),
        )
        return
    loc = update.effective_message.location
    lat, lon = loc.latitude, loc.longitude
    city_name = "локація"
    geo = (lat, lon, city_name)
    ctx_user = await get_user_addressing(update.effective_user.id) if update.effective_user else AddressingContext(None)
    # За UX — одразу показуємо «зараз», а «сьогодні» даємо кнопкою
    response = await _build_response(update, context, "now", datetime.now(KYIV_TZ).date(), city_name, geo)
    city_id = _make_city_id(lat, lon)
    sent = await update.effective_message.reply_html(response, reply_markup=_weather_keyboard(city_id=city_id))
    if sent:
        _remember_weather_state(context, city_id, city_name, geo)
        await _arm_weather_auto_close(context, sent)
        await _schedule_weather_auto_delete(
            context,
            chat_id=sent.chat_id,
            message_id=sent.message_id,
        )
        await _schedule_weather_auto_delete(
            context,
            chat_id=update.effective_message.chat_id,
            message_id=update.effective_message.message_id,
        )


async def weather_close_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    await query.answer()
    cancel_auto_close(context, WEATHER_AUTO_CLOSE_KEY)
    try:
        await query.message.delete()
    except Exception:
        try:
            await query.edit_message_text("Екран погоди закрито.")
        except Exception:
            logger.debug("Не вдалося закрити екран погоди", exc_info=True)


def _city_id_from_callback(data: str, prefix: str) -> Optional[str]:
    if not data or not data.startswith(prefix):
        return None
    return data[len(prefix):]


async def weather_now_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    await query.answer()
    if not query.message:
        return
    city_id = _city_id_from_callback(query.data or "", CB_WEATHER_NOW_PREFIX)
    if not city_id:
        return
    restored = _get_weather_state(context, city_id)
    if not restored:
        try:
            await query.edit_message_text("😿 Не можу згадати це місто. Напиши ще раз: погода <місто>.")
        except Exception:
            logger.debug("Не вдалося відредагувати повідомлення погоди", exc_info=True)
        return
    city_name, geo = restored
    text = await _build_response(update, context, "now", datetime.now(KYIV_TZ).date(), city_name, geo)
    cancel_auto_close(context, WEATHER_AUTO_CLOSE_KEY)
    try:
        await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=_weather_keyboard(city_id=city_id))
    except Exception:
        logger.debug("Не вдалося відредагувати повідомлення погоди", exc_info=True)
        return
    await _arm_weather_auto_close(context, query.message)


async def weather_today_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    await query.answer()
    if not query.message:
        return

    city_id = _city_id_from_callback(query.data or "", CB_WEATHER_TODAY_PREFIX)
    if not city_id:
        return
    restored = _get_weather_state(context, city_id)
    if not restored:
        # Якщо контекст втрачено — мʼякий фолбек
        try:
            await query.edit_message_text("😿 Не можу згадати це місто. Напиши ще раз: погода <місто>.")
        except Exception:
            pass
        return

    city_name, geo = restored
    today = datetime.now(KYIV_TZ).date()
    text = await _build_response(update, context, "today", today, city_name, geo)

    cancel_auto_close(context, WEATHER_AUTO_CLOSE_KEY)
    try:
        await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=_weather_keyboard(city_id=city_id))
    except Exception:
        logger.debug("Не вдалося відредагувати повідомлення погоди", exc_info=True)
        return

    # Переозброюємо автозакриття для відредагованого повідомлення
    await _arm_weather_auto_close(context, query.message)


def register_weather_handlers(application: Application):
    """Реєстрація обробників погоди. Пріоритет вище AI, нижче нагадувань."""
    # Команда /weather
    application.add_handler(CommandHandler(["weather"], weather_command), group=1)

    # Текстовий тригер "погода ..."
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND & filters.Regex(r"(?i)^погода\b"), weather_command, block=True),
        group=1,
    )

    # Геолокація (без команд)
    application.add_handler(MessageHandler(filters.LOCATION, weather_location), group=1)

    # Кнопка Закрити
    application.add_handler(CallbackQueryHandler(weather_close_cb, pattern=f"^{CB_WEATHER_CLOSE}$"), group=1)

    # Кнопка «Сьогодні»
    application.add_handler(CallbackQueryHandler(weather_now_cb, pattern=f"^{CB_WEATHER_NOW_PREFIX}.+$"), group=1)
    application.add_handler(CallbackQueryHandler(weather_today_cb, pattern=f"^{CB_WEATHER_TODAY_PREFIX}.+$"), group=1)

    logger.info("Обробники погоди зареєстровані.")
