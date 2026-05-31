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

from bson import ObjectId
from bson.errors import InvalidId

import discord
from discord import app_commands
from discord.ext import commands, tasks

from cogs.queue_v2 import (
    QUEUE_CHANNEL_NAMES,
    QUEUE_ROLE_NAME,
)
from services import elo_calc, repository
from services.elo_updater import (
    apply_match_validation,
)
from services.leaderboard_refresh import refresh_leaderboard_channel
from services.match_category import (
    create_match_category,
    delete_match_category,
    cleanup_orphan_match_categories,
)
from services.match_service import (
    build_players,
    plan_match,
    serialize_team,
)
from services.repository import reserve_match_number
from services.match_verifier import (
    compute_acs_multipliers,
    find_henrik_custom_match,
)
from services.riot_api import HenrikDevClient

from cogs.match._constants import (
    ADMIN_ROLE_NAMES,
    MATCH_SPECTATOR_ROLE_NAMES,
    MATCH_VIEWER_ROLE_NAMES,
    CONTESTED_EXPIRY_HOURS,
    HENRIK_CIRCUIT_FAIL_THRESHOLD,
    HENRIK_CIRCUIT_OPEN_MINUTES,
    HENRIK_VERIFY_DELAY_MINUTES,
    HENRIK_VERIFY_TIMEOUT_MINUTES,
    MAJORITY_THRESHOLD,
    MATCH_HOST_ROLE_NAME,
    MAX_REPLACE_ELO_DIFF,
    VOTE_TIMEOUT_MINUTES,
)
from cogs.match._embeds import (
    build_elo_changes_embed,
    build_match_embed,
)
from cogs.match._vote import VoteView


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

        plan = plan_match(players, free_category=free_cat_name, rng=self.rng)

        # Setup ordering: we persist the match (DB) BEFORE announcing on
        # Discord. If persistence fails (Mongo down, timeout), we do NOT
        # want the 10 players to see a "Match found!" message without an
        # associated match doc (dead buttons, /match-cancel finds nothing).
        #
        # Step 1: persist the match with message_id=None. This is the
        # commit point: after this, the match state machine has a
        # source of truth.
        match_id = await asyncio.to_thread(
            repository.create_match,
            self.db,
            queue_type=queue_type,
            origin_guild_id=guild.id,
            team_a=serialize_team(plan.teams.team_a),
            team_b=serialize_team(plan.teams.team_b),
            map_name=plan.map_name,
            lobby_leader_id=plan.lobby_leader.id,
            category_name=plan.category_name,
            category_id=category.id,
            match_number=match_number,
            message_id=None,
            channel_id=prep_channel.id,
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
                "[match] _move_players_to_waiting_match: 'Waiting Match' "
                "not found in %s, no-op",
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
        timeout_cutoff = now - timedelta(minutes=HENRIK_VERIFY_TIMEOUT_MINUTES)

        # Per-guild parallelization: same principle as check_vote_timeouts.
        results = await asyncio.gather(
            *[
                self._check_henrik_verifications_for_guild(g, start_cutoff, timeout_cutoff)
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
        timeout_cutoff: datetime,
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
            validated_at = match.get("validated_at") or match.get("created_at")
            timed_out = bool(validated_at is not None and validated_at <= timeout_cutoff)
            try:
                await self._verify_match(guild, match, force_apply=timed_out)
            except Exception:
                logger.exception("[match] verify_match raised")
            processed += 1
        return processed

    async def _verify_match(
        self,
        guild,
        match_doc: dict,
        *,
        force_apply: bool = False,
    ) -> None:
        """
        Apply the flat ±20 ELO for a validated match. ACS/Henrik
        weighting has been removed.

        Idempotency: we **claim** the match (`elo_applied=True`) BEFORE
        applying the ELO. If the claim fails (already applied elsewhere),
        we skip. If the ELO application raises, we release the claim to
        allow a retry on the next tick.
        """
        queue_type = match_doc.get("queue_type", "open")

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

        try:
            outcome = await asyncio.to_thread(
                apply_match_validation,
                self.db,
                match_doc,
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
            found=False,
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

    async def _fetch_henrik_multipliers(
        self,
        guild,
        match_doc: dict,
    ) -> dict[str, float] | None:
        """Attempt to find the HenrikDev custom and compute ACS
        multipliers. Returns None if not usable."""

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
        # Serialized read: without the lock, several guilds running in
        # parallel could observe an intermediate state (see #17).
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

        verified = compute_acs_multipliers(
            summary,
            team_a_uid_by_puuid=team_a_uid_by_puuid,
            team_b_uid_by_puuid=team_b_uid_by_puuid,
        )
        multipliers = {p.user_id: p.multiplier for p in verified.performances}
        # If compute_acs_multipliers could not extract anything (the 2
        # teams on Riot's side are mixed: players switched Attack/Defense
        # in the lobby), we return None rather than an empty dict.
        # Otherwise apply_match_validation would have `weighted=True` but
        # would still apply flat ELO (mults.get -> 1.0 by default),
        # displaying "ACS weighting applied" while nothing actually is.
        if not multipliers:
            logger.warning(
                "[match] Henrik found the custom %s but compute_acs_multipliers "
                "could not extract any multiplier (mixed Attack/Defense teams "
                "in the Valorant lobby?). Flat ELO applied.",
                summary.matchid,
            )
            return None
        return multipliers

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
            return

        team_key: str | None = None
        for tk in ("team_a", "team_b"):
            if any(int(p.get("id", 0)) == leaver.id for p in match.get(tk, [])):
                team_key = tk
                break
        if team_key is None:
            await interaction.followup.send(
                f"❌ {leaver.mention} is not in this match.",
                ephemeral=True,
            )
            return

        if any(
            int(p.get("id", 0)) == replacement.id
            for tk in ("team_a", "team_b")
            for p in match.get(tk, [])
        ):
            await interaction.followup.send(
                f"❌ {replacement.mention} is already in this match.",
                ephemeral=True,
            )
            return

        riot = await asyncio.to_thread(
            repository.get_riot_account,
            self.db,
            replacement.id,
        )
        if not riot:
            await interaction.followup.send(
                f"❌ {replacement.mention} does not have a linked Riot account (`/link-riot Name#TAG`).",
                ephemeral=True,
            )
            return

        # Look up the replacement's ELO in the queue_type of the current match.
        # The player doc uses a compound _id `<uid>:<queue_type>`.
        match_queue_type = match.get("queue_type", "open")
        elo_col = repository.get_elo_col(self.db)
        elo_doc = await asyncio.to_thread(
            elo_col.find_one,
            {"_id": repository.player_doc_id(replacement.id, match_queue_type)},
        )
        new_elo = int(elo_doc.get("elo", elo_calc.ELO_START)) if elo_doc else elo_calc.ELO_START

        # Refuse the replace if the gap is too large: the teams had been
        # balanced at formation time, a swap with a gap > MAX_REPLACE_ELO_DIFF
        # breaks that balance and the post-match ELO would not reflect
        # the real performance.
        leaver_player = next(
            (p for p in match[team_key] if int(p.get("id", 0)) == leaver.id),
            None,
        )
        leaver_elo = int(leaver_player.get("elo", 0)) if leaver_player else 0
        elo_diff = abs(leaver_elo - new_elo)
        if elo_diff > MAX_REPLACE_ELO_DIFF:
            await interaction.followup.send(
                f"❌ ELO gap too large: {leaver.mention} "
                f"({leaver_elo}) vs {replacement.mention} ({new_elo}) "
                f"-> diff={elo_diff} > {MAX_REPLACE_ELO_DIFF}. The teams "
                "would be unbalanced. Cancel the match (`/match-cancel`) "
                "and reform the queue.",
                ephemeral=True,
            )
            return

        new_player = {
            "id": replacement.id,
            "name": replacement.display_name,
            "elo": new_elo,
        }
        new_team = [new_player if int(p.get("id", 0)) == leaver.id else p for p in match[team_key]]
        # If the leaver was the lobby leader, transfer the role to the
        # replacement: without this, `_fetch_henrik_multipliers` would
        # query the original lobby leader's Riot history (who did not
        # play the custom) -> match never found on Henrik's side -> flat
        # ELO applied instead of the expected ACS weighting. The Discord
        # "Match Host" role follows as well.
        update: dict[str, Any] = {team_key: new_team}
        leader_replaced = int(match.get("lobby_leader_id", 0)) == int(leaver.id)
        if leader_replaced:
            update["lobby_leader_id"] = str(replacement.id)
        # CAS on the status: if in the meantime a vote moved the match
        # to validated_*/contested, we no longer touch the teams.
        result = await asyncio.to_thread(
            matches_col.update_one,
            {"_id": match["_id"], "status": "pending"},
            {"$set": update},
        )
        if result.modified_count != 1:
            await interaction.followup.send(
                "❌ The match was validated or cancelled in the meantime. Replace aborted.",
                ephemeral=True,
            )
            return

        # Transfer the "Match Host" role if the leader is being replaced.
        if leader_replaced:
            host_role = discord.utils.get(interaction.guild.roles, name=MATCH_HOST_ROLE_NAME)
            if host_role is not None:
                if host_role in leaver.roles:
                    with contextlib.suppress(discord.Forbidden, discord.HTTPException):
                        await leaver.remove_roles(
                            host_role, reason="Match replace: host transferred"
                        )
                with contextlib.suppress(discord.Forbidden, discord.HTTPException):
                    await replacement.add_roles(host_role, reason="Match replace: host transferred")

        suffix = " (lobby host)" if leader_replaced else ""
        await interaction.followup.send(
            f"✅ {leaver.mention} replaced by {replacement.mention} in `{team_key}`{suffix}.",
            ephemeral=True,
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

    @match_cancel.error
    @match_replace.error
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
        # Auto-expire any lingering contested matches (admins doing
        # /win+/lose without /match-cancel). We do this BEFORE computing
        # active_ids: a contested > CONTESTED_EXPIRY_HOURS must be
        # cleaned_up and therefore NOT protect its Discord category
        # from orphan cleanup.
        try:
            await self.expire_stale_contested_matches()
        except Exception:
            logger.exception("[match] cog_load expire_stale_contested raised")
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
