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
