"""Pure ELO calculation logic. No Discord nor MongoDB dependency."""

from __future__ import annotations

from typing import Final

# -- Constants -----------------------------------------------------
ELO_START: Final[int] = 2000
MAPS: Final[tuple[str, ...]] = (
    "Breeze",
    "Ascent",
    "Lotus",
    "Fracture",
    "Split",
    "Haven",
    "Pearl",
)


# -- V2: ELO change proportional to match average ------------------
# Server reserved to Immortal+ players: baseline is Immortal 1.
IMMORTAL_FLOOR_ELO: Final[int] = 2400  # Immortal 1 (HenrikDev tier 24 * 100)
ELO_REFERENCE: Final[int] = IMMORTAL_FLOOR_ELO
# Strict zero-sum: gain == loss. ELO injected per match = 0.
ELO_BASE_CHANGE: Final[int] = 20  # flat gain/loss per match across all queues
# Backward-compatible aliases (used by tests/legacy code)
ELO_BASE_GAIN: Final[int] = ELO_BASE_CHANGE
ELO_BASE_LOSS: Final[int] = ELO_BASE_CHANGE

# -- Pro-queue performance weighting -------------------------------
# Pondère le ±20 par le Rating 2.0 du joueur (pro queue uniquement).
# Modèle additif : delta = base*signe + clamp(C*(rating - baseline), ±P_MAX).
# Comme P_MAX < ELO_BASE_CHANGE, le signe est garanti (un gagnant gagne
# toujours, un perdant perd toujours).
ELO_WEIGHT_C: Final[int] = 15            # sensibilité (pente)
ELO_WEIGHT_P_MAX: Final[int] = 6         # plafond du terme perf
ELO_RATING_BASELINE: Final[float] = 1.0  # ancre absolue (ajustable anti-inflation)
ELO_MIN_ROUNDS_FOR_WEIGHT: Final[int] = 6  # sous ce seuil: fallback plat (forfaits)


def compute_team_avg_elo(players: list[dict]) -> int:
    """Rounded average of the effective_elo of a player list."""
    if not players:
        return 0
    return round(sum(p.get("elo", 0) for p in players) / len(players))


def compute_match_elo_change(avg_match_elo: int) -> tuple[int, int]:
    """
    Returns (gain, loss) with strict zero-sum: gain == loss = ELO_BASE_CHANGE.

    Flat ±20 across all queues regardless of average ELO. ACS-based
    per-player scaling has been removed.
    """
    if avg_match_elo < 0:
        raise ValueError(f"avg_match_elo must be >= 0, received {avg_match_elo}")
    return ELO_BASE_CHANGE, ELO_BASE_CHANGE


def compute_weighted_delta(
    rating: float,
    *,
    win: bool,
    baseline: float = ELO_RATING_BASELINE,
    base: int = ELO_BASE_CHANGE,
    c: float = ELO_WEIGHT_C,
    p_max: int = ELO_WEIGHT_P_MAX,
) -> int:
    """ELO delta pondéré par le Rating 2.0 (pro queue).

        delta = base*signe + clamp(c*(rating - baseline), ±p_max)

    Le terme performance s'ajoute avec le même signe quel que soit le
    résultat : un rating élevé fait gagner plus / perdre moins. Comme
    `p_max < base`, le signe du résultat est toujours préservé.

    Exemples (base=20, c=15, p_max=6, baseline=1.0) :
        rating 1.40 -> +26 (win) / -14 (loss)   # perf clampée à +6
        rating 1.00 -> +20 / -20                 # ±20 plat
        rating 0.50 -> +14 / -26                 # perf clampée à -6
    """
    sign = 1 if win else -1
    perf = c * (rating - baseline)
    perf = max(-p_max, min(p_max, perf))
    return round(base * sign + perf)
