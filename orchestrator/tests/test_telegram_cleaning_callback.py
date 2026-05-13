"""Tests for the cleaning-run callback path."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from orchestrator.telegram_bot import _handle_clean_callback


@pytest.mark.asyncio
async def test_clean_callback_dispatches_confirm() -> None:
    query = MagicMock()
    query.edit_message_text = AsyncMock()
    update = MagicMock()
    update.effective_chat = MagicMock()
    update.effective_chat.id = 12345
    router = MagicMock()
    router.dispatch = AsyncMock(return_value={"ok": True, "result": {"ok": True}})

    await _handle_clean_callback(update, query, ["clean", "42", "partial"], router)

    router.dispatch.assert_awaited_once()
    args = router.dispatch.call_args.args
    assert args[0] == "household_ops"
    assert args[1] == "confirm_cleaning_run"
    assert args[2] == {"cleaning_run_id": 42, "status": "partial", "chat_id": 12345}
    query.edit_message_text.assert_awaited_once_with("✅ Saved: partial")


@pytest.mark.asyncio
async def test_clean_callback_skip_short_circuits() -> None:
    query = MagicMock()
    query.edit_message_text = AsyncMock()
    update = MagicMock()
    update.effective_chat = MagicMock()
    update.effective_chat.id = 99
    router = MagicMock()
    router.dispatch = AsyncMock()

    await _handle_clean_callback(update, query, ["clean", "42", "_skip"], router)

    router.dispatch.assert_not_called()
    query.edit_message_text.assert_awaited_once_with("👌 Skipped.")


@pytest.mark.asyncio
async def test_clean_callback_handles_bad_id() -> None:
    query = MagicMock()
    query.edit_message_text = AsyncMock()
    update = MagicMock()
    update.effective_chat = MagicMock()
    update.effective_chat.id = 1
    router = MagicMock()
    router.dispatch = AsyncMock()

    await _handle_clean_callback(update, query, ["clean", "not-a-number", "partial"], router)

    router.dispatch.assert_not_called()
    msg = query.edit_message_text.call_args.args[0]
    assert "bad id" in msg


@pytest.mark.asyncio
async def test_clean_callback_surfaces_dispatch_failure() -> None:
    query = MagicMock()
    query.edit_message_text = AsyncMock()
    update = MagicMock()
    update.effective_chat = MagicMock()
    update.effective_chat.id = 1
    router = MagicMock()
    router.dispatch = AsyncMock(
        return_value={"ok": False, "result": {"ok": False, "error": "cleaning_run not found"}}
    )

    await _handle_clean_callback(update, query, ["clean", "999", "partial"], router)

    msg = query.edit_message_text.call_args.args[0]
    assert "cleaning_run not found" in msg
