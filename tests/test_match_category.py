"""Tests for services/match_category.py — dynamic match category lifecycle."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import discord
import pytest


@pytest.mark.asyncio
async def test_create_match_category_returns_match_channels_dataclass():
    from services.match_category import MatchChannels, create_match_category

    cat = MagicMock(name="Category Match #1")
    text = MagicMock(name="match-preparation")
    vc1 = MagicMock(name="Team 1")
    vc2 = MagicMock(name="Team 2")
    waiting = MagicMock(name="Waiting Match")

    guild = MagicMock()
    guild.create_category = AsyncMock(return_value=cat)
    cat.create_text_channel = AsyncMock(return_value=text)
    cat.create_voice_channel = AsyncMock(side_effect=[vc1, vc2, waiting])
    guild.get_member = MagicMock(return_value=None)
    guild.default_role = MagicMock()
    guild.me = MagicMock()
    guild.me.top_role = MagicMock()

    channels = await create_match_category(
        guild=guild,
        match_number=1,
        player_ids=[],
        admin_role_ids=[],
    )

    assert isinstance(channels, MatchChannels)
    assert channels.category is cat
    assert channels.prep_channel is text
    assert channels.team1_vc is vc1
    assert channels.team2_vc is vc2
    assert channels.waiting_match_vc is waiting


@pytest.mark.asyncio
async def test_create_match_category_overwrites_deny_everyone_and_allow_players():
    from services.match_category import create_match_category

    captured = {}

    async def fake_create_category(name, **kwargs):
        captured["name"] = name
        captured["overwrites"] = kwargs.get("overwrites") or {}
        category = MagicMock()
        category.create_text_channel = AsyncMock(return_value=MagicMock())
        category.create_voice_channel = AsyncMock(return_value=MagicMock())
        return category

    everyone = MagicMock(name="@everyone")
    bot_top = MagicMock(name="bot top role")
    admin_role = MagicMock(name="admin role")
    player_a = MagicMock(name="Member A")
    player_b = MagicMock(name="Member B")

    guild = MagicMock()
    guild.default_role = everyone
    guild.me = MagicMock()
    guild.me.top_role = bot_top
    guild.create_category = AsyncMock(side_effect=fake_create_category)
    guild.get_role = MagicMock(return_value=admin_role)
    guild.get_member = MagicMock(side_effect=lambda uid: {1001: player_a, 1002: player_b}.get(uid))

    await create_match_category(
        guild=guild,
        match_number=7,
        player_ids=[1001, 1002, 9999],  # 9999 not in guild
        admin_role_ids=[42],
    )

    overwrites = captured["overwrites"]
    assert everyone in overwrites
    assert overwrites[everyone].view_channel is False
    assert overwrites[everyone].connect is False

    assert bot_top in overwrites
    assert overwrites[bot_top].view_channel is True
    assert overwrites[bot_top].manage_channels is True

    assert admin_role in overwrites
    assert overwrites[admin_role].view_channel is True
    assert overwrites[admin_role].manage_channels is True

    assert player_a in overwrites
    assert overwrites[player_a].view_channel is True
    assert overwrites[player_a].connect is True
    assert overwrites[player_a].send_messages is True

    assert player_b in overwrites
    # 9999 has no guild member — silently skipped, must not raise


@pytest.mark.asyncio
async def test_create_match_category_rolls_back_on_partial_failure():
    from services.match_category import create_match_category

    category = MagicMock()
    category.delete = AsyncMock()
    text_channel = MagicMock()
    text_channel.delete = AsyncMock()

    category.create_text_channel = AsyncMock(return_value=text_channel)
    # First VC succeeds, second VC raises -> must rollback
    vc1 = MagicMock()
    vc1.delete = AsyncMock()
    category.create_voice_channel = AsyncMock(
        side_effect=[vc1, RuntimeError("api fail")]
    )

    guild = MagicMock()
    guild.create_category = AsyncMock(return_value=category)
    guild.default_role = MagicMock()
    guild.me = MagicMock()
    guild.me.top_role = MagicMock()
    guild.get_member = MagicMock(return_value=None)
    guild.get_role = MagicMock(return_value=None)

    with pytest.raises(RuntimeError, match="api fail"):
        await create_match_category(
            guild=guild,
            match_number=99,
            player_ids=[],
            admin_role_ids=[],
        )

    text_channel.delete.assert_awaited_once()
    vc1.delete.assert_awaited_once()
    category.delete.assert_awaited_once()


@pytest.mark.asyncio
async def test_delete_match_category_deletes_children_then_category():
    from services.match_category import delete_match_category

    ch1 = MagicMock()
    ch1.delete = AsyncMock()
    ch2 = MagicMock()
    ch2.delete = AsyncMock()
    category = MagicMock(spec=discord.CategoryChannel)
    category.channels = [ch1, ch2]
    category.delete = AsyncMock()

    guild = MagicMock()
    guild.get_channel = MagicMock(return_value=category)

    await delete_match_category(guild=guild, category_id=123, reason="vote validated")

    ch1.delete.assert_awaited_once()
    ch2.delete.assert_awaited_once()
    category.delete.assert_awaited_once()


@pytest.mark.asyncio
async def test_delete_match_category_idempotent_when_already_gone():
    from services.match_category import delete_match_category

    guild = MagicMock()
    guild.get_channel = MagicMock(return_value=None)

    # Must not raise
    await delete_match_category(guild=guild, category_id=999, reason="orphan")


@pytest.mark.asyncio
async def test_delete_match_category_skips_non_category():
    from services.match_category import delete_match_category

    # If get_channel returns something that is not a CategoryChannel
    not_a_category = MagicMock(spec=discord.TextChannel)
    guild = MagicMock()
    guild.get_channel = MagicMock(return_value=not_a_category)

    await delete_match_category(guild=guild, category_id=12, reason="x")

    # Must not invoke any delete on the wrong object
    assert not getattr(not_a_category, "delete", MagicMock()).called
