from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from orchestrator.data_science.reembed import ReembedJob
from tests.data_science_fakes import FakePool


class FakeConn:
    def __init__(self) -> None:
        self.fetch = AsyncMock(
            side_effect=[
                [
                    {
                        "id": 1,
                        "ts": datetime(2026, 5, 12, 7, tzinfo=UTC),
                        "agent": "home_automation",
                        "capability": "turn_on",
                        "summary": "Kitchen lights on",
                        "payload": {"room": "kitchen"},
                        "embedding_model": "old",
                    },
                    {
                        "id": 2,
                        "ts": datetime(2026, 5, 12, 8, tzinfo=UTC),
                        "agent": "system_health",
                        "capability": "check",
                        "summary": "Disk ok",
                        "payload": {},
                        "embedding_model": None,
                    },
                ],
                [],
            ]
        )
        self.fetchval = AsyncMock(return_value=3)
        self.execute = AsyncMock(return_value="UPDATE 2")


@pytest.mark.asyncio
async def test_reembed_batches_stale_events_and_marks_model() -> None:
    conn = FakeConn()
    qdrant = SimpleNamespace(
        get_collection=AsyncMock(return_value={}),
        upsert=AsyncMock(return_value=None),
    )
    embedder = SimpleNamespace(npu_model="bge-m3-int8", embed=AsyncMock(return_value=[0.1, 0.2]))
    event_log = SimpleNamespace(record_event=AsyncMock())

    result = await ReembedJob(
        FakePool(conn), qdrant, embedder, batch_size=10, event_log_store=event_log
    ).run()

    assert result["processed"] == 2
    assert result["skipped"] == 3
    assert result["errors"] == 0
    assert result["current_model"] == "bge-m3-int8"
    assert embedder.embed.await_count == 2
    qdrant.upsert.assert_awaited_once()
    conn.execute.assert_awaited_once()
    assert conn.execute.await_args.args[1] == "bge-m3-int8"
    assert conn.execute.await_args.args[2] == [1, 2]
    event_log.record_event.assert_awaited_once()


@pytest.mark.asyncio
async def test_reembed_skips_when_qdrant_missing() -> None:
    embedder = SimpleNamespace(npu_model="bge-m3-int8", embed=AsyncMock())
    result = await ReembedJob(FakePool(FakeConn()), None, embedder).run()

    assert result["status"] == "skipped"
    assert result["reason"] == "semantic_index_unavailable"
    assert result["processed"] == 0
