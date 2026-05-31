"""Pro / Semi-Pro Queue Map Pick & Ban Service.

Used after the captain draft completes: cap_a bans first, then alternating
ABABAB (6 bans on 7 maps). The 1 remaining map is the match map.

Module isolated by queue: open and gc queues do NOT call this. They keep
plan_match (random map).

Module structure mirrors services/captain_draft.py for consistency:
  - pure MapBanState dataclass (immutable, testable)
  - MapBanResult dataclass (final outcome) -- added in Task 6
  - MapBanSession class (Discord UI + event loop) -- added in Task 7
  - MapBanCancelledError exception -- added in Task 6
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
    ) -> MapBanState:
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

    def apply_ban(self, map_name: str) -> MapBanState:
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


@dataclass(frozen=True)
class MapBanResult:
    selected_map: str
    ban_history: tuple[tuple[Literal["A", "B"], str], ...]

    @classmethod
    def from_state(cls, state: MapBanState) -> MapBanResult:
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


def _is_admin(user: Any, role_names: tuple[str, ...]) -> bool:
    """Same logic as captain_draft._is_admin: manage_guild OR named role."""
    perms = getattr(user, "guild_permissions", None)
    if perms is not None and getattr(perms, "manage_guild", False):
        return True
    return any(r.name in role_names for r in getattr(user, "roles", []))


def _build_banned_lines(banned: tuple[tuple[Literal["A", "B"], str], ...]) -> str:
    if not banned:
        return "_(none yet)_"
    return "\n".join(
        f"{'🅰️' if side == 'A' else '🅱️'} ~~{m}~~" for side, m in banned
    )


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
            f"- map ban phase, your turn to ban!"
        )
        self.message = await self.prep_channel.send(
            content=content, embed=embed, view=view
        )
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

            async def interaction_check(
                self, interaction: discord.Interaction
            ) -> bool:
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
            if (
                self.state.is_complete
                or interaction.user.id != self.state.current_captain.id
            ):
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
            if (
                self.state.is_complete
                and self._done is not None
                and not self._done.done()
            ):
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
