"""
Pure match formation logic (testable without Discord).

Responsibilities:
  - Build the Player list from queued IDs and linked Riot accounts
    (effective_elo).
  - Find a free 'Match #N' category.
  - Pick a random map and lobby leader.
  - Return a complete MatchPlan ready to be posted on Discord.

The cog cogs/match.py then handles the side effects (sending the
message, attaching the VoteView, persistence).
"""

from __future__ import annotations

import random
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING

from services import elo_calc
from services.team_balancer import BalancedTeams, Player, balance_teams

if TYPE_CHECKING:
    pass


@dataclass(frozen=True)
class MatchPlan:
    teams: BalancedTeams
    map_name: str
    lobby_leader: Player
    category_name: str | None  # None if no free category


def build_players(
    player_ids: Sequence[str],
    riot_accounts: dict[str, dict],
    member_names: dict[str, str],
    bot_elos: dict[str, int] | None = None,
) -> list[Player]:
    """
    Build the Player list by crossing queue + Riot + server ELO + display_names.

    Args:
        player_ids:    Discord IDs (str) in queue
        riot_accounts: dict[user_id_str -> Riot doc] (gate-keep only)
        member_names:  dict[user_id_str -> display_name]
        bot_elos:      dict[user_id_str -> server ELO (shared `elo` collection, `elo` field)].
                       Source of truth for matchmaking.

    Player without a linked Riot account -> ignored (queue will reject < 10).
    The ELO used for balancing is `bot_elos[uid]` (server ELO seeded at
    /link-riot and updated after each valid match).
    """
    bot_elos = bot_elos or {}
    out: list[Player] = []
    for uid in player_ids:
        riot = riot_accounts.get(uid)
        if riot is None:
            continue
        name = member_names.get(uid, riot.get("riot_name", "Unknown"))
        out.append(
            Player(
                id=int(uid),
                name=name,
                elo=int(bot_elos.get(uid, 0)),
            )
        )
    return out


def plan_match(
    players: Sequence[Player],
    *,
    free_category: str | None,
    rng: random.Random | None = None,
) -> MatchPlan:
    """
    Pure step: balance + map + lobby leader.

    Args:
        players:       exactly 10 players with effective_elo
        free_category: name of the free 'Match #N' category (None if none)
        rng:           random source (injectable for tests)
    """
    if len(players) != 10:
        raise ValueError(f"10 players required, received {len(players)}")

    rng = rng or random.Random()
    teams = balance_teams(players)
    map_name = rng.choice(elo_calc.MAPS)
    lobby_leader = rng.choice(players)
    return MatchPlan(
        teams=teams,
        map_name=map_name,
        lobby_leader=lobby_leader,
        category_name=free_category,
    )


def serialize_team(team: tuple[Player, ...]) -> list[dict]:
    """For MongoDB storage."""
    return [asdict(p) for p in team]
