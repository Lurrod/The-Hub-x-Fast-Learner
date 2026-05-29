"""
Integration tests for the cogs/applications.py cog.

Covers:
  - _parse_application_embed: parses the ID + nickname + staff flag from the embed
  - _try_acquire_candidature_cooldown: atomic CAS 1h cooldown
  - ApplicationReviewView.accept: happy path + edge cases (no perm,
    corrupted embed, missing member, double-claim via CAS)
  - RefuseReasonModal.on_submit: graceful skip when member is missing
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import discord
import mongomock

from cogs.applications import (
    ApplicationReviewView,
    RankModal,
    RefuseReasonModal,
    ReportModal,
    TicketPanelView,
    _open_ticket_channel,
    _parse_application_embed,
    _try_acquire_candidature_cooldown,
)


# ── _parse_application_embed ─────────────────────────────────────
def _embed_with(
    *,
    title: str = "📋 New application",
    footer_id: int | str | None = 42,
    fields: list[tuple[str, str]] | None = None,
) -> MagicMock:
    embed = MagicMock()
    embed.title = title
    footer = MagicMock()
    footer.text = f"ID: {footer_id}" if footer_id is not None else None
    embed.footer = footer
    embed.fields = []
    if fields:
        for name, value in fields:
            f = MagicMock()
            f.name = name
            f.value = value
            embed.fields.append(f)
    return embed


def _message_with_embeds(embeds: list) -> MagicMock:
    msg = MagicMock()
    msg.embeds = embeds
    return msg


def test_parse_embed_returns_id_pseudo_player():
    embed = _embed_with(
        title="📋 New application",
        footer_id=42,
        fields=[("🎮 In-game username", "Alice")],
    )
    msg = _message_with_embeds([embed])
    applicant_id, pseudo, is_staff = _parse_application_embed(msg)
    assert applicant_id == 42
    assert pseudo == "Alice"
    assert is_staff is False


def test_parse_embed_detects_staff_in_title():
    embed = _embed_with(
        title="📋 New Staff application",
        footer_id=99,
        fields=[("🎮 Username", "Bob")],
    )
    msg = _message_with_embeds([embed])
    _, _, is_staff = _parse_application_embed(msg)
    assert is_staff is True


def test_parse_embed_returns_none_when_no_embeds():
    msg = _message_with_embeds([])
    applicant_id, pseudo, is_staff = _parse_application_embed(msg)
    assert applicant_id is None
    assert pseudo == ""
    assert is_staff is False


def test_parse_embed_returns_none_on_invalid_footer():
    embed = _embed_with(
        title="📋 New application",
        footer_id=None,
        fields=[("🎮 In-game username", "Alice")],
    )
    msg = _message_with_embeds([embed])
    applicant_id, _, _ = _parse_application_embed(msg)
    assert applicant_id is None


def test_parse_embed_returns_none_on_non_numeric_footer():
    embed = _embed_with(title="X", footer_id="abc", fields=[("🎮 Nickname", "A")])
    msg = _message_with_embeds([embed])
    applicant_id, _, _ = _parse_application_embed(msg)
    assert applicant_id is None


# ── _try_acquire_candidature_cooldown ─────────────────────────────
def test_cooldown_first_apply_returns_allowed():
    db = mongomock.MongoClient(tz_aware=True).db
    allowed, remaining = _try_acquire_candidature_cooldown(db, "user-1")
    assert allowed is True
    assert remaining == 0.0
    # Doc created
    doc = db["candidature_cooldowns"].find_one({"_id": "user-1"})
    assert doc is not None


def test_cooldown_within_window_returns_blocked():
    db = mongomock.MongoClient(tz_aware=True).db
    # Pre-insert: 30 minutes ago
    recent = datetime.now(UTC) - timedelta(minutes=30)
    db["candidature_cooldowns"].insert_one({"_id": "user-1", "last_apply": recent})
    allowed, remaining = _try_acquire_candidature_cooldown(db, "user-1")
    assert allowed is False
    assert remaining > 0
    # Roughly 30 minutes remaining
    assert 1700 < remaining < 1850


def test_cooldown_after_window_returns_allowed():
    db = mongomock.MongoClient(tz_aware=True).db
    # 2 hours ago (past the 60-minute window)
    old = datetime.now(UTC) - timedelta(hours=2)
    db["candidature_cooldowns"].insert_one({"_id": "user-1", "last_apply": old})
    allowed, remaining = _try_acquire_candidature_cooldown(db, "user-1")
    assert allowed is True
    assert remaining == 0.0
    # Doc updated
    doc = db["candidature_cooldowns"].find_one({"_id": "user-1"})
    assert doc["last_apply"] > old


# ── ApplicationReviewView.accept ──────────────────────────────────
def _fake_member(member_id: int, name: str = "Alice", *, manage_guild: bool = True) -> MagicMock:
    m = MagicMock()
    m.id = member_id
    m.display_name = name
    m.mention = f"<@{member_id}>"
    m.guild_permissions.manage_guild = manage_guild
    m.roles = []
    avatar = MagicMock()
    avatar.url = "https://cdn.test/avatar.png"
    m.display_avatar = avatar
    m.send = AsyncMock()
    m.edit = AsyncMock()
    m.add_roles = AsyncMock()
    return m


def _fake_interaction(user, guild, message) -> MagicMock:
    inter = MagicMock()
    inter.user = user
    inter.guild = guild
    inter.guild_id = guild.id
    inter.message = message
    inter.response = MagicMock()
    inter.response.send_message = AsyncMock()
    inter.response.defer = AsyncMock()
    inter.response.is_done = MagicMock(return_value=False)
    inter.followup = MagicMock()
    inter.followup.send = AsyncMock()
    return inter


def _fake_guild(guild_id: int, members: list[MagicMock] | None = None) -> MagicMock:
    g = MagicMock()
    g.id = guild_id
    g.name = "TestGuild"
    g.members = members or []
    g.get_member = lambda mid: next((m for m in g.members if m.id == mid), None)
    g.roles = []
    return g


async def test_accept_happy_path_grants_role_and_validates():
    from services import repository

    db = mongomock.MongoClient(tz_aware=True).db
    admin = _fake_member(1, "Admin", manage_guild=True)
    applicant = _fake_member(42, "Alice", manage_guild=False)
    guild = _fake_guild(99, members=[admin, applicant])

    # Role "Members"
    members_role = MagicMock()
    members_role.name = "Members"
    guild.roles = [members_role]

    embed = _embed_with(
        title="📋 Nouvelle candidature",
        footer_id=42,
        fields=[("🎮 In-game username", "Alice")],
    )
    message = MagicMock()
    message.id = 1000
    message.embeds = [embed]
    message.edit = AsyncMock()

    inter = _fake_interaction(admin, guild, message)
    # Mandatory pre-registration: claim_application_decision is a CAS
    # on status=pending that requires an existing doc.
    repository.register_application(db, guild.id, message.id, applicant.id, is_staff=False)

    view = ApplicationReviewView(db=db)
    await view.accept.callback(inter)

    # CAS DB: status moves to "accepted"
    app = repository.get_applications_col(db, guild.id).find_one({"_id": str(message.id)})
    assert app is not None and app.get("status") == "accepted"
    # Role grant + DM were attempted
    applicant.add_roles.assert_awaited()
    inter.followup.send.assert_awaited()


async def test_accept_refuses_when_no_permission():
    db = mongomock.MongoClient(tz_aware=True).db
    non_admin = _fake_member(1, "User", manage_guild=False)
    applicant = _fake_member(42, "Alice", manage_guild=False)
    guild = _fake_guild(99, members=[non_admin, applicant])

    embed = _embed_with(footer_id=42, fields=[("🎮 In-game username", "Alice")])
    message = MagicMock()
    message.id = 1000
    message.embeds = [embed]
    inter = _fake_interaction(non_admin, guild, message)

    view = ApplicationReviewView(db=db)
    await view.accept.callback(inter)

    inter.response.send_message.assert_awaited_once()
    args, kwargs = inter.response.send_message.call_args
    assert "permission" in args[0].lower()
    # No side effect: no role grant.
    applicant.add_roles.assert_not_awaited()


async def test_accept_bails_on_corrupted_embed_without_cas():
    """Critical audit bug: the CAS must run after validation to avoid the
    stuck state. Verify that an embed without applicant_id does NOT
    consume the DB CAS."""
    from services import repository

    db = mongomock.MongoClient(tz_aware=True).db
    admin = _fake_member(1, "Admin", manage_guild=True)
    guild = _fake_guild(99, members=[admin])

    # Embed without footer ID -> applicant_id = None
    embed = _embed_with(footer_id=None, fields=[])
    message = MagicMock()
    message.id = 1000
    message.embeds = [embed]
    inter = _fake_interaction(admin, guild, message)

    view = ApplicationReviewView(db=db)
    await view.accept.callback(inter)

    # The followup must say "unreadable" or "corrupted embed"
    inter.followup.send.assert_awaited()
    msg = inter.followup.send.call_args.args[0]
    assert "unreadable" in msg.lower() or "corrupted" in msg.lower()

    # CRITICAL: the application must NOT be marked accepted in DB
    # (otherwise the candidate stays stuck).
    apps_col = repository.get_applications_col(db, guild.id)
    app_doc = apps_col.find_one({"_id": str(message.id)})
    assert app_doc is None, (
        "Audit bug: CAS executed while validation failed. "
        "The candidate is now stuck in 'already processed' state."
    )


async def test_accept_bails_on_missing_member_without_cas():
    """Same principle: if get_member returns None, no CAS consumed."""
    from services import repository

    db = mongomock.MongoClient(tz_aware=True).db
    admin = _fake_member(1, "Admin", manage_guild=True)
    # No member 42 in the guild -> applicant missing
    guild = _fake_guild(99, members=[admin])

    embed = _embed_with(footer_id=42, fields=[("🎮 In-game username", "Alice")])
    message = MagicMock()
    message.id = 1000
    message.embeds = [embed]
    inter = _fake_interaction(admin, guild, message)

    view = ApplicationReviewView(db=db)
    await view.accept.callback(inter)

    # Followup: Member not found
    inter.followup.send.assert_awaited()
    msg = inter.followup.send.call_args.args[0]
    assert "not found" in msg.lower()

    # CAS NOT consumed -> retry possible
    apps_col = repository.get_applications_col(db, guild.id)
    assert apps_col.find_one({"_id": str(message.id)}) is None


async def test_accept_rejects_double_claim_via_cas():
    """Two admins click concurrently: only one wins the CAS."""
    from services import repository

    db = mongomock.MongoClient(tz_aware=True).db
    admin1 = _fake_member(1, "Admin1", manage_guild=True)
    admin2 = _fake_member(2, "Admin2", manage_guild=True)
    applicant = _fake_member(42, "Alice", manage_guild=False)
    guild = _fake_guild(99, members=[admin1, admin2, applicant])

    members_role = MagicMock()
    members_role.name = "Members"
    guild.roles = [members_role]

    embed = _embed_with(footer_id=42, fields=[("🎮 In-game username", "Alice")])
    message = MagicMock()
    message.id = 1000
    message.embeds = [embed]
    message.edit = AsyncMock()

    # Pre-claim by admin2: the application is already "refused" in DB.
    repository.register_application(db, guild.id, message.id, applicant.id, is_staff=False)
    claimed = repository.claim_application_decision(
        db,
        guild.id,
        message.id,
        status="refused",
        decided_by=admin2.id,
    )
    assert claimed is not None

    # Admin1 attempts to accept second -> must fail cleanly
    inter = _fake_interaction(admin1, guild, message)
    view = ApplicationReviewView(db=db)
    await view.accept.callback(inter)

    # The followup must say "already handled"
    inter.followup.send.assert_awaited()
    msg = inter.followup.send.call_args.args[0]
    assert "already" in msg.lower()
    # No role grant on the applicant
    applicant.add_roles.assert_not_awaited()


# -- RefuseReasonModal: member None graceful skip --
async def test_refuse_modal_skips_dm_kick_when_member_gone():
    """If the candidate left the server between the 'Decline' click and
    the modal submission, the DM/kick are gracefully skipped and the
    embed is still updated (DB state + message consistent)."""
    from services import repository

    db = mongomock.MongoClient(tz_aware=True).db
    admin = _fake_member(1, "Admin", manage_guild=True)
    # No candidate 42 in the guild
    guild = _fake_guild(99, members=[admin])

    embed = _embed_with(footer_id=42, fields=[("🎮 In-game username", "Alice")])
    message = MagicMock()
    message.id = 1000
    message.embeds = [embed]
    message.edit = AsyncMock()

    inter = _fake_interaction(admin, guild, message)
    # Pre-register the application (otherwise claim returns None)
    repository.register_application(db, guild.id, message.id, 42, is_staff=False)

    modal = RefuseReasonModal(db=db, applicant_id=42)
    modal.reason = MagicMock()
    modal.reason.value = "Not convinced"

    await modal.on_submit(inter)

    # CAS consumed - application marked refused
    apps_col = repository.get_applications_col(db, guild.id)
    app = apps_col.find_one({"_id": str(message.id)})
    assert app is not None
    assert app.get("status") == "refused"

    # Embed update attempted even without a member
    message.edit.assert_awaited()
    # Followup shows success
    inter.followup.send.assert_awaited()


# -- Tickets: Reports / Ranks panel --
def _forbidden() -> discord.Forbidden:
    resp = MagicMock()
    resp.status = 403
    resp.reason = "Forbidden"
    return discord.Forbidden(resp, "missing permissions")


def _ticket_interaction(guild, user: MagicMock | None = None) -> MagicMock:
    inter = MagicMock()
    inter.guild = guild
    inter.user = user or _fake_member(7, "Candidate", manage_guild=False)
    inter.response = MagicMock()
    inter.response.defer = AsyncMock()
    inter.response.send_modal = AsyncMock()
    inter.followup = MagicMock()
    inter.followup.send = AsyncMock()
    return inter


def _ticket_guild(
    guild_id: int = 99,
    *,
    has_category: bool = True,
    channel_name: str = "ticket-1",
    create_category_error: Exception | None = None,
    create_channel_error: Exception | None = None,
) -> tuple[MagicMock, MagicMock]:
    """Build a mock guild + the channel it returns (for asserts)."""
    channel = MagicMock()
    channel.name = channel_name
    channel.mention = f"<#{guild_id}>"
    channel.send = AsyncMock()

    guild = MagicMock()
    guild.id = guild_id

    if has_category:
        category = MagicMock()
        category.name = "Tickets"
        guild.categories = [category]
        guild.create_category = AsyncMock()
    else:
        guild.categories = []
        new_cat = MagicMock()
        new_cat.name = "Tickets"
        guild.create_category = (
            AsyncMock(side_effect=create_category_error)
            if create_category_error
            else AsyncMock(return_value=new_cat)
        )

    guild.create_text_channel = (
        AsyncMock(side_effect=create_channel_error)
        if create_channel_error
        else AsyncMock(return_value=channel)
    )
    return guild, channel


# ── _open_ticket_channel ─────────────────────────────────────────
async def test_open_ticket_channel_uses_existing_category_and_increments():
    db = mongomock.MongoClient(tz_aware=True).db
    guild, channel = _ticket_guild()
    inter = _ticket_interaction(guild)

    result = await _open_ticket_channel(inter, db)

    assert result is channel
    guild.create_category.assert_not_awaited()
    assert guild.create_text_channel.call_args.args[0] == "ticket-1"
    # Shared counter incremented in DB (scoped by guild id, str)
    doc = db["ticket_counters"].find_one({"_id": "99"})
    assert doc["counter"] == 1


async def test_open_ticket_channel_creates_category_when_missing():
    db = mongomock.MongoClient(tz_aware=True).db
    guild, channel = _ticket_guild(has_category=False)
    inter = _ticket_interaction(guild)

    result = await _open_ticket_channel(inter, db)

    assert result is channel
    guild.create_category.assert_awaited_once()
    # The channel is created in the newly created category
    created_cat = guild.create_category.return_value
    assert guild.create_text_channel.call_args.kwargs["category"] is created_cat


async def test_open_ticket_channel_returns_none_without_guild():
    db = mongomock.MongoClient(tz_aware=True).db
    inter = _ticket_interaction(guild=None)

    result = await _open_ticket_channel(inter, db)

    assert result is None
    inter.followup.send.assert_awaited_once()
    assert "server" in inter.followup.send.call_args.args[0].lower()


async def test_open_ticket_channel_handles_category_forbidden():
    db = mongomock.MongoClient(tz_aware=True).db
    guild, _ = _ticket_guild(has_category=False, create_category_error=_forbidden())
    inter = _ticket_interaction(guild)

    result = await _open_ticket_channel(inter, db)

    assert result is None
    guild.create_text_channel.assert_not_awaited()
    inter.followup.send.assert_awaited_once()


async def test_open_ticket_channel_handles_channel_forbidden():
    db = mongomock.MongoClient(tz_aware=True).db
    guild, _ = _ticket_guild(create_channel_error=_forbidden())
    inter = _ticket_interaction(guild)

    result = await _open_ticket_channel(inter, db)

    assert result is None
    inter.followup.send.assert_awaited_once()


# -- ReportModal (anonymous report) --
async def test_report_modal_creates_anonymous_ticket():
    db = mongomock.MongoClient(tz_aware=True).db
    guild, channel = _ticket_guild()
    inter = _ticket_interaction(guild)

    modal = ReportModal(db=db, close_view=MagicMock())
    for name, value in [
        ("cible", "Cheater#1"),
        ("queue", "Pro"),
        ("raison", "Cheating"),
        ("details", "Obvious aimbot on Ascent"),
        ("preuves", ""),  # empty -> optional field omitted
    ]:
        field = MagicMock()
        field.value = value
        setattr(modal, name, field)

    await modal.on_submit(inter)

    inter.response.defer.assert_awaited_once()
    channel.send.assert_awaited_once()
    embed = channel.send.call_args.kwargs["embed"]
    assert embed.footer.text == "Anonymous report"
    field_names = [f.name for f in embed.fields]
    assert "Reported player" in field_names
    assert "Evidence" not in field_names  # empty -> not added
    assert channel.send.call_args.kwargs["view"] is modal.close_view
    # Anonymity: no per-user overwrite -> the channel stays synced with
    # the category and the reporter gets no explicit access.
    assert "overwrites" not in guild.create_text_channel.call_args.kwargs
    inter.followup.send.assert_awaited_once()
    assert "anonymous" in inter.followup.send.call_args.args[0].lower()


async def test_report_modal_includes_evidence_when_provided():
    db = mongomock.MongoClient(tz_aware=True).db
    guild, channel = _ticket_guild()
    inter = _ticket_interaction(guild)

    modal = ReportModal(db=db, close_view=MagicMock())
    for name, value in [
        ("cible", "Cheater#1"),
        ("queue", "Open"),
        ("raison", "Toxicity"),
        ("details", "Repeated insults"),
        ("preuves", "https://clips.twitch.tv/xyz"),
    ]:
        field = MagicMock()
        field.value = value
        setattr(modal, name, field)

    await modal.on_submit(inter)

    embed = channel.send.call_args.kwargs["embed"]
    values = {f.name: f.value for f in embed.fields}
    assert values["Evidence"] == "https://clips.twitch.tv/xyz"


# -- RankModal (rank application, identified) --
async def test_rank_modal_creates_identified_ticket():
    db = mongomock.MongoClient(tz_aware=True).db
    guild, channel = _ticket_guild()
    user = _fake_member(7, "Candidate", manage_guild=False)
    inter = _ticket_interaction(guild, user=user)

    modal = RankModal(db=db, close_view=MagicMock())
    for name, value in [
        ("rank", "Pro Queue"),
        ("tracker", "https://tracker.gg/valorant/profile/x"),
        ("experience", "VCT 2024, LAN Paris, VLR team"),
    ]:
        field = MagicMock()
        field.value = value
        setattr(modal, name, field)

    await modal.on_submit(inter)

    inter.response.defer.assert_awaited_once()
    channel.send.assert_awaited_once()
    embed = channel.send.call_args.kwargs["embed"]
    values = {f.name: f.value for f in embed.fields}
    # Identified: the candidate's mention is present
    assert values["Member"] == user.mention
    assert values["Target rank"] == "Pro Queue"
    assert values["Tracker"] == "https://tracker.gg/valorant/profile/x"
    assert "VCT 2024" in values["Experience (tournaments / LANs / VLR)"]
    assert "Queue Application" in embed.title
    assert channel.send.call_args.kwargs["view"] is modal.close_view
    # Identified candidate: they get read/write access to THEIR ticket
    overwrites = guild.create_text_channel.call_args.kwargs["overwrites"]
    assert user in overwrites
    assert overwrites[user].view_channel is True
    assert overwrites[user].send_messages is True
    inter.followup.send.assert_awaited_once()
    assert "rank" in inter.followup.send.call_args.args[0].lower()


async def test_rank_modal_reports_error_when_channel_post_fails():
    """The channel is created but sending the embed fails: the user must
    see an error, not a fake success message."""
    db = mongomock.MongoClient(tz_aware=True).db
    guild, channel = _ticket_guild()
    channel.send = AsyncMock(side_effect=_forbidden())
    inter = _ticket_interaction(guild)

    modal = RankModal(db=db, close_view=MagicMock())
    for name in ("rank", "tracker", "experience"):
        field = MagicMock()
        field.value = "x"
        setattr(modal, name, field)

    await modal.on_submit(inter)

    inter.followup.send.assert_awaited_once()
    msg = inter.followup.send.call_args.args[0]
    assert msg.startswith("❌")
    assert "sent" not in msg  # no fake success


async def test_rank_modal_aborts_when_channel_creation_fails():
    db = mongomock.MongoClient(tz_aware=True).db
    guild, channel = _ticket_guild(create_channel_error=_forbidden())
    inter = _ticket_interaction(guild)

    modal = RankModal(db=db, close_view=MagicMock())
    for name in ("rank", "tracker", "experience"):
        field = MagicMock()
        field.value = "x"
        setattr(modal, name, field)

    await modal.on_submit(inter)

    # No channel -> no message sent in the channel, but ephemeral error
    channel.send.assert_not_awaited()
    inter.followup.send.assert_awaited_once()


# -- TicketPanelView: 2-button routing --
async def test_ticket_panel_reports_button_opens_report_modal():
    db = mongomock.MongoClient(tz_aware=True).db
    view = TicketPanelView(db=db, close_view=MagicMock())
    inter = _ticket_interaction(guild=MagicMock())

    await view.open_reports.callback(inter)

    inter.response.send_modal.assert_awaited_once()
    modal = inter.response.send_modal.call_args.args[0]
    assert isinstance(modal, ReportModal)


async def test_ticket_panel_ranks_button_opens_rank_modal():
    db = mongomock.MongoClient(tz_aware=True).db
    close_view = MagicMock()
    view = TicketPanelView(db=db, close_view=close_view)
    inter = _ticket_interaction(guild=MagicMock())

    await view.open_ranks.callback(inter)

    inter.response.send_modal.assert_awaited_once()
    modal = inter.response.send_modal.call_args.args[0]
    assert isinstance(modal, RankModal)
    # The RankModal receives the shared close_view from the panel
    assert modal.close_view is close_view
