"""
Updates player ELO (shared collection `elo`) after the validation of a V2
match.

The gain/loss is proportional to the average effective_elo (Riot) of the
10 players of the match: avg=1500 -> +20/-10, avg=3000 -> +40/-20.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final

from pymongo import ReturnDocument

from services import elo_calc, repository


VALIDATED_A: Final[str] = "validated_a"
VALIDATED_B: Final[str] = "validated_b"

# Fixed fallback when HenrikDev does not provide ACS multipliers
# (custom not found after 30 min timeout, or extraction impossible:
# mixed Attack/Defense teams in a Valorant lobby). We apply a flat
# +20/-20 rather than the value proportional to the match avg ELO.
# More readable for players and avoids weird variations (15/17/19)
# depending on the average tier.
FLAT_FALLBACK_ELO_CHANGE: Final[int] = 16


@dataclass(frozen=True)
class PlayerEloChange:
    user_id: str
    name: str
    old_elo: int
    new_elo: int
    delta: int
    win: bool
    multiplier: float = 1.0


@dataclass(frozen=True)
class MatchEloOutcome:
    avg_elo: int
    gain: int
    loss: int
    changes: tuple[PlayerEloChange, ...]
    weighted: bool = False  # True if called with Henrik multipliers


def apply_match_validation(
    db,
    match_doc: dict,
    multipliers: dict[str, float] | None = None,
) -> MatchEloOutcome:
    """
    Distribute ELO in a single pass, **strict zero-sum**.

    Floor at 0: if a loser has less ELO than the computed loss, their
    delta is clamped to -old_elo (does not go below 0).

    Args:
        db:          mongomock/pymongo Database (shared ELO collection)
        match_doc:   match doc with `team_a`, `team_b`, `status`, `queue_type`
        multipliers: dict user_id (str) -> ACS multiplier (~0.7..1.3)

    Raises:
        ValueError if status != validated_a/b
    """
    status = match_doc.get("status")
    if status not in (VALIDATED_A, VALIDATED_B):
        raise ValueError(f"Match not valid: status={status}")

    queue_type = match_doc.get("queue_type", "open")

    if status == VALIDATED_A:
        winners, losers = match_doc["team_a"], match_doc["team_b"]
    else:
        winners, losers = match_doc["team_b"], match_doc["team_a"]

    avg_elo = elo_calc.compute_team_avg_elo(winners + losers)

    if multipliers is None:
        base_gain = base_loss = FLAT_FALLBACK_ELO_CHANGE
        mults: dict[str, float] = {}
        weighted = False
    else:
        base_gain, base_loss = elo_calc.compute_match_elo_change(avg_elo)
        mults = multipliers
        weighted = True

    elo_col = repository.get_elo_col(db)

    winner_mults = [float(mults.get(str(p["id"]), 1.0)) for p in winners]
    loser_mults = [float(mults.get(str(p["id"]), 1.0)) for p in losers]

    winner_deltas = [round(+base_gain * m) for m in winner_mults]
    loser_deltas = [round(-base_loss * (2.0 - m)) for m in loser_mults]

    # Clamp to 0 ELO for losers (compound _id for the lookup).
    loser_old_elos: list[int] = []
    for p in losers:
        doc = elo_col.find_one({"_id": repository.player_doc_id(p["id"], queue_type)})
        loser_old_elos.append(
            int(doc.get("elo", elo_calc.ELO_START)) if doc else elo_calc.ELO_START
        )
    clamped_loser_deltas = [
        max(-old, delta) for old, delta in zip(loser_old_elos, loser_deltas, strict=True)
    ]

    match_id = match_doc.get("_id")
    changes: list[PlayerEloChange] = []
    for p, delta, mult in zip(winners, winner_deltas, winner_mults, strict=True):
        changes.append(
            _apply_player(
                elo_col,
                p,
                queue_type=queue_type,
                match_id=match_id,
                delta=delta,
                win=True,
                multiplier=mult,
            )
        )
    for p, delta, mult in zip(losers, clamped_loser_deltas, loser_mults, strict=True):
        changes.append(
            _apply_player(
                elo_col,
                p,
                queue_type=queue_type,
                match_id=match_id,
                delta=delta,
                win=False,
                multiplier=mult,
            )
        )

    return MatchEloOutcome(
        avg_elo=avg_elo,
        gain=base_gain,
        loss=base_loss,
        changes=tuple(changes),
        weighted=weighted,
    )


def _apply_player(
    col,
    player: dict,
    *,
    queue_type: str,
    match_id,
    delta: int,
    win: bool,
    multiplier: float = 1.0,
) -> PlayerEloChange:
    """Apply the ELO delta idempotently per match.

    The player doc is identified by the compound _id `<user_id>:<queue_type>`.
    Per-match idempotence is preserved via `processed_matches`."""
    uid = str(player["id"])
    name = player.get("name", uid)
    doc_id = repository.player_doc_id(uid, queue_type)
    match_id_str = str(match_id) if match_id is not None else None

    col.update_one(
        {"_id": doc_id},
        {
            "$setOnInsert": {
                "name": name,
                "elo": elo_calc.ELO_START,
                "wins": 0,
                "losses": 0,
                "queue_type": queue_type,
                "user_id": uid,
            }
        },
        upsert=True,
    )

    inc_field = "wins" if win else "losses"
    update: dict[str, Any] = {
        "$inc": {"elo": delta, inc_field: 1},
        # last_played: timestamp of the last game played. Read by the
        # permanent Pro leaderboard to remove inactives (> 7 days).
        "$set": {"name": name, "last_played": datetime.now(UTC)},
    }
    if match_id_str is not None:
        update["$addToSet"] = {"processed_matches": match_id_str}
        filter_q = {"_id": doc_id, "processed_matches": {"$nin": [match_id_str]}}
    else:
        filter_q = {"_id": doc_id}

    pre = col.find_one_and_update(
        filter_q,
        update,
        return_document=ReturnDocument.BEFORE,
    )

    if pre is None:
        cur_doc = col.find_one({"_id": doc_id})
        cur_elo = int(cur_doc.get("elo", 0)) if cur_doc else 0
        return PlayerEloChange(
            user_id=uid,
            name=name,
            old_elo=cur_elo,
            new_elo=cur_elo,
            delta=0,
            win=win,
            multiplier=multiplier,
        )

    old_elo = int(pre.get("elo", 0))
    new_elo = old_elo + delta
    return PlayerEloChange(
        user_id=uid,
        name=name,
        old_elo=old_elo,
        new_elo=new_elo,
        delta=delta,
        win=win,
        multiplier=multiplier,
    )
