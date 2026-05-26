"""Cog reglement : commande /rules + bouton d'acceptation persistant.

Pose un message permanent affichant le reglement avec un bouton « J'accepte ».
Les joueurs doivent accepter pour rejoindre la Pro Queue (gate dans
cogs/queue_v2.py). L'acceptation est stockee globalement par joueur dans la
collection `rules` (cf. services/repository.py). Le bouton a un custom_id fixe
pour survivre aux redemarrages, enregistre via bot.add_view dans setup().
"""

from __future__ import annotations

import asyncio
import logging

import discord
from discord import app_commands
from discord.ext import commands

from services import repository

logger = logging.getLogger(__name__)


RULES_TITLE = "📜 Règlement"

RULES_LINES: tuple[str, ...] = (
    "Pas de type in game",
    "Pas d'insulte en vocal / à l'écrit sur le Discord et in game",
    "Tbag autorisé dans la mesure du raisonnable",
    "Pas de troll in game, on est là pour jouer sérieusement",
)

RULES_RECLAMATIONS = (
    "Pour toutes plaintes / réclamations vous pouvez ouvrir un ticket dans #tickets-reports"
)


def build_rules_embed() -> discord.Embed:
    """Construit l'embed du reglement (lignes + bloc reclamations)."""
    description = "\n".join(f"• {line}" for line in RULES_LINES)
    embed = discord.Embed(
        title=RULES_TITLE,
        description=description,
        color=0x5865F2,
    )
    embed.add_field(name="Réclamations", value=RULES_RECLAMATIONS, inline=False)
    embed.set_footer(text="Clique sur « J'accepte » pour pouvoir rejoindre la Pro Queue.")
    return embed


class RulesView(discord.ui.View):
    """View persistante : un bouton « J'accepte » qui enregistre l'acceptation.

    Le bouton est cree manuellement avec un custom_id fixe (`rules:accept`)
    pour survivre aux redemarrages, comme QueueView."""

    def __init__(self, db) -> None:
        super().__init__(timeout=None)
        self.db = db
        accept: discord.ui.Button = discord.ui.Button(
            label="J'accepte le règlement",
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
            logger.exception("[rules] echec enregistrement acceptation")
            await inter.response.send_message(
                "❌ Une erreur est survenue, réessaie dans un instant.",
                ephemeral=True,
            )
            return
        await inter.response.send_message(
            "✅ Règlement accepté, tu peux rejoindre la Pro Queue.",
            ephemeral=True,
        )


class RulesCog(commands.Cog):
    def __init__(self, bot: commands.Bot, db) -> None:
        self.bot = bot
        self.db = db
        self.rules_view = RulesView(db)

    @app_commands.command(
        name="rules",
        description="Poste le reglement avec un bouton d'acceptation dans ce salon",
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    async def rules(self, interaction: discord.Interaction) -> None:
        if interaction.channel is None:
            await interaction.response.send_message(
                "❌ Salon introuvable, relance la commande dans un salon texte.",
                ephemeral=True,
            )
            return
        await self.post_rules_message(interaction.channel)
        await interaction.response.send_message(
            f"✅ Règlement posté dans {interaction.channel.mention}.",
            ephemeral=True,
        )

    async def post_rules_message(self, channel: discord.abc.Messageable) -> None:
        """Pose le message permanent (embed + RulesView) dans `channel`."""
        await channel.send(embed=build_rules_embed(), view=self.rules_view)

    @rules.error
    async def _rules_perm_error(
        self, inter: discord.Interaction, error: app_commands.AppCommandError
    ):
        if isinstance(error, app_commands.MissingPermissions):
            await inter.response.send_message(
                "🚫 Réservé aux administrateurs.",
                ephemeral=True,
            )


async def setup(bot: commands.Bot, db) -> None:
    cog = RulesCog(bot, db)
    await bot.add_cog(cog)
    # Enregistre la view pour qu'elle persiste apres restart.
    bot.add_view(cog.rules_view)
