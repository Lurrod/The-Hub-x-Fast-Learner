"""
Configuration pytest globale.

IMPORTANT : ce fichier est charge AVANT les tests, donc avant l'import de bot.py.
On en profite pour :
  1. Patcher pymongo.MongoClient avec mongomock (in-memory, pas besoin de Mongo)
  2. Definir des variables d'environnement bidons pour que bot.py s'importe
  3. Exposer des fixtures dpytest pour les tests d'integration Discord
"""

import os
from unittest.mock import patch

import mongomock
import pymongo
import pytest
import pytest_asyncio


# ── 1. Variables d'environnement bidons (avant import de bot.py) ──
os.environ.setdefault("DISCORD_TOKEN", "test-token-not-real")
os.environ.setdefault("MONGO_URL", "mongodb://localhost:27017")


# ── 2. Patch MongoClient AVANT que bot.py soit importe ────────────
# bot.py fait `client = MongoClient(MONGO_URL)` au top-level, donc on
# doit remplacer MongoClient au niveau du module pymongo lui-meme.
_mongo_patcher = patch.object(pymongo, "MongoClient", mongomock.MongoClient)
_mongo_patcher.start()


# ── 2b. Shim dpytest pour discord.py 2.5+ ─────────────────────────
# discord.py 2.5 passe `colors` (pluriel) a HTTPClient.create_role en
# plus de `color`. dpytest 0.7.0 appelle make_role(**fields), qui ne
# connait pas `colors` -> TypeError. On wrap make_role pour ignorer
# les kwargs inconnus (on garde le mecanisme _get_higher_locs intact
# en ne modifiant PAS create_role lui-meme).
def _install_dpytest_make_role_shim() -> None:
    import inspect
    from discord.ext.test import backend as _dpytest_backend

    original_make_role = _dpytest_backend.make_role
    accepted = set(inspect.signature(original_make_role).parameters)

    def make_role_compat(*args, **kwargs):
        cleaned = {k: v for k, v in kwargs.items() if k in accepted}
        return original_make_role(*args, **cleaned)

    _dpytest_backend.make_role = make_role_compat


_install_dpytest_make_role_shim()


# ── 3. Reset de la base in-memory entre chaque test ───────────────
@pytest.fixture(autouse=True)
def clean_mongo():
    """Vide toutes les collections mongomock avant chaque test."""
    import bot

    for name in bot.db.list_collection_names():
        bot.db.drop_collection(name)
    yield


# ── 4. Fixture dpytest : bot Discord simule ───────────────────────
@pytest_asyncio.fixture
async def discord_bot():
    """
    Bot Discord simule via dpytest.

    Configure automatiquement :
      - 1 guild "TestGuild"
      - 1 channel texte "general"
      - 3 membres (TestUser0, TestUser1, TestUser2)
    """
    import discord.ext.test as dpytest
    import bot as bot_module

    # discord.py 2.x : le loop n'est pas defini avant setup_hook.
    # On le force ici pour que dpytest puisse dispatcher des events.
    await bot_module.bot._async_setup_hook()
    # _async_setup_hook ne declenche pas setup_hook lui-meme (login
    # uniquement). En tests, on appelle setup_hook explicitement pour
    # charger tous les cogs (sinon /commande et !commande sont
    # introuvables pour dpytest car les @app_commands.command et
    # @commands.command vivent maintenant dans des cogs).
    if not bot_module.bot.cogs:
        await bot_module.setup_hook()

    dpytest.configure(
        bot_module.bot,
        guilds=1,
        text_channels=1,
        voice_channels=0,
        members=3,
    )
    yield bot_module.bot
    await dpytest.empty_queue()


# ── 5. Helpers reutilisables ──────────────────────────────────────
@pytest.fixture
def fake_member(discord_bot):
    """Retourne le 1er membre du guild de test."""
    import discord.ext.test as dpytest

    config = dpytest.get_config()
    return config.members[0]


@pytest.fixture
def fake_guild(discord_bot):
    import discord.ext.test as dpytest

    config = dpytest.get_config()
    return config.guilds[0]


@pytest.fixture
def mongo_db():
    """In-memory mongomock database, isolated per test."""
    return mongomock.MongoClient(tz_aware=True).db
