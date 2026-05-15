"""Tests for the presence-override Telegram intent matcher."""
from __future__ import annotations

import pytest
from fakeredis.aioredis import FakeRedis

from orchestrator.telegram_bot import _presence_intent

# ── _presence_intent ────────────────────────────────────────────────────


def test_away_phrases_match() -> None:
    for phrase in [
        "I'm not home",
        "im not home",
        "I am not home",
        "I'm out",
        "I left",
        "I've left",
        "I'm gone",
        "I'm away",
        "I'm not home now",
    ]:
        assert _presence_intent(phrase) == "not_home", phrase


def test_home_phrases_match() -> None:
    for phrase in [
        "I'm home",
        "im home",
        "I am home",
        "I'm back",
        "I've arrived",
        "I'm in",
    ]:
        assert _presence_intent(phrase) == "home", phrase


def test_trailing_punctuation_tolerated() -> None:
    assert _presence_intent("I'm home.") == "home"
    assert _presence_intent("I'm not home!") == "not_home"


def test_partial_match_does_not_trigger() -> None:
    """Whole-line match only — don't fire on phrases that contain the
    keyword but mean something else."""
    assert _presence_intent("I'm not home yet but on my way back") is None
    assert _presence_intent("I think I'm home") is None
    assert _presence_intent("Could you check if I'm home?") is None


def test_unrelated_text_returns_none() -> None:
    assert _presence_intent("Turn off the lights") is None
    assert _presence_intent("") is None
    assert _presence_intent("hello") is None


# ── _people_home applies overrides ──────────────────────────────────────


@pytest.mark.asyncio
async def test_people_home_respects_not_home_override() -> None:
    """REGRESSION: 'I'm not home' Telegram message used to be a no-op.
    Now it sets a redis key that _people_home consults."""
    from types import SimpleNamespace

    from orchestrator.app import _people_home

    redis = FakeRedis(decode_responses=True)
    await redis.set("policy:override:presence:Saeed", "not_home", ex=3600)

    # Build a minimal app with no pool but the redis + ha env unset
    app = SimpleNamespace(
        state=SimpleNamespace(pool=None, redis=redis)
    )

    # Without a pool, _people_home returns []. Even so, an override saying
    # 'home' should add the member; 'not_home' should remove them.
    home = await _people_home(app)
    assert "Saeed" not in home


@pytest.mark.asyncio
async def test_people_home_adds_member_with_home_override() -> None:
    from types import SimpleNamespace

    from orchestrator.app import _people_home

    redis = FakeRedis(decode_responses=True)
    await redis.set("policy:override:presence:Jude", "home", ex=3600)
    app = SimpleNamespace(state=SimpleNamespace(pool=None, redis=redis))

    home = await _people_home(app)
    assert "Jude" in home


@pytest.mark.asyncio
async def test_people_home_no_override_unchanged_behavior() -> None:
    from types import SimpleNamespace

    from orchestrator.app import _people_home

    redis = FakeRedis(decode_responses=True)
    app = SimpleNamespace(state=SimpleNamespace(pool=None, redis=redis))

    home = await _people_home(app)
    assert home == []
