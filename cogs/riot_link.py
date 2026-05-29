"""
V2 cog: Discord account <-> Riot account linking.

Commands:
  /link-riot riot_id:Username#TAG     (region forced to EU)
  /unlink-riot

No gate-keeping: rank verification for new members is done manually
when they enter the Discord server.

The Riot link only persists the Riot account metadata (PUUID,
username, tag) to enable post-match verification via the HenrikDev
API. No ELO is seeded: players start at `ELO_START` (=2000) when
they first appear in a given queue.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime

import discord
from discord import app_commands
from discord.ext import commands
from pymongo.errors import DuplicateKeyError

from services import repository
from services.riot_api import (
    HenrikDevClient,
    PlayerNotFoundError,
    RateLimitedError,
    RiotApiError,
)
from services.riot_id import parse_riot_id

logger = logging.getLogger(__name__)


# Server reserved to EU
DEFAULT_REGION = "eu"


class RiotLinkCog(commands.Cog):
    def __init__(self, bot: commands.Bot, db, riot_client: HenrikDevClient) -> None:
        self.bot = bot
        self.db = db
        self.riot_client = riot_client

    # ── /link-riot ────────────────────────────────────────────────
    @app_commands.command(
        name="link-riot", description="Link your Discord account to your Riot account (EU)"
    )
    @app_commands.describe(
        riot_id="Your Riot ID in Username#TAG format (e.g. Player#EUW)",
    )
    async def link_riot(
        self,
        interaction: discord.Interaction,
        riot_id: str,
    ) -> None:
        region = DEFAULT_REGION
        # 1) Parse riot_id
        try:
            name, tag = parse_riot_id(riot_id)
        except ValueError as e:
            await interaction.response.send_message(f"❌ {e}", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        # 2) Verify the Riot account's existence + fetch current rank (display).
        # HenrikDev calls are synchronous (`requests`) and would block the
        # Discord event loop for ~10s if the API is slow. We run them in a
        # thread to preserve the bot's responsiveness.
        try:
            account = await asyncio.to_thread(self.riot_client.get_account, name, tag)
            mmr = await asyncio.to_thread(self.riot_client.get_current_mmr, region, name, tag)
        except PlayerNotFoundError:
            await interaction.followup.send(
                f"❌ Player **{name}#{tag}** not found.", ephemeral=True
            )
            return
        except RateLimitedError:
            await interaction.followup.send(
                "⏳ HenrikDev API rate-limited, try again in 1 minute.", ephemeral=True
            )
            return
        except RiotApiError as e:
            # Do not leak the raw API response (potentially contains
            # internal details or HTML error excerpts). We log on the
            # server side and return a generic message to the user.
            logger.error(
                f"[link-riot] RiotApiError for user={interaction.user.id}: {e!r}", exc_info=True
            )
            await interaction.followup.send(
                "❌ Temporary Riot API error. Try again in a few moments.",
                ephemeral=True,
            )
            return

        # 2.5) PUUID dedup: a Riot account can only be linked to a single
        # Discord account per server. Without this check, a player could
        # hold 2 spots in queue with a single in-game account via two
        # Discord accounts linked to the same PUUID.
        existing = await asyncio.to_thread(
            repository.find_riot_account_by_puuid,
            self.db,
            account.puuid,
        )
        if existing is not None and str(existing.get("_id")) != str(interaction.user.id):
            await interaction.followup.send(
                f"❌ The Riot account **{name}#{tag}** is already linked to another "
                "member of the server. A Riot account can only be linked to "
                "a single Discord account per server.",
                ephemeral=True,
            )
            return

        # 3) Persist the Riot metadata (used for queue gate-keeping and
        # post-match verification via HenrikDev). No ELO seed: each
        # queue's ELO starts at ELO_START on the first match.
        # DuplicateKeyError: race condition with another Discord linking
        # the same PUUID in parallel. The unique index on puuid protects
        # the data - we return the same friendly message as the dedup
        # check above.
        try:
            repository.link_riot_account(
                self.db,
                user_id=interaction.user.id,
                riot_name=name,
                riot_tag=tag,
                riot_region=region,
                puuid=account.puuid,
                peak_elo=0,
                source="link_base",
            )
        except DuplicateKeyError:
            await interaction.followup.send(
                f"❌ The Riot account **{name}#{tag}** is already linked to another "
                "member of the server. A Riot account can only be linked to "
                "a single Discord account per server.",
                ephemeral=True,
            )
            return

        # 5) Confirmation embed
        embed = discord.Embed(
            title="🎯 Riot account linked!",
            color=0x2ECC71,
            timestamp=datetime.now(UTC),
        )
        embed.add_field(name="Riot ID", value=f"**{name}#{tag}**", inline=True)
        embed.add_field(name="Region", value=region.upper(), inline=True)
        embed.add_field(name="Current rank", value=mmr.tier_name, inline=True)
        embed.set_footer(text=f"Discord: {interaction.user.display_name}")
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ── /unlink-riot ──────────────────────────────────────────────
    @app_commands.command(name="unlink-riot", description="Remove the link to your Riot account")
    async def unlink_riot(self, interaction: discord.Interaction) -> None:
        ok = repository.unlink_riot_account(
            self.db,
            interaction.user.id,
        )
        if ok:
            await interaction.response.send_message("✅ Riot account unlinked.", ephemeral=True)
        else:
            await interaction.response.send_message("ℹ️ No Riot account linked.", ephemeral=True)


async def setup(bot: commands.Bot, db, riot_client: HenrikDevClient) -> None:
    await bot.add_cog(RiotLinkCog(bot, db, riot_client))
