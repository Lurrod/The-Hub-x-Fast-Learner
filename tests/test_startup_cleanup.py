"""Integration test for cog_load orphan cleanup (Task 13)."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.mark.asyncio
async def test_cog_load_deletes_orphan_categories(monkeypatch):
    """cog_load must query active+disputed match category_ids and call
    cleanup_orphan_match_categories per guild."""
    from cogs.match._cog import MatchCog
    from cogs.match import _cog as match_cog_module

    cleanup_mock = AsyncMock(return_value=2)
    monkeypatch.setattr(
        match_cog_module, "cleanup_orphan_match_categories", cleanup_mock
    )

    bot = MagicMock()
    guild_a = MagicMock(name="GuildA")
    guild_b = MagicMock(name="GuildB")
    bot.guilds = [guild_a, guild_b]

    db = MagicMock()
    db["matches"].find = MagicMock(return_value=iter([
        {"category_id": 100, "status": "active"},
        {"category_id": 200, "status": "disputed"},
    ]))

    cog = MatchCog(bot, db)
    await cog.cog_load()

    # Called once per guild
    assert cleanup_mock.await_count == 2
    # Both active and disputed category IDs are protected
    first_call_kwargs = cleanup_mock.await_args_list[0].kwargs
    assert first_call_kwargs["active_category_ids"] == {100, 200}


@pytest.mark.asyncio
async def test_cog_load_handles_empty_active_set(monkeypatch):
    """When no active matches exist, cleanup is called with empty set."""
    from cogs.match._cog import MatchCog
    from cogs.match import _cog as match_cog_module

    cleanup_mock = AsyncMock(return_value=5)
    monkeypatch.setattr(
        match_cog_module, "cleanup_orphan_match_categories", cleanup_mock
    )

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
    monkeypatch.setattr(
        match_cog_module, "cleanup_orphan_match_categories", cleanup_mock
    )

    bot = MagicMock()
    bot.guilds = [MagicMock(name="bad"), MagicMock(name="good")]
    db = MagicMock()
    db["matches"].find = MagicMock(return_value=iter([]))

    cog = MatchCog(bot, db)
    # Must not raise
    await cog.cog_load()

    # Second guild still got processed
    assert cleanup_mock.await_count == 2
