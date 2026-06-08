"""Tests for the open-role cog (/open button that grants FL OPEN)."""

from unittest.mock import AsyncMock, MagicMock

import discord

from cogs.open_role import (
    OPEN_ROLE_NAME,
    OpenRoleCog,
    OpenRoleView,
    build_open_embed,
)


def _fake_member(role_names: tuple[str, ...] = ()):
    member = MagicMock(spec=discord.Member)
    member.roles = [MagicMock(name=name) for name in ()]
    # MagicMock(name=...) sets the mock's repr, not a `.name` attribute,
    # so build role mocks explicitly.
    member.roles = []
    for rn in role_names:
        r = MagicMock()
        r.name = rn
        member.roles.append(r)
    member.add_roles = AsyncMock()
    return member


def _fake_interaction(member, role_in_guild: bool = True):
    inter = MagicMock()
    inter.user = member
    inter.guild = MagicMock()
    inter.guild_id = 123
    if role_in_guild:
        role = MagicMock()
        role.name = OPEN_ROLE_NAME
        inter.guild.roles = [role]
    else:
        inter.guild.roles = []
    inter.response = MagicMock()
    inter.response.send_message = AsyncMock()
    return inter


def test_build_open_embed_mentions_role():
    embed = build_open_embed()
    text = (embed.description or "") + (embed.footer.text or "")
    assert OPEN_ROLE_NAME in text or "FL OPEN" in text


async def test_grant_button_custom_id_is_fixed():
    view = OpenRoleView()
    assert view.grant_btn.custom_id == "open:grant"


async def test_button_grants_role_when_missing():
    member = _fake_member(role_names=())
    inter = _fake_interaction(member, role_in_guild=True)

    await OpenRoleView()._grant_callback(inter)

    member.add_roles.assert_awaited_once()
    inter.response.send_message.assert_awaited_once()
    assert inter.response.send_message.call_args.kwargs.get("ephemeral") is True


async def test_button_idempotent_when_already_has_role():
    member = _fake_member(role_names=(OPEN_ROLE_NAME,))
    inter = _fake_interaction(member, role_in_guild=True)

    await OpenRoleView()._grant_callback(inter)

    member.add_roles.assert_not_awaited()
    inter.response.send_message.assert_awaited_once()
    assert inter.response.send_message.call_args.kwargs.get("ephemeral") is True


async def test_button_errors_when_role_missing_from_guild():
    member = _fake_member(role_names=())
    inter = _fake_interaction(member, role_in_guild=False)

    await OpenRoleView()._grant_callback(inter)

    member.add_roles.assert_not_awaited()
    inter.response.send_message.assert_awaited_once()
    msg = inter.response.send_message.call_args.args[0]
    assert "missing" in msg.lower()


async def test_open_command_posts_in_current_channel():
    cog = OpenRoleCog(MagicMock())
    inter = MagicMock()
    inter.channel = MagicMock()
    inter.channel.mention = "#open"
    inter.channel.send = AsyncMock()
    inter.response = MagicMock()
    inter.response.send_message = AsyncMock()

    await cog.open_cmd.callback(cog, inter)

    inter.channel.send.assert_awaited_once()
    kwargs = inter.channel.send.call_args.kwargs
    assert isinstance(kwargs["embed"], discord.Embed)
    assert kwargs["view"] is cog.open_view
    inter.response.send_message.assert_awaited_once()
    assert inter.response.send_message.call_args.kwargs.get("ephemeral") is True


async def test_open_command_handles_none_channel():
    cog = OpenRoleCog(MagicMock())
    inter = MagicMock()
    inter.channel = None
    inter.response = MagicMock()
    inter.response.send_message = AsyncMock()

    await cog.open_cmd.callback(cog, inter)

    inter.response.send_message.assert_awaited_once()
    assert inter.response.send_message.call_args.kwargs.get("ephemeral") is True
