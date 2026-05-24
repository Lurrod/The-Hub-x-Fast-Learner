"""
Cog candidatures + welcome + report. Extrait de bot.py (refactor monolithe).

Contient :
  - Systeme de candidatures (ApplicationModal, StaffModal, RefuseReasonModal,
    RoleChoiceView, WelcomeView, ApplicationReviewView).
  - /welcome : pose le bouton Postuler dans #verify.
  - /report : pose le panel d'ouverture de ticket (TicketPanelView) avec 2
    options dans le salon courant :
      * Reports -> ReportModal (signalement anonyme).
      * Ranks   -> RankModal (candidature de rank, candidat identifie).
  - _open_ticket_channel : cree le salon `ticket-{N}` (mutualise Reports/Ranks).
  - CloseTicketView : ferme un ticket.

Toutes les views persistantes (custom_id stable) sont enregistrees via
`bot.add_view(...)` dans `setup()`. Les Modals et la RoleChoiceView (timeout =
APPLICATION_VIEW_TIMEOUT_SECONDS) sont instancies a la volee.
"""

from __future__ import annotations

import contextlib
import logging
from datetime import UTC, datetime, timedelta

import discord
from discord import app_commands
from discord.ext import commands
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from services import repository

logger = logging.getLogger(__name__)

# Timeout d'une RoleChoiceView ephemere (Joueur vs Staff). Decouple
# volontairement de VOTE_TIMEOUT_MINUTES : c'est une UX rapide, pas le flow
# match.
APPLICATION_VIEW_TIMEOUT_SECONDS: int = 60


# ── Constantes ───────────────────────────────────────────────────
CANDIDATURE_CHANNEL = "candidatures"
WELCOME_CHANNEL = "verify"
PLAYERS_ROLE = "Members"
STAFF_ROLE = "Coach/Analyst/Manager"
TICKETS_CATEGORY_NAME = "Tickets"
CANDIDATURE_COOLDOWN_SECONDS = 3600


def _has_access(interaction: discord.Interaction, db) -> bool:
    """Reproduit `bot.has_access` sans dependance circulaire.

    Admin (manage_guild) OU role bypass configure via /bypass.
    """
    if interaction.user.guild_permissions.manage_guild:
        return True
    role_id = repository.get_bypass_role(db, interaction.guild_id)
    return bool(role_id and any(r.id == role_id for r in interaction.user.roles))


def _try_acquire_candidature_cooldown(db, uid: str) -> tuple[bool, float]:
    """Tente d'acquerir atomiquement un slot de cooldown candidature.

    Resout la race read-then-write : deux soumissions concurrentes ne
    peuvent pas toutes deux passer le check (CAS via update conditionnel
    + insert avec gestion DuplicateKeyError).
    """
    now = datetime.now(UTC)
    cutoff = now - timedelta(seconds=CANDIDATURE_COOLDOWN_SECONDS)
    cooldown_col = db["candidature_cooldowns"]
    res = cooldown_col.update_one(
        {"_id": uid, "last_apply": {"$lt": cutoff}},
        {"$set": {"last_apply": now}},
    )
    if res.modified_count == 1:
        return True, 0.0
    try:
        cooldown_col.insert_one({"_id": uid, "last_apply": now})
        return True, 0.0
    except DuplicateKeyError:
        pass
    doc = cooldown_col.find_one({"_id": uid})
    if doc is None:
        return True, 0.0
    last = doc["last_apply"]
    if last.tzinfo is None:
        last = last.replace(tzinfo=UTC)
    remaining = CANDIDATURE_COOLDOWN_SECONDS - (now - last).total_seconds()
    if remaining <= 0:
        return True, 0.0
    return False, remaining


def _parse_application_embed(message: discord.Message) -> tuple[int | None, str, bool]:
    """Extrait (applicant_id, pseudo, is_staff) depuis l'embed d'une candidature.

    Permet a `ApplicationReviewView` d'etre persistante (sans state interne)
    en reconstruisant le contexte depuis le message a chaque clic.
    """
    if not message.embeds:
        return None, "", False
    embed = message.embeds[0]
    is_staff = "Staff" in (embed.title or "")
    applicant_id: int | None = None
    footer_text = (embed.footer.text or "") if embed.footer else ""
    if footer_text.startswith("ID:"):
        try:
            applicant_id = int(footer_text.split(":", 1)[1].strip())
        except (ValueError, IndexError):
            applicant_id = None
    pseudo = ""
    for field in embed.fields:
        if field.name in ("🎮 Pseudo en jeu", "🎮 Pseudo"):
            pseudo = field.value or ""
            break
    return applicant_id, pseudo, is_staff


# ── Modals ────────────────────────────────────────────────────────
class ApplicationModal(discord.ui.Modal, title="Candidature 10mans"):
    pseudo: discord.ui.TextInput = discord.ui.TextInput(
        label="Quel est ton pseudo ?",
        placeholder="Comment puis-je t'appeler ? ex : jetax",
        max_length=50,
    )
    tracker: discord.ui.TextInput = discord.ui.TextInput(
        label="Lien vers ton tracker", placeholder="https://tracker.gg/...", max_length=200
    )
    experience: discord.ui.TextInput = discord.ui.TextInput(
        label="Experiences en tournois / LAN ?",
        placeholder="Indique les tournois/lans auxquels tu as participe",
        style=discord.TextStyle.paragraph,
        required=False,
        max_length=500,
    )

    def __init__(self, db, review_view: ApplicationReviewView) -> None:
        super().__init__()
        self.db = db
        self.review_view = review_view

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        uid = str(interaction.user.id)
        allowed, remaining = _try_acquire_candidature_cooldown(self.db, uid)
        if not allowed:
            minutes = int(remaining // 60)
            seconds = int(remaining % 60)
            await interaction.followup.send(
                f"⏳ Tu as déjà postulé récemment ! Réessaie dans **{minutes}min {seconds}s**.",
                ephemeral=True,
            )
            return
        with contextlib.suppress(discord.Forbidden):
            await interaction.user.send(
                embed=discord.Embed(
                    title="✅ Candidature reçue !",
                    description="Merci d'avoir postulé, nous analysons votre profil et nous revenons vers vous le plus vite possible.",
                    color=0x2ECC71,
                    timestamp=datetime.now(UTC),
                )
            )
        channel = discord.utils.get(interaction.guild.text_channels, name=CANDIDATURE_CHANNEL)
        if not channel:
            await interaction.followup.send("Salon candidatures introuvable.", ephemeral=True)
            return
        embed = discord.Embed(
            title="📋 Nouvelle candidature",
            description="🎮 **Candidature Joueur**",
            color=0x5865F2,
            timestamp=datetime.now(UTC),
        )
        embed.set_thumbnail(url=interaction.user.display_avatar.url)
        embed.add_field(name="👤 Membre", value=interaction.user.mention, inline=True)
        embed.add_field(name="🎮 Pseudo en jeu", value=self.pseudo.value, inline=True)
        embed.add_field(name="🔗 Tracker", value=self.tracker.value, inline=False)
        embed.add_field(
            name="🏆 Tournois / LAN",
            value=self.experience.value if self.experience.value else "Aucune",
            inline=False,
        )
        embed.set_footer(text=f"ID: {interaction.user.id}")
        msg = await channel.send(embed=embed, view=self.review_view)
        repository.register_application(
            self.db,
            interaction.guild_id,
            msg.id,
            interaction.user.id,
            is_staff=False,
        )
        await interaction.followup.send("✅ Ta candidature a bien été envoyée !", ephemeral=True)


class StaffModal(discord.ui.Modal, title="Candidature Staff"):
    pseudo: discord.ui.TextInput = discord.ui.TextInput(
        label="Quel est ton pseudo ?",
        placeholder="Comment puis-je t'appeler ? ex : jetax",
        max_length=50,
    )
    poste: discord.ui.TextInput = discord.ui.TextInput(
        label="Poste occupe actuellement",
        placeholder="Ex : Coach, Analyst, Manager... et dans quelle structure/organisation ?",
        max_length=100,
    )
    experience: discord.ui.TextInput = discord.ui.TextInput(
        label="Experiences",
        placeholder="Decris tes experiences dans le domaine...",
        style=discord.TextStyle.paragraph,
        required=False,
        max_length=500,
    )

    def __init__(self, db, review_view: ApplicationReviewView) -> None:
        super().__init__()
        self.db = db
        self.review_view = review_view

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        uid = str(interaction.user.id)
        allowed, remaining = _try_acquire_candidature_cooldown(self.db, uid)
        if not allowed:
            minutes = int(remaining // 60)
            seconds = int(remaining % 60)
            await interaction.followup.send(
                f"⏳ Tu as déjà postulé récemment ! Réessaie dans **{minutes}min {seconds}s**.",
                ephemeral=True,
            )
            return
        with contextlib.suppress(discord.Forbidden):
            await interaction.user.send(
                embed=discord.Embed(
                    title="✅ Candidature reçue !",
                    description="Merci d'avoir postulé, nous analysons votre profil et nous revenons vers vous le plus vite possible.",
                    color=0x2ECC71,
                    timestamp=datetime.now(UTC),
                )
            )
        channel = discord.utils.get(interaction.guild.text_channels, name=CANDIDATURE_CHANNEL)
        if not channel:
            await interaction.followup.send("Salon candidatures introuvable.", ephemeral=True)
            return
        embed = discord.Embed(
            title="📋 Nouvelle candidature Staff",
            description="🎯 **Candidature Coach / Analyst / Manager**",
            color=0xE67E22,
            timestamp=datetime.now(UTC),
        )
        embed.set_thumbnail(url=interaction.user.display_avatar.url)
        embed.add_field(name="👤 Membre", value=interaction.user.mention, inline=True)
        embed.add_field(name="🎮 Pseudo", value=self.pseudo.value, inline=True)
        embed.add_field(name="💼 Poste", value=self.poste.value, inline=False)
        embed.add_field(
            name="📋 Expériences",
            value=self.experience.value if self.experience.value else "Aucune",
            inline=False,
        )
        embed.set_footer(text=f"ID: {interaction.user.id}")
        msg = await channel.send(embed=embed, view=self.review_view)
        repository.register_application(
            self.db,
            interaction.guild_id,
            msg.id,
            interaction.user.id,
            is_staff=True,
        )
        await interaction.followup.send("✅ Ta candidature a bien été envoyée !", ephemeral=True)


class RefuseReasonModal(discord.ui.Modal, title="Raison du refus"):
    reason: discord.ui.TextInput = discord.ui.TextInput(
        label="Raison du refus (optionnel)",
        placeholder="Explique pourquoi...",
        style=discord.TextStyle.paragraph,
        required=False,
        max_length=500,
    )

    def __init__(self, db, applicant_id: int):
        super().__init__()
        self.db = db
        self.applicant_id = applicant_id

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        claimed = repository.claim_application_decision(
            self.db,
            interaction.guild_id,
            interaction.message.id,
            status="refused",
            decided_by=interaction.user.id,
        )
        if not claimed:
            await interaction.followup.send(
                "❌ Cette candidature a deja ete traitee par un autre admin.",
                ephemeral=True,
            )
            return
        member = interaction.guild.get_member(self.applicant_id)
        reason_text = self.reason.value if self.reason.value else "Aucune raison fournie."
        if member:
            try:
                embed_dm = discord.Embed(
                    title="❌ Candidature refusée",
                    description="Désolé, votre candidature n'a pas été retenue, merci de réessayer plus tard.",
                    color=0xE74C3C,
                    timestamp=datetime.now(UTC),
                )
                embed_dm.add_field(name="📋 Raison", value=reason_text, inline=False)
                await member.send(embed=embed_dm)
            except discord.Forbidden:
                pass
            with contextlib.suppress(discord.Forbidden):
                await member.kick(reason=f"Candidature refusee : {reason_text}")
        try:
            embed = interaction.message.embeds[0]
            embed.color = 0xE74C3C
            embed.add_field(name="Refuse par", value=interaction.user.mention, inline=True)
            embed.add_field(name="📋 Raison", value=reason_text, inline=True)
            await interaction.message.edit(embed=embed, view=None)
        except Exception:
            with contextlib.suppress(Exception):
                await interaction.message.edit(view=None)
        await interaction.followup.send(
            "✅ Candidature refusée et utilisateur kické.", ephemeral=True
        )


async def _open_ticket_channel(
    interaction: discord.Interaction, db
) -> discord.TextChannel | None:
    """Cree le salon `ticket-{N}` dans la categorie `Tickets`.

    Mutualise par les tickets Reports et Ranks. Renvoie le salon cree, ou
    `None` si l'operation echoue (dans ce cas l'utilisateur a deja recu un
    message d'erreur ephemere via `followup`). L'appelant doit avoir defer
    l'interaction au prealable (`defer(..., thinking=True)`).
    """
    guild = interaction.guild
    if guild is None:
        await interaction.followup.send(
            "❌ Cette commande doit etre utilisee dans un serveur.",
            ephemeral=True,
        )
        return None

    category = discord.utils.get(guild.categories, name=TICKETS_CATEGORY_NAME)
    if category is None:
        try:
            category = await guild.create_category(TICKETS_CATEGORY_NAME)
        except discord.Forbidden:
            await interaction.followup.send(
                "❌ Le bot n'a pas la permission **Gerer les salons** pour "
                f"creer la categorie `{TICKETS_CATEGORY_NAME}`.",
                ephemeral=True,
            )
            return None

    # Le compteur est incremente AVANT la creation du salon : si la creation
    # echoue (Forbidden), le numero est "consomme" et il restera un trou dans
    # la numerotation. C'est volontairement tolere — les trous dans les numeros
    # de tickets sont inoffensifs et evitent une logique de rollback fragile.
    counter_doc = db["ticket_counters"].find_one_and_update(
        {"_id": str(guild.id)},
        {"$inc": {"counter": 1}},
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )
    next_number = int(counter_doc["counter"])
    channel_name = f"ticket-{next_number}"

    try:
        return await guild.create_text_channel(channel_name, category=category)
    except discord.Forbidden:
        await interaction.followup.send(
            "❌ Le bot n'a pas la permission de creer le salon ticket.",
            ephemeral=True,
        )
        return None


class ReportModal(discord.ui.Modal, title="Envoyer un report anonyme"):
    cible: discord.ui.TextInput = discord.ui.TextInput(
        label="Qui report-tu ?",
        placeholder="Pseudo Discord / @mention / ID du joueur",
        style=discord.TextStyle.short,
        required=True,
        max_length=200,
    )
    queue: discord.ui.TextInput = discord.ui.TextInput(
        label="Dans quelle queue ?",
        placeholder="Pro / Open / GC",
        style=discord.TextStyle.short,
        required=True,
        max_length=50,
    )
    raison: discord.ui.TextInput = discord.ui.TextInput(
        label="Pour quelle raison ?",
        placeholder="Triche, toxicite, throw, insultes, AFK, etc.",
        style=discord.TextStyle.short,
        required=True,
        max_length=200,
    )
    details: discord.ui.TextInput = discord.ui.TextInput(
        label="Details / contexte",
        placeholder="Decris la situation : quand, ou, ce qu'il s'est passe...",
        style=discord.TextStyle.paragraph,
        required=True,
        max_length=1500,
    )
    preuves: discord.ui.TextInput = discord.ui.TextInput(
        label="Preuves (liens, clips, screens)",
        placeholder="Colle ici les liens vers tes preuves (optionnel)",
        style=discord.TextStyle.paragraph,
        required=False,
        max_length=1000,
    )

    def __init__(self, db, close_view: CloseTicketView) -> None:
        super().__init__()
        self.db = db
        self.close_view = close_view

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        ticket_channel = await _open_ticket_channel(interaction, self.db)
        if ticket_channel is None:
            return

        embed = discord.Embed(
            title=f"🎫 Nouveau report — {ticket_channel.name}",
            color=0xE67E22,
            timestamp=datetime.now(UTC),
        )
        embed.add_field(name="Joueur reporte", value=self.cible.value, inline=False)
        embed.add_field(name="Queue concernee", value=self.queue.value, inline=False)
        embed.add_field(name="Raison", value=self.raison.value, inline=False)
        embed.add_field(name="Details", value=self.details.value, inline=False)
        if self.preuves.value.strip():
            embed.add_field(name="Preuves", value=self.preuves.value, inline=False)
        embed.set_footer(text="Report anonyme")
        try:
            await ticket_channel.send(embed=embed, view=self.close_view)
        except discord.HTTPException:
            logger.exception("[ticket] envoi du message initial a leve")
            await interaction.followup.send(
                "❌ Une erreur est survenue lors de l'envoi de ton report.",
                ephemeral=True,
            )
            return

        await interaction.followup.send(
            f"✅ Ton report anonyme a ete envoye ({ticket_channel.mention}).",
            ephemeral=True,
        )


class RankModal(discord.ui.Modal, title="Candidature de rank"):
    """Ouvre un ticket de candidature de rank (candidat identifie).

    Pose 3 questions automatiquement puis cree un salon `ticket-{N}` dans la
    categorie `Tickets` avec un embed recapitulatif + bouton de fermeture.
    """

    rank: discord.ui.TextInput = discord.ui.TextInput(
        label="Pour quel rank souhaites-tu postuler ?",
        placeholder="Pro Queue / Open Queue / GC Queue",
        style=discord.TextStyle.short,
        required=True,
        max_length=100,
    )
    tracker: discord.ui.TextInput = discord.ui.TextInput(
        label="Le lien de ton tracker",
        placeholder="https://tracker.gg/valorant/profile/...",
        style=discord.TextStyle.short,
        required=True,
        max_length=300,
    )
    experience: discord.ui.TextInput = discord.ui.TextInput(
        label="Ton experience en tournois/LANs et/ou VLR",
        placeholder="Decris ton parcours competitif : tournois, LANs, equipes VLR...",
        style=discord.TextStyle.paragraph,
        required=True,
        max_length=1500,
    )

    def __init__(self, db, close_view: CloseTicketView) -> None:
        super().__init__()
        self.db = db
        self.close_view = close_view

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        ticket_channel = await _open_ticket_channel(interaction, self.db)
        if ticket_channel is None:
            return

        embed = discord.Embed(
            title=f"🎖️ Candidature Rank — {ticket_channel.name}",
            color=0x9B59B6,
            timestamp=datetime.now(UTC),
        )
        embed.add_field(name="Membre", value=interaction.user.mention, inline=False)
        embed.add_field(name="Rank vise", value=self.rank.value, inline=False)
        embed.add_field(name="Tracker", value=self.tracker.value, inline=False)
        embed.add_field(
            name="Experience (tournois / LANs / VLR)",
            value=self.experience.value,
            inline=False,
        )
        embed.set_footer(text=f"Candidature de {interaction.user}")
        try:
            await ticket_channel.send(embed=embed, view=self.close_view)
        except discord.HTTPException:
            logger.exception("[ticket] envoi du message initial (rank) a leve")
            await interaction.followup.send(
                "❌ Une erreur est survenue lors de l'envoi de ta candidature.",
                ephemeral=True,
            )
            return

        await interaction.followup.send(
            f"✅ Ta candidature de rank a ete envoyee ({ticket_channel.mention}).",
            ephemeral=True,
        )


# ── Views ────────────────────────────────────────────────────────
class ApplicationReviewView(discord.ui.View):
    """Vue persistante : se reconstruit a partir de l'embed du message."""

    def __init__(self, db) -> None:
        super().__init__(timeout=None)
        self.db = db

    @discord.ui.button(
        label="Accepter",
        style=discord.ButtonStyle.success,
        custom_id="application_accept",
    )
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not _has_access(interaction, self.db):
            await interaction.response.send_message(
                "❌ Tu n'as pas la permission de traiter les candidatures.",
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True)
        # 1) Valider AVANT le CAS (cf. audit : evite l'etat coince).
        applicant_id, pseudo, is_staff = _parse_application_embed(interaction.message)
        if applicant_id is None:
            await interaction.followup.send(
                "❌ Donnees candidature illisibles (embed corrompu).",
                ephemeral=True,
            )
            return
        member = interaction.guild.get_member(applicant_id)
        if not member:
            await interaction.followup.send("❌ Membre introuvable.", ephemeral=True)
            return
        # 2) CAS atomique
        claimed = repository.claim_application_decision(
            self.db,
            interaction.guild_id,
            interaction.message.id,
            status="accepted",
            decided_by=interaction.user.id,
        )
        if not claimed:
            await interaction.followup.send(
                "❌ Cette candidature a deja ete traitee par un autre admin.",
                ephemeral=True,
            )
            return
        try:
            old_embed = interaction.message.embeds[0] if interaction.message.embeds else None
            new_embed = discord.Embed(
                title="📋 Candidature acceptée", color=0x2ECC71, timestamp=datetime.now(UTC)
            )
            new_embed.set_thumbnail(url=member.display_avatar.url)
            new_embed.add_field(name="👤 Membre", value=member.mention, inline=True)
            new_embed.add_field(name="🎮 Pseudo", value=pseudo, inline=True)
            if old_embed:
                for field in old_embed.fields:
                    if field.name in (
                        "🔗 Tracker",
                        "🏆 Tournois / LAN",
                        "💼 Poste",
                        "📋 Expériences",
                        "Tracker",
                        "Tournois / LAN",
                        "Poste",
                        "Experiences",
                    ):
                        new_embed.add_field(name=field.name, value=field.value, inline=False)
            new_embed.add_field(name="✅ Accepté par", value=interaction.user.mention, inline=False)
            await interaction.message.edit(embed=new_embed, view=None)
        except Exception:
            logger.exception("[accept] Edit impossible")
            with contextlib.suppress(Exception):
                await interaction.message.edit(view=None)
        role_name = STAFF_ROLE if is_staff else PLAYERS_ROLE
        role = discord.utils.get(interaction.guild.roles, name=role_name)
        if role:
            try:
                await member.add_roles(role)
            except Exception:
                logger.exception("[accept] Role impossible")
        if is_staff:
            members_role = discord.utils.get(interaction.guild.roles, name=PLAYERS_ROLE)
            if members_role:
                try:
                    await member.add_roles(members_role)
                except Exception:
                    logger.exception("[accept] Role Members impossible")
        with contextlib.suppress(Exception):
            await member.edit(nick=pseudo)
        with contextlib.suppress(discord.Forbidden):
            await member.send(
                embed=discord.Embed(
                    title="🎉 Candidature acceptée !",
                    description="Bravo, vous avez été accepté, vous pouvez désormais faire des 10mans !",
                    color=0x2ECC71,
                    timestamp=datetime.now(UTC),
                )
            )
        await interaction.followup.send("✅ Candidature acceptée !", ephemeral=True)

    @discord.ui.button(
        label="Refuser",
        style=discord.ButtonStyle.danger,
        custom_id="application_refuse",
    )
    async def refuse(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not _has_access(interaction, self.db):
            await interaction.response.send_message(
                "❌ Tu n'as pas la permission de traiter les candidatures.",
                ephemeral=True,
            )
            return
        applicant_id, _pseudo, _is_staff = _parse_application_embed(interaction.message)
        if applicant_id is None:
            await interaction.response.send_message(
                "❌ Donnees candidature illisibles (embed corrompu).",
                ephemeral=True,
            )
            return
        await interaction.response.send_modal(
            RefuseReasonModal(db=self.db, applicant_id=applicant_id)
        )


class RoleChoiceView(discord.ui.View):
    """Vue ephemere (timeout = APPLICATION_VIEW_TIMEOUT_SECONDS) : Joueur vs Staff."""

    def __init__(self, db, review_view: ApplicationReviewView) -> None:
        super().__init__(timeout=APPLICATION_VIEW_TIMEOUT_SECONDS)
        self.db = db
        self.review_view = review_view

    @discord.ui.button(label="Joueur", style=discord.ButtonStyle.primary)
    async def joueur_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(
            ApplicationModal(db=self.db, review_view=self.review_view)
        )

    @discord.ui.button(label="Coach / Analyst / Manager", style=discord.ButtonStyle.secondary)
    async def staff_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(StaffModal(db=self.db, review_view=self.review_view))


class WelcomeView(discord.ui.View):
    """Vue persistante : bouton Postuler dans #verify."""

    def __init__(self, db, review_view: ApplicationReviewView) -> None:
        super().__init__(timeout=None)
        self.db = db
        self.review_view = review_view

    @discord.ui.button(
        label="Postuler", style=discord.ButtonStyle.primary, custom_id="postuler_btn"
    )
    async def postuler(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Peek non-atomique : on ne consomme pas le cooldown ici (sinon
        # l'utilisateur qui ferme le modal sans submit serait bloque 1h
        # pour rien). Le vrai claim atomique a lieu dans
        # ApplicationModal/StaffModal.on_submit.
        uid = str(interaction.user.id)
        doc = self.db["candidature_cooldowns"].find_one({"_id": uid})
        if doc:
            last = doc["last_apply"]
            if last.tzinfo is None:
                last = last.replace(tzinfo=UTC)
            diff = datetime.now(UTC) - last
            if diff.total_seconds() < CANDIDATURE_COOLDOWN_SECONDS:
                remaining = CANDIDATURE_COOLDOWN_SECONDS - diff.total_seconds()
                minutes = int(remaining // 60)
                seconds = int(remaining % 60)
                await interaction.response.send_message(
                    f"⏳ Tu as déjà postulé récemment ! Réessaie dans **{minutes}min {seconds}s**.",
                    ephemeral=True,
                )
                return
        await interaction.response.send_message(
            "## Pour quel poste souhaites-tu postuler ? 🎮",
            view=RoleChoiceView(db=self.db, review_view=self.review_view),
            ephemeral=True,
        )


class CloseTicketView(discord.ui.View):
    """Vue persistante : un bouton 'Fermer le ticket' qui supprime le salon."""

    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Fermer le ticket",
        style=discord.ButtonStyle.danger,
        custom_id="ticket_close_btn",
    )
    async def close(self, interaction: discord.Interaction, button: discord.ui.Button):
        channel = interaction.channel
        if channel is None or not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message(
                "❌ Impossible de fermer ce salon ici.",
                ephemeral=True,
            )
            return
        with contextlib.suppress(discord.HTTPException):
            await interaction.response.send_message(
                "🔒 Fermeture du ticket...",
                ephemeral=True,
            )
        try:
            await channel.delete(reason=f"Ticket ferme par {interaction.user}")
        except discord.NotFound:
            pass
        except discord.Forbidden:
            with contextlib.suppress(discord.HTTPException):
                await interaction.followup.send(
                    "❌ Permission manquante pour supprimer ce salon.",
                    ephemeral=True,
                )
        except discord.HTTPException:
            logger.exception("[ticket] suppression du salon a leve")


class ReportView(discord.ui.View):
    """Vue persistante : un bouton 'Report' qui ouvre le ReportModal."""

    def __init__(self, db, close_view: CloseTicketView) -> None:
        super().__init__(timeout=None)
        self.db = db
        self.close_view = close_view

    @discord.ui.button(
        label="Report",
        style=discord.ButtonStyle.danger,
        custom_id="report_open_btn",
    )
    async def open_report(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(ReportModal(db=self.db, close_view=self.close_view))


class TicketPanelView(discord.ui.View):
    """Vue persistante : panel d'ouverture de ticket a 2 options.

    - **Reports** -> ReportModal (signalement anonyme).
    - **Ranks**   -> RankModal (candidature de rank, candidat identifie).
    """

    def __init__(self, db, close_view: CloseTicketView) -> None:
        super().__init__(timeout=None)
        self.db = db
        self.close_view = close_view

    @discord.ui.button(
        label="Reports",
        style=discord.ButtonStyle.danger,
        emoji="🚨",
        custom_id="ticket_panel_reports_btn",
    )
    async def open_reports(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(ReportModal(db=self.db, close_view=self.close_view))

    @discord.ui.button(
        label="Ranks",
        style=discord.ButtonStyle.primary,
        emoji="🎖️",
        custom_id="ticket_panel_ranks_btn",
    )
    async def open_ranks(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(RankModal(db=self.db, close_view=self.close_view))


# ── Cog ──────────────────────────────────────────────────────────
class ApplicationsCog(commands.Cog):
    def __init__(self, bot: commands.Bot, db) -> None:
        self.bot = bot
        self.db = db
        # Persistent view instances (registered via bot.add_view in setup).
        self.close_view = CloseTicketView()
        self.review_view = ApplicationReviewView(db=db)
        self.welcome_view = WelcomeView(db=db, review_view=self.review_view)
        self.report_view = ReportView(db=db, close_view=self.close_view)
        self.ticket_panel_view = TicketPanelView(db=db, close_view=self.close_view)

    @app_commands.command(
        name="welcome", description="Envoie le message de bienvenue dans le salon verify"
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    async def welcome(self, interaction: discord.Interaction) -> None:
        channel = discord.utils.get(interaction.guild.text_channels, name=WELCOME_CHANNEL)
        if not channel:
            await interaction.response.send_message("Salon verify introuvable.", ephemeral=True)
            return
        embed = discord.Embed(
            title="Bienvenu sur The Hub Matchmaking",
            description="Bienvenue sur un serveur de **10mans français** avec 3 queues :\n\n• **Pro Queue** — TOP VRC\n• **Open Queue** — Immortal peak\n• **GC Queue** — Ascendant peak\n\nPour pouvoir accéder au serveur, merci de cliquer sur le bouton **Postuler** juste en dessous.\n\n**Amusez-vous ! 🍀**",
            color=0x5865F2,
            timestamp=datetime.now(UTC),
        )
        embed.set_footer(text=interaction.guild.name)
        await channel.send(embed=embed, view=self.welcome_view)
        await interaction.response.send_message(
            f"Message envoye dans {channel.mention} !", ephemeral=True
        )

    @welcome.error
    async def _welcome_error(self, interaction: discord.Interaction, error):
        if isinstance(error, app_commands.MissingPermissions):
            await interaction.response.send_message(
                "Seuls les administrateurs peuvent utiliser cette commande.", ephemeral=True
            )

    @app_commands.command(
        name="report",
        description="Poste le panel d'ouverture de ticket (Reports / Ranks) dans ce salon",
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    async def report(self, interaction: discord.Interaction) -> None:
        channel = interaction.channel
        if channel is None or not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message(
                "❌ Cette commande doit etre utilisee dans un salon textuel.",
                ephemeral=True,
            )
            return
        embed = discord.Embed(
            title="🎫 Ouvrir un ticket",
            description=(
                "Choisis le type de ticket que tu souhaites ouvrir :\n\n"
                "🚨 **Reports** — Signaler un joueur (triche, toxicite, throw, "
                "insultes, AFK...). Ton report est **anonyme** : ton identite "
                "n'est pas revelee au staff.\n\n"
                "🎖️ **Ranks** — Postuler pour un rank. On te demandera le rank "
                "vise, le lien de ton tracker et ton experience en tournois/LANs/VLR."
            ),
            color=0x5865F2,
            timestamp=datetime.now(UTC),
        )
        embed.set_footer(text=interaction.guild.name if interaction.guild else "Tickets")
        await channel.send(embed=embed, view=self.ticket_panel_view)
        await interaction.response.send_message(
            f"Message envoye dans {channel.mention} !",
            ephemeral=True,
        )

    @report.error
    async def _report_error(self, interaction: discord.Interaction, error):
        if isinstance(error, app_commands.MissingPermissions):
            await interaction.response.send_message(
                "🚫 Reservé aux administrateurs.",
                ephemeral=True,
            )


async def setup(bot: commands.Bot, db) -> None:
    cog = ApplicationsCog(bot, db)
    await bot.add_cog(cog)
    # Enregistre les vues persistantes (apres restart, leurs custom_id
    # doivent etre routables par le bot meme sans instance de message).
    bot.add_view(cog.review_view)
    bot.add_view(cog.welcome_view)
    bot.add_view(cog.close_view)
    bot.add_view(cog.ticket_panel_view)
    # Conserve pour router les anciens panels "Report" deja postes (custom_id
    # report_open_btn) apres restart ; les nouveaux panels utilisent
    # ticket_panel_view.
    bot.add_view(cog.report_view)
