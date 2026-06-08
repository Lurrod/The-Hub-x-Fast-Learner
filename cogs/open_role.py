"""Open-role cog: /open command + persistent "Get FL OPEN" button.

Posts a permanent message with a button that grants the "FL OPEN" role,
giving members access to the Open queue (gated in cogs/queue_v2.py) without
going through the application flow. The button has a fixed custom_id so it
survives restarts, registered via bot.add_view in setup().
"""

from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands

logger = logging.getLogger(__name__)

# Role granted by the button. Must match the role used by the Open queue
# (cf. cogs/queue_v2.py and cogs/applications.py).
OPEN_ROLE_NAME = "FL OPEN"

OPEN_TITLE = "🎮 Open Queue Access"
OPEN_DESCRIPTION = (
    "Click the button below to get the **FL OPEN** role and unlock the "
    "Open queue. No application required."
)


def build_open_embed() -> discord.Embed:
    """Builds the embed shown above the "Get FL OPEN" button."""
    embed = discord.Embed(
        title=OPEN_TITLE,
        description=OPEN_DESCRIPTION,
        color=0x57F287,
    )
    embed.set_footer(text="Click « Get access » to receive the FL OPEN role.")
    return embed


class OpenRoleView(discord.ui.View):
    """Persistent view: a button that grants the "FL OPEN" role.

    The button is created manually with a fixed custom_id (`open:grant`)
    so it survives restarts, like RulesView."""

    def __init__(self) -> None:
        super().__init__(timeout=None)
        grant: discord.ui.Button = discord.ui.Button(
            label="Get access",
            style=discord.ButtonStyle.success,
            custom_id="open:grant",
        )
        grant.callback = self._grant_callback
        self.grant_btn = grant
        self.add_item(grant)

    async def _grant_callback(self, inter: discord.Interaction) -> None:
        member = inter.user
        if inter.guild is None or not isinstance(member, discord.Member):
            await inter.response.send_message(
                "❌ This button can only be used inside the server.",
                ephemeral=True,
            )
            return

        # Idempotent: clicking again when already granted just acknowledges.
        if any(getattr(r, "name", None) == OPEN_ROLE_NAME for r in member.roles):
            await inter.response.send_message(
                f"✅ You already have the `{OPEN_ROLE_NAME}` role.",
                ephemeral=True,
            )
            return

        role = discord.utils.get(inter.guild.roles, name=OPEN_ROLE_NAME)
        if role is None:
            logger.warning(
                "[open] role %r missing from guild %s; cannot grant",
                OPEN_ROLE_NAME,
                inter.guild_id,
            )
            await inter.response.send_message(
                f"❌ The `{OPEN_ROLE_NAME}` role is missing from this server. "
                f"Contact an admin.",
                ephemeral=True,
            )
            return

        try:
            await member.add_roles(role, reason="Opted into the Open queue")
        except Exception:
            logger.exception("[open] FL OPEN grant failed")
            await inter.response.send_message(
                "❌ Something went wrong while granting the role. Try again later.",
                ephemeral=True,
            )
            return

        await inter.response.send_message(
            f"✅ You now have the `{OPEN_ROLE_NAME}` role and can join the Open queue.",
            ephemeral=True,
        )


class OpenRoleCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.open_view = OpenRoleView()

    @app_commands.command(
        name="open",
        description="Posts a button that grants the FL OPEN role in this channel",
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    async def open_cmd(self, interaction: discord.Interaction) -> None:
        if interaction.channel is None:
            await interaction.response.send_message(
                "❌ Channel not found, please run the command in a text channel.",
                ephemeral=True,
            )
            return
        await self.post_open_message(interaction.channel)
        await interaction.response.send_message(
            f"✅ Open-queue button posted in {interaction.channel.mention}.",
            ephemeral=True,
        )

    async def post_open_message(self, channel: discord.abc.Messageable) -> None:
        """Posts the permanent message (embed + OpenRoleView) in `channel`."""
        await channel.send(embed=build_open_embed(), view=self.open_view)

    @open_cmd.error
    async def _open_perm_error(
        self, inter: discord.Interaction, error: app_commands.AppCommandError
    ):
        if isinstance(error, app_commands.MissingPermissions):
            await inter.response.send_message(
                "🚫 Reserved for administrators.",
                ephemeral=True,
            )


async def setup(bot: commands.Bot, db) -> None:
    cog = OpenRoleCog(bot)
    await bot.add_cog(cog)
    # Register the view so it persists after restart.
    bot.add_view(cog.open_view)
