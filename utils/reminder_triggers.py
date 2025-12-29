# -*- coding: utf-8 -*-
"""bot.utils.reminder_triggers

Єдине джерело правди для тригерів нагадувань.

Правило пріоритету:
Якщо текст повідомлення ПОЧИНАЄТЬСЯ з одного з дозволених тригерів
(звернення + точне слово "нагадай"), то це повідомлення має
оброблятися виключно модулем нагадувань. AI повністю ігнорує.

Список тригерів строго фіксований — без варіацій.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Optional, Tuple


# ✅ ДОЗВОЛЕНІ ТРИГЕРИ (тільки ці, без варіацій)
REMINDER_TRIGGERS: Tuple[str, ...] = (
    "кошеня, нагадай",
    "кошеня нагадай",
    "котик, нагадай",
    "котик нагадай",
    "котику, нагадай",
    "котику нагадай",
    "бот, нагадай",
    "бот нагадай",
    "ботик, нагадай",
    "ботик нагадай",
    "ботику, нагадай",
    "ботику нагадай",
)


def build_trigger_regex() -> str:
    """Повертає regex-рядок для PTB filters.Regex.

    - Дозволяємо лише лідируючі пробіли (\s*).
    - Внутрішній формат тригерів не нормалізуємо: подвійні пробіли тощо — НЕ тригер.
    - Після слова "нагадай" вимагаємо word boundary, щоб "нагадайка" не матчило.
    """
    escaped = [re.escape(t) for t in REMINDER_TRIGGERS]
    # (?is) = IGNORECASE + DOTALL
    # NB: Regex лишається як додатковий шлях (для «звичайних» випадків).
    # Основний шлях (з Unicode-normalize) реалізовано в is_reminder_trigger().
    return r"(?is)^\s*(?:" + "|".join(escaped) + r")\b"


REMINDER_TRIGGER_REGEX: str = build_trigger_regex()
REMINDER_TRIGGER_RE = re.compile(REMINDER_TRIGGER_REGEX, re.IGNORECASE | re.UNICODE | re.DOTALL)


def _normalize_for_trigger(text: str) -> str:
    """Нормалізує текст для стабільного матчінгу тригерів.

    Чому це потрібно:
    - У Telegram/Windows інколи прилітає комбінований символ «й» як
      'и' + COMBINING BREVE (U+0306), а не precomposed 'й'.
      Візуально це однаково, але startswith/regex НЕ спрацьовує.

    Важливо:
    - Ми НЕ «пом'якшуємо» правила (не стискаємо подвійні пробіли тощо).
    - Лише приводимо Unicode до NFC і прибираємо технічні пробіли.
    """

    s = unicodedata.normalize("NFC", text)
    # NBSP -> звичайний пробіл
    s = s.replace("\u00A0", " ")
    # Zero-width spaces
    s = s.replace("\u200B", "").replace("\u200C", "").replace("\u200D", "")
    return s


def is_reminder_trigger(text: Optional[str]) -> bool:
    if not text:
        return False
    s = _normalize_for_trigger(text).lower().lstrip()

    # Строга перевірка: тільки дозволені префікси, і ПІСЛЯ них має бути
    # кінець рядка або пробіл/перенос. Це відсікає «нагадайка».
    for t in REMINDER_TRIGGERS:
        if s.startswith(t):
            if len(s) == len(t):
                return True
            nxt = s[len(t)]
            if nxt.isspace():
                return True
            return False

    # Як fallback (не обов'язково, але корисно) — regex.
    return REMINDER_TRIGGER_RE.match(_normalize_for_trigger(text).strip()) is not None


def strip_trigger_prefix(text: str) -> Tuple[bool, str]:
    """(activated, rest_text).

    Якщо це тригер — повертаємо решту тексту після префікса.
    """
    if not text:
        return False, ""
    raw = _normalize_for_trigger(text)
    s = raw.strip()

    # Спершу — через строгий startswith (той самий, що в is_reminder_trigger)
    low = s.lower().lstrip()
    for t in REMINDER_TRIGGERS:
        if low.startswith(t):
            # позицію зрізу рахуємо по оригінальному рядку після lstrip()
            # (щоб не з'їсти пробіли всередині).
            lstrip_len = len(s) - len(s.lstrip())
            cut_at = lstrip_len + len(t)
            # Якщо далі НЕ пробіл — це не тригер
            if len(s) > cut_at and not s[cut_at].isspace():
                return False, s
            rest = s[cut_at:].lstrip()
            return True, rest

    # Fallback: regex
    m = REMINDER_TRIGGER_RE.match(s)
    if not m:
        return False, s
    rest = s[m.end():].lstrip()
    return True, rest
