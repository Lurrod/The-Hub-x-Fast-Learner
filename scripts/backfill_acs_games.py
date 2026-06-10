"""One-shot backfill: recomputes acs_sum + acs_games on
player_rating_aggregates from match_player_stats.

Contexte : acs_sum n'est accumule que depuis le 2026-06-08, mais le site
le divisait par `games` (tous les matchs de la saison), ce qui diluait
l'ACS saison. Le bon denominateur est acs_games = nombre de matchs dont
l'ACS est connu. Ce script recalcule les deux champs depuis la source de
verite (match_player_stats.acs).

Usage:
    MONGO_URL=mongodb://... python scripts/backfill_acs_games.py

Idempotent (recalcul complet a chaque execution). Les aggregats sans
aucun match avec ACS connu sont remis a acs_sum/acs_games absents
($unset) pour que le site affiche "-" plutot qu'une valeur fausse.

ATTENTION : aucune des deux collections n'est remise a zero en fin de
saison aujourd'hui ; si un reset de saison est introduit un jour, ce
script devra filtrer match_player_stats par date de debut de saison.
"""

from __future__ import annotations

import os
import sys

from pymongo import MongoClient


def main() -> int:
    mongo_url = os.environ.get("MONGO_URL")
    if not mongo_url:
        print("ERROR: MONGO_URL not set", file=sys.stderr)
        return 1

    client: MongoClient = MongoClient(mongo_url)
    db = client["elobot"]

    per_player = list(
        db["match_player_stats"].aggregate(
            [
                {"$match": {"acs": {"$type": "number"}}},
                {
                    "$group": {
                        # $toString : l'_id des aggregats est "str(user_id):queue_type",
                        # on s'aligne meme si user_id etait stocke en int.
                        "_id": {
                            "user_id": {"$toString": "$user_id"},
                            "queue_type": "$queue_type",
                        },
                        "acs_sum": {"$sum": "$acs"},
                        "acs_games": {"$sum": 1},
                    }
                },
            ]
        )
    )
    computed = {
        f"{g['_id']['user_id']}:{g['_id']['queue_type']}": g for g in per_player
    }

    aggregates = db["player_rating_aggregates"]
    updated = 0
    cleared = 0
    for agg in aggregates.find({}, {"_id": 1}):
        g = computed.get(agg["_id"])
        if g is not None:
            aggregates.update_one(
                {"_id": agg["_id"]},
                {"$set": {"acs_sum": g["acs_sum"], "acs_games": g["acs_games"]}},
            )
            updated += 1
        else:
            res = aggregates.update_one(
                {"_id": agg["_id"]},
                {"$unset": {"acs_sum": "", "acs_games": ""}},
            )
            cleared += res.modified_count

    print(f"backfilled {updated} aggregates, cleared {cleared} without ACS data")
    return 0


if __name__ == "__main__":
    sys.exit(main())
