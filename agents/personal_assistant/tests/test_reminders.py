from __future__ import annotations

from datetime import UTC, datetime

import pytest

from tools import core


class _FakeConn:
    def __init__(self) -> None:
        self.rows = [{"id": 1, "text": "renew", "due_at": datetime.now(UTC), "status": "pending"}]

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def fetchrow(self, query, *args):
        if "INSERT INTO reminders" in query:
            return {"id": 1, "text": args[1], "due_at": args[2], "status": "pending"}
        return None

    async def fetch(self, query, *args):
        if "FROM reminders" in query:
            return self.rows
        return []

    async def execute(self, query, *args):
        return "UPDATE 1"


class _FakePool:
    def __init__(self) -> None:
        self.conn = _FakeConn()

    def acquire(self):
        return self.conn


@pytest.mark.asyncio
async def test_add_list_cancel_lifecycle(monkeypatch) -> None:
    async def _fake_pool():
        return _FakePool()

    monkeypatch.setattr(core, "_pool", _fake_pool)

    added = await core.add_reminder("renew insurance", "in 2 weeks")
    assert added["status"] == "pending"

    listed = await core.list_reminders()
    assert listed["items"][0]["id"] == 1

    cancelled = await core.cancel_reminder(1)
    assert cancelled["ok"] is True
