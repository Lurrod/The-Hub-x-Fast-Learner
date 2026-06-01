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


from dataclasses import replace


def test_apply_ban_removes_map_and_records_history():
    state = MapBanState.initial(cap_a=_p(1), cap_b=_p(2), maps=MAPS_7)
    state2 = state.apply_ban("Breeze")
    assert "Breeze" not in state2.remaining
    assert state2.banned == (("A", "Breeze"),)
    assert state2.turn_index == 1
    assert state2.status == "banning"


def test_apply_ban_is_immutable():
    state = MapBanState.initial(cap_a=_p(1), cap_b=_p(2), maps=MAPS_7)
    state.apply_ban("Breeze")
    # original unchanged
    assert state.remaining == MAPS_7
    assert state.banned == ()
    assert state.turn_index == 0


def test_apply_ban_raises_on_unknown_map():
    state = MapBanState.initial(cap_a=_p(1), cap_b=_p(2), maps=MAPS_7)
    with pytest.raises(ValueError, match="not in remaining"):
        state.apply_ban("Sunset")


def test_apply_ban_raises_on_already_banned_map():
    state = MapBanState.initial(cap_a=_p(1), cap_b=_p(2), maps=MAPS_7)
    state2 = state.apply_ban("Breeze")
    with pytest.raises(ValueError, match="not in remaining"):
        state2.apply_ban("Breeze")


def test_apply_ban_raises_when_cancelled():
    state = MapBanState.initial(cap_a=_p(1), cap_b=_p(2), maps=MAPS_7)
    cancelled = replace(state, status="cancelled")
    with pytest.raises(RuntimeError, match="cannot ban"):
        cancelled.apply_ban("Breeze")


def test_six_bans_leave_one_map_and_complete():
    state = MapBanState.initial(cap_a=_p(1), cap_b=_p(2), maps=MAPS_7)
    for m in ("Breeze", "Ascent", "Lotus", "Fracture", "Split", "Haven"):
        state = state.apply_ban(m)
    assert state.status == "complete"
    assert state.is_complete
    assert state.remaining == ("Pearl",)
    assert len(state.banned) == 6


def test_apply_ban_raises_when_complete():
    state = MapBanState.initial(cap_a=_p(1), cap_b=_p(2), maps=MAPS_7)
    for m in ("Breeze", "Ascent", "Lotus", "Fracture", "Split", "Haven"):
        state = state.apply_ban(m)
    with pytest.raises(RuntimeError, match="cannot ban"):
        state.apply_ban("Pearl")


def test_current_captain_raises_when_complete():
    state = MapBanState.initial(cap_a=_p(1), cap_b=_p(2), maps=MAPS_7)
    for m in ("Breeze", "Ascent", "Lotus", "Fracture", "Split", "Haven"):
        state = state.apply_ban(m)
    with pytest.raises(RuntimeError, match="no current captain"):
        _ = state.current_captain


def test_map_ban_result_from_complete_state():
    from services.map_pick_ban import MapBanResult

    state = MapBanState.initial(cap_a=_p(1), cap_b=_p(2), maps=MAPS_7)
    for m in ("Breeze", "Ascent", "Lotus", "Fracture", "Split", "Haven"):
        state = state.apply_ban(m)
    result = MapBanResult.from_state(state)
    assert result.selected_map == "Pearl"
    assert result.ban_history == (
        ("A", "Breeze"),
        ("B", "Ascent"),
        ("A", "Lotus"),
        ("B", "Fracture"),
        ("A", "Split"),
        ("B", "Haven"),
    )


def test_map_ban_result_raises_if_state_not_complete():
    from services.map_pick_ban import MapBanResult

    state = MapBanState.initial(cap_a=_p(1), cap_b=_p(2), maps=MAPS_7)
    with pytest.raises(ValueError, match="not complete"):
        MapBanResult.from_state(state)


def test_map_ban_cancelled_error_stores_reason_and_actor():
    from services.map_pick_ban import MapBanCancelledError

    class _FakeActor:
        id = 42

    actor = _FakeActor()
    err = MapBanCancelledError("admin", actor=actor)
    assert err.reason == "admin"
    assert err.actor is actor


def test_map_ban_cancelled_error_actor_defaults_to_none():
    from services.map_pick_ban import MapBanCancelledError

    err = MapBanCancelledError("system")
    assert err.reason == "system"
    assert err.actor is None
