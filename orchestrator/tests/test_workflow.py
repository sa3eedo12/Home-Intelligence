from __future__ import annotations

import json
import uuid

import pytest

from orchestrator.workflow import WorkflowEngine


class InMemoryPool:
    """In-memory asyncpg pool mock that stores data in a dict."""

    def __init__(self) -> None:
        self._rows: dict[str, dict] = {}

    def acquire(self):
        return self

    def transaction(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def execute(self, query: str, *args):
        q = query.strip().upper()
        if q.startswith("INSERT INTO WORKFLOWS"):
            wf_id, status, payload = args[0], args[1], args[2]
            self._rows[str(wf_id)] = {"id": str(wf_id), "status": status, "payload": payload}
        elif q.startswith("UPDATE WORKFLOWS SET STATUS = 'RUNNING'"):
            wf_id = args[0]
            if str(wf_id) in self._rows:
                self._rows[str(wf_id)]["status"] = "running"
        elif q.startswith("UPDATE WORKFLOWS SET STATUS = 'DONE'"):
            payload, wf_id = args[0], args[1]
            if str(wf_id) in self._rows:
                self._rows[str(wf_id)]["status"] = "done"
                self._rows[str(wf_id)]["payload"] = payload
        elif q.startswith("UPDATE WORKFLOWS SET STATUS = 'FAILED'"):
            payload, wf_id = args[0], args[1]
            if str(wf_id) in self._rows:
                self._rows[str(wf_id)]["status"] = "failed"
                self._rows[str(wf_id)]["payload"] = payload

    async def fetchrow(self, query: str, *args):
        wf_id = str(args[0])
        row = self._rows.get(wf_id)
        if row is None:
            return None
        return {"payload": row["payload"], "status": row["status"], "id": row["id"]}

    async def fetch(self, query: str, *args):
        return [
            {"id": r["id"], "status": r["status"], "payload": r["payload"]}
            for r in self._rows.values()
            if r["status"] in ("running", "awaiting_user")
        ]


@pytest.fixture
def pool():
    return InMemoryPool()


@pytest.fixture
def engine(pool):
    return WorkflowEngine(pool)


@pytest.mark.asyncio
async def test_start_returns_uuid(engine):
    wf_id = await engine.start({"action": "test"})
    assert uuid.UUID(wf_id)


@pytest.mark.asyncio
async def test_start_status_running(engine):
    wf_id = await engine.start({"action": "test"})
    rows = await engine.list_pending()
    row = next((r for r in rows if r["id"] == wf_id), None)
    assert row is not None
    assert row["status"] == "running"


@pytest.mark.asyncio
async def test_start_with_confirmation(engine):
    wf_id = await engine.start({"action": "unlock", "needs_confirmation": True})
    rows = await engine.list_pending()
    row = next((r for r in rows if r["id"] == wf_id), None)
    assert row is not None
    assert row["status"] == "awaiting_user"


@pytest.mark.asyncio
async def test_resume(engine, pool):
    wf_id = await engine.start({"action": "test", "needs_confirmation": True})
    payload = await engine.resume(wf_id, {"action": "confirm"})
    assert isinstance(payload, dict)
    assert pool._rows[wf_id]["status"] == "running"


@pytest.mark.asyncio
async def test_mark_done(engine, pool):
    wf_id = await engine.start({"action": "test"})
    await engine.mark_done(wf_id, {"output": "success"})
    assert pool._rows[wf_id]["status"] == "done"
    stored = json.loads(pool._rows[wf_id]["payload"])
    assert stored["result"] == {"output": "success"}


@pytest.mark.asyncio
async def test_mark_failed(engine, pool):
    wf_id = await engine.start({"action": "test"})
    await engine.mark_failed(wf_id, "something went wrong")
    assert pool._rows[wf_id]["status"] == "failed"
    stored = json.loads(pool._rows[wf_id]["payload"])
    assert stored["error"] == "something went wrong"


@pytest.mark.asyncio
async def test_list_pending(engine):
    wf1 = await engine.start({"action": "a"})
    wf2 = await engine.start({"action": "b", "needs_confirmation": True})
    wf3 = await engine.start({"action": "c"})
    await engine.mark_done(wf3, "done")
    pending = await engine.list_pending()
    ids = [r["id"] for r in pending]
    assert wf1 in ids
    assert wf2 in ids
    assert wf3 not in ids
