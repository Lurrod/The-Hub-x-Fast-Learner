"""Tests for MapBanSession (Discord orchestration), Discord mocked."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from services.map_pick_ban import (
    BAN_SEQUENCE,
    MapBanCancelledError,
    MapBanResult,
    MapBanSession,
)
from services.team_balancer import Player

pytestmark = pytest.mark.integration


MAPS_7 = ("Breeze", "Ascent", "Lotus", "Fracture", "Split", "Haven", "Pearl")
ADMIN_ROLES = ("ADMINISTRATORS",)


def _p(uid: int) -> Player:
    return Player(id=uid, name=f"P{uid}", elo=2000)


def _mock_interaction(
    *,
    user_id: int,
    custom_id: str,
    picked_map: str | None = None,
    is_admin: bool = False,
):
    interaction = MagicMock()
    interaction.user = SimpleNamespace(
        id=user_id,
        guild_permissions=SimpleNamespace(manage_guild=is_admin),
        roles=[SimpleNamespace(name="ADMINISTRATORS")] if is_admin else [],
    )
    interaction.data = {"custom_id": custom_id}
    if picked_map is not None:
        interaction.data["values"] = [picked_map]
    interaction.response = MagicMock()
    interaction.response.send_message = AsyncMock()
    interaction.response.edit_message = AsyncMock()
    interaction.response.defer = AsyncMock()
    return interaction


@pytest.mark.asyncio
async def test_six_bans_resolve_to_pearl_remaining():
    channel = MagicMock()
    channel.send = AsyncMock(return_value=MagicMock(edit=AsyncMock()))
    cap_a, cap_b = _p(1), _p(2)
    session = MapBanSession(
        prep_channel=channel,
        cap_a=cap_a,
        cap_b=cap_b,
        maps=MAPS_7,
        admin_role_names=ADMIN_ROLES,
    )
    run_task = asyncio.create_task(session.run())
    await asyncio.sleep(0)  # let session.run() post the initial message

    bans = [
        (1, "Breeze"),
        (2, "Ascent"),
        (1, "Lotus"),
        (2, "Fracture"),
        (1, "Split"),
        (2, "Haven"),
    ]
    for uid, m in bans:
        inter = _mock_interaction(user_id=uid, picked_map=m, custom_id="map_ban_pick")
        await session._on_ban(inter)

    result = await asyncio.wait_for(run_task, timeout=1.0)
    assert isinstance(result, MapBanResult)
    assert result.selected_map == "Pearl"
    assert len(result.ban_history) == 6


@pytest.mark.asyncio
async def test_non_current_captain_cannot_ban():
    channel = MagicMock()
    channel.send = AsyncMock(return_value=MagicMock(edit=AsyncMock()))
    session = MapBanSession(
        prep_channel=channel,
        cap_a=_p(1),
        cap_b=_p(2),
        maps=MAPS_7,
        admin_role_names=ADMIN_ROLES,
    )
    run_task = asyncio.create_task(session.run())
    await asyncio.sleep(0)

    # cap_b on turn 0 (A's turn)
    inter = _mock_interaction(user_id=2, picked_map="Breeze", custom_id="map_ban_pick")
    allowed = await session._interaction_check(inter)
    assert allowed is False
    inter.response.send_message.assert_awaited_once()
    _, kwargs = inter.response.send_message.call_args
    assert kwargs.get("ephemeral") is True
    run_task.cancel()
    await asyncio.gather(run_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_admin_cancel_raises_map_ban_cancelled_error():
    channel = MagicMock()
    channel.send = AsyncMock(return_value=MagicMock(edit=AsyncMock()))
    session = MapBanSession(
        prep_channel=channel,
        cap_a=_p(1),
        cap_b=_p(2),
        maps=MAPS_7,
        admin_role_names=ADMIN_ROLES,
    )
    run_task = asyncio.create_task(session.run())
    await asyncio.sleep(0)

    admin_inter = _mock_interaction(user_id=99, custom_id="map_ban_cancel", is_admin=True)
    await session._on_cancel(admin_inter)

    with pytest.raises(MapBanCancelledError):
        await asyncio.wait_for(run_task, timeout=1.0)


@pytest.mark.asyncio
async def test_non_admin_cannot_cancel():
    channel = MagicMock()
    channel.send = AsyncMock(return_value=MagicMock(edit=AsyncMock()))
    session = MapBanSession(
        prep_channel=channel,
        cap_a=_p(1),
        cap_b=_p(2),
        maps=MAPS_7,
        admin_role_names=ADMIN_ROLES,
    )
    run_task = asyncio.create_task(session.run())
    await asyncio.sleep(0)

    non_admin = _mock_interaction(user_id=42, custom_id="map_ban_cancel", is_admin=False)
    allowed = await session._interaction_check(non_admin)
    assert allowed is False
    non_admin.response.send_message.assert_awaited_once()
    run_task.cancel()
    await asyncio.gather(run_task, return_exceptions=True)


def test_ban_sequence_constant_for_external_consumers():
    assert BAN_SEQUENCE == ("A", "B", "A", "B", "A", "B")
