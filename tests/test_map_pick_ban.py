"""Pure tests for the map pick/ban state machine."""

from __future__ import annotations

import pytest

from services.map_pick_ban import (
    BAN_SEQUENCE,
    MapBanState,
)
from services.team_balancer import Player

pytestmark = pytest.mark.unit


def _p(uid: int, elo: int = 2000) -> Player:
    return Player(id=uid, name=f"P{uid}", elo=elo)


MAPS_7 = ("Breeze", "Ascent", "Lotus", "Fracture", "Split", "Haven", "Pearl")


def test_ban_sequence_is_ABABAB():
    assert BAN_SEQUENCE == ("A", "B", "A", "B", "A", "B")


def test_initial_state():
    state = MapBanState.initial(cap_a=_p(1), cap_b=_p(2), maps=MAPS_7)
    assert state.cap_a.id == 1
    assert state.cap_b.id == 2
    assert state.remaining == MAPS_7
    assert state.banned == ()
    assert state.turn_index == 0
    assert state.status == "banning"
    assert not state.is_complete


def test_current_captain_alternates():
    state = MapBanState.initial(cap_a=_p(1), cap_b=_p(2), maps=MAPS_7)
    assert state.current_captain.id == 1  # turn 0 = A
    state2 = state.apply_ban("Breeze")
    assert state2.current_captain.id == 2  # turn 1 = B
    state3 = state2.apply_ban("Ascent")
    assert state3.current_captain.id == 1  # turn 2 = A
