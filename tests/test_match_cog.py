"""Tests d'integration du cog match (formation + persistance + reset queue)."""

import contextlib
import random
import pytest
from unittest.mock import AsyncMock, MagicMock


from cogs.match import MatchCog, VoteView, build_match_embed, VOTE_A_BTN_ID, VOTE_B_BTN_ID
from services import repository
from services.team_balancer import Player


def _fake_member(member_id: int, name: str = "User", voice_channel=None):
    m = MagicMock()
    m.id = member_id
    m.display_name = name
    m.mention = f"<@{member_id}>"
    m.roles = []
    m.guild = MagicMock(roles=[])
    m.add_roles = AsyncMock()
    m.remove_roles = AsyncMock()
    m.move_to = AsyncMock()
    if voice_channel is not None:
        voice = MagicMock()
        voice.channel = voice_channel
        m.voice = voice
    else:
        m.voice = None
    return m


def _fake_category(
    name: str,
    t1_empty: bool = True,
    t2_empty: bool = True,
    with_prep: bool = True,
    with_waiting: bool = True,
):
    cat = MagicMock()
    cat.name = name
    t1 = MagicMock()
    t1.name = "Team 1"
    t1.members = [] if t1_empty else [object()]
    t2 = MagicMock()
    t2.name = "Team 2"
    t2.members = [] if t2_empty else [object()]
    vcs = [t1, t2]
    if with_waiting:
        waiting = MagicMock()
        waiting.name = "Waiting Match"
        waiting.id = 800 + (hash(name) % 100)
        waiting.members = []
        vcs.append(waiting)
    cat.voice_channels = vcs
    if with_prep:
        prep = MagicMock()
        prep.name = "match-preparation"
        prep.id = 700 + (hash(name) % 100)
        prep.send = AsyncMock(return_value=MagicMock(id=555))
        prep.category = cat  # Back-reference to parent category
        cat.text_channels = [prep]
    else:
        cat.text_channels = []
    return cat


def _fake_channel(channel_id: int = 100):
    ch = MagicMock()
    ch.id = channel_id
    ch.send = AsyncMock(return_value=MagicMock(id=555, channel=ch))
    return ch


def _fake_guild(guild_id: int = 42, members=None, categories=None, channel=None):
    g = MagicMock()
    g.id = guild_id
    g.name = "TestGuild"
    g.members = members or []
    g.categories = categories or []
    g.get_member = lambda mid: next((m for m in g.members if m.id == int(mid)), None)
    g.get_channel = lambda cid: channel
    return g


def _fake_interaction(guild, user=None):
    inter = MagicMock()
    inter.guild = guild
    inter.user = user or _fake_member(1)
    inter.guild_id = guild.id
    inter.followup = MagicMock()
    inter.followup.send = AsyncMock()
    return inter


def _seed_full_queue(
    db,
    guild_id: int,
    channel_id: int = 100,
    queue_type: str = "open",
):
    """Cree la queue active + 10 comptes Riot lies + leur ELO serveur.

    Par defaut on simule une Open Queue (legacy, sans gate). Les tests
    Pro Queue passeront `queue_type="pro"` explicitement.
    """
    repository.setup_active_queue(
        db,
        guild_id=guild_id,
        queue_type=queue_type,
        channel_id=channel_id,
        message_id=999,
    )
    elo_col = repository.get_elo_col(db)
    for i in range(10):
        repository.link_riot_account(
            db,
            user_id=i,
            riot_name=f"P{i}",
            riot_tag="EUW",
            riot_region="eu",
            puuid=f"pu{i}",
            peak_elo=1500 + i * 50,
            source="peak_recent",
        )
        # Compound _id `<uid>:<queue_type>` pour le doc joueur.
        elo_col.insert_one(
            {
                "_id": repository.player_doc_id(i, queue_type),
                "name": f"P{i}",
                "elo": 1500 + i * 50,
                "wins": 0,
                "losses": 0,
                "linked_once": True,
            }
        )
        repository.add_player_to_queue(
            db,
            guild_id=guild_id,
            queue_type=queue_type,
            user_id=i,
        )
    return repository.get_active_queue(
        db,
        guild_id=guild_id,
        queue_type=queue_type,
    )




def _make_fake_channels(cat_id: int = 4242, cat_name: str = "Match #1", prep_id: int = 999):
    """Build a fully-wired MatchChannels mock for on_queue_full tests."""
    from services.match_category import MatchChannels

    fake_category = MagicMock()
    fake_category.id = cat_id
    fake_category.name = cat_name

    fake_prep = MagicMock()
    fake_prep.name = "match-preparation"
    fake_prep.id = prep_id
    fake_prep.category = fake_category
    fake_prep.send = AsyncMock(return_value=MagicMock(id=555))

    fake_team1 = MagicMock()
    fake_team1.name = "Team 1"
    fake_team1.id = prep_id + 10
    fake_team1.members = []

    fake_team2 = MagicMock()
    fake_team2.name = "Team 2"
    fake_team2.id = prep_id + 11
    fake_team2.members = []

    fake_waiting = MagicMock()
    fake_waiting.name = "Waiting Match"
    fake_waiting.id = prep_id + 12
    fake_waiting.members = []

    fake_category.text_channels = [fake_prep]
    fake_category.voice_channels = [fake_team1, fake_team2, fake_waiting]

    return MatchChannels(
        category=fake_category,
        prep_channel=fake_prep,
        team1_vc=fake_team1,
        team2_vc=fake_team2,
        waiting_match_vc=fake_waiting,
    )

# ── on_queue_full : succes ────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_on_queue_full_posts_message_with_view(monkeypatch):
    """on_queue_full sends match embed + VoteView to the prep channel."""
    import bot as bot_module
    import cogs.match._cog as match_cog_module

    fake_channels = _make_fake_channels()
    monkeypatch.setattr(match_cog_module, "reserve_match_number", lambda db, *, guild_id: 1)
    monkeypatch.setattr(match_cog_module, "create_match_category", AsyncMock(return_value=fake_channels))

    queue_doc = _seed_full_queue(bot_module.db, guild_id=42)
    members = [_fake_member(i, f"P{i}") for i in range(10)]
    channel = _fake_channel(100)
    guild = _fake_guild(42, members=members, categories=[], channel=channel)
    inter = _fake_interaction(guild, user=members[9])

    cog = MatchCog(bot_module.bot, bot_module.db, rng=random.Random(42))
    match_id = await cog.on_queue_full(inter, queue_doc, "open")

    assert match_id is not None
    fake_channels.prep_channel.send.assert_awaited_once()
    _, kwargs = fake_channels.prep_channel.send.call_args
    assert "Match trouve" in kwargs["content"]
    embed = kwargs["embed"]
    assert "Map" in embed.description
    assert any("Team A" in f.name for f in embed.fields)
    assert any("Team B" in f.name for f in embed.fields)
    assert isinstance(kwargs["view"], VoteView)


@pytest.mark.asyncio
async def test_on_queue_full_persists_match(monkeypatch):
    """on_queue_full stores a match doc with expected fields."""
    import bot as bot_module
    import cogs.match._cog as match_cog_module

    fake_channels = _make_fake_channels(cat_id=1111, cat_name="Match #1", prep_id=777)
    monkeypatch.setattr(match_cog_module, "reserve_match_number", lambda db, *, guild_id: 1)
    monkeypatch.setattr(match_cog_module, "create_match_category", AsyncMock(return_value=fake_channels))

    queue_doc = _seed_full_queue(bot_module.db, guild_id=42)
    members = [_fake_member(i, f"P{i}") for i in range(10)]
    channel = _fake_channel(100)
    guild = _fake_guild(42, members=members, categories=[], channel=channel)
    inter = _fake_interaction(guild)

    cog = MatchCog(bot_module.bot, bot_module.db, rng=random.Random(0))
    match_id = await cog.on_queue_full(inter, queue_doc, "open")

    match = repository.get_match(bot_module.db, match_id)
    assert match is not None
    assert match["status"] == "pending"
    assert match["map"] in ("Breeze", "Ascent", "Lotus", "Fracture", "Split", "Haven", "Pearl")
    assert match["category_name"] == "Match #1"
    assert match["message_id"] == 555
    assert match["channel_id"] == 777
    assert len(match["team_a"]) == 5
    assert len(match["team_b"]) == 5
    assert match["votes"] == {}
    assert int(match["lobby_leader_id"]) in range(10)


@pytest.mark.asyncio
async def test_on_queue_full_resets_queue(monkeypatch):
    """After a successful match formation the active queue is cleared."""
    import bot as bot_module
    import cogs.match._cog as match_cog_module

    fake_channels = _make_fake_channels()
    monkeypatch.setattr(match_cog_module, "reserve_match_number", lambda db, *, guild_id: 1)
    monkeypatch.setattr(match_cog_module, "create_match_category", AsyncMock(return_value=fake_channels))

    queue_doc = _seed_full_queue(bot_module.db, guild_id=42)
    members = [_fake_member(i) for i in range(10)]
    channel = _fake_channel(100)
    guild = _fake_guild(42, members=members, categories=[], channel=channel)
    inter = _fake_interaction(guild)

    cog = MatchCog(bot_module.bot, bot_module.db, rng=random.Random(0))
    await cog.on_queue_full(inter, queue_doc, "open")

    assert repository.get_active_queue(bot_module.db, guild_id=42, queue_type="open") is None


@pytest.mark.asyncio
async def test_on_queue_full_balanced_teams_in_persistence(monkeypatch):
    """The persisted teams must be ELO-balanced within acceptable range."""
    import bot as bot_module
    import cogs.match._cog as match_cog_module

    fake_channels = _make_fake_channels()
    monkeypatch.setattr(match_cog_module, "reserve_match_number", lambda db, *, guild_id: 1)
    monkeypatch.setattr(match_cog_module, "create_match_category", AsyncMock(return_value=fake_channels))

    queue_doc = _seed_full_queue(bot_module.db, guild_id=42)
    members = [_fake_member(i) for i in range(10)]
    channel = _fake_channel(100)
    guild = _fake_guild(42, members=members, categories=[], channel=channel)
    inter = _fake_interaction(guild)

    cog = MatchCog(bot_module.bot, bot_module.db, rng=random.Random(0))
    match_id = await cog.on_queue_full(inter, queue_doc, "open")

    match = repository.get_match(bot_module.db, match_id)
    sum_a = sum(p["elo"] for p in match["team_a"])
    sum_b = sum(p["elo"] for p in match["team_b"])
    diff = abs(sum_a - sum_b)
    assert diff <= 100, f"diff={diff}, attendu <=100 sur cet ensemble"


@pytest.mark.asyncio
async def test_on_queue_full_aborts_if_player_unlinked(monkeypatch):
    """If a player has no linked Riot account, the match is aborted."""
    import bot as bot_module
    import cogs.match._cog as match_cog_module

    fake_channels = _make_fake_channels()
    monkeypatch.setattr(match_cog_module, "reserve_match_number", lambda db, *, guild_id: 1)
    monkeypatch.setattr(match_cog_module, "create_match_category", AsyncMock(return_value=fake_channels))

    queue_doc = _seed_full_queue(bot_module.db, guild_id=42)
    repository.unlink_riot_account(bot_module.db, user_id=5)

    members = [_fake_member(i) for i in range(10)]
    channel = _fake_channel(100)
    guild = _fake_guild(42, members=members, categories=[], channel=channel)
    inter = _fake_interaction(guild)

    cog = MatchCog(bot_module.bot, bot_module.db, rng=random.Random(0))
    result = await cog.on_queue_full(inter, queue_doc, "open")

    assert result is None
    # Queue is deleted on failure
    assert repository.get_active_queue(bot_module.db, guild_id=42, queue_type="open") is None
    # Verify the user-facing cancel message was sent
    channel.send.assert_awaited()
    send_args = channel.send.await_args
    sent_content = send_args.args[0] if send_args.args else send_args.kwargs.get("content", "")
    assert "annule" in sent_content.lower()




# ── VoteView stub (Phase 4 — Phase 5 implementera) ────────────────
async def test_vote_view_buttons_have_stable_custom_ids():
    import bot as bot_module

    view = VoteView(bot_module.db)
    # Cherche les custom_ids dans les children
    custom_ids = {c.custom_id for c in view.children}
    assert VOTE_A_BTN_ID in custom_ids
    assert VOTE_B_BTN_ID in custom_ids


# ── Ordre overwrites -> message (audit user) ─────────────────────
@pytest.mark.asyncio
async def test_overwrites_set_before_match_message_sent(monkeypatch):
    """create_match_category (which sets channel overwrites) must be awaited
    BEFORE prep_channel.send — otherwise players can't see the announce."""
    import bot as bot_module
    import cogs.match._cog as match_cog_module

    queue_doc = _seed_full_queue(bot_module.db, guild_id=42)
    members = [_fake_member(i, f"P{i}") for i in range(10)]
    channel = _fake_channel(100)
    guild = _fake_guild(42, members=members, categories=[], channel=channel)
    inter = _fake_interaction(guild)

    events = []
    fake_channels = _make_fake_channels()

    async def _fake_create(**kwargs):
        events.append("create_match_category")
        return fake_channels

    async def _prep_send(*args, **kwargs):
        events.append("prep_send")
        msg = MagicMock()
        msg.id = 555
        return msg

    fake_channels.prep_channel.send.side_effect = _prep_send

    monkeypatch.setattr(match_cog_module, "reserve_match_number", lambda db, *, guild_id: 1)
    monkeypatch.setattr(match_cog_module, "create_match_category", _fake_create)

    cog = MatchCog(bot_module.bot, bot_module.db, rng=random.Random(0))
    await cog.on_queue_full(inter, queue_doc, "open")

    assert "create_match_category" in events, "create_match_category must be called"
    assert "prep_send" in events, "prep_channel.send must be called"
    assert events.index("create_match_category") < events.index("prep_send"), (
        "create_match_category must complete before prep_channel.send"
    )


@pytest.mark.asyncio
async def test_players_moved_to_team_vcs(monkeypatch):
    """In open queue, players in a voice channel are moved to Team 1 or Team 2 (5+5)."""
    import bot as bot_module
    import cogs.match._cog as match_cog_module

    queue_doc = _seed_full_queue(bot_module.db, guild_id=42)

    waiting_room = MagicMock()
    waiting_room.name = "Waiting Room"
    waiting_room.id = 999

    fake_channels = _make_fake_channels(cat_name="Match #1")
    team1 = fake_channels.team1_vc
    team2 = fake_channels.team2_vc
    # The cog looks up the category by name from guild.categories, so include it.
    guild_category = fake_channels.category

    members = [_fake_member(i, f"P{i}", voice_channel=waiting_room) for i in range(10)]
    channel = _fake_channel(100)
    guild = _fake_guild(42, members=members, categories=[guild_category], channel=channel)
    inter = _fake_interaction(guild)

    monkeypatch.setattr(match_cog_module, "reserve_match_number", lambda db, *, guild_id: 1)
    monkeypatch.setattr(match_cog_module, "create_match_category", AsyncMock(return_value=fake_channels))

    cog = MatchCog(bot_module.bot, bot_module.db, rng=random.Random(0))
    await cog.on_queue_full(inter, queue_doc, "open")

    dests = []
    for m in members:
        if m.move_to.await_count:
            dest = m.move_to.await_args.args[0]
            assert dest in (team1, team2), f"Player {m.id} moved to unexpected VC"
            dests.append(dest)
    assert len(dests) == 10
    assert dests.count(team1) == 5
    assert dests.count(team2) == 5


@pytest.mark.asyncio
async def test_player_already_in_team_vc_not_moved(monkeypatch):
    """Players already in their team VC are not re-moved to it."""
    import bot as bot_module
    import cogs.match._cog as match_cog_module

    queue_doc = _seed_full_queue(bot_module.db, guild_id=42)

    fake_channels = _make_fake_channels(cat_name="Match #1")
    team1 = fake_channels.team1_vc
    guild_category = fake_channels.category

    # All players start in Team 1 VC
    members = [_fake_member(i, f"P{i}", voice_channel=team1) for i in range(10)]
    channel = _fake_channel(100)
    guild = _fake_guild(42, members=members, categories=[guild_category], channel=channel)
    inter = _fake_interaction(guild)

    monkeypatch.setattr(match_cog_module, "reserve_match_number", lambda db, *, guild_id: 1)
    monkeypatch.setattr(match_cog_module, "create_match_category", AsyncMock(return_value=fake_channels))

    cog = MatchCog(bot_module.bot, bot_module.db, rng=random.Random(0))
    await cog.on_queue_full(inter, queue_doc, "open")

    # Nobody should be moved back to Team 1 (they are already there)
    for m in members:
        for call in m.move_to.await_args_list:
            assert call.args[0] is not team1, "Player already in Team 1 should not be re-moved there"


@pytest.mark.asyncio
async def test_queue_full_does_not_crash_when_no_team_vcs(monkeypatch):
    """If the category has no voice channels, the match is still created (graceful fallback)."""
    import bot as bot_module
    import cogs.match._cog as match_cog_module
    from services.match_category import MatchChannels

    fake_category = MagicMock()
    fake_category.id = 7777
    fake_category.name = "Match #1"
    fake_prep = MagicMock()
    fake_prep.name = "match-preparation"
    fake_prep.id = 777
    fake_prep.category = fake_category
    fake_prep.send = AsyncMock(return_value=MagicMock(id=42424242))
    fake_category.text_channels = [fake_prep]
    fake_category.voice_channels = []

    fake_channels = MatchChannels(
        category=fake_category,
        prep_channel=fake_prep,
        team1_vc=None,
        team2_vc=None,
        waiting_match_vc=None,
    )

    monkeypatch.setattr(match_cog_module, "reserve_match_number", lambda db, *, guild_id: 1)
    monkeypatch.setattr(match_cog_module, "create_match_category", AsyncMock(return_value=fake_channels))

    queue_doc = _seed_full_queue(bot_module.db, guild_id=42)
    members = [_fake_member(i, f"P{i}") for i in range(10)]
    channel = _fake_channel(100)
    guild = _fake_guild(42, members=members, categories=[], channel=channel)
    inter = _fake_interaction(guild)

    cog = MatchCog(bot_module.bot, bot_module.db, rng=random.Random(0))
    match_id = await cog.on_queue_full(inter, queue_doc, "open")
    assert match_id is not None




# ── queue_type propagation ────────────────────────────────────────
@pytest.mark.asyncio
async def test_on_queue_full_persists_queue_type_in_match_doc(monkeypatch):
    """The match doc must store queue_type='pro' for a Pro Queue match."""
    import bot as bot_module
    import cogs.match._cog as match_cog_module
    import services.captain_draft as cd_module

    async def _fake_run(self):
        from services.captain_draft import DraftResult
        state = self.state
        for p in list(state.pool):
            state = state.apply_pick(p)
        return DraftResult.from_state(state)

    monkeypatch.setattr(cd_module.CaptainDraftSession, "run", _fake_run)

    fake_channels = _make_fake_channels()
    monkeypatch.setattr(match_cog_module, "reserve_match_number", lambda db, *, guild_id: 1)
    monkeypatch.setattr(match_cog_module, "create_match_category", AsyncMock(return_value=fake_channels))

    queue_doc = _seed_full_queue(bot_module.db, guild_id=42, queue_type="pro")
    members = [_fake_member(i, f"P{i}") for i in range(10)]
    channel = _fake_channel(100)
    guild = _fake_guild(42, members=members, categories=[], channel=channel)
    inter = _fake_interaction(guild)

    cog = MatchCog(bot_module.bot, bot_module.db, rng=random.Random(0))
    match_id = await cog.on_queue_full(inter, queue_doc, "pro")

    match = repository.get_match(bot_module.db, match_id)
    assert match is not None
    assert match["queue_type"] == "pro"


@pytest.mark.asyncio
async def test_on_queue_full_passes_queue_type_to_create_match(monkeypatch):
    """Spy on repository.create_match to verify queue_type kwarg is forwarded."""
    import bot as bot_module
    import cogs.match._cog as match_cog_module

    fake_channels = _make_fake_channels()
    monkeypatch.setattr(match_cog_module, "reserve_match_number", lambda db, *, guild_id: 1)
    monkeypatch.setattr(match_cog_module, "create_match_category", AsyncMock(return_value=fake_channels))

    queue_doc = _seed_full_queue(bot_module.db, guild_id=42, queue_type="gc")

    captured: dict = {}
    real_create = repository.create_match

    def spy_create(*args, **kwargs):
        captured.update(kwargs)
        return real_create(*args, **kwargs)

    monkeypatch.setattr("services.repository.create_match", spy_create)

    members = [_fake_member(i, f"P{i}") for i in range(10)]
    channel = _fake_channel(100)
    guild = _fake_guild(42, members=members, categories=[], channel=channel)
    inter = _fake_interaction(guild)

    cog = MatchCog(bot_module.bot, bot_module.db, rng=random.Random(0))
    await cog.on_queue_full(inter, queue_doc, "gc")

    assert captured.get("queue_type") == "gc"





# -- Task 7: dynamic category creation ----------------------------------
@pytest.mark.asyncio
async def test_start_match_creates_dynamic_category(monkeypatch):
    """After Task 7 wire-up: start_match must reserve a number + create
    the category via services.match_category, and persist category_id +
    match_number on the match doc."""
    import bot as bot_module
    import cogs.match._cog as match_cog_module

    fake_category = MagicMock()
    fake_category.id = 4242
    fake_category.name = "Match #43"

    fake_prep = MagicMock()
    fake_prep.name = "match-preparation"
    fake_prep.id = 999
    fake_prep.category = fake_category
    fake_prep.send = AsyncMock(return_value=MagicMock(id=555))

    fake_team1 = MagicMock()
    fake_team1.name = "Team 1"
    fake_team1.id = 991
    fake_team1.members = []

    fake_team2 = MagicMock()
    fake_team2.name = "Team 2"
    fake_team2.id = 992
    fake_team2.members = []

    fake_waiting = MagicMock()
    fake_waiting.name = "Waiting Match"
    fake_waiting.id = 993
    fake_waiting.members = []

    fake_category.text_channels = [fake_prep]
    fake_category.voice_channels = [fake_team1, fake_team2, fake_waiting]

    from services.match_category import MatchChannels

    fake_channels = MatchChannels(
        category=fake_category,
        prep_channel=fake_prep,
        team1_vc=fake_team1,
        team2_vc=fake_team2,
        waiting_match_vc=fake_waiting,
    )

    mock_create = AsyncMock(return_value=fake_channels)

    monkeypatch.setattr(match_cog_module, "reserve_match_number", lambda db, *, guild_id: 43)
    monkeypatch.setattr(match_cog_module, "create_match_category", mock_create)

    queue_doc = _seed_full_queue(bot_module.db, guild_id=42)
    members = [_fake_member(i, f"P{i}") for i in range(10)]
    channel = _fake_channel(100)
    guild = _fake_guild(42, members=members, categories=[], channel=channel)
    inter = _fake_interaction(guild, user=members[0])

    cog = MatchCog(bot_module.bot, bot_module.db, rng=random.Random(42))
    match_id = await cog.on_queue_full(inter, queue_doc, "open")

    assert match_id is not None, "on_queue_full doit retourner un match_id"
    mock_create.assert_awaited_once()

    match = repository.get_match(bot_module.db, match_id)
    assert match is not None
    assert match["category_id"] == 4242
    assert match["category_name"] == "Match #43"
    assert match["match_number"] == 43

# ── build_match_embed ─────────────────────────────────────────────
def test_build_match_embed_shows_all_players_and_map():
    from services.match_service import MatchPlan
    from services.team_balancer import balance_teams

    players = [Player(id=i, name=f"P{i}", elo=1500 + i * 50) for i in range(10)]
    teams = balance_teams(players)
    plan = MatchPlan(
        teams=teams, map_name="Ascent", lobby_leader=players[0], category_name="Match #1"
    )

    embed = build_match_embed(plan, "MyGuild")
    assert "Ascent" in embed.description
    assert "<@0>" in embed.description  # leader
    fields_str = " ".join(f.value for f in embed.fields)
    for i in range(10):
        assert f"<@{i}>" in fields_str
    assert "Match #1" in fields_str


# ── _move_players_to_waiting_match ────────────────────────────────
async def test_move_to_waiting_match_routes_all_players():
    """_move_players_to_waiting_match deplace les 10 joueurs vers Waiting Match."""
    import bot as bot_module

    # Voice channel source
    waiting_room = MagicMock()
    waiting_room.name = "Pro Waiting Room"
    waiting_room.id = 7777

    members = [_fake_member(i, f"P{i}", voice_channel=waiting_room) for i in range(10)]
    cat = _fake_category("Match #1", with_waiting=True)
    channel = _fake_channel(100)
    guild = _fake_guild(42, members=members, categories=[cat], channel=channel)

    cog = MatchCog(bot_module.bot, bot_module.db, rng=random.Random(42))
    player_ids = [str(m.id) for m in members]

    await cog._move_players_to_waiting_match(guild, cat, player_ids)

    waiting_match_vc = next(c for c in cat.voice_channels if c.name == "Waiting Match")
    moved_to_waiting = sum(
        1
        for m in members
        if m.move_to.await_count > 0 and m.move_to.call_args.args[0].id == waiting_match_vc.id
    )
    assert moved_to_waiting == 10


# ── Pro Queue Captain Draft integration ───────────────────────────


def _make_10_players():
    """Retourne 10 Player avec ELO croissant pour les tests pro queue."""
    return [Player(id=i, name=f"P{i}", elo=1500 + i * 50) for i in range(10)]


def _patch_build_players(monkeypatch, players):
    """Monkeypatch build_players dans cogs.match._cog pour court-circuiter le fetch Mongo.

    `cogs.match` est un package depuis le split — `MatchCog.on_queue_full` vit
    dans le sous-module `_cog`, qui importe `build_players` dans son propre
    namespace. Patcher l'alias du package n'aurait aucun effet.
    """
    import cogs.match._cog as cog_module

    monkeypatch.setattr(cog_module, "build_players", lambda *a, **kw: players)


@pytest.mark.asyncio
async def test_on_queue_full_open_does_not_invoke_captain_draft(monkeypatch):
    """queue_type='open' -> plan_match utilise, CaptainDraftSession PAS instancie."""
    from cogs.match import MatchCog
    import bot as bot_module
    import services.captain_draft as cd_module

    players = _make_10_players()
    _patch_build_players(monkeypatch, players)

    instantiated = []
    original_init = cd_module.CaptainDraftSession.__init__

    def _spy_init(self, *args, **kwargs):
        instantiated.append(1)
        return original_init(self, *args, **kwargs)

    monkeypatch.setattr(cd_module.CaptainDraftSession, "__init__", _spy_init)

    members = [_fake_member(i, f"P{i}") for i in range(10)]
    channel = _fake_channel(100)
    cat = _fake_category("Match #1")
    guild = _fake_guild(42, members=members, categories=[cat], channel=channel)
    inter = _fake_interaction(guild, user=members[9])

    cog = MatchCog(bot_module.bot, bot_module.db, rng=random.Random(42))
    queue_doc = {"players": [str(m.id) for m in members], "channel_id": "100"}
    with contextlib.suppress(Exception):
        await cog.on_queue_full(inter, queue_doc, queue_type="open")
    assert instantiated == [], "CaptainDraftSession ne doit pas etre instancie en open queue"


@pytest.mark.asyncio
async def test_on_queue_full_pro_invokes_captain_draft(monkeypatch):
    """queue_type='pro' -> CaptainDraftSession.run() is called exactly once."""
    from cogs.match import MatchCog
    import bot as bot_module
    import cogs.match._cog as match_cog_module
    import services.captain_draft as cd_module

    players = _make_10_players()
    _patch_build_players(monkeypatch, players)

    run_calls = []

    async def _fake_run(self):
        run_calls.append(self)
        from services.captain_draft import DraftResult
        state = self.state
        for p in list(state.pool):
            state = state.apply_pick(p)
        return DraftResult.from_state(state)

    monkeypatch.setattr(cd_module.CaptainDraftSession, "run", _fake_run)

    fake_channels = _make_fake_channels()
    monkeypatch.setattr(match_cog_module, "reserve_match_number", lambda db, *, guild_id: 1)
    monkeypatch.setattr(match_cog_module, "create_match_category", AsyncMock(return_value=fake_channels))

    members = [_fake_member(i, f"P{i}") for i in range(10)]
    channel = _fake_channel(100)
    guild = _fake_guild(42, members=members, categories=[], channel=channel)
    inter = _fake_interaction(guild, user=members[9])

    cog = MatchCog(bot_module.bot, bot_module.db, rng=random.Random(42))
    queue_doc = _seed_full_queue(bot_module.db, guild_id=42, queue_type="pro")
    with contextlib.suppress(Exception):
        await cog.on_queue_full(inter, queue_doc, queue_type="pro")
    assert len(run_calls) == 1, "CaptainDraftSession.run() must be called exactly once"




# ── match_cancel : suppression catégorie ─────────────────────────
@pytest.mark.asyncio
async def test_admin_cancel_deletes_match_category(monkeypatch):
    """When admin runs /match-cancel, the dynamic category is deleted."""
    import bot as bot_module
    import cogs.match._cog as match_cog_module

    delete_mock = AsyncMock()
    monkeypatch.setattr(match_cog_module, "delete_match_category", delete_mock)

    # Seed a pending match with category_id=5555 so cancel_match_atomically finds it.
    queue_doc = _seed_full_queue(bot_module.db, guild_id=42)
    members = [_fake_member(i, f"P{i}") for i in range(10)]
    channel = _fake_channel(100)
    guild = _fake_guild(42, members=members, categories=[], channel=channel)
    guild.roles = []
    inter_start = _fake_interaction(guild, user=members[0])

    # Create the match via on_queue_full (patching create_match_category to avoid Discord calls).
    fake_category = MagicMock()
    fake_category.id = 5555
    fake_category.name = "Match #1"
    fake_prep = MagicMock()
    fake_prep.name = "match-preparation"
    fake_prep.id = 999
    fake_prep.category = fake_category
    fake_prep.send = AsyncMock(return_value=MagicMock(id=555))
    fake_team1 = MagicMock()
    fake_team1.name = "Team 1"
    fake_team1.id = 991
    fake_team1.members = []
    fake_team2 = MagicMock()
    fake_team2.name = "Team 2"
    fake_team2.id = 992
    fake_team2.members = []
    fake_waiting = MagicMock()
    fake_waiting.name = "Waiting Match"
    fake_waiting.id = 993
    fake_waiting.members = []
    fake_category.text_channels = [fake_prep]
    fake_category.voice_channels = [fake_team1, fake_team2, fake_waiting]

    from services.match_category import MatchChannels

    fake_channels = MatchChannels(
        category=fake_category,
        prep_channel=fake_prep,
        team1_vc=fake_team1,
        team2_vc=fake_team2,
        waiting_match_vc=fake_waiting,
    )
    monkeypatch.setattr(match_cog_module, "reserve_match_number", lambda db, *, guild_id: 1)
    monkeypatch.setattr(match_cog_module, "create_match_category", AsyncMock(return_value=fake_channels))

    cog = MatchCog(bot_module.bot, bot_module.db, rng=random.Random(42))
    await cog.on_queue_full(inter_start, queue_doc, "open")

    # The match is stored with channel_id = prep_channel.id (999) — use that.
    inter_cancel = _fake_interaction(guild)
    inter_cancel.channel_id = fake_prep.id  # 999
    inter_cancel.channel = channel
    inter_cancel.response = MagicMock()
    inter_cancel.response.defer = AsyncMock()

    await cog.match_cancel.callback(cog, inter_cancel)

    delete_mock.assert_awaited_once()
    assert delete_mock.await_args.kwargs["category_id"] == 5555


# ── draft failure rollback ─────────────────────────────────────────
@pytest.mark.asyncio
async def test_draft_failure_deletes_match_category_and_match_doc(monkeypatch):
    """If CaptainDraftSession.run() raises an unexpected exception (timeout,
    disconnect, etc.), the freshly-created category must be torn down.
    No match doc exists yet at this point in the flow (create_match happens
    after the draft), so only the category needs cleanup."""
    import bot as bot_module
    import cogs.match._cog as match_cog_module
    import services.captain_draft as cd_module

    # --- patch delete_match_category to capture calls ---
    delete_mock = AsyncMock()
    monkeypatch.setattr(match_cog_module, "delete_match_category", delete_mock)

    # --- patch create_match_category to return a fake category ---
    fake_category = MagicMock()
    fake_category.id = 7777
    fake_category.name = "Match #99"

    fake_prep = MagicMock()
    fake_prep.name = "match-preparation"
    fake_prep.id = 888
    fake_prep.category = fake_category
    fake_prep.send = AsyncMock(return_value=MagicMock(id=555))

    fake_team1 = MagicMock()
    fake_team1.name = "Team 1"
    fake_team1.id = 881
    fake_team1.members = []

    fake_team2 = MagicMock()
    fake_team2.name = "Team 2"
    fake_team2.id = 882
    fake_team2.members = []

    fake_waiting = MagicMock()
    fake_waiting.name = "Waiting Match"
    fake_waiting.id = 883
    fake_waiting.members = []

    fake_category.text_channels = [fake_prep]
    fake_category.voice_channels = [fake_team1, fake_team2, fake_waiting]

    from services.match_category import MatchChannels

    fake_channels = MatchChannels(
        category=fake_category,
        prep_channel=fake_prep,
        team1_vc=fake_team1,
        team2_vc=fake_team2,
        waiting_match_vc=fake_waiting,
    )
    monkeypatch.setattr(match_cog_module, "reserve_match_number", lambda db, *, guild_id: 99)
    monkeypatch.setattr(
        match_cog_module, "create_match_category", AsyncMock(return_value=fake_channels)
    )

    # --- patch build_players to avoid Mongo fetch ---
    players = _make_10_players()
    _patch_build_players(monkeypatch, players)

    # --- patch CaptainDraftSession.run to raise a generic exception ---
    async def _fail_run(self):
        raise RuntimeError("draft timeout")

    monkeypatch.setattr(cd_module.CaptainDraftSession, "run", _fail_run)

    # --- build a pro-queue doc and guild ---
    queue_doc = _seed_full_queue(bot_module.db, guild_id=42, queue_type="pro")
    members = [_fake_member(i, f"P{i}") for i in range(10)]
    channel = _fake_channel(100)
    guild = _fake_guild(42, members=members, categories=[], channel=channel)
    guild.roles = []
    inter = _fake_interaction(guild, user=members[0])

    cog = MatchCog(bot_module.bot, bot_module.db, rng=random.Random(42))
    result = await cog.on_queue_full(inter, queue_doc, queue_type="pro")

    # Flow must return None (not propagate the exception)
    assert result is None

    # delete_match_category must have been called with the fake category id
    delete_mock.assert_awaited_once()
    assert delete_mock.await_args.kwargs["category_id"] == 7777

# ── draft cancelled by admin rollback ─────────────────────────────────────────
@pytest.mark.asyncio
async def test_draft_cancelled_by_admin_deletes_match_category(monkeypatch):
    """When an admin cancels the captain draft (raising DraftCancelledError),
    the dynamic category must still be torn down before returning."""
    import bot as bot_module
    import cogs.match._cog as match_cog_module
    import services.captain_draft as cd_module

    # --- patch delete_match_category to capture calls ---
    delete_mock = AsyncMock()
    monkeypatch.setattr(match_cog_module, "delete_match_category", delete_mock)

    # --- patch create_match_category to return a fake category ---
    fake_category = MagicMock()
    fake_category.id = 7777
    fake_category.name = "Match #99"

    fake_prep = MagicMock()
    fake_prep.name = "match-preparation"
    fake_prep.id = 888
    fake_prep.category = fake_category
    fake_prep.send = AsyncMock(return_value=MagicMock(id=555))

    fake_team1 = MagicMock()
    fake_team1.name = "Team 1"
    fake_team1.id = 881
    fake_team1.members = []

    fake_team2 = MagicMock()
    fake_team2.name = "Team 2"
    fake_team2.id = 882
    fake_team2.members = []

    fake_waiting = MagicMock()
    fake_waiting.name = "Waiting Match"
    fake_waiting.id = 883
    fake_waiting.members = []

    fake_category.text_channels = [fake_prep]
    fake_category.voice_channels = [fake_team1, fake_team2, fake_waiting]

    from services.match_category import MatchChannels

    fake_channels = MatchChannels(
        category=fake_category,
        prep_channel=fake_prep,
        team1_vc=fake_team1,
        team2_vc=fake_team2,
        waiting_match_vc=fake_waiting,
    )
    monkeypatch.setattr(match_cog_module, "reserve_match_number", lambda db, *, guild_id: 99)
    monkeypatch.setattr(
        match_cog_module, "create_match_category", AsyncMock(return_value=fake_channels)
    )

    # --- patch build_players to avoid Mongo fetch ---
    players = _make_10_players()
    _patch_build_players(monkeypatch, players)

    # --- patch CaptainDraftSession.run to raise DraftCancelledError ---
    async def _admin_cancel_run(self):
        raise cd_module.DraftCancelledError(reason="admin_cancel")

    monkeypatch.setattr(cd_module.CaptainDraftSession, "run", _admin_cancel_run)

    # --- build a pro-queue doc and guild ---
    queue_doc = _seed_full_queue(bot_module.db, guild_id=42, queue_type="pro")
    members = [_fake_member(i, f"P{i}") for i in range(10)]
    channel = _fake_channel(100)
    guild = _fake_guild(42, members=members, categories=[], channel=channel)
    guild.roles = []
    inter = _fake_interaction(guild, user=members[0])

    cog = MatchCog(bot_module.bot, bot_module.db, rng=random.Random(42))
    result = await cog.on_queue_full(inter, queue_doc, queue_type="pro")

    # Flow must return None (not propagate the exception)
    assert result is None

    # delete_match_category must have been called with the fake category id
    delete_mock.assert_awaited_once()
    assert delete_mock.await_args.kwargs["category_id"] == 7777
