"""Tests du cog cogs/leaderboard_weekly + helpers repository associes."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import mongomock

from cogs.leaderboard_weekly import _last_weekly_boundary, LeaderboardWeeklyCog
from services import repository


PARIS = ZoneInfo("Europe/Paris")


def test_last_weekly_boundary_on_monday_morning():
    # Lundi 10/05/2026 09:00 Paris -> boundary = meme jour 00:00
    now = datetime(2026, 5, 11, 9, 0, tzinfo=PARIS)
    assert _last_weekly_boundary(now) == datetime(2026, 5, 11, 0, 0, tzinfo=PARIS)


def test_last_weekly_boundary_on_wednesday():
    # Mercredi 13/05/2026 -> boundary = Lundi 11/05/2026 00:00
    now = datetime(2026, 5, 13, 18, 30, tzinfo=PARIS)
    assert _last_weekly_boundary(now) == datetime(2026, 5, 11, 0, 0, tzinfo=PARIS)


def test_last_weekly_boundary_on_sunday_evening():
    # Dimanche 17/05/2026 23:00 Paris -> boundary = Lundi 11/05/2026 00:00
    now = datetime(2026, 5, 17, 23, 0, tzinfo=PARIS)
    assert _last_weekly_boundary(now) == datetime(2026, 5, 11, 0, 0, tzinfo=PARIS)


def test_repository_weekly_helpers_roundtrip():
    db = mongomock.MongoClient(tz_aware=True).db
    assert repository.get_last_weekly_reset(db) is None

    when = datetime(2026, 5, 11, 0, 0, tzinfo=UTC)
    repository.set_last_weekly_reset(db, when)
    got = repository.get_last_weekly_reset(db)
    assert got is not None
    assert got == when


def test_reset_weekly_elo_clears_collection():
    db = mongomock.MongoClient(tz_aware=True).db
    col = repository.get_elo_weekly_col(db)
    col.insert_one({"_id": "1:pro", "elo": 2000, "queue_type": "pro"})
    col.insert_one({"_id": "2:pro", "elo": 2050, "queue_type": "pro"})
    assert col.count_documents({}) == 2

    deleted = repository.reset_weekly_elo(db)
    assert deleted == 2
    assert col.count_documents({}) == 0


async def test_maybe_reset_triggers_on_first_run():
    """Premier run jamais : last_reset=None -> reset declenche."""
    db = mongomock.MongoClient(tz_aware=True).db
    col = repository.get_elo_weekly_col(db)
    col.insert_one({"_id": "1:pro", "elo": 2000, "queue_type": "pro"})

    bot = MagicMock()
    bot.guilds = []
    bot.wait_until_ready = AsyncMock()
    cog = LeaderboardWeeklyCog.__new__(LeaderboardWeeklyCog)
    cog.bot = bot
    cog.db = db

    await cog._maybe_reset()

    assert col.count_documents({}) == 0
    assert repository.get_last_weekly_reset(db) is not None


async def test_maybe_reset_skips_when_already_reset_this_week():
    """Si last_reset >= boundary (deja reset cette semaine), no-op."""
    db = mongomock.MongoClient(tz_aware=True).db
    col = repository.get_elo_weekly_col(db)
    col.insert_one({"_id": "1:pro", "elo": 2000, "queue_type": "pro"})

    # Marque un reset tout juste effectue (now est forcement > last_monday)
    repository.set_last_weekly_reset(db, datetime.now(UTC))

    bot = MagicMock()
    bot.guilds = []
    bot.wait_until_ready = AsyncMock()
    cog = LeaderboardWeeklyCog.__new__(LeaderboardWeeklyCog)
    cog.bot = bot
    cog.db = db

    await cog._maybe_reset()

    # La collection n'a pas ete videe
    assert col.count_documents({}) == 1


async def test_maybe_reset_triggers_when_last_reset_predates_boundary():
    """Si last_reset < boundary (semaine precedente), reset re-declenche."""
    db = mongomock.MongoClient(tz_aware=True).db
    col = repository.get_elo_weekly_col(db)
    col.insert_one({"_id": "1:pro", "elo": 2000, "queue_type": "pro"})

    repository.set_last_weekly_reset(db, datetime.now(UTC) - timedelta(days=14))

    bot = MagicMock()
    bot.guilds = []
    bot.wait_until_ready = AsyncMock()
    cog = LeaderboardWeeklyCog.__new__(LeaderboardWeeklyCog)
    cog.bot = bot
    cog.db = db

    await cog._maybe_reset()

    assert col.count_documents({}) == 0
