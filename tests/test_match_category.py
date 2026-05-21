"""Tests for services/match_category.py — dynamic match category lifecycle."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

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
