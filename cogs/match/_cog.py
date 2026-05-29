"""MatchCog - orchestrateur du flow match.

Reste un gros cog (~1300 lignes) car les transitions match (formation,
vote, verification Henrik, cleanups) partagent l'etat `self` (db,
henrik_client, circuit breaker, semaphore role edits). Un decoupage en
mixins multiples ajouterait du couplage inverse sans gain de lisibilite.

Le decoupage du *module* en sous-fichiers (`_constants`, `_embeds`,
`_vote`) sort en revanche les blocks purement fonctionnels du cog.
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
        # Circuit breaker Henrik : suspend les appels apres N echecs consecutifs.
        # `_henrik_lock` serialise les transitions du compteur/ouverture
        # quand plusieurs verifications tournent en parallele
        # (asyncio.gather sur les guilds).
        self._henrik_consecutive_failures: int = 0
        self._henrik_circuit_open_until: datetime | None = None
        self._henrik_lock: asyncio.Lock = asyncio.Lock()
        # Garde-fou rate limit Discord pour les ops de roles/voice.
        # Discord plafonne le bucket per-guild (PATCH /members/{u}) a ~10/10s ;
        # on cap a 5 concurrents pour ne jamais saturer (formation match
        # = 10 joueurs simultanes sinon 429 + retry de ~9s).
        self._guild_member_edit_sem: asyncio.Semaphore = asyncio.Semaphore(5)

    # ── Branchement queue full ───────────────────────────────────
    async def on_queue_full(
        self,
        interaction: discord.Interaction,
        queue_doc: dict,
        queue_type: str = "open",
    ):
        guild = interaction.guild
        player_ids = [str(uid) for uid in queue_doc.get("players", [])]

        # Batch 2 requetes Mongo au lieu de 20 (N+1) : on fetch les
        # 10 comptes Riot et les 10 docs ELO en une seule requete chacune.
        # Toutes les ops Mongo sont regroupees dans un seul thread pour
        # ne pas geler l'event loop pendant la formation du match.
        elo_col = repository.get_elo_col(self.db)
        riot_col = repository.get_riot_col(self.db)

        def _batch_fetch() -> tuple[dict[str, dict], dict[str, int]]:
            riot_map: dict[str, dict] = {}
            elo_map: dict[str, int] = {}
            for doc in riot_col.find({"_id": {"$in": player_ids}}):
                riot_map[str(doc["_id"])] = dict(doc)
            # Compound _id : map de "uid:queue_type" -> elo. On stocke par
            # uid simple pour que `build_players` reste pur (cle uid bare).
            compound_ids = [repository.player_doc_id(uid, queue_type) for uid in player_ids]
            for doc in elo_col.find({"_id": {"$in": compound_ids}}):
                uid = str(doc["_id"]).split(":", 1)[0]
                elo_map[uid] = int(doc.get("elo", elo_calc.ELO_START))
            return riot_map, elo_map

        riot_accounts, bot_elos = await asyncio.to_thread(_batch_fetch)

        # Joueurs sans doc ELO encore (premier match, ou apres reset) :
        # default ELO_START au lieu de 0. `build_players` lira ces valeurs
        # via `bot_elos.get(uid, 0)` ; on remplit donc explicitement le
        # fallback ici pour garder la fonction pure.
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
                "Joueur(s) sans compte Riot lie. Match annule.",
                queue_type=queue_type,
            )
            return None

        # Channel d'origine de la queue (pour reposter le setup-queue apres)
        queue_channel = guild.get_channel(int(queue_doc["channel_id"]))
        if queue_channel is None:
            await self._fail(
                interaction,
                queue_doc,
                "Salon de queue introuvable.",
                queue_type=queue_type,
            )
            return None

        # Reserve un numero de match atomique + cree la categorie Discord dynamiquement.
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
                "Erreur Discord lors de la creation de la categorie de match. Reessaie.",
                ephemeral=True,
            )
            return None
        category = channels.category
        prep_channel = channels.prep_channel
        free_cat_name = category.name

        plan = plan_match(players, free_category=free_cat_name, rng=self.rng)

        # Ordre de mise en place : on persiste le match (BDD) AVANT
        # d'annoncer sur Discord. Si la persistance echoue (Mongo down,
        # timeout), on ne veut PAS que les 10 joueurs voient un message
        # "Match trouve !" sans match doc associe (boutons morts,
        # /match-cancel ne trouve rien).
        #
        # Etape 1 : persister le match avec message_id=None. C'est le
        # point d'engagement : apres ca, le state machine du match a
        # une source de verite.
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

        # Etape 2 : ajustement de roles AVANT d'annoncer. Best-effort :
        # crash ici laisse roles partiels mais le match doc existe ->
        # /match-cancel nettoie.
        #
        # Consolidation 1 PATCH/joueur via member.edit(roles=...) :
        # diff atomique cote Discord, supprime les 429 observes en prod
        # (bucket per-guild PATCH /members/{u} ~10/10s).
        # Semaphore(5) en garde-fou.
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
                        reason="Match forme : setup roles",
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
                logger.warning("[match] role setup a echoue: %r", r)

        # Etape 3 : envoyer l'annonce.
        mentions = " ".join(f"<@{p.id}>" for p in players)
        embed = build_match_embed(plan, guild.name, queue_type)
        try:
            msg = await prep_channel.send(
                content=f"🎯 Match trouve ! {mentions}",
                embed=embed,
                view=self.vote_view,
            )
        except Exception:
            # L'annonce a echoue : on annule le match doc fraichement
            # cree pour eviter un orphelin que personne ne peut voter
            # (pas de message_id => VoteView introuvable).
            logger.exception("[match] prep_channel.send a leve, rollback match doc")
            matches_col = repository.get_matches_col(self.db)
            await asyncio.to_thread(
                matches_col.delete_one,
                {"_id": match_id},
            )
            await self._fail(
                interaction,
                queue_doc,
                "Echec de l'envoi de l'annonce match. Match annule.",
                queue_type=queue_type,
            )
            return None

        # Etape 4 : associer le message_id au match doc. Sans ca,
        # `get_match_by_message` (utilise par VoteView) ne retrouve pas
        # le match au moment du vote.
        matches_col = repository.get_matches_col(self.db)
        await asyncio.to_thread(
            matches_col.update_one,
            {"_id": match_id},
            {"$set": {"message_id": msg.id}},
        )

        # Etape 5 : vider la queue immediatement apres la persistance.
        # Empêche un re-trigger eventuel d'on_queue_full sur la meme queue.
        await asyncio.to_thread(
            repository.delete_active_queue,
            self.db,
            guild.id,
            queue_type,
        )

        # Etape 6 : deplacement vocal Waiting Room -> Team 1/Team 2 selon
        # l'assignation calculee par balance_teams. Les joueurs arrivent
        # directement dans leur VC d'equipe, plus besoin de re-split apres
        # le rassemblement Waiting Match.
        await self._move_players_to_match_vc(guild, free_cat_name, plan)

        # Etape 7 : repose setup-queue (best-effort) dans le salon de
        # destination de ce queue_type. On preserve le channel d'origine
        # (queue_doc.channel_id) si possible, sinon on tombe sur le
        # salon nomme QUEUE_CHANNEL_NAMES[queue_type].
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
                logger.exception("[match] echec re-post setup-queue")
        return match_id

    def _admin_role_ids(self, guild: discord.Guild) -> list[int]:
        """Renvoie les IDs des roles admin/staff a inclure dans les
        overwrites de la categorie de match.

        Couvre deux sources :
          1. Les roles nommes dans `ADMIN_ROLE_NAMES` (constante projet
             "Admin", "Match Staff", "Administrateur") : permet aux
             moderateurs custom sans permission `administrator` Discord
             de voir/gerer les categories de match dynamiques.
          2. Le role de bypass configure via /bypass (collection `bypass`
             en BDD, par guild). Utilise par les serveurs qui ont un role
             custom de moderation non liste dans ADMIN_ROLE_NAMES.

        Sans cette methode cablee, seuls les utilisateurs avec la
        permission Discord `administrator` (qui bypasse les overwrites)
        voient les categories -- ce qui exclut les staff custom.
        """
        # Iteration manuelle (pas `discord.utils.get`) : sur des Guild
        # mockes en test, `utils.get` peut renvoyer une coroutine via
        # le fallback `_aget` qui n'expose pas `.id`.
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
        """Renvoie les IDs des roles staff "viewers" a inclure dans les
        overwrites de la categorie de match (acces niveau joueur, pas admin).

        Ces roles (cf. MATCH_VIEWER_ROLE_NAMES) recoivent les memes droits
        que les 10 joueurs : view/send/connect/speak, sans manage_channels.
        Utile pour que le staff (Administrators, Moderators, Coach/Analyst,
        Head Administrators, THE HUB, Moderator En Chef) puisse suivre/aider
        sur n'importe quelle categorie de match sans avoir les pouvoirs
        admin (draft cancel, ping, gestion de salon).
        """
        return self._role_ids_by_names(guild, MATCH_VIEWER_ROLE_NAMES)

    def _spectator_role_ids(self, guild: discord.Guild) -> list[int]:
        """Renvoie les IDs des roles "spectateurs" (cf. MATCH_SPECTATOR_ROLE_NAMES,
        ex: "Members") : voient la categorie + lisent l'historique, mais ne
        peuvent ni rejoindre les vocaux ni envoyer de messages.
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
        """Deplace les 10 joueurs dans la VC d'equipe (`Team 1` / `Team 2`)
        de la categorie attribuee, selon `plan.teams.team_a` / `team_b`.
        Skip silencieusement les joueurs hors vocal ou deja sur place.

        Fallback gracieux si une VC d'equipe manque : on rabat sur l'autre
        si dispo, sinon sur `Waiting Match`, sinon no-op. Tous les joueurs
        valides ont ete auto-deplaces dans `Waiting Room` au clic sur
        Rejoindre (cf. queue_v2._move_to_waiting_room).
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

        # Mapping uid -> VC cible. team_a -> Team 1, team_b -> Team 2.
        # Si une VC d'equipe manque, on rabat sur l'autre puis sur Waiting Match
        # pour garantir que le joueur soit regroupe meme en config degradee.
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

        # Parallelisation : bucket per-member, mais on capt a 5 concurrents
        # via le semaphore partage avec les role edits pour ne pas saturer
        # le bucket Discord PATCH /members/{u} (~10/10s per-guild).
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
                        reason="Match forme : regroupement VC equipe",
                    )

        await asyncio.gather(
            *(_move_one(uid, dest) for uid, dest in targets.items()),
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
                    f"⚠️ {reason} Une nouvelle queue a ete reposee.",
                )
        except Exception:
            logger.exception("[match] _fail send a leve")
        # Repose une queue fraiche pour eviter d'obliger l'admin a refaire
        # /setup-queue manuellement apres chaque echec de formation.
        if channel is not None:
            queue_cog = self.bot.get_cog("QueueCog")
            if queue_cog is not None:
                try:
                    await queue_cog.post_queue_message(channel, queue_type)  # type: ignore[attr-defined]
                except Exception:
                    logger.exception("[match] _fail re-post queue a leve")

    # ── Hook : vote valide ───────────────────────────────────────
    async def _on_match_validated(self, inter, match_doc) -> None:
        """
        Vote valide : on NE TOUCHE PAS encore a l'ELO.
        L'ELO sera applique en une seule passe par `_verify_match`
        apres ~HENRIK_VERIFY_DELAY_MINUTES (avec ponderation ACS si
        HenrikDev a retrouve le custom, plat sinon).
        """
        guild = getattr(inter, "guild", None)

        # Annonce best-effort.
        if guild is None:
            return

        # Suppression de la catégorie Discord du match.
        # Graceful : les anciens matchs sans category_id sont ignorés.
        category_id = match_doc.get("category_id")
        if category_id:
            repository.mark_match_cleanup_started(self.db, match_doc["_id"])
            await delete_match_category(
                guild=guild,
                category_id=category_id,
                reason=(f"Match #{match_doc.get('match_number', '?')} vote validé"),
            )

        try:
            elo_log_channel = discord.utils.get(
                guild.text_channels,
                name="elo-adding",
            )
        except Exception:
            logger.exception("[match] lookup elo-adding a leve")
            return
        if elo_log_channel is None:
            return
        try:
            await elo_log_channel.send(
                f"⏳ Match valide ({match_doc.get('status')}). "
                f"Verification HenrikDev a partir de {HENRIK_VERIFY_DELAY_MINUTES} min "
                f"(retry chaque minute, abandon a {HENRIK_VERIFY_TIMEOUT_MINUTES} min)."
            )
        except discord.Forbidden:
            # Le bot n'a pas la permission Send Messages dans #elo-adding.
            # C'est un probleme de config recoltable par l'operateur.
            logger.warning(
                "[match] envoi annonce Henrik refuse (Forbidden) sur #%s "
                "guild=%s - verifier les permissions du bot.",
                elo_log_channel.name,
                guild.id,
            )
        except discord.HTTPException:
            # Erreur transitoire Discord (5xx, rate limit). On log mais
            # on n'echoue pas le flux ELO.
            logger.exception("[match] envoi annonce Henrik HTTP error")
        except Exception:
            logger.exception("[match] envoi annonce attente Henrik a leve")

    # ── Timeout des votes ────────────────────────────────────────
    async def check_vote_timeouts(self, *, now: datetime | None = None) -> int:
        """
        Scanne tous les guilds connus. Pour chaque match `pending` cree
        depuis plus de VOTE_TIMEOUT_MINUTES, marque `contested` et
        ping le role admin du salon.

        Returns:
            nombre de matches passes en `contested` cet appel
        """
        now = now or datetime.now(UTC)
        cutoff = now - timedelta(minutes=VOTE_TIMEOUT_MINUTES)

        # Traitement parallelise par guild : sur N guilds, l'execution
        # sequentielle attendrait la fin du scan + transitions de chaque
        # guild avant de passer a la suivante. asyncio.gather rend le
        # tick borne par la guild la plus lente, pas par leur somme.
        results = await asyncio.gather(
            *[self._check_vote_timeouts_for_guild(g, cutoff) for g in self.bot.guilds],
            return_exceptions=True,
        )
        flagged = 0
        for r in results:
            if isinstance(r, BaseException):
                logger.info(f"[match] check_vote_timeouts (guild) a leve : {r!r}")
                continue
            flagged += r
        return flagged

    async def _check_vote_timeouts_for_guild(self, guild, cutoff: datetime) -> int:
        flagged = 0
        col = repository.get_matches_col(self.db)

        # Scan en thread : `find().toList()` est bloquant et peut iterer
        # sur N matches, gelant l'event loop Discord.
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
            # Re-fetch atomique juste avant la transition pour eviter
            # une race avec un vote qui franchirait le seuil entre le
            # scan initial et maintenant. Sans ce re-fetch, on lirait
            # `match.get("votes")` du snapshot stale -> le tick pourrait
            # transitionner pending->contested alors qu'un vote concurrent
            # vient d'atteindre la majorite, laissant le match coince
            # en `contested` avec ELO jamais applique.
            fresh = await asyncio.to_thread(col.find_one, {"_id": match["_id"]})
            if not fresh or fresh.get("status") != "pending":
                continue
            votes = fresh.get("votes", {})
            count_a = sum(1 for v in votes.values() if v == "a")
            count_b = sum(1 for v in votes.values() if v == "b")
            # Auto-reparation : on backdate `validated_at` au moment
            # du `created_at` du match. Sans ce backdate, le delai
            # Henrik (~5min apres validated_at) repartirait de 0 ;
            # or le match a deja ete cree il y a > VOTE_TIMEOUT_MINUTES,
            # le custom HenrikDev est deja indexe et la verification
            # peut tourner immediatement au prochain tick.
            repaired_validated_at = fresh.get("created_at")
            if count_a >= MAJORITY_THRESHOLD:
                # Un match peut avoir atteint 7+ votes sans transition
                # (ex: crash bot entre l'ecriture du vote et set_match_status).
                # On recupere ; check_henrik_verifications appliquera
                # l'ELO au prochain tick.
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
        # Note : la transition vers "contested" est faite par
        # check_vote_timeouts via transition_match_status (CAS atomique).
        # On entre ici uniquement si la transition a reussi.

        # Revoke Match Host role; no longer governed by deferred cleanup.
        leader_id = match.get("lobby_leader_id")
        if leader_id is not None:
            leader_member = guild.get_member(int(leader_id))
            if leader_member is not None:
                host_role = discord.utils.get(guild.roles, name=MATCH_HOST_ROLE_NAME)
                if host_role is not None and host_role in leader_member.roles:
                    with contextlib.suppress(discord.Forbidden, discord.HTTPException):
                        await leader_member.remove_roles(
                            host_role, reason="Vote timeout : Match Host revoque"
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
                f"⏰ {ping} Vote du match en timeout (>{VOTE_TIMEOUT_MINUTES} min "
                f"sans {MAJORITY_THRESHOLD}/10). Score actuel : Team A `{count_a}` / Team B `{count_b}`. "
                f"Validation manuelle requise.",
            )
        except Exception:
            logger.exception("[match] _handle_timeout send a leve")

    # ── Verification HenrikDev + application ELO unique ──────────
    async def check_henrik_verifications(self, *, now: datetime | None = None) -> int:
        """Pour chaque match valide depuis > HENRIK_VERIFY_DELAY_MINUTES sans
        verification Henrik :
          - cherche le custom HenrikDev (multiplicateurs ACS si trouve)
          - si Henrik trouve : applique ELO pondere (definitif)
          - si Henrik ne trouve pas et qu'on est sous le timeout : on retentera
            au prochain tick (boucle 1 min)
          - si on a depasse HENRIK_VERIFY_TIMEOUT_MINUTES : applique ELO plat
            et marque le match comme verifie (abandon Henrik)
        Retourne le nombre de matches traites."""
        now = now or datetime.now(UTC)
        start_cutoff = now - timedelta(minutes=HENRIK_VERIFY_DELAY_MINUTES)
        timeout_cutoff = now - timedelta(minutes=HENRIK_VERIFY_TIMEOUT_MINUTES)

        # Parallelisation per-guild : meme principe que check_vote_timeouts.
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
                logger.info(f"[match] check_henrik_verifications (guild) a leve : {r!r}")
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
        # Scan bloquant -> thread pour ne pas geler l'event loop.
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
                logger.exception("[match] verify_match a leve")
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
        Tente la verif HenrikDev. Applique l'ELO si :
          - Henrik a trouve les multiplicateurs ACS (ELO pondere), OU
          - `force_apply` est True (timeout atteint -> ELO plat).
        Sinon : ne fait rien, le match sera retente au prochain tick.

        Idempotence : on **claim** le match (`elo_applied=True`) AVANT
        d'appliquer l'ELO. Si le claim echoue (deja applique ailleurs), on
        skip. Si l'application ELO leve, on relache le claim pour permettre
        un retry au prochain tick.
        """
        queue_type = match_doc.get("queue_type", "open")

        multipliers: dict[str, float] | None = None
        if self.henrik_client is not None:
            multipliers = await self._fetch_henrik_multipliers(guild, match_doc)

        if multipliers is None and not force_apply:
            # Pas trouve, pas en timeout -> on retentera dans 1 min.
            return

        # Claim atomique : seul le premier appel passe. Empeche la double
        # application en cas de crash entre apply_match_validation et
        # set_match_henrik_verified, ou de tick concurrent.
        claimed = await asyncio.to_thread(
            repository.claim_match_for_elo,
            self.db,
            match_doc["_id"],
        )
        if claimed is None:
            return  # Deja applique par un tick precedent.

        try:
            outcome = await asyncio.to_thread(
                apply_match_validation,
                self.db,
                match_doc,
                multipliers=multipliers,
            )
        except Exception:
            logger.exception("[match] apply_match_validation a leve")
            # Rollback du claim pour permettre un retry au prochain tick.
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
            found=multipliers is not None,
            multipliers=multipliers,
        )

        embed = build_elo_changes_embed(outcome, match_doc, guild.name)
        elo_log = discord.utils.get(guild.text_channels, name="elo-adding")
        if elo_log is not None:
            try:
                await elo_log.send(embed=embed)
            except Exception:
                logger.exception("[match] envoi recap ELO a leve")

        try:
            await refresh_leaderboard_channel(guild, self.db, queue_type)
        except Exception:
            logger.exception("[match] refresh leaderboard a leve")

    async def _fetch_henrik_multipliers(
        self,
        guild,
        match_doc: dict,
    ) -> dict[str, float] | None:
        """Tente de retrouver le custom HenrikDev et de calculer les
        multiplicateurs ACS. Retourne None si pas exploitable."""

        # 10 lookups riot (le leader est l'un des 10 joueurs choisi
        # aleatoirement, on le recupere au passage). Regroupes dans un
        # seul thread pour eviter de geler l'event loop pendant ~10x10ms.
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
            # Fallback : si le leader n'est plus dans les 10 (apres un
            # /match-replace par exemple), lookup direct.
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

        # Circuit breaker : si HenrikDev a echoue 3x de suite recemment,
        # on saute pendant 5 min. Sans ce garde, chaque tick (1 min)
        # relance N matches stale × 12s de retries chacun, gelant le
        # ThreadPoolExecutor et faisant overlap les ticks.
        # Lecture serialisee : sans le lock, plusieurs guilds en
        # parallele pouvaient observer un etat intermediaire (cf #17).
        now = datetime.now(UTC)
        async with self._henrik_lock:
            circuit_open = (
                self._henrik_circuit_open_until is not None
                and now < self._henrik_circuit_open_until
            )
        if circuit_open:
            return None

        # `find_henrik_custom_match` fait un appel HTTP synchrone (`requests`).
        # On l'execute dans un thread pour ne pas bloquer l'event loop Discord
        # pendant le timeout (jusqu'a 10s par appel).
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
                    "[match] Henrik circuit OPEN apres %d echecs consecutifs. "
                    "Reprise dans %d min. Derniere erreur : %r",
                    failures,
                    HENRIK_CIRCUIT_OPEN_MINUTES,
                    e,
                )
            else:
                logger.error(
                    "[match] Henrik echec (%d/%d) : %r",
                    failures,
                    HENRIK_CIRCUIT_FAIL_THRESHOLD,
                    e,
                    exc_info=True,
                )
            return None
        # Succes : reset le compteur d'echecs et ferme le circuit.
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
        # Si compute_acs_multipliers n'a rien pu extraire (les 2 teams cote
        # Riot sont mixtes : joueurs ont switche Attack/Defense en lobby),
        # on retourne None plutot qu'un dict vide. Sinon
        # apply_match_validation aurait `weighted=True` mais appliquerait
        # tout de meme un ELO plat (mults.get -> 1.0 par defaut), affichant
        # "Ponderation ACS appliquee" alors que rien ne l'est.
        if not multipliers:
            logger.warning(
                "[match] Henrik a trouve le custom %s mais compute_acs_multipliers "
                "n'a pu extraire aucun multiplicateur (teams mixtes Attack/Defense "
                "en lobby Valorant ?). ELO plat applique.",
                summary.matchid,
            )
            return None
        return multipliers

    # ── Loop periodique (1 min) ──────────────────────────────────
    @tasks.loop(minutes=1)
    async def _timeout_loop(self):
        try:
            await self.check_vote_timeouts()
        except Exception:
            logger.exception("[match] check_vote_timeouts a leve")
        try:
            await self.check_henrik_verifications()
        except Exception:
            logger.exception("[match] check_henrik_verifications a leve")
        try:
            await self.expire_stale_contested_matches()
        except Exception:
            logger.exception("[match] expire_stale_contested_matches a leve")

    async def expire_stale_contested_matches(self, *, now: datetime | None = None) -> int:
        """Auto-expire les matches en `contested` plus vieux que
        CONTESTED_EXPIRY_HOURS. Sans ca, un contested non resolu par admin
        gele les 10 joueurs dans le gate find_active_match_for_player.

        Scoping par guild : evite de toucher aux matches d'autres guilds.

        Returns:
            Nombre total de docs expires sur l'ensemble des guilds.
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
                logger.exception("[match] expire_stale_contested guild=%s a leve", guild.id)
                continue
            if n:
                logger.info(
                    "[match] auto-expire contested : %d match(es) cleaned_up dans guild %s",
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
        """Filet de securite : `tasks.loop` meurt silencieusement si une
        exception remonte hors du try/except interne du tick. Sans ce
        handler, les votes en timeout ne seraient plus jamais traites
        jusqu'au prochain redemarrage du bot."""
        # logger.error avec exc_info=tuple : preserve la stack du `error`
        # passe en argument (logger.exception() utilise sys.exc_info() qui
        # n'est pas l'`error` courant ici).
        logger.error(
            "[match] _timeout_loop a leve : %r",
            error,
            exc_info=(type(error), error, error.__traceback__),
        )
        try:
            self._timeout_loop.restart()
        except Exception:
            logger.exception("[match] _timeout_loop.restart() a leve")

    # ── Slash commands admin (cancel / replace) ─────────────────
    @app_commands.command(
        name="match-cancel",
        description="Annule le match en cours dans ce salon (admin)",
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    async def match_cancel(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        # CAS atomique : si un vote concurrent valide le match ou si
        # _verify_match claim l'ELO entre la lecture et l'ecriture, le
        # cancel echoue proprement plutot que de creer un etat incoherent.
        match = await asyncio.to_thread(
            repository.cancel_match_atomically,
            self.db,
            channel_id=interaction.channel_id,
        )
        if not match:
            await interaction.followup.send(
                "❌ Aucun match annulable trouve dans ce salon "
                "(status pending/validated/contested et ELO non applique).",
                ephemeral=True,
            )
            return

        category_name = match.get("category_name")

        # Revoke du role "Match Host" au lobby leader.
        leader_id = match.get("lobby_leader_id")
        if leader_id is not None:
            leader = interaction.guild.get_member(int(leader_id))
            if leader is not None:
                host_role = discord.utils.get(interaction.guild.roles, name=MATCH_HOST_ROLE_NAME)
                if host_role is not None and host_role in leader.roles:
                    with contextlib.suppress(discord.Forbidden, discord.HTTPException):
                        await leader.remove_roles(host_role, reason="Match annule")

        try:
            msg_id = match.get("message_id")
            if msg_id and interaction.channel:
                msg = await interaction.channel.fetch_message(int(msg_id))
                await msg.edit(view=None)
        except Exception:
            logger.exception("[match-cancel] retrait view a leve")

        # Suppression de la catégorie Discord du match.
        # Graceful : les anciens matchs sans category_id sont ignorés.
        category_id = match.get("category_id")
        if category_id:
            repository.mark_match_cleanup_started(self.db, match["_id"])
            await delete_match_category(
                guild=interaction.guild,
                category_id=category_id,
                reason=f"Match #{match.get('match_number', '?')} annule par admin",
            )

        await interaction.followup.send(
            f"✅ Match annule. Categorie `{category_name or '?'}` liberee.",
            ephemeral=True,
        )

    @app_commands.command(
        name="match-replace",
        description="Remplace un joueur dans le match en cours (admin)",
    )
    @app_commands.describe(
        quitter="Joueur a remplacer",
        remplacant="Nouveau joueur (doit avoir un compte Riot lie)",
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    async def match_replace(
        self,
        interaction: discord.Interaction,
        quitter: discord.Member,
        remplacant: discord.Member,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        if quitter.id == remplacant.id:
            await interaction.followup.send(
                "❌ Impossible de remplacer un joueur par lui-meme.",
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
                "❌ Aucun match en cours (status pending) dans ce salon.",
                ephemeral=True,
            )
            return

        team_key: str | None = None
        for tk in ("team_a", "team_b"):
            if any(int(p.get("id", 0)) == quitter.id for p in match.get(tk, [])):
                team_key = tk
                break
        if team_key is None:
            await interaction.followup.send(
                f"❌ {quitter.mention} n'est pas dans ce match.",
                ephemeral=True,
            )
            return

        if any(
            int(p.get("id", 0)) == remplacant.id
            for tk in ("team_a", "team_b")
            for p in match.get(tk, [])
        ):
            await interaction.followup.send(
                f"❌ {remplacant.mention} est deja dans ce match.",
                ephemeral=True,
            )
            return

        riot = await asyncio.to_thread(
            repository.get_riot_account,
            self.db,
            remplacant.id,
        )
        if not riot:
            await interaction.followup.send(
                f"❌ {remplacant.mention} n'a pas de compte Riot lie (`/link-riot Pseudo#TAG`).",
                ephemeral=True,
            )
            return

        # Lookup ELO du remplacant dans le queue_type du match en cours.
        # Le doc joueur utilise un compound _id `<uid>:<queue_type>`.
        match_queue_type = match.get("queue_type", "open")
        elo_col = repository.get_elo_col(self.db)
        elo_doc = await asyncio.to_thread(
            elo_col.find_one,
            {"_id": repository.player_doc_id(remplacant.id, match_queue_type)},
        )
        new_elo = int(elo_doc.get("elo", elo_calc.ELO_START)) if elo_doc else elo_calc.ELO_START

        # Refuse le replace si l'ecart est trop grand : les equipes
        # avaient ete equilibrees au moment de la formation, un swap
        # avec un ecart > MAX_REPLACE_ELO_DIFF casse cet equilibre et
        # l'ELO post-match ne refletera pas la vraie perf.
        quitter_player = next(
            (p for p in match[team_key] if int(p.get("id", 0)) == quitter.id),
            None,
        )
        quitter_elo = int(quitter_player.get("elo", 0)) if quitter_player else 0
        elo_diff = abs(quitter_elo - new_elo)
        if elo_diff > MAX_REPLACE_ELO_DIFF:
            await interaction.followup.send(
                f"❌ Ecart d'ELO trop important : {quitter.mention} "
                f"({quitter_elo}) vs {remplacant.mention} ({new_elo}) "
                f"-> diff={elo_diff} > {MAX_REPLACE_ELO_DIFF}. Les equipes "
                "seraient desequilibrees. Annule le match (`/match-cancel`) "
                "et reforme la queue.",
                ephemeral=True,
            )
            return

        new_player = {
            "id": remplacant.id,
            "name": remplacant.display_name,
            "elo": new_elo,
        }
        new_team = [new_player if int(p.get("id", 0)) == quitter.id else p for p in match[team_key]]
        # Si le quitter etait le lobby leader, transferer le role au
        # remplacant : sans ca, `_fetch_henrik_multipliers` interroge
        # l'historique Riot du lobby leader original (qui n'a pas joue
        # le custom) -> match jamais retrouve cote Henrik -> ELO plat
        # applique au lieu de la ponderation ACS attendue. Le role
        # Discord "Match Host" suit aussi.
        update: dict[str, Any] = {team_key: new_team}
        leader_replaced = int(match.get("lobby_leader_id", 0)) == int(quitter.id)
        if leader_replaced:
            update["lobby_leader_id"] = str(remplacant.id)
        # CAS sur le status : si entre temps un vote a fait passer le
        # match en validated_*/contested, on ne touche plus aux equipes.
        result = await asyncio.to_thread(
            matches_col.update_one,
            {"_id": match["_id"], "status": "pending"},
            {"$set": update},
        )
        if result.modified_count != 1:
            await interaction.followup.send(
                "❌ Le match a ete valide ou annule entre temps. Replace abandonne.",
                ephemeral=True,
            )
            return

        # Transfert du role "Match Host" si c'est le leader qu'on remplace.
        if leader_replaced:
            host_role = discord.utils.get(interaction.guild.roles, name=MATCH_HOST_ROLE_NAME)
            if host_role is not None:
                if host_role in quitter.roles:
                    with contextlib.suppress(discord.Forbidden, discord.HTTPException):
                        await quitter.remove_roles(
                            host_role, reason="Match replace : host transferred"
                        )
                with contextlib.suppress(discord.Forbidden, discord.HTTPException):
                    await remplacant.add_roles(host_role, reason="Match replace : host transferred")

        suffix = " (lobby host)" if leader_replaced else ""
        await interaction.followup.send(
            f"✅ {quitter.mention} remplace par {remplacant.mention} dans `{team_key}`{suffix}.",
            ephemeral=True,
        )

    @staticmethod
    def _resolve_match_id(match_id: str) -> ObjectId | str:
        """Convertit l'id saisi par l'admin en ObjectId.

        Les matchs crees via `repository.create_match` ont un `_id`
        ObjectId (insert_one sans `_id`). pymongo ne convertit PAS une hex
        string en ObjectId : `{"_id": "<hex>"}` ne matche jamais un doc a
        `_id` ObjectId. On convertit donc explicitement. Fallback sur la
        valeur brute si ce n'est pas une hex ObjectId valide, pour rester
        compatible avec d'eventuels docs legacy a `_id` string.
        """
        try:
            return ObjectId(match_id)
        except (InvalidId, TypeError):
            return match_id

    @app_commands.command(
        name="match-cleanup",
        description="(Admin) Force la suppression de la categorie d'un match dispute ou bloque.",
    )
    async def match_cleanup(self, interaction: discord.Interaction, match_id: str) -> None:
        """Admin-only force teardown for disputed/blocked matches."""
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message(
                "Cette commande est reservee aux administrateurs.",
                ephemeral=True,
            )
            return

        query_id = self._resolve_match_id(match_id)
        match = self.db["matches"].find_one({"_id": query_id})
        if match is None:
            await interaction.response.send_message(
                f"Match `{match_id}` introuvable.", ephemeral=True
            )
            return

        category_id = match.get("category_id")
        if not category_id:
            await interaction.response.send_message(
                f"Match `{match_id}` n'a pas de category_id (probablement un match pre-migration).",
                ephemeral=True,
            )
            return

        # On reutilise le `_id` reel du doc trouve pour les ops suivantes :
        # garantit qu'on cible le bon document quel que soit le type d'id.
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
        await interaction.response.send_message(f"Match `{match_id}` nettoye.", ephemeral=True)

    @match_cancel.error
    @match_replace.error
    async def _admin_perm_error(self, inter: discord.Interaction, error):
        if isinstance(error, app_commands.MissingPermissions):
            try:
                await inter.response.send_message(
                    "🚫 Reserve aux administrateurs.",
                    ephemeral=True,
                )
            except discord.InteractionResponded:
                await inter.followup.send(
                    "🚫 Reserve aux administrateurs.",
                    ephemeral=True,
                )

    # Statuts pour lesquels la categorie Discord du match doit etre preservee
    # par le cleanup orphelin au boot. Couvre :
    #   - "pending"      : match en cours, vote ouvert
    #   - "validated_a"  : team A gagne mais ELO pas encore applique (Henrik
    #                      verification differee de HENRIK_VERIFY_DELAY_MINUTES)
    #   - "validated_b"  : idem team B
    #   - "contested"    : timeout vote, en attente de resolution admin
    # Les statuts terminaux ("cancelled", "cleaned_up") ne sont PAS proteges :
    # leurs categories doivent disparaitre au boot si elles trainent encore.
    _ACTIVE_MATCH_STATUSES: tuple[str, ...] = (
        "pending",
        "validated_a",
        "validated_b",
        "contested",
    )

    async def cog_load(self) -> None:
        # `_timeout_loop.start()` lance `_before_loop` qui fait
        # `await self.bot.wait_until_ready()`. En test, `self.bot` est un
        # MagicMock dont l'attribut `wait_until_ready` n'est pas awaitable
        # -> TypeError silencieusement loggee par `tasks.Loop`. On detecte
        # ce cas et on skip le start dans les tests (le timeout-loop n'a
        # de sens qu'avec un gateway Discord vivant de toute facon).
        if isinstance(self.bot, commands.Bot):
            self._timeout_loop.start()
        # Auto-expire les contested qui trainent (admins qui font /win+/lose
        # sans /match-cancel). On le fait AVANT le calcul de active_ids :
        # un contested > CONTESTED_EXPIRY_HOURS doit etre cleaned_up et donc
        # NE PAS proteger sa categorie Discord du cleanup orphelin.
        try:
            await self.expire_stale_contested_matches()
        except Exception:
            logger.exception("[match] cog_load expire_stale_contested a leve")
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
            # Safety net : si un cleanup precedent s'est interrompu entre
            # `mark_match_cleanup_started` et la transition de status
            # terminal, on retire ces categories du jeu actif. orphan
            # cleanup reprendra `delete_match_category` (idempotent).
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
