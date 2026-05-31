# Captain Draft (Pro + Semi-Pro) + Map Pick/Ban — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Restore captain draft for Pro queue (deleted in commit `c9f0dd1` V5.0), extend it to Semi-Pro queue, and add a new map pick/ban phase after the draft.

**Architecture:** Two queue branches in `cogs/match/_cog.py`. Pro and Semi-Pro: move 10 players to `Waiting Match` VC → `CaptainDraftSession` (8 alternating picks ABABABAB) → `MapBanSession` (6 alternating bans ABABAB on 7 maps, 1 remains) → `build_plan_from_draft(map_name=...)`. Open and GC: unchanged, `plan_match(...)` (auto-balance + random map). The deleted `services/captain_draft.py` is restored verbatim from `c9f0dd1^`; the new `services/map_pick_ban.py` mirrors its shape.

**Tech Stack:** Python 3.12+, `discord.py`, `pytest`, `dataclasses(frozen=True)`, `asyncio`. Test framework: pytest with `pytest.mark.unit` / `pytest.mark.integration`. Formatting: black + ruff.

**Reference spec:** `docs/superpowers/specs/2026-05-31-captain-draft-and-map-pickban-design.md`

**User preference:** Never `git add` or `git commit` files under `docs/superpowers/`. Source code, tests, and config commits are expected and required.

---

## File map

| File | Action |
|---|---|
| `services/captain_draft.py` | **Restore** verbatim from `c9f0dd1^` |
| `services/map_pick_ban.py` | **Create** — pure state + Discord session |
| `services/match_service.py` | **Modify** — restore `build_plan_from_draft` with new `map_name: str \| None` param |
| `cogs/match/_cog.py` | **Modify** — branch on `queue_type in ("pro", "semipro")`, restore `_move_players_to_waiting_match`, add map ban session call |
| `tests/test_captain_draft.py` | **Restore** verbatim from `c9f0dd1^` |
| `tests/test_captain_draft_session.py` | **Restore** verbatim from `c9f0dd1^` |
| `tests/test_map_pick_ban.py` | **Create** — pure state tests |
| `tests/test_map_pick_ban_session.py` | **Create** — Discord session tests |
| `tests/test_match_cog.py` | **Modify** — add branch coverage for pro / semipro / open / gc |
| `tests/test_match_service.py` | **Modify or create** — coverage for `build_plan_from_draft` with `map_name` |

---

## Task 1: Restore `services/captain_draft.py` from git

**Files:**
- Create: `services/captain_draft.py` (430 lines)

- [ ] **Step 1: Extract the deleted file**

Run (PowerShell):
```powershell
git show c9f0dd1^:services/captain_draft.py | Out-File -Encoding utf8 services/captain_draft.py
```

Or in bash:
```bash
git show c9f0dd1^:services/captain_draft.py > services/captain_draft.py
```

- [ ] **Step 2: Verify import-cleanness**

Run: `python -c "import services.captain_draft; print('OK')"`
Expected: `OK` printed, no traceback.

- [ ] **Step 3: Run ruff + black on the file**

Run: `python -m ruff check services/captain_draft.py && python -m black --check services/captain_draft.py`
Expected: both pass clean (no fixes needed — the file was already formatted).

- [ ] **Step 4: Commit**

```bash
git add services/captain_draft.py
git commit -m "feat: restore services/captain_draft.py from V4.x for pro+semipro draft"
```

---

## Task 2: Restore `tests/test_captain_draft.py` and `tests/test_captain_draft_session.py` from git

**Files:**
- Create: `tests/test_captain_draft.py` (221 lines)
- Create: `tests/test_captain_draft_session.py` (271 lines)

- [ ] **Step 1: Extract both test files**

```bash
git show c9f0dd1^:tests/test_captain_draft.py > tests/test_captain_draft.py
git show c9f0dd1^:tests/test_captain_draft_session.py > tests/test_captain_draft_session.py
```

- [ ] **Step 2: Run the captain_draft tests**

Run: `pytest tests/test_captain_draft.py tests/test_captain_draft_session.py -v`
Expected: all tests pass. If a test fails because it imports `from services.match_service import build_plan_from_draft` (added in Task 3), comment those imports/tests temporarily and re-enable in Task 3.

- [ ] **Step 3: Commit**

```bash
git add tests/test_captain_draft.py tests/test_captain_draft_session.py
git commit -m "test: restore captain_draft tests from V4.x"
```

---

## Task 3: Restore `build_plan_from_draft` in `services/match_service.py` with optional `map_name`

**Files:**
- Modify: `services/match_service.py` (append `build_plan_from_draft` function)

- [ ] **Step 1: Write the failing test**

Create `tests/test_match_service.py` (if absent, otherwise append to existing). Add:

```python
import random
from types import SimpleNamespace

import pytest

from services.match_service import build_plan_from_draft
from services.team_balancer import Player

pytestmark = pytest.mark.unit


def _p(uid: int, elo: int = 2000) -> Player:
    return Player(id=uid, name=f"P{uid}", elo=elo)


def _draft_result(team_a_ids: list[int], team_b_ids: list[int]):
    """Build a fake DraftResult duck-typed for build_plan_from_draft."""
    team_a = tuple(_p(i) for i in team_a_ids)
    team_b = tuple(_p(i) for i in team_b_ids)
    return SimpleNamespace(
        cap_a=team_a[0],
        cap_b=team_b[0],
        team_a=team_a,
        team_b=team_b,
    )


def test_build_plan_from_draft_uses_provided_map_name():
    result = _draft_result([1, 2, 3, 4, 5], [6, 7, 8, 9, 10])
    plan = build_plan_from_draft(
        result,
        free_category="Match #1",
        rng=random.Random(0),
        map_name="Haven",
    )
    assert plan.map_name == "Haven"
    assert plan.category_name == "Match #1"
    assert plan.lobby_leader.id == 1  # cap_a
    assert plan.teams.team_a == result.team_a
    assert plan.teams.team_b == result.team_b


def test_build_plan_from_draft_falls_back_to_random_map_when_none():
    result = _draft_result([1, 2, 3, 4, 5], [6, 7, 8, 9, 10])
    plan = build_plan_from_draft(
        result,
        free_category="Match #1",
        rng=random.Random(0),
        map_name=None,
    )
    # Random.choice with seed 0 over the 7-map tuple is deterministic.
    # Just assert it's a valid map from MAPS.
    from services.elo_calc import MAPS
    assert plan.map_name in MAPS


def test_build_plan_from_draft_computes_elo_diff():
    # team_a heavy: 5 players at 3000 -> sum 15000
    # team_b light: 5 players at 2000 -> sum 10000
    team_a = tuple(Player(id=i, name=f"A{i}", elo=3000) for i in range(1, 6))
    team_b = tuple(Player(id=i, name=f"B{i}", elo=2000) for i in range(6, 11))
    result = SimpleNamespace(cap_a=team_a[0], cap_b=team_b[0], team_a=team_a, team_b=team_b)
    plan = build_plan_from_draft(
        result, free_category="Match #1", rng=random.Random(0), map_name="Ascent"
    )
    assert plan.teams.elo_diff == 5000
    assert plan.teams.peak_diff == 1000
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_match_service.py -v -k build_plan_from_draft`
Expected: FAIL with `ImportError: cannot import name 'build_plan_from_draft'`.

- [ ] **Step 3: Add `build_plan_from_draft` to `services/match_service.py`**

Append at the end of `services/match_service.py` (before `serialize_team` or after `plan_match`, your choice — keep with `plan_match`):

```python
def build_plan_from_draft(
    result,  # services.captain_draft.DraftResult (duck-typed to avoid import cycle)
    *,
    free_category: str,
    rng: random.Random,
    map_name: str | None = None,
) -> MatchPlan:
    """Build a MatchPlan from a captain DraftResult.

    Used on the Pro / Semi-Pro branch where teams come from the captain
    draft (not balance_teams). Computes elo_diff/peak_diff for info only.

    Args:
        result:        DraftResult with team_a, team_b, cap_a, cap_b.
        free_category: name of the free `Match #N` category.
        rng:           random source (used only when map_name is None).
        map_name:      map chosen by the map ban phase; if None, falls
                       back to rng.choice(MAPS).
    """
    team_a = result.team_a
    team_b = result.team_b
    sum_a = sum(p.elo for p in team_a)
    sum_b = sum(p.elo for p in team_b)
    max_a = max(p.elo for p in team_a)
    max_b = max(p.elo for p in team_b)
    teams = BalancedTeams(
        team_a=team_a,
        team_b=team_b,
        elo_diff=abs(sum_a - sum_b),
        peak_diff=abs(max_a - max_b),
    )
    chosen_map = map_name if map_name is not None else rng.choice(elo_calc.MAPS)
    return MatchPlan(
        teams=teams,
        map_name=chosen_map,
        lobby_leader=result.cap_a,
        category_name=free_category,
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_match_service.py -v -k build_plan_from_draft`
Expected: 3 PASSED.

- [ ] **Step 5: Re-run captain_draft tests (if any were skipped in Task 2)**

Run: `pytest tests/test_captain_draft.py tests/test_captain_draft_session.py -v`
Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add services/match_service.py tests/test_match_service.py
git commit -m "feat: restore build_plan_from_draft with optional map_name param"
```

---

## Task 4: Pure logic for `MapBanState` — initial state + `current_captain`

**Files:**
- Create: `tests/test_map_pick_ban.py`
- Create: `services/map_pick_ban.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_map_pick_ban.py`:

```python
"""Pure tests for the map pick/ban state machine."""

from __future__ import annotations

import pytest

from services.map_pick_ban import (
    BAN_SEQUENCE,
    MapBanState,
    MapBanResult,
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
    # turn 0 = A, turn 1 = B, turn 2 = A...
    assert state.current_captain.id == 1
    state2 = state.apply_ban("Breeze")
    assert state2.current_captain.id == 2
    state3 = state2.apply_ban("Ascent")
    assert state3.current_captain.id == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_map_pick_ban.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'services.map_pick_ban'`.

- [ ] **Step 3: Create `services/map_pick_ban.py` with initial state + `current_captain`**

```python
"""Pro / Semi-Pro Queue Map Pick & Ban Service.

Used after the captain draft completes: cap_a bans first, then alternating
ABABAB (6 bans on 7 maps). The 1 remaining map is the match map.

Module isolated by queue: open and gc queues do NOT call this. They keep
plan_match (random map).

Module structure mirrors services/captain_draft.py for consistency:
  - pure MapBanState dataclass (immutable, testable)
  - MapBanResult dataclass (final outcome)
  - MapBanSession class (Discord UI + event loop)
  - MapBanCancelledError exception
"""

from __future__ import annotations

import asyncio
import contextlib
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
    ) -> "MapBanState":
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
```

- [ ] **Step 4: Add `apply_ban` stub so `current_captain` test passes**

Append to the `MapBanState` class:

```python
    def apply_ban(self, map_name: str) -> "MapBanState":
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
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_map_pick_ban.py -v`
Expected: 3 PASSED.

- [ ] **Step 6: Commit**

```bash
git add services/map_pick_ban.py tests/test_map_pick_ban.py
git commit -m "feat(map-ban): MapBanState initial + current_captain + apply_ban"
```

---

## Task 5: `MapBanState.apply_ban` error cases + immutability + completion

**Files:**
- Modify: `tests/test_map_pick_ban.py`

- [ ] **Step 1: Add failing tests**

Append to `tests/test_map_pick_ban.py`:

```python
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
    cancelled = replace_status(state, "cancelled")
    with pytest.raises(RuntimeError, match="cancelled"):
        cancelled.apply_ban("Breeze")


def replace_status(state: MapBanState, status: str) -> MapBanState:
    from dataclasses import replace
    return replace(state, status=status)


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
    with pytest.raises(RuntimeError, match="complete"):
        state.apply_ban("Pearl")


def test_current_captain_raises_when_complete():
    state = MapBanState.initial(cap_a=_p(1), cap_b=_p(2), maps=MAPS_7)
    for m in ("Breeze", "Ascent", "Lotus", "Fracture", "Split", "Haven"):
        state = state.apply_ban(m)
    with pytest.raises(RuntimeError, match="complete"):
        _ = state.current_captain
```

- [ ] **Step 2: Run tests to verify they pass**

The implementation from Task 4 already covers these cases. Run:

```bash
pytest tests/test_map_pick_ban.py -v
```

Expected: all PASS. If `test_apply_ban_raises_when_cancelled` fails, the error message check is too strict — adjust either the message in `apply_ban` to include the literal `"cancelled"`, or relax the regex to `match="status="`.

- [ ] **Step 3: Commit**

```bash
git add tests/test_map_pick_ban.py
git commit -m "test(map-ban): immutability, error cases, completion path"
```

---

## Task 6: `MapBanResult.from_state` + `MapBanCancelledError`

**Files:**
- Modify: `services/map_pick_ban.py`
- Modify: `tests/test_map_pick_ban.py`

- [ ] **Step 1: Add failing tests**

Append to `tests/test_map_pick_ban.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_map_pick_ban.py -v -k MapBanResult`
Expected: FAIL with `cannot import name 'MapBanResult'`.

- [ ] **Step 3: Implement `MapBanResult` and `MapBanCancelledError`**

Append to `services/map_pick_ban.py` (after the `MapBanState` class):

```python
@dataclass(frozen=True)
class MapBanResult:
    selected_map: str
    ban_history: tuple[tuple[Literal["A", "B"], str], ...]

    @classmethod
    def from_state(cls, state: MapBanState) -> "MapBanResult":
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_map_pick_ban.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add services/map_pick_ban.py tests/test_map_pick_ban.py
git commit -m "feat(map-ban): MapBanResult + MapBanCancelledError"
```

---

## Task 7: `MapBanSession` Discord orchestration class

**Files:**
- Modify: `services/map_pick_ban.py`
- Create: `tests/test_map_pick_ban_session.py`

- [ ] **Step 1: Add helper functions and `MapBanSession` to `services/map_pick_ban.py`**

Append (after `MapBanCancelledError`):

```python
def _is_admin(user: Any, role_names: tuple[str, ...]) -> bool:
    """Same logic as captain_draft._is_admin: manage_guild OR named role."""
    perms = getattr(user, "guild_permissions", None)
    if perms is not None and getattr(perms, "manage_guild", False):
        return True
    return any(r.name in role_names for r in getattr(user, "roles", []))


def _build_team_lines(players: tuple[Player, ...]) -> str:
    if not players:
        return "_(empty)_"
    return "\n".join(f"• <@{p.id}>" for p in players)


def _build_banned_lines(banned: tuple[tuple[Literal["A", "B"], str], ...]) -> str:
    if not banned:
        return "_(none yet)_"
    return "\n".join(f"{'🅰️' if side == 'A' else '🅱️'} ~~{m}~~" for side, m in banned)


def _build_remaining_lines(remaining: tuple[str, ...]) -> str:
    if not remaining:
        return "_(empty)_"
    return "\n".join(f"• {m}" for m in remaining)


def _build_sequence_marker(turn_index: int) -> str:
    parts = []
    for i, side in enumerate(BAN_SEQUENCE):
        if i == turn_index:
            parts.append(f"·{side}·")
        else:
            parts.append(side)
    return " ".join(parts)


class MapBanSession:
    """Posts the map ban embed and resolves to a MapBanResult after 6 bans.

    Args:
        prep_channel:      Discord text channel where the embed is posted.
        cap_a / cap_b:     captains from the captain draft (cap_a bans first).
        maps:              tuple of map names (typically elo_calc.MAPS, 7 maps).
        admin_role_names:  roles allowed to cancel (typically ADMIN_ROLE_NAMES).

    Raises MapBanCancelledError if an admin cancels.
    """

    def __init__(
        self,
        *,
        prep_channel: Any,
        cap_a: Player,
        cap_b: Player,
        maps: Sequence[str],
        admin_role_names: tuple[str, ...],
    ):
        self.prep_channel = prep_channel
        self.state = MapBanState.initial(cap_a=cap_a, cap_b=cap_b, maps=maps)
        self.admin_role_names = admin_role_names
        self.message: Any | None = None
        self._lock = asyncio.Lock()
        self._done: asyncio.Future[MapBanResult] | None = None

    async def run(self) -> MapBanResult:
        loop = asyncio.get_running_loop()
        self._done = loop.create_future()
        embed = self._build_embed()
        view = self._build_view()
        content = (
            f"<@{self.state.cap_a.id}> <@{self.state.cap_b.id}> "
            f"- map ban phase, à vous de bannir !"
        )
        self.message = await self.prep_channel.send(content=content, embed=embed, view=view)
        logger.info(
            "[map_ban] init cap_a=%s cap_b=%s maps=%d",
            self.state.cap_a.id,
            self.state.cap_b.id,
            len(self.state.remaining),
        )
        return await self._done

    def _build_embed(self) -> Any:
        import discord

        e = discord.Embed(
            title="🗺️ Map Pick & Ban",
            color=discord.Color.blue(),
        )
        e.add_field(
            name=f"🅰️ Cap. <@{self.state.cap_a.id}>",
            value="_team A_",
            inline=True,
        )
        e.add_field(
            name=f"🅱️ Cap. <@{self.state.cap_b.id}>",
            value="_team B_",
            inline=True,
        )
        e.add_field(
            name="❌ Banned maps",
            value=_build_banned_lines(self.state.banned),
            inline=False,
        )
        e.add_field(
            name="🗺️ Remaining maps",
            value=_build_remaining_lines(self.state.remaining),
            inline=False,
        )
        if self.state.is_complete:
            e.set_footer(text=f"✅ Map selected: {self.state.remaining[0]}")
        elif self.state.status == "banning":
            cur = self.state.current_captain
            seq = _build_sequence_marker(self.state.turn_index)
            e.add_field(
                name=f"⏳ <@{cur.id}>'s turn — ban #{self.state.turn_index + 1}",
                value=f"Sequence: {seq}",
                inline=False,
            )
        return e

    def _build_view(self) -> Any:
        import discord

        session = self

        class _View(discord.ui.View):
            def __init__(self) -> None:
                super().__init__(timeout=None)

            async def interaction_check(self, interaction: discord.Interaction) -> bool:
                return await session._interaction_check(interaction)

        view = _View()
        if not self.state.is_complete and self.state.status == "banning":
            options = [
                discord.SelectOption(label=m, value=m)
                for m in self.state.remaining
            ]
            select: discord.ui.Select[Any] = discord.ui.Select(
                custom_id="map_ban_pick",
                placeholder="Choose a map to ban",
                min_values=1,
                max_values=1,
                options=options,
            )

            async def _select_cb(interaction: discord.Interaction) -> None:
                await session._on_ban(interaction)

            select.callback = _select_cb  # type: ignore[method-assign]
            view.add_item(select)

        cancel_btn: discord.ui.Button[Any] = discord.ui.Button(
            custom_id="map_ban_cancel",
            style=discord.ButtonStyle.danger,
            label="❌ Cancel ban phase",
            disabled=self.state.status != "banning",
        )

        async def _cancel_cb(interaction: discord.Interaction) -> None:
            await session._on_cancel(interaction)

        cancel_btn.callback = _cancel_cb  # type: ignore[method-assign]
        view.add_item(cancel_btn)
        return view

    async def _interaction_check(self, interaction: Any) -> bool:
        cid = interaction.data.get("custom_id", "")
        if cid == "map_ban_pick":
            if self.state.is_complete or interaction.user.id != self.state.current_captain.id:
                await interaction.response.send_message(
                    "⏳ It's not your turn.",
                    ephemeral=True,
                )
                return False
        elif cid == "map_ban_cancel" and not _is_admin(
            interaction.user, self.admin_role_names
        ):
            await interaction.response.send_message(
                "❌ Admins only.",
                ephemeral=True,
            )
            return False
        return True

    async def _on_ban(self, interaction: Any) -> None:
        async with self._lock:
            if self.state.status != "banning":
                with contextlib.suppress(Exception):
                    await interaction.response.defer()
                return
            picked_map = interaction.data["values"][0]
            if picked_map not in self.state.remaining:
                await interaction.response.send_message(
                    "❌ Map already banned.",
                    ephemeral=True,
                )
                return
            self.state = self.state.apply_ban(picked_map)
            logger.info(
                "[map_ban] ban turn=%d by=%s map=%s",
                self.state.turn_index - 1,
                interaction.user.id,
                picked_map,
            )
            embed = self._build_embed()
            view = self._build_view()
            try:
                await interaction.response.edit_message(embed=embed, view=view)
            except Exception:
                logger.exception(
                    "[map_ban] edit_message via interaction raised, fallback message.edit"
                )
                if self.message is not None:
                    with contextlib.suppress(Exception):
                        await self.message.edit(embed=embed, view=view)
            if self.state.is_complete and self._done is not None and not self._done.done():
                self._done.set_result(MapBanResult.from_state(self.state))

    async def _on_cancel(self, interaction: Any) -> None:
        async with self._lock:
            if self.state.status != "banning":
                with contextlib.suppress(Exception):
                    await interaction.response.defer()
                return
            self.state = replace(self.state, status="cancelled")
            actor = interaction.user
            embed = self._build_embed()
            embed.title = "❌ Map ban cancelled"
            embed.description = f"Cancelled by <@{actor.id}>"
            view = self._build_view()
            try:
                await interaction.response.edit_message(embed=embed, view=view)
            except Exception:
                logger.exception(
                    "[map_ban] edit_message via interaction raised, fallback message.edit"
                )
                if self.message is not None:
                    with contextlib.suppress(Exception):
                        await self.message.edit(embed=embed, view=view)
            logger.info("[map_ban] cancelled by=%s", actor.id)
            if self._done is not None and not self._done.done():
                self._done.set_exception(MapBanCancelledError("admin", actor))
```

- [ ] **Step 2: Create the session test file**

Create `tests/test_map_pick_ban_session.py`. Use the existing `tests/test_captain_draft_session.py` as a structural reference — the fixture patterns (mock channel, mock interaction with `data["values"]`, fake user with `guild_permissions.manage_guild`) are identical here. Implementation:

```python
"""Tests for MapBanSession (Discord orchestration), Discord mocked."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from services.map_pick_ban import (
    BAN_SEQUENCE,
    MapBanCancelledError,
    MapBanResult,
    MapBanSession,
)
from services.team_balancer import Player

pytestmark = pytest.mark.integration


MAPS_7 = ("Breeze", "Ascent", "Lotus", "Fracture", "Split", "Haven", "Pearl")
ADMIN_ROLES = ("ADMINISTRATORS",)


def _p(uid: int) -> Player:
    return Player(id=uid, name=f"P{uid}", elo=2000)


def _mock_interaction(*, user_id: int, picked_map: str | None = None, custom_id: str, is_admin: bool = False):
    interaction = MagicMock()
    interaction.user = SimpleNamespace(
        id=user_id,
        guild_permissions=SimpleNamespace(manage_guild=is_admin),
        roles=[SimpleNamespace(name="ADMINISTRATORS")] if is_admin else [],
    )
    interaction.data = {"custom_id": custom_id}
    if picked_map is not None:
        interaction.data["values"] = [picked_map]
    interaction.response = MagicMock()
    interaction.response.send_message = AsyncMock()
    interaction.response.edit_message = AsyncMock()
    interaction.response.defer = AsyncMock()
    return interaction


@pytest.mark.asyncio
async def test_six_bans_resolve_to_pearl_remaining():
    """cap_a bans Breeze, cap_b Ascent, ... 6 bans leave Pearl."""
    channel = MagicMock()
    channel.send = AsyncMock(return_value=MagicMock(edit=AsyncMock()))
    cap_a, cap_b = _p(1), _p(2)
    session = MapBanSession(
        prep_channel=channel,
        cap_a=cap_a, cap_b=cap_b,
        maps=MAPS_7,
        admin_role_names=ADMIN_ROLES,
    )
    run_task = asyncio.create_task(session.run())
    # let session.run() post the initial message
    await asyncio.sleep(0)

    bans = [
        (1, "Breeze"),
        (2, "Ascent"),
        (1, "Lotus"),
        (2, "Fracture"),
        (1, "Split"),
        (2, "Haven"),
    ]
    for uid, m in bans:
        inter = _mock_interaction(user_id=uid, picked_map=m, custom_id="map_ban_pick")
        await session._on_ban(inter)

    result = await asyncio.wait_for(run_task, timeout=1.0)
    assert isinstance(result, MapBanResult)
    assert result.selected_map == "Pearl"
    assert len(result.ban_history) == 6


@pytest.mark.asyncio
async def test_non_current_captain_cannot_ban():
    """cap_b tries to ban on cap_a's turn -> ephemeral refusal."""
    channel = MagicMock()
    channel.send = AsyncMock(return_value=MagicMock(edit=AsyncMock()))
    session = MapBanSession(
        prep_channel=channel,
        cap_a=_p(1), cap_b=_p(2),
        maps=MAPS_7,
        admin_role_names=ADMIN_ROLES,
    )
    asyncio.create_task(session.run())
    await asyncio.sleep(0)

    # cap_b on turn 0 (which is A's turn)
    inter = _mock_interaction(user_id=2, picked_map="Breeze", custom_id="map_ban_pick")
    allowed = await session._interaction_check(inter)
    assert allowed is False
    inter.response.send_message.assert_awaited_once()
    args, kwargs = inter.response.send_message.call_args
    assert kwargs.get("ephemeral") is True


@pytest.mark.asyncio
async def test_admin_cancel_raises_map_ban_cancelled_error():
    channel = MagicMock()
    channel.send = AsyncMock(return_value=MagicMock(edit=AsyncMock()))
    session = MapBanSession(
        prep_channel=channel,
        cap_a=_p(1), cap_b=_p(2),
        maps=MAPS_7,
        admin_role_names=ADMIN_ROLES,
    )
    run_task = asyncio.create_task(session.run())
    await asyncio.sleep(0)

    admin_inter = _mock_interaction(
        user_id=99, custom_id="map_ban_cancel", is_admin=True
    )
    await session._on_cancel(admin_inter)

    with pytest.raises(MapBanCancelledError):
        await asyncio.wait_for(run_task, timeout=1.0)


@pytest.mark.asyncio
async def test_non_admin_cannot_cancel():
    channel = MagicMock()
    channel.send = AsyncMock(return_value=MagicMock(edit=AsyncMock()))
    session = MapBanSession(
        prep_channel=channel,
        cap_a=_p(1), cap_b=_p(2),
        maps=MAPS_7,
        admin_role_names=ADMIN_ROLES,
    )
    asyncio.create_task(session.run())
    await asyncio.sleep(0)

    non_admin = _mock_interaction(user_id=42, custom_id="map_ban_cancel", is_admin=False)
    allowed = await session._interaction_check(non_admin)
    assert allowed is False
    non_admin.response.send_message.assert_awaited_once()


def test_ban_sequence_constant_for_external_consumers():
    assert BAN_SEQUENCE == ("A", "B", "A", "B", "A", "B")
```

- [ ] **Step 3: Run session tests to verify they pass**

Run: `pytest tests/test_map_pick_ban_session.py -v`
Expected: 5 PASSED.

If any test hangs, the most likely cause is an unawaited future — check that `_on_ban` / `_on_cancel` set `_done` (look at the corresponding pattern in `CaptainDraftSession`).

- [ ] **Step 4: Run full suite to catch regressions**

Run: `pytest -q`
Expected: all PASS (previously 400/400, now ~420+).

- [ ] **Step 5: Commit**

```bash
git add services/map_pick_ban.py tests/test_map_pick_ban_session.py
git commit -m "feat(map-ban): MapBanSession Discord orchestration + tests"
```

---

## Task 8: Restore `_move_players_to_waiting_match` in `cogs/match/_cog.py`

**Files:**
- Modify: `cogs/match/_cog.py`

- [ ] **Step 1: Recover the deleted method**

Run:
```bash
git show c9f0dd1^:cogs/match/_cog.py | sed -n '483,545p'
```
Expected output: the full `_move_players_to_waiting_match` method (~50 lines).

- [ ] **Step 2: Insert the method into `cogs/match/_cog.py`**

Add it right after `_move_players_to_match_vc` (around line 460 in the current file). The full method body (verbatim from git):

```python
    async def _move_players_to_waiting_match(
        self,
        guild,
        category,
        player_ids: list[str],
    ) -> None:
        """Move all `player_ids` to the 'Waiting Match' VC of `category`.

        Used on the Pro / Semi-Pro branch BEFORE the captain draft, so the
        10 players are grouped in one VC while captains pick their teams.

        Guards:
          - skip if guild.get_member returns None
          - skip if member is not in voice
          - skip if already at destination
        """
        waiting_match = discord.utils.get(category.voice_channels, name="Waiting Match")
        if waiting_match is None:
            logger.warning(
                "[match] _move_players_to_waiting_match: 'Waiting Match' "
                "not found in %s, no-op",
                category.name,
            )
            return

        async def _move_one(uid_str: str) -> None:
            try:
                uid = int(uid_str)
            except (TypeError, ValueError):
                return
            member = guild.get_member(uid)
            if member is None:
                return
            voice = getattr(member, "voice", None)
            if voice is None or voice.channel is None:
                return
            if voice.channel.id == waiting_match.id:
                return
            async with self._guild_member_edit_sem:
                with contextlib.suppress(discord.Forbidden, discord.HTTPException):
                    await member.move_to(
                        waiting_match,
                        reason="Pro/Semi-Pro Queue: grouping before captain draft",
                    )

        await asyncio.gather(
            *[_move_one(uid) for uid in player_ids],
            return_exceptions=True,
        )
```

- [ ] **Step 3: Verify the file still loads**

Run: `python -c "import cogs.match._cog; print('OK')"`
Expected: `OK`.

- [ ] **Step 4: Commit**

```bash
git add cogs/match/_cog.py
git commit -m "feat(match): restore _move_players_to_waiting_match helper"
```

---

## Task 9: Add imports in `cogs/match/_cog.py`

**Files:**
- Modify: `cogs/match/_cog.py` (top imports block)

- [ ] **Step 1: Add the new imports**

In `cogs/match/_cog.py`, modify the import block at the top:

Replace:
```python
from services import elo_calc, repository
from services.elo_updater import (
    apply_match_validation,
)
```

With:
```python
from services import elo_calc, repository
from services.captain_draft import (
    CaptainDraftSession,
    DraftCancelledError,
    pick_captains,
)
from services.elo_updater import (
    apply_match_validation,
)
from services.map_pick_ban import (
    MapBanCancelledError,
    MapBanSession,
)
```

And in the `from services.match_service import (...)` block, add `build_plan_from_draft`:

Replace:
```python
from services.match_service import (
    build_players,
    plan_match,
    serialize_team,
)
```

With:
```python
from services.match_service import (
    build_plan_from_draft,
    build_players,
    plan_match,
    serialize_team,
)
```

- [ ] **Step 2: Verify the file still loads**

Run: `python -c "import cogs.match._cog; print('OK')"`
Expected: `OK`.

- [ ] **Step 3: Commit (no behavior change yet)**

```bash
git add cogs/match/_cog.py
git commit -m "feat(match): import captain_draft and map_pick_ban services"
```

---

## Task 10: Branch on `queue_type in ("pro", "semipro")` in `on_queue_full`

**Files:**
- Modify: `cogs/match/_cog.py` (around line 193, where `plan = plan_match(...)` is called)

- [ ] **Step 1: Replace the single `plan_match` call with the branch**

In `cogs/match/_cog.py`, locate the line `plan = plan_match(players, free_category=free_cat_name, rng=self.rng)` (around line 193) and replace with:

```python
        # Pro / Semi-Pro: captain draft + map ban. Open / GC: auto-balance + random map.
        if queue_type in ("pro", "semipro"):
            player_ids_for_move = [str(p.id) for p in players]
            await self._move_players_to_waiting_match(
                guild,
                category,
                player_ids_for_move,
            )
            cap_a, cap_b = pick_captains(players, rng=self.rng)
            pool = tuple(p for p in players if p.id not in (cap_a.id, cap_b.id))
            draft_session = CaptainDraftSession(
                prep_channel=prep_channel,
                cap_a=cap_a,
                cap_b=cap_b,
                pool=pool,
                admin_role_names=ADMIN_ROLE_NAMES,
            )
            try:
                draft_result = await draft_session.run()
            except DraftCancelledError as exc:
                logger.info(
                    "[match] draft cancelled (reason=%s actor=%s) - queue preserved",
                    exc.reason,
                    getattr(exc.actor, "id", None),
                )
                with contextlib.suppress(discord.HTTPException):
                    await interaction.followup.send(
                        "❌ Draft cancelled. The queue stays active. "
                        "`/leave` then `/join` to reset if needed.",
                        ephemeral=False,
                    )
                try:
                    await delete_match_category(
                        guild=guild,
                        category_id=category.id,
                        reason=f"Match #{match_number} draft cancelled",
                    )
                except Exception:
                    logger.exception("[match] failed to delete category on draft cancel")
                return None
            except Exception:
                logger.exception(
                    "[match] captain draft failed for #%d, rolling back category",
                    match_number,
                )
                await delete_match_category(
                    guild=guild,
                    category_id=category.id,
                    reason=f"Match #{match_number} draft aborted",
                )
                with contextlib.suppress(discord.HTTPException):
                    await interaction.followup.send(
                        f"❌ The draft for Match #{match_number} failed, match cancelled.",
                        ephemeral=True,
                    )
                return None

            ban_session = MapBanSession(
                prep_channel=prep_channel,
                cap_a=cap_a,
                cap_b=cap_b,
                maps=elo_calc.MAPS,
                admin_role_names=ADMIN_ROLE_NAMES,
            )
            try:
                ban_result = await ban_session.run()
            except MapBanCancelledError as exc:
                logger.info(
                    "[match] map ban cancelled (reason=%s actor=%s) - queue preserved",
                    exc.reason,
                    getattr(exc.actor, "id", None),
                )
                with contextlib.suppress(discord.HTTPException):
                    await interaction.followup.send(
                        "❌ Map ban cancelled. The queue stays active. "
                        "`/leave` then `/join` to reset if needed.",
                        ephemeral=False,
                    )
                try:
                    await delete_match_category(
                        guild=guild,
                        category_id=category.id,
                        reason=f"Match #{match_number} map ban cancelled",
                    )
                except Exception:
                    logger.exception("[match] failed to delete category on map ban cancel")
                return None
            except Exception:
                logger.exception(
                    "[match] map ban failed for #%d, rolling back category",
                    match_number,
                )
                await delete_match_category(
                    guild=guild,
                    category_id=category.id,
                    reason=f"Match #{match_number} map ban aborted",
                )
                with contextlib.suppress(discord.HTTPException):
                    await interaction.followup.send(
                        f"❌ The map ban for Match #{match_number} failed, match cancelled.",
                        ephemeral=True,
                    )
                return None

            plan = build_plan_from_draft(
                draft_result,
                free_category=free_cat_name,
                rng=self.rng,
                map_name=ban_result.selected_map,
            )
        else:
            plan = plan_match(players, free_category=free_cat_name, rng=self.rng)
```

- [ ] **Step 2: Verify file loads + format**

Run:
```bash
python -c "import cogs.match._cog; print('OK')"
python -m ruff check cogs/match/_cog.py
python -m black --check cogs/match/_cog.py
```
If `ruff` / `black` flag issues, apply:
```bash
python -m black cogs/match/_cog.py
python -m ruff check --fix cogs/match/_cog.py
```

- [ ] **Step 3: Run full test suite to detect breakage**

Run: `pytest -q`
Expected: most tests pass, but `tests/test_match_cog.py` may show new failures because the existing tests don't mock the draft/ban path for pro/semipro. These are addressed in Task 11.

- [ ] **Step 4: Commit**

```bash
git add cogs/match/_cog.py
git commit -m "feat(match): captain draft + map ban for pro and semipro queues"
```

---

## Task 11: Update `tests/test_match_cog.py` for the new branches

**Files:**
- Modify: `tests/test_match_cog.py`

- [ ] **Step 1: Inspect current test layout**

Run:
```bash
grep -n "queue_type\|plan_match\|on_queue_full\|def test_" tests/test_match_cog.py | head -40
```
Note the existing test that exercises `on_queue_full`. The change in Task 10 means that for `queue_type` in `("pro", "semipro")`, `plan_match` is NOT called. The existing tests likely set `queue_type="open"` so they should keep working, but verify.

- [ ] **Step 2: Add branch tests**

Add new tests to `tests/test_match_cog.py` (adapt fixture names to whatever the file already uses):

```python
@pytest.mark.asyncio
async def test_on_queue_full_open_queue_uses_plan_match(monkeypatch, match_cog_fixture):
    """Open queue: plan_match is called, no draft / map ban session constructed."""
    called = {"plan_match": 0, "draft": 0, "ban": 0}

    def fake_plan_match(*a, **kw):
        called["plan_match"] += 1
        return MagicMock(teams=MagicMock(team_a=(), team_b=()), map_name="Ascent",
                         lobby_leader=MagicMock(id=1), category_name="Match #1")

    class _FakeDraft:
        def __init__(self, *a, **kw):
            called["draft"] += 1
        async def run(self):
            raise AssertionError("Draft should not run for open queue")

    class _FakeBan:
        def __init__(self, *a, **kw):
            called["ban"] += 1
        async def run(self):
            raise AssertionError("Ban should not run for open queue")

    monkeypatch.setattr("cogs.match._cog.plan_match", fake_plan_match)
    monkeypatch.setattr("cogs.match._cog.CaptainDraftSession", _FakeDraft)
    monkeypatch.setattr("cogs.match._cog.MapBanSession", _FakeBan)

    # Use the existing on_queue_full invocation pattern with queue_type="open".
    # (Adapt to the fixture's signature.)
    ...  # call the fixture helper here

    assert called["plan_match"] == 1
    assert called["draft"] == 0
    assert called["ban"] == 0


@pytest.mark.asyncio
async def test_on_queue_full_pro_queue_runs_draft_then_ban(monkeypatch, match_cog_fixture):
    """Pro queue: draft runs first, then map ban, then build_plan_from_draft."""
    seq: list[str] = []

    class _FakeDraft:
        def __init__(self, *a, **kw):
            seq.append("draft_ctor")
        async def run(self):
            seq.append("draft_run")
            return MagicMock(
                cap_a=MagicMock(id=1),
                cap_b=MagicMock(id=2),
                team_a=tuple(MagicMock(id=i) for i in range(1, 6)),
                team_b=tuple(MagicMock(id=i) for i in range(6, 11)),
            )

    class _FakeBan:
        def __init__(self, *a, **kw):
            seq.append("ban_ctor")
        async def run(self):
            seq.append("ban_run")
            return MagicMock(selected_map="Haven", ban_history=())

    def fake_build_plan(*a, **kw):
        seq.append(f"build({kw.get('map_name')})")
        return MagicMock(teams=MagicMock(team_a=(), team_b=()), map_name="Haven",
                         lobby_leader=MagicMock(id=1), category_name="Match #1")

    monkeypatch.setattr("cogs.match._cog.CaptainDraftSession", _FakeDraft)
    monkeypatch.setattr("cogs.match._cog.MapBanSession", _FakeBan)
    monkeypatch.setattr("cogs.match._cog.build_plan_from_draft", fake_build_plan)
    # pick_captains is pure -> let it run normally, or stub for determinism:
    monkeypatch.setattr(
        "cogs.match._cog.pick_captains",
        lambda players, *, rng: (players[0], players[1]),
    )

    # Call on_queue_full with queue_type="pro" (adapt to fixture signature).
    ...

    assert seq == [
        "draft_ctor", "draft_run", "ban_ctor", "ban_run", "build(Haven)",
    ]


@pytest.mark.asyncio
async def test_on_queue_full_semipro_runs_draft_and_ban(monkeypatch, match_cog_fixture):
    """Semi-pro queue: same branching as pro queue."""
    # Identical assertions as the pro test but with queue_type="semipro".
    # Verifies the branch condition is `in ("pro", "semipro")` not `== "pro"`.
    ...
```

> **Engineer note:** The exact fixture signature for `match_cog_fixture` and the way `on_queue_full` is called depends on the existing test file. Read 30–50 lines around the existing `on_queue_full` test before writing yours. The `...` placeholders above mark the lines where you copy the existing invocation pattern. Replace `match_cog_fixture` with whatever the file already uses (e.g., `cog`, `match_cog`).

- [ ] **Step 3: Run the updated tests**

Run: `pytest tests/test_match_cog.py -v`
Expected: all PASS (new branch tests + existing tests).

- [ ] **Step 4: Run full suite**

Run: `pytest -q`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add tests/test_match_cog.py
git commit -m "test(match): branch coverage for pro/semipro draft+ban vs open/gc plan_match"
```

---

## Task 12: Manual smoke check + final cleanup

- [ ] **Step 1: Run full test suite one more time**

Run: `pytest -q --tb=short`
Expected: all PASS, no warnings about new collection deprecations.

- [ ] **Step 2: Run linters across the changed files**

Run:
```bash
python -m ruff check services/captain_draft.py services/map_pick_ban.py services/match_service.py cogs/match/_cog.py
python -m black --check services/captain_draft.py services/map_pick_ban.py services/match_service.py cogs/match/_cog.py
```
Expected: clean. If not, run `python -m black <files>` and re-commit.

- [ ] **Step 3: Verify no stale captain_draft / map_pick_ban references**

Run:
```bash
grep -rn "captain_draft\|map_pick_ban\|build_plan_from_draft\|MapBanSession\|CaptainDraftSession" --include="*.py" .
```
Expected: only the files we created/modified appear. No stray references in unrelated cogs.

- [ ] **Step 4: Smoke check — start the bot locally (optional but recommended)**

Run the bot in dev (whatever the project's entry is — likely `python bot.py`). Watch logs for:
- No `ImportError`
- `[map_ban]` and `[draft]` log lines are silent (no flow triggered yet)

If a Discord guild is available for testing, fill the pro queue with 10 alts → verify the draft message appears → complete picks → verify the map ban message appears → complete bans → verify the standard match embed appears with the selected map.

- [ ] **Step 5: Final commit if any formatting changes**

```bash
git status
git diff
# if anything was reformatted by black:
git add <files>
git commit -m "style: black formatting after feature integration"
```

---

## Done criteria

- All new tests pass (`pytest -q`).
- `services/captain_draft.py` and its tests are restored from `c9f0dd1^`.
- `services/map_pick_ban.py` exists with `MapBanState`, `MapBanResult`, `MapBanCancelledError`, `MapBanSession`, fully covered by tests.
- `cogs/match/_cog.py` calls `CaptainDraftSession` → `MapBanSession` → `build_plan_from_draft` for `queue_type in ("pro", "semipro")` and the original `plan_match` for everything else.
- No `docs/superpowers/specs/` or `docs/superpowers/plans/` files are committed (user preference).
- Manual smoke check passes on a test guild (Task 12 Step 4).
