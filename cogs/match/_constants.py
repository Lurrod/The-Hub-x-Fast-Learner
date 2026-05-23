"""Constantes du package cogs.match.

Extraites pour eviter les imports cycliques entre _embeds, _vote et _cog,
et pour servir de point de configuration unique du flow match.
"""

from __future__ import annotations

from typing import Final


# Ecart d'ELO max entre le joueur sortant et le remplacant. Au-dela, on
# refuse le /match-replace : les equipes du match en cours seraient trop
# desequilibrees pour que le resultat reflete une vraie perf des joueurs.
MAX_REPLACE_ELO_DIFF: Final[int] = 500
MATCH_HOST_ROLE_NAME: Final[str] = "Match Host"

VOTE_A_BTN_ID: Final[str] = "vote_v2:a"
VOTE_B_BTN_ID: Final[str] = "vote_v2:b"
MAJORITY_THRESHOLD: Final[int] = 7
VOTE_TIMEOUT_MINUTES: Final[int] = 90
HENRIK_VERIFY_DELAY_MINUTES: Final[int] = 5  # premier essai Henrik a 5 min
HENRIK_VERIFY_TIMEOUT_MINUTES: Final[int] = 30  # abandon Henrik et ELO plat a 30 min

# Filet de securite : un match en "contested" non resolu par admin bloque
# les 10 joueurs dans le gate find_active_match_for_player. /win et /lose
# distribuent l'ELO mais ne touchent pas au doc match -> on auto-expire
# pour eviter qu'un admin distrait gele 10 joueurs indefiniment.
CONTESTED_EXPIRY_HOURS: Final[int] = 24

# Circuit breaker Henrik : si N appels consecutifs echouent, on suspend
# les tentatives pendant T minutes pour eviter de saturer les threads
# (chaque appel = ~12s avec retries) et de polluer les logs.
HENRIK_CIRCUIT_FAIL_THRESHOLD: Final[int] = 3
HENRIK_CIRCUIT_OPEN_MINUTES: Final[int] = 5

# Roles cibles pour le ping admin (premier trouve gagne)
ADMIN_ROLE_NAMES: Final[tuple[str, ...]] = ("Admin", "Match Staff", "Administrateur")

# Roles staff "viewers" : voient et participent aux categories de match
# (memes overwrites que les 10 joueurs), mais sans `manage_channels`.
# Distinct de ADMIN_ROLE_NAMES qui donne des pouvoirs admin (draft cancel,
# ping, gestion de salon).
MATCH_VIEWER_ROLE_NAMES: Final[tuple[str, ...]] = (
    "Administrators",
    "Moderators",
    "Coach/Analyst/Manager",
    "Head Administrators",
    "THE HUB",
    "Moderator En Chef",
)

# Roles "spectateurs" : voient la categorie + lisent l'historique, mais
# ne peuvent ni rejoindre les vocaux, ni envoyer de messages.
MATCH_SPECTATOR_ROLE_NAMES: Final[tuple[str, ...]] = ("Members",)
