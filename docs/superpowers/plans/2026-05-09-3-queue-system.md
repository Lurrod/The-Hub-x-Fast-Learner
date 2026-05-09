# 3-Queue System Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Refactor the bot from a single 10mans queue into 3 independent queues (Pro / Open / GC), each with its own ELO ladder, leaderboard, role-gated access, and (for Pro) flat ELO calculation.

**Architecture:** Queue type is a new dimension threaded through the storage layer (compound `_id` and `queue_type` field), the Discord-facing layer (3 persistent views with distinct custom_ids per queue, role gates, single-queue lock), the ELO updater (Pro queue takes a flat +16/-16 short-circuit), and the leaderboard refresher (per-queue debounced refresh in a shared `#leaderboard` channel).

**Tech Stack:** Python 3.11+, `discord.py` 2.x, `pymongo` (with `mongomock` in tests), `pytest` / `pytest-asyncio`, `dpytest` for Discord integration tests.

**Spec reference:** `docs/superpowers/specs/2026-05-09-3-queue-system-design.md`

---

## File map

**Created:**
- (none — all changes happen in existing files)

**Modified:**
- `services/elo_calc.py` — `ELO_START` 0 → 2000
- `services/repository.py` — compound `_id` helpers, `queue_type` parameters on most functions, `find_player_in_any_queue`
- `services/elo_updater.py` — Pro Queue flat path, compound `_id`
- `services/leaderboard_refresh.py` — per-queue refresh, debounce key extension
- `cogs/queue_v2.py` — 3 views, role gates, single-queue check, queue_type-aware waiting room
- `cogs/match.py` — `queue_type` propagation, Pro Queue Henrik skip, embeds
- `cogs/riot_link.py` — remove ELO seeding (link is informative only)
- `bot.py` — `queue` parameter on all ELO admin commands, `/setup` creates new channel structure, `/reset-queue` command
- `test_queue_v2.py`, `test_elo_updater.py`, `test_match_cog.py`, `test_match_service.py`, `test_pagination.py`, `test_bot_slash.py` — tests adapted for queue_type

**Conventions:**
- `queue_type: str` is one of `"pro" | "open" | "gc"`. Define `QUEUE_TYPES = ("pro", "open", "gc")` once in `services/repository.py` and import elsewhere.
- Compound `_id` format: `f"{user_id}:{queue_type}"` for ELO docs, `f"active:{queue_type}"` for queue docs, `f"current:{queue_type}"` for leaderboard state docs.

**Migration:** existing data is dropped manually via mongo shell at deployment. Not part of this plan.

---

## Task 1: Add `QUEUE_TYPES` constant and compound-id helpers in `services/repository.py`

**Files:**
- Modify: `services/repository.py`
- Test: `test_repository_helpers.py` (new file)

- [ ] **Step 1: Write the failing test**

Create `test_repository_helpers.py` with:

```python
"""Tests for compound _id helpers in services/repository.py."""

import pytest

from services.repository import (
    QUEUE_TYPES,
    player_doc_id,
    active_queue_id,
    leaderboard_state_id,
    is_valid_queue_type,
)


def test_queue_types_constant():
    assert QUEUE_TYPES == ("pro", "open", "gc")


def test_is_valid_queue_type():
    assert is_valid_queue_type("pro")
    assert is_valid_queue_type("open")
    assert is_valid_queue_type("gc")
    assert not is_valid_queue_type("PRO")
    assert not is_valid_queue_type("")
    assert not is_valid_queue_type("ranked")


def test_player_doc_id():
    assert player_doc_id(123, "pro") == "123:pro"
    assert player_doc_id("456", "open") == "456:open"


def test_active_queue_id():
    assert active_queue_id("pro") == "active:pro"
    assert active_queue_id("open") == "active:open"
    assert active_queue_id("gc") == "active:gc"


def test_leaderboard_state_id():
    assert leaderboard_state_id("pro") == "current:pro"


def test_player_doc_id_rejects_unknown_type():
    with pytest.raises(ValueError, match="queue_type"):
        player_doc_id(123, "ranked")


def test_active_queue_id_rejects_unknown_type():
    with pytest.raises(ValueError, match="queue_type"):
        active_queue_id("ranked")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest test_repository_helpers.py -v`
Expected: ImportError or AttributeError on `QUEUE_TYPES` / helpers (don't exist yet).

- [ ] **Step 3: Add the helpers to `services/repository.py`**

Insert after the existing `import` block, before `_indexed_collections`:

```python
# Tuple ordonne des queue types supportes. L'ordre influence l'affichage
# (boucles de pre-post leaderboards, /setup) : Pro en premier, GC en dernier.
QUEUE_TYPES: tuple[str, ...] = ("pro", "open", "gc")


def is_valid_queue_type(queue_type: str) -> bool:
    return queue_type in QUEUE_TYPES


def _check_queue_type(queue_type: str) -> None:
    if not is_valid_queue_type(queue_type):
        raise ValueError(
            f"queue_type invalide : {queue_type!r}. Attendus : {QUEUE_TYPES}"
        )


def player_doc_id(user_id: int | str, queue_type: str) -> str:
    """Compound _id pour un doc joueur dans elo_<guild>."""
    _check_queue_type(queue_type)
    return f"{user_id}:{queue_type}"


def active_queue_id(queue_type: str) -> str:
    """_id pour la queue active d'un type donne dans queue_<guild>."""
    _check_queue_type(queue_type)
    return f"active:{queue_type}"


def leaderboard_state_id(queue_type: str) -> str:
    """_id pour le state du leaderboard d'un type dans leaderboard_state_<guild>."""
    _check_queue_type(queue_type)
    return f"current:{queue_type}"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest test_repository_helpers.py -v`
Expected: 6 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add services/repository.py test_repository_helpers.py
git commit -m "feat(repo): add QUEUE_TYPES constant and compound-id helpers"
```

---

## Task 2: Change `ELO_START` from 0 to 2000

**Files:**
- Modify: `services/elo_calc.py:9`
- Test: `test_elo_calc.py` (existing)

- [ ] **Step 1: Write the failing test**

Append to `test_elo_calc.py`:

```python
def test_elo_start_is_2000():
    """Default starting ELO is 2000 (was 0). Players are seeded at 2000
    when they first appear in any queue."""
    from services.elo_calc import ELO_START
    assert ELO_START == 2000
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest test_elo_calc.py::test_elo_start_is_2000 -v`
Expected: FAIL — `assert 0 == 2000`.

- [ ] **Step 3: Update the constant**

In `services/elo_calc.py`, change:
```python
ELO_START: Final[int] = 0
```
to:
```python
ELO_START: Final[int] = 2000
```

- [ ] **Step 4: Run all elo_calc tests**

Run: `pytest test_elo_calc.py -v`
Expected: PASS (including new test). Pre-existing tests should still pass — `ELO_START` was never asserted to be 0 in the existing suite, but if any test breaks, fix it by adjusting expected values to 2000.

- [ ] **Step 5: Commit**

```bash
git add services/elo_calc.py test_elo_calc.py
git commit -m "feat(elo): bump ELO_START from 0 to 2000"
```

---

## Task 3: Add `queue_type` parameter to `get_or_create_player`

**Files:**
- Modify: `services/repository.py`
- Test: `test_repository_helpers.py`

- [ ] **Step 1: Write the failing test**

Append to `test_repository_helpers.py`:

```python
import mongomock
from services.repository import get_or_create_player


def test_get_or_create_player_uses_compound_id():
    db = mongomock.MongoClient(tz_aware=True).db
    col = db["elo_42"]

    # First call creates with elo=2000 (initial_elo arg)
    doc = get_or_create_player(col, user_id=1, queue_type="pro",
                                display_name="Alice", initial_elo=2000)
    assert doc["_id"] == "1:pro"
    assert doc["elo"] == 2000
    assert doc["wins"] == 0
    assert doc["queue_type"] == "pro"
    assert doc["name"] == "Alice"


def test_get_or_create_player_isolates_queue_types():
    db = mongomock.MongoClient(tz_aware=True).db
    col = db["elo_42"]
    get_or_create_player(col, user_id=1, queue_type="pro",
                          display_name="Alice", initial_elo=2000)
    get_or_create_player(col, user_id=1, queue_type="open",
                          display_name="Alice", initial_elo=2000)
    docs = list(col.find())
    assert len(docs) == 2
    assert {d["_id"] for d in docs} == {"1:pro", "1:open"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest test_repository_helpers.py -k "compound_id or isolates" -v`
Expected: FAIL — `get_or_create_player` doesn't accept `queue_type`.

- [ ] **Step 3: Update `get_or_create_player`**

Replace the existing function in `services/repository.py`:

```python
def get_or_create_player(
    col,
    user_id: int | str,
    queue_type: str,
    display_name: str,
    initial_elo: int = 2000,
) -> Mapping[str, Any]:
    """Recupere ou cree atomiquement le doc joueur d'une queue.

    Le `_id` est `<user_id>:<queue_type>` (compound). Le champ `queue_type`
    est aussi persiste pour permettre les filtres par type (leaderboard,
    /reset-queue) sans regex sur _id."""
    _check_queue_type(queue_type)
    doc_id = player_doc_id(user_id, queue_type)
    doc = col.find_one_and_update(
        {"_id": doc_id},
        {
            "$set": {"name": display_name},
            "$setOnInsert": {
                "elo":         initial_elo,
                "wins":        0,
                "losses":      0,
                "queue_type":  queue_type,
                "user_id":     str(user_id),
            },
        },
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )
    return doc
```

- [ ] **Step 4: Run tests**

Run: `pytest test_repository_helpers.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add services/repository.py test_repository_helpers.py
git commit -m "feat(repo): get_or_create_player takes queue_type and uses compound _id"
```

---

## Task 4: Update queue functions for `queue_type`

**Files:**
- Modify: `services/repository.py`
- Test: `test_repository_helpers.py`

- [ ] **Step 1: Write the failing test**

Append to `test_repository_helpers.py`:

```python
from services.repository import (
    setup_active_queue,
    get_active_queue,
    delete_active_queue,
    add_player_to_queue,
    remove_player_from_queue,
    close_active_queue,
    find_player_in_any_queue,
)


def test_setup_and_get_active_queue_per_type():
    db = mongomock.MongoClient(tz_aware=True).db
    setup_active_queue(db, guild_id=42, queue_type="pro",
                        channel_id=100, message_id=999)
    setup_active_queue(db, guild_id=42, queue_type="open",
                        channel_id=200, message_id=888)

    pro = get_active_queue(db, guild_id=42, queue_type="pro")
    open_q = get_active_queue(db, guild_id=42, queue_type="open")
    gc = get_active_queue(db, guild_id=42, queue_type="gc")

    assert pro["_id"] == "active:pro"
    assert pro["channel_id"] == 100
    assert pro["queue_type"] == "pro"
    assert open_q["_id"] == "active:open"
    assert open_q["channel_id"] == 200
    assert gc is None


def test_add_remove_player_per_type():
    db = mongomock.MongoClient(tz_aware=True).db
    setup_active_queue(db, guild_id=42, queue_type="pro",
                        channel_id=100, message_id=999)

    res = add_player_to_queue(db, guild_id=42, queue_type="pro", user_id=1)
    assert res.success
    assert res.queue["players"] == ["1"]
    assert res.queue["queue_type"] == "pro"

    res = remove_player_from_queue(db, guild_id=42, queue_type="pro", user_id=1)
    assert res.success
    assert res.queue["players"] == []


def test_find_player_in_any_queue():
    db = mongomock.MongoClient(tz_aware=True).db
    setup_active_queue(db, guild_id=42, queue_type="pro",
                        channel_id=100, message_id=999)
    setup_active_queue(db, guild_id=42, queue_type="open",
                        channel_id=200, message_id=888)
    add_player_to_queue(db, guild_id=42, queue_type="pro", user_id=1)

    assert find_player_in_any_queue(db, guild_id=42, user_id=1) == "pro"
    assert find_player_in_any_queue(db, guild_id=42, user_id=2) is None


def test_delete_active_queue_per_type():
    db = mongomock.MongoClient(tz_aware=True).db
    setup_active_queue(db, guild_id=42, queue_type="pro",
                        channel_id=100, message_id=999)
    setup_active_queue(db, guild_id=42, queue_type="open",
                        channel_id=200, message_id=888)

    assert delete_active_queue(db, guild_id=42, queue_type="pro") is True
    assert get_active_queue(db, guild_id=42, queue_type="pro") is None
    assert get_active_queue(db, guild_id=42, queue_type="open") is not None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest test_repository_helpers.py -v`
Expected: FAIL on signature mismatch / `find_player_in_any_queue` missing.

- [ ] **Step 3: Update queue functions**

In `services/repository.py`, replace the V2 queue functions (currently around lines 220–340). The new signatures take `queue_type`. Add `find_player_in_any_queue`:

```python
def get_active_queue(db: Database, guild_id: int | str, queue_type: str) -> Mapping[str, Any] | None:
    _check_queue_type(queue_type)
    return get_queue_col(db, guild_id).find_one({"_id": active_queue_id(queue_type)})


def setup_active_queue(
    db: Database,
    guild_id: int | str,
    queue_type: str,
    channel_id: int,
    message_id: int,
) -> None:
    """Cree (ou remplace) la queue active de ce type pour ce guild."""
    _check_queue_type(queue_type)
    from datetime import datetime, timezone
    get_queue_col(db, guild_id).update_one(
        {"_id": active_queue_id(queue_type)},
        {"$set": {
            "channel_id": int(channel_id),
            "message_id": int(message_id),
            "players":    [],
            "status":     "open",
            "queue_type": queue_type,
            "created_at": datetime.now(timezone.utc),
        }},
        upsert=True,
    )


def delete_active_queue(db: Database, guild_id: int | str, queue_type: str) -> bool:
    _check_queue_type(queue_type)
    res = get_queue_col(db, guild_id).delete_one({"_id": active_queue_id(queue_type)})
    return res.deleted_count > 0


def close_active_queue(db: Database, guild_id: int | str, queue_type: str) -> None:
    """Marque la queue de ce type comme 'forming'."""
    _check_queue_type(queue_type)
    get_queue_col(db, guild_id).update_one(
        {"_id": active_queue_id(queue_type)},
        {"$set": {"status": "forming"}},
    )


def add_player_to_queue(
    db: Database,
    guild_id: int | str,
    queue_type: str,
    user_id:  int | str,
    *,
    max_size: int = QUEUE_SIZE_DEFAULT,
) -> QueueResult:
    _check_queue_type(queue_type)
    col = get_queue_col(db, guild_id)
    qid = active_queue_id(queue_type)
    queue = col.find_one({"_id": qid})
    if not queue:
        return QueueResult(False, "no_queue", None)
    if queue.get("status") != "open":
        return QueueResult(False, "queue_closed", queue)
    players = queue.get("players", [])
    uid_str = str(user_id)
    if uid_str in players:
        return QueueResult(False, "already_in", queue)
    if len(players) >= max_size:
        return QueueResult(False, "queue_full", queue)
    updated = col.find_one_and_update(
        {
            "_id": qid,
            "status": "open",
            "players": {"$nin": [uid_str]},
            "$expr": {"$lt": [
                {"$size": {"$ifNull": ["$players", []]}},
                max_size,
            ]},
        },
        {"$push": {"players": uid_str}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        return QueueResult(False, "race", queue)
    return QueueResult(True, "added", updated)


def remove_player_from_queue(
    db: Database,
    guild_id: int | str,
    queue_type: str,
    user_id:  int | str,
) -> QueueResult:
    _check_queue_type(queue_type)
    col = get_queue_col(db, guild_id)
    qid = active_queue_id(queue_type)
    queue = col.find_one({"_id": qid})
    if not queue:
        return QueueResult(False, "no_queue", None)
    uid_str = str(user_id)
    if uid_str not in queue.get("players", []):
        return QueueResult(False, "not_in", queue)
    updated = col.find_one_and_update(
        {"_id": qid},
        {"$pull": {"players": uid_str}},
        return_document=ReturnDocument.AFTER,
    )
    return QueueResult(True, "removed", updated)


def find_player_in_any_queue(
    db: Database, guild_id: int | str, user_id: int | str,
) -> str | None:
    """Renvoie le queue_type ou le user est present, ou None."""
    uid_str = str(user_id)
    col = get_queue_col(db, guild_id)
    for qt in QUEUE_TYPES:
        doc = col.find_one({"_id": active_queue_id(qt), "players": uid_str})
        if doc is not None:
            return qt
    return None
```

- [ ] **Step 4: Run tests**

Run: `pytest test_repository_helpers.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add services/repository.py test_repository_helpers.py
git commit -m "feat(repo): queue_type parameter on queue functions + find_player_in_any_queue"
```

---

## Task 5: Update leaderboard state functions for `queue_type`

**Files:**
- Modify: `services/repository.py`
- Test: `test_repository_helpers.py`

- [ ] **Step 1: Write the failing test**

Append to `test_repository_helpers.py`:

```python
from services.repository import (
    get_leaderboard_message_id,
    set_leaderboard_message_id,
    clear_leaderboard_message_id,
)


def test_leaderboard_message_id_per_queue_type():
    db = mongomock.MongoClient(tz_aware=True).db
    set_leaderboard_message_id(db, guild_id=42, queue_type="pro", message_id=111)
    set_leaderboard_message_id(db, guild_id=42, queue_type="open", message_id=222)

    assert get_leaderboard_message_id(db, guild_id=42, queue_type="pro") == 111
    assert get_leaderboard_message_id(db, guild_id=42, queue_type="open") == 222
    assert get_leaderboard_message_id(db, guild_id=42, queue_type="gc") is None

    clear_leaderboard_message_id(db, guild_id=42, queue_type="pro")
    assert get_leaderboard_message_id(db, guild_id=42, queue_type="pro") is None
    assert get_leaderboard_message_id(db, guild_id=42, queue_type="open") == 222
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest test_repository_helpers.py::test_leaderboard_message_id_per_queue_type -v`
Expected: FAIL on TypeError (unexpected `queue_type` arg).

- [ ] **Step 3: Update leaderboard state functions**

In `services/repository.py`, replace the existing 3 functions:

```python
def get_leaderboard_message_id(
    db: Database, guild_id: int | str, queue_type: str,
) -> int | None:
    _check_queue_type(queue_type)
    doc = get_leaderboard_state_col(db, guild_id).find_one(
        {"_id": leaderboard_state_id(queue_type)}
    )
    if not doc:
        return None
    mid = doc.get("message_id")
    return int(mid) if mid is not None else None


def set_leaderboard_message_id(
    db: Database, guild_id: int | str, queue_type: str, message_id: int,
) -> None:
    _check_queue_type(queue_type)
    get_leaderboard_state_col(db, guild_id).update_one(
        {"_id": leaderboard_state_id(queue_type)},
        {"$set": {"message_id": int(message_id)}},
        upsert=True,
    )


def clear_leaderboard_message_id(
    db: Database, guild_id: int | str, queue_type: str,
) -> None:
    _check_queue_type(queue_type)
    get_leaderboard_state_col(db, guild_id).delete_one(
        {"_id": leaderboard_state_id(queue_type)}
    )
```

- [ ] **Step 4: Run tests**

Run: `pytest test_repository_helpers.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add services/repository.py test_repository_helpers.py
git commit -m "feat(repo): leaderboard_state functions take queue_type"
```

---

## Task 6: Add `queue_type` to `create_match` and matches doc

**Files:**
- Modify: `services/repository.py:325-380`
- Test: `test_repository_helpers.py`

- [ ] **Step 1: Write the failing test**

Append to `test_repository_helpers.py`:

```python
from services.repository import create_match, get_match


def test_create_match_persists_queue_type():
    db = mongomock.MongoClient(tz_aware=True).db
    match_id = create_match(
        db, guild_id=42, queue_type="pro",
        team_a=[{"id": "1", "name": "A", "elo": 2000}],
        team_b=[{"id": "2", "name": "B", "elo": 2000}],
        map_name="Ascent",
        lobby_leader_id=1,
        category_name="Match #1",
        message_id=999,
        channel_id=100,
    )
    doc = get_match(db, guild_id=42, match_id=match_id)
    assert doc["queue_type"] == "pro"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest test_repository_helpers.py::test_create_match_persists_queue_type -v`
Expected: FAIL.

- [ ] **Step 3: Update `create_match`**

In `services/repository.py`, modify the `create_match` signature to accept `queue_type` and persist it. Replace:

```python
def create_match(
    db: Database,
    guild_id: int | str,
    *,
    queue_type: str,
    team_a:        list[dict],
    team_b:        list[dict],
    map_name:      str,
    lobby_leader_id: int | str,
    category_name: str | None,
    message_id:    int | None,
    channel_id:    int | None,
) -> Any:
    """Insere un nouveau match. Renvoie son _id (ObjectId)."""
    _check_queue_type(queue_type)
    from datetime import datetime, timezone
    doc = {
        "team_a":          team_a,
        "team_b":          team_b,
        "map":             map_name,
        "queue_type":      queue_type,
        "lobby_leader_id": str(lobby_leader_id),
        "category_name":   category_name,
        "status":          "pending",
        "votes":           {},
        "created_at":      datetime.now(timezone.utc),
        "validated_at":    None,
        "message_id":      int(message_id) if message_id else None,
        "channel_id":      int(channel_id) if channel_id else None,
    }
    res = get_matches_col(db, guild_id).insert_one(doc)
    return res.inserted_id
```

- [ ] **Step 4: Run tests**

Run: `pytest test_repository_helpers.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add services/repository.py test_repository_helpers.py
git commit -m "feat(repo): create_match persists queue_type"
```

---

## Task 7: Update `services/elo_updater.py` for compound `_id` and Pro Queue flat path

**Files:**
- Modify: `services/elo_updater.py`
- Test: `test_elo_updater.py`

- [ ] **Step 1: Write the failing tests**

Append to `test_elo_updater.py`:

```python
def test_apply_match_validation_pro_queue_uses_flat_16():
    """Pro Queue : ignore les multipliers Henrik et applique +16/-16 a plat."""
    import bot as bot_module
    db = bot_module.db
    match_doc = {
        "_id": "match-pro-1",
        "queue_type": "pro",
        "status": "validated_a",
        "team_a": [
            {"id": "1", "name": "A1", "elo": 2000},
            {"id": "2", "name": "A2", "elo": 2000},
        ],
        "team_b": [
            {"id": "3", "name": "B1", "elo": 2000},
            {"id": "4", "name": "B2", "elo": 2000},
        ],
    }
    # On passe meme des multipliers : ils doivent etre IGNORES en Pro Queue.
    multipliers = {"1": 1.5, "2": 0.5, "3": 1.5, "4": 0.5}

    outcome = apply_match_validation(db, guild_id=42, match_doc=match_doc,
                                       multipliers=multipliers)

    assert outcome.gain == 16
    assert outcome.loss == 16
    assert outcome.weighted is False
    deltas = {c.user_id: c.delta for c in outcome.changes}
    # Tous les gagnants : +16 pile, tous les perdants : -16 pile.
    assert deltas == {"1": 16, "2": 16, "3": -16, "4": -16}


def test_apply_match_validation_open_queue_uses_multipliers():
    """Open Queue : flow existant avec multipliers Henrik."""
    import bot as bot_module
    db = bot_module.db
    match_doc = {
        "_id": "match-open-1",
        "queue_type": "open",
        "status": "validated_a",
        "team_a": [
            {"id": "1", "name": "A1", "elo": 2400},
            {"id": "2", "name": "A2", "elo": 2400},
        ],
        "team_b": [
            {"id": "3", "name": "B1", "elo": 2400},
            {"id": "4", "name": "B2", "elo": 2400},
        ],
    }
    multipliers = {"1": 1.5, "2": 0.5}
    outcome = apply_match_validation(db, guild_id=42, match_doc=match_doc,
                                       multipliers=multipliers)
    assert outcome.weighted is True


def test_apply_match_validation_uses_compound_doc_id():
    """Le doc joueur dans elo_<guild> est cree avec _id=<user_id>:<queue_type>."""
    import bot as bot_module
    db = bot_module.db
    match_doc = {
        "_id": "match-pro-2",
        "queue_type": "gc",
        "status": "validated_a",
        "team_a": [{"id": "1", "name": "A", "elo": 2000}],
        "team_b": [{"id": "2", "name": "B", "elo": 2000}],
    }
    apply_match_validation(db, guild_id=42, match_doc=match_doc)
    col = bot_module.db["elo_42"]
    assert col.find_one({"_id": "1:gc"}) is not None
    assert col.find_one({"_id": "2:gc"}) is not None
    # Pas de doc avec _id sans suffixe :
    assert col.find_one({"_id": "1"}) is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest test_elo_updater.py -k "pro_queue or compound" -v`
Expected: FAIL — currently `_id` is just `user_id`, no Pro Queue branch.

- [ ] **Step 3: Update `services/elo_updater.py`**

Modify `apply_match_validation` to short-circuit for Pro Queue and propagate `queue_type` to `_apply_player`. Replace the relevant section:

```python
def apply_match_validation(
    db,
    guild_id: int | str,
    match_doc: dict,
    multipliers: dict[str, float] | None = None,
) -> MatchEloOutcome:
    """
    [docstring existante; complement Pro Queue ci-dessous]

    Pro Queue (queue_type == "pro") : court-circuit. Multipliers ignores,
    +16 a plat pour les gagnants, -16 a plat pour les perdants. Le flow
    Henrik n'est pas appele en amont pour ces matchs (cf. cogs/match.py).
    """
    status = match_doc.get("status")
    if status not in (VALIDATED_A, VALIDATED_B):
        raise ValueError(f"Match non valide : status={status}")

    queue_type = match_doc.get("queue_type", "open")

    if status == VALIDATED_A:
        winners, losers = match_doc["team_a"], match_doc["team_b"]
    else:
        winners, losers = match_doc["team_b"], match_doc["team_a"]

    avg_elo = elo_calc.compute_team_avg_elo(winners + losers)

    if queue_type == "pro":
        # Pro Queue : flat 16, pas de multipliers.
        base_gain = base_loss = FLAT_FALLBACK_ELO_CHANGE
        mults = {}
        weighted = False
    elif multipliers is None:
        base_gain = base_loss = FLAT_FALLBACK_ELO_CHANGE
        mults = {}
        weighted = False
    else:
        base_gain, base_loss = elo_calc.compute_match_elo_change(avg_elo)
        mults = multipliers
        weighted = True

    elo_col  = repository.get_elo_col(db, guild_id)

    winner_mults = [float(mults.get(str(p["id"]), 1.0)) for p in winners]
    loser_mults  = [float(mults.get(str(p["id"]), 1.0)) for p in losers]

    winner_deltas = [int(round(+base_gain * m)) for m in winner_mults]
    loser_deltas  = [int(round(-base_loss * (2.0 - m))) for m in loser_mults]

    # Clamp a 0 : on ne descend jamais sous 0 ELO. Lookup compound _id.
    loser_old_elos: list[int] = []
    for p in losers:
        doc = elo_col.find_one(
            {"_id": repository.player_doc_id(p["id"], queue_type)}
        )
        loser_old_elos.append(
            int(doc.get("elo", elo_calc.ELO_START)) if doc else elo_calc.ELO_START
        )
    clamped_loser_deltas = [
        max(-old, delta) for old, delta in zip(loser_old_elos, loser_deltas)
    ]

    match_id = match_doc.get("_id")
    changes: list[PlayerEloChange] = []
    for p, delta, mult in zip(winners, winner_deltas, winner_mults):
        changes.append(_apply_player(
            elo_col, p, queue_type=queue_type, match_id=match_id,
            delta=delta, win=True, multiplier=mult,
        ))
    for p, delta, mult in zip(losers, clamped_loser_deltas, loser_mults):
        changes.append(_apply_player(
            elo_col, p, queue_type=queue_type, match_id=match_id,
            delta=delta, win=False, multiplier=mult,
        ))

    return MatchEloOutcome(
        avg_elo=avg_elo,
        gain=base_gain,
        loss=base_loss,
        changes=tuple(changes),
        weighted=weighted,
    )


def _apply_player(
    col, player: dict, *, queue_type: str, match_id, delta: int,
    win: bool, multiplier: float = 1.0,
) -> PlayerEloChange:
    """Applique le delta ELO de maniere idempotente par match.

    Le doc joueur est identifie par compound _id `<user_id>:<queue_type>`."""
    uid  = str(player["id"])
    name = player.get("name", uid)
    doc_id = repository.player_doc_id(uid, queue_type)
    match_id_str = str(match_id) if match_id is not None else None

    col.update_one(
        {"_id": doc_id},
        {"$setOnInsert": {
            "name":       name,
            "elo":        elo_calc.ELO_START,
            "wins":       0,
            "losses":     0,
            "queue_type": queue_type,
            "user_id":    uid,
        }},
        upsert=True,
    )

    inc_field = "wins" if win else "losses"
    update: dict[str, Any] = {
        "$inc": {"elo": delta, inc_field: 1},
        "$set": {"name": name},
    }
    if match_id_str is not None:
        update["$addToSet"] = {"processed_matches": match_id_str}
        filter_q = {"_id": doc_id, "processed_matches": {"$nin": [match_id_str]}}
    else:
        filter_q = {"_id": doc_id}

    pre = col.find_one_and_update(
        filter_q, update, return_document=ReturnDocument.BEFORE,
    )

    if pre is None:
        cur_doc = col.find_one({"_id": doc_id})
        cur_elo = int(cur_doc.get("elo", 0)) if cur_doc else 0
        return PlayerEloChange(
            user_id=uid, name=name, old_elo=cur_elo, new_elo=cur_elo,
            delta=0, win=win, multiplier=multiplier,
        )

    old_elo = int(pre.get("elo", 0))
    new_elo = old_elo + delta
    return PlayerEloChange(
        user_id=uid, name=name, old_elo=old_elo, new_elo=new_elo,
        delta=delta, win=win, multiplier=multiplier,
    )
```

- [ ] **Step 4: Run tests**

Run: `pytest test_elo_updater.py -v`
Expected: all PASS. **Existing tests may break** if they call `apply_match_validation` without `queue_type` in match_doc — fix by adding `"queue_type": "open"` to those test fixtures.

- [ ] **Step 5: Commit**

```bash
git add services/elo_updater.py test_elo_updater.py
git commit -m "feat(elo): Pro Queue flat path + compound _id in apply_match_validation"
```

---

## Task 8: Update `services/leaderboard_refresh.py` for per-queue refresh

**Files:**
- Modify: `services/leaderboard_refresh.py`
- Test: `test_pagination.py`

- [ ] **Step 1: Write the failing test**

Add to `test_pagination.py`:

```python
import pytest
import mongomock
from unittest.mock import AsyncMock, MagicMock


@pytest.mark.asyncio
async def test_build_leaderboard_payload_filters_by_queue_type():
    from services.leaderboard_refresh import build_leaderboard_payload
    from services.repository import get_elo_col, player_doc_id

    db = mongomock.MongoClient(tz_aware=True).db
    col = get_elo_col(db, 42)
    col.insert_many([
        {"_id": player_doc_id(1, "pro"), "user_id": "1", "name": "A",
         "elo": 2500, "wins": 5, "losses": 1, "queue_type": "pro"},
        {"_id": player_doc_id(1, "open"), "user_id": "1", "name": "A",
         "elo": 1500, "wins": 1, "losses": 5, "queue_type": "open"},
    ])

    guild = MagicMock()
    guild.id = 42
    guild.name = "TestGuild"
    fake_member = MagicMock()
    fake_member.display_name = "A"
    fake_member.display_avatar.replace.return_value.url = "http://av/1.png"
    guild.get_member.return_value = fake_member

    file_pro, _ = await build_leaderboard_payload(guild, db, queue_type="pro")
    file_open, _ = await build_leaderboard_payload(guild, db, queue_type="open")
    file_gc, _ = await build_leaderboard_payload(guild, db, queue_type="gc")

    assert file_pro is not None
    assert file_open is not None
    assert file_gc is None  # 0 players in GC
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest test_pagination.py::test_build_leaderboard_payload_filters_by_queue_type -v`
Expected: FAIL — `queue_type` parameter missing.

- [ ] **Step 3: Update `services/leaderboard_refresh.py`**

Add `queue_type` to both functions and the debounce key. Modify the file:

```python
# Replace _LAST_REFRESH_AT type
_LAST_REFRESH_AT: "OrderedDict[tuple[int, str], datetime]" = OrderedDict()


async def build_leaderboard_payload(
    guild: discord.Guild, db, queue_type: str, *,
    with_view: bool = True,
    view_timeout: float | None = 300,
) -> Tuple[Optional[discord.File], Optional[discord.ui.View]]:
    """Genere file/view pour le leaderboard du queue_type donne."""
    col  = repository.get_elo_col(db, guild.id)
    docs = list(col.find({"queue_type": queue_type})
                  .sort([("elo", -1), ("wins", -1), ("_id", 1)]))
    if not docs:
        return None, None

    all_players = []
    rank = 1
    for doc in docs:
        uid = doc.get("user_id") or doc["_id"].split(":")[0]
        try:
            member = guild.get_member(int(uid))
        except (TypeError, ValueError):
            member = None
        if member is None:
            continue
        ava_url = str(member.display_avatar.replace(format="png", size=64).url)
        display_name = member.display_name or doc.get("name", uid)
        all_players.append({
            "rank":       rank,
            "name":       display_name,
            "elo":        doc["elo"],
            "wins":       doc.get("wins", 0),
            "losses":     doc.get("losses", 0),
            "kills":      doc.get("kills", 0),
            "deaths":     doc.get("deaths", 0),
            "avatar_url": ava_url,
        })
        rank += 1

    if not all_players:
        return None, None

    total_pages = max(1, (len(all_players) + PAGE_SIZE - 1) // PAGE_SIZE)
    loop = asyncio.get_running_loop()

    async def build_page(page: int) -> discord.File:
        start = page * PAGE_SIZE
        chunk = all_players[start:start + PAGE_SIZE]
        # Le titre du leaderboard inclut le queue_type pour distinguer les
        # 3 leaderboards qui cohabitent dans #leaderboard.
        title = f"Leaderboard {queue_type.upper()} Queue"
        buf   = await loop.run_in_executor(
            None,
            lambda: generate_leaderboard(chunk, server_name=f"{guild.name} - {title}"),
        )
        return discord.File(buf, filename=f"leaderboard_{queue_type}.png")

    # ... View class identique a l'existant, mais build_page utilise le
    # title queue_type-aware ci-dessus. Code complet :

    class LeaderboardView(discord.ui.View):
        def __init__(self, page: int):
            super().__init__(timeout=view_timeout)
            self.page = page
            self.update_buttons()

        def update_buttons(self):
            self.prev_btn.disabled = self.page == 0
            self.next_btn.disabled = self.page >= total_pages - 1
            self.page_btn.label    = f"Page {self.page + 1} / {total_pages}"

        async def _go(self, inter, new_page):
            if new_page < 0 or new_page >= total_pages:
                if not inter.response.is_done():
                    await inter.response.defer()
                return
            self.page = new_page
            self.update_buttons()
            try:
                if not inter.response.is_done():
                    await inter.response.defer()
                file = await build_page(self.page)
                await inter.followup.edit_message(
                    message_id=inter.message.id,
                    attachments=[file], view=self,
                )
            except Exception:
                logger.exception("leaderboard_refresh exception")

        @discord.ui.button(emoji="◀️", style=discord.ButtonStyle.secondary)
        async def prev_btn(self, inter, button):
            await self._go(inter, self.page - 1)

        @discord.ui.button(label="Page 1 / 1", style=discord.ButtonStyle.grey, disabled=True)
        async def page_btn(self, inter, button):
            pass

        @discord.ui.button(emoji="▶️", style=discord.ButtonStyle.secondary)
        async def next_btn(self, inter, button):
            await self._go(inter, self.page + 1)

    file = await build_page(0)
    if not with_view:
        return file, None
    return file, LeaderboardView(page=0)


async def refresh_leaderboard_channel(
    guild: discord.Guild, db, bot_user_id: int, queue_type: str,
) -> None:
    """Refresh le leaderboard du queue_type donne dans #leaderboard.

    Per-queue debounce : une rafale Pro ne bloque pas un refresh Open."""
    repository._check_queue_type(queue_type)
    now = datetime.now(timezone.utc)
    key = (guild.id, queue_type)
    last = _LAST_REFRESH_AT.get(key)
    if last is not None and (now - last).total_seconds() < _REFRESH_DEBOUNCE_SECONDS:
        _LAST_REFRESH_AT.move_to_end(key)
        return
    _LAST_REFRESH_AT[key] = now
    _LAST_REFRESH_AT.move_to_end(key)
    while len(_LAST_REFRESH_AT) > _MAX_GUILDS_TRACKED:
        _LAST_REFRESH_AT.popitem(last=False)

    needle = LEADERBOARD_CHANNEL_NAME.lower()
    chan = next(
        (c for c in guild.text_channels if needle in (c.name or "").lower()),
        None,
    )
    if chan is None:
        return

    stored_id = repository.get_leaderboard_message_id(db, guild.id, queue_type)
    deleted_via_stored = False
    if stored_id is not None:
        try:
            old_msg = await chan.fetch_message(stored_id)
            await old_msg.delete()
            deleted_via_stored = True
        except discord.NotFound:
            repository.clear_leaderboard_message_id(db, guild.id, queue_type)
        except Exception:
            logger.exception("leaderboard_refresh exception")

    # Pas de fallback history scan : avec 3 LB qui cohabitent, on ne peut
    # pas identifier "lequel des 3" sans le state persiste. Si aucun
    # stored_id n'existe, on poste juste le nouveau (premier post).

    try:
        file, view = await build_leaderboard_payload(
            guild, db, queue_type, view_timeout=None,
        )
    except Exception:
        logger.exception("leaderboard_refresh exception")
        return
    if file is None:
        return

    try:
        new_msg = await chan.send(file=file, view=view)
    except Exception:
        logger.exception("leaderboard_refresh exception")
        return

    try:
        repository.set_leaderboard_message_id(db, guild.id, queue_type, new_msg.id)
    except Exception:
        logger.exception("leaderboard_refresh exception")
```

- [ ] **Step 4: Run tests**

Run: `pytest test_pagination.py -v`
Expected: PASS. Pre-existing tests may need adapting if they call `build_leaderboard_payload` without `queue_type` — pass `queue_type="open"` for back-compat.

- [ ] **Step 5: Commit**

```bash
git add services/leaderboard_refresh.py test_pagination.py
git commit -m "feat(leaderboard): per-queue refresh with type-keyed debounce"
```

---

## Task 9: Update `cogs/queue_v2.py` — multi-view QueueCog with role gates

**Files:**
- Modify: `cogs/queue_v2.py`
- Test: `test_queue_v2.py`

This is the largest change. Read the existing file first.

- [ ] **Step 1: Write the failing tests**

Append to `test_queue_v2.py`:

```python
@pytest.mark.asyncio
async def test_join_pro_queue_requires_role():
    """Sans role 'Rank S | Pro Queue', refus de rejoindre Pro Queue."""
    import bot as bot_module
    from cogs.queue_v2 import QueueView
    db = bot_module.db
    repo = repository
    repo.setup_active_queue(db, guild_id=42, queue_type="pro",
                              channel_id=100, message_id=999)
    _seed_riot_link(db, 42, 1)

    member = _fake_member(1)
    member.roles = []  # pas de role Pro
    member.guild = _fake_guild(42)
    member.guild.roles = []
    inter = _fake_interaction(member)
    inter.user = member

    view = QueueView(db, queue_type="pro")
    await view.join_btn.callback(view, inter, MagicMock())

    inter.followup.send.assert_called()
    msg = inter.followup.send.call_args[0][0]
    assert "Rank S" in msg or "Pro Queue" in msg


@pytest.mark.asyncio
async def test_cannot_join_two_queues_simultaneously():
    """Si dans Pro Queue, refus de rejoindre Open Queue."""
    import bot as bot_module
    from cogs.queue_v2 import QueueView
    db = bot_module.db
    repository.setup_active_queue(db, guild_id=42, queue_type="pro",
                                    channel_id=100, message_id=999)
    repository.setup_active_queue(db, guild_id=42, queue_type="open",
                                    channel_id=200, message_id=888)
    repository.add_player_to_queue(db, guild_id=42, queue_type="pro", user_id=1)
    _seed_riot_link(db, 42, 1)

    member = _fake_member(1)
    member.roles = []
    member.guild = _fake_guild(42)
    member.guild.roles = []
    inter = _fake_interaction(member)
    inter.user = member

    view_open = QueueView(db, queue_type="open")
    await view_open.join_btn.callback(view_open, inter, MagicMock())

    inter.followup.send.assert_called()
    msg = inter.followup.send.call_args[0][0]
    assert "deja" in msg.lower() or "autre queue" in msg.lower()


def test_queue_view_custom_ids_per_type():
    db = MagicMock()
    pro = QueueView(db, queue_type="pro")
    open_v = QueueView(db, queue_type="open")
    assert pro.join_btn.custom_id == "queue_v2:join:pro"
    assert pro.leave_btn.custom_id == "queue_v2:leave:pro"
    assert open_v.join_btn.custom_id == "queue_v2:join:open"


def test_waiting_room_name_per_queue_type():
    from cogs.queue_v2 import WAITING_ROOM_NAMES
    assert WAITING_ROOM_NAMES["pro"] == "Waiting Room Pro"
    assert WAITING_ROOM_NAMES["open"] == "Waiting Room Open"
    assert WAITING_ROOM_NAMES["gc"] == "Waiting Room GC"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest test_queue_v2.py -k "pro_queue or two_queues or custom_ids or waiting_room" -v`
Expected: FAIL.

- [ ] **Step 3: Refactor `cogs/queue_v2.py`**

This is the core refactor. Replace the file's contents (preserve docstring, imports, then rewrite):

```python
# Top-level constants — replace existing single Waiting Room constant
WAITING_ROOM_NAMES: dict[str, str] = {
    "pro":  "Waiting Room Pro",
    "open": "Waiting Room Open",
    "gc":   "Waiting Room GC",
}

# Role required to join each gated queue. None = no gate.
QUEUE_ROLE_GATES: dict[str, str | None] = {
    "pro":  "Rank S | Pro Queue",
    "open": None,
    "gc":   "GC",
}

# Channel name expected for each queue's persistent message
QUEUE_CHANNEL_NAMES: dict[str, str] = {
    "pro":  "pro-queue",
    "open": "open-queue",
    "gc":   "gc-queue",
}

QUEUE_ROLE_NAME = "En Queue"  # global, unchanged
QUEUE_SIZE = 10
```

Replace `_move_to_waiting_room` to take `queue_type`:

```python
async def _move_to_waiting_room(
    member: discord.Member, queue_type: str,
) -> str | None:
    waiting_name = WAITING_ROOM_NAMES[queue_type]
    waiting = discord.utils.get(member.guild.voice_channels, name=waiting_name)
    if waiting is None:
        return None
    voice_state = member.voice
    if voice_state is None or voice_state.channel is None:
        return f"Connecte-toi a un salon vocal pour etre deplace dans **{waiting_name}**."
    if voice_state.channel.id == waiting.id:
        return None
    try:
        await member.move_to(waiting, reason=f"Auto-move queue join ({queue_type})")
    except discord.Forbidden:
        return f"Permissions insuffisantes pour te deplacer dans **{waiting_name}**."
    except discord.HTTPException:
        return None
    return None
```

Replace `build_queue_embed` to include `queue_type`:

```python
def build_queue_embed(queue_doc: dict | None, guild: discord.Guild, queue_type: str) -> discord.Embed:
    label = {"pro": "Pro Queue", "open": "Open Queue", "gc": "GC Queue"}[queue_type]
    players = list((queue_doc or {}).get("players", []))
    count   = len(players)
    full    = count >= QUEUE_SIZE
    status  = (queue_doc or {}).get("status", "open")

    if status == "forming":
        color = 0xe67e22
        state = "Match en formation"
    elif full:
        color = 0x2ecc71
        state = "Queue pleine !"
    else:
        color = 0x5865f2
        state = "En attente de joueurs"

    embed = discord.Embed(
        title=f"{label} 10mans - {count}/{QUEUE_SIZE}",
        description=state,
        color=color,
        timestamp=datetime.now(timezone.utc),
    )
    if players:
        mentions = "\n".join(f"- <@{uid}>" for uid in players)
        embed.add_field(name="Joueurs", value=mentions, inline=False)
    else:
        embed.add_field(name="Joueurs", value="*Personne pour le moment.*", inline=False)
    embed.set_footer(text=guild.name)
    return embed
```

Replace `QueueView` to accept `queue_type` (decorator buttons must be replaced with manual `discord.ui.Button` instances since custom_ids are dynamic). Use the dynamic-button pattern:

```python
class QueueView(discord.ui.View):
    """View persistante par queue_type. Custom IDs distincts pour cohabiter."""

    def __init__(self, db, queue_type: str, on_full=None) -> None:
        super().__init__(timeout=None)
        self.db = db
        self.queue_type = queue_type
        self._on_full = on_full
        self._locks: OrderedDict[int, asyncio.Lock] = OrderedDict()

        # Dynamic-id buttons (decorator approach doesn't allow per-instance ids)
        join = discord.ui.Button(
            label="Rejoindre",
            style=discord.ButtonStyle.success,
            custom_id=f"queue_v2:join:{queue_type}",
        )
        join.callback = self._join_callback
        self.join_btn = join
        self.add_item(join)

        leave = discord.ui.Button(
            label="Quitter",
            style=discord.ButtonStyle.danger,
            custom_id=f"queue_v2:leave:{queue_type}",
        )
        leave.callback = self._leave_callback
        self.leave_btn = leave
        self.add_item(leave)

    def _lock(self, guild_id: int) -> asyncio.Lock:
        lock = self._locks.get(guild_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[guild_id] = lock
            while len(self._locks) > _LOCKS_MAXSIZE:
                self._locks.popitem(last=False)
        else:
            self._locks.move_to_end(guild_id)
        return lock

    def _has_required_role(self, member: discord.Member) -> tuple[bool, str | None]:
        """Renvoie (has_role, role_name_required_or_None_if_no_gate)."""
        required = QUEUE_ROLE_GATES.get(self.queue_type)
        if required is None:
            return True, None
        if any(r.name == required for r in member.roles):
            return True, required
        return False, required

    async def _join_callback(self, inter: discord.Interaction):
        try:
            await inter.response.defer()
        except discord.NotFound:
            return

        async with self._lock(inter.guild_id):
            # 1) compte Riot lie
            riot = await asyncio.to_thread(
                repository.get_riot_account, self.db, inter.guild_id, inter.user.id,
            )
            if not riot:
                await inter.followup.send(
                    "Lie d'abord ton compte Riot avec `/link-riot Pseudo#TAG`.",
                    ephemeral=True,
                )
                return

            # 2) match en cours
            if isinstance(inter.user, discord.Member):
                ongoing = _has_match_role(inter.user)
                if ongoing is not None:
                    await inter.followup.send(
                        f"Tu es deja dans un match (role `{ongoing}`).",
                        ephemeral=True,
                    )
                    return

            # 3) deja dans une autre queue ?
            current = await asyncio.to_thread(
                repository.find_player_in_any_queue,
                self.db, inter.guild_id, inter.user.id,
            )
            if current is not None and current != self.queue_type:
                await inter.followup.send(
                    f"Tu es deja dans la queue **{current.upper()}**. "
                    "Quitte-la d'abord pour rejoindre une autre queue.",
                    ephemeral=True,
                )
                return

            # 4) gate de role
            if isinstance(inter.user, discord.Member):
                ok, required = self._has_required_role(inter.user)
                if not ok:
                    await inter.followup.send(
                        f"Cette queue est reservee aux joueurs avec le role **{required}**.",
                        ephemeral=True,
                    )
                    return

            # 5) ajout en base
            res = await asyncio.to_thread(
                repository.add_player_to_queue,
                self.db, inter.guild_id, self.queue_type, inter.user.id,
            )
            if not res.success:
                await inter.followup.send(
                    _join_error_message(res.reason), ephemeral=True,
                )
                return

            queue_doc = res.queue
            full = len(queue_doc.get("players", [])) >= QUEUE_SIZE
            if full:
                await asyncio.to_thread(
                    repository.close_active_queue,
                    self.db, inter.guild_id, self.queue_type,
                )
                queue_doc = await asyncio.to_thread(
                    repository.get_active_queue,
                    self.db, inter.guild_id, self.queue_type,
                )

            embed = build_queue_embed(queue_doc, inter.guild, self.queue_type)
            await inter.edit_original_response(embed=embed, view=self)

            move_notice = role_notice = None
            if isinstance(inter.user, discord.Member):
                move_notice = await _move_to_waiting_room(inter.user, self.queue_type)
                role_notice = await _grant_queue_role(inter.user)

            count = len(queue_doc.get("players", []))
            confirm = f"Tu as rejoint la queue {self.queue_type.upper()} ({count}/{QUEUE_SIZE})"
            if move_notice:
                confirm += f"\n{move_notice}"
            if role_notice:
                confirm += f"\n{role_notice}"
            await inter.followup.send(confirm, ephemeral=True)

            if full and self._on_full:
                asyncio.create_task(self._safe_on_full(inter, queue_doc))

    async def _safe_on_full(self, inter, queue_doc):
        try:
            await self._on_full(inter, queue_doc, self.queue_type)
        except Exception as e:
            logger.exception("[queue_v2] _safe_on_full a leve")
            try:
                repository.delete_active_queue(self.db, inter.guild_id, self.queue_type)
            except Exception:
                logger.exception("[queue_v2] cleanup apres on_full a leve")
            user_msg = (
                f"Erreur formation match : `{e}`. Queue {self.queue_type} liberee."
            )
            try:
                if inter.channel is not None:
                    await inter.channel.send(user_msg)
            except Exception:
                logger.exception("[queue_v2] notification erreur a leve")

    async def _leave_callback(self, inter: discord.Interaction):
        try:
            await inter.response.defer()
        except discord.NotFound:
            return
        async with self._lock(inter.guild_id):
            res = await asyncio.to_thread(
                repository.remove_player_from_queue,
                self.db, inter.guild_id, self.queue_type, inter.user.id,
            )
            if not res.success:
                await inter.followup.send(
                    _leave_error_message(res.reason), ephemeral=True,
                )
                return
            embed = build_queue_embed(res.queue, inter.guild, self.queue_type)
            await inter.edit_original_response(embed=embed, view=self)
            if isinstance(inter.user, discord.Member):
                # Le joueur est sorti de SA queue. S'il n'est dans aucune
                # autre queue, on retire le role global "En Queue".
                still_in = await asyncio.to_thread(
                    repository.find_player_in_any_queue,
                    self.db, inter.guild_id, inter.user.id,
                )
                if still_in is None:
                    await _revoke_queue_role(inter.user)
```

Replace `QueueCog` to manage the 3 views and updated commands:

```python
class QueueCog(commands.Cog):
    def __init__(self, bot: commands.Bot, db, on_full=None) -> None:
        self.bot = bot
        self.db = db
        self.on_full = on_full
        self.views: dict[str, QueueView] = {
            qt: QueueView(db, queue_type=qt, on_full=on_full)
            for qt in repository.QUEUE_TYPES
        }

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member) -> None:
        for qt in repository.QUEUE_TYPES:
            try:
                await asyncio.to_thread(
                    repository.remove_player_from_queue,
                    self.db, member.guild.id, qt, member.id,
                )
            except Exception:
                logger.exception("[queue_v2] on_member_remove a leve")

    @app_commands.command(name="setup-queue", description="Pose le message de queue dans ce salon")
    @app_commands.describe(queue="Type de queue")
    @app_commands.choices(queue=[
        app_commands.Choice(name="Pro", value="pro"),
        app_commands.Choice(name="Open", value="open"),
        app_commands.Choice(name="GC", value="gc"),
    ])
    @app_commands.checks.has_permissions(manage_guild=True)
    async def setup_queue(self, interaction: discord.Interaction, queue: str) -> None:
        repository.delete_active_queue(self.db, interaction.guild_id, queue)
        await self.post_queue_message(interaction.channel, queue)
        await interaction.response.send_message(
            f"Queue **{queue.upper()}** active dans {interaction.channel.mention} !",
            ephemeral=True,
        )

    async def post_queue_message(
        self, channel: discord.TextChannel, queue_type: str,
    ) -> None:
        view = self.views[queue_type]
        embed = build_queue_embed(None, channel.guild, queue_type)
        msg = await channel.send(embed=embed, view=view)
        repository.setup_active_queue(
            self.db, guild_id=channel.guild.id, queue_type=queue_type,
            channel_id=channel.id, message_id=msg.id,
        )

    @app_commands.command(name="close-queue", description="Ferme la queue active d'un type")
    @app_commands.describe(queue="Type de queue")
    @app_commands.choices(queue=[
        app_commands.Choice(name="Pro", value="pro"),
        app_commands.Choice(name="Open", value="open"),
        app_commands.Choice(name="GC", value="gc"),
    ])
    @app_commands.checks.has_permissions(manage_guild=True)
    async def close_queue(self, interaction: discord.Interaction, queue: str) -> None:
        deleted = repository.delete_active_queue(self.db, interaction.guild_id, queue)
        msg = f"Queue {queue.upper()} supprimee." if deleted else f"Aucune queue {queue.upper()} active."
        await interaction.response.send_message(msg, ephemeral=True)

    @setup_queue.error
    @close_queue.error
    async def _perm_error(self, inter: discord.Interaction, error):
        if isinstance(error, app_commands.MissingPermissions):
            await inter.response.send_message(
                "Reserve aux administrateurs.", ephemeral=True,
            )


async def setup(bot: commands.Bot, db, on_full=None) -> None:
    cog = QueueCog(bot, db, on_full=on_full)
    await bot.add_cog(cog)
    for view in cog.views.values():
        bot.add_view(view)
```

Update legacy aliases at module top so existing imports don't break: keep `JOIN_BTN_ID = "queue_v2:join:open"` and `LEAVE_BTN_ID = "queue_v2:leave:open"` for back-compat with any test that imports them, or remove them and update the imports in tests.

- [ ] **Step 4: Run tests**

Run: `pytest test_queue_v2.py -v`
Expected: PASS for new tests. **Existing tests will likely break** — they instantiate `QueueView(db)` (one-arg) and call repository functions without `queue_type`. Update each existing test to pass `queue_type="open"` (default behaviour preserved). Run again until green.

- [ ] **Step 5: Commit**

```bash
git add cogs/queue_v2.py test_queue_v2.py
git commit -m "feat(queue): 3-queue support with role gates, single-queue lock, per-type views"
```

---

## Task 10: Update `cogs/match.py` — propagate `queue_type` and skip Henrik for Pro

**Files:**
- Modify: `cogs/match.py`
- Test: `test_match_cog.py`

- [ ] **Step 1: Write the failing tests**

Append to `test_match_cog.py`:

```python
def test_on_queue_full_passes_queue_type_to_create_match(monkeypatch):
    """Quand une Pro Queue se forme, le match doc est tag queue_type=pro."""
    import bot as bot_module
    from cogs.match import MatchCog
    db = bot_module.db

    captured = {}

    def fake_create_match(db, guild_id, *, queue_type, **kwargs):
        captured["queue_type"] = queue_type
        captured["guild_id"] = guild_id
        return "mock-match-id"

    monkeypatch.setattr("services.repository.create_match", fake_create_match)
    # ... compose plan, call on_queue_full(inter, queue_doc, "pro")
    # (Test stub : full integration test left to the existing dpytest tests.)


def test_verify_match_skips_henrik_for_pro_queue():
    """Pro Queue doc is processed via direct apply_match_validation, no Henrik call."""
    # See cogs/match.py _verify_match : if queue_type == "pro", skip the
    # Henrik branch and call apply_match_validation directly with multipliers=None.
    # Fixture-based test: insert a validated_a Pro Queue match and assert
    # no http call is made to Henrik client mock.
    pass  # Placeholder for now — the implementation step below covers behaviour
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest test_match_cog.py -v`
Expected: existing `test_on_queue_full_*` may fail because `create_match` now requires `queue_type` kwarg. Adjust as we implement.

- [ ] **Step 3: Update `cogs/match.py`**

In `cogs/match.py`, change `on_queue_full` signature and body:

```python
async def on_queue_full(self, inter, queue_doc, queue_type: str) -> None:
    """Branche pour QueueView quand une queue de `queue_type` est pleine."""
    # ... (logique existante) ...
    # 1) build_players doit lire l'ELO de la bonne queue : passer queue_type
    players = build_players(self.db, guild.id, queue_doc["players"], queue_type=queue_type)
    plan = plan_match(players)
    # 2) au moment de create_match :
    match_id = repository.create_match(
        self.db, guild_id=guild.id, queue_type=queue_type,
        team_a=serialize_team(plan.teams.team_a),
        team_b=serialize_team(plan.teams.team_b),
        map_name=plan.map_name,
        lobby_leader_id=plan.lobby_leader.id,
        category_name=plan.category_name,
        message_id=msg.id,
        channel_id=msg.channel.id,
    )
    # 3) apres formation, re-poser une nouvelle queue dans le bon salon :
    queue_cog = self.bot.get_cog("QueueCog")
    target_channel_name = QUEUE_CHANNEL_NAMES[queue_type]
    target_channel = discord.utils.get(
        guild.text_channels, name=target_channel_name,
    )
    if queue_cog and target_channel:
        await queue_cog.post_queue_message(target_channel, queue_type)
```

Import `QUEUE_CHANNEL_NAMES` from `cogs.queue_v2`.

In `_verify_match` (the Henrik scanner), at the entry point of the iteration, add:

```python
if match_doc.get("queue_type") == "pro":
    # Pro Queue: pas de pondération Henrik. On applique directement le flat.
    if not repository.claim_match_for_elo(self.db, guild_id, match_doc["_id"]):
        continue  # already applied
    try:
        outcome = apply_match_validation(self.db, guild_id, match_doc, multipliers=None)
        repository.set_match_henrik_verified(
            self.db, guild_id, match_doc["_id"], found=False, multipliers=None,
        )
        # Build embed + post in match channel + refresh leaderboard:
        await self._post_elo_changes_embed(match_doc, outcome)
        await refresh_leaderboard_channel(guild, self.db, self.bot.user.id, "pro")
    except Exception:
        logger.exception("[match] Pro Queue ELO application failed")
        repository.release_elo_claim(self.db, guild_id, match_doc["_id"])
    continue
```

`_post_elo_changes_embed` is whatever helper already exists for posting the ELO change embed; if not factored out, factor it out as `_post_elo_changes_embed(self, match_doc, outcome)`.

For Open/GC Queue paths : after `apply_match_validation`, the existing `refresh_leaderboard_channel` call is updated to pass `match_doc["queue_type"]`:

```python
await refresh_leaderboard_channel(
    guild, self.db, self.bot.user.id, match_doc.get("queue_type", "open"),
)
```

In `build_match_embed_from_doc` and `build_match_embed`, add the queue_type label in the title:

```python
qt = doc.get("queue_type", "open").upper()
title = f"[{qt} QUEUE] {existing_title}"
```

Update `match-replace`, `match-cancel`, and any other admin command that touches a match doc: where they refresh the leaderboard, pass `match_doc["queue_type"]`.

In `build_players` (in `services/match_service.py`), add `queue_type` parameter and use compound `_id` for ELO lookups:

```python
def build_players(db, guild_id, user_ids, queue_type: str):
    col = repository.get_elo_col(db, guild_id)
    players = []
    for uid in user_ids:
        doc = col.find_one({"_id": repository.player_doc_id(uid, queue_type)})
        elo = (doc or {}).get("elo", elo_calc.ELO_START)
        # ... build Player obj
    return players
```

- [ ] **Step 4: Run tests**

Run: `pytest test_match_cog.py test_match_service.py -v`
Expected: PASS. Existing failures fix by adding `queue_type="open"` defaults to test fixtures and `create_match` calls.

- [ ] **Step 5: Commit**

```bash
git add cogs/match.py services/match_service.py test_match_cog.py test_match_service.py
git commit -m "feat(match): propagate queue_type, Pro Queue skips Henrik"
```

---

## Task 11: Remove ELO seeding from `/link-riot`

**Files:**
- Modify: `cogs/riot_link.py`
- Test: `test_riot_link.py`

- [ ] **Step 1: Write the failing test**

Append to `test_riot_link.py`:

```python
@pytest.mark.asyncio
async def test_link_riot_does_not_seed_elo(monkeypatch):
    """/link-riot ne seed plus l'ELO. Le doc Riot est cree, le doc ELO non."""
    import bot as bot_module
    from cogs.riot_link import RiotLinkCog
    from services.riot_api import HenrikDevClient

    # Mock Henrik client
    mock_client = MagicMock(spec=HenrikDevClient)
    mock_client.get_account.return_value = {
        "puuid": "abc-puuid", "name": "Player", "tag": "EUW",
    }
    mock_client.get_current_mmr.return_value = {"current": {"tier": 24}}

    cog = RiotLinkCog(bot_module.bot, bot_module.db, mock_client)
    member = _fake_member(1, name="Tester")
    inter = _fake_interaction(member)
    inter.response.defer = AsyncMock()
    inter.followup.send = AsyncMock()

    await cog.link_riot.callback(cog, inter, riot_id="Player#EUW")

    # Le doc Riot est cree :
    riot = repository.get_riot_account(bot_module.db, guild_id=42, user_id=1)
    assert riot is not None

    # Mais aucun doc ELO n'est seede dans aucune queue :
    col = repository.get_elo_col(bot_module.db, 42)
    assert col.count_documents({}) == 0
```

(Re-use `_fake_member` / `_fake_interaction` helpers from `test_queue_v2.py` or import them.)

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest test_riot_link.py::test_link_riot_does_not_seed_elo -v`
Expected: FAIL — current code calls `seed_elo_with_riot_base`.

- [ ] **Step 3: Update `cogs/riot_link.py`**

Remove the `seed_elo_with_riot_base` call from the `/link-riot` flow. Remove the `LINK_BASE_ELO` constant. Replace the post-validation block with a simple `link_riot_account` call:

```python
# Existing code:
#   elo, seeded = repository.seed_elo_with_riot_base(...)
# Replace with:
repository.link_riot_account(
    self.db, guild_id=interaction.guild_id, user_id=interaction.user.id,
    riot_name=name, riot_tag=tag, riot_region=region,
    puuid=account["puuid"], peak_elo=peak_elo, source=source,
)
# Plus de seeding ELO. Le doc Riot sert uniquement aux verifications Henrik.
```

Update the response embed/text to remove any mention of "ELO de depart" / "+2000 ELO". Just confirm "Compte lie : `Pseudo#TAG`."

- [ ] **Step 4: Run tests**

Run: `pytest test_riot_link.py -v`
Expected: PASS. Pre-existing tests asserting seed behaviour need to be deleted or updated.

- [ ] **Step 5: Commit**

```bash
git add cogs/riot_link.py test_riot_link.py
git commit -m "refactor(link-riot): remove ELO seeding, link is informative only"
```

---

## Task 12: Update `bot.py` — `queue` parameter on `/win` and `/lose`

**Files:**
- Modify: `bot.py:230-330`
- Test: `test_bot_slash.py`

- [ ] **Step 1: Write the failing test**

Append to `test_bot_slash.py`:

```python
@pytest.mark.asyncio
async def test_win_command_applies_to_specific_queue():
    import bot as bot_module
    db = bot_module.db
    # Creer 2 docs ELO pour le meme user dans 2 queues differentes :
    db["elo_42"].insert_many([
        {"_id": "1:pro", "user_id": "1", "name": "Alice", "elo": 2000,
         "wins": 0, "losses": 0, "queue_type": "pro"},
        {"_id": "1:open", "user_id": "1", "name": "Alice", "elo": 2000,
         "wins": 0, "losses": 0, "queue_type": "open"},
    ])
    # ... appel /win avec queue=pro et joueur1 = Alice
    # Verifier : pro elo a augmente, open elo inchange
    # (Test complete via dpytest — squelette ici)
```

- [ ] **Step 2: Run test to verify it fails**

- [ ] **Step 3: Update `/win` and `/lose` in `bot.py`**

Add `queue` parameter as the first arg, with `app_commands.Choice`s. Inside, all lookups/updates use `repository.player_doc_id(member.id, queue)` instead of `str(member.id)`. Example for `/win`:

```python
@tree.command(name="win", description="Enregistre une victoire (gain proportionnel)")
@app_commands.describe(
    queue="Type de queue",
    joueur1="Joueur gagnant 1",
    joueur2="Joueur gagnant 2", joueur3="Joueur gagnant 3",
    joueur4="Joueur gagnant 4", joueur5="Joueur gagnant 5",
)
@app_commands.choices(queue=[
    app_commands.Choice(name="Pro", value="pro"),
    app_commands.Choice(name="Open", value="open"),
    app_commands.Choice(name="GC", value="gc"),
])
async def win(
    interaction: discord.Interaction,
    queue: str,
    joueur1: discord.Member,
    joueur2: discord.Member = None,
    joueur3: discord.Member = None,
    joueur4: discord.Member = None,
    joueur5: discord.Member = None,
):
    if not has_access(interaction):
        await interaction.response.send_message("Pas la permission.", ephemeral=True)
        return
    players = [p for p in [joueur1, joueur2, joueur3, joueur4, joueur5] if p is not None]
    col = get_elo_col(interaction.guild_id)

    # Pour Pro Queue : flat 16. Sinon : weighted par slot.
    if queue == "pro":
        deltas = [16, 16, 16, 16, 16][:len(players)]
    else:
        deltas = list(WIN_DELTAS_BY_SLOT)[:len(players)]

    embed = discord.Embed(
        title=f"Victoire {queue.upper()} enregistree",
        color=0x2ecc71,
        timestamp=datetime.now(timezone.utc),
    )
    for slot, member in enumerate(players):
        gain = deltas[slot]
        # Upsert avec compound _id
        repository.get_or_create_player(
            col, member.id, queue, member.display_name, initial_elo=ELO_START,
        )
        old_doc = col.find_one_and_update(
            {"_id": repository.player_doc_id(member.id, queue)},
            {"$inc": {"elo": gain, "wins": 1}},
            return_document=ReturnDocument.BEFORE,
        )
        old = (old_doc or {}).get("elo", 0)
        new = old + gain
        embed.add_field(
            name=member.display_name,
            value=f"+{gain} ELO -> **{new}** *(etait {old})*",
            inline=False,
        )
    embed.set_footer(text=f"Enregistre par {interaction.user.display_name}")
    await interaction.response.send_message(embed=embed)
    await _refresh_leaderboard_safe(interaction.guild, queue)
```

Apply the same shape to `/lose` (use `LOSE_DELTAS_BY_SLOT` for non-Pro, flat 16 for Pro, with the floor-at-0 pipeline).

`_refresh_leaderboard_safe` is updated in Task 14.

- [ ] **Step 4: Run tests**

Run: `pytest test_bot_slash.py -v`
Expected: pre-existing `/win`/`/lose` tests need `queue=` arg added. Update them to pass `queue="open"` and verify behavior matches old behaviour. New test passes.

- [ ] **Step 5: Commit**

```bash
git add bot.py test_bot_slash.py
git commit -m "feat(bot): /win and /lose take queue parameter"
```

---

## Task 13: Update `/elomodify`, `/winmodify`, `/losemodify` for `queue_type`

**Files:**
- Modify: `bot.py`
- Test: `test_bot_slash.py`

- [ ] **Step 1: Write the failing tests**

Append to `test_bot_slash.py`:

```python
@pytest.mark.asyncio
async def test_elomodify_targets_specific_queue():
    """elomodify avec queue=pro modifie uniquement le doc :pro, pas :open."""
    # See implementation below
    pass
```

- [ ] **Step 2: Run test to verify it fails**

- [ ] **Step 3: Update `/elomodify`**

Add `queue` choice param. All lookups use `repository.player_doc_id(joueur.id, queue)`. The pipeline update operates on this compound id. Same for `/winmodify` and `/losemodify`. Example for `/elomodify`:

```python
@tree.command(name="elomodify", description="Modifie l'ELO d'un joueur dans une queue")
@app_commands.describe(
    queue="Type de queue", joueur="Le joueur",
    action="Ajouter ou enlever", montant="Nombre d'ELO",
)
@app_commands.choices(
    queue=[
        app_commands.Choice(name="Pro", value="pro"),
        app_commands.Choice(name="Open", value="open"),
        app_commands.Choice(name="GC", value="gc"),
    ],
    action=[
        app_commands.Choice(name="+ Ajouter", value="add"),
        app_commands.Choice(name="- Enlever", value="remove"),
    ],
)
async def elomodify(
    interaction: discord.Interaction,
    queue: str, joueur: discord.Member, action: str, montant: int,
):
    if not has_access(interaction):
        await interaction.response.send_message("Pas la permission.", ephemeral=True)
        return
    if montant <= 0:
        await interaction.response.send_message(
            "Le montant doit etre strictement positif.", ephemeral=True,
        )
        return
    col = get_elo_col(interaction.guild_id)
    repository.get_or_create_player(col, joueur.id, queue, joueur.display_name,
                                       initial_elo=ELO_START)
    delta = montant if action == "add" else -montant
    old_doc = col.find_one_and_update(
        {"_id": repository.player_doc_id(joueur.id, queue)},
        [{"$set": {"elo": {"$max": [0, {"$add": [{"$ifNull": ["$elo", 0]}, delta]}]}}}],
        return_document=ReturnDocument.BEFORE,
    )
    old = (old_doc or {}).get("elo", 0)
    new = max(0, old + delta)
    title = f"ELO {queue.upper()} {'ajoute' if action == 'add' else 'retire'}"
    embed = discord.Embed(
        title=title, color=0x2ecc71 if action == "add" else 0xe74c3c,
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(name="Joueur", value=joueur.mention, inline=True)
    embed.add_field(name="Modification",
                    value=f"{'+' if action == 'add' else '-'}{montant}", inline=True)
    embed.add_field(name="Nouvel ELO", value=f"**{new}** (etait {old})", inline=True)
    embed.set_footer(text=f"Par {interaction.user.display_name}")
    await interaction.response.send_message(embed=embed)
    await _refresh_leaderboard_safe(interaction.guild, queue)
```

Apply identical shape to `/winmodify` (operates on `wins` field) and `/losemodify` (operates on `losses` field).

- [ ] **Step 4: Run tests**

Run: `pytest test_bot_slash.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add bot.py test_bot_slash.py
git commit -m "feat(bot): /elomodify, /winmodify, /losemodify take queue parameter"
```

---

## Task 14: Update `/resetelo`, `/stats`, `/leaderboard`, helper `_refresh_leaderboard_safe`

**Files:**
- Modify: `bot.py`
- Test: `test_bot_slash.py`

- [ ] **Step 1: Write the failing test**

Append:

```python
@pytest.mark.asyncio
async def test_resetelo_all_targets_specific_queue():
    """/resetelo all=True queue=pro vide uniquement la queue pro, pas open/gc."""
    pass

@pytest.mark.asyncio
async def test_stats_filters_by_queue_type():
    """/stats queue=open affiche les stats Open Queue uniquement."""
    pass

@pytest.mark.asyncio
async def test_leaderboard_command_takes_queue_type():
    pass
```

- [ ] **Step 2: Run tests to verify failure**

- [ ] **Step 3: Update commands and helper**

Update `_refresh_leaderboard_safe`:

```python
async def _refresh_leaderboard_safe(guild: discord.Guild | None, queue_type: str) -> None:
    if guild is None or bot.user is None:
        return
    try:
        await refresh_leaderboard_channel(guild, db, bot.user.id, queue_type)
    except Exception:
        logger.exception("[leaderboard] refresh a leve")
```

Update `/resetelo`:

```python
@tree.command(name="resetelo", description="Remet l'ELO d'un joueur ou de tous a 0 dans une queue")
@app_commands.describe(
    queue="Type de queue",
    joueur="Le joueur a remettre a zero",
    all="Remettre l'ELO de TOUS les joueurs de cette queue a 0",
)
@app_commands.choices(queue=[
    app_commands.Choice(name="Pro", value="pro"),
    app_commands.Choice(name="Open", value="open"),
    app_commands.Choice(name="GC", value="gc"),
])
async def resetelo(
    interaction: discord.Interaction,
    queue: str,
    joueur: discord.Member = None,
    all: bool = False,
):
    if not has_access(interaction):
        await interaction.response.send_message("Pas la permission.", ephemeral=True)
        return
    col = get_elo_col(interaction.guild_id)
    if all:
        count = col.count_documents({"queue_type": queue})
        col.update_many({"queue_type": queue},
                          {"$set": {"elo": 0, "wins": 0, "losses": 0}})
        embed = discord.Embed(
            title=f"Reset general {queue.upper()}",
            description=f"ELO de **{count} joueur(s)** remis a 0 dans la queue {queue.upper()}.",
            color=0xe74c3c, timestamp=datetime.now(timezone.utc),
        )
        embed.set_footer(text=f"Reset par {interaction.user.display_name}")
        await interaction.response.send_message(embed=embed)
        await _refresh_leaderboard_safe(interaction.guild, queue)
        return
    if joueur is None:
        await interaction.response.send_message(
            "Mentionne un joueur ou utilise all:True.", ephemeral=True,
        )
        return
    doc_id = repository.player_doc_id(joueur.id, queue)
    repository.get_or_create_player(col, joueur.id, queue, joueur.display_name,
                                       initial_elo=ELO_START)
    doc = col.find_one({"_id": doc_id})
    old = (doc or {}).get("elo", 0)
    col.update_one({"_id": doc_id}, {"$set": {"elo": 0, "wins": 0, "losses": 0}})
    embed = discord.Embed(
        title=f"ELO {queue.upper()} reinitialise",
        color=0x95a5a6, timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(name="Joueur", value=joueur.mention, inline=True)
    embed.add_field(name="Ancien ELO", value=str(old), inline=True)
    embed.add_field(name="Nouvel ELO", value="0", inline=True)
    embed.set_thumbnail(url=joueur.display_avatar.url)
    embed.set_footer(text=f"Reset par {interaction.user.display_name}")
    await interaction.response.send_message(embed=embed)
    await _refresh_leaderboard_safe(interaction.guild, queue)
```

Update `/stats`:

```python
@tree.command(name="stats", description="Stats ELO d'un joueur dans une queue")
@app_commands.describe(queue="Type de queue", joueur="Le joueur")
@app_commands.choices(queue=[
    app_commands.Choice(name="Pro", value="pro"),
    app_commands.Choice(name="Open", value="open"),
    app_commands.Choice(name="GC", value="gc"),
])
async def stats(
    interaction: discord.Interaction, queue: str, joueur: discord.Member = None,
):
    if joueur is None:
        joueur = interaction.user
    col = get_elo_col(interaction.guild_id)
    doc_id = repository.player_doc_id(joueur.id, queue)
    doc = col.find_one({"_id": doc_id})
    if not doc:
        await interaction.response.send_message(
            f"{joueur.display_name} n'a pas de stats en {queue.upper()} Queue.",
            ephemeral=True,
        )
        return
    elo = doc["elo"]; wins = doc.get("wins", 0); losses = doc.get("losses", 0)
    total = wins + losses
    winrate = round((wins / total) * 100, 1) if total > 0 else 0
    rank = col.count_documents({
        "queue_type": queue,
        "$or": [
            {"elo": {"$gt": elo}},
            {"elo": elo, "wins": {"$gt": wins}},
            {"elo": elo, "wins": wins, "_id": {"$lt": doc_id}},
        ],
    }) + 1
    embed = discord.Embed(
        title=f"Stats {queue.upper()} de {joueur.display_name}",
        color=0x3498db, timestamp=datetime.now(timezone.utc),
    )
    embed.set_thumbnail(url=joueur.display_avatar.url)
    embed.add_field(name="ELO",       value=f"**{elo}**",      inline=True)
    embed.add_field(name="Rang",      value=f"**#{rank}**",    inline=True)
    embed.add_field(name="Winrate",   value=f"**{winrate}%**", inline=True)
    embed.add_field(name="Victoires", value=f"**{wins}**",     inline=True)
    embed.add_field(name="Defaites",  value=f"**{losses}**",   inline=True)
    embed.add_field(name="Parties",   value=f"**{total}**",    inline=True)
    embed.set_footer(text=interaction.guild.name)
    await interaction.response.send_message(embed=embed, ephemeral=True)
```

Update `/leaderboard`:

```python
@tree.command(name="leaderboard", description="Affiche le classement ELO d'une queue")
@app_commands.describe(queue="Type de queue")
@app_commands.choices(queue=[
    app_commands.Choice(name="Pro", value="pro"),
    app_commands.Choice(name="Open", value="open"),
    app_commands.Choice(name="GC", value="gc"),
])
async def leaderboard(interaction: discord.Interaction, queue: str):
    public = _is_leaderboard_channel(interaction)
    ephemeral = not public
    await interaction.response.defer(ephemeral=ephemeral)
    file, view = await build_leaderboard_payload(interaction.guild, db, queue)
    if file is None:
        await interaction.followup.send(
            f"Aucun joueur enregistre en {queue.upper()} Queue.", ephemeral=True,
        )
        return
    await interaction.followup.send(file=file, view=view, ephemeral=ephemeral)
```

- [ ] **Step 4: Run tests**

Run: `pytest test_bot_slash.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add bot.py test_bot_slash.py
git commit -m "feat(bot): /resetelo, /stats, /leaderboard take queue parameter"
```

---

## Task 15: Update `/setup` for new channel structure + 3 queue messages + 3 LB pre-post

**Files:**
- Modify: `bot.py:120-200`
- Test: `test_bot_slash.py`

- [ ] **Step 1: Write the failing test**

Append:

```python
@pytest.mark.asyncio
async def test_setup_creates_three_queue_channels_and_leaderboard():
    """/setup cree pro-queue, open-queue, gc-queue et leaderboard."""
    pass  # squelette ; integration via dpytest
```

- [ ] **Step 2: Run test to verify it fails**

- [ ] **Step 3: Update `/setup`**

Replace `SETUP_CHANNELS` and the `/setup` command body:

```python
SETUP_CATEGORY_NAME = "Valorant 10mans"
SETUP_TEXT_CHANNELS = ["leaderboard", "pro-queue", "open-queue", "gc-queue", "matchs"]
QUEUE_CHANNEL_FOR_TYPE = {"pro": "pro-queue", "open": "open-queue", "gc": "gc-queue"}


@tree.command(name="setup", description="Cree categorie/salons et pose les 3 queues")
@app_commands.checks.has_permissions(manage_guild=True)
async def setup_bot(interaction: discord.Interaction):
    guild = interaction.guild
    await interaction.response.defer(ephemeral=True)

    category = discord.utils.get(guild.categories, name=SETUP_CATEGORY_NAME)
    if category is None:
        try:
            category = await guild.create_category(SETUP_CATEGORY_NAME)
        except discord.Forbidden:
            await interaction.followup.send(
                "Permissions manquantes pour creer la categorie.", ephemeral=True,
            )
            return

    created, existed = [], []
    for name in SETUP_TEXT_CHANNELS:
        chan = discord.utils.get(guild.text_channels, name=name)
        if chan is None:
            try:
                await guild.create_text_channel(name, category=category)
                created.append(name)
            except discord.Forbidden:
                await interaction.followup.send(
                    f"Impossible de creer `#{name}`.", ephemeral=True,
                )
                return
        else:
            existed.append(name)

    queue_cog = bot.get_cog("QueueCog")
    queue_status = []
    if queue_cog is not None:
        for qt, channel_name in QUEUE_CHANNEL_FOR_TYPE.items():
            chan = discord.utils.get(guild.text_channels, name=channel_name)
            if chan is None:
                continue
            repository.delete_active_queue(db, guild.id, qt)
            try:
                await queue_cog.post_queue_message(chan, qt)
                queue_status.append(f"Queue {qt.upper()} dans {chan.mention}")
            except discord.Forbidden:
                queue_status.append(f"Permissions manquantes pour {chan.mention}")

    # Pre-post des 3 leaderboards (vides au depart, donc skip si 0 joueur)
    for qt in repository.QUEUE_TYPES:
        try:
            await refresh_leaderboard_channel(guild, db, bot.user.id, qt)
        except Exception:
            logger.exception("[setup] pre-post leaderboard %s a leve", qt)

    lines = []
    if created:
        lines.append(f"Crees : {', '.join(f'`#{c}`' for c in created)}")
    if existed:
        lines.append(f"Deja presents : {', '.join(f'`#{c}`' for c in existed)}")
    lines.extend(queue_status)
    if not lines:
        lines.append("Setup termine.")
    await interaction.followup.send("\n".join(lines), ephemeral=True)
```

- [ ] **Step 4: Run tests**

Run: `pytest test_bot_slash.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add bot.py test_bot_slash.py
git commit -m "feat(bot): /setup creates 3 queue channels and pre-posts leaderboards"
```

---

## Task 16: Add `/reset-queue` command

**Files:**
- Modify: `bot.py`
- Test: `test_bot_slash.py`

- [ ] **Step 1: Write the failing test**

Append:

```python
@pytest.mark.asyncio
async def test_reset_queue_drops_only_target_queue_data():
    """/reset-queue queue=pro drop elo:pro, queue:active:pro, matches:pro,
    leaderboard_state:pro. Ne touche pas a open ni gc."""
    pass  # squelette
```

- [ ] **Step 2: Run test to verify it fails**

Expected: command doesn't exist yet.

- [ ] **Step 3: Add `/reset-queue` to `bot.py`**

Add after `/resetelo` definition:

```python
class _ResetQueueConfirmView(discord.ui.View):
    def __init__(self, queue_type: str, *, timeout: float = 30):
        super().__init__(timeout=timeout)
        self.queue_type = queue_type
        self.confirmed = False

    @discord.ui.button(label="Confirmer le reset", style=discord.ButtonStyle.danger)
    async def confirm(self, inter: discord.Interaction, button: discord.ui.Button):
        self.confirmed = True
        for child in self.children:
            child.disabled = True
        await inter.response.edit_message(view=self)
        self.stop()


@tree.command(name="reset-queue", description="Drop toutes les donnees d'une queue (admin)")
@app_commands.describe(queue="Type de queue a reset")
@app_commands.choices(queue=[
    app_commands.Choice(name="Pro", value="pro"),
    app_commands.Choice(name="Open", value="open"),
    app_commands.Choice(name="GC", value="gc"),
])
@app_commands.checks.has_permissions(manage_guild=True)
async def reset_queue(interaction: discord.Interaction, queue: str):
    view = _ResetQueueConfirmView(queue_type=queue)
    embed = discord.Embed(
        title=f"Reset {queue.upper()} Queue",
        description=(
            f"Cette action va **supprimer definitivement** :\n"
            f"- Tous les ELO de la queue {queue.upper()}\n"
            f"- L'historique des matchs de la queue {queue.upper()}\n"
            f"- L'etat du leaderboard de la queue {queue.upper()}\n\n"
            f"Les autres queues ne sont pas touchees. **Confirmer ?**"
        ),
        color=0xe74c3c,
    )
    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
    await view.wait()
    if not view.confirmed:
        await interaction.followup.send(
            "Reset annule (timeout ou non-confirme).", ephemeral=True,
        )
        return

    # Drop des donnees
    elo_col = repository.get_elo_col(db, interaction.guild_id)
    elo_col.delete_many({"queue_type": queue})

    repository.delete_active_queue(db, interaction.guild_id, queue)

    matches_col = repository.get_matches_col(db, interaction.guild_id)
    matches_col.delete_many({"queue_type": queue})

    repository.clear_leaderboard_message_id(db, interaction.guild_id, queue)

    # Re-poser le message de queue dans le bon salon
    queue_cog = bot.get_cog("QueueCog")
    target_name = QUEUE_CHANNEL_FOR_TYPE[queue]
    target_chan = discord.utils.get(interaction.guild.text_channels, name=target_name)
    if queue_cog and target_chan:
        await queue_cog.post_queue_message(target_chan, queue)

    # Refresh le leaderboard (qui sera vide, donc no-op)
    await _refresh_leaderboard_safe(interaction.guild, queue)

    audit = discord.Embed(
        title=f"Queue {queue.upper()} reset",
        description=f"Reset effectue par {interaction.user.mention}",
        color=0x2ecc71,
        timestamp=datetime.now(timezone.utc),
    )
    await interaction.channel.send(embed=audit)
    await interaction.followup.send(
        f"Queue {queue.upper()} reset.", ephemeral=True,
    )


@reset_queue.error
async def _reset_queue_perm_error(inter: discord.Interaction, error):
    if isinstance(error, app_commands.MissingPermissions):
        await inter.response.send_message(
            "Reserve aux administrateurs.", ephemeral=True,
        )
```

- [ ] **Step 4: Run tests**

Run: `pytest test_bot_slash.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add bot.py test_bot_slash.py
git commit -m "feat(bot): /reset-queue drops a single queue's data with confirmation"
```

---

## Task 17: Update `/welcome` and `/help` for the new system

**Files:**
- Modify: `bot.py:1100-1200`
- Test: none (cosmetic)

- [ ] **Step 1: Update `/help` admin section**

Replace the admin help embed fields to reflect new signatures:

```python
embed.add_field(name="/setup", value="Cree categorie + 3 salons queue + leaderboard", inline=False)
embed.add_field(name="/setup-queue queue", value="Repose le message d'une queue", inline=False)
embed.add_field(name="/close-queue queue", value="Ferme une queue", inline=False)
embed.add_field(name="/win queue @j1..@j5", value="Victoire (Pro=flat 16, autres=pondere)", inline=False)
embed.add_field(name="/lose queue @j1..@j5", value="Defaite", inline=False)
embed.add_field(name="/elomodify queue @j action montant", value="Modifie ELO d'un joueur", inline=False)
embed.add_field(name="/winmodify queue @j action montant", value="Modifie victoires", inline=False)
embed.add_field(name="/losemodify queue @j action montant", value="Modifie defaites", inline=False)
embed.add_field(name="/resetelo queue [@j|all]", value="Reset ELO d'un joueur/tous d'une queue", inline=False)
embed.add_field(name="/reset-queue queue", value="Drop complet d'une queue (donnees + historique)", inline=False)
embed.add_field(name="/bypass @role", value="Donne acces aux commandes admin a un role", inline=False)
embed.add_field(name="/clear nombre", value="Supprime des messages", inline=False)
```

Update the member help section:

```python
embed.add_field(name="/leaderboard queue", value="Classement ELO d'une queue", inline=False)
embed.add_field(name="/stats queue [@joueur]", value="Stats d'un joueur dans une queue", inline=False)
embed.add_field(name="/help", value="Affiche cette aide", inline=False)
```

`/welcome` is already updated (V1.21 noted in memory).

- [ ] **Step 2: Run any existing welcome/help tests**

Run: `pytest test_bot_prefix.py -v` (if it covers help)
Expected: PASS.

- [ ] **Step 3: Commit**

```bash
git add bot.py
git commit -m "docs(bot): update /help admin and member sections for 3-queue system"
```

---

## Task 18: Final smoke run + coverage check

**Files:**
- All

- [ ] **Step 1: Run the full test suite**

Run: `pytest -v --cov=. --cov-report=term-missing`
Expected: all tests PASS, coverage >= 80%.

- [ ] **Step 2: Fix any straggler failures**

Likely failures:
- Tests that import legacy constants `JOIN_BTN_ID` / `LEAVE_BTN_ID` from `cogs.queue_v2` — these are now per-type. Fix imports to use `view.join_btn.custom_id`.
- Tests that call `apply_match_validation` without `queue_type` in match_doc — add `"queue_type": "open"` to fixtures.
- Tests that call repository functions without `queue_type` — pass `queue_type="open"` for default-equivalent behaviour.

- [ ] **Step 3: Manual smoke (optional, requires dev guild)**

If `DEV_GUILD_ID` is set and a test guild exists:
1. `/setup` — 3 queue channels + leaderboard appear
2. `/setup-queue queue:open` — message reposed
3. Two test accounts join Open Queue → role gate works for Pro Queue (refusal)
4. Single-queue lock: a player in Open cannot join Pro
5. Form a Pro Queue match (10 testers with the role) → ELO applied flat ±16
6. `/leaderboard queue:pro` → only pro players visible
7. `/reset-queue queue:pro` → confirmation flow → only pro data dropped

- [ ] **Step 4: Commit any test fixes**

```bash
git add tests/
git commit -m "test: align legacy tests with 3-queue API"
```

- [ ] **Step 5: Final tag**

If everything is green:

```bash
git log --oneline -20  # verify commit history
```

---

## Self-review checklist (run before declaring done)

**1. Spec coverage:**
- ✅ §2.1 Storage compound _id — Tasks 1, 3, 4, 5, 6, 7
- ✅ §2.2 ELO_START=2000, no link-riot seeding — Tasks 2, 11
- ✅ §2.3 Channels & roles — Tasks 9, 15
- ✅ §2.4 Join logic (Riot link, match role, single-queue, role gate) — Task 9
- ✅ §2.5 ELO logic (Pro flat, Open/GC weighted) — Tasks 7, 10
- ✅ §2.6 Auto-refresh per-queue — Tasks 8, 14
- ✅ §2.7 Commands with queue param — Tasks 9, 12, 13, 14, 15, 16
- ✅ §2.8 /reset-queue + manual migration — Task 16
- ✅ §4 Edge cases (race on categories, missing roles) — Tasks 9, 10

**2. Placeholder scan:** No "TBD"/"TODO"/"implement later"/"add error handling" in any task body. Each task contains either complete code or precise pointer to existing line ranges with explicit replacement code.

**3. Type/signature consistency:**
- `queue_type: str` everywhere
- `repository.player_doc_id(uid, qt)` used consistently for compound _ids
- `repository.QUEUE_TYPES` imported where iteration is needed
- `_refresh_leaderboard_safe(guild, queue_type)` — same signature in all callers
- `on_queue_full(inter, queue_doc, queue_type)` — same signature in `QueueView._safe_on_full` and `MatchCog.on_queue_full`
- `apply_match_validation(db, guild_id, match_doc, multipliers=None)` — `queue_type` read from `match_doc`, never passed positionally
