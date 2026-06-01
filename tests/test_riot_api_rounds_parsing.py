"""Tests for round-level + aggregate parsing in services/riot_api.py."""

from __future__ import annotations


def _henrik_min_match_with_damage():
    """Minimal Henrik /v3/matches response with damage + shots."""
    return {
        "data": {
            "metadata": {
                "matchid": "abc",
                "mode": "Custom Game",
                "map": "Ascent",
                "game_start": 1700000000,
                "rounds_played": 24,
            },
            "players": {
                "all_players": [
                    {
                        "puuid": "puuid-A",
                        "name": "Alice", "tag": "EUW",
                        "team": "Red",
                        "character": "Jett",
                        "stats": {
                            "score": 5400,
                            "kills": 22, "deaths": 14, "assists": 5,
                            "headshots": 18, "bodyshots": 50, "legshots": 4,
                        },
                        "damage_made": 4123,
                        "damage_received": 3580,
                    },
                ],
            },
            "teams": {
                "red":  {"rounds_won": 13},
                "blue": {"rounds_won": 11},
            },
            "rounds": [],
        }
    }


def test_parser_extracts_damage_and_shots():
    from services.riot_api import _parse_match

    summary = _parse_match(_henrik_min_match_with_damage()["data"])
    assert summary is not None
    p = summary.players[0]
    assert p.damage_made == 4123
    assert p.damage_received == 3580
    assert p.headshots == 18
    assert p.bodyshots == 50
    assert p.legshots == 4
