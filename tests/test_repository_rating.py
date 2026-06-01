"""Tests for Rating 2.0 stats persistence in services/repository.py."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest


def test_insert_match_player_stats_persists_docs(mongo_db):
    from services.repository import insert_match_player_stats

    docs = [
        {
            "_id": "match-1:user-A",
            "match_id": "match-1", "user_id": "A", "queue_type": "pro",
            "map": "Ascent", "agent": "Jett",
            "rounds_played": 24, "win": True,
            "kills": 22, "deaths": 14, "assists": 5,
            "damage_made": 4123, "damage_received": 3580,
            "headshots": 18, "bodyshots": 50, "legshots": 4,
            "multikills_2k": 3, "multikills_3k": 1,
            "multikills_4k": 0, "multikills_5k": 0,
            "first_kills": 4, "first_deaths": 2,
            "kast_rounds": 19, "acs": 287,
            "rating_2_0": 1.34,
            "created_at": datetime.now(UTC),
        }
    ]
    n = insert_match_player_stats(mongo_db, docs)
    assert n == 1
    saved = mongo_db["match_player_stats"].find_one({"_id": "match-1:user-A"})
    assert saved["rating_2_0"] == 1.34


def test_insert_match_player_stats_is_idempotent_on_duplicate(mongo_db):
    """Re-inserting the same _id must return 0 inserted and not crash."""
    from services.repository import insert_match_player_stats

    doc = {
        "_id": "match-2:user-B", "match_id": "match-2",
        "user_id": "B", "queue_type": "open",
        "rounds_played": 24, "kills": 1, "deaths": 0, "assists": 0,
        "damage_made": 100, "kast_rounds": 1, "rating_2_0": 0.5,
        "created_at": datetime.now(UTC),
    }
    assert insert_match_player_stats(mongo_db, [doc]) == 1
    assert insert_match_player_stats(mongo_db, [doc]) == 0


def test_insert_match_player_stats_partial_dup_inserts_new(mongo_db):
    """When a batch has some duplicates and some new docs, the new ones must still land."""
    from services.repository import insert_match_player_stats

    base = {
        "match_id": "m", "queue_type": "gc", "rounds_played": 24,
        "kills": 0, "deaths": 0, "assists": 0, "damage_made": 0,
        "kast_rounds": 0, "rating_2_0": 0.0,
        "created_at": datetime.now(UTC),
    }
    assert insert_match_player_stats(mongo_db, [
        {**base, "_id": "m:U1", "user_id": "U1"},
    ]) == 1

    n = insert_match_player_stats(mongo_db, [
        {**base, "_id": "m:U1", "user_id": "U1"},
        {**base, "_id": "m:U2", "user_id": "U2"},
    ])
    assert n == 1
    assert mongo_db["match_player_stats"].count_documents({}) == 2


def test_insert_match_player_stats_empty_list_is_noop(mongo_db):
    from services.repository import insert_match_player_stats
    assert insert_match_player_stats(mongo_db, []) == 0
