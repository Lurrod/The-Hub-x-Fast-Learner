"""One-shot migration: promotes elo_<guild>/riot_accounts_<guild>/matches_<guild>
into shared elo/riot/matches collections.

Usage:
    MONGO_URL=mongodb://... MIGRATE_SOURCE_GUILD_ID=<guild_A_id> \
        python scripts/migrate_shared_collections.py

Idempotent (replace_one upsert). The old collections are renamed to
archive_<timestamp>_<source_name> (not deleted) to allow a rollback.

Notes:
- riot was historically named `riot_accounts_<guild>`, not `riot_<guild>`.
- elo and matches follow the `<name>_<guild>` pattern.
"""

from __future__ import annotations

import os
import sys
from datetime import UTC, datetime

from pymongo import MongoClient


def main() -> int:
    mongo_url = os.environ.get("MONGO_URL")
    if not mongo_url:
        print("ERROR: MONGO_URL not set", file=sys.stderr)
        return 1
    raw_guild = os.environ.get("MIGRATE_SOURCE_GUILD_ID")
    if not raw_guild:
        print("ERROR: MIGRATE_SOURCE_GUILD_ID not set", file=sys.stderr)
        return 1
    source_guild_id = int(raw_guild)

    client: MongoClient = MongoClient(mongo_url)
    db = client["elobot"]

    stamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")

    mappings = {
        "elo": f"elo_{source_guild_id}",
        "riot": f"riot_accounts_{source_guild_id}",
        "matches": f"matches_{source_guild_id}",
    }

    for dst_name, src_name in mappings.items():
        if src_name not in db.list_collection_names():
            print(f"  skip {src_name} (not present - already migrated?)")
            continue

        src = db[src_name]
        dst = db[dst_name]

        n = 0
        for doc in src.find():
            dst.replace_one({"_id": doc["_id"]}, doc, upsert=True)
            n += 1
        print(f"  copied {n} docs : {src_name} -> {dst_name}")

        archive_name = f"archive_{stamp}_{src_name}"
        db[src_name].rename(archive_name, dropTarget=True)
        print(f"  archived : {src_name} -> {archive_name}")

    print("Migration complete.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
