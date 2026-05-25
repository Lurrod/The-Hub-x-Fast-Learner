"""Tests : classement des joueurs par inactivite (commande /inactivity).

Logique pure, sans dependance Discord ni MongoDB.
"""

from datetime import UTC, datetime, timedelta

import mongomock

from services.inactivity import (
    DEFAULT_INACTIVITY_LIMIT,
    format_inactivity,
    rank_by_inactivity,
)
from services.repository import get_elo_col, player_doc_id

NOW = datetime(2026, 5, 25, 12, 0, 0, tzinfo=UTC)


def _doc(name: str, last_played: datetime | None) -> dict:
    return {"_id": f"{name}:pro", "name": name, "last_played": last_played}


# ── rank_by_inactivity ───────────────────────────────────────


def test_default_limit_is_25():
    assert DEFAULT_INACTIVITY_LIMIT == 25


def test_never_played_ranked_first():
    played = _doc("Bob", NOW - timedelta(days=1))
    never = _doc("Alice", None)
    ranked = rank_by_inactivity([played, never])
    assert [d["name"] for d in ranked] == ["Alice", "Bob"]


def test_oldest_last_played_first():
    recent = _doc("Recent", NOW - timedelta(days=1))
    old = _doc("Old", NOW - timedelta(days=30))
    mid = _doc("Mid", NOW - timedelta(days=7))
    ranked = rank_by_inactivity([recent, old, mid])
    assert [d["name"] for d in ranked] == ["Old", "Mid", "Recent"]


def test_multiple_never_played_then_oldest():
    docs = [
        _doc("PlayedRecent", NOW - timedelta(hours=2)),
        _doc("NeverB", None),
        _doc("PlayedOld", NOW - timedelta(days=20)),
        _doc("NeverA", None),
    ]
    ranked = rank_by_inactivity(docs)
    assert [d["name"] for d in ranked] == ["NeverA", "NeverB", "PlayedOld", "PlayedRecent"]


def test_limit_applied():
    docs = [_doc(f"P{i:02d}", NOW - timedelta(days=i)) for i in range(50)]
    ranked = rank_by_inactivity(docs, limit=25)
    assert len(ranked) == 25


def test_name_tiebreaker_deterministic():
    same = NOW - timedelta(days=3)
    docs = [_doc("Charlie", same), _doc("alice", same), _doc("Bob", same)]
    ranked = rank_by_inactivity(docs)
    assert [d["name"] for d in ranked] == ["alice", "Bob", "Charlie"]


def test_empty_input():
    assert rank_by_inactivity([]) == []


def test_naive_last_played_treated_as_utc():
    # Naive datetime 1 day old vs aware 1h old -> naive plus inactif -> en tete.
    naive = _doc("Naive", datetime(2026, 5, 24, 12, 0, 0))
    aware = _doc("Aware", NOW - timedelta(hours=1))
    ranked = rank_by_inactivity([aware, naive])
    assert ranked[0]["name"] == "Naive"


# ── format_inactivity ────────────────────────────────────────


def test_format_never_played():
    assert format_inactivity(None, NOW) == "jamais joué"


def test_format_days_hours_minutes():
    last = NOW - timedelta(days=12, hours=3, minutes=40)
    assert format_inactivity(last, NOW) == "12d 3h 40m"


def test_format_zero():
    assert format_inactivity(NOW, NOW) == "0d 0h 0m"


def test_format_negative_clamped():
    future = NOW + timedelta(hours=5)
    assert format_inactivity(future, NOW) == "0d 0h 0m"


def test_format_naive_last_played():
    last = datetime(2026, 5, 24, 12, 0, 0)  # naive, 1 jour avant NOW
    assert format_inactivity(last, NOW) == "1d 0h 0m"


# ── integration : docs reels via get_elo_col (mongomock) ─────


def _insert(col, uid, *, last_played, queue_type="pro"):
    doc = {
        "_id": player_doc_id(uid, queue_type),
        "user_id": str(uid),
        "name": f"P{uid}",
        "elo": 2500,
        "wins": 5,
        "losses": 0,
        "queue_type": queue_type,
    }
    if last_played is not None:
        doc["last_played"] = last_played
    col.insert_one(doc)


def test_ranking_over_real_elo_docs_and_queue_isolation():
    db = mongomock.MongoClient(tz_aware=True).db
    col = get_elo_col(db)
    _insert(col, 1, last_played=NOW - timedelta(days=2))  # recent
    _insert(col, 2, last_played=None)  # jamais joue -> en tete
    _insert(col, 3, last_played=NOW - timedelta(days=30))  # le plus ancien (joue)
    _insert(col, 99, last_played=None, queue_type="open")  # autre queue, exclu

    docs = list(col.find({"queue_type": "pro"}))
    ranked = rank_by_inactivity(docs)

    assert [d["user_id"] for d in ranked] == ["2", "3", "1"]
    # le mention se reconstruit aussi depuis le _id compound
    assert str(ranked[1]["_id"]).rsplit(":", 1)[0] == "3"
