# Pro Queue Captain Pick System — Design Spec

**Date:** 2026-05-15
**Scope:** Pro queue only (Open and GC queues remain unchanged)
**Status:** Draft — awaiting user approval before implementation plan

---

## 1. Goal

Replace the automatic team-balancer for the Pro queue with a captain draft flow:

1. When the Pro queue reaches 10 players, find a free Match category as today.
2. Move the 10 players to that category's `Waiting Match` voice channel.
3. Designate the two highest-ELO players as captains (tie-break: random).
4. Post a draft message in `#match-preparation` of that category. The two captains alternate picking from the 8 remaining players using the snake order **A B B A A B B A**.
5. When the 8 picks are complete, build a `MatchPlan` whose `team_a` / `team_b` come from the draft (not from `team_balancer`) and resume the existing match-found flow (persist match, grant roles, announce in `#pro-queue`, move players to `Team 1` / `Team 2`, clear the queue, repost setup-queue).

Open and GC queues continue to use the auto-balancer (`plan_match`) with zero behavior change.

---

## 2. Architectural decisions

### 2.1 Module layout

**New file: `services/captain_draft.py`** (~250 lines, isolated, testable).

```
services/captain_draft.py
├─ pick_captains(players: Sequence[Player], rng: random.Random) -> tuple[Player, Player]
│     - Sort by ELO descending; ties resolved by RNG draw.
│     - Returns (capA, capB) where capA is the resolved top-ELO captain.
│
├─ @dataclass(frozen=True) class DraftState
│     - capA: Player, capB: Player
│     - team_a: tuple[Player, ...]   # starts as (capA,)
│     - team_b: tuple[Player, ...]   # starts as (capB,)
│     - pool: tuple[Player, ...]     # 8 players at start
│     - turn_index: int              # 0..7, position in PICK_SEQUENCE
│     - status: Literal["picking", "complete", "cancelled"]
│
│     Methods:
│     - apply_pick(player: Player) -> DraftState   # pure, returns new state
│     - current_captain -> Player                  # capA or capB per turn_index
│     - is_complete -> bool                        # turn_index == 8
│
├─ PICK_SEQUENCE: tuple[Literal["A","B"], ...] = ("A","B","B","A","A","B","B","A")
│     8 picks total; the 2 captains occupy their team's first slot before the draft starts.
│
├─ class CaptainDraftSession  (orchestration, holds Discord state)
│     - guild, prep_channel, state: DraftState, message: discord.Message | None
│     - _lock: asyncio.Lock
│     - async run() -> DraftResult
│         Blocks until 8 picks complete OR DraftCancelledError raised.
│     - Internal:
│         _build_embed(state) -> discord.Embed
│         _build_view(state) -> discord.ui.View   # Select + admin Cancel button
│         _on_pick(interaction, player)
│         _on_cancel(interaction)
│
└─ exceptions:
   - DraftCancelledError(reason: str, actor: discord.Member | None)
```

### 2.2 Integration point in `cogs/match.py`

`on_queue_full` (currently ~line 394) gains a single conditional branch:

```python
if queue_type == "pro":
    free = find_free_match_prep(guild)
    if not free:
        # Existing fallback: announce "vocaux libres" in #pro-queue, do not start draft.
        return await self._announce_no_free_match(interaction, queue_doc)

    free_cat, free_cat_name = free
    prep_channel = discord.utils.get(
        free_cat.text_channels, name="match-preparation",
    )
    if prep_channel is None:
        # Fallback: announce + auto-balance (no draft possible without prep channel)
        logger.warning("[match] Pro queue: match-preparation channel missing, fallback auto-balance")
        plan = plan_match(players, free_category=free_cat_name, rng=self.rng)
    else:
        # Move 10 players to Waiting Match BEFORE draft
        await self._move_players_to_waiting_match(guild, free_cat, players)

        # Run draft
        try:
            capA, capB = pick_captains(players, rng=self.rng)
            session = CaptainDraftSession(
                guild=guild,
                prep_channel=prep_channel,
                capA=capA, capB=capB,
                pool=tuple(p for p in players if p.id not in (capA.id, capB.id)),
                admin_role_names=ADMIN_ROLE_NAMES,
            )
            result = await session.run()
        except DraftCancelledError as exc:
            return await self._handle_draft_cancelled(interaction, queue_doc, exc)

        # Build MatchPlan from draft result instead of auto-balance
        plan = build_plan_from_draft(
            result, free_category=free_cat_name, lobby_leader=capA,
        )
else:
    plan = plan_match(players, free_category=free_cat_name, rng=self.rng)

# === resume existing flow from here (persist, grants, announce, move VCs, clear queue) ===
```

The non-pro branch is **byte-identical** to today's flow. The pro branch is the only addition.

### 2.3 Player selection criteria

| Decision | Value |
|---|---|
| Captains | Top 2 by current ELO |
| Tie-break | Random (seeded RNG) |
| Pick order | Snake `A B B A A B B A` (8 picks) |
| Pick widget | `discord.ui.Select`, 8 options max |
| Undo | Not allowed (picks are final) |
| Timeout per pick | None (wait indefinitely) |
| Cancel trigger | Admin button in draft message only |
| On cancel | Trivial revert: nothing to undo (queue role not revoked, no DB doc, queue still active) |

### 2.4 Flow ordering — Pro vs non-Pro

```
PRO BRANCH                              NON-PRO BRANCH (unchanged)
──────────────────────────────────       ──────────────────────────────────
1. find_free_match_prep                 1. find_free_match_prep
2. Move 10 → Waiting Match              2. plan_match (auto-balance)
3. pick_captains                        3. Persist match (message_id=None)
4. CaptainDraftSession.run() ◄── draft   4. Grant roles (semaphore 5)
5. Build MatchPlan from draft            5. Announce in #queue
6. Persist match (message_id=None)       6. Persist message_id
7. Grant roles (semaphore 5)             7. delete_active_queue
8. Announce in #pro-queue                8. Move to Team1/Team2
9. Persist message_id                    9. Repost setup-queue
10. delete_active_queue
11. Move to Team1/Team2
12. Repost setup-queue
```

Steps 6–12 of the pro branch are the same code path as steps 3–9 of the non-pro branch. The only insertions are 2–5.

---

## 3. State machine of the draft

```
              ┌───────────────────┐
              │  queue full (pro) │
              └─────────┬─────────┘
                        │
            ┌───────────▼────────────┐
            │ find_free_match_prep   │
            └───────────┬────────────┘
                        │ free_cat found AND #match-preparation present
            ┌───────────▼────────────┐
            │ Move 10 → Waiting Match│
            │ (Semaphore(5), gather) │
            └───────────┬────────────┘
                        │
            ┌───────────▼────────────┐
            │ pick_captains          │
            └───────────┬────────────┘
                        │
            ┌───────────▼────────────┐    ┌──────────────────┐
            │ DraftState init        │    │  PICKING         │
            │ team_a=(capA,)         │◄───┤ (current cap     │
            │ team_b=(capB,)         │    │  interacts)      │
            │ pool=8, turn=0         │    └─┬──────────┬─────┘
            └───────────┬────────────┘      │          │
                        │            pick valid    admin cancel
            ┌───────────▼────────────┐      │          │
            │ Post embed + Select    │   ┌──▼───────┐ ┌▼──────────┐
            │ in #match-preparation  │   │apply_pick│ │ CANCELLED │
            └───────────┬────────────┘   │turn++    │ │ raise     │
                        │                └──┬───────┘ └─┬─────────┘
                        └───────────────────┘           │
                                                        │
                                                 ┌──────▼─────────┐
                                                 │ revert (no-op  │
                                                 │ on DB / roles) │
                                                 │ notify queue   │
                                                 └────────────────┘

turn_index == 8 → status="complete" → DraftResult(team_a, team_b)
                                       → resume normal flow
```

### Concurrency guarantees

1. **Immutable state** — `apply_pick` returns a new `DraftState`. No mutation, no torn reads.
2. **Per-session lock** — `asyncio.Lock` in `CaptainDraftSession` serializes the `(validate → apply → re-render)` transition. Required because the `ui.View` is shared across interactions.
3. **Double-click idempotence** — second concurrent `_on_pick` for the same player observes `player not in state.pool` after the lock and responds with an ephemeral `❌ Joueur déjà drafté`, no state change.

---

## 4. UI

### Embed (rebuilt + edited after each pick)

```
🎯 [PRO] Captain Draft — Match #N
─────────────────────────────────────
🅰️ Team 1 — Cap. <@capA>
   • <@capA> (1487)
   • <@player3> (1290)

🅱️ Team 2 — Cap. <@capB>
   • <@capB> (1455)
   • <@player7> (1310)

🎲 Pool disponible (tri ELO ↓)
   • <@player1> (1410)
   • <@player2> (1380)
   • ...

⏳ Au tour de <@capX> — pick #4
   Séquence : A B B A · A · A B B A   ← cursor on current pick
```

### View

- `discord.ui.Select` (`custom_id="pro_draft_pick"`) with up to 8 options (one per remaining pool player). Each option: `label=player.name`, `description="{elo} ELO · peak {peak}"`, `value=str(player.id)`.
- `discord.ui.Button` (`custom_id="pro_draft_cancel"`, `style=danger`, label `❌ Annuler le draft`).

### Interaction filtering

```python
async def interaction_check(self, interaction: discord.Interaction) -> bool:
    cid = interaction.data["custom_id"]
    if cid == "pro_draft_pick":
        if interaction.user.id != self.state.current_captain.id:
            await interaction.response.send_message(
                "⏳ Ce n'est pas ton tour.", ephemeral=True,
            )
            return False
    elif cid == "pro_draft_cancel":
        if not _has_any_role(interaction.user, ADMIN_ROLE_NAMES):
            await interaction.response.send_message(
                "❌ Réservé aux admins.", ephemeral=True,
            )
            return False
    return True
```

### Initial mention

The draft message includes a content line outside the embed pinging both captains so Discord raises a push notification:

```
<@capA> <@capB> — vous êtes capitaines, à vous de drafter !
```

### Final state

When the 8th pick lands, the embed is edited one last time: pool empty, footer `✅ Draft terminé`, all components disabled. Then the standard `🎯 Match trouvé` announcement posts in `#pro-queue`.

---

## 5. Error handling & edge cases

| # | Case | Detection | Action |
|---|------|-----------|--------|
| 1 | No free Match category | `find_free_match_prep()` returns None | Existing behavior: announce "vocaux libres" in `#pro-queue`, **no draft started**, queue stays full. |
| 2 | Match category exists but no `match-preparation` text channel | Channel lookup returns None | Log warning, fallback to `plan_match` auto-balance + announce "Pro draft indisponible, balance auto". |
| 3 | Double-click on Select (race) | `asyncio.Lock` + `player in state.pool` check | 1st: pick applied, message edited. 2nd: `❌ Joueur déjà drafté` ephemeral. |
| 4 | Captain leaves `Waiting Match` mid-draft | Not actively detected | Draft continues. Admin cancels if needed. |
| 5 | Pool player leaves Discord during draft | Not detected | If picked, `_move_players_to_match_vc` already skips silently (`guild.get_member` returns None, existing guard). Match continues with 9 players. Admin can cancel. |
| 6 | Cancel button clicked mid-pick | Shared `asyncio.Lock` | Cancel waits for pick to finish, then revokes. No double-state. |
| 7 | `Waiting Match` move fails partially (Discord 429) | `gather(return_exceptions=True)` already in place | Successfully-moved players stay there. Draft starts anyway. Others see the draft text and can join the VC manually. |
| 8 | Unhandled exception during draft | `try/except` around `session.run()` in `on_queue_full` | Log + post `⚠️ Draft crashé, admin va intervenir` in `#pro-queue`. No cleanup needed (no DB doc was created). |
| 9 | Bot restarts mid-draft | Session is in-memory only | Draft is dead. Discord message remains with dead buttons. Admin uses `/reset-queue pro` to clear state. **Persistence is deliberately not implemented** (YAGNI). |
| 10 | Admin clicks Cancel | `_on_cancel` callback under lock | Status → `cancelled`. Edit message to `❌ Draft annulé par <@admin>`, disable components. Raise `DraftCancelledError`. Post follow-up in `#pro-queue`: `❌ Draft annulé. La queue reste active.` |
| 11 | Captains tied on ELO | `pick_captains` tie-break | Seeded RNG draw. Reproducible in tests. |
| 12 | Fewer than 2 distinct ELOs among 10 players | Edge of `pick_captains` | Function still returns 2 captains (RNG draw across all tied players). Acceptable. |

### Revert flow (admin cancel)

```
1. acquire session lock
2. state.status = "cancelled"
3. edit draft message → embed "❌ Draft annulé par <@admin>", components disabled
4. session.run() raises DraftCancelledError("admin", admin_user)
5. on_queue_full catches:
   - "En Queue" roles untouched (pro branch defers role revoke until after draft completes)
   - no match doc was persisted → nothing to delete
   - active queue still in DB (delete_active_queue not yet called)
   - 10 players remain in Waiting Match VC
6. post in #pro-queue: "❌ Draft annulé. La queue reste active, /leave puis /join si reset."
```

The pro branch defers all destructive actions (revoke queue role, delete_active_queue) until *after* `draft.complete`. This makes revert a no-op.

### Pre-conditions before launching the draft

The draft only starts if **all** invariants are satisfied:

- [x] `queue_type == "pro"`
- [x] 10 distinct players in `queue_doc["players"]`
- [x] `find_free_match_prep` returns a free category
- [x] The category contains a `match-preparation` text channel (else fallback to auto-balance with a warning announcement)
- [x] `pick_captains` returns 2 distinct captains

### Logging

Each state transition (`init`, `picked`, `cancelled`, `complete`) emits a `logger.info` with `guild_id`, `match_n`, `captain_a_id`, `captain_b_id`, `turn`, `picked_player_id` (when relevant). Sufficient for post-mortem reconstruction.

---

## 6. Testing strategy

### 6.1 Pure logic — `tests/test_captain_draft.py`

```
- test_pick_captains_top_two_elo
- test_pick_captains_tiebreak_random            (seeded RNG, reproducible)
- test_pick_captains_tiebreak_position_2
- test_draft_state_apply_pick_immutable
- test_draft_state_sequence_ABBAABBA            (validates PICK_SEQUENCE)
- test_draft_state_complete_after_8_picks
- test_draft_state_apply_pick_rejects_player_not_in_pool
- test_draft_state_apply_pick_rejects_when_complete
```

### 6.2 Discord session integration — `tests/test_captain_draft_session.py`

Reuses fakes from `tests/test_match_cog.py` (`_fake_guild`, `_fake_member`, `_fake_category` with Team1/Team2/Waiting Match/match-preparation).

```
- test_session_run_happy_path                   (8 mocked picks → DraftResult)
- test_session_run_admin_cancel                 (cancel button by admin → DraftCancelledError)
- test_session_run_cancel_by_non_admin_rejected (ephemeral, session continues)
- test_session_run_pick_not_current_captain_rejected
- test_session_run_double_click_idempotent
- test_session_message_edit_after_each_pick
```

### 6.3 Non-regression on other queues — extends `tests/test_match_cog.py`

```
- test_on_queue_full_open_unchanged             (queue_type="open" → plan_match called)
- test_on_queue_full_gc_unchanged
- test_on_queue_full_pro_uses_captain_draft     (queue_type="pro" → CaptainDraftSession.run called)
- test_on_queue_full_pro_no_free_category_fallback
- test_on_queue_full_pro_no_match_preparation_fallback   (auto-balance fallback)
```

### Coverage target

85%+ on `services/captain_draft.py`. All critical paths (init, pick, complete, cancel, tie-break) covered. Exception paths via `discord.HTTPException` mocks.

### Out of scope for tests

- Live Discord prod tests (impossible)
- Bot restart mid-draft (accepted as "dead", no recovery)
- Real Discord rate-limit verification (already covered by existing tests via Semaphore(5))

---

## 7. Files changed

| File | Type | Approx. lines |
|---|---|---|
| `services/captain_draft.py` | **new** | ~250 |
| `cogs/match.py` | modified | +60 / -0 (new branch in `on_queue_full`, new `_move_players_to_waiting_match` helper, new `_handle_draft_cancelled` helper) |
| `services/match_service.py` | modified | +20 (new `build_plan_from_draft` factory) |
| `tests/test_captain_draft.py` | **new** | ~150 |
| `tests/test_captain_draft_session.py` | **new** | ~200 |
| `tests/test_match_cog.py` | extended | +80 (5 new tests) |

**No changes** to: `services/team_balancer.py`, `services/repository.py`, `services/elo_calc.py`, `services/elo_updater.py`, `cogs/queue_v2.py`, `cogs/applications.py`, `cogs/admin.py`, `cogs/elo_admin.py`, `bot.py`.

---

## 8. Out of scope (explicitly)

- Captain pick for Open or GC queues
- Map veto / map selection by captains
- Voice-channel-based detection of captain abandonment (no `on_voice_state_update` listener)
- Server-leave-based cancellation (no `on_member_remove` listener)
- Persistent draft state across bot restarts
- Captain undo / reset by non-admin
- Penalty for AFK captains (no timeout)
- Statistics on draft picks (e.g. "Cap X always picks player Y")

These can be revisited in a follow-up spec if needed.
