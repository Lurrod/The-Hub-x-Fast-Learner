"""MatchCog - orchestrator of the match flow.

Remains a large cog (~1300 lines) because the match transitions (formation,
vote, Henrik verification, cleanups) share `self` state (db,
henrik_client, circuit breaker, role-edit semaphore). Splitting into
multiple mixins would add reverse coupling without gaining readability.

Splitting the *module* into sub-files (`_constants`, `_embeds`,
`_vote`) does however extract the purely functional blocks from the cog.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

import discord
from bson import ObjectId
from bson.errors import InvalidId
from discord import app_commands
from discord.ext import commands, tasks

from cogs.match._constants import (
    ADMIN_ROLE_NAMES,
    CONTESTED_EXPIRY_HOURS,
    HENRIK_CIRCUIT_FAIL_THRESHOLD,
    HENRIK_CIRCUIT_OPEN_MINUTES,
    HENRIK_VERIFY_DELAY_MINUTES,
    HENRIK_VERIFY_TIMEOUT_MINUTES,
    MAJORITY_THRESHOLD,
    MATCH_HOST_ROLE_NAME,
    MATCH_HUB_SPECTATOR_ROLE_NAMES,
    MATCH_SPECTATOR_ROLE_NAMES,
    MATCH_VIEWER_ROLE_NAMES,
    MAX_REPLACE_ELO_DIFF,
    RESULTS_CHANNELS,
    VOTE_TIMEOUT_MINUTES,
)
from cogs.match._embeds import (
    build_elo_changes_embed,
    build_match_embed,
)
from cogs.match._vote import VoteView
from cogs.queue_v2 import (
    QUEUE_CHANNEL_NAMES,
    QUEUE_ROLE_NAME,
)
from services import elo_calc, repository
from services.captain_draft import (
    CaptainDraftSession,
    DraftCancelledError,
    pick_captains,
)
from services.elo_updater import (
    apply_match_validation,
)
from services.leaderboard_refresh import refresh_leaderboard_channel
from services.map_pick_ban import (
    MapBanCancelledError,
    MapBanSession,
)
from services.match_category import (
    cleanup_orphan_match_categories,
    create_match_category,
    delete_match_category,
)
from services.match_service import (
    build_plan_from_draft,
    build_players,
    plan_match,
    serialize_team,
)
from services.match_verifier import (
    build_extended_stats,
    find_henrik_custom_match,
    ratings_by_uid,
)
from services.repository import reserve_match_number
from services.riot_api import HenrikDevClient
from services.rating import RatingInputs, compute_rating_2_0
from services.scoreboard_img import generate_scoreboard

# Inlined to avoid cross-cog import (cogs.queue_v2 -> match), would
# introduce a cycle. Kept in sync manually with cogs.queue_v2.QUEUE_LABELS.
_QUEUE_LABEL_BY_TYPE: dict[str, str] = {
    "pro": "Pro Queue",
    "semipro": "Semi Pro Queue",
    "open": "Open Queue",
    "gc": "GC Queue",
}


logger = logging.getLogger(__name__)


class MatchCog(commands.Cog):
    def __init__(
        self,
        bot: commands.Bot,
        db,
        *,
        rng: random.Random | None = None,
        henrik_client: HenrikDevClient | None = None,
    ) -> None:
        self.bot = bot
        self.db = db
        self.rng = rng or random.Random()
        self.henrik_client = henrik_client
        self.vote_view = VoteView(db, on_validated=self._on_match_validated)
        # Henrik circuit breaker: suspends calls after N consecutive failures.
        # `_henrik_lock` serializes counter/open-state transitions when
        # several verifications run in parallel (asyncio.gather over guilds).
        self._henrik_consecutive_failures: int = 0
        self._henrik_circuit_open_until: datetime | None = None
        self._henrik_lock: asyncio.Lock = asyncio.Lock()
        # Safeguard for Discord rate limits on role/voice operations.
        # Discord caps the per-guild bucket (PATCH /members/{u}) at ~10/10s;
        # we cap at 5 concurrent calls to never saturate (match formation
        # = 10 simultaneous players, otherwise 429 + ~9s retry).
        self._guild_member_edit_sem: asyncio.Semaphore = asyncio.Semaphore(5)

    # ── Queue-full hook ──────────────────────────────────────────
    async def on_queue_full(
        self,
        interaction: discord.Interaction,
        queue_doc: dict,
        queue_type: str = "open",
    ):
        guild = interaction.guild
        player_ids = [str(uid) for uid in queue_doc.get("players", [])]

        # Batch 2 Mongo queries instead of 20 (N+1): we fetch the 10 Riot
        # accounts and the 10 ELO docs in a single query each.
        # All Mongo ops are grouped in a single thread to avoid freezing
        # the event loop during match formation.
        elo_col = repository.get_elo_col(self.db)
        riot_col = repository.get_riot_col(self.db)

        def _batch_fetch() -> tuple[dict[str, dict], dict[str, int]]:
            riot_map: dict[str, dict] = {}
            elo_map: dict[str, int] = {}
            for doc in riot_col.find({"_id": {"$in": player_ids}}):
                riot_map[str(doc["_id"])] = dict(doc)
            # Compound _id: map of "uid:queue_type" -> elo. We store by
            # bare uid so that `build_players` stays pure (bare uid key).
            compound_ids = [repository.player_doc_id(uid, queue_type) for uid in player_ids]
            for doc in elo_col.find({"_id": {"$in": compound_ids}}):
                uid = str(doc["_id"]).split(":", 1)[0]
                elo_map[uid] = int(doc.get("elo", elo_calc.ELO_START))
            return riot_map, elo_map

        riot_accounts, bot_elos = await asyncio.to_thread(_batch_fetch)

        # Players without an ELO doc yet (first match, or post-reset):
        # default to ELO_START instead of 0. `build_players` reads these
        # via `bot_elos.get(uid, 0)`; we therefore fill the fallback
        # explicitly here to keep the function pure.
        for uid in player_ids:
            bot_elos.setdefault(uid, elo_calc.ELO_START)

        member_names: dict[str, str] = {}
        for uid in player_ids:
            member = guild.get_member(int(uid))
            if member:
                member_names[uid] = member.display_name

        players = build_players(player_ids, riot_accounts, member_names, bot_elos)
        if len(players) < 10:
            await self._fail(
                interaction,
                queue_doc,
                "Player(s) without a linked Riot account. Match cancelled.",
                queue_type=queue_type,
            )
            return None

        # Origin channel of the queue (to repost setup-queue afterwards).
        queue_channel = guild.get_channel(int(queue_doc["channel_id"]))
        if queue_channel is None:
            await self._fail(
                interaction,
                queue_doc,
                "Queue channel not found.",
                queue_type=queue_type,
            )
            return None

        # Reserve an atomic match number + dynamically create the Discord category.
        match_number = reserve_match_number(self.db, guild_id=guild.id)
        try:
            channels = await create_match_category(
                guild=guild,
                match_number=match_number,
                player_ids=[p.id for p in players],
                admin_role_ids=self._admin_role_ids(guild),
                viewer_role_ids=self._viewer_role_ids(guild),
                spectator_role_ids=self._spectator_role_ids(guild),
                hub_spectator_role_ids=self._hub_spectator_role_ids(guild),
            )
        except Exception:
            logger.exception("[match] create_match_category failed for #%d", match_number)
            await interaction.followup.send(
                "Discord error while creating the match category. Please retry.",
                ephemeral=True,
            )
            return None
        category = channels.category
        prep_channel = channels.prep_channel
        free_cat_name = category.name

        # Persist a 'preparing' placeholder doc BEFORE draft/map-ban so:
        # - admins can /match-cancel during draft/ban (DB lookup by channel)
        # - startup orphan cleanup keeps the category (status is active)
        # - startup recovery detects bot restarts mid-draft and cleans up
        preparing_match_id = await asyncio.to_thread(
            repository.create_preparing_match,
            self.db,
            queue_type=queue_type,
            origin_guild_id=guild.id,
            match_number=match_number,
            category_id=category.id,
            channel_id=prep_channel.id,
            player_ids=[int(p.id) for p in players],
        )

        # Pro / Semi-Pro: captain draft + map ban. Open / GC: auto-balance + random map.
        if queue_type in ("pro", "semipro"):
            player_ids_for_move = [str(p.id) for p in players]
            await self._move_players_to_waiting_match(
                guild,
                category,
                player_ids_for_move,
            )
            cap_a, cap_b = pick_captains(players, rng=self.rng)
            pool = tuple(p for p in players if p.id not in (cap_a.id, cap_b.id))
            draft_session = CaptainDraftSession(
                prep_channel=prep_channel,
                cap_a=cap_a,
                cap_b=cap_b,
                pool=pool,
                admin_role_names=ADMIN_ROLE_NAMES,
            )
            try:
                draft_result = await draft_session.run()
            except DraftCancelledError as exc:
                logger.info(
                    "[match] draft cancelled (reason=%s actor=%s) - queue preserved",
                    exc.reason,
                    getattr(exc.actor, "id", None),
                )
                await asyncio.to_thread(
                    repository.cancel_preparing_match, self.db, preparing_match_id
                )
                with contextlib.suppress(discord.HTTPException):
                    await interaction.followup.send(
                        "❌ Draft cancelled. The queue stays active. "
                        "`/leave` then `/join` to reset if needed.",
                        ephemeral=False,
                    )
                try:
                    await delete_match_category(
                        guild=guild,
                        category_id=category.id,
                        reason=f"Match #{match_number} draft cancelled",
                    )
                except Exception:
                    logger.exception("[match] failed to delete category on draft cancel")
                return None
            except Exception:
                logger.exception(
                    "[match] captain draft failed for #%d, rolling back category",
                    match_number,
                )
                await asyncio.to_thread(
                    repository.cancel_preparing_match, self.db, preparing_match_id
                )
                await delete_match_category(
                    guild=guild,
                    category_id=category.id,
                    reason=f"Match #{match_number} draft aborted",
                )
                with contextlib.suppress(discord.HTTPException):
                    await interaction.followup.send(
                        f"❌ The draft for Match #{match_number} failed, match cancelled.",
                        ephemeral=True,
                    )
                return None

            ban_session = MapBanSession(
                prep_channel=prep_channel,
                cap_a=cap_a,
                cap_b=cap_b,
                maps=elo_calc.MAPS,
                admin_role_names=ADMIN_ROLE_NAMES,
            )
            try:
                ban_result = await ban_session.run()
            except MapBanCancelledError as exc:
                logger.info(
                    "[match] map ban cancelled (reason=%s actor=%s) - queue preserved",
                    exc.reason,
                    getattr(exc.actor, "id", None),
                )
                await asyncio.to_thread(
                    repository.cancel_preparing_match, self.db, preparing_match_id
                )
                with contextlib.suppress(discord.HTTPException):
                    await interaction.followup.send(
                        "❌ Map ban cancelled. The queue stays active. "
                        "`/leave` then `/join` to reset if needed.",
                        ephemeral=False,
                    )
                try:
                    await delete_match_category(
                        guild=guild,
                        category_id=category.id,
                        reason=f"Match #{match_number} map ban cancelled",
                    )
                except Exception:
                    logger.exception("[match] failed to delete category on map ban cancel")
                return None
            except Exception:
                logger.exception(
                    "[match] map ban failed for #%d, rolling back category",
                    match_number,
                )
                await asyncio.to_thread(
                    repository.cancel_preparing_match, self.db, preparing_match_id
                )
                await delete_match_category(
                    guild=guild,
                    category_id=category.id,
                    reason=f"Match #{match_number} map ban aborted",
                )
                with contextlib.suppress(discord.HTTPException):
                    await interaction.followup.send(
                        f"❌ The map ban for Match #{match_number} failed, match cancelled.",
                        ephemeral=True,
                    )
                return None

            plan = build_plan_from_draft(
                draft_result,
                free_category=free_cat_name,
                rng=self.rng,
                map_name=ban_result.selected_map,
            )
        else:
            plan = plan_match(players, free_category=free_cat_name, rng=self.rng)

        # Setup ordering: the match doc was already inserted with
        # status='preparing' before captain draft / map ban started, so
        # the channel is always resolvable from DB. Here we promote it
        # to 'pending' with the now-known teams/map (message_id is
        # filled in later, after the announcement is sent).
        match_id = preparing_match_id
        await asyncio.to_thread(
            repository.finalize_preparing_match,
            self.db,
            match_id,
            team_a=serialize_team(plan.teams.team_a),
            team_b=serialize_team(plan.teams.team_b),
            map_name=plan.map_name,
            lobby_leader_id=plan.lobby_leader.id,
            category_name=plan.category_name,
        )

        # Step 2: adjust roles BEFORE announcing. Best-effort: a crash
        # here leaves partial roles but the match doc exists ->
        # /match-cancel cleans up.
        #
        # Consolidated 1 PATCH/player via member.edit(roles=...): atomic
        # diff on Discord's side, eliminates the 429s observed in prod
        # (per-guild PATCH /members/{u} bucket ~10/10s).
        # Semaphore(5) as a safeguard.
        leader_id = int(plan.lobby_leader.id)

        async def _setup_roles_for(member: discord.Member) -> None:
            mg = member.guild
            queue_role = discord.utils.get(mg.roles, name=QUEUE_ROLE_NAME)
            host_role = (
                discord.utils.get(mg.roles, name=MATCH_HOST_ROLE_NAME)
                if member.id == leader_id
                else None
            )
            current = set(member.roles)
            target = set(current)
            if queue_role is not None:
                target.discard(queue_role)
            if host_role is not None:
                target.add(host_role)
            if target == current:
                return
            async with self._guild_member_edit_sem:
                with contextlib.suppress(discord.Forbidden, discord.HTTPException):
                    await member.edit(
                        roles=list(target),
                        reason="Match formed: role setup",
                    )

        role_members = [
            m for m in (guild.get_member(int(uid)) for uid in player_ids) if m is not None
        ]
        if not any(m.id == leader_id for m in role_members):
            leader_member = guild.get_member(leader_id)
            if leader_member is not None:
                role_members.append(leader_member)
        role_results = await asyncio.gather(
            *(_setup_roles_for(m) for m in role_members),
            return_exceptions=True,
        )
        for r in role_results:
            if isinstance(r, BaseException):
                logger.warning("[match] role setup failed: %r", r)

        # Step 3: send the announcement.
        mentions = " ".join(f"<@{p.id}>" for p in players)
        embed = build_match_embed(plan, guild.name, queue_type)
        try:
            msg = await prep_channel.send(
                content=f"🎯 Match found! {mentions}",
                embed=embed,
                view=self.vote_view,
            )
        except Exception:
            # The announcement failed: cancel the freshly created match
            # doc to avoid an orphan that nobody can vote on (no
            # message_id => VoteView cannot be found).
            logger.exception("[match] prep_channel.send raised, rolling back match doc")
            matches_col = repository.get_matches_col(self.db)
            await asyncio.to_thread(
                matches_col.delete_one,
                {"_id": match_id},
            )
            await self._fail(
                interaction,
                queue_doc,
                "Failed to send the match announcement. Match cancelled.",
                queue_type=queue_type,
            )
            return None

        # Step 4: associate the message_id with the match doc. Without
        # this, `get_match_by_message` (used by VoteView) cannot find
        # the match at vote time.
        matches_col = repository.get_matches_col(self.db)
        await asyncio.to_thread(
            matches_col.update_one,
            {"_id": match_id},
            {"$set": {"message_id": msg.id}},
        )

        # Step 5: empty the queue immediately after persistence.
        # Prevents any potential re-trigger of on_queue_full on the same queue.
        await asyncio.to_thread(
            repository.delete_active_queue,
            self.db,
            guild.id,
            queue_type,
        )

        # Step 6: voice move Waiting Room -> Team 1/Team 2 based on
        # the assignment computed by balance_teams. Players land
        # directly in their team VC, no need to re-split after the
        # Waiting Match gathering.
        await self._move_players_to_match_vc(guild, free_cat_name, plan)

        # Step 7: repost setup-queue (best-effort) in the destination
        # channel for this queue_type. We preserve the origin channel
        # (queue_doc.channel_id) if possible, otherwise we fall back on
        # the channel named QUEUE_CHANNEL_NAMES[queue_type].
        target_channel = queue_channel
        target_name = QUEUE_CHANNEL_NAMES.get(queue_type)
        if target_name and target_channel.name != target_name:
            named = discord.utils.get(guild.text_channels, name=target_name)
            if named is not None:
                target_channel = named
        queue_cog = self.bot.get_cog("QueueCog")
        if queue_cog is not None:
            try:
                await queue_cog.post_queue_message(target_channel, queue_type)  # type: ignore[attr-defined]
            except Exception:
                logger.exception("[match] failed to re-post setup-queue")
        return match_id

    def _admin_role_ids(self, guild: discord.Guild) -> list[int]:
        """Return the IDs of the admin/staff roles to include in the
        overwrites of the match category.

        Covers two sources:
          1. The roles named in `ADMIN_ROLE_NAMES` (project constant):
             allows custom moderators without Discord `administrator`
             permission to view/manage the dynamic match categories.
          2. The bypass role configured via /bypass (`bypass` collection
             in the DB, per guild). Used by servers that have a custom
             moderation role not listed in ADMIN_ROLE_NAMES.

        Without this method wired up, only users with the Discord
        `administrator` permission (which bypasses overwrites) see the
        categories -- which excludes custom staff.
        """
        # Manual iteration (not `discord.utils.get`): on mocked Guilds
        # in tests, `utils.get` may return a coroutine via the `_aget`
        # fallback which does not expose `.id`.
        admin_names: set[str] = set(ADMIN_ROLE_NAMES)
        ids: list[int] = []
        try:
            roles_iter = list(guild.roles)
        except TypeError:
            roles_iter = []
        for role in roles_iter:
            name = getattr(role, "name", None)
            role_id = getattr(role, "id", None)
            if isinstance(name, str) and name in admin_names and isinstance(role_id, int):
                ids.append(role_id)
        try:
            bypass_id = repository.get_bypass_role(self.db, guild.id)
        except Exception:  # pragma: no cover - guild.id missing/mock weirdness
            bypass_id = None
        if isinstance(bypass_id, int) and bypass_id not in ids:
            ids.append(bypass_id)
        return ids

    def _viewer_role_ids(self, guild: discord.Guild) -> list[int]:
        """Return the IDs of the "viewer" staff roles to include in the
        overwrites of the match category (player-level access, not admin).

        These roles (see MATCH_VIEWER_ROLE_NAMES) receive the same rights
        as the 10 players: view/send/connect/speak, without manage_channels.
        Useful so that staff (FAST LEARNER x The Hub, ADMINISTRATORS,
        FL STAFF PRO, FL STAFF SEMIPRO, FL STAFF GC) can follow/help on
        any match category without having admin powers (draft cancel,
        ping, channel management).
        """
        return self._role_ids_by_names(guild, MATCH_VIEWER_ROLE_NAMES)

    def _spectator_role_ids(self, guild: discord.Guild) -> list[int]:
        """Return the IDs of "spectator" roles (see MATCH_SPECTATOR_ROLE_NAMES,
        e.g. "Members"): they see the category + read history, but cannot
        join the voice channels or send messages.
        """
        return self._role_ids_by_names(guild, MATCH_SPECTATOR_ROLE_NAMES)

    def _hub_spectator_role_ids(self, guild: discord.Guild) -> list[int]:
        """Return the IDs of "hub spectator" roles (see
        MATCH_HUB_SPECTATOR_ROLE_NAMES, e.g. "FL HUB"): they see the
        match category and voice channels in the sidebar but cannot
        join voice nor read the prep text channel — that channel is
        hidden via a per-channel view_channel=False override. Players
        in the match keep full access via their member-level overwrite.
        """
        return self._role_ids_by_names(guild, MATCH_HUB_SPECTATOR_ROLE_NAMES)

    @staticmethod
    def _role_ids_by_names(guild: discord.Guild, names: tuple[str, ...]) -> list[int]:
        wanted: set[str] = set(names)
        ids: list[int] = []
        try:
            roles_iter = list(guild.roles)
        except TypeError:
            roles_iter = []
        for role in roles_iter:
            name = getattr(role, "name", None)
            role_id = getattr(role, "id", None)
            if isinstance(name, str) and name in wanted and isinstance(role_id, int):
                ids.append(role_id)
        return ids

    async def _move_players_to_match_vc(
        self,
        guild,
        free_cat_name: str,
        plan,
    ) -> None:
        """Move the 10 players into the team VC (`Team 1` / `Team 2`)
        of the assigned category, according to `plan.teams.team_a` /
        `team_b`. Silently skip players who are out of voice or already
        in place.

        Graceful fallback if a team VC is missing: fall back to the
        other one if available, otherwise to `Waiting Match`, otherwise
        no-op. All valid players have already been auto-moved into
        `Waiting Room` on clicking Join (see queue_v2._move_to_waiting_room).
        """
        category = discord.utils.get(guild.categories, name=free_cat_name)
        if category is None:
            return
        team1_vc = discord.utils.get(category.voice_channels, name="Team 1")
        team2_vc = discord.utils.get(category.voice_channels, name="Team 2")
        waiting_match = discord.utils.get(
            category.voice_channels,
            name="Waiting Match",
        )

        # uid -> target VC mapping. team_a -> Team 1, team_b -> Team 2.
        # If a team VC is missing, fall back to the other one then to
        # Waiting Match to guarantee the player is regrouped even in
        # degraded config.
        a_dest = team1_vc or team2_vc or waiting_match
        b_dest = team2_vc or team1_vc or waiting_match
        if a_dest is None and b_dest is None:
            return

        targets: dict[int, Any] = {}
        for player in plan.teams.team_a:
            if a_dest is not None:
                targets[int(player.id)] = a_dest
        for player in plan.teams.team_b:
            if b_dest is not None:
                targets[int(player.id)] = b_dest

        # Parallelization: per-member bucket, but we cap at 5 concurrent
        # via the semaphore shared with role edits so we never saturate
        # the Discord PATCH /members/{u} per-guild bucket (~10/10s).
        async def _move_one(uid: int, dest) -> None:
            member = guild.get_member(uid)
            if member is None:
                return
            voice = getattr(member, "voice", None)
            if voice is None or getattr(voice, "channel", None) is None:
                return
            if voice.channel.id == dest.id:
                return
            async with self._guild_member_edit_sem:
                with contextlib.suppress(discord.Forbidden, discord.HTTPException):
                    await member.move_to(
                        dest,
                        reason="Match formed: regrouping into team VC",
                    )

        await asyncio.gather(
            *(_move_one(uid, dest) for uid, dest in targets.items()),
            return_exceptions=True,
        )

    async def _move_players_to_waiting_match(
        self,
        guild,
        category,
        player_ids: list[str],
    ) -> None:
        """Move all `player_ids` to the 'Waiting Match' VC of `category`.

        Used on the Pro / Semi-Pro branch BEFORE the captain draft, so the
        10 players are grouped in one VC while captains pick their teams.

        Guards:
          - skip if guild.get_member returns None
          - skip if member is not in voice
          - skip if already at destination
        """
        waiting_match = discord.utils.get(category.voice_channels, name="Waiting Match")
        if waiting_match is None:
            logger.warning(
                "[match] _move_players_to_waiting_match: 'Waiting Match' not found in %s, no-op",
                category.name,
            )
            return

        async def _move_one(uid_str: str) -> None:
            try:
                uid = int(uid_str)
            except (TypeError, ValueError):
                return
            member = guild.get_member(uid)
            if member is None:
                return
            voice = getattr(member, "voice", None)
            if voice is None or voice.channel is None:
                return
            if voice.channel.id == waiting_match.id:
                return
            async with self._guild_member_edit_sem:
                with contextlib.suppress(discord.Forbidden, discord.HTTPException):
                    await member.move_to(
                        waiting_match,
                        reason="Pro/Semi-Pro Queue: grouping before captain draft",
                    )

        await asyncio.gather(
            *[_move_one(uid) for uid in player_ids],
            return_exceptions=True,
        )

    async def _fail(
        self,
        interaction,
        queue_doc,
        reason: str,
        queue_type: str = "open",
    ) -> None:
        repository.delete_active_queue(
            self.db,
            interaction.guild.id,
            queue_type,
        )
        channel = None
        try:
            channel = interaction.guild.get_channel(int(queue_doc["channel_id"]))
            if channel:
                await channel.send(
                    f"⚠️ {reason} A new queue has been reposted.",
                )
        except Exception:
            logger.exception("[match] _fail send raised")
        # Repost a fresh queue to avoid forcing the admin to redo
        # /setup-queue manually after every formation failure.
        if channel is not None:
            queue_cog = self.bot.get_cog("QueueCog")
            if queue_cog is not None:
                try:
                    await queue_cog.post_queue_message(channel, queue_type)  # type: ignore[attr-defined]
                except Exception:
                    logger.exception("[match] _fail re-post queue raised")

    # ── Hook: vote validated ─────────────────────────────────────
    async def _on_match_validated(self, inter, match_doc) -> None:
        """
        Vote validated: we do NOT touch ELO yet.
        ELO will be applied in a single pass by `_verify_match`
        after ~HENRIK_VERIFY_DELAY_MINUTES (with ACS weighting if
        HenrikDev found the custom, flat otherwise).
        """
        guild = getattr(inter, "guild", None)

        # Best-effort announcement.
        if guild is None:
            return

        # Deletion of the match's Discord category.
        # Graceful: legacy matches without category_id are ignored.
        category_id = match_doc.get("category_id")
        if category_id:
            repository.mark_match_cleanup_started(self.db, match_doc["_id"])
            await delete_match_category(
                guild=guild,
                category_id=category_id,
                reason=(f"Match #{match_doc.get('match_number', '?')} vote validated"),
            )

        try:
            elo_log_channel = discord.utils.get(
                guild.text_channels,
                name="elo-adding",
            )
        except Exception:
            logger.exception("[match] elo-adding lookup raised")
            return
        if elo_log_channel is None:
            return
        try:
            await elo_log_channel.send(
                f"⏳ Match validated ({match_doc.get('status')}). "
                f"HenrikDev verification starting in {HENRIK_VERIFY_DELAY_MINUTES} min "
                f"(retry every minute, give up at {HENRIK_VERIFY_TIMEOUT_MINUTES} min)."
            )
        except discord.Forbidden:
            # The bot does not have Send Messages permission in #elo-adding.
            # This is a config issue that the operator should pick up.
            logger.warning(
                "[match] Henrik announcement send denied (Forbidden) on #%s "
                "guild=%s - check the bot permissions.",
                elo_log_channel.name,
                guild.id,
            )
        except discord.HTTPException:
            # Transient Discord error (5xx, rate limit). We log but do
            # not fail the ELO flow.
            logger.exception("[match] Henrik announcement HTTP error")
        except Exception:
            logger.exception("[match] Henrik wait announcement send raised")

    # ── Vote timeouts ────────────────────────────────────────────
    async def check_vote_timeouts(self, *, now: datetime | None = None) -> int:
        """
        Scan every known guild. For each `pending` match created more
        than VOTE_TIMEOUT_MINUTES ago, mark it `contested` and ping
        the channel's admin role.

        Returns:
            number of matches moved to `contested` during this call.
        """
        now = now or datetime.now(UTC)
        cutoff = now - timedelta(minutes=VOTE_TIMEOUT_MINUTES)

        # Per-guild parallelization: with N guilds, sequential execution
        # would wait for each guild's scan + transitions to finish before
        # moving to the next. asyncio.gather makes the tick bounded by
        # the slowest guild, not by their sum.
        results = await asyncio.gather(
            *[self._check_vote_timeouts_for_guild(g, cutoff) for g in self.bot.guilds],
            return_exceptions=True,
        )
        flagged = 0
        for r in results:
            if isinstance(r, BaseException):
                logger.info(f"[match] check_vote_timeouts (guild) raised: {r!r}")
                continue
            flagged += r
        return flagged

    async def _check_vote_timeouts_for_guild(self, guild, cutoff: datetime) -> int:
        flagged = 0
        col = repository.get_matches_col(self.db)

        # Scan in a thread: `find().toList()` is blocking and can iterate
        # over N matches, freezing the Discord event loop.
        def _fetch_stale() -> list[Mapping[str, Any]]:
            return list(
                col.find(
                    {
                        "status": "pending",
                        "created_at": {"$lt": cutoff},
                        "origin_guild_id": guild.id,
                    }
                )
            )

        stale = await asyncio.to_thread(_fetch_stale)
        for match in stale:
            # Atomic re-fetch just before the transition to avoid a race
            # with a vote that would cross the threshold between the
            # initial scan and now. Without this re-fetch, we would read
            # `match.get("votes")` from the stale snapshot -> the tick
            # could transition pending->contested while a concurrent vote
            # just reached the majority, leaving the match stuck in
            # `contested` with ELO never applied.
            fresh = await asyncio.to_thread(col.find_one, {"_id": match["_id"]})
            if not fresh or fresh.get("status") != "pending":
                continue
            votes = fresh.get("votes", {})
            count_a = sum(1 for v in votes.values() if v == "a")
            count_b = sum(1 for v in votes.values() if v == "b")
            # Auto-repair: we backdate `validated_at` to the match's
            # `created_at`. Without this backdate, the Henrik delay
            # (~5 min after validated_at) would restart from 0; the
            # match was however already created > VOTE_TIMEOUT_MINUTES
            # ago, the HenrikDev custom is already indexed and the
            # verification can run immediately on the next tick.
            repaired_validated_at = fresh.get("created_at")
            if count_a >= MAJORITY_THRESHOLD:
                # A match may have reached 7+ votes without transitioning
                # (e.g. bot crash between vote write and set_match_status).
                # We catch up; check_henrik_verifications will apply
                # the ELO on the next tick.
                await asyncio.to_thread(
                    repository.transition_match_status,
                    self.db,
                    match["_id"],
                    from_status="pending",
                    to_status="validated_a",
                    validated_at=repaired_validated_at,
                )
                continue
            if count_b >= MAJORITY_THRESHOLD:
                await asyncio.to_thread(
                    repository.transition_match_status,
                    self.db,
                    match["_id"],
                    from_status="pending",
                    to_status="validated_b",
                    validated_at=repaired_validated_at,
                )
                continue
            transitioned = await asyncio.to_thread(
                repository.transition_match_status,
                self.db,
                match["_id"],
                from_status="pending",
                to_status="contested",
            )
            if transitioned is None:
                continue
            await self._handle_timeout(guild, match)
            flagged += 1
        return flagged

    async def _handle_timeout(self, guild, match) -> None:
        # Note: the transition to "contested" is done by
        # check_vote_timeouts via transition_match_status (atomic CAS).
        # We only enter here if the transition succeeded.

        # Revoke Match Host role; no longer governed by deferred cleanup.
        leader_id = match.get("lobby_leader_id")
        if leader_id is not None:
            leader_member = guild.get_member(int(leader_id))
            if leader_member is not None:
                host_role = discord.utils.get(guild.roles, name=MATCH_HOST_ROLE_NAME)
                if host_role is not None and host_role in leader_member.roles:
                    with contextlib.suppress(discord.Forbidden, discord.HTTPException):
                        await leader_member.remove_roles(
                            host_role, reason="Vote timeout: Match Host revoked"
                        )

        admin_role = None
        for role_name in ADMIN_ROLE_NAMES:
            admin_role = discord.utils.get(guild.roles, name=role_name)
            if admin_role:
                break

        channel_id = match.get("channel_id")
        if not channel_id:
            return
        channel = guild.get_channel(int(channel_id))
        if not channel:
            return

        ping = admin_role.mention if admin_role else "@admin"
        votes = match.get("votes", {})
        count_a = sum(1 for v in votes.values() if v == "a")
        count_b = sum(1 for v in votes.values() if v == "b")

        try:
            await channel.send(
                f"⏰ {ping} Match vote timed out (>{VOTE_TIMEOUT_MINUTES} min "
                f"without {MAJORITY_THRESHOLD}/10). Current score: Team A `{count_a}` / Team B `{count_b}`. "
                f"Manual validation required.",
            )
        except Exception:
            logger.exception("[match] _handle_timeout send raised")

    # ── HenrikDev verification + single ELO application ──────────
    async def check_henrik_verifications(self, *, now: datetime | None = None) -> int:
        """For each match validated > HENRIK_VERIFY_DELAY_MINUTES ago without
        Henrik verification:
          - look up the HenrikDev custom (ACS multipliers if found)
          - if Henrik finds it: apply weighted ELO (final)
          - if Henrik does not find it and we are under the timeout: we will retry
            on the next tick (1 min loop)
          - if we passed HENRIK_VERIFY_TIMEOUT_MINUTES: apply flat ELO
            and mark the match as verified (Henrik given up)
        Returns the number of matches processed."""
        now = now or datetime.now(UTC)
        start_cutoff = now - timedelta(minutes=HENRIK_VERIFY_DELAY_MINUTES)

        # Per-guild parallelization: same principle as check_vote_timeouts.
        results = await asyncio.gather(
            *[
                self._check_henrik_verifications_for_guild(g, start_cutoff, now=now)
                for g in self.bot.guilds
            ],
            return_exceptions=True,
        )
        processed = 0
        for r in results:
            if isinstance(r, BaseException):
                logger.info(f"[match] check_henrik_verifications (guild) raised: {r!r}")
                continue
            processed += r
        return processed

    async def _check_henrik_verifications_for_guild(
        self,
        guild,
        start_cutoff: datetime,
        *,
        now: datetime | None = None,
    ) -> int:
        processed = 0
        # Blocking scan -> thread to avoid freezing the event loop.
        stale = await asyncio.to_thread(
            repository.find_validated_unverified,
            self.db,
            start_cutoff,
            origin_guild_id=guild.id,
        )
        for match in stale:
            try:
                await self._verify_match(guild, match, now=now)
            except Exception:
                logger.exception("[match] verify_match raised")
            processed += 1
        return processed

    async def _verify_match(
        self,
        guild,
        match_doc: dict,
        *,
        now: datetime | None = None,
        force_apply: bool = False,
    ) -> None:
        """
        Verify the match against Henrik and apply ELO.

        Flow:
          1. Try Henrik first. If the custom is found, apply flat ELO,
             mark `henrik_verified=True, found=True`, post the scoreboard
             and persist extended stats.
          2. If Henrik did not find the custom AND we are still within
             HENRIK_VERIFY_TIMEOUT_MINUTES of `validated_at`, do nothing —
             the next tick (1 min loop) will retry. Henrik typically
             indexes customs 10-30 min after they end, so retrying is
             essential to give the scoreboard a chance.
          3. If Henrik never responded AND the timeout has elapsed (or
             `force_apply=True`), apply flat ELO and mark
             `henrik_verified=True, found=False` (no scoreboard).

        Idempotency: we **claim** the match (`elo_applied=True`) BEFORE
        applying the ELO. If the claim fails (already applied elsewhere),
        we skip. If the ELO application raises, we release the claim to
        allow a retry on the next tick.

        `force_apply=True` bypasses the retry-window check. Used by
        tests that want to assert the flat-fallback path without
        manipulating `validated_at`.
        """
        queue_type = match_doc.get("queue_type", "open")
        now = now or datetime.now(UTC)

        # Try Henrik BEFORE claiming the match. If Henrik has not indexed
        # the custom yet and we are still within the retry window, we
        # must leave the match doc untouched so the next tick retries.
        try:
            fetched = await self._fetch_henrik_match_summary(guild, match_doc)
        except Exception:
            logger.exception("[match] _fetch_henrik_match_summary raised")
            fetched = None

        if fetched is None and not force_apply:
            validated_at = match_doc.get("validated_at")
            if validated_at is not None:
                elapsed = now - validated_at
                if elapsed < timedelta(minutes=HENRIK_VERIFY_TIMEOUT_MINUTES):
                    # Within retry window — leave the match alone, the
                    # next 1-min tick will try Henrik again.
                    return

        # Atomic claim: only the first call goes through. Prevents double
        # application in case of a crash between apply_match_validation
        # and set_match_henrik_verified, or a concurrent tick.
        claimed = await asyncio.to_thread(
            repository.claim_match_for_elo,
            self.db,
            match_doc["_id"],
        )
        if claimed is None:
            return  # Already applied by a previous tick.

        # Pro-queue ELO is weighted by Rating 2.0. Build the per-player
        # ratings from the Henrik summary (already fetched above); other
        # queues — or matches without Henrik data — stay flat ±20.
        ratings = None
        if fetched is not None and match_doc.get("queue_type") == "pro":
            summary_for_ratings, ta_map, tb_map = fetched
            ratings = ratings_by_uid(
                summary_for_ratings, {**(ta_map or {}), **(tb_map or {})}
            )

        try:
            outcome = await asyncio.to_thread(
                apply_match_validation,
                self.db,
                match_doc,
                ratings=ratings,
            )
        except Exception:
            logger.exception("[match] apply_match_validation raised")
            # Roll back the claim to allow a retry on the next tick.
            await asyncio.to_thread(
                repository.release_elo_claim,
                self.db,
                match_doc["_id"],
            )
            return

        await asyncio.to_thread(
            repository.set_match_henrik_verified,
            self.db,
            match_doc["_id"],
            found=(fetched is not None),
            multipliers=None,
        )

        embed = build_elo_changes_embed(outcome, match_doc, guild.name)
        elo_log = discord.utils.get(guild.text_channels, name="elo-adding")
        if elo_log is not None:
            try:
                await elo_log.send(embed=embed)
            except Exception:
                logger.exception("[match] ELO recap send raised")

        try:
            await refresh_leaderboard_channel(guild, self.db, queue_type)
        except Exception:
            logger.exception("[match] leaderboard refresh raised")

        # Scoreboard + extended stats: only if Henrik responded.
        if fetched is not None:
            summary, team_a_uid_by_puuid, team_b_uid_by_puuid = fetched
            try:
                await self._post_match_scoreboard(
                    guild,
                    summary,
                    team_a_uid_by_puuid,
                    team_b_uid_by_puuid,
                    match_doc,
                    outcome,
                )
            except Exception:
                logger.exception("[match] _post_match_scoreboard raised")
            try:
                puuid_to_user_id: dict = {}
                puuid_to_user_id.update(team_a_uid_by_puuid or {})
                puuid_to_user_id.update(team_b_uid_by_puuid or {})
                extended = build_extended_stats(
                    summary,
                    puuid_to_user_id=puuid_to_user_id,
                    queue_type=match_doc.get("queue_type", "open"),
                )
                await self._persist_extended_stats(
                    match_id=str(match_doc["_id"]),
                    extended=extended,
                )
            except Exception:
                logger.exception("[match] _persist_extended_stats wrapper raised")

    async def _persist_extended_stats(
        self,
        *,
        match_id: str,
        extended,
    ) -> None:
        """Persist per-match Rating 2.0 stats + update aggregates.

        Best-effort: errors are logged but do NOT propagate (the ELO
        is already applied at this point — the match doc is the source
        of truth for ranking).
        """
        if not extended:
            return
        now = datetime.now(UTC)

        match_docs: list[dict] = []
        deltas: list[dict] = []
        for s in extended:
            match_docs.append(
                {
                    "_id": f"{match_id}:{s.user_id}",
                    "match_id": match_id,
                    "user_id": s.user_id,
                    "queue_type": s.queue_type,
                    "map": s.map_name,
                    "agent": s.agent,
                    "rounds_played": s.rounds_played,
                    "win": s.win,
                    "kills": s.kills,
                    "deaths": s.deaths,
                    "assists": s.assists,
                    "damage_made": s.damage_made,
                    "damage_received": s.damage_received,
                    "headshots": s.headshots,
                    "bodyshots": s.bodyshots,
                    "legshots": s.legshots,
                    "multikills_2k": s.multikills_2k,
                    "multikills_3k": s.multikills_3k,
                    "multikills_4k": s.multikills_4k,
                    "multikills_5k": s.multikills_5k,
                    "first_kills": s.first_kills,
                    "first_deaths": s.first_deaths,
                    "kast_rounds": s.kast_rounds,
                    "acs": s.acs,
                    "rating_2_0": s.rating_2_0,
                    "created_at": now,
                }
            )
            deltas.append(
                {
                    "user_id": s.user_id,
                    "queue_type": s.queue_type,
                    "games": 1,
                    "rounds_played": s.rounds_played,
                    "kills": s.kills,
                    "deaths": s.deaths,
                    "assists": s.assists,
                    "damage_made": s.damage_made,
                    "damage_received": s.damage_received,
                    "headshots": s.headshots,
                    "bodyshots": s.bodyshots,
                    "legshots": s.legshots,
                    "multikills_2k": s.multikills_2k,
                    "multikills_3k": s.multikills_3k,
                    "multikills_4k": s.multikills_4k,
                    "multikills_5k": s.multikills_5k,
                    "first_kills": s.first_kills,
                    "first_deaths": s.first_deaths,
                    "kast_rounds": s.kast_rounds,
                    "rating_2_0_sum": s.rating_2_0,
                }
            )

        try:
            inserted = await asyncio.to_thread(
                repository.insert_match_player_stats, self.db, match_docs
            )
        except Exception:
            logger.exception("[stats] insert_match_player_stats failed")
            return

        if inserted == 0:
            return

        try:
            await asyncio.to_thread(repository.update_rating_aggregates, self.db, deltas)
        except Exception:
            logger.exception("[stats] update_rating_aggregates failed")

    async def _fetch_henrik_match_summary(
        self,
        guild,
        match_doc: dict,
    ) -> tuple[Any, dict[str, str], dict[str, str]] | None:
        """Attempt to find the HenrikDev custom for `match_doc`. Returns
        (summary, team_a_uid_by_puuid, team_b_uid_by_puuid) or None if not
        usable. The team maps let the caller decide which Henrik side
        (Red/Blue) corresponds to Team A vs Team B.

        Best-effort: a None return means "no scoreboard for this match"
        (Henrik down, custom not found, riot accounts missing, circuit
        open). The flat ELO has already been applied upstream."""

        # 10 Riot lookups (the leader is one of the 10 randomly chosen
        # players, we grab them in passing). Grouped in a single thread
        # to avoid freezing the event loop for ~10x10ms.
        def _gather_riot_accounts() -> tuple[
            Mapping[str, Any] | None, dict[str, str], dict[str, str]
        ]:
            leader_uid_local = str(match_doc.get("lobby_leader_id"))
            leader: Mapping[str, Any] | None = None
            a_map: dict[str, str] = {}
            b_map: dict[str, str] = {}
            for player in match_doc.get("team_a", []):
                pid = str(player["id"])
                r = repository.get_riot_account(self.db, pid)
                if r and r.get("puuid"):
                    a_map[r["puuid"]] = pid
                if pid == leader_uid_local:
                    leader = r
            for player in match_doc.get("team_b", []):
                pid = str(player["id"])
                r = repository.get_riot_account(self.db, pid)
                if r and r.get("puuid"):
                    b_map[r["puuid"]] = pid
                if pid == leader_uid_local:
                    leader = r
            # Fallback: if the leader is no longer one of the 10 (after
            # a /match-replace for example), do a direct lookup.
            if leader is None:
                leader = repository.get_riot_account(self.db, leader_uid_local)
            return leader, a_map, b_map

        leader_riot, team_a_uid_by_puuid, team_b_uid_by_puuid = await asyncio.to_thread(
            _gather_riot_accounts,
        )
        if not leader_riot:
            return None

        expected = set(team_a_uid_by_puuid) | set(team_b_uid_by_puuid)
        if len(expected) < 10:
            return None

        after = match_doc.get("created_at") or match_doc.get("validated_at")

        # Circuit breaker: if HenrikDev failed 3x in a row recently,
        # skip for 5 min. Without this guard, each tick (1 min)
        # would re-run N stale matches × 12s of retries each, freezing
        # the ThreadPoolExecutor and overlapping ticks.
        now = datetime.now(UTC)
        async with self._henrik_lock:
            circuit_open = (
                self._henrik_circuit_open_until is not None
                and now < self._henrik_circuit_open_until
            )
        if circuit_open:
            return None

        # `find_henrik_custom_match` makes a synchronous HTTP call (`requests`).
        # We run it in a thread to avoid blocking the Discord event loop
        # during the timeout (up to 10s per call).
        try:
            summary = await asyncio.to_thread(
                find_henrik_custom_match,
                self.henrik_client,
                region=str(leader_riot.get("riot_region", "eu")),
                leader_name=str(leader_riot.get("riot_name", "")),
                leader_tag=str(leader_riot.get("riot_tag", "")),
                expected_puuids=expected,
                after=after,
            )
        except Exception as e:
            async with self._henrik_lock:
                self._henrik_consecutive_failures += 1
                failures = self._henrik_consecutive_failures
                if failures >= HENRIK_CIRCUIT_FAIL_THRESHOLD:
                    self._henrik_circuit_open_until = now + timedelta(
                        minutes=HENRIK_CIRCUIT_OPEN_MINUTES,
                    )
                    just_opened = True
                else:
                    just_opened = False
            if just_opened:
                logger.warning(
                    "[match] Henrik circuit OPEN after %d consecutive failures. "
                    "Resuming in %d min. Last error: %r",
                    failures,
                    HENRIK_CIRCUIT_OPEN_MINUTES,
                    e,
                )
            else:
                logger.error(
                    "[match] Henrik failure (%d/%d): %r",
                    failures,
                    HENRIK_CIRCUIT_FAIL_THRESHOLD,
                    e,
                    exc_info=True,
                )
            return None
        # Success: reset the failure counter and close the circuit.
        async with self._henrik_lock:
            if self._henrik_consecutive_failures > 0 or self._henrik_circuit_open_until is not None:
                self._henrik_consecutive_failures = 0
                self._henrik_circuit_open_until = None
        if summary is None:
            return None
        return summary, team_a_uid_by_puuid, team_b_uid_by_puuid

    async def _post_match_scoreboard(
        self,
        guild,
        summary,
        team_a_uid_by_puuid: dict[str, str],
        team_b_uid_by_puuid: dict[str, str],
        match_doc: dict,
        outcome,
    ) -> None:
        """Best-effort: build the per-match scoreboard image and post it
        in the queue's results channel (pro-results / semi-pro-results /
        gc-results / open-results). Silently no-op if the channel is
        missing or the image generation fails — the flat ELO was already
        applied upstream and shouldn't be blocked by a cosmetic post."""
        queue_type = match_doc.get("queue_type", "open")
        channel_name = RESULTS_CHANNELS.get(queue_type)
        if not channel_name:
            return
        channel = discord.utils.get(guild.text_channels, name=channel_name)
        if channel is None:
            logger.warning(
                "[match] results channel %r not found on guild %s; skipping scoreboard",
                channel_name,
                guild.id,
            )
            return

        rounds = max(int(getattr(summary, "rounds_played", 0)) or 1, 1)
        elo_by_uid = {c.user_id: c.new_elo for c in outcome.changes}
        elo_delta_by_uid = {c.user_id: c.delta for c in outcome.changes}

        # Resolve which Henrik side (Red/Blue) corresponds to each bot team.
        # Use majority vote — in the edge case of "mixed teams in the Valorant
        # lobby" both teams may share a side, but majority gives a reasonable
        # rendering anyway.
        def _side(uid_by_puuid: dict[str, str]) -> str:
            counts = {"Red": 0, "Blue": 0}
            for p in summary.players:
                if p.puuid in uid_by_puuid:
                    counts[p.team] = counts.get(p.team, 0) + 1
            return "Red" if counts["Red"] >= counts["Blue"] else "Blue"

        a_side = _side(team_a_uid_by_puuid)
        b_side = "Blue" if a_side == "Red" else "Red"
        rounds_a = summary.rounds_red if a_side == "Red" else summary.rounds_blue
        rounds_b = summary.rounds_red if b_side == "Red" else summary.rounds_blue

        def _rows(uid_by_puuid: dict[str, str]) -> list[dict]:
            rows = []
            for stats in summary.players:
                uid = uid_by_puuid.get(stats.puuid)
                if uid is None:
                    continue
                member = guild.get_member(int(uid)) if uid.isdigit() else None
                # Prefer Discord display name; fall back to Riot name#tag
                # if the user is no longer on the server (kick/leave).
                if member is not None:
                    display_name = member.display_name
                else:
                    display_name = f"{stats.name}#{stats.tag}" if stats.tag else stats.name
                shots_total = (
                    int(getattr(stats, "headshots", 0) or 0)
                    + int(getattr(stats, "bodyshots", 0) or 0)
                    + int(getattr(stats, "legshots", 0) or 0)
                )
                hs_pct = (
                    (int(getattr(stats, "headshots", 0) or 0) / shots_total) * 100.0
                    if shots_total
                    else 0.0
                )
                kast_pct = (
                    (int(getattr(stats, "kast_rounds", 0) or 0) / rounds) * 100.0
                )
                adr = int(getattr(stats, "damage_made", 0) or 0) / rounds
                rating_2_0 = compute_rating_2_0(
                    RatingInputs(
                        rounds_played=rounds,
                        kills=int(stats.kills or 0),
                        deaths=int(stats.deaths or 0),
                        assists=int(stats.assists or 0),
                        damage_made=int(getattr(stats, "damage_made", 0) or 0),
                        kast_rounds=int(getattr(stats, "kast_rounds", 0) or 0),
                    )
                )
                rows.append(
                    {
                        "name": display_name,
                        "kills": stats.kills,
                        "deaths": stats.deaths,
                        "assists": stats.assists,
                        "acs": round(stats.score / rounds),
                        "elo": elo_by_uid.get(uid, 0),
                        "elo_delta": elo_delta_by_uid.get(uid, 0),
                        "agent": getattr(stats, "agent", ""),
                        "rating_2_0": rating_2_0,
                        "kast_pct": kast_pct,
                        "adr": adr,
                        "hs_pct": hs_pct,
                        "first_kills": int(getattr(stats, "first_kills", 0) or 0),
                        "first_deaths": int(getattr(stats, "first_deaths", 0) or 0),
                    }
                )
            return rows

        team_a_rows = _rows(team_a_uid_by_puuid)
        team_b_rows = _rows(team_b_uid_by_puuid)
        queue_label = _QUEUE_LABEL_BY_TYPE.get(queue_type, queue_type)
        # Re-order the per-round winners so that index 0 maps to team A's
        # actual perspective. Henrik labels each round by Red/Blue, but
        # team A may be either; the scoreboard expects A = Red.
        round_winners = tuple(getattr(summary, "round_winners", ()) or ())
        if a_side == "Blue":
            round_winners = tuple(
                "Red" if w == "Blue" else ("Blue" if w == "Red" else "")
                for w in round_winners
            )
        # End-type is symmetric per round, no remap needed.
        round_end_types = tuple(getattr(summary, "round_end_types", ()) or ())

        try:
            buf = await asyncio.to_thread(
                generate_scoreboard,
                map_name=summary.map_name,
                rounds_a=rounds_a,
                rounds_b=rounds_b,
                team_a_label="Team A",
                team_b_label="Team B",
                team_a_players=team_a_rows,
                team_b_players=team_b_rows,
                queue_label=queue_label,
                round_winners=round_winners,
                round_end_types=round_end_types,
            )
        except Exception:
            logger.exception("[match] scoreboard image generation raised")
            return

        try:
            await channel.send(file=discord.File(buf, filename=f"scoreboard_{queue_type}.png"))
        except Exception:
            logger.exception("[match] scoreboard send raised")

    # ── Periodic loop (1 min) ────────────────────────────────────
    @tasks.loop(minutes=1)
    async def _timeout_loop(self):
        try:
            await self.check_vote_timeouts()
        except Exception:
            logger.exception("[match] check_vote_timeouts raised")
        try:
            await self.check_henrik_verifications()
        except Exception:
            logger.exception("[match] check_henrik_verifications raised")
        try:
            await self.expire_stale_contested_matches()
        except Exception:
            logger.exception("[match] expire_stale_contested_matches raised")

    async def expire_stale_contested_matches(self, *, now: datetime | None = None) -> int:
        """Auto-expire `contested` matches older than CONTESTED_EXPIRY_HOURS.
        Without this, an unresolved contested match (no admin action)
        freezes the 10 players in the find_active_match_for_player gate.

        Scoped per guild: avoids touching other guilds' matches.

        Returns:
            Total number of docs expired across all guilds.
        """
        now = now or datetime.now(UTC)
        cutoff = now - timedelta(hours=CONTESTED_EXPIRY_HOURS)
        total = 0
        for guild in self.bot.guilds:
            try:
                n = await asyncio.to_thread(
                    repository.expire_stale_contested,
                    self.db,
                    origin_guild_id=guild.id,
                    cutoff_dt=cutoff,
                )
            except Exception:
                logger.exception("[match] expire_stale_contested guild=%s raised", guild.id)
                continue
            if n:
                logger.info(
                    "[match] auto-expire contested: %d match(es) cleaned_up in guild %s",
                    n,
                    guild.name,
                )
            total += n
        return total

    @_timeout_loop.before_loop
    async def _before_loop(self):
        await self.bot.wait_until_ready()

    @_timeout_loop.error
    async def _timeout_loop_error(self, error: BaseException) -> None:
        """Safety net: `tasks.loop` dies silently if an exception leaks
        outside the tick's internal try/except. Without this handler,
        timed-out votes would no longer be processed until the next
        bot restart."""
        # logger.error with exc_info=tuple: preserves the stack of the
        # `error` passed as argument (logger.exception() uses
        # sys.exc_info() which is not the current `error` here).
        logger.error(
            "[match] _timeout_loop raised: %r",
            error,
            exc_info=(type(error), error, error.__traceback__),
        )
        try:
            self._timeout_loop.restart()
        except Exception:
            logger.exception("[match] _timeout_loop.restart() raised")

    # ── Admin slash commands (cancel / replace) ─────────────────
    @app_commands.command(
        name="match-cancel",
        description="Cancel the match currently running in this channel (admin)",
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    async def match_cancel(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        # Atomic CAS: if a concurrent vote validates the match or if
        # _verify_match claims the ELO between read and write, the cancel
        # fails cleanly rather than creating an inconsistent state.
        match = await asyncio.to_thread(
            repository.cancel_match_atomically,
            self.db,
            channel_id=interaction.channel_id,
        )
        if not match:
            await interaction.followup.send(
                "❌ No cancellable match found in this channel "
                "(status pending/validated/contested and ELO not applied).",
                ephemeral=True,
            )
            return

        category_name = match.get("category_name")

        # Revoke the "Match Host" role from the lobby leader.
        leader_id = match.get("lobby_leader_id")
        if leader_id is not None:
            leader = interaction.guild.get_member(int(leader_id))
            if leader is not None:
                host_role = discord.utils.get(interaction.guild.roles, name=MATCH_HOST_ROLE_NAME)
                if host_role is not None and host_role in leader.roles:
                    with contextlib.suppress(discord.Forbidden, discord.HTTPException):
                        await leader.remove_roles(host_role, reason="Match cancelled")

        try:
            msg_id = match.get("message_id")
            if msg_id and interaction.channel:
                msg = await interaction.channel.fetch_message(int(msg_id))
                await msg.edit(view=None)
        except Exception:
            logger.exception("[match-cancel] view removal raised")

        # Deletion of the match's Discord category.
        # Graceful: legacy matches without category_id are ignored.
        category_id = match.get("category_id")
        if category_id:
            repository.mark_match_cleanup_started(self.db, match["_id"])
            await delete_match_category(
                guild=interaction.guild,
                category_id=category_id,
                reason=f"Match #{match.get('match_number', '?')} cancelled by admin",
            )

        await interaction.followup.send(
            f"✅ Match cancelled. Category `{category_name or '?'}` released.",
            ephemeral=True,
        )

    @app_commands.command(
        name="match-replace",
        description="Replace a player in the current match (admin)",
    )
    @app_commands.describe(
        leaver="Player to replace",
        replacement="New player (must have a linked Riot account)",
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    async def match_replace(
        self,
        interaction: discord.Interaction,
        leaver: discord.Member,
        replacement: discord.Member,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        if leaver.id == replacement.id:
            await interaction.followup.send(
                "❌ Cannot replace a player with themselves.",
                ephemeral=True,
            )
            return

        match = await self._fetch_pending_match(interaction)
        if match is None:
            return

        team_key = self._team_of_player(match, leaver.id)
        if team_key is None:
            await interaction.followup.send(
                f"❌ {leaver.mention} is not in this match.",
                ephemeral=True,
            )
            return

        if self._is_player_in_match(match, replacement.id):
            await interaction.followup.send(
                f"❌ {replacement.mention} is already in this match.",
                ephemeral=True,
            )
            return

        new_elo = await self._resolve_replacement_elo(replacement, match)
        if new_elo is None:
            await interaction.followup.send(
                f"❌ {replacement.mention} does not have a linked Riot account "
                "(`/link-riot Name#TAG`).",
                ephemeral=True,
            )
            return

        leaver_elo = self._elo_of_player(match, team_key, leaver.id)
        if not await self._replace_within_elo_band(
            interaction, leaver, replacement, leaver_elo, new_elo
        ):
            return

        leader_replaced, modified = await self._apply_replace_update(
            match, team_key, leaver.id, replacement, new_elo
        )
        if not modified:
            await interaction.followup.send(
                "❌ The match was validated or cancelled in the meantime. Replace aborted.",
                ephemeral=True,
            )
            return

        if leader_replaced:
            await self._transfer_match_host_role(interaction.guild, leaver, replacement)

        suffix = " (lobby host)" if leader_replaced else ""
        await interaction.followup.send(
            f"✅ {leaver.mention} replaced by {replacement.mention} in `{team_key}`{suffix}.",
            ephemeral=True,
        )

    async def _fetch_pending_match(
        self, interaction: discord.Interaction
    ) -> dict | None:
        """Fetch the active ``pending`` match doc for the current channel.

        Sends an error followup and returns ``None`` if no such match exists.
        """
        matches_col = repository.get_matches_col(self.db)
        match = await asyncio.to_thread(
            matches_col.find_one,
            {"channel_id": interaction.channel_id, "status": "pending"},
        )
        if not match:
            await interaction.followup.send(
                "❌ No match in progress (status pending) in this channel.",
                ephemeral=True,
            )
        return match

    @staticmethod
    def _team_of_player(match: dict, user_id: int) -> str | None:
        """Return ``"team_a"`` / ``"team_b"`` for ``user_id`` or ``None``."""
        for tk in ("team_a", "team_b"):
            if any(int(p.get("id", 0)) == user_id for p in match.get(tk, [])):
                return tk
        return None

    @staticmethod
    def _is_player_in_match(match: dict, user_id: int) -> bool:
        return any(
            int(p.get("id", 0)) == user_id
            for tk in ("team_a", "team_b")
            for p in match.get(tk, [])
        )

    @staticmethod
    def _elo_of_player(match: dict, team_key: str, user_id: int) -> int:
        player = next(
            (p for p in match[team_key] if int(p.get("id", 0)) == user_id),
            None,
        )
        return int(player.get("elo", 0)) if player else 0

    async def _resolve_replacement_elo(
        self,
        replacement: discord.Member,
        match: dict,
    ) -> int | None:
        """Look up the replacement's queue-typed ELO.

        Returns ``None`` if the player has no linked Riot account; falls
        back to ``ELO_START`` if they have no ELO doc yet for this queue.
        """
        riot = await asyncio.to_thread(
            repository.get_riot_account,
            self.db,
            replacement.id,
        )
        if not riot:
            return None
        match_queue_type = match.get("queue_type", "open")
        elo_col = repository.get_elo_col(self.db)
        elo_doc = await asyncio.to_thread(
            elo_col.find_one,
            {"_id": repository.player_doc_id(replacement.id, match_queue_type)},
        )
        if elo_doc:
            return int(elo_doc.get("elo", elo_calc.ELO_START))
        return elo_calc.ELO_START

    async def _replace_within_elo_band(
        self,
        interaction: discord.Interaction,
        leaver: discord.Member,
        replacement: discord.Member,
        leaver_elo: int,
        new_elo: int,
    ) -> bool:
        """Reject the replace if |leaver - replacement| > MAX_REPLACE_ELO_DIFF.

        Reasoning: teams were balanced at formation; a big gap breaks the
        balance and the post-match ELO would not reflect real performance.
        """
        elo_diff = abs(leaver_elo - new_elo)
        if elo_diff <= MAX_REPLACE_ELO_DIFF:
            return True
        await interaction.followup.send(
            f"❌ ELO gap too large: {leaver.mention} "
            f"({leaver_elo}) vs {replacement.mention} ({new_elo}) "
            f"-> diff={elo_diff} > {MAX_REPLACE_ELO_DIFF}. The teams "
            "would be unbalanced. Cancel the match (`/match-cancel`) "
            "and reform the queue.",
            ephemeral=True,
        )
        return False

    async def _apply_replace_update(
        self,
        match: dict,
        team_key: str,
        leaver_id: int,
        replacement: discord.Member,
        new_elo: int,
    ) -> tuple[bool, bool]:
        """Apply the team swap and (if applicable) lobby-leader transfer.

        The transfer matters because ``_fetch_henrik_multipliers`` queries
        the lobby leader's Riot history; leaving the old leader would make
        Henrik miss the custom and fall back to flat ELO. Updates use a
        CAS on ``status=pending`` to avoid clobbering a concurrent
        vote/cancel.

        Returns ``(leader_replaced, modified)``.
        """
        new_player = {
            "id": replacement.id,
            "name": replacement.display_name,
            "elo": new_elo,
        }
        new_team = [
            new_player if int(p.get("id", 0)) == leaver_id else p
            for p in match[team_key]
        ]
        update: dict[str, Any] = {team_key: new_team}
        leader_replaced = int(match.get("lobby_leader_id", 0)) == int(leaver_id)
        if leader_replaced:
            update["lobby_leader_id"] = str(replacement.id)

        matches_col = repository.get_matches_col(self.db)
        result = await asyncio.to_thread(
            matches_col.update_one,
            {"_id": match["_id"], "status": "pending"},
            {"$set": update},
        )
        return leader_replaced, result.modified_count == 1

    @staticmethod
    async def _transfer_match_host_role(
        guild: discord.Guild,
        leaver: discord.Member,
        replacement: discord.Member,
    ) -> None:
        """Move ``MATCH_HOST_ROLE_NAME`` from leaver to replacement. Best-effort."""
        host_role = discord.utils.get(guild.roles, name=MATCH_HOST_ROLE_NAME)
        if host_role is None:
            return
        if host_role in leaver.roles:
            with contextlib.suppress(discord.Forbidden, discord.HTTPException):
                await leaver.remove_roles(
                    host_role, reason="Match replace: host transferred"
                )
        with contextlib.suppress(discord.Forbidden, discord.HTTPException):
            await replacement.add_roles(
                host_role, reason="Match replace: host transferred"
            )

    @staticmethod
    def _resolve_match_id(match_id: str) -> ObjectId | str:
        """Convert the id entered by the admin into an ObjectId.

        Matches created via `repository.create_match` have an ObjectId
        `_id` (insert_one without `_id`). pymongo does NOT convert a
        hex string into an ObjectId: `{"_id": "<hex>"}` never matches
        a doc with an ObjectId `_id`. We therefore convert explicitly.
        Fallback to the raw value if it is not a valid hex ObjectId, to
        stay compatible with possible legacy docs with a string `_id`.
        """
        try:
            return ObjectId(match_id)
        except (InvalidId, TypeError):
            return match_id

    @app_commands.command(
        name="match-cleanup",
        description="(Admin) Force deletion of the category of a disputed or stuck match.",
    )
    async def match_cleanup(self, interaction: discord.Interaction, match_id: str) -> None:
        """Admin-only force teardown for disputed/blocked matches."""
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message(
                "This command is reserved for administrators.",
                ephemeral=True,
            )
            return

        query_id = self._resolve_match_id(match_id)
        match = self.db["matches"].find_one({"_id": query_id})
        if match is None:
            await interaction.response.send_message(
                f"Match `{match_id}` not found.", ephemeral=True
            )
            return

        category_id = match.get("category_id")
        if not category_id:
            await interaction.response.send_message(
                f"Match `{match_id}` has no category_id (probably a pre-migration match).",
                ephemeral=True,
            )
            return

        # Reuse the doc's real `_id` for the next ops: guarantees we
        # target the right document whatever the id type.
        real_id = match["_id"]
        repository.mark_match_cleanup_started(self.db, real_id)
        await delete_match_category(
            guild=interaction.guild,
            category_id=category_id,
            reason=f"Admin cleanup by {interaction.user} (match {match_id})",
        )
        self.db["matches"].update_one(
            {"_id": real_id},
            {
                "$set": {
                    "status": "cleaned_up",
                    "cleaned_up_at": datetime.now(UTC),
                    "cleaned_up_by": interaction.user.id,
                }
            },
        )
        await interaction.response.send_message(f"Match `{match_id}` cleaned up.", ephemeral=True)

    @app_commands.command(
        name="match-force-result",
        description="Force the winner of a timed-out / stuck vote in this channel (admin)",
    )
    @app_commands.describe(winner="Team that won the match")
    @app_commands.choices(
        winner=[
            app_commands.Choice(name="Team A", value="a"),
            app_commands.Choice(name="Team B", value="b"),
        ]
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    async def match_force_result(
        self,
        interaction: discord.Interaction,
        winner: app_commands.Choice[str],
    ) -> None:
        """Settle a `contested` (vote timed out) or `pending` match by hand.

        Atomic CAS on status pending/contested + elo_applied != True: a
        concurrent vote reaching the majority, /match-cancel or the Henrik
        ELO claim all make this fail cleanly rather than overwriting an
        already-settled result. On success we fire the exact same
        post-validation hook as a normal vote (category teardown + Henrik
        verification scheduling), so the ELO is applied downstream."""
        await interaction.response.defer(ephemeral=True)
        forced = await asyncio.to_thread(
            repository.force_match_result_atomically,
            self.db,
            channel_id=interaction.channel_id,
            winner=winner.value,
        )
        if not forced:
            await interaction.followup.send(
                "❌ No forceable match in this channel "
                "(must be pending/contested with ELO not yet applied).",
                ephemeral=True,
            )
            return

        team_label = "Team A" if winner.value == "a" else "Team B"
        await interaction.followup.send(
            f"✅ Result forced: **{team_label} won**. "
            f"ELO will be applied after HenrikDev verification.",
            ephemeral=True,
        )

        # Same hook as a vote reaching the majority: deletes the match
        # category and schedules the Henrik verification / ELO pass.
        try:
            await self._on_match_validated(interaction, forced)
        except Exception:
            logger.exception("[match-force-result] _on_match_validated raised")

    @match_cancel.error
    @match_replace.error
    @match_force_result.error
    async def _admin_perm_error(self, inter: discord.Interaction, error):
        if isinstance(error, app_commands.MissingPermissions):
            try:
                await inter.response.send_message(
                    "🚫 Reserved for administrators.",
                    ephemeral=True,
                )
            except discord.InteractionResponded:
                await inter.followup.send(
                    "🚫 Reserved for administrators.",
                    ephemeral=True,
                )

    # Statuses for which the match's Discord category must be preserved
    # by the orphan cleanup at boot. Covers:
    #   - "pending"      : match in progress, vote open
    #   - "validated_a"  : team A won but ELO not yet applied (Henrik
    #                      verification deferred by HENRIK_VERIFY_DELAY_MINUTES)
    #   - "validated_b"  : same for team B
    #   - "contested"    : vote timeout, awaiting admin resolution
    # Terminal statuses ("cancelled", "cleaned_up") are NOT protected:
    # their categories must disappear at boot if they are still hanging around.
    _ACTIVE_MATCH_STATUSES: tuple[str, ...] = (
        "pending",
        "validated_a",
        "validated_b",
        "contested",
    )

    async def cog_load(self) -> None:
        # `_timeout_loop.start()` calls `_before_loop` which does
        # `await self.bot.wait_until_ready()`. In tests, `self.bot` is a
        # MagicMock whose `wait_until_ready` attribute is not awaitable
        # -> TypeError silently logged by `tasks.Loop`. We detect this
        # case and skip the start in tests (the timeout-loop only makes
        # sense with a live Discord gateway anyway).
        if isinstance(self.bot, commands.Bot):
            self._timeout_loop.start()
            # cog_load runs inside setup_hook(), which is BEFORE on_ready
            # -> self.bot.guilds is empty here. Defer the actual cleanup
            # to a background task that awaits wait_until_ready first.
            self.bot.loop.create_task(self._deferred_startup_cleanup())
        else:
            # Tests: bot is a MagicMock with synthetic .guilds. Run the
            # cleanup inline so assertions can observe its effects
            # (no real gateway -> wait_until_ready is meaningless).
            await self._run_startup_cleanup()

    async def _deferred_startup_cleanup(self) -> None:
        """Wait for the gateway READY (so self.bot.guilds is populated)
        then run the startup cleanup. Errors are swallowed and logged:
        a failed sweep must not crash the bot's task loop.
        """
        try:
            await self.bot.wait_until_ready()
            await self._run_startup_cleanup()
        except Exception:
            logger.exception("[match] deferred startup cleanup failed")

    async def _run_startup_cleanup(self) -> None:
        """Startup recovery + orphan-category sweep.

        Order matters:
          1. Expire stale 'contested' matches so their categories drop
             out of the active set.
          2. Recover 'preparing' matches: a bot restart mid-draft or
             mid-map-ban kills the in-memory session, leaving dead
             buttons and a stuck category. We mark these cancelled and
             delete the category. This MUST run BEFORE computing
             active_ids so the now-cancelled docs don't protect a
             category from orphan cleanup later.
          3. Sweep orphan 'Match #N' categories not referenced by any
             active match.
        """
        try:
            await self.expire_stale_contested_matches()
        except Exception:
            logger.exception("[match] _run_startup_cleanup expire_stale_contested raised")

        await self._recover_preparing_matches()

        active_ids: set[int] = {
            m["category_id"]
            for m in self.db["matches"].find(
                {
                    "status": {"$in": list(self._ACTIVE_MATCH_STATUSES)},
                    "elo_applied": {"$ne": True},
                },
                {"category_id": 1},
            )
            if m.get("category_id")
        }
        for guild in self.bot.guilds:
            # Safety net: if a previous cleanup was interrupted between
            # `mark_match_cleanup_started` and the terminal status
            # transition, we remove those categories from the active set.
            # Orphan cleanup will resume `delete_match_category` (idempotent).
            in_flight_cleanup = repository.find_category_ids_with_cleanup_started(
                self.db, origin_guild_id=guild.id
            )
            guild_active_ids = active_ids - in_flight_cleanup
            try:
                deleted = await cleanup_orphan_match_categories(
                    guild=guild, active_category_ids=guild_active_ids
                )
                logger.info(
                    "[match] Startup cleanup in %s: %d orphan categories deleted",
                    guild.name,
                    deleted,
                )
            except Exception:
                logger.exception("[match] cog_load cleanup failed for guild %s", guild.name)

    async def _recover_preparing_matches(self) -> None:
        """Find every match stuck in status='preparing' (i.e. a draft
        or map-ban session that was running when the bot last shut
        down), atomically transition it to 'cancelled', and delete its
        Discord category. The in-memory session can't be restored
        (button views are not persistent), so leaving the category
        would mean dead buttons and an undeletable channel for admins
        (since /match-cancel previously could not find the match).
        """
        try:
            preparing = await asyncio.to_thread(repository.find_preparing_matches, self.db)
        except Exception:
            logger.exception("[match] find_preparing_matches raised")
            return

        for m in preparing:
            match_id = m.get("_id")
            cat_id = m.get("category_id")
            guild_id = m.get("origin_guild_id")
            try:
                await asyncio.to_thread(repository.cancel_preparing_match, self.db, match_id)
                if cat_id and guild_id:
                    guild = self.bot.get_guild(int(guild_id))
                    if guild is not None:
                        await delete_match_category(
                            guild=guild,
                            category_id=int(cat_id),
                            reason="Bot restart during draft/map-ban",
                        )
                logger.info(
                    "[match] Recovered preparing match #%s (cancelled + category deleted)",
                    m.get("match_number"),
                )
            except Exception:
                logger.exception("[match] failed to recover preparing match _id=%s", match_id)

    async def cog_unload(self):
        self._timeout_loop.cancel()


async def setup(
    bot: commands.Bot,
    db,
    *,
    rng: random.Random | None = None,
    henrik_client: HenrikDevClient | None = None,
) -> MatchCog:
    cog = MatchCog(bot, db, rng=rng, henrik_client=henrik_client)
    await bot.add_cog(cog)
    bot.add_view(cog.vote_view)
    return cog
