# -*- coding: utf-8 -*-
"""bot.features.new_year_mode

Новорічний режим — невеличка сезонна магія для котиків. 🎄🐾
- AUTO: активується 20.12–10.01 (Europe/Kyiv)
- ON/OFF: ручний оверрайд для конкретного чату
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, date
from zoneinfo import ZoneInfo
from typing import Tuple

from bot.core.database import get_chat_settings

KYIV_TZ = ZoneInfo("Europe/Kyiv")

NEW_YEAR_START = (12, 20)  # 20 грудня
NEW_YEAR_END = (1, 10)     # 10 січня (включно)

VALID_MODES = ("auto", "on", "off")


def is_in_new_year_period(dt: datetime | None = None) -> bool:
    """Чи ми зараз у проміжку 20.12–10.01 (включно)."""
    if dt is None:
        dt = datetime.now(KYIV_TZ)
    d = dt.date()
    # період перетинає рік: 20.12..31.12 або 01.01..10.01
    if d.month == 12 and d.day >= NEW_YEAR_START[1]:
        return True
    if d.month == 1 and d.day <= NEW_YEAR_END[1]:
        return True
    return False


async def is_new_year_mode(chat_id: int) -> bool:
    """Ефективний прапорець новорічного режиму для чату."""
    settings = await get_chat_settings(chat_id)
    mode = str(settings.get("new_year_mode", "auto") or "auto").lower().strip()
    if mode == "on":
        return True
    if mode == "off":
        return False
    # auto
    return is_in_new_year_period()


def format_new_year_mode(mode: str, active_now: bool) -> str:
    """Людяний статус для меню."""
    mode = (mode or "auto").lower().strip()
    if mode not in VALID_MODES:
        mode = "auto"
    if mode == "on":
        return "ON ✅"
    if mode == "off":
        return "OFF ❌"
    # auto
    return "AUTO 🎄" + (" (активний)" if active_now else "")


def apply_new_year_style(text: str) -> str:
    """Дуже легенький зимовий вайб для нових повідомлень (не для AI)."""
    text = text.strip()
    if not text:
        return text
    # без перебору: 1-2 емодзі максимум
    if text[0] in "🎄❄️🍊🐾😺✨":
        return text
    return f"🎄 {text}"
