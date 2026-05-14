from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from orchestrator.notify import send_morning_brief


@pytest.mark.asyncio
async def test_send_morning_brief_formats_markdown_under_limit() -> None:
    bot = SimpleNamespace(send_message=AsyncMock())
    tg_app = SimpleNamespace(bot=bot)
    brief = {
        "summary": "Reflection found one improvement.",
        "body_json": {
            "yesterday": [{"summary": "Washer completed"}],
            "questions_for_you": [{"title": "Confirm wake time"}],
            "suggestions_for_me": [{"title": "Clean stale proposal"}],
            "code_wishlist": [{"title": "Add retry tests"}],
        },
    }

    await send_morning_brief(tg_app, brief, 123)

    kwargs = bot.send_message.await_args.kwargs
    assert kwargs["chat_id"] == 123
    assert kwargs["parse_mode"] == "Markdown"
    assert "Morning Brief" in kwargs["text"]
    assert len(kwargs["text"]) <= 4000


@pytest.mark.asyncio
async def test_brief_surfaces_what_i_learned_yesterday() -> None:
    bot = SimpleNamespace(send_message=AsyncMock())
    tg_app = SimpleNamespace(bot=bot)
    brief = {
        "summary": "Reflection found 5 profile gaps.",
        "body_json": {
            "questions_for_you": [{"title": "Confirm wake time"}],
            "evidence": {
                "events": [
                    {"capability": "appliance.cycle_completed", "summary": "Washer done"},
                    {"capability": "appliance.cycle_completed", "summary": "Washer done 2"},
                    {"capability": "cleaning.completed", "summary": "Roomba done"},
                    {"capability": "coffee.brewed", "summary": "Coffee"},
                    {"capability": "presence.changed", "summary": "..."},
                    {"capability": "presence.changed", "summary": "..."},
                    {"capability": "presence.changed", "summary": "..."},
                    {"capability": "presence.changed", "summary": "..."},
                ],
            },
        },
    }
    await send_morning_brief(tg_app, brief, 123)
    text = bot.send_message.await_args.kwargs["text"]
    assert "What I learned about you" in text
    assert "Washer cycle ran 2× yesterday" in text
    assert "Vacuum cleaned 1× yesterday" in text
    assert "Coffee brewed 1× yesterday" in text
    assert "4 'home/away' events" in text


@pytest.mark.asyncio
async def test_brief_surfaces_anomalies() -> None:
    bot = SimpleNamespace(send_message=AsyncMock())
    tg_app = SimpleNamespace(bot=bot)
    brief = {
        "summary": "Reflection complete.",
        "body_json": {
            "evidence": {
                "events": [
                    {
                        "capability": "anomaly.detected",
                        "summary": "🧹 Vacuum hasn't run in 8 days. Want me to remind you?",
                        "payload": {"anomaly_type": "vacuum_overdue"},
                    },
                    {
                        "capability": "anomaly.detected",
                        "summary": "🌙 No sleep summary recorded for 3 night(s).",
                        "payload": {"anomaly_type": "sleep_summary_missing"},
                    },
                ],
            },
        },
    }
    await send_morning_brief(tg_app, brief, 123)
    text = bot.send_message.await_args.kwargs["text"]
    assert "What I noticed" in text
    assert "Vacuum hasn't run" in text
    assert "sleep summary" in text


@pytest.mark.asyncio
async def test_brief_omits_empty_sections() -> None:
    bot = SimpleNamespace(send_message=AsyncMock())
    tg_app = SimpleNamespace(bot=bot)
    brief = {"summary": "Nothing happened.", "body_json": {}}
    await send_morning_brief(tg_app, brief, 123)
    text = bot.send_message.await_args.kwargs["text"]
    # No "Yesterday" / "Code wishlist" / "What I learned" / "What I noticed"
    # should appear when their corresponding data is empty.
    assert "What I learned" not in text
    assert "What I noticed" not in text
    assert "Yesterday" not in text
    assert "Code wishlist" not in text
    assert "I noticed" not in text  # cross-source correlations section


@pytest.mark.asyncio
async def test_brief_renders_correlations_section() -> None:
    bot = SimpleNamespace(send_message=AsyncMock())
    tg_app = SimpleNamespace(bot=bot)
    brief = {
        "summary": "Reflection complete.",
        "body_json": {
            "correlations": [
                {
                    "id": "resting_hr_drift",
                    "headline": "Resting HR trended up 7 bpm this week.",
                    "detail": "Could indicate illness or sleep debt.",
                    "confidence": 0.85,
                },
                {
                    "id": "step_trend",
                    "headline": "Daily steps ↓ 30% this week.",
                    "detail": None,
                    "confidence": 0.75,
                },
            ],
        },
    }
    await send_morning_brief(tg_app, brief, 123)
    text = bot.send_message.await_args.kwargs["text"]
    assert "🔍 I noticed" in text
    assert "Resting HR trended up 7 bpm" in text
    assert "Could indicate illness" in text  # detail is rendered when present
    assert "Daily steps ↓ 30%" in text


@pytest.mark.asyncio
async def test_brief_correlations_section_skips_malformed_entries() -> None:
    bot = SimpleNamespace(send_message=AsyncMock())
    tg_app = SimpleNamespace(bot=bot)
    brief = {
        "summary": "Reflection complete.",
        "body_json": {
            "correlations": [
                {"headline": ""},  # empty headline → skipped
                "not a dict",      # wrong type → skipped
                {"headline": "Real insight here.", "confidence": 0.7},
            ],
        },
    }
    await send_morning_brief(tg_app, brief, 123)
    text = bot.send_message.await_args.kwargs["text"]
    assert "Real insight here" in text
    # Should still render the section header since at least one entry was valid
    assert "🔍 I noticed" in text
