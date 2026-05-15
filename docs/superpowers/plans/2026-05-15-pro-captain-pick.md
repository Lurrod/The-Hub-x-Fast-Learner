# Pro Queue Captain Pick — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a captain draft phase to the Pro queue (only). Two top-ELO players become captains and alternate picking the 8 remaining players via snake order before the standard match flow continues. Open and GC queues remain unchanged.

**Architecture:** New isolated service `services/captain_draft.py` (pure logic + Discord session class). Single conditional branch in `cogs/match.py::on_queue_full` that routes pro queues through the draft, then builds a `MatchPlan` from the draft result instead of calling `plan_match`. All destructive actions (revoke queue role, delete_active_queue) are deferred until after the draft completes, making cancel a trivial no-op revert.

**Tech Stack:** Python 3.11+, discord.py v2 (`discord.ui.Select`, `discord.ui.Button`, `discord.ui.View`), asyncio, frozen dataclasses, pytest with AsyncMock fakes (reusing patterns from `tests/test_match_cog.py`).

**Spec:** `docs/superpowers/specs/2026-05-15-pro-captain-pick-design.md`

---

## File Map

| File | Action | Purpose |
|---|---|---|
| `services/captain_draft.py` | **create** | Pure logic (`pick_captains`, `DraftState`, `PICK_SEQUENCE`) + Discord orchestration (`CaptainDraftSession`) + exceptions (`DraftCancelledError`) |
| `services/match_service.py` | modify | Add `build_plan_from_draft` factory that turns a `DraftResult` into a `MatchPlan` (so `on_queue_full` resumes the existing flow unchanged) |
| `cogs/match.py` | modify | (a) Import `captain_draft`. (b) New helper `_move_players_to_waiting_match`. (c) In `on_queue_full`: conditional branch on `queue_type == "pro"`. (d) Defer queue-role revoke + `delete_active_queue` until *after* draft completes on the pro branch. |
| `tests/test_captain_draft.py` | **create** | Pure-logic tests for `pick_captains`, `DraftState`, `PICK_SEQUENCE`, `build_plan_from_draft` |
| `tests/test_captain_draft_session.py` | **create** | Session integration tests with fakes (happy path, cancel, double-click, non-cap rejection, non-admin rejection) |
| `tests/test_match_cog.py` | extend | 5 new tests covering pro branch routing + open/gc non-regression + pro fallback paths |

**Reuse:** existing helpers `_fake_member`, `_fake_category(with_prep, with_waiting)`, `_fake_guild`, `_fake_channel`, `_fake_interaction` in `tests/test_match_cog.py`.

---

## Task 1: `pick_captains` — pure function

**Files:**
- Create: `services/captain_draft.py`
- Test: `tests/test_captain_draft.py`

- [ ] **Step 1: Create the test file with imports and the first failing test**

Write `tests/test_captain_draft.py`:

```python
"""Tests purs pour le draft capitaine de la Pro Queue."""
from __future__ import annotations

import random

import pytest

from services.captain_draft import pick_captains
from services.team_balancer import Player


def _p(uid: int, elo: int) -> Player:
    return Player(id=uid, name=f"P{uid}", elo=elo)


def test_pick_captains_top_two_elo():
    """Les 2 ELO les plus hauts sont designes capitaines."""
    players = [
        _p(1, 1000), _p(2, 1100), _p(3, 1200), _p(4, 1300), _p(5, 1400),
        _p(6, 1500), _p(7, 1600), _p(8, 1700), _p(9, 1800), _p(10, 1900),
    ]
    rng = random.Random(42)
    cap_a, cap_b = pick_captains(players, rng=rng)
    assert cap_a.id == 10  # ELO 1900
    assert cap_b.id == 9   # ELO 1800
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest tests/test_captain_draft.py::test_pick_captains_top_two_elo -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'services.captain_draft'`

- [ ] **Step 3: Create the module skeleton + minimal `pick_captains`**

Write `services/captain_draft.py`:

```python
"""
Pro Queue Captain Draft Service.

Module isole pour la pro queue uniquement. Contient :
  - pick_captains : selection des 2 capitaines (top 2 ELO, tie = RNG)
  - DraftState    : etat immutable du draft
  - CaptainDraftSession : orchestration Discord (UI + machine d'etat)

Open et GC queues n'utilisent PAS ce module : elles continuent
de passer par plan_match (auto-balance).
"""
from __future__ import annotations

import random
from typing import Sequence

from services.team_balancer import Player


def pick_captains(
    players: Sequence[Player],
    *,
    rng: random.Random,
) -> tuple[Player, Player]:
    """Designe 2 capitaines : top 2 ELO, tie-break aleatoire.

    Args:
        players: liste de Player (typiquement 10).
        rng: random.Random seede (pour reproductibilite des tests).

    Returns:
        (cap_a, cap_b) ou cap_a a l'ELO le plus haut (apres tie-break).
    """
    if len(players) < 2:
        raise ValueError(f"Il faut au moins 2 joueurs, recu {len(players)}")

    # Tri par ELO decroissant, RNG sur les egalites.
    # On groupe par ELO et on melange chaque groupe avec rng.
    by_elo: dict[int, list[Player]] = {}
    for p in players:
        by_elo.setdefault(p.elo, []).append(p)
    ordered: list[Player] = []
    for elo in sorted(by_elo.keys(), reverse=True):
        bucket = list(by_elo[elo])
        rng.shuffle(bucket)
        ordered.extend(bucket)
    return ordered[0], ordered[1]
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `pytest tests/test_captain_draft.py::test_pick_captains_top_two_elo -v`
Expected: PASS

- [ ] **Step 5: Add tie-break tests**

Append to `tests/test_captain_draft.py`:

```python
def test_pick_captains_tiebreak_random_seeded():
    """Avec 4 joueurs a ELO max identique, la seed RNG determine les capitaines."""
    players = [_p(i, 1500) for i in range(1, 5)]  # 4 joueurs tous a 1500
    cap_a_seed1, cap_b_seed1 = pick_captains(players, rng=random.Random(1))
    cap_a_seed2, cap_b_seed2 = pick_captains(players, rng=random.Random(2))
    # Reproductible : meme seed -> meme resultat
    cap_a_again, cap_b_again = pick_captains(players, rng=random.Random(1))
    assert (cap_a_seed1.id, cap_b_seed1.id) == (cap_a_again.id, cap_b_again.id)
    # Deux seeds donnent generalement des resultats differents (sur 4!=24 perms)
    assert (cap_a_seed1.id, cap_b_seed1.id) != (cap_a_seed2.id, cap_b_seed2.id)


def test_pick_captains_tiebreak_position_2():
    """1 joueur clairement top, 3 a egalite pour position 2 -> RNG entre les 3."""
    players = [_p(1, 2000)] + [_p(i, 1500) for i in range(2, 11)]
    cap_a, cap_b = pick_captains(players, rng=random.Random(7))
    assert cap_a.id == 1            # le top ELO unique
    assert cap_b.id in {2, 3, 4, 5, 6, 7, 8, 9, 10}  # un des tied


def test_pick_captains_raises_if_too_few_players():
    with pytest.raises(ValueError, match="au moins 2 joueurs"):
        pick_captains([_p(1, 1500)], rng=random.Random(0))
```

- [ ] **Step 6: Run the new tests**

Run: `pytest tests/test_captain_draft.py -v`
Expected: PASS (4 tests)

- [ ] **Step 7: Commit**

```bash
git add services/captain_draft.py tests/test_captain_draft.py
git commit -m "feat(captain-draft): add pick_captains pure function with RNG tie-break"
```

---

## Task 2: `DraftState` + `PICK_SEQUENCE` + `apply_pick`

**Files:**
- Modify: `services/captain_draft.py`
- Test: `tests/test_captain_draft.py`

- [ ] **Step 1: Write failing test for the sequence + initial state**

Append to `tests/test_captain_draft.py`:

```python
from services.captain_draft import DraftState, PICK_SEQUENCE


def test_pick_sequence_is_snake_ABBAABBA():
    assert PICK_SEQUENCE == ("A", "B", "B", "A", "A", "B", "B", "A")


def test_draft_state_initial():
    cap_a = _p(1, 1900)
    cap_b = _p(2, 1800)
    pool = tuple(_p(i, 1500 - i) for i in range(3, 11))
    state = DraftState.initial(cap_a=cap_a, cap_b=cap_b, pool=pool)
    assert state.team_a == (cap_a,)
    assert state.team_b == (cap_b,)
    assert state.pool == pool
    assert state.turn_index == 0
    assert state.status == "picking"
    assert state.current_captain is cap_a  # PICK_SEQUENCE[0] == "A"
    assert not state.is_complete
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_captain_draft.py::test_pick_sequence_is_snake_ABBAABBA tests/test_captain_draft.py::test_draft_state_initial -v`
Expected: FAIL with `ImportError: cannot import name 'DraftState'`

- [ ] **Step 3: Add `DraftState` + `PICK_SEQUENCE` to `services/captain_draft.py`**

Append to `services/captain_draft.py`:

```python
from dataclasses import dataclass, field, replace
from typing import Literal

# Snake order ABBAABBA. Sur 8 picks, capA pick aux indices 0, 3, 4, 7
# et capB pick aux indices 1, 2, 5, 6. Avec les 2 captains deja en team,
# chaque equipe finit avec 5 joueurs (1 cap + 4 picks).
PICK_SEQUENCE: tuple[Literal["A", "B"], ...] = (
    "A", "B", "B", "A", "A", "B", "B", "A",
)

DraftStatus = Literal["picking", "complete", "cancelled"]


@dataclass(frozen=True)
class DraftState:
    cap_a:       Player
    cap_b:       Player
    team_a:      tuple[Player, ...]
    team_b:      tuple[Player, ...]
    pool:        tuple[Player, ...]
    turn_index:  int
    status:      DraftStatus

    @classmethod
    def initial(
        cls,
        *,
        cap_a: Player,
        cap_b: Player,
        pool: tuple[Player, ...],
    ) -> "DraftState":
        return cls(
            cap_a=cap_a,
            cap_b=cap_b,
            team_a=(cap_a,),
            team_b=(cap_b,),
            pool=tuple(pool),
            turn_index=0,
            status="picking",
        )

    @property
    def is_complete(self) -> bool:
        return self.turn_index >= len(PICK_SEQUENCE)

    @property
    def current_captain(self) -> Player:
        if self.is_complete:
            raise RuntimeError("Draft complet : pas de capitaine courant.")
        side = PICK_SEQUENCE[self.turn_index]
        return self.cap_a if side == "A" else self.cap_b

    def apply_pick(self, player: Player) -> "DraftState":
        """Retourne un nouvel etat avec `player` ajoute a l'equipe du cap courant.

        Raises:
            ValueError si player n'est pas dans pool.
            RuntimeError si draft deja complet ou cancelled.
        """
        if self.status != "picking":
            raise RuntimeError(f"Draft status={self.status}, impossible de pick.")
        if player not in self.pool:
            raise ValueError(f"Joueur {player.id} pas dans le pool.")
        side = PICK_SEQUENCE[self.turn_index]
        new_pool = tuple(p for p in self.pool if p.id != player.id)
        if side == "A":
            new_team_a = self.team_a + (player,)
            new_team_b = self.team_b
        else:
            new_team_a = self.team_a
            new_team_b = self.team_b + (player,)
        new_turn = self.turn_index + 1
        new_status: DraftStatus = "complete" if new_turn >= len(PICK_SEQUENCE) else "picking"
        return replace(
            self,
            team_a=new_team_a,
            team_b=new_team_b,
            pool=new_pool,
            turn_index=new_turn,
            status=new_status,
        )
```

- [ ] **Step 4: Run the new tests**

Run: `pytest tests/test_captain_draft.py::test_pick_sequence_is_snake_ABBAABBA tests/test_captain_draft.py::test_draft_state_initial -v`
Expected: PASS

- [ ] **Step 5: Add immutability + complete-after-8 tests**

Append to `tests/test_captain_draft.py`:

```python
def _make_state_with_8_pool() -> tuple[DraftState, list[Player]]:
    cap_a = _p(1, 1900)
    cap_b = _p(2, 1800)
    pool = [_p(i, 1500 - i) for i in range(3, 11)]  # 8 joueurs
    return DraftState.initial(cap_a=cap_a, cap_b=cap_b, pool=tuple(pool)), pool


def test_draft_state_apply_pick_is_immutable():
    state, pool = _make_state_with_8_pool()
    state2 = state.apply_pick(pool[0])
    # original inchange
    assert state.team_a == (state.cap_a,)
    assert state.pool == tuple(pool)
    assert state.turn_index == 0
    # nouvel etat decale
    assert state2.team_a == (state.cap_a, pool[0])
    assert pool[0] not in state2.pool
    assert state2.turn_index == 1


def test_draft_state_apply_pick_follows_ABBAABBA():
    state, pool = _make_state_with_8_pool()
    expected_sides = ["A", "B", "B", "A", "A", "B", "B", "A"]
    for i, side in enumerate(expected_sides):
        assert state.current_captain.id == (state.cap_a.id if side == "A" else state.cap_b.id), (
            f"turn {i}: expected side {side}"
        )
        state = state.apply_pick(pool[i])
    assert state.is_complete
    assert state.status == "complete"


def test_draft_state_complete_has_5_each_team():
    state, pool = _make_state_with_8_pool()
    for p in pool:
        state = state.apply_pick(p)
    assert len(state.team_a) == 5
    assert len(state.team_b) == 5
    assert state.pool == ()


def test_draft_state_apply_pick_rejects_player_not_in_pool():
    state, _ = _make_state_with_8_pool()
    stranger = _p(99, 1500)
    with pytest.raises(ValueError, match="pas dans le pool"):
        state.apply_pick(stranger)


def test_draft_state_apply_pick_rejects_when_complete():
    state, pool = _make_state_with_8_pool()
    for p in pool:
        state = state.apply_pick(p)
    extra = _p(99, 1500)
    with pytest.raises(RuntimeError, match="status=complete"):
        state.apply_pick(extra)
```

- [ ] **Step 6: Run the full test file**

Run: `pytest tests/test_captain_draft.py -v`
Expected: PASS (10 tests)

- [ ] **Step 7: Commit**

```bash
git add services/captain_draft.py tests/test_captain_draft.py
git commit -m "feat(captain-draft): add DraftState + PICK_SEQUENCE with snake ABBAABBA"
```

---

## Task 3: `DraftResult` + `build_plan_from_draft`

**Files:**
- Modify: `services/captain_draft.py` (add `DraftResult`)
- Modify: `services/match_service.py` (add `build_plan_from_draft`)
- Test: `tests/test_captain_draft.py`

- [ ] **Step 1: Write failing test**

Append to `tests/test_captain_draft.py`:

```python
from services.captain_draft import DraftResult
from services.match_service import build_plan_from_draft


def test_draft_result_from_state_when_complete():
    state, pool = _make_state_with_8_pool()
    for p in pool:
        state = state.apply_pick(p)
    result = DraftResult.from_state(state)
    assert result.team_a[0] is state.cap_a
    assert result.team_b[0] is state.cap_b
    assert len(result.team_a) == 5 and len(result.team_b) == 5


def test_draft_result_rejects_incomplete_state():
    state, _ = _make_state_with_8_pool()
    with pytest.raises(ValueError, match="non termine"):
        DraftResult.from_state(state)


def test_build_plan_from_draft_uses_capA_as_leader():
    state, pool = _make_state_with_8_pool()
    for p in pool:
        state = state.apply_pick(p)
    result = DraftResult.from_state(state)
    plan = build_plan_from_draft(
        result, free_category="Match #1", rng=random.Random(42),
    )
    assert plan.category_name == "Match #1"
    assert plan.lobby_leader is state.cap_a
    assert plan.teams.team_a == result.team_a
    assert plan.teams.team_b == result.team_b
    # map_name est choisi par rng parmi elo_calc.MAPS, non vide
    assert plan.map_name
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_captain_draft.py::test_draft_result_from_state_when_complete -v`
Expected: FAIL with `ImportError: cannot import name 'DraftResult'`

- [ ] **Step 3: Add `DraftResult` to `services/captain_draft.py`**

Append to `services/captain_draft.py`:

```python
@dataclass(frozen=True)
class DraftResult:
    cap_a:  Player
    cap_b:  Player
    team_a: tuple[Player, ...]   # 5 joueurs incl. cap_a
    team_b: tuple[Player, ...]   # 5 joueurs incl. cap_b

    @classmethod
    def from_state(cls, state: DraftState) -> "DraftResult":
        if state.status != "complete":
            raise ValueError(f"Draft non termine (status={state.status}).")
        return cls(
            cap_a=state.cap_a,
            cap_b=state.cap_b,
            team_a=state.team_a,
            team_b=state.team_b,
        )
```

- [ ] **Step 4: Add `build_plan_from_draft` to `services/match_service.py`**

Open `services/match_service.py`. Find the `plan_match` function (currently ~line 72-99). Add immediately after it:

```python
def build_plan_from_draft(
    result,           # services.captain_draft.DraftResult (typed via duck typing pour eviter import cycle)
    *,
    free_category: str,
    rng:           random.Random,
) -> MatchPlan:
    """Construit un MatchPlan a partir d'un DraftResult capitaine.

    Utilise sur la branche Pro Queue ou les equipes viennent du draft
    et NON de balance_teams. Compute elo_diff/peak_diff pour info
    (pas de fonction objectif puisque le draft n'optimise pas).
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
    return MatchPlan(
        teams=teams,
        map_name=rng.choice(elo_calc.MAPS),
        lobby_leader=result.cap_a,
        category_name=free_category,
    )
```

If `elo_calc` is not imported at module-level in `match_service.py`, verify it is. If not, add `from services import elo_calc` at top. (Check existing imports first with `grep -n "elo_calc" services/match_service.py` — `plan_match` already uses `elo_calc.MAPS` so the import exists.)

- [ ] **Step 5: Run the tests**

Run: `pytest tests/test_captain_draft.py -v`
Expected: PASS (13 tests)

- [ ] **Step 6: Commit**

```bash
git add services/captain_draft.py services/match_service.py tests/test_captain_draft.py
git commit -m "feat(captain-draft): add DraftResult + build_plan_from_draft factory"
```

---

## Task 4: `CaptainDraftSession` — Discord orchestration (happy path)

**Files:**
- Modify: `services/captain_draft.py` (add `CaptainDraftSession` + `DraftCancelledError`)
- Create: `tests/test_captain_draft_session.py`

- [ ] **Step 1: Create the session test file with the happy-path test**

Write `tests/test_captain_draft_session.py`:

```python
"""Tests d'integration pour CaptainDraftSession (UI Discord avec fakes)."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from services.captain_draft import (
    CaptainDraftSession,
    DraftCancelledError,
    DraftState,
)
from services.team_balancer import Player


ADMIN_ROLES = ("Admin", "Match Staff")


def _p(uid: int, elo: int) -> Player:
    return Player(id=uid, name=f"P{uid}", elo=elo)


def _fake_role(name: str):
    r = MagicMock()
    r.name = name
    return r


def _fake_user(user_id: int, role_names: tuple[str, ...] = ()):
    u = MagicMock()
    u.id = user_id
    u.mention = f"<@{user_id}>"
    u.roles = [_fake_role(n) for n in role_names]
    return u


def _fake_interaction(user, custom_id: str, values: list[str] | None = None):
    inter = MagicMock()
    inter.user = user
    inter.data = {"custom_id": custom_id}
    if values is not None:
        inter.data["values"] = values
    inter.response = MagicMock()
    inter.response.defer = AsyncMock()
    inter.response.send_message = AsyncMock()
    inter.message = MagicMock()
    inter.message.edit = AsyncMock()
    return inter


def _fake_prep_channel():
    ch = MagicMock()
    msg = MagicMock()
    msg.edit = AsyncMock()
    msg.id = 12345
    ch.send = AsyncMock(return_value=msg)
    return ch, msg


@pytest.mark.asyncio
async def test_session_happy_path_8_picks_complete():
    """Simule 8 picks consecutifs : session.run() termine avec un DraftResult."""
    cap_a = _p(1, 1900)
    cap_b = _p(2, 1800)
    pool = tuple(_p(i, 1500 - i) for i in range(3, 11))
    prep_channel, draft_msg = _fake_prep_channel()

    session = CaptainDraftSession(
        prep_channel=prep_channel,
        cap_a=cap_a,
        cap_b=cap_b,
        pool=pool,
        admin_role_names=ADMIN_ROLES,
    )

    # On lance session.run() en background, et on simule les picks via les callbacks.
    run_task = asyncio.create_task(session.run())

    # Attend que le message soit poste (initialisation)
    for _ in range(50):
        if session.message is not None:
            break
        await asyncio.sleep(0.01)
    assert session.message is not None, "Le message draft doit etre poste au demarrage"

    # Ordre snake : A, B, B, A, A, B, B, A
    pick_users = [cap_a, cap_b, cap_b, cap_a, cap_a, cap_b, cap_b, cap_a]
    for i, picker in enumerate(pick_users):
        inter = _fake_interaction(picker, "pro_draft_pick", values=[str(pool[i].id)])
        await session._on_pick(inter)

    result = await asyncio.wait_for(run_task, timeout=1.0)
    assert len(result.team_a) == 5
    assert len(result.team_b) == 5
    assert result.cap_a is cap_a
    assert result.cap_b is cap_b
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest tests/test_captain_draft_session.py::test_session_happy_path_8_picks_complete -v`
Expected: FAIL with `ImportError: cannot import name 'CaptainDraftSession'`

- [ ] **Step 3: Add `DraftCancelledError` + `CaptainDraftSession` (happy path only first)**

Append to `services/captain_draft.py`:

```python
import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)


class DraftCancelledError(Exception):
    """Leve quand un admin annule le draft via le bouton."""
    def __init__(self, reason: str, actor: Any | None = None):
        super().__init__(reason)
        self.reason = reason
        self.actor = actor


def _has_any_role(user: Any, role_names: tuple[str, ...]) -> bool:
    return any(r.name in role_names for r in getattr(user, "roles", []))


def _build_player_lines(players: tuple[Player, ...]) -> str:
    if not players:
        return "_(vide)_"
    return "\n".join(f"• <@{p.id}> ({p.elo})" for p in players)


def _build_pool_lines(pool: tuple[Player, ...]) -> str:
    if not pool:
        return "_(vide)_"
    ordered = sorted(pool, key=lambda p: p.elo, reverse=True)
    return "\n".join(f"• <@{p.id}> ({p.elo})" for p in ordered)


def _build_sequence_marker(turn_index: int) -> str:
    """Affiche la sequence ABBAABBA avec un curseur sur le pick courant."""
    parts = []
    for i, side in enumerate(PICK_SEQUENCE):
        if i == turn_index:
            parts.append(f"·{side}·")
        else:
            parts.append(side)
    return " ".join(parts)


class CaptainDraftSession:
    """Orchestration du draft : poste le message, gere les interactions,
    retourne un DraftResult quand les 8 picks sont termines (ou leve
    DraftCancelledError si annule par un admin).
    """

    def __init__(
        self,
        *,
        prep_channel: Any,
        cap_a: Player,
        cap_b: Player,
        pool: tuple[Player, ...],
        admin_role_names: tuple[str, ...],
    ):
        self.prep_channel = prep_channel
        self.state = DraftState.initial(cap_a=cap_a, cap_b=cap_b, pool=pool)
        self.admin_role_names = admin_role_names
        self.message: Any | None = None
        self._lock = asyncio.Lock()
        self._done: asyncio.Future[DraftResult] = asyncio.get_event_loop().create_future()

    async def run(self) -> DraftResult:
        """Bloque jusqu'a la fin du draft (complete OU cancelled).

        Returns: DraftResult si complete.
        Raises: DraftCancelledError si annule.
        """
        # discord.py imports locaux : on n'alourdit pas l'import du module
        # cote tests (qui n'ont pas discord installe en chemin facile).
        import discord  # noqa: F401

        embed = self._build_embed()
        view = self._build_view()
        content = (
            f"<@{self.state.cap_a.id}> <@{self.state.cap_b.id}> "
            f"— vous etes capitaines, a vous de drafter !"
        )
        self.message = await self.prep_channel.send(content=content, embed=embed, view=view)
        logger.info(
            "[draft] init cap_a=%s cap_b=%s pool_size=%d",
            self.state.cap_a.id, self.state.cap_b.id, len(self.state.pool),
        )
        return await self._done

    def _build_embed(self) -> Any:
        import discord
        e = discord.Embed(
            title="🎯 [PRO] Captain Draft",
            color=discord.Color.gold(),
        )
        e.add_field(
            name=f"🅰️ Team 1 — Cap. <@{self.state.cap_a.id}>",
            value=_build_player_lines(self.state.team_a),
            inline=False,
        )
        e.add_field(
            name=f"🅱️ Team 2 — Cap. <@{self.state.cap_b.id}>",
            value=_build_player_lines(self.state.team_b),
            inline=False,
        )
        e.add_field(
            name="🎲 Pool disponible (tri ELO ↓)",
            value=_build_pool_lines(self.state.pool),
            inline=False,
        )
        if self.state.is_complete:
            e.set_footer(text="✅ Draft termine")
        else:
            cur = self.state.current_captain
            seq = _build_sequence_marker(self.state.turn_index)
            e.add_field(
                name=f"⏳ Au tour de <@{cur.id}> — pick #{self.state.turn_index + 1}",
                value=f"Sequence : {seq}",
                inline=False,
            )
        return e

    def _build_view(self) -> Any:
        import discord

        session = self  # capture pour les callbacks

        class _View(discord.ui.View):
            def __init__(self) -> None:
                super().__init__(timeout=None)

            async def interaction_check(self, interaction: discord.Interaction) -> bool:
                return await session._interaction_check(interaction)

        view = _View()
        if not self.state.is_complete and self.state.status == "picking":
            options = [
                discord.SelectOption(
                    label=p.name[:100],
                    description=f"{p.elo} ELO",
                    value=str(p.id),
                )
                for p in sorted(self.state.pool, key=lambda x: x.elo, reverse=True)
            ]
            select = discord.ui.Select(
                custom_id="pro_draft_pick",
                placeholder="Choisis ton joueur",
                min_values=1, max_values=1,
                options=options,
            )

            async def _select_cb(interaction: discord.Interaction) -> None:
                await session._on_pick(interaction)

            select.callback = _select_cb
            view.add_item(select)

        cancel_btn = discord.ui.Button(
            custom_id="pro_draft_cancel",
            style=discord.ButtonStyle.danger,
            label="❌ Annuler le draft",
            disabled=self.state.status != "picking",
        )

        async def _cancel_cb(interaction: discord.Interaction) -> None:
            await session._on_cancel(interaction)

        cancel_btn.callback = _cancel_cb
        view.add_item(cancel_btn)
        return view

    async def _interaction_check(self, interaction: Any) -> bool:
        cid = interaction.data.get("custom_id", "")
        if cid == "pro_draft_pick":
            if interaction.user.id != self.state.current_captain.id:
                await interaction.response.send_message(
                    "⏳ Ce n'est pas ton tour.", ephemeral=True,
                )
                return False
        elif cid == "pro_draft_cancel":
            if not _has_any_role(interaction.user, self.admin_role_names):
                await interaction.response.send_message(
                    "❌ Reserve aux admins.", ephemeral=True,
                )
                return False
        return True

    async def _on_pick(self, interaction: Any) -> None:
        async with self._lock:
            if self.state.status != "picking":
                return
            picked_id_str = interaction.data["values"][0]
            picked_id = int(picked_id_str)
            picked = next(
                (p for p in self.state.pool if p.id == picked_id),
                None,
            )
            if picked is None:
                await interaction.response.send_message(
                    "❌ Joueur deja drafte.", ephemeral=True,
                )
                return
            self.state = self.state.apply_pick(picked)
            logger.info(
                "[draft] pick turn=%d by=%s player=%s",
                self.state.turn_index - 1, interaction.user.id, picked_id,
            )
            embed = self._build_embed()
            view = self._build_view()
            await self.message.edit(embed=embed, view=view)
            if self.state.is_complete:
                self._done.set_result(DraftResult.from_state(self.state))
            # defer pour acquitter l'interaction sans envoyer de message
            if not interaction.response.is_done() if hasattr(interaction.response, "is_done") else True:
                try:
                    await interaction.response.defer()
                except Exception:
                    pass

    async def _on_cancel(self, interaction: Any) -> None:
        async with self._lock:
            if self.state.status != "picking":
                return
            self.state = replace(self.state, status="cancelled")
            actor = interaction.user
            embed = self._build_embed()
            embed.title = "❌ Draft annule"
            embed.description = f"Annule par <@{actor.id}>"
            view = self._build_view()
            await self.message.edit(embed=embed, view=view)
            logger.info("[draft] cancelled by=%s", actor.id)
            try:
                await interaction.response.defer()
            except Exception:
                pass
            self._done.set_exception(DraftCancelledError("admin", actor))
```

- [ ] **Step 4: Install pytest-asyncio if missing**

Verify `pytest-asyncio` is in `pyproject.toml` or `requirements*.txt`. Run:
```bash
pytest tests/test_captain_draft_session.py --collect-only 2>&1 | head -30
```
If missing, install: `pip install pytest-asyncio` and add to dev deps.

If `pytest.ini`/`pyproject.toml` does not set `asyncio_mode = "auto"`, the test file needs `pytestmark = pytest.mark.asyncio` at top OR each async test needs `@pytest.mark.asyncio` (already on it).

- [ ] **Step 5: Run the happy-path test**

Run: `pytest tests/test_captain_draft_session.py::test_session_happy_path_8_picks_complete -v`
Expected: PASS

If it fails because `discord` import fails (the package is not installed in test env): check `tests/conftest.py` — it likely has a `sys.modules["discord"] = ...` shim. If not, add a fixture or use `pytest.importorskip("discord")`.

- [ ] **Step 6: Commit**

```bash
git add services/captain_draft.py tests/test_captain_draft_session.py
git commit -m "feat(captain-draft): add CaptainDraftSession orchestration (happy path)"
```

---

## Task 5: Session — cancel, non-cap rejection, non-admin rejection

**Files:**
- Test: `tests/test_captain_draft_session.py`

(No new impl — the code from Task 4 already implements these paths. We add tests to validate them.)

- [ ] **Step 1: Add cancel test**

Append to `tests/test_captain_draft_session.py`:

```python
@pytest.mark.asyncio
async def test_session_admin_cancel_raises():
    """Un admin clique Cancel -> run() leve DraftCancelledError."""
    cap_a = _p(1, 1900)
    cap_b = _p(2, 1800)
    pool = tuple(_p(i, 1500 - i) for i in range(3, 11))
    prep_channel, _ = _fake_prep_channel()

    session = CaptainDraftSession(
        prep_channel=prep_channel, cap_a=cap_a, cap_b=cap_b,
        pool=pool, admin_role_names=ADMIN_ROLES,
    )
    run_task = asyncio.create_task(session.run())
    for _ in range(50):
        if session.message is not None:
            break
        await asyncio.sleep(0.01)

    admin = _fake_user(99, role_names=("Admin",))
    inter = _fake_interaction(admin, "pro_draft_cancel")
    await session._on_cancel(inter)

    with pytest.raises(DraftCancelledError) as exc_info:
        await asyncio.wait_for(run_task, timeout=1.0)
    assert exc_info.value.reason == "admin"


@pytest.mark.asyncio
async def test_session_non_admin_cancel_rejected_by_interaction_check():
    """Un non-admin qui clique Cancel : interaction_check renvoie False (ephemeral)."""
    cap_a = _p(1, 1900); cap_b = _p(2, 1800)
    pool = tuple(_p(i, 1500 - i) for i in range(3, 11))
    prep_channel, _ = _fake_prep_channel()
    session = CaptainDraftSession(
        prep_channel=prep_channel, cap_a=cap_a, cap_b=cap_b,
        pool=pool, admin_role_names=ADMIN_ROLES,
    )
    asyncio.create_task(session.run())
    for _ in range(50):
        if session.message is not None:
            break
        await asyncio.sleep(0.01)

    rando = _fake_user(500, role_names=())  # pas de role admin
    inter = _fake_interaction(rando, "pro_draft_cancel")
    ok = await session._interaction_check(inter)
    assert ok is False
    inter.response.send_message.assert_awaited_once()
    args, kwargs = inter.response.send_message.call_args
    assert kwargs.get("ephemeral") is True
    # Le draft est toujours en picking
    assert session.state.status == "picking"


@pytest.mark.asyncio
async def test_session_pick_by_wrong_captain_rejected_by_interaction_check():
    """Cap B clique pendant tour de Cap A : interaction_check renvoie False."""
    cap_a = _p(1, 1900); cap_b = _p(2, 1800)
    pool = tuple(_p(i, 1500 - i) for i in range(3, 11))
    prep_channel, _ = _fake_prep_channel()
    session = CaptainDraftSession(
        prep_channel=prep_channel, cap_a=cap_a, cap_b=cap_b,
        pool=pool, admin_role_names=ADMIN_ROLES,
    )
    asyncio.create_task(session.run())
    for _ in range(50):
        if session.message is not None:
            break
        await asyncio.sleep(0.01)

    # Tour 0 == cap_a, cap_b ne doit pas pouvoir pick
    inter = _fake_interaction(cap_b, "pro_draft_pick", values=[str(pool[0].id)])
    ok = await session._interaction_check(inter)
    assert ok is False
    inter.response.send_message.assert_awaited_once()
    assert session.state.turn_index == 0
```

- [ ] **Step 2: Run the new tests**

Run: `pytest tests/test_captain_draft_session.py -v`
Expected: PASS (4 tests total)

- [ ] **Step 3: Commit**

```bash
git add tests/test_captain_draft_session.py
git commit -m "test(captain-draft): cover cancel + non-cap + non-admin rejection paths"
```

---

## Task 6: Session — double-click idempotence

**Files:**
- Test: `tests/test_captain_draft_session.py`

(Already covered by the `_on_pick` lock + `picked is None` check. We add an explicit test.)

- [ ] **Step 1: Add the test**

Append to `tests/test_captain_draft_session.py`:

```python
@pytest.mark.asyncio
async def test_session_double_pick_same_player_is_idempotent():
    """2 _on_pick concurrents sur le meme player -> 1 pick applique."""
    cap_a = _p(1, 1900); cap_b = _p(2, 1800)
    pool = tuple(_p(i, 1500 - i) for i in range(3, 11))
    prep_channel, _ = _fake_prep_channel()
    session = CaptainDraftSession(
        prep_channel=prep_channel, cap_a=cap_a, cap_b=cap_b,
        pool=pool, admin_role_names=ADMIN_ROLES,
    )
    asyncio.create_task(session.run())
    for _ in range(50):
        if session.message is not None:
            break
        await asyncio.sleep(0.01)

    target = pool[0]
    inter1 = _fake_interaction(cap_a, "pro_draft_pick", values=[str(target.id)])
    inter2 = _fake_interaction(cap_a, "pro_draft_pick", values=[str(target.id)])
    # Lance les deux callbacks en parallele
    await asyncio.gather(session._on_pick(inter1), session._on_pick(inter2))
    # Apres : 1 seul pick applique
    assert session.state.turn_index == 1
    assert target in session.state.team_a
    assert target not in session.state.pool
    # Un des 2 a recu un ephemeral "deja drafte"
    n_ephemeral = sum(
        1 for i in (inter1, inter2)
        if i.response.send_message.await_count > 0
    )
    assert n_ephemeral == 1
```

- [ ] **Step 2: Run the test**

Run: `pytest tests/test_captain_draft_session.py::test_session_double_pick_same_player_is_idempotent -v`
Expected: PASS

- [ ] **Step 3: Commit**

```bash
git add tests/test_captain_draft_session.py
git commit -m "test(captain-draft): verify double-pick idempotence under concurrent callbacks"
```

---

## Task 7: `_move_players_to_waiting_match` helper in `match.py`

**Files:**
- Modify: `cogs/match.py`
- Extend: `tests/test_match_cog.py`

The existing `_move_players_to_match_vc` takes a `MatchPlan` and splits to Team 1/Team 2. We need a separate helper that moves all 10 players to `Waiting Match` (single destination) BEFORE the draft starts.

- [ ] **Step 1: Write failing test**

Append to `tests/test_match_cog.py`:

```python
import pytest

@pytest.mark.asyncio
async def test_move_to_waiting_match_routes_all_players(monkeypatch):
    """_move_players_to_waiting_match deplace les 10 joueurs vers Waiting Match."""
    from cogs.match import MatchCog
    import bot as bot_module

    # Voice channel source
    waiting_room = MagicMock()
    waiting_room.name = "Pro Waiting Room"
    waiting_room.id = 7777

    members = [
        _fake_member(i, f"P{i}", voice_channel=waiting_room) for i in range(10)
    ]
    cat = _fake_category("Match #1", with_waiting=True)
    channel = _fake_channel(100)
    guild = _fake_guild(42, members=members, categories=[cat], channel=channel)

    cog = MatchCog(bot_module.bot, bot_module.db, rng=random.Random(42))
    player_ids = [str(m.id) for m in members]

    await cog._move_players_to_waiting_match(guild, cat, player_ids)

    waiting_match_vc = next(c for c in cat.voice_channels if c.name == "Waiting Match")
    moved_to_waiting = sum(
        1 for m in members
        if m.move_to.await_count > 0
        and m.move_to.call_args.args[0].id == waiting_match_vc.id
    )
    assert moved_to_waiting == 10
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest tests/test_match_cog.py::test_move_to_waiting_match_routes_all_players -v`
Expected: FAIL with `AttributeError: 'MatchCog' object has no attribute '_move_players_to_waiting_match'`

- [ ] **Step 3: Add the helper to `cogs/match.py`**

Find `_move_players_to_match_vc` in `cogs/match.py` (around line 618). Add this NEW helper immediately above it:

```python
    async def _move_players_to_waiting_match(
        self,
        guild,
        category,
        player_ids: list[str],
    ) -> None:
        """Deplace tous les `player_ids` vers la VC 'Waiting Match' de `category`.

        Utilise sur la branche Pro Queue AVANT le draft, pour regrouper
        les 10 joueurs dans un meme vocal pendant que les capitaines
        choisissent leurs equipes.

        Guards :
          - skip si guild.get_member retourne None (utilisateur parti)
          - skip si member n'est pas en voice
          - skip si deja a destination
        """
        import discord
        import contextlib

        waiting_match = discord.utils.get(category.voice_channels, name="Waiting Match")
        if waiting_match is None:
            logger.warning(
                "[match] _move_players_to_waiting_match: 'Waiting Match' "
                "introuvable dans %s, no-op", category.name,
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
                        reason="Pro Queue : regroupement avant draft capitaine",
                    )

        await asyncio.gather(
            *[_move_one(uid) for uid in player_ids],
            return_exceptions=True,
        )
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `pytest tests/test_match_cog.py::test_move_to_waiting_match_routes_all_players -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add cogs/match.py tests/test_match_cog.py
git commit -m "feat(match): add _move_players_to_waiting_match helper for pro queue draft"
```

---

## Task 8: Integrate pro branch into `on_queue_full`

**Files:**
- Modify: `cogs/match.py`
- Extend: `tests/test_match_cog.py`

**Approach:** wrap the existing `on_queue_full` body in a conditional. Pro queue runs the draft between `find_free_match_prep` and `plan_match`, and defers `delete_active_queue` + queue role revoke until after the draft completes (so cancel is a no-op revert).

- [ ] **Step 1: Add tests for routing — Pro vs Open vs GC**

Append to `tests/test_match_cog.py`:

```python
@pytest.mark.asyncio
async def test_on_queue_full_open_does_not_invoke_captain_draft(monkeypatch):
    """queue_type='open' -> plan_match utilise, CaptainDraftSession PAS instancie."""
    from cogs.match import MatchCog
    import bot as bot_module
    import services.captain_draft as cd_module

    instantiated = []
    original_init = cd_module.CaptainDraftSession.__init__

    def _spy_init(self, *args, **kwargs):
        instantiated.append(1)
        return original_init(self, *args, **kwargs)

    monkeypatch.setattr(cd_module.CaptainDraftSession, "__init__", _spy_init)

    members = [_fake_member(i, f"P{i}") for i in range(10)]
    channel = _fake_channel(100)
    cat = _fake_category("Match #1")
    guild = _fake_guild(42, members=members, categories=[cat], channel=channel)
    inter = _fake_interaction(guild, user=members[9])

    cog = MatchCog(bot_module.bot, bot_module.db, rng=random.Random(42))
    queue_doc = {"players": [str(m.id) for m in members]}
    # Patch les dependances Mongo pour que on_queue_full atteigne plan_match
    # (la majorite des tests existants font deja ca, reutilise leur setup)
    monkeypatch.setattr(bot_module, "db", MagicMock())
    # On laisse l'erreur arriver apres plan_match si Mongo n'est pas dispo,
    # ce qui suffit pour valider que CaptainDraftSession n'a PAS ete cree.
    try:
        await cog.on_queue_full(inter, queue_doc, queue_type="open")
    except Exception:
        pass
    assert instantiated == [], "CaptainDraftSession ne doit pas etre instancie en open queue"


@pytest.mark.asyncio
async def test_on_queue_full_pro_invokes_captain_draft(monkeypatch):
    """queue_type='pro' -> CaptainDraftSession.run() est appele."""
    from cogs.match import MatchCog
    import bot as bot_module
    import services.captain_draft as cd_module

    run_calls = []

    async def _fake_run(self):
        run_calls.append(self)
        # On simule un draft complet : retourne un DraftResult coherent
        from services.captain_draft import DraftResult, DraftState
        state = self.state
        for p in list(state.pool):
            state = state.apply_pick(p)
        return DraftResult.from_state(state)

    monkeypatch.setattr(cd_module.CaptainDraftSession, "run", _fake_run)

    members = [_fake_member(i, f"P{i}") for i in range(10)]
    channel = _fake_channel(100)
    cat = _fake_category("Match #1", with_waiting=True)
    guild = _fake_guild(42, members=members, categories=[cat], channel=channel)
    inter = _fake_interaction(guild, user=members[9])

    cog = MatchCog(bot_module.bot, bot_module.db, rng=random.Random(42))
    queue_doc = {"players": [str(m.id) for m in members]}
    try:
        await cog.on_queue_full(inter, queue_doc, queue_type="pro")
    except Exception:
        pass
    assert len(run_calls) == 1, "CaptainDraftSession.run() doit etre appele exactement 1 fois"


@pytest.mark.asyncio
async def test_on_queue_full_pro_cancelled_does_not_delete_queue(monkeypatch):
    """Si le draft est annule, delete_active_queue n'est PAS appele."""
    from cogs.match import MatchCog
    import bot as bot_module
    import services.captain_draft as cd_module
    from services.captain_draft import DraftCancelledError

    async def _fake_run_cancel(self):
        raise DraftCancelledError("admin", actor=None)

    monkeypatch.setattr(cd_module.CaptainDraftSession, "run", _fake_run_cancel)

    delete_calls = []
    from services import repository
    monkeypatch.setattr(
        repository, "delete_active_queue",
        lambda *a, **kw: delete_calls.append((a, kw)),
    )

    members = [_fake_member(i, f"P{i}") for i in range(10)]
    channel = _fake_channel(100)
    cat = _fake_category("Match #1", with_waiting=True)
    guild = _fake_guild(42, members=members, categories=[cat], channel=channel)
    inter = _fake_interaction(guild, user=members[9])

    cog = MatchCog(bot_module.bot, bot_module.db, rng=random.Random(42))
    queue_doc = {"players": [str(m.id) for m in members]}
    await cog.on_queue_full(inter, queue_doc, queue_type="pro")
    assert delete_calls == [], "delete_active_queue ne doit pas etre appele apres cancel"
```

- [ ] **Step 2: Run tests to verify they fail (Pro routing missing)**

Run: `pytest tests/test_match_cog.py::test_on_queue_full_pro_invokes_captain_draft -v`
Expected: FAIL (CaptainDraftSession not invoked because the pro branch doesn't exist yet)

- [ ] **Step 3: Modify `cogs/match.py::on_queue_full` to add the pro branch**

Open `cogs/match.py`. Locate the imports (around lines 50-55). Add:

```python
from services.captain_draft import (
    CaptainDraftSession,
    DraftCancelledError,
    pick_captains,
)
from services.match_service import build_plan_from_draft
```

Locate `on_queue_full`. After the `free_cat_name, prep_channel = free` line (currently ~line 468), and BEFORE `plan = plan_match(...)` (currently ~line 470), insert the pro branch. The current code:

```python
        free_cat_name, prep_channel = free

        plan = plan_match(players, free_category=free_cat_name, rng=self.rng)
```

Replace with:

```python
        free_cat_name, prep_channel = free

        # Branche Pro Queue : draft capitaine au lieu d'auto-balance.
        # Les autres queues continuent avec plan_match comme avant.
        if queue_type == "pro":
            # Trouver l'objet category (par nom) pour _move_players_to_waiting_match
            category = discord.utils.get(guild.categories, name=free_cat_name)
            if category is None:
                logger.warning(
                    "[match] Pro queue : category %s introuvable, fallback auto-balance",
                    free_cat_name,
                )
                plan = plan_match(players, free_category=free_cat_name, rng=self.rng)
            else:
                player_ids_for_move = [str(p.id) for p in players]
                await self._move_players_to_waiting_match(
                    guild, category, player_ids_for_move,
                )
                cap_a, cap_b = pick_captains(players, rng=self.rng)
                pool = tuple(p for p in players if p.id not in (cap_a.id, cap_b.id))
                session = CaptainDraftSession(
                    prep_channel=prep_channel,
                    cap_a=cap_a,
                    cap_b=cap_b,
                    pool=pool,
                    admin_role_names=ADMIN_ROLE_NAMES,
                )
                try:
                    result = await session.run()
                except DraftCancelledError as exc:
                    logger.info(
                        "[match] Pro draft annule (reason=%s actor=%s) — "
                        "queue conservee, aucune action destructive",
                        exc.reason, getattr(exc.actor, "id", None),
                    )
                    with contextlib.suppress(discord.HTTPException):
                        await interaction.followup.send(
                            "❌ Draft annule. La queue reste active. "
                            "`/leave` puis `/join` pour reset si besoin.",
                            ephemeral=False,
                        )
                    return None
                plan = build_plan_from_draft(
                    result, free_category=free_cat_name, rng=self.rng,
                )
        else:
            plan = plan_match(players, free_category=free_cat_name, rng=self.rng)
```

Verify `contextlib` is imported at top of `cogs/match.py` (it is — already used by other helpers in the file).

- [ ] **Step 4: Run the pro routing tests**

Run: `pytest tests/test_match_cog.py::test_on_queue_full_pro_invokes_captain_draft tests/test_match_cog.py::test_on_queue_full_pro_cancelled_does_not_delete_queue -v`
Expected: PASS

- [ ] **Step 5: Run the non-regression test for open queue**

Run: `pytest tests/test_match_cog.py::test_on_queue_full_open_does_not_invoke_captain_draft -v`
Expected: PASS

- [ ] **Step 6: Run the full match cog test suite to check no regression**

Run: `pytest tests/test_match_cog.py -v`
Expected: PASS (all existing tests + 3 new ones)

If any existing test fails, look at the diff. The most likely cause is the `if queue_type == "pro"` branch capturing more than intended. The pro branch must NOT execute when `queue_type != "pro"`.

- [ ] **Step 7: Commit**

```bash
git add cogs/match.py tests/test_match_cog.py
git commit -m "feat(match): route pro queue through captain draft in on_queue_full"
```

---

## Task 9: Final verification + spec consistency

**Files:**
- (read-only verification)

- [ ] **Step 1: Run the full test suite**

Run: `pytest -v`
Expected: PASS for all tests (existing + new).

If any failure: investigate. The most common failure modes:
1. `discord` import in `services/captain_draft.py` — must be local (inside methods), not top-level
2. `asyncio.get_event_loop()` deprecation warnings in Python 3.12+ — if blocking, switch to `asyncio.get_running_loop()` (only callable from inside coroutines) by creating the future lazily inside `run()`
3. `pytest-asyncio` config — verify `pyproject.toml` has the asyncio mode or that each async test has `@pytest.mark.asyncio`

- [ ] **Step 2: Run linter**

Run: `ruff check services/captain_draft.py cogs/match.py services/match_service.py tests/test_captain_draft.py tests/test_captain_draft_session.py`
Expected: no errors (or fix them)

- [ ] **Step 3: Run type checker (if used in CI)**

Run: `mypy services/captain_draft.py 2>&1 | head -30`
Expected: no errors or only `Any` warnings on Discord types (acceptable — Discord types are stub-only at runtime)

- [ ] **Step 4: Manual sanity check of the spec → plan mapping**

Reopen the spec (`docs/superpowers/specs/2026-05-15-pro-captain-pick-design.md`). For each section, verify the implementation matches:

| Spec section | Implementation |
|---|---|
| §2.1 Module layout | `services/captain_draft.py` created with `pick_captains`, `DraftState`, `PICK_SEQUENCE`, `DraftResult`, `CaptainDraftSession`, `DraftCancelledError` |
| §2.2 Integration | `on_queue_full` branches on `queue_type == "pro"` |
| §2.3 Selection criteria | Top 2 ELO with RNG tie-break; snake ABBAABBA; no undo; no timeout; admin button cancel |
| §3 State machine | `apply_pick` returns new state; lock in session; cancel raises |
| §4 UI | Embed with Team1/Team2/Pool/Sequence; Select with 8 options; admin cancel button |
| §5 Error handling | Case 1 (no free cat) → existing fallback; Case 3 (double-click) → covered by Task 6 test; Case 10 (admin cancel) → covered by Task 8 test |
| §6 Tests | 3 test files covering pure logic, session, and integration |

- [ ] **Step 5: Commit the final tag-friendly state**

If everything green:
```bash
git log --oneline -10
```
Verify the chain of 8 commits is clean (1 per task). No fixup needed.

- [ ] **Step 6: Smoke test (optional, requires Discord)**

This step is OPTIONAL and only relevant if you have a test Discord server.

In a test environment:
1. Fill the pro queue with 10 test users
2. Verify all 10 are moved to "Waiting Match"
3. Verify the draft message appears in `#match-preparation` with the right captains
4. Have each captain alternately pick (8 picks total)
5. Verify the standard match-found announcement fires after the 8th pick
6. Verify players are moved to Team 1 / Team 2

If unavailable, document this step as "deferred to first prod deploy" in the PR description.

---

## Self-review

### Spec coverage

| Spec requirement | Task |
|---|---|
| Pro queue triggers captain draft | Task 8 (branch in on_queue_full) |
| Open/GC queues unchanged | Task 8 (else branch + Task 8 Step 5 non-regression test) |
| Top 2 ELO captains, RNG tie-break | Task 1 (`pick_captains`) |
| Snake ABBAABBA order | Task 2 (`PICK_SEQUENCE`) |
| Move 10 to Waiting Match BEFORE draft | Task 7 (`_move_players_to_waiting_match`) + Task 8 (called in pro branch) |
| Draft UI: embed + Select + admin cancel | Task 4 (`_build_embed`, `_build_view`) |
| Admin role check on cancel | Task 4 (`_interaction_check`) + Task 5 (test) |
| Non-current-cap rejected on pick | Task 4 (`_interaction_check`) + Task 5 (test) |
| Pick final (no undo) | Task 2 — `apply_pick` is the only mutator, no `undo` method |
| Cancel is no-op revert (no DB doc, no role revoke) | Task 8 — pro branch returns early on `DraftCancelledError` BEFORE the existing destructive code runs |
| Double-click idempotence | Task 6 (test) — relies on `_lock` + `picked is None` check in `_on_pick` |
| No timeout | Task 4 — `discord.ui.View(timeout=None)`, no `asyncio.wait_for` around `_done` |
| Logging at each transition | Task 4 — `logger.info` in `run`, `_on_pick`, `_on_cancel` |
| 85% coverage on captain_draft.py | Tasks 1-6 cover all public functions and critical paths |

**Gap check**: spec §5 Case 5 (pool player leaves Discord during draft) is not actively detected. This is intentional per the design decision "match continues with 9 players if cap picks absent" — no implementation task needed. The existing `_move_players_to_match_vc` already handles `guild.get_member returning None` silently.

### Placeholder scan

Searched for: `TBD`, `TODO`, `implement later`, `appropriate error handling`, `similar to`. None found in the plan.

### Type consistency

- `pick_captains` returns `tuple[Player, Player]` — consistent across Task 1, Task 4, Task 8
- `DraftState.apply_pick` returns `DraftState` — consistent
- `DraftResult.from_state` returns `DraftResult` — consistent
- `build_plan_from_draft(result, *, free_category, rng)` returns `MatchPlan` — signature consistent in Task 3 and Task 8
- `CaptainDraftSession(...).run()` returns `DraftResult`, raises `DraftCancelledError` — consistent in Task 4, 5, 8
- `_move_players_to_waiting_match(self, guild, category, player_ids)` — signature consistent in Task 7 and Task 8

All identifiers used in later tasks are defined in earlier tasks.

---

## Out of scope (deferred to follow-up specs if needed)

- Captain pick for Open / GC queues
- Map veto by captains
- Captain abandonment detection via `on_voice_state_update` / `on_member_remove`
- Persistent draft state across bot restarts
- Captain undo or non-admin reset
- Penalty for AFK captains (timeout)
- Statistics on draft picks
