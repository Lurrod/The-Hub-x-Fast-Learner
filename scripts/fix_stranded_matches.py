from __future__ import annotations

import argparse
import os
import sys
from datetime import UTC, datetime, timedelta

from pymongo import MongoClient

DB_NAME = "elobot"
MATCHES_COLL = "matches"

BLOCKING_STATUSES: tuple[str, ...] = (
    "pending",
    "validated_a",
    "validated_b",
    "contested",
)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="MANUAL fix: releases players blocked in the queue gate.",
    )
    p.add_argument(
        "--guild",
        type=int,
        default=None,
        help="Filter on origin_guild_id (recommended)",
    )
    p.add_argument(
        "--status",
        choices=BLOCKING_STATUSES,
        default="contested",
        help='Status to target (default: "contested")',
    )
    p.add_argument(
        "--older-than-hours",
        type=float,
        default=24.0,
        help="Only matches older than N hours (default: 24)",
    )
    p.add_argument(
        "--match-ids",
        type=str,
        default=None,
        help="Comma-separated list of match_number (overrides other time/status filters)",
    )
    p.add_argument(
        "--apply",
        action="store_true",
        help="Without this flag, dry-run (no writes)",
    )
    return p.parse_args()


def build_query(args: argparse.Namespace) -> dict:
    if args.match_ids:
        ids = [int(x.strip()) for x in args.match_ids.split(",") if x.strip()]
        return {"match_number": {"$in": ids}}
    q: dict = {
        "status": args.status,
        "elo_applied": {"$ne": True},
    }
    if args.guild is not None:
        q["origin_guild_id"] = args.guild
    if args.older_than_hours > 0:
        q["created_at"] = {"$lt": datetime.now(UTC) - timedelta(hours=args.older_than_hours)}
    return q


def main() -> int:
    args = parse_args()
    mongo_url = os.environ.get("MONGO_URL")
    if not mongo_url:
        print("[ERROR] MONGO_URL not set", file=sys.stderr)
        return 2

    client: MongoClient = MongoClient(mongo_url, serverSelectionTimeoutMS=5000)
    matches = client[DB_NAME][MATCHES_COLL]

    query = build_query(args)
    docs = list(matches.find(query, {"_id": 1, "match_number": 1, "status": 1, "created_at": 1}))
    print(f"[INFO] {len(docs)} match(es) targeted by the fix")
    for d in docs:
        print(
            f"  match #{d.get('match_number', '?'):>5} "
            f"status={d.get('status'):<13} "
            f"created={d.get('created_at')} "
            f"_id={d.get('_id')}"
        )

    if not docs:
        print("[OK] Nothing to do.")
        return 0

    if not args.apply:
        print("\n[DRY-RUN] No writes. Re-run with --apply to apply.")
        return 0

    confirm = input(f"\nConfirm switching {len(docs)} match(es) to 'cleaned_up'? [yes/N] ")
    if confirm.strip().lower() != "yes":
        print("[ABORT]")
        return 1

    res = matches.update_many(
        query,
        {
            "$set": {
                "status": "cleaned_up",
                "cleaned_up_at": datetime.now(UTC),
                "cleaned_up_by": "manual_fix_stranded_matches",
            }
        },
    )
    print(f"[OK] {res.modified_count} doc(s) modified.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
