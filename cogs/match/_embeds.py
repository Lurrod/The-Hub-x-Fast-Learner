"""Discord embed builders for the match flow.

3 builders: initial embed (from MatchPlan), embed after vote (from the
match doc), recap ELO embed post-validation.
"""

from __future__ import annotations

from datetime import UTC, datetime

import discord

from cogs.match._constants import MAJORITY_THRESHOLD
from cogs.queue_v2 import QUEUE_LABELS
from services.elo_updater import MatchEloOutcome


# ── Embed: from MatchPlan (initial publication) ───────────────────
def build_match_embed(
    plan,
    guild_name: str,
    queue_type: str = "open",
) -> discord.Embed:
    teams = plan.teams
    map_name = plan.map_name
    leader = plan.lobby_leader
    category = plan.category_name

    qt_label = QUEUE_LABELS.get(queue_type, queue_type.upper())
    embed = discord.Embed(
        title=f"🎯 [{qt_label}] Match found!",
        description=f"**Map:** {map_name}\n**Lobby host:** <@{leader.id}> ({leader.name})",
        color=0x5865F2,
        timestamp=datetime.now(UTC),
    )

    a_lines = "\n".join(f"• <@{p.id}> ({p.elo})" for p in teams.team_a)
    b_lines = "\n".join(f"• <@{p.id}> ({p.elo})" for p in teams.team_b)
    embed.add_field(name=f"🔵 Team A ({teams.total_a})", value=a_lines, inline=True)
    embed.add_field(name=f"🔴 Team B ({teams.total_b})", value=b_lines, inline=True)
    embed.add_field(
        name="Balance",
        value=f"diff `{teams.elo_diff}` · peak diff `{teams.peak_diff}`",
        inline=False,
    )

    if category:
        embed.add_field(
            name="🔊 Voice",
            value=f"**Team A** -> `{category} / Team 1`\n**Team B** -> `{category} / Team 2`",
            inline=False,
        )
    else:
        embed.add_field(
            name="🔊 Voice",
            value="⚠️ Discord error while creating the match category.",
            inline=False,
        )

    embed.add_field(
        name="🗳️ Votes",
        value=f"Team A: **0** / Team B: **0** *(majority: {MAJORITY_THRESHOLD}/10)*",
        inline=False,
    )

    embed.set_footer(text=f"{guild_name} · Report below which team won the game")
    return embed


# ── Embed: from match_doc (vote update) ───────────────────────────
def build_match_embed_from_doc(doc: dict, guild_name: str) -> discord.Embed:
    team_a = doc["team_a"]
    team_b = doc["team_b"]
    map_name = doc["map"]
    leader_id = doc["lobby_leader_id"]
    leader_name = next(
        (p["name"] for p in (team_a + team_b) if str(p["id"]) == str(leader_id)),
        "?",
    )
    category = doc.get("category_name")
    status = doc.get("status", "pending")
    votes = doc.get("votes", {})
    count_a = sum(1 for v in votes.values() if v == "a")
    count_b = sum(1 for v in votes.values() if v == "b")

    queue_type = doc.get("queue_type", "open")
    qt_label = QUEUE_LABELS.get(queue_type, queue_type.upper())
    qt_prefix = f"[{qt_label}] "

    if status == "validated_a":
        title, color, footer_extra = f"🏆 {qt_prefix}Team A won!", 0x2ECC71, "Match validated"
    elif status == "validated_b":
        title, color, footer_extra = f"🏆 {qt_prefix}Team B won!", 0xE74C3C, "Match validated"
    elif status == "contested":
        title, color, footer_extra = (
            f"⚠️ {qt_prefix}Match awaiting admin",
            0xE67E22,
            "Vote in timeout",
        )
    else:
        title, color, footer_extra = (
            f"🎯 {qt_prefix}Match finished - Report the winner",
            0x5865F2,
            "Click the team that won the game",
        )

    embed = discord.Embed(
        title=title,
        color=color,
        timestamp=datetime.now(UTC),
        description=f"**Map:** {map_name}\n**Lobby host:** <@{leader_id}> ({leader_name})",
    )

    sum_a = sum(p["elo"] for p in team_a)
    sum_b = sum(p["elo"] for p in team_b)
    a_lines = "\n".join(f"• <@{p['id']}> ({p['elo']})" for p in team_a)
    b_lines = "\n".join(f"• <@{p['id']}> ({p['elo']})" for p in team_b)
    embed.add_field(name=f"🔵 Team A ({sum_a})", value=a_lines, inline=True)
    embed.add_field(name=f"🔴 Team B ({sum_b})", value=b_lines, inline=True)
    embed.add_field(name="Balance", value=f"diff `{abs(sum_a - sum_b)}`", inline=False)

    if category:
        embed.add_field(
            name="🔊 Voice",
            value=f"**Team A** -> `{category} / Team 1`\n**Team B** -> `{category} / Team 2`",
            inline=False,
        )

    embed.add_field(
        name="🗳️ Votes",
        value=f"Team A: **{count_a}** / Team B: **{count_b}** *(majority: {MAJORITY_THRESHOLD}/10)*",
        inline=False,
    )

    embed.set_footer(text=f"{guild_name} · {footer_extra}")
    return embed


# ── Embed: ELO update recap post-validation ───────────────────────
def build_elo_changes_embed(
    outcome: MatchEloOutcome,
    match_doc: dict,
    guild_name: str,
) -> discord.Embed:
    status = match_doc.get("status")
    if status == "validated_a":
        winner_label, color = "Team A", 0x2ECC71
    else:
        winner_label, color = "Team B", 0xE74C3C

    weighted = outcome.weighted
    title = f"🏆 {winner_label} wins! ELO updated{' (ACS weighting)' if weighted else ''}"
    desc_extra = (
        "\nACS weighting applied via HenrikDev stats."
        if weighted
        else "\n⚠️ Riot match not found on HenrikDev - flat ELO applied."
    )

    embed = discord.Embed(
        title=title,
        description=(
            f"Match avg ELO: **{outcome.avg_elo}**\n"
            f"Winner base: **+{outcome.gain}**\n"
            f"Loser base: **-{outcome.loss}**"
            f"{desc_extra}"
        ),
        color=color,
        timestamp=datetime.now(UTC),
    )

    winners = [c for c in outcome.changes if c.win]
    losers = [c for c in outcome.changes if not c.win]

    def _fmt(c):
        sign = "+" if c.delta >= 0 else ""
        mult = f" ×{c.multiplier:.2f}" if weighted else ""
        return f"• <@{c.user_id}>{mult}  {sign}{c.delta}  →  **{c.new_elo}** *(was {c.old_elo})*"

    w_lines = "\n".join(_fmt(c) for c in winners)
    l_lines = "\n".join(_fmt(c) for c in losers)
    embed.add_field(name="🟢 Winners", value=w_lines or "-", inline=False)
    embed.add_field(name="🔴 Losers", value=l_lines or "-", inline=False)
    embed.set_footer(text=guild_name)
    return embed
