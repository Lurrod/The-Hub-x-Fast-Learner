"""
HenrikDev API client (unofficial Valorant).

Endpoints used:
  - GET /valorant/v1/account/{name}/{tag}
  - GET /valorant/v2/mmr/{region}/{name}/{tag}
  - GET /valorant/v1/mmr-history/{region}/{name}/{tag}

Doc: https://docs.henrikdev.xyz/valorant.html

Without an API key: ~30 req/min. With a key (env HENRIK_API_KEY): higher.
We cache responses for 1h to limit calls.
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime, UTC
from typing import Any, Final
from urllib.parse import quote

import requests


BASE_URL: Final[str] = "https://api.henrikdev.xyz/valorant"
DEFAULT_TIMEOUT: Final[int] = 10
CACHE_TTL_SECONDS: Final[int] = 3600  # 1h
RETRY_ATTEMPTS: Final[int] = 3  # 1 initial attempt + 2 retries
RETRY_BACKOFF_BASE: Final[float] = 1.0  # delays: 1s, 2s, 4s


VALID_REGIONS: Final[frozenset[str]] = frozenset({"eu", "na", "ap", "kr", "latam", "br"})


class RiotApiError(Exception):
    """Generic client error."""


class PlayerNotFoundError(RiotApiError):
    """Name#tag does not exist on the Riot side."""


class RateLimitedError(RiotApiError):
    """API returned 429."""


@dataclass(frozen=True)
class Account:
    puuid: str
    name: str
    tag: str
    region: str


@dataclass(frozen=True)
class CurrentMMR:
    elo: int
    tier: int
    tier_name: str
    ranking_in_tier: int
    mmr_change_last: int


@dataclass(frozen=True)
class HistoricalMatch:
    elo: int
    tier: int
    date: datetime
    mmr_change: int


@dataclass(frozen=True)
class MatchPlayerStats:
    puuid: str
    name: str
    tag: str
    team: str  # "Red" or "Blue"
    score: int  # total combat score
    kills: int
    deaths: int
    assists: int
    agent: str = ""  # Valorant agent name, e.g. "Jett" / "KAY/O" / "Sage"
    # Extended (Rating 2.0) — default to 0 so legacy parse paths still build.
    damage_made: int = 0
    damage_received: int = 0
    headshots: int = 0
    bodyshots: int = 0
    legshots: int = 0
    multikills_2k: int = 0
    multikills_3k: int = 0
    multikills_4k: int = 0
    multikills_5k: int = 0
    first_kills: int = 0
    first_deaths: int = 0
    kast_rounds: int = 0


@dataclass(frozen=True)
class MatchSummary:
    matchid: str
    mode: str  # "Custom Game", "Competitive", etc.
    map_name: str
    started_at: datetime
    rounds_played: int
    players: tuple[MatchPlayerStats, ...]
    rounds_red: int
    rounds_blue: int


# -- Simple TTL cache ----------------------------------------------
class _TTLCache:
    """Thread-safe TTL cache: protects _store from concurrent access
    from multiple `asyncio.to_thread`."""

    def __init__(self, ttl: int) -> None:
        self._ttl = ttl
        self._store: dict[str, tuple[float, Any]] = {}
        self._lock = threading.Lock()

    def get(self, key: str) -> Any | None:
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return None
            ts, value = entry
            if time.time() - ts > self._ttl:
                # Pop with default: avoids KeyError if another thread
                # has already deleted the key in the meantime.
                self._store.pop(key, None)
                return None
            return value

    def set(self, key: str, value: Any) -> None:
        with self._lock:
            self._store[key] = (time.time(), value)

    def clear(self) -> None:
        with self._lock:
            self._store.clear()


# -- Client --------------------------------------------------------
class HenrikDevClient:
    def __init__(
        self,
        api_key: str | None = None,
        session: requests.Session | None = None,
        cache_ttl: int = CACHE_TTL_SECONDS,
    ) -> None:
        self.api_key = api_key or os.environ.get("HENRIK_API_KEY")
        self.session = session or requests.Session()
        self._cache = _TTLCache(cache_ttl)
        # `requests.Session` is not safe for concurrent multi-thread
        # calls (the urllib3 connection pool can get corrupted).
        # The bot dispatches several Henrik calls via `asyncio.to_thread`,
        # so we serialize the requests via this lock. Perf impact is
        # negligible (Henrik volume < 1 req/sec on this bot).
        self._session_lock = threading.Lock()

    def _headers(self) -> dict[str, str]:
        h = {"Accept": "application/json"}
        if self.api_key:
            h["Authorization"] = self.api_key
        return h

    def _get(self, path: str, *, cache: bool = True) -> dict[str, Any]:
        """GET HenrikDev. If `cache=False`, neither reads nor writes the TTL cache.

        Useful for endpoints that must stay fresh (polling the match
        history to detect a recent custom: with a 1h cache, the 1st retry
        returns the stale 'not indexed yet' response forever)."""
        if cache:
            cached = self._cache.get(path)
            if cached is not None:
                return cached

        url = f"{BASE_URL}{path}"
        last_err: Exception | None = None
        # Retry only on network errors and 5xx (transient).
        # 404, 429, other 4xx: no retry (deterministic failure).
        for attempt in range(RETRY_ATTEMPTS):
            try:
                with self._session_lock:
                    resp = self.session.get(url, headers=self._headers(), timeout=DEFAULT_TIMEOUT)
            except requests.RequestException as e:
                last_err = e
                if attempt < RETRY_ATTEMPTS - 1:
                    time.sleep(RETRY_BACKOFF_BASE * (2**attempt))
                    continue
                raise RiotApiError(f"Network error after {RETRY_ATTEMPTS} attempts: {e}") from e

            if resp.status_code == 404:
                raise PlayerNotFoundError(f"Player not found: {path}")
            if resp.status_code == 429:
                raise RateLimitedError("HenrikDev returned 429 (rate limited)")
            if 500 <= resp.status_code < 600:
                last_err = RiotApiError(f"HTTP {resp.status_code}: {resp.text[:200]}")
                if attempt < RETRY_ATTEMPTS - 1:
                    time.sleep(RETRY_BACKOFF_BASE * (2**attempt))
                    continue
                raise last_err
            if resp.status_code >= 400:
                raise RiotApiError(f"HTTP {resp.status_code}: {resp.text[:200]}")

            try:
                data = resp.json()
            except ValueError as e:
                raise RiotApiError(f"Non-JSON response: {e}") from e

            if data.get("status") and data["status"] >= 400:
                # If HenrikDev returns an application-level 5xx status, we retry too.
                if 500 <= int(data["status"]) < 600 and attempt < RETRY_ATTEMPTS - 1:
                    last_err = RiotApiError(f"API status {data['status']}")
                    time.sleep(RETRY_BACKOFF_BASE * (2**attempt))
                    continue
                raise RiotApiError(f"API status {data['status']}")

            if cache:
                self._cache.set(path, data)
            return data

        # Normally unreachable, but a safety net.
        raise RiotApiError(
            f"_get: failure after {RETRY_ATTEMPTS} attempts. last_err={last_err}",
        )

    # -- Public endpoints ------------------------------------------
    def get_account(self, name: str, tag: str) -> Account:
        data = self._get(f"/v1/account/{quote(name, safe='')}/{quote(tag, safe='')}")
        d = data.get("data", {})
        return Account(
            puuid=d.get("puuid", ""),
            name=d.get("name", name),
            tag=d.get("tag", tag),
            region=d.get("region", "eu"),
        )

    def get_current_mmr(self, region: str, name: str, tag: str) -> CurrentMMR:
        if region not in VALID_REGIONS:
            raise ValueError(f"Invalid region: {region}")
        data = self._get(f"/v2/mmr/{region}/{quote(name, safe='')}/{quote(tag, safe='')}")
        c = data.get("data", {}).get("current_data", {})
        return CurrentMMR(
            elo=int(c.get("elo") or 0),
            tier=int(c.get("currenttier") or 0),
            tier_name=str(c.get("currenttierpatched") or "Unrated"),
            ranking_in_tier=int(c.get("ranking_in_tier") or 0),
            mmr_change_last=int(c.get("mmr_change_to_last_game") or 0),
        )

    def get_mmr_history(
        self,
        region: str,
        name: str,
        tag: str,
    ) -> list[HistoricalMatch]:
        if region not in VALID_REGIONS:
            raise ValueError(f"Invalid region: {region}")
        data = self._get(f"/v1/mmr-history/{region}/{quote(name, safe='')}/{quote(tag, safe='')}")
        out: list[HistoricalMatch] = []
        for entry in data.get("data", []):
            ts = entry.get("date_raw")
            if ts is None:
                continue
            out.append(
                HistoricalMatch(
                    elo=int(entry.get("elo") or 0),
                    tier=int(entry.get("currenttier") or 0),
                    date=datetime.fromtimestamp(int(ts), tz=UTC),
                    mmr_change=int(entry.get("mmr_change_to_last_game") or 0),
                )
            )
        return out

    def get_match_history(
        self,
        region: str,
        name: str,
        tag: str,
        *,
        size: int = 5,
        mode: str | None = None,
    ) -> list[MatchSummary]:
        """Fetch the recent matches of a player. `mode` filters on the API side ('custom', etc.).

        IMPORTANT: the query parameter is `mode=`, not `filter=`. HenrikDev still
        accepts `filter=` in the URL for backward compatibility, but silently IGNORES it
        (returns the unfiltered history). Verified against the API in May 2026:
        `?filter=custom` -> 10 Competitive matches; `?mode=custom` -> 10 Custom Games.
        Without this correct param, `find_henrik_custom_match` never finds the custom
        if the leader has played >= 10 other modes since."""
        if region not in VALID_REGIONS:
            raise ValueError(f"Invalid region: {region}")
        safe_name = quote(name, safe="")
        safe_tag = quote(tag, safe="")
        path = f"/v3/matches/{region}/{safe_name}/{safe_tag}?size={int(size)}"
        if mode:
            path += f"&mode={quote(str(mode), safe='')}"
        # No cache: this endpoint is called in a loop to detect the
        # appearance of a recent custom. With the 1h TTL, the 1st retry
        # would forever return the stale "not indexed yet" response.
        data = self._get(path, cache=False)
        return [_parse_match(entry) for entry in data.get("data", [])]

    def get_match_details(self, matchid: str) -> MatchSummary:
        """Full detail of a match from its id."""
        data = self._get(f"/v2/match/{quote(matchid, safe='')}")
        d = data.get("data", {})
        if not d:
            raise RiotApiError(f"Match {matchid}: empty payload")
        return _parse_match(d)

    def clear_cache(self) -> None:
        self._cache.clear()


_TRADE_WINDOW_MS = 5000


def _accumulate_round_events(
    rounds: list[dict],
    all_players: list[dict],
) -> dict[str, dict[str, int]]:
    """Walk the Henrik rounds[] array and return per-puuid counters.

    Counters per puuid:
      first_kills, first_deaths,
      multikills_2k / 3k / 4k / 5k,
      kast_rounds

    Notes:
      - "kast" credits a round when the player has a kill, an assist,
        survived, or died-but-was-traded (teammate killed the
        attacker within _TRADE_WINDOW_MS).
      - Round events with ``kill_time_in_round=None`` are ignored.
    """
    team_of: dict[str, str] = {
        p.get("puuid", ""): str(p.get("team", ""))
        for p in all_players
    }
    counters: dict[str, dict[str, int]] = {
        puuid: {
            "first_kills": 0, "first_deaths": 0,
            "multikills_2k": 0, "multikills_3k": 0,
            "multikills_4k": 0, "multikills_5k": 0,
            "kast_rounds": 0,
        }
        for puuid in team_of
        if puuid
    }

    for rnd in rounds or []:
        events = rnd.get("kill_events", []) or []
        events_sorted = sorted(
            (e for e in events if e.get("kill_time_in_round") is not None),
            key=lambda e: e["kill_time_in_round"],
        )

        # First kill / first death.
        if events_sorted:
            first = events_sorted[0]
            fk = first.get("killer_puuid", "")
            fd = first.get("victim_puuid", "")
            if fk in counters:
                counters[fk]["first_kills"] += 1
            if fd in counters:
                counters[fd]["first_deaths"] += 1

        # Per-round kills per player -> multikills.
        kills_per_player: dict[str, int] = {}
        for e in events_sorted:
            k = e.get("killer_puuid", "")
            if k:
                kills_per_player[k] = kills_per_player.get(k, 0) + 1
        for puuid, n in kills_per_player.items():
            if puuid not in counters:
                continue
            if n == 2:
                counters[puuid]["multikills_2k"] += 1
            elif n == 3:
                counters[puuid]["multikills_3k"] += 1
            elif n == 4:
                counters[puuid]["multikills_4k"] += 1
            elif n >= 5:
                counters[puuid]["multikills_5k"] += 1

        # KAST: for each player, did they K / A / S / T in this round?
        killers = {e.get("killer_puuid", "") for e in events_sorted}
        victims_with_time: dict[str, int] = {}
        assistants_in_round: set[str] = set()
        for e in events_sorted:
            v = e.get("victim_puuid", "")
            t = e.get("kill_time_in_round")
            if v and t is not None:
                victims_with_time[v] = t
            for a in e.get("assistants", []) or []:
                ap = a.get("assistant_puuid", "")
                if ap:
                    assistants_in_round.add(ap)

        for puuid in counters:
            # K
            if puuid in killers:
                counters[puuid]["kast_rounds"] += 1
                continue
            # A
            if puuid in assistants_in_round:
                counters[puuid]["kast_rounds"] += 1
                continue
            # S (not a victim this round)
            if puuid not in victims_with_time:
                counters[puuid]["kast_rounds"] += 1
                continue
            # T (death was traded by a teammate within the window)
            death_time = victims_with_time[puuid]
            team = team_of.get(puuid, "")
            attacker = next(
                (e.get("killer_puuid", "") for e in events_sorted
                 if e.get("victim_puuid") == puuid),
                "",
            )
            traded = False
            for e in events_sorted:
                t = e.get("kill_time_in_round")
                if t is None or t < death_time:
                    continue
                if t - death_time > _TRADE_WINDOW_MS:
                    break
                if (
                    e.get("victim_puuid") == attacker
                    and team_of.get(e.get("killer_puuid", "")) == team
                ):
                    traded = True
                    break
            if traded:
                counters[puuid]["kast_rounds"] += 1

    return counters


def _parse_match(entry: dict) -> MatchSummary:
    meta = entry.get("metadata", {}) or {}
    teams = entry.get("teams", {}) or {}
    players = (entry.get("players", {}) or {}).get("all_players", []) or []

    started_raw = meta.get("game_start") or 0
    started_at = datetime.fromtimestamp(int(started_raw), tz=UTC)

    rounds_data = entry.get("rounds", []) or []
    counters = _accumulate_round_events(rounds_data, players)

    parsed_players: list[MatchPlayerStats] = []
    for p in players:
        stats = p.get("stats", {}) or {}
        puuid = p.get("puuid", "")
        c = counters.get(puuid, {})
        parsed_players.append(
            MatchPlayerStats(
                puuid=puuid,
                name=p.get("name", ""),
                tag=p.get("tag", ""),
                team=str(p.get("team", "")),
                score=int(stats.get("score") or 0),
                kills=int(stats.get("kills") or 0),
                deaths=int(stats.get("deaths") or 0),
                assists=int(stats.get("assists") or 0),
                agent=str(p.get("character", "") or ""),
                damage_made=int(p.get("damage_made") or 0),
                damage_received=int(p.get("damage_received") or 0),
                headshots=int(stats.get("headshots") or 0),
                bodyshots=int(stats.get("bodyshots") or 0),
                legshots=int(stats.get("legshots") or 0),
                multikills_2k=c.get("multikills_2k", 0),
                multikills_3k=c.get("multikills_3k", 0),
                multikills_4k=c.get("multikills_4k", 0),
                multikills_5k=c.get("multikills_5k", 0),
                first_kills=c.get("first_kills", 0),
                first_deaths=c.get("first_deaths", 0),
                kast_rounds=c.get("kast_rounds", 0),
            )
        )

    return MatchSummary(
        matchid=str(meta.get("matchid", "")),
        mode=str(meta.get("mode", "")),
        map_name=str(meta.get("map", "")),
        started_at=started_at,
        rounds_played=int(meta.get("rounds_played") or 0),
        players=tuple(parsed_players),
        rounds_red=int((teams.get("red") or {}).get("rounds_won") or 0),
        rounds_blue=int((teams.get("blue") or {}).get("rounds_won") or 0),
    )


def _parse_henrik_match(payload: dict) -> MatchSummary:
    """Unwrap the outer ``{"data": {...}}`` envelope and parse the match.

    Backwards-compatible alias used by tests that work with the full
    Henrik API response shape rather than the already-unwrapped entry.
    """
    return _parse_match(payload.get("data", {}) or {})
