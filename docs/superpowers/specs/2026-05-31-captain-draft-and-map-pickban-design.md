# Captain Draft (Pro + Semi-Pro) + Map Pick/Ban — Design

**Date:** 2026-05-31
**Status:** Approved, ready for implementation plan
**Scope:** Restore captain draft for Pro queue, extend it to Semi-Pro, add a new map pick/ban phase that runs after the draft.

## Context

Before commit `c9f0dd1` (V5.0, 2026-05-29), the Pro queue used a captain draft flow instead of auto-balance. V5.0 deleted "all pro-queue specificity" — `services/captain_draft.py` (430 lines) and its tests (492 lines) were removed.

User wants the captain draft back, this time for both Pro **and** Semi-Pro queues, plus a new map pick/ban phase between the draft and the match announcement. Open and GC queues keep the current auto-balance + random map.

## Functional flow (Pro / Semi-Pro)

1. Queue fills (10 players).
2. Bot creates the match category and prep channel (existing flow).
3. **NEW**: 10 players are moved to the `Waiting Match` VC of the new category.
4. **NEW**: 2 captains are picked uniformly at random from the 10 players (ELO-independent).
5. **NEW — Draft phase**: pre-existing flow restored. Embed in prep channel, capA picks first (sequence ABABABAB, 8 picks), Select UI restricted to the current captain. Admin can cancel via button.
6. **NEW — Map pick/ban phase**: embed in prep channel. CapA bans first, alternating ABABAB (6 bans on 7 maps). The 1 remaining map is the match map. Admin can cancel via button.
7. Match doc is persisted (teams from draft, map from ban phase).
8. Standard match embed posted in prep channel with VoteView (existing flow).
9. Players moved to Team A / Team B VCs (existing flow).

Open and GC queues skip steps 3–6 entirely. They go directly from step 2 to step 7 with `plan_match(...)` (auto-balance + random map).

## Components

### `services/captain_draft.py` (restored verbatim from `c9f0dd1^`)

No logic changes. Restored from git: 430 lines, already covered by 492 lines of tests.

Public API:
- `pick_captains(players: Sequence[Player], *, rng: random.Random) -> tuple[Player, Player]`
- `PICK_SEQUENCE: tuple[Literal["A","B"], ...] = ("A","B","A","B","A","B","A","B")`
- `@dataclass(frozen=True) class DraftState` with `apply_pick(player) -> DraftState`
- `@dataclass(frozen=True) class DraftResult` with `from_state(state) -> DraftResult`
- `class CaptainDraftSession` — Discord UI orchestration, `.run() -> DraftResult` (raises `DraftCancelledError`)
- `class DraftCancelledError(Exception)`

Admin cancel check: `manage_guild` permission OR membership of a role in `ADMIN_ROLE_NAMES` (same as `cogs/match/_constants.py`).

### `services/map_pick_ban.py` (NEW — mirrors `captain_draft.py`)

```python
BAN_SEQUENCE: tuple[Literal["A","B"], ...] = ("A","B","A","B","A","B")  # 6 bans on 7 maps

@dataclass(frozen=True)
class MapBanState:
    cap_a: Player
    cap_b: Player
    remaining: tuple[str, ...]        # initially all 7 MAPS
    banned: tuple[tuple[Literal["A","B"], str], ...]  # history (side, map)
    turn_index: int
    status: Literal["banning", "complete", "cancelled"]

    @classmethod
    def initial(cls, *, cap_a, cap_b, maps) -> "MapBanState": ...

    @property
    def is_complete(self) -> bool:
        return self.turn_index >= len(BAN_SEQUENCE)

    @property
    def current_captain(self) -> Player:
        side = BAN_SEQUENCE[self.turn_index]
        return self.cap_a if side == "A" else self.cap_b

    def apply_ban(self, map_name: str) -> "MapBanState":
        # validate map_name in self.remaining, status == "banning"
        # returns new state with map removed, turn_index + 1,
        # status = "complete" if turn_index == len(BAN_SEQUENCE)

@dataclass(frozen=True)
class MapBanResult:
    selected_map: str                                          # the 1 remaining map
    ban_history: tuple[tuple[Literal["A","B"], str], ...]

    @classmethod
    def from_state(cls, state: MapBanState) -> "MapBanResult": ...

class MapBanCancelledError(Exception):
    def __init__(self, reason: str, actor: Any | None = None): ...

class MapBanSession:
    def __init__(self, *, prep_channel, cap_a, cap_b, maps, admin_role_names): ...
    async def run(self) -> MapBanResult: ...  # raises MapBanCancelledError
```

UI:
- Embed title: `🗺️ Map Pick & Ban`
- Field 1: `🅰️ Cap. <@capA>` (team A members for reference)
- Field 2: `🅱️ Cap. <@capB>` (team B members for reference)
- Field 3: `❌ Banned maps` — history with the side that banned each
- Field 4: `🗺️ Remaining maps` — the maps still in play
- Field 5 (while `banning`): `⏳ Au tour de <@current_cap> — ban #N` + sequence cursor `·A· B A B A B`
- Footer when `complete`: `✅ Map sélectionnée : <map>`

Discord components:
- `discord.ui.Select` with `custom_id="map_ban_pick"`, options = current `remaining` maps, restricted via `interaction_check` to `current_captain.id`.
- `discord.ui.Button` with `custom_id="map_ban_cancel"`, danger style, admin-only (same `_is_admin` helper as draft).

Locking & error handling: same pattern as `CaptainDraftSession` — `asyncio.Lock` around state mutation, `interaction.response.edit_message` with `message.edit` fallback, `Future[MapBanResult]` resolved on completion or `set_exception` on cancel.

### `services/match_service.py` — `build_plan_from_draft` restored & extended

Restored from `c9f0dd1^` with one change: accept the chosen map as a parameter instead of always picking randomly.

```python
def build_plan_from_draft(
    result,                          # captain_draft.DraftResult
    *,
    free_category: str,
    rng: random.Random,
    map_name: str | None = None,     # NEW: if provided, use it; else rng.choice(MAPS)
) -> MatchPlan:
    ...
    chosen_map = map_name if map_name is not None else rng.choice(elo_calc.MAPS)
    return MatchPlan(
        teams=BalancedTeams(team_a=result.team_a, team_b=result.team_b, ...),
        map_name=chosen_map,
        lobby_leader=result.cap_a,
        category_name=free_category,
    )
```

The `map_name=None` branch is kept as a safety net — currently unused, but lets `build_plan_from_draft` be called from contexts that haven't run a map ban (future flexibility).

### `cogs/match/_cog.py` — branch on `queue_type`

Re-introduce the deleted branch (V5.0 deletion blueprint preserved in `c9f0dd1^:cogs/match/_cog.py`), with two changes:
1. The branch condition becomes `queue_type in ("pro", "semipro")` instead of `queue_type == "pro"`.
2. After `CaptainDraftSession.run()` succeeds, call `MapBanSession.run()` and pass `map_name=ban_result.selected_map` to `build_plan_from_draft`.

```python
if queue_type in ("pro", "semipro"):
    await self._move_players_to_waiting_match(guild, category, [str(p.id) for p in players])
    cap_a, cap_b = pick_captains(players, rng=self.rng)
    pool = tuple(p for p in players if p.id not in (cap_a.id, cap_b.id))

    draft_session = CaptainDraftSession(
        prep_channel=prep_channel,
        cap_a=cap_a, cap_b=cap_b, pool=pool,
        admin_role_names=ADMIN_ROLE_NAMES,
    )
    try:
        draft_result = await draft_session.run()
    except DraftCancelledError as exc:
        # log, delete category, notify channel, return None (queue stays active)
        ...

    ban_session = MapBanSession(
        prep_channel=prep_channel,
        cap_a=cap_a, cap_b=cap_b,
        maps=elo_calc.MAPS,
        admin_role_names=ADMIN_ROLE_NAMES,
    )
    try:
        ban_result = await ban_session.run()
    except MapBanCancelledError as exc:
        # same cleanup as draft cancel
        ...

    plan = build_plan_from_draft(
        draft_result,
        free_category=free_cat_name,
        rng=self.rng,
        map_name=ban_result.selected_map,
    )
else:
    plan = plan_match(players, free_category=free_cat_name, rng=self.rng)
```

The existing `_move_players_to_waiting_match` helper is restored from `c9f0dd1^:cogs/match/_cog.py` verbatim. It moves players to the `Waiting Match` VC of the match category; guards against missing VC, members not in voice, or already-at-destination cases.

### `cogs/match/_constants.py` — unchanged

`ADMIN_ROLE_NAMES` already contains `FL STAFF SEMIPRO`, so admin cancel works for both queues without any change.

## Error handling

| Failure | Behavior |
|---|---|
| `DraftCancelledError` (admin click) | Log, `delete_match_category`, public message in prep channel: "❌ Draft annulé. La queue reste active.", return `None` so the queue is reopened. |
| `MapBanCancelledError` (admin click) | Same cleanup as draft cancel. |
| Unexpected exception during draft or map ban | Log with `exc_info`, `delete_match_category`, ephemeral followup to the queue-fill interaction, return `None`. |
| `interaction.response.edit_message` raises (interaction expired) | Fallback to `message.edit(...)` — same pattern already used in the legacy draft code. |
| Player leaves voice during draft / map ban | Out of scope — Discord IDs are preserved in the draft state. The match goes ahead; the player can be replaced via `/match-replace` after the match announcement. |

No timeout on either phase (user-confirmed: admins handle stalled drafts manually).

## Testing

### Restored from git (`c9f0dd1^`)

- `tests/test_captain_draft.py` (221 lines)
- `tests/test_captain_draft_session.py` (271 lines)

These verify: `pick_captains` randomness/reproducibility/edge cases, `DraftState.apply_pick` immutability, `PICK_SEQUENCE`, `DraftResult.from_state`, admin permission check, full session run with mocked Discord, cancel path, current-captain enforcement.

### New tests for `services/map_pick_ban.py`

`tests/test_map_pick_ban.py`:
- `MapBanState.initial` with 7 maps → `remaining` has 7, `banned` empty, `turn_index=0`, `status="banning"`.
- `apply_ban` removes the map, increments turn_index, returns new instance (originals unchanged).
- `apply_ban` raises `ValueError` if map not in `remaining`.
- `apply_ban` raises `RuntimeError` if status != `"banning"`.
- After 6 `apply_ban`s, `status == "complete"`, `len(remaining) == 1`.
- `MapBanResult.from_state` returns the 1 remaining map as `selected_map`.
- `current_captain` alternates cap_a, cap_b, cap_a, ...
- `BAN_SEQUENCE == ("A","B","A","B","A","B")`.

`tests/test_map_pick_ban_session.py` (mirrors `test_captain_draft_session.py`):
- Mocked Discord channel & interactions.
- Drive 6 bans → assert `MapBanResult` with the expected map.
- Non-current-captain interaction → "⏳ Ce n'est pas ton tour." ephemeral.
- Already-banned map (race) → ephemeral error.
- Admin cancel → `MapBanCancelledError` with actor.
- Non-admin cancel → ephemeral refusal.

### Updated `tests/test_match_cog.py`

- `queue_type="pro"` → assert `CaptainDraftSession` constructed, `MapBanSession` constructed after draft completes, `build_plan_from_draft` called with `map_name=<from ban>`.
- `queue_type="semipro"` → same assertions as pro (new coverage).
- `queue_type="open"` → assert `plan_match` called, no draft/ban session constructed.
- `queue_type="gc"` → same as open.
- Draft cancel → assert `delete_match_category` called, queue stays active.
- Map ban cancel → same.

## Out of scope

- BO3 / BO5 support.
- Side pick (attack/defense) after the map is locked.
- Map ban for Open / GC queues.
- Timeouts / auto-ban after inactivity.
- Persistent map ban state in Mongo (if the bot restarts mid-ban, the session is lost — same property as the original draft).
- Veto roles for specific maps based on player preferences.

## Files touched

| File | Action | Lines (approx.) |
|---|---|---|
| `services/captain_draft.py` | Restore from `c9f0dd1^` | +430 |
| `services/map_pick_ban.py` | New file | +350 |
| `services/match_service.py` | Restore `build_plan_from_draft`, add `map_name` param | +35 |
| `cogs/match/_cog.py` | Restore branch + extend to semipro + add ban session call | +120 |
| `tests/test_captain_draft.py` | Restore from `c9f0dd1^` | +221 |
| `tests/test_captain_draft_session.py` | Restore from `c9f0dd1^` | +271 |
| `tests/test_map_pick_ban.py` | New | +200 |
| `tests/test_map_pick_ban_session.py` | New | +250 |
| `tests/test_match_cog.py` | Update branch tests | +80 |
