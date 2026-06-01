"""
Verification of a bot match via the HenrikDev API and computation of ACS
multipliers for per-player ELO adjustment.

Flow:
  1. Fetch the recent custom match history of the lobby leader.
  2. Find the match containing the 10 expected puuids, started after `after`.
  3. Compute each player's ACS and their multiplier (clamped to [0.7, 1.3])
     relative to the team average.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from services.rating import RatingInputs, compute_rating_2_0
from services.riot_api import (
    HenrikDevClient,
    MatchPlayerStats,
    MatchSummary,
    RiotApiError,
)

CUSTOM_MODE_NAME: Final[str] = "Custom Game"
DEFAULT_MULT_MIN: Final[float] = 0.7
DEFAULT_MULT_MAX: Final[float] = 1.3


@dataclass(frozen=True)
class PlayerPerformance:
    user_id: str
    puuid: str
    acs: float
    multiplier: float
    win: bool


@dataclass(frozen=True)
class VerifiedMatch:
    matchid: str
    started_at: datetime
    winning_team: str  # "Red" or "Blue" (empty if draw)
    performances: tuple[PlayerPerformance, ...]


@dataclass(frozen=True)
class PlayerStatsExtended:
    """Full Rating 2.0 footprint for a single player in one match."""

    user_id: str
    puuid: str
    queue_type: str
    map_name: str
    agent: str
    team: str
    win: bool
    rounds_played: int
    acs: float
    kills: int
    deaths: int
    assists: int
    damage_made: int
    damage_received: int
    headshots: int
    bodyshots: int
    legshots: int
    multikills_2k: int
    multikills_3k: int
    multikills_4k: int
    multikills_5k: int
    first_kills: int
    first_deaths: int
    kast_rounds: int
    rating_2_0: float


def find_henrik_custom_match(
    client: HenrikDevClient,
    *,
    region: str,
    leader_name: str,
    leader_tag: str,
    expected_puuids: set[str],
    after: datetime,
    history_size: int = 10,
) -> MatchSummary | None:
    """Look up a custom match of `leader` that contains `expected_puuids`
    and started after `after`. Returns the `MatchSummary` or None.
    """
    try:
        history = client.get_match_history(
            region,
            leader_name,
            leader_tag,
            size=history_size,
            mode="custom",
        )
    except RiotApiError:
        return None

    for match in history:
        if match.mode != CUSTOM_MODE_NAME:
            continue
        if match.started_at < after:
            continue
        match_puuids = {p.puuid for p in match.players}
        if expected_puuids.issubset(match_puuids):
            return match
    return None


def compute_acs_multipliers(
    match: MatchSummary,
    *,
    team_a_uid_by_puuid: Mapping[str, str],
    team_b_uid_by_puuid: Mapping[str, str],
    mult_min: float = DEFAULT_MULT_MIN,
    mult_max: float = DEFAULT_MULT_MAX,
) -> VerifiedMatch:
    """Compute the ACS and clamped multiplier for each player, based on
    the team average (Henrik side: Red / Blue, mapped to the bot's
    teams a/b via the provided puuids)."""
    rounds = max(match.rounds_played, 1)
    if match.rounds_red > match.rounds_blue:
        winning = "Red"
    elif match.rounds_blue > match.rounds_red:
        winning = "Blue"
    else:
        winning = ""  # draw, edge case

    by_puuid: dict[str, MatchPlayerStats] = {p.puuid: p for p in match.players}

    perfs: list[PlayerPerformance] = []
    for team_uids in (team_a_uid_by_puuid, team_b_uid_by_puuid):
        labels = {by_puuid[pu].team for pu in team_uids if pu in by_puuid}
        if len(labels) != 1:
            continue  # inconsistent team on the Henrik side
        side = next(iter(labels))

        team_acs = [by_puuid[pu].score / rounds for pu in team_uids if pu in by_puuid]
        if not team_acs:
            continue
        avg_acs = sum(team_acs) / len(team_acs)
        if avg_acs <= 0:
            avg_acs = 1.0

        for pu, uid in team_uids.items():
            stats = by_puuid.get(pu)
            if stats is None:
                continue
            acs = stats.score / rounds
            raw = acs / avg_acs
            mult = max(mult_min, min(mult_max, raw))
            perfs.append(
                PlayerPerformance(
                    user_id=uid,
                    puuid=pu,
                    acs=acs,
                    multiplier=mult,
                    win=(side == winning),
                )
            )

    return VerifiedMatch(
        matchid=match.matchid,
        started_at=match.started_at,
        winning_team=winning,
        performances=tuple(perfs),
    )


def build_extended_stats(
    summary: MatchSummary,
    *,
    puuid_to_user_id: dict[str, str],
    queue_type: str,
) -> tuple[PlayerStatsExtended, ...]:
    """Translate a verified MatchSummary into one PlayerStatsExtended
    per linked Discord user. Players whose puuid is not in
    `puuid_to_user_id` are skipped.
    """
    if summary.rounds_red > summary.rounds_blue:
        winning_team = "Red"
    elif summary.rounds_blue > summary.rounds_red:
        winning_team = "Blue"
    else:
        winning_team = ""

    rounds = max(int(summary.rounds_played), 1)
    out: list[PlayerStatsExtended] = []
    for p in summary.players:
        uid = puuid_to_user_id.get(p.puuid)
        if not uid:
            continue
        rating = compute_rating_2_0(
            RatingInputs(
                rounds_played=rounds,
                kills=p.kills,
                deaths=p.deaths,
                assists=p.assists,
                damage_made=p.damage_made,
                kast_rounds=p.kast_rounds,
            )
        )
        out.append(
            PlayerStatsExtended(
                user_id=str(uid),
                puuid=p.puuid,
                queue_type=queue_type,
                map_name=summary.map_name,
                agent=p.agent,
                team=p.team,
                win=(p.team == winning_team) if winning_team else False,
                rounds_played=rounds,
                acs=p.score / rounds if rounds else 0.0,
                kills=p.kills,
                deaths=p.deaths,
                assists=p.assists,
                damage_made=p.damage_made,
                damage_received=p.damage_received,
                headshots=p.headshots,
                bodyshots=p.bodyshots,
                legshots=p.legshots,
                multikills_2k=p.multikills_2k,
                multikills_3k=p.multikills_3k,
                multikills_4k=p.multikills_4k,
                multikills_5k=p.multikills_5k,
                first_kills=p.first_kills,
                first_deaths=p.first_deaths,
                kast_rounds=p.kast_rounds,
                rating_2_0=rating,
            )
        )
    return tuple(out)
