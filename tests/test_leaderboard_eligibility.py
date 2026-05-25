"""Tests : eligibilite du leaderboard Pro Queue permanent.

Deux regles (Pro Queue permanent uniquement, cf. brainstorming 2026-05-25) :
  - minimum 5 games joues (wins + losses >= 5) pour apparaitre
  - retire si derniere partie (last_played) absente ou strictement > 7 jours

Open / GC / hebdo ne sont PAS filtres.
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import mongomock
import pytest

from services import leaderboard_refresh
from services.leaderboard_refresh import (
    PRO_LEADERBOARD_INACTIVITY,
    PRO_LEADERBOARD_MIN_GAMES,
    _clear_page_cache_for_tests,
    _is_pro_leaderboard_eligible,
    build_leaderboard_payload,
)
from services.repository import get_elo_col, get_elo_weekly_col, player_doc_id

NOW = datetime(2026, 5, 25, 12, 0, 0, tzinfo=UTC)


def _doc(*, wins=5, losses=0, last_played=NOW):
    return {"wins": wins, "losses": losses, "last_played": last_played}


# ── _is_pro_leaderboard_eligible (logique pure) ───────────────────


def test_constants_match_spec():
    assert PRO_LEADERBOARD_MIN_GAMES == 5
    assert timedelta(days=7) == PRO_LEADERBOARD_INACTIVITY


def test_fewer_than_5_games_is_ineligible():
    assert _is_pro_leaderboard_eligible(_doc(wins=2, losses=2), NOW) is False


def test_exactly_5_games_recent_is_eligible():
    assert _is_pro_leaderboard_eligible(_doc(wins=3, losses=2), NOW) is True


def test_inactive_over_7_days_is_ineligible():
    doc = _doc(wins=5, losses=0, last_played=NOW - timedelta(days=8))
    assert _is_pro_leaderboard_eligible(doc, NOW) is False


def test_exactly_7_days_is_still_eligible():
    doc = _doc(wins=5, losses=0, last_played=NOW - timedelta(days=7))
    assert _is_pro_leaderboard_eligible(doc, NOW) is True


def test_missing_last_played_is_ineligible():
    doc = _doc(wins=5, losses=0, last_played=None)
    assert _is_pro_leaderboard_eligible(doc, NOW) is False


def test_naive_last_played_treated_as_utc():
    doc = _doc(wins=5, losses=0, last_played=NOW.replace(tzinfo=None))
    assert _is_pro_leaderboard_eligible(doc, NOW) is True


# ── build_leaderboard_payload : filtrage Pro ──────────────────────


def _guild(guild_id: int = 4242):
    guild = MagicMock()
    guild.id = guild_id
    guild.name = "TestGuild"
    member = MagicMock()
    member.display_name = "P"
    member.display_avatar.replace.return_value.url = "http://av/p.png"
    guild.get_member.return_value = member
    return guild


def _insert(col, uid, queue_type, *, wins, losses, last_played, elo=2500):
    doc = {
        "_id": player_doc_id(uid, queue_type),
        "user_id": str(uid),
        "name": f"P{uid}",
        "elo": elo,
        "wins": wins,
        "losses": losses,
        "queue_type": queue_type,
    }
    if last_played is not None:
        doc["last_played"] = last_played
    col.insert_one(doc)


@pytest.mark.asyncio
async def test_pro_player_under_5_games_is_hidden():
    _clear_page_cache_for_tests()
    db = mongomock.MongoClient(tz_aware=True).db
    _insert(get_elo_col(db), 1, "pro", wins=2, losses=2, last_played=datetime.now(UTC))
    file, _ = await build_leaderboard_payload(_guild(), db, queue_type="pro")
    assert file is None


@pytest.mark.asyncio
async def test_pro_player_inactive_is_hidden():
    _clear_page_cache_for_tests()
    db = mongomock.MongoClient(tz_aware=True).db
    stale = datetime.now(UTC) - timedelta(days=10)
    _insert(get_elo_col(db), 1, "pro", wins=5, losses=0, last_played=stale)
    file, _ = await build_leaderboard_payload(_guild(), db, queue_type="pro")
    assert file is None


@pytest.mark.asyncio
async def test_pro_player_without_last_played_is_hidden():
    _clear_page_cache_for_tests()
    db = mongomock.MongoClient(tz_aware=True).db
    _insert(get_elo_col(db), 1, "pro", wins=5, losses=0, last_played=None)
    file, _ = await build_leaderboard_payload(_guild(), db, queue_type="pro")
    assert file is None


@pytest.mark.asyncio
async def test_pro_eligible_player_is_shown():
    _clear_page_cache_for_tests()
    db = mongomock.MongoClient(tz_aware=True).db
    _insert(get_elo_col(db), 1, "pro", wins=5, losses=0, last_played=datetime.now(UTC))
    file, _ = await build_leaderboard_payload(_guild(), db, queue_type="pro")
    assert file is not None


@pytest.mark.asyncio
async def test_open_queue_is_not_filtered():
    # 1 game, pas de last_played : serait cache en Pro, mais Open est exempt.
    _clear_page_cache_for_tests()
    db = mongomock.MongoClient(tz_aware=True).db
    _insert(get_elo_col(db), 1, "open", wins=1, losses=0, last_played=None)
    file, _ = await build_leaderboard_payload(_guild(), db, queue_type="open")
    assert file is not None


@pytest.mark.asyncio
async def test_weekly_pro_is_not_filtered():
    _clear_page_cache_for_tests()
    db = mongomock.MongoClient(tz_aware=True).db
    _insert(get_elo_weekly_col(db), 1, "pro", wins=1, losses=0, last_played=None)
    file, _ = await build_leaderboard_payload(_guild(), db, queue_type="pro", weekly=True)
    assert file is not None


@pytest.mark.asyncio
async def test_ineligible_pro_players_do_not_leave_rank_gaps():
    # Un joueur ineligible intercale (elo 2450, 2 games) ne doit pas
    # consommer de rang : les rangs restent 1..N contigus.
    _clear_page_cache_for_tests()
    db = mongomock.MongoClient(tz_aware=True).db
    col = get_elo_col(db)
    now = datetime.now(UTC)
    _insert(col, 1, "pro", wins=10, losses=0, last_played=now, elo=2500)
    _insert(col, 2, "pro", wins=1, losses=1, last_played=now, elo=2450)  # 2 games -> cache
    _insert(col, 3, "pro", wins=5, losses=0, last_played=now, elo=2400)

    captured: dict = {}
    real = leaderboard_refresh.generate_leaderboard

    def spy(chunk, **kw):
        captured["rows"] = chunk
        return real(chunk, **kw)

    leaderboard_refresh.generate_leaderboard = spy
    try:
        await build_leaderboard_payload(_guild(), db, queue_type="pro")
    finally:
        leaderboard_refresh.generate_leaderboard = real

    rows = captured["rows"]
    assert [r["rank"] for r in rows] == [1, 2]
    assert [r["elo"] for r in rows] == [2500, 2400]  # 2450 (ineligible) exclu
