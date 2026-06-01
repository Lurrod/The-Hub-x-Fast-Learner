"""HLTV 2.0-equivalent Rating formula adapted for Valorant.

The formula is identical to CSGO/CS2 Rating 2.0 (the constants are
calibration parameters from the original article); the only
adaptation is that the per-round inputs are sourced from Henrik's
Valorant match data instead of HLTV's CS data.

Reference: https://www.hltv.org/news/20695/introducing-rating-20
"""

from __future__ import annotations

from dataclasses import dataclass

_INTERCEPT = 0.1587
_KAST_C    = 0.0073
_KPR_C     = 0.3591
_DPR_C     = -0.5329
_IMPACT_C  = 0.2372
_ADR_C     = 0.0032

_IMPACT_KPR_C = 2.13
_IMPACT_APR_C = 0.42
_IMPACT_OFFSET = -0.41


@dataclass(frozen=True)
class RatingInputs:
    """Raw counters needed to evaluate a Rating 2.0 score.

    All fields are *totals* over the rounds the player was in. The
    formula divides each by `rounds_played` to get per-round rates.
    """

    rounds_played: int
    kills: int
    deaths: int
    assists: int
    damage_made: int
    kast_rounds: int


def compute_impact(*, kpr: float, apr: float) -> float:
    """Impact rating: kill-pressure component of Rating 2.0.

    Captures multi-kill / opening-duel weight indirectly via KPR and
    assist participation via APR.
    """
    return _IMPACT_KPR_C * kpr + _IMPACT_APR_C * apr + _IMPACT_OFFSET


def compute_rating_2_0(inputs: RatingInputs) -> float:
    """Rating 2.0 score. Returns 0.0 when `rounds_played <= 0`."""
    rounds = inputs.rounds_played
    if rounds <= 0:
        return 0.0
    kast = (inputs.kast_rounds / rounds) * 100.0
    kpr = inputs.kills / rounds
    dpr = inputs.deaths / rounds
    apr = inputs.assists / rounds
    adr = inputs.damage_made / rounds
    impact = compute_impact(kpr=kpr, apr=apr)
    return (
        _KAST_C * kast
        + _KPR_C * kpr
        + _DPR_C * dpr
        + _IMPACT_C * impact
        + _ADR_C * adr
        + _INTERCEPT
    )
