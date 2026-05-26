"""Tests du cog reglement (rules) + repository acceptation du reglement."""

from unittest.mock import AsyncMock, MagicMock

import discord

from cogs.rules import RulesCog, RulesView, build_rules_embed
from services import repository


# ── Repository ────────────────────────────────────────────────────
def test_has_accepted_rules_false_by_default():
    import bot as bot_module

    assert repository.has_accepted_rules(bot_module.db, 1) is False


def test_record_then_has_accepted_rules_true():
    import bot as bot_module

    repository.record_rules_acceptance(bot_module.db, 1, display_name="Bob")
    assert repository.has_accepted_rules(bot_module.db, 1) is True


def test_record_rules_acceptance_idempotent():
    import bot as bot_module

    repository.record_rules_acceptance(bot_module.db, 1, display_name="Bob")
    repository.record_rules_acceptance(bot_module.db, 1, display_name="Bob2")
    col = repository.get_rules_col(bot_module.db)
    assert col.count_documents({"_id": "1"}) == 1
