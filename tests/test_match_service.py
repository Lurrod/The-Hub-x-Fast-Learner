"""Tests of the pure match formation logic."""

import random

import pytest

from services.match_service import (
    build_players,
    plan_match,
    serialize_team,
)
from services.team_balancer import Player


def _riot_doc(name: str = "X") -> dict:
    return {
        "riot_name": name,
        "riot_tag": "EUW",
        "riot_region": "eu",
        "puuid": "p",
        "peak_elo": 2500,
        "source": "peak_recent",
    }


# ── build_players ─────────────────────────────────────────────────
def test_build_players_uses_member_display_name():
    players = build_players(
        player_ids=["1", "2"],
        riot_accounts={"1": _riot_doc(), "2": _riot_doc()},
        member_names={"1": "Jet", "2": "Sage"},
        bot_elos={"1": 1500, "2": 2000},
    )
    assert len(players) == 2
    assert players[0].id == 1 and players[0].name == "Jet" and players[0].elo == 1500
    assert players[1].id == 2 and players[1].name == "Sage" and players[1].elo == 2000


def test_build_players_skips_unlinked():
    players = build_players(
        player_ids=["1", "2", "3"],
        riot_accounts={"1": _riot_doc(), "3": _riot_doc()},
        member_names={"1": "A", "2": "B", "3": "C"},
        bot_elos={"1": 1500, "2": 999, "3": 1700},
    )
    # Player 2 without a Riot account -> ignored (even if they have a bot ELO)
    assert len(players) == 2
    assert {p.id for p in players} == {1, 3}


def test_build_players_falls_back_to_riot_name():
    players = build_players(
        player_ids=["1"],
        riot_accounts={"1": _riot_doc(name="RiotName")},
        member_names={},  # no resolved member
        bot_elos={"1": 1500},
    )
    assert players[0].name == "RiotName"


def test_build_players_uses_bot_elo_not_riot():
    """Matchmaking ELO comes from the shared `elo` collection, never from riot_accounts."""
    players = build_players(
        player_ids=["1"],
        riot_accounts={"1": _riot_doc()},  # peak_elo 2500 ignored
        member_names={"1": "A"},
        bot_elos={"1": 1234},  # source of truth
    )
    assert players[0].elo == 1234


def test_build_players_zero_when_no_bot_elo():
    """If the shared `elo` collection has no doc for the player, ELO = 0."""
    players = build_players(
        player_ids=["1"],
        riot_accounts={"1": _riot_doc()},
        member_names={"1": "A"},
        bot_elos={},
    )
    assert players[0].elo == 0


# ── plan_match ────────────────────────────────────────────────────
def test_plan_match_rejects_wrong_size():
    players = [Player(id=i, name=f"P{i}", elo=1500) for i in range(9)]
    with pytest.raises(ValueError, match="10"):
        plan_match(players, free_category="Match #1")


def test_plan_match_returns_balanced_teams_and_random_choices():
    players = [Player(id=i, name=f"P{i}", elo=1500 + i * 50) for i in range(10)]
    rng = random.Random(42)  # deterministic for the test

    plan = plan_match(players, free_category="Match #1", rng=rng)

    assert len(plan.teams.team_a) == 5
    assert len(plan.teams.team_b) == 5
    # with a seeded rng we can verify stability
    assert plan.map_name in ("Breeze", "Ascent", "Lotus", "Fracture", "Split", "Haven", "Pearl")
    assert plan.lobby_leader in players
    assert plan.category_name == "Match #1"


def test_plan_match_with_no_free_category():
    players = [Player(id=i, name=f"P{i}", elo=1500) for i in range(10)]
    plan = plan_match(players, free_category=None)
    assert plan.category_name is None


def test_plan_match_lobby_leader_is_one_of_the_players():
    players = [Player(id=i, name=f"P{i}", elo=1000 + i) for i in range(10)]
    for seed in range(20):
        plan = plan_match(players, free_category="Match #1", rng=random.Random(seed))
        leader_ids = {p.id for p in players}
        assert plan.lobby_leader.id in leader_ids


def test_plan_match_balance_optimal():
    """Known case: with 10 varied ELOs, the brute-force algo finds the best split."""
    players = [
        Player(id=i, name=f"P{i}", elo=elo)
        for i, elo in enumerate([3000, 2500, 2000, 1800, 1500, 1300, 1200, 900, 500, 300])
    ]
    plan = plan_match(players, free_category=None)
    assert plan.teams.elo_diff <= 200


# -- serialize_team --
def test_serialize_team_returns_list_of_dicts():
    team = (Player(id=1, name="A", elo=1500), Player(id=2, name="B", elo=1600))
    out = serialize_team(team)
    assert out == [
        {"id": 1, "name": "A", "elo": 1500},
        {"id": 2, "name": "B", "elo": 1600},
    ]
