"""Rules cog: /rules command + persistent acceptance button.

Posts a permanent message displaying the rules with an "I accept" button.
Players must accept to join the Pro Queue (gated in cogs/queue_v2.py).
Acceptance is stored globally per player in the `rules` collection
(cf. services/repository.py). The button has a fixed custom_id so it
survives restarts, registered via bot.add_view in setup().
"""

from __future__ import annotations

import asyncio
import logging

import discord
from discord import app_commands
from discord.ext import commands

from services import repository

logger = logging.getLogger(__name__)


RULES_TITLE = "📜 Rules"

RULES_LINES: tuple[str, ...] = (
    "No typing in game",
    "No insults in voice / text on Discord and in game",
    "Tbagging allowed within reason",
    "No trolling in game, we are here to play seriously",
)

RULES_RECLAMATIONS = "For any complaints / claims you can open a ticket in #tickets-reports"


def build_rules_embed() -> discord.Embed:
    """Builds the rules embed (lines + complaints block)."""
    description = "\n".join(f"• {line}" for line in RULES_LINES)
    embed = discord.Embed(
        title=RULES_TITLE,
        description=description,
        color=0x5865F2,
    )
    embed.add_field(name="Complaints", value=RULES_RECLAMATIONS, inline=False)
    embed.set_footer(text="Click « I accept » to be able to join the Pro Queue.")
    return embed


class RulesView(discord.ui.View):
    """Persistent view: an « I accept » button that records the acceptance.

    The button is created manually with a fixed custom_id (`rules:accept`)
    so it survives restarts, like QueueView."""

    def __init__(self, db) -> None:
        super().__init__(timeout=None)
        self.db = db
        accept: discord.ui.Button = discord.ui.Button(
            label="I accept the rules",
            style=discord.ButtonStyle.success,
            custom_id="rules:accept",
        )
        accept.callback = self._accept_callback
        self.accept_btn = accept
        self.add_item(accept)

    async def _accept_callback(self, inter: discord.Interaction) -> None:
        try:
            await asyncio.to_thread(
                repository.record_rules_acceptance,
                self.db,
                inter.user.id,
                display_name=getattr(inter.user, "display_name", str(inter.user.id)),
            )
        except Exception:
            logger.exception("[rules] failed to save acceptance")
            await inter.response.send_message(
                "❌ An error occurred, please try again in a moment.",
                ephemeral=True,
            )
            return
        await inter.response.send_message(
            "✅ Rules accepted, you can join the Pro Queue.",
            ephemeral=True,
        )


class RulesCog(commands.Cog):
    def __init__(self, bot: commands.Bot, db) -> None:
        self.bot = bot
        self.db = db
        self.rules_view = RulesView(db)

    @app_commands.command(
        name="rules",
        description="Posts the rules with an acceptance button in this channel",
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    async def rules(self, interaction: discord.Interaction) -> None:
        if interaction.channel is None:
            await interaction.response.send_message(
                "❌ Channel not found, please run the command in a text channel.",
                ephemeral=True,
            )
            return
        await self.post_rules_message(interaction.channel)
        await interaction.response.send_message(
            f"✅ Rules posted in {interaction.channel.mention}.",
            ephemeral=True,
        )

    async def post_rules_message(self, channel: discord.abc.Messageable) -> None:
        """Posts the permanent message (embed + RulesView) in `channel`."""
        await channel.send(embed=build_rules_embed(), view=self.rules_view)

    @rules.error
    async def _rules_perm_error(
        self, inter: discord.Interaction, error: app_commands.AppCommandError
    ):
        if isinstance(error, app_commands.MissingPermissions):
            await inter.response.send_message(
                "🚫 Reserved for administrators.",
                ephemeral=True,
            )


async def setup(bot: commands.Bot, db) -> None:
    cog = RulesCog(bot, db)
    await bot.add_cog(cog)
    # Register the view so it persists after restart.
    bot.add_view(cog.rules_view)
