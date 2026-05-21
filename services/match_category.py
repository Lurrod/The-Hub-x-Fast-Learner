"""Lifecycle of dynamic match categories (create, delete, cleanup).

This module owns *all* operations on the per-match Discord category that
holds the match-preparation text channel and team voice channels. It
replaces the legacy 5-slot static system that relied on permanent
``Match #1``..``Match #5`` categories and roles.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Iterable

import discord

logger = logging.getLogger(__name__)

MATCH_CATEGORY_PATTERN = re.compile(r"^Match #\d+$")


@dataclass(frozen=True)
class MatchChannels:
    """Concrete handles for the 4 Discord channels backing a match."""

    category: discord.CategoryChannel
    prep_channel: discord.TextChannel
    team1_vc: discord.VoiceChannel
    team2_vc: discord.VoiceChannel
    waiting_match_vc: discord.VoiceChannel


async def create_match_category(
    *,
    guild: discord.Guild,
    match_number: int,
    player_ids: Iterable[int],
    admin_role_ids: Iterable[int],
) -> MatchChannels:
    """Create a 'Match #N' category with 4 channels and proper overwrites.

    Overwrites are posted *on the category only*; child channels inherit
    via Discord's sync mechanism.
    """
    overwrites = _build_overwrites(
        guild=guild,
        player_ids=list(player_ids),
        admin_role_ids=list(admin_role_ids),
    )
    reason = f"Match #{match_number} created"
    category = await guild.create_category(
        f"Match #{match_number}", overwrites=overwrites, reason=reason
    )
    prep = await category.create_text_channel("match-preparation", reason=reason)
    team1 = await category.create_voice_channel("Team 1", reason=reason)
    team2 = await category.create_voice_channel("Team 2", reason=reason)
    waiting = await category.create_voice_channel("Waiting Match", reason=reason)
    return MatchChannels(
        category=category,
        prep_channel=prep,
        team1_vc=team1,
        team2_vc=team2,
        waiting_match_vc=waiting,
    )


def _build_overwrites(
    *,
    guild: discord.Guild,
    player_ids: list[int],
    admin_role_ids: list[int],
) -> dict:
    """Stub — implemented in Task 3."""
    return {}
