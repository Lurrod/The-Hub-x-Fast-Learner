"""Classement des joueurs par inactivite (temps ecoule depuis la derniere partie).

Logique pure, sans dependance Discord ni MongoDB, pour etre testable
isolement. Le cog `elo_admin` l'utilise pour la commande /inactivity.

Le champ `last_played` est horodate sur chaque match valide par
`services.elo_updater`. Un joueur sans `last_played` n'a jamais joue de
match valide depuis l'introduction du suivi : il est considere comme le
plus inactif.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

DEFAULT_INACTIVITY_LIMIT = 25


def _as_utc(value: datetime) -> datetime:
    """Normalise un datetime naive en UTC (coherent avec l'eligibilite)."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def rank_by_inactivity(
    docs: Sequence[Mapping[str, Any]], *, limit: int = DEFAULT_INACTIVITY_LIMIT
) -> list[Mapping[str, Any]]:
    """Trie les docs joueurs du plus inactif au moins inactif.

    - Les joueurs sans `last_played` sont les plus inactifs -> places en tete.
    - Les autres sont tries par `last_played` croissant (le plus ancien d'abord).
    - `name` sert de depart pour un ordre deterministe.

    Renvoie au plus `limit` docs (liste vide si `limit <= 0`).
    """

    def sort_key(doc: Mapping[str, Any]) -> tuple[int, float, str]:
        last = doc.get("last_played")
        name = str(doc.get("name", "")).lower()
        if last is None:
            return (0, 0.0, name)
        return (1, _as_utc(last).timestamp(), name)

    return sorted(docs, key=sort_key)[: max(limit, 0)]


def format_inactivity(last_played: datetime | None, now: datetime) -> str:
    """Texte d'inactivite : « jamais joué » ou « Xd Xh Xm ».

    La duree negative (horloge en avance) est ramenee a zero.
    """
    if last_played is None:
        return "jamais joué"
    delta = _as_utc(now) - _as_utc(last_played)
    total_minutes = max(int(delta.total_seconds() // 60), 0)
    days, rem = divmod(total_minutes, 1440)
    hours, minutes = divmod(rem, 60)
    return f"{days}d {hours}h {minutes}m"
