"""Cog : leaderboard Pro Queue avec reset hebdomadaire.

Reset chaque Lundi 00:00 Europe/Paris :
  - vide la collection `elo_weekly`
  - invalide le cache de pages weekly
  - refresh le message persistant dans `#leaderboard-weekly` pour
    chaque guild ou le bot est present
  - memorise l'instant du reset dans `weekly_meta._id = last_weekly_reset`
    pour eviter les double-resets entre ticks de la loop.

La loop tourne toutes les minutes : a chaque tick on calcule "maintenant"
en Europe/Paris, on derive le dernier Lundi 00:00 Paris precedent, et on
declenche le reset si on n'a pas encore franchi ce seuil.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import discord
from discord.ext import commands, tasks

from services import repository
from services.leaderboard_refresh import (
    _cache_invalidate,
    refresh_leaderboard_channel,
)


logger = logging.getLogger(__name__)

PARIS_TZ = ZoneInfo("Europe/Paris")


def _last_weekly_boundary(now_paris: datetime) -> datetime:
    """Dernier Lundi 00:00 Europe/Paris <= now_paris.

    `now_paris` doit etre tz-aware en Europe/Paris.
    """
    monday_00 = now_paris.replace(hour=0, minute=0, second=0, microsecond=0)
    days_since_monday = monday_00.weekday()  # Mon=0
    return monday_00 - timedelta(days=days_since_monday)


class LeaderboardWeeklyCog(commands.Cog):
    def __init__(self, bot: commands.Bot, db) -> None:
        self.bot = bot
        self.db = db

    async def cog_load(self) -> None:
        self._reset_loop.start()

    async def cog_unload(self) -> None:
        self._reset_loop.cancel()

    @tasks.loop(minutes=1)
    async def _reset_loop(self) -> None:
        try:
            await self._maybe_reset()
        except Exception:
            logger.exception("[leaderboard_weekly] _maybe_reset a leve")

    @_reset_loop.before_loop
    async def _before_loop(self) -> None:
        await self.bot.wait_until_ready()

    @_reset_loop.error
    async def _reset_loop_error(self, error: BaseException) -> None:
        logger.error(
            "[leaderboard_weekly] _reset_loop a leve : %r",
            error,
            exc_info=(type(error), error, error.__traceback__),
        )
        try:
            self._reset_loop.restart()
        except Exception:
            logger.exception("[leaderboard_weekly] _reset_loop.restart() a leve")

    async def _maybe_reset(self) -> None:
        now_paris = datetime.now(PARIS_TZ)
        boundary = _last_weekly_boundary(now_paris)
        last_reset = repository.get_last_weekly_reset(self.db)
        # Comparaison cross-timezone OK : Python compare deux datetime
        # tz-aware par leur instant UTC equivalent.
        if last_reset is not None and last_reset >= boundary:
            return

        deleted = repository.reset_weekly_elo(self.db)
        repository.set_last_weekly_reset(self.db, boundary)
        logger.info(
            "[leaderboard_weekly] reset effectue : %d docs supprimes, boundary=%s",
            deleted,
            boundary.isoformat(),
        )

        # Refresh des messages persistants weekly dans chaque guild.
        for guild in self.bot.guilds:
            _cache_invalidate(guild.id, "pro", weekly=True)
            try:
                await refresh_leaderboard_channel(
                    guild, self.db, "pro", weekly=True
                )
            except Exception:
                logger.exception(
                    "[leaderboard_weekly] refresh weekly a leve (guild=%s)",
                    guild.id,
                )


async def setup(bot: commands.Bot, db) -> LeaderboardWeeklyCog:
    cog = LeaderboardWeeklyCog(bot, db)
    await bot.add_cog(cog)
    return cog
