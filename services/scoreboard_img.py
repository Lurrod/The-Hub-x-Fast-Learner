"""
Per-match scoreboard image generation via Pillow.

Posted by the bot in the queue's results channel (pro-results / etc.)
once Henrik finds the custom and we have the match stats.

Layout: title band (map + score), 2 team columns side by side, each
row = circle avatar + Riot name#tag + K/D/A + ACS + post-game ELO.
Style mirrors leaderboard_img.py (dark theme, Inter/DejaVu fonts, green
for winning side, red for losing).

Input format (per player, both teams):
    {
        "name":       str,   # Riot "name#tag"
        "kills":      int,
        "deaths":     int,
        "assists":    int,
        "acs":        int,   # average combat score (rounded)
        "elo":        int,   # post-game ELO
        "avatar_url": str | None,
    }
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Iterable, Mapping, Sequence
from io import BytesIO
from typing import Any

import requests
from PIL import Image, ImageDraw, ImageFont


# ── Layout ────────────────────────────────────────────────────────
WIDTH = 1400
TITLE_BAND = 110
TEAM_HEADER_BAND = 55
ROW_HEIGHT = 70
FOOTER_BAND = 50
PLAYERS_PER_TEAM = 5

# Two columns, ~700 px each
COL_W = WIDTH // 2
COL_A_X0 = 0
COL_B_X0 = COL_W
COL_GAP_X = COL_W  # divider line position

# Per-row x positions (relative to the column origin)
COL_PAD_LEFT = 24
X_AVATAR_REL = COL_PAD_LEFT
X_NAME_REL = COL_PAD_LEFT + 70  # avatar (50) + gap
X_KDA_REL = COL_W - 290
X_ACS_REL = COL_W - 170
X_ELO_REL = COL_W - 70

AVATAR_SIZE = 50

# ── Colors ────────────────────────────────────────────────────────
BG = (12, 16, 22)
ROW_BG_A = (19, 24, 30)
ROW_BG_B = (24, 30, 38)
SEPARATOR = (35, 41, 50)
DIVIDER = (45, 52, 64)

WHITE = (245, 245, 250)
SOFT_GRAY = (140, 145, 158)
DIM_GRAY = (100, 105, 118)
GREEN = (96, 220, 134)
RED = (228, 88, 88)
BLUE = (96, 160, 240)

WINNER_BG_TINT = (24, 40, 30)  # slight green tint on winning team
LOSER_BG_TINT = (40, 24, 24)

# ── Avatar cache (LRU) ────────────────────────────────────────────
_AVATAR_CACHE_MAXSIZE: int = 500
_AVATAR_CACHE: OrderedDict[str, Image.Image] = OrderedDict()


def _avatar_cache_get(url: str) -> Image.Image | None:
    img = _AVATAR_CACHE.get(url)
    if img is not None:
        _AVATAR_CACHE.move_to_end(url)
    return img


def _avatar_cache_set(url: str, img: Image.Image) -> None:
    _AVATAR_CACHE[url] = img
    _AVATAR_CACHE.move_to_end(url)
    while len(_AVATAR_CACHE) > _AVATAR_CACHE_MAXSIZE:
        _AVATAR_CACHE.popitem(last=False)


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
    for path in (bold_paths if bold else regular_paths):
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


# ── Avatar fetch ─────────────────────────────────────────────────
_HTTP_TIMEOUT_SECONDS = 5


def _fetch_avatar(url: str | None) -> Image.Image | None:
    if not url:
        return None
    cached = _avatar_cache_get(url)
    if cached is not None:
        return cached
    try:
        resp = requests.get(url, timeout=_HTTP_TIMEOUT_SECONDS)
        resp.raise_for_status()
        a = Image.open(BytesIO(resp.content)).convert("RGBA")
        a = a.resize((AVATAR_SIZE, AVATAR_SIZE), Image.Resampling.LANCZOS)
        mask = Image.new("L", (AVATAR_SIZE, AVATAR_SIZE), 0)
        ImageDraw.Draw(mask).ellipse((0, 0, AVATAR_SIZE, AVATAR_SIZE), fill=255)
        a.putalpha(mask)
    except Exception:
        return None
    _avatar_cache_set(url, a)
    return a


def _placeholder_avatar() -> Image.Image:
    """Generic circular grey avatar used when no URL is available or fetch fails."""
    a = Image.new("RGBA", (AVATAR_SIZE, AVATAR_SIZE), (*DIM_GRAY, 255))
    mask = Image.new("L", (AVATAR_SIZE, AVATAR_SIZE), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, AVATAR_SIZE, AVATAR_SIZE), fill=255)
    a.putalpha(mask)
    return a


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
    """Generate a PNG scoreboard image and return it as a BytesIO.

    Both team rosters are sorted by ACS descending. Missing keys default
    safely (0 for ints, "?" for name). Avatars are best-effort: a grey
    circle is used when no URL is given or the fetch fails.
    """
    a_players: Sequence[Mapping[str, Any]] = sorted(
        team_a_players, key=lambda p: p.get("acs", 0), reverse=True
    )
    b_players: Sequence[Mapping[str, Any]] = sorted(
        team_b_players, key=lambda p: p.get("acs", 0), reverse=True
    )

    height = TITLE_BAND + TEAM_HEADER_BAND + PLAYERS_PER_TEAM * ROW_HEIGHT + FOOTER_BAND
    img = Image.new("RGB", (WIDTH, height), BG)
    draw = ImageDraw.Draw(img)

    a_wins = rounds_a > rounds_b
    b_wins = rounds_b > rounds_a

    # Title band
    title_font = _font(38, bold=True)
    sub_font = _font(22, bold=False)
    title_y = TITLE_BAND // 2 - 14
    sub_y = TITLE_BAND // 2 + 22

    title_text = f"{map_name.upper()}"
    _draw_centered(draw, title_text, WIDTH // 2, title_y, title_font, WHITE)

    sub_left = f"{team_a_label} {rounds_a}"
    sub_right = f"{rounds_b} {team_b_label}"
    sep = " — "
    sub_full = f"{sub_left}{sep}{sub_right}"
    _draw_centered(draw, sub_full, WIDTH // 2, sub_y, sub_font, SOFT_GRAY)

    if queue_label:
        small_font = _font(16, bold=False)
        _draw_v_center(draw, queue_label.upper(), 24, 22, small_font, DIM_GRAY)

    # Vertical divider between columns
    draw.line(
        [(COL_GAP_X, TITLE_BAND), (COL_GAP_X, height - FOOTER_BAND)],
        fill=DIVIDER,
        width=2,
    )

    # Team headers
    header_y = TITLE_BAND + TEAM_HEADER_BAND // 2
    header_font = _font(24, bold=True)
    a_color = GREEN if a_wins else (RED if b_wins else WHITE)
    b_color = GREEN if b_wins else (RED if a_wins else WHITE)
    _draw_centered(draw, team_a_label, COL_A_X0 + COL_W // 2, header_y, header_font, a_color)
    _draw_centered(draw, team_b_label, COL_B_X0 + COL_W // 2, header_y, header_font, b_color)

    # Rows
    name_font = _font(20, bold=True)
    stats_font = _font(20, bold=True)
    rows_y0 = TITLE_BAND + TEAM_HEADER_BAND
    for i in range(PLAYERS_PER_TEAM):
        y_top = rows_y0 + i * ROW_HEIGHT
        y_center = y_top + ROW_HEIGHT // 2
        bg = ROW_BG_A if i % 2 == 0 else ROW_BG_B
        # Left column
        draw.rectangle([(COL_A_X0, y_top), (COL_GAP_X - 1, y_top + ROW_HEIGHT)], fill=bg)
        # Right column
        draw.rectangle([(COL_B_X0 + 1, y_top), (WIDTH, y_top + ROW_HEIGHT)], fill=bg)

        if i < len(a_players):
            _draw_player_row(
                img, draw, a_players[i], COL_A_X0, y_center,
                name_font, stats_font,
            )
        if i < len(b_players):
            _draw_player_row(
                img, draw, b_players[i], COL_B_X0, y_center,
                name_font, stats_font,
            )

    # Footer
    footer_font = _font(16, bold=False)
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
    avatar_url = player.get("avatar_url")

    # Avatar
    avatar = _fetch_avatar(avatar_url) if avatar_url else None
    if avatar is None:
        avatar = _placeholder_avatar()
    img.paste(avatar, (col_x0 + X_AVATAR_REL, y_center - AVATAR_SIZE // 2), avatar)

    # Name (truncate to fit)
    name_text = _truncate(draw, name, name_font, X_KDA_REL - X_NAME_REL - 10)
    _draw_v_center(draw, name_text, col_x0 + X_NAME_REL, y_center, name_font, WHITE)

    # K/D/A
    kda_text = f"{kills}/{deaths}/{assists}"
    _draw_centered(draw, kda_text, col_x0 + X_KDA_REL, y_center, stats_font, SOFT_GRAY)

    # ACS
    _draw_centered(draw, str(acs), col_x0 + X_ACS_REL, y_center, stats_font, GREEN)

    # ELO
    _draw_right(draw, str(elo), col_x0 + X_ELO_REL, y_center, stats_font, BLUE)


def _truncate(draw, text: str, font, max_width: int) -> str:
    if _text_w(draw, text, font) <= max_width:
        return text
    ellipsis = "…"
    # Shrink character-by-character until the text + ellipsis fits.
    truncated = text
    while truncated and _text_w(draw, truncated + ellipsis, font) > max_width:
        truncated = truncated[:-1]
    return (truncated + ellipsis) if truncated else ellipsis
