# The Hub - Valorant 10mans Discord Bot

Valorant 5v5 customs (10mans) matchmaking Discord bot for the **EU Immortal+** community.
Manages **4 parallel queues** (Pro / Semi Pro / Open / GC), automatic team balancing by ELO,
result voting, HenrikDev verification with ACS weighting, and an image-generated leaderboard.

---

## Table of Contents

- [Overview](#overview)
- [Main Features](#main-features)
- [Architecture](#architecture)
- [Local Installation](#local-installation)
- [Environment Variables](#environment-variables)
- [Discord Configuration](#discord-configuration)
- [Commands](#commands)
- [ELO System](#elo-system)
- [HenrikDev Verification (ACS)](#henrikdev-verification-acs)
- [Deployment Kimsufi + PM2](#deployment-kimsufi--pm2)
- [Tests](#tests)
- [Tech Stack](#tech-stack)
- [License](#license)

---

## Overview

Full lifecycle of a match :

1. 10 players click **Join** on the persistent message of their queue (Pro / Semi Pro / Open / GC).
2. At 10/10, the bot closes the queue, **balances the teams** (brute-force optimal across the
   126 5+5 partitions), dynamically creates a `Match #N` category dedicated to the match (monotonic counter persisted in MongoDB), picks a map + lobby host.
3. Visibility permissions are applied directly to the 10 players, then the announcement message
   is posted with 2 vote buttons.
4. Players are voice-moved to the `Waiting Match` channel of their category.
5. After the game, **7/10 votes** are enough to validate (click "Team A" / "Team B").
6. 5 minutes later, the bot queries **HenrikDev** to retrieve the custom stats and
   apply an **ACS** multiplier on ELO gains/losses. If Henrik can't find the
   match within 30 min, flat ELO is applied.
7. The leaderboard is automatically regenerated (PIL image) in `#leaderboard`.

---

## Main Features

### 4 simultaneous queues with role gates

| Queue    | Dedicated channel  | Required role | Waiting voice channel    |
|----------|--------------------|---------------|--------------------------|
| Pro      | `#pro-queue`       | `FL PRO`      | `Waiting Room Pro`       |
| Semi Pro | `#semi-pro-queue`  | `FL SEMIPRO`  | `Waiting Room Semi Pro`  |
| Open     | `#open-queue`      | `FL HUB`      | `Waiting Room Open`      |
| GC       | `#gc-queue`        | `FL GC`       | `Waiting Room GC`        |

- A player can only be in **one queue at a time**.
- The Join / Leave buttons are **persistent** (they survive bot restarts).
- Rejected if no linked Riot account, already in queue, already in an ongoing match (`Match #N` category still active), or missing role gate.
- A player who leaves the server is automatically removed from queues (`on_member_remove`).

### Team balancing

- **Brute-force optimal** algorithm: iterates through the 126 unique 5+5 partitions among 10.
- Minimizes `|sum(team_a) - sum(team_b)|`.
- Tie-breakers: peak diff then ID order (deterministic).
- ELO source used: **server ELO** (`elo_<guild>.elo`), seeded at `/link-riot`.

### Match formation - dynamic categories

- Category created on the fly: `Match #N` where N is assigned by a monotonic Mongo counter. Automatically deleted at the end of the match (vote validated, admin cancel, or failed draft).
- **Discord API parallelization** (since v3): the 10 overwrites + 10 voice moves
  are executed in `asyncio.gather`, reducing formation time from ~7-10s to ~1.5-2s.
- Preserved invariants: permissions are applied to the 10 players **before** sending the announcement message
  (otherwise players don't see the channel).

### Match category lifecycle

- **Creation**: for each match found, a `Match #N` category + `match-preparation` (text), `Team 1`, `Team 2`, `Waiting Match` (voice) channels are dynamically created.
- **Visibility**: direct permissions on the 10 players (deny `@everyone`, allow players + admins + bot) - the `Match #N` role no longer exists as of V3.13.
- **Deletion**: automatic on validated vote, admin cancel (`/match-cancel`), or failed draft. Kept on contested vote until resolved.
- **Contested cleanup**: admin command `/match-cleanup <match_id>` to force deletion after manual resolution.
- **Boot cleanup**: orphaned `Match #N` categories (with no active match in DB) are automatically deleted on bot startup.

### Result vote

- 2 buttons attached to the match message: `Team A won` / `Team B won`.
- **Only the 10 participants** can vote. Vote is modifiable.
- **7/10 majority** → match automatically validated (atomic CAS transition: no double validation).
- **90 min** timeout without majority → status `contested`, ping of the admin role with the current score.
- The `Match #N` category is automatically deleted after validation to free up resources.

### HenrikDev verification + ACS weighting

- ~5 min after validation, the bot queries the HenrikDev API to find the played custom.
- If found → computes an **ACS multiplier** per player (individual performance).
- If not found after 30 min → flat ELO applied (gain/loss = 16 each).
- **Circuit breaker**: if 3 consecutive Henrik calls fail, attempts are
  suspended for 5 min (avoids saturating threads and polluting logs).

### Leaderboard

- 4 distinct leaderboards coexist in `#leaderboard` (one per queue_type).
- PNG image generated via Pillow (`leaderboard_img.py`), 15 players per page.
- Pagination via `<` / `>` buttons, **persistent after restart**.
- Auto-refresh after each ELO change (debounced per-guild).

### Application system (legacy)

- `/welcome` places a persistent **Apply** button in `#verify`.
- Player modal (in-game name, tracker, experience) or Staff modal (role, experience).
- 1h cooldown between two applications per user.
- Admin accepts → `Members` role (or `Coach/Analyst/Manager`) + rename + DM.
- Admin denies → DM with reason + kick.

---

## Architecture

```
bot.py                     # Entry point: slash command tree + prefix commands + applications
cogs/
  ├── queue_v2.py          # QueueCog + QueueView (Join/Leave, 4 queues)
  ├── match.py             # MatchCog + VoteView (formation, vote, Henrik, ELO update, role cleanup)
  └── riot_link.py         # /link-riot, /unlink-riot
services/
  ├── elo_calc.py          # Constants (ELO_START=2000, BASE=16) + pure helpers
  ├── elo_mapping.py       # Numeric tier <-> name conversion (Iron 1 → Radiant)
  ├── elo_updater.py       # apply_match_validation: distributes gains/losses with ACS multipliers
  ├── leaderboard_refresh.py  # Paginated LeaderboardView + debounced refresh
  ├── match_service.py     # Pure logic: build_players, plan_match, find_free_match_prep
  ├── match_verifier.py    # find_henrik_custom_match + compute_acs_multipliers
  ├── repository.py        # Centralized MongoDB access (all collections)
  ├── riot_api.py          # HenrikDev client (1h cache, retry, 404/429 handling)
  ├── riot_id.py           # Riot ID parsing (Name#TAG)
  └── team_balancer.py     # Brute-force optimal across 126 partitions
leaderboard_img.py         # Leaderboard PNG generation (Pillow)
preview_leaderboard.py     # Dev tool: local leaderboard preview
seed_users.py              # Dev tool: populates Mongo with fake players
test_*.py                  # 255 pytest tests
```

### Layers

- **`services/`** = pure logic, testable without Discord or Mongo (except `repository.py`).
- **`cogs/`** = Discord wiring: receives interactions, calls `services/`, applies side-effects.
- **`bot.py`** = entry point + legacy commands (applications, manual `/win`, `/setup`).

### MongoDB collections (`elobot` database)

| Collection                  | Content                                                                 |
|-----------------------------|-------------------------------------------------------------------------|
| `elo_<guild_id>`            | Server ELO per player+queue (`_id = "<uid>:<queue_type>"`)              |
| `riot_accounts_<guild_id>`  | Discord ↔ Riot link (puuid, effective_elo, peak, source)                |
| `queue_<guild_id>`          | Active queues (1 doc per queue_type, `_id = "active:<qt>"`)             |
| `matches_<guild_id>`        | Match history (teams, votes, status, ACS, cleanup flags)                |
| `bypass`                    | Roles with access to admin commands (per-guild)                         |
| `candidature_cooldowns`     | 1h cooldowns for the application system                                 |

---

## Local Installation

### Prerequisites

- Python **3.11+** (tested on 3.12 and 3.13)
- MongoDB (local or Atlas)
- A Discord bot with **Server Members Intent** enabled

### Setup

```bash
git clone <repo-url>
cd "The Hub"

python -m venv venv
source venv/bin/activate         # Windows: venv\Scripts\activate

pip install -r requirements.txt

# Copy the template and fill in the values
cp .env.example .env
# DISCORD_TOKEN, MONGO_URI, HENRIK_API_KEY (optional)

python bot.py
```

---

## Environment Variables

| Name             | Required | Description                                                  |
|------------------|:--------:|--------------------------------------------------------------|
| `DISCORD_TOKEN`  | yes      | Discord bot token                                            |
| `MONGO_URI`      | yes      | MongoDB URI (Atlas or local: `mongodb://localhost:27017`)    |
| `HENRIK_API_KEY` | no       | HenrikDev key (increases rate limit, recommended in prod)    |

The `.env` is **never** deployed via CI (excluded from rsync and `.gitignore`).

---

## Discord Configuration

### Automatic setup

```
/setup
```

Creates the `Valorant 10mans` category and all required text channels
(`leaderboard`, `pro-queue`, `semi-pro-queue`, `open-queue`, `gc-queue`, `matches`), posts the 4 queue
messages, and pre-posts the 4 leaderboards. **Idempotent**, safe to re-run.

### Manual (create if necessary)

**Match categories**: created automatically by the bot (no manual configuration required).
- The bot generates `Match #N` for each match, with `match-preparation`, `Team 1`, `Team 2` and `Waiting Match`.
- Orphaned categories are cleaned up on startup.

**Roles**:
- `In Queue` (given to players in queue)
- `Match #N` *(removed in V3.13 - visibility via per-user overwrites)*
- `Match Host` (given to the lobby leader, removed after 10 min)
- `FL PRO` (Pro queue gate)
- `FL SEMIPRO` (Semi Pro queue gate)
- `FL HUB` (Open queue gate)
- `FL GC` (GC queue gate)
- `FAST LEARNER x The Hub` / `ADMINISTRATORS` / `FL STAFF PRO` / `FL STAFF SEMIPRO` / `FL STAFF GC` (pinged on vote timeout)
- `Members`, `Coach/Analyst/Manager` (application system, optional)

**Auxiliary channels** (optional):
- `verify`: for `/welcome` (application button)
- `candidatures`: for submitted modals
- `elo-adding`: Henrik verification announcements

### Required Discord permissions

The bot needs: `View Channels`, `Send Messages`, `Embed Links`,
`Attach Files`, `Manage Channels` (for `/setup`), `Manage Messages` (for `/clear`),
`Move Members` (for voice moves), `Manage Roles`, `Use Slash Commands`.

---

## Commands

### Players

| Command               | Description                                                              |
|-----------------------|--------------------------------------------------------------------------|
| `/link-riot riot_id:` | Links the Discord account to a Valorant account (EU, Immortal+ required) |
| `/unlink-riot`        | Removes the Riot link                                                    |
| `/leaderboard queue:` | Displays the ranking of the chosen queue (Pro/Semi Pro/Open/GC)          |
| `/stats queue: @player` | ELO stats of a player in the chosen queue (ephemeral)                  |
| `/coinflip`           | Heads or tails                                                           |

### Admin - Setup

| Command                        | Description                                                  |
|--------------------------------|--------------------------------------------------------------|
| `/setup`                       | Creates category + channels + posts the 4 queue messages     |
| `/setup-queue queue:`          | Re-posts a queue message manually                            |
| `/close-queue queue:`          | Closes the active queue of a given type                      |
| `/welcome`                     | Places the **Apply** button in `#verify`                     |
| `/report`                      | Places the report message in the current channel             |
| `/bypass role:`                | Grants admin command access to a role                        |

### Admin - Match

| Command                                        | Description                                          |
|------------------------------------------------|------------------------------------------------------|
| `/match-cancel`                                | Cancels the ongoing match in this channel            |
| `/match-replace leaver: replacement:`         | Replaces a player (ELO diff < 500 required)          |

### Admin - Manual ELO (per queue)

| Command                                               | Description                                  |
|-------------------------------------------------------|----------------------------------------------|
| `/win queue: @p1..@p5`                                | Records a manual win                         |
| `/lose queue: @p1..@p5`                               | Records a manual loss                        |
| `/elomodify queue: @player action: amount:`           | Adds/removes ELO                             |
| `/winmodify queue: @player action: amount:`           | Adds/removes wins                            |
| `/losemodify queue: @player action: amount:`          | Adds/removes losses                          |
| `/resetelo queue: player: \| all:True`                | Resets ELO to 2000 (player or all)           |
| `/reset-queue queue:`                                 | Drops all data of a queue                    |

### Admin - Utilities

| Command            | Description                                  |
|--------------------|----------------------------------------------|
| `/map`             | Random map from the 7 maps                   |
| `/clear amount:`   | Deletes up to 100 messages                   |
| `/help type:`      | List of commands (members or admin)          |

### Prefix commands (legacy)

`!leaderboard`, `!stats`, `!win`, `!lose`, `!map` - behavior equivalent
to the slash commands but with prefix syntax. Kept for backward compatibility.

---

## ELO System

- **Starting ELO**: `2000`
- **Zero-sum base**: gain = loss = `16` per match (constant formula regardless of the average).
- **ACS weighting**: multiplier computed per player from HenrikDev stats.
- **Floor**: a loser's ELO never drops below `0`.
- **Wins / losses**: incremented automatically on each validation.
- **Per-queue**: each player has an independent ELO per queue (Pro / Semi Pro / Open / GC), via
  a compound `_id = "<uid>:<queue_type>"` in the `elo_<guild_id>` collection.

### Immortal+ restriction

The bot refuses to link an account whose `max(peak_elo, current_mmr) < 2400`.
Effective ELO computation:
- If peak is < 6 months old → peak used.
- Otherwise → average of MMR over the last 6 months.
- Fallback to peak if no recent matches.

---

## HenrikDev Verification (ACS)

| Constant                             | Value   | Effect                                             |
|--------------------------------------|---------|----------------------------------------------------|
| `HENRIK_VERIFY_DELAY_MINUTES`        | 5 min   | First attempt to fetch the custom                  |
| `HENRIK_VERIFY_TIMEOUT_MINUTES`      | 30 min  | Give up → flat ELO (16/16)                         |
| `HENRIK_CIRCUIT_FAIL_THRESHOLD`      | 3       | Consecutive failures before circuit opens          |
| `HENRIK_CIRCUIT_OPEN_MINUTES`        | 5 min   | Duration of Henrik call suspension                 |

The ACS multiplier rewards top fraggers and penalizes bottom fraggers **within
their own team**, while keeping the sum zero-sum.

---

## Deployment Kimsufi + PM2

The bot runs on a **Kimsufi server** (OVH) via PM2, automatically deployed by
**GitHub Actions** on every push to `main`. Full details in `DEPLOY.md`.

### CI/CD pipeline

- `.github/workflows/ci.yml`: `pytest` on every PR + every push (except `main`).
- `.github/workflows/deploy.yml`: on push to `main` → tests → rsync to Kimsufi →
  `pip install` → `pm2 reload vrc-bot`.

### Useful PM2 commands

```bash
pm2 status
pm2 logs vrc-bot --lines 100
pm2 restart vrc-bot
pm2 restart vrc-bot --update-env   # after editing the .env
pm2 monit
```

### Edit the `.env` in prod

```bash
ssh ubuntu@<kimsufi-host>
nano /home/ubuntu/vrc-bot/.env
pm2 restart vrc-bot --update-env
```

---

## Tests

```bash
pip install -r requirements-test.txt
pytest
```

**255 automated tests**, run in ~10 s. Coverage:

| Module                         | Aspects tested                                                 |
|--------------------------------|----------------------------------------------------------------|
| `test_elo_calc.py`             | ELO + ACS formulas                                             |
| `test_elo_updater.py`          | Gain/loss distribution in DB                                   |
| `test_team_balancer.py`        | Brute-force algorithm and tie-breakers                         |
| `test_match_service.py`        | `build_players`, `plan_match`, `find_free_match_prep`          |
| `test_match_cog.py`            | Match formation integration (Discord mocked)                   |
| `test_vote.py`                 | Vote, CAS transitions, timeout, ELO update                     |
| `test_queue_v2.py`             | Repository + QueueView + ephemeral confirmation                |
| `test_riot_api.py`             | HenrikDev client (HTTP mocks, cache, 404/429)                  |
| `test_riot_link.py`            | `/link-riot` cog + Immortal+ check                             |
| `test_riot_id.py`              | Riot ID parsing                                                |
| `test_pagination.py`           | Leaderboard pagination logic                                   |
| `test_repository_helpers.py`   | Mongo helpers (compound id, CAS)                               |
| `test_bot_slash.py`            | Slash commands + `/setup` + `/win` (Discord mocked)            |
| `test_bot_prefix.py`           | Legacy prefix commands (dpytest)                               |

---

## Tech Stack

- **Python** 3.11+
- **discord.py** 2.3.2
- **pymongo** 4.6+ (with `retryWrites`, `retryReads`, `serverSelectionTimeoutMS=5000`)
- **Pillow** 10+ (leaderboard PNG rendering)
- **requests** 2.31+ (HenrikDev client)
- **python-dotenv** 1.0+
- **pytest** 8+ / **pytest-asyncio** / **mongomock** / **dpytest** / **faker** (tests)

---

## License

MIT - see [LICENSE](LICENSE).
