
# -*- coding: utf-8 -*-
import pytest

import bot.core.database as db


@pytest.mark.asyncio
async def test_record_win_lose_draw_idempotent(tmp_path, monkeypatch):
    # Use isolated DB
    test_db_path = tmp_path / "memory.db"
    monkeypatch.setattr(db, "DB_PATH", str(test_db_path))

    await db.init_db()

    u1, u2 = 111, 222

    # WIN
    db.mems_set_current_game_id("game_win_1")
    ok1 = await db.record_memes_cats_game(u1, u2, "win")
    ok2 = await db.record_memes_cats_game(u1, u2, "win")  # duplicate
    db.mems_set_current_game_id(None)

    assert ok1 is True
    assert ok2 is False

    # LOSE (for u2)
    db.mems_set_current_game_id("game_win_1")  # same match id, but different user row -> allowed once
    ok3 = await db.record_memes_cats_game(u2, u1, "lose")
    ok4 = await db.record_memes_cats_game(u2, u1, "lose")  # duplicate
    db.mems_set_current_game_id(None)

    assert ok3 is True
    assert ok4 is False

    # DRAW
    db.mems_set_current_game_id("game_draw_1")
    ok5 = await db.record_memes_cats_game(u1, u2, "draw")
    ok6 = await db.record_memes_cats_game(u2, u1, "draw")
    ok7 = await db.record_memes_cats_game(u1, u2, "draw")  # duplicate for u1
    db.mems_set_current_game_id(None)

    assert ok5 is True
    assert ok6 is True
    assert ok7 is False

    top = await db.mems_get_top(limit=10)
    by_id = {r["user_id"]: r for r in top}

    assert by_id[u1]["wins"] == 1
    assert by_id[u1]["draws"] == 1
    assert by_id[u1]["games"] == 2

    assert by_id[u2]["wins"] == 0
    # losses are tracked in DB, but top returns only wins/draws/games
    assert by_id[u2]["draws"] == 1
    assert by_id[u2]["games"] == 2


@pytest.mark.asyncio
async def test_record_survives_restart(tmp_path, monkeypatch):
    test_db_path = tmp_path / "memory.db"
    monkeypatch.setattr(db, "DB_PATH", str(test_db_path))
    await db.init_db()

    u1, u2 = 1, 2

    db.mems_set_current_game_id("game_1")
    assert await db.record_memes_cats_game(u1, u2, "win") is True
    db.mems_set_current_game_id(None)

    # "Restart" simulation: call again with same game id should still be idempotent
    db.mems_set_current_game_id("game_1")
    assert await db.record_memes_cats_game(u1, u2, "win") is False
    db.mems_set_current_game_id(None)

    top = await db.mems_get_top(limit=10)
    assert any(r["user_id"] == u1 and r["wins"] == 1 and r["games"] == 1 for r in top)
