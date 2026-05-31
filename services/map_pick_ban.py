"""Pro / Semi-Pro Queue Map Pick & Ban Service.

Used after the captain draft completes: cap_a bans first, then alternating
ABABAB (6 bans on 7 maps). The 1 remaining map is the match map.

Module isolated by queue: open and gc queues do NOT call this. They keep
plan_match (random map).

Module structure mirrors services/captain_draft.py for consistency:
  - pure MapBanState dataclass (immutable, testable)
  - MapBanResult dataclass (final outcome) -- added in Task 6
  - MapBanSession class (Discord UI + event loop) -- added in Task 7
  - MapBanCancelledError exception -- added in Task 6
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from typing import Any, Literal
from collections.abc import Sequence

from services.team_balancer import Player

logger = logging.getLogger(__name__)


# 6 bans on 7 maps -> 1 remains. cap_a bans first.
BAN_SEQUENCE: tuple[Literal["A", "B"], ...] = ("A", "B", "A", "B", "A", "B")

BanStatus = Literal["banning", "complete", "cancelled"]


@dataclass(frozen=True)
class MapBanState:
    cap_a: Player
    cap_b: Player
    remaining: tuple[str, ...]
    banned: tuple[tuple[Literal["A", "B"], str], ...]
    turn_index: int
    status: BanStatus

    @classmethod
    def initial(
        cls,
        *,
        cap_a: Player,
        cap_b: Player,
        maps: Sequence[str],
    ) -> MapBanState:
        return cls(
            cap_a=cap_a,
            cap_b=cap_b,
            remaining=tuple(maps),
            banned=(),
            turn_index=0,
            status="banning",
        )

    @property
    def is_complete(self) -> bool:
        return self.turn_index >= len(BAN_SEQUENCE)

    @property
    def current_captain(self) -> Player:
        if self.is_complete:
            raise RuntimeError("Ban phase complete: no current captain.")
        side = BAN_SEQUENCE[self.turn_index]
        return self.cap_a if side == "A" else self.cap_b

    def apply_ban(self, map_name: str) -> MapBanState:
        """Returns a new state with `map_name` removed from remaining.

        Raises:
            ValueError if map_name not in remaining.
            RuntimeError if status != "banning".
        """
        if self.status != "banning":
            raise RuntimeError(f"Ban status={self.status}, cannot ban.")
        if map_name not in self.remaining:
            raise ValueError(f"Map {map_name!r} not in remaining {self.remaining}.")
        side = BAN_SEQUENCE[self.turn_index]
        new_remaining = tuple(m for m in self.remaining if m != map_name)
        new_banned = (*self.banned, (side, map_name))
        new_turn = self.turn_index + 1
        new_status: BanStatus = "complete" if new_turn >= len(BAN_SEQUENCE) else "banning"
        return replace(
            self,
            remaining=new_remaining,
            banned=new_banned,
            turn_index=new_turn,
            status=new_status,
        )


@dataclass(frozen=True)
class MapBanResult:
    selected_map: str
    ban_history: tuple[tuple[Literal["A", "B"], str], ...]

    @classmethod
    def from_state(cls, state: MapBanState) -> MapBanResult:
        if state.status != "complete":
            raise ValueError(f"Ban state not complete (status={state.status}).")
        if len(state.remaining) != 1:
            raise ValueError(
                f"Expected exactly 1 map remaining, got {len(state.remaining)}."
            )
        return cls(
            selected_map=state.remaining[0],
            ban_history=state.banned,
        )


class MapBanCancelledError(Exception):
    """Raised when an admin cancels the map ban phase via the button."""

    def __init__(self, reason: str, actor: Any | None = None):
        super().__init__(reason)
        self.reason = reason
        self.actor = actor
