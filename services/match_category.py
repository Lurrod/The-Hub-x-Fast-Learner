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
    created: list = []
    try:
        prep = await category.create_text_channel("match-preparation", reason=reason)
        created.append(prep)
        team1 = await category.create_voice_channel("Team 1", reason=reason)
        created.append(team1)
        team2 = await category.create_voice_channel("Team 2", reason=reason)
        created.append(team2)
        waiting = await category.create_voice_channel("Waiting Match", reason=reason)
        created.append(waiting)
    except Exception:
        logger.exception(
            "[match_category] partial creation failed for Match #%d, rolling back",
            match_number,
        )
        for ch in created:
            try:
                await ch.delete(reason="rollback partial match category creation")
            except Exception:  # noqa: BLE001 - best effort cleanup
                logger.exception("[match_category] rollback delete child failed")
        try:
            await category.delete(reason="rollback partial match category creation")
        except Exception:  # noqa: BLE001
            logger.exception("[match_category] rollback delete category failed")
        raise
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
    """Build the permission overwrite matrix for a match category.

    - @everyone: deny view + connect (private category)
    - Bot's top role: full privileged access + manage_channels
    - Each admin role: full privileged access + manage_channels
    - Each player (by member ID): view, send, connect, speak
    - Members not found in the guild are silently skipped.
    """
    everyone_ow = discord.PermissionOverwrite(view_channel=False, connect=False)
    privileged_ow = discord.PermissionOverwrite(
        view_channel=True,
        send_messages=True,
        read_message_history=True,
        connect=True,
        speak=True,
        manage_channels=True,
    )
    player_ow = discord.PermissionOverwrite(
        view_channel=True,
        send_messages=True,
        read_message_history=True,
        connect=True,
        speak=True,
    )

    overwrites: dict = {
        guild.default_role: everyone_ow,
        guild.me.top_role: privileged_ow,
    }

    for role_id in admin_role_ids:
        role = guild.get_role(role_id)
        if role is not None:
            overwrites[role] = privileged_ow

    for uid in player_ids:
        member = guild.get_member(uid)
        if member is not None:
            overwrites[member] = player_ow

    return overwrites


async def delete_match_category(
    *,
    guild: discord.Guild,
    category_id: int,
    reason: str,
) -> None:
    """Delete the category and all its children. No-op if already gone."""
    channel = guild.get_channel(category_id)
    if channel is None:
        return
    if not isinstance(channel, discord.CategoryChannel):
        logger.warning(
            "[match_category] delete: id=%d is not a CategoryChannel (got %s)",
            category_id,
            type(channel).__name__,
        )
        return
    for child in list(channel.channels):
        try:
            await child.delete(reason=reason)
        except discord.NotFound:
            pass
        except Exception:  # noqa: BLE001
            logger.exception("[match_category] failed to delete child %s", child)
    try:
        await channel.delete(reason=reason)
    except discord.NotFound:
        pass
    except Exception:  # noqa: BLE001
        logger.exception("[match_category] failed to delete category %d", category_id)
