"""Tests du cog moderation (/warn et /warn-list)."""

from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

import bot as bot_module
from cogs.moderation import (
    WARN_LIST_PAGE_SIZE,
    WARN_ROLE_NAMES,
    ModerationCog,
    _has_warn_access,
    _truncate,
)
from services import repository


# ── Helpers ────────────────────────────────────────────────────────


def _role(name: str) -> MagicMock:
    r = MagicMock()
    r.name = name
    return r


def _member(
    member_id: int = 1,
    name: str = "User",
    roles: list[MagicMock] | None = None,
    *,
    manage_guild: bool = False,
    is_bot: bool = False,
) -> MagicMock:
    m = MagicMock(spec=discord.Member)
    m.id = member_id
    m.display_name = name
    m.mention = f"<@{member_id}>"
    m.bot = is_bot
    m.roles = roles or []
    perms = MagicMock()
    perms.manage_guild = manage_guild
    m.guild_permissions = perms
    m.send = AsyncMock()
    return m


def _interaction(user: MagicMock, guild_id: int = 99) -> MagicMock:
    inter = MagicMock()
    inter.user = user
    inter.guild_id = guild_id
    guild = MagicMock()
    guild.name = "TestGuild"
    guild.id = guild_id
    inter.guild = guild
    inter.response = MagicMock()
    inter.response.send_message = AsyncMock()
    return inter


# ── _has_warn_access ───────────────────────────────────────────────


def test_has_warn_access_manage_guild_perm():
    user = _member(manage_guild=True)
    assert _has_warn_access(user) is True


def test_has_warn_access_warn_role():
    user = _member(roles=[_role("Head Administrators")])
    assert _has_warn_access(user) is True


def test_has_warn_access_any_warn_role():
    for role_name in WARN_ROLE_NAMES:
        user = _member(roles=[_role(role_name)])
        assert _has_warn_access(user) is True, f"role {role_name!r} doit etre accepte"


def test_has_warn_access_refused_without_perm_or_role():
    user = _member(roles=[_role("Random Role")])
    assert _has_warn_access(user) is False


# ── _truncate ──────────────────────────────────────────────────────


def test_truncate_no_change_when_under_limit():
    assert _truncate("short", 20) == "short"


def test_truncate_cuts_with_ellipsis():
    out = _truncate("a" * 50, 10)
    assert len(out) == 10
    assert out.endswith("…")


# ── /warn ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_warn_refused_outside_guild():
    """interaction.user n'est pas un Member -> refus."""
    cog = ModerationCog(bot_module.bot, bot_module.db)
    user = MagicMock()  # pas un Member
    inter = _interaction(user)
    target = _member(member_id=2, name="Target")

    await cog.warn.callback(cog, inter, target, "spam")

    inter.response.send_message.assert_awaited_once()
    msg = inter.response.send_message.await_args.args[0]
    assert "uniquement dans un serveur" in msg


@pytest.mark.asyncio
async def test_warn_refused_without_permission():
    cog = ModerationCog(bot_module.bot, bot_module.db)
    user = _member(member_id=1, name="Mod", roles=[_role("Random Role")])
    inter = _interaction(user)
    target = _member(member_id=2, name="Target")

    await cog.warn.callback(cog, inter, target, "spam")

    msg = inter.response.send_message.await_args.args[0]
    assert "pas la permission" in msg


@pytest.mark.asyncio
async def test_warn_refused_on_bot_target():
    cog = ModerationCog(bot_module.bot, bot_module.db)
    user = _member(manage_guild=True)
    inter = _interaction(user)
    target = _member(member_id=2, name="Bot", is_bot=True)

    await cog.warn.callback(cog, inter, target, "spam")

    msg = inter.response.send_message.await_args.args[0]
    assert "warn un bot" in msg


@pytest.mark.asyncio
async def test_warn_refused_on_self():
    cog = ModerationCog(bot_module.bot, bot_module.db)
    user = _member(member_id=1, name="Mod", manage_guild=True)
    inter = _interaction(user)

    await cog.warn.callback(cog, inter, user, "spam")

    msg = inter.response.send_message.await_args.args[0]
    assert "te warn toi-meme" in msg


@pytest.mark.asyncio
async def test_warn_happy_path_persists_and_dms():
    cog = ModerationCog(bot_module.bot, bot_module.db)
    user = _member(member_id=1, name="Mod", manage_guild=True)
    inter = _interaction(user, guild_id=99)
    target = _member(member_id=2, name="Target")

    await cog.warn.callback(cog, inter, target, "spam excessif")

    # DM envoye
    target.send.assert_awaited_once()
    # Persistance
    warns = repository.list_warns(bot_module.db, 99)
    assert len(warns) == 1
    assert warns[0]["member_id"] == 2
    assert warns[0]["reason"] == "spam excessif"
    # Reponse "succes"
    msg = inter.response.send_message.await_args.args[0]
    assert "✅" in msg


@pytest.mark.asyncio
async def test_warn_forbidden_dm_still_persists():
    """DM fermes -> warn quand meme persiste + message d'avertissement."""
    cog = ModerationCog(bot_module.bot, bot_module.db)
    user = _member(member_id=1, name="Mod", manage_guild=True)
    inter = _interaction(user, guild_id=99)
    target = _member(member_id=2, name="Target")
    target.send.side_effect = discord.Forbidden(MagicMock(status=403), "DMs closed")

    await cog.warn.callback(cog, inter, target, "raison")

    warns = repository.list_warns(bot_module.db, 99)
    assert len(warns) == 1
    msg = inter.response.send_message.await_args.args[0]
    assert "DM impossible" in msg or "DM fermes" in msg


@pytest.mark.asyncio
async def test_warn_http_exception_dm_still_persists():
    """HTTPException transitoire sur DM -> persistance preservee (regression test)."""
    cog = ModerationCog(bot_module.bot, bot_module.db)
    user = _member(member_id=1, name="Mod", manage_guild=True)
    inter = _interaction(user, guild_id=99)
    target = _member(member_id=2, name="Target")
    target.send.side_effect = discord.HTTPException(MagicMock(status=500), "transient")

    await cog.warn.callback(cog, inter, target, "raison")

    warns = repository.list_warns(bot_module.db, 99)
    assert len(warns) == 1, "Un blip reseau sur le DM ne doit PAS faire perdre le warn"


# ── /warn-list ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_warn_list_refused_outside_guild():
    cog = ModerationCog(bot_module.bot, bot_module.db)
    inter = _interaction(MagicMock())  # user non-Member
    await cog.warn_list.callback(cog, inter, None)
    msg = inter.response.send_message.await_args.args[0]
    assert "uniquement dans un serveur" in msg


@pytest.mark.asyncio
async def test_warn_list_refused_without_permission():
    cog = ModerationCog(bot_module.bot, bot_module.db)
    user = _member(roles=[_role("Random")])
    inter = _interaction(user)
    await cog.warn_list.callback(cog, inter, None)
    msg = inter.response.send_message.await_args.args[0]
    assert "pas la permission" in msg


@pytest.mark.asyncio
async def test_warn_list_empty_no_filter():
    cog = ModerationCog(bot_module.bot, bot_module.db)
    user = _member(manage_guild=True)
    inter = _interaction(user, guild_id=42)
    await cog.warn_list.callback(cog, inter, None)
    kwargs = inter.response.send_message.await_args.kwargs
    embed = kwargs["embed"]
    assert "Aucun warn" in embed.description


@pytest.mark.asyncio
async def test_warn_list_filters_by_member():
    cog = ModerationCog(bot_module.bot, bot_module.db)
    user = _member(member_id=1, manage_guild=True)
    # Pre-insere 2 warns sur 2 membres differents
    repository.add_warn(
        bot_module.db,
        42,
        member_id=10,
        member_name="A",
        moderator_id=1,
        moderator_name="Mod",
        reason="reason A",
    )
    repository.add_warn(
        bot_module.db,
        42,
        member_id=20,
        member_name="B",
        moderator_id=1,
        moderator_name="Mod",
        reason="reason B",
    )
    target = _member(member_id=10, name="A")
    inter = _interaction(user, guild_id=42)
    await cog.warn_list.callback(cog, inter, target)
    kwargs = inter.response.send_message.await_args.kwargs
    embed = kwargs["embed"]
    # Un seul field (warn de member 10 uniquement)
    assert len(embed.fields) == 1
    assert "<@10>" in embed.fields[0].name


@pytest.mark.asyncio
async def test_warn_list_page_size_caps_at_constant():
    cog = ModerationCog(bot_module.bot, bot_module.db)
    user = _member(manage_guild=True)
    # Pre-insere plus de WARN_LIST_PAGE_SIZE warns
    for i in range(WARN_LIST_PAGE_SIZE + 5):
        repository.add_warn(
            bot_module.db,
            42,
            member_id=100 + i,
            member_name=f"P{i}",
            moderator_id=1,
            moderator_name="Mod",
            reason=f"r{i}",
        )
    inter = _interaction(user, guild_id=42)
    await cog.warn_list.callback(cog, inter, None)
    kwargs = inter.response.send_message.await_args.kwargs
    embed = kwargs["embed"]
    assert len(embed.fields) == WARN_LIST_PAGE_SIZE
