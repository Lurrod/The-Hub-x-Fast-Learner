"""Integration test for cog_load orphan cleanup (Task 13)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.mark.asyncio
async def test_cog_load_deletes_orphan_categories(monkeypatch):
    """cog_load must query in-progress + validated + contested match
    category_ids and call cleanup_orphan_match_categories per guild.
    Uses the real status strings persisted by the bot (pending /
    validated_a / validated_b / contested), not synthetic ones."""
    from cogs.match._cog import MatchCog
    from cogs.match import _cog as match_cog_module

    cleanup_mock = AsyncMock(return_value=2)
    monkeypatch.setattr(match_cog_module, "cleanup_orphan_match_categories", cleanup_mock)

    bot = MagicMock()
    guild_a = MagicMock(name="GuildA")
    guild_b = MagicMock(name="GuildB")
    bot.guilds = [guild_a, guild_b]

    db = MagicMock()
    db["matches"].find = MagicMock(
        return_value=iter(
            [
                {"category_id": 100, "status": "pending"},
                {"category_id": 200, "status": "contested"},
                {"category_id": 300, "status": "validated_a"},
            ]
        )
    )

    cog = MatchCog(bot, db)
    await cog.cog_load()

    # Called once per guild
    assert cleanup_mock.await_count == 2
    # pending + contested + validated_a category IDs all protected
    first_call_kwargs = cleanup_mock.await_args_list[0].kwargs
    assert first_call_kwargs["active_category_ids"] == {100, 200, 300}

    # Verify the Mongo filter targets the right statuses. cog_load now
    # emet plusieurs requetes find() (active_ids puis recovery per-guild) ;
    # on cherche celle qui filtre par status.
    status_call = next(c for c in db["matches"].find.call_args_list if "status" in c.args[0])
    status_filter = status_call.args[0]["status"]["$in"]
    assert set(status_filter) == {"pending", "validated_a", "validated_b", "contested"}


@pytest.mark.asyncio
async def test_cog_load_handles_empty_active_set(monkeypatch):
    """When no active matches exist, cleanup is called with empty set."""
    from cogs.match._cog import MatchCog
    from cogs.match import _cog as match_cog_module

    cleanup_mock = AsyncMock(return_value=5)
    monkeypatch.setattr(match_cog_module, "cleanup_orphan_match_categories", cleanup_mock)

    bot = MagicMock()
    bot.guilds = [MagicMock()]
    db = MagicMock()
    db["matches"].find = MagicMock(return_value=iter([]))

    cog = MatchCog(bot, db)
    await cog.cog_load()

    cleanup_mock.assert_awaited_once()
    assert cleanup_mock.await_args.kwargs["active_category_ids"] == set()


@pytest.mark.asyncio
async def test_cog_load_per_guild_error_does_not_block_others(monkeypatch):
    """A cleanup error in one guild must not stop processing of other guilds."""
    from cogs.match._cog import MatchCog
    from cogs.match import _cog as match_cog_module

    cleanup_mock = AsyncMock(side_effect=[RuntimeError("boom"), 3])
    monkeypatch.setattr(match_cog_module, "cleanup_orphan_match_categories", cleanup_mock)

    bot = MagicMock()
    bot.guilds = [MagicMock(name="bad"), MagicMock(name="good")]
    db = MagicMock()
    db["matches"].find = MagicMock(return_value=iter([]))

    cog = MatchCog(bot, db)
    # Must not raise
    await cog.cog_load()

    # Second guild still got processed
    assert cleanup_mock.await_count == 2


@pytest.mark.asyncio
async def test_cog_load_excludes_in_flight_cleanup_categories(monkeypatch):
    """Recovery apres crash partiel : un match dont la cleanup a ete
    amorcee (delete_started_at set) mais dont le status est encore actif
    doit etre retire de active_category_ids pour que orphan_cleanup
    finisse le travail."""
    from cogs.match._cog import MatchCog
    from cogs.match import _cog as match_cog_module

    cleanup_mock = AsyncMock(return_value=1)
    monkeypatch.setattr(match_cog_module, "cleanup_orphan_match_categories", cleanup_mock)
    # 200 est marquee in-flight cleanup -> doit etre retiree malgre
    # son status actif.
    monkeypatch.setattr(
        match_cog_module.repository,
        "find_category_ids_with_cleanup_started",
        lambda db, *, origin_guild_id: {200},
    )

    bot = MagicMock()
    guild = MagicMock()
    guild.id = 42
    bot.guilds = [guild]

    db = MagicMock()
    db["matches"].find = MagicMock(
        return_value=iter(
            [
                {"category_id": 100, "status": "pending"},
                {"category_id": 200, "status": "validated_a"},  # in-flight cleanup
                {"category_id": 300, "status": "contested"},
            ]
        )
    )

    cog = MatchCog(bot, db)
    await cog.cog_load()

    passed_ids = cleanup_mock.await_args.kwargs["active_category_ids"]
    assert passed_ids == {100, 300}
    assert 200 not in passed_ids
