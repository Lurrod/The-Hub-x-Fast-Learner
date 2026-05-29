"""
ELO admin cog: /win, /lose, /elomodify, /winmodify, /losemodify, /resetelo,
/reset-queue, /stats, /leaderboard, /inactivity. Extracted from bot.py (monolith refactor).

Admin commands reserved to manage_guild OR bypass role.
`/stats` is public (visible to everyone).
`/leaderboard` is public in #leaderboard, ephemeral elsewhere.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

import discord
from discord import app_commands
from discord.ext import commands
from pymongo import ReturnDocument

from services import elo_calc, repository
from services.inactivity import (
    DEFAULT_INACTIVITY_LIMIT,
    format_inactivity,
    rank_by_inactivity,
)
from services.leaderboard_refresh import (
    build_leaderboard_payload,
    refresh_leaderboard_channel,
)

logger = logging.getLogger(__name__)


ELO_START = elo_calc.ELO_START

# ELO weighting by player position (slot 1..5) for /win and /lose.
# The first slot takes the biggest gain / the smallest loss.
WIN_DELTAS_BY_SLOT: tuple[int, ...] = (20, 18, 17, 16, 15)
LOSE_DELTAS_BY_SLOT: tuple[int, ...] = (10, 10, 12, 13, 15)

# Mapping queue_type -> channel name where to post the persistent message.
# (Intentionally duplicated from cogs/admin.py to avoid an inter-cog
# dependency: this mapping is very stable, and the duplication avoids a
# `from cogs.admin import ...` that would create an import cycle.)
QUEUE_CHANNEL_FOR_TYPE = {
    "pro": "pro-queue",
    "semipro": "semi-pro-queue",
    "open": "open-queue",
    "gc": "gc-queue",
}

_QUEUE_CHOICES = [
    app_commands.Choice(name="Pro", value="pro"),
    app_commands.Choice(name="SemiPro", value="semipro"),
    app_commands.Choice(name="Open", value="open"),
    app_commands.Choice(name="GC", value="gc"),
]

# Human-friendly queue labels for embeds and leaderboard titles.
QUEUE_LABELS = {
    "pro": "Pro Queue",
    "semipro": "Semi Pro Queue",
    "open": "Open Queue",
    "gc": "GC Queue",
}


def _has_access(interaction: discord.Interaction, db) -> bool:
    """Admin (manage_guild) OR bypass role configured via /bypass."""
    if interaction.user.guild_permissions.manage_guild:
        return True
    role_id = repository.get_bypass_role(db, interaction.guild_id)
    return bool(role_id and any(r.id == role_id for r in interaction.user.roles))


def _get_player(col, member: discord.Member, queue_type: str):
    return repository.get_or_create_player(
        col,
        member.id,
        queue_type,
        member.display_name,
        initial_elo=ELO_START,
    )


def _match_elo_for_member(db, guild_id: int, user_id: int, queue_type: str) -> int:
    """Server ELO of the player in the given queue, falling back to ELO_REFERENCE."""
    doc = repository.get_elo_col(db).find_one(
        {"_id": repository.player_doc_id(user_id, queue_type)}
    )
    if doc and doc.get("elo") is not None:
        return int(doc["elo"])
    return elo_calc.ELO_REFERENCE


def _compute_match_change_for_members(
    db,
    guild_id: int,
    members: list,
    queue_type: str,
) -> tuple[int, int, int]:
    """(avg_elo, gain, loss) for the list of players in the queue."""
    elos = [_match_elo_for_member(db, guild_id, m.id, queue_type) for m in members]
    avg = round(sum(elos) / len(elos)) if elos else elo_calc.ELO_REFERENCE
    gain, loss = elo_calc.compute_match_elo_change(avg)
    return avg, gain, loss


async def _refresh_leaderboard_safe(guild: discord.Guild | None, db, queue_type: str) -> None:
    """Refresh the leaderboard of the given queue in `#leaderboard`."""
    if guild is None:
        return
    try:
        await refresh_leaderboard_channel(guild, db, queue_type)
    except Exception:
        logger.exception("[leaderboard] refresh raised")


def _is_leaderboard_channel(interaction: discord.Interaction) -> bool:
    chan = interaction.channel
    name = getattr(chan, "name", "") or ""
    return "leaderboard" in name.lower()


class _ResetQueueConfirmView(discord.ui.View):
    """Interactive confirmation button for /reset-queue."""

    def __init__(self, queue_type: str, *, timeout: float = 30):
        super().__init__(timeout=timeout)
        self.queue_type = queue_type
        self.confirmed = False

    @discord.ui.button(label="Confirm reset", style=discord.ButtonStyle.danger)
    async def confirm(self, inter: discord.Interaction, button: discord.ui.Button):
        self.confirmed = True
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = True
        await inter.response.edit_message(view=self)
        self.stop()


class ELOAdminCog(commands.Cog):
    def __init__(self, bot: commands.Bot, db) -> None:
        self.bot = bot
        self.db = db

    # ── /win ───────────────────────────────────────────────────
    @app_commands.command(
        name="win",
        description="Record a win in a queue (gains weighted by position)",
    )
    @app_commands.describe(
        queue="Queue type",
        player1="Winning player 1",
        player2="Winning player 2",
        player3="Winning player 3",
        player4="Winning player 4",
        player5="Winning player 5",
    )
    @app_commands.choices(queue=_QUEUE_CHOICES)
    async def win(
        self,
        interaction: discord.Interaction,
        queue: str,
        player1: discord.Member,
        player2: discord.Member = None,
        player3: discord.Member = None,
        player4: discord.Member = None,
        player5: discord.Member = None,
    ):
        if not _has_access(interaction, self.db):
            await interaction.response.send_message("You don't have permission.", ephemeral=True)
            return
        players = [p for p in [player1, player2, player3, player4, player5] if p is not None]
        col = repository.get_elo_col(self.db)

        deltas = list(WIN_DELTAS_BY_SLOT)[: len(players)]
        avg_elo, _, _ = _compute_match_change_for_members(
            self.db,
            interaction.guild_id,
            players,
            queue,
        )
        desc = f"Group avg ELO: **{avg_elo}** -> gains weighted by position."

        embed = discord.Embed(
            title=f"{QUEUE_LABELS[queue]} results - Win recorded!",
            description=desc,
            color=0x2ECC71,
            timestamp=datetime.now(UTC),
        )
        for slot, member in enumerate(players):
            gain = deltas[slot]
            _get_player(col, member, queue)
            old_doc = col.find_one_and_update(
                {"_id": repository.player_doc_id(member.id, queue)},
                {"$inc": {"elo": gain, "wins": 1}},
                return_document=ReturnDocument.BEFORE,
            )
            old = (old_doc or {}).get("elo", 0)
            new = old + gain
            embed.add_field(
                name=member.display_name,
                value=f"+{gain} ELO -> **{new}** *(was {old})*",
                inline=False,
            )
        embed.set_footer(text=f"Recorded by {interaction.user.display_name}")
        await interaction.response.send_message(embed=embed)
        await _refresh_leaderboard_safe(interaction.guild, self.db, queue)

    # ── /lose ──────────────────────────────────────────────────
    @app_commands.command(
        name="lose",
        description="Record a loss in a queue (losses weighted by position)",
    )
    @app_commands.describe(
        queue="Queue type",
        player1="Losing player 1",
        player2="Losing player 2",
        player3="Losing player 3",
        player4="Losing player 4",
        player5="Losing player 5",
    )
    @app_commands.choices(queue=_QUEUE_CHOICES)
    async def lose(
        self,
        interaction: discord.Interaction,
        queue: str,
        player1: discord.Member,
        player2: discord.Member = None,
        player3: discord.Member = None,
        player4: discord.Member = None,
        player5: discord.Member = None,
    ):
        if not _has_access(interaction, self.db):
            await interaction.response.send_message("You don't have permission.", ephemeral=True)
            return
        players = [p for p in [player1, player2, player3, player4, player5] if p is not None]
        col = repository.get_elo_col(self.db)

        deltas = list(LOSE_DELTAS_BY_SLOT)[: len(players)]
        avg_elo, _, _ = _compute_match_change_for_members(
            self.db,
            interaction.guild_id,
            players,
            queue,
        )
        desc = f"Group avg ELO: **{avg_elo}** -> losses weighted by position."

        embed = discord.Embed(
            title=f"{QUEUE_LABELS[queue]} results - Loss recorded!",
            description=desc,
            color=0xE74C3C,
            timestamp=datetime.now(UTC),
        )
        for slot, member in enumerate(players):
            loss = deltas[slot]
            _get_player(col, member, queue)
            old_doc = col.find_one_and_update(
                {"_id": repository.player_doc_id(member.id, queue)},
                [
                    {
                        "$set": {
                            "elo": {"$max": [0, {"$subtract": [{"$ifNull": ["$elo", 0]}, loss]}]},
                            "losses": {"$add": [{"$ifNull": ["$losses", 0]}, 1]},
                        }
                    }
                ],
                return_document=ReturnDocument.BEFORE,
            )
            old = (old_doc or {}).get("elo", 0)
            new = max(0, old - loss)
            embed.add_field(
                name=member.display_name,
                value=f"-{loss} ELO -> **{new}** (was {old})",
                inline=False,
            )
        embed.set_footer(text=f"Recorded by {interaction.user.display_name}")
        await interaction.response.send_message(embed=embed)
        await _refresh_leaderboard_safe(interaction.guild, self.db, queue)

    # ── /leaderboard ───────────────────────────────────────────
    @app_commands.command(name="leaderboard", description="Show the ELO ranking of a queue")
    @app_commands.describe(queue="Queue type")
    @app_commands.choices(queue=_QUEUE_CHOICES)
    async def leaderboard(self, interaction: discord.Interaction, queue: str):
        public = _is_leaderboard_channel(interaction)
        ephemeral = not public
        await interaction.response.defer(ephemeral=ephemeral)
        file, view = await build_leaderboard_payload(interaction.guild, self.db, queue)
        if file is None:
            await interaction.followup.send(
                f"No players registered in {QUEUE_LABELS[queue]}.",
                ephemeral=True,
            )
            return
        await interaction.followup.send(file=file, view=view, ephemeral=ephemeral)

    # ── /resetelo ──────────────────────────────────────────────
    @app_commands.command(
        name="resetelo",
        description=f"Reset a player's ELO (or everyone's) to {ELO_START} in a queue",
    )
    @app_commands.describe(
        queue="Queue type",
        player="The player to reset to the initial value",
        all_players=f"Reset every player's ELO in this queue to {ELO_START}",
    )
    @app_commands.choices(queue=_QUEUE_CHOICES)
    @app_commands.rename(all_players="all")
    async def resetelo(
        self,
        interaction: discord.Interaction,
        queue: str,
        player: discord.Member = None,
        all_players: bool = False,
    ):
        if not _has_access(interaction, self.db):
            await interaction.response.send_message("You don't have permission.", ephemeral=True)
            return
        col = repository.get_elo_col(self.db)
        if all_players:
            count = col.count_documents({"queue_type": queue})
            col.update_many(
                {"queue_type": queue},
                {"$set": {"elo": ELO_START, "wins": 0, "losses": 0}},
            )
            embed = discord.Embed(
                title=f"🔄 Global reset - {QUEUE_LABELS[queue]}!",
                description=f"ELO of **{count} player(s)** reset to {ELO_START} in the {QUEUE_LABELS[queue]}.",
                color=0xE74C3C,
                timestamp=datetime.now(UTC),
            )
            embed.set_footer(text=f"Reset by {interaction.user.display_name}")
            await interaction.response.send_message(embed=embed)
            await _refresh_leaderboard_safe(interaction.guild, self.db, queue)
            return
        if player is None:
            await interaction.response.send_message(
                "Mention a player or use `all:True`.", ephemeral=True
            )
            return
        doc = _get_player(col, player, queue)
        old = doc["elo"]
        col.update_one(
            {"_id": repository.player_doc_id(player.id, queue)},
            {"$set": {"elo": ELO_START, "wins": 0, "losses": 0}},
        )
        embed = discord.Embed(
            title=f"🔄 {QUEUE_LABELS[queue]} ELO reset!",
            color=0x95A5A6,
            timestamp=datetime.now(UTC),
        )
        embed.add_field(name="Player", value=player.mention, inline=True)
        embed.add_field(name="Old ELO", value=str(old), inline=True)
        embed.add_field(name="New ELO", value=str(ELO_START), inline=True)
        embed.set_thumbnail(url=player.display_avatar.url)
        embed.set_footer(text=f"Reset by {interaction.user.display_name}")
        await interaction.response.send_message(embed=embed)
        await _refresh_leaderboard_safe(interaction.guild, self.db, queue)

    # ── /reset-queue ───────────────────────────────────────────
    @app_commands.command(
        name="reset-queue", description="Drop all data of a queue (admin)"
    )
    @app_commands.describe(queue="Queue type to reset")
    @app_commands.choices(queue=_QUEUE_CHOICES)
    @app_commands.checks.has_permissions(manage_guild=True)
    async def reset_queue(self, interaction: discord.Interaction, queue: str):
        view = _ResetQueueConfirmView(queue_type=queue)
        embed = discord.Embed(
            title=f"⚠️ Reset {QUEUE_LABELS[queue]}",
            description=(
                f"This action will **permanently delete**:\n"
                f"- All ELO of the {QUEUE_LABELS[queue]}\n"
                f"- The match history of the {QUEUE_LABELS[queue]}\n"
                f"- The leaderboard state of the {QUEUE_LABELS[queue]}\n\n"
                f"Other queues are not affected. **Confirm?**"
            ),
            color=0xE74C3C,
        )
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
        await view.wait()
        if not view.confirmed:
            await interaction.followup.send(
                "Reset cancelled (timeout or not confirmed).",
                ephemeral=True,
            )
            return

        elo_col = repository.get_elo_col(self.db)
        elo_col.delete_many({"queue_type": queue})
        repository.delete_active_queue(self.db, interaction.guild_id, queue)
        matches_col = repository.get_matches_col(self.db)
        matches_col.delete_many({"queue_type": queue})
        repository.clear_leaderboard_message_id(self.db, interaction.guild_id, queue)

        # Re-post the queue message in the correct channel
        queue_cog = self.bot.get_cog("QueueCog")
        target_name = QUEUE_CHANNEL_FOR_TYPE[queue]
        target_chan = discord.utils.get(interaction.guild.text_channels, name=target_name)
        if queue_cog and target_chan:
            try:
                await queue_cog.post_queue_message(target_chan, queue)  # type: ignore[attr-defined]
            except Exception:
                logger.exception("[reset-queue] re-post queue raised")

        await _refresh_leaderboard_safe(interaction.guild, self.db, queue)

        audit = discord.Embed(
            title=f"🔄 {QUEUE_LABELS[queue]} reset",
            description=f"Reset performed by {interaction.user.mention}",
            color=0x2ECC71,
            timestamp=datetime.now(UTC),
        )
        try:
            await interaction.channel.send(embed=audit)
        except Exception:
            logger.exception("[reset-queue] audit log raised")
        await interaction.followup.send(
            f"✅ {QUEUE_LABELS[queue]} reset.",
            ephemeral=True,
        )

    @reset_queue.error
    async def _reset_queue_perm_error(self, inter: discord.Interaction, error):
        if isinstance(error, app_commands.MissingPermissions):
            await inter.response.send_message(
                "🚫 Reserved for administrators.",
                ephemeral=True,
            )

    # ── /elomodify ─────────────────────────────────────────────
    @app_commands.command(
        name="elomodify", description="Add or remove ELO from a player in a queue"
    )
    @app_commands.describe(
        queue="Queue type",
        player="The player",
        action="Add or remove",
        amount="Amount of ELO",
    )
    @app_commands.choices(
        queue=_QUEUE_CHOICES,
        action=[
            app_commands.Choice(name="+ Add", value="add"),
            app_commands.Choice(name="- Remove", value="remove"),
        ],
    )
    async def elomodify(
        self,
        interaction: discord.Interaction,
        queue: str,
        player: discord.Member,
        action: str,
        amount: int,
    ):
        if not _has_access(interaction, self.db):
            await interaction.response.send_message("You don't have permission.", ephemeral=True)
            return
        if amount <= 0:
            await interaction.response.send_message(
                "❌ The amount must be strictly positive. Use the `- Remove` action to take away ELO.",
                ephemeral=True,
            )
            return
        col = repository.get_elo_col(self.db)
        _get_player(col, player, queue)
        delta = amount if action == "add" else -amount
        old_doc = col.find_one_and_update(
            {"_id": repository.player_doc_id(player.id, queue)},
            [{"$set": {"elo": {"$max": [0, {"$add": [{"$ifNull": ["$elo", 0]}, delta]}]}}}],
            return_document=ReturnDocument.BEFORE,
        )
        old = (old_doc or {}).get("elo", 0)
        new = max(0, old + delta)
        if action == "add":
            color = 0x2ECC71
            label = f"+{amount}"
            title = f"➕ {QUEUE_LABELS[queue]} ELO added"
        else:
            color = 0xE74C3C
            label = f"-{amount}"
            title = f"➖ {QUEUE_LABELS[queue]} ELO removed"
        embed = discord.Embed(title=title, color=color, timestamp=datetime.now(UTC))
        embed.add_field(name="Player", value=player.mention, inline=True)
        embed.add_field(name="Change", value=label, inline=True)
        embed.add_field(name="New ELO", value=f"**{new}** (was {old})", inline=True)
        embed.set_footer(text=f"By {interaction.user.display_name}")
        await interaction.response.send_message(embed=embed)
        await _refresh_leaderboard_safe(interaction.guild, self.db, queue)

    # ── /winmodify ─────────────────────────────────────────────
    @app_commands.command(
        name="winmodify", description="Add or remove wins from a player in a queue"
    )
    @app_commands.describe(
        queue="Queue type",
        player="The player",
        action="Add or remove",
        amount="Number of wins",
    )
    @app_commands.choices(
        queue=_QUEUE_CHOICES,
        action=[
            app_commands.Choice(name="+ Add", value="add"),
            app_commands.Choice(name="- Remove", value="remove"),
        ],
    )
    async def winmodify(
        self,
        interaction: discord.Interaction,
        queue: str,
        player: discord.Member,
        action: str,
        amount: int,
    ):
        if not _has_access(interaction, self.db):
            await interaction.response.send_message("You don't have permission.", ephemeral=True)
            return
        if amount <= 0:
            await interaction.response.send_message(
                "❌ The amount must be strictly positive.", ephemeral=True
            )
            return
        col = repository.get_elo_col(self.db)
        _get_player(col, player, queue)
        delta = amount if action == "add" else -amount
        old_doc = col.find_one_and_update(
            {"_id": repository.player_doc_id(player.id, queue)},
            [{"$set": {"wins": {"$max": [0, {"$add": [{"$ifNull": ["$wins", 0]}, delta]}]}}}],
            return_document=ReturnDocument.BEFORE,
        )
        old = (old_doc or {}).get("wins", 0)
        new = max(0, old + delta)
        if action == "add":
            color = 0x2ECC71
            label = f"+{amount}"
            title = f"➕ {QUEUE_LABELS[queue]} wins added"
        else:
            color = 0xE74C3C
            label = f"-{amount}"
            title = f"➖ {QUEUE_LABELS[queue]} wins removed"
        embed = discord.Embed(title=title, color=color, timestamp=datetime.now(UTC))
        embed.add_field(name="Player", value=player.mention, inline=True)
        embed.add_field(name="Change", value=label, inline=True)
        embed.add_field(name="New total", value=f"**{new}** (was {old})", inline=True)
        embed.set_footer(text=f"By {interaction.user.display_name}")
        await interaction.response.send_message(embed=embed)
        await _refresh_leaderboard_safe(interaction.guild, self.db, queue)

    # ── /losemodify ────────────────────────────────────────────
    @app_commands.command(
        name="losemodify", description="Add or remove losses from a player in a queue"
    )
    @app_commands.describe(
        queue="Queue type",
        player="The player",
        action="Add or remove",
        amount="Number of losses",
    )
    @app_commands.choices(
        queue=_QUEUE_CHOICES,
        action=[
            app_commands.Choice(name="+ Add", value="add"),
            app_commands.Choice(name="- Remove", value="remove"),
        ],
    )
    async def losemodify(
        self,
        interaction: discord.Interaction,
        queue: str,
        player: discord.Member,
        action: str,
        amount: int,
    ):
        if not _has_access(interaction, self.db):
            await interaction.response.send_message("You don't have permission.", ephemeral=True)
            return
        if amount <= 0:
            await interaction.response.send_message(
                "❌ The amount must be strictly positive.", ephemeral=True
            )
            return
        col = repository.get_elo_col(self.db)
        _get_player(col, player, queue)
        delta = amount if action == "add" else -amount
        old_doc = col.find_one_and_update(
            {"_id": repository.player_doc_id(player.id, queue)},
            [{"$set": {"losses": {"$max": [0, {"$add": [{"$ifNull": ["$losses", 0]}, delta]}]}}}],
            return_document=ReturnDocument.BEFORE,
        )
        old = (old_doc or {}).get("losses", 0)
        new = max(0, old + delta)
        if action == "add":
            color = 0xE74C3C
            label = f"+{amount}"
            title = f"➕ {QUEUE_LABELS[queue]} losses added"
        else:
            color = 0x2ECC71
            label = f"-{amount}"
            title = f"➖ {QUEUE_LABELS[queue]} losses removed"
        embed = discord.Embed(title=title, color=color, timestamp=datetime.now(UTC))
        embed.add_field(name="Player", value=player.mention, inline=True)
        embed.add_field(name="Change", value=label, inline=True)
        embed.add_field(name="New total", value=f"**{new}** (was {old})", inline=True)
        embed.set_footer(text=f"By {interaction.user.display_name}")
        await interaction.response.send_message(embed=embed)
        await _refresh_leaderboard_safe(interaction.guild, self.db, queue)

    # ── /stats ─────────────────────────────────────────────────
    @app_commands.command(
        name="stats", description="Show a player's ELO stats in a queue"
    )
    @app_commands.describe(queue="Queue type", player="The player whose stats you want to see")
    @app_commands.choices(queue=_QUEUE_CHOICES)
    async def stats(
        self, interaction: discord.Interaction, queue: str, player: discord.Member = None
    ):
        if player is None:
            player = interaction.user
        col = repository.get_elo_col(self.db)
        doc_id = repository.player_doc_id(player.id, queue)
        doc = col.find_one({"_id": doc_id})
        if not doc:
            await interaction.response.send_message(
                f"{player.display_name} hasn't played yet in {QUEUE_LABELS[queue]}.",
                ephemeral=True,
            )
            return
        elo = doc["elo"]
        wins = doc.get("wins", 0)
        losses = doc.get("losses", 0)
        total = wins + losses
        winrate = round((wins / total) * 100, 1) if total > 0 else 0
        rank = (
            col.count_documents(
                {
                    "queue_type": queue,
                    "$or": [
                        {"elo": {"$gt": elo}},
                        {"elo": elo, "wins": {"$gt": wins}},
                        {"elo": elo, "wins": wins, "_id": {"$lt": doc_id}},
                    ],
                }
            )
            + 1
        )
        embed = discord.Embed(
            title=f"📊 {QUEUE_LABELS[queue]} stats for {player.display_name}",
            color=0x3498DB,
            timestamp=datetime.now(UTC),
        )
        embed.set_thumbnail(url=player.display_avatar.url)
        embed.add_field(name="🏅 ELO", value=f"**{elo}**", inline=True)
        embed.add_field(name="🏆 Rank", value=f"**#{rank}**", inline=True)
        embed.add_field(name="📈 Winrate", value=f"**{winrate}%**", inline=True)
        embed.add_field(name="✅ Wins", value=f"**{wins}**", inline=True)
        embed.add_field(name="❌ Losses", value=f"**{losses}**", inline=True)
        embed.add_field(name="🎮 Games", value=f"**{total}**", inline=True)
        embed.set_footer(text=interaction.guild.name)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ── /inactivity ────────────────────────────────────────────
    @app_commands.command(
        name="inactivity",
        description="Show the most inactive players of a queue",
    )
    @app_commands.describe(queue="Queue type")
    @app_commands.choices(queue=_QUEUE_CHOICES)
    async def inactivity(self, interaction: discord.Interaction, queue: str):
        if not _has_access(interaction, self.db):
            await interaction.response.send_message("You don't have permission.", ephemeral=True)
            return

        col = repository.get_elo_col(self.db)
        docs = list(col.find({"queue_type": queue}))
        ranked = rank_by_inactivity(docs, limit=DEFAULT_INACTIVITY_LIMIT)

        if not ranked:
            await interaction.response.send_message(
                f"No players in the {QUEUE_LABELS[queue]}.", ephemeral=True
            )
            return

        now = datetime.now(UTC)
        lines = []
        for rank, doc in enumerate(ranked, start=1):
            user_id = doc.get("user_id") or str(doc["_id"]).rsplit(":", 1)[0]
            duration = format_inactivity(doc.get("last_played"), now)
            lines.append(f"`{rank:>2}.` <@{user_id}> - {duration}")

        embed = discord.Embed(
            title=f"Inactivity - {QUEUE_LABELS[queue]}",
            description="\n".join(lines),
            color=discord.Color.orange(),
            timestamp=now,
        )
        embed.set_footer(text=f"Top {len(ranked)} most inactive players")
        await interaction.response.send_message(
            embed=embed,
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )


async def setup(bot: commands.Bot, db) -> None:
    await bot.add_cog(ELOAdminCog(bot, db))
