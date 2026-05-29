from __future__ import annotations

import argparse
import os
import sys
from datetime import UTC, datetime, timedelta

from pymongo import MongoClient

DB_NAME = "elobot"
MATCHES_COLL = "matches"

# Must stay aligned with services.repository._ACTIVE_MATCH_STATUSES_FOR_QUEUE_GATE
ACTIVE_MATCH_STATUSES_FOR_QUEUE_GATE: tuple[str, ...] = (
    "pending",
    "validated_a",
    "validated_b",
    "contested",
)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="READ-ONLY diagnostic: matches blocking players in the queue gate.",
    )
    p.add_argument(
        "--guild",
        type=int,
        default=None,
        help="Filter on a single origin_guild_id (default: all guilds)",
    )
    p.add_argument(
        "--status",
        choices=ACTIVE_MATCH_STATUSES_FOR_QUEUE_GATE,
        default=None,
        help="Filter on a specific status",
    )
    p.add_argument(
        "--older-than-hours",
        type=float,
        default=0.0,
        help="Keep only matches older than N hours",
    )
    return p.parse_args()


def main() -> int:
    args = parse_args()
    mongo_url = os.environ.get("MONGO_URL")
    if not mongo_url:
        print("[ERROR] MONGO_URL not set", file=sys.stderr)
        return 2

    client: MongoClient = MongoClient(mongo_url, serverSelectionTimeoutMS=5000)
    matches = client[DB_NAME][MATCHES_COLL]

    status_filter = (
        args.status if args.status else {"$in": list(ACTIVE_MATCH_STATUSES_FOR_QUEUE_GATE)}
    )
    query: dict = {
        "status": status_filter,
        "elo_applied": {"$ne": True},
    }
    if args.guild is not None:
        query["origin_guild_id"] = args.guild
    if args.older_than_hours > 0:
        cutoff = datetime.now(UTC) - timedelta(hours=args.older_than_hours)
        query["created_at"] = {"$lt": cutoff}

    docs = list(matches.find(query).sort("created_at", 1))
    print(f"[INFO] {len(docs)} active match(es) without applied ELO\n")

    blocked_ids: set[int] = set()
    by_status: dict[str, int] = {}

    for d in docs:
        status = d.get("status", "?")
        by_status[status] = by_status.get(status, 0) + 1
        team_a = [p.get("id") for p in d.get("team_a", []) if p.get("id") is not None]
        team_b = [p.get("id") for p in d.get("team_b", []) if p.get("id") is not None]
        blocked_ids.update(team_a)
        blocked_ids.update(team_b)
        print(
            f"  match #{d.get('match_number', '?'):>5} "
            f"status={status:<13} "
            f"created={d.get('created_at')} "
            f"guild={d.get('origin_guild_id')} "
            f"_id={d.get('_id')}"
        )
        print(f"    team_a={team_a}")
        print(f"    team_b={team_b}")

    print("\n[SUMMARY]")
    for s, n in sorted(by_status.items()):
        print(f"  {s:<13} : {n}")
    print(f"  blocked players (unique)  : {len(blocked_ids)}")
    if blocked_ids:
        print(f"  ids                       : {sorted(blocked_ids)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
