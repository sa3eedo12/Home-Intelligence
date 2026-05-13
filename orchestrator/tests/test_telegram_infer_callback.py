from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from orchestrator.telegram_bot import _handle_infer_callback


@pytest.mark.asyncio
async def test_infer_callback_dispatches_confirm() -> None:
    query = MagicMock()
    query.edit_message_text = AsyncMock()
    update = MagicMock()
    update.effective_chat = MagicMock()
    update.effective_chat.id = 12345
    router = MagicMock()
    router.dispatch = AsyncMock(return_value={"ok": True, "result": {"ok": True}})

    await _handle_infer_callback(update, query, ["infer", "42", "confirmed"], router)

    router.dispatch.assert_awaited_once()
    args = router.dispatch.call_args.args
    assert args[0] == "personal_assistant"
    assert args[1] == "confirm_auto_inference"
    assert args[2] == {"auto_inference_id": 42, "status": "confirmed", "chat_id": 12345}
    query.edit_message_text.assert_awaited_once_with("✅ Logged.")


@pytest.mark.asyncio
async def test_infer_callback_dispatches_rejected() -> None:
    query = MagicMock()
    query.edit_message_text = AsyncMock()
    update = MagicMock()
    update.effective_chat = MagicMock()
    update.effective_chat.id = 99
    router = MagicMock()
    router.dispatch = AsyncMock(return_value={"ok": True, "result": {"ok": True}})

    await _handle_infer_callback(update, query, ["infer", "42", "rejected"], router)

    assert router.dispatch.call_args.args[2]["status"] == "rejected"
    query.edit_message_text.assert_awaited_once_with("👌 Ignored.")


@pytest.mark.asyncio
async def test_infer_callback_handles_bad_status() -> None:
    query = MagicMock()
    query.edit_message_text = AsyncMock()
    update = MagicMock()
    update.effective_chat = MagicMock()
    update.effective_chat.id = 1
    router = MagicMock()
    router.dispatch = AsyncMock()

    await _handle_infer_callback(update, query, ["infer", "42", "maybe"], router)

    router.dispatch.assert_not_called()
    assert "bad status" in query.edit_message_text.call_args.args[0]
