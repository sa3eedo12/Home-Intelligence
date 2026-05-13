from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import fakeredis.aioredis
import pytest

from tools import core


class _FakePool:
    def __init__(self, conn) -> None:  # noqa: ANN001
        self.conn = conn

    def acquire(self):
        return self.conn


class _BaseConn:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):  # noqa: ANN002
        return None

    async def execute(self, query, *args):  # noqa: ANN001, ANN002
        return "UPDATE 1"


@pytest.fixture
def fake_redis(monkeypatch):
    fake = fakeredis.aioredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(core.notify_helper, "_redis_client", lambda: fake)
    return fake


@pytest.mark.asyncio
async def test_chores_add_publishes_when_created_due(fake_redis, monkeypatch) -> None:
    due_at = datetime.now(UTC) - timedelta(minutes=1)

    class _Conn(_BaseConn):
        async def fetchrow(self, _query, title, due, recurrence):
            return {
                "id": 7,
                "title": title,
                "due_at": due,
                "recurrence": recurrence,
                "status": "pending",
            }

    async def _fake_pool():
        return _FakePool(_Conn())

    monkeypatch.setattr(core, "_pool", _fake_pool)
    monkeypatch.setattr(core, "_parse_dt", lambda _raw: due_at)

    result = await core.chores_add("Take bins out", due_at="now")

    assert result["id"] == 7
    rows = await fake_redis.xrange("notify.outbound")
    payload = json.loads(rows[0][1]["payload"])
    assert payload["text"] == "Chore due: Take bins out"
    assert payload["topic"] == "chores.due"
    assert payload["capability"] == "chores_add"


@pytest.mark.asyncio
async def test_chores_list_publishes_due_items(fake_redis, monkeypatch) -> None:
    due_at = datetime.now(UTC) - timedelta(hours=1)

    class _Conn(_BaseConn):
        async def fetch(self, *_args):
            return [
                {
                    "id": 8,
                    "title": "Water plants",
                    "due_at": due_at,
                    "recurrence": None,
                    "status": "pending",
                }
            ]

    async def _fake_pool():
        return _FakePool(_Conn())

    monkeypatch.setattr(core, "_pool", _fake_pool)

    result = await core.chores_list()

    assert result["items"][0]["title"] == "Water plants"
    rows = await fake_redis.xrange("notify.outbound")
    payload = json.loads(rows[0][1]["payload"])
    assert payload["capability"] == "chores_list"
    assert payload["chore_id"] == 8


@pytest.mark.asyncio
async def test_chores_complete_publishes_due_recurrence(fake_redis, monkeypatch) -> None:
    old_due = datetime.now(UTC) - timedelta(days=2)

    class _Conn(_BaseConn):
        async def fetchrow(self, query, *args):  # noqa: ANN001, ANN002
            if "SELECT" in query:
                return {"id": 9, "title": "Clean filters", "due_at": old_due, "recurrence": "daily"}
            return {
                "id": 10,
                "title": args[0],
                "due_at": args[1],
                "recurrence": args[2],
                "status": "pending",
            }

    async def _fake_pool():
        return _FakePool(_Conn())

    monkeypatch.setattr(core, "_pool", _fake_pool)

    result = await core.chores_complete(9)

    assert result["ok"] is True
    rows = await fake_redis.xrange("notify.outbound")
    payload = json.loads(rows[0][1]["payload"])
    assert payload["capability"] == "chores_complete"
    assert payload["chore_id"] == 10
