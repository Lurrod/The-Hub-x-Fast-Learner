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
