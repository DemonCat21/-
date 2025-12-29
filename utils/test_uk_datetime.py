# -*- coding: utf-8 -*-
import re
import pytest

from bot.utils.uk_datetime import (
    UK_APOSTROPHE,
    build_hint,
    detect_conflicts,
    extract_relative_offset,
    extract_time,
    format_day_month,
    format_offset,
    normalize_uk_text,
    ordinal_day_genitive,
    pluralize_unit,
    resolve_day_token,
    resolve_daypart,
    resolve_month_token,
    resolve_relative_anchor,
)


def test_normalize_apostrophes_and_typos():
    src = "п'ятниця пятого опів'ночі"
    normalized = normalize_uk_text(src)
    assert "пят" not in normalized
    assert UK_APOSTROPHE in normalized
    assert "опівночі" in normalized


def test_day_and_month_resolution():
    assert resolve_day_token("в пʼятницю ввечері") == 4
    assert resolve_day_token("на вихідних у суботу") == 5  # конкретний день переважає
    assert resolve_month_token("5 березня") == (3, "gen")
    assert resolve_month_token("початок жов") == (10, "stem")


def test_relative_and_daypart():
    assert resolve_relative_anchor("на завтра зранку") == "tomorrow"
    assert resolve_daypart("зранку") == "morning"


def test_time_and_offsets():
    assert extract_time("о 7:30 ранку") == (7, 30)
    assert extract_relative_offset("через півтори години") == (1.5, "години")
    assert extract_relative_offset("через 5 днів") == (5.0, "днів")


def test_ordinals_and_formatting():
    assert ordinal_day_genitive(5) == "пʼятого"
    assert format_day_month(5, 3, "gen") == "5 березня"
    assert format_day_month(5, 3, "gen_ordinal") == "пʼятого березня"


def test_plural_and_offset_format():
    assert pluralize_unit(2, ("день", "дні", "днів")) == "дні"
    assert format_offset(2, "години") == "2 години"
    assert format_offset(5, "години") == "5 годин"


def test_conflict_detection():
    assert detect_conflicts("через два дні післязавтра") is not None
    hint = build_hint("у суботу ввечері о 8")
    assert hint.day_index == 5
    assert hint.daypart == "evening"
    assert hint.explicit_time == (8, 0)


@pytest.fixture(autouse=True)
def _ensure_apostrophes_only():
    # Запобіжник: якщо десь випадково використали ASCII апостроф, тест провалиться.
    from pathlib import Path

    src = Path(__file__).read_text(encoding="utf-8")
    assert "'" not in re.findall(r"[А-Яа-яЁёЇїІіЄєҐґ]'[А-Яа-яЁёЇїІіЄєҐґ]", src), "Не використовуйте ASCII апостроф у словах"
