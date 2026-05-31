"""Unit tests for services/scoreboard_img.py.

We don't pixel-diff the output — the goal here is to make sure the
generator returns a valid PNG without crashing on representative and
edge-case inputs (missing keys, draws, etc.). The image content is
sanity-checked by re-opening the bytes with PIL.
"""

from __future__ import annotations

from io import BytesIO
from unittest.mock import patch

from PIL import Image

from services.scoreboard_img import (
    PLAYERS_PER_TEAM,
    WIDTH,
    _truncate,
    generate_scoreboard,
)


def _player(name: str = "Jet", kills=20, deaths=15, assists=5, acs=240, elo=1500):
    return {
        "name": name,
        "kills": kills,
        "deaths": deaths,
        "assists": assists,
        "acs": acs,
        "elo": elo,
        "avatar_url": None,
    }


def _team(prefix: str = "P", n: int = PLAYERS_PER_TEAM):
    return [
        _player(name=f"{prefix}{i}#tag", kills=20 + i, deaths=14 + i, acs=230 + 10 * i)
        for i in range(n)
    ]


def test_generate_scoreboard_returns_valid_png():
    buf = generate_scoreboard(
        map_name="Ascent",
        rounds_a=13,
        rounds_b=7,
        team_a_label="Team A",
        team_b_label="Team B",
        team_a_players=_team("A"),
        team_b_players=_team("B"),
        queue_label="Pro Queue",
    )
    assert isinstance(buf, BytesIO)
    buf.seek(0)
    img = Image.open(buf)
    assert img.format == "PNG"
    assert img.width == WIDTH
    assert img.height > 0


def test_generate_scoreboard_handles_draw():
    """13-13 (overtime cap) shouldn't paint either side as winner."""
    buf = generate_scoreboard(
        map_name="Bind",
        rounds_a=12,
        rounds_b=12,
        team_a_label="Team A",
        team_b_label="Team B",
        team_a_players=_team("A"),
        team_b_players=_team("B"),
    )
    img = Image.open(buf)
    assert img.width == WIDTH


def test_generate_scoreboard_handles_short_rosters():
    """If a team has fewer than 5 players (e.g. missing puuid), the
    layout still renders without IndexError."""
    short_a = _team("A", n=3)
    short_b = _team("B", n=4)
    buf = generate_scoreboard(
        map_name="Haven",
        rounds_a=13,
        rounds_b=9,
        team_a_label="Team A",
        team_b_label="Team B",
        team_a_players=short_a,
        team_b_players=short_b,
    )
    img = Image.open(buf)
    assert img.width == WIDTH


def test_generate_scoreboard_handles_missing_fields():
    """Missing player fields (no kills/elo/etc.) default to 0 safely."""
    sparse = [{"name": "Mystery"} for _ in range(5)]
    buf = generate_scoreboard(
        map_name="Split",
        rounds_a=13,
        rounds_b=0,
        team_a_label="Team A",
        team_b_label="Team B",
        team_a_players=sparse,
        team_b_players=_team("B"),
    )
    img = Image.open(buf)
    assert img.width == WIDTH


def test_generate_scoreboard_sorts_by_acs_desc():
    """Top fragger appears in row 1 even if input is unsorted."""
    # Patch _draw_player_row to record the per-row players (in render order)
    rendered_a: list = []
    rendered_b: list = []

    real = generate_scoreboard.__globals__["_draw_player_row"]

    def spy(img, draw, player, col_x0, y_center, name_font, stats_font):
        if col_x0 == 0:
            rendered_a.append(player)
        else:
            rendered_b.append(player)
        return real(img, draw, player, col_x0, y_center, name_font, stats_font)

    unsorted = [
        _player(name="low", acs=100),
        _player(name="mid", acs=200),
        _player(name="top", acs=300),
        _player(name="other1", acs=150),
        _player(name="other2", acs=250),
    ]
    with patch("services.scoreboard_img._draw_player_row", spy):
        generate_scoreboard(
            map_name="Lotus",
            rounds_a=13,
            rounds_b=11,
            team_a_label="Team A",
            team_b_label="Team B",
            team_a_players=unsorted,
            team_b_players=_team("B"),
        )
    assert [p["name"] for p in rendered_a] == ["top", "other2", "mid", "other1", "low"]


def test_truncate_returns_original_when_it_fits():
    """_truncate should leave short strings alone (no spurious ellipsis)."""
    from PIL import Image as _PILImage
    from PIL import ImageDraw as _PILDraw

    img = _PILImage.new("RGB", (200, 50), (0, 0, 0))
    draw = _PILDraw.Draw(img)
    from services.scoreboard_img import _font

    font = _font(20)
    assert _truncate(draw, "Jet", font, 9999) == "Jet"


def test_truncate_adds_ellipsis_when_too_long():
    from PIL import Image as _PILImage
    from PIL import ImageDraw as _PILDraw

    img = _PILImage.new("RGB", (200, 50), (0, 0, 0))
    draw = _PILDraw.Draw(img)
    from services.scoreboard_img import _font

    font = _font(20)
    long = "ThisIsAReallyLongNameThatWillNotFitInTheColumn"
    result = _truncate(draw, long, font, max_width=80)
    assert result.endswith("…")
    assert len(result) < len(long)


def test_generate_scoreboard_omits_queue_label_when_empty():
    """Empty queue_label must not raise — just skip the corner label."""
    buf = generate_scoreboard(
        map_name="Sunset",
        rounds_a=13,
        rounds_b=8,
        team_a_label="Team A",
        team_b_label="Team B",
        team_a_players=_team("A"),
        team_b_players=_team("B"),
        queue_label="",
    )
    img = Image.open(buf)
    assert img.width == WIDTH


# ── Agent icon loading ──────────────────────────────────────────
def test_load_agent_icon_returns_image_for_known_agent():
    """A committed agent asset should resolve to a PIL image."""
    from services.scoreboard_img import (
        AGENT_ICON_SIZE,
        _AGENT_ICON_CACHE,
        _load_agent_icon,
    )

    _AGENT_ICON_CACHE.clear()
    icon = _load_agent_icon("Jett")
    assert icon is not None
    assert icon.size == (AGENT_ICON_SIZE, AGENT_ICON_SIZE)


def test_load_agent_icon_handles_slash_in_name():
    """KAY/O lives on disk as KAY_O.png — the loader must remap."""
    from services.scoreboard_img import _AGENT_ICON_CACHE, _load_agent_icon

    _AGENT_ICON_CACHE.clear()
    assert _load_agent_icon("KAY/O") is not None


def test_load_agent_icon_returns_none_for_unknown_agent():
    """Unknown agent name (never released, typo) -> None, no crash."""
    from services.scoreboard_img import _AGENT_ICON_CACHE, _load_agent_icon

    _AGENT_ICON_CACHE.clear()
    assert _load_agent_icon("MysteryAgentZ") is None


def test_load_agent_icon_returns_none_for_empty_string():
    """Empty/None agent -> None (the renderer falls back to a placeholder)."""
    from services.scoreboard_img import _load_agent_icon

    assert _load_agent_icon("") is None
    assert _load_agent_icon(None) is None


def test_load_agent_icon_caches_repeated_lookups():
    """Second call returns the same Image instance from memory."""
    from services.scoreboard_img import _AGENT_ICON_CACHE, _load_agent_icon

    _AGENT_ICON_CACHE.clear()
    first = _load_agent_icon("Sage")
    second = _load_agent_icon("Sage")
    assert first is second


# ── Column headers + new layout ────────────────────────────────
def test_scoreboard_includes_column_header_strip():
    """The header band must be drawn — total height reflects COLUMN_HEADER_BAND."""
    from services.scoreboard_img import (
        COLUMN_HEADER_BAND,
        FOOTER_BAND,
        PLAYERS_PER_TEAM,
        ROW_HEIGHT,
        SCORE_BAND,
        TOP_STRIP_BAND,
    )

    buf = generate_scoreboard(
        map_name="Ascent",
        rounds_a=13,
        rounds_b=7,
        team_a_label="Team A",
        team_b_label="Team B",
        team_a_players=_team("A"),
        team_b_players=_team("B"),
    )
    img = Image.open(buf)
    expected_h = (
        TOP_STRIP_BAND
        + SCORE_BAND
        + COLUMN_HEADER_BAND
        + PLAYERS_PER_TEAM * ROW_HEIGHT
        + FOOTER_BAND
    )
    assert img.height == expected_h


def test_scoreboard_renders_with_agent_field_present():
    """Generator must not crash when player rows carry an agent name."""
    a = [{**p, "agent": "Jett"} for p in _team("A")]
    b = [{**p, "agent": "Sage"} for p in _team("B")]
    buf = generate_scoreboard(
        map_name="Haven",
        rounds_a=10,
        rounds_b=13,
        team_a_label="Team A",
        team_b_label="Team B",
        team_a_players=a,
        team_b_players=b,
    )
    img = Image.open(buf)
    assert img.width == WIDTH
