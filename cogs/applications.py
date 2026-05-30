"""
Applications + welcome + report cog. Extracted from bot.py (monolith refactor).

Contains:
  - Application system (ApplicationModal, StaffModal, RefuseReasonModal,
    RoleChoiceView, WelcomeView, ApplicationReviewView).
  - /welcome: posts the Apply button in #verify.
  - /report: posts the ticket opening panel (TicketPanelView) with 2
    options in the current channel:
      * Reports -> ReportModal (anonymous report).
      * Ranks   -> RankModal (rank application, identified candidate).
  - _open_ticket_channel: creates the `ticket-{N}` channel (shared by Reports/Ranks).
  - CloseTicketView: closes a ticket.

All persistent views (stable custom_id) are registered via
`bot.add_view(...)` in `setup()`. Modals and RoleChoiceView (timeout =
APPLICATION_VIEW_TIMEOUT_SECONDS) are instantiated on the fly.
"""

from __future__ import annotations

import contextlib
import logging
from datetime import UTC, datetime, timedelta

import discord
from discord import app_commands
from discord.ext import commands
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from services import repository

logger = logging.getLogger(__name__)

# Timeout for an ephemeral RoleChoiceView (Player vs Staff). Intentionally
# decoupled from VOTE_TIMEOUT_MINUTES: this is a quick UX, not the match
# flow.
APPLICATION_VIEW_TIMEOUT_SECONDS: int = 60


# ── Constants ───────────────────────────────────────────────────
CANDIDATURE_CHANNEL = "candidatures"
WELCOME_CHANNEL = "verify"
PLAYERS_ROLE = "Members"
STAFF_ROLE = "Coach/Analyst/Manager"
TICKETS_CATEGORY_NAME = "Tickets"
CANDIDATURE_COOLDOWN_SECONDS = 3600


def _has_access(interaction: discord.Interaction, db) -> bool:
    """Reproduces `bot.has_access` without circular dependency.

    Admin (manage_guild) OR bypass role configured via /bypass.
    """
    if interaction.user.guild_permissions.manage_guild:
        return True
    role_id = repository.get_bypass_role(db, interaction.guild_id)
    return bool(role_id and any(r.id == role_id for r in interaction.user.roles))


def _try_acquire_candidature_cooldown(db, uid: str) -> tuple[bool, float]:
    """Atomically attempts to acquire an application cooldown slot.

    Resolves the read-then-write race: two concurrent submissions cannot
    both pass the check (CAS via conditional update + insert with
    DuplicateKeyError handling).
    """
    now = datetime.now(UTC)
    cutoff = now - timedelta(seconds=CANDIDATURE_COOLDOWN_SECONDS)
    cooldown_col = db["candidature_cooldowns"]
    res = cooldown_col.update_one(
        {"_id": uid, "last_apply": {"$lt": cutoff}},
        {"$set": {"last_apply": now}},
    )
    if res.modified_count == 1:
        return True, 0.0
    try:
        cooldown_col.insert_one({"_id": uid, "last_apply": now})
        return True, 0.0
    except DuplicateKeyError:
        pass
    doc = cooldown_col.find_one({"_id": uid})
    if doc is None:
        return True, 0.0
    last = doc["last_apply"]
    if last.tzinfo is None:
        last = last.replace(tzinfo=UTC)
    remaining = CANDIDATURE_COOLDOWN_SECONDS - (now - last).total_seconds()
    if remaining <= 0:
        return True, 0.0
    return False, remaining


def _parse_application_embed(message: discord.Message) -> tuple[int | None, str, bool]:
    """Extracts (applicant_id, username, is_staff) from an application embed.

    Allows `ApplicationReviewView` to be persistent (without internal state)
    by reconstructing the context from the message on each click.
    """
    if not message.embeds:
        return None, "", False
    embed = message.embeds[0]
    is_staff = "Staff" in (embed.title or "")
    applicant_id: int | None = None
    footer_text = (embed.footer.text or "") if embed.footer else ""
    if footer_text.startswith("ID:"):
        try:
            applicant_id = int(footer_text.split(":", 1)[1].strip())
        except (ValueError, IndexError):
            applicant_id = None
    pseudo = ""
    for field in embed.fields:
        if field.name in ("🎮 In-game username", "🎮 Username"):
            pseudo = field.value or ""
            break
    return applicant_id, pseudo, is_staff


# ── Modals ────────────────────────────────────────────────────────
class ApplicationModal(discord.ui.Modal, title="10mans Application"):
    pseudo: discord.ui.TextInput = discord.ui.TextInput(
        label="What is your username?",
        placeholder="What should I call you? e.g. jetax",
        max_length=50,
    )
    tracker: discord.ui.TextInput = discord.ui.TextInput(
        label="Link to your tracker", placeholder="https://tracker.gg/...", max_length=200
    )
    experience: discord.ui.TextInput = discord.ui.TextInput(
        label="Tournament / LAN experience?",
        placeholder="List the tournaments/LANs you have participated in",
        style=discord.TextStyle.paragraph,
        required=False,
        max_length=500,
    )

    def __init__(self, db, review_view: ApplicationReviewView) -> None:
        super().__init__()
        self.db = db
        self.review_view = review_view

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        uid = str(interaction.user.id)
        allowed, remaining = _try_acquire_candidature_cooldown(self.db, uid)
        if not allowed:
            minutes = int(remaining // 60)
            seconds = int(remaining % 60)
            await interaction.followup.send(
                f"⏳ You have already applied recently! Try again in **{minutes}min {seconds}s**.",
                ephemeral=True,
            )
            return
        with contextlib.suppress(discord.Forbidden):
            await interaction.user.send(
                embed=discord.Embed(
                    title="✅ Application received!",
                    description="Thanks for applying, we are reviewing your profile and will get back to you as soon as possible.",
                    color=0x2ECC71,
                    timestamp=datetime.now(UTC),
                )
            )
        channel = discord.utils.get(interaction.guild.text_channels, name=CANDIDATURE_CHANNEL)
        if not channel:
            await interaction.followup.send("Applications channel not found.", ephemeral=True)
            return
        embed = discord.Embed(
            title="📋 New application",
            description="🎮 **Player application**",
            color=0x5865F2,
            timestamp=datetime.now(UTC),
        )
        embed.set_thumbnail(url=interaction.user.display_avatar.url)
        embed.add_field(name="👤 Member", value=interaction.user.mention, inline=True)
        embed.add_field(name="🎮 In-game username", value=self.pseudo.value, inline=True)
        embed.add_field(name="🔗 Tracker", value=self.tracker.value, inline=False)
        embed.add_field(
            name="🏆 Tournaments / LAN",
            value=self.experience.value if self.experience.value else "None",
            inline=False,
        )
        embed.set_footer(text=f"ID: {interaction.user.id}")
        msg = await channel.send(embed=embed, view=self.review_view)
        repository.register_application(
            self.db,
            interaction.guild_id,
            msg.id,
            interaction.user.id,
            is_staff=False,
        )
        await interaction.followup.send("✅ Your application has been sent!", ephemeral=True)


class StaffModal(discord.ui.Modal, title="Staff Application"):
    pseudo: discord.ui.TextInput = discord.ui.TextInput(
        label="What is your username?",
        placeholder="What should I call you? e.g. jetax",
        max_length=50,
    )
    poste: discord.ui.TextInput = discord.ui.TextInput(
        label="Current position",
        placeholder="e.g. Coach, Analyst, Manager... and in which structure/organization?",
        max_length=100,
    )
    experience: discord.ui.TextInput = discord.ui.TextInput(
        label="Experience",
        placeholder="Describe your experience in the field...",
        style=discord.TextStyle.paragraph,
        required=False,
        max_length=500,
    )

    def __init__(self, db, review_view: ApplicationReviewView) -> None:
        super().__init__()
        self.db = db
        self.review_view = review_view

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        uid = str(interaction.user.id)
        allowed, remaining = _try_acquire_candidature_cooldown(self.db, uid)
        if not allowed:
            minutes = int(remaining // 60)
            seconds = int(remaining % 60)
            await interaction.followup.send(
                f"⏳ You have already applied recently! Try again in **{minutes}min {seconds}s**.",
                ephemeral=True,
            )
            return
        with contextlib.suppress(discord.Forbidden):
            await interaction.user.send(
                embed=discord.Embed(
                    title="✅ Application received!",
                    description="Thanks for applying, we are reviewing your profile and will get back to you as soon as possible.",
                    color=0x2ECC71,
                    timestamp=datetime.now(UTC),
                )
            )
        channel = discord.utils.get(interaction.guild.text_channels, name=CANDIDATURE_CHANNEL)
        if not channel:
            await interaction.followup.send("Applications channel not found.", ephemeral=True)
            return
        embed = discord.Embed(
            title="📋 New Staff application",
            description="🎯 **Coach / Analyst / Manager application**",
            color=0xE67E22,
            timestamp=datetime.now(UTC),
        )
        embed.set_thumbnail(url=interaction.user.display_avatar.url)
        embed.add_field(name="👤 Member", value=interaction.user.mention, inline=True)
        embed.add_field(name="🎮 Username", value=self.pseudo.value, inline=True)
        embed.add_field(name="💼 Position", value=self.poste.value, inline=False)
        embed.add_field(
            name="📋 Experience",
            value=self.experience.value if self.experience.value else "None",
            inline=False,
        )
        embed.set_footer(text=f"ID: {interaction.user.id}")
        msg = await channel.send(embed=embed, view=self.review_view)
        repository.register_application(
            self.db,
            interaction.guild_id,
            msg.id,
            interaction.user.id,
            is_staff=True,
        )
        await interaction.followup.send("✅ Your application has been sent!", ephemeral=True)


class RefuseReasonModal(discord.ui.Modal, title="Decline reason"):
    reason: discord.ui.TextInput = discord.ui.TextInput(
        label="Decline reason (optional)",
        placeholder="Explain why...",
        style=discord.TextStyle.paragraph,
        required=False,
        max_length=500,
    )

    def __init__(self, db, applicant_id: int):
        super().__init__()
        self.db = db
        self.applicant_id = applicant_id

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        claimed = repository.claim_application_decision(
            self.db,
            interaction.guild_id,
            interaction.message.id,
            status="refused",
            decided_by=interaction.user.id,
        )
        if not claimed:
            await interaction.followup.send(
                "❌ This application has already been handled by another admin.",
                ephemeral=True,
            )
            return
        member = interaction.guild.get_member(self.applicant_id)
        reason_text = self.reason.value if self.reason.value else "No reason provided."
        if member:
            try:
                embed_dm = discord.Embed(
                    title="❌ Application declined",
                    description="Sorry, your application was not accepted. Please try again later.",
                    color=0xE74C3C,
                    timestamp=datetime.now(UTC),
                )
                embed_dm.add_field(name="📋 Reason", value=reason_text, inline=False)
                await member.send(embed=embed_dm)
            except discord.Forbidden:
                pass
            with contextlib.suppress(discord.Forbidden):
                await member.kick(reason=f"Application declined: {reason_text}")
        try:
            embed = interaction.message.embeds[0]
            embed.color = 0xE74C3C
            embed.add_field(name="Declined by", value=interaction.user.mention, inline=True)
            embed.add_field(name="📋 Reason", value=reason_text, inline=True)
            await interaction.message.edit(embed=embed, view=None)
        except Exception:
            with contextlib.suppress(Exception):
                await interaction.message.edit(view=None)
        await interaction.followup.send(
            "✅ Application declined and user kicked.", ephemeral=True
        )


async def _open_ticket_channel(
    interaction: discord.Interaction,
    db,
    *,
    member_access: discord.Member | discord.User | None = None,
) -> discord.TextChannel | None:
    """Creates the `ticket-{N}` channel in the `Tickets` category.

    Shared by Reports and Queue Application tickets. Returns the created
    channel, or `None` if the operation fails (in that case the user has
    already received an ephemeral error message via `followup`). The caller
    must have deferred the interaction beforehand (`defer(..., thinking=True)`).

    If `member_access` is provided (e.g. Queue Application, where the
    candidate is identified), the channel inherits overwrites copied from
    the category + read/write access for this member, so they can chat with
    staff in THEIR ticket. Without `member_access` (e.g. anonymous Reports),
    the channel stays synced with the category: the creator has no explicit
    access and anonymity is preserved.
    """
    guild = interaction.guild
    if guild is None:
        await interaction.followup.send(
            "❌ This command must be used in a server.",
            ephemeral=True,
        )
        return None

    category = discord.utils.get(guild.categories, name=TICKETS_CATEGORY_NAME)
    if category is None:
        try:
            category = await guild.create_category(TICKETS_CATEGORY_NAME)
        except discord.Forbidden:
            await interaction.followup.send(
                "❌ The bot does not have the **Manage Channels** permission to "
                f"create the `{TICKETS_CATEGORY_NAME}` category.",
                ephemeral=True,
            )
            return None

    # The counter is incremented BEFORE the channel is created: if creation
    # fails (Forbidden), the number is "consumed" and a gap will remain in
    # the ticket numbering. This is intentionally tolerated - gaps in ticket
    # numbers are harmless and avoid fragile rollback logic.
    counter_doc = db["ticket_counters"].find_one_and_update(
        {"_id": str(guild.id)},
        {"$inc": {"counter": 1}},
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )
    next_number = int(counter_doc["counter"])
    channel_name = f"ticket-{next_number}"

    # For an identified ticket (Queue Application), we copy the category's
    # overwrites to preserve its config (staff / @everyone) and then add
    # dedicated access for the candidate. Without `member_access`, we let
    # the channel sync with the category (behavior of anonymous Reports).
    create_kwargs: dict = {"category": category}
    if member_access is not None:
        overwrites = dict(category.overwrites)
        overwrites[member_access] = discord.PermissionOverwrite(
            view_channel=True,
            send_messages=True,
            read_message_history=True,
            attach_files=True,
        )
        create_kwargs["overwrites"] = overwrites

    try:
        return await guild.create_text_channel(channel_name, **create_kwargs)
    except discord.Forbidden:
        await interaction.followup.send(
            "❌ The bot does not have permission to create the ticket channel.",
            ephemeral=True,
        )
        return None


class ReportModal(discord.ui.Modal, title="Send an anonymous report"):
    cible: discord.ui.TextInput = discord.ui.TextInput(
        label="Who are you reporting?",
        placeholder="Discord username / @mention / player ID",
        style=discord.TextStyle.short,
        required=True,
        max_length=200,
    )
    queue: discord.ui.TextInput = discord.ui.TextInput(
        label="In which queue?",
        placeholder="Pro / SemiPro / Open / GC",
        style=discord.TextStyle.short,
        required=True,
        max_length=50,
    )
    raison: discord.ui.TextInput = discord.ui.TextInput(
        label="For what reason?",
        placeholder="Cheating, toxicity, throwing, insults, AFK, etc.",
        style=discord.TextStyle.short,
        required=True,
        max_length=200,
    )
    details: discord.ui.TextInput = discord.ui.TextInput(
        label="Details / context",
        placeholder="Describe the situation: when, where, what happened...",
        style=discord.TextStyle.paragraph,
        required=True,
        max_length=1500,
    )
    preuves: discord.ui.TextInput = discord.ui.TextInput(
        label="Evidence (links, clips, screenshots)",
        placeholder="Paste links to your evidence here (optional)",
        style=discord.TextStyle.paragraph,
        required=False,
        max_length=1000,
    )

    def __init__(self, db, close_view: CloseTicketView) -> None:
        super().__init__()
        self.db = db
        self.close_view = close_view

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        ticket_channel = await _open_ticket_channel(interaction, self.db)
        if ticket_channel is None:
            return

        embed = discord.Embed(
            title=f"🎫 New report - {ticket_channel.name}",
            color=0xE67E22,
            timestamp=datetime.now(UTC),
        )
        embed.add_field(name="Reported player", value=self.cible.value, inline=False)
        embed.add_field(name="Queue concerned", value=self.queue.value, inline=False)
        embed.add_field(name="Reason", value=self.raison.value, inline=False)
        embed.add_field(name="Details", value=self.details.value, inline=False)
        if self.preuves.value.strip():
            embed.add_field(name="Evidence", value=self.preuves.value, inline=False)
        embed.set_footer(text="Anonymous report")
        try:
            await ticket_channel.send(embed=embed, view=self.close_view)
        except discord.HTTPException:
            logger.exception("[ticket] sending the initial message raised")
            await interaction.followup.send(
                "❌ An error occurred while sending your report.",
                ephemeral=True,
            )
            return

        await interaction.followup.send(
            f"✅ Your anonymous report has been sent ({ticket_channel.mention}).",
            ephemeral=True,
        )


class RankModal(discord.ui.Modal, title="Rank application"):
    """Opens a rank application ticket (identified candidate).

    Asks 3 questions automatically and then creates a `ticket-{N}` channel
    in the `Tickets` category with a summary embed + close button.
    """

    rank: discord.ui.TextInput = discord.ui.TextInput(
        label="Which rank are you applying for?",
        placeholder="Pro Queue / Semi Pro Queue / Advanced Queue",
        style=discord.TextStyle.short,
        required=True,
        max_length=100,
    )
    tracker: discord.ui.TextInput = discord.ui.TextInput(
        label="Your tracker link",
        placeholder="https://tracker.gg/valorant/profile/...",
        style=discord.TextStyle.short,
        required=True,
        max_length=300,
    )
    experience: discord.ui.TextInput = discord.ui.TextInput(
        label="Your tournament/LAN and/or VLR experience",
        placeholder="Describe your competitive history: tournaments, LANs, VLR teams...",
        style=discord.TextStyle.paragraph,
        required=True,
        max_length=1500,
    )

    def __init__(self, db, close_view: CloseTicketView) -> None:
        super().__init__()
        self.db = db
        self.close_view = close_view

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        ticket_channel = await _open_ticket_channel(
            interaction, self.db, member_access=interaction.user
        )
        if ticket_channel is None:
            return

        embed = discord.Embed(
            title=f"🎖️ Queue Application - {ticket_channel.name}",
            color=0x9B59B6,
            timestamp=datetime.now(UTC),
        )
        embed.add_field(name="Member", value=interaction.user.mention, inline=False)
        embed.add_field(name="Target rank", value=self.rank.value, inline=False)
        embed.add_field(name="Tracker", value=self.tracker.value, inline=False)
        embed.add_field(
            name="Experience (tournaments / LANs / VLR)",
            value=self.experience.value,
            inline=False,
        )
        embed.set_footer(text=f"Application by {interaction.user}")
        try:
            await ticket_channel.send(embed=embed, view=self.close_view)
        except discord.HTTPException:
            logger.exception("[ticket] sending the initial message (rank) raised")
            await interaction.followup.send(
                "❌ An error occurred while sending your application.",
                ephemeral=True,
            )
            return

        await interaction.followup.send(
            f"✅ Your rank application has been sent ({ticket_channel.mention}).",
            ephemeral=True,
        )


# ── Views ────────────────────────────────────────────────────────
class ApplicationReviewView(discord.ui.View):
    """Persistent view: rebuilds itself from the message's embed."""

    def __init__(self, db) -> None:
        super().__init__(timeout=None)
        self.db = db

    @discord.ui.button(
        label="Accept",
        style=discord.ButtonStyle.success,
        custom_id="application_accept",
    )
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not _has_access(interaction, self.db):
            await interaction.response.send_message(
                "❌ You do not have permission to handle applications.",
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True)
        # 1) Validate BEFORE the CAS (cf. audit: avoids stuck state).
        applicant_id, pseudo, is_staff = _parse_application_embed(interaction.message)
        if applicant_id is None:
            await interaction.followup.send(
                "❌ Application data unreadable (corrupted embed).",
                ephemeral=True,
            )
            return
        member = interaction.guild.get_member(applicant_id)
        if not member:
            await interaction.followup.send("❌ Member not found.", ephemeral=True)
            return
        # 2) Atomic CAS
        claimed = repository.claim_application_decision(
            self.db,
            interaction.guild_id,
            interaction.message.id,
            status="accepted",
            decided_by=interaction.user.id,
        )
        if not claimed:
            await interaction.followup.send(
                "❌ This application has already been handled by another admin.",
                ephemeral=True,
            )
            return
        try:
            old_embed = interaction.message.embeds[0] if interaction.message.embeds else None
            new_embed = discord.Embed(
                title="📋 Application accepted", color=0x2ECC71, timestamp=datetime.now(UTC)
            )
            new_embed.set_thumbnail(url=member.display_avatar.url)
            new_embed.add_field(name="👤 Member", value=member.mention, inline=True)
            new_embed.add_field(name="🎮 Username", value=pseudo, inline=True)
            if old_embed:
                for field in old_embed.fields:
                    if field.name in (
                        "🔗 Tracker",
                        "🏆 Tournaments / LAN",
                        "💼 Position",
                        "📋 Experience",
                        "Tracker",
                        "Tournaments / LAN",
                        "Position",
                        "Experience",
                    ):
                        new_embed.add_field(name=field.name, value=field.value, inline=False)
            new_embed.add_field(name="✅ Accepted by", value=interaction.user.mention, inline=False)
            await interaction.message.edit(embed=new_embed, view=None)
        except Exception:
            logger.exception("[accept] Edit failed")
            with contextlib.suppress(Exception):
                await interaction.message.edit(view=None)
        role_name = STAFF_ROLE if is_staff else PLAYERS_ROLE
        role = discord.utils.get(interaction.guild.roles, name=role_name)
        if role:
            try:
                await member.add_roles(role)
            except Exception:
                logger.exception("[accept] Role assignment failed")
        if is_staff:
            members_role = discord.utils.get(interaction.guild.roles, name=PLAYERS_ROLE)
            if members_role:
                try:
                    await member.add_roles(members_role)
                except Exception:
                    logger.exception("[accept] Members role assignment failed")
        with contextlib.suppress(Exception):
            await member.edit(nick=pseudo)
        with contextlib.suppress(discord.Forbidden):
            await member.send(
                embed=discord.Embed(
                    title="🎉 Application accepted!",
                    description="Congrats, you have been accepted, you can now play 10mans!",
                    color=0x2ECC71,
                    timestamp=datetime.now(UTC),
                )
            )
        await interaction.followup.send("✅ Application accepted!", ephemeral=True)

    @discord.ui.button(
        label="Decline",
        style=discord.ButtonStyle.danger,
        custom_id="application_refuse",
    )
    async def refuse(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not _has_access(interaction, self.db):
            await interaction.response.send_message(
                "❌ You do not have permission to handle applications.",
                ephemeral=True,
            )
            return
        applicant_id, _pseudo, _is_staff = _parse_application_embed(interaction.message)
        if applicant_id is None:
            await interaction.response.send_message(
                "❌ Application data unreadable (corrupted embed).",
                ephemeral=True,
            )
            return
        await interaction.response.send_modal(
            RefuseReasonModal(db=self.db, applicant_id=applicant_id)
        )


class RoleChoiceView(discord.ui.View):
    """Ephemeral view (timeout = APPLICATION_VIEW_TIMEOUT_SECONDS): Player vs Staff."""

    def __init__(self, db, review_view: ApplicationReviewView) -> None:
        super().__init__(timeout=APPLICATION_VIEW_TIMEOUT_SECONDS)
        self.db = db
        self.review_view = review_view

    @discord.ui.button(label="Player", style=discord.ButtonStyle.primary)
    async def player_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(
            ApplicationModal(db=self.db, review_view=self.review_view)
        )

    @discord.ui.button(label="Coach / Analyst / Manager", style=discord.ButtonStyle.secondary)
    async def staff_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(StaffModal(db=self.db, review_view=self.review_view))


class WelcomeView(discord.ui.View):
    """Persistent view: Apply button in #verify."""

    def __init__(self, db, review_view: ApplicationReviewView) -> None:
        super().__init__(timeout=None)
        self.db = db
        self.review_view = review_view

    @discord.ui.button(
        label="Apply", style=discord.ButtonStyle.primary, custom_id="postuler_btn"
    )
    async def postuler(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Non-atomic peek: we do not consume the cooldown here (otherwise
        # a user who closes the modal without submitting would be blocked
        # for 1h for nothing). The real atomic claim happens in
        # ApplicationModal/StaffModal.on_submit.
        uid = str(interaction.user.id)
        doc = self.db["candidature_cooldowns"].find_one({"_id": uid})
        if doc:
            last = doc["last_apply"]
            if last.tzinfo is None:
                last = last.replace(tzinfo=UTC)
            diff = datetime.now(UTC) - last
            if diff.total_seconds() < CANDIDATURE_COOLDOWN_SECONDS:
                remaining = CANDIDATURE_COOLDOWN_SECONDS - diff.total_seconds()
                minutes = int(remaining // 60)
                seconds = int(remaining % 60)
                await interaction.response.send_message(
                    f"⏳ You have already applied recently! Try again in **{minutes}min {seconds}s**.",
                    ephemeral=True,
                )
                return
        await interaction.response.send_message(
            "## Which position would you like to apply for? 🎮",
            view=RoleChoiceView(db=self.db, review_view=self.review_view),
            ephemeral=True,
        )


class CloseTicketView(discord.ui.View):
    """Persistent view: a 'Close ticket' button that deletes the channel."""

    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Close ticket",
        style=discord.ButtonStyle.danger,
        custom_id="ticket_close_btn",
    )
    async def close(self, interaction: discord.Interaction, button: discord.ui.Button):
        channel = interaction.channel
        if channel is None or not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message(
                "❌ Unable to close this channel here.",
                ephemeral=True,
            )
            return
        with contextlib.suppress(discord.HTTPException):
            await interaction.response.send_message(
                "🔒 Closing the ticket...",
                ephemeral=True,
            )
        try:
            await channel.delete(reason=f"Ticket closed by {interaction.user}")
        except discord.NotFound:
            pass
        except discord.Forbidden:
            with contextlib.suppress(discord.HTTPException):
                await interaction.followup.send(
                    "❌ Missing permission to delete this channel.",
                    ephemeral=True,
                )
        except discord.HTTPException:
            logger.exception("[ticket] deleting the channel raised")


class ReportView(discord.ui.View):
    """Persistent view: a 'Report' button that opens the ReportModal."""

    def __init__(self, db, close_view: CloseTicketView) -> None:
        super().__init__(timeout=None)
        self.db = db
        self.close_view = close_view

    @discord.ui.button(
        label="Report",
        style=discord.ButtonStyle.danger,
        custom_id="report_open_btn",
    )
    async def open_report(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(ReportModal(db=self.db, close_view=self.close_view))


class TicketPanelView(discord.ui.View):
    """Persistent view: ticket opening panel with 2 options.

    - **Reports** -> ReportModal (anonymous report).
    - **Ranks**   -> RankModal (rank application, identified candidate).
    """

    def __init__(self, db, close_view: CloseTicketView) -> None:
        super().__init__(timeout=None)
        self.db = db
        self.close_view = close_view

    @discord.ui.button(
        label="Reports",
        style=discord.ButtonStyle.danger,
        custom_id="ticket_panel_reports_btn",
    )
    async def open_reports(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(ReportModal(db=self.db, close_view=self.close_view))

    @discord.ui.button(
        label="Queue Application",
        style=discord.ButtonStyle.primary,
        custom_id="ticket_panel_ranks_btn",
    )
    async def open_ranks(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(RankModal(db=self.db, close_view=self.close_view))


# ── Cog ──────────────────────────────────────────────────────────
class ApplicationsCog(commands.Cog):
    def __init__(self, bot: commands.Bot, db) -> None:
        self.bot = bot
        self.db = db
        # Persistent view instances (registered via bot.add_view in setup).
        self.close_view = CloseTicketView()
        self.review_view = ApplicationReviewView(db=db)
        self.welcome_view = WelcomeView(db=db, review_view=self.review_view)
        self.report_view = ReportView(db=db, close_view=self.close_view)
        self.ticket_panel_view = TicketPanelView(db=db, close_view=self.close_view)

    @app_commands.command(
        name="welcome", description="Sends the welcome message in the verify channel"
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    async def welcome(self, interaction: discord.Interaction) -> None:
        channel = discord.utils.get(interaction.guild.text_channels, name=WELCOME_CHANNEL)
        if not channel:
            await interaction.response.send_message("Verify channel not found.", ephemeral=True)
            return
        embed = discord.Embed(
            title="Welcome to The Hub Matchmaking",
            description="Welcome to a **10mans** server with 4 queues:\n\n• **FL Pro** - VCL / VCT\n• **FL Semi-Pro** - TOP VRC\n• **FL Open** - Open to everyone\n• **FL GC** - Open to GC only\n\nTo gain access to the server, please click the **Apply** button just below.\n\n**Have fun! 🍀**",
            color=0x5865F2,
            timestamp=datetime.now(UTC),
        )
        embed.set_footer(text=interaction.guild.name)
        await channel.send(embed=embed, view=self.welcome_view)
        await interaction.response.send_message(
            f"Message sent in {channel.mention}!", ephemeral=True
        )

    @welcome.error
    async def _welcome_error(self, interaction: discord.Interaction, error):
        if isinstance(error, app_commands.MissingPermissions):
            await interaction.response.send_message(
                "Only administrators can use this command.", ephemeral=True
            )

    @app_commands.command(
        name="report",
        description="Posts the ticket opening panel (Reports / Ranks) in this channel",
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    async def report(self, interaction: discord.Interaction) -> None:
        channel = interaction.channel
        if channel is None or not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message(
                "❌ This command must be used in a text channel.",
                ephemeral=True,
            )
            return
        embed = discord.Embed(
            title="🎫 Open a ticket",
            description=(
                "Choose the type of ticket you would like to open:\n\n"
                "**Reports** - Report a player (cheating, toxicity, throwing, "
                "insults, AFK...). Your report is anonymous: your identity "
                "is not revealed to staff.\n\n"
                "**Queue Application** - Apply for a private queue. We will "
                "ask which queue you are aiming for, the criteria are:\n"
                "• Pro Queue: Recent 600rr peak or 5 VLR 2025/2026 lineups\n"
                "• Semi Pro Queue: Current High Immortal / Radiant or by invitation\n"
                "• Advanced Queue: Current Immortal 3 or by invitation"
            ),
            color=0x5865F2,
            timestamp=datetime.now(UTC),
        )
        embed.set_footer(text=interaction.guild.name if interaction.guild else "Tickets")
        await channel.send(embed=embed, view=self.ticket_panel_view)
        await interaction.response.send_message(
            f"Message sent in {channel.mention}!",
            ephemeral=True,
        )

    @report.error
    async def _report_error(self, interaction: discord.Interaction, error):
        if isinstance(error, app_commands.MissingPermissions):
            await interaction.response.send_message(
                "🚫 Reserved for administrators.",
                ephemeral=True,
            )


async def setup(bot: commands.Bot, db) -> None:
    cog = ApplicationsCog(bot, db)
    await bot.add_cog(cog)
    # Register persistent views (after restart, their custom_ids must be
    # routable by the bot even without a message instance).
    bot.add_view(cog.review_view)
    bot.add_view(cog.welcome_view)
    bot.add_view(cog.close_view)
    bot.add_view(cog.ticket_panel_view)
    # Kept to route the old "Report" panels already posted (custom_id
    # report_open_btn) after restart; new panels use ticket_panel_view.
    bot.add_view(cog.report_view)
