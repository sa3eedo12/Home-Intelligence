from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fakeredis.aioredis import FakeRedis

from orchestrator.telegram_bot import _make_text, resolve_member


class DummyMessage:
    def __init__(self, text: str = "hello", chat_id: int = 100) -> None:
        self.text = text
        self.chat_id = chat_id
        self.replies: list[str] = []
        self.reply_kwargs: list[dict] = []

    async def reply_text(self, text: str, **kwargs) -> None:
        self.replies.append(text)
        self.reply_kwargs.append(kwargs)


def _update(chat_id: int = 100, text: str = "hello", first_name: str = "Saeed") -> SimpleNamespace:
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=1, first_name=first_name, username="saeed"),
        effective_chat=SimpleNamespace(id=chat_id),
        message=DummyMessage(text=text, chat_id=chat_id),
    )


@pytest.mark.asyncio
async def test_resolve_member_looks_up_chat_id() -> None:
    graph = SimpleNamespace(
        get_member_by_chat_id=AsyncMock(return_value={"id": 5, "name": "Saeed"})
    )

    member = await resolve_member(100, graph)

    assert member == {"id": 5, "name": "Saeed"}
    graph.get_member_by_chat_id.assert_awaited_once_with(100)


@pytest.mark.asyncio
async def test_known_chat_routes_member_context_to_router() -> None:
    redis = FakeRedis(decode_responses=True)
    graph = SimpleNamespace(
        get_member_by_chat_id=AsyncMock(return_value={"id": 5, "name": "Saeed"})
    )
    store = SimpleNamespace(add_proposal=AsyncMock())
    router = SimpleNamespace(handle=AsyncMock(return_value={"reply": "Hi"}))
    handler = _make_text({1}, router, redis, graph, store)

    update = _update(chat_id=100)
    await handler(update, SimpleNamespace())

    router.handle.assert_awaited_once_with(
        "hello",
        "1",
        member_id=5,
        member_name="Saeed",
    )
    store.add_proposal.assert_not_awaited()
    assert update.message.replies[-1] == "Hi"


@pytest.mark.asyncio
async def test_unknown_chat_creates_household_inference_proposal() -> None:
    redis = FakeRedis(decode_responses=True)
    graph = SimpleNamespace(get_member_by_chat_id=AsyncMock(return_value=None))
    store = SimpleNamespace(add_proposal=AsyncMock(return_value=42))
    router = SimpleNamespace(handle=AsyncMock(return_value={"reply": "Hi"}))
    handler = _make_text({1}, router, redis, graph, store)

    update = _update(chat_id=999, first_name="Alex")
    await handler(update, SimpleNamespace())

    router.handle.assert_awaited_once_with("hello", "1", member_id=None, member_name=None)
    kwargs = store.add_proposal.await_args.kwargs
    assert kwargs["kind"] == "household_inference"
    assert kwargs["delivery_channel"] == "inbox"
    assert "Alex" in kwargs["title"]
    assert "999" in kwargs["rationale"]

    await handler(update, SimpleNamespace())
    assert store.add_proposal.await_count == 1
