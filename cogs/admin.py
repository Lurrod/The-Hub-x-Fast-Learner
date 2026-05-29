"""
Admin cog: utility commands (/setup, /bypass, /map, /coinflip,
/clear, /help). Extracted from bot.py (monolith refactor).

`/setup` creates the category + the channels and posts the 4 queue
messages by delegating to QueueCog.post_queue_message and
refresh_leaderboard_channel.
"""

from __future__ import annotations

import logging
import random
from datetime import UTC, datetime

import discord
from discord import app_commands
from discord.ext import commands

from services import elo_calc, repository
from services.leaderboard_refresh import refresh_leaderboard_channel

logger = logging.getLogger(__name__)


SETUP_CATEGORY_NAME = "🎮 Valorant 10mans"
# 4 queue channels + 1 shared leaderboard + 1 matches.
SETUP_CHANNELS = [
    "leaderboard",
    "pro-queue",
    "semi-pro-queue",
    "open-queue",
    "gc-queue",
    "matches",
]
# Mapping queue_type -> channel name to post the persistent message in.
QUEUE_CHANNEL_FOR_TYPE = {
    "pro": "pro-queue",
    "semipro": "semi-pro-queue",
    "open": "open-queue",
    "gc": "gc-queue",
}


def _has_access(interaction: discord.Interaction, db) -> bool:
    """Admin (manage_guild) OR bypass role configured via /bypass."""
    if interaction.user.guild_permissions.manage_guild:
        return True
    role_id = repository.get_bypass_role(db, interaction.guild_id)
    return bool(role_id and any(r.id == role_id for r in interaction.user.roles))


class AdminCog(commands.Cog):
    def __init__(self, bot: commands.Bot, db) -> None:
        self.bot = bot
        self.db = db

    # ── /setup ─────────────────────────────────────────────────
    @app_commands.command(
        name="setup", description="Create the category and channels required by the bot"
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    async def setup_bot(self, interaction: discord.Interaction):
        guild = interaction.guild
        await interaction.response.defer(ephemeral=True)

        # 1) Category
        category = discord.utils.get(guild.categories, name=SETUP_CATEGORY_NAME)
        if category is None:
            try:
                category = await guild.create_category(SETUP_CATEGORY_NAME)
            except discord.Forbidden:
                await interaction.followup.send(
                    "❌ The bot does not have the **Manage Channels** permission.",
                    ephemeral=True,
                )
                return

        # 2) Channels
        created: list[str] = []
        existed: list[str] = []
        for name in SETUP_CHANNELS:
            chan = discord.utils.get(guild.text_channels, name=name)
            if chan is None:
                try:
                    await guild.create_text_channel(name, category=category)
                    created.append(name)
                except discord.Forbidden:
                    await interaction.followup.send(
                        f"❌ Could not create `#{name}` (missing permissions).",
                        ephemeral=True,
                    )
                    return
            else:
                existed.append(name)

        # 3) Post the persistent message of each queue in its dedicated channel
        queue_cog = self.bot.get_cog("QueueCog")
        queue_status: list[str] = []
        if queue_cog is not None:
            for qt in repository.QUEUE_TYPES:
                channel_name = QUEUE_CHANNEL_FOR_TYPE[qt]
                chan = discord.utils.get(guild.text_channels, name=channel_name)
                if chan is None:
                    queue_status.append(f"⚠️ Channel `#{channel_name}` not found.")
                    continue
                repository.delete_active_queue(self.db, guild.id, qt)
                try:
                    await queue_cog.post_queue_message(chan, qt)  # type: ignore[attr-defined]
                    queue_status.append(f"🎯 {qt.upper()} queue posted in {chan.mention}")
                except discord.Forbidden:
                    queue_status.append(
                        f"⚠️ Could not send in {chan.mention} (permissions)"
                    )

        # 4) Pre-post the leaderboards (silently skip if 0 players)
        for qt in repository.QUEUE_TYPES:
            try:
                await refresh_leaderboard_channel(guild, self.db, qt)
            except Exception:
                logger.exception("[setup] pre-post leaderboard %s raised", qt)

        # 4b) Pre-post the weekly Pro leaderboard (#leaderboard-weekly channel)
        try:
            await refresh_leaderboard_channel(guild, self.db, "pro", weekly=True)
        except Exception:
            logger.exception("[setup] pre-post weekly leaderboard raised")

        # 5) Recap
        lines: list[str] = []
        if created:
            lines.append(f"✅ Created: {', '.join(f'`#{c}`' for c in created)}")
        if existed:
            lines.append(f"ℹ️ Already present: {', '.join(f'`#{c}`' for c in existed)}")
        lines.extend(queue_status)
        if not lines:
            lines.append("✅ Setup complete.")
        await interaction.followup.send("\n".join(lines), ephemeral=True)

    @setup_bot.error
    async def _setup_perm_error(self, inter: discord.Interaction, error):
        if isinstance(error, app_commands.MissingPermissions):
            await inter.response.send_message(
                "🚫 Reserved for administrators.",
                ephemeral=True,
            )

    # ── /bypass ────────────────────────────────────────────────
    @app_commands.command(
        name="bypass", description="Grants access to all bot commands to a role"
    )
    @app_commands.describe(role="The role that will get access to all commands")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def bypass(self, interaction: discord.Interaction, role: discord.Role):
        if role.id == interaction.guild_id or role.is_default():
            await interaction.response.send_message(
                "❌ Cannot grant bypass to @everyone - that would give admin access to the whole server.",
                ephemeral=True,
            )
            return
        if role.managed:
            await interaction.response.send_message(
                "❌ Cannot grant bypass to a role managed by an integration (bot, booster, etc.).",
                ephemeral=True,
            )
            return
        repository.set_bypass_role(self.db, interaction.guild_id, role.id)
        embed = discord.Embed(
            title="🔓 Bypass enabled!",
            description=f"The role {role.mention} now has access to all bot commands.",
            color=0xE67E22,
            timestamp=datetime.now(UTC),
        )
        embed.set_footer(text=f"Configured by {interaction.user.display_name}")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @bypass.error
    async def _bypass_error(self, interaction: discord.Interaction, error):
        if isinstance(error, app_commands.MissingPermissions):
            await interaction.response.send_message(
                "Only administrators can configure the bypass.", ephemeral=True
            )

    # ── /map ───────────────────────────────────────────────────
    @app_commands.command(name="map", description="Pick a random map for the game")
    async def map_pick(self, interaction: discord.Interaction):
        if not _has_access(interaction, self.db):
            await interaction.response.send_message(
                "🚫 You do not have permission to use this command.", ephemeral=True
            )
            return
        chosen = random.choice(elo_calc.MAPS)
        embed = discord.Embed(
            title="🗺️ Map selected!",
            description=f"## {chosen}",
            color=0x9B59B6,
            timestamp=datetime.now(UTC),
        )
        embed.set_footer(text=f"Pulled by {interaction.user.display_name}")
        await interaction.response.send_message(embed=embed)

    # ── /coinflip ──────────────────────────────────────────────
    @app_commands.command(name="coinflip", description="Flip a coin")
    async def coinflip(self, interaction: discord.Interaction):
        result = random.choice(["Heads", "Tails"])
        embed = discord.Embed(
            title="🪙 Heads or Tails!",
            description=f"## {result}",
            color=0xF1C40F,
            timestamp=datetime.now(UTC),
        )
        embed.set_footer(text=f"Flipped by {interaction.user.display_name}")
        await interaction.response.send_message(embed=embed)

    # ── /clear ─────────────────────────────────────────────────
    @app_commands.command(name="clear", description="Delete a number of messages in the channel")
    @app_commands.describe(amount="Number of messages to delete (max 100)")
    async def clear(self, interaction: discord.Interaction, amount: int):
        if not _has_access(interaction, self.db):
            await interaction.response.send_message("No permission.", ephemeral=True)
            return
        if amount < 1 or amount > 100:
            await interaction.response.send_message(
                "The number must be between 1 and 100.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True)
        deleted = await interaction.channel.purge(limit=amount)
        embed = discord.Embed(
            title="🗑️ Messages deleted",
            description=f"**{len(deleted)}** message(s) deleted.",
            color=0xE74C3C,
            timestamp=datetime.now(UTC),
        )
        embed.set_footer(text=f"By {interaction.user.display_name}")
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ── /help ──────────────────────────────────────────────────
    @app_commands.command(name="help", description="Display the list of available commands")
    @app_commands.describe(kind="Choose the help type")
    @app_commands.choices(
        kind=[
            app_commands.Choice(name="Member commands", value="members"),
            app_commands.Choice(name="Admin commands", value="admin"),
        ]
    )
    @app_commands.rename(kind="type")
    async def help_cmd(self, interaction: discord.Interaction, kind: str = "members"):
        if kind == "admin":
            if not _has_access(interaction, self.db):
                await interaction.response.send_message("No permission.", ephemeral=True)
                return
            embed = discord.Embed(
                title="⚙️ Admin commands", color=0xE74C3C, timestamp=datetime.now(UTC)
            )
            embed.add_field(
                name="/setup",
                value="Create the category + 4 queue channels (`pro-queue`, `semi-pro-queue`, `open-queue`, `gc-queue`) + `leaderboard` + `matches` and post the 4 queue messages",
                inline=False,
            )
            embed.add_field(
                name="/setup-queue queue",
                value="Re-post the persistent message of a queue (pro/semipro/open/gc)",
                inline=False,
            )
            embed.add_field(
                name="/close-queue queue", value="Close the active queue of a type", inline=False
            )
            embed.add_field(
                name="/win queue @p1..@p5",
                value="Win - Pro Queue: flat ±16; SemiPro/Open/GC: weighted by position",
                inline=False,
            )
            embed.add_field(
                name="/lose queue @p1..@p5",
                value="Loss - Pro Queue: flat ±16; SemiPro/Open/GC: weighted by position",
                inline=False,
            )
            embed.add_field(name="/map", value="Random map", inline=False)
            embed.add_field(
                name="/elomodify queue @p action amount",
                value="Add or remove ELO from a player in a queue",
                inline=False,
            )
            embed.add_field(
                name="/winmodify queue @p action amount",
                value="Add or remove wins",
                inline=False,
            )
            embed.add_field(
                name="/losemodify queue @p action amount",
                value="Add or remove losses",
                inline=False,
            )
            embed.add_field(
                name="/resetelo queue [@player|all]",
                value=f"Reset a player's ELO (or all) to {elo_calc.ELO_START} in a queue",
                inline=False,
            )
            embed.add_field(
                name="/reset-queue queue",
                value="Full drop of a queue (ELO + matches + leaderboard) - confirmation required",
                inline=False,
            )
            embed.add_field(
                name="/bypass @role",
                value="Grants access to admin commands to a role",
                inline=False,
            )
            embed.add_field(name="/clear amount", value="Delete messages", inline=False)
            embed.set_footer(text=f"Requested by {interaction.user.display_name}")
            await interaction.response.send_message(embed=embed, ephemeral=True)
        else:
            embed = discord.Embed(
                title="📖 Available commands", color=0x3498DB, timestamp=datetime.now(UTC)
            )
            embed.add_field(
                name="/leaderboard queue",
                value="ELO ranking of a queue (pro/semipro/open/gc)",
                inline=False,
            )
            embed.add_field(
                name="/stats queue [@player]",
                value="Player stats in a queue. Without a mention = your own stats",
                inline=False,
            )
            embed.add_field(name="/help", value="Display this help", inline=False)
            embed.set_footer(text=f"Requested by {interaction.user.display_name}")
            await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot, db) -> None:
    await bot.add_cog(AdminCog(bot, db))
