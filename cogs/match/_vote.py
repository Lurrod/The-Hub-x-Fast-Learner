"""VoteView persistante : 2 boutons "Team A/B a gagne".

Logique :
- recherche du match par message_id
- CAS atomique sur status=pending pour eviter votes tardifs et double-validation
- transition_match_status (CAS) declenche `on_validated` UNIQUEMENT pour le vote
  qui fait basculer la majorite (pas pour les votes concurrents arrives apres).
"""

from __future__ import annotations

import asyncio
import logging

import discord

from services import repository

from cogs.match._constants import MAJORITY_THRESHOLD, VOTE_A_BTN_ID, VOTE_B_BTN_ID
from cogs.match._embeds import build_match_embed_from_doc


logger = logging.getLogger(__name__)


class VoteView(discord.ui.View):
    """View persistante : reporter le vainqueur du match (Team A / Team B)."""

    def __init__(self, db, on_validated=None) -> None:
        super().__init__(timeout=None)
        self.db = db
        self.on_validated = on_validated  # callable(inter, match_doc) -> awaitable

    async def _vote(self, inter: discord.Interaction, choice: str) -> None:
        # 1) Retrouver le match via le message_id
        match = await asyncio.to_thread(
            repository.get_match_by_message,
            self.db,
            inter.message.id,
        )
        if not match:
            await inter.response.send_message("❌ Match introuvable.", ephemeral=True)
            return

        # 2) Match deja valide -> refus
        if match.get("status") in ("validated_a", "validated_b"):
            await inter.response.send_message(
                "✅ Ce match est deja valide.",
                ephemeral=True,
            )
            return

        # 3) Verifie la participation
        all_player_ids = {str(p["id"]) for p in (match.get("team_a", []) + match.get("team_b", []))}
        if str(inter.user.id) not in all_player_ids:
            await inter.response.send_message(
                "❌ Tu n'as pas joue ce match, tu ne peux pas voter.",
                ephemeral=True,
            )
            return

        # 4) Enregistre le vote (ecrase un vote precedent). CAS sur
        # status=pending : si le match a ete annule/conteste/valide
        # entre-temps, le vote est rejete proprement.
        updated = await asyncio.to_thread(
            repository.add_match_vote,
            self.db,
            match["_id"],
            inter.user.id,
            choice,
        )
        if updated is None:
            await inter.response.send_message(
                "❌ Ce match n'est plus en cours de vote (annule, conteste ou deja valide).",
                ephemeral=True,
            )
            return

        # 5) Compte
        votes = updated.get("votes", {})
        count_a = sum(1 for v in votes.values() if v == "a")
        count_b = sum(1 for v in votes.values() if v == "b")

        # 6) Majorite atteinte ? Transition atomique (CAS) pour eviter
        #    qu'un vote concurrent ne valide deux fois et ne declenche
        #    `on_validated` plusieurs fois.
        target_status = None
        if count_a >= MAJORITY_THRESHOLD:
            target_status = "validated_a"
        elif count_b >= MAJORITY_THRESHOLD:
            target_status = "validated_b"

        transitioned_doc = None
        if target_status:
            transitioned_doc = await asyncio.to_thread(
                lambda: repository.transition_match_status(
                    self.db,
                    match["_id"],
                    from_status="pending",
                    to_status=target_status,
                ),
            )
            if transitioned_doc is not None:
                updated = transitioned_doc
            else:
                # Un autre vote concurrent a deja valide. On re-fetch pour
                # afficher l'etat reel sans tirer `on_validated` de notre cote.
                fetched = await asyncio.to_thread(
                    repository.get_match,
                    self.db,
                    match["_id"],
                )
                updated = fetched or updated

        # 7) Edit du message (embed maj, view retiree si valide)
        embed = build_match_embed_from_doc(updated, inter.guild.name)
        if updated.get("status") in ("validated_a", "validated_b"):
            await inter.response.edit_message(embed=embed, view=None)
        else:
            await inter.response.edit_message(embed=embed, view=self)

        # 8) Hook Phase 6 : MAJ ELO. Tire UNIQUEMENT si la transition CAS a
        #    reussi de notre cote (i.e. ce vote-ci est celui qui a fait
        #    basculer le match).
        if transitioned_doc is not None and self.on_validated:
            try:
                await self.on_validated(inter, transitioned_doc)
            except Exception:
                logger.exception("[vote] on_validated a leve")

    @discord.ui.button(
        label="Team A a gagne",
        style=discord.ButtonStyle.primary,
        custom_id=VOTE_A_BTN_ID,
    )
    async def vote_a(self, inter: discord.Interaction, button: discord.ui.Button):
        await self._vote(inter, "a")

    @discord.ui.button(
        label="Team B a gagne",
        style=discord.ButtonStyle.primary,
        custom_id=VOTE_B_BTN_ID,
    )
    async def vote_b(self, inter: discord.Interaction, button: discord.ui.Button):
        await self._vote(inter, "b")
