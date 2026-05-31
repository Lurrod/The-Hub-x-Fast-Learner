"""Constants for the cogs.match package.

Extracted to avoid cyclic imports between _embeds, _vote and _cog,
and to serve as a single configuration point for the match flow.
"""

from __future__ import annotations

from typing import Final


# Maximum ELO gap between the leaving player and the replacement. Beyond,
# /match-replace is refused: the teams of the ongoing match would be too
# unbalanced for the result to reflect real player performance.
MAX_REPLACE_ELO_DIFF: Final[int] = 500
MATCH_HOST_ROLE_NAME: Final[str] = "Match Host"

VOTE_A_BTN_ID: Final[str] = "vote_v2:a"
VOTE_B_BTN_ID: Final[str] = "vote_v2:b"
MAJORITY_THRESHOLD: Final[int] = 7
VOTE_TIMEOUT_MINUTES: Final[int] = 90
HENRIK_VERIFY_DELAY_MINUTES: Final[int] = 5  # first Henrik attempt at 5 min
HENRIK_VERIFY_TIMEOUT_MINUTES: Final[int] = 30  # give up Henrik and flat ELO at 30 min

# Safety net: a "contested" match unresolved by admin blocks the 10
# players in the find_active_match_for_player gate. /win and /lose
# distribute ELO but do not touch the match doc -> we auto-expire
# to avoid a distracted admin freezing 10 players indefinitely.
CONTESTED_EXPIRY_HOURS: Final[int] = 24

# Henrik circuit breaker: if N consecutive calls fail, we suspend
# attempts for T minutes to avoid saturating the threads (each call =
# ~12s with retries) and polluting the logs.
HENRIK_CIRCUIT_FAIL_THRESHOLD: Final[int] = 3
HENRIK_CIRCUIT_OPEN_MINUTES: Final[int] = 5

# Target roles for the admin ping (first found wins).
ADMIN_ROLE_NAMES: Final[tuple[str, ...]] = (
    "FAST LEARNER x The Hub",
    "ADMINISTRATORS",
    "FL STAFF PRO",
    "FL STAFF SEMIPRO",
    "FL STAFF GC",
)

# Staff "viewer" roles: see and participate in match categories
# (same overwrites as the 10 players), but without `manage_channels`.
# Distinct from ADMIN_ROLE_NAMES which grants admin powers (draft
# cancel, ping, channel management).
# FL CAST lives here (not in QUEUE_ROLE_GATES): casters see all match
# channels and can join the voice rooms to commentate, but they do not
# queue up as players.
MATCH_VIEWER_ROLE_NAMES: Final[tuple[str, ...]] = (
    "FAST LEARNER x The Hub",
    "ADMINISTRATORS",
    "FL STAFF PRO",
    "FL STAFF SEMIPRO",
    "FL STAFF GC",
    "FL CAST",
)

# "Spectator" roles: see the category + read history, but cannot
# join voice channels or send messages.
MATCH_SPECTATOR_ROLE_NAMES: Final[tuple[str, ...]] = ("Members",)
