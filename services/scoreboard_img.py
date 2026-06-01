"""
Per-match scoreboard image generation via Pillow.

Posted by the bot in the queue's results channel (pro-results / etc.)
once Henrik finds the custom and we have the match stats.

Modern layout:
    top strip      — queue label (left) + map name (right)
    score band     — big team labels and rounds
    column headers — PLAYER  KILLS  DEATHS  ASSISTS  ACS  ELO
    rows           — agent icon + Discord display name + stats per column
    footer         — "Play'IT Matchmaking Bot"

Agent icons are loaded from `assets/agents/<AgentName>.png` (committed to
the repo). "KAY/O" maps to `KAY_O.png`. Missing icon → grey placeholder.

Input format (per player, both teams):
    {
        "name":    str,          # Discord display name
        "kills":   int,
        "deaths":  int,
        "assists": int,
        "acs":     int,
        "elo":     int,
        "agent":   str | None,   # Valorant agent name, optional
    }
"""

from __future__ import annotations

import os
from collections import OrderedDict
from collections.abc import Iterable, Mapping, Sequence
from io import BytesIO
from typing import Any

from PIL import Image, ImageDraw, ImageFont

# ── Layout ────────────────────────────────────────────────────────
WIDTH = 1500
TOP_STRIP_BAND = 38
SCORE_BAND = 130
COLUMN_HEADER_BAND = 42
ROW_HEIGHT = 70
FOOTER_BAND = 44
PLAYERS_PER_TEAM = 5

COL_W = WIDTH // 2
COL_A_X0 = 0
COL_B_X0 = COL_W

# Per-row positions (within a 750px column)
COL_PAD_LEFT = 22
AGENT_ICON_SIZE = 50
X_AGENT_REL = COL_PAD_LEFT
X_NAME_REL = X_AGENT_REL + AGENT_ICON_SIZE + 14  # right of agent icon
X_K = 380
X_D = 460
X_A = 540
X_ACS = 625
X_ELO_RIGHT = COL_W - 30  # right-aligned

# ── Colors ────────────────────────────────────────────────────────
BG = (12, 16, 22)
TOP_STRIP_BG = (16, 21, 28)
ROW_BG_A = (19, 24, 30)
ROW_BG_B = (24, 30, 38)
SEPARATOR = (35, 41, 50)
DIVIDER = (45, 52, 64)

WHITE = (245, 245, 250)
SOFT_GRAY = (148, 154, 168)
DIM_GRAY = (100, 105, 118)
HEADER_GRAY = (130, 138, 152)

WIN_GREEN = (96, 220, 134)
LOSE_RED = (228, 88, 88)

# Column accents
KILL_COLOR = WHITE
DEATH_COLOR = (210, 130, 130)
ASSIST_COLOR = (140, 195, 235)
ACS_COLOR = (240, 200, 120)
ELO_COLOR = (130, 165, 240)

WINNER_TINT = (24, 38, 28)
LOSER_TINT = (36, 24, 24)


# ── Cache ────────────────────────────────────────────────────────
_AGENT_ICON_CACHE_MAXSIZE: int = 64
_AGENT_ICON_CACHE: OrderedDict[str, Image.Image] = OrderedDict()


def _agent_icon_cache_get(key: str) -> Image.Image | None:
    img = _AGENT_ICON_CACHE.get(key)
    if img is not None:
        _AGENT_ICON_CACHE.move_to_end(key)
    return img


def _agent_icon_cache_set(key: str, img: Image.Image) -> None:
    _AGENT_ICON_CACHE[key] = img
    _AGENT_ICON_CACHE.move_to_end(key)
    while len(_AGENT_ICON_CACHE) > _AGENT_ICON_CACHE_MAXSIZE:
        _AGENT_ICON_CACHE.popitem(last=False)


# ── Font / text helpers ──────────────────────────────────────────
def _font(size: int, bold: bool = True):
    bold_paths = [
        "C:/Windows/Fonts/Inter-Bold.ttf",
        "C:/Windows/Fonts/arialbd.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
    ]
    regular_paths = [
        "C:/Windows/Fonts/Inter-Regular.ttf",
        "C:/Windows/Fonts/arial.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]
    for path in bold_paths if bold else regular_paths:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _text_w(draw: ImageDraw.ImageDraw, text: str, font) -> int:
    try:
        bbox = draw.textbbox((0, 0), text, font=font)
        return int(bbox[2] - bbox[0])
    except AttributeError:
        return int(font.getsize(text)[0])


def _draw_v_center(draw, text, x_left, y_center, font, color):
    """Draw `text` with its vertical middle aligned to y_center."""
    try:
        bbox = draw.textbbox((0, 0), text, font=font)
        y_arg = y_center - (bbox[1] + bbox[3]) // 2
    except AttributeError:
        _, h = font.getsize(text)
        y_arg = y_center - h // 2
    draw.text((x_left, y_arg), text, fill=color, font=font)


def _draw_centered(draw, text, x_center, y_center, font, color):
    w = _text_w(draw, text, font)
    _draw_v_center(draw, text, x_center - w // 2, y_center, font, color)


def _draw_right(draw, text, x_right, y_center, font, color):
    w = _text_w(draw, text, font)
    _draw_v_center(draw, text, x_right - w, y_center, font, color)


# ── Agent icon (local assets) ─────────────────────────────────────
_AGENTS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "assets",
    "agents",
)


def _agent_icon_filename(agent: str) -> str:
    """Map an agent name to its on-disk filename, normalizing the
    one edge case ('KAY/O' -> 'KAY_O.png')."""
    safe = agent.replace("/", "_")
    return f"{safe}.png"


def _load_agent_icon(agent: str | None) -> Image.Image | None:
    """Returns the agent icon from local cache (memory or disk).
    None if the agent is unknown / file is missing."""
    if not agent:
        return None
    key = agent
    cached = _agent_icon_cache_get(key)
    if cached is not None:
        return cached
    path = os.path.join(_AGENTS_DIR, _agent_icon_filename(agent))
    if not os.path.isfile(path):
        return None
    try:
        img = Image.open(path).convert("RGBA")
        img = img.resize((AGENT_ICON_SIZE, AGENT_ICON_SIZE), Image.Resampling.LANCZOS)
    except Exception:
        return None
    _agent_icon_cache_set(key, img)
    return img


def _placeholder_agent_icon() -> Image.Image:
    """Soft grey rounded square used when the agent is unknown."""
    img = Image.new("RGBA", (AGENT_ICON_SIZE, AGENT_ICON_SIZE), (*DIM_GRAY, 255))
    return img


# ── Public API ───────────────────────────────────────────────────
def generate_scoreboard(
    *,
    map_name: str,
    rounds_a: int,
    rounds_b: int,
    team_a_label: str,
    team_b_label: str,
    team_a_players: Iterable[Mapping[str, Any]],
    team_b_players: Iterable[Mapping[str, Any]],
    queue_label: str = "",
) -> BytesIO:
    """Generate a PNG scoreboard image and return it as a BytesIO."""
    a_players: Sequence[Mapping[str, Any]] = sorted(
        team_a_players, key=lambda p: p.get("acs", 0), reverse=True
    )
    b_players: Sequence[Mapping[str, Any]] = sorted(
        team_b_players, key=lambda p: p.get("acs", 0), reverse=True
    )

    height = (
        TOP_STRIP_BAND
        + SCORE_BAND
        + COLUMN_HEADER_BAND
        + PLAYERS_PER_TEAM * ROW_HEIGHT
        + FOOTER_BAND
    )
    img = Image.new("RGB", (WIDTH, height), BG)
    draw = ImageDraw.Draw(img)

    a_wins = rounds_a > rounds_b
    b_wins = rounds_b > rounds_a

    # ── Top strip ────────────────────────────────────────────────
    draw.rectangle([(0, 0), (WIDTH, TOP_STRIP_BAND)], fill=TOP_STRIP_BG)
    strip_y = TOP_STRIP_BAND // 2
    small_font = _font(15, bold=True)
    if queue_label:
        _draw_v_center(draw, queue_label.upper(), 24, strip_y, small_font, HEADER_GRAY)
    if map_name:
        _draw_right(draw, map_name.upper(), WIDTH - 24, strip_y, small_font, HEADER_GRAY)

    # ── Score band ───────────────────────────────────────────────
    score_y_top = TOP_STRIP_BAND
    score_y_center = score_y_top + SCORE_BAND // 2

    team_a_color = WIN_GREEN if a_wins else (LOSE_RED if b_wins else WHITE)
    team_b_color = WIN_GREEN if b_wins else (LOSE_RED if a_wins else WHITE)

    label_font = _font(28, bold=True)
    score_font = _font(58, bold=True)
    dash_font = _font(36, bold=False)

    a_score_str = str(rounds_a)
    b_score_str = str(rounds_b)
    dash = "-"

    # Centered group: LABEL_A  SCORE_A  -  SCORE_B  LABEL_B
    pad = 32
    label_a_w = _text_w(draw, team_a_label, label_font)
    label_b_w = _text_w(draw, team_b_label, label_font)
    score_a_w = _text_w(draw, a_score_str, score_font)
    score_b_w = _text_w(draw, b_score_str, score_font)
    dash_w = _text_w(draw, dash, dash_font)
    group_w = label_a_w + pad + score_a_w + pad + dash_w + pad + score_b_w + pad + label_b_w
    x = (WIDTH - group_w) // 2

    _draw_v_center(draw, team_a_label, x, score_y_center, label_font, team_a_color)
    x += label_a_w + pad
    _draw_v_center(draw, a_score_str, x, score_y_center, score_font, team_a_color)
    x += score_a_w + pad
    _draw_v_center(draw, dash, x, score_y_center, dash_font, SOFT_GRAY)
    x += dash_w + pad
    _draw_v_center(draw, b_score_str, x, score_y_center, score_font, team_b_color)
    x += score_b_w + pad
    _draw_v_center(draw, team_b_label, x, score_y_center, label_font, team_b_color)

    # ── Vertical divider between columns ─────────────────────────
    div_top = TOP_STRIP_BAND + SCORE_BAND
    div_bottom = height - FOOTER_BAND
    draw.line([(COL_W, div_top), (COL_W, div_bottom)], fill=DIVIDER, width=2)

    # ── Column header strip ──────────────────────────────────────
    header_y_top = TOP_STRIP_BAND + SCORE_BAND
    _draw_column_header(draw, COL_A_X0, header_y_top)
    _draw_column_header(draw, COL_B_X0, header_y_top)

    # ── Rows ─────────────────────────────────────────────────────
    rows_y0 = header_y_top + COLUMN_HEADER_BAND
    name_font = _font(20, bold=True)
    stats_font = _font(22, bold=True)

    for i in range(PLAYERS_PER_TEAM):
        y_top = rows_y0 + i * ROW_HEIGHT
        y_center = y_top + ROW_HEIGHT // 2
        bg = ROW_BG_A if i % 2 == 0 else ROW_BG_B
        # Left column
        draw.rectangle([(COL_A_X0, y_top), (COL_W - 1, y_top + ROW_HEIGHT)], fill=bg)
        # Right column
        draw.rectangle([(COL_W + 1, y_top), (WIDTH, y_top + ROW_HEIGHT)], fill=bg)
        if i < len(a_players):
            _draw_player_row(img, draw, a_players[i], COL_A_X0, y_center, name_font, stats_font)
        if i < len(b_players):
            _draw_player_row(img, draw, b_players[i], COL_B_X0, y_center, name_font, stats_font)

    # ── Footer ───────────────────────────────────────────────────
    footer_font = _font(15, bold=False)
    footer_y = height - FOOTER_BAND // 2
    _draw_centered(
        draw,
        "Play'IT Matchmaking Bot",
        WIDTH // 2,
        footer_y,
        footer_font,
        DIM_GRAY,
    )

    buf = BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf


def _draw_column_header(draw: ImageDraw.ImageDraw, col_x0: int, y_top: int) -> None:
    """Renders the PLAYER / KILLS / DEATHS / ASSISTS / ACS / ELO labels above
    the rows for a single team column."""
    header_font = _font(13, bold=True)
    y_center = y_top + COLUMN_HEADER_BAND // 2

    # Soft separator below the header
    draw.line(
        [
            (col_x0, y_top + COLUMN_HEADER_BAND - 1),
            (col_x0 + COL_W, y_top + COLUMN_HEADER_BAND - 1),
        ],
        fill=SEPARATOR,
        width=1,
    )

    _draw_v_center(draw, "PLAYER", col_x0 + X_NAME_REL, y_center, header_font, HEADER_GRAY)
    _draw_centered(draw, "KILLS", col_x0 + X_K, y_center, header_font, KILL_COLOR)
    _draw_centered(draw, "DEATHS", col_x0 + X_D, y_center, header_font, DEATH_COLOR)
    _draw_centered(draw, "ASSISTS", col_x0 + X_A, y_center, header_font, ASSIST_COLOR)
    _draw_centered(draw, "ACS", col_x0 + X_ACS, y_center, header_font, ACS_COLOR)
    _draw_right(draw, "ELO", col_x0 + X_ELO_RIGHT, y_center, header_font, ELO_COLOR)


def _draw_player_row(
    img: Image.Image,
    draw: ImageDraw.ImageDraw,
    player: Mapping[str, Any],
    col_x0: int,
    y_center: int,
    name_font,
    stats_font,
) -> None:
    name = str(player.get("name", "?"))
    kills = int(player.get("kills", 0) or 0)
    deaths = int(player.get("deaths", 0) or 0)
    assists = int(player.get("assists", 0) or 0)
    acs = int(player.get("acs", 0) or 0)
    elo = int(player.get("elo", 0) or 0)
    agent = player.get("agent") or None

    # Agent icon (square)
    icon = _load_agent_icon(agent)
    if icon is None:
        icon = _placeholder_agent_icon()
    icon_pos = (col_x0 + X_AGENT_REL, y_center - AGENT_ICON_SIZE // 2)
    img.paste(icon, icon_pos, icon if icon.mode == "RGBA" else None)

    # Name (truncate to fit before K column)
    max_name_w = (col_x0 + X_K) - (col_x0 + X_NAME_REL) - 40
    name_text = _truncate(draw, name, name_font, max_name_w)
    _draw_v_center(draw, name_text, col_x0 + X_NAME_REL, y_center, name_font, WHITE)

    # Stats columns
    _draw_centered(draw, str(kills), col_x0 + X_K, y_center, stats_font, KILL_COLOR)
    _draw_centered(draw, str(deaths), col_x0 + X_D, y_center, stats_font, DEATH_COLOR)
    _draw_centered(draw, str(assists), col_x0 + X_A, y_center, stats_font, ASSIST_COLOR)
    _draw_centered(draw, str(acs), col_x0 + X_ACS, y_center, stats_font, ACS_COLOR)
    _draw_right(draw, str(elo), col_x0 + X_ELO_RIGHT, y_center, stats_font, ELO_COLOR)


def _truncate(draw, text: str, font, max_width: int) -> str:
    if _text_w(draw, text, font) <= max_width:
        return text
    ellipsis = "…"
    truncated = text
    while truncated and _text_w(draw, truncated + ellipsis, font) > max_width:
        truncated = truncated[:-1]
    return (truncated + ellipsis) if truncated else ellipsis
