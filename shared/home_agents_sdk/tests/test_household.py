from __future__ import annotations

import json
from datetime import UTC, datetime, time
from unittest.mock import AsyncMock, MagicMock

import pytest

from home_agents_sdk.knowledge_graph import KnowledgeGraph


def _pool_with(conn: MagicMock) -> MagicMock:
    manager = MagicMock()
    manager.__aenter__ = AsyncMock(return_value=conn)
    manager.__aexit__ = AsyncMock(return_value=None)
    pool = MagicMock()
    pool.acquire.return_value = manager
    return pool


def _member_row(**overrides):
    row = {
        "id": 1,
        "name": "Saeed",
        "role": "adult",
        "telegram_chat_id": 12345,
        "allergies": ["peanuts"],
        "dietary_restrictions": ["vegetarian"],
        "sleep_time": time(22, 30),
        "wake_time": time(7, 0),
        "attributes": '{"room":"primary"}',
        "created_at": datetime(2026, 1, 1, tzinfo=UTC),
        "updated_at": datetime(2026, 1, 2, tzinfo=UTC),
    }
    row.update(overrides)
    return row


@pytest.mark.asyncio
async def test_list_members_can_exclude_pets() -> None:
    conn = MagicMock()
    conn.fetch = AsyncMock(return_value=[_member_row()])
    graph = KnowledgeGraph(pool=_pool_with(conn))

    members = await graph.list_members(include_pets=False)

    assert members[0]["name"] == "Saeed"
    assert members[0]["sleep_time"] == "22:30"
    assert members[0]["attributes"] == {"room": "primary"}
    assert conn.fetch.await_args.args[1] is False


@pytest.mark.asyncio
async def test_get_member_and_get_member_by_chat_id() -> None:
    conn = MagicMock()
    conn.fetchrow = AsyncMock(side_effect=[_member_row(id=7), _member_row(id=8)])
    graph = KnowledgeGraph(pool=_pool_with(conn))

    by_id = await graph.get_member(7)
    by_chat = await graph.get_member_by_chat_id(12345)

    assert by_id and by_id["id"] == 7
    assert by_chat and by_chat["id"] == 8
    assert conn.fetchrow.await_count == 2
    assert conn.fetchrow.await_args.args[1] == 12345


@pytest.mark.asyncio
async def test_put_member_inserts_and_updates() -> None:
    conn = MagicMock()
    conn.fetchrow = AsyncMock(side_effect=[_member_row(id=1), _member_row(id=1, name="Sam")])
    graph = KnowledgeGraph(pool=_pool_with(conn))

    inserted = await graph.put_member(
        name="Saeed",
        role="adult",
        telegram_chat_id=12345,
        allergies=["peanuts"],
        dietary_restrictions=["vegetarian"],
        sleep_time="22:30",
        wake_time="07:00",
        attributes={"room": "primary"},
    )
    updated = await graph.put_member(
        member_id=1,
        name="Sam",
        role="guest",
        telegram_chat_id=None,
        allergies=[],
        dietary_restrictions=[],
        attributes={"visiting": True},
    )

    assert inserted and inserted["telegram_chat_id"] == 12345
    assert updated and updated["name"] == "Sam"
    insert_args = conn.fetchrow.await_args_list[0].args
    update_args = conn.fetchrow.await_args_list[1].args
    assert insert_args[4] == ["peanuts"]
    assert json.loads(insert_args[8]) == {"room": "primary"}
    assert update_args[1] == 1
    assert json.loads(update_args[9]) == {"visiting": True}


@pytest.mark.asyncio
async def test_forget_member_and_noop_without_pool() -> None:
    conn = MagicMock()
    conn.execute = AsyncMock(return_value="DELETE 1")
    graph = KnowledgeGraph(pool=_pool_with(conn))

    await graph.forget_member(9)

    conn.execute.assert_awaited_once_with("DELETE FROM household_members WHERE id = $1", 9)
    assert await KnowledgeGraph(pool=None).list_members() == []
    assert await KnowledgeGraph(pool=None).get_member(1) is None
    assert await KnowledgeGraph(pool=None).get_member_by_chat_id(1) is None
    assert await KnowledgeGraph(pool=None).put_member(name="x") is None
    await KnowledgeGraph(pool=None).forget_member(1)
