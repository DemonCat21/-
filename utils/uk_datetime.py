# -*- coding: utf-8 -*-
"""Українська обробка дат, часу та текстових форм.

Головні принципи:
- Пріоритет українських форм без кальок.
- Правильний апостроф U+02BC (ʼ), жодних ASCII `'`.
- Усі ключові відмінки для днів/місяців та числівників.
- Толерантність до розмовних / скорочених / помилкових варіантів.

Модуль свідомо автономний: не чіпає AI, нагадування, погоду, ігри чи економіку.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

UK_APOSTROPHE = "ʼ"  # U+02BC
_APOSTROPHE_RE = re.compile(r"[’'`‘ʼ]")
_ZERO_WIDTH_RE = re.compile(r"[\u200B-\u200D\uFEFF]")


# === ДНІ ТИЖНЯ ===
# canonical index: 0=Mon ... 6=Sun
_DAY_BASE: Dict[int, Dict[str, str]] = {
    0: {"nom": "понеділок", "acc": "понеділок", "dat": "понеділку", "loc_u": "у понеділок", "loc_v": "в понеділок"},
    1: {"nom": "вівторок", "acc": "вівторок", "dat": "вівторку", "loc_u": "у вівторок", "loc_v": "в вівторок"},
    2: {"nom": "середа", "acc": "середу", "dat": "середі", "loc_u": "у середу", "loc_v": "в середу"},
    3: {"nom": "четвер", "acc": "четвер", "dat": "четвергу", "loc_u": "у четвер", "loc_v": "в четвер"},
    4: {"nom": "пʼятниця", "acc": "пʼятницю", "dat": "пʼятниці", "loc_u": "у пʼятницю", "loc_v": "в пʼятницю"},
    5: {"nom": "субота", "acc": "суботу", "dat": "суботі", "loc_u": "в суботу", "loc_v": "у суботу"},
    6: {"nom": "неділя", "acc": "неділлю", "dat": "неділі", "loc_u": "в неділю", "loc_v": "у неділю"},
}

_DAY_ALIASES: Dict[str, int] = {}
for idx, forms in _DAY_BASE.items():
    _DAY_ALIASES[forms["nom"]] = idx
    _DAY_ALIASES[forms["acc"]] = idx
    _DAY_ALIASES[forms["dat"]] = idx
    _DAY_ALIASES[forms["loc_u"].split(" ")[-1]] = idx  # середу
    _DAY_ALIASES[forms["loc_v"].split(" ")[-1]] = idx

_DAY_ALIASES.update({
    "пн": 0, "понед": 0,
    "вт": 1,
    "ср": 2,
    "чт": 3,
    "пт": 4, "пʼятн": 4,
    "сб": 5,
    "нд": 6,
    "на вихідних": 100, "у вихідні": 100, "вихідні": 100,
    "в будні": 101, "у будні": 101, "будні": 101,
})

_RELATIVE_ANCHORS = {
    "сьогодні": "today",
    "завтра": "tomorrow",
    "післязавтра": "after_tomorrow",
    "після завтра": "after_tomorrow",
    "вчора": "yesterday",
    "позавчора": "before_yesterday",
    "на днях": "soon",
    "цими днями": "soon",
}


# === МІСЯЦІ ===
_MONTH_FORMS: Dict[int, Dict[str, str]] = {
    1: {"nom": "січень", "gen": "січня"},
    2: {"nom": "лютий", "gen": "лютого"},
    3: {"nom": "березень", "gen": "березня"},
    4: {"nom": "квітень", "gen": "квітня"},
    5: {"nom": "травень", "gen": "травня"},
    6: {"nom": "червень", "gen": "червня"},
    7: {"nom": "липень", "gen": "липня"},
    8: {"nom": "серпень", "gen": "серпня"},
    9: {"nom": "вересень", "gen": "вересня"},
    10: {"nom": "жовтень", "gen": "жовтня"},
    11: {"nom": "листопад", "gen": "листопада"},
    12: {"nom": "грудень", "gen": "грудня"},
}

_MONTH_ALIASES: Dict[str, Tuple[int, str]] = {}
for num, forms in _MONTH_FORMS.items():
    _MONTH_ALIASES[forms["nom"]] = (num, "nom")
    _MONTH_ALIASES[forms["gen"]] = (num, "gen")

_MONTH_ALIASES.update({
    "січ": (1, "stem"), "лют": (2, "stem"), "бер": (3, "stem"), "квіт": (4, "stem"),
    "трав": (5, "stem"), "чер": (6, "stem"), "лип": (7, "stem"), "серп": (8, "stem"),
    "вер": (9, "stem"), "жов": (10, "stem"), "лист": (11, "stem"), "груд": (12, "stem"),
})


# === ОРДИНАЛИ (1–31) У РОДОВОМУ ===
_ORDINAL_GENITIVE = {
    1: "першого", 2: "другого", 3: "третього", 4: "четвертого", 5: "пʼятого",
    6: "шостого", 7: "сьомого", 8: "восьмого", 9: "девʼятого", 10: "десятого",
    11: "одинадцятого", 12: "дванадцятого", 13: "тринадцятого", 14: "чотирнадцятого", 15: "пʼятнадцятого",
    16: "шістнадцятого", 17: "сімнадцятого", 18: "вісімнадцятого", 19: "девʼятнадцятого", 20: "двадцятого",
    21: "двадцять першого", 22: "двадцять другого", 23: "двадцять третього", 24: "двадцять четвертого", 25: "двадцять пʼятого",
    26: "двадцять шостого", 27: "двадцять сьомого", 28: "двадцять восьмого", 29: "двадцять девʼятого", 30: "тридцятого", 31: "тридцять першого",
}


# === ЧАСТИНИ ДОБИ ===
_DAYPARTS = {
    "зранку": "morning",
    "вранці": "morning",
    "зрання": "morning",
    "вдень": "day",
    "опівдні": "noon",
    "після обіду": "afternoon",
    "післяобіду": "afternoon",
    "увечері": "evening",
    "ввечері": "evening",
    "увечерi": "evening",
    "вночі": "night",
    "опівночі": "midnight",
}


@dataclass
class ParsedDateHint:
    """Окремі підказки про дату/час без жорсткого парсингу."""
    day_index: Optional[int] = None  # 0-6, або 100=weekend, 101=weekday
    month: Optional[int] = None  # 1-12
    relative_anchor: Optional[str] = None  # today/tomorrow/after_tomorrow/.../soon
    daypart: Optional[str] = None  # morning/evening/etc.
    explicit_time: Optional[Tuple[int, int]] = None  # (h, m)
    had_conflict: bool = False
    conflict_reason: Optional[str] = None


def normalize_uk_text(text: str) -> str:
    """NFC + прибирає zero-width + замінює апострофи на ʼ + виправляє «пят» -> «пʼят».
    Не «розмʼякшує» інші правила.
    """
    if not text:
        return ""
    s = unicodedata.normalize("NFC", text)
    s = s.replace("\u00A0", " ")
    s = _ZERO_WIDTH_RE.sub("", s)
    s = _APOSTROPHE_RE.sub(UK_APOSTROPHE, s)
    s = re.sub(r"\bпят", f"п{UK_APOSTROPHE}ят", s, flags=re.IGNORECASE)
    s = re.sub(r"\bопів['’`ʼ]?(ночі|дні)", r"опів\1", s, flags=re.IGNORECASE)
    return s


def resolve_day_token(text: str) -> Optional[int]:
    """Повертає індекс дня тижня або спец-значення (100=вихідні, 101=будні)."""
    if not text:
        return None
    s = normalize_uk_text(text).lower()
    for token, idx in _DAY_ALIASES.items():
        if re.search(rf"\b{re.escape(token)}\b", s):
            return idx
    return None


def resolve_relative_anchor(text: str) -> Optional[str]:
    if not text:
        return None
    s = normalize_uk_text(text).lower()
    for token, code in _RELATIVE_ANCHORS.items():
        if token in s:
            return code
    return None


def resolve_month_token(text: str) -> Optional[Tuple[int, str]]:
    """Повертає (місяць, форма) або None."""
    if not text:
        return None
    s = normalize_uk_text(text).lower()
    for token, payload in _MONTH_ALIASES.items():
        if re.search(rf"\b{re.escape(token)}\w*\b", s):
            return payload
    return None


def ordinal_day_genitive(day: int) -> Optional[str]:
    if 1 <= day <= 31:
        return _ORDINAL_GENITIVE.get(day)
    return None


def format_day_month(day: int, month: int, case: str = "gen") -> Optional[str]:
    """Форматує «5 березня» або «пʼятого березня» (case="gen_ordinal")."""
    forms = _MONTH_FORMS.get(month)
    if not forms:
        return None
    if case == "gen_ordinal":
        ord_word = ordinal_day_genitive(day)
        if not ord_word:
            return None
        return f"{ord_word} {forms['gen']}"
    name = forms.get("gen" if case == "gen" else "nom")
    if not name:
        return None
    return f"{day} {name}"


def resolve_daypart(text: str) -> Optional[str]:
    if not text:
        return None
    s = normalize_uk_text(text).lower()
    for token, code in _DAYPARTS.items():
        if token in s:
            return code
    return None


_TIME_RE = re.compile(r"\b(?:о\s*)?(\d{1,2})(?::(\d{2}))?\b")


def extract_time(text: str) -> Optional[Tuple[int, int]]:
    """Витягує явно вказаний час (годину та, опційно, хвилини)."""
    if not text:
        return None
    s = normalize_uk_text(text)
    m = _TIME_RE.search(s)
    if not m:
        return None
    h = int(m.group(1))
    mi = int(m.group(2) or 0)
    if 0 <= h <= 23 and 0 <= mi <= 59:
        return (h, mi)
    return None


_REL_OFFSET_RE = re.compile(r"через\s+(півтори|пів|\d+)\s*(хв|хвилин|хвилини|годин|години|годину|день|дні|днів|тиждень|тижні|тижнів|місяць|місяці|місяців|рік|роки|років)", re.IGNORECASE)


def extract_relative_offset(text: str) -> Optional[Tuple[float, str]]:
    """Повертає (кількість, одиниця) з фрази «через X ...» або «за X ...».
    1.5 повертається для «півтори», 0.5 для «пів».
    """
    if not text:
        return None
    s = normalize_uk_text(text)
    m = _REL_OFFSET_RE.search(s)
    if not m:
        return None
    raw_num, unit = m.group(1), m.group(2).lower()
    if raw_num == "півтори":
        num = 1.5
    elif raw_num == "пів":
        num = 0.5
    else:
        num = float(raw_num)
    return num, unit


def pluralize_unit(n: int, forms: Tuple[str, str, str]) -> str:
    """Українська множина: (1, 2-4, 5+)."""
    n_abs = abs(int(n))
    mod10 = n_abs % 10
    mod100 = n_abs % 100
    if mod10 == 1 and mod100 != 11:
        return forms[0]
    if 2 <= mod10 <= 4 and not (12 <= mod100 <= 14):
        return forms[1]
    return forms[2]


_UNIT_FORMS = {
    "хв": ("хвилина", "хвилини", "хвилин"),
    "хвилина": ("хвилина", "хвилини", "хвилин"),
    "хвилини": ("хвилина", "хвилини", "хвилин"),
    "хвилин": ("хвилина", "хвилини", "хвилин"),
    "година": ("година", "години", "годин"),
    "години": ("година", "години", "годин"),
    "годин": ("година", "години", "годин"),
    "день": ("день", "дні", "днів"),
    "дні": ("день", "дні", "днів"),
    "днів": ("день", "дні", "днів"),
    "тиждень": ("тиждень", "тижні", "тижнів"),
    "тижні": ("тиждень", "тижні", "тижнів"),
    "тижнів": ("тиждень", "тижні", "тижнів"),
    "місяць": ("місяць", "місяці", "місяців"),
    "місяці": ("місяць", "місяці", "місяців"),
    "місяців": ("місяць", "місяці", "місяців"),
    "рік": ("рік", "роки", "років"),
    "роки": ("рік", "роки", "років"),
    "років": ("рік", "роки", "років"),
}


def format_offset(num: float, unit: str) -> Optional[str]:
    base_forms = _UNIT_FORMS.get(unit)
    if base_forms is None:
        return None
    if num.is_integer():
        n_int = int(num)
        return f"{n_int} {pluralize_unit(n_int, base_forms)}"
    # Для 1.5 / 0.5 використовуємо середню форму (родовий множини)
    return f"{num:g} {base_forms[2]}"


def detect_conflicts(text: str) -> Optional[str]:
    """Шукає суперечливі анкори (наприклад, «через два дні післязавтра»)."""
    s = normalize_uk_text(text).lower()
    anchors: List[str] = []
    for token in _RELATIVE_ANCHORS:
        if token in s:
            anchors.append(token)
    # Два і більше різні відносні анкори — конфлікт
    if len(set(anchors)) > 1:
        return "Знайшлося кілька різних відносних дат, уточніть одну."
    # Offset + ще один анкор
    has_offset = bool(_REL_OFFSET_RE.search(s))
    if has_offset and anchors:
        return "Є і відлік «через …», і фіксована відносна дата. Уточніть одну."
    # Вихідні + будні одночасно
    if "вихідн" in s and "будн" in s:
        return "Не можу поєднати «вихідні» і «будні». Уточніть, будь ласка."
    return None


def build_hint(text: str) -> ParsedDateHint:
    """Надає мʼякий словесний розбір без привʼязки до конкретного datetime.
    Мета — безпечно підказати, що користувач мав на увазі, і перевірити суперечності.
    """
    s = normalize_uk_text(text)
    conflict = detect_conflicts(s)
    hint = ParsedDateHint()
    if conflict:
        hint.had_conflict = True
        hint.conflict_reason = conflict
        return hint

    hint.day_index = resolve_day_token(s)
    hint.relative_anchor = resolve_relative_anchor(s)
    month_payload = resolve_month_token(s)
    if month_payload:
        hint.month = month_payload[0]
    hint.daypart = resolve_daypart(s)
    hint.explicit_time = extract_time(s)
    return hint


__all__ = [
    "UK_APOSTROPHE",
    "normalize_uk_text",
    "resolve_day_token",
    "resolve_month_token",
    "resolve_relative_anchor",
    "ordinal_day_genitive",
    "format_day_month",
    "resolve_daypart",
    "extract_time",
    "extract_relative_offset",
    "pluralize_unit",
    "format_offset",
    "detect_conflicts",
    "build_hint",
    "ParsedDateHint",
]
