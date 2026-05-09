"""
Helper pour rafraichir automatiquement le leaderboard apres une modification
d'ELO. Supprime le dernier message leaderboard du bot dans le salon
`#leaderboard` puis poste une nouvelle image generee a partir des donnees
courantes de la base.

Utilise par :
  - cogs/match.py (apres application de l'ELO post-match)
  - bot.py /elomodify, /resetelo (slash + prefix)
"""

from __future__ import annotations

import logging

import asyncio
from collections import OrderedDict
from datetime import datetime, timezone
from typing import Optional, Tuple

import discord

from leaderboard_img import generate_leaderboard
from services import repository

logger = logging.getLogger(__name__)


LEADERBOARD_CHANNEL_NAME = "leaderboard"
LEADERBOARD_FILENAME     = "leaderboard.png"
PAGE_SIZE                = 15

# Debounce per-guild : evite de regenerer + reposter le leaderboard
# en rafale apres N modifs ELO consecutives (ex: /win + /lose + autres
# admin ops). Discord rate-limit le delete+send (~5/min/channel).
_REFRESH_DEBOUNCE_SECONDS: int = 30
# Borne le cache pour eviter une fuite memoire si le bot tourne sur de
# nombreuses guilds (entree par guild_id, jamais purgee). LRU avec
# eviction FIFO du plus ancien acces au-dela de _MAX_GUILDS_TRACKED.
_MAX_GUILDS_TRACKED: int = 1024
_LAST_REFRESH_AT: "OrderedDict[tuple[int, str], datetime]" = OrderedDict()


async def build_leaderboard_payload(
    guild: discord.Guild, db, queue_type: str, *,
    with_view: bool = True,
    view_timeout: float | None = 300,
) -> Tuple[Optional[discord.File], Optional[discord.ui.View]]:
    """Genere file/view pour le leaderboard du queue_type donne."""
    repository._check_queue_type(queue_type)
    col  = repository.get_elo_col(db, guild.id)
    docs = list(col.find({"queue_type": queue_type})
                  .sort([("elo", -1), ("wins", -1), ("_id", 1)]))
    if not docs:
        return None, None

    all_players = []
    rank = 1
    for doc in docs:
        uid = doc.get("user_id") or doc["_id"].split(":")[0]
        try:
            member = guild.get_member(int(uid))
        except (TypeError, ValueError):
            member = None
        if member is None:
            continue
        ava_url = str(member.display_avatar.replace(format="png", size=64).url)
        display_name = member.display_name or doc.get("name", uid)
        all_players.append({
            "rank":       rank,
            "name":       display_name,
            "elo":        doc["elo"],
            "wins":       doc.get("wins", 0),
            "losses":     doc.get("losses", 0),
            "kills":      doc.get("kills", 0),
            "deaths":     doc.get("deaths", 0),
            "avatar_url": ava_url,
        })
        rank += 1

    if not all_players:
        return None, None

    total_pages = max(1, (len(all_players) + PAGE_SIZE - 1) // PAGE_SIZE)
    loop = asyncio.get_running_loop()

    async def build_page(page: int) -> discord.File:
        start = page * PAGE_SIZE
        chunk = all_players[start:start + PAGE_SIZE]
        # Le titre du leaderboard inclut le queue_type pour distinguer les
        # 3 leaderboards qui cohabitent dans #leaderboard.
        title = f"Leaderboard {queue_type.upper()} Queue"
        buf   = await loop.run_in_executor(
            None,
            lambda: generate_leaderboard(chunk, server_name=f"{guild.name} - {title}"),
        )
        return discord.File(buf, filename=f"leaderboard_{queue_type}.png")

    class LeaderboardView(discord.ui.View):
        def __init__(self, page: int):
            super().__init__(timeout=view_timeout)
            self.page = page
            self.update_buttons()

        def update_buttons(self):
            self.prev_btn.disabled = self.page == 0
            self.next_btn.disabled = self.page >= total_pages - 1
            self.page_btn.label    = f"Page {self.page + 1} / {total_pages}"

        async def _go(self, inter, new_page):
            if new_page < 0 or new_page >= total_pages:
                if not inter.response.is_done():
                    await inter.response.defer()
                return
            self.page = new_page
            self.update_buttons()
            try:
                if not inter.response.is_done():
                    await inter.response.defer()
                file = await build_page(self.page)
                await inter.followup.edit_message(
                    message_id=inter.message.id,
                    attachments=[file], view=self,
                )
            except Exception:
                logger.exception("leaderboard_refresh exception")

        @discord.ui.button(emoji="◀️", style=discord.ButtonStyle.secondary)
        async def prev_btn(self, inter, button):
            await self._go(inter, self.page - 1)

        @discord.ui.button(label="Page 1 / 1", style=discord.ButtonStyle.grey, disabled=True)
        async def page_btn(self, inter, button):
            pass

        @discord.ui.button(emoji="▶️", style=discord.ButtonStyle.secondary)
        async def next_btn(self, inter, button):
            await self._go(inter, self.page + 1)

    file = await build_page(0)
    if not with_view:
        return file, None
    return file, LeaderboardView(page=0)


async def refresh_leaderboard_channel(
    guild: discord.Guild, db, bot_user_id: int, queue_type: str,
) -> None:
    """Refresh le leaderboard du queue_type donne dans #leaderboard.

    Per-queue debounce : une rafale Pro ne bloque pas un refresh Open."""
    repository._check_queue_type(queue_type)
    now = datetime.now(timezone.utc)
    key = (guild.id, queue_type)
    last = _LAST_REFRESH_AT.get(key)
    if last is not None and (now - last).total_seconds() < _REFRESH_DEBOUNCE_SECONDS:
        _LAST_REFRESH_AT.move_to_end(key)
        return
    _LAST_REFRESH_AT[key] = now
    _LAST_REFRESH_AT.move_to_end(key)
    while len(_LAST_REFRESH_AT) > _MAX_GUILDS_TRACKED:
        _LAST_REFRESH_AT.popitem(last=False)

    needle = LEADERBOARD_CHANNEL_NAME.lower()
    chan = next(
        (c for c in guild.text_channels if needle in (c.name or "").lower()),
        None,
    )
    if chan is None:
        return

    stored_id = repository.get_leaderboard_message_id(db, guild.id, queue_type)
    if stored_id is not None:
        try:
            old_msg = await chan.fetch_message(stored_id)
            await old_msg.delete()
        except discord.NotFound:
            repository.clear_leaderboard_message_id(db, guild.id, queue_type)
        except Exception:
            logger.exception("leaderboard_refresh exception")

    # NO fallback history scan : avec 3 LBs qui cohabitent dans #leaderboard,
    # on ne peut pas identifier "lequel des 3" sans le state persiste. Si
    # aucun stored_id n'existe, on poste juste le nouveau message.

    try:
        file, view = await build_leaderboard_payload(
            guild, db, queue_type, view_timeout=None,
        )
    except Exception:
        logger.exception("leaderboard_refresh exception")
        return
    if file is None:
        return

    try:
        new_msg = await chan.send(file=file, view=view)
    except Exception:
        logger.exception("leaderboard_refresh exception")
        return

    try:
        repository.set_leaderboard_message_id(db, guild.id, queue_type, new_msg.id)
    except Exception:
        logger.exception("leaderboard_refresh exception")
